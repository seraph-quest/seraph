"""Genuine rollback-owner census scope; no execution receipt claimed here."""
import asyncio
import sqlite3

import pytest

from tests.test_programme_raw_rollback_owner import raw_paths
from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
from src.workspace.accounting_continuity import _RawRollbackPair
from src.workspace.accounting_witness import _original_composition_metadata_rows

TABLES = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid LIMIT 1360"


def test_original_paid_census_succeeds_without_general_aggregate_permission(raw_paths):
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.source.begin()
        certificate = pair.source.certify()
        with headers.snapshot_reads(certificate):
            before = certificate.budget.remaining
            rows = _original_composition_metadata_rows(pair.source._db, TABLES, (),
                ("name",), appearance=("table-census",))
            assert "goals" in {row[0] for row in rows}
            assert certificate.budget.remaining < before
            assert headers._RAW_METADATA_CENSUS.get() is None
            # Even the exact original string is not authority via direct SQL.
            with pytest.raises(sqlite3.OperationalError, match="not authorized"):
                pair.source._db.execute(headers._original_metadata_census_sql(TABLES, ("name",)))
        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            pair.source._db.execute("SELECT count(*) FROM goals")
    finally:
        pair.close()


@pytest.mark.parametrize("query", ["SELECT count(*) FROM goals", "SELECT sum(1)",
    "SELECT coalesce(NULL,1)", "SELECT hex(NULL)"])
def test_direct_functions_remain_denied_inside_snapshot(raw_paths, query):
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.source.begin()
        with headers.snapshot_reads(pair.source.certify()):
            with pytest.raises(sqlite3.OperationalError, match="not authorized"):
                pair.source._db.execute(query)
    finally:
        pair.close()


def test_original_census_rejects_wrong_connection_and_budget(raw_paths):
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.source.begin()
        pair.destination.begin()
        with headers.snapshot_reads(pair.source.certify()):
            query = headers._original_metadata_census_sql(TABLES, ("name",))
            for connection, budget in ((pair.destination._db, pair.source._budget),
                    (pair.source._db, HeaderReadBudget())):
                with pytest.raises(HeaderBoundsError, match="owner_unavailable"):
                    headers._metadata_rows(connection, query, (), _header_budget=budget,
                        upper=headers._metadata_upper(2, (20,20,20)), appearance=("test",))
            assert headers._RAW_METADATA_CENSUS.get() is None
    finally:
        pair.close()


@pytest.mark.parametrize("query,columns", [("SELECT title FROM goals", ("title",)),
    (TABLES, ('name) FROM goals --',)), ("SELECT name FROM sqlite_master", ("name",))])
def test_census_builder_rejects_private_bodies_and_unbounded_or_injected_sources(query, columns):
    with pytest.raises(HeaderBoundsError, match="owner_unavailable"):
        headers._original_metadata_census_sql(query, columns)


@pytest.mark.asyncio
async def test_inherited_snapshot_context_cannot_authorize_other_task(raw_paths):
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.source.begin()
        with headers.snapshot_reads(pair.source.certify()):
            async def stolen_context():
                with pytest.raises(HeaderBoundsError, match="owner_unavailable"):
                    _original_composition_metadata_rows(pair.source._db, TABLES, (),
                        ("name",), appearance=("table-census",))
            await asyncio.create_task(stolen_context())
            # Denial in the child does not invalidate the authentic parent.
            assert _original_composition_metadata_rows(pair.source._db, TABLES, (),
                ("name",), appearance=("table-census",))
    finally:
        pair.close()


def test_direct_metadata_helper_cannot_mint_census_scope_or_spend_frame(raw_paths):
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.source.begin()
        with headers.snapshot_reads(pair.source.certify()):
            before = pair.source._budget.remaining
            query = headers._original_metadata_census_sql(TABLES, ("name",))
            # Exact generated SQL, original bound, frame and appearance are
            # deliberately supplied. Only the original owner wrapper can arm it.
            with pytest.raises(HeaderBoundsError, match="owner_unavailable"):
                headers._metadata_rows(pair.source._db, query, (),
                    _header_budget=pair.source._budget,
                    upper=headers._metadata_upper(2, (20,20,20)),
                    appearance=("composition-schema-width", "table-census"))
            assert pair.source._budget.remaining == before
            assert headers._RAW_METADATA_CENSUS.get() is None
            with pytest.raises(sqlite3.OperationalError, match="not authorized"):
                pair.source._db.execute("SELECT count(*) FROM goals")
            assert _original_composition_metadata_rows(pair.source._db, TABLES, (),
                ("name",), appearance=("table-census",))
            assert headers._RAW_METADATA_CENSUS.get() is None
    finally:
        pair.close()
