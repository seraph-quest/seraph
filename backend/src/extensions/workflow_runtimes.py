"""Inventory helpers for typed workflow runtime profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.extensions.registry import ExtensionContributionRecord


@dataclass(frozen=True)
class WorkflowRuntimeInventoryEntry:
    extension_id: str
    name: str
    engine_kind: str
    description: str
    delegation_mode: str
    checkpoint_policy: str
    structured_output: bool
    default_output_surface: str
    reference: str


def list_workflow_runtime_inventory(
    contributions: list["ExtensionContributionRecord"],
) -> list[WorkflowRuntimeInventoryEntry]:
    inventory: list[WorkflowRuntimeInventoryEntry] = []
    for contribution in contributions:
        if contribution.contribution_type != "workflow_runtimes":
            continue
        if isinstance(contribution.metadata.get("registry_conflict"), dict):
            continue
        name = contribution.metadata.get("name")
        engine_kind = contribution.metadata.get("engine_kind")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(engine_kind, str) or not engine_kind:
            continue
        inventory.append(
            WorkflowRuntimeInventoryEntry(
                extension_id=contribution.extension_id,
                name=name,
                engine_kind=engine_kind,
                description=str(contribution.metadata.get("description") or ""),
                delegation_mode=str(contribution.metadata.get("delegation_mode") or ""),
                checkpoint_policy=str(contribution.metadata.get("checkpoint_policy") or ""),
                structured_output=bool(contribution.metadata.get("structured_output", False)),
                default_output_surface=str(contribution.metadata.get("default_output_surface") or ""),
                reference=contribution.reference,
            )
        )
    return sorted(inventory, key=lambda item: (item.extension_id, item.engine_kind, item.name))
