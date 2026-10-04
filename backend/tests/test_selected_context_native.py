"""Real Auth/SQLite/Vault/native approval/private bytes; no external service."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
import uuid
import sqlite3
import shutil
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
