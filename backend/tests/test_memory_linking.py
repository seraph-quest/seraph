import pytest

from src.db.models import MemoryKind
from src.memory.linking import resolve_memory_links
from src.memory.types import ConsolidatedMemoryItem, kind_to_category


@pytest.mark.asyncio
async def test_resolve_memory_links_does_not_merge_projects_on_shared_summary(async_db):
    first = await resolve_memory_links(
        ConsolidatedMemoryItem(
            text="Send the weekly update for Mercury.",
            kind=MemoryKind.commitment,
            category=kind_to_category(MemoryKind.commitment),
            summary="Send weekly update",
            project_name="Project Mercury",
        )
    )
    second = await resolve_memory_links(
        ConsolidatedMemoryItem(
            text="Send the weekly update for Apollo.",
            kind=MemoryKind.commitment,
            category=kind_to_category(MemoryKind.commitment),
            summary="Send weekly update",
            project_name="Project Apollo",
        )
    )

    assert first.project_entity_id is not None
    assert second.project_entity_id is not None
    assert first.project_entity_id != second.project_entity_id
