"""Active browser-provider selection for packaged browser reach."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.extensions.registry import ExtensionContributionRecord


_PROVIDER_KIND_ORDER = {
    "local": 0,
    "browserbase": 1,
    "remote_cdp": 2,
    "extension_relay": 3,
}


@dataclass(frozen=True)
class ActiveBrowserProvider:
    extension_id: str
    name: str
    provider_kind: str
    description: str
    default_enabled: bool
    reference: str
    resolved_path: str | None
    manifest_root_index: int
    configured: bool
    config_keys: tuple[str, ...]
    requires_network: bool
    requires_daemon: bool
    capabilities: tuple[str, ...]


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


def select_active_browser_provider(
    contributions: list["ExtensionContributionRecord"],
    *,
    state_by_id: dict[str, Any] | None = None,
    enabled_overrides: dict[tuple[str, str], bool] | None = None,
    requested_name: str | None = None,
) -> ActiveBrowserProvider | None:
    selected: ActiveBrowserProvider | None = None
    requested = requested_name.strip().casefold() if isinstance(requested_name, str) and requested_name.strip() else None

    for contribution in contributions:
        if contribution.contribution_type != "browser_providers":
            continue
        name = contribution.metadata.get("name")
        provider_kind = contribution.metadata.get("provider_kind")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(provider_kind, str) or not provider_kind:
            continue
        if requested is not None and name.casefold() != requested:
            continue

        default_enabled = bool(contribution.metadata.get("default_enabled", True))
        enabled = enabled_overrides.get((contribution.extension_id, contribution.reference), default_enabled) if enabled_overrides else default_enabled
        if not enabled:
            continue

        state_entry = state_by_id.get(contribution.extension_id, {}) if isinstance(state_by_id, dict) else {}
        config_entry = {}
        if isinstance(state_entry, dict):
            raw_config = state_entry.get("config")
            if isinstance(raw_config, dict):
                provider_bucket = raw_config.get("browser_providers")
                if isinstance(provider_bucket, dict):
                    candidate_entry = provider_bucket.get(name)
                    if isinstance(candidate_entry, dict):
                        config_entry = candidate_entry

        config_fields = contribution.metadata.get("config_fields")
        config_field_list = config_fields if isinstance(config_fields, list) else []
        configured = not _required_config_missing(config_field_list, config_entry)

        candidate = ActiveBrowserProvider(
            extension_id=contribution.extension_id,
            name=name,
            provider_kind=provider_kind,
            description=str(contribution.metadata.get("description") or ""),
            default_enabled=default_enabled,
            reference=contribution.reference,
            resolved_path=(
                str(contribution.metadata.get("resolved_path"))
                if isinstance(contribution.metadata.get("resolved_path"), str)
                else None
            ),
            manifest_root_index=int(contribution.metadata.get("manifest_root_index", 999999)),
            configured=configured,
            config_keys=tuple(sorted(config_entry.keys())),
            requires_network=bool(contribution.metadata.get("requires_network", provider_kind != "local")),
            requires_daemon=bool(contribution.metadata.get("requires_daemon", provider_kind == "extension_relay")),
            capabilities=tuple(
                item
                for item in contribution.metadata.get("capabilities", [])
                if isinstance(item, str) and item.strip()
            ) if isinstance(contribution.metadata.get("capabilities"), list) else (),
        )
        if not candidate.configured:
            continue
        if selected is None:
            selected = candidate
            continue
        selected_priority = (
            selected.manifest_root_index,
            _PROVIDER_KIND_ORDER.get(selected.provider_kind, 999),
            selected.extension_id,
            selected.name,
        )
        candidate_priority = (
            candidate.manifest_root_index,
            _PROVIDER_KIND_ORDER.get(candidate.provider_kind, 999),
            candidate.extension_id,
            candidate.name,
        )
        if candidate_priority < selected_priority:
            selected = candidate

    return selected
