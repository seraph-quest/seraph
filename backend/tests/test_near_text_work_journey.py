"""Actual auth, canonical ledger, native job and private readback; HTTP alone intercepted."""
from datetime import datetime,timedelta,timezone
import json
from uuid import uuid4,uuid5,NAMESPACE_DNS
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import Goal,WorkBoardTask,WorkBoardAttempt,WorkflowRunState,InferenceCostReservation,WorkBoardStatus,OperatorSession
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import DurableJobRepository
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_near_text_setup_api import near_payload

class ResponseBytes(httpx.AsyncByteStream):
    def __init__(self,value):self.data=json.dumps(value,separators=(',',':')).encode()
    async def __aiter__(self):yield self.data

def response(value,**kwargs):
    return httpx.Response(200,stream=ResponseBytes(value),**kwargs)

async def create_actual_near_task(client,factory,*,question='Private bounded fixture question',max_output_tokens=32,expected_policy_revision=1,goal_budget_changes=None,expected_input_error=None):
    login=await client.post('/api/auth/login',json={'password':'research-vertical-private-secret'})
    assert login.status_code==200,login.text
    owner=login.json();goal_id=uuid4().hex
    budget_values=dict(reviewed_grant=True,grant_id='near-fixture-explicit-review',max_outstanding_jobs=1,max_attempts=1,max_runtime_seconds=120)
    budget_values.update(goal_budget_changes or {})
    async with factory.accounting_sessions() as db:
        db.add(Goal(id=goal_id,title='Finite NEAR question',status='active',revision=1,
            owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],
            admission_budget_json=(json.dumps(budget_values,default=lambda value:value.isoformat()) if expected_input_error else serialize_admission_budget(GoalAdmissionBudget(**budget_values)))))
    save=await client.put('/api/settings/model-fabric',json={'expected_policy_revision':expected_policy_revision,
        'near_text':near_payload(api_key='private-intercepted-near-key')})
    assert save.status_code==200,save.text
    identifier=str(uuid4())
    for invalid in ('123e4567e89b12d3a456426614174000','123E4567-E89B-12D3-A456-426614174000','urn:uuid:123e4567-e89b-12d3-a456-426614174000',' 123e4567-e89b-12d3-a456-426614174000'):
        denied=await client.post('/api/work-board/input-artifacts',json={'schema_version':1,
            'capability_id':'inference.near-text.v1','goal_id':goal_id,'goal_revision':1,'idempotency_key':invalid,
            'input':{'schema_version':'seraph.near.text.input.v1','question':question,'max_output_tokens':max_output_tokens}})
        assert denied.status_code==422 and 'near_idempotency_invalid' in denied.text,denied.text
    artifact=await client.post('/api/work-board/input-artifacts',json={'schema_version':1,
        'capability_id':'inference.near-text.v1','goal_id':goal_id,'goal_revision':1,'idempotency_key':identifier,
        'input':{'schema_version':'seraph.near.text.input.v1','question':question,'max_output_tokens':max_output_tokens}})
    if expected_input_error:
        assert artifact.status_code==409 and artifact.json()['detail']['code']==expected_input_error,artifact.text
        return None,owner
    assert artifact.status_code==200,artifact.text
    for invalid in ('123e4567e89b12d3a456426614174000','123E4567-E89B-12D3-A456-426614174000','urn:uuid:123e4567-e89b-12d3-a456-426614174000',' 123e4567-e89b-12d3-a456-426614174000'):
        denied=await client.post('/api/work-board/tasks',json={'title':'NEAR text question','goal_id':goal_id,
            'goal_revision':1,'status':'todo','capability_id':'inference.near-text.v1',
            'input_artifact_id':artifact.json()['artifact_id'],'idempotency_key':invalid})
        assert denied.status_code==422 and 'near_idempotency_invalid' in denied.text,denied.text
    task_identifier=str(uuid4())
    denied=await client.post('/api/work-board/tasks',json={'title':'NEAR text question','goal_id':goal_id,
        'goal_revision':1,'status':'todo','requires_review':False,'capability_id':'inference.near-text.v1',
        'input_artifact_id':artifact.json()['artifact_id'],'idempotency_key':task_identifier})
    assert denied.status_code==422 and denied.json()['detail']['code']=='near_human_review_required',denied.text
    created=await client.post('/api/work-board/tasks',json={'title':'NEAR text question','goal_id':goal_id,
        'goal_revision':1,'status':'todo','capability_id':'inference.near-text.v1',
        'input_artifact_id':artifact.json()['artifact_id'],'idempotency_key':task_identifier})
    assert created.status_code==200,created.text
    assert created.json()['task']['requires_review'] is True
    return created.json()['task']['task_id'],owner

@pytest.mark.asyncio
@pytest.mark.parametrize('scenario',['settled','missing_cost','cancel_before_adoption','revoke_before_adoption','goal_expired_before_adoption','input_digest_tamper','authority_digest_tamper','fingerprint_tamper','deadline_tamper','owner_limit','short_goal'])
async def test_actual_native_https_answer_and_unknown_no_release(accounting_db,real_auth,monkeypatch,scenario,caplog):
    from src.api import auth,work_board,model_fabric_settings,goals
    root,engine,factory=accounting_db
    from src.work_board import near_text_native
    from src.model_fabric.remote_inference_admission import remote_inference_admission_broker
    monkeypatch.setattr('src.model_fabric.execution.gpu_admission_broker',remote_inference_admission_broker)
    calls=[];body_id='chatcmpl-private-fixture';provider_id=str(uuid5(NAMESPACE_DNS,body_id))
    second_task_id=None
    async def publish_second():
        nonlocal second_task_id
        second_goal=await client.post('/api/goals',json={'title':'Second finite NEAR question',
            'admission_budget':{'reviewed_grant':True,'grant_id':'second-explicit-fixture-review',
                'max_outstanding_jobs':1,'max_attempts':1,'max_runtime_seconds':120}})
        assert second_goal.status_code==200,second_goal.text
        second_goal_id=second_goal.json()['id'];second_uuid=str(uuid4())
        second_input=await client.post('/api/work-board/input-artifacts',json={'schema_version':1,
            'capability_id':'inference.near-text.v1','goal_id':second_goal_id,'goal_revision':1,'idempotency_key':second_uuid,
            'input':{'schema_version':'seraph.near.text.input.v1','question':'Private second question','max_output_tokens':32}})
        assert second_input.status_code==200,second_input.text
        second=await client.post('/api/work-board/tasks',json={'title':'NEAR text question','goal_id':second_goal_id,
            'goal_revision':1,'status':'todo','requires_review':True,'priority':100,'capability_id':'inference.near-text.v1',
            'input_artifact_id':second_input.json()['artifact_id'],'idempotency_key':second_uuid})
        assert second.status_code==200,second.text
        second_task_id=second.json()['task']['task_id']
        await WorkBoardDispatcher(jobs=DurableJobRepository(),session_provider=factory.accounting_sessions).run_pass()
        async with factory.accounting_sessions() as db:
            live_runs=list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind=='inference.near-text.v1'))).all())
            assert len(live_runs)==1 and live_runs[0].status=='running'
            second_live=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==second_task_id))
            assert second_live.block_reason=='near_owner_outstanding_limit'
            second_revision=second_live.task_revision;second_status=second_live.status.value
    async def provider(request):
        calls.append(request.url.path)
        assert request.url.host=='cloud-api.near.ai'
        assert request.headers['authorization']=='Bearer private-intercepted-near-key'
        body=json.loads(request.content)
        if request.url.path=='/v1/chat/completions':
            assert body['n']==1 and body['stream'] is False and body['max_tokens']==32
            assert body['messages']==[{'role':'user','content':'Private bounded fixture question'}]
            if scenario=='owner_limit':await publish_second()
            return response({'id':body_id,'model':'z-ai/glm-5.3-flash',
                'choices':[{'message':{'role':'assistant','content':'Private fixture answer'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':5,'completion_tokens':4,'total_tokens':9}},headers={'inference-id':provider_id})
        assert request.url.path=='/v1/billing/costs' and body=={'requestIds':[provider_id]}
        if scenario in {'cancel_before_adoption','revoke_before_adoption','goal_expired_before_adoption'}:
            async with factory.accounting_sessions() as db:
                live=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                if scenario=='cancel_before_adoption':
                    active_attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id).order_by(WorkBoardAttempt.created_at.desc()).limit(1))
                    active_attempt.cancel_requested_at=datetime.now(timezone.utc);db.add(active_attempt)
                elif scenario=='revoke_before_adoption':
                    session=await db.get(OperatorSession,owner['session_id'])
                    session.revoked_at=datetime.now(timezone.utc);db.add(session)
                else:
                    live_goal=await db.get(Goal,live.goal_id)
                    live_goal.due_date=datetime.now(timezone.utc)-timedelta(seconds=1);db.add(live_goal)
        if scenario in {'input_digest_tamper','authority_digest_tamper','fingerprint_tamper','deadline_tamper'}:
            async with factory.accounting_sessions() as db:
                native=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind=='inference.near-text.v1'))
                if scenario=='deadline_tamper':native.deadline_at=native.deadline_at+timedelta(seconds=1)
                else:setattr(native,{'input_digest_tamper':'input_digest','authority_digest_tamper':'authority_digest','fingerprint_tamper':'run_fingerprint'}[scenario],'0'*64)
                db.add(native)
        return response({'requests':[{'requestId':provider_id,'costNanoUsd':1001}],
            **({'warning':'not ready'} if scenario=='missing_cost' else {})})
    original=httpx.AsyncClient
    def clients(**kwargs):
        if 'transport' not in kwargs:kwargs['transport']=httpx.MockTransport(provider)
        return original(**kwargs)
    monkeypatch.setattr(httpx,'AsyncClient',clients)
    app=FastAPI();app.add_middleware(OperatorAuthMiddleware)
    for router,prefix in ((auth.router,'/api/auth'),(work_board.router,'/api'),(model_fabric_settings.router,'/api'),(goals.router,'/api')):
        app.include_router(router,prefix=prefix)
    async with original(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'origin':'http://localhost:3001'}) as client:
        task_id,owner=await create_actual_near_task(client,factory,goal_budget_changes=({'max_runtime_seconds':10} if scenario=='short_goal' else None))
        jobs=DurableJobRepository();dispatcher=WorkBoardDispatcher(jobs=jobs,session_provider=factory.accounting_sessions)
        results=[]
        for _ in range(4):results.append(await dispatcher.run_pass())
        async with factory.accounting_sessions() as db:
            task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
            runs=list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind=='inference.near-text.v1'))).all())
            rows=list((await db.scalars(select(InferenceCostReservation))).all())
            assert len(runs)==1,results
            assert len(rows)==1,results
            assert runs[0].attempt_count==1
            if scenario=='short_goal':
                from src.db.models import WorkBoardInputArtifact
                from src.work_board.near_text_native import utc
                original=await db.get(WorkBoardInputArtifact,task.input_artifact_id)
                sealed=json.loads(original.document_metadata_json)
                assert utc(runs[0].deadline_at)==utc(datetime.fromisoformat(sealed['deadline']))
                assert utc(runs[0].deadline_at)<=utc(original.created_at)+timedelta(seconds=10)
            if second_task_id:
                second_task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==second_task_id))
                assert second_task.status is WorkBoardStatus.blocked
                assert second_task.goal_id!=task.goal_id
                assert second_task.block_reason=='near_owner_outstanding_limit'
                assert not any('NameError' in record.getMessage() or 'AttributeError' in record.getMessage() for record in caplog.records)
            public=json.dumps([r.model_dump(mode='json') for r in runs])
            assert 'Private bounded fixture question' not in public and 'Private fixture answer' not in public
            if scenario in {'settled','owner_limit','short_goal'}:
                assert task.status is WorkBoardStatus.review,(task.model_dump(),runs[0].model_dump(),results)
                assert rows[0].state=='settled' and rows[0].actual_cost_microusd==2
                assert runs[0].status=='succeeded'
            else:
                assert task.status is WorkBoardStatus.blocked,(task.model_dump(),results)
                assert rows[0].state==('unknown' if scenario=='missing_cost' else 'settled')
                assert not json.loads(runs[0].artifact_receipts_json)
        output=await client.get(f'/api/work-board/tasks/{task_id}/near-text/output')
        if scenario in {'settled','owner_limit','short_goal'}:
            assert output.status_code==200,output.text
            assert output.json()['text']=='Private fixture answer'
            assert output.json()['receipt']['task_id']==task_id
            assert output.json()['receipt']['cost_microusd']==2
            assert output.json()['no_learning'] is True
        else:assert output.status_code in {401,403,409},output.text
        assert calls.count('/v1/chat/completions')==1,calls
        assert calls.count('/v1/billing/costs')==(2 if scenario=='missing_cost' else 1)
        await dispatcher.run_pass()
        restarted=WorkBoardDispatcher(jobs=DurableJobRepository(),session_provider=factory.accounting_sessions)
        await restarted.run_pass()
        assert calls.count('/v1/chat/completions')==1
        if scenario=='missing_cost':
            from src.work_board.repository import WorkBoardRepository,BoardError
            from src.work_board.contracts import WorkBoardOwner
            async with factory.accounting_sessions() as db:
                current=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                assert current.block_reason=='near_cost_readback_required'
                with pytest.raises(BoardError) as rejected:
                    await WorkBoardRepository().retry_task(db,WorkBoardOwner(principal_id=owner['principal_id'],session_id=owner['session_id']),
                        task_id,expected_revision=current.task_revision)
                assert rejected.value.code=='attempt_limit'
            await restarted.run_pass()
            assert calls.count('/v1/chat/completions')==1
        if scenario in {'settled','owner_limit','short_goal'}:
            reads=[]
            original_read=near_text_native.read_output
            def observed_read(*args):
                reads.append(True)
                return original_read(*args)
            monkeypatch.setattr(near_text_native,'read_output',observed_read)
            async with factory.accounting_sessions() as db:
                task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                goal=await db.get(Goal,task.goal_id)
                goal.status='paused';db.add(goal)
            denied=await client.get(f'/api/work-board/tasks/{task_id}/near-text/output')
            assert denied.status_code==409 and not reads
            async with factory.accounting_sessions() as db:
                goal=await db.get(Goal,task.goal_id);goal.status='active'
                goal.due_date=datetime.now(timezone.utc)-timedelta(seconds=1);db.add(goal)
            denied=await client.get(f'/api/work-board/tasks/{task_id}/near-text/output')
            assert denied.status_code==409 and not reads
            async with factory.accounting_sessions() as db:
                goal=await db.get(Goal,task.goal_id);goal.due_date=None;db.add(goal)
                root_session=await db.get(OperatorSession,owner['session_id'])
                original_expiry=root_session.absolute_expires_at
                root_session.absolute_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.add(root_session)
            denied=await client.get(f'/api/work-board/tasks/{task_id}/near-text/output')
            assert denied.status_code in {401,403} and not reads
            async with factory.accounting_sessions() as db:
                root_session=await db.get(OperatorSession,owner['session_id'])
                root_session.absolute_expires_at=original_expiry
                root_session.revoked_at=datetime.now(timezone.utc);db.add(root_session)
            denied=await client.get(f'/api/work-board/tasks/{task_id}/near-text/output')
            assert denied.status_code in {401,403} and not reads
            assert calls.count('/v1/chat/completions')==1

@pytest.mark.asyncio
@pytest.mark.parametrize('changes',[
    {'reviewed_grant':False},{'grant_id':None},
    {'period_started_at':datetime.now(timezone.utc)+timedelta(hours=1)},
    {'period_expires_at':datetime.now(timezone.utc)-timedelta(hours=1)},
    {'after_seal_grant':'a-different-current-reviewed-grant'},
])
async def test_actual_plaintext_consent_cannot_replace_reviewed_goal_grant(accounting_db,real_auth,monkeypatch,changes):
    from src.api import auth,work_board,model_fabric_settings,goals
    _,_,factory=accounting_db
    def forbidden(request):raise AssertionError('Goal denial must never contact a provider')
    original=httpx.AsyncClient
    def clients(**kwargs):
        if 'transport' not in kwargs:kwargs['transport']=httpx.MockTransport(forbidden)
        return original(**kwargs)
    monkeypatch.setattr(httpx,'AsyncClient',clients)
    app=FastAPI();app.add_middleware(OperatorAuthMiddleware)
    for router,prefix in ((auth.router,'/api/auth'),(work_board.router,'/api'),(model_fabric_settings.router,'/api'),(goals.router,'/api')):
        app.include_router(router,prefix=prefix)
    async with original(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'origin':'http://localhost:3001'}) as client:
        if 'after_seal_grant' in changes:
            task,_=await create_actual_near_task(client,factory)
            async with factory.accounting_sessions() as db:
                current=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task))
                goal=await db.get(Goal,current.goal_id)
                from src.goals.repository import deserialize_admission_budget
                budget=deserialize_admission_budget(goal).model_copy(update={'grant_id':changes['after_seal_grant']})
                goal.admission_budget_json=serialize_admission_budget(budget);db.add(goal)
            dispatcher=WorkBoardDispatcher(jobs=DurableJobRepository(),session_provider=factory.accounting_sessions)
            for _ in range(3):await dispatcher.run_pass()
            async with factory.accounting_sessions() as db:
                current=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task))
                assert current.status is WorkBoardStatus.blocked
        else:
            task,_=await create_actual_near_task(client,factory,goal_budget_changes=changes,expected_input_error='near_goal_grant_required')
            assert task is None
    async with factory.accounting_sessions() as db:
        assert not list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind=='inference.near-text.v1'))).all())
        assert not list((await db.scalars(select(InferenceCostReservation))).all())

@pytest.mark.asyncio
async def test_expired_contact_finance_recovers_ended_attempt_without_current_grants(accounting_db,real_auth,monkeypatch):
    import asyncio
    from src.api import auth,work_board,model_fabric_settings
    from src.work_board.near_text_native import expired_finance_binding,utc
    from src.model_fabric.remote_inference_admission import remote_inference_admission_broker
    monkeypatch.setattr('src.model_fabric.execution.gpu_admission_broker',remote_inference_admission_broker)
    _,_,factory=accounting_db
    entered=asyncio.Event();release=asyncio.Event();calls=[]
    async def provider(request):
        calls.append(request.url.path)
        assert request.url.path=='/v1/chat/completions'
        entered.set()
        await release.wait()
        raise httpx.ReadError('Original response permanently unavailable')
    original=httpx.AsyncClient
    def clients(**kwargs):
        if 'transport' not in kwargs:kwargs['transport']=httpx.MockTransport(provider)
        return original(**kwargs)
    monkeypatch.setattr(httpx,'AsyncClient',clients)
    app=FastAPI();app.add_middleware(OperatorAuthMiddleware)
    for router,prefix in ((auth.router,'/api/auth'),(work_board.router,'/api'),(model_fabric_settings.router,'/api')):
        app.include_router(router,prefix=prefix)
    async with original(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'origin':'http://localhost:3001'}) as client:
        task_id,owner=await create_actual_near_task(client,factory,goal_budget_changes={'max_runtime_seconds':3})
        jobs=DurableJobRepository()
        first=WorkBoardDispatcher(jobs=jobs,session_provider=factory.accounting_sessions)
        worker=asyncio.create_task(first.run_pass())
        try:
            await asyncio.wait_for(entered.wait(),2)
            restarted=WorkBoardDispatcher(jobs=jobs,session_provider=factory.accounting_sessions)
            # A new process has no original in-memory worker registrations.
            restarted._active_worker_tasks = {}
            await restarted.run_pass()
            async with factory.accounting_sessions() as db:
                task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id))
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind=='inference.near-text.v1'))
                row=await db.scalar(select(InferenceCostReservation))
                assert task.status is WorkBoardStatus.blocked and attempt.ended_at is not None
                assert run.status=='running' and row.state=='contact_started'
                deadline=utc(run.lease_expires_at);native_id=run.run_identity;original_revision=run.revision
                assert not await expired_finance_binding(db,task,attempt,run,observed=datetime.now(timezone.utc))
                goal=await db.get(Goal,task.goal_id);goal.status='paused';db.add(goal)
                root=await db.get(OperatorSession,owner['session_id']);root.revoked_at=datetime.now(timezone.utc);db.add(root)
            await asyncio.sleep(max(0,(deadline-datetime.now(timezone.utc)).total_seconds())+0.05)
            async with factory.accounting_sessions() as db:
                task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id))
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==native_id))
                observed=datetime.now(timezone.utc)
                assert not await expired_finance_binding(db,task.model_copy(update={'owner_principal_id':'foreign'}),attempt,run,observed=observed)
                assert not await expired_finance_binding(db,run=run.model_copy(update={'lease_expires_at':None}),task=task,attempt=attempt,observed=observed)
                assert not await expired_finance_binding(db,run=run.model_copy(update={'lease_expires_at':observed+timedelta(seconds=10)}),task=task,attempt=attempt,observed=observed)
                assert not await expired_finance_binding(db,task.model_copy(update={'typed_input_digest':'0'*64}),attempt,run,observed=observed)
                row=await db.scalar(select(InferenceCostReservation))
                ambiguous=InferenceCostReservation(**{**row.model_dump(),'operation_id':'negative-ambiguous-row'})
                db.add(ambiguous);await db.flush()
                assert not await expired_finance_binding(db,task,attempt,run,observed=observed)
                await db.rollback()
            async with factory.accounting_sessions() as db:
                attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id))
                newer=WorkBoardAttempt(**{**attempt.model_dump(),'attempt_id':uuid4().hex,
                    'created_at':datetime.now(timezone.utc),'workflow_run_id':None})
                db.add(newer);await db.flush()
                assert not await restarted.repository.list_expired_near_attempts(db,now=datetime.now(timezone.utc))
                await db.rollback()
            def no_private_execution(*args,**kwargs):
                raise AssertionError('Financial recovery must not load private input or execution grants')
            with monkeypatch.context() as isolated:
                isolated.setattr('src.work_board.dispatcher._parse_typed_input',no_private_execution)
                isolated.setattr('src.work_board.near_text_native._input',no_private_execution)
                isolated.setattr('src.model_fabric.effective_policy.current_near_text_policy',no_private_execution)
                assert await restarted.recover_expired_near_finance(now=datetime.now(timezone.utc))==[native_id]
            await restarted.run_pass()
            async with factory.accounting_sessions() as db:
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==native_id))
                row=await db.scalar(select(InferenceCostReservation))
                assert run.status=='cost_liability' and run.lease_owner is None and run.lease_expires_at is None
                assert run.fencing_token==2 and run.revision>original_revision
                assert row.state=='unknown' and row.bound_microusd==1000 and row.actual_cost_microusd is None
                assert row.recovery_reason=='provider_cost_readback_required'
                revision=row.revision;native_revision=run.revision
                assert not json.loads(run.artifact_receipts_json)
            await restarted.run_pass()
            async with factory.accounting_sessions() as db:
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==native_id))
                row=await db.scalar(select(InferenceCostReservation))
                assert row.revision==revision and run.revision==native_revision
                assert len(list((await db.scalars(select(WorkBoardAttempt))).all()))==1
            assert calls==['/v1/chat/completions']
        finally:
            release.set()
            await asyncio.wait_for(worker,5)
