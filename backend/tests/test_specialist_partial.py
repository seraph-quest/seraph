"""Actual authenticated partial review over real stopped specialist debt."""
import json
import pytest
from sqlalchemy import select
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_specialist_parent_synthesis import genuine_mcp_registry,charged_parent


async def failed_stopped_parent(task_runtime,monkeypatch,registry,descriptor):
    from src.db.models import WorkBoardTask,WorkflowRunState
    from src.workflows.general_task_guard import read_manifest
    fixture=await charged_parent(task_runtime,monkeypatch,registry,descriptor)
    sessions,workspace,owner,service,dispatcher,planner,transport,request,task_id=fixture
    (workspace/'child-B.txt').mkdir()
    await dispatcher.run_pass()
    for _ in range(8):
        await dispatcher.reconcile_linked_attempts()
        async with sessions() as db:
            children=list((await db.execute(select(WorkBoardTask).where(WorkBoardTask.idempotency_key.like('specialist:%')))).scalars())
            unknown=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.branch_depth==3,
                WorkflowRunState.status=='unknown_external_effect'))
            if len(children)==2 and unknown is not None:
                break
    else:
        pytest.fail('Second actual stock write did not retain its real Unknown')
    async with sessions() as db:
        task=await service.repository.get_task(db,owner,task_id)
    await dispatcher.cancel_task(owner,task_id,expected_revision=task.task_revision)
    async with sessions() as db:
        task=await service.repository.get_task(db,owner,task_id)
        parent=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind=='agent.task.v1',
            WorkflowRunState.parent_job_id.is_(None)))
        manifest=read_manifest(parent)
    return fixture,task,parent,manifest


@pytest.mark.asyncio
async def test_authenticated_partial_overlay_preserves_stop_and_replays_after_restart(task_runtime,monkeypatch):
    import httpx
    from fastapi import FastAPI
    from src.api.work_board import router
    from src.auth.service import authenticate_session
    from src.work_board.contracts import WorkBoardActionRequest
    from src.workflows.specialist_partial import accept_partial_results,read_partial_overlay
    from src.workflows.general_task_guard import _cancel_witness,read_manifest
    from src.db.models import WorkBoardAttempt,WorkBoardEvent,WorkflowRunState
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher
    async with genuine_mcp_registry(task_runtime[1],monkeypatch) as (registry,descriptor,received,protocol):
        fixture,task,parent,manifest=await failed_stopped_parent(task_runtime,monkeypatch,registry,descriptor)
        sessions,workspace,owner,service,dispatcher,planner,transport,original,task_id=fixture
        operator=await authenticate_session(owner.session_id,touch=False)
        body=WorkBoardActionRequest(action='accept_partial_results',expected_revision=task.task_revision,
            partial_decision={'idempotency_key':'12b5db81-2cf2-492d-aa4d-8a539683da24','attempt_id':manifest.attempt_id,
                'workflow_run_id':parent.run_identity,'expected_manifest_revision':manifest.manifest_revision,
                'expected_plan_revision':manifest.plan_revision,'selected_step_ids':['delegate-A'],'acknowledge_unresolved':True})
        async with sessions() as db:
            attempt=await db.get(WorkBoardAttempt,manifest.attempt_id)
            before_stop=_cancel_witness(parent,task,attempt).model_dump(mode='json')
            before_attempt=attempt.model_dump(mode='json')
        contacts=len(transport['contacts'])
        # Metadata review remains available after both original execution
        # cutoffs; this clock grants no execution or provider contact.
        from datetime import timedelta
        from src.workflows import job_runtime
        monkeypatch.setattr(job_runtime,'_utc_now',lambda:manifest.original_deadline_at+timedelta(seconds=1))
        from src.api import work_board as board_api
        monkeypatch.setattr(board_api,'dispatcher',dispatcher)
        monkeypatch.setattr(board_api,'get_session',sessions)
        app=FastAPI();app.include_router(router)
        @app.middleware('http')
        async def current_auth(request,call_next):
            request.state.operator=await authenticate_session(owner.session_id,touch=False)
            return await call_next(request)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            original_detail=await client.get(f'/work-board/tasks/{task_id}')
            original_plan=await client.get(f'/work-board/tasks/{task_id}/plan')
            assert original_detail.status_code==original_plan.status_code==200
            original_detail=original_detail.json();original_plan=original_plan.json()
            issued_request=body.model_dump(mode='json',exclude_unset=True)
            response=await client.post(f'/work-board/tasks/{task_id}/actions',json=issued_request)
        assert response.status_code==200,response.text
        result=response.json()
        assert not result['idempotent_replay'] and result['partial_review']['selected_step_ids']==['delegate-A']
        assert result['partial_review']['state']=='partial_review_pending_debt'
        assert result['partial_review']['unresolved_job_ids']
        assert result['partial_review']['current_unresolved_job_ids']
        async with sessions() as db:
            current=await dispatcher.jobs._fetch(db,parent.run_identity)
            current_task=await service.repository.get_task(db,owner,task_id)
            attempt=await db.get(WorkBoardAttempt,manifest.attempt_id)
            assert current_task.task_revision==task.task_revision and current_task.status==task.status
            assert current.status==parent.status=='blocked' and read_manifest(current)==manifest
            assert _cancel_witness(current,current_task,attempt).model_dump(mode='json')==before_stop
            assert attempt.model_dump(mode='json')==before_attempt
            assert len(list((await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind=='task.partial_results_accepted'))).scalars()))==1
            plan=await service.plan(db,owner,task_id)
            assert plan['native_execution']['partial_review']['decision_digest']==result['partial_review']['decision_digest']
        service.stop(); registry.stop(); registry.start()
        restored=GeneralTaskService(registry,repository=service.repository,planner=planner)
        restored.start()
        restarted=WorkBoardDispatcher(session_provider=sessions,general_tasks=restored)
        monkeypatch.setattr(board_api,'dispatcher',restarted)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post(f'/work-board/tasks/{task_id}/actions',json=body.model_dump(mode='json',exclude_unset=True))
        assert response.status_code==200,response.text
        replay=response.json()
        assert replay['idempotent_replay'] and replay['partial_review']==result['partial_review']
        await restarted.reconcile_linked_attempts()
        assert len(transport['contacts'])==contacts and not received and not (workspace/'parent.txt').exists()
        assert (workspace/'child-A.txt').read_bytes()==b'physical first\n'
        # Content-free real producer packet for the independent frontend parser.
        from pathlib import Path
        Path('/home/pawel/repos/seraph/.agent-evidence/986/c1-delegation/partial-r61-ui-wire.json').write_text(
            json.dumps({'original_task_detail':original_detail,'issued_request':issued_request,
                'original_plan':original_plan,
                'native_execution':{key:original_plan['native_execution'][key] for key in ('partial_review_options','partial_review')
                    if key in original_plan['native_execution']},
                'action_response':result},sort_keys=True,indent=2)+'\n')


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['tamper','delete','cost_membership','extra_descendant','checkpoint_capacity','writer_rollback'])
async def test_partial_decision_denies_changed_evidence_and_rolls_back_whole_writer(task_runtime,monkeypatch,mutation):
    from sqlalchemy import update
    from src.auth.service import authenticate_session
    from src.db.models import WorkBoardTask,WorkBoardAttempt,WorkBoardEvent,WorkflowRunState,InferenceCostReservation
    from src.work_board.contracts import WorkBoardActionRequest
    from src.work_board.repository import BoardError,WorkBoardRepository
    from src.workflows.specialist_partial import accept_partial_results
    from src.workflows.job_runtime import DurableJobError,_canonical,_digest
    async with genuine_mcp_registry(task_runtime[1],monkeypatch) as (registry,descriptor,received,protocol):
        fixture,task,parent,manifest=await failed_stopped_parent(task_runtime,monkeypatch,registry,descriptor)
        sessions,workspace,owner,service,dispatcher,planner,transport,original,task_id=fixture
        operator=await authenticate_session(owner.session_id,touch=False)
        body=WorkBoardActionRequest(action='accept_partial_results',expected_revision=task.task_revision,
            partial_decision={'idempotency_key':'a25182b9-bac5-4ff6-b127-204fbca93b69','attempt_id':manifest.attempt_id,
                'workflow_run_id':parent.run_identity,'expected_manifest_revision':manifest.manifest_revision,
                'expected_plan_revision':manifest.plan_revision,'selected_step_ids':['delegate-A'],'acknowledge_unresolved':True})
        if mutation=='tamper':
            (workspace/'child-A.txt').write_bytes(b'changed physical output')
        elif mutation=='delete':
            (workspace/'child-A.txt').unlink()
        elif mutation=='cost_membership':
            async with sessions() as db:
                row=await db.scalar(select(InferenceCostReservation))
                await db.execute(update(InferenceCostReservation).where(InferenceCostReservation.operation_id==row.operation_id).values(evidence_json='[]'))
                await db.commit()
        elif mutation=='extra_descendant':
            async with sessions() as db:
                row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.branch_depth==3))
                forged=WorkflowRunState.model_validate({**row.model_dump(),'id':'negative-extra-row',
                    'run_identity':'negative-extra-descendant','idempotency_key':'negative-extra-descendant','idempotency_binding':None})
                db.add(forged);await db.commit()
        elif mutation=='checkpoint_capacity':
            async with sessions() as db:
                row=await dispatcher.jobs._fetch(db,parent.run_identity)
                history=json.loads(row.checkpoint_receipts_json)
                while len(history)<51:
                    payload={'negative_capacity':len(history)}
                    history.append({'checkpoint_id':f'negative-capacity:{len(history)}','payload':payload,
                        'state_digest':_digest(payload),'safe':True,'fencing_token':row.fencing_token})
                await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity==row.run_identity).values(checkpoint_receipts_json=_canonical(history)))
                await db.commit()
        else:
            async def refuse_event(*args,**kwargs):
                raise BoardError('test_partial_event_conflict','Actual event writer conflict after original Root CAS',status_code=409)
            monkeypatch.setattr(WorkBoardRepository,'_event',refuse_event)
        async def snapshot():
            async with sessions() as db:
                return {kind:[row.model_dump(mode='json') for row in (await db.execute(select(model))).scalars()]
                    for kind,model in [('jobs',WorkflowRunState),('tasks',WorkBoardTask),('attempts',WorkBoardAttempt),
                        ('events',WorkBoardEvent),('costs',InferenceCostReservation)]}
        before=await snapshot();contacts=len(transport['contacts'])
        with pytest.raises((BoardError,DurableJobError,OSError)):
            await accept_partial_results(dispatcher.jobs,task_id=task_id,operator=operator,request=body)
        assert await snapshot()==before
        assert len(transport['contacts'])==contacts and not received and not (workspace/'parent.txt').exists()
