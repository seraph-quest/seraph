"""Original Source locator frame forwarding; no additional queries."""
import asyncio
from copy import copy
import pytest
from src.memory import composition_headers
from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
from src.runtime_plugins import memory_producer
from tests.test_native_memory_producer import actual_memory


def test_unissued_locator_payer_denies_before_db():
    class Poison:
        @property
        def info(self):
            raise AssertionError("unissued payer reached DB")
    with pytest.raises(HeaderBoundsError):
        memory_producer._native_memory_original_lookup_payer(Poison(), object())


@pytest.mark.asyncio
async def test_actual_original_locator_pays_eight_with_current_budget_absent(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original_locator = memory_producer._memory_locator_rows
    original_debit = HeaderReadBudget.debit
    observed = []
    charges = []
    def debit(frame, amount, *, appearance=None):
        if type(appearance) is tuple and appearance[0] == "native-memory-locator":
            charges.append((appearance, amount))
        return original_debit(frame, amount, appearance=appearance)
    monkeypatch.setattr(HeaderReadBudget, "debit", debit)
    async def observe(db, model, fields, *predicates, limit=129, _native_memory_operation=None):
        if _native_memory_operation is None:
            return await original_locator(db, model, fields, *predicates, limit=limit)
        operation = _native_memory_operation
        frame = operation.source.admission.header_budget
        before = frame.remaining
        with pytest.raises(HeaderBoundsError):
            memory_producer._native_memory_original_lookup_payer(db, copy(operation))
        async def child():
            with pytest.raises(HeaderBoundsError):
                memory_producer._native_memory_original_lookup_payer(db, operation)
        await asyncio.create_task(child())
        token = composition_headers._CURRENT.set(HeaderReadBudget())
        try:
            with pytest.raises(HeaderBoundsError):
                memory_producer._native_memory_original_lookup_payer(db, operation)
        finally:
            composition_headers._CURRENT.reset(token)
        assert frame.remaining == before
        token = composition_headers._CURRENT.set(None)
        try:
            begin = len(charges)
            result = await original_locator(db, model, fields, *predicates, limit=limit,
                _native_memory_operation=operation)
        finally:
            composition_headers._CURRENT.reset(token)
        stages = [appearance[3] for appearance, amount in charges[begin:]]
        assert stages == ["header-delivery", "header-row-copy", "header-encoded-copy",
            "delivery", "row-copy", "encoded-copy", "result-construction", "return"]
        assert frame.remaining < before
        observed.append(result)
        raise HeaderBoundsError("test_original_locator_paid")
    monkeypatch.setattr(memory_producer, "_memory_locator_rows", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "original-reference-payer"})
    assert response.status_code == 409 and len(observed) == 1
