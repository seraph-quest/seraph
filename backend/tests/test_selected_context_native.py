"""Real Auth/SQLite/Vault/native approval/private bytes; no external service."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
import uuid
import sqlite3
import shutil
import asyncio
import threading
import copy
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from src.auth.middleware import OperatorAuthMiddleware
from src.api import auth, selected_context
from src.db.models import Goal, WorkBoardTask, WorkflowRunState, ScreenObservation, ApprovalRequest
from src.extensions.paired_edge import create_pairing
from src.extensions.state import load_extension_state_payload
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.vault import crypto
from src.workflows.selected_context_contract import ADAPTER_BUILD_DIGEST, COMPANION_ORIGIN, SelectedContextError, signature
from src.workflows import selected_context_files as files
from src.workflows import selected_context_runtime as runtime
from src.workflows.selected_context_contract import PairLocator, Metadata, digest
from src.extensions.state import held_extension_state_lock, save_extension_state_payload, ExtensionStateBusy
from src.vault.repository import vault_repository


def ident():return str(uuid.uuid4())


async def setup(accounting_db,monkeypatch):
    root,engine,factory=accounting_db
    monkeypatch.setattr(crypto,"_fernet",None)
    app=FastAPI();app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router,prefix="/api/auth");app.include_router(selected_context.router,prefix="/api")
    @app.exception_handler(SelectedContextError)
    async def failure(request,error):return JSONResponse({"detail":{"code":error.code}},status_code=error.status)
    client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",headers={"origin":"http://localhost:3001"})
    login=await client.post("/api/auth/login",json={"password":"research-vertical-private-secret"});assert login.status_code==200,login.text
    owner=login.json();now=datetime.now(timezone.utc)
    grant=GoalAdmissionBudget(reviewed_grant=True,grant_id="selected-context",max_outstanding_jobs=1,max_attempts=1,max_runtime_seconds=15,notifications_per_day=0,period_started_at=now-timedelta(seconds=1),period_expires_at=now+timedelta(minutes=5),timezone="UTC")
    async with factory.accounting_sessions() as db:
        db.add(Goal(id="selected-goal",title="Attach private selection",status="active",owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],revision=1,admission_budget_json=serialize_admission_budget(grant)))
        db.add(WorkBoardTask(task_id="selected-task",title="Inspect private documentation",body="No model analysis",owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],goal_id="selected-goal",goal_revision=1,status="ready",task_revision=1,idempotency_scope="selected-fixture",idempotency_key=ident()))
    payload=load_extension_state_payload()
    entry,credential,revision=await create_pairing(payload,extension_id="selected-fixture",reference="connectors/nodes/device.yaml",name="Selected fixture",device_id="selected-device",pairing_id=ident(),owner_principal_id=owner["principal_id"],label="Actual test pair",expected_revision=int(payload.get("revision",0)))
    locator={"extension_id":"selected-fixture","reference":"connectors/nodes/device.yaml","name":"Selected fixture","device_id":"selected-device","pairing_id":entry["pairing_id"]}
    target=await client.post("/api/context/selected-text/tasks/selected-task/target",json={"pair":locator,"expected_state_revision":revision,"expected_task_revision":1,"goal_id":"selected-goal","goal_revision":1,"acknowledge_local_selected_text":True})
    assert target.status_code==200,target.text
    text="A deliberately selected ordinary paragraph."
    metadata={"schema_version":1,"adapter_profile":"browser-selected-text-v1","adapter_version":"1","adapter_build_digest":ADAPTER_BUILD_DIGEST,"pair":locator,"target":target.json()["target"],"capture_uuid":ident(),"source":{"origin":"https://example.com","path":"/documentation","document_id":"selected-document","frame_id":0,"source_revision_digest":"b"*64,"captured_at":int(time.time()),"reviewed_origin":True,"protected_surface_checked":True},"reviewed_utf8_sha256":hashlib.sha256(text.encode()).hexdigest(),"reviewed_byte_count":len(text.encode()),"expires_at":int(time.time())+100,"request_uuid":ident(),"privacy_reviewed":True}
    device=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",headers={"origin":COMPANION_ORIGIN})
    async def signed(action,value,extra=None):
        return await device.post("/api/context/selected-text/paired/"+action,json=value,headers={"authorization":"Bearer "+credential,"x-seraph-context-mac":signature(credential,action,value),**(extra or {})})
    return root,factory,client,device,signed,metadata,text


@pytest.mark.asyncio
async def test_actual_signed_native_private_read_and_tombstone(accounting_db,real_auth,monkeypatch):
    root,factory,client,device,signed,metadata,text=await setup(accounting_db,monkeypatch)
    try:
        response=await signed("prepare",metadata);assert response.status_code==200,response.text
        job=response.json();assert job["status"]=="paused" and job["source_task_id"]=="selected-task"
        assert not list(root.glob("artifacts/context/private/selected-text/*.enc"))
        # Cookies and missing/forged MAC never borrow operator authority.
        denied=await signed("upload",{"metadata":metadata,"text":text});assert denied.status_code==403,denied.text
        denied=await signed("prepare",metadata,{"Cookie":"seraph_operator=irrelevant"});assert denied.status_code==403
        url="/api/context/selected-text/tasks/selected-task/captures/"+job["job_id"]
        receipt=(await client.get(url)).json()
        approved=await client.post(url+"/decision",json={"decision":"approved","expected_digest":receipt["approval_decision_digest"]});assert approved.status_code==200,approved.text
        changed=await signed("upload",{"metadata":metadata,"text":text+" changed"})
        assert changed.status_code==422 and changed.json()["detail"]["code"]=="selected_context_content_changed"
        result=await signed("upload",{"metadata":metadata,"text":text});assert result.status_code==200,result.text
        assert result.json()["status"]=="succeeded" and result.json()["no_learning"] is True
        private=await client.get(url+"/private");assert private.status_code==200,private.text;assert private.json()["text"]==text
        async with factory.accounting_sessions() as db:
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==job["job_id"]))
            checkpoint=json.loads(run.checkpoint_context_json);assert text not in run.checkpoint_context_json and text not in run.declared_authority_json
            assert run.selected_context_reserved_bytes==len(text.encode()) and run.source_task_id=="selected-task"
            assert not (await db.scalars(select(ScreenObservation))).all()
            ref=checkpoint["file"];cipher=(root/ref["path"]).read_bytes();assert text.encode() not in cipher
            assert (root/ref["path"]).stat().st_mode&0o777==0o600
        # Retain actual pre-discard private bytes/SQLite before runtime cleanup.
        retained=root.parent/"retained-before-discard";retained.mkdir(mode=0o700)
        shutil.copy2(root/ref["path"],retained/"capture.enc")
        with sqlite3.connect(root/"seraph.db") as source, sqlite3.connect(retained/"seraph.db") as target:
            source.backup(target)
        body={"expected_revision":private.json()["revision"],"request_uuid":ident()}
        discarded=await client.post(url+"/discard",json=body);assert discarded.status_code==200,discarded.text
        assert discarded.json()["cleanup_state"]=="verified_unavailable" and not (root/ref["path"]).exists()
        assert (await client.get(url+"/private")).status_code==403
        replay=await signed("prepare",metadata);assert replay.status_code==200,replay.text;assert replay.json()["tombstone"]
        assert (await signed("upload",{"metadata":metadata,"text":text})).status_code==410
        async with factory.accounting_sessions() as db:
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==job["job_id"]))
            assert run.selected_context_reserved_bytes==0 and run.status=="cancelled"
    finally:
        await device.aclose();await client.aclose()


@pytest.mark.asyncio
async def test_signed_ingress_and_vault_before_json_authority_race(accounting_db,real_auth,monkeypatch):
    root,factory,client,device,signed,metadata,text=await setup(accounting_db,monkeypatch)
    try:
        assert (await signed("prepare",metadata,{"Origin":"chrome-extension://wrong"})).status_code==403
        assert (await signed("prepare",metadata,{"Cookie":"irrelevant=1"})).status_code==403
        for path,value in [("schema_version",True),("schema_version",1.0),("privacy_reviewed",1)]:
            bad=copy.deepcopy(metadata);bad[path]=value
            assert (await signed("prepare",bad)).status_code==422
        bad=copy.deepcopy(metadata);bad["target"]["task_id"]="another-task"
        assert (await signed("prepare",bad)).status_code in {403,409}
        async with runtime.stage_pair(PairLocator.model_validate(metadata["pair"])) as (proof,credential):
            with pytest.raises(ExtensionStateBusy):
                save_extension_state_payload(load_extension_state_payload(),expected_revision=proof.state_revision)
            # Actual owner Vault invalidation while JSON still has the generation.
            assert await vault_repository.delete(proof.entry["credential_vault_key"],owner_principal_id=metadata["target"]["owner_principal_id"])
            with pytest.raises(SelectedContextError,match="credential_changed"):
                async with runtime.writer() as db:
                    await runtime.assert_target(db,proof)
        assert (await signed("prepare",metadata)).status_code==403
        async with factory.accounting_sessions() as db:
            assert not (await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind=="selected_context_v1"))).all()
    finally:
        await device.aclose();await client.aclose()


@pytest.mark.asyncio
async def test_selected_primary_replay_composes_with_sql_admission_callback(accounting_db,real_auth,monkeypatch):
    root,factory,client,device,signed,metadata,text=await setup(accounting_db,monkeypatch)
    real_admit=runtime.durable_job_repository.admit_job;calls=[];refuse=True
    async def combined(spec,**kwargs):
        proof=kwargs["selected_context_admission"]
        async def current_authority(db,candidate):
            assert db.in_transaction() and candidate.source_task_id=="selected-task"
            await runtime.assert_secret(db,proof.pair)
            calls.append(candidate.run_identity)
            if refuse:raise SelectedContextError("integration_callback_denied",403)
        return await real_admit(spec,**kwargs,admission_authority_check=current_authority)
    monkeypatch.setattr(runtime.durable_job_repository,"admit_job",combined)
    try:
        denied=await signed("prepare",metadata);assert denied.status_code==403,denied.text
        async with factory.accounting_sessions() as db:
            assert not (await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind=="selected_context_v1"))).all()
        refuse=False
        admitted=await signed("prepare",metadata);assert admitted.status_code==200,admitted.text
        original=admitted.json();assert original["status"]=="paused" and len(calls)==2
        async with factory.accounting_sessions() as db:
            goal=await db.get(Goal,"selected-goal");goal.revision=2;db.add(goal)
        # Exact capture lookup precedes mutable Goal fences/callback, and only
        # returns original immutable history. It cannot grant new execution.
        replay=await signed("prepare",metadata);assert replay.status_code==200,replay.text
        assert replay.json()["job_id"]==original["job_id"] and len(calls)==2
        async with factory.accounting_sessions() as db:
            rows=(await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind=="selected_context_v1"))).all()
            assert len(rows)==1 and rows[0].goal_revision==1 and rows[0].status=="paused"
    finally:
        await device.aclose();await client.aclose()


@pytest.mark.asyncio
async def test_cancelled_physical_publication_retains_quota_until_settled(accounting_db,real_auth,monkeypatch):
    root,factory,client,device,signed,metadata,text=await setup(accounting_db,monkeypatch)
    entered=threading.Event();release=threading.Event();original_publish=files.publish
    def delayed_publish(ref,ciphertext):
        entered.set()
        assert release.wait(10),"owned test publication barrier timed out"
        original_publish(ref,ciphertext)
    pending=None
    try:
        job=(await signed("prepare",metadata)).json();ident=job["job_id"]
        url="/api/context/selected-text/tasks/selected-task/captures/"+ident
        receipt=(await client.get(url)).json()
        assert (await client.post(url+"/decision",json={"decision":"approved","expected_digest":receipt["approval_decision_digest"]})).status_code==200
        monkeypatch.setattr(files,"publish",delayed_publish)
        pending=asyncio.create_task(signed("upload",{"metadata":metadata,"text":text}))
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):await pending
        receipt=(await client.get(url)).json();assert receipt["cleanup_state"]=="blocked_cleanup" and receipt["tombstone"]
        body={"expected_revision":receipt["revision"],"request_uuid":str(uuid.uuid4())}
        blocked=await client.post(url+"/discard",json=body);assert blocked.status_code==503,blocked.text
        async with factory.accounting_sessions() as db:
            row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==ident))
            assert row.status=="cancelled" and row.selected_context_reserved_bytes==len(text.encode())
            immutable=(row.deadline_at,row.declared_authority_json,row.input_digest,row.fencing_token)
        release.set()
        await asyncio.wait_for(asyncio.shield(runtime._publications[ident][1]),5)
        assert not list(root.glob("artifacts/context/private/selected-text/*.enc"))
        cleaned=await client.post(url+"/discard",json=body);assert cleaned.status_code==200,cleaned.text
        assert cleaned.json()["cleanup_state"]=="verified_unavailable"
        assert (await signed("upload",{"metadata":metadata,"text":text})).status_code==410
        async with factory.accounting_sessions() as db:
            row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==ident))
            assert row.status=="cancelled" and row.selected_context_reserved_bytes==0
            assert immutable==(row.deadline_at,row.declared_authority_json,row.input_digest,row.fencing_token)
    finally:
        release.set()
        if pending and not pending.done():
            pending.cancel()
        await device.aclose();await client.aclose()


@pytest.mark.asyncio
async def test_actual_signed_owner_root_goal_expiry_and_envelope_denials(accounting_db,real_auth,monkeypatch):
    """Named ingress negatives on one real pair; no admitted success fixture."""
    from src.workflows.selected_context_contract import MAX_ENVELOPE_BYTES
    root,factory,client,device,signed,metadata,text=await setup(accounting_db,monkeypatch)
    root.chmod(0o700)
    outcomes=[]
    async def empty_selection():
        assert not list(root.glob("artifacts/context/private/selected-text/*.enc"))
        async with factory.accounting_sessions() as db:
            assert not (await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind=="selected_context_v1"))).all()
            assert not (await db.scalars(select(ApprovalRequest))).all()
            assert not (await db.scalars(select(ScreenObservation))).all()
            task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id=="selected-task"))
            assert task.status=="ready" and task.task_revision==1 and task.input_artifact_id is None
            assert task.body=="No model analysis"
    async def denied(name,response,status,code=None):
        assert response.status_code in status,(name,response.text)
        if code is not None:assert response.json()["detail"]["code"]==code,(name,response.text)
        outcomes.append({"case":name,"status":response.status_code,"body":response.json()})
        await empty_selection()
    try:
        # A genuinely different authenticated principal cannot bind this pair
        # or inspect its owned Task. No copied operator cookie or forged row.
        async with httpx.AsyncClient(transport=client._transport,base_url="http://test",headers={"origin":"http://localhost:3001"}) as foreign:
            login=await foreign.post("/api/auth/login",json={"password":"research-vertical-private-secret","start_new_scope":True})
            assert login.status_code==200 and login.json()["principal_id"]!=metadata["target"]["owner_principal_id"]
            before=load_extension_state_payload()
            response=await foreign.post("/api/context/selected-text/tasks/selected-task/target",json={
                "pair":metadata["pair"],"expected_state_revision":before["revision"],"expected_task_revision":1,
                "goal_id":"selected-goal","goal_revision":1,"acknowledge_local_selected_text":True})
            await denied("foreign_operator_pair_owner",response,{404},"selected_context_pair_unavailable")
            assert load_extension_state_payload()==before
            response=await foreign.get("/api/context/selected-text/tasks/selected-task/captures")
            await denied("foreign_operator_task",response,{404},"selected_context_task_unavailable")
        for field,value in (("goal_id","different-goal"),("goal_revision",2),
                            ("owner_principal_id",login.json()["principal_id"])):
            bad=copy.deepcopy(metadata);bad["target"][field]=value
            await denied("signed_wrong_"+field,await signed("prepare",bad),{409},"selected_context_target_changed")
        expired=copy.deepcopy(metadata);expired["expires_at"]=int(time.time())-1
        await denied("authentic_signed_expired_backlog",await signed("prepare",expired),{410},"selected_context_ticket_expired")
        oversized=copy.deepcopy(metadata);oversized["padding"]="x"*MAX_ENVELOPE_BYTES
        await denied("oversized_envelope",await signed("prepare",oversized),{413},"selected_context_envelope_bound")
        async with runtime.stage_pair(PairLocator.model_validate(metadata["pair"])) as (_proof,credential):
            # Real paired bearer, malformed JSON rejected before MAC/authority
            # parsing; no malformed-body grant can exist.
            headers={"authorization":"Bearer "+credential,"x-seraph-context-mac":signature(credential,"prepare",metadata),"content-type":"application/json"}
        response=await device.post("/api/context/selected-text/paired/prepare",content=b'{"schema_version":',headers=headers)
        await denied("malformed_envelope",response,{422},"selected_context_schema_invalid")
        bad=copy.deepcopy(metadata);bad["reviewed_utf8_sha256"]="a"*64
        await denied("changed_reviewed_digest",await signed("upload",{"metadata":bad,"text":text}),{422},"selected_context_content_changed")
        await denied("changed_text_bytes",await signed("upload",{"metadata":metadata,"text":text+" changed"}),{422},"selected_context_content_changed")
        # A real current canonical Goal correction must also fail the writer,
        # not merely the caller-supplied target equality check above.
        async with factory.accounting_sessions() as db:
            goal=await db.get(Goal,"selected-goal");goal.revision+=1;db.add(goal)
        await denied("canonical_goal_revision_changed",await signed("prepare",metadata),{409},"selected_context_native_authority_denied")
        revoked=await client.post("/api/auth/logout")
        assert revoked.status_code==204
        await denied("original_root_revoked",await signed("prepare",metadata),{403},"selected_context_pair_authentication_denied")
        (root.parent/"named-ingress-negative-receipts.json").write_text(json.dumps({
            "boundary":"Actual Auth/SQLite/owner Vault pairing; no selected job, approval, ciphertext, Task adoption or ScreenObservation created.",
            "outcomes":outcomes},indent=2))
    finally:
        await device.aclose();await client.aclose()
