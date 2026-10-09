"""Canonical child publication rollback; display receipts grant no execution."""
import json
import pytest
from sqlalchemy import select,func
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_specialist_evidence_runtime import copied_evidence_fixture,reserve_evidence_specialist

@pytest.mark.asyncio
async def test_existing_lineage_key_collision_rolls_back_actual_child_publication(task_runtime,monkeypatch):
    from src.db.models import WorkBoardEvent,WorkBoardTask,WorkBoardAttempt,WorkflowRunState
    from src.workflows.specialist_delegation import execute_specialist,current_delegation
    from src.workflows.specialist_lineage import SpecialistLineage,lineage_event_key
    from src.workflows.specialist_lifecycle import CREATION_KEY,read_fact,SpecialistChildCreationV1
    from src.work_board.general_task import digest
    from src.work_board.repository import BoardError
    fixture = await copied_evidence_fixture(task_runtime,monkeypatch)
    sessions,workspace,owner,dispatcher,service,envelope,original,reference,plan,planner,transport = fixture
    context,principal = await reserve_evidence_specialist(fixture)
    reservation = context.reservation
    lineage = SpecialistLineage(parent_task_id=context.task.task_id,parent_attempt_id=context.attempt.attempt_id,
        step_id=context.request.step_id,child_task_id=reservation.child_task_id,
        child_attempt_id=reservation.child_attempt_id,child_job_id=reservation.child_job_id,
        delegation_invocation_id=context.callback.run_identity,reservation_digest=digest(reservation.model_dump(mode='json')))
    payload = lineage.model_dump(mode='json')
    async with sessions() as db:
        db.add(WorkBoardEvent(task_id=context.task.task_id,owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,actor_principal_id=owner.principal_id,
            kind='task.updated',metadata_json=json.dumps({'unrelated':True}),
            mutation_idempotency_key=lineage_event_key('task.specialist_published',lineage),
            mutation_request_digest=digest(payload)))
    with pytest.raises(BoardError) as failure:
        await execute_specialist(dispatcher.jobs,service=service,invocation_id=context.callback.run_identity,
            fencing_token=context.callback.fencing_token,principal=principal)
    assert failure.value.code == 'specialist_lineage_collision'
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(WorkBoardTask).where(WorkBoardTask.task_id==reservation.child_task_id)) == 0
        assert await db.scalar(select(func.count()).select_from(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id==reservation.child_attempt_id)) == 0
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState).where(WorkflowRunState.run_identity==reservation.child_job_id)) == 0
        restored = await current_delegation(db,context.callback.run_identity)
        assert read_fact(restored.callback,CREATION_KEY,SpecialistChildCreationV1) is None
    assert not (workspace/'copied-result.txt').exists()
    assert len(transport['contacts']) == 1
