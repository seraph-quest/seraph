"""Real SQLite metadata payer seams; no admission, Source or effect proof."""
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event, text

from src.db.engine import get_session
from src.db.models import OperatorIdentity, OperatorSession
from src.memory import composition_headers as headers
from src.memory.header_bounds import (
    HeaderReadBudget, HeaderBoundsError, COMPOSITION_DESCRIPTORS, _descriptor,
    _trace_memory_numeric_charges,
)
from src.memory.universe import native_memory_universe
from src.runtime_plugins.ownership import begin_native_writer
from tests.test_memory_composition_headers import model_db
from tests.test_memory_universe import inventory_row
from tests.test_auth_session_composition_privacy import (
    original_auth_composition, sql_state, private_files,
)

pytestmark = pytest.mark.asyncio


@contextmanager
def observed(connection, budget=None):
    queries = []
    def before(_conn, _cursor, statement, _parameters, _context, _many):
        queries.append((statement, None if budget is None else budget.remaining))
    event.listen(connection.sync_connection, "before_cursor_execute", before)
    try:
        yield queries
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", before)


async def test_delivery_and_both_copies_paid_before_actual_query(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    before = budget.remaining
    # A supplied finite scalar envelope: three distinct appearances, not one
    # post-read result size. This SELECT touches no private body or authority.
    with observed(connection, budget) as queries, _trace_memory_numeric_charges(budget) as trace:
        rows = await connection.run_sync(lambda conn: headers._metadata_rows(
            conn, "SELECT 7", (), _header_budget=budget, upper=32, appearance=("test-scalar",)))
    assert [tuple(row) for row in rows] == [(7,)]
    assert queries == [("SELECT 7", before - 96)]
    assert [appearance[-1] for appearance, _ in trace] == ["delivery", "row-copy", "encoded-copy"]
    assert [amount for _, amount in trace] == [32, 32, 32]


@pytest.mark.parametrize("remaining", [0, 63])
async def test_exhaustion_denies_query_without_refunding_prior_copies(model_db, remaining):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    budget.debit(budget.remaining - remaining)
    with observed(connection) as queries:
        with pytest.raises(HeaderBoundsError):
            await connection.run_sync(lambda conn: headers._metadata_rows(
                conn, "SELECT 7", (), _header_budget=budget, upper=32, appearance=("test-scalar",)))
    assert queries == []
    assert budget.remaining == (0 if remaining == 0 else 31)


async def test_delivered_bound_failure_keeps_all_prequery_debits(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    before = budget.remaining
    with observed(connection, budget) as queries:
        with pytest.raises(HeaderBoundsError, match="^header_metadata_bound$"):
            await connection.run_sync(lambda conn: headers._metadata_rows(
                conn, "SELECT 'longer-than-eight'", (), _header_budget=budget,
                upper=8, appearance=("test-underbound",)))
    assert len(queries) == 1 and queries[0][1] == before - 24
    assert budget.remaining == before - 24


async def test_state_and_both_schema_cookies_repeat_payment(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    before = budget.remaining
    with observed(connection, budget) as queries:
        def read(conn):
            return (headers._state(conn, _header_budget=budget),
                headers._snapshot_schema_cookies(conn, budget, _header_budget=budget))
        first = await connection.run_sync(read)
        middle = budget.remaining
        second = await connection.run_sync(read)
    assert first == second and before - middle == middle - budget.remaining > 0
    assert [sql for sql, _ in queries] == ["SELECT total_changes()", "PRAGMA main.schema_version",
        "PRAGMA temp.schema_version"] * 2
    assert all(later < earlier for (_, earlier), (_, later) in zip(queries, queries[1:]))


async def test_full_1360_object_envelope_stops_before_schema_delivery(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    with observed(connection) as queries:
        with pytest.raises(HeaderBoundsError):
            await connection.run_sync(lambda conn: headers.preflight_composition_superset(
                conn, budget, _header_budget=budget))
    assert any(sql == "SELECT total_changes()" for sql, _ in queries)
    assert not any("FROM sqlite_schema" in sql for sql, _ in queries)
    assert budget.remaining < 1_048_576
    # This is the retained full-census capacity STOP, not an issued selected
    # common certificate or authorization to shrink the census/schema bound.


@pytest.mark.parametrize("reader", ["discover", "identity_descriptor", "token"])
@pytest.mark.parametrize("damage", ["altered", "extra", "hidden"])
async def test_selected_schema_drift_denies_before_locator_or_body(model_db, reader, damage):
    table = {"discover": "goals", "identity_descriptor": "operator_identities",
        "token": "operator_sessions"}[reader]
    if damage == "altered":
        await model_db.execute(text(f'ALTER TABLE "{table}" RENAME COLUMN created_at TO altered_created_at'))
    elif damage == "extra":
        await model_db.execute(text(f'ALTER TABLE "{table}" ADD COLUMN extra_column TEXT'))
    else:
        await model_db.execute(text(f'ALTER TABLE "{table}" ADD COLUMN hidden_column TEXT GENERATED ALWAYS AS (\'x\') VIRTUAL'))
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    with observed(connection, budget) as queries:
        with pytest.raises(HeaderBoundsError, match="^header_schema_changed$"):
            if reader == "token":
                await headers.locate_operator_token(model_db, "0" * 64, budget, _header_budget=budget)
            else:
                descriptor = (COMPOSITION_DESCRIPTORS["goals"] if reader == "discover" else
                    _descriptor(OperatorIdentity, "id", ("id", "created_at", "revoked_at")))
                await connection.run_sync(lambda conn: headers._discover(
                    conn, descriptor, budget, key="absent-resource-address", _header_budget=budget))
    assert queries and all("pragma_table_xinfo" in sql for sql, _ in queries)
    assert budget.remaining < 1_048_576 and not budget.physical_references
    # Identity uses the original three-column descriptor/validator here.
    # The selected Identity3 issuer cannot lawfully be reached past the above
    # full census STOP; no copied or mutated common certificate is substituted.


@pytest.mark.parametrize("mismatch", ["budget", "connection"])
async def test_original_issued_snapshot_rejects_wrong_metadata_owner(model_db, async_db, mismatch):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    certificate = await headers.certify_composition_superset(model_db, budget)
    if mismatch == "budget":
        target, selected = connection, HeaderReadBudget()
        with observed(target) as queries:
            def read(conn):
                with headers.snapshot_reads(certificate):
                    return headers._metadata_rows(conn, "SELECT 7", (),
                        _header_budget=selected, upper=32, appearance=("wrong-owner",))
            with pytest.raises(HeaderBoundsError, match="^header_metadata_owner_unavailable$"):
                await target.run_sync(read)
        assert not any(sql == "SELECT 7" for sql, _ in queries)
        assert selected.remaining == 1_048_576
    else:
        async with async_db() as other:
            await other.execute(text("BEGIN IMMEDIATE"))
            target = await other.connection()
            # Enter/validate the actual issued certificate on its own driver,
            # then use another original connection in that lexical scope.
            async with headers.snapshot_reads_async(model_db, certificate):
                with observed(target) as queries:
                    with pytest.raises(HeaderBoundsError, match="^header_metadata_owner_unavailable$"):
                        await target.run_sync(lambda conn: headers._metadata_rows(conn, "SELECT 7", (),
                            _header_budget=budget, upper=32, appearance=("wrong-owner",)))
                assert queries == []


async def test_universe_preserves_real_inventory_and_identity_copy_payers(model_db):
    model_db.add_all([inventory_row(2), inventory_row(1)])
    await model_db.flush()
    connection = await model_db.connection()
    with observed(connection) as ordinary:
        legacy = await native_memory_universe(model_db)
    budget = HeaderReadBudget()
    with observed(connection, budget) as selected, _trace_memory_numeric_charges(budget) as trace:
        identities = await native_memory_universe(model_db, _header_budget=budget)
    assert identities == legacy == ("memory-1", "memory-2")
    assert [sql for sql, _ in ordinary] == [sql for sql, _ in selected]
    copy_labels = [appearance[-1] for appearance, amount in trace
        if appearance[:2] == ("metadata-prequery", "memory-universe")
        and appearance[-1] in {"identity-list-copy", "sorted-copy", "tuple-copy"} and amount > 0]
    assert copy_labels == ["identity-list-copy", "sorted-copy", "tuple-copy"]
    assert all("checkpoint_context_json" not in sql for sql, _ in selected)


async def test_universe_zero_budget_denies_even_initial_state_query(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    budget.debit(budget.remaining)
    with observed(connection) as queries:
        with pytest.raises(HeaderBoundsError):
            await native_memory_universe(model_db, _header_budget=budget)
    assert queries == [] and budget.remaining == 0


async def test_default_none_metadata_path_preserves_direct_query_and_no_debit(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    with observed(connection) as queries:
        direct = await connection.run_sync(lambda conn: list(headers._sql(conn, "SELECT 7")))
        default = await connection.run_sync(lambda conn: headers._metadata_rows(conn, "SELECT 7", (),
            _header_budget=None, upper=0, appearance=("unused",)))
    assert [tuple(row) for row in default] == [tuple(row) for row in direct] == [(7,)]
    assert queries == [("SELECT 7", None), ("SELECT 7", None)]
    assert budget.remaining == 1_048_576


@pytest.mark.parametrize("reader", ["discover", "token"])
async def test_default_none_locator_queries_results_and_debits_match(model_db, reader):
    connection = await model_db.connection()
    first, second = HeaderReadBudget(), HeaderReadBudget()
    async def call(budget, explicit):
        kwargs = {"_header_budget": None} if explicit else {}
        if reader == "token":
            return await headers.locate_operator_token(model_db, "0" * 64, budget, **kwargs)
        return await headers.locate_exact_rows(model_db, COMPOSITION_DESCRIPTORS["goals"],
            "absent-resource-address", budget, **kwargs)
    with observed(connection) as ordinary, _trace_memory_numeric_charges(first) as first_trace:
        first_result = await call(first, False)
    with observed(connection) as explicit_none, _trace_memory_numeric_charges(second) as second_trace:
        second_result = await call(second, True)
    assert first_result == second_result == ()
    assert ordinary == explicit_none and first_trace == second_trace
    assert first.remaining == second.remaining < 1_048_576
    assert first.physical_references == second.physical_references == set()


@pytest.mark.parametrize("entry", ["discover", "exact", "token", "tombstone", "superset", "snapshot"])
async def test_distinct_budget_without_current_certificate_denies_before_connection(model_db, monkeypatch, entry):
    connection = await model_db.connection()
    owner, selected = HeaderReadBudget(), HeaderReadBudget()
    assert headers._CURRENT.get() is None
    connection_calls = []
    async def deny_connection(*args, **kwargs):
        connection_calls.append(True)
        raise AssertionError("mismatched metadata budget reached connection access")
    monkeypatch.setattr(model_db, "connection", deny_connection)
    descriptor = COMPOSITION_DESCRIPTORS["goals"]
    with observed(connection) as queries:
        with pytest.raises(HeaderBoundsError, match="^header_metadata_owner_unavailable$"):
            if entry == "discover":
                await headers.discover_rows(model_db, descriptor, owner, _header_budget=selected)
            elif entry == "exact":
                await headers.locate_exact_rows(model_db, descriptor, "absent-resource-address", owner,
                    _header_budget=selected)
            elif entry == "token":
                await headers.locate_operator_token(model_db, "0" * 64, owner, _header_budget=selected)
            elif entry == "tombstone":
                await headers.locate_tombstones(model_db, "absent-resource-address", owner,
                    _header_budget=selected)
            elif entry == "superset":
                await headers.certify_composition_superset(model_db, owner, _header_budget=selected)
            else:
                await headers._certify_current_memory_snapshot(model_db, owner, _header_budget=selected)
    assert queries == [] and connection_calls == []
    assert owner.remaining == selected.remaining == 1_048_576
    assert owner.physical_references == selected.physical_references == set()
    assert not owner.references and not selected.references


async def test_original_auth_reservation_scope_denies_metadata_query(original_auth_composition):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    class ObservedScope(RuntimeError):
        pass
    with pytest.raises(ObservedScope):
        async with get_session() as db:
            budget = HeaderReadBudget()
            guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
            record = await db.get(OperatorSession, case.operator.session_id)
            await guard.reserve_session_mutation(record, {"last_seen_at": record.last_seen_at + timedelta(seconds=1)})
            connection = await db.connection()
            with guard._session_reservation.reads(), observed(connection) as queries:
                remaining = budget.remaining
                with pytest.raises(HeaderBoundsError, match="^header_metadata_owner_unavailable$"):
                    await connection.run_sync(lambda conn: headers._metadata_rows(conn, "SELECT 7", (),
                        _header_budget=budget, upper=32, appearance=("auth-active",)))
                assert queries == [] and budget.remaining == remaining
            raise ObservedScope()
    assert sql_state(case) == before and private_files(case) == files
