"""Completion capacity cannot promote a genuine unretained effect Source."""
from copy import copy

import pytest

from tests.test_native_memory_producer import actual_memory


@pytest.mark.asyncio
async def test_completion_authority_scope_denies_unretained_and_copied_source_before_debit(
        actual_memory, monkeypatch):
    from src.runtime_plugins import memory_producer as producer, ownership
    from src.memory.header_bounds import HeaderBoundsError
    from src.memory import m5
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original_validate = ownership.validate_invocation
    original_apply = m5._apply_native_memory_mutation_plan
    observed = []
    effects = []

    async def observe_apply(*args, **kwargs):
        effects.append(True)
        return await original_apply(*args, **kwargs)

    async def validate(db, binding, **kwargs):
        owner = producer._MEMORY_AUTHORITY_CAPACITY.get()
        if owner is not None:
            before = owner.budget.remaining
            for candidate in (owner.source, copy(owner.source)):
                with pytest.raises(producer.NativeServiceBlocked):
                    async with producer._memory_completion_authority_capacity(candidate, db):
                        pytest.fail("unretained Source entered completion capacity")
            with pytest.raises(HeaderBoundsError, match="memory_completion_authority_owner_changed"):
                async with producer._memory_completion_authority_capacity(object(), db):
                    pytest.fail("unissued object entered completion capacity")
            assert owner.budget.remaining == before
            assert producer._MEMORY_AUTHORITY_CAPACITY.get() is owner
            observed.append(True)
            raise HeaderBoundsError("completion_capacity_test_stop_before_inventory")
        return await original_validate(db, binding, **kwargs)

    monkeypatch.setattr(ownership, "validate_invocation", validate)
    monkeypatch.setattr(m5, "_apply_native_memory_mutation_plan", observe_apply)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "completion-unretained-negative"})
    assert response.status_code != 200
    assert observed == [True]
    assert effects == []
    assert producer._MEMORY_AUTHORITY_CAPACITY.get() is None
