"""MCP server management API — CRUD + test endpoints."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.audit.runtime import log_integration_event
from src.tools.mcp_manager import mcp_manager

router = APIRouter()

_ENV_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")
_VAULT_PLACEHOLDER_RE = re.compile(r"\$\{vault:([A-Za-z0-9_.:-]+)\}")
_MANAGED_SENSITIVE_HEADER_VALUE_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9_-]*\s+)?(?:\$\{\w+\}|\$\{vault:[A-Za-z0-9_.:-]+\})$"
)
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-auth-token",
}


def _packaged_server_detail(name: str, config: dict[str, object], *, action: str) -> str:
    extension_label = (
        str(config.get("extension_display_name"))
        if isinstance(config.get("extension_display_name"), str) and str(config.get("extension_display_name")).strip()
        else str(config.get("extension_id") or "extension package")
    )
    return (
        f"Server '{name}' is managed by {extension_label}; "
        f"use the extension connector lifecycle instead of raw MCP {action}."
    )


class AddServerRequest(BaseModel):
    name: str
    url: str
    description: str = ""
    enabled: bool = True
    headers: dict[str, str] | None = None
    auth_hint: str = ""


class UpdateServerRequest(BaseModel):
    enabled: bool | None = None
    url: str | None = None
    description: str | None = None
    headers: dict[str, str] | None = None


class SetTokenRequest(BaseModel):
    token: str


def _is_sensitive_header_name(name: str) -> bool:
    normalized = name.strip().lower()
    return (
        normalized in _SENSITIVE_HEADER_NAMES
        or "token" in normalized
        or "secret" in normalized
        or "api-key" in normalized
    )


def _uses_managed_credential_placeholder(value: str) -> bool:
    return bool(_MANAGED_SENSITIVE_HEADER_VALUE_RE.fullmatch(value.strip()))


def _validate_header_credentials(headers: dict[str, str] | None) -> list[str]:
    if headers is None:
        return []
    issues: list[str] = []
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if not _is_sensitive_header_name(key):
            continue
        if _uses_managed_credential_placeholder(value):
            continue
        issues.append(
            f"Sensitive header '{key}' must use ${{ENV_VAR}} or ${{vault:key}}; "
            "use the token endpoint for bearer-token setup."
        )
    return issues


@router.get("/mcp/servers")
async def list_servers():
    """List all configured MCP servers with status."""
    return {"servers": mcp_manager.get_config()}


@router.post("/mcp/servers/validate")
async def validate_server(req: AddServerRequest):
    issues: list[str] = []
    warnings: list[str] = []
    url = (req.url or "").strip()
    if not req.name.strip():
        issues.append("Server name is required")
    if not url:
        issues.append("Server URL is required")
    parsed = urlparse(url) if url else None
    if parsed and parsed.scheme not in {"http", "https"}:
        issues.append("Server URL must use http or https")
    if parsed and not parsed.netloc:
        issues.append("Server URL must include a host")
    if req.headers is not None:
        for key, value in req.headers.items():
            if not isinstance(key, str) or not key.strip():
                issues.append("Header names must be non-empty strings")
                break
            if not isinstance(value, str):
                issues.append("Header values must be strings")
                break
    issues.extend(_validate_header_credentials(req.headers))
    try:
        missing_vars, missing_vault_keys, _ = mcp_manager.inspect_headers(req.headers)
    except Exception as exc:
        issues.append(f"Credential inspection failed: {exc}")
        missing_vars = []
        missing_vault_keys = []
    if req.name in mcp_manager._config:
        warnings.append("Server already exists and will need update instead of create")
    if missing_vars:
        warnings.append("Environment variables are still required before the server can connect")
    if missing_vault_keys:
        warnings.append("Vault secrets are still required before the server can connect")
    return {
        "valid": not issues,
        "name": req.name.strip(),
        "url": url,
        "status": (
            "invalid"
            if issues
            else ("auth_required" if missing_vars or missing_vault_keys else "ready_to_test")
        ),
        "issues": issues,
        "warnings": warnings,
        "missing_env_vars": missing_vars,
        "missing_vault_keys": missing_vault_keys,
        "enabled": req.enabled,
        "description": req.description,
        "has_headers": bool(req.headers),
        "auth_hint": req.auth_hint,
        "existing": req.name in mcp_manager._config,
    }


@router.post("/mcp/servers", status_code=201)
async def add_server(req: AddServerRequest):
    """Add a new MCP server."""
    if req.name in mcp_manager._config:
        raise HTTPException(status_code=409, detail=f"Server '{req.name}' already exists")
    header_issues = _validate_header_credentials(req.headers)
    if header_issues:
        raise HTTPException(status_code=400, detail=" ".join(header_issues))
    mcp_manager.add_server(
        name=req.name,
        url=req.url,
        description=req.description,
        enabled=req.enabled,
        headers=req.headers,
        auth_hint=req.auth_hint,
    )
    return {"status": "created", "name": req.name}


@router.put("/mcp/servers/{name}")
async def update_server(name: str, req: UpdateServerRequest):
    """Update an MCP server (enable/disable, etc.)."""
    config = mcp_manager._config.get(name)
    if isinstance(config, dict) and config.get("source") == "extension":
        raise HTTPException(status_code=409, detail=_packaged_server_detail(name, config, action="updates"))
    updates = req.model_dump(exclude_none=True)
    header_issues = _validate_header_credentials(
        updates.get("headers") if isinstance(updates.get("headers"), dict) else updates.get("headers")
    )
    if header_issues:
        raise HTTPException(status_code=400, detail=" ".join(header_issues))
    if not mcp_manager.update_server(name, **updates):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"status": "updated", "name": name}


@router.delete("/mcp/servers/{name}")
async def remove_server(name: str):
    """Remove an MCP server from config."""
    config = mcp_manager._config.get(name)
    if isinstance(config, dict) and config.get("source") == "extension":
        raise HTTPException(status_code=409, detail=_packaged_server_detail(name, config, action="removal"))
    if not mcp_manager.remove_server(name):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"status": "removed", "name": name}


@router.post("/mcp/servers/{name}/token")
async def set_server_token(name: str, req: SetTokenRequest):
    """Set auth token for an MCP server. Reconnects if enabled."""
    config = mcp_manager._config.get(name)
    if isinstance(config, dict) and config.get("source") == "extension":
        raise HTTPException(status_code=409, detail=_packaged_server_detail(name, config, action="token updates"))
    if not mcp_manager.set_token(name, req.token):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    configs = mcp_manager.get_config()
    entry = next((c for c in configs if c["name"] == name), None)
    return {"status": "updated", "server": entry}


@router.post("/mcp/servers/{name}/test")
async def test_server(name: str):
    """Test connection to an MCP server. Connects, lists tools, disconnects."""
    config = mcp_manager._config.get(name)
    if not config:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    if config.get("source") == "extension":
        raise HTTPException(status_code=409, detail=_packaged_server_detail(name, config, action="tests"))
    url = config["url"]

    raw_headers = config.get("headers")
    try:
        resolved_headers, missing_vars, missing_vault_keys, credential_sources = mcp_manager.resolve_headers(raw_headers)
    except Exception as exc:
        await log_integration_event(
            integration_type="mcp_test",
            name=name,
            outcome="auth_required",
            details={
                "url": url,
                "status": "credential_resolution_failed",
                "error": str(exc),
            },
        )
        return {
            "status": "auth_required",
            "message": f"Credential resolution failed: {exc}",
            "missing_env_vars": [],
            "missing_vault_keys": [],
        }
    if missing_vars or missing_vault_keys:
        message_parts: list[str] = []
        if missing_vars:
            message_parts.append(f"Missing environment variables: {', '.join(missing_vars)}")
        if missing_vault_keys:
            message_parts.append(f"Missing vault secrets: {', '.join(missing_vault_keys)}")
        await log_integration_event(
            integration_type="mcp_test",
            name=name,
            outcome="auth_required",
            details={
                "url": url,
                "missing_env_vars": missing_vars,
                "missing_vault_keys": missing_vault_keys,
                "credential_sources": credential_sources,
            },
        )
        return {
            "status": "auth_required",
            "message": "; ".join(message_parts),
            "missing_env_vars": missing_vars,
            "missing_vault_keys": missing_vault_keys,
        }

    try:
        from smolagents import MCPClient
        params: dict = {"url": url, "transport": "streamable-http"}
        if resolved_headers:
            params["headers"] = resolved_headers
        client = MCPClient(params, structured_output=False)
        tools = client.get_tools()
        tool_names = [t.name for t in tools]
        client.disconnect()
        await log_integration_event(
            integration_type="mcp_test",
            name=name,
            outcome="succeeded",
            details={
                "url": url,
                "tool_count": len(tools),
                "tool_names": tool_names,
                "used_headers": bool(resolved_headers),
                "credential_sources": credential_sources,
            },
        )
        return {"status": "ok", "tool_count": len(tools), "tools": tool_names}
    except Exception as e:
        exc_str = str(e).lower()
        if any(kw in exc_str for kw in ("401", "403", "unauthorized", "forbidden")):
            await log_integration_event(
                integration_type="mcp_test",
                name=name,
                outcome="failed",
                details={
                    "url": url,
                    "status": "auth_failed",
                    "error": str(e),
                },
            )
            return {"status": "auth_failed", "message": f"Authentication failed: {e}"}
        await log_integration_event(
            integration_type="mcp_test",
            name=name,
            outcome="failed",
            details={
                "url": url,
                "status": "connection_failed",
                "error": str(e),
            },
        )
        raise HTTPException(status_code=502, detail=f"Connection failed: {e}")
