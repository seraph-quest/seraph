"""Real SQLite transaction denial checks, never native-service authority."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import func, insert, select

from tests.test_inference_accounting import accounting_db
from src.db.models import GovernedScheduleBinding, GovernedScheduleOccurrence, ScheduledJob
from src.runtime_plugins.ownership import CompositionBindingError
from src.scheduler.governed_schedules import (apply_control, claim_occurrence, create_binding,
    reserve_occurrence, settle_occurrence)
from src.work_board.contracts import WorkBoardOwner


@pytest.mark.asyncio
async def test_occurrence_uniqueness_retry_rechecks_current_authority_before_body(accounting_db, monkeypatch):
    _, _, factory = accounting_db
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    async with factory.accounting_sessions() as db:
        job = ScheduledJob(id="guarded-schedule", name="Guarded", enabled=True,
            trigger_type="governed", action_type="calendar.observe_due_events.v1",
            action_spec_json=json.dumps({"binding_id": "guarded-binding"}))
        binding = GovernedScheduleBinding(binding_id="guarded-binding", scheduled_job_id=job.id,
            owner_principal_id="guard-owner", owner_session_id="guard-root", goal_id="guard-goal",
            input_artifact_id="guard-artifact", schedule_idempotency_key="guard-key",
            schedule_request_digest="a" * 64, action_digest="b" * 64,
            cadence_kind="hourly", timezone="UTC", expires_at=now + timedelta(hours=1))
        db.add(job)
        db.add(binding)
        await db.flush()
    checks = []
    async def current_check(db):
        assert db.in_transaction()
        checks.append(db)
        if len(checks) == 2:
            raise CompositionBindingError("composition_epoch_stale")
    async with factory.accounting_sessions() as db:
        binding = await db.get(GovernedScheduleBinding, "guarded-binding")
        original_flush = db.flush
        attempted = []
        async def flush_with_real_unique_conflict(*args, **kwargs):
            await original_flush(*args, **kwargs)
            row = await db.scalar(select(GovernedScheduleOccurrence).where(
                GovernedScheduleOccurrence.binding_id == "guarded-binding"))
            attempted.append(row.occurrence_id)
            # The actual SQLite unique constraint fails, rolling back the
            # first transaction. The new transaction must deny before lookup.
            await db.execute(insert(GovernedScheduleOccurrence).values(
                occurrence_id="conflicting-occurrence", binding_id=row.binding_id,
                binding_revision=row.binding_revision, slot_utc=row.slot_utc,
                idempotency_key="conflicting-key", request_digest="b" * 64,
                claim_token="conflicting-token", fencing_token=1, state="reserved"))
        monkeypatch.setattr(db, "flush", flush_with_real_unique_conflict)
        with pytest.raises(CompositionBindingError, match="composition_epoch_stale"):
            await reserve_occurrence(db, binding, slot_utc=now,
                composition_authority_check=current_check)
        assert len(attempted) == 1
        assert len(checks) == 2
        await db.rollback()
    async with factory.accounting_sessions() as db:
        assert await db.scalar(select(func.count()).select_from(GovernedScheduleOccurrence)) == 0
        current = await db.get(GovernedScheduleBinding, "guarded-binding")
        assert current.last_slot_utc is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["register", "disable", "reserve", "claim", "settle"])
async def test_current_guard_denies_before_any_scheduler_owner_body_or_effect(accounting_db, operation):
    _, _, factory = accounting_db
    checks = []
    async def deny(db):
        assert db.in_transaction()
        checks.append(db)
        raise CompositionBindingError("composition_epoch_stale")
    # These selectors cannot confer authority: every operation must enter its
    # real serialized transaction and deny before reading an owner row.
    async with factory.accounting_sessions() as db:
        async def private_body_forbidden(*args, **kwargs):
            raise AssertionError("scheduler body reached after current-authority denial")
        db.get = private_body_forbidden
        owner = WorkBoardOwner(principal_id="denied-principal", session_id="denied-root")
        with pytest.raises(CompositionBindingError, match="composition_epoch_stale"):
            if operation == "register":
                await create_binding(db, owner, {}, composition_authority_check=deny)
            elif operation == "disable":
                await apply_control(db, owner, "absent-binding", "pause", 1, "denied-key",
                    composition_authority_check=deny)
            elif operation == "reserve":
                await reserve_occurrence(db, GovernedScheduleBinding(binding_id="absent-binding"),
                    slot_utc=datetime.now(timezone.utc), composition_authority_check=deny)
            else:
                occurrence = GovernedScheduleOccurrence(occurrence_id="absent-occurrence",
                    state="reserved" if operation == "claim" else "running",
                    claim_token="absent-token", fencing_token=1)
                if operation == "claim":
                    await claim_occurrence(db, occurrence, composition_authority_check=deny)
                else:
                    await settle_occurrence(db, occurrence, state="blocked", composition_authority_check=deny)
        assert len(checks) == 1
        await db.rollback()
    async with factory.accounting_sessions() as db:
        for model in (GovernedScheduleBinding, GovernedScheduleOccurrence, ScheduledJob):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
