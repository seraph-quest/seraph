"""Observe the real pre-effect accounting metadata owner without issuing fees."""
import asyncio
import copy
from contextlib import asynccontextmanager

import pytest

from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
from src.workspace import accounting_witness
from tests.test_native_memory_producer import actual_memory


@pytest.mark.asyncio
async def test_genuine_accounting_capture_rejects_copied_frame_and_child_owner(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original = accounting_witness._native_memory_accounting_certification_capture
    original_init = accounting_witness._NativeMemoryAccountingTraceCollector.__init__
    original_debit = HeaderReadBudget.debit
    observed = []
    paid = set()

    def debit(frame, upper, *, appearance=None):
        result = original_debit(frame, upper, appearance=appearance)
        if type(appearance) is tuple and appearance[0] == "native-memory-accounting-certification-trace":
            paid.add((id(frame), appearance[1]))
        return result

    def initialize(collector, guard, *args):
        for stage in ("context-wrapper", "empty-list", "collector-construction",
                "collector-weak-reference", "collector-registry-entry", "active-context-token",
                "trace-context-token", "trace-binding-tuple"):
            assert (id(guard.header_budget), stage) in paid
        return original_init(collector, guard, *args)

    monkeypatch.setattr(HeaderReadBudget, "debit", debit)
    monkeypatch.setattr(accounting_witness._NativeMemoryAccountingTraceCollector, "__init__", initialize)

    @asynccontextmanager
    async def observe(guard, operation, certificate):
        async with original(guard, operation, certificate):
            if operation is None:
                yield
                return
            collector = accounting_witness._NATIVE_MEMORY_ACCOUNTING_TRACE_ACTIVE.get()
            collector._checked()
            before = guard.header_budget.remaining
            copied = copy.copy(collector)
            with pytest.raises(HeaderBoundsError, match="memory_accounting_certification_numeric_owner_changed"):
                copied.append_charge(("unissued-accounting-observation",), 1)
            frame = collector.budget
            collector.budget = object()
            try:
                with pytest.raises(HeaderBoundsError, match="memory_accounting_certification_numeric_owner_changed"):
                    collector.append_charge(("foreign-frame",), 1)
            finally:
                collector.budget = frame
            async def child():
                with pytest.raises(HeaderBoundsError, match="memory_accounting_certification_numeric_owner_changed"):
                    collector.append_charge(("foreign-task",), 1)
            await asyncio.create_task(child())
            assert guard.header_budget.remaining == before and collector.entries == []
            yield
            collector._checked()
            assert collector.entries
            assert all(type(amount) is int and amount >= 0 for _, amount in collector.entries)
            observed.append((operation, certificate))
        lineage = guard._native_memory_accounting_certification_numeric
        assert lineage[0] is operation and lineage[2] is certificate
        assert lineage[3] is guard.header_budget and lineage[4] is asyncio.current_task()
        raise HeaderBoundsError("accounting_certification_test_observation_complete")

    monkeypatch.setattr(accounting_witness, "_native_memory_accounting_certification_capture", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "accounting-certification-lineage",
    })
    # This observes the genuine prefix; it does not substitute a complete
    # composer, permission, Source, claim, certificate or successful effect.
    assert observed
    assert response.status_code == 409
