"""MCP server management API — CRUD + test endpoints."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.audit.runtime import log_integration_event
from src.tools.mcp_manager import mcp_manager

router = APIRouter()


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
    missing_vars = mcp_manager._check_unresolved_vars(req.headers)
    if req.name in mcp_manager._config:
        warnings.append("Server already exists and will need update instead of create")
    if missing_vars:
        warnings.append("Environment variables are still required before the server can connect")
    return {
        "valid": not issues,
        "name": req.name.strip(),
        "url": url,
        "status": (
            "invalid"
            if issues
            else ("auth_required" if missing_vars else "ready_to_test")
        ),
        "issues": issues,
        "warnings": warnings,
        "missing_env_vars": missing_vars,
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
    updates = req.model_dump(exclude_none=True)
    if not mcp_manager.update_server(name, **updates):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"status": "updated", "name": name}


@router.delete("/mcp/servers/{name}")
async def remove_server(name: str):
    """Remove an MCP server from config."""
    if not mcp_manager.remove_server(name):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"status": "removed", "name": name}


@router.post("/mcp/servers/{name}/token")
async def set_server_token(name: str, req: SetTokenRequest):
    """Set auth token for an MCP server. Reconnects if enabled."""
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
    url = config["url"]

    # Check for unresolved env vars before attempting connection
    raw_headers = config.get("headers")
    missing_vars = mcp_manager._check_unresolved_vars(raw_headers)
    if missing_vars:
        await log_integration_event(
            integration_type="mcp_test",
            name=name,
            outcome="auth_required",
            details={
                "url": url,
                "missing_env_vars": missing_vars,
            },
        )
        return {
            "status": "auth_required",
            "message": f"Missing environment variables: {', '.join(missing_vars)}",
            "missing_env_vars": missing_vars,
        }

    try:
        from smolagents import MCPClient
        params: dict = {"url": url, "transport": "streamable-http"}
        if raw_headers:
            params["headers"] = {
                k: mcp_manager._resolve_env_vars(v) for k, v in raw_headers.items()
            }
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
                "used_headers": bool(raw_headers),
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
