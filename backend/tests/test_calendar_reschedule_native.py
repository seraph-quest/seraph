"""Actual auth/API/SQLite/Vault/task/approval/jobs; only Google HTTP simulated."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_calendar_reschedule_contract import ACCOUNT,CALENDAR,source_event,timed
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import Goal,WorkflowRunState,CalendarRescheduleConsent,WorkBoardTask
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.integrations.calendar_reschedule_contract import READ_SERVICE,SEND_SERVICE,SCOPES
from src.integrations import calendar_reschedule_runtime as runtime
from src.vault import crypto


def ident(): return str(uuid.uuid4())


class Google:
    def __init__(self):
        self.calls=[]; self.event=source_event(); self.event["summary"]="Exact owned event"
        self.patch=None; self.conflict=False; self.lose_response=False

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
    monkeypatch.setattr(calendar_controls,"GoogleCalendarReadonlyAdapter",boundary)
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
                preserved={name:getattr(original,name) for name in ("status","checkpoint_context_json","deadline_at","goal_id","goal_revision","authority_digest","fencing_token","lease_owner","lease_expires_at","approval_id","budget_digest")}
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
