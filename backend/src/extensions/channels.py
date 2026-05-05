"""Typed channel-adapter definition helpers for extension packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.extensions.connectors import ConnectorDefinitionError, load_connector_payload

if TYPE_CHECKING:
    from src.extensions.registry import ExtensionContributionRecord


_SUPPORTED_CHANNEL_TRANSPORTS = {"websocket", "native_notification"}
_CHANNEL_TRANSPORT_ORDER = {"websocket": 0, "native_notification": 1}
_PRESENCE_SURFACE_KINDS = {
    "channel_adapters": "channel_adapter",
    "messaging_connectors": "messaging_connector",
    "node_adapters": "node_adapter",
}


@dataclass(frozen=True)
class ChannelAdapterDefinition:
    name: str
    transport: str
    description: str = ""
    enabled: bool = True

    @property
    def requires_daemon(self) -> bool:
        return self.transport == "native_notification"

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "description": self.description,
            "default_enabled": self.enabled,
            "requires_daemon": self.requires_daemon,
        }


@dataclass(frozen=True)
class ActiveChannelAdapter:
    extension_id: str
    name: str
    transport: str
    description: str
    default_enabled: bool
    reference: str
    resolved_path: str | None
    manifest_root_index: int


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def build_presence_boundary_contract(
    *,
    contribution_type: str,
    extension_id: str,
    reference: str,
    metadata: dict[str, Any],
    status: str,
    active: bool,
    ready: bool,
    pairing: dict[str, Any] | None = None,
    follow_up_ready: bool | None = None,
) -> dict[str, Any]:
    """Build the M4 operator-visible boundary contract for presence surfaces."""

    surface_kind = _PRESENCE_SURFACE_KINDS.get(contribution_type, contribution_type)
    name = str(metadata.get("name") or "").strip()
    trust = str(metadata.get("trust") or "unknown").strip() or "unknown"
    transport = str(metadata.get("transport") or "").strip()
    platform = str(metadata.get("platform") or "").strip()
    adapter_kind = str(metadata.get("adapter_kind") or "").strip()
    capabilities = _string_list(metadata.get("capabilities"))
    delivery_modes = _string_list(metadata.get("delivery_modes"))
    requires_network = bool(metadata.get("requires_network", False))
    requires_daemon = bool(metadata.get("requires_daemon", False))
    pairing_payload = pairing if isinstance(pairing, dict) else {}
    pairing_state = str(pairing_payload.get("pairing_state") or "not_applicable").strip()
    trust_state = str(pairing_payload.get("trust_state") or trust).strip() or trust
    revoked = pairing_state == "revoked" or trust_state in {"revoked", "untrusted"}
    effective_follow_up_ready = bool(ready if follow_up_ready is None else follow_up_ready) and not revoked
    can_notify = (
        contribution_type in {"channel_adapters", "messaging_connectors"}
        or "notify" in capabilities
        or "notification" in adapter_kind
    )
    scope_kind = "presence"
    if contribution_type == "channel_adapters":
        scope_kind = "operator_channel"
    elif contribution_type == "messaging_connectors":
        scope_kind = "external_messaging"
    elif contribution_type == "node_adapters":
        scope_kind = "paired_node"

    identity: dict[str, Any] = {
        "surface_kind": surface_kind,
        "extension_id": extension_id,
        "reference": reference,
        "name": name,
    }
    if transport:
        identity["transport"] = transport
    if platform:
        identity["platform"] = platform
    if adapter_kind:
        identity["adapter_kind"] = adapter_kind

    mutation_boundary_status = "closed"
    if effective_follow_up_ready:
        mutation_boundary_status = "approval_required"
    mutation_boundaries = {
        "inbound": {
            "allowed": effective_follow_up_ready,
            "live": False,
        },
        "outbound": {
            "allowed": can_notify and effective_follow_up_ready,
            "live": False,
        },
        "reply": {
            "allowed": contribution_type == "messaging_connectors" and effective_follow_up_ready,
            "live": False,
        },
        "workflow": {
            "allowed": effective_follow_up_ready,
            "live": False,
        },
        "approval": {
            "required": True,
            "status": mutation_boundary_status,
        },
        "device_action": {
            "allowed": contribution_type == "node_adapters" and effective_follow_up_ready,
            "live": False,
        },
    }

    return {
        "schema": "seraph.m4.presence_boundary.v1",
        "identity": identity,
        "trust": {
            "extension_trust": trust,
            "trust_state": trust_state,
            "pairing_state": pairing_state,
            "trusted": trust_state == "trusted" and not revoked,
            "revoked": revoked,
        },
        "scope": {
            "kind": scope_kind,
            "status": status,
            "active": active,
            "ready": bool(ready) and not revoked,
            "capabilities": capabilities,
            "delivery_modes": delivery_modes,
            "requires_network": requires_network,
            "requires_daemon": requires_daemon,
        },
        "notification": {
            "can_notify": can_notify and effective_follow_up_ready,
            "modes": delivery_modes or ([transport] if transport else []),
            "requires_operator_confirmation": True,
        },
        "mutation": {
            "boundary": "staged_contract_only",
            "boundaries": mutation_boundaries,
            "live_transport_claimed": False,
            "live_reach_claimed": False,
            "operator_approval_required": True,
            "follow_up_ready": effective_follow_up_ready,
            "unsafe_follow_up_hidden": not effective_follow_up_ready,
        },
    }


def parse_channel_adapter_definition(payload: Any, *, source: str) -> ChannelAdapterDefinition:
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: channel adapter definition must be an object")

    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ConnectorDefinitionError(f"{source}: channel adapter definition must include a non-empty name")
    name = raw_name.strip()

    raw_transport = payload.get("transport")
    if not isinstance(raw_transport, str) or not raw_transport.strip():
        raise ConnectorDefinitionError(f"{source}: channel adapter definition must include a non-empty transport")
    transport = raw_transport.strip()
    if transport not in _SUPPORTED_CHANNEL_TRANSPORTS:
        raise ConnectorDefinitionError(
            f"{source}: channel adapter transport '{transport}' is not supported"
        )

    raw_description = payload.get("description")
    description = raw_description.strip() if isinstance(raw_description, str) else ""

    raw_enabled = payload.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ConnectorDefinitionError(f"{source}: channel adapter enabled must be a boolean")
    enabled = True if raw_enabled is None else raw_enabled

    return ChannelAdapterDefinition(
        name=name,
        transport=transport,
        description=description,
        enabled=enabled,
    )


def load_channel_adapter_definition(path: Path) -> ChannelAdapterDefinition:
    payload = load_connector_payload(path)
    return parse_channel_adapter_definition(payload, source=str(path))


def select_active_channel_adapters(
    contributions: list["ExtensionContributionRecord"],
    *,
    enabled_overrides: dict[tuple[str, str], bool] | None = None,
) -> list[ActiveChannelAdapter]:
    selected_by_transport: dict[str, ActiveChannelAdapter] = {}

    for contribution in contributions:
        if contribution.contribution_type != "channel_adapters":
            continue
        transport = contribution.metadata.get("transport")
        name = contribution.metadata.get("name")
        if not isinstance(transport, str) or not transport:
            continue
        if not isinstance(name, str) or not name:
            continue
        default_enabled = bool(contribution.metadata.get("default_enabled", True))
        enabled = enabled_overrides.get((contribution.extension_id, contribution.reference), default_enabled) if enabled_overrides else default_enabled
        if not enabled:
            continue
        candidate = ActiveChannelAdapter(
            extension_id=contribution.extension_id,
            name=name,
            transport=transport,
            description=str(contribution.metadata.get("description") or ""),
            default_enabled=default_enabled,
            reference=contribution.reference,
            resolved_path=(
                str(contribution.metadata.get("resolved_path"))
                if isinstance(contribution.metadata.get("resolved_path"), str)
                else None
            ),
            manifest_root_index=int(contribution.metadata.get("manifest_root_index", 999999)),
        )
        existing = selected_by_transport.get(transport)
        if existing is None or candidate.manifest_root_index < existing.manifest_root_index:
            selected_by_transport[transport] = candidate
            continue
        if (
            candidate.manifest_root_index == existing.manifest_root_index
            and candidate.extension_id < existing.extension_id
        ):
            selected_by_transport[transport] = candidate

    return sorted(
        selected_by_transport.values(),
        key=lambda item: (_CHANNEL_TRANSPORT_ORDER.get(item.transport, 999), item.extension_id, item.name),
    )
