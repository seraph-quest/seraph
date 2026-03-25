from __future__ import annotations

from dataclasses import dataclass

from src.db.models import MemoryEntityType, MemoryKind
from src.memory.repository import memory_repository
from src.memory.types import ConsolidatedMemoryItem


@dataclass(frozen=True)
class MemoryLinkResolution:
    subject_entity_id: str | None = None
    project_entity_id: str | None = None


def _canonical_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


async def resolve_memory_links(item: ConsolidatedMemoryItem) -> MemoryLinkResolution:
    subject_name = _canonical_name(item.subject_name)
    project_name = _canonical_name(item.project_name)

    if item.kind == MemoryKind.project and project_name is None:
        project_name = _canonical_name(item.summary) or _canonical_name(item.text)
    if item.kind == MemoryKind.routine and subject_name is None:
        subject_name = _canonical_name(item.summary) or _canonical_name(item.text)
    if item.kind == MemoryKind.obligation and subject_name is None:
        subject_name = _canonical_name(item.summary) or _canonical_name(item.text)

    subject_entity_id: str | None = None
    project_entity_id: str | None = None

    if subject_name is not None:
        subject_entity_type = {
            MemoryKind.collaborator: MemoryEntityType.person,
            MemoryKind.routine: MemoryEntityType.routine,
            MemoryKind.obligation: MemoryEntityType.obligation,
        }.get(item.kind)
        if subject_entity_type is not None:
            subject_entity = await memory_repository.get_or_create_entity(
                canonical_name=subject_name,
                entity_type=subject_entity_type,
            )
            subject_entity_id = subject_entity.id

    if project_name is not None:
        aliases: list[str] = []
        summary_name = _canonical_name(item.summary)
        if summary_name is not None and summary_name != project_name:
            aliases.append(summary_name)
        project_entity = await memory_repository.get_or_create_entity(
            canonical_name=project_name,
            entity_type=MemoryEntityType.project,
            aliases=aliases,
        )
        project_entity_id = project_entity.id

    return MemoryLinkResolution(
        subject_entity_id=subject_entity_id,
        project_entity_id=project_entity_id,
    )
