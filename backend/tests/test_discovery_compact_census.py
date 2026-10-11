"""Same-connection census oracle; catalog fixtures never issue authority.

The original SQL is the verdict/output oracle. Corrupt-catalog rows are confined
by the original rollback-only fixture, and never certify a snapshot or body.
"""
import pytest

from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from tests.test_memory_composition_headers import model_db
from tests.test_discovery_schema_projection import corrupt_catalog

pytestmark = pytest.mark.asyncio


async def test_every_original_named_index_preserves_rows_and_independent_table_membership(model_db):
    # All 1146 original names, including uncommon ux_ and fallback groups, are
    # delivered together. A lexical table prefix must not grant/table-bind them.
    rows = [("index", name, "task_method_active" if name in {
        "ix_task_method_active_goal_id", "ix_task_method_active_owner_identity_id"
    } else "goals") for name in sorted(headers._NAMED_INDEXES)]
    connection = await model_db.connection()
    def check(conn):
        with corrupt_catalog(conn, rows):
            original = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_sql(), ())]
            budget = HeaderReadBudget()
            actual = [tuple(row) for row in headers._selected_census_rows(conn, budget)]
            assert actual == original
            assert len(actual) == 1146 and all(row[1] == 1 for row in actual)
            assert budget.physical_references == frozenset()
    await connection.run_sync(check)


ADVERSARIAL_ROWS = [
    ("index", "ix_memories_kind_extra", "memories"),
    ("index", "ix_memories_kind\x00extra", "memories"),
    ("index", "ux_memory_proposals_owner_attempt_no_learning\x00extra", "memory_proposals"),
    ("index", "IX_memories_kind", "memories"),
    ("index", "ix_memories_kınd", "memories"),
    ("index", "ix_memories_非ascii", "memories"),
    ("index", "ix_memories_kind", "goals"),
    ("index", "ix_task_method_active_goal_id", "goals"),
    ("index", "ix_task_method_active_goal_id", "task_method_active"),
    ("index", "sqlite_autoindex_goals_0001", "goals"),
    ("index", "sqlite_autoindex_goals_9223372036854775808", "goals"),
    ("index", "sqlite_autoindex_goals_-1", "goals"),
    ("index", "sqlite_autoindex_foreign_0001", "foreign"),
    ("index", "sqlite_autoindex_task_method_active_0002", "task_method_active"),
    ("index", "sqlite_autoindex_task_method_active_0003", "task_method_active"),
    ("index", "sqlite_autoindex_goals_0001\x00", "goals"),
    ("trigger", "session_recall_sessions_ad", "sessions"),
    ("trigger", "session_recall_sessions_ad", "goals"),
]


@pytest.mark.parametrize("kind,name,table", ADVERSARIAL_ROWS)
async def test_compact_prefix_auto_and_fts_predicates_equal_original_delivery(model_db, kind, name, table):
    connection = await model_db.connection()
    def check(conn):
        # A valid surrounding table establishes exact ordered delivery; no
        # predicate can silently omit the adversarial object or its verdict.
        with corrupt_catalog(conn, [("table", "memories", "memories"), (kind, name, table)]):
            original = [tuple(row) for row in headers._sql(conn, headers._selected_schema_census_sql(), ())]
            budget = HeaderReadBudget()
            actual = [tuple(row) for row in headers._selected_census_rows(conn, budget)]
            assert actual == original and len(actual) == 2
            assert [row[0] for row in actual] == [1, 2]
            assert budget.physical_references == frozenset()
            # The full selected census must deny the same invalid flag and
            # preserve the original FTS ordinal for accepted trigger names.
            current = HeaderReadBudget()
            if original[1][1] == 0:
                with pytest.raises(HeaderBoundsError, match="^header_schema_object_unavailable$"):
                    headers._projected_schema_objects(conn, current, _header_budget=current)
            else:
                names = headers._projected_schema_objects(conn, current, _header_budget=current)
                expected = {name} if original[1][2] else set()
                assert names == expected
    await connection.run_sync(check)
