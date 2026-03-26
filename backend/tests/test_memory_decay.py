from datetime import datetime, timedelta, timezone

import pytest

from src.db.models import MemoryEdgeType, MemoryKind
from src.memory.decay import apply_memory_decay_policies
from src.memory.repository import memory_repository
from src.memory.snapshots import render_bounded_guardian_snapshot


@pytest.mark.asyncio
async def test_apply_memory_decay_archives_stale_memories(async_db):
    reference_now = datetime.now(timezone.utc)
    old_confirmed_at = reference_now - timedelta(days=800)

    created = await memory_repository.create_memory(
        content="User prefers redundant weekly recap messages.",
        kind=MemoryKind.communication_preference,
        summary="User prefers redundant weekly recap messages.",
        confidence=0.4,
        importance=0.35,
        reinforcement=0.3,
        last_confirmed_at=old_confirmed_at,
    )

    result = await apply_memory_decay_policies(now=reference_now)
    archived_memories = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
        status="archived",
    )

    assert result.archived_count == 1
    assert [memory.id for memory in archived_memories] == [created.memory_id]


@pytest.mark.asyncio
async def test_apply_memory_decay_supersedes_contradictory_project_memory(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas launch",
        entity_type="project",
    )
    older = await memory_repository.create_memory(
        content="Atlas launch is delayed.",
        kind=MemoryKind.project,
        summary="Atlas launch delayed",
        importance=0.8,
        confidence=0.7,
        project_entity_id=atlas.id,
        last_confirmed_at=datetime.now(timezone.utc) - timedelta(days=7),
    )
    newer = await memory_repository.create_memory(
        content="Atlas launch is on track.",
        kind=MemoryKind.project,
        summary="Atlas launch on track",
        importance=0.9,
        confidence=0.9,
        project_entity_id=atlas.id,
        last_confirmed_at=datetime.now(timezone.utc),
    )

    result = await apply_memory_decay_policies()
    active_projects = await memory_repository.list_memories(kind=MemoryKind.project, limit=10)
    superseded_projects = await memory_repository.list_memories(
        kind=MemoryKind.project,
        limit=10,
        status="superseded",
    )
    edges = await memory_repository.list_edges(from_memory_id=newer.memory_id)
    snapshot, _ = await render_bounded_guardian_snapshot(
        soul_context="# Soul\n\n## Identity\nBuilder",
    )

    assert result.contradiction_count == 1
    assert result.superseded_count == 1
    assert [memory.id for memory in active_projects] == [newer.memory_id]
    assert [memory.id for memory in superseded_projects] == [older.memory_id]
    assert any(edge.edge_type == MemoryEdgeType.contradicts for edge in edges)
    assert any(edge.edge_type == MemoryEdgeType.supersedes for edge in edges)
    assert "Atlas launch on track" in snapshot
    assert "Atlas launch delayed" not in snapshot


@pytest.mark.asyncio
async def test_apply_memory_decay_detects_not_helpful_contradictions_from_content(async_db):
    older = await memory_repository.create_memory(
        content="User finds morning briefings helpful.",
        kind=MemoryKind.communication_preference,
        summary="Morning briefings",
        importance=0.8,
        confidence=0.7,
        last_confirmed_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    newer = await memory_repository.create_memory(
        content="User finds morning briefings not helpful.",
        kind=MemoryKind.communication_preference,
        summary="Morning briefings",
        importance=0.9,
        confidence=0.9,
        last_confirmed_at=datetime.now(timezone.utc),
    )

    result = await apply_memory_decay_policies()
    active_memories = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
    )
    superseded_memories = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
        status="superseded",
    )
    edges = await memory_repository.list_edges(from_memory_id=newer.memory_id)

    assert result.contradiction_count == 1
    assert result.superseded_count == 1
    assert [memory.id for memory in active_memories] == [newer.memory_id]
    assert [memory.id for memory in superseded_memories] == [older.memory_id]
    assert any(edge.edge_type == MemoryEdgeType.contradicts for edge in edges)
