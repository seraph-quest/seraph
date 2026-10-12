"""Original INTEGER event keys remain typed through exact header evidence."""
import pytest
from sqlalchemy import event, text

from src.db.models import WorkBoardEvent, WorkBoardTask
from src.memory.header_bounds import (
    COMPOSITION_DESCRIPTORS, HeaderBoundsError, HeaderReadBudget,
    MAX_BYTES, MEMORY_DESCRIPTORS, preflight_exact_rows, validate_certificate,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [-(2**63), 2**63 - 1])
async def test_real_event_integer_identity_is_preserved_and_header_only(async_db, identity):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        task = WorkBoardTask(owner_principal_id="operator:test", owner_session_id="session:test",
            goal_id="goal:test")
        db.add(task)
        await db.flush()
        row = WorkBoardEvent(event_id=identity, task_id=task.task_id,
            owner_principal_id="operator:test", owner_session_id="session:test",
            actor_principal_id="operator:test", kind="task.updated", metadata_json="{}")
        db.add(row)
        await db.flush()
        statements = []
        connection = await db.connection()
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        budget = HeaderReadBudget()
        try:
            certificate = await budget.certify(db, COMPOSITION_DESCRIPTORS["work_board_events"],
                (identity,), _header_budget=budget)
            await validate_certificate(db, certificate, _header_budget=budget)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert certificate.row_ids == (identity,)
        assert type(certificate.row_ids[0]) is int
        assert certificate.rowids == (identity,)
        assert budget.remaining < MAX_BYTES
        assert any("octet_length" in statement and '"event_id"' in statement for statement in statements)
        assert all("SELECT work_board_events." not in statement and
            'SELECT "metadata_json"' not in statement for statement in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("table,identity", [
    ("work_board_events", True), ("work_board_events", False),
    ("work_board_events", -(2**63)-1), ("work_board_events", 2**63),
    ("work_board_events", "1"), ("memories", 1),
])
async def test_invalid_or_foreign_integer_identity_denies_before_sql_or_debit(async_db, table, identity):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        statements = []
        connection = await db.connection()
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        descriptor = (COMPOSITION_DESCRIPTORS[table] if table == "work_board_events"
            else MEMORY_DESCRIPTORS[table])
        budget = HeaderReadBudget()
        try:
            with pytest.raises(HeaderBoundsError, match="header_request_bound"):
                await preflight_exact_rows(db, descriptor, (identity,), MAX_BYTES, _header_budget=budget)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert statements == []
        assert budget.remaining == MAX_BYTES
