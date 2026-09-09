"""Extension lifecycle API."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from smolagents import MCPClient

from config.settings import settings
from src.approval.identity import build_approval_owner_details
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.approval.runtime import (
    get_current_trust_principal,
    reset_runtime_context,
    set_runtime_context,
)
from src.audit.runtime import log_integration_event
from src.auth.cancellation import assert_runtime_not_revoked
from src.auth.cancellation import RuntimeRevokedError
from src.auth.service import bind_operator_principal
from src.api.chat import (
    _begin_rest_revocation_watch,
    _end_rest_revocation_watch,
    _ensure_rest_authorized,
)
from src.extensions.channel_routing import (
    SUPPORTED_CHANNEL_ROUTE_TRANSPORTS,
    list_channel_route_bindings,
    route_runtime_statuses,
    transport_runtime_status,
    set_channel_route_binding,
)
from src.extensions.channels import select_active_channel_adapters
from src.extensions.lifecycle import (
    configure_extension,
    disable_extension,
    enable_extension,
    extension_lifecycle_status,
    get_extension,
    get_extension_connector,
    get_extension_source,
    install_extension_path,
    list_extension_connectors,
    list_extensions,
    quarantine_extension,
    record_extension_review,
    reenter_extension,
    remove_extension,
    rollback_extension,
    save_extension_source,
    set_extension_connector_enabled,
    update_extension_path,
    validate_extension_path,
)
from src.extensions.permissions import LIFECYCLE_APPROVAL_BOUNDARIES
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.scaffold import scaffold_extension_package
from src.extensions.state import (
    connector_enabled_overrides,
    load_extension_state_payload,
    redact_lifecycle_error_text,
    redact_lifecycle_receipt_value,
    save_extension_state_payload,
)
from src.native_tools.registry import canonical_tool_name
from src.observer.manager import context_manager
from src.tools.policy import get_tool_execution_boundaries, get_tool_risk_level
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
_NEW_SECRET_CONFIG_SENTINEL = "__SERAPH_NEW_SECRET_VALUE__"
_SENSITIVE_DIAGNOSTIC_KEY_TOKENS = (
    "auth",
    "config",
    "credential",
    "header",
    "password",
    "secret",
    "token",
)
_PRIVATE_PATH_PATTERN = re.compile(
    r"(^|[\s'\"=:])("
    r"/(?:Users|private|tmp|var|home|etc|Volumes|opt|run|srv)/[^\s'\",;)]*"
    r"|~/?[^\s'\",;)]*"
    r"|[A-Za-z]:\\[^\s'\",;)]*"
    r")"
)
_EXTERNAL_URL_PATTERN = re.compile(r"https?://[^\s'\",;\)\]}]+", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(authorization|api[_-]?key|access[_-]?token|client[_-]?secret|password|secret|token)"
    r"(\s*[:=]\s*)[^\s,;]+"
)

# Lifecycle helpers are synchronous and reopen package/registry state.  Keep
# the final snapshot check and the corresponding effect together for API
# requests in this process.  The lifecycle module has no shared lock, so this
# does not claim to serialize filesystem changes made by another process.
_EXTENSION_EFFECT_LOCK = RLock()


@contextmanager
def _extension_effect_guard():
    """Serialize approval identity checks and effects across API processes."""

    lock_path = Path(settings.workspace_dir) / ".seraph-extension-lifecycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _EXTENSION_EFFECT_LOCK:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise RuntimeError("extension lifecycle lock is unavailable") from exc
        with os.fdopen(descriptor, "a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise RuntimeError("extension lifecycle lock is unavailable") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


_SESSION_REVOKED_DETAIL = {
    "code": "session_revoked",
    "message": "Operator session was revoked.",
}


def _require_authenticated_capability_operator(request: Request):
    """Load the shared capability operator gate without an API import cycle."""

    from src.api.capabilities import _require_authenticated_capability_operator

    return _require_authenticated_capability_operator(request)


def _assert_extension_runtime_not_revoked() -> None:
    try:
        assert_runtime_not_revoked()
    except RuntimeRevokedError as exc:
        raise HTTPException(status_code=401, detail=dict(_SESSION_REVOKED_DETAIL)) from exc


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _operator_actor(operator: Any) -> str:
    principal_id = str(getattr(getattr(operator, "principal", None), "principal_id", "") or "")
    session_id = str(getattr(operator, "session_id", "") or "")
    identity = principal_id or session_id or "operator"
    return f"operator:{_content_hash(identity)}"


def _redacted_path_receipt(value: Any) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    return {"redacted": True, "digest": _content_hash(text)}


def _redact_extension_error(value: Any) -> str:
    """Keep lifecycle errors useful without returning paths, URLs, or secrets."""
    return redact_lifecycle_error_text(value)


def _redact_lifecycle_receipt_value(value: Any) -> Any:
    """Apply the shared structured lifecycle redaction contract."""
    return redact_lifecycle_receipt_value(value)


def _redact_lifecycle_api_value(value: Any, *, key: str | None = None) -> Any:
    """Apply the shared structured lifecycle redaction contract."""
    return redact_lifecycle_receipt_value(value, key=key)


def _snapshot_digest(value: Any) -> str:
    """Hash an internal lifecycle snapshot without putting it in a receipt."""

    try:
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        serialized = repr(value)
    return _content_hash(serialized)


async def _ensure_extension_rest_authorized(request: Request, scope) -> None:
    """Recheck REST authority and close the watcher race after its await.

    ``_ensure_rest_authorized`` checks the revocation event before and during
    token authentication.  A watcher can still set the event just as that
    await completes, so perform a synchronous post-await check before the
    caller enters its effect boundary.
    """

    await _ensure_rest_authorized(request, scope)
    if scope is None:
        return
    try:
        assert_runtime_not_revoked()
    except RuntimeRevokedError as exc:
        raise HTTPException(status_code=401, detail=dict(_SESSION_REVOKED_DETAIL)) from exc
    if scope[0].is_set():
        raise HTTPException(status_code=401, detail=dict(_SESSION_REVOKED_DETAIL))


def _extension_identity(preview: dict[str, Any]) -> tuple[str, str | None, str | None]:
    extension_id = str(preview.get("id") or preview.get("extension_id") or "")
    root_path = preview.get("root_path") or preview.get("path")
    path_text = str(root_path).strip() if root_path else None
    package_digest = preview.get("package_digest")
    digest_text = str(package_digest).strip() if package_digest else None
    return extension_id, path_text, digest_text


def _assert_extension_effect_identity(
    approved_preview: dict[str, Any],
    current_preview: dict[str, Any],
    *,
    action: str,
) -> None:
    """Reject a lifecycle effect when its approved package snapshot changed."""

    approved_id, approved_path, approved_digest = _extension_identity(approved_preview)
    current_id, current_path, current_digest = _extension_identity(current_preview)
    if (
        not approved_id
        or approved_id != current_id
        or not approved_path
        or not current_path
        or _content_hash(approved_path) != _content_hash(current_path)
        or not approved_digest
        or not current_digest
        or approved_digest != current_digest
        or _snapshot_digest(approved_preview.get("config"))
        != _snapshot_digest(current_preview.get("config"))
    ):
        raise ValueError(
            f"extension package changed after {action} approval; retry the lifecycle action"
        )


def _assert_extension_path_effect_identity(
    path: str,
    approved_preview: dict[str, Any],
    *,
    action: str,
) -> None:
    current_preview = validate_extension_path(path)
    if not isinstance(current_preview, dict) or not current_preview.get("ok", False):
        raise ValueError(
            f"extension package changed after {action} approval; retry the lifecycle action"
        )
    _assert_extension_effect_identity(approved_preview, current_preview, action=action)


def _assert_registered_extension_effect_identity(
    extension_id: str,
    approved_preview: dict[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    current_preview = get_extension(extension_id)
    _assert_extension_effect_identity(approved_preview, current_preview, action=action)
    return current_preview


def _extension_contribution(
    preview: dict[str, Any],
    reference: str,
) -> dict[str, Any] | None:
    contributions = preview.get("contributions")
    if not isinstance(contributions, list):
        return None
    return next(
        (
            item for item in contributions
            if isinstance(item, dict) and item.get("reference") == reference
        ),
        None,
    )


def _connector_snapshot(contribution: dict[str, Any]) -> dict[str, Any]:
    """Select connector identity/state fields while ignoring health churn."""

    return {
        key: value
        for key, value in contribution.items()
        if key not in {"health", "status"}
    }


def _assert_connector_effect_identity(
    extension_id: str,
    reference: str,
    approved_preview: dict[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    current_preview = get_extension(extension_id)
    _assert_extension_effect_identity(approved_preview, current_preview, action=action)
    approved_connector = _extension_contribution(approved_preview, reference)
    current_connector = _extension_contribution(current_preview, reference)
    if (
        not isinstance(approved_connector, dict)
        or not isinstance(current_connector, dict)
        or _snapshot_digest(_connector_snapshot(approved_connector))
        != _snapshot_digest(_connector_snapshot(current_connector))
        or _snapshot_digest(approved_preview.get("config"))
        != _snapshot_digest(current_preview.get("config"))
    ):
        raise ValueError(
            f"extension connector changed after {action} approval; retry the lifecycle action"
        )
    return current_preview


def _assert_config_effect_identity(
    extension_id: str,
    approved_preview: dict[str, Any],
    requested_config: dict[str, Any],
    approved_context: dict[str, Any] | None,
) -> dict[str, Any]:
    current_preview = get_extension(extension_id)
    _assert_extension_effect_identity(approved_preview, current_preview, action="configure")
    if _snapshot_digest(approved_preview.get("config")) != _snapshot_digest(current_preview.get("config")):
        raise ValueError(
            "extension configuration changed after configure approval; retry the lifecycle action"
        )
    if approved_context is not None:
        current_context = _configure_request_approval_context(
            current_preview,
            requested_config,
        )
        if current_context != approved_context:
            raise ValueError(
                "extension configuration changed after configure approval; retry the lifecycle action"
            )
    return current_preview


def _assert_source_effect_identity(
    extension_id: str,
    reference: str,
    approved_source_preview: dict[str, Any],
    requested_content: str,
    approved_context: dict[str, Any] | None,
) -> dict[str, Any]:
    current_source_preview = get_extension_source(extension_id, reference)
    approved_extension = approved_source_preview.get("extension")
    current_extension = current_source_preview.get("extension")
    if not isinstance(approved_extension, dict) or not isinstance(current_extension, dict):
        raise ValueError(
            "extension source identity changed after save_source approval; retry the lifecycle action"
        )
    _assert_extension_effect_identity(approved_extension, current_extension, action="save_source")
    if (
        _content_hash(str(approved_source_preview.get("content") or ""))
        != _content_hash(str(current_source_preview.get("content") or ""))
    ):
        raise ValueError(
            "extension source changed after save_source approval; retry the lifecycle action"
        )
    if approved_context is not None:
        current_context = _source_save_request_approval_context(
            current_source_preview,
            content=requested_content,
        )
        if current_context != approved_context:
            raise ValueError(
                "extension source request changed after save_source approval; retry the lifecycle action"
            )
    return current_source_preview


def _assert_rollback_effect_identity(
    extension_id: str,
    approved_preview: dict[str, Any],
    approved_snapshot: dict[str, Any],
) -> None:
    _assert_registered_extension_effect_identity(
        extension_id,
        approved_preview,
        action="rollback",
    )
    lifecycle = extension_lifecycle_status(extension_id)
    snapshots = lifecycle.get("rollback", {}).get("snapshots")
    current_snapshot = next(
        (
            item for item in snapshots
            if isinstance(item, dict) and item.get("id") == approved_snapshot.get("id")
        ),
        None,
    ) if isinstance(snapshots, list) else None
    if not isinstance(current_snapshot, dict):
        raise ValueError("rollback snapshot changed after rollback approval; retry the lifecycle action")
    identity_fields = ("id", "version", "digest")
    if any(current_snapshot.get(field) != approved_snapshot.get(field) for field in identity_fields):
        raise ValueError("rollback snapshot changed after rollback approval; retry the lifecycle action")
    if _content_hash(str(current_snapshot.get("path") or "")) != _content_hash(str(approved_snapshot.get("path") or "")):
        raise ValueError("rollback snapshot changed after rollback approval; retry the lifecycle action")


def _lifecycle_fallback_preview(preview: dict[str, Any]) -> dict[str, Any]:
    approval_profile = preview.get("approval_profile")
    if isinstance(approval_profile, dict) and approval_profile.get("requires_lifecycle_approval"):
        return preview

    permissions = preview.get("permissions")
    if not isinstance(permissions, dict):
        return preview

    boundaries: list[str] = []
    for boundary in permissions.get("execution_boundaries", []) or []:
        if isinstance(boundary, str) and boundary.strip() and boundary not in boundaries:
            boundaries.append(boundary.strip())

    risk_level = "low"
    for raw_tool_name in permissions.get("tools", []) or []:
        if not isinstance(raw_tool_name, str) or not raw_tool_name.strip():
            continue
        tool_name = canonical_tool_name(raw_tool_name)
        if not tool_name:
            continue
        is_mcp = tool_name.startswith("mcp_")
        for boundary in get_tool_execution_boundaries(tool_name, is_mcp=is_mcp):
            if boundary not in boundaries:
                boundaries.append(boundary)
        tool_risk = get_tool_risk_level(tool_name, is_mcp=is_mcp)
        if tool_risk == "high" or (tool_risk == "medium" and risk_level == "low"):
            risk_level = tool_risk

    lifecycle_boundaries = [
        boundary
        for boundary in boundaries
        if boundary in LIFECYCLE_APPROVAL_BOUNDARIES
    ]
    if not lifecycle_boundaries:
        return preview

    if risk_level != "high":
        risk_level = "high"
    runtime_behavior = "mcp_policy" if "external_mcp" in boundaries else "high_risk"
    requires_runtime_approval = runtime_behavior in {"mcp_policy", "high_risk"}
    return {
        **preview,
        "approval_profile": {
            "requires_runtime_approval": requires_runtime_approval,
            "runtime_behavior": runtime_behavior,
            "requires_lifecycle_approval": True,
            "lifecycle_boundaries": lifecycle_boundaries,
            "risk_level": risk_level,
        },
    }


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


class ExtensionLifecycleReasonRequest(BaseModel):
    reason: str = ""


class ExtensionRollbackRequest(BaseModel):
    snapshot_id: str | None = None


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
    from src.observer.manager import context_manager
    from src.scheduler.connection_manager import ws_manager

    active_transports = {item["transport"] for item in adapters}
    transport_statuses = [
        {
            **transport_runtime_status(
                transport,
                active_transports=active_transports,
                websocket_connection_count=ws_manager.active_count,
                daemon_connected=context_manager.is_daemon_connected(),
            ),
            "adapter": next((item for item in adapters if item["transport"] == transport), None),
        }
        for transport in SUPPORTED_CHANNEL_ROUTE_TRANSPORTS
    ]
    return {
        "bindings": [item.as_payload() for item in list_channel_route_bindings(state_payload)],
        "supported_transports": list(SUPPORTED_CHANNEL_ROUTE_TRANSPORTS),
        "active_transports": sorted(active_transports),
        "active_adapters": adapters,
        "transport_statuses": transport_statuses,
        "route_statuses": route_runtime_statuses(
            state_payload,
            active_transports=active_transports,
            websocket_connection_count=ws_manager.active_count,
            daemon_connected=context_manager.is_daemon_connected(),
        ),
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


def _extension_recommended_diagnostic_actions(
    extension: dict[str, Any],
    lifecycle: dict[str, Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    diagnostics = extension.get("diagnostics_summary")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    permission_summary = extension.get("permission_summary")
    permission_summary = permission_summary if isinstance(permission_summary, dict) else {}
    compatibility = extension.get("compatibility")
    compatibility = compatibility if isinstance(compatibility, dict) else {}
    rollback = lifecycle.get("rollback")
    rollback = rollback if isinstance(rollback, dict) else {}
    quarantine = lifecycle.get("quarantine")
    quarantine = quarantine if isinstance(quarantine, dict) else {}

    if diagnostics.get("issue_count") or diagnostics.get("load_error_count"):
        actions.append({
            "type": "review",
            "label": "Review package diagnostics",
            "reason": "Doctor issues or load errors require operator review before lifecycle changes.",
            "endpoint": f"/api/extensions/{extension['id']}/review",
        })
    if diagnostics.get("degraded_connector_count"):
        actions.append({
            "type": "open_studio",
            "label": "Inspect connector configuration",
            "reason": "At least one connector is degraded or needs configuration.",
        })
    if compatibility.get("compatible") is False:
        actions.append({
            "type": "block_lifecycle",
            "label": "Resolve compatibility before update",
            "reason": "The package compatibility check is failing for this Seraph build.",
        })
    if permission_summary.get("ok") is False:
        actions.append({
            "type": "review_permissions",
            "label": "Review missing permissions",
            "reason": "Required tools, boundaries, or data access are not currently granted.",
        })
    if quarantine.get("active"):
        actions.append({
            "type": "reentry",
            "label": "Run re-entry review",
            "reason": "The package is quarantined and cannot be enabled until review clears it.",
            "endpoint": f"/api/extensions/{extension['id']}/reentry",
        })
    elif rollback.get("available"):
        actions.append({
            "type": "rollback",
            "label": "Rollback to previous snapshot",
            "reason": "A rollback snapshot is available if diagnostics indicate a bad update.",
            "endpoint": f"/api/extensions/{extension['id']}/rollback",
        })
    if not actions:
        actions.append({
            "type": "review",
            "label": "Record lifecycle review",
            "reason": "No blocking diagnostics are present; record an operator review before privileged changes if needed.",
            "endpoint": f"/api/extensions/{extension['id']}/review",
        })
    return actions


def _sanitize_extension_diagnostic_value(value: Any, *, key: str | None = None) -> Any:
    key_text = (key or "").lower()
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_extension_diagnostic_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_extension_diagnostic_value(item) for item in value]
    if isinstance(value, str):
        if key_text == "path" or key_text.endswith("_path") or key_text in {"package_path", "manifest_path", "root_path"}:
            return {"redacted": True, "digest": _content_hash(value)}
        if any(token in key_text for token in _SENSITIVE_DIAGNOSTIC_KEY_TOKENS):
            return {"redacted": True, "digest": _content_hash(value)}
        if _PRIVATE_PATH_PATTERN.search(value):
            return {"redacted": True, "digest": _content_hash(value)}
    return value


def _safe_extension_diagnostics_payload(extension_id: str) -> dict[str, Any]:
    extension = get_extension(extension_id)
    lifecycle = extension_lifecycle_status(extension_id)
    lifecycle_state = lifecycle.get("lifecycle")
    lifecycle_state = lifecycle_state if isinstance(lifecycle_state, dict) else {}
    rollback = lifecycle.get("rollback")
    rollback = rollback if isinstance(rollback, dict) else {}
    quarantine = lifecycle.get("quarantine")
    quarantine = quarantine if isinstance(quarantine, dict) else {"active": False, "state": "clear"}
    snapshots = rollback.get("snapshots")
    safe_snapshots = [
        {
            "id": item.get("id"),
            "version": item.get("version"),
            "digest": item.get("digest"),
            "reason": item.get("reason"),
            "created_by": item.get("created_by"),
            "created_at": item.get("created_at"),
            "path_digest": _content_hash(str(item.get("path") or "")),
        }
        for item in snapshots
        if isinstance(item, dict)
    ] if isinstance(snapshots, list) else []
    issues = extension.get("issues")
    load_errors = extension.get("load_errors")
    diagnostics_summary = extension.get("diagnostics_summary")
    diagnostics_summary = diagnostics_summary if isinstance(diagnostics_summary, dict) else {}
    highlighted_messages = diagnostics_summary.get("highlighted_messages")
    safe_issues = _sanitize_extension_diagnostic_value(issues) if isinstance(issues, list) else []
    safe_load_errors = _sanitize_extension_diagnostic_value(load_errors) if isinstance(load_errors, list) else []
    safe_diagnostics_summary = _sanitize_extension_diagnostic_value(diagnostics_summary)
    safe_lifecycle_diagnostics = _sanitize_extension_diagnostic_value(lifecycle.get("diagnostics"))
    return {
        "extension": {
            "id": extension.get("id"),
            "display_name": extension.get("display_name"),
            "version": extension.get("version"),
            "version_line": extension.get("version_line"),
            "kind": extension.get("kind"),
            "location": extension.get("location"),
            "status": extension.get("status"),
            "trust": extension.get("trust"),
            "source": extension.get("source"),
            "publisher": extension.get("publisher"),
            "compatibility": extension.get("compatibility"),
            "permission_summary": extension.get("permission_summary"),
            "approval_profile": extension.get("approval_profile"),
            "connector_summary": extension.get("connector_summary"),
            "diagnostics_summary": safe_diagnostics_summary,
            "issue_count": len(issues) if isinstance(issues, list) else 0,
            "load_error_count": len(load_errors) if isinstance(load_errors, list) else 0,
        },
        "issues": safe_issues,
        "load_errors": safe_load_errors,
        "highlighted_messages": _sanitize_extension_diagnostic_value(highlighted_messages)
        if isinstance(highlighted_messages, list)
        else [],
        "lifecycle": {
            "last_event": lifecycle_state.get("last_event"),
            "rollback": {
                "available": bool(rollback.get("available")),
                "snapshots": safe_snapshots,
            },
            "quarantine": _sanitize_extension_diagnostic_value(quarantine),
            "diagnostics": safe_lifecycle_diagnostics,
        },
        "recommended_actions": _extension_recommended_diagnostic_actions(extension, lifecycle),
        "claim_boundary": "metadata_only_extension_diagnostics_no_source_secret_config_or_private_paths",
        "redaction": {
            "metadata_only": True,
            "raw_source_content_exposed": False,
            "secret_values_exposed": False,
            "credential_values_exposed": False,
            "config_values_exposed": False,
            "private_paths_exposed": False,
        },
        "blocked_claims": [
            "production_secure_marketplace",
            "solved_third_party_package_security",
            "marketplace_superiority",
            "full_parity",
            "production_readiness",
            "reference_system_exceedance",
        ],
    }


async def _log_extension_lifecycle_event(
    *,
    action: str,
    outcome: str,
    preview: dict[str, Any] | None = None,
    path: str | None = None,
    error: str | None = None,
    extra_details: dict[str, Any] | None = None,
    redact_paths: bool = False,
) -> None:
    preview = preview if isinstance(preview, dict) else {}
    permission_summary = preview.get("permission_summary")
    permission_status = (
        str(permission_summary.get("status"))
        if isinstance(permission_summary, dict) and permission_summary.get("status") is not None
        else None
    )
    extension_id = str(preview.get("id") or preview.get("extension_id") or "")
    display_name = str(
        preview.get("display_name")
        or extension_id
        or ("extension" if redact_paths else Path(path or "extension").name)
        or "extension"
    )
    safe_extension_id = _redact_extension_error(extension_id) if redact_paths else extension_id
    if redact_paths:
        display_name = _redact_extension_error(display_name)
    safe_extra_details = (
        _redact_lifecycle_receipt_value(extra_details)
        if redact_paths and isinstance(extra_details, dict)
        else extra_details
    )
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
        "path": (
            _redacted_path_receipt(preview.get("path") or path)
            if redact_paths
            else preview.get("path") or path
        ),
        "manifest_path": (
            _redacted_path_receipt(preview.get("manifest_path"))
            if redact_paths
            else preview.get("manifest_path")
        ),
        "extension_id": safe_extension_id or None,
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
        "error": _redact_extension_error(error) if error is not None else None,
        **(safe_extra_details or {}),
    }
    await log_integration_event(
        integration_type="extension",
        name=safe_extension_id or display_name,
        outcome=outcome,
        details={key: value for key, value in details.items() if value is not None},
    )


def _scaffold_package_slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("package_name must contain at least one letter or number")
    return normalized


def _normalize_config_value_for_approval(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_config_value_for_approval(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return [_normalize_config_value_for_approval(item) for item in value]
    return value


def _normalize_secret_value_for_approval(value: Any, *, incoming: bool) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        if value == _REDACTED_CONFIG_SENTINEL:
            return _REDACTED_CONFIG_SENTINEL
        if not value.strip():
            return value
        return _NEW_SECRET_CONFIG_SENTINEL if incoming else _REDACTED_CONFIG_SENTINEL
    return _NEW_SECRET_CONFIG_SENTINEL if incoming else _REDACTED_CONFIG_SENTINEL


def _normalize_config_entry_for_approval(
    config_entry: dict[str, Any],
    *,
    allowed_keys: set[str],
    secret_keys: set[str],
    incoming: bool,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in sorted(allowed_keys):
        if key not in config_entry:
            continue
        value = config_entry.get(key)
        if key in secret_keys:
            normalized[key] = _normalize_secret_value_for_approval(value, incoming=incoming)
        else:
            normalized[key] = _normalize_config_value_for_approval(value)
    return normalized


def _configure_request_approval_context(
    preview: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    approval_profile = preview.get("approval_profile")
    if not isinstance(approval_profile, dict) or not approval_profile.get("requires_lifecycle_approval"):
        return None

    contributions = preview.get("contributions")
    if not isinstance(contributions, list) or not isinstance(config, dict):
        return None

    existing_config = preview.get("config")
    if not isinstance(existing_config, dict):
        existing_config = {}

    requested_snapshot: dict[str, Any] = {}
    current_snapshot: dict[str, Any] = {}

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
        allowed_keys = {
            key
            for field in config_fields
            if isinstance(field, dict)
            for key in [field.get("key")]
            if isinstance(key, str) and key
        }
        if not allowed_keys:
            continue
        secret_keys = {
            key
            for field in config_fields
            if isinstance(field, dict) and str(field.get("input") or "") == "password"
            for key in [field.get("key")]
            if isinstance(key, str) and key
        }
        requested_keys = {key for key in incoming_config.keys() if key in allowed_keys}
        if not requested_keys:
            continue
        normalized_requested = _normalize_config_entry_for_approval(
            incoming_config,
            allowed_keys=requested_keys,
            secret_keys=secret_keys,
            incoming=True,
        )
        if not normalized_requested:
            continue
        existing_type_config = existing_config.get(contribution_type)
        existing_entry = (
            existing_type_config.get(contribution_name)
            if isinstance(existing_type_config, dict)
            else None
        )
        normalized_current = _normalize_config_entry_for_approval(
            existing_entry if isinstance(existing_entry, dict) else {},
            allowed_keys=set(normalized_requested.keys()),
            secret_keys=secret_keys,
            incoming=False,
        )
        requested_snapshot.setdefault(contribution_type, {})[contribution_name] = normalized_requested
        if normalized_current:
            current_snapshot.setdefault(contribution_type, {})[contribution_name] = normalized_current

    if not requested_snapshot or requested_snapshot == current_snapshot:
        return None

    return {
        "requested_config": requested_snapshot,
        "current_config": current_snapshot,
    }


def _source_save_request_approval_context(
    source_preview: dict[str, Any],
    *,
    content: str,
) -> dict[str, Any] | None:
    extension = source_preview.get("extension")
    approval_profile = extension.get("approval_profile") if isinstance(extension, dict) else None
    if not isinstance(approval_profile, dict) or not approval_profile.get("requires_lifecycle_approval"):
        return None

    reference = str(source_preview.get("reference") or "").strip()
    current_content = str(source_preview.get("content") or "")
    requested_hash = _content_hash(content)
    current_hash = _content_hash(current_content)
    if requested_hash == current_hash:
        return None

    validation = source_preview.get("validation")
    valid = validation.get("valid") if isinstance(validation, dict) else None
    return {
        "target_reference": reference,
        "current_content_hash": current_hash,
        "requested_content_hash": requested_hash,
        "current_line_count": len(current_content.splitlines()),
        "requested_line_count": len(content.splitlines()),
        "draft_valid": bool(valid) if valid is not None else None,
    }


def _approval_scope_summary(
    preview: dict[str, Any],
    *,
    action: str,
    lifecycle_boundaries: list[str],
    fingerprint_context: dict[str, Any] | None,
    redact_paths: bool = False,
) -> dict[str, Any]:
    target_type = str(preview.get("target_type") or "")
    target_name = str(preview.get("target_name") or "")
    target_reference = str(preview.get("target_reference") or preview.get("reference") or "")
    scope = {
        "action": action,
        "extension_id": (
            _redact_extension_error(str(preview.get("id") or preview.get("extension_id") or ""))
            if redact_paths
            else str(preview.get("id") or preview.get("extension_id") or "")
        ),
        "package_digest": preview.get("package_digest"),
        "lifecycle_boundaries": lifecycle_boundaries,
        "target": {
            "type": _redact_extension_error(target_type) if redact_paths else target_type,
            "name": _redact_extension_error(target_name) if redact_paths else target_name,
            "reference": _redact_extension_error(target_reference) if redact_paths else target_reference,
        },
    }
    if not isinstance(fingerprint_context, dict):
        return scope

    requested_config = fingerprint_context.get("requested_config")
    if isinstance(requested_config, dict) and requested_config:
        current_config = (
            fingerprint_context.get("current_config")
            if isinstance(fingerprint_context.get("current_config"), dict)
            else {}
        )
        changed_types: list[str] = []
        changed_target_count = 0
        for config_type, requested_targets in requested_config.items():
            if not isinstance(requested_targets, dict):
                continue
            current_targets = current_config.get(config_type)
            changed_targets = [
                target_name
                for target_name, requested_payload in requested_targets.items()
                if requested_payload
                != (
                    current_targets.get(target_name)
                    if isinstance(current_targets, dict)
                    else None
                )
            ]
            if changed_targets:
                changed_types.append(str(config_type))
                changed_target_count += len(changed_targets)
        scope["config_scope"] = {
            "config_types": sorted(changed_types),
            "changed_target_count": changed_target_count,
        }

    requested_content_hash = fingerprint_context.get("requested_content_hash")
    if isinstance(requested_content_hash, str) and requested_content_hash.strip():
        scope["source_scope"] = {
            "reference": str(
                fingerprint_context.get("target_reference")
                or scope["target"].get("reference")
                or ""
            ),
            "current_content_hash": fingerprint_context.get("current_content_hash"),
            "requested_content_hash": requested_content_hash,
            "current_line_count": fingerprint_context.get("current_line_count"),
            "requested_line_count": fingerprint_context.get("requested_line_count"),
            "draft_valid": fingerprint_context.get("draft_valid"),
        }
    snapshot_id = fingerprint_context.get("snapshot_id")
    if isinstance(snapshot_id, str) and snapshot_id.strip():
        scope["rollback_scope"] = {
            "snapshot_id": snapshot_id,
            "restored_version": fingerprint_context.get("restored_version"),
            "restored_digest": fingerprint_context.get("restored_digest"),
            "snapshot_path_hash": fingerprint_context.get("snapshot_path_hash"),
        }
    return scope


async def _require_extension_lifecycle_approval(
    action: str,
    preview: dict[str, Any],
    *,
    consume: bool = True,
    session_id: str | None = None,
    fingerprint_context: dict[str, Any] | None = None,
    summary_suffix: str | None = None,
    redact_paths: bool = False,
) -> None:
    preview = _lifecycle_fallback_preview(preview)
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
    target_reference = str(preview.get("target_reference") or preview.get("reference") or "")
    target_name = str(preview.get("target_name") or preview.get("name") or "")
    target_type = str(preview.get("target_type") or preview.get("type") or "")
    safe_target_reference = (
        _redact_extension_error(target_reference) if redact_paths else target_reference
    )
    safe_target_name = _redact_extension_error(target_name) if redact_paths else target_name
    safe_target_type = _redact_extension_error(target_type) if redact_paths else target_type
    safe_extension_id = _redact_extension_error(extension_id) if redact_paths else extension_id
    safe_display_name = _redact_extension_error(display_name) if redact_paths else display_name
    safe_fingerprint_context = (
        _redact_lifecycle_receipt_value(fingerprint_context)
        if redact_paths and isinstance(fingerprint_context, dict)
        else fingerprint_context
    )
    tool_name = f"extension_{action}"
    package_path = preview.get("root_path") or preview.get("path")
    package_identity = (
        {"package_path_hash": _content_hash(str(package_path or ""))}
        if redact_paths
        else {"package_path": package_path}
    )
    safe_permissions = (
        _redact_lifecycle_receipt_value(preview.get("permissions"))
        if redact_paths
        else preview.get("permissions")
    )
    safe_approval_profile = (
        _redact_lifecycle_receipt_value(approval_profile)
        if redact_paths
        else approval_profile
    )
    arguments = {
        "extension_id": safe_extension_id,
        "version": preview.get("version"),
        "package_digest": preview.get("package_digest"),
        "boundaries": lifecycle_boundaries,
        "permissions": safe_permissions,
        **package_identity,
    }
    owner_principal = get_current_trust_principal()
    if isinstance(safe_fingerprint_context, dict):
        arguments.update(safe_fingerprint_context)
    if safe_target_reference:
        arguments["target_reference"] = safe_target_reference
    if safe_target_name:
        arguments["target_name"] = safe_target_name
    if safe_target_type:
        arguments["target_type"] = safe_target_type
    fingerprint = fingerprint_tool_call(tool_name, arguments)
    approval_satisfied = (
        await approval_repository.consume_approved(
            session_id=session_id,
            tool_name=tool_name,
            fingerprint=fingerprint,
        )
        if consume
        else await approval_repository.has_approved(
            session_id=session_id,
            tool_name=tool_name,
            fingerprint=fingerprint,
        )
    )
    if approval_satisfied:
        return

    summary = (
        f"{action.replace('_', ' ').title()} extension "
        f"'{safe_display_name}'"
    )
    if safe_target_reference or safe_target_name:
        target_label = " / ".join(
            part
            for part in (
                safe_target_type.replace("_", " ").strip(),
                safe_target_name,
                safe_target_reference,
            )
            if part
        )
        summary = f"{summary} target '{target_label}'"
    if summary_suffix:
        summary = f"{summary} {summary_suffix.strip()}"
    summary = (
        f"{summary} with access to "
        f"{', '.join(lifecycle_boundaries) or 'high-risk capabilities'}"
    )
    approval_scope = _approval_scope_summary(
        preview,
        action=action,
        lifecycle_boundaries=lifecycle_boundaries,
        fingerprint_context=safe_fingerprint_context,
        redact_paths=redact_paths,
    )
    details = {
        "extension_id": safe_extension_id,
        "extension_display_name": safe_display_name,
        "action": action,
        "target_reference": safe_target_reference or None,
        "target_name": safe_target_name or None,
        "target_type": safe_target_type or None,
        "package_digest": preview.get("package_digest"),
        "permissions": safe_permissions,
        "approval_profile": safe_approval_profile,
        "approval_scope": approval_scope,
        **package_identity,
    }
    details.update(
        build_approval_owner_details(
            session_id=session_id,
            principal=owner_principal,
        )
    )
    if isinstance(safe_fingerprint_context, dict):
        details.update(safe_fingerprint_context)
    request = await approval_repository.get_or_create_pending(
        session_id=session_id,
        tool_name=tool_name,
        risk_level=str(approval_profile.get("risk_level") or "high"),
        summary=summary,
        fingerprint=fingerprint,
        details=details,
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
            "approval_scope": approval_scope,
        },
    )


async def _test_extension_mcp_connector(connector: dict[str, Any]) -> dict[str, Any]:
    name = str(connector.get("name") or "")
    safe_name = _redact_extension_error(name)
    config = mcp_manager._config.get(name)
    if not config:
        health = _redact_lifecycle_receipt_value(connector.get("health"))
        return {
            "status": "inactive",
            "message": "Connector is not registered in the MCP runtime.",
            "health": health,
        }

    if not bool(config.get("enabled", False)):
        await log_integration_event(
            integration_type="extension_connector_test",
            name=safe_name,
            outcome="skipped",
            details=_redact_lifecycle_receipt_value({
                "status": "disabled",
                "extension_id": connector.get("extension_id"),
                "reference": connector.get("reference"),
                "url": config.get("url"),
            }),
        )
        return {
            "status": "disabled",
            "message": "Enable the connector before running a live test.",
            "health": _redact_lifecycle_receipt_value(connector.get("health")),
        }

    url = config["url"]
    raw_headers = config.get("headers")
    missing_vars = mcp_manager._check_unresolved_vars(raw_headers)
    if missing_vars:
        await log_integration_event(
            integration_type="extension_connector_test",
            name=safe_name,
            outcome="auth_required",
            details=_redact_lifecycle_receipt_value({
                "status": "auth_required",
                "extension_id": connector.get("extension_id"),
                "reference": connector.get("reference"),
                "missing_env_vars": missing_vars,
                "url": url,
            }),
        )
        return {
            "status": "auth_required",
            "message": f"Missing environment variables: {', '.join(missing_vars)}",
            "missing_env_vars": missing_vars,
            "health": _redact_lifecycle_receipt_value(connector.get("health")),
        }

    client: MCPClient | None = None
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
        await log_integration_event(
            integration_type="extension_connector_test",
            name=safe_name,
            outcome="succeeded",
            details=_redact_lifecycle_receipt_value({
                "status": "ok",
                "extension_id": connector.get("extension_id"),
                "reference": connector.get("reference"),
                "tool_count": len(tools),
                "tool_names": tool_names,
                "url": url,
            }),
        )
        return {
            "status": "ok",
            "tool_count": len(tools),
            "tools": tool_names,
            "health": _redact_lifecycle_receipt_value(connector.get("health")),
        }
    except Exception as exc:
        exc_str = str(exc).lower()
        status = "auth_failed" if any(token in exc_str for token in ("401", "403", "unauthorized", "forbidden")) else "connection_failed"
        await log_integration_event(
            integration_type="extension_connector_test",
            name=safe_name,
            outcome="failed",
            details=_redact_lifecycle_receipt_value({
                "status": status,
                "extension_id": connector.get("extension_id"),
                "reference": connector.get("reference"),
                "url": url,
                "error": str(exc),
            }),
        )
        return {
            "status": status,
            "message": _redact_extension_error(exc),
            "health": _redact_lifecycle_receipt_value(connector.get("health")),
        }
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                # Disconnect failures must not replace the bounded connector
                # result or leak transport/client diagnostics.
                pass


@router.post("/extensions/scaffold", status_code=201)
async def scaffold_extension_package_in_workspace(
    req: ExtensionScaffoldRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    tokens = set_runtime_context(
        operator.session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, operator.session_id),
    )
    preview: dict[str, Any] | None = None
    try:
        display_name = req.display_name.strip()
        if not display_name:
            raise HTTPException(status_code=422, detail="display_name must be non-empty")
        slug = _scaffold_package_slug(req.package_name)
        extension_id = req.extension_id.strip() if isinstance(req.extension_id, str) and req.extension_id.strip() else f"seraph.{slug}"
        package_root = Path(settings.workspace_dir) / "extensions" / slug
        _assert_extension_runtime_not_revoked()
        scaffold = scaffold_extension_package(
            package_root,
            extension_id=extension_id,
            display_name=display_name,
            kind=req.kind,
            contributions=req.contributions,
        )
        preview = validate_extension_path(str(package_root))
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
    finally:
        reset_runtime_context(tokens)


@router.get("/extensions")
async def list_extension_packages():
    return _redact_lifecycle_api_value(list_extensions())


@router.get("/extensions/diagnostics")
async def get_extension_diagnostics():
    payload = list_extensions()
    extensions = payload.get("extensions", []) if isinstance(payload, dict) else []
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    return {
        "summary": summary,
        "extensions": [
            {
                "id": item.get("id"),
                "display_name": item.get("display_name"),
                "version": item.get("version"),
                "version_line": item.get("version_line"),
                "location": item.get("location"),
                "status": item.get("status"),
                "compatibility": item.get("compatibility"),
                "diagnostics_summary": item.get("diagnostics_summary"),
                "connector_summary": item.get("connector_summary"),
                "permission_summary": item.get("permission_summary"),
                "approval_profile": item.get("approval_profile"),
            }
            for item in extensions
            if isinstance(item, dict)
        ],
    }


@router.get("/extensions/{extension_id}/diagnostics")
async def get_extension_package_diagnostics(extension_id: str):
    try:
        return _safe_extension_diagnostics_payload(extension_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc


@router.get("/extensions/channel-routing")
async def get_channel_routing():
    state_payload = load_extension_state_payload()
    return _channel_routing_response(state_payload)


@router.put("/extensions/channel-routing")
async def update_channel_routing(req: ChannelRoutingUpdateRequest, request: Request):
    operator = _require_authenticated_capability_operator(request)
    tokens = set_runtime_context(
        operator.session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, operator.session_id),
    )
    try:
        state_payload = load_extension_state_payload()
        for route, binding in req.bindings.items():
            set_channel_route_binding(
                state_payload,
                route=route,
                primary_transport=binding.primary_transport,
                fallback_transport=binding.fallback_transport,
            )
        _assert_extension_runtime_not_revoked()
        save_extension_state_payload(state_payload)
        await log_integration_event(
            integration_type="channel_routing",
            name="observer_delivery",
            outcome="updated",
            details={"routes": sorted(req.bindings.keys())},
        )
        return _channel_routing_response(state_payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        reset_runtime_context(tokens)


@router.get("/extensions/{extension_id}")
async def get_extension_package(extension_id: str):
    try:
        return {"extension": _redact_lifecycle_api_value(get_extension(extension_id))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc


@router.get("/extensions/{extension_id}/lifecycle")
async def get_extension_package_lifecycle(extension_id: str):
    try:
        return _redact_lifecycle_api_value(extension_lifecycle_status(extension_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc


@router.post("/extensions/{extension_id}/review")
async def review_extension_package(
    extension_id: str,
    req: ExtensionLifecycleReasonRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = get_extension(extension_id)
        await _require_extension_lifecycle_approval(
            "review",
            preview,
            session_id=active_session_id,
            redact_paths=True,
        )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_registered_extension_effect_identity(
                extension_id,
                preview,
                action="review",
            )
            _assert_extension_runtime_not_revoked()
            result = record_extension_review(
                extension_id,
                reviewed_by=_operator_actor(operator),
                reason=req.reason,
            )
        await _log_extension_lifecycle_event(
            action="review",
            outcome="succeeded",
            preview=result.get("extension"),
            path=extension_id,
            redact_paths=True,
            extra_details={"receipt_id": result.get("receipt", {}).get("id")},
        )
        return result
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="review",
            outcome="failed",
            path=extension_id,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="review",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/extensions/{extension_id}/quarantine")
async def quarantine_extension_package(
    extension_id: str,
    req: ExtensionLifecycleReasonRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = get_extension(extension_id)
        await _require_extension_lifecycle_approval(
            "quarantine",
            preview,
            session_id=active_session_id,
            redact_paths=True,
        )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_registered_extension_effect_identity(
                extension_id,
                preview,
                action="quarantine",
            )
            _assert_extension_runtime_not_revoked()
            result = quarantine_extension(
                extension_id,
                reason=req.reason or "operator quarantine",
                actor=_operator_actor(operator),
            )
        await _log_extension_lifecycle_event(
            action="quarantine",
            outcome="succeeded",
            preview=result.get("extension"),
            path=extension_id,
            redact_paths=True,
            extra_details={"receipt_id": result.get("receipt", {}).get("id")},
        )
        return result
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="quarantine",
            outcome="failed",
            path=extension_id,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="quarantine",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/extensions/{extension_id}/reentry")
async def reenter_extension_package(
    extension_id: str,
    req: ExtensionLifecycleReasonRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = get_extension(extension_id)
        await _require_extension_lifecycle_approval(
            "reentry",
            preview,
            session_id=active_session_id,
            redact_paths=True,
        )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_registered_extension_effect_identity(
                extension_id,
                preview,
                action="reentry",
            )
            _assert_extension_runtime_not_revoked()
            result = reenter_extension(
                extension_id,
                reviewed_by=_operator_actor(operator),
                reason=req.reason,
            )
        await _log_extension_lifecycle_event(
            action="reentry",
            outcome="succeeded",
            preview=result.get("extension"),
            path=extension_id,
            redact_paths=True,
            extra_details={"receipt_id": result.get("receipt", {}).get("id")},
        )
        return result
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="reentry",
            outcome="failed",
            path=extension_id,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="reentry",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/extensions/{extension_id}/rollback")
async def rollback_extension_package(
    extension_id: str,
    req: ExtensionRollbackRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = get_extension(extension_id)
        lifecycle = extension_lifecycle_status(extension_id)
        snapshots = lifecycle.get("rollback", {}).get("snapshots")
        snapshot = next(
            (
                item for item in snapshots
                if isinstance(item, dict)
                and (not req.snapshot_id or item.get("id") == req.snapshot_id)
            ),
            None,
        ) if isinstance(snapshots, list) else None
        if not isinstance(snapshot, dict):
            raise ValueError(f"extension '{extension_id}' has no rollback snapshot")
        await _require_extension_lifecycle_approval(
            "rollback",
            {
                **preview,
                "target_reference": str(snapshot.get("id") or ""),
                "target_name": str(snapshot.get("version") or "rollback snapshot"),
                "target_type": "rollback_snapshot",
            },
            fingerprint_context={
                "snapshot_id": snapshot.get("id"),
                "restored_version": snapshot.get("version"),
                "restored_digest": snapshot.get("digest"),
                "snapshot_path_hash": _content_hash(str(snapshot.get("path") or "")),
            },
            session_id=active_session_id,
            redact_paths=True,
        )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_rollback_effect_identity(extension_id, preview, snapshot)
            _assert_extension_runtime_not_revoked()
            # Pass the approved snapshot id even when the request omitted one;
            # the lifecycle effect must restore the exact approved record.
            result = rollback_extension(
                extension_id,
                snapshot_id=str(snapshot.get("id") or ""),
            )
        await _log_extension_lifecycle_event(
            action="rollback",
            outcome="succeeded",
            preview=result.get("extension"),
            path=extension_id,
            redact_paths=True,
            extra_details={"receipt_id": result.get("receipt", {}).get("id")},
        )
        return result
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="rollback",
            outcome="failed",
            path=extension_id,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="rollback",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.get("/extensions/{extension_id}/connectors")
async def list_extension_package_connectors(extension_id: str):
    try:
        return _redact_lifecycle_api_value(list_extension_connectors(extension_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc


@router.post("/extensions/{extension_id}/connectors/test")
async def test_extension_package_connector(
    extension_id: str,
    req: ExtensionConnectorTestRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        try:
            preview = get_extension(extension_id)
            connector = get_extension_connector(extension_id, req.reference)
        except KeyError as exc:
            detail = (
                f"Extension '{extension_id}' not found"
                if str(exc) == f"'{extension_id}'"
                else f"Connector reference '{req.reference}' is not part of extension '{extension_id}'"
            )
            raise HTTPException(
                status_code=404,
                detail=_redact_extension_error(detail),
            ) from exc

        connector_type = str(connector.get("type") or "")
        health = connector.get("health") if isinstance(connector.get("health"), dict) else None
        if connector_type == "mcp_servers":
            await _ensure_extension_rest_authorized(request, revocation_scope)
            with _extension_effect_guard():
                current_preview = _assert_connector_effect_identity(
                    extension_id,
                    req.reference,
                    preview,
                    action="test",
                )
                current_connector = _extension_contribution(current_preview, req.reference)
                if isinstance(current_connector, dict):
                    connector = current_connector
                _assert_extension_runtime_not_revoked()
            result = await _test_extension_mcp_connector(connector)
            await _ensure_extension_rest_authorized(request, revocation_scope)
            return result

        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            current_preview = _assert_connector_effect_identity(
                extension_id,
                req.reference,
                preview,
                action="test",
            )
            current_connector = _extension_contribution(current_preview, req.reference)
            if isinstance(current_connector, dict):
                connector = current_connector
            _assert_extension_runtime_not_revoked()
        connector_type = str(connector.get("type") or "")
        health = connector.get("health") if isinstance(connector.get("health"), dict) else None
        await log_integration_event(
            integration_type="extension_connector_test",
            name=_redact_extension_error(str(connector.get("name") or req.reference)),
            outcome="succeeded" if isinstance(health, dict) and bool(health.get("ready")) else "skipped",
            details=_redact_lifecycle_receipt_value({
                "status": str(health.get("state") if isinstance(health, dict) else connector.get("status") or "unknown"),
                "extension_id": extension_id,
                "reference": req.reference,
                "connector_type": connector_type,
            }),
        )
        return {
            "status": str(health.get("state") if isinstance(health, dict) else connector.get("status") or "unknown"),
            "message": _redact_extension_error(
                str(health.get("summary") if isinstance(health, dict) else connector.get("status") or "Connector status")
            ),
            "health": _redact_lifecycle_api_value(health),
        }
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/extensions/{extension_id}/connectors/enabled")
async def set_extension_package_connector_enabled(
    extension_id: str,
    req: ExtensionConnectorToggleRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = get_extension(extension_id)
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
            "target_reference": req.reference,
            "target_name": target_connector.get("name"),
            "target_type": target_connector.get("type"),
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
        if req.enabled:
            if preview.get("status") != "ready":
                raise ValueError(
                    f"extension '{extension_id}' is degraded and cannot enable packaged connectors until validation issues are fixed"
                )
            await _require_extension_lifecycle_approval(
                "enable",
                connector_preview,
                session_id=active_session_id,
                redact_paths=True,
            )
        else:
            await _require_extension_lifecycle_approval(
                "disable",
                connector_preview,
                session_id=active_session_id,
                redact_paths=True,
            )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_connector_effect_identity(
                extension_id,
                req.reference,
                connector_preview,
                action="enable" if req.enabled else "disable",
            )
            _assert_extension_runtime_not_revoked()
            result = set_extension_connector_enabled(extension_id, req.reference, enabled=req.enabled)
        await _log_extension_lifecycle_event(
            action="enable" if req.enabled else "disable",
            outcome="succeeded",
            preview=result.get("extension"),
            path=extension_id,
            redact_paths=True,
            extra_details={
                "reference": req.reference,
                "changed": result.get("changed"),
            },
        )
        return {
            "status": "enabled" if req.enabled else "disabled",
            **result,
        }
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
            error=_redact_extension_error(detail),
            redact_paths=True,
            extra_details={"reference": req.reference},
        )
        raise HTTPException(status_code=404, detail=_redact_extension_error(detail)) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="enable" if req.enabled else "disable",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
            extra_details={"reference": req.reference},
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc

    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.get("/extensions/{extension_id}/source")
async def get_extension_package_source(extension_id: str, reference: str):
    try:
        return get_extension_source(extension_id, reference)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/extensions/{extension_id}/source")
async def save_extension_package_source(
    extension_id: str,
    req: ExtensionSourceSaveRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    source_preview: dict[str, Any] | None = None
    approval_context: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        source_preview = get_extension_source(extension_id, req.reference)
        preview = source_preview.get("extension") if isinstance(source_preview, dict) else None
        if isinstance(preview, dict):
            approval_context = _source_save_request_approval_context(
                source_preview,
                content=req.content,
            )
            await _require_extension_lifecycle_approval(
                "save_source",
                {
                    **preview,
                    "target_reference": req.reference,
                    "target_name": req.reference,
                    "target_type": "source_file",
                },
                fingerprint_context=approval_context,
                session_id=active_session_id,
                summary_suffix="for requested source changes",
                redact_paths=True,
            )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            if not isinstance(source_preview, dict):
                raise ValueError(
                    "extension source identity is unavailable; retry the lifecycle action"
                )
            _assert_source_effect_identity(
                extension_id,
                req.reference,
                source_preview,
                req.content,
                approval_context,
            )
            _assert_extension_runtime_not_revoked()
            payload = save_extension_source(extension_id, req.reference, req.content)
        await _log_extension_lifecycle_event(
            action="save_source",
            outcome="succeeded",
            preview=payload.get("extension"),
            path=extension_id,
            redact_paths=True,
            extra_details={"reference": req.reference},
        )
        return payload
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="save_source",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
            extra_details={"reference": req.reference},
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="save_source",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
            extra_details={"reference": req.reference},
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/extensions/validate")
async def validate_extension_package_path(req: ExtensionPathRequest):
    try:
        payload = validate_extension_path(req.path)
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="validate",
            outcome="failed",
            path=req.path,
            error=_redact_extension_error(exc),
        )
        raise HTTPException(status_code=400, detail=_redact_extension_error(exc)) from exc
    await _log_extension_lifecycle_event(
        action="validate",
        outcome="succeeded",
        preview=payload,
        path=req.path,
    )
    return payload


@router.post("/extensions/install", status_code=201)
async def install_extension_package(req: ExtensionPathRequest, request: Request):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = validate_extension_path(req.path)
        if not preview.get("ok", False):
            raise ValueError("extension package failed validation")
        lifecycle_plan = preview.get("lifecycle_plan")
        if isinstance(lifecycle_plan, dict) and lifecycle_plan.get("recommended_action") == "update":
            extension_id = preview.get("extension_id") or preview.get("id") or "extension"
            raise ValueError(
                f"extension '{extension_id}' is already installed; use update to replace the workspace package"
            )
        await _require_extension_lifecycle_approval(
            "install",
            preview,
            session_id=active_session_id,
            redact_paths=True,
        )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_extension_path_effect_identity(
                req.path,
                preview,
                action="install",
            )
            _assert_extension_runtime_not_revoked()
            extension = install_extension_path(req.path)
        await _log_extension_lifecycle_event(
            action="install",
            outcome="succeeded",
            preview=extension,
            path=req.path,
            redact_paths=True,
            extra_details={
                "location": extension.get("location"),
            },
        )
        return {"status": "installed", "extension": extension}
    except FileExistsError as exc:
        await _log_extension_lifecycle_event(
            action="install",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=409, detail=_redact_extension_error(exc)) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="install",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/extensions/update")
async def update_extension_package(req: ExtensionPathRequest, request: Request):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = validate_extension_path(req.path)
        if not preview.get("ok", False):
            raise ValueError("extension package failed validation")
        lifecycle_plan = preview.get("lifecycle_plan")
        if not isinstance(lifecycle_plan, dict) or lifecycle_plan.get("recommended_action") != "update":
            extension_id = preview.get("extension_id") or preview.get("id") or "extension"
            raise ValueError(
                f"extension '{extension_id}' is not updateable from this package path"
            )
        if lifecycle_plan.get("version_relation") == "downgrade":
            extension_id = preview.get("extension_id") or preview.get("id") or "extension"
            raise ValueError(
                f"extension '{extension_id}' downgrade requires explicit downgrade lifecycle control"
            )
        await _require_extension_lifecycle_approval(
            "update",
            preview,
            session_id=active_session_id,
            redact_paths=True,
        )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_extension_path_effect_identity(
                req.path,
                preview,
                action="update",
            )
            _assert_extension_runtime_not_revoked()
            extension = update_extension_path(req.path)
        await _log_extension_lifecycle_event(
            action="update",
            outcome="succeeded",
            preview=extension,
            path=req.path,
            redact_paths=True,
            extra_details={
                "location": extension.get("location"),
            },
        )
        return {"status": "updated", "extension": extension}
    except KeyError as exc:
        extension_id = preview.get("extension_id") if isinstance(preview, dict) else req.path
        await _log_extension_lifecycle_event(
            action="update",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="update",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/extensions/{extension_id}/enable")
async def enable_extension_package(extension_id: str, request: Request):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = get_extension(extension_id)
        if preview.get("status") != "ready":
            if preview.get("status") == "quarantined":
                raise ValueError(
                    f"extension '{extension_id}' is quarantined and requires re-entry review before enable"
                )
            raise ValueError(
                f"extension '{extension_id}' is degraded and cannot be enabled until validation issues are fixed"
            )
        await _require_extension_lifecycle_approval(
            "enable",
            preview,
            session_id=active_session_id,
            redact_paths=True,
        )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_registered_extension_effect_identity(
                extension_id,
                preview,
                action="enable",
            )
            _assert_extension_runtime_not_revoked()
            result = enable_extension(extension_id)
        await _log_extension_lifecycle_event(
            action="enable",
            outcome="succeeded",
            preview=result.get("extension"),
            path=extension_id,
            redact_paths=True,
            extra_details={
                "changed": result["changed"],
                "changed_count": len(result.get("changed", [])),
            },
        )
        return {"status": "enabled", **result}
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="enable",
            outcome="failed",
            path=extension_id,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="enable",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/extensions/{extension_id}/disable")
async def disable_extension_package(extension_id: str, request: Request):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = get_extension(extension_id)
        await _require_extension_lifecycle_approval(
            "disable",
            preview,
            session_id=active_session_id,
            redact_paths=True,
        )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_registered_extension_effect_identity(
                extension_id,
                preview,
                action="disable",
            )
            _assert_extension_runtime_not_revoked()
            result = disable_extension(extension_id)
        await _log_extension_lifecycle_event(
            action="disable",
            outcome="succeeded",
            preview=result.get("extension"),
            path=extension_id,
            redact_paths=True,
            extra_details={
                "changed": result["changed"],
                "changed_count": len(result.get("changed", [])),
            },
        )
        return {"status": "disabled", **result}
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="disable",
            outcome="failed",
            path=extension_id,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="disable",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/extensions/{extension_id}/configure")
async def configure_extension_package(
    extension_id: str,
    req: ExtensionConfigRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    approval_context: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = get_extension(extension_id)
        approval_context = _configure_request_approval_context(preview, req.config)
        if approval_context is not None:
            await _require_extension_lifecycle_approval(
                "configure",
                preview,
                fingerprint_context=approval_context,
                session_id=active_session_id,
                summary_suffix="for requested config changes",
                redact_paths=True,
            )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_config_effect_identity(
                extension_id,
                preview,
                req.config,
                approval_context,
            )
            _assert_extension_runtime_not_revoked()
            extension = configure_extension(extension_id, req.config)
        await _log_extension_lifecycle_event(
            action="configure",
            outcome="succeeded",
            preview=extension or preview,
            path=extension_id,
            redact_paths=True,
            extra_details={"config_keys": sorted(req.config.keys())},
        )
        return {"status": "configured", "extension": extension}
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="configure",
            outcome="failed",
            path=extension_id,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
            extra_details={"config_keys": sorted(req.config.keys())},
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="configure",
            outcome="failed",
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
            extra_details={"config_keys": sorted(req.config.keys())},
        )
        raise HTTPException(status_code=422, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.delete("/extensions/{extension_id}")
async def remove_extension_package(extension_id: str, request: Request):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    preview: dict[str, Any] | None = None
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        preview = get_extension(extension_id)
        await _require_extension_lifecycle_approval(
            "remove",
            preview,
            session_id=active_session_id,
            redact_paths=True,
        )
        await _ensure_extension_rest_authorized(request, revocation_scope)
        with _extension_effect_guard():
            _assert_registered_extension_effect_identity(
                extension_id,
                preview,
                action="remove",
            )
            _assert_extension_runtime_not_revoked()
            remove_extension(extension_id)
        await _log_extension_lifecycle_event(
            action="remove",
            outcome="succeeded",
            preview=preview,
            path=extension_id,
            redact_paths=True,
        )
        return {"status": "removed", "name": extension_id}
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="remove",
            outcome="failed",
            path=extension_id,
            error=_redact_extension_error(f"Extension '{extension_id}' not found"),
            redact_paths=True,
        )
        raise HTTPException(
            status_code=404,
            detail=_redact_extension_error(f"Extension '{extension_id}' not found"),
        ) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="remove",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=_redact_extension_error(exc),
            redact_paths=True,
        )
        raise HTTPException(status_code=409, detail=_redact_extension_error(exc)) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)
