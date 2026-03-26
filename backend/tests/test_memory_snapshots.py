import pytest

from src.agent.session import SessionManager
from src.db.models import MemoryKind, MemorySnapshotKind
from src.guardian.feedback import guardian_feedback_repository
from src.memory.repository import memory_repository
from src.memory.snapshots import (
    get_or_create_bounded_guardian_snapshot,
    refresh_bounded_guardian_snapshot,
)


@pytest.mark.asyncio
async def test_refresh_bounded_guardian_snapshot_persists_structured_summary(async_db):
    await memory_repository.create_memory(
        content="Ship Batch A memory upgrades.",
        kind=MemoryKind.goal,
        summary="Ship Batch A memory upgrades",
        importance=0.95,
    )
    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.9,
    )
    await memory_repository.create_memory(
        content="User prefers concise morning briefings.",
        kind=MemoryKind.communication_preference,
        summary="Prefers concise morning briefings",
        importance=0.85,
    )
    await memory_repository.create_memory(
        content="For advisory interventions, avoid direct interruption during deep-work windows.",
        kind=MemoryKind.procedural,
        summary="For advisory interventions, avoid direct interruption during deep-work windows.",
        importance=0.84,
    )
    await memory_repository.create_memory(
        content="For advisory interventions, prefer async native continuation when the user is blocked.",
        kind=MemoryKind.procedural,
        summary="For advisory interventions, prefer async native continuation when the user is blocked.",
        importance=0.83,
    )
    await memory_repository.create_memory(
        content="For advisory interventions, bundle lower-urgency check-ins instead of interrupting immediately.",
        kind=MemoryKind.procedural,
        summary="For advisory interventions, bundle lower-urgency check-ins instead of interrupting immediately.",
        importance=0.82,
    )

    snapshot = await refresh_bounded_guardian_snapshot(
        soul_context=(
            "# Soul\n\n## Identity\nBuilder\n\n## Goals\n- Keep the system grounded\n\n"
            "## Personality Notes\n- Prefer direct wording"
        ),
    )
    stored = await memory_repository.get_snapshot(MemorySnapshotKind.bounded_guardian_context)

    assert snapshot.content == stored.content
    assert "Identity: Builder" in snapshot.content
    assert "Ship Batch A memory upgrades" in snapshot.content
    assert "Atlas launch" in snapshot.content
    assert "Prefers concise morning briefings" in snapshot.content
    assert "avoid direct interruption during deep-work windows" in snapshot.content
    assert "prefer async native continuation when the user is blocked" in snapshot.content
    assert "bundle lower-urgency check-ins instead of interrupting immediately" in snapshot.content
    assert snapshot.source_hash == stored.source_hash


@pytest.mark.asyncio
async def test_get_or_create_bounded_guardian_snapshot_reuses_stored_snapshot(async_db):
    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.9,
    )
    await memory_repository.save_snapshot(
        kind=MemorySnapshotKind.bounded_guardian_context,
        content="- Identity: Stale snapshot",
        source_hash="cached-hash",
    )

    content = await get_or_create_bounded_guardian_snapshot(
        soul_context="# Soul\n\n## Identity\nBuilder\n\n## Goals\n- Keep the system grounded",
    )

    assert "Identity: Builder" in content
    assert "Atlas launch" in content
    assert "Stale snapshot" not in content


@pytest.mark.asyncio
async def test_get_or_create_bounded_guardian_snapshot_freezes_content_per_session(async_db):
    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.9,
    )

    initial = await get_or_create_bounded_guardian_snapshot(
        soul_context="# Soul\n\n## Identity\nBuilder",
        session_id="session-a",
    )
    await memory_repository.create_memory(
        content="Hermes budget memo is now active.",
        kind=MemoryKind.project,
        summary="Hermes budget memo",
        importance=0.95,
    )

    frozen = await get_or_create_bounded_guardian_snapshot(
        soul_context="# Soul\n\n## Identity\nBuilder",
        session_id="session-a",
    )
    fresh = await get_or_create_bounded_guardian_snapshot(
        soul_context="# Soul\n\n## Identity\nBuilder",
        session_id="session-b",
    )

    assert frozen == initial
    assert "Hermes budget memo" not in frozen
    assert "Hermes budget memo" in fresh


@pytest.mark.asyncio
async def test_feedback_refresh_invalidates_session_snapshot_cache(async_db):
    sm = SessionManager()
    await sm.get_or_create("session-feedback-cache")

    initial = await get_or_create_bounded_guardian_snapshot(
        soul_context="# Soul\n\n## Identity\nBuilder",
        session_id="session-feedback-cache",
    )

    for user_state in ("deep_work", "in_meeting"):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id="session-feedback-cache",
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Respect the focus block.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state=user_state,
            interruption_mode="focus",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="websocket",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type="not_helpful")

    refreshed = await get_or_create_bounded_guardian_snapshot(
        soul_context="# Soul\n\n## Identity\nBuilder",
        session_id="session-feedback-cache",
    )

    assert refreshed != initial
    assert "avoid direct interruption during deep-work, meeting, or away windows" in refreshed
