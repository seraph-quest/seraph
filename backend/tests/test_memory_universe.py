"""Actual inventory bounds, without representing seeded rows as native effects."""
import pytest
from sqlalchemy import event, text

from src.db.models import WorkflowRunState
from src.memory.header_bounds import HeaderBoundsError
from src.memory.universe import MEMORY_JOB_KIND, native_memory_universe


def inventory_row(index):
    return WorkflowRunState(run_identity=f"memory-{index}", root_run_identity=f"memory-{index}",
                            workflow_name="native-memory", job_kind=MEMORY_JOB_KIND,
                            status="succeeded" if index % 2 else "unknown_external_effect",
                            checkpoint_context_json="unsealed private context", owner_principal_id="foreign")


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_terminal_unknown_foreign_and_unsealed_all_remain_counted(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        db.add_all(inventory_row(index) for index in range(128))
        await db.flush()
        identities = await native_memory_universe(db)
        assert len(identities) == 128
        assert set(identities) == {f"memory-{index}" for index in range(128)}
        db.add(inventory_row(128))
        await db.flush()
        with pytest.raises(HeaderBoundsError, match="memory_universe_bound"):
            await native_memory_universe(db)


@pytest.mark.asyncio
async def test_overfull_inventory_denies_before_private_context_select(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        db.add_all(inventory_row(index) for index in range(129))
        await db.flush()
        statements = []
        connection = await db.connection()
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(HeaderBoundsError, match="memory_universe_bound"):
                await native_memory_universe(db)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert any("INDEXED BY ix_workflow_run_states_job_kind" in query and "LIMIT 129" in query
                   for query in statements)
        assert all("checkpoint_context_json" not in query for query in statements)


@pytest.mark.asyncio
async def test_missing_original_index_has_no_scan_fallback(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        await db.execute(text("DROP INDEX ix_workflow_run_states_job_kind"))
        with pytest.raises(HeaderBoundsError, match="memory_universe_index_unavailable"):
            await native_memory_universe(db)
