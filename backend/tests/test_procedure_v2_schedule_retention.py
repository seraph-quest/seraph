"""Focused proof for the bounded v2 schedule-seed retention extension."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import Goal, Session, WorkBoardInputArtifact
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner
from src.work_board.input_artifacts import (
    INPUT_ARTIFACT_TTL,
    SCHEDULE_INPUT_ARTIFACT_MAX_RETENTION,
    _payload_path,
    expire_input_artifacts,
    prepare_input_artifact,
)
from src.work_board.repository import BoardError


OWNER = WorkBoardOwner(principal_id="operator:schedule-retention", session_id="session:schedule-retention")
GOAL_ID = "goal:schedule-retention"
OBSERVED = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


async def _seed_goal(async_db) -> None:
    async with async_db() as db:
        db.add(Session(id=OWNER.session_id, owner_principal_id=OWNER.principal_id))
        db.add(
            Goal(
                id=GOAL_ID,
                title="Schedule retention goal",
                status="active",
                revision=1,
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
            )
        )


def _request(*, idempotency_key: str = "schedule:retention-key", invocation_uuid: str | None = None):
    return WorkBoardInputArtifactCreate(
        schema_version=1,
        capability_id="guardian-routine.v2",
        goal_id=GOAL_ID,
        goal_revision=1,
        input={
            "routine_id": "routine-0123456789abcdef0123456789abcdef",
            "version": 1,
            "expected_routine_revision": 1,
            "goal_id": GOAL_ID,
            "expected_goal_revision": 1,
            "parameters": {"goal_id": GOAL_ID, "expected_goal_revision": 1},
            "invocation_uuid": invocation_uuid or idempotency_key,
        },
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_schedule_seed_retention_survives_default_cleanup_window(async_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    await _seed_goal(async_db)
    deadline = OBSERVED + timedelta(days=3)

    async with async_db() as db:
        metadata = await prepare_input_artifact(
            db,
            OWNER,
            _request(),
            now=OBSERVED,
            retention_deadline=deadline,
        )
        row = await db.get(WorkBoardInputArtifact, metadata.artifact_id)
        assert row is not None
        path = _payload_path(row)
        assert metadata.expires_at == deadline
        assert path.exists()

    async with async_db() as db:
        assert await expire_input_artifacts(db, now=OBSERVED + timedelta(hours=25)) == 0
        row = await db.get(WorkBoardInputArtifact, metadata.artifact_id)
        assert row is not None and row.state == "pending"
        assert path.exists()

    async with async_db() as db:
        assert await expire_input_artifacts(db, now=deadline) == 1
        row = await db.get(WorkBoardInputArtifact, metadata.artifact_id)
        assert row is not None and row.state == "expired"
        assert not path.exists()


@pytest.mark.asyncio
async def test_schedule_seed_replay_does_not_extend_existing_expiry(async_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    await _seed_goal(async_db)
    first_deadline = OBSERVED + timedelta(days=3)

    async with async_db() as db:
        first = await prepare_input_artifact(
            db,
            OWNER,
            _request(),
            now=OBSERVED,
            retention_deadline=first_deadline,
        )

    async with async_db() as db:
        with pytest.raises(BoardError) as changed:
            await prepare_input_artifact(
                db,
                OWNER,
                _request(),
                now=OBSERVED + timedelta(days=1),
                retention_deadline=OBSERVED + timedelta(days=6),
            )
        assert changed.value.code == "input_artifact_idempotency_conflict"

    async with async_db() as db:
        replay = await prepare_input_artifact(
            db,
            OWNER,
            _request(),
            now=OBSERVED + timedelta(days=1),
            retention_deadline=first_deadline,
        )

    assert replay.artifact_id == first.artifact_id
    assert replay.expires_at == first_deadline


@pytest.mark.asyncio
async def test_retention_extension_is_server_bound_and_finite(async_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    await _seed_goal(async_db)

    with pytest.raises(BoardError, match="Extended input retention") as wrong_shape:
        async with async_db() as db:
            await prepare_input_artifact(
                db,
                OWNER,
                _request(idempotency_key="ordinary-key", invocation_uuid="ordinary-key"),
                now=OBSERVED,
                retention_deadline=OBSERVED + timedelta(days=2),
            )
    assert wrong_shape.value.code == "input_artifact_retention_invalid"

    with pytest.raises(BoardError, match="outside the bounded window") as too_long:
        async with async_db() as db:
            await prepare_input_artifact(
                db,
                OWNER,
                _request(),
                now=OBSERVED,
                retention_deadline=OBSERVED + SCHEDULE_INPUT_ARTIFACT_MAX_RETENTION + timedelta(seconds=1),
            )
    assert too_long.value.code == "input_artifact_retention_invalid"

    async with async_db() as db:
        ordinary = await prepare_input_artifact(
            db,
            OWNER,
            _request(idempotency_key="ordinary-key-2", invocation_uuid="ordinary-key-2"),
            now=OBSERVED,
        )
    assert ordinary.expires_at == OBSERVED + INPUT_ARTIFACT_TTL
