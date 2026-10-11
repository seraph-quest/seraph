"""Real SQLite metadata payer seams; no admission, Source or effect proof."""
from contextlib import contextmanager
from datetime import timedelta
import json

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
    state_before = await connection.run_sync(headers._state)
    # Retain the legacy 1,360-object/four-column conservative envelope as
    # evidence, without assigning it to the selected aggregate's scalar result.
    legacy_upper = headers._metadata_upper(1360, (20, 44, 770, 770))
    assert legacy_upper == 2_189_601 and legacy_upper > 1_048_576
    construction = headers._selected_schema_census_aggregate_input_upper()
    scalar_copy = headers._metadata_upper(1, (4, 1, 5))
    assert scalar_copy == 16
    # The real model transaction/owner is retained. Pre-exhaust the original
    # frame using its ordinary debit API; do not raise/refill its capacity.
    for remaining in (construction - 1, construction + scalar_copy * 2 - 1):
        budget = HeaderReadBudget()
        assert 0 <= remaining < budget.remaining == 1_048_576
        budget.debit(budget.remaining - remaining)
        with observed(connection) as queries, _trace_memory_numeric_charges(budget) as trace:
            with pytest.raises(HeaderBoundsError, match="^canonical_bound_not_certified$"):
                await connection.run_sync(lambda conn: headers._selected_census_summary(conn, budget))
        assert queries == []  # No sqlite_schema aggregate or other SQL delivery.
        if remaining == construction - 1:
            assert trace == [(("metadata-prequery", "complete-schema-flat-construction"), construction)]
            assert budget.remaining == remaining
        else:
            assert trace == [
                (("metadata-prequery", "complete-schema-flat-construction"), construction),
                (("metadata-prequery", "complete-schema", "delivery"), scalar_copy),
                (("metadata-prequery", "complete-schema", "row-copy"), scalar_copy),
            ]
            assert budget.remaining == scalar_copy - 1  # No refund of successful prior debits.
        assert not budget.references and not budget.physical_references and not budget.future_references
        assert await connection.run_sync(headers._state) == state_before
    # This proves selected source/intermediate prepayment denial, not a full
    # composition certificate, private-body read, admission or execution proof.
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


async def test_descriptor_single_delivery_repeats_without_unused_json_copy(model_db, monkeypatch):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    before = budget.remaining
    def unused_copy(*args):
        raise AssertionError("protected descriptor constructed unused JSON cost")
    monkeypatch.setattr(headers, "_metadata_cost", unused_copy)
    with observed(connection, budget) as queries, _trace_memory_numeric_charges(budget) as trace:
        for _ in range(2):
            assert await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
                conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget)) == 0
    # Independent goals envelope: 23 rows, name26/type10/int2/2/1,
    # row punctuation6 + outer punctuation24 =>1105; table-list upper47.
    # Goal has14 VARCHAR,4 DATETIME,3 INTEGER,1 BOOLEAN outputs:
    # original compile/uppercase debit =14*18+4*20+3*18+18=404.
    assert [amount for _, amount in trace] == [404, 1105, 47, 404, 1105, 47]
    compiler = [(appearance, amount) for appearance, amount in trace
        if appearance[1] == "descriptor-compiler"]
    assert compiler == [(("metadata-prequery", "descriptor-compiler", "goals"), 404)] * 2
    delivery = [(appearance, amount) for appearance, amount in trace if appearance[-1] == "delivery"]
    assert [amount for _, amount in delivery] == [1105, 47, 1105, 47]
    assert sum(amount for _, amount in delivery) == 2304
    assert [appearance[-1] for appearance, _ in delivery] == ["delivery"] * 4
    assert budget.remaining == before - 2304 - 808
    assert [remaining for _, remaining in queries] == [before-404-1105, before-404-1152,
        before-808-2257, before-808-2304]


@pytest.mark.parametrize("remaining,expected_queries,expected_remaining", [(403, 0, 403), (404 + 1104, 0, 1104), (404 + 1105, 1, 0)])
async def test_descriptor_reservation_failure_before_sql_without_refund(
        model_db, remaining, expected_queries, expected_remaining):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    budget.debit(budget.remaining - remaining)
    with observed(connection) as queries:
        with pytest.raises(HeaderBoundsError):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
                conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    assert len(queries) == expected_queries and budget.remaining == expected_remaining


async def test_descriptor_late_row_collection_failure_keeps_delivery_debit(model_db, monkeypatch):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    before = budget.remaining
    class CollectionFailure(RuntimeError):
        pass
    class BrokenRows:
        def __iter__(self):
            raise CollectionFailure
    calls = []
    def broken_sql(*args):
        calls.append(args[1])
        return BrokenRows()
    monkeypatch.setattr(headers, "_sql", broken_sql)
    with pytest.raises(CollectionFailure):
        await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
            conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    assert calls == [headers._DESCRIPTOR_XINFO_SQL] and budget.remaining == before - 404 - 1105


async def test_descriptor_bound_failure_retains_one_debit(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    before = budget.remaining
    with observed(connection, budget) as queries:
        with pytest.raises(HeaderBoundsError, match="^header_metadata_bound$"):
            await connection.run_sync(lambda conn: headers._descriptor_metadata_rows(
                conn, "SELECT 'longer-than-eight'", (), _header_budget=budget,
                upper=8, appearance=("descriptor-test",)))
    assert len(queries) == 1 and queries[0][1] == before - 8
    assert budget.remaining == before - 8


async def test_descriptor_none_keeps_original_query_and_json_cost(model_db, monkeypatch):
    connection = await model_db.connection()
    cost = headers._metadata_cost
    copied = []
    def recorded(rows):
        copied.append(rows)
        return cost(rows)
    monkeypatch.setattr(headers, "_metadata_cost", recorded)
    with observed(connection) as queries:
        result = await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
            conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=None))
    assert len(copied) == 1 and result == cost(copied[0]) > 0
    assert queries[0][0] == ('SELECT CASE WHEN octet_length(name)<=128 THEN name END,'
        'CASE WHEN octet_length(type)<=128 THEN type END,"notnull",pk,hidden '
        'FROM pragma_table_xinfo(?) LIMIT ?')


@pytest.mark.parametrize("value,expected", [(0,1), (1,1), (-1,2), (9,1), (10,2),
    (99,2), (100,3), (-(2**63),20), (2**63-1,19)])
async def test_descriptor_integer_size_signed_decimal_boundaries(value, expected):
    assert headers._descriptor_integer_size(value) == expected
    # Generic accounting must still reserve20 for integers.
    assert headers._metadata_size(value) == 20


@pytest.mark.parametrize("value", [True, 1.0, None, -(2**63)-1, 2**63])
async def test_descriptor_integer_size_rejects_non_domain(value):
    with pytest.raises(HeaderBoundsError, match="^header_metadata_bound$"):
        headers._descriptor_integer_size(value)


async def test_all_common_descriptors_keep_live_model_equality_and_independent_delivery_total(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    with _trace_memory_numeric_charges(budget) as trace:
        for descriptor in COMPOSITION_DESCRIPTORS.values():
            assert await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
                conn, descriptor, _header_budget=budget)) == 0
    xinfo = [amount for appearance, amount in trace if appearance[1] == "descriptor-xinfo"]
    tables = [amount for appearance, amount in trace if appearance[1] == "descriptor-table"]
    # Complete common33 source matrix:33590 xinfo,33*47 table envelopes.
    assert len(xinfo) == len(tables) == 33
    assert sum(xinfo) == 33590 and tables == [47] * 33
    # Conditional locators keep their ORIGINAL accounting, so their debits can
    # appear in trace as well; this is a delivery subtotal, not full-call fit.


@pytest.mark.parametrize("bad", [False, 0.0, None, 1])
async def test_descriptor_table_wr_requires_exact_integer_zero(model_db, monkeypatch, bad):
    connection = await model_db.connection()
    original_delivery = headers._descriptor_metadata_rows
    bounded_deliveries = []
    def delivered(*args, **kwargs):
        rows = original_delivery(*args, **kwargs)
        if kwargs["appearance"] == ("descriptor-table",):
            # Real SQLite wr=0 was queried, prepaid and delivery-validated.
            # Inject a decoder-boundary fault AFTER that check; a floating wr
            # is not claimed to be a genuine pragma_table_list delivery.
            assert len(rows) == 1 and len(rows[0]) == 1
            assert type(rows[0][0]) is int and rows[0][0] == 0
            bounded_deliveries.append(kwargs["_header_budget"].remaining)
            rows[0] = (bad,)
        return rows
    monkeypatch.setattr(headers, "_descriptor_metadata_rows", delivered)
    budget = HeaderReadBudget()
    with observed(connection, budget) as queries, _trace_memory_numeric_charges(budget) as trace:
        with pytest.raises(HeaderBoundsError, match="^header_schema_changed$"):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
                conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    assert [amount for _, amount in trace] == [404, 1105, 47]
    assert len(queries) == 2 and "pragma_table_list" in queries[-1][0]
    assert queries[-1][1] == 1_048_576 - 1556
    assert bounded_deliveries == [1_048_576 - 1556]
    assert budget.remaining == 1_048_576 - 1556


@pytest.mark.parametrize("bad", [0.0, -0.0, 1.0])
async def test_descriptor_table_out_of_family_float_delivery_denies_before_decoder(
        model_db, monkeypatch, bad):
    connection = await model_db.connection()
    original_sql = headers._sql
    calls = []
    def delivered(conn, query, params=()):
        calls.append((query, budget.remaining))
        return [(bad,)] if "pragma_table_list" in query else original_sql(conn, query, params)
    monkeypatch.setattr(headers, "_sql", delivered)
    budget = HeaderReadBudget()
    with _trace_memory_numeric_charges(budget) as trace:
        with pytest.raises(HeaderBoundsError, match="^header_metadata_bound$"):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
                conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    # Independent source ledger: compiler404 + xinfo1105 + integer table47.
    # One float reserves64, two bracket pairs4; compilation is not a SQL call.
    # The paid47 is the original two-row/signed64 integer delivery family.
    assert [amount for _, amount in trace] == [404, 1105, 47]
    assert len(calls) == 2 and "pragma_table_list" in calls[-1][0]
    assert calls[-1][1] == 1_048_576 - 1556
    assert budget.remaining == 1_048_576 - 1556


@pytest.mark.parametrize("bad", [False, None, 1])
async def test_descriptor_table_bounded_noncanonical_delivery_reaches_schema_guard(
        model_db, monkeypatch, bad):
    connection = await model_db.connection()
    original_sql = headers._sql
    calls = []
    def delivered(conn, query, params=()):
        calls.append((query, budget.remaining))
        return [(bad,)] if "pragma_table_list" in query else original_sql(conn, query, params)
    monkeypatch.setattr(headers, "_sql", delivered)
    budget = HeaderReadBudget()
    with _trace_memory_numeric_charges(budget) as trace:
        with pytest.raises(HeaderBoundsError, match="^header_schema_changed$"):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
                conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    assert [amount for _, amount in trace] == [404, 1105, 47]
    assert len(calls) == 2 and "pragma_table_list" in calls[-1][0]
    assert calls[-1][1] == 1_048_576 - 1556
    assert budget.remaining == 1_048_576 - 1556


async def test_descriptor_table_insufficient_remaining_denies_before_table_query(model_db):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    budget.debit(budget.remaining - (404 + 1105 + 46))
    with observed(connection, budget) as queries, _trace_memory_numeric_charges(budget) as trace:
        with pytest.raises(HeaderBoundsError):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
                conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    assert [amount for _, amount in trace] == [404, 1105]
    assert len(queries) == 1 and queries[0][0] == headers._DESCRIPTOR_XINFO_SQL
    assert budget.remaining == 46



# Independent full R141 matrix as pinned/reviewed by R266-R4, not derived from
# the implementation map/helper. The public Identity3 label denotes this exact
# original table; synthetic projections below are not real PRAGMA schema states.
_DESCRIPTOR_CASE_ENVELOPES = [('messages', 18, 20, 8, 837), ('work_board_tasks', 44, 28, 13, 2566), ('work_board_input_artifacts', 21, 23, 8, 1035), ('work_board_attempts', 18, 27, 8, 970), ('work_board_review_intents', 12, 18, 8, 547), ('work_board_links', 7, 18, 8, 337), ('work_board_events', 11, 24, 11, 613), ('work_board_evidence_dependencies', 17, 21, 13, 901), ('work_board_handoffs', 16, 20, 12, 817), ('workflow_run_states', 66, 36, 8, 4021), ('workflow_step_states', 17, 19, 8, 775), ('workflow_artifact_reviews', 15, 19, 8, 689), ('production_workflow_authority_states', 19, 22, 8, 921), ('production_workflow_fault_receipts', 14, 30, 8, 811), ('production_workflow_side_effect_receipts', 16, 29, 8, 902), ('runtime_composition_states', 6, 20, 7, 302), ('sessions', 6, 18, 8, 295), ('audit_events', 10, 12, 8, 397), ('memory_episodes', 15, 17, 11, 705), ('approval_requests', 23, 20, 8, 1057), ('goals', 22, 24, 8, 1105), ('memories', 18, 17, 11, 837), ('memory_edges', 7, 14, 11, 329), ('memory_entities', 7, 14, 11, 329), ('memory_proposals', 60, 30, 12, 3539), ('memory_sources', 7, 17, 8, 329), ('memory_tombstones', 5, 10, 8, 205), ('work_board_decision_receipts', 51, 30, 13, 3069), ('work_board_proposals', 29, 26, 8, 1501), ('inference_cost_reservations', 29, 22, 8, 1381), ('operator_sessions', 12, 25, 8, 638), ('secrets', 8, 18, 8, 379), ('inference_accounting_owners', 9, 21, 8, 451), ('operator_identities', 3, 10, 8, 137)]


@pytest.mark.parametrize("table,n,name_width,type_width,upper", _DESCRIPTOR_CASE_ENVELOPES)
@pytest.mark.parametrize("mode", ["valid", "sentinels", "null-text"])
async def test_every_descriptor_maximum_case_envelope_matches_independent_json_oracle(
        model_db, table, n, name_width, type_width, upper, mode):
    connection = await model_db.connection()
    name, sql_type = "a" * name_width, "A" * type_width
    notnull, pk, hidden = 1, n, 0
    if mode == "sentinels":
        notnull, pk, hidden = 2, n+1, 2
    elif mode == "null-text":
        name, sql_type = name + "!", sql_type + "!"
    # Execute the actual protected CASE on n+1 maximum-width synthetic rows.
    # The PRAGMA replacement only supplies input cells; it neither drops rows
    # nor substitutes a Python implementation of SQLite's CASE predicates.
    query = headers._DESCRIPTOR_XINFO_SQL.replace("FROM pragma_table_xinfo(?)",
        'FROM (WITH RECURSIVE envelope(i) AS (SELECT 1 UNION ALL '
        'SELECT i+1 FROM envelope WHERE i<?) SELECT ? AS name,? AS type,'
        '? AS "notnull",? AS pk,? AS hidden FROM envelope)')
    rows = await connection.run_sync(lambda conn: list(conn.exec_driver_sql(query,
        (name_width, type_width, n, n+1, name, sql_type, notnull, pk, hidden, n+1))))
    assert len(rows) == n+1 and all(len(row) == 5 for row in rows)
    expected = (("a"*name_width, "A"*type_width, -1, -1, 1) if mode == "sentinels" else
        (None, None, 1, n, 0) if mode == "null-text" else
        ("a"*name_width, "A"*type_width, 1, n, 0))
    assert all(tuple(row) == expected for row in rows)
    encoded = json.dumps([list(row) for row in rows], ensure_ascii=True,
        separators=(",", ":")).encode()
    assert headers._descriptor_xinfo_size(rows) == len(encoded) <= upper
    # Exact literal matrix upper includes both rejecting -1 integer cells;
    # the valid-only envelope is tighter, never an excuse to reduce that upper.
    if mode == "sentinels":
        assert len(encoded) == upper
    assert headers._DESCRIPTOR_XINFO_WIDTHS[table] == (n, name_width, type_width)
    # Non-SQL synthetic delivery: signed64 violations must fail measurement.
    # CASE itself projects large signed64 values to -1; an out-of-range Python
    # integer is not claimed to be a SQLite PRAGMA result or bindable integer.
    for invalid in (-(2**63)-1, 2**63):
        with pytest.raises(HeaderBoundsError, match="^header_metadata_bound$"):
            headers._descriptor_xinfo_size([["a", "A", invalid, 0, 0]])


async def test_genuine_budgeted_identity3_preflight_query_wiring(original_auth_composition):
    # Genuine original native guard/certificate/selection; no fake authority,
    # comparator or successful SQL result. Empty programme selection deliberately
    # checks metadata wiring only, not discovery admission or identity body fit.
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    class IdentityWiringObserved(RuntimeError):
        pass
    with pytest.raises(IdentityWiringObserved):
        async with get_session() as db:
            guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            common = await connection.run_sync(lambda conn:
                headers._certify_current_memory_snapshot_on_connection(conn, budget,
                    _header_budget=budget))
            selection = await connection.run_sync(lambda conn: guard._select_programmes(conn, common))
            assert selection.identity_ids == ()
            assert selection.owner is guard and selection.common33 is common
            assert common.budget is common.metadata_budget is guard.header_budget is budget
            spent = budget.remaining
            with observed(connection, budget) as queries, _trace_memory_numeric_charges(budget) as trace:
                identity = await connection.run_sync(lambda conn:
                    headers.preflight_programme_identity_component(common, original_selection=selection))
            assert identity.common_certificate is common and identity.original_selection is selection
            assert identity.budget is identity.metadata_budget is budget and identity.rows == {}
            paid = [(appearance[1], amount) for appearance, amount in trace
                if appearance[1] in {"identity-indexes", "identity-parts", "identity-objects"}]
            assert paid == [("identity-indexes", 1703), ("identity-parts", 4885), ("identity-objects", 2455)]
            assert all(appearance[-1] == "delivery" for appearance, amount in trace
                if appearance[1] in {"identity-indexes", "identity-parts", "identity-objects"})
            statements = [query for query, remaining in queries]
            index_at = next(i for i, query in enumerate(statements) if "FROM pragma_index_list" in query)
            parts_at = next(i for i, query in enumerate(statements) if "FROM pragma_index_xinfo" in query)
            objects_at = next(i for i, query in enumerate(statements)
                if "FROM sqlite_schema WHERE tbl_name=?" in query)
            assert index_at < parts_at < objects_at
            assert budget.remaining < spent and all(remaining < spent for _, remaining in queries)
            assert not any('SELECT "id","created_at","revoked_at"' in query for query in statements)
            raise IdentityWiringObserved
    assert sql_state(case) == before and private_files(case) == files
