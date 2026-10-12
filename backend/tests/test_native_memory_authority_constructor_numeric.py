"""Original authority vector constructor payments; prepared component Source."""
import asyncio
import pytest
from src.memory import composition_headers, header_bounds
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from src.workflows import job_runtime
from tests.test_native_memory_producer import actual_memory


def test_unissued_authority_constructor_denies_before_db_or_scope():
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued authority reached DB")
    with pytest.raises(HeaderBoundsError):
        memory_producer.original_native_memory_authority_construction_numeric_upper(
            Poison(), object(), certificate=object(), facts=object(), retention_widths=object())
    with pytest.raises(HeaderBoundsError):
        job_runtime.original_native_memory_completion_certificate_construction_numeric_upper(
            Poison(), object(), certificate=object(), facts=object(), retention_widths=object())


@pytest.mark.asyncio
async def test_genuine_authority_constructor_scalar_and_original_stage_payments(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    helper = memory_producer.original_native_memory_authority_construction_numeric_upper
    original_entries = memory_producer._native_memory_authority_entries
    debit = header_bounds.HeaderReadBudget.debit
    paid = []
    built = []
    observed = []
    def observe_debit(frame, amount, *, appearance=None):
        result = debit(frame, amount, appearance=appearance)
        if type(appearance) is tuple and len(appearance) == 3 and appearance[:2] == ("native-memory-authority", "child-construction"):
            paid.append((frame, appearance[2], amount))
        return result
    def observe_entries(stage, upper, **kwargs):
        owner = memory_producer._native_memory_authority_capacity_checked()
        if owner is not None:
            assert paid and paid[-1][0] is owner.budget and paid[-1][1] == stage
            assert paid[-1][2] == memory_producer._native_memory_authority_entry_construction_upper(
                stage, dependencies=kwargs.get("dependencies", 0))
            built.append(stage)
        return original_entries(stage, upper, **kwargs)
    monkeypatch.setattr(header_bounds.HeaderReadBudget, "debit", observe_debit)
    monkeypatch.setattr(memory_producer, "_native_memory_authority_entries", observe_entries)
    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        kwargs = dict(certificate=certificate, facts=guard._native_memory_source_capacity_facts[3],
            retention_widths=guard._native_memory_retention_numeric_facts[3])
        before = certificate.budget.remaining
        def forbidden(*args, **kw):
            raise AssertionError("scalar called authority vector constructor")
        monkeypatch.setattr(memory_producer, "_native_memory_authority_entries", forbidden)
        for kind in ("dispatch", "completion-child"):
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **{**kwargs, "facts": dict(kwargs["facts"])}, kind=kind)
            token = composition_headers._CURRENT.set(None)
            try:
                with pytest.raises(HeaderBoundsError):
                    helper(operation, prepared, **kwargs, kind=kind)
            finally:
                composition_headers._CURRENT.reset(token)
            async def child():
                with pytest.raises(HeaderBoundsError):
                    helper(operation, prepared, **kwargs, kind=kind)
            await asyncio.create_task(child())
            capacity = helper(operation, prepared, **kwargs, kind=kind)
            assert type(capacity) is int and capacity > 0
        assert certificate.budget.remaining == before
        assert built and paid
        wrapper = job_runtime.original_native_memory_completion_certificate_construction_numeric_upper(
            operation, prepared, **kwargs)
        assert wrapper > 0 and certificate.budget.remaining == before
        observed.append(wrapper)
        raise HeaderBoundsError("test_authority_constructor_observed")
    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "authority-construction-observer",
    })
    assert response.status_code == 409 and len(observed) == 1


@pytest.mark.asyncio
async def test_genuine_authority_constructor_payment_denies_before_vector(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original = header_bounds.HeaderReadBudget.debit
    entries = memory_producer._native_memory_authority_entries
    blocked = []
    def deny(frame, amount, *, appearance=None):
        if type(appearance) is tuple and len(appearance) == 3 and appearance[:2] == ("native-memory-authority", "child-construction"):
            assert memory_producer._native_memory_authority_capacity_checked().budget is frame
            blocked.append(appearance[2])
            raise HeaderBoundsError("test_original_constructor_payment_denied")
        return original(frame, amount, appearance=appearance)
    def guard(stage, upper, **kwargs):
        assert not blocked, "vector constructed after constructor payer denied"
        return entries(stage, upper, **kwargs)
    monkeypatch.setattr(header_bounds.HeaderReadBudget, "debit", deny)
    monkeypatch.setattr(memory_producer, "_native_memory_authority_entries", guard)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "authority-construction-denial",
    })
    assert response.status_code == 409 and len(blocked) == 1


@pytest.mark.parametrize("trace,allocations", [
    ((("read", 1), ("paid", 11)), (("paid", 10), ("paid", 30))),
    ((("read", 1), ("paid", 9), ("paid", 9)), (("paid", 10),)),
    ((("read", 1), ("paid", 9), ("paid", 21)), (("paid", 10), ("paid", 20))),
])
def test_authority_trace_fifo_denies_larger_or_exhausted_occurrences(trace, allocations):
    # Pure numerical filter regression only: no Source/ledger/cert is fabricated.
    with pytest.raises(HeaderBoundsError, match="memory_authority_numeric_paid_amount_exceeded"):
        memory_producer._native_memory_authority_filter_prepaid_trace(trace, allocations)


def test_authority_trace_fifo_preserves_unpaid_reads_and_repeated_amount_order():
    assert memory_producer._native_memory_authority_filter_prepaid_trace(
        (("read", 7), ("paid", 9), ("paid", 20)), (("paid", 10), ("paid", 20))) == (("read", 7),)


def test_operator_constructor_includes_all_twenty_five_source_generator_frames():
    # Original: four grant string walkers+sum, two map sums+17 key walkers,
    # and the distinct five-entry return tuple generator:25 frames at41.
    # Exact fixed roster/container arithmetic is independent of the payer spy.
    assert memory_producer._native_memory_authority_entry_construction_upper(
        "operator-constructor") == 7254
