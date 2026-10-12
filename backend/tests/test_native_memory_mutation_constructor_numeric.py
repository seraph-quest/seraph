"""ONE actual mutation forecast constructor, observed before effects."""
import asyncio
import pytest
from src.memory import composition_headers, header_bounds
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from tests.test_native_memory_producer import actual_memory


def test_unissued_mutation_constructor_denies_before_db_or_audit():
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued operation reached DB/audit")
    with pytest.raises(HeaderBoundsError):
        memory_producer._native_memory_mutation_allocation_construction_numeric_upper(
            Poison(), object(), certificate=object(), facts=object(), retention_widths=object())

@pytest.mark.asyncio
async def test_genuine_propose_constructor_scalar_does_not_construct_vector(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    helper = memory_producer._native_memory_mutation_allocation_construction_numeric_upper
    builder = memory_producer._native_memory_mutation_allocation_entries
    observed = []
    def forbidden(*args, **kwargs):
        raise AssertionError("scalar constructed mutation forecast")
    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        kwargs = dict(certificate=certificate, facts=guard._native_memory_source_capacity_facts[3],
            retention_widths=guard._native_memory_retention_numeric_facts[3])
        before = certificate.budget.remaining
        monkeypatch.setattr(memory_producer, "_native_memory_mutation_allocation_entries", forbidden)
        with pytest.raises(HeaderBoundsError):
            helper(operation, prepared, **{**kwargs, "facts": dict(kwargs["facts"])})
        with pytest.raises(HeaderBoundsError):
            helper(operation, prepared, **{**kwargs, "retention_widths": dict(kwargs["retention_widths"])})
        token = composition_headers._CURRENT.set(None)
        try:
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **kwargs)
        finally:
            composition_headers._CURRENT.reset(token)
        async def child():
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **kwargs)
        await asyncio.create_task(child())
        capacity = helper(operation, prepared, **kwargs)
        assert type(capacity) is int and capacity > 0
        assert certificate.budget.remaining == before
        monkeypatch.setattr(memory_producer, "_native_memory_mutation_allocation_entries", builder)
        certificate.budget.debit(capacity, appearance=("test-mutation-forecast", "constructor"))
        entries = memory_producer.original_native_memory_mutation_allocation_numeric_schedule(
            operation, prepared, **kwargs)
        assert prepared._method == "memory.propose" and len(entries) == 10
        assert all(phase == "effect" for phase, _, _ in entries)
        assert sum(label[1] == "audit-event-kwargs" for _, label, _ in entries) == 1
        assert sum(label[1] == "prepared-text-construction" for _, label, _ in entries) == 1
        observed.append(capacity)
        raise HeaderBoundsError("test_mutation_constructor_observed")
    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "mutation-construction-observer",
    })
    assert response.status_code == 409 and len(observed) == 1
