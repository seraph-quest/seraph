"""Existing whole-schema proof is reusable only inside its genuine operation."""
import copy
import asyncio
import sys

import pytest

from src.db.engine import get_session
from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.memory.header_bounds import _trace_memory_numeric_charges
from src.runtime_plugins.ownership import begin_native_writer
from src.workspace.accounting_witness import RETAINED_FIELDS, validate_retained_table_schema
from tests.test_auth_session_composition_privacy import original_auth_composition, sql_state, private_files
from tests.test_discovery_compiler_precharge import original_effects
from tests.test_programme_raw_rollback_owner import raw_paths
from src.workspace.accounting_continuity import _RawRollbackPair


class ObservedRollback(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_common_header_uses_paid_unique_key_proof_only_in_original_issuer(original_auth_composition, monkeypatch):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    original = headers._metadata_rows
    observed = []
    def delivery(connection, query, parameters, **kwargs):
        if kwargs.get("appearance") != ("row-header",):
            return original(connection, query, parameters, **kwargs)
        caller = sys._getframe(1)
        descriptor = caller.f_locals["descriptor"]
        assert caller.f_code is headers._headers.__code__
        rows = original(connection, query, parameters, **kwargs)
        assert len(rows) == 1 and query.endswith("LIMIT 2")
        observed.append((descriptor, kwargs["upper"]))
        return rows
    monkeypatch.setattr(headers, "_metadata_rows", delivery)
    with pytest.raises(ObservedRollback):
        with _trace_memory_numeric_charges(budget) as trace:
            async with get_session(header_budget=budget) as db:
                await begin_native_writer(db, owner="finite_service", header_budget=budget)
                connection = await db.connection()
                common = await connection.run_sync(lambda conn:
                    headers._certify_current_memory_snapshot_on_connection(conn, budget, _header_budget=budget))
                protected = [(d, upper) for d, upper in observed if d.table in RETAINED_FIELDS]
                assert protected
                for descriptor, upper in protected:
                    assert upper == headers._metadata_upper(1,
                        (20, *[width for kind in descriptor.kinds for width in
                            (9, 20, {"text": 4, "integer": 20, "real": 64}[kind])]))
                payments = [amount for appearance, amount in trace
                    if appearance in {("metadata-prequery", "row-header", stage)
                        for stage in ("delivery", "row-copy", "encoded-copy")}]
                assert payments == [upper for _, upper in observed for _ in range(3)]
                descriptor = headers.COMPOSITION_DESCRIPTORS[protected[0][0].table]
                keys = tuple(key for table, key in common.rows if table == descriptor.table)
                observed.clear()
                await connection.run_sync(lambda conn:
                    headers._headers(conn, descriptor, keys, _header_budget=budget))
                assert observed and all(upper == headers._metadata_upper(2,
                    (20, *[width for kind in descriptor.kinds for width in
                            (9, 20, {"text": 4, "integer": 20, "real": 64}[kind])])) for _, upper in observed)
                raise ObservedRollback
    assert sql_state(case) == before and private_files(case) == files


@pytest.mark.asyncio
async def test_common_header_proof_rejects_foreign_scope_without_debit_or_query(original_auth_composition):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    with pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as db:
            await begin_native_writer(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            scope = headers._checked_current_snapshot_scope(budget)
            descriptor = headers.COMPOSITION_DESCRIPTORS["workflow_run_states"]
            remaining = budget.remaining
            with original_effects(connection) as (queries, _):
                token = headers._CURRENT_SNAPSHOT_SCOPE.set(copy.copy(scope))
                try:
                    with pytest.raises(HeaderBoundsError, match="^memory_snapshot_scope_unavailable$"):
                        await connection.run_sync(lambda conn:
                            headers._original_common_header_single_row(conn, descriptor, (), budget))
                finally:
                    headers._CURRENT_SNAPSHOT_SCOPE.reset(token)
                async def child():
                    with pytest.raises(HeaderBoundsError, match="^memory_snapshot_scope_unavailable$"):
                        headers._original_common_header_single_row(connection.sync_connection, descriptor, (), budget)
                await asyncio.create_task(child())
                with pytest.raises(HeaderBoundsError, match="^memory_snapshot_scope_unavailable$"):
                    headers._original_common_header_single_row(connection.sync_connection, descriptor, (), HeaderReadBudget())
                assert not headers._original_common_header_single_row(connection.sync_connection, descriptor, (), budget)
            assert not queries and budget.remaining == remaining
            raise ObservedRollback
    assert sql_state(case) == before and private_files(case) == files


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
                        validate_retained_table_schema(conn, "workflow_run_states")
                        assert not headers._original_retained_schema_proved(conn, "workflow_run_states", ("id",))
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
                pair.source._db, "workflow_run_states", RETAINED_FIELDS["workflow_run_states"])
            assert budget.remaining == remaining and pair.source._trace_count == queries
            validate_retained_table_schema(pair.source._db, "workflow_run_states")
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
                    validate_retained_table_schema(pair.source._db, "workflow_run_states")
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
                def before_schema_change(conn):
                    with headers.snapshot_reads(common):
                        assert headers._original_retained_schema_proved(
                            conn, "workflow_run_states", RETAINED_FIELDS["workflow_run_states"])
                await connection.run_sync(before_schema_change)
                # An unprotected common table changes the same database schema
                # cookie; the actual protected proof must then deny as stale.
                await connection.exec_driver_sql("ALTER TABLE goals ADD COLUMN unreviewed_schema_column TEXT")
            if invalid == "foreign_connection":
                async with case.engine.connect() as foreign:
                    def reject(conn):
                        token = headers._CURRENT.set(common)
                        try:
                            with pytest.raises(HeaderBoundsError):
                                headers._original_retained_schema_proved(conn, "workflow_run_states", RETAINED_FIELDS["workflow_run_states"])
                        finally:
                            headers._CURRENT.reset(token)
                    await foreign.run_sync(reject)
            else:
                def reject(conn):
                    token = headers._CURRENT.set(copy.copy(common) if invalid == "copied" else common)
                    try:
                        expected = "^header_certificate_stale$" if invalid == "changed_schema" else "^header_certificate_unavailable$"
                        with pytest.raises(HeaderBoundsError, match=expected):
                            headers._original_retained_schema_proved(conn, "workflow_run_states", RETAINED_FIELDS["workflow_run_states"])
                    finally:
                        headers._CURRENT.reset(token)
                await connection.run_sync(reject)
            raise ObservedRollback
    assert sql_state(case) == before and private_files(case) == files


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create_index", "drop_index", "create_table", "drop_table", "generic_ddl"])
async def test_original_read_guard_denies_compiled_retained_ddl_from_header_callback(original_auth_composition, kind):
    from sqlalchemy import event, Index
    from sqlalchemy.schema import DDL, CreateIndex, DropIndex, CreateTable, DropTable
    from src.workspace.production import ProductionWorkspaceReconciliationError
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    table = headers._COMPOSITION_MODELS["workflow_run_states"].__table__
    # Actual SQLAlchemy DDL over the original mapped retained table, not a
    # substituted guard/connection/certificate or fabricated permission.
    if kind == "generic_ddl":
        operation = DDL('DROP TABLE "workflow_run_states"')
    elif kind.endswith("index"):
        index = Index("one_row_guard_callback_probe", table.c.run_identity)
        operation = CreateIndex(index) if kind == "create_index" else DropIndex(index)
    else:
        operation = CreateTable(table) if kind == "create_table" else DropTable(table)
    budget = HeaderReadBudget()
    attempted = []
    def before_query(connection, cursor, statement, parameters, context, executemany):
        if not attempted and 'FROM "workflow_run_states"' in statement and 'typeof(' in statement and statement.endswith('LIMIT 2'):
            attempted.append(kind)
            connection.execute(operation)
    event.listen(case.engine.sync_engine, "before_cursor_execute", before_query)
    try:
        with pytest.raises(ProductionWorkspaceReconciliationError, match="^composition_native_writer_required$"):
            async with get_session(header_budget=budget) as db:
                await begin_native_writer(db, owner="finite_service", header_budget=budget)
        assert attempted == [kind]
    finally:
        event.remove(case.engine.sync_engine, "before_cursor_execute", before_query)
        if kind.endswith("index"):
            table.indexes.remove(index)
    assert sql_state(case) == before and private_files(case) == files


@pytest.mark.parametrize("table,column,value", [
    ("goals", "title", b"invalid text storage"),
    ("goals", "sort_order", "invalid integer storage"),
    ("goals", "sort_order", 1.5),
    ("memories", "confidence", "invalid real storage"),
    ("memories", "confidence", b"invalid real storage"),
])
@pytest.mark.parametrize("metered", [False, True])
def test_header_expected_kind_rejects_actual_wrong_storage(raw_paths, table, column, value, metered):
    # Actual mapped schemas and SQLite affinity, not substituted header rows.
    # Fixture corruption occurs before any certificate/authority is installed.
    from sqlalchemy import create_engine
    from src.db.models import Memory
    engine = create_engine("sqlite:///" + str(raw_paths[0]))
    try:
        with engine.begin() as connection:
            connection.execute(Memory.__table__.insert().values(
                id="kind-memory", content="header storage regression"))
            identity = "same-goal" if table == "goals" else "kind-memory"
            connection.exec_driver_sql(
                f'UPDATE "{table}" SET "{column}"=? WHERE "id"=?', (value, identity))
        descriptor = headers.COMPOSITION_DESCRIPTORS[table]
        with engine.connect() as connection:
            with connection.begin():
                # SQLAlchemy's logical transaction alone is not the original
                # metered header owner: establish the actual SQLite BEGIN.
                connection.exec_driver_sql("BEGIN")
                budget = HeaderReadBudget() if metered else None
                with pytest.raises(HeaderBoundsError, match="^header_scalar_type_unsupported$"):
                    headers._headers(connection, descriptor, (identity,), _header_budget=budget)
                if budget is not None:
                    assert budget.remaining < 1024 * 1024
    finally:
        engine.dispose()


def test_header_expected_kinds_have_closed_source_widths():
    # The query bound is derived from the original registered descriptor kinds.
    widths = {"text": 4, "integer": 20, "real": 64}
    assert {kind for descriptor in headers.COMPOSITION_DESCRIPTORS.values()
        for kind in descriptor.kinds} == set(widths)
    assert headers._metadata_size(None) == widths["text"]
    assert headers._metadata_size(-(2**63)) == widths["integer"]
    assert headers._metadata_size(2**63 - 1) == widths["integer"]
    assert headers._metadata_size(0.0) == widths["real"]


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_metered_header_preserves_nonfinite_real_denial(raw_paths, value):
    from sqlalchemy import create_engine
    from src.db.models import Memory
    engine = create_engine("sqlite:///" + str(raw_paths[0]))
    try:
        with engine.begin() as connection:
            connection.execute(Memory.__table__.insert().values(
                id="finite-memory", content="finite header regression", confidence=value))
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("BEGIN")
                with pytest.raises(HeaderBoundsError, match="^header_scalar_unavailable$"):
                    headers._headers(connection, headers.COMPOSITION_DESCRIPTORS["memories"],
                        ("finite-memory",), _header_budget=HeaderReadBudget())
    finally:
        engine.dispose()


def test_metered_header_preserves_missing_row_denial(raw_paths):
    from sqlalchemy import create_engine
    engine = create_engine("sqlite:///" + str(raw_paths[0]))
    try:
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("BEGIN")
                with pytest.raises(HeaderBoundsError, match="^header_row_unavailable$"):
                    headers._headers(connection, headers.COMPOSITION_DESCRIPTORS["goals"],
                        ("absent-original-row",), _header_budget=HeaderReadBudget())
    finally:
        engine.dispose()


def test_metered_header_keeps_valid_typed_triples_and_nullable_values(raw_paths):
    from sqlalchemy import create_engine
    from src.db.models import Memory
    engine = create_engine("sqlite:///" + str(raw_paths[0]))
    try:
        with engine.begin() as connection:
            connection.execute(Memory.__table__.insert().values(
                id="valid-memory", content="valid header", confidence=0.0, summary=None))
            connection.exec_driver_sql('UPDATE "goals" SET "sort_order"=? WHERE "id"=?',
                (-(2**63), "same-goal"))
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("BEGIN")
                for table, identity in (("goals", "same-goal"), ("memories", "valid-memory")):
                    descriptor = headers.COMPOSITION_DESCRIPTORS[table]
                    legacy = headers._headers(connection, descriptor, (identity,))
                    metered = headers._headers(connection, descriptor, (identity,),
                        _header_budget=HeaderReadBudget())
                    assert metered == legacy
    finally:
        engine.dispose()


@pytest.mark.parametrize("marker", ["unexpected_storage", "x" * 1024, 73, b"blob marker"])
def test_metered_header_rejects_unrecognized_type_marker_before_numeric_slot(raw_paths, marker):
    # Exercise the actual SQLite CASE and original storage-kind rejection,
    # rather than fabricating delivered rows or weakening a body permission.
    from sqlalchemy import create_engine
    engine = create_engine("sqlite:///" + str(raw_paths[0]))
    try:
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("BEGIN")
                connection.connection.driver_connection.create_function("typeof", 1, lambda value: marker)
                with pytest.raises(HeaderBoundsError, match="^header_scalar_type_unsupported$"):
                    headers._headers(connection, headers.COMPOSITION_DESCRIPTORS["goals"],
                        ("same-goal",), _header_budget=HeaderReadBudget())
    finally:
        engine.dispose()


def test_header_type_marker_literal_bound_is_exact():
    # Only these five literal strings (or NULL4) are returned by the CASE.
    assert max(headers._metadata_size(kind) for kind in
        ("null", "text", "integer", "real", "blob")) == 9
    assert headers._metadata_size(None) <= 9
