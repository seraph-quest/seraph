"""Persisted extension runtime state helpers."""

from __future__ import annotations

import json
import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from config.settings import settings

STATE_FILE_NAME = "extensions-state.json"
_REASON_MAX_LENGTH = 160
_SENSITIVE_DETAIL_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "x-api-key",
    "auth_header",
    "credential",
    "error",
    "headers",
    "message",
    "password",
    "private_key",
    "path",
    "reason",
    "resume_message",
    "secret",
    "summary",
    "token",
    "url",
}
_ERROR_SCOPE_KEYS = {"error", "original_error", "cause", "details", "exception", "failure"}
_SAFE_ERROR_KEYS = {
    "code",
    "type",
    "error_code",
    "error_type",
    "reason_code",
    "status_code",
}
_PRIVATE_PATH_PATTERN = re.compile(
    r"(^|[\s'\"=:])"
    r"((?:/(?:Users|private|tmp|var|home|etc|Volumes|opt|run|srv)/[^\s'\",;)]*"
    r"|~/?[^\s'\",;)]*"
    r"|[A-Za-z]:\\[^\s'\",;)]*))"
)
_EXTERNAL_URL_PATTERN = re.compile(r"https?://[^\s'\",;)\]}]+", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(authorization|api[_-]?key|access[_-]?token|client[_-]?secret|password|secret|token)"
    r"(\s*[:=]\s*)[^\s,;]+"
)
_SENSITIVE_BARE_VALUE_PATTERN = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:secret|token|password)[-_][A-Za-z0-9._~+/=-]{6,})"
)


def _safe_reason(reason: Any, *, default: str = "operator lifecycle request") -> str:
    """Persist a bounded reason without retaining operator-supplied text."""

    text = " ".join(str(reason or "").split())[:_REASON_MAX_LENGTH]
    if not text:
        return default
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{default} (input:{digest})"


def _redacted_text(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"[redacted:{digest}]"


def _redact_inline_text(value: str) -> str:
    """Remove identifiable paths, URLs, and inline secret assignments."""
    text = _PRIVATE_PATH_PATTERN.sub(
        lambda match: f"{match.group(1)}[private path:{hashlib.sha256(match.group(2).encode('utf-8')).hexdigest()[:16]}]",
        value,
    )
    text = _EXTERNAL_URL_PATTERN.sub(
        lambda match: f"[redacted url:{hashlib.sha256(match.group(0).encode('utf-8')).hexdigest()[:16]}]",
        text,
    )
    text = _SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]",
        text,
    )
    return _SENSITIVE_BARE_VALUE_PATTERN.sub("[redacted]", text)


def redact_lifecycle_error_text(value: Any) -> str:
    """Redact a scalar lifecycle error using the shared text contract."""
    return _redact_inline_text(str(value))


def redact_lifecycle_receipt_value(
    value: Any,
    *,
    key: str | None = None,
    _error_scope: bool = False,
) -> Any:
    """Recursively redact lifecycle state and API receipt values.

    Error-like branches are treated as sensitive by default, including
    ``original_error``, ``cause``, and nested ``details``. Only explicit safe
    ``code``/``type`` fields survive so operators retain a stable failure
    classification without exposing exception text, paths, or credentials.
    """
    key_text = str(key or "").strip().lower()
    sensitive_key = (
        key_text in _SENSITIVE_DETAIL_KEYS
        or "reason" in key_text
        or "url" in key_text
        or "secret" in key_text
        or "token" in key_text
        or "credential" in key_text
    )
    error_scope = _error_scope or key_text in _ERROR_SCOPE_KEYS
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for item_key, item in value.items():
            normalized_key = str(item_key)
            normalized_lower = normalized_key.lower()
            if error_scope and normalized_lower in _SAFE_ERROR_KEYS:
                result[normalized_key] = item if isinstance(item, (str, int, float, bool)) or item is None else str(item)
                continue
            result[normalized_key] = redact_lifecycle_receipt_value(
                item,
                key=normalized_key,
                _error_scope=error_scope,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            redact_lifecycle_receipt_value(item, key=key, _error_scope=error_scope)
            for item in value
        ]
    if isinstance(value, (bytes, bytearray)):
        return f"[binary:{len(value)} bytes]"
    if isinstance(value, str):
        if error_scope or sensitive_key:
            return _redacted_text(value)
        return _redact_inline_text(value)
    return value


def _safe_detail_value(key: str, value: Any) -> Any:
    return redact_lifecycle_receipt_value(value, key=key)


def _safe_lifecycle_details(details: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    return {
        str(key): _safe_detail_value(str(key), value)
        for key, value in details.items()
    }


def state_path() -> str:
    return os.path.join(settings.workspace_dir, STATE_FILE_NAME)


def load_extension_state_payload() -> dict[str, Any]:
    path = state_path()
    if not os.path.exists(path):
        return {"extensions": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"extensions": {}}
    if not isinstance(payload, dict):
        return {"extensions": {}}
    extensions = payload.get("extensions")
    if not isinstance(extensions, dict):
        payload["extensions"] = {}
    return payload


def save_extension_state_payload(payload: dict[str, Any]) -> None:
    payload = payload if isinstance(payload, dict) else {"extensions": {}}
    extensions = payload.get("extensions")
    if not isinstance(extensions, dict):
        payload["extensions"] = {}
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def extension_state_entries(payload: dict[str, Any]) -> dict[str, Any]:
    extensions = payload.get("extensions")
    if not isinstance(extensions, dict):
        payload["extensions"] = {}
        return payload["extensions"]
    return extensions


def extension_state_entry(
    payload: dict[str, Any],
    extension_id: str,
    *,
    create: bool = False,
) -> dict[str, Any] | None:
    extensions = extension_state_entries(payload)
    entry = extensions.get(extension_id)
    if isinstance(entry, dict):
        return entry
    if not create:
        return None
    entry = {}
    extensions[extension_id] = entry
    return entry


def extension_governance_entry(
    state_entry: dict[str, Any] | None,
    *,
    create: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(state_entry, dict):
        return None
    governance = state_entry.get("governance")
    if isinstance(governance, dict):
        return governance
    if not create:
        return None
    governance = {}
    state_entry["governance"] = governance
    return governance


def mark_extension_governance_reviewed(
    payload: dict[str, Any],
    extension_id: str,
    *,
    digest: str,
    key_id: str,
    permission_fingerprint: str | None = None,
    reviewed_by: str | None = None,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    state_entry = extension_state_entry(payload, extension_id, create=True)
    assert state_entry is not None
    governance = extension_governance_entry(state_entry, create=True)
    assert governance is not None
    governance["review_status"] = "approved"
    governance["reviewed_digest"] = digest
    governance["reviewed_key_id"] = key_id
    if permission_fingerprint:
        governance["reviewed_permission_fingerprint"] = permission_fingerprint
    if reviewed_by:
        governance["reviewed_by"] = reviewed_by
    if reviewed_at:
        governance["reviewed_at"] = reviewed_at
    governance.pop("revoked", None)
    return governance


def revoke_extension_governance(
    payload: dict[str, Any],
    extension_id: str,
    *,
    digest: str | None = None,
    key_id: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    state_entry = extension_state_entry(payload, extension_id, create=True)
    assert state_entry is not None
    governance = extension_governance_entry(state_entry, create=True)
    assert governance is not None
    governance["revoked"] = True
    if digest:
        revoked_digests = governance.get("revoked_digests")
        if not isinstance(revoked_digests, list):
            revoked_digests = []
            governance["revoked_digests"] = revoked_digests
        if digest not in revoked_digests:
            revoked_digests.append(digest)
    if key_id:
        revoked_key_ids = governance.get("revoked_key_ids")
        if not isinstance(revoked_key_ids, list):
            revoked_key_ids = []
            governance["revoked_key_ids"] = revoked_key_ids
        if key_id not in revoked_key_ids:
            revoked_key_ids.append(key_id)
    if reason:
        governance["revocation_reason"] = _safe_reason(reason, default="operator governance revocation")
    return governance


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extension_lifecycle_entry(
    state_entry: dict[str, Any] | None,
    *,
    create: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(state_entry, dict):
        return None
    lifecycle = state_entry.get("lifecycle")
    if isinstance(lifecycle, dict):
        return lifecycle
    if not create:
        return None
    lifecycle = {}
    state_entry["lifecycle"] = lifecycle
    return lifecycle


def append_extension_lifecycle_event(
    payload: dict[str, Any],
    extension_id: str,
    *,
    action: str,
    status: str,
    actor: str = "operator",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_entry = extension_state_entry(payload, extension_id, create=True)
    assert state_entry is not None
    lifecycle = extension_lifecycle_entry(state_entry, create=True)
    assert lifecycle is not None
    events = lifecycle.get("events")
    if not isinstance(events, list):
        events = []
        lifecycle["events"] = events
    event = {
        "id": f"extension-lifecycle:{extension_id}:{action}:{len(events) + 1}",
        "action": action,
        "status": status,
        "actor": actor,
        "created_at": _utc_now(),
        "details": _safe_lifecycle_details(details),
    }
    events.append(event)
    lifecycle["last_event"] = event
    return event


def add_extension_rollback_snapshot(
    payload: dict[str, Any],
    extension_id: str,
    *,
    snapshot_id: str,
    snapshot_path: str,
    version: str | None,
    digest: str | None,
    reason: str,
    created_by: str = "system",
) -> dict[str, Any]:
    state_entry = extension_state_entry(payload, extension_id, create=True)
    assert state_entry is not None
    lifecycle = extension_lifecycle_entry(state_entry, create=True)
    assert lifecycle is not None
    snapshots = lifecycle.get("rollback_snapshots")
    if not isinstance(snapshots, list):
        snapshots = []
        lifecycle["rollback_snapshots"] = snapshots
    snapshot = {
        "id": snapshot_id,
        "path": snapshot_path,
        "version": version,
        "digest": digest,
        "reason": _safe_reason(reason, default="rollback snapshot"),
        "created_by": created_by,
        "created_at": _utc_now(),
    }
    snapshots.insert(0, snapshot)
    lifecycle["rollback_snapshots"] = snapshots[:10]
    lifecycle["last_rollback_snapshot"] = snapshot
    return snapshot


def set_extension_quarantine(
    payload: dict[str, Any],
    extension_id: str,
    *,
    active: bool,
    reason: str,
    actor: str = "operator",
    digest: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    state_entry = extension_state_entry(payload, extension_id, create=True)
    assert state_entry is not None
    lifecycle = extension_lifecycle_entry(state_entry, create=True)
    assert lifecycle is not None
    quarantine = {
        "active": active,
        "state": "quarantined" if active else "cleared",
        "reason": _safe_reason(reason),
        "actor": actor,
        "digest": digest,
        "version": version,
        "updated_at": _utc_now(),
    }
    lifecycle["quarantine"] = quarantine
    return quarantine


def extension_quarantine_active(state_entry: dict[str, Any] | None) -> bool:
    lifecycle = extension_lifecycle_entry(state_entry, create=False)
    quarantine = lifecycle.get("quarantine") if isinstance(lifecycle, dict) else None
    return isinstance(quarantine, dict) and bool(quarantine.get("active"))


def connector_state_entry(
    state_entry: dict[str, Any] | None,
    reference: str,
    *,
    create: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(state_entry, dict):
        return None
    connector_state = state_entry.get("connector_state")
    if not isinstance(connector_state, dict):
        if not create:
            return None
        connector_state = {}
        state_entry["connector_state"] = connector_state
    entry = connector_state.get(reference)
    if isinstance(entry, dict):
        return entry
    if not create:
        return None
    entry = {}
    connector_state[reference] = entry
    return entry


def connector_enabled_override(
    state_entry: dict[str, Any] | None,
    reference: str,
) -> bool | None:
    entry = connector_state_entry(state_entry, reference, create=False)
    if not isinstance(entry, dict):
        return None
    enabled = entry.get("enabled")
    return enabled if isinstance(enabled, bool) else None


def set_connector_enabled_override(
    payload: dict[str, Any],
    extension_id: str,
    reference: str,
    *,
    enabled: bool,
) -> None:
    state_entry = extension_state_entry(payload, extension_id, create=True)
    assert state_entry is not None
    connector_entry = connector_state_entry(state_entry, reference, create=True)
    assert connector_entry is not None
    connector_entry["enabled"] = enabled


def connector_enabled_overrides(
    state_by_id: dict[str, Any] | None,
) -> dict[tuple[str, str], bool]:
    if not isinstance(state_by_id, dict):
        return {}
    overrides: dict[tuple[str, str], bool] = {}
    for extension_id, raw_entry in state_by_id.items():
        if not isinstance(extension_id, str) or not isinstance(raw_entry, dict):
            continue
        connector_state = raw_entry.get("connector_state")
        if not isinstance(connector_state, dict):
            continue
        for reference, raw_state in connector_state.items():
            if not isinstance(reference, str) or not isinstance(raw_state, dict):
                continue
            enabled = raw_state.get("enabled")
            if isinstance(enabled, bool):
                overrides[(extension_id, reference)] = enabled
    return overrides


def node_adapter_pairing_entry(
    state_entry: dict[str, Any] | None,
    *,
    reference: str,
    name: str,
    create: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(state_entry, dict):
        return None
    pairings = state_entry.get("node_pairings")
    if not isinstance(pairings, dict):
        if not create:
            return _legacy_node_adapter_pairing_entry(state_entry, reference=reference, name=name)
        pairings = {}
        state_entry["node_pairings"] = pairings
    for key in (reference, name):
        entry = pairings.get(key)
        if isinstance(entry, dict):
            return entry
    if not create:
        return _legacy_node_adapter_pairing_entry(state_entry, reference=reference, name=name)
    entry: dict[str, Any] = {"name": name, "reference": reference}
    pairings[reference] = entry
    return entry


def _legacy_node_adapter_pairing_entry(
    state_entry: dict[str, Any],
    *,
    reference: str,
    name: str,
) -> dict[str, Any] | None:
    pairings = state_entry.get("pairings")
    if isinstance(pairings, dict):
        node_pairings = pairings.get("node_adapters")
        if isinstance(node_pairings, dict):
            for key in (reference, name):
                entry = node_pairings.get(key)
                if isinstance(entry, dict):
                    return entry
    connector_state = state_entry.get("connector_state")
    if isinstance(connector_state, dict):
        connector_entry = connector_state.get(reference)
        if isinstance(connector_entry, dict) and isinstance(connector_entry.get("pairing"), dict):
            return connector_entry["pairing"]
    return None


def set_node_adapter_pairing_entry(
    payload: dict[str, Any],
    *,
    extension_id: str,
    reference: str,
    name: str,
    pairing: dict[str, Any],
) -> dict[str, Any]:
    state_entry = extension_state_entry(payload, extension_id, create=True)
    assert state_entry is not None
    entry = node_adapter_pairing_entry(state_entry, reference=reference, name=name, create=True)
    assert entry is not None
    entry.clear()
    entry.update(pairing)
    entry.setdefault("name", name)
    entry.setdefault("reference", reference)
    return entry


def revoke_node_adapter_pairing_entry(
    payload: dict[str, Any],
    *,
    extension_id: str,
    reference: str,
    name: str,
    reason: str = "",
    revoked_at: str = "",
) -> dict[str, Any]:
    state_entry = extension_state_entry(payload, extension_id, create=True)
    assert state_entry is not None
    entry = node_adapter_pairing_entry(state_entry, reference=reference, name=name, create=True)
    assert entry is not None
    entry.setdefault("name", name)
    entry.setdefault("reference", reference)
    entry["revoked"] = True
    entry["trusted"] = False
    entry["trust_state"] = "untrusted"
    entry["pairing_state"] = "revoked"
    if reason:
        entry["revocation_reason"] = _safe_reason(reason, default="node adapter revocation")
    if revoked_at:
        entry["revoked_at"] = revoked_at
    return entry


def clear_node_adapter_pairing_entry(
    payload: dict[str, Any],
    *,
    extension_id: str,
    reference: str,
    name: str,
) -> bool:
    state_entry = extension_state_entry(payload, extension_id, create=False)
    if not isinstance(state_entry, dict):
        return False
    removed = False
    pairings = state_entry.get("node_pairings")
    if isinstance(pairings, dict):
        for key in (reference, name):
            if key in pairings:
                pairings.pop(key, None)
                removed = True
    legacy_pairings = state_entry.get("pairings")
    if isinstance(legacy_pairings, dict):
        node_pairings = legacy_pairings.get("node_adapters")
        if isinstance(node_pairings, dict):
            for key in (reference, name):
                if key in node_pairings:
                    node_pairings.pop(key, None)
                    removed = True
    connector_state = state_entry.get("connector_state")
    if isinstance(connector_state, dict):
        connector_entry = connector_state.get(reference)
        if isinstance(connector_entry, dict) and "pairing" in connector_entry:
            connector_entry.pop("pairing", None)
            removed = True
    return removed
