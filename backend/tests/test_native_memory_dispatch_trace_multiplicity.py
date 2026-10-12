"""Excess allowed allocation labels must fail on genuine original Source."""
import pytest
from src.memory import header_bounds
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from tests.test_native_memory_producer import actual_memory

@pytest.mark.asyncio
async def test_genuine_validator_excess_allowed_label_is_not_hidden_by_unused_slots(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original_read = memory_producer.original_native_memory_dispatch_validation_read_numeric_schedule
    original_validate = memory_producer._validate_memory_dispatch_source
    reached = []
    async def observe(op, prepared, *, certificate, facts, retention_widths):
        db = op.db
        guard = db.info["composition_guard"]
        async def excess(actual_db, actual_run, scope):
            # Original current-now occurs once. Add just one, well below the
            # global trace capacity; rejection must use per-label multiplicity.
            guard.header_budget.debit(44, appearance=("native-memory-source", "dispatch-validation", "current-now"))
            return await original_validate(actual_db, actual_run, scope)
        monkeypatch.setattr(memory_producer, "_validate_memory_dispatch_source", excess)
        try:
            with pytest.raises(HeaderBoundsError, match="memory_dispatch_read_numeric_roster_changed"):
                await original_read(op, prepared, certificate=certificate,
                    facts=facts, retention_widths=retention_widths)
        finally:
            monkeypatch.setattr(memory_producer, "_validate_memory_dispatch_source", original_validate)
        reached.append(1)
        # Stop the attempt before effects; this test never installs a ledger.
        raise HeaderBoundsError("test_excess_trace_rejected")
    monkeypatch.setattr(memory_producer, "original_native_memory_dispatch_validation_read_numeric_schedule", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id":task.task_id,"expected_task_revision":task.task_revision,
        "attempt_id":attempt.attempt_id,"idempotency_key":"dispatch-excess-label"})
    assert response.status_code == 409
    assert reached == [1]


@pytest.mark.asyncio
async def test_genuine_validator_larger_debit_cannot_use_same_paid_label_count(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original_read = memory_producer.original_native_memory_dispatch_validation_read_numeric_schedule
    original_validate = memory_producer._validate_memory_dispatch_source
    original_debit = header_bounds.HeaderReadBudget.debit
    reached, increased = [], []
    inside = False
    target = ("native-memory-source", "dispatch-validation", "current-now")

    def debit(frame, amount, *, appearance):
        if inside and appearance == target:
            assert amount == 44
            amount = 45
            increased.append(1)
        return original_debit(frame, amount, appearance=appearance)

    async def observe(op, prepared, *, certificate, facts, retention_widths):
        nonlocal inside
        retained = op.db.info["composition_guard"]._native_memory_dispatch_allocation_numeric[5]
        assert [amount for phase, label, amount in retained if label == target] == [44]
        async def larger(actual_db, actual_run, scope):
            nonlocal inside
            inside = True
            try:
                return await original_validate(actual_db, actual_run, scope)
            finally:
                inside = False
        monkeypatch.setattr(memory_producer, "_validate_memory_dispatch_source", larger)
        try:
            with pytest.raises(HeaderBoundsError, match="memory_dispatch_read_numeric_paid_amount_exceeded"):
                await original_read(op, prepared, certificate=certificate,
                    facts=facts, retention_widths=retention_widths)
        finally:
            monkeypatch.setattr(memory_producer, "_validate_memory_dispatch_source", original_validate)
        reached.append(1)
        raise HeaderBoundsError("test_larger_actual_debit_rejected")

    monkeypatch.setattr(header_bounds.HeaderReadBudget, "debit", debit)
    monkeypatch.setattr(memory_producer, "original_native_memory_dispatch_validation_read_numeric_schedule", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "dispatch-larger-paid-amount",
    })
    assert response.status_code == 409 and reached == [1] and increased == [1]
