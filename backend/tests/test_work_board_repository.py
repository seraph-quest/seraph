"""Focused persistence and transition checks for work-board M1."""

from hashlib import sha256
from unittest.mock import AsyncMock

import pytest

from src.db.models import (
    Goal,
    WorkBoardComment,
    WorkBoardLink,
    WorkBoardStatus,
    WorkBoardTask,
)
from src.work_board.contracts import (
    WorkBoardAction,
    WorkBoardActionRequest,
    WorkBoardCommentCreate,
    WorkBoardLinkCreate,
    WorkBoardLinkDelete,
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
from pydantic import ValidationError


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
async def test_malformed_cross_owner_comments_are_excluded_from_detail(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        created = await _create(db, key="comment-owner-scope")
        foreign_parent = WorkBoardTask(
            task_id="foreign-detail-parent",
            owner_principal_id="operator:other",
            owner_session_id="other-session",
            goal_id="foreign-goal-parent",
            title="Foreign parent",
            idempotency_key="foreign-detail-parent",
        )
        foreign_child = WorkBoardTask(
            task_id="foreign-detail-child",
            owner_principal_id="operator:other",
            owner_session_id="other-session",
            goal_id="foreign-goal-child",
            title="Foreign child",
            idempotency_key="foreign-detail-child",
        )
        db.add_all([foreign_parent, foreign_child])
        await db.flush()
        db.add_all(
            [
                WorkBoardComment(
                    task_id=created.task.task_id,
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id="old-session",
                    author_principal_id=OWNER.principal_id,
                    author_session_id="old-session",
                    body="stale session comment",
                ),
                WorkBoardComment(
                    task_id=created.task.task_id,
                    owner_principal_id="operator:other",
                    owner_session_id="other-session",
                    author_principal_id="operator:other",
                    author_session_id="other-session",
                    body="cross owner comment",
                ),
                WorkBoardLink(
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    parent_task_id=foreign_parent.task_id,
                    child_task_id=created.task.task_id,
                ),
                WorkBoardLink(
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    parent_task_id=created.task.task_id,
                    child_task_id=foreign_child.task_id,
                ),
            ]
        )
        await db.commit()
        detail = await repository.get_detail(db, OWNER, created.task.task_id)
        assert detail["comments"] == []
        assert detail["parents"] == []
        assert detail["children"] == []


@pytest.mark.asyncio
async def test_delete_link_requires_owner_for_both_endpoints(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        child = await _create(db, key="delete-link-child")
        foreign_parent = WorkBoardTask(
            task_id="foreign-delete-parent",
            owner_principal_id="operator:other",
            owner_session_id="other-session",
            goal_id="foreign-delete-goal",
            title="Foreign parent",
            idempotency_key="foreign-delete-parent",
        )
        db.add(foreign_parent)
        await db.flush()
        db.add(
            WorkBoardLink(
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                parent_task_id=foreign_parent.task_id,
                child_task_id=child.task.task_id,
            )
        )
        await db.commit()
        with pytest.raises(BoardOwnerMismatch):
            await repository.delete_link(
                db,
                OWNER,
                WorkBoardLinkDelete(
                    parent_task_id=foreign_parent.task_id,
                    child_task_id=child.task.task_id,
                    expected_child_revision=child.task.task_revision,
                ),
            )


def test_patch_rejects_unsafe_reference_and_digest_inputs():
    digest = sha256(b"typed-input").hexdigest()
    with pytest.raises(ValidationError):
        WorkBoardTaskPatch(
            expected_revision=1,
            typed_input_ref="/private/operator-secret.json",
            typed_input_digest=digest,
        )
    with pytest.raises(ValidationError):
        WorkBoardTaskPatch(
            expected_revision=1,
            typed_input_ref="workspace-json:inputs/../secret.json",
            typed_input_digest=digest,
        )
    with pytest.raises(ValidationError):
        WorkBoardTaskPatch(
            expected_revision=1,
            capability_id="capability.local",
            typed_input_ref="workspace-json:inputs/task.json",
            typed_input_digest="not-a-digest",
        )


@pytest.mark.parametrize("field", ["capability_id", "executor_id", "assignee_id"])
def test_opaque_task_identifiers_reject_relative_paths(field):
    create_values = {
        "title": "Path-shaped identifier",
        "goal_id": "goal-1",
        "goal_revision": 1,
        "idempotency_key": f"create-{field}",
        field: "guardian/research",
    }
    with pytest.raises(ValidationError):
        WorkBoardTaskCreate(**create_values)

    with pytest.raises(ValidationError):
        WorkBoardTaskPatch(expected_revision=1, **{field: "guardian/research"})


@pytest.mark.parametrize("field", ["reviewer_id", "origin_thread_id"])
def test_create_only_opaque_identifiers_reject_relative_paths(field):
    with pytest.raises(ValidationError):
        WorkBoardTaskCreate(
            title="Path-shaped create identifier",
            goal_id="goal-1",
            goal_revision=1,
            idempotency_key=f"create-only-{field}",
            **{field: "guardian/research"},
        )


@pytest.mark.asyncio
async def test_create_preserves_safe_relative_typed_input_reference(async_db):
    digest = sha256(b"typed-create").hexdigest()
    repository = WorkBoardRepository()
    async with async_db() as db:
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
        mutation = await repository.create_task(
            db,
            OWNER,
            WorkBoardTaskCreate(
                title="Typed input task",
                goal_id="goal-1",
                goal_revision=1,
                capability_id="guardian.research",
                executor_id="executor.local",
                assignee_id="operator.worker",
                typed_input_ref="workspace-json:inputs/task.json",
                typed_input_digest=digest,
                idempotency_key="typed-create",
            ),
        )
        assert mutation.task.typed_input_ref == "workspace-json:inputs/task.json"
        assert mutation.task.typed_input_digest == digest


@pytest.mark.asyncio
async def test_typed_input_patch_requires_pair_and_preserves_complete_changes(async_db):
    digest = sha256(b"typed-input").hexdigest()
    with pytest.raises(ValidationError):
        WorkBoardTaskPatch(
            expected_revision=1,
            typed_input_ref="workspace-json:inputs/task.json",
            typed_input_digest=None,
        )
    with pytest.raises(ValidationError):
        WorkBoardTaskPatch(
            expected_revision=1,
            typed_input_ref=None,
            typed_input_digest=digest,
        )

    repository = WorkBoardRepository()
    async with async_db() as db:
        created = await _create(db, key="typed-pair")
        first = await repository.patch_task(
            db,
            OWNER,
            created.task.task_id,
            WorkBoardTaskPatch(
                expected_revision=created.task.task_revision,
                typed_input_ref="workspace-json:inputs/first.json",
                typed_input_digest=digest,
            ),
        )
        assert first.task.typed_input_ref == "workspace-json:inputs/first.json"
        assert first.task.typed_input_digest == digest

        second_digest = sha256(b"typed-input-second").hexdigest()
        second = await repository.patch_task(
            db,
            OWNER,
            created.task.task_id,
            WorkBoardTaskPatch(
                expected_revision=first.task.task_revision,
                typed_input_ref="workspace-json:inputs/second.json",
                typed_input_digest=second_digest,
            ),
        )
        assert second.task.typed_input_ref == "workspace-json:inputs/second.json"
        assert second.task.typed_input_digest == second_digest

        second.task.status = WorkBoardStatus.todo
        await db.flush()
        with pytest.raises(BoardError) as raised:
            await repository.patch_task(
                db,
                OWNER,
                second.task.task_id,
                WorkBoardTaskPatch(
                    expected_revision=second.task.task_revision,
                    typed_input_ref=None,
                    typed_input_digest=None,
                ),
            )
        assert raised.value.code == "typed_spec_required"


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
async def test_cycle_check_ignores_malformed_cross_owner_link_endpoints(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        parent = await _create(db, key="cycle-owner-parent", title="Parent")
        child = await _create(db, key="cycle-owner-child", title="Child")
        foreign_task = WorkBoardTask(
            task_id="foreign-cycle-node",
            owner_principal_id="operator:other",
            owner_session_id="other-session",
            goal_id="foreign-goal",
            title="Foreign node",
            idempotency_key="foreign-cycle-node",
        )
        db.add(foreign_task)
        await db.flush()
        db.add_all(
            [
                WorkBoardLink(
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    parent_task_id=child.task.task_id,
                    child_task_id=foreign_task.task_id,
                ),
                WorkBoardLink(
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    parent_task_id=foreign_task.task_id,
                    child_task_id=parent.task.task_id,
                ),
            ]
        )
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
        assert link.parent_task_id == parent.task.task_id
        assert link.child_task_id == child.task.task_id


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
async def test_task_detail_reports_owned_dependency_progress(monkeypatch):
    class FakeResult:
        def __init__(self, rows=()):
            self.rows = list(rows)

        def scalars(self):
            return self

        def all(self):
            return self.rows

    child = WorkBoardTask(
        task_id="detail-progress-child",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        goal_id="goal-1",
        title="Child",
        idempotency_key="detail-progress-child",
    )
    repository = WorkBoardRepository()
    monkeypatch.setattr(repository, "_owned_task", AsyncMock(return_value=child))

    async def get_progress(parent_status):
        db = type("FakeSession", (), {})()
        db.execute = AsyncMock(
            side_effect=[
                FakeResult(),  # attempts
                FakeResult(),  # comments
                FakeResult([("detail-progress-parent", parent_status)]),
                FakeResult(),  # children
                FakeResult(),  # events
            ]
        )
        return await repository.get_detail(db, OWNER, child.task_id)

    incomplete = await get_progress(WorkBoardStatus.triage)
    completed = await get_progress(WorkBoardStatus.done)

    assert incomplete["dependency_counts"] == (1, 0)
    assert completed["dependency_counts"] == (1, 1)


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
async def test_todo_creation_requires_capability_but_not_executor(async_db):
    digest = sha256(b"todo-input").hexdigest()
    with pytest.raises(ValidationError, match="registered capability"):
        WorkBoardTaskCreate(
            title="Missing capability",
            goal_id="goal-1",
            goal_revision=1,
            status=WorkBoardStatus.todo,
            typed_input_ref="workspace-json:inputs/todo.json",
            typed_input_digest=digest,
            idempotency_key="todo-missing-capability",
        )

    valid = WorkBoardTaskCreate(
        title="Bounded Todo",
        goal_id="goal-1",
        goal_revision=1,
        status=WorkBoardStatus.todo,
        capability_id="capability.local",
        typed_input_ref="workspace-json:inputs/todo.json",
        typed_input_digest=digest,
        idempotency_key="todo-with-capability",
    )
    assert valid.executor_id is None

    async with async_db() as db:
        await _create(db, key="todo-goal-seed")
        mutation = await WorkBoardRepository().create_task(db, OWNER, valid)
        assert mutation.task.status is WorkBoardStatus.todo
        assert mutation.task.executor_id is None


@pytest.mark.asyncio
async def test_triage_to_todo_requires_capability_and_allows_unassigned_executor(async_db):
    repository = WorkBoardRepository()
    digest = sha256(b"promotion-input").hexdigest()
    async with async_db() as db:
        missing_capability = await _create(db, key="promotion-missing-capability")
        missing_capability.task.typed_input_ref = "workspace-json:inputs/promotion.json"
        missing_capability.task.typed_input_digest = digest
        await db.flush()
        with pytest.raises(BoardError) as raised:
            await repository.action_task(
                db,
                OWNER,
                missing_capability.task.task_id,
                WorkBoardActionRequest(
                    action=WorkBoardAction.promote,
                    expected_revision=missing_capability.task.task_revision,
                ),
            )
        assert raised.value.code == "capability_required"
        assert missing_capability.task.status is WorkBoardStatus.triage

        valid = await _create(db, key="promotion-with-capability")
        valid.task.typed_input_ref = "workspace-json:inputs/promotion.json"
        valid.task.typed_input_digest = digest
        valid.task.capability_id = "capability.local"
        await db.flush()
        promoted = await repository.action_task(
            db,
            OWNER,
            valid.task.task_id,
            WorkBoardActionRequest(
                action=WorkBoardAction.promote,
                expected_revision=valid.task.task_revision,
            ),
        )
        assert promoted.task.status is WorkBoardStatus.todo
        assert promoted.task.executor_id is None


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
@pytest.mark.parametrize(
    "block_kind",
    ["unknown_effect", "cost_liability", "reconcile_admission_binding"],
)
async def test_blocked_authority_patch_requires_reconciliation(async_db, block_kind):
    repository = WorkBoardRepository()
    digest = sha256(b"typed-input").hexdigest()
    async with async_db() as db:
        created = await _create(db, key="blocked-preserve")
        created.task.status = WorkBoardStatus.blocked
        created.task.block_source_status = WorkBoardStatus.triage.value
        created.task.block_kind = block_kind
        created.task.block_reason = "External effect needs reconciliation"
        await db.flush()
        with pytest.raises(BoardError) as raised:
            await repository.patch_task(
                db,
                OWNER,
                created.task.task_id,
                WorkBoardTaskPatch(
                    expected_revision=created.task.task_revision,
                    capability_id="capability.local",
                    typed_input_ref="input:typed",
                    typed_input_digest=digest,
                    executor_id="executor.local",
                    assignee_id="operator:worker",
                ),
            )
        assert raised.value.code == "typed_reconcile_required"
        current = await repository.get_task(db, OWNER, created.task.task_id)
        assert current.status is WorkBoardStatus.blocked
        assert current.block_kind == block_kind
        assert current.block_source_status == "triage"
        assert current.block_reason == "External effect needs reconciliation"


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
