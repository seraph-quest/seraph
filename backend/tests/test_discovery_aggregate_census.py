"""Aggregate mechanism negatives; no certificate, body or admission authority."""
import pytest

from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from tests.test_memory_composition_headers import model_db
from tests.test_discovery_schema_projection import corrupt_catalog

pytestmark = pytest.mark.asyncio


MALFORMED_SUMMARIES = [
    [], [(1, 0, 0), (1, 0, 0)], [(1, 0)], [(True, 0, 0)],
    [(-1, 0, 0)], [(1361, 0, 0)], [(1, True, 0)], [(1, -1, 0)],
    [(1, 4, 0)], [(1, 0, True)], [(1, 0, -1)], [(1, 0, 32768)],
    [(1.0, 0, 0)], [(1, 0, "0")],
]


@pytest.mark.parametrize("rows", MALFORMED_SUMMARIES)
async def test_summary_delivery_denies_malformed_shapes_types_and_ranges(model_db, monkeypatch, rows):
    connection = await model_db.connection()
    monkeypatch.setattr(headers, "_sql", lambda *_args: rows)
    budget = HeaderReadBudget()
    with pytest.raises(HeaderBoundsError, match="^header_schema_object_unavailable$"):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert budget.remaining == 1048576 - 185449


@pytest.mark.parametrize("summary,error", [
    ((1360, 1, 0), "header_schema_object_unavailable"),
    ((1360, 2, 0), "header_schema_object_bound"),
    ((1359, 2, 0), "header_schema_object_unavailable"),
])
async def test_domain_overflow_and_policy_denial_precedence(model_db, monkeypatch, summary, error):
    connection = await model_db.connection()
    monkeypatch.setattr(headers, "_sql", lambda *_args: [summary])
    budget = HeaderReadBudget()
    with pytest.raises(HeaderBoundsError, match="^" + error + "$"):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))


@pytest.mark.parametrize("row", [
    (1.0, 1, 0), ("1", 1, 0), (1, 1.0, 0), (1, 2, 0),
    (1, 1, 1.0), (1, 1, -1), (1, 1, 16),
])
async def test_actual_sql_domain_flag_matches_original_integer_validator(model_db, row):
    connection = await model_db.connection()
    def check(conn):
        # Isolated negative projection fixture on the original SQLite connection.
        # This neither changes a catalog nor certifies a source or admission.
        query = headers._CENSUS_AGGREGATE_PREFIX + (
            "SELECT ? AS rowid, ? AS original_object_valid, ? AS original_fts_name_ordinal"
        ) + headers._CENSUS_AGGREGATE_SUFFIX
        actual = [tuple(value) for value in headers._sql(conn, query, row)]
        delivered = tuple(headers._sql(conn, "SELECT ?, ?, ?", row).fetchone())
        domain_invalid = (any(type(value) is not int for value in delivered)
            or not -(2**63) <= delivered[0] < 2**63 or delivered[1] not in (0, 1)
            or not 0 <= delivered[2] <= 15)
        assert domain_invalid and len(actual) == 1 and actual[0][0] == 1
        assert actual[0][1] & 1
    await connection.run_sync(check)


@pytest.mark.parametrize("remaining", [183913 + 48 + 1259, 185448])
async def test_decoder_reservations_deny_before_any_constructor_or_query(model_db, monkeypatch, remaining):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    budget.debit(budget.remaining - remaining)
    reached = []
    def forbidden(*_args, **_kwargs):
        reached.append(True)
        raise AssertionError("Unpaid summary constructor/query")
    monkeypatch.setattr(headers, "_selected_schema_census_aggregate_sql", forbidden)
    monkeypatch.setattr(headers, "_sql", forbidden)
    with pytest.raises(HeaderBoundsError):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert not reached and budget.remaining < remaining


@pytest.mark.parametrize("mode", ["empty", "all-fts-duplicated"])
async def test_empty_and_complete_fts_membership_matches_original_rows(model_db, mode):
    connection = await model_db.connection()
    catalog = [] if mode == "empty" else [
        (spec[0], name, spec[1]) for name, spec in headers._FTS_SQL.items()
    ] * 2
    def check(conn):
        with corrupt_catalog(conn, catalog):
            original = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_sql(), ())]
            expected_names = {name for ordinal, name in enumerate(headers._FTS_SQL, 1)
                if ordinal in {row[2] for row in original if row[2]}}
            assert len(original) == len(catalog) and all(row[1] == 1 for row in original)
            budget = HeaderReadBudget()
            actual = headers._projected_schema_objects(conn, budget, _header_budget=budget)
            assert actual == expected_names
            assert len(actual) == (0 if mode == "empty" else 15)
            assert budget.remaining == 1048576 - 185449
    await connection.run_sync(check)


@pytest.mark.parametrize("family", ["all-named", "auto-boundaries", "association-and-delimiters"])
async def test_values_projection_matches_original_fields_on_policy_boundaries(model_db, family):
    connection = await model_db.connection()
    if family == "all-named":
        # Every original source name; association remains independent of its prefix.
        catalog = [("index", name, "goals") for name in headers._NAMED_INDEXES]
    elif family == "auto-boundaries":
        bounds = dict(headers._AUTO_MAX)
        bounds.setdefault("alembic_version", 1)
        catalog = [("index", "sqlite_autoindex_" + table + "_" + suffix, table)
            for table, bound in bounds.items()
            for suffix in ("0", "01", str(bound), str(bound + 1))]
        catalog += [("index", "sqlite_autoindex_unknown_1", "unknown"),
            ("index", "sqlite_autoindex_goals_9223372036854775808", "goals"),
            ("index", "sqlite_autoindex_goals_1x", "goals"),
            ("index", "sqlite_autoindex_goals_1\x00", "goals")]
    else:
        name = next(value for value in headers._NAMED_INDEXES
            if value.startswith("ix_") and not value.startswith("ix_task_method_active_"))
        catalog = [("index", value, table) for value, table in (
            (name, "goals"), (name, "unknown"), (name.upper(), "goals"),
            (name + "|", "goals"), (name + "\x00", "goals"),
            ("ix_task_method_active_goal_id", "task_method_active"),
            ("ix_task_method_active_goal_id", "goals"),
            (name, "task_method_active"), ("sqlite_autoindex_task_method_active_1", "task_method_active"))]
    assert len(catalog) <= 1359
    def check(conn):
        with corrupt_catalog(conn, catalog):
            expected = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_sql(), ())]
            full = headers._selected_schema_census_aggregate_sql()
            assert full.endswith(headers._CENSUS_AGGREGATE_SUFFIX)
            # Observe each field before aggregation, preserving the exact capped input.
            projection = full[:-len(headers._CENSUS_AGGREGATE_SUFFIX)] + (
                ") SELECT rowid, original_object_valid, original_fts_name_ordinal FROM projected ORDER BY rowid)")
            actual = [tuple(row) for row in headers._sql(conn, projection, ())]
            assert actual == expected and len(actual) == len(catalog)
    await connection.run_sync(check)


async def test_values_numeric_widths_match_every_constructed_fragment(model_db):
    widths = list(headers._selected_schema_factored_auto_aggregate_parts(numeric=True))
    parts = list(headers._selected_schema_factored_auto_aggregate_parts(numeric=False))
    assert widths == [len(value) for value in parts]
    assert sum(widths) == 32986
    assert headers._selected_schema_census_aggregate_input_upper() == 183913
    # The old flat comparator keeps its byte stream and conservative price.
    assert headers._selected_schema_census_input_upper() == 211348


async def test_values_input_exhaustion_denies_before_text_roster_construction(model_db, monkeypatch):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    budget.debit(budget.remaining - 183912)
    original = headers._selected_schema_factored_auto_aggregate_parts
    reached = []
    def guarded(*, numeric):
        if not numeric:
            reached.append("text")
            raise AssertionError("Unpaid VALUES construction")
        return original(numeric=numeric)
    def forbidden(*_args, **_kwargs):
        reached.append("query")
        raise AssertionError("Unpaid VALUES query")
    monkeypatch.setattr(headers, "_selected_schema_factored_auto_aggregate_parts", guarded)
    monkeypatch.setattr(headers, "_sql", forbidden)
    with pytest.raises(HeaderBoundsError, match="^canonical_bound_not_certified$"):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert not reached and budget.remaining == 183912
