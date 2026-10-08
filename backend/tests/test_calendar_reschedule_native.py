"""Actual auth/API/SQLite/Vault/task/approval/jobs; only Google HTTP simulated."""
from copy import deepcopy
import asyncio
from datetime import datetime,timedelta,timezone
import json
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_calendar_reschedule_contract import ACCOUNT,CALENDAR,source_event,timed
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import Goal,WorkflowRunState,CalendarRescheduleConsent,WorkBoardTask,GoogleServiceConnection,ApprovalRequest
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.integrations.calendar_reschedule_contract import READ_SERVICE,SEND_SERVICE,SCOPES
from src.integrations import calendar_reschedule_runtime as runtime
from src.vault import crypto


def ident(): return str(uuid.uuid4())


class Google:
    def __init__(self):
        self.calls=[]; self.event=source_event(); self.event["summary"]="Exact owned event"
        self.patch=None; self.conflict=False; self.lose_response=False; self.patch_entered=None; self.patch_release=None

    async def handle(self,request):
        self.calls.append({"method":request.method,"host":request.url.host,"path":request.url.path,
            "query":dict(request.url.params),"profile":"write" if SEND_SERVICE in request.headers.get("authorization","") else "read"})
        if request.url.host=="oauth2.googleapis.com":
            role=next((role for role in SCOPES if role.encode() in request.content),None)
            return httpx.Response(200,json={"access_token":"synthetic-"+(role or "legacy"),"scope":" ".join(sorted(SCOPES[role])) if role else "https://www.googleapis.com/auth/calendar.readonly"})
        if request.url.host=="openidconnect.googleapis.com": return httpx.Response(200,json={**ACCOUNT,"email_verified":True})
        assert request.url.host=="www.googleapis.com",request.url
        if request.url.path.endswith("/users/me/calendarList"): return httpx.Response(200,json={"items":[{**CALENDAR,"summary":"Owned calendar"}]})
        if "/users/me/calendarList/" in request.url.path: return httpx.Response(200,json=CALENDAR)
        if request.method=="PATCH":
            assert self.patch is None,"a second PATCH must never occur"
            assert request.headers["if-match"]=='"old"'
            assert dict(request.url.params)=={"sendUpdates":"none","conferenceDataVersion":"0","supportsAttachments":"false"}
            self.patch=json.loads(request.content)
            assert set(self.patch)=={"start","end","extendedProperties"}
            assert len(self.patch["extendedProperties"]["private"])==1
            if self.conflict:
                self.event["description"]="Protected concurrent edit retained"
                self.event["etag"]='"concurrent"'
                return httpx.Response(412,json={"error":{"code":412}})
            self.event.update(start=self.patch["start"],end=self.patch["end"],etag='"new"',sequence=2)
            self.event["extendedProperties"]["private"].update(self.patch["extendedProperties"]["private"])
            if self.patch_entered is not None:
                self.patch_entered.set()
                await asyncio.wait_for(self.patch_release.wait(),timeout=10)
            if self.lose_response: raise httpx.ReadError("simulated accepted conditional PATCH response lost")
            return httpx.Response(200,json=deepcopy(self.event))
        assert request.method=="GET"
        if request.url.path.endswith("/events"): return httpx.Response(200,json={"items":[deepcopy(self.event)]})
        assert request.url.path.endswith("/events/event123"),request.url
        return httpx.Response(200,json=deepcopy(self.event))


async def prepared(accounting_db,monkeypatch):
    from src.api import auth,calendar
    from src.integrations import google_calendar,calendar_controls
    root,engine,factory=accounting_db
    monkeypatch.setattr(crypto,"_fernet",None)
    google=Google(); transport=httpx.MockTransport(google.handle)
    readonly=google_calendar.GoogleCalendarReadonlyAdapter
    boundary=lambda *args,**kwargs:readonly(*args,transport=transport,resolver=lambda host,port:["93.184.216.34"],**kwargs)
    monkeypatch.setattr(google_calendar,"GoogleCalendarReadonlyAdapter",boundary)
    monkeypatch.setattr(calendar,"GoogleCalendarReadonlyAdapter",boundary)
    adapter=runtime.CalendarRescheduleAdapter
    monkeypatch.setattr(runtime,"CalendarRescheduleAdapter",lambda *args,**kwargs:adapter(*args,transport=transport,resolver=lambda host,port:["93.184.216.34"],**kwargs))
    app=FastAPI(); app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router,prefix="/api/auth"); app.include_router(calendar.router,prefix="/api")
    client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",headers={"origin":"http://localhost:3001"})
    login=await client.post("/api/auth/login",json={"password":"research-vertical-private-secret"})
    assert login.status_code==200,login.text
    owner=login.json(); current=datetime.now(timezone.utc)
    grant=GoalAdmissionBudget(reviewed_grant=True,grant_id="calendar-native-grant",max_outstanding_jobs=1,max_attempts=1,max_runtime_seconds=300,
        notifications_per_day=0,period_started_at=current-timedelta(seconds=1),period_expires_at=current+timedelta(hours=1),timezone="UTC")
    async with factory.accounting_sessions() as db:
        db.add(Goal(id="calendar-native-goal",title="Exact Calendar operation",status="active",owner_principal_id=owner["principal_id"],
            owner_session_id=owner["session_id"],revision=1,admission_budget_json=serialize_admission_budget(grant)))
    async def post(path,body):
        response=await client.post("/api/"+path,json=body)
        assert response.status_code in {200,201},(path,response.status_code,response.text)
        return response.json()
    legacy=(await post("calendar/connections",{"schema_version":1,"service":"calendar_readonly","label":"Selection only","client_id":"synthetic-client","refresh_token":"legacy-selection","idempotency_key":ident()}))["connection"]
    verified=await post("calendar/connections/"+legacy["connection_id"]+"/verify",{"expected_revision":legacy["revision"],"idempotency_key":ident()})
    legacy=verified["connection"]
    read_grant=(await post("calendar/read-consents",{"schema_version":1,"connection_id":legacy["connection_id"],"calendar_id":CALENDAR["id"],"goal_id":"calendar-native-goal","goal_revision":1,
        "allowed_fields":["summary","start","end"],"window_minutes":1440,"max_events":1,"allow_remote_model":False,"expires_at":(current+timedelta(minutes=5)).isoformat(),"idempotency_key":ident()}))["consent"]
    listed=await client.get("/api/calendar/connections/"+legacy["connection_id"]+"/events",params={"consent_id":read_grant["consent_id"],"max_events":1})
    assert listed.status_code==200,listed.text
    selected=listed.json()["events"][0]
    base="capabilities/calendar/reschedule/"
    profiles={}
    for role in SCOPES:
        profiles[role]=(await post(base+"profiles",{"service":role,"label":role,"client_id":"synthetic-client","refresh_token":role,
            "declared_scopes":sorted(SCOPES[role]),"acknowledge_separate_identity_profile":True,"idempotency_key":ident()}))["profile"]
    # The legacy selector must never relabel an owned write profile as
    # readonly; its endpoint and the new two-profile endpoint stay disjoint.
    before=len(google.calls)
    selector=await client.get("/api/calendar/connections")
    assert selector.status_code==200 and [row["connection_id"] for row in selector.json()["connections"]]==[legacy["connection_id"]],selector.text
    dedicated=await client.get("/api/"+base+"profiles")
    assert dedicated.status_code==200 and {row["service"] for row in dedicated.json()["profiles"]}==set(SCOPES),dedicated.text
    assert len(google.calls)==before
    pair={"read_connection_id":profiles[READ_SERVICE]["connection_id"],"expected_read_revision":profiles[READ_SERVICE]["revision"],
        "write_connection_id":profiles[SEND_SERVICE]["connection_id"],"expected_write_revision":profiles[SEND_SERVICE]["revision"],
        "event_binding_id":selected["event_binding_id"],"expected_event_binding_revision":selected["event_binding_revision"],
        "goal_id":"calendar-native-goal","goal_revision":1,"acknowledge_identity_and_selected_calendar_read":True,"request_uuid":ident()}
    before=len(google.calls); verified=await post(base+"profiles/verify-pair",pair)
    assert verified["status"]=="succeeded" and verified["contacts_spent"]==6 and len(google.calls)-before==6,verified
    permission={**pair,"request_uuid":ident(),"expires_at":(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
        "acknowledge_owned_event_read":True,"acknowledge_calendar_list_metadata_read":True,"acknowledge_one_conditional_reschedule":True}
    consent=(await post(base+"consents",permission))["consent"]
    task=(await post(base+"tasks",{"request_uuid":ident(),"input":{"schema_version":1,"consent_id":consent["consent_id"],"expected_consent_revision":consent["revision"],
        "event_binding_id":selected["event_binding_id"],"expected_event_binding_revision":selected["event_binding_revision"],"goal_id":"calendar-native-goal","goal_revision":1,
        "new_start":timed(timedelta(days=2)),"new_end":timed(timedelta(days=2,hours=1))}}))["task"]
    proposal={"task_id":task["task_id"],"expected_task_revision":task["task_revision"],"read_connection_id":pair["read_connection_id"],"expected_read_revision":pair["expected_read_revision"],
        "write_connection_id":pair["write_connection_id"],"expected_write_revision":pair["expected_write_revision"],"acknowledge_fresh_preview_read":True,"request_uuid":ident()}
    before=len(google.calls); preview=await post(base+"operations/preview",proposal)
    assert preview["status"]=="paused" and preview["contacts_spent"]==4 and len(google.calls)-before==4,preview
    assert preview["preview"]["calendar_id"]==CALENDAR["id"] and preview["preview"]["old_start"]==google.event["start"]
    approval=await post(base+"operations/"+preview["job_id"]+"/decision",{"decision":"approved","expected_digest":preview["preview"]["decision_digest"]})
    assert approval["preview"]["approval_status"]=="approved",approval
    return client,post,google,preview,proposal,consent,permission,owner


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["success","conflict","lost_response"])
async def test_actual_native_conditional_write_readback_and_readonly_original_liability(accounting_db,real_auth,monkeypatch,mode):
    root,engine,factory=accounting_db
    client,post,google,preview,proposal,consent,permission,owner=await prepared(accounting_db,monkeypatch)
    base="capabilities/calendar/reschedule/operations/"; ident_original=preview["job_id"]
    google.conflict=mode=="conflict"; google.lose_response=mode=="lost_response"
    try:
        before=len(google.calls)
        response=await client.post("/api/"+base+ident_original+"/execute",json={})
        if mode=="success":
            assert response.status_code==200,response.text
            actual=response.json(); assert actual["status"]=="succeeded" and actual["outcome"]=="verified_reschedule" and actual["contacts_spent"]==13,actual
            assert len(google.calls)-before==9
            assert google.event["extendedProperties"]["private"]["retained"]=="exact" and google.event["extendedProperties"]["shared"]=={"shared":"also exact"}
        elif mode=="conflict":
            assert response.status_code==200,response.text
            actual=response.json(); assert actual["status"]=="blocked" and actual["outcome"]=="precondition_conflict" and actual["contacts_spent"]==12,actual
            assert google.event["description"]=="Protected concurrent edit retained" and google.event["start"]==preview["preview"]["old_start"]
        else:
            assert response.status_code==409,response.text
            actual=(await client.get("/api/"+base+ident_original)).json()
            assert actual["status"]=="unknown_external_effect" and actual["transport_quiescent"] is True,actual
            async with factory.accounting_sessions() as db:
                original=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==ident_original))
                preserved={name:getattr(original,name) for name in ("status","checkpoint_context_json","deadline_at","goal_id","goal_revision","authority_digest","fencing_token","lease_owner","lease_expires_at","budget_digest")}
                old_goal=await db.get(Goal,"calendar-native-goal"); old_goal.status="completed"; old_goal.revision+=1
                current=datetime.now(timezone.utc)
                grant=GoalAdmissionBudget(reviewed_grant=True,grant_id="readonly-observation",max_outstanding_jobs=1,max_attempts=1,max_runtime_seconds=120,notifications_per_day=0,
                    period_started_at=current-timedelta(seconds=1),period_expires_at=current+timedelta(minutes=5),timezone="UTC")
                db.add(Goal(id="calendar-recovery-goal",title="Read original event only",status="active",owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],revision=1,admission_budget_json=serialize_admission_budget(grant)))
            before_observe=len(google.calls)
            observed=await post(base+ident_original+"/observe",{"expected_original_revision":actual["revision"],"read_connection_id":permission["read_connection_id"],
                "expected_read_revision":permission["expected_read_revision"],"goal_id":"calendar-recovery-goal","goal_revision":1,"acknowledge_readonly_recovery":True,"request_uuid":ident()})
            assert observed["status"]=="succeeded" and observed["outcome"]=="verified_reschedule_observation" and observed["contacts_spent"]==4,observed
            assert len(google.calls)-before_observe==4 and not any(call["method"]=="PATCH" or call["profile"]=="write" for call in google.calls[before_observe:])
            async with factory.accounting_sessions() as db:
                original=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==ident_original))
                assert {name:getattr(original,name) for name in preserved}==preserved
                effects=json.loads(original.effect_receipts_json)
                assert effects[0]["status"]=="unknown" and effects[0]["observation_history"][0]["outcome"]=="verified_reschedule_observation"
        count=len(google.calls)
        await post(base+ident_original+"/execute",{})
        assert len(google.calls)==count and sum(call["method"]=="PATCH" for call in google.calls)==1
        await engine.dispose()
        read=await client.get("/api/"+base+ident_original)
        assert read.status_code==200 and read.json()["status"]==actual["status"]
        assert read.json()["model_used"] is False and read.json()["no_learning"] is True and "preview" not in read.json()
        (root/("calendar-native-"+mode+"-receipt.json")).write_text(json.dumps({"result":actual,"reopened":read.json(),"wire":google.patch,"contacts":google.calls},indent=2))
    finally: await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed",["consent_revoked","goal_revision","profile_revoked","root_logout"])
async def test_current_authority_denies_contact_and_private_bytes(accounting_db,real_auth,monkeypatch,changed):
    root,engine,factory=accounting_db
    client,post,google,preview,proposal,grant,permission,owner=await prepared(accounting_db,monkeypatch)
    base="capabilities/calendar/reschedule/"; ident_original=preview["job_id"]
    before=len(google.calls)
    try:
        if changed=="consent_revoked":
            await post(base+"consents/"+grant["consent_id"]+"/revoke",{"expected_revision":grant["revision"],"idempotency_key":ident()})
        elif changed=="profile_revoked":
            await post(base+"profiles/"+permission["write_connection_id"]+"/revoke",{"expected_revision":permission["expected_write_revision"],"idempotency_key":ident()})
        elif changed=="goal_revision":
            async with factory.accounting_sessions() as db:
                goal=await db.get(Goal,"calendar-native-goal"); goal.revision+=1
        else:
            response=await client.post("/api/auth/logout",json={}); assert response.status_code==204,response.text
        result=await client.get("/api/"+base+"operations/"+ident_original+"/private")
        if changed=="root_logout": assert result.status_code==401,result.text
        else:
            assert result.status_code==200,result.text
            assert result.json()["private_read_available"] is False and "preview" not in result.json(),result.json()
        denied=await client.post("/api/"+base+"operations/"+ident_original+"/execute",json={})
        assert denied.status_code in {401,409},denied.text
        assert len(google.calls)==before and google.patch is None
        async with factory.accounting_sessions() as db:
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==ident_original))
            assert run.status=="paused" and "intent" not in json.loads(run.checkpoint_context_json)
            approval=await db.get(ApprovalRequest,preview["preview"]["approval_id"])
            assert approval.status=="approved"
        target=root/("calendar-denied-"+changed+"-receipt.json")
        target.write_text(json.dumps({"private":result.json(),"execute":denied.json(),"contacts_before":before,"contacts_after":len(google.calls),"patch":google.patch},indent=2))
    finally: await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_execute_owns_one_actual_patch_and_cancel_waits_for_close(accounting_db,real_auth,monkeypatch):
    root,engine,factory=accounting_db
    client,post,google,preview,proposal,grant,permission,owner=await prepared(accounting_db,monkeypatch)
    google.patch_entered=asyncio.Event(); google.patch_release=asyncio.Event()
    base="/api/capabilities/calendar/reschedule/operations/"+preview["job_id"]
    first=asyncio.create_task(client.post(base+"/execute",json={}))
    try:
        await asyncio.wait_for(google.patch_entered.wait(),timeout=10)
        replay=await client.post(base+"/execute",json={}); assert replay.status_code==200,replay.text
        assert replay.json()["status"]=="running" and replay.json()["transport_quiescent"] is False
        cancel=await client.post(base+"/cancel",json={"expected_revision":replay.json()["revision"],"request_uuid":ident()})
        assert cancel.status_code==200,cancel.text
        actual=cancel.json()
        assert actual["status"]=="unknown_external_effect" and actual["contact_may_have_occurred"] is True and actual["transport_quiescent"] is True,actual
        await first
        assert sum(call["method"]=="PATCH" for call in google.calls)==1
        calls=len(google.calls); final=await client.post(base+"/execute",json={})
        assert final.status_code==200 and final.json()["status"]=="unknown_external_effect" and len(google.calls)==calls
        target=root/"calendar-concurrent-cancel-receipt.json"
        target.write_text(json.dumps({"replay":replay.json(),"cancel":actual,"final":final.json(),"contacts":google.calls},indent=2))
    finally:
        google.patch_release.set()
        if not first.done(): first.cancel()
        await asyncio.gather(first,return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure",["none","exception","no_op"])
async def test_revoke_owner_vault_cleanup_and_original_unknown_history(accounting_db,real_auth,monkeypatch,failure):
    root,engine,factory=accounting_db
    client,post,google,preview,proposal,grant,permission,owner=await prepared(accounting_db,monkeypatch)
    from src.vault import vault_repository
    from src.api import calendar_reschedule as controls
    original_id=preview["job_id"]; base="/api/capabilities/calendar/reschedule/"
    try:
        google.lose_response=True
        lost=await client.post(base+"operations/"+original_id+"/execute",json={});assert lost.status_code==409,lost.text
        async def original_row():
            async with factory.accounting_sessions() as db:
                row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==original_id))
                return {column.name:getattr(row,column.name) for column in WorkflowRunState.__table__.columns}
        before=await original_row();assert before["status"]=="unknown_external_effect"
        async with factory.accounting_sessions() as db:
            connection=await db.get(GoogleServiceConnection,permission["write_connection_id"])
            key=connection.vault_secret_key;setup_uuid=connection.setup_idempotency_key
        assert await vault_repository.snapshot(key,owner_principal_id=owner["principal_id"]) is not None
        assert await vault_repository.snapshot(key,owner_principal_id="different-owner") is None
        in_writer=ContextVar("calendar_cleanup_native_writer",default=False);real_writer=runtime.writer
        @asynccontextmanager
        async def tracked_writer():
            async with real_writer() as db:
                token=in_writer.set(True)
                try:yield db
                finally:in_writer.reset(token)
        monkeypatch.setattr(runtime,"writer",tracked_writer)
        real_delete=vault_repository.delete;real_snapshot=vault_repository.snapshot;calls=[];fail_cleanup=failure!="none"
        async def deletion(target,*,owner_principal_id):
            assert not in_writer.get();assert target==key and owner_principal_id==owner["principal_id"]
            calls.append("delete")
            # Local authority must already be unusable before Vault I/O starts.
            async with factory.accounting_sessions() as db:
                row=await db.get(GoogleServiceConnection,permission["write_connection_id"])
                assert row.state in {"blocked_cleanup","revoked"}
            if fail_cleanup:
                if failure=="exception":raise OSError("forced local credential cleanup failure")
                return False
            return await real_delete(target,owner_principal_id=owner_principal_id)
        async def snapshot(target,*,owner_principal_id):
            assert not in_writer.get();return await real_snapshot(target,owner_principal_id=owner_principal_id)
        monkeypatch.setattr(vault_repository,"delete",deletion);monkeypatch.setattr(vault_repository,"snapshot",snapshot)
        initial={"expected_revision":permission["expected_write_revision"],"idempotency_key":ident()}
        before_contacts=len(google.calls)
        first=await client.post(base+"profiles/"+permission["write_connection_id"]+"/revoke",json=initial)
        if failure=="none":
            assert first.status_code==200 and first.json()["credential_cleanup"]=="verified_unavailable",first.text
        else:
            assert first.status_code==503 and first.json()["detail"]["credential_cleanup"]=="blocked_cleanup",first.text
            rows=(await client.get(base+"profiles")).json()["profiles"];blocked=next(row for row in rows if row["connection_id"]==permission["write_connection_id"])
            assert blocked["state"]=="blocked_cleanup" and blocked["cleanup_retry"]["idempotency_key"]==initial["idempotency_key"]
            assert await real_snapshot(key,owner_principal_id=owner["principal_id"]) is not None
            bad={**blocked["cleanup_retry"],"idempotency_key":ident()}
            conflict=await client.post(base+"profiles/"+permission["write_connection_id"]+"/revoke",json=bad)
            assert conflict.status_code==409 and len(calls)==1,conflict.text
            private=await client.get(base+"operations/"+original_id+"/private")
            assert private.status_code==200 and private.json()["private_read_available"] is False and "preview" not in private.json()
            assert await original_row()==before and len(google.calls)==before_contacts
            fail_cleanup=False
            retry=await client.post(base+"profiles/"+permission["write_connection_id"]+"/revoke",json=blocked["cleanup_retry"])
            assert retry.status_code==200 and retry.json()["credential_cleanup"]=="verified_unavailable",retry.text
        assert await vault_repository.snapshot(key,owner_principal_id=owner["principal_id"]) is None
        assert await vault_repository.get(key,owner_principal_id=owner["principal_id"]) is None
        # Exact initial duplicate may only reconcile; it never renews authority.
        duplicate=await client.post(base+"profiles/"+permission["write_connection_id"]+"/revoke",json=initial)
        assert duplicate.status_code==200 and duplicate.json()["profile"]["state"]=="revoked",duplicate.text
        old_import={"service":SEND_SERVICE,"label":SEND_SERVICE,"client_id":"synthetic-client","client_secret":None,"refresh_token":SEND_SERVICE,
            "declared_scopes":sorted(SCOPES[SEND_SERVICE]),"acknowledge_separate_identity_profile":True,"idempotency_key":setup_uuid}
        imported=await client.post(base+"profiles",json=old_import);assert imported.status_code==409,imported.text
        pair={name:permission[name] for name in ("read_connection_id","expected_read_revision","write_connection_id","expected_write_revision","event_binding_id","expected_event_binding_revision","goal_id","goal_revision","acknowledge_identity_and_selected_calendar_read")}
        pair["request_uuid"]=ident();denied=await client.post(base+"profiles/verify-pair",json=pair);assert denied.status_code==409,denied.text
        private=await client.get(base+"operations/"+original_id+"/private")
        assert private.status_code==200 and private.json()["private_read_available"] is False and "preview" not in private.json()
        replay=await client.post(base+"operations/"+original_id+"/execute",json={});assert replay.status_code==200 and replay.json()["status"]=="unknown_external_effect",replay.text
        assert await original_row()==before and len(google.calls)==before_contacts and sum(c["method"]=="PATCH" for c in google.calls)==1
        (root/("calendar-vault-cleanup-"+failure+"-receipt.json")).write_text(json.dumps({"first":first.json(),"duplicate":duplicate.json(),"import_denied":imported.json(),"pair_denied":denied.json(),"private":private.json(),"original_history_unchanged":True,"vault_snapshot_unavailable":True,"contacts_before":before_contacts,"contacts_after":len(google.calls),"patch_count":1,"vault_calls_outside_writer":calls},indent=2))
    finally:await client.aclose()


@pytest.mark.asyncio
async def test_logout_after_vault_cleanup_cannot_adopt_or_restore_profile(accounting_db,real_auth,monkeypatch):
    root,engine,factory=accounting_db
    client,post,google,preview,proposal,grant,permission,owner=await prepared(accounting_db,monkeypatch)
    from src.vault import vault_repository
    base="/api/capabilities/calendar/reschedule/";original_id=preview["job_id"]
    try:
        google.lose_response=True
        lost=await client.post(base+"operations/"+original_id+"/execute",json={});assert lost.status_code==409,lost.text
        async with factory.accounting_sessions() as db:
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==original_id))
            before={column.name:getattr(run,column.name) for column in WorkflowRunState.__table__.columns}
            row=await db.get(GoogleServiceConnection,permission["write_connection_id"]);key=row.vault_secret_key
        actual_delete=vault_repository.delete
        async def delete_then_logout(target,*,owner_principal_id):
            deleted=await actual_delete(target,owner_principal_id=owner_principal_id)
            # Actual authenticated logout commits while the cleanup request
            # already holds its original request-scoped authenticated operator.
            logged_out=await client.post("/api/auth/logout",json={});assert logged_out.status_code==204
            return deleted
        monkeypatch.setattr(vault_repository,"delete",delete_then_logout)
        request={"expected_revision":permission["expected_write_revision"],"idempotency_key":ident()};contacts=len(google.calls)
        result=await client.post(base+"profiles/"+permission["write_connection_id"]+"/revoke",json=request)
        assert result.status_code==401 and result.json()["detail"]["code"]=="session_revoked",result.text
        assert await vault_repository.snapshot(key,owner_principal_id=owner["principal_id"]) is None
        async with factory.accounting_sessions() as db:
            row=await db.get(GoogleServiceConnection,permission["write_connection_id"])
            assert row.state=="blocked_cleanup" and row.revision==request["expected_revision"]+1
            assert row.revoke_idempotency_key==request["idempotency_key"] and row.revoke_request_digest
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==original_id))
            assert {column.name:getattr(run,column.name) for column in WorkflowRunState.__table__.columns}==before
        retry=await client.post(base+"profiles/"+permission["write_connection_id"]+"/revoke",json=request);assert retry.status_code==401,retry.text
        assert len(google.calls)==contacts
        (root/"calendar-vault-cleanup-logout-receipt.json").write_text(json.dumps({"result":result.json(),"retry":retry.json(),"vault_snapshot_unavailable":True,"profile_state":"blocked_cleanup","original_revoke_identity_retained":True,"original_history_unchanged":True,"contacts_before":contacts,"contacts_after":len(google.calls),"physical_erasure_verified":False},indent=2))
    finally:await client.aclose()
