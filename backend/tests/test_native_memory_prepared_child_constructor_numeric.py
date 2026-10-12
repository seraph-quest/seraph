"""Original registered prepared-child constructors; Source only until released."""
import asyncio
import pytest
from src.memory import composition_headers, header_bounds, m5
from src.memory.header_bounds import HeaderBoundsError
from tests.test_native_memory_producer import actual_memory


def test_unissued_prepared_child_constructor_denies_before_plan_access():
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued constructor accessed body")
    with pytest.raises(HeaderBoundsError):
        m5.original_native_memory_prepared_construction_numeric_upper(Poison(), object(),
            certificate=object(), facts=object(), retention_widths=object())


@pytest.mark.asyncio
async def test_genuine_prepared_child_constructor_is_scalar_before_original_builders(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    from src.audit.repository import original_native_memory_prepared_audit_numeric_schedule
    helper = m5.original_native_memory_prepared_construction_numeric_upper
    width = m5._native_prepared_value_width
    observed = []
    def forbidden(*args, **kwargs):
        raise AssertionError("scalar invoked allocating original width/schedule")
    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        kwargs = dict(certificate=certificate, facts=guard._native_memory_source_capacity_facts[3],
            retention_widths=guard._native_memory_retention_numeric_facts[3])
        before = certificate.budget.remaining
        monkeypatch.setattr(m5, "_native_prepared_value_width", forbidden)
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
        capacities = (helper(operation, prepared, **kwargs),
            helper(operation, prepared, **kwargs, kind="validation", consumed=False),
            helper(operation, prepared, **kwargs, kind="validation", consumed=True),
            helper(operation, prepared, **kwargs, kind="audit"))
        assert all(type(value) is int and value > 0 for value in capacities)
        assert capacities[1] > capacities[2]
        assert certificate.budget.remaining == before
        monkeypatch.setattr(m5, "_native_prepared_value_width", width)
        for value in capacities:
            certificate.budget.debit(value, appearance=("test-prepared-child", "constructor"))
        assert len(m5.original_native_memory_prepared_numeric_schedule(prepared)) == 8
        assert len(m5.original_native_memory_prepared_validation_numeric_schedule(prepared)) == 4
        assert len(m5.original_native_memory_prepared_validation_numeric_schedule(prepared, consumed=True)) == 3
        assert len(original_native_memory_prepared_audit_numeric_schedule(prepared)) == 2
        observed.append(capacities)
        raise HeaderBoundsError("test_prepared_child_constructor_observed")
    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "prepared-child-construction-observer",
    })
    assert response.status_code == 409 and len(observed) == 1
