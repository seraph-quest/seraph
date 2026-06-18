"""Inventory helpers for extension-backed structured canvas outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.extensions.registry import ExtensionContributionRecord


@dataclass(frozen=True)
class CanvasOutputInventoryEntry:
    extension_id: str
    name: str
    title: str
    description: str
    surface_kind: str
    sections: tuple[str, ...]
    artifact_types: tuple[str, ...]
    preferred_panel: str
    reference: str


def list_canvas_output_inventory(
    contributions: list["ExtensionContributionRecord"],
) -> list[CanvasOutputInventoryEntry]:
    inventory: list[CanvasOutputInventoryEntry] = []
    for contribution in contributions:
        if contribution.contribution_type != "canvas_outputs":
            continue
        if isinstance(contribution.metadata.get("registry_conflict"), dict):
            continue
        name = contribution.metadata.get("name")
        title = contribution.metadata.get("title")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(title, str) or not title:
            continue
        sections = contribution.metadata.get("sections")
        artifact_types = contribution.metadata.get("artifact_types")
        inventory.append(
            CanvasOutputInventoryEntry(
                extension_id=contribution.extension_id,
                name=name,
                title=title,
                description=str(contribution.metadata.get("description") or ""),
                surface_kind=str(contribution.metadata.get("surface_kind") or "board"),
                sections=tuple(
                    item for item in sections if isinstance(item, str) and item.strip()
                ) if isinstance(sections, list) else (),
                artifact_types=tuple(
                    item for item in artifact_types if isinstance(item, str) and item.strip()
                ) if isinstance(artifact_types, list) else (),
                preferred_panel=str(contribution.metadata.get("preferred_panel") or ""),
                reference=contribution.reference,
            )
        )
    return sorted(inventory, key=lambda item: (item.extension_id, item.surface_kind, item.name))
