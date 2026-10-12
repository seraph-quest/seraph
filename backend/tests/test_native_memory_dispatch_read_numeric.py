"""Original Source/DB trace tests; no fabricated Source or numeric ledger."""
import asyncio
from collections import Counter
from types import MappingProxyType
import pytest
from src.memory import header_bounds
from src.memory.header_bounds import HeaderBoundsError, OPERATOR_SESSION, RUNTIME_COMPOSITION
from src.runtime_plugins import memory_producer
from tests.test_native_memory_producer import actual_memory


def test_two_pk_paths_retain_every_original_schema_header_and_state_occurrence():
    from src.runtime_plugins.ownership import DOMAINS
    entries = tuple(memory_producer._native_memory_dispatch_read_label_roster("actual-root-id"))
    counts = Counter(entries)
    assert len(entries) == 69
    assert counts[("exact-metadata", "initial-state")] == 2
    assert counts[("exact-metadata", "validation-state")] == 2
    assert counts[("exact-header", OPERATOR_SESSION.table, OPERATOR_SESSION.key, ("actual-root-id",))] == 1
    assert counts[("exact-header", RUNTIME_COMPOSITION.table, RUNTIME_COMPOSITION.key, tuple(DOMAINS))] == 1
    assert sum(label[:2] == ("exact-metadata", "row-header") for label in entries) == 45


@pytest.mark.asyncio
async def test_unregistered_trace_owner_denies_without_reading_its_source():
    class Unissued:
        @property
        def source(self):
            raise AssertionError("unregistered owner reached private Source")
    with pytest.raises(HeaderBoundsError, match="memory_dispatch_validation_numeric_owner_changed"):
        await memory_producer.original_native_memory_dispatch_validation_read_numeric_schedule(
            Unissued(), object(), certificate=object(), facts=MappingProxyType({}),
            retention_widths=MappingProxyType({}))


@pytest.mark.asyncio
async def test_genuine_source_read_trace_uses_exact_payers_and_denies_foreign_lineage_task(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original_read = memory_producer.original_native_memory_dispatch_validation_read_numeric_schedule
    original_certify = header_bounds.HeaderReadBudget.certify
    original_schedule = memory_producer.original_native_memory_dispatch_validation_numeric_schedule
    original_debit = header_bounds.HeaderReadBudget.debit
    planning_paid = set()
    def debit(frame, amount, *, appearance):
        result = original_debit(frame, amount, appearance=appearance)
        if appearance == ("memory-dispatch-read-trace", "planning-before-allocation"):
            planning_paid.add(id(frame))
        return result
    def schedule(op, prepared, **kwargs):
        if inside:
            raise AssertionError("read helper rebuilt factory's R445 vector")
        return original_schedule(op, prepared, **kwargs)
    monkeypatch.setattr(header_bounds.HeaderReadBudget, "debit", debit)
    monkeypatch.setattr(memory_producer, "original_native_memory_dispatch_validation_numeric_schedule", schedule)
    reached = []
    inside = False
    payers = []
    async def certify(frame, db, descriptor, row_ids, *, _header_budget=None):
        if inside:
            assert descriptor is OPERATOR_SESSION or descriptor is RUNTIME_COMPOSITION
            assert _header_budget is frame
            payers.append((descriptor, tuple(row_ids)))
        return await original_certify(frame, db, descriptor, row_ids, _header_budget=_header_budget)
    monkeypatch.setattr(header_bounds.HeaderReadBudget, "certify", certify)
    async def observe(op, prepared, *, certificate, facts, retention_widths):
        nonlocal inside
        db = op.db
        guard = db.info["composition_guard"]
        widths = retention_widths
        arguments = dict(certificate=certificate, facts=facts, retention_widths=widths)
        remaining = guard.header_budget.remaining
        with pytest.raises(HeaderBoundsError, match="memory_dispatch_validation_numeric_owner_changed"):
            await original_read(
                op, prepared, **{**arguments, "facts": dict(facts)})
        async def child():
            with pytest.raises(HeaderBoundsError, match="memory_dispatch_validation_numeric_owner_changed"):
                await original_read(op, prepared, **arguments)
        await asyncio.create_task(child())
        assert guard.header_budget.remaining == remaining
        assert payers == []
        retained = guard._native_memory_dispatch_allocation_numeric
        assert retained[:5] == (op, prepared, certificate, facts, widths)
        del guard._native_memory_dispatch_allocation_numeric
        try:
            with pytest.raises(HeaderBoundsError, match="memory_dispatch_read_allocation_lineage_unavailable"):
                await original_read(op, prepared, **arguments)
            assert guard.header_budget.remaining == remaining
        finally:
            guard._native_memory_dispatch_allocation_numeric = retained
        inside = True
        try:
            entries = await original_read(op, prepared, **arguments)
        finally:
            inside = False
        assert id(guard.header_budget) in planning_paid
        assert guard._native_memory_dispatch_allocation_numeric is retained
        assert len(entries) == 69
        assert Counter(label for label, upper in entries) == Counter(
            memory_producer._native_memory_dispatch_read_label_roster(op.source.operator.session_id))
        assert len(payers) == 2
        assert guard.header_budget.remaining < remaining
        assert guard._native_memory_dispatch_read_numeric[:4] == (op, prepared, certificate, facts)
        assert guard._native_memory_dispatch_read_numeric[-1] is entries
        assert db.sync_session.get_transaction() is op.transaction
        reached.append(1)
        return entries
    monkeypatch.setattr(memory_producer, "original_native_memory_dispatch_validation_read_numeric_schedule", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "dispatch-original-read-vector"})
    assert response.status_code in (200, 409)
    assert reached == [1]
