"""Census-only differential SQLite proof; no certificate/admission authority.

Corrupt sqlite_schema catalogs below are rollback-only negative fixtures. Even
when the original census policy accepts a row, no certificate is ever issued.
"""
from contextlib import contextmanager
import hashlib
import re

import pytest
from sqlalchemy import event, text

from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from tests.test_memory_composition_headers import model_db

pytestmark = pytest.mark.asyncio


class CensusReached(Exception):
    pass


def original_census_verdict(connection, monkeypatch):
    def reached(*_args, **_kwargs):
        raise CensusReached
    with monkeypatch.context() as patch:
        patch.setattr(headers, "_validate_fts_metadata", reached)
        try:
            headers.preflight_composition_superset(connection, HeaderReadBudget())
        except CensusReached:
            return "accepted"
        except HeaderBoundsError as error:
            return str(error)
    raise AssertionError("Original preflight escaped the census-only sentinel")


def selected_census_verdict(connection):
    budget = HeaderReadBudget()
    try:
        headers._projected_schema_objects(connection, budget, _header_budget=budget)
    except HeaderBoundsError as error:
        return str(error)
    return "accepted"


@contextmanager
def corrupt_catalog(connection, rows):
    connection.exec_driver_sql("SAVEPOINT negative_census")
    connection.exec_driver_sql("PRAGMA writable_schema=ON")
    try:
        connection.exec_driver_sql("DELETE FROM sqlite_schema")
        connection.exec_driver_sql(
            "INSERT INTO sqlite_schema(rowid,type,name,tbl_name,rootpage,sql) VALUES(?,?,?,?,0,NULL)",
            [(index, *row) for index, row in enumerate(rows, 1)])
        yield
    finally:
        connection.exec_driver_sql("ROLLBACK TO negative_census")
        connection.exec_driver_sql("RELEASE negative_census")
        connection.exec_driver_sql("PRAGMA writable_schema=OFF")


POLICY_CASES = [
    ("table", "memories", "memories", True),
    ("table", "alembic_version", "alembic_version", True),
    ("table", "memories", "goals", False),
    ("table", "foreign", "foreign", False),
    ("view", "memories", "memories", False),
    ("index", "ix_memories_kind", "memories", True),
    ("index", "ix_memories_kind", "goals", True),
    ("index", "ix_memories_kind", "foreign", False),
    ("index", "ix_memories_kind", "task_method_active", False),
    ("index", "ix_task_method_active_goal_id", "goals", False),
    ("index", "ix_task_method_active_goal_id", "task_method_active", True),
    ("index", "sqlite_autoindex_alembic_version_1", "alembic_version", True),
    ("index", "sqlite_autoindex_alembic_version_0001", "alembic_version", True),
    ("index", "sqlite_autoindex_alembic_version_0", "alembic_version", False),
    ("index", "sqlite_autoindex_alembic_version_2", "alembic_version", False),
    ("index", "sqlite_autoindex_foreign_1", "foreign", False),
    ("index", "sqlite_autoindex_task_method_active_2", "task_method_active", True),
    ("index", "sqlite_autoindex_task_method_active_3", "task_method_active", False),
    ("index", "sqlite_autoindex_goals_", "goals", False),
    ("index", "sqlite_autoindex_goals_" + "0" * 90 + "1", "goals", True),
    ("index", "sqlite_autoindex_goals_" + "9" * 90, "goals", False),
    ("index", "sqlite_autoindex_goals_١", "goals", False),
    ("index", "sqlite_autoindex_goals_1\x00", "goals", False),
    ("index", "sqlite_autoindex_goals_\x001", "goals", False),
    ("index", "sqlite_autoindex_goals_0\x001", "goals", False),
    ("index", "sqlite_autoindex_goals_1\x00junk", "goals", False),
    ("index", "sqlite_autoindex_goals_1\x00١", "goals", False),
    ("index", "sqlite_autoindex_goals_" + "0" * 90 + "1\x00", "goals", False),
    ("index", "sqlite_autoindex_\x00goals_1", "goals", False),
    ("index", "sqlite_autoindex_alembic_version_0001\x00", "alembic_version", False),
    ("index", "sqlite_autoindex_task_method_active_2\x00", "task_method_active", False),
    ("index", "sqlite_autoindex_goals_+1", "goals", False),
    ("trigger", "operator_principal_required_insert", "operator_sessions", True),
    ("trigger", "operator_principal_required_insert", "goals", False),
    ("trigger", "session_recall_sessions_ad", "sessions", True),
    ("trigger", "session_recall_sessions_ad", "goals", False),
    (None, "memories", "memories", False),
    (7, "memories", "memories", False),
    ("tablexxx", "memories", "memories", False),
    ("table", 7, "memories", False),
    ("table", None, "memories", False),
    ("table", "memories", None, False),
    ("table", "memories\x00", "memories", False),
    ("table", "MEMORIES", "MEMORIES", False),
    ("table", "x" * 129, "x" * 129, False),
]


@pytest.mark.parametrize("kind,name,table,accepted", POLICY_CASES)
async def test_negative_catalog_policy_matches_original(model_db, monkeypatch, kind, name, table, accepted):
    connection = await model_db.connection()
    def check(conn):
        with corrupt_catalog(conn, [(kind, name, table)]):
            expected = "accepted" if accepted else "header_schema_object_unavailable"
            assert original_census_verdict(conn, monkeypatch) == expected
            assert selected_census_verdict(conn) == expected
    await connection.run_sync(check)


@pytest.mark.parametrize("count,invalid_at", [(1359, None), (1359, 0), (1359, 1358),
    (1359, 679), (1360, None), (1360, 0), (1360, 679), (1360, 1359)])
async def test_count_precedence_and_sentinel_match_original(model_db, monkeypatch, count, invalid_at):
    rows = [("table", "memories", "memories")] * count
    if invalid_at is not None:
        rows[invalid_at] = ("view", "foreign", "foreign")
    expected = ("header_schema_object_bound" if count == 1360 else
        "header_schema_object_unavailable" if invalid_at is not None else "accepted")
    connection = await model_db.connection()
    def check(conn):
        with corrupt_catalog(conn, rows):
            assert original_census_verdict(conn, monkeypatch) == expected
            assert selected_census_verdict(conn) == expected
    await connection.run_sync(check)


async def test_original_model_schema_matches_without_issuing_certificate(model_db, monkeypatch):
    connection = await model_db.connection()
    assert await connection.run_sync(lambda conn: original_census_verdict(conn, monkeypatch)) == "accepted"
    assert await connection.run_sync(selected_census_verdict) == "accepted"


@pytest.mark.parametrize("mode", ["foreign", "none", "exhausted"])
async def test_wrong_payer_or_insufficient_budget_denies_before_query(model_db, mode):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    payer = HeaderReadBudget() if mode == "foreign" else None if mode == "none" else budget
    if mode == "exhausted":
        budget.debit(budget.remaining)
    before = budget.remaining
    queries = []
    def observe(*args):
        queries.append(args[2])
    event.listen(connection.sync_connection, "before_cursor_execute", observe)
    try:
        with pytest.raises(HeaderBoundsError):
            await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=payer))
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", observe)
    assert queries == [] and budget.remaining == before
    assert budget.physical_references == frozenset()


async def test_census_query_paid_before_delivery_and_repeats_without_refund(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    before = budget.remaining
    observed = []
    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        observed.append((statement, budget.remaining))
    event.listen(connection.sync_connection, "before_cursor_execute", observe)
    try:
        for _ in range(2):
            assert await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget)) == set()
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", observe)
    assert len(observed) == 2
    assert [value for _, value in observed] == [before - 265203, before - 393834 - 265203]
    assert budget.remaining == before - 2 * 393834
    assert budget.physical_references == frozenset()
    with pytest.raises(HeaderBoundsError):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert budget.remaining < before - 2 * 393834


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("change", ["unchanged", "partial", "changed-sql"])
async def test_original_full_fts_checks_remain_mandatory(async_db, change, monkeypatch):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        if change != "unchanged":
            await db.execute(text("DROP TRIGGER session_recall_sessions_ad"))
            if change == "changed-sql":
                await db.execute(text("CREATE TRIGGER session_recall_sessions_ad AFTER DELETE ON sessions BEGIN SELECT 1; END"))
        connection = await db.connection()
        budget = HeaderReadBudget()
        present = await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
        original_rows = headers._metadata_rows
        seen = []
        def observe(*args, **kwargs):
            seen.append((kwargs.get("appearance"), args[2] if len(args) > 2 else ()))
            return original_rows(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(headers, "_metadata_rows", observe)
            if change == "unchanged":
                await connection.run_sync(lambda conn: headers._validate_fts_metadata(conn, budget, None,
                    _header_budget=budget, _present_fts=present))
                assert present == set(headers._FTS_SQL)
                assert {params[0] for appearance, params in seen if appearance == ("fts-sql-header",)} == set(headers._FTS_SQL)
                assert {params[0] for appearance, params in seen if appearance[0] == "fts-sql-body"} == set(headers._FTS_SQL)
                for phase in ("fts-columns", "fts-layout", "fts-indexes"):
                    assert {params[0] for appearance, params in seen if appearance == (phase,)} == set(headers._FTS_META)
                assert any(appearance == ("fts-index-parts",) for appearance, _ in seen)
            else:
                with pytest.raises(HeaderBoundsError, match="^header_fts_metadata_changed$"):
                    await connection.run_sync(lambda conn: headers._validate_fts_metadata(conn, budget, None,
                        _header_budget=budget, _present_fts=present))
        await db.rollback()


async def test_generated_sql_matches_corrected_closed_query_tokens():
    # Token digest of R115 executable canonical SQL, excluding its external
    # documentation comment and terminator, not filtering generated SQL.
    # Pinned literal rosters prove full ordered equality of ALL7997 tokens.
    tokens = re.findall(r"'(?:''|[^'])*'|[A-Za-z_0-9]+|[^\s]", headers._selected_schema_census_sql())
    assert tokens[0] == "WITH" and len(tokens) == 7997
    assert hashlib.sha256("\n".join(tokens).encode()).hexdigest() == "d40ebfbb9e882c510c64a29eccaae7957b9432abe984ab7d37dbe9c2a8d17566"
