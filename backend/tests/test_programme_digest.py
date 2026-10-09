"""Ordinary isolated clock, authority, persistence and delivery-cap checks."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_goal_programmes import programme_setup, deny_external_contacts, accept
from src.guardian import programme_digest as digest
from src.db.models import ProgrammeDigestReceipt, ProgrammeFindingAction, NativeNotificationOutbox, Goal


@pytest.mark.parametrize("age,accepted", [(timedelta(hours=48), True),
    (timedelta(hours=48, microseconds=1), False), (timedelta(microseconds=-1), False)])
def test_source_age_inclusive_boundary_and_future_denial(age, accepted):
    from src.guardian.goal_programmes import GoalProgrammeError
    observed = datetime(2026, 10, 9, 8, tzinfo=timezone.utc)
    witness = SimpleNamespace(artifacts={"source": {"kind": "snapshot",
        "parsed": SimpleNamespace(fetched_at=observed - age)}})
    if accepted:
        digest.assert_fresh_sources(witness, observed)
    else:
        with pytest.raises(GoalProgrammeError, match="programme_finding_refresh_required"):
            digest.assert_fresh_sources(witness, observed)
    with pytest.raises(GoalProgrammeError, match="programme_finding_refresh_required"):
        digest.assert_fresh_sources(SimpleNamespace(artifacts={}), observed)


@pytest.mark.asyncio
async def test_source_eligibility_uses_real_scheduler_daily_occurrence_and_native_hold(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from src.guardian.goal_discovery import run_goal_discovery_tick
    from src.scheduler import engine
    now = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
    programme = {"id": "original", "state": "active", "reason_code": None,
        "cadence": "daily", "expires_at": (now + timedelta(days=2)).isoformat()}
    monkeypatch.setattr(digest.settings, "scheduler_enabled", True)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_goal_discovery_tick, IntervalTrigger(seconds=60), id="goal_public_discovery",
        next_run_time=datetime.now(timezone.utc) + timedelta(days=1))
    monkeypatch.setattr(engine, "get_scheduler", lambda: scheduler)
    scheduler.start(paused=True)
    try:
        assert digest.source_schedule(programme, [], 10, now).next_source_reason == "scheduler_paused"
        scheduler.resume()
        eligible = digest.source_schedule(programme, [], 10, now)
        assert eligible.next_source_state == "eligible" and eligible.next_source_eligible_at == now
        completed = {"programme_id": "original", "occurrence_day": "2026-10-09",
            "status": "succeeded", "outstanding_held": False, "external_effect_state": "resolved",
            "accounting_liability": False}
        # Source cadence is UTC even when digest timezone crosses midnight/DST.
        for zone in ("Europe/Warsaw", "Pacific/Kiritimati", "America/New_York"):
            monkeypatch.setattr(digest.settings, "user_timezone", zone)
            scheduled = digest.source_schedule(programme, [completed], 10, now)
            assert scheduled.next_source_state == "scheduled"
            assert scheduled.next_source_eligible_at == datetime(2026, 10, 10, tzinfo=timezone.utc)
        held = {**completed, "programme_id": "prior-generation", "status": "queued", "outstanding_held": True}
        assert digest.source_schedule(programme, [held], 10, now).next_source_reason == "source_run_in_progress"
        for change in ({"external_effect_state": "unknown"}, {"accounting_liability": True}, {"status": "failed"}):
            blocked = digest.source_schedule(programme, [{**held, **change}], 10, now)
            assert blocked.next_source_state == "held" and blocked.next_source_eligible_at is None
            assert blocked.next_source_reason == "programme_outstanding_occurrence_requires_recovery"
        exhausted = digest.source_schedule(programme, [], 0, now)
        assert exhausted.next_source_reason == "programme_allowance_exhausted" and exhausted.next_source_eligible_at is None
        expired = digest.source_schedule({**programme, "expires_at": "2026-10-10T00:00:00+00:00"}, [completed], 10, now)
        assert expired.next_source_reason == "programme_window_expires_before_next_occurrence"
        assert digest.source_schedule({**programme, "state": "paused"}, [], 10, now).next_source_reason == "programme_inactive"
        assert digest.source_schedule({**programme, "state": "blocked", "reason_code": "programme_route_changed"}, [], 10, now).next_source_reason == "programme_authority_blocked"
        scheduler.pause_job("goal_public_discovery")
        assert digest.source_schedule(programme, [], 10, now).next_source_reason == "scheduler_job_unavailable"
        scheduler.remove_job("goal_public_discovery")
        assert digest.source_schedule(programme, [], 10, now).next_source_reason == "scheduler_job_unavailable"
        monkeypatch.setattr(engine, "get_scheduler", lambda: None)
        assert digest.source_schedule(programme, [], 10, now).next_source_reason == "scheduler_unavailable"
        monkeypatch.setattr(digest.settings, "scheduler_enabled", False)
        disabled = digest.source_schedule(programme, [], 10, now)
        assert disabled.next_source_state == "unavailable" and disabled.next_source_reason == "scheduler_disabled"
    finally:
        scheduler.shutdown(wait=False)


@pytest.fixture
async def digest_owner(programme_setup, async_db, monkeypatch):
    service, operator, goal, request, clock = programme_setup
    monkeypatch.setattr(digest, "goal_programme_service", service)
    monkeypatch.setattr("src.guardian.goal_programmes.goal_programme_service", service)
    monkeypatch.setattr("config.settings.settings.user_timezone", "Europe/Warsaw")
    clock[0] = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
    # The controlled 08:00 clock must lie within this fixture's finite Root
    # lifetime even when the ordinary test command runs just after midnight.
    from dataclasses import replace
    from src.db.models import OperatorSession
    expiry = max(datetime.now(timezone.utc), clock[0]) + timedelta(hours=24)
    async with async_db() as db:
        root = await db.get(OperatorSession, operator.session_id)
        root.idle_expires_at = root.absolute_expires_at = expiry
        db.add(root)
    operator = replace(operator, idle_expires_at=expiry, absolute_expires_at=expiry)
    return service, operator, goal, request, clock


@pytest.mark.asyncio
async def test_current_day_pending_finalization_restart_no_catchup_and_goal_delete(digest_owner, async_db):
    service, operator, goal, request, clock = digest_owner
    await accept(service, operator, goal, request)
    await digest.tick(clock[0])
    async with async_db() as db:
        rows = list((await db.execute(select(ProgrammeDigestReceipt))).scalars())
        assert len(rows) == 1 and rows[0].phase == "pending"
        original_id, original_deadline = rows[0].id, rows[0].finalize_deadline
    await digest.tick(clock[0] + timedelta(minutes=6))
    await digest.tick(clock[0] + timedelta(minutes=7))
    async with async_db() as db:
        rows = list((await db.execute(select(ProgrammeDigestReceipt))).scalars())
        assert len(rows) == 1 and rows[0].id == original_id and rows[0].phase == "finalized"
        assert rows[0].finalize_deadline == original_deadline
        assert "programme_no_completed_output" in json.loads(rows[0].digest_json)["blocked_reasons"]
        await db.delete(await db.get(Goal, goal.id))
    await digest.tick(clock[0] + timedelta(days=3))
    async with async_db() as db:
        assert len(list((await db.execute(select(ProgrammeDigestReceipt))).scalars())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["2026-03-29T06:00:00+00:00", "2026-10-25T07:00:00+00:00"])
async def test_dst_owner_day_identity_and_timezone_change(digest_owner, async_db, monkeypatch, start):
    service, operator, goal, request, clock = digest_owner
    clock[0] = datetime.fromisoformat(start)
    await accept(service, operator, goal, request)
    await digest.tick(clock[0])
    await digest.tick(clock[0] + timedelta(hours=1))
    async with async_db() as db:
        rows = list((await db.execute(select(ProgrammeDigestReceipt))).scalars())
        assert len(rows) == 1 and rows[0].timezone == "Europe/Warsaw"
        assert rows[0].local_date == start[:10]
    # Crossing the dateline in configuration cannot mint a second overlapping day.
    monkeypatch.setattr("config.settings.settings.user_timezone", "Pacific/Kiritimati")
    await digest.tick(clock[0] + timedelta(hours=5))
    async with async_db() as db:
        assert len(list((await db.execute(select(ProgrammeDigestReceipt))).scalars())) == 1


@pytest.mark.asyncio
async def test_owner_notice_cap_is_optin_targeted_and_ambiguous_consumes_slot(digest_owner, async_db, monkeypatch):
    service, operator, goal, request, clock = digest_owner
    request = request.model_copy(update={"notification_limits": {"per_day": 2}})
    from src.goals.contracts import GoalProgrammeNotifications
    request = request.model_copy(update={"notification_limits": GoalProgrammeNotifications(per_day=2)})
    await accept(service, operator, goal, request)
    await digest.tick(clock[0])
    await digest.tick(clock[0] + timedelta(minutes=6))
    async with async_db() as db:
        assert list((await db.execute(select(NativeNotificationOutbox))).scalars()) == []
    await digest.preferences(operator, digest.NotificationPreference(enabled=True, deadline_categories=["grants"]))
    await digest.deliver_notices(clock[0] + timedelta(minutes=7))
    await digest.deliver_notices(clock[0] + timedelta(minutes=8))
    async with async_db() as db:
        outbox = list((await db.execute(select(NativeNotificationOutbox))).scalars())
        assert len(outbox) == 1
        assert outbox[0].owner_principal_id == operator.principal.principal_id
        assert outbox[0].operator_session_id == operator.session_id and outbox[0].max_attempts == 1
        row = (await db.execute(select(ProgrammeDigestReceipt))).scalar_one()
        assert row.digest_notice == "enqueued"
        row.digest_notice = "unknown"
    await digest.deliver_notices(clock[0] + timedelta(minutes=9))
    async with async_db() as db:
        assert len(list((await db.execute(select(NativeNotificationOutbox))).scalars())) == 1
    status = await digest.notification_status(operator, clock[0])
    assert status["digest_slots_remaining"] == 0 and status["delivery_debt"] is True


def test_urgency_is_only_cited_source_deadline_within_48h():
    now = datetime.now(timezone.utc)
    source = "grants deadline: " + (now + timedelta(hours=24)).isoformat(timespec="seconds")
    snapshot = SimpleNamespace(lines=[source], fetched_at=now)
    citation = {"source_id": "source:0", "first_line": 1, "last_line": 1,
        "span_sha256": hashlib.sha256(source.encode()).hexdigest()}
    witness = SimpleNamespace(artifacts={"source": {"kind": "snapshot", "slot": 0, "parsed": snapshot}})
    brief = {"findings": [{"text": "URGENT invented deadline", "citations": [citation]}]}
    assert len(digest.deterministic_deadlines(witness, brief, now)) == 1
    assert digest.deterministic_deadlines(witness, {"findings": [{"text": source, "citations": []}]}, now) == []
    citation["span_sha256"] = "0" * 64
    assert digest.deterministic_deadlines(witness, brief, now) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_cross_programme_restart_concurrency_uses_one_owner_day(digest_owner, async_db):
    import asyncio
    from src.goals.repository import GoalRepository
    service, operator, goal, request, clock = digest_owner
    await accept(service, operator, goal, request)
    second_goal = await GoalRepository().create("Second private programme goal",
        owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    await accept(service, operator, second_goal, request)
    await asyncio.gather(digest.tick(clock[0]), digest.tick(clock[0]))
    await asyncio.gather(digest.tick(clock[0] + timedelta(minutes=6)), digest.tick(clock[0] + timedelta(minutes=6)))
    async with async_db() as db:
        rows = list((await db.execute(select(ProgrammeDigestReceipt))).scalars())
        assert len(rows) == 1 and len(json.loads(rows[0].digest_json)["programme_ids"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_native_optout_race_after_atomic_enqueue_cancels_claim(digest_owner, async_db, monkeypatch):
    import asyncio
    from src.goals.contracts import GoalProgrammeNotifications
    from src.observer.native_notification_queue import NativeNotificationQueue
    service, operator, goal, request, clock = digest_owner
    await accept(service, operator, goal, request.model_copy(update={"notification_limits": GoalProgrammeNotifications(per_day=2)}))
    await digest.tick(clock[0])
    await digest.tick(clock[0] + timedelta(minutes=6))
    await digest.preferences(operator, digest.NotificationPreference(enabled=True))
    original = NativeNotificationQueue.enqueue
    reserved = asyncio.Event()
    allow_insert = asyncio.Event()
    async def held_enqueue(queue, **kwargs):
        assert kwargs["_db"].in_transaction()
        reserved.set()
        await allow_insert.wait()
        return await original(queue, **kwargs)
    monkeypatch.setattr(NativeNotificationQueue, "enqueue", held_enqueue)
    delivery = asyncio.create_task(digest.deliver_notices(clock[0] + timedelta(minutes=7)))
    await asyncio.wait_for(reserved.wait(), 5)
    optout = asyncio.create_task(digest.preferences(operator, digest.NotificationPreference(enabled=False)))
    await asyncio.sleep(0.02)
    allow_insert.set()
    await asyncio.wait_for(asyncio.gather(delivery, optout), 5)
    monkeypatch.setattr("src.observer.native_notification_queue._utc_now", lambda: clock[0] + timedelta(minutes=8))
    assert await NativeNotificationQueue().claim_next(owner_principal_id=operator.principal.principal_id,
        operator_session_id=operator.session_id) is None
    async with async_db() as db:
        rows = list((await db.execute(select(NativeNotificationOutbox))).scalars())
        receipt = (await db.execute(select(ProgrammeDigestReceipt))).scalar_one()
        assert len(rows) == 1 and rows[0].status == "cancelled"
        assert rows[0].last_error == "programme_notifications_disabled"
        assert receipt.digest_notice == "enqueued"


@pytest.mark.asyncio
async def test_canonical_quiet_hours_defers_native_claim(digest_owner, async_db, monkeypatch):
    from src.goals.contracts import GoalProgrammeNotifications, GoalAdmissionBudget
    from src.observer.native_notification_queue import NativeNotificationQueue
    service, operator, goal, request, clock = digest_owner
    await accept(service, operator, goal, request.model_copy(update={"notification_limits": GoalProgrammeNotifications(per_day=1)}))
    await digest.tick(clock[0])
    await digest.tick(clock[0] + timedelta(minutes=6))
    await digest.preferences(operator, digest.NotificationPreference(enabled=True))
    await digest.deliver_notices(clock[0] + timedelta(minutes=7))
    async with async_db() as db:
        stored = await db.get(Goal, goal.id)
        stored.admission_budget_json = GoalAdmissionBudget(quiet_hours_start=9, quiet_hours_end=12,
            timezone="Europe/Warsaw").model_dump_json()
        db.add(stored)
    monkeypatch.setattr("src.observer.native_notification_queue._utc_now", lambda: clock[0] + timedelta(minutes=8))
    assert await NativeNotificationQueue().claim_next(owner_principal_id=operator.principal.principal_id,
        operator_session_id=operator.session_id) is None
    async with async_db() as db:
        row = (await db.execute(select(NativeNotificationOutbox))).scalar_one()
        assert row.status == "queued" and row.attempt_count == 0
    status = await digest.notification_status(operator, clock[0])
    assert status["quiet_hours_active"] is True


@pytest.mark.asyncio
async def test_receipt_capacity_and_encoded_request_limit_deny_before_task_prepare(digest_owner, async_db, monkeypatch):
    from src.guardian.goal_programmes import GoalProgrammeError
    service, operator, goal, request, clock = digest_owner
    async with async_db() as db:
        for index in range(128):
            db.add(ProgrammeFindingAction(owner_identity_id="identity-owned", finding_id="f" * 64,
                idempotency_key=f"retained-{index}", request_digest="a" * 64,
                local_date=digest.local_clock(clock[0]).date().isoformat(), result_json='{"retained":true}'))
    async def no_preparation(*args, **kwargs):
        raise AssertionError("capacity denial must precede task preparation")
    monkeypatch.setattr(digest, "prepare_task", no_preparation)
    with pytest.raises(GoalProgrammeError, match="programme_action_capacity_requires_review"):
        await digest.action(operator, "f" * 64, digest.FindingAction(action="accept_followup", idempotency_key="fresh-card"), clock[0])
    with pytest.raises(GoalProgrammeError, match="programme_action_receipt_capacity_requires_review"):
        await digest.action(operator, "f" * 64, digest.FindingAction(action="accept_followup", idempotency_key="oversize",
            desired_outcome="😀" * 1500), clock[0])
    async with async_db() as db:
        assert len(list((await db.execute(select(ProgrammeFindingAction))).scalars())) == 128


@pytest.mark.asyncio
async def test_invalid_timezone_and_malformed_quiet_budget_fail_closed(digest_owner, async_db, monkeypatch):
    from src.guardian.goal_programmes import GoalProgrammeError
    from src.goals.contracts import GoalProgrammeNotifications
    service, operator, goal, request, clock = digest_owner
    await accept(service, operator, goal, request.model_copy(update={"notification_limits": GoalProgrammeNotifications(per_day=1)}))
    await digest.preferences(operator, digest.NotificationPreference(enabled=True))
    async with async_db() as db:
        stored = await db.get(Goal, goal.id)
        stored.admission_budget_json = '{"quiet_hours_start":99,"quiet_hours_end":1}'
        db.add(stored)
    async with async_db() as db:
        quiet, allowance = await digest.owner_delivery_policy(db, "identity-owned", clock[0])
        assert quiet and allowance == 0
    monkeypatch.setattr("config.settings.settings.user_timezone", "not-an-iana-zone")
    with pytest.raises(GoalProgrammeError, match="programme_timezone_invalid"):
        await digest.tick(clock[0])
    async with async_db() as db:
        assert list((await db.execute(select(ProgrammeDigestReceipt))).scalars()) == []


@pytest.mark.asyncio
async def test_identity_revoke_cancels_unclaimed_notices(digest_owner, async_db, monkeypatch):
    from src.goals.contracts import GoalProgrammeNotifications
    from src.db.models import OperatorIdentity, OperatorSession
    from src.observer.native_notification_queue import NativeNotificationQueue
    service, operator, goal, request, clock = digest_owner
    await accept(service, operator, goal, request.model_copy(update={"notification_limits": GoalProgrammeNotifications(per_day=1)}))
    await digest.tick(clock[0])
    await digest.tick(clock[0] + timedelta(minutes=6))
    await digest.preferences(operator, digest.NotificationPreference(enabled=True))
    await digest.deliver_notices(clock[0] + timedelta(minutes=7))
    async with async_db() as db:
        identity = await db.get(OperatorIdentity, "identity-owned")
        identity.revoked_at = clock[0]
        db.add(identity)
    monkeypatch.setattr("src.observer.native_notification_queue._utc_now", lambda: clock[0] + timedelta(minutes=8))
    assert await NativeNotificationQueue().claim_next(owner_principal_id=operator.principal.principal_id,
        operator_session_id=operator.session_id) is None
    async with async_db() as db:
        row = (await db.execute(select(NativeNotificationOutbox))).scalar_one()
        assert row.status == "cancelled" and row.last_error == "programme_notifications_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["delete", "supersede", "correct"])
async def test_original_programme_authority_cancels_even_without_finding_bindings(digest_owner, async_db, monkeypatch, mutation):
    from src.goals.contracts import GoalProgrammeNotifications
    from src.goals.repository import GoalRepository
    from src.observer.native_notification_queue import NativeNotificationQueue
    service, operator, goal, request, clock = digest_owner
    notified = request.model_copy(update={"notification_limits": GoalProgrammeNotifications(per_day=1)})
    await accept(service, operator, goal, notified)
    second_goal = await GoalRepository().create("Other active programme retains allowance",
        owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    await accept(service, operator, second_goal, notified)
    await digest.tick(clock[0])
    await digest.tick(clock[0] + timedelta(minutes=6))
    await digest.preferences(operator, digest.NotificationPreference(enabled=True))
    await digest.deliver_notices(clock[0] + timedelta(minutes=7))
    async with async_db() as db:
        receipt = (await db.execute(select(ProgrammeDigestReceipt))).scalar_one()
        assert json.loads(receipt.finding_bindings_json) == []
        stored = await db.get(Goal, goal.id)
        if mutation == "delete":
            await db.delete(stored)
        elif mutation == "correct":
            stored.revision += 1
            db.add(stored)
        else:
            state = json.loads(stored.goal_programmes_json)
            state["generations"][-1]["state"] = "paused"
            state["generations"][-1]["reason_code"] = "programme_superseded"
            stored.goal_programmes_json = json.dumps(state)
            db.add(stored)
    monkeypatch.setattr("src.observer.native_notification_queue._utc_now", lambda: clock[0] + timedelta(minutes=8))
    assert await NativeNotificationQueue().claim_next(owner_principal_id=operator.principal.principal_id,
        operator_session_id=operator.session_id) is None
    async with async_db() as db:
        row = (await db.execute(select(NativeNotificationOutbox))).scalar_one()
        assert row.status == "cancelled" and row.last_error == "programme_notice_original_authority_changed"
        assert (await db.execute(select(ProgrammeDigestReceipt))).scalar_one().digest_notice == "enqueued"


@pytest.mark.asyncio
async def test_disposition_replay_requires_exact_selected_goal_after_root_recovery(digest_owner, async_db, monkeypatch):
    from dataclasses import replace
    from src.auth.service import _principal, _token_hash, authenticate_token
    from src.auth import ownership
    from src.db.models import OperatorSession, ProgrammeFollowThrough
    from src.guardian.goal_programmes import GoalProgrammeError
    service, operator, goal, request, clock = digest_owner
    finding = digest.finding_id("original-job", 0)
    action = digest.FindingAction(action="dismiss", idempotency_key="private-existing-disposition")
    request_digest = hashlib.sha256(json.dumps({"finding_id": finding,
        "request": action.model_dump(mode="json")}, sort_keys=True).encode()).hexdigest()
    private_result = {"finding_id": finding, "desired_outcome": "PRIVATE_INTENT", "task_id": "PRIVATE_TASK"}
    async with async_db() as db:
        db.add(ProgrammeDigestReceipt(owner_identity_id="identity-owned", local_date="2026-01-01",
            timezone="Europe/Warsaw", phase="finalized", finalize_deadline=clock[0], digest_json=digest.ProgrammeDigestV1(local_date="2026-01-01",
                timezone="Europe/Warsaw", programme_ids=["original-programme"], finding_ids=[finding],
                prepared_outputs=[{"artifact_id": "PRIVATE_ARTIFACT", "digest": "a" * 64, "schema_version": 1}], blocked_reasons=[]).model_dump_json(),
            finding_bindings_json=json.dumps([{"goal_id": goal.id, "programme_id": "original-programme",
                "job_id": "original-job", "finding_count": 1, "goal_revision": goal.revision, "grant_revision": 1}])))
        db.add(ProgrammeFollowThrough(owner_identity_id="identity-owned", finding_id=finding,
            desired_outcome="PRIVATE_INTENT", task_proposal_id="PRIVATE_TASK", status="prepared"))
        db.add(ProgrammeFindingAction(owner_identity_id="identity-owned", finding_id=finding,
            idempotency_key=action.idempotency_key, request_digest=request_digest, local_date="2026-01-01",
            result_json=json.dumps(private_result)))
        root = OperatorSession(id="replay-fresh-root", token_hash=_token_hash("replay-fresh-token"), principal_id="operator:root:replay1234",
            operator_identity_id="identity-owned", idle_expires_at=operator.idle_expires_at, absolute_expires_at=operator.absolute_expires_at)
        db.add(root)
    assert await digest.action(operator, finding, action, clock[0]) == private_result
    fresh = await authenticate_token("replay-fresh-token", touch=False)
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.guardian_inbox import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    @app.middleware("http")
    async def authenticated_request(request, call_next):
        request.state.operator = await authenticate_token(request.headers["Authorization"].removeprefix("Bearer "), touch=False)
        return await call_next(request)
    async def get_payload():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/guardian/inbox/programme-digests", headers={"Authorization": "Bearer replay-fresh-token"})
            assert response.status_code == 200
            return response.json()
    with pytest.raises(GoalProgrammeError, match="programme_disposition_read_denied"):
        await digest.action(fresh, finding, action, clock[0])
    unselected = await get_payload()
    for private_value in ("PRIVATE_INTENT", "PRIVATE_TASK", "PRIVATE_ARTIFACT", goal.id,
        "original-programme", "original-job", finding):
        assert private_value not in json.dumps(unselected)
    assert unselected["digests"] == [] and unselected["programmes"] == []
    selections = [ownership.RecoverySelection(kind="goal", record_id=goal.id)]
    preview = await ownership.preview(fresh, ownership.RecoveryRequest(selections=selections))
    await ownership.confirm(fresh, ownership.RecoveryConfirmRequest(selections=selections,
        idempotency_key="select-original-goal", preview_digest=preview["preview_digest"], acknowledge_read_only=True))
    assert await digest.action(fresh, finding, action, clock[0]) == private_result
    selected_payload = await get_payload()
    selected = selected_payload["digests"][0]["findings"][0]
    assert selected["follow_through"]["desired_outcome"] == "PRIVATE_INTENT" and selected["task_id"] == "PRIVATE_TASK"
    assert selected["goal_id"] == goal.id and selected["programme_id"] == "original-programme"
    # A metadata-authorized fallback never launders an unverified stored artifact.
    assert selected_payload["digests"][0]["digest"]["prepared_outputs"] == []
    for new_action in (digest.FindingAction(action="dismiss", idempotency_key="recovered-dismiss"),
        digest.FindingAction(action="snooze", until=clock[0] + timedelta(days=1), idempotency_key="recovered-snooze")):
        with pytest.raises(GoalProgrammeError, match="programme_finding_refresh_required"):
            await digest.action(fresh, finding, new_action, clock[0])
    async def deleted_during_readback(*args, **kwargs):
        async with async_db() as db:
            await db.delete(await db.get(Goal, goal.id))
        raise PermissionError("Deleted during asynchronous source read")
    monkeypatch.setattr(digest, "_read_binding", deleted_during_readback)
    deleted = await get_payload()
    with pytest.raises(GoalProgrammeError, match="programme_disposition_read_denied"):
        await digest.action(fresh, finding, action, clock[0])
    for private_value in ("PRIVATE_INTENT", "PRIVATE_TASK", "PRIVATE_ARTIFACT", goal.id,
        "original-programme", "original-job", finding):
        assert private_value not in json.dumps(deleted)
    assert deleted["digests"] == [] and deleted["programmes"] == []
    from src.auth.service import AuthFailure
    async with async_db() as db:
        revoked = await db.get(OperatorSession, root.id)
        revoked.revoked_at = datetime.now(timezone.utc)
        db.add(revoked)
    for denied_action in (digest.FindingAction(action="dismiss", idempotency_key="revoked-dismiss"),
        digest.FindingAction(action="snooze", until=clock[0] + timedelta(days=1), idempotency_key="revoked-snooze")):
        with pytest.raises(AuthFailure):
            await digest.action(fresh, finding, denied_action, clock[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("current_status", ["accepted", "queued", "running", "unknown"])
async def test_current_occurrence_unresolved_never_stages_history_and_finalized_tick_reads_none(digest_owner, async_db, monkeypatch, current_status):
    from uuid import uuid5, NAMESPACE_URL
    from src.db.models import WorkflowRunState
    from src.guardian.goal_discovery import DISCOVERY_KIND
    service, operator, goal, request, clock = digest_owner
    programme = await accept(service, operator, goal, request)
    day = clock[0].astimezone(timezone.utc).date().isoformat()
    current_id = "goal-discovery:" + uuid5(NAMESPACE_URL,
        f"seraph:public-discovery:identity-owned:{programme['id']}:{day}").hex
    async with async_db() as db:
        for index in range(100):
            db.add(WorkflowRunState(run_identity=f"obsolete-discovery-{index}", root_run_identity=f"obsolete-discovery-{index}",
                workflow_name=DISCOVERY_KIND, job_kind=DISCOVERY_KIND, status="succeeded",
                declared_authority_json="obsolete-malformed-history-must-not-be-parsed"))
        db.add(WorkflowRunState(run_identity=current_id, root_run_identity=current_id,
            workflow_name=DISCOVERY_KIND, job_kind=DISCOVERY_KIND, status=current_status))
    physical_reads = []
    async def deny_physical(*args, **kwargs):
        physical_reads.append(args)
        raise AssertionError("Unresolved/current-only shortlist must never stage older physical output")
    monkeypatch.setattr("src.workflows.research_sources.physical_discovery_inputs", deny_physical)
    await digest.tick(clock[0])
    await digest.tick(clock[0] + timedelta(minutes=6))
    async with async_db() as db:
        receipt = (await db.execute(select(ProgrammeDigestReceipt))).scalar_one()
        output = digest.ProgrammeDigestV1.model_validate_json(receipt.digest_json)
        assert receipt.phase == "finalized"
        assert "programme_current_output_unresolved" in output.blocked_reasons
        assert output.finding_ids == [] and output.prepared_outputs == []
    await digest.tick(clock[0] + timedelta(minutes=7))
    await digest.tick(clock[0] + timedelta(minutes=8))
    assert physical_reads == []


async def negative_deadline_pointer(digest_owner, async_db):
    """Malformed source metadata only tests denial BEFORE any physical read."""
    from src.goals.contracts import GoalProgrammeNotifications
    service, operator, goal, request, clock = digest_owner
    programme = await accept(service, operator, goal, request.model_copy(update={"notification_limits": GoalProgrammeNotifications(per_day=2)}))
    await digest.tick(clock[0])
    await digest.tick(clock[0] + timedelta(minutes=6))
    async with async_db() as db:
        receipt = (await db.execute(select(ProgrammeDigestReceipt))).scalar_one()
        receipt.finding_bindings_json = json.dumps([{"goal_id": goal.id, "programme_id": programme["id"],
            "goal_revision": goal.revision, "grant_revision": programme["grant_revision"],
            "job_id": "MUST_NOT_REOPEN_UNAUTHORIZED_METADATA", "finding_count": 0,
            "deadline_evidence": [{"due_at": (clock[0] + timedelta(hours=24)).isoformat(), "source_id": "source:0", "span_sha256": "f" * 64}]}])
        db.add(receipt)
    return operator, goal, clock[0] + timedelta(minutes=7)


@pytest.mark.asyncio
async def test_default_disabled_empty_category_and_reserved_deadline_skip_physical_staging(digest_owner, async_db, monkeypatch):
    operator, goal, now = await negative_deadline_pointer(digest_owner, async_db)
    async def no_physical(*args, **kwargs):
        raise AssertionError("Consent/category/reservation metadata must precede physical source staging")
    monkeypatch.setattr("src.workflows.research_sources.physical_discovery_inputs", no_physical)
    await digest.tick(now)  # Default Inbox, finalized owner.
    await digest.preferences(operator, digest.NotificationPreference(enabled=False, deadline_categories=["grants"]))
    await digest.tick(now + timedelta(minutes=1))
    await digest.preferences(operator, digest.NotificationPreference(enabled=True, deadline_categories=[]))
    await digest.tick(now + timedelta(minutes=2))
    await digest.preferences(operator, digest.NotificationPreference(enabled=True, deadline_categories=["grants"]))
    async with async_db() as db:
        receipt = (await db.execute(select(ProgrammeDigestReceipt))).scalar_one()
        receipt.deadline_notice = "unknown"
        db.add(receipt)
    await digest.tick(now + timedelta(minutes=3))


@pytest.mark.asyncio
async def test_quiet_then_revoked_recipient_skip_physical_deadline_staging(digest_owner, async_db, monkeypatch):
    from src.goals.contracts import GoalAdmissionBudget
    from src.db.models import OperatorSession
    operator, goal, now = await negative_deadline_pointer(digest_owner, async_db)
    await digest.preferences(operator, digest.NotificationPreference(enabled=True, deadline_categories=["grants"]))
    async def no_physical(*args, **kwargs):
        raise AssertionError("Quiet/revoked recipient metadata must precede physical source staging")
    monkeypatch.setattr("src.workflows.research_sources.physical_discovery_inputs", no_physical)
    async with async_db() as db:
        stored = await db.get(Goal, goal.id)
        stored.admission_budget_json = GoalAdmissionBudget(quiet_hours_start=9, quiet_hours_end=12,
            timezone="Europe/Warsaw").model_dump_json()
        db.add(stored)
    await digest.tick(now)
    async with async_db() as db:
        stored = await db.get(Goal, goal.id)
        stored.admission_budget_json = GoalAdmissionBudget().model_dump_json()
        db.add(stored)
        root = await db.get(OperatorSession, operator.session_id)
        root.revoked_at = datetime.now(timezone.utc)
        db.add(root)
    await digest.tick(now + timedelta(minutes=1))


@pytest.mark.asyncio
async def test_deadline_read_failure_negative_memo_skips_repeated_fs_and_rechecks_changed_preferences(digest_owner, async_db, monkeypatch):
    """Negative metadata fixture proves no success or authority from a cache."""
    from src.db.models import WorkflowRunState, OperatorSession
    operator, goal, now = await negative_deadline_pointer(digest_owner, async_db)
    async with async_db() as db:
        db.add(WorkflowRunState(run_identity="MUST_NOT_REOPEN_UNAUTHORIZED_METADATA",
            root_run_identity="negative-source", workflow_name="negative-source", status="succeeded"))
        from src.auth.service import _token_hash
        root = await db.get(OperatorSession, operator.session_id)
        root.token_hash = _token_hash("negative-auth-token")
        root.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        root.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.add(root)
    await digest.preferences(operator, digest.NotificationPreference(enabled=True, deadline_categories=["nonmatching"]))
    reads = []
    async def unreadable(*args, **kwargs):
        reads.append(args[1])
        raise PermissionError("Private physical bytes changed; never publish this exception")
    monkeypatch.setattr("src.workflows.research_sources.physical_discovery_inputs", unreadable)
    await digest.deliver_notices(now)
    assert len(reads) == 1
    async with async_db() as db:
        receipt = (await db.execute(select(ProgrammeDigestReceipt))).scalar_one()
        memo = json.loads(receipt.finding_bindings_json)[0]["deadline_no_match"]
        assert memo["reason"] == "deadline_source_requires_current_readback"
        assert len(json.dumps(memo).encode()) <= 160
        assert receipt.deadline_notice == "unreserved"
    from src.auth.service import authenticate_token
    # Authenticate with the same explicitly bounded lifetime as the 08:00
    # fixture; touching must not expire it before the controlled delivery clock.
    monkeypatch.setattr("config.settings.settings.operator_auth_idle_seconds", 24 * 3600)
    async with async_db() as db:
        before_touch = (await db.get(OperatorSession, operator.session_id)).last_seen_at
    await authenticate_token("negative-auth-token", touch=True)
    async with async_db() as db:
        assert (await db.get(OperatorSession, operator.session_id)).last_seen_at != before_touch
    await digest.deliver_notices(now + timedelta(minutes=1))
    assert len(reads) == 1
    await digest.preferences(operator, digest.NotificationPreference(enabled=True, deadline_categories=["grants"]))
    await digest.deliver_notices(now + timedelta(minutes=2))
    assert len(reads) == 2  # Changed categories physically recheck; failure grants nothing.
    async with async_db() as db:
        run = (await db.execute(select(WorkflowRunState))).scalar_one()
        run.input_digest = "b" * 64  # Original metadata change invalidates a negative decision.
        db.add(run)
    await digest.deliver_notices(now + timedelta(minutes=3))
    assert len(reads) == 3
    async with async_db() as db:
        root = await db.get(OperatorSession, operator.session_id)
        root.revoked_at = datetime.now(timezone.utc)
        db.add(root)
    await digest.deliver_notices(now + timedelta(minutes=4))
    assert len(reads) == 3
    async with async_db() as db:
        receipt = (await db.execute(select(ProgrammeDigestReceipt))).scalar_one()
        assert receipt.deadline_notice == "unreserved"
        assert all(row.intervention_type != "programme_deadline"
            for row in (await db.execute(select(NativeNotificationOutbox))).scalars())
