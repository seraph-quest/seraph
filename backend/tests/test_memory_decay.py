import json
from datetime import datetime, timedelta, timezone

import pytest

from src.db.models import MemoryEdgeType, MemoryKind
from src.memory.decay import apply_memory_decay_policies, summarize_memory_reconciliation_state
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
async def test_apply_memory_decay_repeats_terminal_decay_until_archive(async_db):
    reference_now = datetime.now(timezone.utc)
    old_confirmed_at = reference_now - timedelta(days=800)

    created = await memory_repository.create_memory(
        content="User prefers redundant weekly recap messages.",
        kind=MemoryKind.communication_preference,
        summary="User prefers redundant weekly recap messages.",
        confidence=0.95,
        importance=0.35,
        reinforcement=2.0,
        last_confirmed_at=old_confirmed_at,
    )

    final_result = None
    for _ in range(5):
        final_result = await apply_memory_decay_policies(now=reference_now)

    archived_memories = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
        status="archived",
    )

    assert final_result is not None
    assert final_result.decayed_count == 1
    assert final_result.archived_count == 1
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
async def test_apply_memory_decay_detects_same_entity_short_contradictions(async_db):
    slack = await memory_repository.get_or_create_entity(
        canonical_name="Slack",
        entity_type="organization",
    )
    older = await memory_repository.create_memory(
        content="Prefers Slack.",
        kind=MemoryKind.communication_preference,
        summary="Prefers Slack",
        importance=0.7,
        confidence=0.6,
        subject_entity_id=slack.id,
        last_confirmed_at=datetime.now(timezone.utc) - timedelta(days=7),
    )
    newer = await memory_repository.create_memory(
        content="Avoid Slack.",
        kind=MemoryKind.communication_preference,
        summary="Avoid Slack",
        importance=0.9,
        confidence=0.9,
        subject_entity_id=slack.id,
        last_confirmed_at=datetime.now(timezone.utc),
    )

    result = await apply_memory_decay_policies()
    active_preferences = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
    )
    superseded_preferences = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
        status="superseded",
    )

    assert result.contradiction_count == 1
    assert result.superseded_count == 1
    assert [memory.id for memory in active_preferences] == [newer.memory_id]
    assert [memory.id for memory in superseded_preferences] == [older.memory_id]


@pytest.mark.asyncio
async def test_apply_memory_decay_keeps_scoped_same_entity_preferences(async_db):
    slack = await memory_repository.get_or_create_entity(
        canonical_name="Slack",
        entity_type="organization",
    )
    first = await memory_repository.create_memory(
        content="Prefers Slack for team chat.",
        kind=MemoryKind.communication_preference,
        summary="Prefers Slack for team chat",
        importance=0.7,
        confidence=0.7,
        subject_entity_id=slack.id,
        last_confirmed_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    second = await memory_repository.create_memory(
        content="Avoid Slack notifications during meetings.",
        kind=MemoryKind.communication_preference,
        summary="Avoid Slack notifications during meetings",
        importance=0.8,
        confidence=0.8,
        subject_entity_id=slack.id,
        last_confirmed_at=datetime.now(timezone.utc),
    )

    result = await apply_memory_decay_policies()
    active_preferences = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
    )
    superseded_preferences = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
        status="superseded",
    )

    assert result.contradiction_count == 0
    assert result.superseded_count == 0
    assert {memory.id for memory in active_preferences} == {first.memory_id, second.memory_id}
    assert superseded_preferences == []


@pytest.mark.asyncio
async def test_apply_memory_decay_keeps_non_contradictory_same_entity_project_updates(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas launch",
        entity_type="project",
    )
    first = await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas active release project",
        importance=0.7,
        confidence=0.7,
        project_entity_id=atlas.id,
        last_confirmed_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    second = await memory_repository.create_memory(
        content="Atlas launch is delayed by the vendor handoff.",
        kind=MemoryKind.project,
        summary="Atlas delayed by vendor handoff",
        importance=0.8,
        confidence=0.8,
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

    assert result.contradiction_count == 0
    assert result.superseded_count == 0
    assert {memory.id for memory in active_projects} == {first.memory_id, second.memory_id}
    assert superseded_projects == []


@pytest.mark.asyncio
async def test_apply_memory_decay_detects_concise_active_paused_state_reversal(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas service",
        entity_type="project",
    )
    older = await memory_repository.create_memory(
        content="Atlas service is active.",
        kind=MemoryKind.project,
        summary="Atlas service active",
        importance=0.7,
        confidence=0.7,
        project_entity_id=atlas.id,
        last_confirmed_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    newer = await memory_repository.create_memory(
        content="Atlas service is paused.",
        kind=MemoryKind.project,
        summary="Atlas service paused",
        importance=0.8,
        confidence=0.8,
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

    assert result.contradiction_count == 1
    assert result.superseded_count == 1
    assert [memory.id for memory in active_projects] == [newer.memory_id]
    assert [memory.id for memory in superseded_projects] == [older.memory_id]


@pytest.mark.asyncio
async def test_memory_reconciliation_summary_reports_conflicts_and_archivals(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas launch",
        entity_type="project",
    )
    superseded = await memory_repository.create_memory(
        content="Atlas launch is delayed.",
        kind=MemoryKind.project,
        summary="Atlas launch delayed",
        importance=0.7,
        confidence=0.6,
        project_entity_id=atlas.id,
        status="superseded",
        metadata={
            "superseded_reason": "contradiction",
            "superseded_by_memory_id": "atlas-current",
        },
    )
    current = await memory_repository.create_memory(
        content="Atlas launch is on track.",
        kind=MemoryKind.project,
        summary="Atlas launch on track",
        importance=0.9,
        confidence=0.9,
        project_entity_id=atlas.id,
    )
    await memory_repository.create_memory(
        content="User prefers redundant weekly recap messages.",
        kind=MemoryKind.communication_preference,
        summary="Weekly recap preference",
        importance=0.2,
        confidence=0.2,
        reinforcement=0.1,
        status="archived",
        metadata={"archived_reason": "stale_decay_archive"},
    )
    await memory_repository.create_edge(
        from_memory_id=current.memory_id,
        to_memory_id=superseded.memory_id,
        edge_type=MemoryEdgeType.contradicts,
    )

    summary = await summarize_memory_reconciliation_state(limit=3)

    assert summary["state"] == "conflict_and_forgetting_active"
    assert summary["superseded_count"] == 1
    assert summary["archived_count"] == 1
    assert summary["contradiction_edge_count"] == 1
    assert summary["policy"]["authoritative_memory"] == "guardian"
    assert summary["policy"]["reconciliation_policy"] == "canonical_first"
    assert summary["recent_conflicts"][0]["summary"] == "Atlas launch delayed"
    assert summary["recent_conflicts"][0]["superseded_by_memory_id"] == "atlas-current"
    assert summary["recent_archivals"][0]["summary"] == "Weekly recap preference"


@pytest.mark.asyncio
async def test_memory_reconciliation_summary_counts_are_not_capped_by_preview_limit(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas launch",
        entity_type="project",
    )
    for index in range(3):
        superseded = await memory_repository.create_memory(
            content=f"Atlas launch status variant {index}",
            kind=MemoryKind.project,
            summary=f"Atlas variant {index}",
            importance=0.99 if index == 0 else 0.2,
            project_entity_id=atlas.id,
            status="superseded",
            metadata={
                "superseded_reason": "contradiction",
                "superseded_by_memory_id": f"atlas-current-{index}",
            },
        )
        current = await memory_repository.create_memory(
            content=f"Atlas launch current status {index}",
            kind=MemoryKind.project,
            summary=f"Atlas current {index}",
            project_entity_id=atlas.id,
        )
        await memory_repository.create_edge(
            from_memory_id=current.memory_id,
            to_memory_id=superseded.memory_id,
            edge_type=MemoryEdgeType.contradicts,
        )
        await memory_repository.create_memory(
            content=f"Archived preference {index}",
            kind=MemoryKind.communication_preference,
            summary=f"Archived preference {index}",
            importance=0.99 if index == 0 else 0.2,
            status="archived",
            metadata={"archived_reason": "stale_decay_archive"},
        )

    summary = await summarize_memory_reconciliation_state(limit=1)

    assert summary["superseded_count"] == 3
    assert summary["archived_count"] == 3
    assert summary["contradiction_edge_count"] == 3
    assert len(summary["recent_conflicts"]) == 1
    assert len(summary["recent_archivals"]) == 1
    assert summary["recent_conflicts"][0]["summary"] == "Atlas variant 2"
    assert summary["recent_archivals"][0]["summary"] == "Archived preference 2"


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


@pytest.mark.asyncio
async def test_merge_refresh_resets_decay_progress_for_future_maintenance(async_db):
    first_now = datetime.now(timezone.utc)
    created = await memory_repository.create_memory(
        content="User prefers concise morning briefings.",
        kind=MemoryKind.communication_preference,
        summary="Prefers concise morning briefings",
        confidence=0.85,
        importance=0.6,
        reinforcement=1.0,
        last_confirmed_at=first_now - timedelta(days=280),
    )

    first_decay = await apply_memory_decay_policies(now=first_now)
    decayed_before_refresh = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
    )
    before_metadata = json.loads(decayed_before_refresh[0].metadata_json or "{}")

    refresh_now = first_now + timedelta(days=1)
    await memory_repository.merge_memory(
        created.memory_id,
        last_confirmed_at=refresh_now,
    )

    refreshed_memories = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
    )
    refreshed_metadata = json.loads(refreshed_memories[0].metadata_json or "{}")

    later_now = refresh_now + timedelta(days=130)
    second_decay = await apply_memory_decay_policies(now=later_now)
    decayed_after_refresh = await memory_repository.list_memories(
        kind=MemoryKind.communication_preference,
        limit=10,
    )
    after_metadata = json.loads(decayed_after_refresh[0].metadata_json or "{}")

    assert first_decay.decayed_count == 1
    assert before_metadata["decay_step"] == 2
    assert "decay_step" not in refreshed_metadata
    assert "decay_age_days" not in refreshed_metadata
    assert second_decay.decayed_count == 1
    assert after_metadata["decay_step"] == 1
