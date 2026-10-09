"""Real original specialist stop writer, never synthetic callback closure."""
from tests.general_task_method_lifecycle import native_admission_lifecycle
import pytest
from sqlalchemy import select
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_specialist_evidence_runtime import copied_evidence_fixture


async def waiting_specialist(task_runtime,monkeypatch):
    from src.auth.service import authenticate_session
    from src.db.models import WorkBoardTask,WorkflowRunState
    fixture=await copied_evidence_fixture(task_runtime,monkeypatch)
    sessions,workspace,owner,dispatcher,service,envelope,original,*_=fixture
    operator=await authenticate_session(owner.session_id,touch=False)
    result=await service.execute(dispatcher.jobs,job_id=original['job']['job_id'],
        owner=original['job']['lease']['owner'],fence=original['job']['lease']['fencing_token'],
        envelope=envelope,principal=operator.principal)
    assert not result.get('verified')
    async with sessions() as db:
        parent=await dispatcher.jobs._fetch(db,original['job']['job_id'])
        from src.workflows.general_task_guard import read_manifest
        manifest=read_manifest(parent)
        task=await service.repository.get_task(db,owner,manifest.task_id)
    return fixture,parent,task


@pytest.mark.asyncio
@pytest.mark.parametrize('action',['cancel','pause'])
async def test_original_stop_fences_unclaimed_child_and_preserves_callback_unknown(task_runtime,monkeypatch,action, native_admission_lifecycle):
    from src.db.models import WorkBoardTask,WorkflowRunState
    from src.workflows.general_task_guard import read_manifest,read_general_task_native_cancel,_cancel_witness
    from src.workflows.specialist_stop import verify_specialist_stop
    fixture,parent,task=await waiting_specialist(task_runtime,monkeypatch)
    sessions,workspace,owner,dispatcher,service,envelope,original,reference,plan,planner,transport=fixture
    contacts=len(transport['contacts'])
    if action=='cancel':
        projection=await dispatcher.cancel_task(owner,task.task_id,expected_revision=task.task_revision)
    else:
        await dispatcher.control_general_task(owner,task.task_id,expected_revision=task.task_revision,action='pause')
    async with sessions() as db:
        parent=await dispatcher.jobs._fetch(db,parent.run_identity)
        task=await service.repository.get_task(db,owner,task.task_id)
        from src.db.models import WorkBoardAttempt
        attempt=await db.get(WorkBoardAttempt,read_manifest(parent).attempt_id)
        cancellation=read_general_task_native_cancel(parent,task,attempt)
        assert cancellation['state']=='pending' and cancellation['stop_action']==action
        assert parent.status=='blocked' and attempt.ended_at is None
        witness=_cancel_witness(parent,task,attempt)
        entry=next(item for item in witness.children if item.delegation_stop_checkpoint)
        assert entry.closure is None and entry.delegation_closure_digest is None
        fact=await verify_specialist_stop(db,parent,entry)
        assert len(fact.jobs)==1 and fact.jobs[0].current_status=='cancelled'
        child=await dispatcher.jobs._fetch(db,fact.child_job_id)
        assert child.status=='cancelled' and child.attempt_count==0 and child.effect_receipts_json=='[]'
        child_ids=(fact.child_task_id,fact.child_attempt_id,fact.child_job_id)
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher
    service.stop()
    service.registry.stop()
    service.registry.start()
    restored=GeneralTaskService(service.registry,repository=service.repository,planner=planner)
    restored.start()
    dispatcher=WorkBoardDispatcher(session_provider=sessions,general_tasks=restored)
    await dispatcher.reconcile_linked_attempts()
    assert len(transport['contacts'])==contacts and not (workspace/'copied-result.txt').exists()
    async with sessions() as db:
        assert await dispatcher.jobs._fetch(db,child_ids[2])
        assert len(list((await db.execute(select(WorkBoardTask).where(WorkBoardTask.idempotency_key.like('specialist:%')))).scalars()))==1


@pytest.mark.asyncio
async def test_claimed_fifo_tool_cancel_retains_real_unknown_and_original_rows(task_runtime,monkeypatch, native_admission_lifecycle):
    import asyncio,os,sys
    from src.db.models import WorkflowRunState,WorkBoardAttempt
    from src.workflows.general_task_guard import read_manifest,_cancel_witness
    from src.workflows.specialist_stop import verify_specialist_stop
    fixture,parent,task=await waiting_specialist(task_runtime,monkeypatch)
    sessions,workspace,owner,dispatcher,service,envelope,original,reference,plan,planner,transport=fixture
    fifo=workspace/'copied-result.txt'
    os.mkfifo(fifo)
    execution=asyncio.create_task(dispatcher.reconcile_linked_attempts())
    reader=None
    try:
        for _ in range(400):
            blocked=False
            for frame in sys._current_frames().values():
                while frame is not None:
                    if frame.f_code.co_name=='_open_workspace_file' and frame.f_code.co_filename.endswith('/filesystem_tool.py'):
                        blocked=True
                    frame=frame.f_back
            if blocked:
                async with sessions() as db:
                    native=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.branch_depth==3,
                        WorkflowRunState.job_kind=='general_task_native_tool_v1'))
                    if native is not None and native.attempt_count==1 and native.status=='running':
                        break
            await asyncio.sleep(.01)
        else:
            pytest.fail('Real stock write_file did not reach the bounded FIFO open')
        native_id=native.run_identity
        native_fence=native.fencing_token
        contacts=len(transport['contacts'])
        await dispatcher.cancel_task(owner,task.task_id,expected_revision=task.task_revision)
        async with sessions() as db:
            root=await dispatcher.jobs._fetch(db,parent.run_identity)
            task=await service.repository.get_task(db,owner,task.task_id)
            attempt=await db.get(WorkBoardAttempt,read_manifest(root).attempt_id)
            witness=_cancel_witness(root,task,attempt)
            entry=next(item for item in witness.children if item.delegation_stop_checkpoint)
            fact=await verify_specialist_stop(db,root,entry)
            native=await dispatcher.jobs._fetch(db,native_id)
            assert native.status=='blocked' and native.fencing_token==native_fence+1
            assert native.lease_owner is None and native.attempt_count==1
            assert witness.state=='pending' and entry.closure is None
            assert len(fact.jobs)==2 and not fact.completed_child
            assert attempt.ended_at is None
        # The actual blocked open returns; stock fstat rejects the FIFO. No
        # invented handler/closure stands in for this genuine late callback.
        reader=os.open(fifo,os.O_RDWR|os.O_NONBLOCK)
        await asyncio.wait_for(execution,10)
        await service.observe_native_cancellation(dispatcher.jobs,fact.child_job_id)
        await dispatcher.reconcile_linked_attempts()
        assert len(transport['contacts'])==contacts
        async with sessions() as db:
            native=await dispatcher.jobs._fetch(db,native_id)
            assert native.attempt_count==1 and native.fencing_token==native_fence+1
            root=await dispatcher.jobs._fetch(db,parent.run_identity)
            assert root.status=='blocked' and read_manifest(root).phase=='unknown_recovery'
            task=await service.repository.get_task(db,owner,task.task_id)
            attempt=await db.get(WorkBoardAttempt,read_manifest(root).attempt_id)
            entry=next(item for item in _cancel_witness(root,task,attempt).children if item.delegation_stop_checkpoint)
            observed=await verify_specialist_stop(db,root,entry)
            assert len(observed.observed_closures)==1 and observed.observed_closures[0].invocation_id==native_id
            assert entry.closure is None and entry.delegation_closure_digest is None
    finally:
        if reader is None:
            reader=os.open(fifo,os.O_RDWR|os.O_NONBLOCK)
        if not execution.done():
            try:
                await asyncio.wait_for(execution,10)
            except Exception:
                pass
        os.close(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['capacity_count','capacity_bytes','late_cas'])
async def test_stop_preflight_and_joint_writer_roll_back_every_row(task_runtime,monkeypatch,failure, native_admission_lifecycle):
    import json
    from sqlalchemy import update
    from src.db.models import WorkBoardTask,WorkBoardAttempt,WorkflowRunState
    from src.workflows import general_task_guard as guard
    from src.workflows.job_runtime import DurableJobTransitionError,DurableJobLeaseError,_canonical,_digest
    fixture,parent,task=await waiting_specialist(task_runtime,monkeypatch)
    sessions,workspace,owner,dispatcher,service,envelope,original,reference,plan,planner,transport=fixture
    if failure.startswith('capacity'):
        async with sessions() as db:
            root=await dispatcher.jobs._fetch(db,parent.run_identity)
            history=json.loads(root.checkpoint_receipts_json)
            if failure=='capacity_count':
                while len(history)<51:
                    payload={'inventory':len(history)}
                    history.append({'checkpoint_id':'capacity-test:'+str(len(history)),'payload':payload,
                        'state_digest':_digest(payload),'safe':True,'fencing_token':root.fencing_token})
            else:
                payload={'inventory':'x'*(4*1024*1024)}
                history.append({'checkpoint_id':'capacity-test:bytes','payload':payload,
                    'state_digest':_digest(payload),'safe':True,'fencing_token':root.fencing_token})
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity==root.run_identity).values(
                checkpoint_receipts_json=_canonical(history)))
            await db.commit()
    else:
        original_board=guard._cancel_cas_board
        async def refuse_original(db,selected,*args,**kwargs):
            if selected.task_id==task.task_id:
                raise DurableJobLeaseError('test exact original Board CAS conflict after descendant writes')
            return await original_board(db,selected,*args,**kwargs)
        monkeypatch.setattr(guard,'_cancel_cas_board',refuse_original)
    async def snapshot():
        async with sessions() as db:
            return {kind:[row.model_dump(mode='json') for row in (await db.execute(select(model))).scalars()]
                for kind,model in [('jobs',WorkflowRunState),('tasks',WorkBoardTask),('attempts',WorkBoardAttempt)]}
    before=await snapshot()
    contacts=len(transport['contacts'])
    with pytest.raises((DurableJobTransitionError,DurableJobLeaseError)):
        await dispatcher.jobs.cancel_general_task_native_parent(parent.run_identity,
            operator_owner=owner,expected_task_revision=task.task_revision)
    assert await snapshot()==before
    assert len(transport['contacts'])==contacts and not (workspace/'copied-result.txt').exists()
