"""Selected census construction mechanisms; no admission or certificate grant."""
import pytest
from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from tests.test_memory_composition_headers import model_db
from tests.test_discovery_schema_projection import corrupt_catalog

pytestmark = pytest.mark.asyncio


async def test_paid_compact_constructor_matches_original_sqlite_rows(model_db, monkeypatch):
    connection = await model_db.connection()
    original = headers._selected_schema_census_sql()
    assert len(original) == 74731 and original.isascii()
    expected_rows = await connection.run_sync(lambda conn: [tuple(row) for row in headers._sql(conn, original, ())])
    observed_rows = []
    observed = []
    actual = headers._selected_schema_census_aggregate_sql
    budget = HeaderReadBudget()
    before = budget.remaining
    def build():
        observed.append(budget.remaining)
        return actual()
    monkeypatch.setattr(headers, "_selected_schema_census_aggregate_sql", build)
    queries = []
    real_sql = headers._sql
    def sql(conn, query, params=()):
        queries.append(query)
        rows = list(real_sql(conn, query, params))
        observed_rows.extend(tuple(row) for row in rows)
        return rows
    monkeypatch.setattr(headers, "_sql", sql)
    await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert len(queries) == 1 and queries[0] != original
    assert len(queries[0]) == 32986 and queries[0].isascii()
    expected_mask = 0
    for ordinal in {row[2] for row in expected_rows if row[2]}:
        expected_mask |= 1 << (ordinal - 1)
    assert observed_rows == [(len(expected_rows), 0, expected_mask)]
    assert len(observed) == 1
    # Independent source ledger, not the candidate's own price as an oracle.
    assert observed[0] == before - 183913 - 48 - 1488


@pytest.mark.parametrize("reserve", ["input", "delivery"])
async def test_insufficient_budget_never_constructs_or_queries(model_db, monkeypatch, reserve):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    # Input and one result appearance are independently source-derived literals.
    upper = 183913
    remaining = upper - 1 if reserve == "input" else upper + 16 - 1
    budget.debit(budget.remaining - remaining)
    reached = []
    def forbidden(*_args, **_kwargs):
        reached.append(True)
        raise AssertionError("Unpaid construction/query")
    monkeypatch.setattr(headers, "_selected_schema_census_aggregate_sql", forbidden)
    monkeypatch.setattr(headers, "_sql", forbidden)
    with pytest.raises(HeaderBoundsError):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert not reached
    assert budget.remaining <= remaining


@pytest.mark.parametrize("leaf", ["apostrophe'", 'quote"', "slash\\", "nul\x00", "非ascii"])
async def test_dynamic_fts_table_leaf_denies_before_debit_or_build(model_db, monkeypatch, leaf):
    connection = await model_db.connection()
    values = dict(headers._FTS_SQL)
    name = next(name for name, spec in values.items() if spec[0] == "trigger")
    old = values[name]
    values[name] = (old[0], leaf, old[2])
    monkeypatch.setattr(headers, "_FTS_SQL", values)
    budget = HeaderReadBudget()
    before = budget.remaining
    def forbidden():
        raise AssertionError("Unpaid invalid-source construction")
    monkeypatch.setattr(headers, "_selected_schema_census_aggregate_sql", forbidden)
    with pytest.raises(HeaderBoundsError, match="^header_schema_source_unavailable$"):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert budget.remaining == before


@pytest.mark.parametrize("row", [(True, 1, 0), (2**63, 1, 0), (-(2**63)-1, 1, 0),
    (1, True, 0), (1, 2, 0), (1, 1, True), (1, 1, -1), (1, 1, 16), (1, 1)])
async def test_closed_integer_delivery_rejects_wrong_types_and_domains(model_db, monkeypatch, row):
    connection = await model_db.connection()
    # Mechanism negative only: deliberately corrupt driver delivery, not positive authority.
    monkeypatch.setattr(headers, "_sql", lambda *_args: [row])
    with pytest.raises(HeaderBoundsError, match="^header_schema_object_unavailable$"):
        await _negative_delivery(connection, row)


async def _negative_delivery(connection, row):
    budget = HeaderReadBudget()
    return await connection.run_sync(lambda conn: headers._projected_schema_row_objects(conn, budget, _header_budget=budget))


@pytest.mark.parametrize("count,expected", [(1359, None), (1360, "header_schema_object_bound"), (1361, "header_schema_object_bound")])
async def test_complete_count_and_sentinel_not_filtered(model_db, monkeypatch, count, expected):
    connection = await model_db.connection()
    monkeypatch.setattr(headers, "_sql", lambda *_args: [(index, 1, 0) for index in range(count)])
    budget = HeaderReadBudget()
    operation = lambda conn: headers._projected_schema_row_objects(conn, budget, _header_budget=budget)
    if expected:
        with pytest.raises(HeaderBoundsError, match="^" + expected + "$"):
            await connection.run_sync(operation)
    else:
        assert await connection.run_sync(operation) == set()


async def test_foreign_active_original_frame_denies_before_build(model_db, monkeypatch):
    connection = await model_db.connection()
    def check(conn):
        original_budget = HeaderReadBudget()
        certificate = headers.preflight_composition_superset(conn, original_budget)
        selected_budget = HeaderReadBudget()
        before = selected_budget.remaining
        def forbidden(*_args, **_kwargs):
            raise AssertionError("Foreign frame constructed SQL")
        with headers.snapshot_reads(certificate):
            with monkeypatch.context() as patch:
                for name in ("_selected_schema_census_input_upper", "_census_source_leaves",
                        "_selected_schema_census_parts", "_census_decimal_width",
                        "_selected_schema_census_aggregate_sql", "_sql"):
                    patch.setattr(headers, name, forbidden)
                with pytest.raises(HeaderBoundsError, match="^header_metadata_owner_unavailable$"):
                    headers._projected_schema_objects(conn, selected_budget, _header_budget=selected_budget)
        assert selected_budget.remaining == before
    await connection.run_sync(check)


async def test_missing_transaction_denies_before_any_source_walk(model_db, monkeypatch):
    connection = await model_db.connection()
    await connection.rollback()
    budget = HeaderReadBudget()
    before = budget.remaining
    reached = []
    def forbidden(*_args, **_kwargs):
        reached.append(True)
        raise AssertionError("Invalid transaction reached sizing/construction/query")
    for name in ("_selected_schema_census_input_upper", "_census_source_leaves",
            "_selected_schema_census_parts", "_census_decimal_width",
            "_selected_schema_census_aggregate_sql", "_sql"):
        monkeypatch.setattr(headers, name, forbidden)
    with pytest.raises(HeaderBoundsError, match="^header_metadata_transaction_required$"):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert not reached and budget.remaining == before


async def test_independent_source_occurrence_ledger_pins_numeric_upper():
    # Independent original row-collector ledger: Q=41589, F=5090.
    # No production generator or price helper computes this expected charge.
    expected = (56860 + 83182 + 6 + 8920 + 17441 + 911 + 13380
        + 960 + 16755 + 12150 + 276 + 51 + 228 + 228)
    assert expected == 211348
    assert headers._selected_schema_census_input_upper() == expected


async def test_streamed_membership_matches_original_complete_source_catalog(model_db):
    connection = await model_db.connection()
    # The old full-name predicate is the independent SQLite oracle; this tests
    # every original named-index leaf, both table branches and the FTS ordinals.
    catalog = [("index", name, "memories") for name in headers._NAMED_INDEXES]
    catalog += [("table", name, name) for name in headers._SCHEMA_TABLES]
    catalog += [("table", "alembic_version", "alembic_version"),
        ("table", "sqlite_sequence", "sqlite_sequence")]
    catalog += [(spec[0], name, spec[1]) for name, spec in headers._FTS_SQL.items()]
    def compare(conn):
        with corrupt_catalog(conn, catalog):
            old = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_sql())]
            new = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_flat_sql())]
            assert old == new and len(old) == len(catalog) < 1360
            assert {row[2] for row in new if row[2]} == set(range(1, 16))
    await connection.run_sync(compare)


@pytest.mark.parametrize("name,table,valid", [
    ("ix_memories_kind", "memories", True),
    ("ix_memories_kind", "goals", True),  # lexical prefix does not bind tbl_name
    ("ix_memories_kind", "foreign", False),
    ("ix_memories_kind", "task_method_active", False),
    ("ix_memories_kind", "session_recall_fts", False),
    ("ix_memories_kin", "memories", False),
    ("ix_memories_kind_extra", "memories", False),
    ("ix_memories_kind|created_at", "memories", False),
    ("ix_memories_|kind", "memories", False),
    ("ix_memories_kind|", "memories", False),
    ("ix_memories_kind\x00", "memories", False),
    ("ix_memories_kind\x00|created_at", "memories", False),
    ("IX_memories_kind", "memories", False),
    ("ix_memory_kind", "memories", False),
    ("ix_task_method_active_goal_id", "task_method_active", True),
    ("ix_task_method_active_goal_id", "goals", False),
    ("sqlite_autoindex_goals_0001", "goals", True),
    ("sqlite_autoindex_goals_0", "goals", False),
    ("sqlite_autoindex_goals_1\x00", "goals", False),
])
async def test_streamed_name_membership_real_sqlite_adversaries(model_db, name, table, valid):
    connection = await model_db.connection()
    def compare(conn):
        with corrupt_catalog(conn, [("index", name, table)]):
            old = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_sql())]
            new = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_flat_sql())]
            assert old == new == [(1, int(valid), 0)]
            budget = HeaderReadBudget()
            if valid:
                assert headers._projected_schema_objects(conn, budget, _header_budget=budget) == set()
            else:
                with pytest.raises(HeaderBoundsError, match="^header_schema_object_unavailable$"):
                    headers._projected_schema_objects(conn, budget, _header_budget=budget)
            assert budget.remaining == 1048576 - 185449
    await connection.run_sync(compare)


async def test_streamed_delimiter_cross_token_and_substring_denial(model_db):
    connection = await model_db.connection()
    # Adjacent tokens in the actual unsorted source traversal are the difficult
    # cross-boundary needle; the old full-name IN expression rejects that name.
    names = [name for name in headers._NAMED_INDEXES if name.startswith("ix_memories_")]
    assert len(names) >= 2
    prefix = "ix_memories_"
    joined = names[0] + "|" + names[1][len(prefix):]
    assert joined not in headers._NAMED_INDEXES
    short = names[0][:-1]
    catalog = [("index", joined, "memories"), ("index", short, "memories")]
    def compare(conn):
        with corrupt_catalog(conn, catalog):
            old = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_sql())]
            new = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_flat_sql())]
            assert old == new and old[0][1] == 0
            # If a shorter name happens to be another allowed member, it must
            # retain precisely that original policy instead of assuming denial.
            assert old[1][1] == int(short in headers._NAMED_INDEXES)
    await connection.run_sync(compare)


async def test_source_pipe_invariant_denies_before_debit_or_paid_build(model_db, monkeypatch):
    connection = await model_db.connection()
    monkeypatch.setattr(headers, "_NAMED_INDEXES", headers._NAMED_INDEXES | {"ix_memories_alpha|beta"})
    budget = HeaderReadBudget()
    before = budget.remaining
    reached = []
    def forbidden(*args, **kwargs):
        reached.append(True)
        raise AssertionError("invalid delimiter source reached allocation/query")
    monkeypatch.setattr(headers, "_selected_schema_census_aggregate_sql", forbidden)
    monkeypatch.setattr(headers, "_sql", forbidden)
    with pytest.raises(HeaderBoundsError, match="^header_schema_source_unavailable$"):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget,
            _header_budget=budget))
    assert reached == [] and budget.remaining == before


@pytest.mark.parametrize("count", [1359, 1360, 1361])
async def test_streamed_census_keeps_unfiltered_ceiling_and_original_row_order(model_db, count):
    connection = await model_db.connection()
    catalog = [("index", "ix_memories_kind", "memories")] * count
    def compare(conn):
        with corrupt_catalog(conn, catalog):
            old = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_sql())]
            new = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_flat_sql())]
            assert old == new and len(new) == min(count, 1360)
            assert [row[0] for row in new] == list(range(1, min(count, 1360)+1))
            budget = HeaderReadBudget()
            if count == 1359:
                assert headers._projected_schema_objects(conn, budget, _header_budget=budget) == set()
            else:
                with pytest.raises(HeaderBoundsError, match="^header_schema_object_bound$"):
                    headers._projected_schema_objects(conn, budget, _header_budget=budget)
    await connection.run_sync(compare)
