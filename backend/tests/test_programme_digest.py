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


@pytest.fixture
def digest_owner(programme_setup, monkeypatch):
    service, operator, goal, request, clock = programme_setup
    monkeypatch.setattr(digest, "goal_programme_service", service)
    monkeypatch.setattr("src.guardian.goal_programmes.goal_programme_service", service)
    monkeypatch.setattr("config.settings.settings.user_timezone", "Europe/Warsaw")
    clock[0] = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
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
async def test_disposition_replay_requires_exact_selected_goal_after_root_recovery(digest_owner, async_db):
    from dataclasses import replace
    from src.auth.service import _principal
    from src.auth import ownership
    from src.db.models import OperatorSession
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
                timezone="Europe/Warsaw", programme_ids=[], finding_ids=[finding], prepared_outputs=[], blocked_reasons=[]).model_dump_json(),
            finding_bindings_json=json.dumps([{"goal_id": goal.id, "job_id": "original-job", "finding_count": 1}])))
        db.add(ProgrammeFindingAction(owner_identity_id="identity-owned", finding_id=finding,
            idempotency_key=action.idempotency_key, request_digest=request_digest, local_date="2026-01-01",
            result_json=json.dumps(private_result)))
        root = OperatorSession(id="replay-fresh-root", token_hash="replay-fresh-token", principal_id="operator:root:replay1234",
            operator_identity_id="identity-owned", idle_expires_at=operator.idle_expires_at, absolute_expires_at=operator.absolute_expires_at)
        db.add(root)
    assert await digest.action(operator, finding, action, clock[0]) == private_result
    fresh = replace(operator, session_id=root.id, principal=_principal(root.id, root.principal_id), _token_hash=root.token_hash)
    with pytest.raises(GoalProgrammeError, match="programme_disposition_read_denied"):
        await digest.action(fresh, finding, action, clock[0])
    selections = [ownership.RecoverySelection(kind="goal", record_id=goal.id)]
    preview = await ownership.preview(fresh, ownership.RecoveryRequest(selections=selections))
    await ownership.confirm(fresh, ownership.RecoveryConfirmRequest(selections=selections,
        idempotency_key="select-original-goal", preview_digest=preview["preview_digest"], acknowledge_read_only=True))
    assert await digest.action(fresh, finding, action, clock[0]) == private_result
    async with async_db() as db:
        await db.delete(await db.get(Goal, goal.id))
    with pytest.raises(GoalProgrammeError, match="programme_disposition_read_denied"):
        await digest.action(fresh, finding, action, clock[0])
