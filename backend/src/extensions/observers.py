"""Typed observer definition helpers for extension packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.extensions.connectors import ConnectorDefinitionError, load_connector_payload

if TYPE_CHECKING:
    from src.extensions.registry import ExtensionContributionRecord


_SUPPORTED_OBSERVER_SOURCE_TYPES = {"time", "calendar", "git", "goals"}
_NETWORKED_OBSERVER_SOURCE_TYPES = {"calendar"}
_OBSERVER_SOURCE_ORDER = {"time": 0, "calendar": 1, "git": 2, "goals": 3}


@dataclass(frozen=True)
class ObserverDefinition:
    name: str
    source_type: str
    description: str = ""
    enabled: bool = True

    @property
    def requires_network(self) -> bool:
        return self.source_type in _NETWORKED_OBSERVER_SOURCE_TYPES

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_type": self.source_type,
            "description": self.description,
            "default_enabled": self.enabled,
            "requires_network": self.requires_network,
        }


@dataclass(frozen=True)
class ActiveObserverDefinition:
    extension_id: str
    name: str
    source_type: str
    description: str
    default_enabled: bool
    reference: str
    resolved_path: str | None
    manifest_root_index: int


def parse_observer_definition(payload: Any, *, source: str) -> ObserverDefinition:
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: observer definition must be an object")

    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ConnectorDefinitionError(f"{source}: observer definition must include a non-empty name")
    name = raw_name.strip()

    raw_source_type = payload.get("source_type")
    if not isinstance(raw_source_type, str) or not raw_source_type.strip():
        raise ConnectorDefinitionError(f"{source}: observer definition must include a non-empty source_type")
    source_type = raw_source_type.strip()
    if source_type not in _SUPPORTED_OBSERVER_SOURCE_TYPES:
        raise ConnectorDefinitionError(
            f"{source}: observer definition source_type '{source_type}' is not supported"
        )

    raw_description = payload.get("description")
    description = raw_description.strip() if isinstance(raw_description, str) else ""

    raw_enabled = payload.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ConnectorDefinitionError(f"{source}: observer definition enabled must be a boolean")
    enabled = True if raw_enabled is None else raw_enabled

    return ObserverDefinition(
        name=name,
        source_type=source_type,
        description=description,
        enabled=enabled,
    )


def load_observer_definition(path: Path) -> ObserverDefinition:
    payload = load_connector_payload(path)
    return parse_observer_definition(payload, source=str(path))


def select_active_observer_definitions(
    contributions: list["ExtensionContributionRecord"],
    *,
    enabled_overrides: dict[tuple[str, str], bool] | None = None,
) -> list[ActiveObserverDefinition]:
    selected_by_source_type: dict[str, ActiveObserverDefinition] = {}

    for contribution in contributions:
        if contribution.contribution_type != "observer_definitions":
            continue
        source_type = contribution.metadata.get("source_type")
        name = contribution.metadata.get("name")
        if not isinstance(source_type, str) or not source_type:
            continue
        if not isinstance(name, str) or not name:
            continue
        default_enabled = bool(contribution.metadata.get("default_enabled", True))
        enabled = enabled_overrides.get((contribution.extension_id, contribution.reference), default_enabled) if enabled_overrides else default_enabled
        if not enabled:
            continue
        candidate = ActiveObserverDefinition(
            extension_id=contribution.extension_id,
            name=name,
            source_type=source_type,
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
        existing = selected_by_source_type.get(source_type)
        if existing is None or candidate.manifest_root_index < existing.manifest_root_index:
            selected_by_source_type[source_type] = candidate
            continue
        if (
            candidate.manifest_root_index == existing.manifest_root_index
            and candidate.extension_id < existing.extension_id
        ):
            selected_by_source_type[source_type] = candidate

    return sorted(
        selected_by_source_type.values(),
        key=lambda item: (_OBSERVER_SOURCE_ORDER.get(item.source_type, 999), item.extension_id, item.name),
    )
