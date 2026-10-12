"""Existing whole-schema proof is reusable only inside its genuine operation."""
import copy

import pytest

from src.db.engine import get_session
from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.runtime_plugins.ownership import begin_native_writer
from src.workspace.accounting_witness import RETAINED_FIELDS, validate_retained_table_schema
from tests.test_auth_session_composition_privacy import original_auth_composition, sql_state, private_files
from tests.test_discovery_compiler_precharge import original_effects
from tests.test_programme_raw_rollback_owner import raw_paths
from src.workspace.accounting_continuity import _RawRollbackPair


class ObservedRollback(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_retained_columns_use_original_schema_proof_and_paid_cookie_validation(original_auth_composition):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    with pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as db:
            await begin_native_writer(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            common = await connection.run_sync(lambda conn:
                headers._certify_current_memory_snapshot_on_connection(conn, budget, _header_budget=budget))
            remaining = budget.remaining
            with original_effects(connection) as (queries, _):
                def check(conn):
                    with headers.snapshot_reads(common):
                        validate_retained_table_schema(conn, "goals")
                        assert not headers._original_retained_schema_proved(conn, "goals", ("id",))
                        assert not headers._original_retained_schema_proved(conn, "sessions", RETAINED_FIELDS["sessions"])
                await connection.run_sync(check)
            assert queries and not any("pragma_table_info" in sql.lower() for sql in queries)
            assert budget.remaining < remaining and budget.remaining > 0
            raise ObservedRollback
    assert sql_state(case) == before and private_files(case) == files


def test_original_raw_certificate_keeps_paid_retained_schema_validation(raw_paths):
    budget = HeaderReadBudget()
    pair = _RawRollbackPair(*raw_paths, budget)
    try:
        pair.source.begin()
        common = pair.source.certify()
        assert common.raw_owner is pair.source
        assert headers._SNAPSHOT_CERTIFICATES.get(id(common)) is None
        with headers.snapshot_reads(common):
            remaining, queries = budget.remaining, pair.source._trace_count
            assert not headers._original_retained_schema_proved(
                pair.source._db, "goals", RETAINED_FIELDS["goals"])
            assert budget.remaining == remaining and pair.source._trace_count == queries
            validate_retained_table_schema(pair.source._db, "goals")
            assert budget.remaining < remaining
            assert pair.source._trace_count > queries
    finally:
        pair.close()


def test_copied_raw_certificate_cannot_enter_retained_schema_fallback(raw_paths):
    budget = HeaderReadBudget()
    pair = _RawRollbackPair(*raw_paths, budget)
    try:
        pair.source.begin()
        common = pair.source.certify()
        with headers.snapshot_reads(common):
            remaining, queries = budget.remaining, pair.source._trace_count
            token = headers._CURRENT.set(copy.copy(common))
            try:
                with pytest.raises(HeaderBoundsError, match="^header_certificate_unavailable$"):
                    validate_retained_table_schema(pair.source._db, "goals")
            finally:
                headers._CURRENT.reset(token)
            assert budget.remaining == remaining and pair.source._trace_count == queries
    finally:
        pair.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["copied", "foreign_connection", "changed_schema"])
async def test_retained_schema_proof_rejects_changed_original_owner(original_auth_composition, invalid):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    with pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as db:
            await begin_native_writer(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            common = await connection.run_sync(lambda conn:
                headers._certify_current_memory_snapshot_on_connection(conn, budget, _header_budget=budget))
            if invalid == "changed_schema":
                await connection.exec_driver_sql("ALTER TABLE goals ADD COLUMN unreviewed_schema_column TEXT")
            if invalid == "foreign_connection":
                async with case.engine.connect() as foreign:
                    def reject(conn):
                        token = headers._CURRENT.set(common)
                        try:
                            with pytest.raises(HeaderBoundsError):
                                headers._original_retained_schema_proved(conn, "goals", RETAINED_FIELDS["goals"])
                        finally:
                            headers._CURRENT.reset(token)
                    await foreign.run_sync(reject)
            else:
                def reject(conn):
                    token = headers._CURRENT.set(copy.copy(common) if invalid == "copied" else common)
                    try:
                        with pytest.raises(HeaderBoundsError):
                            headers._original_retained_schema_proved(conn, "goals", RETAINED_FIELDS["goals"])
                    finally:
                        headers._CURRENT.reset(token)
                await connection.run_sync(reject)
            raise ObservedRollback
    assert sql_state(case) == before and private_files(case) == files
