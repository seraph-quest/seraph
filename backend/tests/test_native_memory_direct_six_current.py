"""Genuine Current planning retains six full builds without constructing33."""
import asyncio
import pytest

from src.memory import header_bounds, composition_headers
from src.memory.header_bounds import HeaderBoundsError
from src.workspace import accounting_witness
from tests.test_native_memory_producer import actual_memory


def test_unissued_current_planning_denies_before_database_or_source():
    class Unissued:
        @property
        def db(self):
            raise AssertionError("unissued operation reached private database")
    with pytest.raises(HeaderBoundsError, match="memory_current_numeric_owner_unavailable"):
        accounting_witness._native_memory_six_current_construction_numeric_entries(
            Unissued(), object(), certificate=object(), retention_widths=object(), _construction_only=True)


@pytest.mark.asyncio
async def test_real_source_prices_before_direct_six_and_preserves_all_phase_builds(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    direct = accounting_witness._native_memory_six_current_construction_numeric_entries
    original_preflight = accounting_witness._native_memory_reference_preflight_numeric_entries
    reached, preflights = [], []
    prepaid = False

    def forbidden33(*args, **kwargs):
        raise AssertionError("successful Current constructed unused33 blocks")

    def preflight(*args, **kwargs):
        assert prepaid
        preflights.append(1)
        return original_preflight(*args, **kwargs)

    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        nonlocal prepaid
        guard = db.info["composition_guard"]
        widths = guard._native_memory_retention_numeric_facts[3]
        arguments = dict(certificate=certificate, retention_widths=widths, _construction_only=True)
        before = certificate.budget.remaining
        with pytest.raises(HeaderBoundsError):
            direct(operation, prepared, **{**arguments, "retention_widths": dict(widths)})
        with pytest.raises(HeaderBoundsError, match="memory_current_construction_mode_unavailable"):
            direct(operation, prepared, **{**arguments, "_construction_only": 1})
        token = composition_headers._CURRENT.set(None)
        try:
            with pytest.raises(HeaderBoundsError):
                direct(operation, prepared, **arguments)
            with pytest.raises(HeaderBoundsError):
                direct(operation, prepared, certificate=certificate, retention_widths=widths)
            with pytest.raises(HeaderBoundsError):
                accounting_witness._native_memory_successful_current_lifecycle_numeric_entries(
                    operation, prepared, certificate=certificate, retention_widths=widths)
            assert preflights == [] and certificate.budget.remaining == before
        finally:
            composition_headers._CURRENT.reset(token)
        original_budget = guard.header_budget
        guard.header_budget = object()
        try:
            with pytest.raises(HeaderBoundsError):
                direct(operation, prepared, **arguments)
        finally:
            guard.header_budget = original_budget
        async def child():
            with pytest.raises(HeaderBoundsError):
                direct(operation, prepared, **arguments)
        await asyncio.create_task(child())
        capacity = direct(operation, prepared, **arguments)
        assert type(capacity) is int and capacity > 0
        assert certificate.budget.remaining == before and preflights == []
        # Actual owner frame pays complete helper construction before ANY
        # preflight/lex/splice/vector arrays. No Source or permission supplied.
        certificate.budget.debit(capacity, appearance=("test-direct-six-current", "construction"))
        prepaid = True
        entries = accounting_witness._native_memory_successful_current_lifecycle_numeric_entries(
            operation, prepared, certificate=certificate, retention_widths=widths)
        for phase in ("capture", "completion"):
            selected = [(label, upper) for entry_phase, label, upper in entries if entry_phase == phase]
            assert sum(label == ("memory-current-allocation", run.run_identity, "count-update")
                for label, upper in selected) == 6
            assert sum(label is None for label, upper in selected) == 7
            assert sum(label == ("memory-current-allocation", run.run_identity, "projection-copy")
                for label, upper in selected) == 1
        assert preflights == [1, 1]
        reached.append(1)
        raise HeaderBoundsError("test_direct_six_current_observed")

    monkeypatch.setattr(accounting_witness, "_native_memory_current_construction_numeric_entries", forbidden33)
    monkeypatch.setattr(accounting_witness, "_native_memory_reference_preflight_numeric_entries", preflight)
    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "direct-six-current",
    })
    assert response.status_code == 409 and reached == [1]
