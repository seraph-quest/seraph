"""Consume original completion contributions on the genuine pre-effect owner."""
import asyncio

import pytest

from src.memory import header_bounds
from src.memory.header_bounds import HeaderBoundsError
from src.workflows.job_runtime import (
    original_native_memory_claim_verifier_allocation_numeric_schedule,
    original_native_memory_completion_goal_fence_numeric_schedule,
)
from tests.test_native_memory_producer import actual_memory


@pytest.mark.asyncio
async def test_genuine_completion_claim_and_goal_vectors_preserve_exact_owner(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    reached = []

    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        arguments = dict(certificate=certificate, facts=facts, retention_widths=widths)
        before = certificate.budget.remaining
        for helper in (original_native_memory_claim_verifier_allocation_numeric_schedule,
                original_native_memory_completion_goal_fence_numeric_schedule):
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **{**arguments, "facts": dict(facts)})
            async def child():
                with pytest.raises(HeaderBoundsError):
                    helper(operation, prepared, **arguments)
            await asyncio.create_task(child())
        assert certificate.budget.remaining == before
        # Original closed labels are ASCII<=32; 512 per claim triplet and
        # 768 per Goal intermediate-pair+output-triplet cover both constructors.
        # This is test observation storage, not a factory or fee-list grant.
        certificate.budget.debit(5 * 512 + 9 * 768,
            appearance=("test-original-completion-contributions", "planning"))
        claim = original_native_memory_claim_verifier_allocation_numeric_schedule(operation, prepared, **arguments)
        goal = original_native_memory_completion_goal_fence_numeric_schedule(operation, prepared, **arguments)
        assert len(claim) == 5
        assert [label[1] for phase, label, amount in claim] == [
            "binding-parser", "producer-roster", "authority-json-parser", "grant-generator", "grant-sort"]
        assert claim[-1][2] == claim[-2][2] > 0
        assert run.goal_id is not None and len(goal) == 9
        assert goal[-1][1][-1] == "authority-json-parser"
        assert all(phase == "completion" and type(amount) is int and amount > 0
            for phase, label, amount in claim + goal)
        reached.append(1)
        # Observe actual Source-issued lineage but stop before the real factory
        # can issue capacity or arm any effect; no synthetic future certificate.
        raise HeaderBoundsError("test_completion_owned_contributions_observed")

    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "completion-owned-contributions",
    })
    assert response.status_code == 409 and reached == [1]
