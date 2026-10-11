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
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, COMPOSITION_DESCRIPTORS
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
        if rows:
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
async def test_wrong_payer_or_insufficient_budget_denies_before_query(model_db, mode, monkeypatch):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    payer = HeaderReadBudget() if mode == "foreign" else None if mode == "none" else budget
    if mode == "exhausted":
        budget.debit(budget.remaining)
    before = budget.remaining
    reached = []
    if mode in ("foreign", "none"):
        def forbidden(*_args, **_kwargs):
            reached.append(True)
            raise AssertionError("Wrong payer reached sizing/allocation/construction")
        for name in ("_selected_schema_census_input_upper", "_census_source_leaves",
                "_selected_schema_census_parts", "_census_decimal_width",
                "_selected_schema_census_aggregate_sql", "_selected_census_rows", "_sql"):
            monkeypatch.setattr(headers, name, forbidden)
    queries = []
    def observe(*args):
        queries.append(args[2])
    event.listen(connection.sync_connection, "before_cursor_execute", observe)
    try:
        with pytest.raises(HeaderBoundsError):
            await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=payer))
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", observe)
    assert not reached and queries == [] and budget.remaining == before
    assert budget.physical_references == frozenset()


async def test_census_query_paid_before_delivery_and_repeats_without_refund(model_db, monkeypatch):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    before = budget.remaining
    assert before == 1048576
    observed = []
    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        observed.append((statement, budget.remaining))
    event.listen(connection.sync_connection, "before_cursor_execute", observe)
    # Independent compact grammar/allocation ledger; all three result fields
    # and original decoder appearances remain paid. No helper prices this oracle.
    input_upper, output_upper, decoder_upper = 183913, 48, 1488
    call_upper = 185449
    assert input_upper + output_upper + decoder_upper == call_upper
    constructors = []
    real_constructor = headers._selected_schema_census_aggregate_sql
    def construct():
        constructors.append(budget.remaining)
        return real_constructor()
    monkeypatch.setattr(headers, "_selected_schema_census_aggregate_sql", construct)
    reached = []
    try:
        for index in range(2):
            assert await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget)) == set()
            assert observed[-1][1] == before - index * call_upper - input_upper - output_upper - decoder_upper
            assert constructors[-1] == observed[-1][1]
            assert budget.remaining == before - (index + 1) * call_upper
        assert before - budget.remaining == 370898
        assert budget.remaining == 677678
        budget.debit(budget.remaining - (input_upper - 1))
        spent = budget.remaining
        def forbidden(*_args, **_kwargs):
            reached.append(True)
            raise AssertionError("Third census reached unpaid constructor/query")
        monkeypatch.setattr(headers, "_selected_schema_census_aggregate_sql", forbidden)
        monkeypatch.setattr(headers, "_sql", forbidden)
        with pytest.raises(HeaderBoundsError):
            await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", observe)
    assert not reached and len(observed) == len(constructors) == 2
    # The next complete input reservation fails before construction/query.
    # Existing spent capacity is retained; failure never refunds the caller.
    assert budget.remaining == spent
    assert budget.physical_references == frozenset()



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


@pytest.mark.parametrize("name,sql_type,notnull,pk,hidden,expected", [
    ("a"*24, "DATETIME", 1, 22, 0, ("a"*24, "DATETIME", 1, 22, 0)),
    ("a"*25, "DATETIME", 1, 1, 0, (None, "DATETIME", 1, 1, 0)),
    ("a", "DATETIMEZ", 1, 1, 0, ("a", None, 1, 1, 0)),
    ("a\x00z", "DATETIME", 1, 1, 0, (None, "DATETIME", 1, 1, 0)),
    ("é", "DATETIME", 1, 1, 0, (None, "DATETIME", 1, 1, 0)),
    ('a"', "DATETIME", 1, 1, 0, (None, "DATETIME", 1, 1, 0)),
    ("a\\", "DATETIME", 1, 1, 0, (None, "DATETIME", 1, 1, 0)),
    ("a", "DATE\x00", 1, 1, 0, ("a", None, 1, 1, 0)),
    ("a", "date", 1, 1, 0, ("a", None, 1, 1, 0)),
    (None, 7, 2, 23, 1, (None, None, -1, -1, 1)),
    ("a", "DATE", 1.0, 1.0, 0.0, ("a", "DATE", -1, -1, 1)),
])
async def test_descriptor_case_domains_on_isolated_sqlite_projection(
        model_db, name, sql_type, notnull, pk, hidden, expected):
    connection = await model_db.connection()
    # Synthetic projection, not a claim built-in PRAGMA emits malformed cells.
    query = headers._DESCRIPTOR_XINFO_SQL.replace("FROM pragma_table_xinfo(?)",
        'FROM (SELECT ? AS name,? AS type,? AS "notnull",? AS pk,? AS hidden)')
    rows = await connection.run_sync(lambda conn: list(conn.exec_driver_sql(
        query, (24, 8, 22, name, sql_type, notnull, pk, hidden, 23))))
    assert [tuple(row) for row in rows] == [expected]


@pytest.mark.parametrize("field,bad", [(1,"DATE"), (1,None), (3,-1), (3,23), (3,True), (3,1.0), (3,10**10),
    (2,-1), (2,True), (2,1.0), (2,None), (4,-1), (4,1), (4,False), (4,0.0), (4,None)])
async def test_descriptor_strict_delivered_scalar_domains(model_db, monkeypatch, field, bad):
    connection = await model_db.connection()
    original_sql = headers._sql
    rows = await connection.run_sync(lambda conn: [list(row) for row in original_sql(conn,
        'SELECT name,type,"notnull",pk,hidden FROM pragma_table_xinfo(?)', ("goals",))])
    target = next(row for row in rows if row[0] == "id")
    target[field] = bad
    def delivered(conn, query, params=()):
        return rows if "pragma_table_xinfo" in query else original_sql(conn, query, params)
    monkeypatch.setattr(headers, "_sql", delivered)
    if field == 3:
        def forbidden_sort(*args, **kwargs):
            raise AssertionError("invalid PK reached sorting")
        monkeypatch.setattr(headers, "sorted", forbidden_sort, raising=False)
    budget = HeaderReadBudget()
    with pytest.raises(HeaderBoundsError, match="^header_schema_changed$"):
        await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
            conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    assert budget.remaining == 1_048_576 - 404 - 1105


@pytest.mark.parametrize("damage", ["count", "name", "hidden", "reserved"])
async def test_descriptor_prior_schema_failures_precede_pk_access(model_db, monkeypatch, damage):
    connection = await model_db.connection()
    original_sql = headers._sql
    rows = await connection.run_sync(lambda conn: [list(row) for row in original_sql(conn,
        'SELECT name,type,"notnull",pk,hidden FROM pragma_table_xinfo(?)', ("goals",))])
    class NoPKAccess(list):
        def __getitem__(self, index):
            if index == 3:
                raise AssertionError("earlier schema fault reached PK validation")
            return super().__getitem__(index)
    rows[0] = NoPKAccess(rows[0])
    if damage == "count":
        rows.pop()
    elif damage == "name":
        rows[0][0] = "missing"
    elif damage == "hidden":
        rows[0][4] = 1
    else:
        rows[0][0] = "rowid"
    def delivered(conn, query, params=()):
        return rows if "pragma_table_xinfo" in query else original_sql(conn, query, params)
    monkeypatch.setattr(headers, "_sql", delivered)
    with pytest.raises(HeaderBoundsError, match="^header_schema_changed$"):
        await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
            conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=HeaderReadBudget()))


async def test_descriptor_notnull_domain_stays_after_current_type_compile(model_db, monkeypatch):
    connection = await model_db.connection()
    original_sql = headers._sql
    rows = await connection.run_sync(lambda conn: [list(row) for row in original_sql(conn,
        'SELECT name,type,"notnull",pk,hidden FROM pragma_table_xinfo(?)', ("goals",))])
    rows[0][2] = -1
    class CompileReached(RuntimeError):
        pass
    def live_dialect():
        raise CompileReached
    def delivered(conn, query, params=()):
        return rows if "pragma_table_xinfo" in query else original_sql(conn, query, params)
    monkeypatch.setattr(headers, "_sql", delivered)
    monkeypatch.setattr(headers, "dialect", live_dialect)
    with pytest.raises(CompileReached):
        await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
            conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=HeaderReadBudget()))


@pytest.mark.parametrize("group", ["index", "parts", "objects"])
async def test_identity_positional_predicates_preserve_literal_order_and_equality(group):
    # Original literal comparison expressions are the independent oracle.
    if group == "index":
        rows = [[0, "sqlite_autoindex_operator_identities_1", 1, "pk", 0]]
        predicate = headers._identity_index_matches
        def original(candidate):
            return len(candidate) == 1 and tuple(candidate[0]) == (0,
                "sqlite_autoindex_operator_identities_1", 1, "pk", 0)
    elif group == "parts":
        rows = [[0, 0, "id", 0, "BINARY", 1], [1, -1, None, 0, "BINARY", 0]]
        predicate = headers._identity_parts_match
        def original(candidate):
            return [tuple(row) for row in candidate] == [(0,0,"id",0,"BINARY",1),
                (1,-1,None,0,"BINARY",0)]
    else:
        rows = [["table", "operator_identities"], ["index", "sqlite_autoindex_operator_identities_1"]]
        def predicate(candidate):
            return headers._identity_objects_match(candidate, "operator_identities",
                "sqlite_autoindex_operator_identities_1")
        def original(candidate):
            return [tuple(row) for row in candidate] == [("table","operator_identities"),
                ("index","sqlite_autoindex_operator_identities_1")]
    assert predicate(rows) is original(rows) is True
    variants = [[], rows + [rows[0]], list(reversed(rows))] if len(rows) > 1 else [[], rows+rows]
    for i, row in enumerate(rows):
        shorter = [list(value) for value in rows]
        shorter[i].pop()
        variants.append(shorter)
        # Extra cells must reject even when every original position matches.
        longer = [list(value) for value in rows]
        longer[i].append(None)
        variants.append(longer)
        for j, value in enumerate(row):
            changed = [list(value) for value in rows]
            changed[i][j] = value+2 if type(value) is int else "changed"
            variants.append(changed)
    for candidate in variants:
        assert predicate(candidate) is original(candidate) is False


@pytest.mark.parametrize("group,position,bad", [
    ("index",0,False), ("index",2,True), ("index",4,0.0),
    ("index",0,None), ("parts",0,0.0), ("parts",1,False), ("parts",3,False), ("parts",5,True),
    ("parts",1,None)])
async def test_identity_numeric_booleans_and_floats_do_not_equal_integer_fields(group, position, bad):
    if group == "index":
        rows = [[0, "sqlite_autoindex_operator_identities_1", 1, "pk", 0]]
        rows[0][position] = bad
        assert not headers._identity_index_matches(rows)
    else:
        rows = [[0,0,"id",0,"BINARY",1], [1,-1,None,0,"BINARY",0]]
        rows[0][position] = bad
        assert not headers._identity_parts_match(rows)


@pytest.mark.parametrize("column,nullable", [("id", False), ("description", True)])
@pytest.mark.parametrize("bad", [-1, True, 1.0, None])
async def test_descriptor_notnull_domain_on_real_nullable_and_nonnullable_columns(
        model_db, monkeypatch, column, nullable, bad):
    from src.db.models import Goal
    connection = await model_db.connection()
    original_sql = headers._sql
    assert Goal.__table__.columns[column].nullable is nullable
    rows = await connection.run_sync(lambda conn: [list(row) for row in original_sql(conn,
        'SELECT name,type,"notnull",pk,hidden FROM pragma_table_xinfo(?)', ("goals",))])
    target = next(row for row in rows if row[0] == column)
    assert target[2] == int(not nullable)
    target[2] = bad
    # Only the delivered notnull cell is substituted. Source schema/model and
    # all genuine SQL comparisons remain unchanged; bool/float/NULL delivery
    # is not represented as a possible built-in PRAGMA result.
    def delivered(conn, query, params=()):
        return rows if "pragma_table_xinfo" in query else original_sql(conn, query, params)
    monkeypatch.setattr(headers, "_sql", delivered)
    budget = HeaderReadBudget()
    with pytest.raises(HeaderBoundsError, match="^header_schema_changed$"):
        await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
            conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    assert budget.remaining == 1_048_576 - 404 - 1105
