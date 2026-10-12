"""Original initial claim observations, never constructed Source/trace receipts."""
import pytest
from src.memory import header_bounds
from src.memory.header_bounds import HeaderBoundsError
from src.workflows.job_runtime import original_native_memory_completion_claim_read_numeric_schedule
from tests.test_native_memory_producer import actual_memory

@pytest.mark.asyncio
async def test_original_claim_order_is_retained_on_genuine_source(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original_factory = header_bounds._reserve_native_memory_effect_ledger
    reached = []
    async def observe(db, run, op, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        remaining = certificate.budget.remaining
        entries = original_native_memory_completion_claim_read_numeric_schedule(op, prepared,
            certificate=certificate, facts=facts, retention_widths=widths)
        assert entries is op.source.claim._memory_claim_read_numeric[7]
        assert certificate.budget.remaining == remaining
        headers = [label[1] for label, amount in entries
            if type(label) is tuple and label[0] == "exact-header"]
        assert headers == ["operator_sessions", "runtime_composition_states", "operator_sessions"]
        for stage in ("root-now", "root-query", "root-materialization"):
            assert sum(label == ("native-memory-original-claim",stage) for label,amount in entries) == 1
        root_copies = [amount for label, amount in entries
            if label in (("native-memory-original-claim","root-query"),
                ("native-memory-original-claim","root-materialization"))]
        assert len(root_copies) == 2 and root_copies[0] == root_copies[1] > 0

        with pytest.raises(HeaderBoundsError):
            original_native_memory_completion_claim_read_numeric_schedule(op, prepared,
                certificate=certificate, facts=dict(facts), retention_widths=widths)
        assert certificate.budget.remaining == remaining
        reached.append(1)
        return await original_factory(db, run, op, prepared, existing_references=existing_references,
            before_row=before_row, certificate=certificate)
    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={"task_id":task.task_id,
        "expected_task_revision":task.task_revision,"attempt_id":attempt.attempt_id,
        "idempotency_key":"initial-native-claim-order"})
    assert response.status_code in (200,409)
    assert reached == [1]
