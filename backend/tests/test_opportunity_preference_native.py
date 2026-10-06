"""System-only recommendation publication and original attempt boundaries."""
import pytest
from unittest.mock import AsyncMock

from src.db.models import Goal, WorkBoardTask, WorkBoardStatus
from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate, WorkBoardTaskPatch, WorkBoardActionRequest
from src.work_board.repository import WorkBoardRepository, BoardError
from src.work_board.opportunity_preference_native import CAPABILITY
from src.work_board.triage import _validate_proposed_typed_inputs

OWNER = WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["triage", "todo"])
@pytest.mark.parametrize("callback", [False, True])
async def test_generic_creation_cannot_publish_recommendation(async_db, status, callback):
    check = AsyncMock()
    request = WorkBoardTaskCreate(title="Unauthorized recommendation", goal_id="goal", goal_revision=1,
        capability_id=CAPABILITY, status=status, idempotency_key="untrusted", requires_review=False,
        typed_input_ref="artifacts/untrusted.json", typed_input_digest="a"*64)
    async with async_db() as db:
        with pytest.raises(BoardError) as denied:
            await WorkBoardRepository().create_task(db, OWNER, request,
                publication_authority_check=check if callback else None)
        assert denied.value.code == "opportunity_recommendation_system_only"
        check.assert_not_awaited()


async def _negative_task(db, capability=None, status=WorkBoardStatus.triage):
    # Deliberately corrupted persisted rows test fail-closed guards, never a successful source.
    db.add(Goal(id="goal", title="Negative fixture", owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id, revision=1))
    task = WorkBoardTask(owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id,
        title="Negative fixture", goal_id="goal", goal_revision=1,
        capability_id=capability, status=status, idempotency_scope="negative", idempotency_key="negative")
    db.add(task)
    await db.flush()
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_generic_patch_cannot_create_or_rebind_system_recommendation(async_db, existing):
    async with async_db() as db:
        task = await _negative_task(db, CAPABILITY if existing else None)
        changes = {"title":"Replacement"} if existing else {"capability_id":CAPABILITY}
        with pytest.raises(BoardError) as denied:
            await WorkBoardRepository().patch_task(db, OWNER, task.task_id,
                WorkBoardTaskPatch(expected_revision=task.task_revision, **changes))
        assert denied.value.code == "opportunity_recommendation_system_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["promote", "retry", "unblock"])
async def test_generic_actions_cannot_renew_original_recommendation(async_db, action):
    async with async_db() as db:
        task = await _negative_task(db, CAPABILITY)
        with pytest.raises(BoardError) as denied:
            await WorkBoardRepository().action_task(db, OWNER, task.task_id,
                WorkBoardActionRequest(expected_revision=task.task_revision, action=action,
                    resolution="Caller claims resolved" if action == "unblock" else None))
        assert denied.value.code in {"opportunity_recommendation_system_only", "opportunity_recommendation_original_attempt_required"}


def test_generic_proposal_cannot_select_system_capability():
    parent = WorkBoardTask(task_id="parent", title="Ordinary", goal_id="goal", goal_revision=1,
        owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id)
    with pytest.raises(BoardError) as denied:
        _validate_proposed_typed_inputs([{"capability_id":CAPABILITY}], parent=parent)
    assert denied.value.code == "opportunity_recommendation_system_only"


def test_system_parent_cannot_be_replanned_to_ordinary_capability():
    parent = WorkBoardTask(task_id="parent", title="Recommendation", goal_id="goal", goal_revision=1,
        owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id, capability_id=CAPABILITY)
    with pytest.raises(BoardError) as denied:
        _validate_proposed_typed_inputs([{"capability_id":"browser.public-task.v1"}], parent=parent)
    assert denied.value.code == "opportunity_recommendation_system_only"
