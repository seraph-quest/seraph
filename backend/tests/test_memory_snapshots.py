import pytest

from src.db.models import MemoryKind, MemorySnapshotKind
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
        content="For advisory nudges, avoid direct interruption during deep-work windows.",
        kind=MemoryKind.procedural,
        summary="Advisory timing lesson",
        importance=0.84,
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
    assert "Advisory timing lesson" in snapshot.content
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
