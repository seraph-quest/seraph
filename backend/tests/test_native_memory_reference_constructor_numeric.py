"""Registered original reference forecast construction; tests are offline-only."""
import asyncio
import pytest
from src.memory import composition_headers, header_bounds
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from tests.test_native_memory_producer import actual_memory


def test_unissued_reference_constructor_denies_before_source_or_db():
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued lookup reached db")
    with pytest.raises(HeaderBoundsError):
        memory_producer.original_native_memory_reference_lookup_construction_numeric_upper(
            Poison(), object(), certificate=object(), facts=object(), retention_widths=object())


@pytest.mark.asyncio
async def test_genuine_reference_constructor_scalar_does_not_parse_candidate_or_query(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    helper = memory_producer.original_native_memory_reference_lookup_construction_numeric_upper
    observed = []
    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        kwargs = dict(certificate=certificate, facts=guard._native_memory_source_capacity_facts[3],
            retention_widths=guard._native_memory_retention_numeric_facts[3])
        before = certificate.budget.remaining
        def forbidden(*args, **kw):
            raise AssertionError("scalar parsed candidate or constructed forecast")
        monkeypatch.setattr(memory_producer.NativeMemoryMutationAdmission, "candidate", forbidden)
        monkeypatch.setattr(memory_producer, "original_native_memory_source_candidate_numeric_entries", forbidden)
        monkeypatch.setattr(memory_producer, "_memory_locator_rows", forbidden)
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
        assert memory_producer.original_native_memory_source_candidate_construction_numeric_upper(
            operation, prepared, **kwargs) == 3056
        assert type(capacity) is int and capacity > 0
        assert certificate.budget.remaining == before
        assert prepared._method == "memory.propose" and prepared._result["status"] == "proposed"
        observed.append(capacity)
        raise HeaderBoundsError("test_reference_constructor_observed")
    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "reference-construction-observer",
    })
    assert response.status_code == 409 and len(observed) == 1
