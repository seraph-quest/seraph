"""Node-adapter inventory for staged device and companion reach."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.extensions.channels import build_presence_boundary_contract
from src.extensions.state import node_adapter_pairing_entry

if TYPE_CHECKING:
    from src.extensions.registry import ExtensionContributionRecord


@dataclass(frozen=True)
class NodeAdapterInventoryEntry:
    extension_id: str
    name: str
    adapter_kind: str
    description: str
    enabled: bool
    configured: bool
    config_keys: tuple[str, ...]
    capabilities: tuple[str, ...]
    requires_network: bool
    requires_daemon: bool
    runtime_state: str
    pairing_state: str
    trust_state: str
    paired: bool
    revoked: bool
    pairing: dict[str, Any]
    safe_follow_up_ready: bool
    presence_contract: dict[str, Any]
    reference: str


def _required_config_missing(config_fields: list[dict[str, Any]], config_entry: dict[str, Any]) -> bool:
    for field in config_fields:
        if not isinstance(field, dict):
            continue
        key = field.get("key")
        if not isinstance(key, str) or not key:
            continue
        if not bool(field.get("required", False)):
            continue
        value = config_entry.get(key)
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True
    return False


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _node_pairing_payload(raw_pairing: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw_pairing, dict) or not raw_pairing:
        return {
            "pairing_state": "unpaired",
            "trust_state": "unpaired",
            "paired": False,
            "revoked": False,
            "safe_follow_up_ready": False,
            "pairing_id": None,
            "device_id": None,
            "label": None,
            "scopes": [],
            "paired_at": None,
            "revoked_at": None,
            "revocation_reason": None,
        }

    revoked = bool(raw_pairing.get("revoked")) or str(raw_pairing.get("pairing_state") or "") == "revoked"
    trusted = raw_pairing.get("trusted")
    trust_state = str(raw_pairing.get("trust_state") or "").strip()
    if revoked:
        trust_state = "untrusted"
    elif trust_state:
        trust_state = trust_state
    elif trusted is False:
        trust_state = "untrusted"
    else:
        trust_state = "trusted"
    pairing_id = raw_pairing.get("pairing_id") or raw_pairing.get("id")
    device_id = raw_pairing.get("device_id") or raw_pairing.get("node_id")
    paired = not revoked and (
        bool(pairing_id)
        or bool(device_id)
        or bool(raw_pairing.get("paired_at"))
        or str(raw_pairing.get("pairing_state") or "") == "paired"
    )
    pairing_state = "revoked" if revoked else "paired" if paired else "unpaired"
    safe_follow_up_ready = pairing_state == "paired" and trust_state == "trusted"
    return {
        "pairing_state": pairing_state,
        "trust_state": trust_state,
        "paired": paired,
        "revoked": revoked,
        "safe_follow_up_ready": safe_follow_up_ready,
        "pairing_id": str(pairing_id) if pairing_id is not None else None,
        "device_id": str(device_id) if device_id is not None else None,
        "label": (
            str(raw_pairing.get("label") or raw_pairing.get("device_label") or raw_pairing.get("name") or "")
            or None
        ),
        "scopes": _string_list(raw_pairing.get("scopes") or raw_pairing.get("scope")),
        "paired_at": str(raw_pairing.get("paired_at")) if raw_pairing.get("paired_at") is not None else None,
        "revoked_at": str(raw_pairing.get("revoked_at")) if raw_pairing.get("revoked_at") is not None else None,
        "revocation_reason": (
            str(raw_pairing.get("revocation_reason") or raw_pairing.get("reason") or "") or None
        ),
    }


def _node_runtime_state(*, enabled: bool, configured: bool, adapter_kind: str, pairing: dict[str, Any]) -> str:
    if pairing["revoked"]:
        return "revoked"
    if pairing["paired"]:
        return "paired_staged"
    if adapter_kind in {"device", "camera", "notification"}:
        return "unpaired_staged"
    if not enabled:
        return "disabled"
    if not configured:
        return "requires_config"
    if adapter_kind == "canvas":
        return "staged_canvas"
    return "staged_link"


def list_node_adapter_inventory(
    contributions: list["ExtensionContributionRecord"],
    *,
    state_by_id: dict[str, Any] | None = None,
    enabled_overrides: dict[tuple[str, str], bool] | None = None,
) -> list[NodeAdapterInventoryEntry]:
    inventory: list[NodeAdapterInventoryEntry] = []
    for contribution in contributions:
        if contribution.contribution_type != "node_adapters":
            continue
        name = contribution.metadata.get("name")
        adapter_kind = contribution.metadata.get("adapter_kind")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(adapter_kind, str) or not adapter_kind:
            continue
        default_enabled = bool(contribution.metadata.get("default_enabled", False))
        enabled = (
            enabled_overrides.get((contribution.extension_id, contribution.reference), default_enabled)
            if enabled_overrides
            else default_enabled
        )
        state_entry = state_by_id.get(contribution.extension_id, {}) if isinstance(state_by_id, dict) else {}
        config_entry: dict[str, Any] = {}
        if isinstance(state_entry, dict):
            raw_config = state_entry.get("config")
            if isinstance(raw_config, dict):
                adapter_bucket = raw_config.get("node_adapters")
                if isinstance(adapter_bucket, dict):
                    candidate_entry = adapter_bucket.get(name)
                    if isinstance(candidate_entry, dict):
                        config_entry = candidate_entry
        config_fields = contribution.metadata.get("config_fields")
        config_field_list = config_fields if isinstance(config_fields, list) else []
        configured = not _required_config_missing(config_field_list, config_entry)
        raw_pairing = node_adapter_pairing_entry(
            state_entry if isinstance(state_entry, dict) else None,
            reference=contribution.reference,
            name=name,
            create=False,
        )
        pairing = _node_pairing_payload(raw_pairing)
        runtime_state = _node_runtime_state(
            enabled=enabled,
            configured=configured,
            adapter_kind=adapter_kind,
            pairing=pairing,
        )
        presence_contract = build_presence_boundary_contract(
            contribution_type="node_adapters",
            extension_id=contribution.extension_id,
            reference=contribution.reference,
            metadata=contribution.metadata,
            status=runtime_state,
            active=enabled,
            ready=pairing["safe_follow_up_ready"],
            pairing=pairing,
            follow_up_ready=pairing["safe_follow_up_ready"],
        )
        inventory.append(
            NodeAdapterInventoryEntry(
                extension_id=contribution.extension_id,
                name=name,
                adapter_kind=adapter_kind,
                description=str(contribution.metadata.get("description") or ""),
                enabled=enabled,
                configured=configured,
                config_keys=tuple(sorted(config_entry.keys())),
                capabilities=tuple(
                    item
                    for item in contribution.metadata.get("capabilities", [])
                    if isinstance(item, str) and item.strip()
                ) if isinstance(contribution.metadata.get("capabilities"), list) else (),
                requires_network=bool(contribution.metadata.get("requires_network", False)),
                requires_daemon=bool(contribution.metadata.get("requires_daemon", adapter_kind != "canvas")),
                runtime_state=runtime_state,
                pairing_state=pairing["pairing_state"],
                trust_state=pairing["trust_state"],
                paired=pairing["paired"],
                revoked=pairing["revoked"],
                pairing=pairing,
                safe_follow_up_ready=pairing["safe_follow_up_ready"],
                presence_contract=presence_contract,
                reference=contribution.reference,
            )
        )
    return sorted(inventory, key=lambda item: (item.extension_id, item.adapter_kind, item.name))
