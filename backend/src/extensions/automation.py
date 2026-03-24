"""Automation-trigger inventory for extension-backed trigger surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.extensions.registry import ExtensionContributionRecord


@dataclass(frozen=True)
class AutomationTriggerInventoryEntry:
    extension_id: str
    name: str
    trigger_type: str
    description: str
    enabled: bool
    configured: bool
    config_keys: tuple[str, ...]
    schedule: str
    endpoint: str
    topic: str
    capabilities: tuple[str, ...]
    requires_network: bool
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


def list_automation_trigger_inventory(
    contributions: list["ExtensionContributionRecord"],
    *,
    state_by_id: dict[str, Any] | None = None,
    enabled_overrides: dict[tuple[str, str], bool] | None = None,
) -> list[AutomationTriggerInventoryEntry]:
    inventory: list[AutomationTriggerInventoryEntry] = []
    for contribution in contributions:
        if contribution.contribution_type != "automation_triggers":
            continue
        if isinstance(contribution.metadata.get("registry_conflict"), dict):
            continue
        name = contribution.metadata.get("name")
        trigger_type = contribution.metadata.get("trigger_type")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(trigger_type, str) or not trigger_type:
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
                trigger_bucket = raw_config.get("automation_triggers")
                if isinstance(trigger_bucket, dict):
                    candidate_entry = trigger_bucket.get(name)
                    if isinstance(candidate_entry, dict):
                        config_entry = candidate_entry
        config_fields = contribution.metadata.get("config_fields")
        config_field_list = config_fields if isinstance(config_fields, list) else []
        configured = not _required_config_missing(config_field_list, config_entry)
        if not enabled:
            runtime_state = "disabled"
        elif not configured:
            runtime_state = "requires_config"
        elif trigger_type == "webhook":
            runtime_state = "armed_webhook"
        else:
            runtime_state = "staged_runtime"
        inventory.append(
            AutomationTriggerInventoryEntry(
                extension_id=contribution.extension_id,
                name=name,
                trigger_type=trigger_type,
                description=str(contribution.metadata.get("description") or ""),
                enabled=enabled,
                configured=configured,
                config_keys=tuple(sorted(config_entry.keys())),
                schedule=str(contribution.metadata.get("schedule") or ""),
                endpoint=str(contribution.metadata.get("endpoint") or ""),
                topic=str(contribution.metadata.get("topic") or ""),
                capabilities=tuple(
                    item
                    for item in contribution.metadata.get("capabilities", [])
                    if isinstance(item, str) and item.strip()
                ) if isinstance(contribution.metadata.get("capabilities"), list) else (),
                requires_network=bool(contribution.metadata.get("requires_network", trigger_type != "cron")),
                runtime_state=runtime_state,
                reference=contribution.reference,
            )
        )
    return sorted(
        inventory,
        key=lambda item: (item.extension_id, item.trigger_type, item.name),
    )


def select_automation_trigger(
    contributions: list["ExtensionContributionRecord"],
    *,
    trigger_name: str,
    state_by_id: dict[str, Any] | None = None,
    enabled_overrides: dict[tuple[str, str], bool] | None = None,
) -> AutomationTriggerInventoryEntry | None:
    normalized = trigger_name.strip().casefold()
    for item in list_automation_trigger_inventory(
        contributions,
        state_by_id=state_by_id,
        enabled_overrides=enabled_overrides,
    ):
        if item.name.casefold() == normalized:
            return item
    return None
