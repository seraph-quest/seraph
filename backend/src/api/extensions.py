"""Extension lifecycle API."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from smolagents import MCPClient

from config.settings import settings
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.audit.runtime import log_integration_event
from src.extensions.channel_routing import (
    SUPPORTED_CHANNEL_ROUTE_TRANSPORTS,
    list_channel_route_bindings,
    set_channel_route_binding,
)
from src.extensions.channels import select_active_channel_adapters
from src.extensions.lifecycle import (
    configure_extension,
    disable_extension,
    enable_extension,
    get_extension,
    get_extension_connector,
    get_extension_source,
    install_extension_path,
    list_extension_connectors,
    list_extensions,
    remove_extension,
    save_extension_source,
    set_extension_connector_enabled,
    update_extension_path,
    validate_extension_path,
)
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.scaffold import scaffold_extension_package
from src.extensions.state import (
    connector_enabled_overrides,
    load_extension_state_payload,
    save_extension_state_payload,
)
from src.tools.mcp_manager import mcp_manager

router = APIRouter()

_BUILTIN_CHANNEL_ADAPTERS = (
    {
        "extension_id": "seraph.builtin-channel-adapters",
        "name": "websocket",
        "transport": "websocket",
        "reference": "builtin:websocket",
    },
    {
        "extension_id": "seraph.builtin-channel-adapters",
        "name": "native-notification",
        "transport": "native_notification",
        "reference": "builtin:native_notification",
    },
)
_REDACTED_CONFIG_SENTINEL = "__SERAPH_STORED_SECRET__"


class ExtensionPathRequest(BaseModel):
    path: str


class ExtensionScaffoldRequest(BaseModel):
    package_name: str
    display_name: str
    extension_id: str | None = None
    kind: str = "capability-pack"
    contributions: list[str] = Field(default_factory=lambda: ["skills"])


class ExtensionConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class ExtensionSourceSaveRequest(BaseModel):
    reference: str
    content: str


class ExtensionConnectorTestRequest(BaseModel):
    reference: str


class ExtensionConnectorToggleRequest(BaseModel):
    reference: str
    enabled: bool


class ChannelRoutingBindingUpdateRequest(BaseModel):
    primary_transport: str
    fallback_transport: str | None = None


class ChannelRoutingUpdateRequest(BaseModel):
    bindings: dict[str, ChannelRoutingBindingUpdateRequest] = Field(default_factory=dict)


def _active_channel_adapter_payloads(state_payload: dict[str, Any]) -> list[dict[str, Any]]:
    state_by_id = state_payload.get("extensions")
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    contributions = snapshot.list_contributions("channel_adapters")
    adapters = select_active_channel_adapters(
        contributions,
        enabled_overrides=connector_enabled_overrides(state_by_id if isinstance(state_by_id, dict) else None),
    )
    payloads = [
        {
            "extension_id": item.extension_id,
            "name": item.name,
            "transport": item.transport,
            "reference": item.reference,
        }
        for item in adapters
    ]
    active_transports = {
        str(item.get("transport"))
        for item in payloads
        if isinstance(item.get("transport"), str) and str(item.get("transport")).strip()
    }
    for builtin in _BUILTIN_CHANNEL_ADAPTERS:
        if builtin["transport"] in active_transports:
            continue
        payloads.append(dict(builtin))
    return payloads


def _channel_routing_response(state_payload: dict[str, Any]) -> dict[str, Any]:
    adapters = _active_channel_adapter_payloads(state_payload)
    return {
        "bindings": [item.as_payload() for item in list_channel_route_bindings(state_payload)],
        "supported_transports": list(SUPPORTED_CHANNEL_ROUTE_TRANSPORTS),
        "active_transports": sorted({item["transport"] for item in adapters}),
        "active_adapters": adapters,
    }


def _extension_issue_count(preview: dict[str, Any] | None) -> int:
    if not isinstance(preview, dict):
        return 0
    issues = preview.get("issues")
    if isinstance(issues, list):
        return len(issues)
    results = preview.get("results")
    if isinstance(results, list):
        return sum(
            len(result.get("issues", []))
            for result in results
            if isinstance(result, dict) and isinstance(result.get("issues"), list)
        )
    return 0


def _extension_load_error_count(preview: dict[str, Any] | None) -> int:
    if not isinstance(preview, dict):
        return 0
    load_errors = preview.get("load_errors")
    return len(load_errors) if isinstance(load_errors, list) else 0


async def _log_extension_lifecycle_event(
    *,
    action: str,
    outcome: str,
    preview: dict[str, Any] | None = None,
    path: str | None = None,
    error: str | None = None,
    extra_details: dict[str, Any] | None = None,
) -> None:
    preview = preview if isinstance(preview, dict) else {}
    permission_summary = preview.get("permission_summary")
    permission_status = (
        str(permission_summary.get("status"))
        if isinstance(permission_summary, dict) and permission_summary.get("status") is not None
        else None
    )
    extension_id = str(preview.get("id") or preview.get("extension_id") or "")
    display_name = str(preview.get("display_name") or extension_id or Path(path or "extension").name or "extension")
    details = {
        "action": action,
        "status": f"{action}_{outcome}" if outcome == "failed" else (
            "validated" if action == "validate"
            else "source_saved" if action == "save_source"
            else "installed" if action == "install"
            else "updated" if action == "update"
            else "enabled" if action == "enable"
            else "disabled" if action == "disable"
            else "configured" if action == "configure"
            else "removed" if action == "remove"
            else action
        ),
        "path": preview.get("path") or path,
        "manifest_path": preview.get("manifest_path"),
        "extension_id": extension_id or None,
        "extension_display_name": display_name,
        "version": preview.get("version"),
        "kind": preview.get("kind"),
        "trust": preview.get("trust"),
        "location": preview.get("location"),
        "package_digest": preview.get("package_digest"),
        "permission_status": permission_status,
        "issue_count": _extension_issue_count(preview),
        "load_error_count": _extension_load_error_count(preview),
        "extension_status": preview.get("status"),
        "ok": preview.get("ok"),
        "error": error,
        **(extra_details or {}),
    }
    await log_integration_event(
        integration_type="extension",
        name=extension_id or display_name,
        outcome=outcome,
        details={key: value for key, value in details.items() if value is not None},
    )


def _scaffold_package_slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("package_name must contain at least one letter or number")
    return normalized


def _configure_request_uses_new_secret_values(
    preview: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    contributions = preview.get("contributions")
    if not isinstance(contributions, list) or not isinstance(config, dict):
        return False

    for contribution in contributions:
        if not isinstance(contribution, dict):
            continue
        contribution_type = contribution.get("type")
        contribution_name = contribution.get("name")
        config_fields = contribution.get("config_fields")
        if not isinstance(contribution_type, str) or not isinstance(contribution_name, str):
            continue
        if not isinstance(config_fields, list):
            continue
        contribution_configs = config.get(contribution_type)
        if not isinstance(contribution_configs, dict):
            continue
        incoming_config = contribution_configs.get(contribution_name)
        if not isinstance(incoming_config, dict):
            continue
        for field in config_fields:
            if not isinstance(field, dict):
                continue
            if str(field.get("input") or "") != "password":
                continue
            key = field.get("key")
            if not isinstance(key, str) or not key:
                continue
            if key not in incoming_config:
                continue
            value = incoming_config.get(key)
            if isinstance(value, str):
                if not value.strip() or value == _REDACTED_CONFIG_SENTINEL:
                    continue
                return True
            return True
    return False


async def _require_extension_lifecycle_approval(
    action: str,
    preview: dict[str, Any],
    *,
    consume: bool = True,
) -> None:
    approval_profile = preview.get("approval_profile")
    if not isinstance(approval_profile, dict) or not approval_profile.get("requires_lifecycle_approval"):
        return

    lifecycle_boundaries = [
        str(boundary)
        for boundary in approval_profile.get("lifecycle_boundaries", [])
        if isinstance(boundary, str) and boundary.strip()
    ]
    if action == "enable":
        enable_boundaries = [
            boundary
            for boundary in lifecycle_boundaries
            if boundary != "secret_management"
        ]
        if not enable_boundaries:
            return
        lifecycle_boundaries = enable_boundaries or lifecycle_boundaries

    extension_id = str(preview.get("id") or preview.get("extension_id") or "")
    display_name = str(preview.get("display_name") or extension_id or "extension")
    tool_name = f"extension_{action}"
    arguments = {
        "extension_id": extension_id,
        "version": preview.get("version"),
        "package_path": preview.get("root_path") or preview.get("path"),
        "package_digest": preview.get("package_digest"),
        "boundaries": lifecycle_boundaries,
        "permissions": preview.get("permissions"),
    }
    fingerprint = fingerprint_tool_call(tool_name, arguments)
    approval_satisfied = (
        await approval_repository.consume_approved(
            session_id=None,
            tool_name=tool_name,
            fingerprint=fingerprint,
        )
        if consume
        else await approval_repository.has_approved(
            session_id=None,
            tool_name=tool_name,
            fingerprint=fingerprint,
        )
    )
    if approval_satisfied:
        return

    summary = (
        f"{action.replace('_', ' ').title()} extension '{display_name}' "
        f"with access to {', '.join(lifecycle_boundaries) or 'high-risk capabilities'}"
    )
    request = await approval_repository.get_or_create_pending(
        session_id=None,
        tool_name=tool_name,
        risk_level=str(approval_profile.get("risk_level") or "high"),
        summary=summary,
        fingerprint=fingerprint,
        details={
            "extension_id": extension_id,
            "extension_display_name": display_name,
            "action": action,
            "package_path": preview.get("root_path") or preview.get("path"),
            "package_digest": preview.get("package_digest"),
            "permissions": preview.get("permissions"),
            "approval_profile": approval_profile,
        },
    )
    raise HTTPException(
        status_code=409,
        detail={
            "type": "approval_required",
            "approval_id": request.id,
            "tool_name": tool_name,
            "risk_level": request.risk_level,
            "message": (
                f"{summary}\n\n"
                "Approve it first, then retry the extension action."
            ),
        },
    )


async def _test_extension_mcp_connector(connector: dict[str, Any]) -> dict[str, Any]:
    name = str(connector.get("name") or "")
    config = mcp_manager._config.get(name)
    if not config:
        health = connector.get("health")
        return {
            "status": "inactive",
            "message": "Connector is not registered in the MCP runtime.",
            "health": health,
        }

    if not bool(config.get("enabled", False)):
        await log_integration_event(
            integration_type="extension_connector_test",
            name=name,
            outcome="skipped",
            details={
                "status": "disabled",
                "extension_id": connector.get("extension_id"),
                "reference": connector.get("reference"),
                "url": config.get("url"),
            },
        )
        return {
            "status": "disabled",
            "message": "Enable the connector before running a live test.",
            "health": connector.get("health"),
        }

    url = config["url"]
    raw_headers = config.get("headers")
    missing_vars = mcp_manager._check_unresolved_vars(raw_headers)
    if missing_vars:
        await log_integration_event(
            integration_type="extension_connector_test",
            name=name,
            outcome="auth_required",
            details={
                "status": "auth_required",
                "extension_id": connector.get("extension_id"),
                "reference": connector.get("reference"),
                "missing_env_vars": missing_vars,
                "url": url,
            },
        )
        return {
            "status": "auth_required",
            "message": f"Missing environment variables: {', '.join(missing_vars)}",
            "missing_env_vars": missing_vars,
            "health": connector.get("health"),
        }

    try:
        params: dict[str, Any] = {"url": url, "transport": "streamable-http"}
        if raw_headers:
            params["headers"] = {
                key: mcp_manager._resolve_env_vars(value)
                for key, value in raw_headers.items()
            }
        client = MCPClient(params, structured_output=False)
        tools = client.get_tools()
        tool_names = [tool.name for tool in tools]
        client.disconnect()
        await log_integration_event(
            integration_type="extension_connector_test",
            name=name,
            outcome="succeeded",
            details={
                "status": "ok",
                "extension_id": connector.get("extension_id"),
                "reference": connector.get("reference"),
                "tool_count": len(tools),
                "tool_names": tool_names,
                "url": url,
            },
        )
        return {
            "status": "ok",
            "tool_count": len(tools),
            "tools": tool_names,
            "health": connector.get("health"),
        }
    except Exception as exc:
        exc_str = str(exc).lower()
        status = "auth_failed" if any(token in exc_str for token in ("401", "403", "unauthorized", "forbidden")) else "connection_failed"
        await log_integration_event(
            integration_type="extension_connector_test",
            name=name,
            outcome="failed",
            details={
                "status": status,
                "extension_id": connector.get("extension_id"),
                "reference": connector.get("reference"),
                "url": url,
                "error": str(exc),
            },
        )
        return {
            "status": status,
            "message": str(exc),
            "health": connector.get("health"),
        }


@router.post("/extensions/scaffold", status_code=201)
async def scaffold_extension_package_in_workspace(req: ExtensionScaffoldRequest):
    preview: dict[str, Any] | None = None
    display_name = req.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="display_name must be non-empty")
    try:
        slug = _scaffold_package_slug(req.package_name)
        extension_id = req.extension_id.strip() if isinstance(req.extension_id, str) and req.extension_id.strip() else f"seraph.{slug}"
        package_root = Path(settings.workspace_dir) / "extensions" / slug
        scaffold = scaffold_extension_package(
            package_root,
            extension_id=extension_id,
            display_name=display_name,
            kind=req.kind,
            contributions=req.contributions,
        )
        preview = validate_extension_path(str(package_root))
    except FileExistsError as exc:
        await _log_extension_lifecycle_event(
            action="scaffold",
            outcome="failed",
            path=str(Path(settings.workspace_dir) / "extensions" / req.package_name.strip()),
            error=str(exc),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="scaffold",
            outcome="failed",
            path=req.package_name,
            error=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await _log_extension_lifecycle_event(
        action="scaffold",
        outcome="succeeded",
        preview=preview,
        path=str(scaffold.package_root),
        extra_details={
            "created_file_count": len(scaffold.created_files),
            "created_files": [str(path.relative_to(scaffold.package_root)) for path in scaffold.created_files],
        },
    )
    return {
        "status": "scaffolded" if preview.get("ok") else "scaffolded_invalid",
        "path": str(scaffold.package_root),
        "created_files": [str(path.relative_to(scaffold.package_root)) for path in scaffold.created_files],
        "preview": preview,
    }


@router.get("/extensions")
async def list_extension_packages():
    return list_extensions()


@router.get("/extensions/channel-routing")
async def get_channel_routing():
    state_payload = load_extension_state_payload()
    return _channel_routing_response(state_payload)


@router.put("/extensions/channel-routing")
async def update_channel_routing(req: ChannelRoutingUpdateRequest):
    state_payload = load_extension_state_payload()
    try:
        for route, binding in req.bindings.items():
            set_channel_route_binding(
                state_payload,
                route=route,
                primary_transport=binding.primary_transport,
                fallback_transport=binding.fallback_transport,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    save_extension_state_payload(state_payload)
    await log_integration_event(
        integration_type="channel_routing",
        name="observer_delivery",
        outcome="updated",
        details={"routes": sorted(req.bindings.keys())},
    )
    return _channel_routing_response(state_payload)


@router.get("/extensions/{extension_id}")
async def get_extension_package(extension_id: str):
    try:
        return {"extension": get_extension(extension_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc


@router.get("/extensions/{extension_id}/connectors")
async def list_extension_package_connectors(extension_id: str):
    try:
        return list_extension_connectors(extension_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc


@router.post("/extensions/{extension_id}/connectors/test")
async def test_extension_package_connector(extension_id: str, req: ExtensionConnectorTestRequest):
    try:
        connector = get_extension_connector(extension_id, req.reference)
    except KeyError as exc:
        detail = (
            f"Extension '{extension_id}' not found"
            if str(exc) == f"'{extension_id}'"
            else f"Connector reference '{req.reference}' is not part of extension '{extension_id}'"
        )
        raise HTTPException(status_code=404, detail=detail) from exc

    connector_type = str(connector.get("type") or "")
    health = connector.get("health") if isinstance(connector.get("health"), dict) else None
    if connector_type == "mcp_servers":
        return await _test_extension_mcp_connector(connector)

    await log_integration_event(
        integration_type="extension_connector_test",
        name=str(connector.get("name") or req.reference),
        outcome="succeeded" if isinstance(health, dict) and bool(health.get("ready")) else "skipped",
        details={
            "status": str(health.get("state") if isinstance(health, dict) else connector.get("status") or "unknown"),
            "extension_id": extension_id,
            "reference": req.reference,
            "connector_type": connector_type,
        },
    )
    return {
        "status": str(health.get("state") if isinstance(health, dict) else connector.get("status") or "unknown"),
        "message": str(health.get("summary") if isinstance(health, dict) else connector.get("status") or "Connector status"),
        "health": health,
    }


@router.post("/extensions/{extension_id}/connectors/enabled")
async def set_extension_package_connector_enabled(extension_id: str, req: ExtensionConnectorToggleRequest):
    preview: dict[str, Any] | None = None
    try:
        preview = get_extension(extension_id)
        if req.enabled:
            if preview.get("status") != "ready":
                raise ValueError(
                    f"extension '{extension_id}' is degraded and cannot enable packaged connectors until validation issues are fixed"
                )
            target_connector = next(
                (
                    contribution
                    for contribution in preview.get("contributions", [])
                    if isinstance(contribution, dict) and contribution.get("reference") == req.reference
                ),
                None,
            )
            if target_connector is None:
                raise KeyError(req.reference)
            permission_profile = target_connector.get("permission_profile")
            connector_preview = {
                **preview,
                "approval_profile": {
                    "requires_runtime_approval": bool(
                        isinstance(permission_profile, dict) and permission_profile.get("requires_approval")
                    ),
                    "runtime_behavior": (
                        str(permission_profile.get("approval_behavior") or "never")
                        if isinstance(permission_profile, dict)
                        else "never"
                    ),
                    "requires_lifecycle_approval": bool(
                        isinstance(permission_profile, dict)
                        and permission_profile.get("lifecycle_approval_boundaries")
                    ),
                    "lifecycle_boundaries": (
                        list(permission_profile.get("lifecycle_approval_boundaries", []))
                        if isinstance(permission_profile, dict)
                        else []
                    ),
                    "risk_level": (
                        str(permission_profile.get("risk_level") or "low")
                        if isinstance(permission_profile, dict)
                        else "low"
                    ),
                },
            }
            await _require_extension_lifecycle_approval("enable", connector_preview)
        result = set_extension_connector_enabled(extension_id, req.reference, enabled=req.enabled)
    except KeyError as exc:
        detail = (
            f"Extension '{extension_id}' not found"
            if str(exc) == f"'{extension_id}'"
            else f"Connector reference '{req.reference}' is not part of extension '{extension_id}'"
        )
        await _log_extension_lifecycle_event(
            action="enable" if req.enabled else "disable",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=detail,
            extra_details={"reference": req.reference},
        )
        raise HTTPException(status_code=404, detail=detail) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="enable" if req.enabled else "disable",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=str(exc),
            extra_details={"reference": req.reference},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await _log_extension_lifecycle_event(
        action="enable" if req.enabled else "disable",
        outcome="succeeded",
        preview=result.get("extension"),
        path=extension_id,
        extra_details={
            "reference": req.reference,
            "changed": result.get("changed"),
        },
    )
    return {
        "status": "enabled" if req.enabled else "disabled",
        **result,
    }


@router.get("/extensions/{extension_id}/source")
async def get_extension_package_source(extension_id: str, reference: str):
    try:
        return get_extension_source(extension_id, reference)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/extensions/{extension_id}/source")
async def save_extension_package_source(extension_id: str, req: ExtensionSourceSaveRequest):
    try:
        payload = save_extension_source(extension_id, req.reference, req.content)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="save_source",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
            extra_details={"reference": req.reference},
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="save_source",
            outcome="failed",
            path=extension_id,
            error=str(exc),
            extra_details={"reference": req.reference},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="save_source",
        outcome="succeeded",
        preview=payload.get("extension"),
        path=extension_id,
        extra_details={"reference": req.reference},
    )
    return payload


@router.post("/extensions/validate")
async def validate_extension_package_path(req: ExtensionPathRequest):
    try:
        payload = validate_extension_path(req.path)
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="validate",
            outcome="failed",
            path=req.path,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="validate",
        outcome="succeeded",
        preview=payload,
        path=req.path,
    )
    return payload


@router.post("/extensions/install", status_code=201)
async def install_extension_package(req: ExtensionPathRequest):
    preview: dict[str, Any] | None = None
    try:
        preview = validate_extension_path(req.path)
        if not preview.get("ok", False):
            raise ValueError("extension package failed validation")
        lifecycle_plan = preview.get("lifecycle_plan")
        if isinstance(lifecycle_plan, dict) and lifecycle_plan.get("recommended_action") == "update":
            extension_id = preview.get("extension_id") or preview.get("id") or "extension"
            raise ValueError(
                f"extension '{extension_id}' is already installed; use update to replace the workspace package"
            )
        await _require_extension_lifecycle_approval("install", preview)
        extension = install_extension_path(req.path)
    except FileExistsError as exc:
        await _log_extension_lifecycle_event(
            action="install",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=str(exc),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="install",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="install",
        outcome="succeeded",
        preview=extension,
        path=req.path,
        extra_details={
            "location": extension.get("location"),
        },
    )
    return {"status": "installed", "extension": extension}


@router.post("/extensions/update")
async def update_extension_package(req: ExtensionPathRequest):
    preview: dict[str, Any] | None = None
    try:
        preview = validate_extension_path(req.path)
        if not preview.get("ok", False):
            raise ValueError("extension package failed validation")
        lifecycle_plan = preview.get("lifecycle_plan")
        if not isinstance(lifecycle_plan, dict) or lifecycle_plan.get("recommended_action") != "update":
            extension_id = preview.get("extension_id") or preview.get("id") or "extension"
            raise ValueError(
                f"extension '{extension_id}' is not updateable from this package path"
            )
        await _require_extension_lifecycle_approval("update", preview)
        extension = update_extension_path(req.path)
    except KeyError as exc:
        extension_id = preview.get("extension_id") if isinstance(preview, dict) else req.path
        await _log_extension_lifecycle_event(
            action="update",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=f"Extension '{extension_id}' not found",
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="update",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="update",
        outcome="succeeded",
        preview=extension,
        path=req.path,
        extra_details={
            "location": extension.get("location"),
        },
    )
    return {"status": "updated", "extension": extension}


@router.post("/extensions/{extension_id}/enable")
async def enable_extension_package(extension_id: str):
    preview: dict[str, Any] | None = None
    try:
        preview = get_extension(extension_id)
        if preview.get("status") != "ready":
            raise ValueError(
                f"extension '{extension_id}' is degraded and cannot be enabled until validation issues are fixed"
            )
        await _require_extension_lifecycle_approval("enable", preview)
        result = enable_extension(extension_id)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="enable",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="enable",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="enable",
        outcome="succeeded",
        preview=result.get("extension"),
        path=extension_id,
        extra_details={
            "changed": result["changed"],
            "changed_count": len(result.get("changed", [])),
        },
    )
    return {"status": "enabled", **result}


@router.post("/extensions/{extension_id}/disable")
async def disable_extension_package(extension_id: str):
    try:
        result = disable_extension(extension_id)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="disable",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    await _log_extension_lifecycle_event(
        action="disable",
        outcome="succeeded",
        preview=result.get("extension"),
        path=extension_id,
        extra_details={
            "changed": result["changed"],
            "changed_count": len(result.get("changed", [])),
        },
    )
    return {"status": "disabled", **result}


@router.post("/extensions/{extension_id}/configure")
async def configure_extension_package(extension_id: str, req: ExtensionConfigRequest):
    preview: dict[str, Any] | None = None
    try:
        preview = get_extension(extension_id)
        if _configure_request_uses_new_secret_values(preview, req.config):
            await _require_extension_lifecycle_approval("configure", preview)
        extension = configure_extension(extension_id, req.config)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="configure",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
            extra_details={"config_keys": sorted(req.config.keys())},
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="configure",
            outcome="failed",
            path=extension_id,
            error=str(exc),
            extra_details={"config_keys": sorted(req.config.keys())},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="configure",
        outcome="succeeded",
        preview=extension or preview,
        path=extension_id,
        extra_details={"config_keys": sorted(req.config.keys())},
    )
    return {"status": "configured", "extension": extension}


@router.delete("/extensions/{extension_id}")
async def remove_extension_package(extension_id: str):
    preview: dict[str, Any] | None = None
    try:
        preview = get_extension(extension_id)
        remove_extension(extension_id)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="remove",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="remove",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=str(exc),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="remove",
        outcome="succeeded",
        preview=preview,
        path=extension_id,
    )
    return {"status": "removed", "name": extension_id}
