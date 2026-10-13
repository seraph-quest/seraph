"""Real initialized SQLite FTS metadata keeps its original exact validation."""
import pytest

from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, MAX_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_fixed_fts_types_preserve_full_original_metadata(async_db, monkeypatch):
    observed = []
    original = headers._metadata_rows
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["appearance"] == ("fts-columns",):
            observed.append((kwargs["_header_budget"] is not None, tuple(tuple(row) for row in result)))
        return result
    monkeypatch.setattr(headers, "_metadata_rows", observe)
    async with async_db() as db:
        def check(connection):
            connection.exec_driver_sql("BEGIN")
            present = set(headers._FTS_SQL)
            headers._validate_fts_metadata(connection, HeaderReadBudget(), None, _present_fts=present)
            budget = HeaderReadBudget()
            headers._validate_fts_metadata(connection, budget, None,
                _header_budget=budget, _present_fts=present)
            assert budget.remaining < MAX_BYTES
        await (await db.connection()).run_sync(check)
    legacy = [rows for metered, rows in observed if not metered]
    metered = [rows for metered, rows in observed if metered]
    assert len(legacy) == len(metered) == 6
    assert legacy == metered == [tuple(tuple(row) for row in meta["columns"])
        for meta in headers._FTS_META.values()]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_fixed_fts_changed_trigger_still_denies(async_db):
    async with async_db() as db:
        def check(connection):
            connection.exec_driver_sql("BEGIN")
            connection.exec_driver_sql("DROP TRIGGER session_recall_sessions_ai")
            connection.exec_driver_sql("CREATE TRIGGER session_recall_sessions_ai AFTER INSERT ON sessions BEGIN SELECT 1; END")
            budget = HeaderReadBudget()
            with pytest.raises(HeaderBoundsError, match="^header_fts_metadata_changed$"):
                headers._validate_fts_metadata(connection, budget, None,
                    _header_budget=budget, _present_fts=set(headers._FTS_SQL))
        await (await db.connection()).run_sync(check)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_fixed_fts_exhaustion_denies_before_metadata_delivery(async_db, monkeypatch):
    deliveries = []
    original = headers._sql
    def observe(*args, **kwargs):
        deliveries.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(headers, "_sql", observe)
    async with async_db() as db:
        def check(connection):
            connection.exec_driver_sql("BEGIN")
            budget = HeaderReadBudget()
            budget.debit(MAX_BYTES, appearance=("test-original-frame-exhaustion",))
            with pytest.raises(HeaderBoundsError, match="^canonical_bound_not_certified$"):
                headers._validate_fts_metadata(connection, budget, None,
                    _header_budget=budget, _present_fts=set(headers._FTS_SQL))
            assert budget.remaining == 0 and deliveries == []
        await (await db.connection()).run_sync(check)
