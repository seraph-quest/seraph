"""Actual authenticated SQLite/pack/native/OS/output vertical; no provider calls."""
import json
import asyncio
import os
from pathlib import Path
import shutil

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db
from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState
from src.execution.tool_package_profile import PROFILE, source_package
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import DurableJobRepository


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['positive','goal_revision','logout','cancel','written_recovery','vault_drift','callback_deadline'])
async def test_actual_authenticated_review_approval_native_formatter_reopen(accounting_db,monkeypatch,mode):
    from src.api import auth,capability_packs,goals,work_board
    root,engine,factory=accounting_db
    os.chmod(root,0o700)
    monkeypatch.setattr(settings,"operator_auth_allow_unauthenticated_tests",False)
    monkeypatch.setattr(settings,"operator_auth_secret","tool-package-private-test-root")
    monkeypatch.setattr(settings,"operator_auth_secret_hash","")
    monkeypatch.setattr(settings,"operator_auth_allowed_hosts","test,localhost,127.0.0.1")
    monkeypatch.setattr(settings,"operator_auth_allowed_origins","http://localhost:3001")
    monkeypatch.setattr(settings,"operator_auth_cookie_secure",False)
    from src.api.auth import _reset_login_throttle_for_tests
    _reset_login_throttle_for_tests()
    # Explicit existing locally built optional dependency. Missing profile is
    # a skip, never proof of enforcement or native capability readiness.
    pinned=Path('/home/pawel/repos/seraph/.agent-evidence/916/profile-probe-r10.json')
    if not pinned.exists():pytest.skip('explicit pinned local profile not prepared')
    bundle=Path(json.loads(pinned.read_bytes())["fixture_root"])/"runtime"
    target=root/"artifacts/tool-package-runtime"/PROFILE
    target.parent.mkdir(parents=True,mode=0o700)
    for parent in (target.parent,target.parent.parent):os.chmod(parent,0o700)
    shutil.copytree(bundle,target)
    app=FastAPI();app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router,prefix="/api/auth")
    for router in (capability_packs.router,goals.router,work_board.router):app.include_router(router,prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",headers={"origin":"http://localhost:3001"}) as client:
        assert (await client.get('/api/capability-packs/seraph.tool.json-format/profile')).status_code==401
        login=await client.post('/api/auth/login',json={"password":"tool-package-private-test-root"})
        assert login.status_code==200,login.text
        goal=await client.post('/api/goals',json={"title":"Format operator-selected JSON",
            "admission_budget":{"reviewed_grant":True,"grant_id":"tool-format-reviewed-local",
                "max_outstanding_jobs":1,"max_attempts":1,"max_runtime_seconds":10}})
        assert goal.status_code==200,goal.text
        goal_id=goal.json()['id']
        profile=await client.get('/api/capability-packs/seraph.tool.json-format/profile')
        assert profile.status_code==200,profile.text
        packet=profile.json();assert packet['profile']['status']=='available',packet
        reviewed=await client.post('/api/capability-packs/seraph.tool.json-format/review',json={
            "goal_id":goal_id,"goal_revision":1,"content_digest":packet['content_digest'],"authority_digest":packet['authority_digest']})
        assert reviewed.status_code==200,reviewed.text
        prepared=await client.post('/api/capability-packs/seraph.tool.json-format/approvals',json={
            "action":"activate","goal_id":goal_id,"digest":packet['content_digest'],"version":"1.0.0",
            "content_digest":packet['content_digest'],"authority_digest":packet['authority_digest']})
        assert prepared.status_code==200,prepared.text
        approval=prepared.json()['approval']
        approved=await client.post('/api/capability-packs/seraph.tool.json-format/approvals/'+approval['approval_id']+'/approve')
        assert approved.status_code==200,approved.text
        activated=await client.post('/api/capability-packs/seraph.tool.json-format/activate',json={
            "manifest":packet['manifest'],"root_path":str(source_package().parent),"goal_id":goal_id,
            "review_id":reviewed.json()['review']['review_id'],"approval_id":approval['approval_id'],
            "content_digest":packet['content_digest'],"authority_digest":packet['authority_digest']})
        assert activated.status_code==200,activated.text
        artifact=await client.post('/api/work-board/input-artifacts',json={"schema_version":1,
            "capability_id":"work.json-format.v1","goal_id":goal_id,"goal_revision":1,
            "input":{"schema_version":1,"json_text":"{\"z\":2,\"a\":\"<script>literal</script>\"}","no_learning":True},
            "idempotency_key":"tool-format-input"})
        assert artifact.status_code==200,artifact.text
        created=await client.post('/api/work-board/tasks',json={"title":"Actual isolated formatter",
            "capability_id":"work.json-format.v1","goal_id":goal_id,"goal_revision":1,"status":"todo",
            "input_artifact_id":artifact.json()['artifact_id'],"idempotency_key":"tool-format-task"})
        assert created.status_code==200,created.text
        task_id=created.json()['task']['task_id']
        jobs=DurableJobRepository();dispatcher=WorkBoardDispatcher(jobs=jobs,session_provider=factory.accounting_sessions)
        monkeypatch.setattr(work_board,'dispatcher',dispatcher)
        from src.work_board import tool_package_native
        actual_execute=tool_package_native.execute
        async def traced_execute(*args,**kwargs):
            try:return await actual_execute(*args,**kwargs)
            except Exception:
                import traceback
                traceback.print_exc()
                raise
        monkeypatch.setattr(tool_package_native,'execute',traced_execute)
        if mode in {'goal_revision','logout','cancel','vault_drift','callback_deadline'}:
            from src.execution import tool_package_runner
            actual_runner=tool_package_runner.execute
            started=asyncio.Event();release=asyncio.Event()
            async def held_runner(stage,request,*,before_dispatch):
                async def held(identity):
                    await before_dispatch(identity)
                    started.set()
                    await release.wait()
                return await actual_runner(stage,request,before_dispatch=held)
            monkeypatch.setattr(tool_package_runner,'execute',held_runner)
            if mode=='vault_drift':
                from src.work_board.repository import WorkBoardRepository
                original_safe=WorkBoardRepository._safe_text
                changed_vault=False
                async def staged_safe(value,**kwargs):
                    nonlocal changed_vault
                    redacted=await original_safe(value,**kwargs)
                    if not changed_vault and value=='transient':
                        changed_vault=True
                        from src.vault.repository import vault_repository
                        await vault_repository.store('fake-race-secret',value)
                    return redacted
                monkeypatch.setattr(WorkBoardRepository,'_safe_text',staticmethod(staged_safe))
            active=asyncio.create_task(dispatcher.run_pass())
            try:
                await asyncio.wait_for(started.wait(),timeout=7)
                if mode in {'goal_revision','vault_drift'}:
                    changed=await client.patch('/api/goals/'+goal_id,json={'title':'New current Goal revision','expected_revision':1})
                    assert changed.status_code==200,changed.text
                elif mode=='logout':
                    assert (await client.post('/api/auth/logout')).status_code==204
                elif mode=='cancel':
                    detail=await client.get('/api/work-board/tasks/'+task_id)
                    cancelled=await client.post('/api/work-board/tasks/'+task_id+'/actions',json={'action':'cancel','expected_revision':detail.json()['task']['task_revision']})
                    assert cancelled.status_code==200,cancelled.text
                receipt=await asyncio.wait_for(active,timeout=12 if mode=='callback_deadline' else 5)
            finally:
                release.set()
                if not active.done():active.cancel()
                await asyncio.gather(active,return_exceptions=True)
            async with factory.accounting_sessions() as db:
                task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id))
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id))
                projection=await jobs.get_job(run.run_identity)
                assert run.status!='succeeded' and task.status.value!='done'
                assert not json.loads(run.artifact_receipts_json)
                assert tool_package_native.cleanup_proven(task,attempt,projection)
                assert run.attempt_count==1
                if mode=='vault_drift':
                    assert changed_vault
                    assert task.block_reason=='execution_blocked_redaction_state_changed'
                (root/'tool-package-authority-negative.json').write_text(json.dumps({'mode':mode,'dispatch':receipt,'native':projection,'task_status':task.status.value,'attempt_id':attempt.attempt_id,'no_learning':True},indent=2))
            await engine.dispose()
            assert (await jobs.get_job(run.run_identity))['status']!='succeeded'
            return
        if mode=='written_recovery':
            actual_adopt=tool_package_native.adopt_output
            async def crash_after_written(*args,**kwargs):
                raise RuntimeError('declared actual-output/reap-before-adoption crash boundary')
            monkeypatch.setattr(tool_package_native,'adopt_output',crash_after_written)
            receipt=await dispatcher.run_pass()
            monkeypatch.setattr(tool_package_native,'adopt_output',actual_adopt)
            async with factory.accounting_sessions() as db:
                task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id))
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id))
                original=(run.run_identity,attempt.attempt_id,run.deadline_at,run.fencing_token,run.attempt_count,run.run_fingerprint)
                # Declared worker-lease expiry fault boundary; Root, admission,
                # output, exact actual process proof and deadline stay real.
                from datetime import datetime,timedelta,timezone
                run.lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
                revision=task.task_revision
            await engine.dispose()
            state=await client.get('/api/work-board/tasks/'+task_id+'/tool-package')
            assert state.status_code==200 and state.json()['recoverable'] is True,state.text
            recovered=await client.post('/api/work-board/tasks/'+task_id+'/tool-package/recover',json={'expected_revision':revision,'idempotency_key':'original-output-recovery'})
            assert recovered.status_code==200,recovered.text
            async with factory.accounting_sessions() as db:
                task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id))
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id))
                assert original==(run.run_identity,attempt.attempt_id,run.deadline_at,run.fencing_token,run.attempt_count,run.run_fingerprint)
                assert task.status.value=='done' and run.status=='succeeded'
                artifact,raw=tool_package_native.verified_output(task,attempt,run)
                (root/'tool-package-recovery-readback.json').write_text(json.dumps({'state':state.json(),'recovery':recovered.json(),'native':await jobs.get_job(run.run_identity),'artifact':artifact,'fault_boundary':'actual output and actual reap before artifact adoption; original lease expiry','no_new_execution':True},indent=2))
            return
        receipt=await dispatcher.run_pass()
        detail=await client.get('/api/work-board/tasks/'+task_id)
        (root/'tool-package-native-readback.json').write_text(json.dumps({"dispatch":receipt,"detail":detail.json(),
            "profile":packet,"review":reviewed.json(),"activation":activated.json()},indent=2))
        assert receipt['completed']==1,detail.json()
        async with factory.accounting_sessions() as db:
            task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
            attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id))
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id))
            from src.work_board.tool_package_native import verified_output
            output,raw=verified_output(task,attempt,run)
            assert raw==b'{\n  "a": "<script>literal</script>",\n  "z": 2\n}\n'
            assert run.attempt_count==1 and json.loads(run.declared_authority_json)['no_learning'] is True
        await engine.dispose()
        assert (await jobs.get_job(run.run_identity))['status']=='succeeded'
        assert (await dispatcher.run_pass())['completed']==0
        assert (root/output['file_path']).read_bytes()==raw
        state=await client.get('/api/work-board/tasks/'+task_id+'/tool-package')
        assert state.status_code==200 and state.json()['cleanup_proven'] is True,state.text
        literal=await client.get('/api/work-board/tasks/'+task_id+'/tool-package-output')
        assert literal.status_code==200 and literal.content==raw
        assert literal.headers['content-type'].startswith('text/plain') and literal.headers['x-content-type-options']=='nosniff'
        prepared=await client.post('/api/capability-packs/seraph.tool.json-format/approvals',json={
            'action':'revoke','goal_id':goal_id,'digest':packet['content_digest'],'version':'1.0.0',
            'content_digest':packet['content_digest'],'authority_digest':packet['authority_digest']})
        assert prepared.status_code==200,prepared.text
        revoke_id=prepared.json()['approval']['approval_id']
        assert (await client.post('/api/capability-packs/seraph.tool.json-format/approvals/'+revoke_id+'/approve')).status_code==200
        revoked=await client.post('/api/capability-packs/seraph.tool.json-format/revoke',json={
            'approval_id':revoke_id,'digest':packet['content_digest'],'content_digest':packet['content_digest'],
            'authority_digest':packet['authority_digest'],'reason':'Operator revoked exact reviewed formatter'})
        assert revoked.status_code==200,revoked.text
        assert (await client.get('/api/work-board/tasks/'+task_id+'/tool-package-output')).content==raw
        from src.work_board.tool_package_native import pack_binding
        with pytest.raises(ValueError,match='exact_review_required'):pack_binding(task)
        (root/'tool-package-native-final-readback.json').write_text(json.dumps({'state':state.json(),
            'revoke':revoked.json(),'literal_sha256':output['content_sha256'],'no_learning':True},indent=2))
