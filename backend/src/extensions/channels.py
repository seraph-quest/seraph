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
        if not default_enabled:
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
