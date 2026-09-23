"""Focused persistence and transition checks for work-board M1."""

from hashlib import sha256
from unittest.mock import AsyncMock

import pytest

from src.db.models import Goal, WorkBoardStatus
from src.work_board.contracts import (
    WorkBoardAction,
    WorkBoardActionRequest,
    WorkBoardCommentCreate,
    WorkBoardLinkCreate,
    WorkBoardOwner,
    WorkBoardTaskCreate,
    WorkBoardTaskPatch,
)
from src.work_board.repository import (
    BoardError,
    BoardGoalNotFound,
    BoardGoalOwnerMismatch,
    BoardGoalRevisionConflict,
    BoardIdempotencyConflict,
    BoardOwnerMismatch,
    BoardRevisionConflict,
    WorkBoardRepository,
)


OWNER = WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass")


async def _create(db, *, key: str, title: str = "Task"):
    if await db.get(Goal, "goal-1") is None:
        db.add(
            Goal(
                id="goal-1",
                title="Board test goal",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                revision=1,
            )
        )
        await db.flush()
    return await WorkBoardRepository().create_task(
        db,
        OWNER,
        WorkBoardTaskCreate(
            title=title,
            goal_id="goal-1",
            goal_revision=1,
            idempotency_key=key,
        ),
    )


@pytest.mark.asyncio
async def test_task_persists_after_restart(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        mutation = await _create(db, key="persist")
        task_id = mutation.task.task_id
    async with async_db() as db:
        task = await repository.get_task(db, OWNER, task_id)
        assert task.task_id == task_id
        assert task.status.value == "triage"


@pytest.mark.asyncio
async def test_duplicate_idempotency_returns_same_task(async_db):
    async with async_db() as db:
        first = await _create(db, key="same")
        replay = await _create(db, key="same")
        assert replay.task.task_id == first.task.task_id
        assert replay.idempotent_replay is True


@pytest.mark.asyncio
async def test_idempotency_conflict_is_rejected(async_db):
    async with async_db() as db:
        await _create(db, key="collision", title="first")
        with pytest.raises(BoardIdempotencyConflict):
            await _create(db, key="collision", title="different")


@pytest.mark.asyncio
async def test_cross_owner_and_revoked_session_denied(async_db):
    async with async_db() as db:
        mutation = await _create(db, key="owner")
        with pytest.raises(BoardOwnerMismatch):
            await WorkBoardRepository().get_task(
                db,
                WorkBoardOwner(principal_id="operator:test-bypass", session_id="revoked-session"),
                mutation.task.task_id,
            )


@pytest.mark.asyncio
async def test_dependency_cycle_rejected(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        parent = await _create(db, key="parent", title="Parent")
        child = await _create(db, key="child", title="Child")
        await repository.add_link(
            db,
            OWNER,
            WorkBoardLinkCreate(
                parent_task_id=parent.task.task_id,
                child_task_id=child.task.task_id,
                expected_child_revision=child.task.task_revision,
            ),
        )
        with pytest.raises(BoardError, match="cycle"):
            await repository.add_link(
                db,
                OWNER,
                WorkBoardLinkCreate(
                    parent_task_id=child.task.task_id,
                    child_task_id=parent.task.task_id,
                    expected_child_revision=parent.task.task_revision,
                ),
            )


@pytest.mark.asyncio
async def test_triage_requires_typed_spec_before_promotion(async_db):
    async with async_db() as db:
        mutation = await _create(db, key="promote")
        with pytest.raises(BoardError, match="typed"):
            await WorkBoardRepository().action_task(
                db,
                OWNER,
                mutation.task.task_id,
                WorkBoardActionRequest(
                    action=WorkBoardAction.promote,
                    expected_revision=mutation.task.task_revision,
                ),
            )


@pytest.mark.asyncio
async def test_ready_child_is_demoted_when_new_parent_is_unfinished(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        parent = await _create(db, key="unfinished-parent", title="Parent")
        child = await _create(db, key="ready-child", title="Child")
        child.task.status = WorkBoardStatus.ready
        await db.flush()
        await db.commit()
        link, _event = await repository.add_link(
            db,
            OWNER,
            WorkBoardLinkCreate(
                parent_task_id=parent.task.task_id,
                child_task_id=child.task.task_id,
                expected_child_revision=child.task.task_revision,
            ),
        )
        assert link.child_task_id == child.task.task_id
        assert child.task.status is WorkBoardStatus.todo
        assert child.task.task_revision == 2


@pytest.mark.asyncio
async def test_blocked_authority_patch_preserves_recovery_state(async_db):
    repository = WorkBoardRepository()
    digest = sha256(b"typed-input").hexdigest()
    async with async_db() as db:
        created = await _create(db, key="blocked-preserve")
        blocked = await repository.action_task(
            db,
            OWNER,
            created.task.task_id,
            WorkBoardActionRequest(
                action=WorkBoardAction.block,
                expected_revision=created.task.task_revision,
                block_kind="unknown_effect",
                reason="External effect needs reconciliation",
            ),
        )
        patched = await repository.patch_task(
            db,
            OWNER,
            blocked.task.task_id,
            WorkBoardTaskPatch(
                expected_revision=blocked.task.task_revision,
                capability_id="capability.local",
                typed_input_ref="input:typed",
                typed_input_digest=digest,
                executor_id="executor.local",
            ),
        )
        assert patched.task.status is WorkBoardStatus.blocked
        assert patched.task.block_kind == "unknown_effect"
        assert patched.task.block_source_status == "triage"
        assert patched.task.block_reason == "External effect needs reconciliation"


@pytest.mark.asyncio
async def test_stale_revision_write_is_rejected_after_first_mutation(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        created = await _create(db, key="cas")
        await repository.patch_task(
            db,
            OWNER,
            created.task.task_id,
            WorkBoardTaskPatch(expected_revision=1, title="first"),
        )
        with pytest.raises(BoardRevisionConflict):
            await repository.patch_task(
                db,
                OWNER,
                created.task.task_id,
                WorkBoardTaskPatch(expected_revision=1, title="stale"),
            )


@pytest.mark.asyncio
async def test_comment_requires_revision_and_advances_task_revision(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        created = await _create(db, key="comment-cas")
        comment, _event = await repository.add_comment(
            db,
            OWNER,
            created.task.task_id,
            WorkBoardCommentCreate(expected_revision=1, body="handoff"),
        )
        assert comment.body == "handoff"
        assert created.task.task_revision == 2
        with pytest.raises(BoardRevisionConflict):
            await repository.add_comment(
                db,
                OWNER,
                created.task.task_id,
                WorkBoardCommentCreate(expected_revision=1, body="stale"),
            )


@pytest.mark.asyncio
async def test_goal_existence_owner_and_revision_are_checked(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        with pytest.raises(BoardGoalNotFound):
            await repository.create_task(
                db,
                OWNER,
                WorkBoardTaskCreate(title="missing", goal_id="missing", goal_revision=1, idempotency_key="missing"),
            )
        db.add(
            Goal(
                id="other-goal",
                title="Other",
                owner_principal_id="operator:other",
                owner_session_id="other-session",
                revision=1,
            )
        )
        db.add(
            Goal(
                id="stale-goal",
                title="Stale",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                revision=2,
            )
        )
        await db.flush()
        with pytest.raises(BoardGoalOwnerMismatch):
            await repository.create_task(
                db,
                OWNER,
                WorkBoardTaskCreate(title="other", goal_id="other-goal", goal_revision=1, idempotency_key="other"),
            )
        with pytest.raises(BoardGoalRevisionConflict):
            await repository.create_task(
                db,
                OWNER,
                WorkBoardTaskCreate(title="stale", goal_id="stale-goal", goal_revision=1, idempotency_key="stale"),
            )


@pytest.mark.asyncio
async def test_vault_redaction_applies_before_task_comment_and_block_persistence(async_db, monkeypatch):
    repository = WorkBoardRepository()
    monkeypatch.setattr(
        "src.vault.redaction.vault_repository.list_secret_values",
        AsyncMock(return_value=[("secret-ref", "super-secret-value")]),
    )
    async with async_db() as db:
        db.add(
            Goal(
                id="goal-1",
                title="Board test goal",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                revision=1,
            )
        )
        await db.flush()
        created = await repository.create_task(
            db,
            OWNER,
            WorkBoardTaskCreate(
                title="title super-secret-value",
                body="body super-secret-value",
                goal_id="goal-1",
                goal_revision=1,
                idempotency_key="redaction",
            ),
        )
        assert "super-secret-value" not in created.task.title
        assert "super-secret-value" not in created.task.body
        comment, _event = await repository.add_comment(
            db,
            OWNER,
            created.task.task_id,
            WorkBoardCommentCreate(expected_revision=1, body="comment super-secret-value"),
        )
        assert "super-secret-value" not in comment.body
        blocked = await repository.action_task(
            db,
            OWNER,
            created.task.task_id,
            WorkBoardActionRequest(
                action=WorkBoardAction.block,
                expected_revision=2,
                reason="reason super-secret-value",
            ),
        )
        assert "super-secret-value" not in (blocked.task.block_reason or "")
