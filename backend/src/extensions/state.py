"""Persisted extension runtime state helpers."""

from __future__ import annotations

import json
import os
from typing import Any

from config.settings import settings

STATE_FILE_NAME = "extensions-state.json"


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
        entry["revocation_reason"] = reason
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
