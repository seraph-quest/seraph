"""Active browser-provider selection and inventory for packaged browser reach."""

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


@dataclass(frozen=True)
class BrowserProviderInventoryEntry:
    extension_id: str
    name: str
    provider_kind: str
    description: str
    default_enabled: bool
    enabled: bool
    reference: str
    resolved_path: str | None
    manifest_root_index: int
    configured: bool
    config_keys: tuple[str, ...]
    requires_network: bool
    requires_daemon: bool
    capabilities: tuple[str, ...]
    execution_mode: str
    runtime_state: str
    selected: bool


_LOCAL_PROVIDER_ENTRY = BrowserProviderInventoryEntry(
    extension_id="seraph.runtime-browser",
    name="local-browser",
    provider_kind="local",
    description="Built-in local browser runtime available without packaged remote transport.",
    default_enabled=True,
    enabled=True,
    reference="runtime/local-browser",
    resolved_path=None,
    manifest_root_index=999999,
    configured=True,
    config_keys=(),
    requires_network=False,
    requires_daemon=False,
    capabilities=("extract", "html", "screenshot"),
    execution_mode="local_runtime",
    runtime_state="ready",
    selected=False,
)


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


def _provider_inventory_entry(
    contribution: "ExtensionContributionRecord",
    *,
    state_by_id: dict[str, Any] | None = None,
    enabled_overrides: dict[tuple[str, str], bool] | None = None,
    selected_name: str | None = None,
) -> BrowserProviderInventoryEntry | None:
    name = contribution.metadata.get("name")
    provider_kind = contribution.metadata.get("provider_kind")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(provider_kind, str) or not provider_kind:
        return None

    default_enabled = bool(contribution.metadata.get("default_enabled", True))
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
            provider_bucket = raw_config.get("browser_providers")
            if isinstance(provider_bucket, dict):
                candidate_entry = provider_bucket.get(name)
                if isinstance(candidate_entry, dict):
                    config_entry = candidate_entry

    config_fields = contribution.metadata.get("config_fields")
    config_field_list = config_fields if isinstance(config_fields, list) else []
    configured = not _required_config_missing(config_field_list, config_entry)
    execution_mode = "local_runtime" if provider_kind == "local" else "local_fallback"
    if not enabled:
        runtime_state = "disabled"
    elif not configured:
        runtime_state = "requires_config"
    elif provider_kind == "local":
        runtime_state = "ready"
    else:
        runtime_state = "staged_local_fallback"

    return BrowserProviderInventoryEntry(
        extension_id=contribution.extension_id,
        name=name,
        provider_kind=provider_kind,
        description=str(contribution.metadata.get("description") or ""),
        default_enabled=default_enabled,
        enabled=enabled,
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
        execution_mode=execution_mode,
        runtime_state=runtime_state,
        selected=selected_name == name,
    )


def list_browser_provider_inventory(
    contributions: list["ExtensionContributionRecord"],
    *,
    state_by_id: dict[str, Any] | None = None,
    enabled_overrides: dict[tuple[str, str], bool] | None = None,
) -> list[BrowserProviderInventoryEntry]:
    inventory: list[BrowserProviderInventoryEntry] = [_LOCAL_PROVIDER_ENTRY]
    for contribution in contributions:
        if contribution.contribution_type != "browser_providers":
            continue
        if isinstance(contribution.metadata.get("registry_conflict"), dict):
            continue
        entry = _provider_inventory_entry(
            contribution,
            state_by_id=state_by_id,
            enabled_overrides=enabled_overrides,
            selected_name=None,
        )
        if entry is not None:
            inventory.append(entry)
    inventory.sort(
        key=lambda item: (
            item.manifest_root_index,
            _PROVIDER_KIND_ORDER.get(item.provider_kind, 999),
            item.extension_id,
            item.name,
        )
    )
    selected = _select_active_provider_from_inventory(inventory)
    selected_name = selected.name if selected is not None else None
    display_inventory = [
        BrowserProviderInventoryEntry(
            **{
                **item.__dict__,
                "selected": (
                    item.name == selected_name
                    if selected_name is not None
                    else item.name == _LOCAL_PROVIDER_ENTRY.name
                ),
            }
        )
        for item in inventory
    ]
    return sorted(
        display_inventory,
        key=lambda item: (
            0 if item.name == _LOCAL_PROVIDER_ENTRY.name else 1,
            item.manifest_root_index,
            _PROVIDER_KIND_ORDER.get(item.provider_kind, 999),
            item.extension_id,
            item.name,
        ),
    )


def _select_active_provider_from_inventory(
    inventory: list[BrowserProviderInventoryEntry],
    *,
    requested_name: str | None = None,
) -> ActiveBrowserProvider | None:
    requested = requested_name.strip().casefold() if isinstance(requested_name, str) and requested_name.strip() else None
    selected: ActiveBrowserProvider | None = None
    for item in inventory:
        if requested is not None and item.name.casefold() != requested:
            continue
        if not item.enabled or not item.configured:
            continue
        candidate = ActiveBrowserProvider(
            extension_id=item.extension_id,
            name=item.name,
            provider_kind=item.provider_kind,
            description=item.description,
            default_enabled=item.default_enabled,
            reference=item.reference,
            resolved_path=item.resolved_path,
            manifest_root_index=item.manifest_root_index,
            configured=item.configured,
            config_keys=item.config_keys,
            requires_network=item.requires_network,
            requires_daemon=item.requires_daemon,
            capabilities=item.capabilities,
        )
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


def select_active_browser_provider(
    contributions: list["ExtensionContributionRecord"],
    *,
    state_by_id: dict[str, Any] | None = None,
    enabled_overrides: dict[tuple[str, str], bool] | None = None,
    requested_name: str | None = None,
) -> ActiveBrowserProvider | None:
    inventory = list_browser_provider_inventory(
        contributions,
        state_by_id=state_by_id,
        enabled_overrides=enabled_overrides,
    )
    return _select_active_provider_from_inventory(inventory, requested_name=requested_name)
