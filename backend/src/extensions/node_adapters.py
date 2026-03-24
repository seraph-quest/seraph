"""Node-adapter inventory for staged device and companion reach."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
        if not enabled:
            runtime_state = "disabled"
        elif not configured:
            runtime_state = "requires_config"
        elif adapter_kind == "canvas":
            runtime_state = "staged_canvas"
        else:
            runtime_state = "staged_link"
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
                reference=contribution.reference,
            )
        )
    return sorted(inventory, key=lambda item: (item.extension_id, item.adapter_kind, item.name))
