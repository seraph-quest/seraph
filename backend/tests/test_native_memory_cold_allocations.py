"""Cold inspection pays allocations; the retained job never becomes a Source."""
import pytest
import pytest_asyncio
from sqlalchemy import text

from tests.test_native_memory_producer import actual_memory


class StopBeforeOriginalQueue(BaseException):
    """Interrupt after original admission has committed, before queue/claim."""


@pytest_asyncio.fixture
async def original_cold_job(actual_memory, monkeypatch):
    client, operator, jobs, host, calls, task, attempt, _output = actual_memory
    from src.runtime_plugins.memory_producer import memory_job_id, _MEMORY_SOURCES
    sources_before = tuple(_MEMORY_SOURCES)
    observed = []
    async def stop_queue(job_id, status, **kwargs):
        assert status == "queued"
        observed.append(job_id)
        raise StopBeforeOriginalQueue()
    monkeypatch.setattr(jobs, "transition_job", stop_queue)
    key = "cold-allocation-original"
    with pytest.raises(StopBeforeOriginalQueue):
        await client.post("/api/memory/task-proposals/native", json={
            "task_id": task.task_id, "expected_task_revision": task.task_revision,
            "attempt_id": attempt.attempt_id, "idempotency_key": key})
    job_id = memory_job_id(operator, "memory.propose", key)
    assert observed == [job_id]
    assert calls == [] and tuple(_MEMORY_SOURCES) == sources_before
    assert host.admitting
    yield job_id, task, attempt


async def inspected_row(job_id, callback):
    from src.db.engine import async_session_factory
    from src.memory.header_bounds import HeaderReadBudget
    from src.memory.composition_headers import certify_composition_superset, snapshot_reads
    from src.workspace.accounting_witness import _composition_row
    frame = HeaderReadBudget()
    async with async_session_factory() as db:
        await db.execute(text("BEGIN"))
        certificate = await certify_composition_superset(db, frame)
        connection = await db.connection()
        def inspect(raw_connection):
            with snapshot_reads(certificate):
                row = _composition_row(raw_connection, "workflow_run_states", job_id)
                return callback(raw_connection, row, frame)
        try:
            return await connection.run_sync(inspect)
        finally:
            await db.rollback()


@pytest.mark.asyncio
async def test_real_executed_source_admission_passes_paid_cold_candidate_inspection(original_cold_job, monkeypatch):
    from src.memory.header_bounds import HeaderReadBudget
    from src.workspace.accounting_witness import _checked_memory_reference_row
    job_id, task, attempt = original_cold_job
    charges = []
    original_debit = HeaderReadBudget.debit
    def observed_debit(frame, amount, *, appearance=None):
        charges.append((frame, appearance, amount))
        return original_debit(frame, amount, appearance=appearance)
    monkeypatch.setattr(HeaderReadBudget, "debit", observed_debit)
    def inspect(connection, row, frame):
        assert row["status"] == "accepted" and row["attempt_count"] == 0
        candidate, verified_audit = _checked_memory_reference_row(connection, row)
        assert candidate["source"]["task_id"] == task.task_id
        assert candidate["source"]["attempt_id"] == attempt.attempt_id
        assert verified_audit is None
        cold = [label for owner, label, _amount in charges if owner is frame
            and type(label) is tuple and label[:1] == ("native-memory-cold",)]
        assert sum(label[1:] == ("candidate", job_id, "run-namespace") for label in cold) == 2
        assert sum(label[1:] == ("context", job_id, "json-parser") for label in cold) == 2
        assert sum(label[1:] == ("claim", job_id, "journal-parser") for label in cold) == 1
        assert frame.remaining < 1_048_576
    await inspected_row(job_id, inspect)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["candidate", "claim"])
async def test_spent_original_frame_denies_before_cold_parser(original_cold_job, monkeypatch, owner):
    from src.workspace.accounting_witness import (
        ProductionWorkspaceReconciliationError, _checked_memory_candidate_row, _checked_memory_claim_row)
    job_id, _task, _attempt = original_cold_job
    reached = []
    def forbidden(*args, **kwargs):
        reached.append(True)
        raise AssertionError("cold parser ran without allocation capacity")
    if owner == "candidate":
        monkeypatch.setattr("src.runtime_plugins.memory_producer.memory_context", forbidden)
    else:
        monkeypatch.setattr("src.memory.header_bounds.strict_json_loads", forbidden)
    def inspect(_connection, row, frame):
        frame.debit(frame.remaining)
        with pytest.raises(ProductionWorkspaceReconciliationError) as caught:
            if owner == "candidate":
                _checked_memory_candidate_row(row)
            else:
                _checked_memory_claim_row(row, None, None, None)
        assert str(caught.value) == "composition_native_memory_preoriginal_invalid"
        assert reached == [] and frame.remaining == 0
    await inspected_row(job_id, inspect)
