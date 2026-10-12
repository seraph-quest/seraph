"""BODY forecast construction is gated before any forecast arrays are built."""
import asyncio
import pytest
from src.memory import header_bounds, composition_headers
from src.memory.header_bounds import HeaderBoundsError
from src.workspace import accounting_witness
from tests.test_native_memory_producer import actual_memory


def test_unissued_body_construction_denies_before_database():
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued operation touched private database")
    with pytest.raises(HeaderBoundsError):
        accounting_witness._native_memory_generic_body_construction_numeric_upper(
            Poison(), object(), certificate=object(), facts=object(), retention_widths=object())


@pytest.mark.asyncio
async def test_genuine_body_scalar_no_forecast_or_debit_and_foreign_lineage_denied(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    helper = accounting_witness._native_memory_generic_body_construction_numeric_upper
    reached = []

    def forbidden(*args, **kwargs):
        raise AssertionError("scalar constructed a forecast vector")

    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        arguments = dict(certificate=certificate, facts=facts, retention_widths=widths)
        before = certificate.budget.remaining
        monkeypatch.setattr(accounting_witness, "_native_memory_generic_future_numeric_schedule", forbidden)
        monkeypatch.setattr(accounting_witness, "_native_memory_postoriginal_cold_numeric_entries", forbidden)
        monkeypatch.setattr(accounting_witness, "_original_native_row_digest_entries", forbidden)
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
        old = guard._native_memory_retained_proofs
        guard._native_memory_retained_proofs = (old[0], old[1], (object(),))
        try:
            with pytest.raises(HeaderBoundsError, match="memory_body_construction_affected_unavailable"):
                helper(operation, prepared, **arguments)
        finally:
            guard._native_memory_retained_proofs = old
        upper = helper(operation, prepared, **arguments)
        assert type(upper) is int and upper > 0
        assert certificate.budget.remaining == before
        # Numerical constructor acceptance is separate from actual full-U fit.
        # Parent must debit before the original BODY schedule; no fake ledger
        # or successful mutation is issued by this observation.
        reached.append(upper)
        raise HeaderBoundsError("test_body_constructor_observed")

    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "body-constructor",
    })
    assert response.status_code == 409 and len(reached) == 1
