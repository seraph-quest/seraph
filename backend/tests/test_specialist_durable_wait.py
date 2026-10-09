"""Actual sealed callback wait and original child wake, no provider spend."""
import json
import pytest
from sqlalchemy import select
from tests.test_specialist_evidence_runtime import copied_evidence_fixture
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime

@pytest.mark.asyncio
async def test_wait_releases_callback_and_reconcile_runs_exact_original_child(task_runtime,monkeypatch):
    from src.auth.service import authenticate_session
    from src.db.models import WorkflowRunState,WorkBoardTask,WorkBoardAttempt
    from src.workflows.specialist_delegation import read_reservation,current_delegation
    from src.workflows.specialist_lifecycle import read_fact,WAIT_KEY,SpecialistWaitV1
    fixture = await copied_evidence_fixture(task_runtime,monkeypatch)
    sessions,workspace,owner,dispatcher,service,envelope,original,reference,plan,planner,transport = fixture
    operator = await authenticate_session(owner.session_id,touch=False)
    result = await service.execute(dispatcher.jobs,job_id=original['job']['job_id'],
        owner=original['job']['lease']['owner'],fence=original['job']['lease']['fencing_token'],
        envelope=envelope,principal=operator.principal)
    assert not result.get('verified'),result
    assert not (workspace/'copied-result.txt').exists()
    async with sessions() as db:
        callback = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.failure_reason=='specialist_wait'))
        assert callback is not None
        wait = read_fact(callback,WAIT_KEY,SpecialistWaitV1)
        reservation = read_reservation(callback)
        assert callback.lease_owner is None and callback.lease_expires_at is None
        assert callback.attempt_count == 1 and callback.fencing_token == wait.original_claim_fence
        context = await current_delegation(db,callback.run_identity)
        assert context.reservation == reservation
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == wait.child_job_id))
        assert child.status == 'accepted' and child.attempt_count == 0
        ids = wait.child_task_id,wait.child_attempt_id,wait.child_job_id
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.general_task import GeneralTaskService
    registry = service.registry
    service.stop()
    registry.stop()
    registry.start()
    restored_service = GeneralTaskService(registry,repository=service.repository,planner=planner)
    restored_service.start()
    restarted = WorkBoardDispatcher(session_provider=sessions,general_tasks=restored_service,jobs=dispatcher.jobs)
    await restarted.reconcile_linked_attempts()
    async with sessions() as db:
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == ids[2]))
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == ids[0]))
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == ids[1]))
        assert child.status == 'succeeded',(child.status,child.failure_reason)
        assert task.status.value == 'done' and attempt.workflow_run_id == ids[2]
        assert child.attempt_count == 1
        assert len((await db.execute(select(WorkBoardTask).where(WorkBoardTask.idempotency_key.like('specialist:%')))).scalars().all()) == 1
    assert (workspace/'copied-result.txt').read_text() == 'explicit copied bytes'
    assert len(transport['contacts']) == 1
    await restarted.reconcile_linked_attempts()
    async with sessions() as db:
        callback = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == wait.invocation_id))
        assert callback.status == 'succeeded' and callback.failure_reason is None
        from src.workflows.specialist_lifecycle import CLOSURE_KEY,SpecialistDelegationClosureV1
        closure = read_fact(callback,CLOSURE_KEY,SpecialistDelegationClosureV1)
        assert closure.outcome == 'durable_result_verified' and not closure.unresolved
        assert callback.lease_owner is None and callback.lease_expires_at is None
        assert callback.attempt_count == 1 and callback.fencing_token == wait.original_claim_fence
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == ids[2]))
        assert child.attempt_count == 1 and child.status == 'succeeded'
        parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == original['job']['job_id']))
        assert parent.status == 'succeeded',parent.failure_reason
    assert len(transport['contacts']) == 1


@pytest.mark.asyncio
async def test_changed_original_wait_cannot_contact_admitted_child(task_runtime,monkeypatch):
    from src.auth.service import authenticate_session
    from src.db.models import WorkflowRunState
    from src.workflows.specialist_lifecycle import WAIT_KEY
    from src.work_board.general_task import digest
    fixture = await copied_evidence_fixture(task_runtime,monkeypatch)
    sessions,workspace,owner,dispatcher,service,envelope,original,reference,plan,planner,transport = fixture
    operator = await authenticate_session(owner.session_id,touch=False)
    await service.execute(dispatcher.jobs,job_id=original['job']['job_id'],
        owner=original['job']['lease']['owner'],fence=original['job']['lease']['fencing_token'],
        envelope=envelope,principal=operator.principal)
    async with sessions() as db:
        callback = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.failure_reason=='specialist_wait'))
        history = json.loads(callback.checkpoint_receipts_json)
        record = next(item for item in history if item['checkpoint_id']==WAIT_KEY)
        child_id = record['payload']['child_job_id']
        record['payload']['child_authority_digest'] = '0'*64
        record['state_digest'] = digest(record['payload'])
        callback.checkpoint_receipts_json = json.dumps(history,sort_keys=True,separators=(',',':'))
    await dispatcher.reconcile_linked_attempts()
    async with sessions() as db:
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==child_id))
        assert child.status == 'accepted' and child.attempt_count == 0
        assert json.loads(child.effect_receipts_json) == []
    assert not (workspace/'copied-result.txt').exists()
    assert len(transport['contacts']) == 1


@pytest.mark.asyncio
async def test_staged_callback_output_tamper_holds_actual_original_wait(task_runtime,monkeypatch):
    from src.auth.service import authenticate_session
    from src.db.models import WorkflowRunState
    from src.workflows.specialist_lifecycle import WAIT_KEY,CLOSURE_KEY,read_fact,SpecialistWaitV1,SpecialistDelegationClosureV1
    from src.workflows.specialist_result import settle_specialist_callback
    from src.work_board.general_task import digest
    from src.work_board.repository import BoardError
    from src.work_board import input_artifacts
    fixture = await copied_evidence_fixture(task_runtime,monkeypatch)
    sessions,workspace,owner,dispatcher,service,envelope,original,reference,plan,planner,transport = fixture
    operator = await authenticate_session(owner.session_id,touch=False)
    await service.execute(dispatcher.jobs,job_id=original['job']['job_id'],
        owner=original['job']['lease']['owner'],fence=original['job']['lease']['fencing_token'],
        envelope=envelope,principal=operator.principal)
    await dispatcher.reconcile_linked_attempts()
    async with sessions() as db:
        callback = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.failure_reason=='specialist_wait'))
        wait = read_fact(callback,WAIT_KEY,SpecialistWaitV1)
        from src.workflows.general_task_guard import child_binding
        binding = child_binding(callback)
        key = digest([callback.run_identity,binding.plan_digest,binding.step_id])
    actual_write = input_artifacts._write_payload
    def mutate_actual_output(path,content):
        actual_write(path,content)
        if path.name.startswith(key+'-'):
            path.write_bytes(b'changed actual staged callback output')
    monkeypatch.setattr(input_artifacts,'_write_payload',mutate_actual_output)
    with pytest.raises(BoardError):
        await settle_specialist_callback(dispatcher.jobs,wait.invocation_id)
    async with sessions() as db:
        callback = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==wait.invocation_id))
        parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==original['job']['job_id']))
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==wait.child_job_id))
        assert callback.status=='paused' and callback.failure_reason=='specialist_wait'
        assert read_fact(callback,CLOSURE_KEY,SpecialistDelegationClosureV1) is None
        assert parent.status=='paused' and parent.failure_reason=='general_task_native_wait'
        assert child.status=='succeeded' and child.attempt_count==1
    assert (workspace/'copied-result.txt').read_text()=='explicit copied bytes'
    assert len(transport['contacts'])==1
