"""Actual session writer mechanics; no accepted programme or execution Source."""
from datetime import timedelta

import pytest
from sqlalchemy import event

from src.auth import service as auth
from src.db.engine import get_session
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.memory.composition_headers import _certify_current_memory_snapshot, snapshot_reads_async
from src.runtime_plugins.ownership import begin_native_writer
from src.workspace.accounting_witness import (
    CompositionSessionGuard, _programme_reference_row_on_connection,
)
from src.workspace.production import ProductionWorkspaceReconciliationError
from tests.test_auth_session_composition_privacy import (
    original_auth_composition, sql_state, private_files, root_row, utc,
    assert_only_original_root_changed, state_digest,
)


@pytest.mark.asyncio
async def test_original_auth_reserves_once_before_mutation_same_frame(original_auth_composition, monkeypatch):
    case = original_auth_composition
    before = sql_state(case)
    case.clock[0] = utc(root_row(case, before)["last_seen_at"]) + timedelta(seconds=31)
    budgets, reservations = [], []
    original_init = HeaderReadBudget.__init__
    def observe_init(budget):
        original_init(budget)
        budgets.append(budget)
    original_reserve = CompositionSessionGuard.reserve_session_mutation
    async def observe_reserve(guard, record, changes):
        assert not guard.db.dirty
        assert state_digest(sql_state(case)) == state_digest(before)
        assert guard.header_budget is budgets[0]
        assert guard.db.info["composition_writer_owner"] == "finite_service"
        await original_reserve(guard, record, changes)
        assert not guard.db.dirty
        assert state_digest(sql_state(case)) == state_digest(before)
        reservations.append((guard._session_reservation, guard.header_budget.remaining))
    monkeypatch.setattr(HeaderReadBudget, "__init__", observe_init)
    monkeypatch.setattr(CompositionSessionGuard, "reserve_session_mutation", observe_reserve)
    operator = await auth.authenticate_token(case.token)
    assert operator.session_id == case.operator.session_id
    assert len(budgets) == len(reservations) == 1
    reservation, after_reserve = reservations[0]
    assert reservation.closed and not any(reservation.entries.values())
    assert reservation.consumed
    # Publication consumes its prepaid resource appearances, not a fresh frame.
    assert budgets[0].remaining == after_reserve
    assert_only_original_root_changed(case, before, {"last_seen_at", "idle_expires_at"})


@pytest.mark.asyncio
async def test_reservation_exhaustion_has_no_sql_or_private_effect(original_auth_composition, monkeypatch):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    case.clock[0] = utc(root_row(case, before)["last_seen_at"]) + timedelta(seconds=31)
    original_debit = HeaderReadBudget.debit
    reached = []
    def exhaust_at_reservation(budget, amount, *, appearance=None):
        if appearance == ("session-publication-reservation",):
            reached.append(amount)
            original_debit(budget, budget.remaining)
        return original_debit(budget, amount, appearance=appearance)
    monkeypatch.setattr(HeaderReadBudget, "debit", exhaust_at_reservation)
    with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
        await auth.authenticate_token(case.token)
    assert len(reached) == 1 and reached[0] > 0
    assert state_digest(sql_state(case)) == state_digest(before)
    assert private_files(case) == files


@pytest.mark.asyncio
async def test_unresolved_extra_mutation_denied_before_flush(original_auth_composition, monkeypatch):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    case.clock[0] = utc(root_row(case, before)["last_seen_at"]) + timedelta(seconds=31)
    original_change = auth._original_session_change
    async def changed_bind(db, record, changes):
        await original_change(db, record, changes)
        record.principal_id = "unresolved-principal"
        # The actual original SQLAlchemy autoflush boundary must deny this too.
        await db.flush()
    monkeypatch.setattr(auth, "_original_session_change", changed_bind)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="session_mutation_changed"):
        await auth.authenticate_token(case.token)
    assert state_digest(sql_state(case)) == state_digest(before)
    assert private_files(case) == files


@pytest.mark.asyncio
async def test_original_token_body_read_follows_exact_header_debit(original_auth_composition, monkeypatch):
    case = original_auth_composition
    observations = []
    original_debit = HeaderReadBudget.debit
    def observe_debit(budget, amount, *, appearance=None):
        result = original_debit(budget, amount, appearance=appearance)
        observations.append(("debit", appearance, amount))
        return result
    monkeypatch.setattr(HeaderReadBudget, "debit", observe_debit)
    engine = case.engine.sync_engine
    def observe_sql(connection, cursor, statement, parameters, context, executemany):
        operation = getattr(getattr(context, "compiled", None), "statement", None)
        descriptions = getattr(operation, "column_descriptions", ())
        if any(item.get("entity") is auth.OperatorSession and item.get("expr") is auth.OperatorSession
                for item in descriptions):
            observations.append(("root-body",))
            assert any(item[:2] == ("debit", ("exact-header", "operator_sessions", "id", (case.operator.session_id,)))
                for item in observations)
    event.listen(engine, "before_cursor_execute", observe_sql)
    try:
        operator = await auth.authenticate_token(case.token, touch=False)
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)
    assert operator.session_id == case.operator.session_id
    assert any(item[0] == "root-body" for item in observations)


@pytest.mark.asyncio
async def test_original_reference_reader_retains_descriptor_row_raw_contract(original_auth_composition):
    case = original_auth_composition
    budget = HeaderReadBudget()
    async with get_session(header_budget=budget) as db:
        await begin_native_writer(db, owner="finite_service", header_budget=budget)
        certificate = await _certify_current_memory_snapshot(db, budget)
        async with snapshot_reads_async(db, certificate):
            descriptor, row, raw = await (await db.connection()).run_sync(
                lambda connection: _programme_reference_row_on_connection(
                    connection, "operator_sessions", case.operator.session_id))
        assert descriptor.table == "operator_sessions"
        assert row["id"] == case.operator.session_id
        assert row["principal_id"] == case.operator.principal.principal_id
        assert type(raw) is bytes and raw
    # This read/caller tuple is not a positive selected-programme acceptance.


@pytest.mark.asyncio
async def test_reservation_cannot_cross_original_task(original_auth_composition, monkeypatch):
    import asyncio
    case = original_auth_composition
    case.clock[0] = utc(root_row(case)["last_seen_at"]) + timedelta(seconds=31)
    original_reserve = CompositionSessionGuard.reserve_session_mutation
    denials = []
    async def observe_reserve(guard, record, changes):
        await original_reserve(guard, record, changes)
        async def foreign_task():
            with pytest.raises(HeaderBoundsError, match="session_reservation_owner_changed"):
                with guard._session_reservation.reads():
                    guard.header_budget.debit(0)
            denials.append(True)
        await asyncio.create_task(foreign_task())
    monkeypatch.setattr(CompositionSessionGuard, "reserve_session_mutation", observe_reserve)
    operator = await auth.authenticate_token(case.token)
    assert operator.session_id == case.operator.session_id and denials == [True]


@pytest.mark.asyncio
async def test_exact_publication_reader_rejects_json_equal_changed_bytes(original_auth_composition):
    from src.workspace.production import (read_accounting_checkpoint, read_lifecycle_receipt,
        _verify_composition_publication_bytes)
    case = original_auth_composition
    checkpoint, receipt = read_accounting_checkpoint(case.workspace), read_lifecycle_receipt(case.workspace)
    path = case.workspace.lifecycle_directory / "receipt.json"
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n")
        assert read_lifecycle_receipt(case.workspace) == receipt
        with pytest.raises(ProductionWorkspaceReconciliationError, match="session_publication_readback_changed"):
            _verify_composition_publication_bytes(case.workspace, checkpoint, receipt,
                header_budget=HeaderReadBudget())
    finally:
        path.write_bytes(original)
