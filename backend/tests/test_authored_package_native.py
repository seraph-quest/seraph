"""Actual reviewed authored package through canonical Auth/SQLite/native/OS."""
import json
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
from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState, OperatorSession
from src.extensions.authored_scaffold import scaffold_adapter
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import DurableJobRepository


@pytest.mark.asyncio
async def test_actual_authored_time_ledger_native_reopen_private_read(accounting_db, monkeypatch):
    from src.api import auth, capability_packs, goals, work_board
    root,engine,factory=accounting_db
    os.chmod(root,0o700)
    monkeypatch.setattr(settings,"operator_auth_allow_unauthenticated_tests",False)
    monkeypatch.setattr(settings,"operator_auth_secret","authored-private-dummy-root")
    monkeypatch.setattr(settings,"operator_auth_secret_hash","")
    monkeypatch.setattr(settings,"operator_auth_allowed_hosts","test,localhost,127.0.0.1")
    monkeypatch.setattr(settings,"operator_auth_allowed_origins","http://localhost:3001")
    monkeypatch.setattr(settings,"operator_auth_cookie_secure",False)
    monkeypatch.setattr(settings,"vault_encryption_key","")
    from src.vault import crypto
    monkeypatch.setattr(crypto,"_fernet",None)
    from src.api.auth import _reset_login_throttle_for_tests
    _reset_login_throttle_for_tests()
    bundle=Path(os.environ["SERAPH_TEST_TOOL_PACKAGE_RUNTIME"])
    target=root/"artifacts/tool-package-runtime/json-python-bwrap-v1"
    target.parent.mkdir(mode=0o700,parents=True)
    shutil.copytree(bundle,target)
    os.chmod(target/"bwrap",0o700)
    os.chmod(target/"rootfs/runtime/bin/isolated-python",0o700)
    package_root=root/"selected-package"
    scaffold_adapter(package_root,package_id="local.time-ledger-summary",display_name="Time ledger summary")
    app=FastAPI();app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router,prefix="/api/auth")
    for router in (capability_packs.router,goals.router,work_board.router):app.include_router(router,prefix="/api")
    jobs=DurableJobRepository();dispatcher=WorkBoardDispatcher(jobs=jobs,session_provider=factory.accounting_sessions)
    monkeypatch.setattr(work_board,"dispatcher",dispatcher)
    records={}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",headers={"origin":"http://localhost:3001"}) as client:
        async def post(path,payload):
            response=await client.post(path,json=payload)
            records[path]=response.json()
            (root/"actual-authored-api.json").write_text(json.dumps(records,indent=2))
            assert response.status_code==200,(response.status_code,response.text)
            return response.json()
        assert (await client.post("/api/capability-packs/authored/inspect",json={"root_path":str(package_root)})).status_code==401
        await post("/api/auth/login",{"password":"authored-private-dummy-root"})
        async with factory.accounting_sessions() as db:
            session=await db.scalar(select(OperatorSession).where(OperatorSession.revoked_at.is_(None)))
            principal=session.principal_id
        from src.vault.repository import VaultRepository
        vault=VaultRepository()
        await vault.store("authored-isolation-sentinel","private-dummy-never-authorized",owner_principal_id=principal)
        assert (await vault.snapshot("authored-isolation-sentinel",owner_principal_id=principal)).value=="private-dummy-never-authorized"
        goal=await post("/api/goals",{"title":"Summarize reviewed local time ledger","admission_budget":{
            "reviewed_grant":True,"grant_id":"authored-reviewed-local","max_outstanding_jobs":1,"max_attempts":1,"max_runtime_seconds":10}})
        packet=await post("/api/capability-packs/authored/inspect",{"root_path":str(package_root)})
        assert packet["profile"]["status"]=="available",packet
        pack_id=packet["pack_id"]
        reviewed=await post(f"/api/capability-packs/{pack_id}/review",{"goal_id":goal["id"],"goal_revision":1,
            "root_path":str(package_root),"content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"],
            "acknowledge_unsigned_local":True})
        approval=await post(f"/api/capability-packs/{pack_id}/approvals",{"action":"activate","goal_id":goal["id"],
            "digest":packet["content_digest"],"version":"1.0.0","content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"]})
        approval_id=approval["approval"]["approval_id"]
        await post(f"/api/capability-packs/{pack_id}/approvals/{approval_id}/approve",{})
        await post(f"/api/capability-packs/{pack_id}/activate",{"manifest":packet["manifest"],"root_path":str(package_root),
            "goal_id":goal["id"],"review_id":reviewed["review"]["review_id"],"approval_id":approval_id,
            "content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"]})
        vector=json.loads((package_root/"evals/known-answer.json").read_bytes())
        cap=packet["descriptor"]["capability_id"]
        artifact=await post("/api/work-board/input-artifacts",{"schema_version":1,"capability_id":cap,
            "goal_id":goal["id"],"goal_revision":1,"input":{"schema_version":1,"json_text":json.dumps(vector["input"]),"no_learning":True},
            "idempotency_key":"authored-input"})
        created=await post("/api/work-board/tasks",{"title":"Actual authored ledger summary","capability_id":cap,
            "goal_id":goal["id"],"goal_revision":1,"status":"todo","input_artifact_id":artifact["artifact_id"],"idempotency_key":"authored-task"})
        task_id=created["task"]["task_id"]
        dispatch=await dispatcher.run_pass()
        records["dispatch"]=dispatch
        detail=await client.get(f"/api/work-board/tasks/{task_id}")
        records["detail"]=detail.json()
        (root/"actual-authored-api.json").write_text(json.dumps(records,indent=2))
        async with factory.accounting_sessions() as db:
            task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
            attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id))
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id)) if attempt else None
            assert run is not None and run.status=="succeeded" and task.status.value=="done",records
            assert run.job_kind=="local_authored_json" and run.attempt_count==1
            original=(run.run_identity,run.deadline_at,run.run_fingerprint)
        await engine.dispose()
        state=await client.get(f"/api/work-board/tasks/{task_id}/tool-package")
        assert state.status_code==200 and state.json()["cleanup_proven"] and state.json()["report_available"],state.text
        output=await client.get(f"/api/work-board/tasks/{task_id}/tool-package-output")
        assert output.status_code==200 and output.json()==vector["output"],output.text
        records["state"]=state.json();records["output"]=output.json()
        assert (await dispatcher.run_pass())["completed"]==0
        changed=await client.patch(f"/api/goals/{goal['id']}",json={"title":"Corrected current Goal","expected_revision":1})
        assert changed.status_code==200,changed.text
        denied=await client.get(f"/api/work-board/tasks/{task_id}/tool-package-output")
        denied_state=await client.get(f"/api/work-board/tasks/{task_id}/tool-package")
        assert denied.status_code==409 and not denied_state.json()["report_available"]
        assert output.content not in denied.content
        async with factory.accounting_sessions() as db:
            retained=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==original[0]))
            assert retained.status=="succeeded" and original==(retained.run_identity,retained.deadline_at,retained.run_fingerprint)
        records["denied"]=denied.json();records["denied_state"]=denied_state.json()
        (root/"actual-authored-api.json").write_text(json.dumps(records,indent=2))
