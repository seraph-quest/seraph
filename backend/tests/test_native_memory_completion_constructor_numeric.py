"""Original completion constructor capacity; no fake ledger/Source success."""
import asyncio
import pytest
from src.memory import composition_headers, header_bounds
from src.memory.header_bounds import HeaderBoundsError
from src.workflows import job_runtime
from tests.test_native_memory_producer import actual_memory

HELPERS = (
    job_runtime.original_native_memory_completion_claim_read_numeric_schedule,
    job_runtime.original_native_memory_claim_verifier_allocation_numeric_schedule,
    job_runtime.original_native_memory_completion_goal_fence_numeric_schedule,
    job_runtime.original_native_memory_completion_output_numeric_schedule,
)

@pytest.mark.parametrize("helper", HELPERS)
@pytest.mark.parametrize("construction", [False, True])
def test_unissued_completion_constructor_denies_before_db(helper, construction):
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued operation reached DB")
    with pytest.raises(HeaderBoundsError):
        helper(Poison(), object(), certificate=object(), facts=object(),
            retention_widths=object(), _construction_only=construction)

@pytest.mark.asyncio
async def test_genuine_completion_planning_does_not_construct_future_vectors(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    from src.workspace import accounting_witness
    from src.runtime_plugins import memory_producer
    observed = []
    def forbidden(*args, **kwargs):
        raise AssertionError("scalar constructed a generic future row map or binding vector")
    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        monkeypatch.setattr(accounting_witness, "_native_memory_generic_future_bound_inputs", forbidden)
        monkeypatch.setattr(memory_producer, "_native_memory_dispatch_binding_json_entries", forbidden)
        before = certificate.budget.remaining
        scratch = job_runtime._native_memory_claim_checkpoint_scratch_upper(operation.source, db)
        methods = operation.source.claim.checkpoint["payload"]["allowed_child_methods"]
        assert scratch == (85 + 3 * len(methods)) * 41
        assert certificate.budget.remaining == before
        import copy
        with pytest.raises(HeaderBoundsError):
            job_runtime._native_memory_claim_checkpoint_scratch_upper(copy.copy(operation.source), db)
        async def wrong_scratch_task():
            with pytest.raises(HeaderBoundsError):
                job_runtime._native_memory_claim_checkpoint_scratch_upper(operation.source, db)
        await asyncio.create_task(wrong_scratch_task())
        for helper in HELPERS:
            kwargs = dict(certificate=certificate, facts=facts, retention_widths=widths,
                _construction_only=True)
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **{**kwargs, "facts": dict(facts)})
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **{**kwargs, "retention_widths": dict(widths)})
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
            assert type(capacity) is int and capacity >= 0
            assert certificate.budget.remaining == before
            observed.append(capacity)
        raise HeaderBoundsError("test_completion_construction_observed")
    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "completion-construction-observer",
    })
    assert response.status_code == 409 and len(observed) == 4


def test_unissued_checkpoint_scratch_denies_before_db_or_checkpoint():
    class Poison:
        @property
        def info(self):
            raise AssertionError("unissued Source reached DB")
    with pytest.raises(HeaderBoundsError):
        job_runtime._native_memory_claim_checkpoint_scratch_upper(object(), Poison())
