"""Observe the conservative branch through actual original Source facts."""
import pytest
from src.memory import header_bounds
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins.memory_producer import original_native_memory_dispatch_validation_numeric_schedule
from tests.test_native_memory_producer import actual_memory


@pytest.mark.asyncio
async def test_actual_source_false_cannot_discount_result_or_replace_retained_facts(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original = header_bounds._reserve_native_memory_effect_ledger
    observations = []
    async def observe(db, run, op, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        false = original_native_memory_dispatch_validation_numeric_schedule(op, prepared,
            certificate=certificate, facts=facts, retention_widths=widths, result_present=False)
        true = original_native_memory_dispatch_validation_numeric_schedule(op, prepared,
            certificate=certificate, facts=facts, retention_widths=widths, result_present=True)
        assert false == true
        assert any(appearance == ("native-memory-source","context","result-validation")
            for phase, appearance, upper in false)
        assert any(appearance == ("native-memory-source","retention-locator","field-set")
            for phase, appearance, upper in false)
        remaining = guard.header_budget.remaining
        with pytest.raises(HeaderBoundsError, match="memory_dispatch_validation_numeric_owner_changed"):
            original_native_memory_dispatch_validation_numeric_schedule(op, prepared,
                certificate=certificate, facts=dict(facts), retention_widths=widths, result_present=False)
        assert guard.header_budget.remaining == remaining
        observations.append(1)
        return await original(db, run, op, prepared, existing_references=existing_references,
            before_row=before_row, certificate=certificate)
    monkeypatch.setattr(header_bounds,"_reserve_native_memory_effect_ledger",observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id":task.task_id,"expected_task_revision":task.task_revision,
        "attempt_id":attempt.attempt_id,"idempotency_key":"conservative-dispatch-result"})
    assert response.status_code in (200,409)
    assert observations == [1]


@pytest.mark.asyncio
async def test_genuine_source_empty_journal_prepays_fallback_before_missing_witness(actual_memory, monkeypatch):
    from src.runtime_plugins import memory_producer
    from src.runtime_plugins.dispatch import NativeServiceBlocked
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original = memory_producer._validate_memory_dispatch_source
    original_pay = memory_producer._native_memory_source_pay
    witnessed = []
    async def empty_original_row(db, run, scope):
        # The stock invocation issues the actual Source. Only its already-read
        # local row is adversarially changed; no fake registry, certificate or
        # Source/budget and no write/commit is supplied by this test.
        source = scope.native_memory_source
        assert source is not None and source.claim._host is source.host
        saved = run.checkpoint_receipts_json
        paid = []
        def observe_pay(owner, stage, upper):
            if owner == "dispatch-validation":
                paid.append((stage,upper))
            return original_pay(owner,stage,upper)
        monkeypatch.setattr(memory_producer,"_native_memory_source_pay",observe_pay)
        run.checkpoint_receipts_json = ""
        try:
            with pytest.raises(NativeServiceBlocked, match="native_original_invocation_missing"):
                await original(db,run,scope)
            assert dict(paid)["witness-json-parser"] >= 14
            assert dict(paid)["witness-selected-list"] >= 14
            witnessed.append(1)
            raise NativeServiceBlocked("native_original_invocation_missing")
        finally:
            run.checkpoint_receipts_json = saved
            monkeypatch.setattr(memory_producer,"_native_memory_source_pay",original_pay)
    monkeypatch.setattr(memory_producer,"_validate_memory_dispatch_source",empty_original_row)
    response = await client.post("/api/memory/task-proposals/native",json={
        "task_id":task.task_id,"expected_task_revision":task.task_revision,
        "attempt_id":attempt.attempt_id,"idempotency_key":"empty-original-source-journal"})
    assert response.status_code == 409
    assert witnessed == [1]
