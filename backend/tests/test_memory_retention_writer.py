"""Real existing writer ownership and bounded retention, with no native grant."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, text
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateIndex

from src.db.models import Memory, MemorySource
from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, MAX_BYTES
from src.memory.retention import read_memory_rows
from src.memory.retention_schema import validate_memory_schema
from src.memory.composition_headers import _metadata_cost
from src.runtime_plugins.ownership import CompositionBindingError, begin_native_writer
from src.workspace.accounting_witness import CompositionSessionGuard
from src.workspace.production import ProductionWorkspaceReconciliationError
from tests.test_inference_accounting import accounting_db


@asynccontextmanager
async def retention_writer(accounting_db):
    # Real private lifecycle/accounting lock and actual BEGIN IMMEDIATE issuer.
    _root, _engine, factory = accounting_db
    budget = HeaderReadBudget()
    async with factory() as db:
        guard = await begin_native_writer(db, owner="durable_jobs", fresh=True, header_budget=budget)
        try:
            yield db, budget
        finally:
            await db.rollback()
            if guard is not None and not guard._retention_closed:
                guard.close()


CONTROLS = (
    ";COMMIT", "/* x */;ROLLBACK", " ; /*x*/ ; BEGIN IMMEDIATE",
    " \ufeffCOMMIT", ";\ufeff;-- ignored\n/* ; */;CoMmIt;",
    " ;END", "/* COMMIT */;SAVEPOINT nested", ";RELEASE nested",
)


@pytest.mark.parametrize("statement", CONTROLS)
@pytest.mark.asyncio
async def test_transaction_controls_are_denied_at_actual_cursor_in_retention_scope(accounting_db, statement):
    async with retention_writer(accounting_db) as (db, budget):
        guard = db.info["composition_guard"]
        with guard._retention_reads(budget):
            with pytest.raises(HeaderBoundsError, match="memory_retention_writer_changed"):
                await db.execute(text(statement))
            # Denial did not execute COMMIT/ROLLBACK or grant a renewed writer.
            assert (await db.connection()).sync_connection.get_transaction().is_active
            assert await db.scalar(text("SELECT 'COMMIT; /* ROLLBACK */'")) == "COMMIT; /* ROLLBACK */"


@pytest.mark.parametrize("statement", (";COMMIT", "/* x */;ROLLBACK", " \ufeff;END;"))
@pytest.mark.asyncio
async def test_raw_sql_transaction_end_and_rebegin_invalidates_original_record(accounting_db, statement):
    async with retention_writer(accounting_db) as (db, budget):
        connection = await db.connection()
        original_outer = connection.sync_connection.get_transaction()
        await db.execute(text(statement))
        await db.execute(text(" ; /*x*/ ; BEGIN IMMEDIATE"))
        assert connection.sync_connection.get_transaction() is original_outer
        with pytest.raises(HeaderBoundsError, match="memory_retention_writer_unavailable"):
            await validate_memory_schema(db, budget)


@pytest.mark.parametrize("end", ("commit", "rollback"))
@pytest.mark.asyncio
async def test_session_transaction_end_and_deferred_begin_cannot_reuse_writer(accounting_db, end):
    async with retention_writer(accounting_db) as (db, budget):
        await getattr(db, end)()
        await db.execute(text("BEGIN"))
        with pytest.raises(HeaderBoundsError, match="memory_retention_writer_unavailable"):
            await validate_memory_schema(db, budget)


@pytest.mark.asyncio
async def test_different_task_budget_and_session_deny_original_scope(accounting_db):
    async with retention_writer(accounting_db) as (db, budget):
        with pytest.raises(HeaderBoundsError):
            await validate_memory_schema(db, HeaderReadBudget())
        with pytest.raises(HeaderBoundsError, match="memory_retention_writer_changed"):
            await asyncio.create_task(validate_memory_schema(db, budget))
        _root, _engine, factory = accounting_db
        async with factory() as other:
            await other.execute(text("BEGIN"))
            with pytest.raises(HeaderBoundsError, match="memory_retention_writer_unavailable"):
                await validate_memory_schema(other, budget)
            await other.rollback()


@pytest.mark.asyncio
async def test_genuine_sqlite_select_prefixes_do_not_invalidate_record(accounting_db):
    async with retention_writer(accounting_db) as (db, budget):
        guard = db.info["composition_guard"]
        with guard._retention_reads(budget):
            for statement in (";SELECT 'COMMIT'", " /*ROLLBACK*/ ;\ufeff;SELECT 'COMMIT'",
                              " ;-- END\n ; SELECT 'COMMIT'"):
                assert await db.scalar(text(statement)) == "COMMIT"
        await validate_memory_schema(db, budget)


@pytest.mark.asyncio
async def test_raw_immediate_without_original_guard_is_not_retention_authority(accounting_db):
    _root, _engine, factory = accounting_db
    async with factory() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        with pytest.raises(HeaderBoundsError, match="memory_retention_writer_unavailable"):
            await validate_memory_schema(db, HeaderReadBudget())
        await db.rollback()


@pytest.mark.asyncio
async def test_closed_original_guard_denies_without_querying_metadata(accounting_db):
    async with retention_writer(accounting_db) as (db, budget):
        db.info["composition_guard"].close()
        with pytest.raises(HeaderBoundsError, match="memory_retention_writer_unavailable"):
            await validate_memory_schema(db, budget)


@pytest.mark.asyncio
async def test_real_nested_savepoint_denies_retention_in_original_writer(accounting_db):
    async with retention_writer(accounting_db) as (db, budget):
        async with db.begin_nested():
            # Force actual SAVEPOINT creation through the real SQLAlchemy owner.
            assert await db.scalar(text("SELECT 1")) == 1
            with pytest.raises(HeaderBoundsError, match="memory_retention_writer_unavailable"):
                await validate_memory_schema(db, budget)
        # RELEASE cannot make the old original snapshot valid again.
        with pytest.raises(HeaderBoundsError, match="memory_retention_writer_unavailable"):
            await validate_memory_schema(db, budget)


@pytest.mark.asyncio
async def test_shared_128_physical_rows_remain_enrolled_across_retained_repeats(accounting_db):
    _root, _engine, factory = accounting_db
    records = [Memory(content="finite") for _ in range(128)]
    async with factory() as db:
        db.add_all(records)
        await db.flush()
        await db.commit()
    async with retention_writer(accounting_db) as (db, budget):
        assert len(budget.physical_references) == 128
        original = set(budget.physical_references)
        before = budget.remaining
        refs = (("memories", records[0].id), ("memories", records[-1].id))
        first = await read_memory_rows(db, refs, header_budget=budget, remaining_bytes=MAX_BYTES)
        second = await read_memory_rows(db, refs, header_budget=budget, remaining_bytes=MAX_BYTES)
        assert first.references == second.references == tuple(sorted(refs))
        assert budget.physical_references == original
        assert budget.remaining < before


@pytest.mark.parametrize("overflow", ("bytes", "references"))
@pytest.mark.asyncio
async def test_original_writer_rejects_committed_whole_frame_overflow_before_private_bodies(accounting_db, overflow):
    _root, engine, factory = accounting_db
    async with factory() as db:
        if overflow == "references":
            db.add_all([Memory(content="finite") for _ in range(129)])
        else:
            memory = Memory(content="visible")
            db.add(memory)
            await db.flush()
            db.add(MemorySource(memory_id=memory.id, source_type="message", snippet="x" * 200_000))
        await db.flush()
        await db.commit()
    statements = []
    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        async with factory() as db:
            code = "header_reference_bound" if overflow == "references" else "canonical_bound_not_certified"
            with pytest.raises(HeaderBoundsError, match=code):
                await begin_native_writer(db, owner="durable_jobs", fresh=True, header_budget=HeaderReadBudget())
            await db.rollback()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert not any(statement.startswith('SELECT "id",') or "SELECT memories." in statement
                   or "SELECT memory_sources." in statement for statement in statements)


@pytest.mark.asyncio
async def test_existing_guard_cannot_be_rebound_to_another_budget(accounting_db):
    async with retention_writer(accounting_db) as (db, budget):
        await db.rollback()
        with pytest.raises(CompositionBindingError, match="composition_native_writer_budget_changed"):
            await begin_native_writer(db, owner="durable_jobs", header_budget=HeaderReadBudget())
        assert db.info["composition_guard"].header_budget is budget
        assert not db.in_transaction()


@pytest.mark.parametrize("statement, expected", (
    (" ; /*COMMIT*/ ; SELECT 'ROLLBACK'", "SELECT"),
    ("-- COMMIT\n\ufeff;SELECT \"COMMIT\"", "SELECT"),
    ("SELECT ';COMMIT'", "SELECT"), ("COMMIT_other", "COMMIT_OTHER"),
    ("/* unclosed", None), (";-- comment", None), ("\"COMMIT\"", None),
))
def test_sql_prefix_preserves_quoted_comment_lookalikes_and_denies_ambiguity(statement, expected):
    assert CompositionSessionGuard._retention_sql_keyword(statement) == expected


@pytest.mark.asyncio
async def test_repeated_schema_metadata_debits_use_one_frame_and_no_new_budget(accounting_db, monkeypatch):
    async with retention_writer(accounting_db) as (db, budget):
        charges = []
        original = budget.debit
        def debit(amount, *, appearance=None):
            charges.append((appearance, amount))
            return original(amount, appearance=appearance)
        monkeypatch.setattr(budget, "debit", debit)
        def denied_init(_self):
            raise AssertionError("retention manufactured a frame")
        monkeypatch.setattr(HeaderReadBudget, "__init__", denied_init)
        before = budget.remaining
        await validate_memory_schema(db, budget)
        first = tuple(charges)
        charges.clear()
        await validate_memory_schema(db, budget)
        assert tuple(charges) == first
        assert before - budget.remaining == 2 * sum(amount for _, amount in first)
        assert {appearance[0] for appearance, _ in first} == {
            "retention-table", "retention-layout", "retention-fk", "retention-index-list",
            "retention-index-columns", "retention-index-ddl-header", "retention-index-ddl"}


@pytest.mark.parametrize("stop", ("metadata", "ddl_body"))
@pytest.mark.asyncio
async def test_metadata_or_ddl_body_exhaustion_prevents_private_bodies(accounting_db, monkeypatch, stop):
    async with retention_writer(accounting_db) as (db, budget):
        statements = []
        connection = (await db.connection()).sync_connection
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection, "before_cursor_execute", observe)
        original = budget.debit
        def debit(amount, *, appearance=None):
            if stop == "ddl_body" and appearance[0] == "retention-index-ddl":
                original(budget.remaining)
            return original(amount, appearance=appearance)
        if stop == "metadata":
            budget.debit(budget.remaining)
        monkeypatch.setattr(budget, "debit", debit)
        try:
            with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                await read_memory_rows(db, (), header_budget=budget, remaining_bytes=MAX_BYTES)
        finally:
            event.remove(connection, "before_cursor_execute", observe)
        assert not any(statement.startswith("SELECT sql ") or statement.startswith('SELECT "id",')
                       for statement in statements)
        if stop == "ddl_body":
            assert any("SELECT typeof(sql),octet_length(sql)" in statement for statement in statements)


@pytest.mark.asyncio
async def test_oversized_real_index_ddl_denies_before_ddl_body(accounting_db):
    _root, _engine, factory = accounting_db
    async with factory() as db:
        index = sorted(Memory.__table__.indexes, key=lambda item: item.name)[0]
        await db.execute(text('DROP INDEX "' + index.name + '"'))
        sql = str(CreateIndex(index).compile(dialect=dialect()))
        await db.execute(text(sql.replace("(", "(/*" + "x" * 9000 + "*/", 1)))
        assert await db.scalar(text("SELECT octet_length(sql) FROM sqlite_master WHERE name=:name"),
                               {"name": index.name}) > 8192
        await db.commit()
    # Original mapped-column/locator preflight permits this named index;
    # the separate retention DDL numeric header owns its length denial.
    async with retention_writer(accounting_db) as (db, budget):
        statements = []
        connection = (await db.connection()).sync_connection
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append((statement, _parameters))
        event.listen(connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(HeaderBoundsError, match="memory_retained_index_changed"):
                await validate_memory_schema(db, budget)
        finally:
            event.remove(connection, "before_cursor_execute", observe)
        # Other canonical named indexes may already have passed their own bodies.
        # The oversized index itself must never get a DDL body SELECT.
        assert any("SELECT typeof(sql),octet_length(sql)" in statement and parameters == (index.name,)
                   for statement, parameters in statements)
        assert not any(statement.startswith("SELECT sql ") and parameters == (index.name,)
                       for statement, parameters in statements)


@pytest.mark.asyncio
async def test_actual_guard_refuses_retained_ddl_before_sql_execution(accounting_db):
    async with retention_writer(accounting_db) as (db, budget):
        statements = []
        connection = (await db.connection()).sync_connection
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_unhooked_bulk_sql"):
                await db.execute(text("DROP INDEX ux_memory_proposals_owner_attempt_preview"))
        finally:
            event.remove(connection, "before_cursor_execute", observe)
        assert not any("DROP INDEX" in statement for statement in statements)


@pytest.mark.parametrize("route, statement", (
    ("orm", 'DROP INDEX "ux_memory_proposals_owner_attempt_preview"'),
    ("cursor", ' ;\ufeff;/* x */ DROP INDEX "ux_memory_proposals_owner_attempt_preview"'),
    ("orm", 'CREATE INDEX "neutral_name" ON "memory_proposals" ("proposal_id")'),
    ("cursor", '/*x*/;CREATE TRIGGER "neutral_name" AFTER INSERT ON "memory_proposals" BEGIN SELECT 1; END'),
    ("orm", 'ALTER TABLE "memories" RENAME COLUMN "content" TO "renamed_content"'),
    ("cursor", ' ; -- ignored\nREINDEX "ux_memory_proposals_owner_attempt_preview"'),
    ("orm", 'VACUUM'), ("cursor", "ATTACH DATABASE ':memory:' AS neutral_name"),
    ("orm", 'DETACH DATABASE neutral_name'),
    ("orm", 'PRAGMA writable_schema=ON'),
    ("cursor", ' ;\ufeff;/*x*/PRAGMA writable_schema(1)'),
    ("orm", 'PRAGMA main.schema_version=123'),
    ("cursor", 'PRAGMA temp.schema_version(123)'),
    ("orm", "PRAGMA encoding='UTF-16'"),
    ("cursor", 'PRAGMA journal_mode=WAL'),
    ("orm", 'PRAGMA encoding; PRAGMA writable_schema=ON'),
    ("cursor", 'PRAGMA table_info("memories")=1'),
    ("orm", 'PRAGMA table_info(memories)'),
    ("cursor", 'PRAGMA table_info("' + 'x' * 129 + '")'),
    ("orm", 'PRAGMA schema_version'),
))
@pytest.mark.asyncio
async def test_original_budgeted_guard_denies_schema_controls_and_preserves_raw_metadata(accounting_db, route, statement):
    async with retention_writer(accounting_db) as (db, budget):
        async def metadata():
            rows = list(await db.execute(text(
                "SELECT type,name,tbl_name,rootpage FROM sqlite_schema ORDER BY rowid LIMIT 1355")))
            budget.debit(_metadata_cost([list(row) for row in rows]),
                         appearance=("test-schema-readback", route, statement))
            return tuple(tuple(row) for row in rows)
        before = await metadata()
        connection = await db.connection()
        cursor_calls = []
        def observe(_conn, _cursor, actual, _parameters, _context, _many):
            cursor_calls.append(actual)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_unhooked_bulk_sql"):
                if route == "orm":
                    await db.execute(text(statement))
                else:
                    await connection.exec_driver_sql(statement)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert not cursor_calls
        assert await metadata() == before


@pytest.mark.parametrize("statement", (
    'PRAGMA encoding', 'PRAGMA main.schema_version', 'PRAGMA temp.schema_version',
    ' ;\ufeff;/* x */pRaGmA encoding;',
    'PRAGMA table_info("memories")', 'PRAGMA foreign_key_list("memory_sources")',
    'PRAGMA index_list("memories")',
    'PRAGMA index_info("ux_memory_proposals_owner_attempt_preview")',
    'PRAGMA index_xinfo("ux_memory_proposals_owner_attempt_preview")',
))
@pytest.mark.asyncio
async def test_original_budgeted_guard_preserves_only_readonly_pragma_grammar(accounting_db, statement):
    async with retention_writer(accounting_db) as (db, budget):
        rows = list(await db.execute(text(statement)))
        budget.debit(_metadata_cost([list(row) for row in rows]),
                     appearance=("test-readonly-pragma", statement))
        assert rows


@pytest.mark.asyncio
async def test_actual_cursor_blocks_compiled_sqlalchemy_ddl(accounting_db):
    async with retention_writer(accounting_db) as (db, budget):
        index = next(iter(Memory.__table__.indexes))
        with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_unhooked_bulk_sql"):
            await db.execute(CreateIndex(index))
