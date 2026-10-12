"""No extra selector queries while forecasting original reference lookup."""
import pytest
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from src.memory import header_bounds
from tests.test_native_memory_producer import actual_memory


@pytest.mark.asyncio
async def test_unissued_reference_forecast_denies_without_locator(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("numeric forecast replayed selector")
    monkeypatch.setattr(memory_producer, "_memory_locator_rows", forbidden)
    with pytest.raises(HeaderBoundsError):
        await memory_producer.original_native_memory_reference_lookup_numeric_schedule(
            object(), object(), certificate=object(), facts=object())


@pytest.mark.asyncio
async def test_genuine_reference_forecast_never_replays_original_queries(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original_locator = memory_producer._memory_locator_rows
    original_calls = []
    async def counted(*args, **kwargs):
        original_calls.append(args[1].__tablename__)
        return await original_locator(*args, **kwargs)
    monkeypatch.setattr(memory_producer, "_memory_locator_rows", counted)
    observed = []
    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        assert guard._native_memory_reference_lookup_original[0] is operation
        before = tuple(original_calls)
        async def forbidden(*args, **kwargs):
            raise AssertionError("forecast replayed original selector")
        monkeypatch.setattr(memory_producer, "_memory_locator_rows", forbidden)
        # A missing genuinely selected/projected board is a closed graph STOP;
        # it must never be filled through an additional body query.
        try:
            await memory_producer.original_native_memory_reference_lookup_numeric_schedule(
                operation, prepared, certificate=certificate,
                facts=guard._native_memory_source_capacity_facts[3])
        except HeaderBoundsError as error:
            assert str(error) in {"memory_lookup_numeric_graph_unavailable", "memory_lookup_numeric_source_changed"}
        assert tuple(original_calls) == before
        observed.append(before)
        raise HeaderBoundsError("test_no_replayed_reference_query")
    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "reference-query-reuse"})
    assert response.status_code == 409 and len(observed) == 1
