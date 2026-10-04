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
@pytest.mark.parametrize("authored_fixture",["time-ledger","tiny-copy"])
async def test_actual_authored_time_ledger_native_reopen_private_read(accounting_db, monkeypatch, authored_fixture):
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
    os.chmod(target/"rootfs/lib64/ld-linux-x86-64.so.2",0o700)
    # #916 profile-probe-r10 creates this empty mountpoint; its managed
    # receipt retention copied files only, omitting empty directories.
    before=(target/"rootfs/proc").exists()
    (target/"rootfs/proc").mkdir(mode=0o700)
    (root/"runtime-directory-restoration.json").write_text(json.dumps({
        "reference":"#916 profile-probe-r10.py:41; retain-managed-r1.py files-only",
        "proc_before":before,"proc_after":True,"mode":"0700","entries":[],
        "immutable_execute_restored":["bwrap","rootfs/runtime/bin/isolated-python","rootfs/lib64/ld-linux-x86-64.so.2"]}))
    package_root=root/"selected-package"
    scaffold_adapter(package_root,package_id="local.time-ledger-summary" if authored_fixture=="time-ledger" else "local.test-json-copy",display_name="Time ledger summary" if authored_fixture=="time-ledger" else "Test-only JSON copy")
    if authored_fixture=="tiny-copy":
        # Second data-only contract proves resolver/runner generality. The
        # authored bytes are never imported, compiled or executed on the host.
        from src.extensions.authored_adapter import canonical,sha256
        code=b'import json\nwith open("/input.json", encoding="utf-8") as source: value=json.load(source)\nwith open("/out/result.json","w",encoding="utf-8") as output: json.dump(value,output,sort_keys=True)\n'
        schema={"type":"object","properties":{"schema_version":{"type":"integer","minimum":1,"maximum":1,"const":1},"value":{"type":"integer","minimum":0,"maximum":9}},"required":["schema_version","value"],"additionalProperties":False}
        descriptor=json.loads((package_root/"adapters/adapter.json").read_bytes())
        descriptor.update(code_sha256=sha256(code),input_schema=schema,output_schema=schema,input_schema_sha256=sha256(canonical(schema)),output_schema_sha256=sha256(canonical(schema)))
        (package_root/"adapter.py").write_bytes(code)
        (package_root/"adapters/adapter.json").write_bytes(canonical(descriptor))
        (package_root/"evals/known-answer.json").write_bytes(canonical({"input":{"schema_version":1,"value":7},"output":{"schema_version":1,"value":7}}))
    app=FastAPI();app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router,prefix="/api/auth")
    for router in (capability_packs.router,goals.router,work_board.router):app.include_router(router,prefix="/api")
    jobs=DurableJobRepository();dispatcher=WorkBoardDispatcher(jobs=jobs,session_provider=factory.accounting_sessions)
    monkeypatch.setattr(work_board,"dispatcher",dispatcher)
    from src.work_board import tool_package_native
    actual_execute=tool_package_native.execute
    async def traced_execute(*args,**kwargs):
        try:return await actual_execute(*args,**kwargs)
        except Exception:
            import traceback
            traceback.print_exc()
            raise
    monkeypatch.setattr(tool_package_native,"execute",traced_execute)
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
        from src.extensions.capability_pack import CapabilityPackLifecycle
        mirror=CapabilityPackLifecycle().status(pack_id,owner_principal_id=principal,session_id=session.id)
        original_mirror=next(item for item in mirror["jobs"] if item["job_id"]==original[0])
        assert original_mirror["status"]=="succeeded" and original_mirror["control_authority"]=="canonical_native_job"
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
        old_review_input=await client.post("/api/work-board/input-artifacts",json={"schema_version":1,"capability_id":cap,
            "goal_id":goal["id"],"goal_revision":2,"input":{"schema_version":1,"json_text":json.dumps(vector["input"]),"no_learning":True},"idempotency_key":"current-goal-old-review"})
        assert old_review_input.status_code==409 and old_review_input.json()["detail"]["code"]=="authored_package_goal_review_stale",old_review_input.text
        fresh_review=await post(f"/api/capability-packs/{pack_id}/review",{"goal_id":goal["id"],"goal_revision":2,
            "root_path":str(package_root),"content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"],"acknowledge_unsigned_local":True})
        assert fresh_review["review"]["review_id"]!=reviewed["review"]["review_id"]
        fresh_approval=await post(f"/api/capability-packs/{pack_id}/approvals",{"action":"activate","goal_id":goal["id"],"digest":packet["content_digest"],"version":"1.0.0","content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"]})
        fresh_id=fresh_approval["approval"]["approval_id"]
        await post(f"/api/capability-packs/{pack_id}/approvals/{fresh_id}/approve",{})
        await post(f"/api/capability-packs/{pack_id}/activate",{"manifest":packet["manifest"],"root_path":str(package_root),"goal_id":goal["id"],"review_id":fresh_review["review"]["review_id"],"approval_id":fresh_id,"content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"]})
        fresh_artifact=await post("/api/work-board/input-artifacts",{"schema_version":1,"capability_id":cap,
            "goal_id":goal["id"],"goal_revision":2,"input":{"schema_version":1,"json_text":json.dumps(vector["input"]),"no_learning":True},"idempotency_key":"current-goal-new-review"})
        fresh_task=await post("/api/work-board/tasks",{"title":"Fresh reviewed current Goal","capability_id":cap,"goal_id":goal["id"],"goal_revision":2,"status":"todo","input_artifact_id":fresh_artifact["artifact_id"],"idempotency_key":"current-goal-new-task"})
        await dispatcher.run_pass()
        fresh_output=await client.get(f"/api/work-board/tasks/{fresh_task['task']['task_id']}/tool-package-output")
        assert fresh_output.status_code==200 and fresh_output.json()==vector["output"],fresh_output.text
        async with factory.accounting_sessions() as db:
            unchanged=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==original[0]))
            assert unchanged.status=="succeeded" and original==(unchanged.run_identity,unchanged.deadline_at,unchanged.run_fingerprint)
        records["old_review_current_goal_denial"]=old_review_input.json();records["fresh_current_goal_output"]=fresh_output.json()
        (root/"actual-authored-api.json").write_text(json.dumps(records,indent=2))
