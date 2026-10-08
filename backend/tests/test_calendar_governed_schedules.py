"""Canonical database tests for the governed Calendar observation seam."""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from src.db.models import (
    CalendarReadConsent,
    Goal,
    GovernedScheduleBinding,
    GovernedScheduleOccurrence,
    GoogleServiceConnection,
    OperatorSession,
    Secret,
    ScheduledJob,
    ScheduledJobRun,
    WorkBoardInputArtifact,
    WorkBoardTask,
)
from src.security.http_transport import PinnedResponse
from src.scheduler.scheduled_jobs import execute_scheduled_job
from config.settings import settings
from src.vault import crypto as vault_crypto
from src.vault import encrypt
from src.scheduler.governed_schedules import (
    action_spec,
    apply_control,
    claim_occurrence,
    latest_due_slot,
    normalize_cadence,
    reconcile_occurrence,
    reserve_occurrence,
    settle_occurrence,
    write_server_cleanup_proof,
)
from src.work_board.contracts import WorkBoardOwner


OWNER = "operator:scheduler-tests"
SESSION = "scheduler-test-session"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def reset_calendar_vault_cipher(monkeypatch):
    """Bind test ciphertext and readonly redaction to one fresh key per test."""

    monkeypatch.setattr(settings, "vault_encryption_key", Fernet.generate_key().decode())
    vault_crypto._fernet = None
    yield
    vault_crypto._fernet = None


class _SchedulerProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.event = {
            "id": "provider-event-1",
            "etag": '"event-etag-1"',
            "updated": "2026-09-30T08:00:00Z",
            "summary": "Operator planning review",
            "start": {"dateTime": "2026-09-30T13:00:00Z"},
            "end": {"dateTime": "2026-09-30T14:00:00Z"},
            "location": "Room 4",
        }

    async def __call__(self, url: str, **kwargs: Any) -> PinnedResponse:
        method = str(kwargs.get("method") or "GET").upper()
        self.calls.append((method, url))
        if url == "https://oauth2.googleapis.com/token" and method == "POST":
            payload: dict[str, Any] = {"access_token": "scheduler-test-access-token"}
        elif "/calendar/v3/users/me/calendarList" in url and method == "GET":
            payload = {
                "etag": '"calendar-list-etag-1"',
                "items": [{"id": "owner-calendar", "summary": "Operator calendar"}],
            }
        elif "/calendar/v3/calendars/owner-calendar/events" in url and method == "GET":
            payload = {"etag": '"events-etag-1"', "items": [self.event]}
        else:
            raise AssertionError(f"unexpected scheduler provider request: {method} {url}")
        return PinnedResponse(
            url=url,
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            pinned_address="8.8.8.8",
        )


async def _seed_binding(async_db, *, binding_id: str = "binding-1", owner: str = OWNER, session: str = SESSION):
    async with async_db() as db:
        job = ScheduledJob(
            id=f"job-{binding_id}",
            name="Calendar observation",
            enabled=True,
            trigger_type="governed",
            trigger_spec_json=json.dumps(
                {"kind": "hourly", "timezone": "UTC", "daily_hour": None, "daily_minute": None}
            ),
            action_type="calendar.observe_due_events.v1",
            action_spec_json=json.dumps({"binding_id": binding_id}),
        )
        db.add(job)
        await db.flush()
        binding = GovernedScheduleBinding(
            binding_id=binding_id,
            scheduled_job_id=job.id,
            owner_principal_id=owner,
            owner_session_id=session,
            goal_id="goal-1",
            goal_revision=1,
            capability_id="calendar.observe_due_events.v1",
            action_type="calendar.observe_due_events.v1",
            input_artifact_id=f"artifact-{binding_id}",
            input_digest="a" * 64,
            action_digest="b" * 64,
            consent_kind="calendar_read",
            read_consent_id=f"consent-{binding_id}",
            consent_revision=1,
            consent_digest="c" * 64,
            schedule_idempotency_key=f"key-{binding_id}",
            schedule_request_digest="d" * 64,
            cadence_kind="hourly",
            timezone="UTC",
            binding_revision=1,
            expires_at=NOW + timedelta(days=1),
            state="active",
        )
        db.add(binding)
        await db.flush()
    return binding_id


async def _seed_control_binding(async_db) -> None:
    await _seed_binding(async_db)
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(
            OperatorSession(
                id=SESSION,
                token_hash="scheduler-control-session-token-hash",
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=2),
            )
        )
        await db.flush()


@pytest.mark.asyncio
async def test_schedule_control_receipts_replay_across_binding_revisions(async_db):
    """Controls use immutable run receipts rather than one mutable binding cache."""

    await _seed_control_binding(async_db)
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with async_db() as db:
        first = await apply_control(db, owner, "binding-1", "pause", 1, "control-a")
        second = await apply_control(db, owner, "binding-1", "resume", 2, "control-b")
        replay = await apply_control(db, owner, "binding-1", "pause", 1, "control-a")

        assert first["state"] == "paused"
        assert second["state"] == "active"
        assert replay == first
        current = await db.get(GovernedScheduleBinding, "binding-1")
        assert current is not None
        assert current.state == "active"
        assert current.binding_revision == 3
        receipts = (
            await db.execute(
                select(ScheduledJobRun).where(
                    ScheduledJobRun.scheduled_job_id == "job-binding-1",
                    ScheduledJobRun.action_type == "calendar.governed_schedule_control.v1",
                )
            )
        ).scalars().all()
        assert len(receipts) == 2
        for receipt in receipts:
            metadata = json.loads(receipt.metadata_json or "{}")
            assert metadata["control"]["scheduled_job_id"] == "job-binding-1"
            assert metadata["control"]["owner_session_id"] == SESSION
            assert "calendar_id" not in receipt.metadata_json
            assert "refresh_token" not in receipt.metadata_json


@pytest.mark.asyncio
async def test_schedule_control_changed_digest_conflicts_and_revoked_session_cannot_mutate(async_db):
    await _seed_control_binding(async_db)
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with async_db() as db:
        await apply_control(db, owner, "binding-1", "pause", 1, "control-a")
        with pytest.raises(RuntimeError, match="idempotency_conflict"):
            await apply_control(db, owner, "binding-1", "pause", 1, "control-a", "changed reason")

        session = await db.get(OperatorSession, SESSION)
        assert session is not None
        session.revoked_at = datetime.now(timezone.utc)
        await db.flush()
        with pytest.raises(RuntimeError, match="session_unavailable"):
            await apply_control(db, owner, "binding-1", "resume", 2, "control-b")

        current = await db.get(GovernedScheduleBinding, "binding-1")
        assert current is not None
        assert current.state == "paused"
        assert current.binding_revision == 2


@pytest.mark.asyncio
async def test_schedule_control_corrupt_receipt_fails_closed(async_db):
    await _seed_control_binding(async_db)
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with async_db() as db:
        await apply_control(db, owner, "binding-1", "pause", 1, "control-corrupt")
        receipt_id = (
            await db.execute(
                select(ScheduledJobRun.id).where(
                    ScheduledJobRun.scheduled_job_id == "job-binding-1",
                    ScheduledJobRun.action_type == "calendar.governed_schedule_control.v1",
                )
            )
        ).scalar_one()
        receipt = await db.get(ScheduledJobRun, receipt_id)
        assert receipt is not None
        metadata = json.loads(receipt.metadata_json or "{}")
        metadata["response"]["untrusted"] = "must not be projected"
        receipt.metadata_json = json.dumps(metadata, separators=(",", ":"))
        await db.flush()
        with pytest.raises(RuntimeError, match="receipt_invalid"):
            await apply_control(db, owner, "binding-1", "pause", 1, "control-corrupt")


@pytest.mark.asyncio
async def test_schedule_control_concurrent_same_key_has_one_canonical_receipt(tmp_path):
    """A SQLite writer race returns one identical durable response to both callers."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'scheduler-control-race.sqlite'}",
        connect_args={"timeout": 5},
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with factory() as db:
            db.add_all(
                [
                    ScheduledJob(
                        id="job-control-race",
                        name="Calendar observation",
                        enabled=True,
                        trigger_type="governed",
                        trigger_spec_json=json.dumps(
                            {"kind": "hourly", "timezone": "UTC", "daily_hour": None, "daily_minute": None}
                        ),
                        action_type="calendar.observe_due_events.v1",
                        action_spec_json=json.dumps({"binding_id": "binding-control-race"}),
                    ),
                    GovernedScheduleBinding(
                        binding_id="binding-control-race",
                        scheduled_job_id="job-control-race",
                        owner_principal_id=OWNER,
                        owner_session_id=SESSION,
                        goal_id="goal-control-race",
                        input_artifact_id="artifact-control-race",
                        input_digest="a" * 64,
                        action_digest="b" * 64,
                        read_consent_id="consent-control-race",
                        expires_at=now + timedelta(days=1),
                        cadence_kind="hourly",
                        timezone="UTC",
                    ),
                    OperatorSession(
                        id=SESSION,
                        token_hash="scheduler-control-race-token-hash",
                        idle_expires_at=now + timedelta(hours=1),
                        absolute_expires_at=now + timedelta(hours=2),
                    ),
                ]
            )
            await db.commit()

        owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)

        async def worker() -> dict[str, Any]:
            async with factory() as db:
                result = await apply_control(
                    db,
                    owner,
                    "binding-control-race",
                    "pause",
                    1,
                    "control-race",
                )
                await db.commit()
                return result

        first, second = await asyncio.gather(worker(), worker())
        assert first == second
        async with factory() as db:
            receipts = (
                await db.execute(
                    select(ScheduledJobRun).where(
                        ScheduledJobRun.scheduled_job_id == "job-control-race",
                        ScheduledJobRun.action_type == "calendar.governed_schedule_control.v1",
                    )
                )
            ).scalars().all()
            assert len(receipts) == 1
            current = await db.get(GovernedScheduleBinding, "binding-control-race")
            assert current is not None
            assert current.state == "paused"
            assert current.binding_revision == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_run_summary_preserves_governed_cleanup_proof(async_db):
    """A late run summary cannot erase the server-owned recovery proof."""

    async with async_db() as db:
        db.add(
            ScheduledJobRun(
                id="governed-run-summary",
                scheduled_job_id="governed-job-summary",
                action_type="calendar.observe_due_events.v1",
                metadata_json=json.dumps(
                    {
                        "governed_occurrence_id": "occurrence-1",
                        "governed_cleanup_proof": {
                            "status": "verified",
                            "run_id": "governed-run-summary",
                        },
                    }
                ),
            )
        )
        await db.flush()

    from src.scheduler.scheduled_jobs import scheduled_job_repository

    await scheduled_job_repository.finish_run(
        "governed-run-summary",
        outcome="blocked",
        status="blocked",
        metadata={"event_count": 0},
    )

    async with async_db() as db:
        run = await db.get(ScheduledJobRun, "governed-run-summary")
        assert run is not None
        metadata = json.loads(run.metadata_json or "{}")
        assert metadata["event_count"] == 0
        assert metadata["governed_occurrence_id"] == "occurrence-1"
        assert metadata["governed_cleanup_proof"]["status"] == "verified"


def test_registry_and_cadence_are_closed_world() -> None:
    assert action_spec("calendar.observe_due_events.v1")["model"] is False
    assert action_spec("guardian.run_procedure.v2") == {
        "capability_id": "guardian.run_procedure.v2",
        "consent_kind": "goal_budget",
        "model": False,
        "enabled": True,
    }
    with pytest.raises(ValueError):
        normalize_cadence({"kind": "cron", "timezone": "UTC", "daily_hour": None, "daily_minute": None})
    assert normalize_cadence(
        {"kind": "daily", "timezone": "Europe/Warsaw", "daily_hour": 8, "daily_minute": 30}
    ) == {"kind": "daily", "timezone": "Europe/Warsaw", "daily_hour": 8, "daily_minute": 30}
    assert latest_due_slot(
        {"kind": "5min", "timezone": "UTC", "daily_hour": None, "daily_minute": None}, NOW + timedelta(minutes=4)
    ) == NOW
    assert latest_due_slot({"cron": "*/2 * * * *", "timezone": "UTC"}, NOW) is None


@pytest.mark.asyncio
async def test_slot_is_unique_and_claim_settlement_use_token_fence(async_db):
    await _seed_binding(async_db)
    async with async_db() as db:
        binding = (await db.execute(select(GovernedScheduleBinding))).scalar_one()
        first, replay = await reserve_occurrence(db, binding, slot_utc=NOW, now_utc=NOW)
        assert replay is False
        token = first.claim_token
        assert token
        await claim_occurrence(db, first, claim_token=token, fencing_token=1, now_utc=NOW)
        assert first.state == "running"
        assert first.fencing_token == 2
        stale = GovernedScheduleOccurrence(
            occurrence_id=first.occurrence_id,
            state="running",
            claim_token=token,
            fencing_token=1,
        )
        with pytest.raises(RuntimeError, match="settlement_stale"):
            await settle_occurrence(db, stale, state="succeeded", claim_token=token, fencing_token=1)
        await settle_occurrence(db, first, state="succeeded", claim_token=token, fencing_token=2)
        replayed, is_replay = await reserve_occurrence(db, binding, slot_utc=NOW, now_utc=NOW + timedelta(minutes=1))
        assert is_replay is True
        assert replayed.occurrence_id == first.occurrence_id
        assert replayed.state == "succeeded"


@pytest.mark.asyncio
async def test_expired_claim_becomes_persisted_unknown_and_blocks_global_lane(async_db):
    await _seed_binding(async_db)
    await _seed_binding(async_db, binding_id="binding-2")
    async with async_db() as db:
        binding = (await db.execute(select(GovernedScheduleBinding).where(GovernedScheduleBinding.binding_id == "binding-1"))).scalar_one()
        occurrence, _ = await reserve_occurrence(db, binding, slot_utc=NOW, now_utc=NOW)
        occurrence.lease_expires_at = NOW - timedelta(seconds=1)
        await db.flush()
    async with async_db() as db:
        second_binding = (await db.execute(select(GovernedScheduleBinding).where(GovernedScheduleBinding.binding_id == "binding-2"))).scalar_one()
        with pytest.raises(RuntimeError, match="reconciliation"):
            await reserve_occurrence(db, second_binding, slot_utc=NOW, now_utc=NOW + timedelta(minutes=10))
    async with async_db() as db:
        stored = (await db.execute(select(GovernedScheduleOccurrence))).scalars().all()
        assert len(stored) == 1
        assert stored[0].state == "unknown"
        with pytest.raises(RuntimeError, match="cleanup"):
            await reconcile_occurrence(
                db,
                stored[0],
                known_state="blocked",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
            )
        with pytest.raises(TypeError):
            await reconcile_occurrence(db, stored[0], cleanup_verified=True, known_state="blocked")
        with pytest.raises(RuntimeError, match="cleanup"):
            await reconcile_occurrence(
                db,
                stored[0],
                known_state="blocked",
                server_cleanup_run_id="forged-run",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
            )
        # A proof is accepted only when the server-owned scheduler run is
        # bound to the exact occurrence, claim fence, binding revision, and
        # owner.  The next slot remains blocked until that durable receipt is
        # present.
        run = ScheduledJobRun(
            id="cleanup-run-1",
            scheduled_job_id="job-binding-1",
            action_type="calendar.observe_due_events.v1",
            status="failed",
            metadata_json="{}",
        )
        db.add(run)
        stored[0].durable_job_id = run.id
        verified_at = datetime.now(timezone.utc).isoformat()
        run.metadata_json = json.dumps(
            {
                "governed_cleanup_proof": {
                    "status": "verified",
                    "run_id": run.id,
                    "occurrence_id": stored[0].occurrence_id,
                    "binding_id": stored[0].binding_id,
                    "binding_revision": stored[0].binding_revision,
                    "claim_token": stored[0].claim_token,
                    "fencing_token": stored[0].fencing_token,
                    "owner_principal_id": OWNER,
                    "owner_session_id": SESSION,
                    "verified_at": verified_at,
                }
            }
        )
        await db.flush()
        reconciled = await reconcile_occurrence(
            db,
            stored[0],
            known_state="blocked",
            server_cleanup_run_id=run.id,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
        assert reconciled.state == "blocked"
    async with async_db() as db:
        second_binding = (await db.execute(select(GovernedScheduleBinding).where(GovernedScheduleBinding.binding_id == "binding-2"))).scalar_one()
        next_occurrence, replay = await reserve_occurrence(db, second_binding, slot_utc=NOW + timedelta(hours=1), now_utc=NOW + timedelta(minutes=11))
        assert replay is False
        assert next_occurrence.state == "reserved"


@pytest.mark.asyncio
async def test_server_cleanup_proof_is_bound_and_survives_revocation(async_db):
    """A verified read close settles only the exact old occurrence."""

    await _seed_binding(async_db)
    async with async_db() as db:
        binding = await db.get(GovernedScheduleBinding, "binding-1")
        assert binding is not None
        occurrence, replay = await reserve_occurrence(db, binding, slot_utc=NOW, now_utc=NOW)
        assert replay is False
        await claim_occurrence(db, occurrence, now_utc=NOW)
        token = occurrence.claim_token
        fence = occurrence.fencing_token
        occurrence_id = occurrence.occurrence_id
        run = ScheduledJobRun(
            id="cleanup-owned-run",
            scheduled_job_id=binding.scheduled_job_id,
            trigger_type="governed",
            action_type="calendar.observe_due_events.v1",
            status="started",
        )
        db.add(run)
        occurrence.durable_job_id = run.id
        await db.flush()

    async with async_db() as db:
        binding = await db.get(GovernedScheduleBinding, "binding-1")
        assert binding is not None
        binding.binding_revision = 2
        binding.state = "revoked"
        await db.flush()
        occurrence = await db.get(GovernedScheduleOccurrence, occurrence_id)
        assert occurrence is not None
        with pytest.raises(RuntimeError, match="quiescence"):
            await write_server_cleanup_proof(
                db,
                occurrence_id=occurrence.occurrence_id,
                scheduled_run_id="cleanup-owned-run",
                claim_token=token,
                fencing_token=fence,
                transport_quiescence={
                    "status": "unknown",
                    "active_operations": 1,
                    "unsettled_operations": 1,
                    "requests_started": 1,
                    "requests_settled": 0,
                },
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
            )
        assert occurrence.state == "running"
        cleaned = await write_server_cleanup_proof(
            db,
            occurrence_id=occurrence_id,
            scheduled_run_id="cleanup-owned-run",
            claim_token=token,
            fencing_token=fence,
            transport_quiescence={
                "status": "verified",
                "active_operations": 0,
                "unsettled_operations": 0,
                "requests_started": 2,
                "requests_settled": 2,
            },
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
        assert cleaned.state == "blocked"
        assert cleaned.claim_token is None
        run = await db.get(ScheduledJobRun, "cleanup-owned-run")
        assert run is not None
        proof = json.loads(run.metadata_json or "{}")["governed_cleanup_proof"]
        assert proof["binding_revision"] == 1
        assert proof["transport_quiescence"]["requests_settled"] == 2

        replayed = await write_server_cleanup_proof(
            db,
            occurrence_id=occurrence_id,
            scheduled_run_id="cleanup-owned-run",
            claim_token=token,
            fencing_token=fence,
            transport_quiescence={
                "status": "verified",
                "active_operations": 0,
                "unsettled_operations": 0,
                "requests_started": 2,
                "requests_settled": 2,
            },
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
        assert replayed.state == "blocked"
        assert json.loads((await db.get(ScheduledJobRun, "cleanup-owned-run")).metadata_json or "{}")[
            "governed_cleanup_proof"
        ]["verified_at"] == proof["verified_at"]


@pytest.mark.asyncio
async def test_server_cleanup_proof_rejects_foreign_action_and_fence(async_db):
    await _seed_binding(async_db)
    async with async_db() as db:
        binding = await db.get(GovernedScheduleBinding, "binding-1")
        assert binding is not None
        occurrence, _ = await reserve_occurrence(db, binding, slot_utc=NOW, now_utc=NOW)
        await claim_occurrence(db, occurrence, now_utc=NOW)
        run = ScheduledJobRun(
            id="cleanup-foreign-run",
            scheduled_job_id=binding.scheduled_job_id,
            trigger_type="governed",
            action_type="guardian.run_procedure.v2",
            status="started",
        )
        db.add(run)
        occurrence.durable_job_id = run.id
        await db.flush()
        with pytest.raises(RuntimeError, match="proof_unavailable"):
            await write_server_cleanup_proof(
                db,
                occurrence_id=occurrence.occurrence_id,
                scheduled_run_id=run.id,
                claim_token=occurrence.claim_token,
                fencing_token=occurrence.fencing_token,
                transport_quiescence={
                    "status": "verified",
                    "active_operations": 0,
                    "unsettled_operations": 0,
                    "requests_started": 1,
                    "requests_settled": 1,
                },
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
            )
        run.action_type = "calendar.observe_due_events.v1"
        await db.flush()
        with pytest.raises(RuntimeError, match="proof_mismatch"):
            await write_server_cleanup_proof(
                db,
                occurrence_id=occurrence.occurrence_id,
                scheduled_run_id=run.id,
                claim_token=occurrence.claim_token,
                fencing_token=occurrence.fencing_token + 1,
                transport_quiescence={
                    "status": "verified",
                    "active_operations": 0,
                    "unsettled_operations": 0,
                    "requests_started": 1,
                    "requests_settled": 1,
                },
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
            )


@pytest.mark.asyncio
async def test_fresh_transaction_reloads_binding_revision_and_never_replays_unknown(async_db):
    await _seed_binding(async_db)
    async with async_db() as db:
        binding = (await db.execute(select(GovernedScheduleBinding))).scalar_one()
        binding.state = "paused"
        await db.flush()
    async with async_db() as db:
        stale_object = GovernedScheduleBinding(
            binding_id="binding-1",
            scheduled_job_id="job-binding-1",
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
            action_type="calendar.observe_due_events.v1",
            capability_id="calendar.observe_due_events.v1",
            expires_at=NOW + timedelta(days=1),
            state="active",
        )
        with pytest.raises(ValueError, match="not active"):
            await reserve_occurrence(db, stale_object, slot_utc=NOW, now_utc=NOW)


async def _seed_real_schedule(async_db) -> str:
    """Seed the canonical owner/consent/artifact graph used by scheduler E2E tests."""
    from src.scheduler.governed_schedules import (
        create_binding,
        create_observation_input_artifact,
    )
    from src.work_board.contracts import WorkBoardOwner

    now = datetime.now(timezone.utc)
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with async_db() as db:
        goal = Goal(
            id="scheduler-goal",
            title="Scheduler integration goal",
            status="active",
            revision=1,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
        connection = GoogleServiceConnection(
            connection_id="scheduler-connection",
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
            service="calendar_readonly",
            label="Scheduler test",
            vault_secret_key="vault:scheduler-test",
            state="active",
            revision=1,
        )
        consent = CalendarReadConsent(
            consent_id="scheduler-consent",
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
            connection_id=connection.connection_id,
            connection_revision=1,
            calendar_id=encrypt("owner-calendar"),
            goal_id=goal.id,
            goal_revision=1,
            allowed_fields_json=json.dumps(["summary", "start", "end", "location"]),
            window_minutes=120,
            max_events=3,
            allow_remote_model=True,
            expires_at=now + timedelta(days=1),
            state="active",
            revision=1,
            consent_digest="sha256:" + "c" * 64,
        )
        db.add_all(
            [
                goal,
                connection,
                consent,
                OperatorSession(
                    id=SESSION,
                    token_hash="scheduler-test-session-token-hash",
                    idle_expires_at=now + timedelta(hours=1),
                    absolute_expires_at=now + timedelta(hours=2),
                ),
                Secret(
                    key=connection.vault_secret_key,
                    encrypted_value=encrypt(
                        json.dumps(
                            {
                                "client_id": "scheduler-client",
                                "client_secret": "scheduler-client-secret",
                                "refresh_token": "scheduler-refresh-token",
                            }
                        )
                    ),
                ),
            ]
        )
        await db.flush()
        input_metadata = await create_observation_input_artifact(
            db,
            owner,
            consent=consent,
            connection_id=connection.connection_id,
            idempotency_key="schedule:scheduler-binding-key",
        )
        binding = await create_binding(
            db,
            owner,
            {
                "action_type": "calendar.observe_due_events.v1",
                "capability_id": "calendar.observe_due_events.v1",
                "cadence": {"kind": "hourly", "timezone": "UTC", "daily_hour": None, "daily_minute": None},
                "expires_at": now + timedelta(hours=12),
                "idempotency_key": "scheduler-binding-key",
                "goal_id": goal.id,
                "goal_revision": goal.revision,
                "consent_id": consent.consent_id,
                "consent_revision": consent.revision,
                "consent_digest": consent.consent_digest,
                "input_artifact_id": input_metadata.artifact_id,
                "input_digest": input_metadata.typed_input_digest,
                "action_digest": "sha256:" + "a" * 64,
                "schedule_request_digest": "sha256:" + "b" * 64,
            },
        )
        job_id = binding.scheduled_job_id
    return job_id


@pytest.mark.asyncio
async def test_execute_governed_schedule_publishes_todo_and_dedupes_changed_events(
    async_db,
    monkeypatch,
):
    """Exercise provider read, occurrence fencing, and WorkBoard publication."""

    provider = _SchedulerProvider()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    job_id = await _seed_real_schedule(async_db)

    from src.scheduler.scheduled_jobs import scheduled_job_repository

    job = await scheduled_job_repository.get_job(job_id)
    assert job is not None
    first_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot)

    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == OWNER,
                    WorkBoardTask.owner_session_id == SESSION,
                    WorkBoardTask.capability_id == "calendar.meeting-prep.v1",
                )
            )
        ).scalars().all()
        occurrences = (await db.execute(select(GovernedScheduleOccurrence))).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].status.value == "todo"
        assert tasks[0].priority == 40
        assert len(occurrences) == 1
        assert occurrences[0].state == "succeeded"
        assert occurrences[0].durable_job_id

    provider_calls_after_first = len(provider.calls)
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot + timedelta(hours=1))
    async with async_db() as db:
        unchanged_tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == OWNER,
                    WorkBoardTask.owner_session_id == SESSION,
                    WorkBoardTask.capability_id == "calendar.meeting-prep.v1",
                )
            )
        ).scalars().all()
        assert len(unchanged_tasks) == 1
    assert len(provider.calls) > provider_calls_after_first

    provider.event = {
        **provider.event,
        "etag": '"event-etag-2"',
        "updated": "2026-09-30T09:00:00Z",
        "summary": "Changed operator planning review",
    }
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot + timedelta(hours=2))
    async with async_db() as db:
        changed_tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == OWNER,
                    WorkBoardTask.owner_session_id == SESSION,
                    WorkBoardTask.capability_id == "calendar.meeting-prep.v1",
                )
            )
        ).scalars().all()
        assert len(changed_tasks) == 2
        assert {task.title for task in changed_tasks} == {
            "Prepare for Operator planning review",
            "Prepare for Changed operator planning review",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["consent", "goal", "session", "binding"])
async def test_governed_schedule_rechecks_revocation_before_provider(
    async_db,
    monkeypatch,
    mutation: str,
):
    """A stale goal/session/consent/binding cannot admit an external read."""

    provider = _SchedulerProvider()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    job_id = await _seed_real_schedule(async_db)
    async with async_db() as db:
        if mutation == "consent":
            row = await db.get(CalendarReadConsent, "scheduler-consent")
            row.state = "revoked"
        elif mutation == "goal":
            row = await db.get(Goal, "scheduler-goal")
            row.status = "paused"
        elif mutation == "session":
            row = await db.get(OperatorSession, SESSION)
            row.revoked_at = datetime.now(timezone.utc)
        else:
            row = (await db.execute(select(GovernedScheduleBinding))).scalar_one()
            row.state = "revoked"
        await db.flush()

    await execute_scheduled_job(
        job_id,
        scheduled_slot_utc=datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0),
    )
    assert provider.calls == []
    async with async_db() as db:
        assert not (await db.execute(select(GovernedScheduleOccurrence))).scalars().all()
        run = (
            await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == job_id))
        ).scalar_one()
        assert run.outcome == "failed"


@pytest.mark.asyncio
async def test_governed_schedule_revocation_during_read_records_cleanup_and_blocks_next_slot(
    async_db,
    monkeypatch,
):
    """A revocation after the provider request cannot publish or replay a task.

    The adapter's awaited HTTPX close receipt now lets the server settle the
    old read to ``blocked``.  This is a cleanup proof only; it is never a
    successful observation and the next slot still has to pass fresh fences.
    """

    mutated = False
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mutated
        method = request.method.upper()
        url = str(request.url)
        requests.append((method, url))
        if url == "https://oauth2.googleapis.com/token" and method == "POST":
            payload: dict[str, Any] = {"access_token": "scheduler-test-access-token"}
        elif "/calendar/v3/users/me/calendarList" in url and method == "GET":
            payload = {
                "etag": '"calendar-list-etag-1"',
                "items": [{"id": "owner-calendar", "summary": "Operator calendar"}],
            }
        elif "/calendar/v3/calendars/owner-calendar/events" in url and method == "GET":
            if not mutated:
                mutated = True
                async with async_db() as db:
                    consent = await db.get(CalendarReadConsent, "scheduler-consent")
                    assert consent is not None
                    consent.state = "revoked"
                    await db.flush()
            payload = {
                "etag": '"events-etag-1"',
                "items": [
                    {
                        "id": "provider-event-1",
                        "etag": '"event-etag-1"',
                        "updated": "2026-09-30T08:00:00Z",
                        "summary": "Operator planning review",
                        "start": {"dateTime": "2026-09-30T13:00:00Z"},
                        "end": {"dateTime": "2026-09-30T14:00:00Z"},
                        "location": "Room 4",
                    }
                ],
            }
        else:
            raise AssertionError(f"unexpected scheduler provider request: {method} {url}")
        return httpx.Response(200, headers={"content-type": "application/json"}, json=payload)

    transport = httpx.MockTransport(handler)
    adapters: list[Any] = []

    from src.integrations.google_calendar import GoogleCalendarReadonlyAdapter

    class RealPinnedAdapter(GoogleCalendarReadonlyAdapter):
        def __init__(self, connection, *, owner_principal_id: str, authority_check=None):
            super().__init__(
                connection,
                owner_principal_id=owner_principal_id,
                transport=transport,
                resolver=lambda _hostname, _port: ["8.8.8.8"],
                authority_check=authority_check,
            )
            adapters.append(self)

    monkeypatch.setattr("src.integrations.google_calendar.GoogleCalendarReadonlyAdapter", RealPinnedAdapter)
    job_id = await _seed_real_schedule(async_db)
    first_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot)

    async with async_db() as db:
        occurrences = (await db.execute(select(GovernedScheduleOccurrence))).scalars().all()
        assert len(occurrences) == 1
        assert occurrences[0].state == "blocked"
        run = (
            await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == job_id))
        ).scalar_one()
        metadata = json.loads(run.metadata_json or "{}")
        proof = metadata["governed_cleanup_proof"]
        assert proof["status"] == "verified"
        assert proof["occurrence_id"] == occurrences[0].occurrence_id
        assert proof["transport_quiescence"]["active_operations"] == 0
        assert proof["transport_quiescence"]["unsettled_operations"] == 0
        assert proof["transport_quiescence"]["requests_started"] >= 2
        assert proof["transport_quiescence"]["requests_started"] == proof["transport_quiescence"]["requests_settled"]
        assert not (await db.execute(select(WorkBoardTask))).scalars().all()

    assert requests[0][0] == "POST"
    assert any("/events" in path for _method, path in requests)
    assert adapters and adapters[0].transport_quiescence()["status"] == "verified"
    calls_after_blocked = len(requests)
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot + timedelta(hours=1))
    assert len(requests) == calls_after_blocked
    async with async_db() as db:
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.state == "blocked"


@pytest.mark.asyncio
async def test_governed_schedule_close_failure_keeps_unknown_lane_quarantined(async_db, monkeypatch):
    """A failed awaited HTTPX close cannot mint a cleanup proof."""

    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "oauth2.googleapis.com":
            payload: dict[str, Any] = {"access_token": "scheduler-test-access-token"}
        else:
            payload = {"etag": '"events-etag-1"', "items": []}
        return httpx.Response(200, headers={"content-type": "application/json"}, json=payload)

    class CloseFailureTransport(httpx.MockTransport):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("mock client close failed")

    transport = CloseFailureTransport(handler)
    adapters: list[Any] = []
    from src.integrations.google_calendar import GoogleCalendarReadonlyAdapter

    class CloseFailureAdapter(GoogleCalendarReadonlyAdapter):
        def __init__(self, connection, *, owner_principal_id: str, authority_check=None):
            super().__init__(
                connection,
                owner_principal_id=owner_principal_id,
                transport=transport,
                resolver=lambda _hostname, _port: ["8.8.8.8"],
                authority_check=authority_check,
            )
            adapters.append(self)

    monkeypatch.setattr("src.integrations.google_calendar.GoogleCalendarReadonlyAdapter", CloseFailureAdapter)
    job_id = await _seed_real_schedule(async_db)
    first_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot)

    async with async_db() as db:
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.state == "unknown"

        run = (
            await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == job_id))
        ).scalar_one()
        assert "governed_cleanup_proof" not in json.loads(run.metadata_json or "{}")
    assert adapters and adapters[0].transport_quiescence()["status"] == "unknown"
    calls_after_unknown = len(requests)

    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot + timedelta(hours=1))
    assert len(requests) == calls_after_unknown
    async with async_db() as db:
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.state == "unknown"


@pytest.mark.asyncio
async def test_governed_schedule_pre_adapter_failure_records_known_no_contact(async_db, monkeypatch):
    """A fixed local failure before adapter construction may release safely."""

    job_id = await _seed_real_schedule(async_db)
    async with async_db() as db:
        consent = await db.get(CalendarReadConsent, "scheduler-consent")
        assert consent is not None
        consent.calendar_id = "malformed-encrypted-calendar-id"
        await db.flush()

    adapter_constructed = False

    class MustNotConstructAdapter:
        def __init__(self, *args: Any, **kwargs: Any):
            nonlocal adapter_constructed
            adapter_constructed = True
            raise AssertionError("provider adapter must not be constructed after local decrypt failure")

    monkeypatch.setattr("src.integrations.google_calendar.GoogleCalendarReadonlyAdapter", MustNotConstructAdapter)
    first_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot)

    async with async_db() as db:
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.state == "blocked"
    assert adapter_constructed is False

    async with async_db() as db:
        consent = await db.get(CalendarReadConsent, "scheduler-consent")
        assert consent is not None
        consent.state = "revoked"
        await db.flush()
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot + timedelta(hours=1))
    async with async_db() as db:
        occurrences = (await db.execute(select(GovernedScheduleOccurrence))).scalars().all()
        assert len(occurrences) == 1
        assert occurrences[0].state == "blocked"
        # The second invocation creates its own failed scheduler receipt before
        # rechecking revoked consent.  Select the durable cleanup receipt by
        # its proof marker instead of assuming one run row for the job.
        runs = (
            await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == job_id))
        ).scalars().all()
        proof_runs = [run for run in runs if "governed_cleanup_proof" in json.loads(run.metadata_json or "{}")]
        assert len(proof_runs) == 1
        metadata = json.loads(proof_runs[0].metadata_json or "{}")
        assert metadata["governed_cleanup_origin"] == "server_no_contact_before_adapter"
        proof = metadata["governed_cleanup_proof"]
        assert proof["transport_quiescence"] == {
            "status": "verified",
            "active_operations": 0,
            "unsettled_operations": 0,
            "requests_started": 0,
            "requests_settled": 0,
        }


@pytest.mark.asyncio
async def test_governed_schedule_cancellation_before_adapter_finalizes_run(async_db, monkeypatch):
    """Cancellation after claim still leaves a terminal run and cleanup proof."""

    job_id = await _seed_real_schedule(async_db)

    class CancelBeforeAdapter:
        def __init__(self, *args: Any, **kwargs: Any):
            raise asyncio.CancelledError()

    monkeypatch.setattr("src.integrations.google_calendar.GoogleCalendarReadonlyAdapter", CancelBeforeAdapter)
    first_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    with pytest.raises(asyncio.CancelledError):
        await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot)

    async with async_db() as db:
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.state == "blocked"
        run = (
            await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == job_id))
        ).scalar_one()
        assert run.status == "cancelled"
        assert run.outcome == "cancelled"
        assert run.finished_at is not None
        metadata = json.loads(run.metadata_json or "{}")
        assert metadata["governed_cleanup_origin"] == "server_no_contact_before_adapter"


@pytest.mark.asyncio
async def test_governed_schedule_cancellation_after_adapter_admission_finalizes_run(async_db, monkeypatch):
    """Cancellation during a real provider request retains close proof."""

    job_id = await _seed_real_schedule(async_db)
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"access_token": "scheduler-test-access-token"},
            )
        if request.url.path.endswith("/calendarList"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "etag": '"calendar-list-etag-1"',
                    "items": [{"id": "owner-calendar", "summary": "Operator calendar"}],
                },
            )
        if request.url.path.endswith("/events"):
            raise asyncio.CancelledError()
        raise AssertionError(f"unexpected scheduler provider request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    from src.integrations.google_calendar import GoogleCalendarReadonlyAdapter

    class CancelAfterAdapter(GoogleCalendarReadonlyAdapter):
        def __init__(self, connection, *, owner_principal_id: str, authority_check=None):
            super().__init__(
                connection,
                owner_principal_id=owner_principal_id,
                transport=transport,
                resolver=lambda _hostname, _port: ["8.8.8.8"],
                authority_check=authority_check,
            )

    monkeypatch.setattr("src.integrations.google_calendar.GoogleCalendarReadonlyAdapter", CancelAfterAdapter)
    first_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    with pytest.raises(asyncio.CancelledError):
        await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot)

    async with async_db() as db:
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.state == "blocked"
        run = (
            await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == job_id))
        ).scalar_one()
        assert run.status == "cancelled"
        assert run.outcome == "cancelled"
        assert run.finished_at is not None
        metadata = json.loads(run.metadata_json or "{}")
        assert metadata["governed_cleanup_origin"] == "adapter_quiescence"
        assert metadata["governed_cleanup_proof"]["status"] == "verified"
        transport_quiescence = metadata["governed_cleanup_proof"]["transport_quiescence"]
        assert transport_quiescence["requests_started"] >= 2
        assert transport_quiescence["requests_started"] == transport_quiescence["requests_settled"]
    assert len(requests) >= 2


@pytest.mark.asyncio
async def test_governed_schedule_publication_rechecks_revoked_consent_and_tombstones_artifact(
    async_db,
    monkeypatch,
):
    """A consent revocation after preparation cannot publish a stale task."""

    provider = _SchedulerProvider()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    from src.scheduler import scheduled_jobs as scheduled_jobs_module
    from src.work_board.input_artifacts import _payload_path

    original_prepare = scheduled_jobs_module.prepare_input_artifact

    async def prepare_then_revoke(db, owner, request, **kwargs):
        metadata = await original_prepare(db, owner, request, **kwargs)
        # Model the real artifact writer's commit boundary, then mutate the
        # authority from a separate session before the publication writer
        # callback acquires its serialized transaction.
        await db.commit()
        async with async_db() as revoke_db:
            consent = await revoke_db.get(CalendarReadConsent, "scheduler-consent")
            assert consent is not None
            consent.state = "revoked"
            consent.revision += 1
            await revoke_db.flush()
        return metadata

    monkeypatch.setattr(
        "src.scheduler.scheduled_jobs.prepare_input_artifact",
        prepare_then_revoke,
    )
    job_id = await _seed_real_schedule(async_db)
    first_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot)

    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.capability_id == "calendar.meeting-prep.v1",
                )
            )
        ).scalars().all()
        assert tasks == []
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.capability_id == "calendar.meeting-prep.v1",
                )
            )
        ).scalars().all()
        assert len(artifacts) == 1
        assert artifacts[0].state == "revoked"
        assert artifacts[0].bound_task_id is None
        assert not _payload_path(artifacts[0]).exists()

        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.state == "blocked"
        run = (
            await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == job_id))
        ).scalar_one()
        assert run.status == "failed"
        assert run.outcome == "failed"
        metadata = json.loads(run.metadata_json or "{}")
        assert metadata["governed_cleanup_origin"] == "adapter_quiescence"
        assert metadata["governed_cleanup_proof"]["status"] == "verified"


@pytest.mark.asyncio
async def test_governed_schedule_orphan_cleanup_failure_stays_unknown_and_visible(
    async_db,
    monkeypatch,
):
    """A failed orphan tombstone blocks recovery and exposes its stable error."""

    provider = _SchedulerProvider()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    from src.scheduler import scheduled_jobs as scheduled_jobs_module
    from src.work_board.input_artifacts import _payload_path

    original_prepare = scheduled_jobs_module.prepare_input_artifact

    async def prepare_then_revoke(db, owner, request, **kwargs):
        metadata = await original_prepare(db, owner, request, **kwargs)
        await db.commit()
        async with async_db() as revoke_db:
            consent = await revoke_db.get(CalendarReadConsent, "scheduler-consent")
            assert consent is not None
            consent.state = "revoked"
            consent.revision += 1
            await revoke_db.flush()
        return metadata

    async def fail_orphan_revoke(*args: Any, **kwargs: Any):
        raise RuntimeError("injected_orphan_cleanup_failure")

    monkeypatch.setattr(
        "src.scheduler.scheduled_jobs.prepare_input_artifact",
        prepare_then_revoke,
    )
    monkeypatch.setattr(
        "src.scheduler.scheduled_jobs.revoke_input_artifact",
        fail_orphan_revoke,
    )
    job_id = await _seed_real_schedule(async_db)
    first_slot = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot)

    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.capability_id == "calendar.meeting-prep.v1",
                )
            )
        ).scalars().all()
        assert tasks == []
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.capability_id == "calendar.meeting-prep.v1",
                )
            )
        ).scalars().all()
        assert len(artifacts) == 1
        assert artifacts[0].state == "pending"
        assert artifacts[0].bound_task_id is None
        assert _payload_path(artifacts[0]).exists()

        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.state == "unknown"
        run = (
            await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == job_id))
        ).scalar_one()
        assert run.status == "failed"
        assert run.outcome == "failed"
        assert run.error == "governed_schedule_artifact_cleanup_required"
        assert "governed_cleanup_proof" not in json.loads(run.metadata_json or "{}")

    calls_after_cleanup_failure = len(provider.calls)
    await execute_scheduled_job(job_id, scheduled_slot_utc=first_slot + timedelta(hours=1))
    assert len(provider.calls) == calls_after_cleanup_failure
    async with async_db() as db:
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.state == "unknown"


@pytest.mark.asyncio
async def test_two_sqlite_workers_share_one_slot_without_duplicate_reservation(tmp_path):
    """A real file-backed SQLite writer race returns one reservation and one replay."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'scheduler-race.sqlite'}",
        connect_args={"timeout": 5},
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with factory() as db:
            db.add_all(
                [
                    ScheduledJob(
                        id="race-job",
                        name="Race observation",
                        enabled=True,
                        trigger_type="governed",
                        trigger_spec_json=json.dumps(
                            {"kind": "hourly", "timezone": "UTC", "daily_hour": None, "daily_minute": None}
                        ),
                        action_type="calendar.observe_due_events.v1",
                        action_spec_json=json.dumps({"binding_id": "race-binding"}),
                    ),
                    GovernedScheduleBinding(
                        binding_id="race-binding",
                        scheduled_job_id="race-job",
                        owner_principal_id="race-owner",
                        owner_session_id="race-session",
                        goal_id="race-goal",
                        input_artifact_id="race-artifact",
                        input_digest="a" * 64,
                        action_digest="b" * 64,
                        read_consent_id="race-consent",
                        expires_at=now + timedelta(days=1),
                        cadence_kind="hourly",
                        timezone="UTC",
                    ),
                ]
            )
            await db.commit()

        async def worker(worker_id: int):
            async with factory() as db:
                binding = await db.get(GovernedScheduleBinding, "race-binding")
                try:
                    occurrence, replay = await reserve_occurrence(
                        db,
                        binding,
                        slot_utc=now,
                        now_utc=now,
                    )
                    await db.commit()
                    return worker_id, occurrence.occurrence_id, replay
                except Exception:
                    await db.rollback()
                    raise

        results = await asyncio.gather(worker(1), worker(2))
        assert {result[2] for result in results} == {False, True}
        assert len({result[1] for result in results}) == 1
    finally:
        await engine.dispose()
