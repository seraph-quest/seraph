"""Real metadata transaction owners, pre-query charges and closed denials."""
from contextlib import asynccontextmanager
import sqlite3

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import create_async_engine

from src.memory import composition_headers as headers
from src.memory.header_bounds import (
    HeaderReadBudget, HeaderBoundsError, _trace_memory_numeric_charges,
)

pytestmark = pytest.mark.asyncio


async def _on_connection(kind, connection, operation):
    if kind == "aiosqlite":
        return await connection.run_sync(operation)
    return operation(connection)


@asynccontextmanager
async def _real_connection(kind, budget):
    queries = []
    if kind == "raw":
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.set_trace_callback(lambda statement: queries.append((statement, budget.remaining)))
        try:
            yield connection, queries
        finally:
            connection.close()
        return
    engine = (create_async_engine("sqlite+aiosqlite:///:memory:") if kind == "aiosqlite"
        else create_engine("sqlite:///:memory:"))
    observed_engine = engine.sync_engine if kind == "aiosqlite" else engine
    def before(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append((statement, budget.remaining))
    event.listen(observed_engine, "before_cursor_execute", before)
    connection = await engine.connect() if kind == "aiosqlite" else engine.connect()
    try:
        yield connection, queries
    finally:
        # A test may close the genuine driver beneath the live SA handle.
        # Invalidate that actual handle before ordinary release/engine cleanup.
        if not connection.closed:
            if kind == "aiosqlite":
                await connection.invalidate()
                await connection.close()
            else:
                connection.invalidate()
                connection.close()
        event.remove(observed_engine, "before_cursor_execute", before)
        if kind == "aiosqlite":
            await engine.dispose()
        else:
            engine.dispose()


def _check_metadata(connection, budget):
    headers._selected_census_owner(connection, budget)
    return headers._metadata_rows(connection, "SELECT 7", (),
        _header_budget=budget, upper=32, appearance=("real-transaction-owner",))


@pytest.mark.parametrize("kind", ["raw", "sqlalchemy", "aiosqlite"])
@pytest.mark.parametrize("state", ["active", "unbegun", "closed"])
async def test_actual_transaction_state_controls_delivery_and_all_three_fees(kind, state):
    budget = HeaderReadBudget()
    before = budget.remaining
    async with _real_connection(kind, budget) as (connection, queries):
        if state == "active":
            await _on_connection(kind, connection, lambda current:
                current.execute("BEGIN") if kind == "raw" else current.exec_driver_sql("BEGIN"))
        elif state == "closed":
            if kind == "aiosqlite":
                await connection.close()
            else:
                connection.close()
        queries.clear()
        with _trace_memory_numeric_charges(budget) as trace:
            if state == "active":
                rows = await _on_connection(kind, connection, lambda current: _check_metadata(current, budget))
                assert [tuple(row) for row in rows] == [(7,)]
            else:
                with pytest.raises(HeaderBoundsError, match="^header_metadata_transaction_required$"):
                    await _on_connection(kind, connection, lambda current:
                        headers._selected_census_owner(current, budget))
                with pytest.raises(HeaderBoundsError, match="^header_metadata_transaction_required$"):
                    await _on_connection(kind, connection, lambda current:
                        headers._metadata_rows(current, "SELECT 7", (), _header_budget=budget,
                            upper=32, appearance=("real-transaction-owner",)))
        if state == "active":
            assert queries == [("SELECT 7", before - 96)]
            assert [appearance[-1] for appearance, _ in trace] == ["delivery", "row-copy", "encoded-copy"]
            assert [amount for _, amount in trace] == [32, 32, 32]
            assert budget.remaining == before - 96
        else:
            assert queries == [] and trace == [] and budget.remaining == before


@pytest.mark.parametrize("kind", ["sqlalchemy", "aiosqlite"])
@pytest.mark.parametrize("damage", ["logical_only", "invalidated", "closed_driver"])
async def test_actual_sqlalchemy_handle_requires_its_live_physical_transaction(kind, damage):
    budget = HeaderReadBudget()
    before = budget.remaining
    async with _real_connection(kind, budget) as (connection, queries):
        if damage == "logical_only":
            # SQLAlchemy begins logically; genuine SQLite has received no BEGIN.
            if kind == "aiosqlite":
                await connection.begin()
            else:
                connection.begin()
            assert connection.in_transaction()
            assert not await _on_connection(kind, connection,
                lambda current: current.connection.driver_connection.in_transaction)
        else:
            await _on_connection(kind, connection, lambda current: current.exec_driver_sql("BEGIN"))
            if damage == "invalidated":
                if kind == "aiosqlite":
                    await connection.invalidate()
                else:
                    connection.invalidate()
                assert connection.invalidated
            else:
                driver = await _on_connection(kind, connection,
                    lambda current: current.connection.driver_connection)
                if kind == "aiosqlite":
                    await driver.close()
                else:
                    driver.close()
                assert not connection.closed and connection.in_transaction()
        queries.clear()
        with _trace_memory_numeric_charges(budget) as trace:
            for reader in (headers._selected_census_owner,
                    lambda current, frame: headers._metadata_rows(current, "SELECT 7", (),
                        _header_budget=frame, upper=32, appearance=("damaged-transaction",))):
                with pytest.raises(HeaderBoundsError, match="^header_metadata_transaction_required$"):
                    await _on_connection(kind, connection, lambda current: reader(current, budget))
        assert queries == [] and trace == [] and budget.remaining == before


@pytest.mark.parametrize("kind", ["poison_proxy", "sqlite_subclass"])
async def test_attribute_shaped_or_subclass_connection_denied_before_callbacks(kind):
    callbacks, queries = [], []
    class PoisonProxy:
        def __getattribute__(self, name):
            callbacks.append(name)
            raise AssertionError("unsupported connection callback reached")
    class PoisonSQLite(sqlite3.Connection):
        @property
        def in_transaction(self):
            callbacks.append("in_transaction")
            raise AssertionError("unsupported SQLite subclass callback reached")
    connection = (PoisonProxy() if kind == "poison_proxy" else
        sqlite3.connect(":memory:", isolation_level=None, factory=PoisonSQLite))
    if kind == "sqlite_subclass":
        # This is a genuinely active physical SQLite transaction. Its subtype
        # cannot replace the original concrete owner or override the predicate.
        sqlite3.Connection.execute(connection, "BEGIN")
        assert sqlite3.Connection.in_transaction.__get__(connection) is True
        sqlite3.Connection.set_trace_callback(connection, queries.append)
    budget = HeaderReadBudget()
    before = budget.remaining
    try:
        with _trace_memory_numeric_charges(budget) as trace:
            for reader in (headers._selected_census_owner,
                    lambda current, frame: headers._metadata_rows(current, "SELECT 7", (),
                        _header_budget=frame, upper=32, appearance=("unsupported-owner",))):
                with pytest.raises(HeaderBoundsError, match="^header_metadata_transaction_required$"):
                    reader(connection, budget)
        assert callbacks == [] and queries == [] and trace == []
        assert budget.remaining == before
    finally:
        if kind == "sqlite_subclass":
            sqlite3.Connection.close(connection)
