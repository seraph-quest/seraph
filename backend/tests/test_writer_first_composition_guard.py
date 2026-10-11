"""Private writer foundation on original lifecycle/host; no admission/effects."""
import asyncio
import copy
import fcntl
import os

import pytest
from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.engine import get_session
from src.db.models import Goal, ProgrammeNotificationPreference
from src.guardian.goal_discovery import goal_discovery_service
from src.guardian.goal_programmes import goal_programme_service
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.memory.composition_headers import (
    _memory_current_snapshot_scope, _certify_current_memory_snapshot_on_connection, _validate)
from src.runtime_plugins.bridge import cordis_host
from src.runtime_plugins.ownership import _begin_native_writer_first
from src.workspace.accounting_witness import prepare_composition_session
from src.workspace.production import ProductionWorkspaceReconciliationError
from tests.test_programme_prospective_live_selection import (
    genuine_live_selection, SelectionObserved, observe_identity_body, private_outputs)
from tests.test_auth_session_composition_privacy import sql_state


@pytest.mark.asyncio
@pytest.mark.parametrize("stage, reason", [
    ("reader", "composition_native_writer_required"),
    ("writer", "composition_unhooked_bulk_sql"),
])
async def test_real_guards_still_deny_cte_retained_mutation(
        genuine_live_selection, stage, reason):
    case = genuine_live_selection
    before, files = sql_state(case), private_outputs(case)
    budget = HeaderReadBudget()
    with pytest.raises(ProductionWorkspaceReconciliationError, match="^" + reason + "$"):
        with _memory_current_snapshot_scope(budget):
            async with get_session(header_budget=budget) as db:
                if stage == "writer":
                    guard = await _begin_native_writer_first(
                        db, owner="finite_service", header_budget=budget)
                    assert db.info["composition_guard"] is guard
                    assert db.info["native_writer_started"] is True
                else:
                    assert db.info["composition_read_guard"].closed is False
                    assert not db.info.get("native_writer_started")
                await db.execute(text(
                    "WITH attempted AS (SELECT 1) UPDATE workflow_run_states SET result_summary=result_summary"))
    assert sql_state(case) == before
    assert private_outputs(case) == files
    assert case.contacts == []


def assert_lock_released(case):
    fd = os.open(case.workspace.lifecycle_directory / "accounting.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_writer_first_original_selection_on_one_retained_transaction(
        genuine_live_selection, monkeypatch):
    case = genuine_live_selection
    before, files = sql_state(case), private_outputs(case)
    from src.memory import composition_headers
    original_preflight = composition_headers.preflight_composition_superset
    snapshots = []
    def observed_preflight(connection, budget, **kwargs):
        assert connection.connection.driver_connection.in_transaction
        assert kwargs["_header_budget"] is budget
        snapshots.append(connection.get_transaction())
        return original_preflight(connection, budget, **kwargs)
    monkeypatch.setattr(composition_headers, "preflight_composition_superset", observed_preflight)
    statements, rollbacks = [], []
    def sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    def rollback(connection):
        rollbacks.append(len(statements))
    event.listen(case.engine.sync_engine, "before_cursor_execute", sql)
    event.listen(case.engine.sync_engine, "rollback", rollback)
    observed = []
    async def boundary(**requested):
        budget = HeaderReadBudget()
        with _memory_current_snapshot_scope(budget):
            async with get_session(header_budget=budget) as db:
                guard = await _begin_native_writer_first(db, owner="finite_service", header_budget=budget)
                assert guard.header_budget is budget and db.info["composition_guard"] is guard
                assert db.info["native_writer_started"] is True
                connection = await db.connection()
                certificate = await connection.run_sync(lambda conn:
                    _certify_current_memory_snapshot_on_connection(conn, budget, _header_budget=budget))
                assert certificate.metadata_budget is budget
                assert len(snapshots) == 1
                begin = statements.index("BEGIN IMMEDIATE")
                assert not any(index > begin for index in rollbacks)
                with observe_identity_body(case.engine, budget, case.operator.operator_identity_id) as (bodies, provenance):
                    programme, binding, common, identity = await connection.run_sync(lambda conn:
                        guard._select_programme_admission(conn, certificate,
                            service=goal_discovery_service, host=cordis_host,
                            goal_id=requested["goal_id"], programme_id=requested["programme_id"],
                            grant_revision=requested["grant_revision"]))
                    current = await goal_programme_service.validate_current_binding(db=db, binding=binding,
                        policy=case.policy, original_admission_guard=guard, identity_certificate=identity)
                    assert current.id == programme.id and common is certificate
                    assert bodies and provenance and budget.remaining < 1048576
                observed.append(guard)
                raise SelectionObserved("writer inspected without admission")
    monkeypatch.setattr(goal_programme_service, "assert_authority", boundary)
    try:
        with pytest.raises(SelectionObserved):
            await goal_discovery_service.admit(goal_id=case.goal_id, programme_id=case.programme["id"],
                grant_revision=case.programme["grant_revision"])
    finally:
        event.remove(case.engine.sync_engine, "before_cursor_execute", sql)
        event.remove(case.engine.sync_engine, "rollback", rollback)
    assert observed[0]._retention_closed
    assert_lock_released(case)
    assert sql_state(case) == before and private_outputs(case) == files and case.contacts == []


@pytest.mark.asyncio
async def test_original_reader_preparation_still_rolls_back(genuine_live_selection):
    case = genuine_live_selection
    before, files = sql_state(case), private_outputs(case)
    budget = HeaderReadBudget()
    with pytest.raises(SelectionObserved):
        with _memory_current_snapshot_scope(budget):
            async with get_session(header_budget=budget) as db:
                guard = await prepare_composition_session(db, header_budget=budget)
                assert not db.in_transaction()
                assert not db.info.get("native_writer_started")
                assert guard.header_budget is budget
                raise SelectionObserved("reader rollback unchanged")
    assert_lock_released(case)
    assert sql_state(case) == before and private_outputs(case) == files


@pytest.mark.parametrize("scenario", ["copy", "foreign_frame", "foreign_task", "no_scope", "active_transaction"])
@pytest.mark.asyncio
async def test_writer_first_rejects_detached_resource_owner(genuine_live_selection, scenario):
    case = genuine_live_selection
    before, files = sql_state(case), private_outputs(case)
    budget = HeaderReadBudget()
    with pytest.raises(SelectionObserved):
        async with get_session(header_budget=budget) as db:
            async def invoke(frame):
                return await _begin_native_writer_first(db, owner="finite_service", header_budget=frame)
            if scenario == "no_scope":
                with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_writer_first_unavailable"):
                    await invoke(budget)
            else:
                with _memory_current_snapshot_scope(budget):
                    if scenario == "active_transaction":
                        await db.connection()
                        with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_writer_first_unavailable"):
                            await invoke(budget)
                        await db.rollback()
                    elif scenario == "foreign_task":
                        with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
                            await asyncio.create_task(invoke(budget))
                    else:
                        frame = copy.copy(budget) if scenario == "copy" else HeaderReadBudget()
                        with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
                            await invoke(frame)
            assert db.info.get("composition_guard") is None
            assert not db.info.get("native_writer_started")
            raise SelectionObserved("no protected body")
    assert_lock_released(case)
    assert sql_state(case) == before and private_outputs(case) == files


@pytest.mark.parametrize("fault", ["exhausted", "cancel", "checkpoint"])
@pytest.mark.asyncio
async def test_writer_first_failure_rolls_back_and_releases_original_lock(
        genuine_live_selection, monkeypatch, fault):
    case = genuine_live_selection
    before, files = sql_state(case), private_outputs(case)
    budget = HeaderReadBudget()
    fault_balances = []
    if fault == "cancel":
        from src.memory import composition_headers
        def cancelled(connection, frame, **kwargs):
            assert frame is budget and kwargs["_header_budget"] is budget
            fault_balances.append(budget.remaining)
            raise asyncio.CancelledError
        monkeypatch.setattr(composition_headers, "_certify_current_memory_snapshot_on_connection", cancelled)
    elif fault == "checkpoint":
        from src.workspace import production
        original = production.read_accounting_checkpoint
        def changed_checkpoint(*args, **kwargs):
            result = original(*args, **kwargs)
            assert result and result["schema_version"] == 2
            assert kwargs["header_budget"] is budget
            fault_balances.append(budget.remaining)
            return {**result, "composition_target": None}  # Negative changed readback, never accepted proof.
        monkeypatch.setattr(production, "read_accounting_checkpoint", changed_checkpoint)
    with pytest.raises(SelectionObserved):
        with _memory_current_snapshot_scope(budget):
            async with get_session(header_budget=budget) as db:
                if fault == "exhausted":
                    budget.debit(budget.remaining)
                before_attempt = budget.remaining
                error = (HeaderBoundsError if fault == "exhausted" else
                    asyncio.CancelledError if fault == "cancel" else ProductionWorkspaceReconciliationError)
                with pytest.raises(error):
                    await _begin_native_writer_first(db, owner="finite_service", header_budget=budget)
                assert not db.in_transaction()
                assert not db.info.get("native_writer_started")
                assert db.info.get("composition_guard") is None
                assert budget.remaining <= before_attempt
                assert_lock_released(case)
                if fault == "exhausted":
                    assert budget.remaining == 0 and fault_balances == []
                else:
                    # Keep every actual debit observed at the injected failure,
                    # including internal probe/lock/readback/snapshot payments.
                    assert fault_balances == [budget.remaining]
                raise SelectionObserved("failure cleaned up")
    assert sql_state(case) == before and private_outputs(case) == files


@pytest.mark.asyncio
async def test_real_flush_invalidates_certificate_and_recertification_is_paid(genuine_live_selection):
    case = genuine_live_selection
    # Unprotected preference is numerical write input, not Source/authority.
    factory = sessionmaker(case.engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(ProgrammeNotificationPreference(owner_identity_id=case.operator.operator_identity_id,
            recipient_principal_id=case.operator.principal.principal_id, recipient_root_id=case.operator.session_id))
        await db.commit()
    before, files = sql_state(case), private_outputs(case)
    budget = HeaderReadBudget()
    with pytest.raises(SelectionObserved):
        with _memory_current_snapshot_scope(budget):
            async with get_session(header_budget=budget) as db:
                await _begin_native_writer_first(db, owner="finite_service", header_budget=budget)
                connection = await db.connection()
                certificate = await connection.run_sync(lambda conn:
                    _certify_current_memory_snapshot_on_connection(conn, budget, _header_budget=budget))
                preference = await db.get(ProgrammeNotificationPreference, case.operator.operator_identity_id)
                preference.enabled = True
                await db.flush()
                with pytest.raises(HeaderBoundsError, match="header_certificate_stale"):
                    await connection.run_sync(lambda conn: _validate(conn, certificate))
                remaining = budget.remaining
                try:
                    fresh = await connection.run_sync(lambda conn:
                        _certify_current_memory_snapshot_on_connection(conn, budget, _header_budget=budget))
                except HeaderBoundsError as error:
                    # Full second snapshot may honestly exceed the same spent frame.
                    assert str(error) == "canonical_bound_not_certified"
                else:
                    assert fresh is not certificate and fresh.budget is budget
                    await connection.run_sync(lambda conn: _validate(conn, fresh))
                assert budget.remaining < remaining
                raise SelectionObserved("actual write rolled back")
    assert_lock_released(case)
    assert sql_state(case) == before and private_outputs(case) == files


@pytest.mark.parametrize("scenario", ["copy", "wrong_connection"])
@pytest.mark.asyncio
async def test_writer_first_certificate_keeps_original_connection(genuine_live_selection, scenario):
    case = genuine_live_selection
    before, files = sql_state(case), private_outputs(case)
    budget = HeaderReadBudget()
    with pytest.raises(SelectionObserved):
        with _memory_current_snapshot_scope(budget):
            async with get_session(header_budget=budget) as db:
                await _begin_native_writer_first(db, owner="finite_service", header_budget=budget)
                connection = await db.connection()
                certificate = await connection.run_sync(lambda conn:
                    _certify_current_memory_snapshot_on_connection(conn, budget, _header_budget=budget))
                if scenario == "copy":
                    with pytest.raises(HeaderBoundsError, match="header_certificate_unavailable"):
                        await connection.run_sync(lambda conn: _validate(conn, copy.copy(certificate)))
                else:
                    factory = sessionmaker(case.engine, class_=AsyncSession, expire_on_commit=False)
                    async with factory() as foreign:
                        other = await foreign.connection()
                        with pytest.raises(HeaderBoundsError, match="header_certificate_unavailable"):
                            await other.run_sync(lambda conn: _validate(conn, certificate))
                        await foreign.rollback()
                raise SelectionObserved("certificate rejected without body")
    assert_lock_released(case)
    assert sql_state(case) == before and private_outputs(case) == files


@pytest.mark.asyncio
async def test_writer_first_unknown_schema_denies_without_body(genuine_live_selection):
    case = genuine_live_selection
    # Actual unsupported schema in disposable SQLite; no accepted marker/proof.
    async with case.engine.begin() as connection:
        await connection.exec_driver_sql("CREATE TABLE writer_first_bad(id INTEGER)")
    before, files = sql_state(case), private_outputs(case)
    budget = HeaderReadBudget()
    with pytest.raises(SelectionObserved):
        with _memory_current_snapshot_scope(budget):
            async with get_session(header_budget=budget) as db:
                with pytest.raises(HeaderBoundsError, match="^header_schema_object_unavailable$"):
                    await _begin_native_writer_first(db, owner="finite_service", header_budget=budget)
                assert not db.in_transaction()
                assert db.info.get("composition_guard") is None
                assert not db.info.get("native_writer_started")
                raise SelectionObserved("schema blocked")
    assert_lock_released(case)
    assert sql_state(case) == before and private_outputs(case) == files


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["none", "preference", "retained_goal"])
async def test_original_session_publishes_writer_first_same_budget(
        genuine_live_selection, monkeypatch, mutation):
    case = genuine_live_selection
    if mutation == "preference":
        factory = sessionmaker(case.engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            db.add(ProgrammeNotificationPreference(owner_identity_id=case.operator.operator_identity_id,
                recipient_principal_id=case.operator.principal.principal_id,
                recipient_root_id=case.operator.session_id))
            await db.commit()
    before, files = sql_state(case), private_outputs(case)
    from src.memory import composition_headers
    original = composition_headers._certify_current_memory_snapshot_on_connection
    requests = []
    budget = HeaderReadBudget()
    def observed(connection, requested, **kwargs):
        assert requested is budget and kwargs["_header_budget"] is budget
        remaining = budget.remaining
        result = original(connection, requested, **kwargs)
        assert result.metadata_budget is budget and result.budget is budget
        assert budget.remaining <= remaining
        requests.append((connection, result, remaining, budget.remaining))
        return result
    monkeypatch.setattr(composition_headers, "_certify_current_memory_snapshot_on_connection", observed)
    with _memory_current_snapshot_scope(budget) as scope:
        async with get_session(header_budget=budget) as db:
            guard = await _begin_native_writer_first(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            issued = guard._writer_first_metadata_frame
            assert issued[0] is scope and issued[1] is budget
            assert issued[2] is guard._retention_writer_snapshot
            initial = requests[0][1]
            if mutation == "preference":
                row = await db.get(ProgrammeNotificationPreference, case.operator.operator_identity_id)
                row.enabled = True
            elif mutation == "retained_goal":
                row = await db.get(Goal, case.goal_id)
                row.title += "!"
            if mutation != "none":
                await db.flush()
                with pytest.raises(HeaderBoundsError, match="^header_certificate_stale$"):
                    await connection.run_sync(lambda conn: _validate(conn, initial))
            before_publication = budget.remaining
        # Original get_session invoked original publish, commit and close.
        assert guard.prepared_commit and guard._retention_closed
        assert guard._writer_first_metadata_frame is None
        assert guard._writer_first_metadata_mode == "original-budget-v1"
        assert budget.remaining < before_publication
        assert all(item[0] is connection.sync_connection for item in requests)
        if mutation != "none":
            assert any(item[1] is not initial for item in requests[1:])
    after = sql_state(case)
    if mutation == "none":
        assert after == before and private_outputs(case) == files
    else:
        table, field, identity = (("programme_notification_preferences", "enabled", case.operator.operator_identity_id)
            if mutation == "preference" else ("goals", "title", case.goal_id))
        # All database changes must come from the actual ORM write or original
        # composition publisher; no caller, queue, Auth or provider is activated.
        allowed = {table}
        assert set(after) == set(before)
        for name in before:
            if name not in allowed:
                assert after[name] == before[name]
        columns, old_rows = before[table]
        new_columns, new_rows = after[table]
        assert columns == new_columns and len(old_rows) == len(new_rows)
        key = "owner_identity_id" if mutation == "preference" else "id"
        old = {r[columns.index(key)]: r for r in old_rows}
        new = {r[columns.index(key)]: r for r in new_rows}
        assert old.keys() == new.keys()
        for row_id in old:
            changed = {col for i, col in enumerate(columns) if old[row_id][i] != new[row_id][i]}
            assert changed == ({field} if row_id == identity else set())
        if mutation == "preference":
            assert private_outputs(case) == files
        else:
            assert private_outputs(case) != files
    assert_lock_released(case)
    assert case.contacts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [
    "copy_guard", "copy_and_clear", "paired_clear", "missing_frame", "unknown_mode", "budget", "snapshot_copy",
    "foreign_task", "closed_scope", "replaced_scope", "direct_issuer",
])
async def test_issued_writer_metadata_rejects_damage_before_flush_or_sql(
        genuine_live_selection, damage):
    case = genuine_live_selection
    before, files = sql_state(case), private_outputs(case)
    budget = HeaderReadBudget()
    scope_context = _memory_current_snapshot_scope(budget)
    scope_context.__enter__()
    scope_open = True
    replacement = None
    statements, flushes = [], []
    def statement(*args):
        statements.append(True)
    def flush(*args):
        flushes.append(True)
    try:
        with pytest.raises(SelectionObserved):
            async with get_session(header_budget=budget) as db:
                guard = await _begin_native_writer_first(db, owner="finite_service", header_budget=budget)
                connection = await db.connection()
                target = guard
                if damage in {"copy_guard", "copy_and_clear"}:
                    target = copy.copy(guard)
                    if damage == "copy_and_clear":
                        target._writer_first_metadata_mode = None
                        target._writer_first_metadata_frame = None
                elif damage == "paired_clear":
                    guard._writer_first_metadata_mode = None
                    guard._writer_first_metadata_frame = None
                elif damage == "missing_frame":
                    guard._writer_first_metadata_frame = None
                elif damage == "unknown_mode":
                    guard._writer_first_metadata_mode = "unissued"
                elif damage == "budget":
                    guard.header_budget = HeaderReadBudget()
                elif damage == "snapshot_copy":
                    guard._retention_writer_snapshot = tuple(list(guard._retention_writer_snapshot))
                elif damage in {"closed_scope", "replaced_scope"}:
                    scope_context.__exit__(None, None, None)
                    scope_open = False
                    if damage == "replaced_scope":
                        replacement = _memory_current_snapshot_scope(budget)
                        replacement.__enter__()
                event.listen(case.engine.sync_engine, "before_cursor_execute", statement)
                event.listen(db.sync_session, "before_flush", flush)
                remaining = budget.remaining
                try:
                    with pytest.raises(HeaderBoundsError):
                        if damage == "foreign_task":
                            await asyncio.create_task(target.publish())
                        elif damage == "direct_issuer":
                            await connection.run_sync(lambda conn:
                                target._issue_writer_first_metadata_mode(conn, budget))
                        else:
                            await target.publish()
                    assert statements == [] and flushes == []
                    assert budget.remaining == remaining
                finally:
                    event.remove(case.engine.sync_engine, "before_cursor_execute", statement)
                    event.remove(db.sync_session, "before_flush", flush)
                raise SelectionObserved("invalid writer mode never publishes")
    finally:
        if replacement is not None:
            replacement.__exit__(None, None, None)
        if scope_open:
            scope_context.__exit__(None, None, None)
    assert sql_state(case) == before and private_outputs(case) == files
    assert_lock_released(case)
    assert case.contacts == []


@pytest.mark.asyncio
async def test_default_guard_metadata_mode_and_closed_issued_mode(genuine_live_selection):
    case = genuine_live_selection
    before, files = sql_state(case), private_outputs(case)
    budget = HeaderReadBudget()
    with pytest.raises(SelectionObserved):
        with _memory_current_snapshot_scope(budget):
            async with get_session(header_budget=budget) as db:
                default = await prepare_composition_session(db, header_budget=budget)
                assert default._writer_metadata_budget() is None
                assert default._writer_first_metadata_mode is None
                raise SelectionObserved("original default remains unissued")
    # A separate rolled-back invocation has its own original frame.
    budget = HeaderReadBudget()
    with pytest.raises(SelectionObserved):
        with _memory_current_snapshot_scope(budget):
            async with get_session(header_budget=budget) as db:
                issued = await _begin_native_writer_first(db, owner="finite_service", header_budget=budget)
                raise SelectionObserved("real issued guard closes normally")
    with pytest.raises(HeaderBoundsError, match="^memory_retention_writer_changed$"):
        issued._writer_metadata_budget()
    issued._writer_first_metadata_mode = None
    issued._writer_first_metadata_frame = None
    with pytest.raises(HeaderBoundsError, match="^memory_retention_writer_changed$"):
        issued._writer_metadata_budget()
    assert sql_state(case) == before and private_outputs(case) == files
    assert_lock_released(case)
    assert case.contacts == []
