"""Factored census boundaries on original SQLite; no snapshot authority."""
import pytest

from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from tests.test_memory_composition_headers import model_db
from tests.test_discovery_schema_projection import corrupt_catalog

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("bound", [False, True, -1, 2**63, 1.0, None, "1"])
async def test_auto_source_domain_denies_before_values_text_or_sql(model_db, monkeypatch, bound):
    connection = await model_db.connection()
    policy = dict(headers._AUTO_MAX)
    policy["goals"] = bound
    monkeypatch.setattr(headers, "_AUTO_MAX", policy)
    budget = HeaderReadBudget()
    before = budget.remaining
    reached = []
    original = headers._selected_schema_factored_auto_aggregate_parts
    def observed(*, numeric):
        if not numeric:
            reached.append("text")
            raise AssertionError("Invalid AUTO source constructed SQL")
        return original(numeric=numeric)
    def forbidden(*_args, **_kwargs):
        reached.append("query")
        raise AssertionError("Invalid AUTO source queried SQLite")
    monkeypatch.setattr(headers, "_selected_schema_factored_auto_aggregate_parts", observed)
    monkeypatch.setattr(headers, "_sql", forbidden)
    with pytest.raises(HeaderBoundsError, match="^header_schema_object_unavailable$"):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(
            conn, budget, _header_budget=budget))
    assert not reached and budget.remaining == before
    assert budget.physical_references == frozenset()


@pytest.mark.parametrize("mode,count,error", [
    ("below-cap-policy", 1359, "header_schema_object_unavailable"),
    ("sentinel-policy", 1360, "header_schema_object_bound"),
    ("beyond-cap-policy", 1361, "header_schema_object_bound"),
])
async def test_factored_mixed_catalog_preserves_capped_mask_and_policy_precedence(model_db, mode, count, error):
    connection = await model_db.connection()
    # Every original FTS ordinal appears before the cap. The invalid object is
    # last, so the beyond-cap case proves it cannot contaminate the summary.
    catalog = [(spec[0], name, spec[1]) for name, spec in headers._FTS_SQL.items()]
    catalog += [("index", "ix_memories_kind", "goals")] * (count - len(catalog) - 1)
    catalog.append(("index", "unknown_factoring_policy", "goals"))
    def check(conn):
        with corrupt_catalog(conn, catalog):
            expected = [tuple(row) for row in headers._sql(
                conn, headers._selected_schema_census_sql(), ())]
            assert len(expected) == min(count, 1360)
            assert [row[0] for row in expected] == list(range(1, len(expected) + 1))
            assert {row[2] for row in expected if row[2]} == set(range(1, 16))
            invalid = any(row[1] != 1 for row in expected)
            assert invalid == (mode != "beyond-cap-policy")
            query = headers._selected_schema_census_aggregate_sql()
            actual = [tuple(row) for row in headers._sql(conn, query, ())]
            assert actual == [(len(expected), 2 if invalid else 0, 32767)]
            budget = HeaderReadBudget()
            with pytest.raises(HeaderBoundsError, match="^" + error + "$"):
                headers._projected_schema_objects(conn, budget, _header_budget=budget)
            assert budget.remaining < 1_048_576
            assert budget.physical_references == frozenset()
    await connection.run_sync(check)


def _factored_projection_sql():
    query = headers._selected_schema_census_aggregate_sql()
    assert query.endswith(headers._CENSUS_AGGREGATE_SUFFIX)
    return query[:-len(headers._CENSUS_AGGREGATE_SUFFIX)] + (
        ") SELECT rowid, original_object_valid, original_fts_name_ordinal "
        "FROM projected ORDER BY rowid)")


async def test_overlapping_prefixes_empty_tail_and_residual_preserve_each_original_row(
        model_db, monkeypatch):
    connection = await model_db.connection()
    # Small test-owned rosters exercise the grouping rule independently of its
    # source helper. Overlap must neither duplicate a row nor bind tbl_name to
    # the name's lexical prefix. An empty tail remains an exact allowed member.
    monkeypatch.setattr(headers, "_SCHEMA_TABLES", frozenset({"root", "root_leaf"}))
    names = {"ix_root_kind", "ix_root_leaf_kind", "ux_root_leaf_kind",
        "ix_root_", "residual_index"}
    monkeypatch.setattr(headers, "_NAMED_INDEXES", frozenset(names))
    cases = [
        ("ix_root_kind", "root_leaf", True),
        ("ix_root_leaf_kind", "root", True),
        ("ix_root_leaf_kind", "root", True),  # duplicate input stays a row
        ("ux_root_leaf_kind", "root", True),
        ("ix_root_", "root_leaf", True),
        ("residual_index", "root", True),
        ("ix_root_leaf_", "root", False),
        ("ix_root_leaf_kind", "foreign", False),
        ("IX_root_leaf_kind", "root", False),
        ("ix_root_leaf_kind|kind", "root", False),
        ("ix_root_\x00leaf_kind", "root", False),
        ("ix_root_leaf_kind\x00", "root", False),
        ("residual_index\x00", "root", False),
    ]
    catalog = [("index", name, table) for name, table, _accepted in cases]
    expected = [(index, int(accepted), 0)
        for index, (_name, _table, accepted) in enumerate(cases, 1)]
    def check(conn):
        with corrupt_catalog(conn, catalog):
            original = [tuple(row) for row in headers._sql(
                conn, headers._selected_schema_census_sql(), ())]
            factored = [tuple(row) for row in headers._sql(
                conn, _factored_projection_sql(), ())]
            assert original == factored == expected
    await connection.run_sync(check)


@pytest.mark.parametrize("bound", [0, 1, 2**63 - 1])
async def test_auto_zero_and_signed_max_sources_keep_original_decimal_cast_policy(
        model_db, monkeypatch, bound):
    connection = await model_db.connection()
    policy = dict(headers._AUTO_MAX)
    policy["goals"] = bound
    monkeypatch.setattr(headers, "_AUTO_MAX", policy)
    suffixes = ("", "0", "000", "1", "0001", "2", "-1", "+1", " 1", "1 ",
        "1.0", "1e0", "\u0661", "1\u0661", "1|", "1\x00", "\x001",
        "9223372036854775807", "9223372036854775808", "9" * 90,
        "0" * 90 + "1")
    catalog = [("index", "sqlite_autoindex_goals_" + suffix, "goals")
        for suffix in suffixes]
    # SQLite CAST saturates at signed max. Preserve that original SQL behavior,
    # including leading zeroes; do not introduce a canonical decimal spelling.
    expected = [(index, int(bool(suffix) and suffix.isascii() and suffix.isdecimal()
        and 1 <= min(int(suffix), 2**63 - 1) <= bound), 0)
        for index, suffix in enumerate(suffixes, 1)]
    def check(conn):
        with corrupt_catalog(conn, catalog):
            original = [tuple(row) for row in headers._sql(
                conn, headers._selected_schema_census_sql(), ())]
            factored = [tuple(row) for row in headers._sql(
                conn, _factored_projection_sql(), ())]
            assert original == factored == expected
    await connection.run_sync(check)
