"""Fixed opportunity admission uses existing outbox intents, no provider."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import select, func

from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION
from tests.test_guardian_opportunity_policy import publish_source
from src.db.models import Goal, GuardianOpportunity, GuardianIntervention, NativeNotificationOutbox
from src.guardian.opportunities import now
from src.guardian.opportunity_contracts import OpportunityError
from src.observer.native_notification_queue import NativeNotificationQueue


async def test_opportunity_outbox_default_opt_in_idempotency_unknown_cap(isolated_runtime):
    sessions, goal, _, _, row, _ = await publish_source(isolated_runtime)
    # This focused admission test supplies a proposed lineage; actual model
    # creation of that lineage is separately required by the vertical test.
    async with sessions() as db:
        current = await db.get(GuardianOpportunity, row.id)
        current.status = "proposed"
        current.intervention_id = "opportunity:" + row.id
        db.add(current)
        db.add(GuardianIntervention(id=current.intervention_id, intervention_type="opportunity",
            owner_principal_id=OWNER, original_root_id=SESSION, goal_id=goal.id,
            goal_revision=1, opportunity_id=row.id, delivery_status="not_requested"))
    queue = NativeNotificationQueue()
    fields = dict(intervention_id="opportunity:" + row.id, title="Guardian opportunity",
        body="Review the cited judgment in Inbox.", intervention_type="opportunity", urgency=2,
        owner_principal_id=OWNER, operator_session_id=SESSION, goal_id=goal.id, goal_revision=1,
        budget_period_key=now().date().isoformat(), budget_limit=1, idempotency_key="bounded-opportunity")
    with pytest.raises(OpportunityError, match="opportunity_notifications_disabled"):
        await queue.enqueue(**fields)
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox))).scalar() == 0
        current = await db.get(Goal, goal.id)
        policy = json.loads(current.guardian_policy_json)
        policy["max_notification_per_utc_day"] = 1
        current.guardian_policy_json = json.dumps(policy)
        db.add(current)
    notification = await queue.enqueue(**fields)
    assert (await queue.enqueue(**fields)).id == notification.id
    async with sessions() as db:
        intent = await db.get(NativeNotificationOutbox, notification.id)
        intent.status = "unknown"
        db.add(intent)
    with pytest.raises(ValueError, match="goal_budget_notification_limit|opportunity_notification_limit"):
        await queue.enqueue(**dict(fields, idempotency_key="no-unknown-retry"))
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox))).scalar() == 1
        from src.guardian.opportunities import project_item
        current = await db.get(GuardianOpportunity, row.id)
        projection = await project_item(db, current)
        assert projection["delivery_status"] == "unknown"


# Unlike the historical admission-only fixture above, these regressions run
# real auth/Goal/watch/policy/proofs/native assessment; only HTTP is intercepted.
from tests.test_inference_accounting import accounting_db, setup_configuration
from tests.test_research_native_vertical import real_auth, ResponseBytes
from tests.test_guardian_opportunity_vertical import OpportunityHttpBoundary


async def _actual_notification_intent(accounting_db, monkeypatch, *, auth_cookies=None):
    import httpx
    from dataclasses import replace
    from datetime import datetime, timezone
    from uuid import uuid4
    from fastapi import FastAPI
    from src.api import auth, goals, model_fabric_settings
    from src.auth.middleware import OperatorAuthMiddleware
    from src.guardian import source_watch
    from src.guardian.opportunity_runtime import execute_assessment
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.model_fabric import remote_inference_admission
    from src.security.http_transport import fetch_pinned_https
    from src.workflows.job_runtime import durable_job_repository
    from config.settings import settings
    monkeypatch.setattr(settings, "operator_auth_idle_seconds", 3600)
    monkeypatch.setattr("src.model_fabric.execution.gpu_admission_broker",
        remote_inference_admission.remote_inference_admission_broker)
    root, _, factory = accounting_db
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30, capabilities=("text", "structured_output")),
        profiles=(), policies=()), egress_revision=configured.egress_revision+1))
    await durable_job_repository.configure_inference_accounting(1000)
    calls, sources = [], []
    original_client = httpx.AsyncClient
    def clients(**kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = OpportunityHttpBoundary(calls, {})
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    versions = iter(("Previous public release\n", "A relevant new public release\n"))
    async def public_http(request):
        sources.append(str(request.url))
        return httpx.Response(200, request=request, headers={"content-type":"text/plain"},
            stream=ResponseBytes(next(versions).encode()))
    async def pinned_http(url, **kwargs):
        return await fetch_pinned_https(url, resolver=lambda host, port:["93.184.216.34"],
            transport=httpx.MockTransport(public_http), **kwargs)
    monkeypatch.setattr(source_watch, "fetch_pinned_https", pinned_http)
    app = FastAPI();app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router,prefix="/api/auth");app.include_router(goals.router,prefix="/api")
    app.include_router(model_fabric_settings.router,prefix="/api")
    app.include_router(source_watch.source_watch_router,prefix="/api")
    async with original_client(transport=httpx.ASGITransport(app=app),base_url="http://test",
            headers={"origin":"http://localhost:3001"}) as client:
        login=await client.post("/api/auth/login",json={"password":"research-vertical-private-secret"})
        assert login.status_code==200,login.text
        owner=login.json()
        if auth_cookies is not None:
            enrolled=await client.post("/api/auth/ownership/enroll")
            assert enrolled.status_code==200,enrolled.text
            authenticated=await client.get("/api/auth/session")
            assert authenticated.status_code==200,authenticated.text
            owner=authenticated.json()
            for name in (settings.operator_auth_cookie_name, auth._continuity_cookie_name()):
                auth_cookies[name]=client.cookies.get(name)
                assert auth_cookies[name]
        for capability in ("text","structured_output","health","latency_ms"):
            response=await client.post("/api/settings/model-fabric/canary",json=dict(
                profile_id="openrouter",capability=capability,timeout_seconds=30))
            assert response.status_code==200 and response.json()["outcome"]=="passed",response.text
        current=datetime.now(timezone.utc)
        response=await client.post("/api/goals",json=dict(title="Actual notification review",
            proactive_enabled=True,admission_budget=dict(reviewed_grant=True,grant_id="notification-review",
                max_outstanding_jobs=2,max_attempts=2,max_runtime_seconds=300,
                period_started_at=current.isoformat(),period_expires_at=(current+timedelta(hours=1)).isoformat())))
        assert response.status_code==200,response.text
        goal=response.json()
        response=await client.post("/api/capabilities/source-watches",json=dict(goal_id=goal["id"],
            expected_goal_revision=goal["revision"],sources=[dict(source_key="public",kind="public_https_text",
                target="https://example.com/public",label="Public release",priority=1)],criteria={},
            schedule=dict(cron="0 * * * *",timezone="UTC"),write_mode="standing_reviewed",reviewed_grant_id="notification-review"))
        assert response.status_code==200,response.text
        watch=response.json()
        response=await client.put(f"/api/goals/{goal['id']}/guardian-policy",json=dict(
            expected_goal_revision=goal["revision"],expected_policy_revision=0,idempotency_key=str(uuid4()),
            acknowledge_notifications=True,policy=dict(schema_version="seraph.guardian.policy.v1",
                assessment_enabled=True,confirmed_at=current.isoformat(),review_due_at=(current+timedelta(hours=1)).isoformat(),
                grant_id="notification-review",original_root_id=owner["session_id"],goal_revision=goal["revision"],
                source_watch_ids=[watch["id"]],max_assessments_per_utc_day=2,max_notification_per_utc_day=1)))
        assert response.status_code==200,response.text
        service=source_watch.SourceWatchService()
        for occurrence in ("baseline","changed"):
            response=await service.run_watch(watch["id"],occurrence_id=occurrence,
                expected_plan_revision=1,expected_owner_session_id=owner["session_id"])
            assert response["status"] in {"baseline_initialized","succeeded"},response
        async with factory.accounting_sessions() as db:
            row=(await db.execute(select(GuardianOpportunity))).scalar_one()
        await execute_assessment(row.id)
        async with factory.accounting_sessions() as db:
            row=await db.get(GuardianOpportunity,row.id)
            assert row.status=="proposed",row.reason_code
            notification=(await db.execute(select(NativeNotificationOutbox))).scalars().first()
        queue=NativeNotificationQueue()
        if notification is None:
            notification=await queue.enqueue(intervention_id=row.intervention_id,title="Guardian opportunity",
                body="A cited public-source judgment is ready in Guardian Inbox.",intervention_type="opportunity",urgency=2,
                owner_principal_id=owner["principal_id"],operator_session_id=owner["session_id"],goal_id=goal["id"],
                goal_revision=goal["revision"],budget_period_key=current.date().isoformat(),budget_limit=1,
                idempotency_key=f"opportunity:{row.id}:notification")
        assert len(calls)==5 and len(sources)==2
        assert (await durable_job_repository.get_job(row.job_id))["status"]=="succeeded"
        return factory.accounting_sessions,owner,row,notification.id,queue


@pytest.mark.parametrize("action",["snooze","dismiss"])
async def test_actual_intent_then_inbox_review_suppresses_claim(accounting_db,real_auth,monkeypatch,action):
    from src.guardian import inbox
    from src.db.models import GuardianInboxDisposition
    sessions,owner,row,notification_id,queue=await _actual_notification_intent(accounting_db,monkeypatch)
    caller_scope=dict(owner_principal_id=owner["principal_id"],operator_session_id=owner["session_id"])
    detail=await inbox.get_owned_item(owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],item_id=row.id)
    until=now()+timedelta(minutes=20) if action=="snooze" else None
    result=await inbox.apply_action(owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],
        item_id=row.id,action=action,expected_revision=detail["revision"],idempotency_key="actual-review-"+action,
        until=until)
    assert result["state"]==("snoozed" if until else "dismissed")
    assert await queue.claim_next(worker_id="actual-native-daemon",**caller_scope) is None
    async with sessions() as db:
        intent=await db.get(NativeNotificationOutbox,notification_id)
        assert intent.status==("queued" if until else "cancelled")
        assert intent.attempt_count==0 and intent.fencing_token==0
        deadline=intent.deadline_at
    if until:
        ordinary=await queue.enqueue(intervention_id=None,title="Recovery",body="Existing recovery notice",
            intervention_type="recovery",urgency=1,idempotency_key="ordinary-recovery")
        assert (await queue.claim_next(worker_id="recovery-daemon")).id==ordinary.id
        after=until+timedelta(seconds=1)
        monkeypatch.setattr("src.guardian.opportunities.now",lambda:after)
        monkeypatch.setattr("src.observer.native_notification_queue._utc_now",lambda:after)
        claimed=await queue.claim_next(worker_id="actual-native-daemon",**caller_scope)
        assert claimed.id==notification_id and claimed.attempt_count==1 and claimed.fencing_token==1
        async with sessions() as db:
            intent=await db.get(NativeNotificationOutbox,notification_id)
            assert intent.deadline_at==deadline
            assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox).where(
                NativeNotificationOutbox.intervention_type=="opportunity"))).scalar()==1


@pytest.mark.parametrize("callback", ["poll", "handoff", "ack"])
async def test_actual_claim_then_inbox_dismiss_is_unknown_no_replay(accounting_db,real_auth,monkeypatch,callback):
    from src.guardian import inbox
    sessions,owner,row,notification_id,queue=await _actual_notification_intent(accounting_db,monkeypatch)
    caller_scope=dict(owner_principal_id=owner["principal_id"],operator_session_id=owner["session_id"])
    claimed=await queue.claim_next(worker_id="actual-native-daemon",**caller_scope)
    assert claimed.id==notification_id
    if callback=="ack":
        assert await queue.mark_display_attempted(notification_id,worker_id="actual-native-daemon",fencing_token=claimed.fencing_token,**caller_scope)
    detail=await inbox.get_owned_item(owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],item_id=row.id)
    await inbox.apply_action(owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],
        item_id=row.id,action="dismiss",expected_revision=detail["revision"],idempotency_key="claimed-dismiss")
    # A foreign/stale daemon fence cannot use the new authority guard to
    # mutate the current holder's claim, even after the Inbox is dismissed.
    for bad_worker,bad_fence in (("foreign-daemon",claimed.fencing_token),("actual-native-daemon",claimed.fencing_token+1)):
        assert await queue.mark_display_attempted(notification_id,worker_id=bad_worker,fencing_token=bad_fence,**caller_scope) is False
        assert await queue.ack(notification_id,worker_id=bad_worker,fencing_token=bad_fence,**caller_scope) is False
    async with sessions() as db:
        assert (await db.get(NativeNotificationOutbox,notification_id)).status==("display_attempted" if callback=="ack" else "claimed")
    if callback=="handoff":
        assert await queue.mark_display_attempted(notification_id,worker_id="actual-native-daemon",fencing_token=claimed.fencing_token,**caller_scope) is False
    elif callback=="ack":
        assert await queue.ack(notification_id,worker_id="actual-native-daemon",fencing_token=claimed.fencing_token,**caller_scope) is False
    else:
        assert await queue.claim_next(worker_id="actual-native-daemon",**caller_scope) is None
    async with sessions() as db:
        intent=await db.get(NativeNotificationOutbox,notification_id)
        assert intent.status=="unknown" and intent.attempt_count==1
    assert await queue.claim_next(worker_id="another-daemon",**caller_scope) is None


@pytest.mark.parametrize("change", ["source", "lineage"])
async def test_actual_intent_current_binding_denies_claim(accounting_db,real_auth,monkeypatch,change):
    from src.db.models import GuardianSourceBaseline
    sessions,owner,row,notification_id,queue=await _actual_notification_intent(accounting_db,monkeypatch)
    caller_scope=dict(owner_principal_id=owner["principal_id"],operator_session_id=owner["session_id"])
    # Negative canonical mutation invalidates actual prior verified lineage;
    # it does not insert a success, proof, outbox intent or feedback outcome.
    async with sessions() as db:
        if change=="source":
            baseline=(await db.execute(select(GuardianSourceBaseline).where(GuardianSourceBaseline.watch_id==row.watch_id))).scalar_one()
            baseline.baseline_sha256="0"*64
            db.add(baseline)
        else:
            intervention=await db.get(GuardianIntervention,row.intervention_id)
            intervention.original_root_id="foreign-root"
            db.add(intervention)
    assert await queue.claim_next(worker_id="actual-native-daemon",**caller_scope) is None
    async with sessions() as db:
        intent=await db.get(NativeNotificationOutbox,notification_id)
        assert intent.status=="cancelled" and intent.attempt_count==0
        assert intent.last_error==("source_stale" if change=="source" else "opportunity_notification_lineage_invalid")


async def _sql_ordering_many_ineligible_intents_cannot_starve_recovery(isolated_runtime,excluded):
    # Deliberately malformed SQL ordering-only fixture, NOT native opportunity
    # capability proof: future-snoozed/foreign intents must never be claimed or charged.
    from tests.test_work_board_m6_provider_free_journey import _goal
    from src.db.models import GuardianInboxDisposition
    sessions,_=isolated_runtime
    current=now()
    goal=_goal("ordering-only-goal","Ordering-only negative")
    async with sessions() as db:
        db.add(goal)
        for index in range(25):
            source_id=f"ordering-only-{index}"
            intervention_id="opportunity:"+source_id
            db.add(GuardianIntervention(id=intervention_id,intervention_type="opportunity",opportunity_id=source_id))
            db.add(GuardianInboxDisposition(id=source_id,source_id=source_id,source_kind="guardian_opportunity",
                owner_principal_id=OWNER,owner_session_id=SESSION,goal_id=goal.id,goal_revision=1,
                watch_id="ordering-only-watch",state="snoozed" if excluded=="future_snooze" else "pending",
                snoozed_until=current+timedelta(minutes=20) if excluded=="future_snooze" else None,
                expires_at=current+timedelta(hours=1)))
            db.add(NativeNotificationOutbox(id=source_id,idempotency_key=source_id,payload_digest="0"*64,
                intervention_id=intervention_id,intervention_type="opportunity",owner_principal_id=OWNER,
                operator_session_id=SESSION,goal_id=goal.id,goal_revision=1,title="Ordering only",body="Never display",
                urgency=4,created_at=current,deadline_at=current+timedelta(hours=1)))
    queue=NativeNotificationQueue()
    recovery=await queue.enqueue(intervention_id=None,title="Recovery",body="Existing recovery notice",
        intervention_type="recovery",urgency=1,idempotency_key="ordering-only-recovery")
    assert (await queue.claim_next(worker_id="recovery-daemon",owner_principal_id=OWNER,
        operator_session_id=SESSION if excluded=="future_snooze" else "foreign-root")).id==recovery.id
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox).where(
            NativeNotificationOutbox.intervention_type=="opportunity",NativeNotificationOutbox.status=="queued",
            NativeNotificationOutbox.attempt_count==0,NativeNotificationOutbox.fencing_token==0))).scalar()==25


async def test_sql_ordering_many_future_snoozes_cannot_starve_recovery(isolated_runtime):
    await _sql_ordering_many_ineligible_intents_cannot_starve_recovery(isolated_runtime,"future_snooze")


async def test_sql_ordering_many_foreign_root_intents_cannot_starve_recovery(isolated_runtime):
    await _sql_ordering_many_ineligible_intents_cannot_starve_recovery(isolated_runtime,"foreign_root")


async def test_actual_snoozed_unknown_never_reconciles_to_retry(accounting_db,real_auth,monkeypatch):
    from src.guardian import inbox
    sessions,owner,row,notification_id,queue=await _actual_notification_intent(accounting_db,monkeypatch)
    caller_scope=dict(owner_principal_id=owner["principal_id"],operator_session_id=owner["session_id"])
    claimed=await queue.claim_next(worker_id="actual-native-daemon",**caller_scope)
    assert claimed.id==notification_id
    detail=await inbox.get_owned_item(owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],item_id=row.id)
    until=now()+timedelta(minutes=20)
    result=await inbox.apply_action(owner_principal_id=owner["principal_id"],owner_session_id=owner["session_id"],
        item_id=row.id,action="snooze",expected_revision=detail["revision"],idempotency_key="unknown-snooze",until=until)
    assert result["state"]=="snoozed"
    assert await queue.claim_next(worker_id="actual-native-daemon",**caller_scope) is None
    async with sessions() as db:
        intent=await db.get(NativeNotificationOutbox,notification_id)
        assert intent.status=="unknown"
        original=(intent.id,intent.idempotency_key,intent.deadline_at,intent.attempt_count,intent.fencing_token,intent.updated_at)
    assert await queue.reconcile_unknown(notification_id,retry=True,owner_principal_id=owner["principal_id"],
        operator_session_id=owner["session_id"]) is False
    after=until+timedelta(seconds=1)
    monkeypatch.setattr("src.guardian.opportunities.now",lambda:after)
    monkeypatch.setattr("src.observer.native_notification_queue._utc_now",lambda:after)
    assert await queue.reconcile_unknown(notification_id,retry=True,owner_principal_id=owner["principal_id"],
        operator_session_id=owner["session_id"]) is False
    assert await queue.claim_next(worker_id="actual-native-daemon",**caller_scope) is None
    async with sessions() as db:
        intent=await db.get(NativeNotificationOutbox,notification_id)
        assert intent.status=="unknown"
        assert (intent.id,intent.idempotency_key,intent.deadline_at,intent.attempt_count,intent.fencing_token,intent.updated_at)==original
        assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox))).scalar()==1
