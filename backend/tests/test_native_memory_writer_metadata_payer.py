"""Unretained/copied Source and wrong frame cannot reach writer certification."""
from copy import copy

import pytest
from sqlalchemy import event

from tests.test_native_memory_producer import actual_memory


@pytest.mark.asyncio
async def test_native_writer_denies_source_or_frame_before_begin_and_metadata_delivery(
        actual_memory, monkeypatch):
    from src.runtime_plugins import memory_producer as producer, ownership
    from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original = ownership.validate_invocation
    observed = []

    async def validate(db, binding, **kwargs):
        owner = producer._MEMORY_AUTHORITY_CAPACITY.get()
        if owner is not None:
            connection = await db.connection()
            statements = []
            def sql(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)
            event.listen(connection.sync_connection, "before_cursor_execute", sql)
            remaining = owner.budget.remaining
            try:
                for source in (owner.source, copy(owner.source)):
                    with pytest.raises(producer.NativeServiceBlocked):
                        await ownership.begin_native_writer(db, owner="durable_jobs",
                            header_budget=owner.budget, _native_memory_source=source)
                with pytest.raises(ownership.CompositionBindingError,
                        match="composition_native_writer_budget_changed"):
                    await ownership.begin_native_writer(db, owner="durable_jobs",
                        header_budget=HeaderReadBudget(), _native_memory_source=owner.source)
                assert statements == [] and owner.budget.remaining == remaining
                assert producer._MEMORY_AUTHORITY_CAPACITY.get() is owner
                observed.append(True)
            finally:
                event.remove(connection.sync_connection, "before_cursor_execute", sql)
            raise HeaderBoundsError("native_writer_payer_test_stop_before_effect")
        return await original(db, binding, **kwargs)

    monkeypatch.setattr(ownership, "validate_invocation", validate)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "native-writer-payer-negative"})
    assert response.status_code != 200
    assert observed == [True]
    assert producer._MEMORY_AUTHORITY_CAPACITY.get() is None
