"""Real auth/SQLite/Vault/Board/model accounting and reply native flow.

Only Google and OpenRouter HTTP transports (and DNS answers) are intercepted.
Draft setup makes real governed model calls; sending/recovery make none.
"""
import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db, setup_configuration
from tests.test_research_native_vertical import real_auth
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import Goal, WorkBoardTask, WorkflowRunState
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.integrations.gmail_send import READ_SERVICE, SEND_SERVICE, SCOPES
from src.integrations.gmail_read import GMAIL_READONLY_SCOPE
from src.vault import crypto
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import DurableJobRepository


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class GoogleBoundary:
    def __init__(self):
        self.calls = []
        self.sent = None
        self.drop_send = False
        self.raw_original = (b"From: Author <author@example.test>\r\nTo: mailbox@example.test\r\n"
            b"Reply-To: reply@example.test\r\nSubject: Architecture review\r\n"
            b"Message-ID: <original@example.test>\r\nDate: Sun, 04 Oct 2026 12:00:00 +0000\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\nPrivate source with <script> literal.\r\n")
        self.full = {"id": "original-id", "threadId": "original-thread", "historyId": "123",
            "internalDate": str(int(datetime.now(timezone.utc).timestamp()*1000)), "labelIds": ["INBOX", "UNREAD"],
            "snippet": "Private source", "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": "Author <author@example.test>"},
                {"name": "To", "value": "mailbox@example.test"},
                {"name": "Subject", "value": "Architecture review"},
                {"name": "Date", "value": "Sun, 04 Oct 2026 12:00:00 +0000"},
                {"name": "Message-ID", "value": "<original@example.test>"}],
                "body": {"data": encoded(b"Private source with <script> literal."), "size": 42}}}

    async def handle(self, request):
        self.calls.append((request.method, request.url.path, str(request.url.query)))
        if request.url.host == "oauth2.googleapis.com":
            service = next((s for s in (READ_SERVICE, SEND_SERVICE) if s.encode() in request.content), "legacy")
            return httpx.Response(200, json={"access_token": "dummy-"+service,
                "scope": " ".join(sorted(SCOPES[service])) if service != "legacy" else GMAIL_READONLY_SCOPE})
        if request.url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"sub": "ExactSubject", "email": "mailbox@example.test", "email_verified": True})
        assert request.url.host == "gmail.googleapis.com", request.url
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "mailbox@example.test"})
        if path.endswith("/labels"):
            return httpx.Response(200, json={"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}]})
        if path.endswith("/messages/send"):
            assert request.method == "POST" and self.sent is None
            self.sent = json.loads(request.content)
            assert set(self.sent) == {"raw", "threadId"} and self.sent["threadId"] == "original-thread"
            if self.drop_send:
                raise httpx.ReadError("intercepted accepted POST response lost")
            return httpx.Response(200, json={"id": "sent-id", "threadId": "original-thread"})
        if "/threads/" in path:
            return httpx.Response(200, json={"id": "original-thread", "messages": [{"id": "original-id", "threadId": "original-thread"}]})
        if path.endswith("/messages"):
            sent_search = "in:sent" in request.url.params.get("q", "")
            return httpx.Response(200, json={"messages": [{"id": "sent-id" if sent_search else "original-id", "threadId": "original-thread"}], "resultSizeEstimate": 1})
        if path.endswith("/messages/sent-id"):
            assert self.sent is not None
            return httpx.Response(200, json={"id": "sent-id", "threadId": "original-thread", "labelIds": ["SENT"], "raw": self.sent["raw"]})
        assert path.endswith("/messages/original-id"), path
        if request.url.params.get("format") == "raw":
            return httpx.Response(200, json={"id": "original-id", "threadId": "original-thread", "labelIds": ["INBOX"], "raw": encoded(self.raw_original)})
        return httpx.Response(200, json=self.full)


def model_response(request, calls):
    assert request.url.host == "openrouter.ai" and request.method == "POST"
    body = json.loads(request.content)
    calls.append(body)
    if len(body["messages"]) == 1:
        content = '{"ok":true}' if body.get("response_format") else "CANARY_OK"
    else:
        content = json.dumps({"subject": "Re: Architecture review", "body": "Thanks — <script>alert(1)</script> is literal text.\nI will follow up.", "caveats": ["Review before sending."]})
    return httpx.Response(200, json={"id": "intercepted-"+str(len(calls)),
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"cost": "0.000002", "prompt_tokens": 20, "completion_tokens": 20}})


async def prepared_flow(accounting_db, real_auth, monkeypatch):
    from src.api import auth, work_board, model_fabric_settings, goals, mail
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.integrations import gmail_read, mail_reply_runtime
    root, engine, factory = accounting_db
    root.chmod(0o700)
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(mail, "get_session", factory.accounting_sessions)
    original_error = mail._error
    def traced_error(exc):
        import traceback
        traceback.print_exception(exc)
        return original_error(exc)
    monkeypatch.setattr(mail, "_error", traced_error)
    configured = setup_configuration()
    setup = replace(configured.openrouter_setup, timeout_seconds=30, capabilities=("text", "structured_output"))
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(setup, profiles=(), policies=()), egress_revision=configured.egress_revision+1))
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(1000)
    google = GoogleBoundary()
    transport = httpx.MockTransport(google.handle)
    real_google = gmail_read.GoogleGmailReadonlyAdapter
    monkeypatch.setattr(gmail_read, "GoogleGmailReadonlyAdapter", lambda *args, **kwargs: real_google(*args,
        transport=transport, resolver=lambda host, port: ["93.184.216.34"], **kwargs))
    monkeypatch.setattr(mail, "GoogleGmailReadonlyAdapter", gmail_read.GoogleGmailReadonlyAdapter)
    real_reply = mail_reply_runtime.GmailReplyAdapter
    monkeypatch.setattr(mail_reply_runtime, "GmailReplyAdapter", lambda *args, **kwargs: real_reply(*args,
        transport=transport, resolver=lambda host, port: ["93.184.216.34"], **kwargs))
    model_calls = []
    async_client, sync_client = httpx.AsyncClient, httpx.Client
    def async_clients(**kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(lambda req: model_response(req, model_calls))
        return async_client(**kwargs)
    def sync_clients(**kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(lambda req: model_response(req, model_calls))
        return sync_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", async_clients)
    monkeypatch.setattr(httpx, "Client", sync_clients)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    for router, prefix in ((auth.router,"/api/auth"), (work_board.router,"/api"),
        (model_fabric_settings.router,"/api"), (goals.router,"/api"), (mail.router,"/api")):
        app.include_router(router, prefix=prefix)
    client = async_client(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"origin":"http://localhost:3001"})
    login = await client.post("/api/auth/login", json={"password":"research-vertical-private-secret"})
    assert login.status_code == 200, login.text
    owner = login.json()
    for capability in ("text", "structured_output", "latency_ms", "health"):
        response = await client.post("/api/settings/model-fabric/canary", json={"profile_id":"openrouter", "capability":capability, "timeout_seconds":30})
        assert response.status_code == 200 and response.json()["outcome"] == "passed", response.text
    async with factory.accounting_sessions() as db:
        db.add(Goal(id="mail-actual-goal", title="Finite actual mail reply", status="active", revision=1,
            owner_principal_id=owner["principal_id"], owner_session_id=owner["session_id"],
            admission_budget_json=serialize_admission_budget(GoalAdmissionBudget(reviewed_grant=True, grant_id="mail-actual-grant", max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=300))))
    async def post(path, body):
        response = await client.post("/api/capabilities/mail/"+path, json=body)
        assert response.status_code in (200,201), (path,response.status_code,response.text)
        return response.json()
    legacy = (await post("connections", {"label":"Source only", "client_id":"dummy-client", "refresh_token":"legacy", "declared_scopes":[GMAIL_READONLY_SCOPE], "idempotency_key":"legacy-once"}))["connection"]
    legacy_id, rev = legacy["connection_id"], legacy["revision"]
    rev = (await post("connections/"+legacy_id+"/verify", {"expected_revision":rev,"request_uuid":"legacy-verify"}))["connection_revision"]
    labels = await post("labels/refresh", {"connection_id":legacy_id,"expected_connection_revision":rev,"acknowledge_account_label_read":True,"request_uuid":"labels-once"})
    rev = labels["connection_revision"]
    label_id = labels["labels"][0]["label_id"]
    consent = (await post("read-consents", {"connection_id":legacy_id,"expected_connection_revision":rev,"goal_id":"mail-actual-goal","expected_goal_revision":1,
        "label_ids":[label_id],"expires_at":(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),"max_messages":1,"acknowledge_source_read":True,"idempotency_key":"consent-once"}))["consent"]
    consent = (await post("read-consents/"+consent["consent_id"]+"/model-consent", {"expected_revision":consent["revision"],"allow":True,"acknowledged_payload_fields":["subject","plainbody","replyintent"]}))["consent"]
    scanned = await post("messages/scan", {"connection_id":legacy_id,"expected_connection_revision":rev,"mail_consent_id":consent["consent_id"],"expected_source_consent_revision":consent["source_revision"],
        "label_ids":[label_id],"received_after":(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),"max_messages":1,"request_uuid":"scan-once"})
    source = scanned["messages"][0]
    created = await post("reply-tasks", {"schema_version":1,"connection_id":legacy_id,"expected_connection_revision":rev,"message_binding_id":source["source_binding_id"],"expected_message_revision":source["message_revision"],
        "mail_consent_id":consent["consent_id"],"expected_source_consent_revision":consent["source_revision"],"expected_model_consent_revision":consent["model_revision"],"goal_id":"mail-actual-goal","expected_goal_revision":1,
        "reply_intent":"Reply briefly and preserve literal text.","style":"brief","idempotency_key":"draft-once"})
    task_id = created["task_id"]
    dispatcher = WorkBoardDispatcher(jobs=jobs, session_provider=factory.accounting_sessions)
    result = await dispatcher.run_pass()
    draft = await client.get("/api/capabilities/mail/reply-tasks/"+task_id+"/draft")
    assert draft.status_code == 200 and draft.json()["status"] == "verified", (result,draft.text)
    assert len(model_calls) == 5  # four actual canaries + one governed draft call
    profiles = {}
    for service in (READ_SERVICE,SEND_SERVICE):
        profiles[service] = (await post("reply-profiles", {"service":service,"label":service,"client_id":"dummy-client","refresh_token":service,"declared_scopes":sorted(SCOPES[service]),
            "acknowledge_separate_identity_profile":True,"idempotency_key":"profile-"+service}))["profile"]
    pair = {"read_connection_id":profiles[READ_SERVICE]["connection_id"],"expected_read_revision":profiles[READ_SERVICE]["revision"],"send_connection_id":profiles[SEND_SERVICE]["connection_id"],
        "expected_send_revision":profiles[SEND_SERVICE]["revision"],"goal_id":"mail-actual-goal","goal_revision":1,"acknowledge_identity_read":True,"request_uuid":"pair-once"}
    verified = await post("reply-profiles/verify-pair", pair)
    assert verified["status"] == "succeeded" and verified["contacts_spent"] == 5
    preview_body = {key:value for key,value in pair.items() if key in {"read_connection_id","expected_read_revision","send_connection_id","expected_send_revision"}}
    preview_body.update(task_id=task_id,expected_message_revision=source["message_revision"],acknowledge_identity_source_read=True,acknowledge_exact_reply_send=True,request_uuid="preview-once")
    preview = await post("reply-sends/preview", preview_body)
    assert preview["status"] == "paused" and preview["contacts_spent"] == 5 and preview["preview"]["recipient"] == "reply@example.test"
    approved = await post("reply-sends/"+preview["job_id"]+"/decision", {"decision":"approved","expected_digest":preview["preview"]["decision_digest"]})
    assert approved["preview"]["approval_status"] == "approved"
    return client, post, google, model_calls, preview, preview_body, owner


@pytest.mark.asyncio
async def test_actual_operator_draft_approval_send_one_post_sqlite_reopen(accounting_db, real_auth, monkeypatch):
    pure_reply_writer_guard(monkeypatch)
    client, post, google, model_calls, preview, body, owner = await prepared_flow(accounting_db, real_auth, monkeypatch)
    root, engine, factory = accounting_db
    try:
        sent = await post("reply-sends/"+preview["job_id"]+"/execute", {})
        assert sent["status"] == "succeeded" and sent["outcome"] == "verified_in_sender_sent_mailbox"
        assert sent["contacts_spent"] == 14 and sent["no_learning"] is True
        contacts = len(google.calls)
        await post("reply-sends/"+preview["job_id"]+"/execute", {})
        replay = await post("reply-sends/preview", body)
        assert replay["job_id"] == preview["job_id"] and len(google.calls) == contacts and len(model_calls) == 5
        await engine.dispose()
        reopened = await client.get("/api/capabilities/mail/reply-sends/"+preview["job_id"])
        assert reopened.status_code == 200 and reopened.json()["status"] == "succeeded"
        async with factory.accounting_sessions() as db:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==preview["job_id"]))
            assert run.attempt_count == 1 and run.lease_owner is None
            effects = json.loads(run.effect_receipts_json)
            assert len(effects)==1 and effects[0]["status"]=="succeeded"
        (root/"mail-reply-operator-readback.json").write_text(json.dumps({"preview":preview,"sent":sent,"reopened":reopened.json(),"google_contacts":google.calls,"model_calls":len(model_calls),"post_resource":google.sent},indent=2))
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["auxiliary_rollback", "duplicate_observation", "stale_recovery_goal"])
async def test_actual_readonly_observation_atomicity_and_replay(accounting_db, real_auth, monkeypatch, boundary):
    import asyncio
    from src.integrations import mail_reply_runtime as runtime
    pure_reply_writer_guard(monkeypatch)
    client, post, google, model_calls, preview, body, owner = await prepared_flow(accounting_db, real_auth, monkeypatch)
    root, engine, factory = accounting_db
    started=asyncio.Event();release=asyncio.Event();running=None
    try:
        google.drop_send=True
        response=await client.post("/api/capabilities/mail/reply-sends/"+preview["job_id"]+"/execute",json={})
        assert response.status_code>=400
        original=(await client.get("/api/capabilities/mail/reply-sends/"+preview["job_id"])).json()
        assert original["status"]=="unknown_external_effect" and original["transport_quiescent"] is True
        profiles=(await client.get("/api/capabilities/mail/reply-profiles")).json()["profiles"]
        read=next(row for row in profiles if row["service"]==READ_SERVICE)
        async with factory.accounting_sessions() as db:
            db.add(Goal(id="mail-atomic-observation-goal",title="Finite readonly observation",status="active",revision=1,
                owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],
                admission_budget_json=serialize_admission_budget(GoalAdmissionBudget(reviewed_grant=True,grant_id="mail-atomic-observation",max_outstanding_jobs=1,max_attempts=1,max_runtime_seconds=120))))
            row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==preview["job_id"]))
            before=(row.revision,row.effect_receipts_json,row.status,row.checkpoint_context_json)
        request={"expected_original_revision":original["revision"],"read_connection_id":read["connection_id"],"expected_read_revision":read["revision"],
            "goal_id":"mail-atomic-observation-goal","goal_revision":1,"acknowledge_readonly_recovery":True,"request_uuid":"atomic-observation-once"}
        endpoint="/api/capabilities/mail/reply-sends/"+preview["job_id"]+"/observe"
        contacts_before=len(google.calls)
        if boundary=="auxiliary_rollback":
            complete=DurableJobRepository.complete_mail_observation_in_session
            async def fail_after_completion(self,db,run,**kwargs):
                result=await complete(self,db,run,**kwargs)
                raise RuntimeError("intercepted atomic auxiliary completion commit failure")
            monkeypatch.setattr(DurableJobRepository,"complete_mail_observation_in_session",fail_after_completion)
            result=await client.post(endpoint,json=request)
            assert result.status_code>=400
            async with factory.accounting_sessions() as db:
                row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==preview["job_id"]))
                assert (row.revision,row.effect_receipts_json,row.status,row.checkpoint_context_json)==before
                aux=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind==runtime.OBSERVATION_KIND))
                assert aux.status!="succeeded" and json.loads(aux.artifact_receipts_json)==[]
        elif boundary=="stale_recovery_goal":
            async with factory.accounting_sessions() as db:
                goal=await db.get(Goal,"mail-atomic-observation-goal");goal.revision+=1
            result=await client.post(endpoint,json=request)
            assert result.status_code>=400 and len(google.calls)==contacts_before
        else:
            handle=google.handle
            async def hold_search(req):
                response=await handle(req)
                if req.url.path.endswith("/messages") and "in:sent" in req.url.params.get("q",""):
                    started.set();await release.wait()
                return response
            real=__import__("src.integrations.gmail_send",fromlist=["GmailReplyAdapter"]).GmailReplyAdapter
            monkeypatch.setattr(runtime,"GmailReplyAdapter",lambda *args,**kwargs:real(*args,transport=httpx.MockTransport(hold_search),resolver=lambda host,port:["93.184.216.34"],**kwargs))
            running=asyncio.create_task(client.post(endpoint,json=request));await asyncio.wait_for(started.wait(),timeout=30)
            count=len(google.calls);duplicate=await client.post(endpoint,json=request)
            assert duplicate.status_code==200 and duplicate.json()["status"]=="running" and len(google.calls)==count
            release.set();result=await asyncio.wait_for(running,timeout=10)
            assert result.status_code==200 and result.json()["outcome"]=="verified_sent_observation"
            count=len(google.calls);replay=await client.post(endpoint,json=request)
            assert replay.status_code==200 and replay.json()["job_id"]==result.json()["job_id"] and len(google.calls)==count
        await engine.dispose()
        reopened=(await client.get("/api/capabilities/mail/reply-sends/"+preview["job_id"])).json()
        assert reopened["status"]=="unknown_external_effect"
        assert len(reopened.get("observations", []))==(1 if boundary=="duplicate_observation" else 0)
        assert len([call for call in google.calls if call[0]=="POST" and call[1].endswith("/messages/send")])==1 and len(model_calls)==5
        (root/("mail-observation-"+boundary+".json")).write_text(json.dumps({"boundary":boundary,"original_before":original,"original_reopened":reopened,"response_status":result.status_code,"response":result.json(),"contacts":google.calls},indent=2))
    finally:
        release.set()
        if running is not None and not running.done():running.cancel();await asyncio.gather(running,return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_send_readonly_recovery_old_goal_deadline_preserved(accounting_db, real_auth, monkeypatch):
    client, post, google, model_calls, preview, body, owner = await prepared_flow(accounting_db, real_auth, monkeypatch)
    root, engine, factory = accounting_db
    try:
        google.drop_send = True
        unknown = await client.post("/api/capabilities/mail/reply-sends/"+preview["job_id"]+"/execute", json={})
        assert unknown.status_code >= 400
        original = (await client.get("/api/capabilities/mail/reply-sends/"+preview["job_id"])).json()
        assert original["status"] == "unknown_external_effect" and original["transport_quiescent"] is True
        posts = [call for call in google.calls if call[0]=="POST" and call[1].endswith("/messages/send")]
        assert len(posts)==1 and len(model_calls)==5
        async with factory.accounting_sessions() as db:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==preview["job_id"]))
            # Explicit persisted fault/clock-boundary fixture: expire the old
            # immutable deadline, close/change its Goal; recovery never rewrites them.
            run.deadline_at = datetime.now(timezone.utc)-timedelta(seconds=1)
            goal = await db.get(Goal,"mail-actual-goal")
            goal.status="completed";goal.revision+=1
            db.add(Goal(id="mail-recovery-goal",title="Finite readonly observation",status="active",revision=1,
                owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],
                admission_budget_json=serialize_admission_budget(GoalAdmissionBudget(reviewed_grant=True,grant_id="readonly-recovery-grant",max_outstanding_jobs=1,max_attempts=1,max_runtime_seconds=120))))
            before={key:getattr(run,key) for key in ("status","deadline_at","goal_id","goal_revision","fencing_token","attempt_count","declared_authority_json","authority_digest","budget_digest","checkpoint_context_json","lease_owner","lease_expires_at")}
        profiles = (await client.get("/api/capabilities/mail/reply-profiles")).json()["profiles"]
        read = next(row for row in profiles if row["service"]==READ_SERVICE)
        request={"expected_original_revision":original["revision"],"read_connection_id":read["connection_id"],"expected_read_revision":read["revision"],
            "goal_id":"mail-recovery-goal","goal_revision":1,"acknowledge_readonly_recovery":True,"request_uuid":"readonly-observation-once"}
        recovered = await post("reply-sends/"+preview["job_id"]+"/observe",request)
        assert recovered["outcome"]=="verified_sent_observation" and recovered["status"]=="succeeded" and recovered["contacts_spent"]==5
        contacts=len(google.calls)
        repeated=await post("reply-sends/"+preview["job_id"]+"/observe",request)
        assert repeated["job_id"]==recovered["job_id"] and len(google.calls)==contacts
        await engine.dispose()
        async with factory.accounting_sessions() as db:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==preview["job_id"]))
            assert all(str(getattr(run,key))==str(value) or (key=="deadline_at" and getattr(run,key).replace(tzinfo=timezone.utc)==value) for key,value in before.items())
            effects=json.loads(run.effect_receipts_json)
            assert effects[0]["status"]=="unknown" and len(effects[0]["observation_history"])==1
            aux=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==recovered["job_id"]))
            assert json.loads(aux.artifact_receipts_json)[0]["exists"] is True and aux.status=="succeeded"
        after=(await client.get("/api/capabilities/mail/reply-sends/"+preview["job_id"])).json()
        assert after["status"]=="unknown_external_effect" and after["observations"][0]["outcome"]=="verified_sent_observation"
        await post("reply-sends/"+preview["job_id"]+"/execute",{})
        assert len(google.calls)==contacts and len(model_calls)==5
        (root/"mail-unknown-recovery-readback.json").write_text(json.dumps({"unknown":original,"auxiliary":recovered,"original_after":after,"contacts":google.calls,"send_posts":len(posts),"model_setup_calls":5},indent=2))
    finally:
        await client.aclose()


def pure_reply_writer_guard(monkeypatch):
    """Assert new Mail writers never perform staging I/O or nested sessions."""
    from contextlib import asynccontextmanager
    from contextvars import ContextVar
    from pathlib import Path
    import builtins
    import os
    from src.integrations import mail_reply_runtime as runtime
    from src.db import engine
    from src.vault import crypto as vault_crypto
    inside=ContextVar("inside_exact_mail_writer",default=False)
    original_writer=runtime.writer
    @asynccontextmanager
    async def writer():
        assert not inside.get(), "nested immediate Mail writer"
        async with original_writer() as db:
            token=inside.set(True)
            try:yield db
            finally:inside.reset(token)
    monkeypatch.setattr(runtime,"writer",writer)
    def guarded(original):
        def call(*args,**kwargs):
            assert not inside.get(), "physical staging inside immediate Mail writer"
            return original(*args,**kwargs)
        return call
    for target,name in ((Path,"open"),(Path,"read_bytes"),(Path,"read_text"),(builtins,"open"),(os,"open"),(vault_crypto,"decrypt"),(runtime,"decrypt")):
        monkeypatch.setattr(target,name,guarded(getattr(target,name)))
    original_session=engine.get_session
    def session():
        assert not inside.get(), "nested session inside immediate Mail writer"
        return original_session()
    monkeypatch.setattr(engine,"get_session",session)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["goal_revision", "logout", "vault_rotation", "approval_attachments", "missing_actual_thread"])
async def test_actual_reply_precontact_authority_and_pure_writer_negatives(accounting_db, real_auth, monkeypatch, fault):
    pure_reply_writer_guard(monkeypatch)
    client, post, google, model_calls, preview, body, owner = await prepared_flow(accounting_db, real_auth, monkeypatch)
    root, engine, factory = accounting_db
    try:
        from src.db.models import ApprovalRequest, GoogleServiceConnection
        from src.vault import vault_repository
        if fault=="goal_revision":
            changed=await client.patch("/api/goals/mail-actual-goal",json={"title":"Changed current Goal","expected_revision":1})
            assert changed.status_code==200,changed.text
        elif fault=="logout":
            assert (await client.post("/api/auth/logout")).status_code==204
        elif fault=="vault_rotation":
            async with factory.accounting_sessions() as db:
                row=await db.get(GoogleServiceConnection,body["send_connection_id"])
                key=row.vault_secret_key
            await vault_repository.store(key,json.dumps({"client_id":"dummy-client","client_secret":None,"refresh_token":"rotated-send"}),owner_principal_id=owner["principal_id"])
        elif fault=="approval_attachments":
            async with factory.accounting_sessions() as db:
                approval=await db.get(ApprovalRequest,preview["preview"]["approval_id"])
                approval.attachment_refs_json=json.dumps([{"path":"/must-never-be-read"}])
        else:
            original_handle=google.handle
            async def missing_thread(request):
                response=await original_handle(request)
                if request.url.path.endswith("/messages/original-id") and request.url.params.get("format")=="raw":
                    payload=json.loads(response.content);payload.pop("threadId")
                    return httpx.Response(200,json=payload)
                return response
            # The already constructed MockTransport calls its saved handler.
            google.handle=missing_thread
            from src.integrations import mail_reply_runtime
            real_reply=__import__("src.integrations.gmail_send",fromlist=["GmailReplyAdapter"]).GmailReplyAdapter
            monkeypatch.setattr(mail_reply_runtime,"GmailReplyAdapter",lambda *args,**kwargs:real_reply(*args,transport=httpx.MockTransport(missing_thread),resolver=lambda host,port:["93.184.216.34"],**kwargs))
        contacts=len(google.calls)
        denied=await client.post("/api/capabilities/mail/reply-sends/"+preview["job_id"]+"/execute",json={})
        assert denied.status_code>=400
        assert google.sent is None and len(model_calls)==5
        if fault in {"goal_revision","logout","vault_rotation"}:assert len(google.calls)==contacts
        async with factory.accounting_sessions() as db:
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==preview["job_id"]))
            assert "intent" not in json.loads(run.checkpoint_context_json) and json.loads(run.effect_receipts_json)==[]
            approval=await db.get(ApprovalRequest,preview["preview"]["approval_id"])
            assert approval.status=="approved"
        (root/("mail-negative-"+fault+".json")).write_text(json.dumps({"fault":fault,"status":denied.status_code,"response":denied.json(),"google_contacts":google.calls,"send_posts":0,"model_setup_calls":5},indent=2))
    finally:await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["paused_cancel", "active_post_cancel", "intent_rollback"])
async def test_actual_mail_cancellation_and_approval_intent_rollback(accounting_db, real_auth, monkeypatch, boundary):
    import asyncio
    from src.integrations import mail_reply_runtime as runtime, mail_reply_send as kernel
    from src.db.models import ApprovalRequest
    pure_reply_writer_guard(monkeypatch)
    client, post, google, model_calls, preview, body, owner = await prepared_flow(accounting_db, real_auth, monkeypatch)
    root, engine, factory = accounting_db
    started=asyncio.Event();closed=asyncio.Event();release=asyncio.Event();running=None
    try:
        if boundary=="paused_cancel":
            result=await post("reply-sends/"+preview["job_id"]+"/cancel",{"expected_revision":preview["revision"],"request_uuid":"pause-cancel-once"})
            assert result["status"]=="cancelled" and result["cancel_request_uuid"]=="pause-cancel-once"
            assert google.sent is None
        elif boundary=="intent_rollback":
            real_cas=kernel.cas
            async def fail_after_approval(db,run,values,**kwargs):
                if "intent" in json.loads(values.get("checkpoint_context_json") or "{}"):
                    approval=await db.get(ApprovalRequest,preview["preview"]["approval_id"])
                    assert approval.status=="consumed"  # actual same-transaction consumer occurred
                    raise RuntimeError("intercepted SQL commit boundary failure")
                return await real_cas(db,run,values,**kwargs)
            monkeypatch.setattr(kernel,"cas",fail_after_approval)
            response=await client.post("/api/capabilities/mail/reply-sends/"+preview["job_id"]+"/execute",json={})
            assert response.status_code>=400 and google.sent is None
            async with factory.accounting_sessions() as db:
                approval=await db.get(ApprovalRequest,preview["preview"]["approval_id"])
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==preview["job_id"]))
                assert approval.status=="approved" and "intent" not in json.loads(run.checkpoint_context_json) and json.loads(run.effect_receipts_json)==[]
            result=(await client.get("/api/capabilities/mail/reply-sends/"+preview["job_id"])).json()
            assert result["status"]=="blocked"
        else:
            original_handle=google.handle
            async def hold_post(request):
                response=await original_handle(request)
                if request.url.path.endswith("/messages/send"):
                    started.set()
                    try:await release.wait()
                    finally:closed.set()
                return response
            real_reply=__import__("src.integrations.gmail_send",fromlist=["GmailReplyAdapter"]).GmailReplyAdapter
            monkeypatch.setattr(runtime,"GmailReplyAdapter",lambda *args,**kwargs:real_reply(*args,transport=httpx.MockTransport(hold_post),resolver=lambda host,port:["93.184.216.34"],**kwargs))
            running=asyncio.create_task(client.post("/api/capabilities/mail/reply-sends/"+preview["job_id"]+"/execute",json={}))
            await asyncio.wait_for(started.wait(),timeout=30)
            current=(await client.get("/api/capabilities/mail/reply-sends/"+preview["job_id"])).json()
            assert current["status"]=="running" and current["contact_may_have_occurred"] is True
            result=await post("reply-sends/"+preview["job_id"]+"/cancel",{"expected_revision":current["revision"],"request_uuid":"active-cancel-once"})
            await asyncio.wait_for(asyncio.gather(running,return_exceptions=True),timeout=5)
            assert closed.is_set() and result["status"]=="unknown_external_effect" and result["transport_quiescent"] is True
            assert result["contact_may_have_occurred"] is True and google.sent is not None
        contacts=len(google.calls)
        await engine.dispose()
        reopened=(await client.get("/api/capabilities/mail/reply-sends/"+preview["job_id"])).json()
        assert reopened["status"]==result["status"]
        replay=await post("reply-sends/"+preview["job_id"]+"/execute",{})
        assert replay["status"]==result["status"] and len(google.calls)==contacts
        assert len([c for c in google.calls if c[0]=="POST" and c[1].endswith("/messages/send")])==(1 if boundary=="active_post_cancel" else 0)
        (root/("mail-cancellation-"+boundary+".json")).write_text(json.dumps({"boundary":boundary,"result":result,"reopened":reopened,"contacts":google.calls,"model_setup_calls":len(model_calls),"actual_callback_closed":closed.is_set()},indent=2))
    finally:
        release.set()
        if running is not None and not running.done():
            running.cancel();await asyncio.gather(running,return_exceptions=True)
        await client.aclose()
