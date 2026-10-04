"""Actual reviewed authored package through canonical Auth/SQLite/native/OS."""
import json
import os
from pathlib import Path
import shutil
import asyncio

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
@pytest.mark.parametrize("authored_fixture",["time-ledger","tiny-copy","two-goal-race","two-goal-lock-trace","sandbox-denials"])
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
    scaffold_adapter(package_root,package_id="local.test-json-copy" if authored_fixture=="tiny-copy" else "local.time-ledger-summary",display_name="Test-only JSON copy" if authored_fixture=="tiny-copy" else "Time ledger summary")
    if authored_fixture=="sandbox-denials":
        from src.extensions.authored_adapter import canonical,sha256
        # These bytes are reviewed data on the host and execute only through
        # the production native package runner and unchanged #916 namespace.
        secret=root/"private-sandbox-sentinel.txt"
        secret.write_text("dummy-private-value-never-mounted")
        monkeypatch.setenv("SERAPH_TEST_FORBIDDEN_SECRET","dummy-parent-only-secret")
        prefix=("import os,ctypes,json\ndenials={}\n"
            "def denied(name,operation,allowed):\n"
            " try: operation()\n"
            " except OSError as error:\n"
            "  assert error.errno in allowed,(name,error.errno)\n"
            "  denials[name]=True\n"
            "  print(json.dumps({'operation':name,'errno':error.errno}),flush=True)\n"
            " else: raise AssertionError(name+' unexpectedly permitted')\n"
            "def network_socket():\n"
            " libc=ctypes.CDLL(None,use_errno=True)\n"
            " descriptor=libc.socket(2,1,0)\n"
            " if descriptor<0:raise OSError(ctypes.get_errno(),'socket denied')\n"
            " os.close(descriptor)\n"
            "denied('network',network_socket,{1,13})\n"
            f"denied('host_secret',lambda:open({str(secret)!r},'rb'),{{2,13}})\n"
            "assert os.environ.get('SERAPH_TEST_FORBIDDEN_SECRET') is None\n"
            "denials['parent_secret_environment']=True\n"
            "denied('filesystem_escape',lambda:open('/host-escape.json','w'),{13,30})\n"
            "def extra_process():\n"
            " pid=os.fork()\n"
            " if pid==0:os._exit(77)\n"
            " os.waitpid(pid,0)\n"
            "denied('extra_process',extra_process,{1,13})\n"
            "denied('exec',lambda:os.execv('/runtime/bin/isolated-python',['isolated-python','-c','raise SystemExit(78)']),{1,13})\n"
            "denied('output_symlink',lambda:os.symlink('/input.json','/out/escape-link'),{1,13,30})\n"
            "denied('output_same_path_unlink',lambda:os.unlink('/out/result.json'),{1,13,16,30})\n").encode()
        code=prefix+(package_root/"adapter.py").read_bytes()+b'\nwith open("/out/result.json",encoding="utf-8") as source: actual=json.load(source)\nactual["sandbox_denials"]=denials\nwith open("/out/result.json","w",encoding="utf-8") as output:json.dump(actual,output,sort_keys=True)\n'
        names=("network","host_secret","parent_secret_environment","filesystem_escape","extra_process","exec","output_symlink","output_same_path_unlink")
        descriptor=json.loads((package_root/"adapters/adapter.json").read_bytes())
        schema=descriptor["output_schema"]
        schema["properties"]["sandbox_denials"]={"type":"object","properties":{name:{"type":"boolean","const":True} for name in names},"required":list(names),"additionalProperties":False}
        schema["required"].append("sandbox_denials")
        descriptor.update(code_sha256=sha256(code),output_schema_sha256=sha256(canonical(schema)))
        vector=json.loads((package_root/"evals/known-answer.json").read_bytes())
        vector["output"]["sandbox_denials"]={name:True for name in names}
        (package_root/"adapter.py").write_bytes(code)
        (package_root/"adapters/adapter.json").write_bytes(canonical(descriptor))
        (package_root/"evals/known-answer.json").write_bytes(canonical(vector))
    if authored_fixture in {"two-goal-race","two-goal-lock-trace"}:
        # A reviewed test-only barrier uses the one precreated output inode.
        # No new writable mountpoint or runner/profile option is introduced.
        from src.extensions.authored_adapter import canonical,sha256
        original_code=(package_root/"adapter.py").read_bytes()
        # Futex timed lock waits are already permitted by the unchanged
        # profile. time.sleep/clock_nanosleep is deliberately denied (r9).
        code=b'import json,_thread\nwaiter=_thread.allocate_lock();waiter.acquire()\nwith open("/input.json",encoding="utf-8") as source: barrier_input=json.load(source)\nif barrier_input["rows"]:\n with open("/out/result.json","w",encoding="utf-8") as output: json.dump({"barrier":True},output)\n while True:\n  with open("/out/result.json",encoding="utf-8") as output: released=json.load(output)\n  if released.get("barrier") is False: break\n  waiter.acquire(timeout=.01)\n'+original_code
        descriptor=json.loads((package_root/"adapters/adapter.json").read_bytes());descriptor["code_sha256"]=sha256(code)
        (package_root/"adapter.py").write_bytes(code);(package_root/"adapters/adapter.json").write_bytes(canonical(descriptor))
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
    if authored_fixture in {"two-goal-race","two-goal-lock-trace"}:
        from contextlib import contextmanager
        import time,traceback
        from src.extensions.capability_pack import CapabilityPackLifecycle
        original_lock=CapabilityPackLifecycle._state_lock
        lock_trace=[]
        @contextmanager
        def observed_lock(store,*,shared=False):
            entry={"start":time.monotonic(),"shared":shared,"caller":[item.name for item in traceback.extract_stack(limit=5)[:-1]]}
            if len(lock_trace)<2048:lock_trace.append(entry)
            try:
                with original_lock(store,shared=shared):
                    entry["acquired"]=time.monotonic()
                    yield
            except Exception as error:
                entry["error"]=type(error).__name__
                raise
            finally:entry["end"]=time.monotonic()
        monkeypatch.setattr(CapabilityPackLifecycle,"_state_lock",observed_lock)
        records["lifecycle_lock_trace"]=lock_trace
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",headers={"origin":"http://localhost:3001"}) as client:
        async def post(path,payload):
            for exact_try in range(5):
                response=await client.post(path,json=payload)
                if response.status_code!=409 or response.json().get("detail",{}).get("code")!="capability_pack_lifecycle_busy":
                    break
                records.setdefault("lifecycle_busy_requests",[]).append({"path":path,"exact_try":exact_try,"response":response.json()})
                # Only the exact short-lock conflict is retried; no admission,
                # approval, task or immutable execution window is renewed.
                await asyncio.sleep(.02)
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
        if authored_fixture in {"two-goal-race","two-goal-lock-trace"}:
            first_pass=asyncio.create_task(dispatcher.run_pass())
            barrier_path=None
            try:
                async with asyncio.timeout(4):
                    while barrier_path is None:
                        for path in (root/"artifacts/tool-package-runs").glob("*/out/result.json"):
                            try:observed=json.loads(path.read_bytes())
                            except (ValueError,FileNotFoundError):continue
                            if observed=={"barrier":True}:barrier_path=path;break
                        await asyncio.sleep(.01)
                async with factory.accounting_sessions() as db:
                    first_task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                    first_attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id))
                    first_run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==first_attempt.workflow_run_id))
                    first_identity=(first_run.run_identity,first_run.deadline_at,first_run.run_fingerprint)
                    first_history=(first_run.checkpoint_receipts_json,first_run.effect_receipts_json)
                    first_board_identity=(first_task.task_revision,first_attempt.attempt_id,first_attempt.workflow_run_id,first_attempt.fencing_token)
                    assert first_run.status=="running" and first_run.attempt_count==1
                assert await tool_package_native.live_original_owner(jobs,first_task,first_attempt)
                # Current live authority is copied to separate real SQLite
                # databases for negatives; the actual owner is never mutated.
                import sqlite3
                from contextlib import asynccontextmanager
                from sqlalchemy.ext.asyncio import create_async_engine,AsyncSession
                from sqlalchemy.orm import sessionmaker
                from datetime import datetime,timezone,timedelta
                from src.db.models import Goal
                negatives={}
                for mutation in (("lease_expired","goal_revision","cancel_intent","malformed_authority") if authored_fixture=="two-goal-race" else ()):
                    copied=root/("live-native-"+mutation+".db")
                    with sqlite3.connect("file:"+str(root/"seraph.db")+"?mode=ro",uri=True) as origin,sqlite3.connect(copied) as target_db:
                        origin.backup(target_db)
                    copy_engine=create_async_engine("sqlite+aiosqlite:///"+str(copied))
                    copy_factory=sessionmaker(copy_engine,class_=AsyncSession,expire_on_commit=False)
                    @asynccontextmanager
                    async def copy_sessions():
                        async with copy_factory() as db:
                            try:yield db;await db.commit()
                            except BaseException:await db.rollback();raise
                    copy_jobs=DurableJobRepository();copy_jobs._session=copy_sessions
                    assert await tool_package_native.live_original_owner(copy_jobs,first_task,first_attempt)
                    async with copy_sessions() as db:
                        copied_run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==first_identity[0]))
                        if mutation=="lease_expired":copied_run.lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
                        elif mutation=="goal_revision":(await db.get(Goal,goal["id"])).revision+=1
                        elif mutation=="cancel_intent":(await db.get(WorkBoardAttempt,first_attempt.attempt_id)).cancel_requested_at=datetime.now(timezone.utc)
                        else:copied_run.declared_authority_json="{}"
                    negatives[mutation]=await tool_package_native.live_original_owner(copy_jobs,first_task,first_attempt)
                    assert negatives[mutation] is False
                    await copy_engine.dispose()
                records["live_owner_copy_negatives"]={"results":negatives,"scope":"copied actual running SQLite, original owner unchanged"}
                repeated_review=await post(f"/api/capability-packs/{pack_id}/review",{"goal_id":goal["id"],"goal_revision":1,"root_path":str(package_root),"content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"],"acknowledge_unsigned_local":True})
                assert repeated_review["review"]==reviewed["review"]
                assert await tool_package_native.live_original_owner(jobs,first_task,first_attempt)
                records["identical_review_while_live"]={"original":reviewed["review"],"returned":repeated_review["review"],"current_original_owner":True}
                second_goal=await post("/api/goals",{"title":"Second independent Goal for same owner/package","admission_budget":{"reviewed_grant":True,"grant_id":"authored-second-goal","max_outstanding_jobs":1,"max_attempts":1,"max_runtime_seconds":10}})
                second_review=await post(f"/api/capability-packs/{pack_id}/review",{"goal_id":second_goal["id"],"goal_revision":1,"root_path":str(package_root),"content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"],"acknowledge_unsigned_local":True})
                second_approval=await post(f"/api/capability-packs/{pack_id}/approvals",{"action":"activate","goal_id":second_goal["id"],"digest":packet["content_digest"],"version":"1.0.0","content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"]})
                second_id=second_approval["approval"]["approval_id"]
                await post(f"/api/capability-packs/{pack_id}/approvals/{second_id}/approve",{})
                await post(f"/api/capability-packs/{pack_id}/activate",{"manifest":packet["manifest"],"root_path":str(package_root),"goal_id":second_goal["id"],"review_id":second_review["review"]["review_id"],"approval_id":second_id,"content_digest":packet["content_digest"],"authority_digest":packet["authority_digest"]})
                second_input=await post("/api/work-board/input-artifacts",{"schema_version":1,"capability_id":cap,"goal_id":second_goal["id"],"goal_revision":1,"input":{"schema_version":1,"json_text":json.dumps({"schema_version":1,"rows":[]}),"no_learning":True},"idempotency_key":"two-goal-second-input"})
                second_task=await post("/api/work-board/tasks",{"title":"Second package claim waits for real reap","capability_id":cap,"goal_id":second_goal["id"],"goal_revision":1,"status":"todo","input_artifact_id":second_input["artifact_id"],"idempotency_key":"two-goal-second-task"})
                # Two dispatcher instances in one process share actual SQLite;
                # this is not an independent-process parent-death proof.
                second_dispatcher=WorkBoardDispatcher(jobs=DurableJobRepository(),session_provider=factory.accounting_sessions)
                waiting=await second_dispatcher.run_pass()
                records["second_dispatch_waiting"]=waiting
                (root/"actual-authored-api.json").write_text(json.dumps(records,indent=2))
                async with factory.accounting_sessions() as db:
                    second_attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==second_task["task"]["task_id"]))
                    second_row=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==second_task["task"]["task_id"]))
                    assert second_attempt is None and second_row.status.value=="ready",waiting
                    assert list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.goal_id==second_goal["id"]))).all())==[]
                    still_first=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==first_identity[0]))
                    assert still_first.status=="running" and first_identity==(still_first.run_identity,still_first.deadline_at,still_first.run_fingerprint)
                    assert first_history==(still_first.checkpoint_receipts_json,still_first.effect_receipts_json)
                    still_task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                    still_attempt=await db.get(WorkBoardAttempt,first_attempt.attempt_id)
                    assert still_task.status.value=="running" and first_board_identity==(still_task.task_revision,still_attempt.attempt_id,still_attempt.workflow_run_id,still_attempt.fencing_token)
                assert await tool_package_native.live_original_owner(jobs,first_task,first_attempt)
                descriptor=os.open(barrier_path,os.O_WRONLY|os.O_NOFOLLOW)
                try:assert os.write(descriptor,b'{"barrier":false}')==17
                finally:os.close(descriptor)
                await first_pass
                completed_second=await second_dispatcher.run_pass()
                async with factory.accounting_sessions() as db:
                    second_attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==second_task["task"]["task_id"]))
                    assert second_attempt is not None,completed_second
                    second_run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==second_attempt.workflow_run_id))
                    second_identity=(second_run.run_identity,second_run.deadline_at,second_run.run_fingerprint)
                    for identity in (first_identity,second_identity):
                        row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==identity[0]))
                        assert row.status=="succeeded" and row.attempt_count==1 and identity==(row.run_identity,row.deadline_at,row.run_fingerprint),completed_second
                first_output=await client.get(f"/api/work-board/tasks/{task_id}/tool-package-output")
                second_output=await client.get(f"/api/work-board/tasks/{second_task['task']['task_id']}/tool-package-output")
                assert first_output.status_code==200 and first_output.json()==vector["output"]
                assert second_output.status_code==200 and second_output.json()=={"schema_version":1,"groups":[],"total_minutes":0}
                records["two_goal_barrier"]={"first_identity":str(first_identity),"second_identity":str(second_identity),"second_native_identity_minted":"first actual admission after prior positive reap; not queued native continuation","waiting":waiting,"completed_second":completed_second,"first_output":first_output.json(),"second_output":second_output.json(),"source_barrier_observed":True,"registry_scope":"two dispatcher instances in one process"}
                (root/"actual-authored-api.json").write_text(json.dumps(records,indent=2))
                return
            finally:
                if not first_pass.done():
                    if barrier_path is not None:
                        descriptor=os.open(barrier_path,os.O_WRONLY|os.O_NOFOLLOW)
                        try:os.write(descriptor,b'{"barrier":false}')
                        finally:os.close(descriptor)
                    first_pass.cancel()
                    try:await first_pass
                    except asyncio.CancelledError:pass
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
        if authored_fixture=="sandbox-denials":
            assert not (root/"host-escape.json").exists()
            assert secret.read_text()=="dummy-private-value-never-mounted"
            (root/"actual-authored-api.json").write_text(json.dumps(records,indent=2))
            return
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
