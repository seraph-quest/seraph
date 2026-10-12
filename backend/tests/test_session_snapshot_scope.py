"""Genuine model certificates retain original fees and lexical owner identity."""
import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Goal
from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from tests.test_memory_composition_headers import model_db
from tests.test_auth_session_composition_privacy import original_auth_composition


@pytest.mark.asyncio
async def test_public_fresh_certificate_registers_only_exact_metadata_mode(model_db):
    budget = HeaderReadBudget()
    with headers._memory_current_snapshot_scope(budget):
        first = await headers.certify_composition_superset(model_db, budget, _header_budget=budget)
        spent = budget.remaining
        reused = await headers._certify_current_memory_snapshot(model_db, budget, _header_budget=budget)
        assert reused is first and budget.remaining < spent  # validator/cookies still paid
        with pytest.raises(HeaderBoundsError, match="header_certificate_unavailable"):
            await headers._certify_current_memory_snapshot(model_db, budget)
        second = await headers.certify_composition_superset(model_db, budget, _header_budget=budget)
        assert second is not first and second.metadata_budget is budget
        assert await headers._certify_current_memory_snapshot(model_db, budget, _header_budget=budget) is second


@pytest.mark.asyncio
async def test_fresh_writer_replaces_rolled_back_reader_without_mode_reuse(model_db):
    budget = HeaderReadBudget()
    with headers._memory_current_snapshot_scope(budget):
        reader = await headers._certify_current_memory_snapshot(model_db, budget)
        assert reader.metadata_budget is None
        await model_db.rollback()
        await model_db.execute(text("BEGIN IMMEDIATE"))
        writer = await headers.certify_composition_superset(model_db, budget, _header_budget=budget)
        assert writer is not reader and writer.metadata_budget is budget
        with pytest.raises(HeaderBoundsError, match="header_certificate_stale"):
            await headers.validate_composition_certificate(model_db, reader)
        assert await headers._certify_current_memory_snapshot(model_db, budget, _header_budget=budget) is writer
        assert budget.remaining < 1_048_576


@pytest.mark.asyncio
async def test_scope_rejects_inherited_task_wrong_frame_and_copied_certificate(model_db):
    budget = HeaderReadBudget()
    with headers._memory_current_snapshot_scope(budget):
        cert = await headers.certify_composition_superset(model_db, budget)
        with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
            await asyncio.create_task(headers._certify_current_memory_snapshot(model_db, budget))
        with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
            await headers._certify_current_memory_snapshot(model_db, HeaderReadBudget())
        with pytest.raises(HeaderBoundsError, match="header_certificate_unavailable"):
            await headers.validate_composition_certificate(model_db, replace(cert))
        connection = await model_db.connection()
        with pytest.raises(HeaderBoundsError, match="header_certificate_unavailable"):
            async with AsyncSession(model_db.bind) as other:
                await other.execute(text("BEGIN"))
                await headers.validate_composition_certificate(other, cert)
        assert cert.connection is connection.sync_connection


@pytest.mark.asyncio
async def test_registered_certificate_write_and_schema_changes_cannot_reuse(model_db):
    budget = HeaderReadBudget()
    with headers._memory_current_snapshot_scope(budget):
        original = await headers.certify_composition_superset(model_db, budget)
        model_db.add(Goal(title="real write invalidates registered snapshot"))
        await model_db.flush()
        changed = await headers._certify_current_memory_snapshot(model_db, budget)
        assert changed is not original
        await model_db.execute(text("CREATE VIEW unreviewed_scope_view AS SELECT id FROM goals"))
        with pytest.raises(HeaderBoundsError):
            await headers._certify_current_memory_snapshot(model_db, budget)


@pytest.mark.asyncio
async def test_closed_and_copied_scope_cannot_supply_cached_authority(model_db):
    budget = HeaderReadBudget()
    with headers._memory_current_snapshot_scope(budget) as scope:
        cert = await headers.certify_composition_superset(model_db, budget)
        copied = replace(scope)
        token = headers._CURRENT_SNAPSHOT_SCOPE.set(copied)
        try:
            with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
                await headers._certify_current_memory_snapshot(model_db, budget)
        finally:
            headers._CURRENT_SNAPSHOT_SCOPE.reset(token)
    assert not scope.live and not scope.certificates
    token = headers._CURRENT_SNAPSHOT_SCOPE.set(scope)
    try:
        with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
            await headers._certify_current_memory_snapshot(model_db, budget)
    finally:
        headers._CURRENT_SNAPSHOT_SCOPE.reset(token)
    with headers._memory_current_snapshot_scope(budget):
        assert await headers._certify_current_memory_snapshot(model_db, budget) is not cert


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited", [False, True])
async def test_factory_scope_denies_nested_session_before_sql_or_debit(original_auth_composition, inherited):
    from src.db.engine import get_session, original_auth_header_budget
    from sqlalchemy import event
    budget = HeaderReadBudget()
    class ObservedRollback(RuntimeError):
        pass
    with original_auth_header_budget(budget), pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as original:
            scope = headers._CURRENT_SNAPSHOT_SCOPE.get()
            assert scope.session is original
            spent = budget.remaining
            statements = []
            def observe(_conn, _cursor, statement, _params, _context, _many):
                statements.append(statement)
            event.listen(original_auth_composition.engine.sync_engine, "before_cursor_execute", observe)
            try:
                with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
                    async with get_session(**({} if inherited else {"header_budget": budget})):
                        pytest.fail("different factory session reached yield")
                assert not statements and budget.remaining == spent
                assert headers._CURRENT_SNAPSHOT_SCOPE.get() is scope
            finally:
                event.remove(original_auth_composition.engine.sync_engine, "before_cursor_execute", observe)
            raise ObservedRollback
    assert not scope.live and scope.session is None and scope.connection is None


@pytest.mark.asyncio
@pytest.mark.parametrize("same_connection", [False, True])
@pytest.mark.parametrize("public", [False, True])
async def test_factory_scope_denies_fresh_certification_on_second_connection(original_auth_composition, same_connection, public):
    from src.db.engine import get_session
    from sqlalchemy import event
    budget = HeaderReadBudget()
    class ObservedRollback(RuntimeError):
        pass
    with pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as original:
            bind = await original.connection() if same_connection else original_auth_composition.engine
            async with AsyncSession(bind) as other:
                spent = budget.remaining
                statements = []
                def observe(_conn, _cursor, statement, _params, _context, _many):
                    statements.append(statement)
                event.listen(original_auth_composition.engine.sync_engine, "before_cursor_execute", observe)
                try:
                    with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
                        certifier = headers.certify_composition_superset if public else headers._certify_current_memory_snapshot
                        await certifier(other, budget, _header_budget=budget)
                    assert not statements and budget.remaining == spent
                    assert not other.in_transaction()
                finally:
                    event.remove(original_auth_composition.engine.sync_engine, "before_cursor_execute", observe)
            assert headers._CURRENT_SNAPSHOT_SCOPE.get().session is original
            raise ObservedRollback


@pytest.mark.asyncio
async def test_original_factory_rollback_new_begin_keeps_connection_and_spent_frame(original_auth_composition):
    from src.db.engine import get_session
    from src.runtime_plugins.ownership import begin_native_writer
    budget = HeaderReadBudget()
    class ObservedRollback(RuntimeError):
        pass
    with pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as db:
            scope = headers._CURRENT_SNAPSHOT_SCOPE.get()
            connection = await db.connection()
            reader, _ = scope.certificates[connection.sync_connection]
            assert reader.metadata_budget is None
            spent_reader = budget.remaining
            await db.rollback()
            await begin_native_writer(db, owner="finite_service", header_budget=budget)
            assert await db.connection() is connection
            writer, _ = scope.certificates[connection.sync_connection]
            assert writer is not reader and writer.metadata_budget is budget
            assert writer.connection is reader.connection and writer.budget is reader.budget is budget
            assert budget.remaining < spent_reader < 1_048_576
            with pytest.raises(HeaderBoundsError, match="header_certificate_stale"):
                await headers.validate_composition_certificate(db, reader)
            spent_writer = budget.remaining
            assert await headers._certify_current_memory_snapshot(db, budget, _header_budget=budget) is writer
            assert budget.remaining < spent_writer
            assert scope.session is db and scope.connection is connection.sync_connection
            raise ObservedRollback
    assert not scope.live and not scope.certificates
