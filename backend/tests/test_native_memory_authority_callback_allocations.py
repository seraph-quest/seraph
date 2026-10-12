"""The real Source callback's resource context cannot be copied or redirected."""
import asyncio
from copy import copy
from dataclasses import replace

import pytest

from tests.test_native_memory_producer import actual_memory


@pytest.mark.asyncio
async def test_original_callback_rejects_copied_certificate_owner_db_and_task_before_delivery(
        actual_memory, monkeypatch):
    from src.runtime_plugins import memory_producer as producer
    from src.runtime_plugins import ownership
    from src.memory.header_bounds import HeaderBoundsError
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    actual = producer._native_memory_authority_owner_pay
    observed = []
    from src.memory import m5
    actual_apply = m5._apply_native_memory_mutation_plan
    apply_calls = []
    async def observe_apply(*args, **kwargs):
        apply_calls.append(True)
        return await actual_apply(*args, **kwargs)
    monkeypatch.setattr(m5, "_apply_native_memory_mutation_plan", observe_apply)
    original_validate = ownership.validate_invocation
    foreign_checks = []

    async def validate(db, binding, **kwargs):
        owner = producer._MEMORY_AUTHORITY_CAPACITY.get()
        if owner is not None:
            before = owner.budget.remaining
            async def foreign_task():
                with pytest.raises(HeaderBoundsError, match="memory_authority_numeric_owner_changed"):
                    actual(db, "inventory", certificate=None)
            await asyncio.create_task(foreign_task())
            assert owner.budget.remaining == before
            foreign_checks.append(True)
        return await original_validate(db, binding, **kwargs)

    def inspect(db, stage, **kwargs):
        if stage != "inventory" or producer._MEMORY_AUTHORITY_CAPACITY.get() is None:
            return actual(db, stage, **kwargs)
        owner = producer._native_memory_authority_capacity_checked(db)
        before = owner.budget.remaining
        clone = replace(owner)
        token = producer._MEMORY_AUTHORITY_CAPACITY.set(clone)
        try:
            with pytest.raises(HeaderBoundsError, match="memory_authority_numeric_owner_changed"):
                actual(db, stage, **kwargs)
        finally:
            producer._MEMORY_AUTHORITY_CAPACITY.reset(token)
        with pytest.raises(HeaderBoundsError, match="memory_authority_numeric_owner_changed"):
            actual(object(), stage, **kwargs)
        bad = replace(kwargs["certificate"])
        with pytest.raises(HeaderBoundsError, match="memory_authority_numeric_certificate_changed"):
            actual(db, stage, certificate=bad)
        assert owner.budget.remaining == before
        from src.memory.header_bounds import _MEMORY_CHARGE_TRACE
        collector = _MEMORY_CHARGE_TRACE.get()[1]
        foreign_collector = copy(collector)
        with pytest.raises(HeaderBoundsError, match="memory_authority_numeric_trace_changed"):
            foreign_collector.append_charge(None, 0)
        assert owner.budget.remaining == before
        actual(db, stage, **kwargs)
        assert owner.budget.remaining < before
        observed.append(owner)
        from src.memory.composition_headers import _common_trace_label_width
        appearance = ("authority-test", "spend-original-frame")
        pair = 3 + _common_trace_label_width(appearance) + 20
        amount = owner.budget.remaining - (2 * pair + 1)
        assert amount >= 0
        # Spend through the real frame, including the collector's two actual
        # storage fees, rather than replacing a ledger or editing remaining.
        owner.budget.debit(amount, appearance=appearance)
        assert owner.budget.remaining == 0
        count = len(collector.entries)
        with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
            collector.append_charge(("authority-test", "must-not-append"), 0)
        assert len(collector.entries) == count
        # This is the genuine original payer, immediately before the original
        # inventory body SELECT; stop without replacing Source or any grant.
        raise HeaderBoundsError("authority_callback_test_stop_before_inventory_body")

    monkeypatch.setattr(producer, "_native_memory_authority_owner_pay", inspect)
    monkeypatch.setattr(ownership, "validate_invocation", validate)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "authority-copy-negative"})
    assert response.status_code != 200
    assert len(observed) == 1
    assert foreign_checks == [True]
    assert apply_calls == []
    owner = observed[0]
    # Exit invalidates the resource context even on an ordinary denial.
    assert producer._MEMORY_AUTHORITY_CAPACITY.get() is None
    assert producer._MEMORY_AUTHORITY_OWNERS.get(id(owner)) is None
