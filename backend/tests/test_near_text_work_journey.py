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

async def create_actual_near_task(client,factory,*,question='Private bounded fixture question',max_output_tokens=32,expected_policy_revision=1):
    login=await client.post('/api/auth/login',json={'password':'research-vertical-private-secret'})
    assert login.status_code==200,login.text
    owner=login.json();goal_id=uuid4().hex
    async with factory.accounting_sessions() as db:
        db.add(Goal(id=goal_id,title='Finite NEAR question',status='active',revision=1,
            owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],
            admission_budget_json=serialize_admission_budget(GoalAdmissionBudget(reviewed_grant=True,
                grant_id='near-fixture-explicit-review',max_outstanding_jobs=1,max_attempts=1,max_runtime_seconds=120))))
    save=await client.put('/api/settings/model-fabric',json={'expected_policy_revision':expected_policy_revision,
        'near_text':near_payload(api_key='private-intercepted-near-key')})
    assert save.status_code==200,save.text
    identifier=str(uuid4())
    artifact=await client.post('/api/work-board/input-artifacts',json={'schema_version':1,
        'capability_id':'inference.near-text.v1','goal_id':goal_id,'goal_revision':1,'idempotency_key':identifier,
        'input':{'schema_version':'seraph.near.text.input.v1','question':question,'max_output_tokens':max_output_tokens}})
    assert artifact.status_code==200,artifact.text
    created=await client.post('/api/work-board/tasks',json={'title':'NEAR text question','goal_id':goal_id,
        'goal_revision':1,'status':'todo','requires_review':True,'capability_id':'inference.near-text.v1',
        'input_artifact_id':artifact.json()['artifact_id'],'idempotency_key':identifier})
    assert created.status_code==200,created.text
    return created.json()['task']['task_id'],owner

@pytest.mark.asyncio
@pytest.mark.parametrize('scenario',['settled','missing_cost','cancel_before_adoption','revoke_before_adoption','goal_expired_before_adoption'])
async def test_actual_native_https_answer_and_unknown_no_release(accounting_db,real_auth,monkeypatch,scenario):
    from src.api import auth,work_board,model_fabric_settings,goals
    root,engine,factory=accounting_db
    from src.work_board import near_text_native
    original_execute=near_text_native.execute
    async def observed_execute(*args,**kwargs):
        try:return await original_execute(*args,**kwargs)
        except Exception:
            import traceback
            traceback.print_exc()
            raise
    monkeypatch.setattr(near_text_native,'execute',observed_execute)
    from src.model_fabric.remote_inference_admission import remote_inference_admission_broker
    monkeypatch.setattr('src.model_fabric.execution.gpu_admission_broker',remote_inference_admission_broker)
    calls=[];body_id='chatcmpl-private-fixture';provider_id=str(uuid5(NAMESPACE_DNS,body_id))
    async def provider(request):
        calls.append(request.url.path)
        assert request.url.host=='cloud-api.near.ai'
        assert request.headers['authorization']=='Bearer private-intercepted-near-key'
        body=json.loads(request.content)
        if request.url.path=='/v1/chat/completions':
            assert body['n']==1 and body['stream'] is False and body['max_tokens']==32
            assert body['messages']==[{'role':'user','content':'Private bounded fixture question'}]
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
        task_id,owner=await create_actual_near_task(client,factory)
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
            public=json.dumps([r.model_dump(mode='json') for r in runs])
            assert 'Private bounded fixture question' not in public and 'Private fixture answer' not in public
            if scenario=='settled':
                assert task.status is WorkBoardStatus.review,(task.model_dump(),runs[0].model_dump(),results)
                assert rows[0].state=='settled' and rows[0].actual_cost_microusd==2
                assert runs[0].status=='succeeded'
            else:
                assert task.status is WorkBoardStatus.blocked,(task.model_dump(),results)
                assert rows[0].state==('unknown' if scenario=='missing_cost' else 'settled')
                assert not json.loads(runs[0].artifact_receipts_json)
        output=await client.get(f'/api/work-board/tasks/{task_id}/near-text/output')
        if scenario=='settled':
            assert output.status_code==200,output.text
            assert output.json()['text']=='Private fixture answer'
            assert output.json()['receipt']['task_id']==task_id
            assert output.json()['receipt']['cost_microusd']==2
            assert output.json()['no_learning'] is True
        else:assert output.status_code in {401,403,409},output.text
        assert calls.count('/v1/chat/completions')==1,calls
        assert calls.count('/v1/billing/costs')==(2 if scenario=='missing_cost' else 1)
        await dispatcher.run_pass()
        assert calls.count('/v1/chat/completions')==1
        if scenario=='settled':
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
