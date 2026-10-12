"""Original capture preflight construction uses genuine Source-only capacity."""
import asyncio
import pytest
from src.memory import header_bounds, composition_headers
from src.memory.header_bounds import HeaderBoundsError
from src.workspace import accounting_witness
from tests.test_native_memory_producer import actual_memory


def test_unissued_original_preflight_constructor_denies_before_database():
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued operation reached database")
    with pytest.raises(HeaderBoundsError):
        accounting_witness._native_memory_original_preflight_construction_numeric_upper(
            Poison(), object(), certificate=object(), facts=object(), retention_widths=object())


@pytest.mark.asyncio
async def test_genuine_original_preflight_scalar_accepts_without_builder_or_debit(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    helper = accounting_witness._native_memory_original_preflight_construction_numeric_upper
    reached = []

    def forbidden(*args, **kwargs):
        raise AssertionError("constructor scalar built its forecast vector")

    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        arguments = dict(certificate=certificate, facts=facts, retention_widths=widths)
        before = certificate.budget.remaining
        monkeypatch.setattr(accounting_witness, "_native_memory_original_preflight_numeric_entries", forbidden)
        monkeypatch.setattr(accounting_witness, "_native_memory_reference_preflight_numeric_entries", forbidden)
        with pytest.raises(HeaderBoundsError):
            helper(operation, prepared, **{**arguments, "facts": dict(facts)})
        with pytest.raises(HeaderBoundsError):
            helper(operation, prepared, **{**arguments, "retention_widths": dict(widths)})
        token = composition_headers._CURRENT.set(None)
        try:
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **arguments)
        finally:
            composition_headers._CURRENT.reset(token)
        async def child():
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **arguments)
        await asyncio.create_task(child())
        capacity = helper(operation, prepared, **arguments)
        assert type(capacity) is int and capacity > 0
        assert certificate.budget.remaining == before
        # Parent pays capacity before the ONE Original capture builder.
        # This pure observation makes no future cert/ledger or fit claim.
        reached.append(capacity)
        raise HeaderBoundsError("test_original_preflight_constructor_observed")

    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "original-preflight-constructor",
    })
    assert response.status_code == 409 and len(reached) == 1
