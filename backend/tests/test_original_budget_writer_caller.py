"""The original public caller keeps the first retained writer certification."""
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.engine import get_session
from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, _trace_memory_numeric_charges
from src.runtime_plugins.ownership import begin_native_writer, CompositionBindingError
from tests.test_auth_session_composition_privacy import original_auth_composition, sql_state, private_files
from tests.test_discovery_compiler_precharge import original_effects


@pytest.mark.asyncio
async def test_public_writer_uses_one_real_snapshot_and_preserves_identity_double_payment(
        original_auth_composition, monkeypatch):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    original = headers.preflight_composition_superset
    certifications = []
    def observe(connection, frame, **kwargs):
        certifications.append((connection, connection.get_transaction(),
            connection.connection.driver_connection, frame))
        return original(connection, frame, **kwargs)
    monkeypatch.setattr(headers, "preflight_composition_superset", observe)
    class ObservedRollback(RuntimeError):
        pass
    with pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as db:
            connection = db.bind
            statements = []
            def sql(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)
            event.listen(connection.sync_connection, "before_cursor_execute", sql)
            try:
                guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
                common = await connection.run_sync(lambda conn:
                    headers._certify_current_memory_snapshot_on_connection(conn, budget, _header_budget=budget))
                selection = await connection.run_sync(lambda conn: guard._select_programmes(conn, common))
                with original_effects(connection) as (queries, compiles), _trace_memory_numeric_charges(budget) as trace:
                    identity = await connection.run_sync(lambda conn:
                        headers.preflight_programme_identity_component(common, original_selection=selection))
                assert [amount for appearance, amount in trace if appearance ==
                    ("metadata-prequery", "descriptor-compiler", "operator_identities")] == [58, 58]
                assert len(compiles) == 6 and queries
                assert identity.common_certificate is common
                assert len(certifications) == 1
                assert certifications[0] == (connection.sync_connection,
                    connection.sync_connection.get_transaction(),
                    connection.sync_connection.connection.driver_connection, budget)
                assert sum(statement == "BEGIN IMMEDIATE" for statement in statements) == 1
                assert guard._writer_first_metadata_mode == "original-budget-v1"
                assert guard.header_budget is budget and budget.remaining > 0
                raise ObservedRollback
            finally:
                event.remove(connection.sync_connection, "before_cursor_execute", sql)
    assert sql_state(case) == before and private_files(case) == files


@pytest.mark.asyncio
async def test_public_writer_rejects_foreign_session_before_query_or_debit(original_auth_composition):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    async with get_session(header_budget=budget) as db:
        connection = db.bind
        foreign = AsyncSession(bind=case.engine, expire_on_commit=False)
        statements = []
        def sql(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", sql)
        remaining = budget.remaining
        try:
            with pytest.raises(CompositionBindingError, match="composition_native_writer_budget_changed"):
                await begin_native_writer(foreign, owner="finite_service", header_budget=budget)
            assert statements == [] and budget.remaining == remaining
            assert not foreign.in_transaction()
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", sql)
            await foreign.close()
    assert sql_state(case) == before and private_files(case) == files
