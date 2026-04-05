"""Provider-neutral source capability inventory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from config.settings import settings
from src.extensions.browser_providers import list_browser_provider_inventory
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import connector_enabled_overrides, load_extension_state_payload
from src.tools.mcp_manager import mcp_manager

if TYPE_CHECKING:
    from src.extensions.registry import ExtensionContributionRecord


@dataclass(frozen=True)
class SourceContractDefinition:
    name: str
    description: str
    preferred_access: str


@dataclass(frozen=True)
class TypedSourceSurface:
    name: str
    source_kind: str
    provider: str
    description: str
    authenticated: bool
    auth_kind: str
    access_mode: str
    enabled: bool
    configured: bool
    runtime_state: str
    contracts: tuple[str, ...]
    config_keys: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    extension_id: str | None = None
    reference: str | None = None
    notes: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_kind": self.source_kind,
            "provider": self.provider,
            "description": self.description,
            "authenticated": self.authenticated,
            "auth_kind": self.auth_kind,
            "access_mode": self.access_mode,
            "enabled": self.enabled,
            "configured": self.configured,
            "runtime_state": self.runtime_state,
            "contracts": list(self.contracts),
            "config_keys": list(self.config_keys),
            "capabilities": list(self.capabilities),
            "extension_id": self.extension_id,
            "reference": self.reference,
            "notes": list(self.notes),
        }


SOURCE_CONTRACTS: tuple[SourceContractDefinition, ...] = (
    SourceContractDefinition(
        name="source_discovery.read",
        description="Discover public sources or candidate entry points before deeper inspection.",
        preferred_access="web_search",
    ),
    SourceContractDefinition(
        name="webpage.read",
        description="Read or extract public webpage content from an explicit URL.",
        preferred_access="browse_webpage",
    ),
    SourceContractDefinition(
        name="browser_session.manage",
        description="Open, snapshot, and revisit public pages inside a structured browser session.",
        preferred_access="browser_session",
    ),
    SourceContractDefinition(
        name="repository.read",
        description="Read repository-level metadata, structure, or configuration from a typed source.",
        preferred_access="managed_connector",
    ),
    SourceContractDefinition(
        name="code_activity.read",
        description="Read code activity such as pull-request or repository history from a typed source.",
        preferred_access="managed_connector",
    ),
    SourceContractDefinition(
        name="work_items.read",
        description="Read tracked work items such as issues or pull requests from a typed source.",
        preferred_access="managed_connector",
    ),
    SourceContractDefinition(
        name="work_items.write",
        description="Create or update tracked work items through a typed authenticated source.",
        preferred_access="managed_connector",
    ),
)

_MANAGED_CAPABILITY_CONTRACTS: dict[str, tuple[str, ...]] = {
    "repositories.read": ("repository.read", "code_activity.read"),
    "repositories.write": ("repository.read",),
    "pull_requests.read": ("work_items.read", "code_activity.read"),
    "pull_requests.write": ("work_items.write",),
    "issues.read": ("work_items.read",),
    "issues.write": ("work_items.write",),
}


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


def _managed_connector_config(
    state_by_id: dict[str, Any] | None,
    extension_id: str,
    connector_name: str,
) -> dict[str, Any]:
    if not isinstance(state_by_id, dict):
        return {}
    raw_state = state_by_id.get(extension_id)
    if not isinstance(raw_state, dict):
        return {}
    raw_config = raw_state.get("config")
    if not isinstance(raw_config, dict):
        return {}
    bucket = raw_config.get("managed_connectors")
    if not isinstance(bucket, dict):
        return {}
    entry = bucket.get(connector_name)
    return entry if isinstance(entry, dict) else {}


def _normalize_managed_connector_contracts(capabilities: list[str]) -> tuple[str, ...]:
    contracts: list[str] = []
    seen: set[str] = set()
    for capability in capabilities:
        for contract in _MANAGED_CAPABILITY_CONTRACTS.get(capability, ()):
            if contract in seen:
                continue
            contracts.append(contract)
            seen.add(contract)
    return tuple(contracts)


def _native_source_surfaces(browser_provider_names: list[str], selected_browser_provider: str) -> list[TypedSourceSurface]:
    provider_note = (
        f"Structured browser sessions currently route through {selected_browser_provider}."
        if selected_browser_provider
        else "Structured browser sessions currently route through the built-in local browser runtime."
    )
    additional_provider_note = (
        f"Known browser providers: {', '.join(browser_provider_names)}."
        if browser_provider_names
        else "No packaged browser providers are configured yet."
    )
    return [
        TypedSourceSurface(
            name="web_search",
            source_kind="native_tool",
            provider="seraph",
            description="Public-source discovery across the web.",
            authenticated=False,
            auth_kind="none",
            access_mode="discovery",
            enabled=True,
            configured=True,
            runtime_state="ready",
            contracts=("source_discovery.read",),
            notes=("Use this to find candidate public sources before opening a page.",),
        ),
        TypedSourceSurface(
            name="browse_webpage",
            source_kind="native_tool",
            provider="seraph",
            description="Direct public webpage inspection for explicit URLs.",
            authenticated=False,
            auth_kind="none",
            access_mode="public_page_read",
            enabled=True,
            configured=True,
            runtime_state="ready",
            contracts=("webpage.read",),
            notes=("Use this for public or explicitly user-linked pages, not authenticated website login.",),
        ),
        TypedSourceSurface(
            name="browser_session",
            source_kind="native_tool",
            provider="seraph",
            description="Structured public browser sessions with refs and snapshots.",
            authenticated=False,
            auth_kind="none",
            access_mode="session_public_page_read",
            enabled=True,
            configured=True,
            runtime_state="ready",
            contracts=("browser_session.manage", "webpage.read"),
            notes=(provider_note, additional_provider_note),
        ),
    ]


def _managed_source_surfaces(
    contributions: list["ExtensionContributionRecord"],
    *,
    state_by_id: dict[str, Any] | None,
    enabled_overrides: dict[tuple[str, str], bool] | None,
) -> list[TypedSourceSurface]:
    surfaces: list[TypedSourceSurface] = []
    for contribution in contributions:
        if contribution.contribution_type != "managed_connectors":
            continue
        if isinstance(contribution.metadata.get("registry_conflict"), dict):
            continue
        name = contribution.metadata.get("name")
        provider = contribution.metadata.get("provider")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(provider, str) or not provider:
            continue
        default_enabled = bool(contribution.metadata.get("default_enabled", False))
        enabled = (
            enabled_overrides.get((contribution.extension_id, contribution.reference), default_enabled)
            if enabled_overrides
            else default_enabled
        )
        config_entry = _managed_connector_config(state_by_id, contribution.extension_id, name)
        config_fields = contribution.metadata.get("config_fields")
        config_field_list = config_fields if isinstance(config_fields, list) else []
        configured = not _required_config_missing(config_field_list, config_entry)
        auth_kind = str(contribution.metadata.get("auth_kind") or "api_key")
        capabilities = [
            item
            for item in contribution.metadata.get("capabilities", [])
            if isinstance(item, str) and item.strip()
        ] if isinstance(contribution.metadata.get("capabilities"), list) else []
        contracts = _normalize_managed_connector_contracts(capabilities)
        if auth_kind != "none" and not configured:
            runtime_state = "requires_config"
        elif not enabled:
            runtime_state = "disabled"
        else:
            runtime_state = "ready"
        notes: list[str] = []
        if auth_kind != "none":
            notes.append("Prefer this typed connector over browser login for authenticated source access.")
        if not contracts:
            notes.append("This connector does not yet advertise normalized source contracts.")
        surfaces.append(
            TypedSourceSurface(
                name=name,
                source_kind="managed_connector",
                provider=provider,
                description=str(contribution.metadata.get("description") or ""),
                authenticated=auth_kind != "none",
                auth_kind=auth_kind,
                access_mode="typed_connector",
                enabled=enabled,
                configured=configured,
                runtime_state=runtime_state,
                contracts=contracts,
                config_keys=tuple(sorted(config_entry.keys())),
                capabilities=tuple(capabilities),
                extension_id=contribution.extension_id,
                reference=contribution.reference,
                notes=tuple(notes),
            )
        )
    return sorted(surfaces, key=lambda item: (item.provider, item.name))


def _untyped_sources() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in mcp_manager.get_config():
        entries.append(
            {
                "name": item.get("name", ""),
                "source_kind": "mcp_server",
                "provider": item.get("extension_display_name") or item.get("extension_id") or item.get("source") or "manual",
                "url": item.get("url", ""),
                "status": item.get("status", "disconnected"),
                "enabled": bool(item.get("enabled", True)),
                "connected": bool(item.get("connected", False)),
                "tool_count": int(item.get("tool_count", 0) or 0),
                "auth_hint": item.get("auth_hint", ""),
                "has_headers": bool(item.get("has_headers", False)),
                "source": item.get("source", "manual"),
                "notes": [
                    "Raw MCP access is available, but no provider-neutral source contract is attached yet.",
                    "Use a typed adapter or managed connector for authenticated source workflows when possible.",
                ],
            }
        )
    return sorted(entries, key=lambda item: (item["provider"], item["name"]))


def list_source_capability_inventory() -> dict[str, Any]:
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    enabled_overrides = connector_enabled_overrides(state_by_id if isinstance(state_by_id, dict) else None)
    browser_inventory = list_browser_provider_inventory(
        snapshot.list_contributions("browser_providers"),
        state_by_id=state_by_id if isinstance(state_by_id, dict) else None,
        enabled_overrides=enabled_overrides,
    )
    selected_browser_provider = next((item.name for item in browser_inventory if item.selected), "local-browser")
    browser_provider_names = [item.name for item in browser_inventory]
    typed_sources = _native_source_surfaces(browser_provider_names, selected_browser_provider)
    typed_sources.extend(
        _managed_source_surfaces(
            snapshot.list_contributions("managed_connectors"),
            state_by_id=state_by_id if isinstance(state_by_id, dict) else None,
            enabled_overrides=enabled_overrides,
        )
    )
    contract_usage: dict[str, int] = {item.name: 0 for item in SOURCE_CONTRACTS}
    for surface in typed_sources:
        for contract in surface.contracts:
            if contract in contract_usage:
                contract_usage[contract] += 1
    contracts = [
        {
            "name": contract.name,
            "description": contract.description,
            "preferred_access": contract.preferred_access,
            "available_from": contract_usage.get(contract.name, 0),
        }
        for contract in SOURCE_CONTRACTS
    ]
    return {
        "summary": {
            "typed_source_count": len(typed_sources),
            "authenticated_typed_source_count": sum(1 for item in typed_sources if item.authenticated),
            "untyped_source_count": len(mcp_manager.get_config()),
        },
        "contracts": contracts,
        "typed_sources": [item.as_payload() for item in typed_sources],
        "untyped_sources": _untyped_sources(),
        "composition_rules": [
            "Prefer typed authenticated connectors over browser login for authenticated systems.",
            "Use web_search for discovery, browse_webpage for explicit public pages, and browser_session for multi-step public inspection.",
            "If only raw MCP access is present, state that the source is untyped instead of claiming provider-neutral read or write contracts that do not exist.",
        ],
    }
