"""Real original claim projection/reserve mechanics; no Source grant."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text, update

from src.db.models import WorkflowRunState
from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, MAX_BYTES
from src.workflows.job_runtime import _native_memory_pending_output, _serialize
from src.workspace.accounting_witness import reserve_native_memory_pending_run


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_original_pending_claim_output_matches_sqlite_without_staging_write(async_db):
    run = WorkflowRunState(run_identity="numeric-claim", root_run_identity="numeric-claim",
        workflow_name="original", job_kind="runtime_service_memory_v1", status="queued")
    async with async_db() as db:
        db.add(run)
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == "numeric-claim"))).scalar_one()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=300)
        pending = {"status": "running", "lease_owner": "original-owner",
            "lease_expires_at": expires, "heartbeat_at": now, "updated_at": now,
            "attempt_count": run.attempt_count + 1, "fencing_token": run.fencing_token + 1,
            "revision": run.revision + 1, "checkpoint_receipts_json": "[]"}
        receipt = {"kind": "claim", "status": "claimed", "owner": "original-owner",
            "lease_expires_at": expires.isoformat(), "fencing_token": pending["fencing_token"],
            "attempt": pending["attempt_count"], "revision": pending["revision"],
            "operator_visible": True}
        before = (await db.execute(text("SELECT total_changes()"))).scalar_one()
        expected = _native_memory_pending_output(run, pending, receipt=receipt)
        budget = HeaderReadBudget()
        reserved = await reserve_native_memory_pending_run(db, run, pending, budget,
            outputs=(expected,))
        assert reserved > 0
        assert not db.new and not db.dirty and not db.deleted
        assert run.status == "queued" and run.attempt_count == 0
        assert (await db.execute(text("SELECT total_changes()"))).scalar_one() == before
        await db.execute(update(WorkflowRunState).where(
            WorkflowRunState.run_identity == run.run_identity,
            WorkflowRunState.revision == run.revision,
            WorkflowRunState.fencing_token == run.fencing_token,
        ).values(**pending).execution_options(synchronize_session=False))
        await db.refresh(run)
        assert _serialize(run, receipt=receipt) == expected


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_pending_claim_reserve_exhaustion_and_sql_expression_do_not_write(async_db):
    run = WorkflowRunState(run_identity="denied-claim", root_run_identity="denied-claim",
        workflow_name="original", status="queued")
    async with async_db() as db:
        db.add(run)
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == "denied-claim"))).scalar_one()
        before = (await db.execute(text("SELECT total_changes()"))).scalar_one()
        with pytest.raises(HeaderBoundsError):
            _native_memory_pending_output(run, {"revision": WorkflowRunState.revision + 1}, receipt={})
        budget = HeaderReadBudget()
        budget.debit(MAX_BYTES)
        with pytest.raises(HeaderBoundsError):
            await reserve_native_memory_pending_run(db, run, {"status": "running"}, budget)
        assert not db.new and not db.dirty and not db.deleted
        assert run.status == "queued"
        assert (await db.execute(text("SELECT total_changes()"))).scalar_one() == before
