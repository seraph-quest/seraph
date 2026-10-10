"""Private selector denials on genuine original Auth/stopped composition.

No positive live-host admission is manufactured here. That fixture remains an
integration dependency until the original lifecycle owner is available.
"""
from contextlib import contextmanager
from dataclasses import replace

import pytest
from sqlalchemy import event

from src.db.engine import get_session
from src.db.models import Goal, OperatorIdentity, OperatorSession
from src.guardian.goal_discovery import goal_discovery_service
from src.memory.header_bounds import HeaderReadBudget
from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection
from src.runtime_plugins.bridge import cordis_host
from src.runtime_plugins.ownership import begin_native_writer
from src.workspace.production import ProductionWorkspaceReconciliationError
from tests.test_auth_session_composition_privacy import original_auth_composition, private_files, sql_state


class _SelectionReadComplete(Exception):
    """End a read-only inspection through the original session rollback path."""


@contextmanager
def selected_body_observer(engine):
    starts, deliveries = [], []
    models = (Goal, OperatorSession, OperatorIdentity)
    full_columns = [set(column.name for column in model.__table__.columns) for model in models]
    def before(connection, cursor, statement, parameters, context, executemany):
        compiled = getattr(context, "compiled", None)
        query = getattr(compiled, "statement", None)
        for item in getattr(query, "column_descriptions", ()):
            if any(item.get("expr") is model for model in models):
                starts.append("mapped-selected-body")
                raise AssertionError("selected body started before owner denial")
    def after(connection, cursor, statement, parameters, context, executemany):
        names = {item[0] for item in cursor.description or ()}
        if any(columns.issubset(names) for columns in full_columns):
            deliveries.append("literal-selected-body")
            raise AssertionError("selected body delivered before owner denial")
    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)
    try:
        yield starts, deliveries
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
        event.remove(engine.sync_engine, "after_cursor_execute", after)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["foreign_service", "foreign_host", "foreign_frame", "ended", "stale"])
async def test_prospective_owner_denial_precedes_selected_bodies(original_auth_composition, damage):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    with pytest.raises(_SelectionReadComplete):
        async with get_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner="finite_service", fresh=False, header_budget=budget)
            connection = await db.connection()
            certificate = await connection.run_sync(
                lambda current: _certify_current_memory_snapshot_on_connection(current, budget))
            service, host = goal_discovery_service, cordis_host
            if damage == "foreign_service":
                service = object()  # Negative only; never an issuer.
            elif damage == "foreign_host":
                host = object()
            elif damage == "foreign_frame":
                other = HeaderReadBudget()
                certificate = await connection.run_sync(
                    lambda current: _certify_current_memory_snapshot_on_connection(current, other))
            elif damage == "ended":
                guard._finish_programme_admission_selection()
            elif damage == "stale":
                await db.rollback()
                connection = await db.connection()
            with selected_body_observer(case.engine) as (starts, deliveries):
                with pytest.raises(ProductionWorkspaceReconciliationError,
                                   match="^programme_original_selection_unavailable$"):
                    await connection.run_sync(lambda current: guard._select_programme_admission(
                        current, certificate, service=service, host=host,
                        goal_id="not-issued", programme_id="not-issued", grant_revision=1))
            assert starts == deliveries == []
            assert guard._prospective_admission is None
            assert guard._prospective_admission_facts is None
            raise _SelectionReadComplete()
    assert sql_state(case) == before
    assert private_files(case) == files


@pytest.mark.asyncio
async def test_original_persisted_selection_still_rejects_copy(original_auth_composition):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    with pytest.raises(_SelectionReadComplete):
        async with get_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner="finite_service", fresh=False, header_budget=budget)
            connection = await db.connection()
            def check(current):
                common33 = _certify_current_memory_snapshot_on_connection(current, budget)
                selection = guard._select_programmes(current, common33)
                guard._validate_programme_selection(selection)
                assert selection.runs == {} and selection.identity_ids == ()
                assert guard._prospective_admission is None
                with pytest.raises(ProductionWorkspaceReconciliationError,
                                   match="^programme_original_selection_unavailable$"):
                    guard._validate_programme_selection(replace(selection))
            await connection.run_sync(check)
            raise _SelectionReadComplete()
    assert sql_state(case) == before
    assert private_files(case) == files
