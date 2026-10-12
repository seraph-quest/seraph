"""The original public caller keeps the first retained writer certification."""
import fcntl
import json
import os

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


@pytest.mark.asyncio
async def test_first_writer_retains_one_binding_receipt_under_original_lock(
        original_auth_composition, monkeypatch):
    from src.workspace import accounting_witness as witness
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    original_binding, original_read = witness.assert_deployment_binding, witness.read_lifecycle_receipt
    calls = []
    def binding(workspace, *, header_budget=None):
        assert header_budget is budget
        descriptor = os.open(workspace.lifecycle_directory / "accounting.lock",
            os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        calls.append("binding")
        return original_binding(workspace, header_budget=header_budget)
    def read(workspace, *, header_budget=None):
        assert header_budget is budget
        calls.append("receipt")
        return original_read(workspace, header_budget=header_budget)
    monkeypatch.setattr(witness, "assert_deployment_binding", binding)
    monkeypatch.setattr(witness, "read_lifecycle_receipt", read)
    class ObservedRollback(RuntimeError):
        pass
    with pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
            assert calls == ["binding", "receipt"]
            assert guard.header_budget is budget and budget.remaining > 0
            raise ObservedRollback
    assert sql_state(case) == before and private_files(case) == files


@pytest.mark.asyncio
async def test_first_writer_invalid_binding_releases_lock_before_begin(
        original_auth_composition):
    from src.workspace.accounting_witness import ProductionWorkspaceReconciliationError
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    receipt_path = case.workspace.lifecycle_directory / "receipt.json"
    receipt = json.loads(files["receipt.json"])
    receipt["deployment_binding"]["root_path_digest"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    budget = HeaderReadBudget()
    try:
        async with get_session(header_budget=budget) as db:
            connection = db.bind
            statements = []
            def sql(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)
            event.listen(connection.sync_connection, "before_cursor_execute", sql)
            try:
                with pytest.raises(ProductionWorkspaceReconciliationError,
                        match="accounting_deployment_binding_unavailable"):
                    await begin_native_writer(db, owner="finite_service", header_budget=budget)
                assert "BEGIN IMMEDIATE" not in statements
                assert not db.in_transaction() and not db.new and not db.dirty and not db.deleted
                assert db.info.get("composition_guard") is None
                assert not db.info.get("native_writer_started")
                descriptor = os.open(case.workspace.lifecycle_directory / "accounting.lock",
                    os.O_RDWR | os.O_NOFOLLOW)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
            finally:
                event.remove(connection.sync_connection, "before_cursor_execute", sql)
    finally:
        receipt_path.write_bytes(files["receipt.json"])
    assert sql_state(case) == before and private_files(case) == files
