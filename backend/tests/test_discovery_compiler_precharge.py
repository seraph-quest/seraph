"""Original compiler preservation and fail-before-effect prepayment checks."""
from contextlib import contextmanager
import json
import sys

import pytest
from sqlalchemy import event, Integer
from sqlalchemy.sql.type_api import TypeEngine

from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, COMPOSITION_DESCRIPTORS
from tests.test_memory_composition_headers import model_db
from tests.test_auth_session_composition_privacy import original_auth_composition, sql_state, private_files
from tests.test_programme_prospective_live_selection import (
    genuine_live_selection, private_outputs, SelectionObserved, goal_programme_service,
    goal_discovery_service, cordis_host,
)
from src.db.engine import get_session
from src.runtime_plugins.ownership import begin_native_writer
from src.memory.header_bounds import _trace_memory_numeric_charges

pytestmark = pytest.mark.asyncio


@contextmanager
def original_effects(connection, *, on_compile=None):
    sql, compiles = [], []
    original_profile = sys.getprofile()
    original_compile = TypeEngine.compile.__code__
    def before(_conn, _cursor, statement, _parameters, _context, _many):
        sql.append(statement)
    def profile(frame, action, _arg):
        if action == "call" and frame.f_code is original_compile:
            compiles.append(frame.f_locals["self"])
            if on_compile is not None:
                on_compile()
    event.listen(connection.sync_connection, "before_cursor_execute", before)
    sys.setprofile(profile)
    try:
        yield sql, compiles
    finally:
        sys.setprofile(original_profile)
        event.remove(connection.sync_connection, "before_cursor_execute", before)


@pytest.mark.parametrize("remaining", [0, 403])
async def test_compiler_exhaustion_before_xinfo_or_original_compile(model_db, remaining):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    budget.debit(budget.remaining - remaining)
    with original_effects(connection) as (queries, calls):
        with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
                conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    assert queries == [] and calls == [] and budget.remaining == remaining


@pytest.mark.parametrize("mutation", ["variant", "instance_compile", "unknown_subclass", "impl_alias", "collation"])
async def test_original_live_type_drift_denies_before_query_compile(model_db, monkeypatch, mutation):
    connection = await model_db.connection()
    descriptor = COMPOSITION_DESCRIPTORS["goals"]
    column = headers._compiler_models.Goal.__table__.columns["id"]
    dtype = column.type
    if mutation == "variant":
        monkeypatch.setitem(dtype.__dict__, "_variant_mapping", {"sqlite": Integer()})
    elif mutation == "instance_compile":
        monkeypatch.setitem(dtype.__dict__, "compile", lambda **_kw: "VARCHAR")
    elif mutation == "unknown_subclass":
        class Unknown(Integer):
            pass
        monkeypatch.setattr(column, "type", Unknown())
    elif mutation == "impl_alias":
        monkeypatch.setitem(dtype.__dict__, "impl_instance", Integer())
    else:
        monkeypatch.setitem(dtype.__dict__["impl"].__dict__, "collation", "NOCASE")
    budget = HeaderReadBudget()
    with original_effects(connection) as (queries, calls):
        with pytest.raises(HeaderBoundsError, match="header_schema_changed"):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(conn, descriptor, _header_budget=budget))
    assert queries == [] and calls == [] and budget.remaining == 1_048_576


async def test_dialect_callable_drift_denies_before_original_factory(model_db, monkeypatch):
    connection = await model_db.connection()
    called = []
    def changed():
        called.append(True)
        raise AssertionError("changed dialect must never run")
    monkeypatch.setattr(headers, "dialect", changed)
    budget = HeaderReadBudget()
    with original_effects(connection) as (queries, compiles):
        with pytest.raises(HeaderBoundsError, match="header_schema_changed"):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(
                conn, COMPOSITION_DESCRIPTORS["goals"], _header_budget=budget))
    assert called == [] and queries == [] and compiles == []


async def test_original_compile_paid_before_xinfo_and_each_actual_occurrence(model_db):
    connection = await model_db.connection()
    descriptor = COMPOSITION_DESCRIPTORS["goals"]
    budget = HeaderReadBudget()
    trace = []
    original_debit = budget.debit
    def debit(amount, *, appearance=None):
        trace.append((appearance, amount))
        return original_debit(amount, appearance=appearance)
    budget.debit = debit
    with original_effects(connection) as (queries, compiles):
        for _ in range(2):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(conn, descriptor, _header_budget=budget))
    compiler_debits = [entry for entry in trace if entry[0] == ("metadata-prequery", "descriptor-compiler", "goals")]
    assert compiler_debits == [(("metadata-prequery", "descriptor-compiler", "goals"), 404)] * 2
    assert trace[0] == compiler_debits[0]
    assert len(compiles) == 2 * len(descriptor.columns) and queries


async def test_default_none_keeps_original_compiler_and_no_prepayment(model_db, monkeypatch):
    connection = await model_db.connection()
    descriptor = COMPOSITION_DESCRIPTORS["goals"]
    def forbidden(*_args, **_kwargs):
        raise AssertionError("default path must not invoke selected pricing")
    monkeypatch.setattr(headers, "_check_original_compiler_bindings", forbidden)
    with original_effects(connection) as (queries, compiles):
        result = await connection.run_sync(lambda conn: headers.validate_descriptor_schema(conn, descriptor))
    assert result > 0 and queries and len(compiles) == len(descriptor.columns)


async def test_original_autostring_cold_impl_not_touched_by_price(model_db, monkeypatch):
    dtype = headers._compiler_models.Goal.__table__.columns["id"].type
    monkeypatch.delitem(dtype.__dict__, "impl_instance", raising=False)
    assert "impl_instance" not in dtype.__dict__
    descriptor = COMPOSITION_DESCRIPTORS["goals"]
    spelling = descriptor.sql_types[descriptor.columns.index("id")]
    assert headers._compiler_type_cost(dtype, spelling, "goals", "id") > 0
    assert "impl_instance" not in dtype.__dict__
    connection = await model_db.connection()
    await connection.run_sync(lambda conn: headers.validate_descriptor_schema(conn, descriptor, _header_budget=HeaderReadBudget()))
    assert dtype.__dict__["impl_instance"] is dtype.__dict__["impl"]


@pytest.mark.parametrize("field,value", [("values_callable", lambda _values: []), ("_omit_aliases", False),
    ("native_enum", False), ("create_constraint", True), ("validate_strings", True),
    ("length", True), ("enums", ("triage",)), ("collation", "BINARY")])
async def test_enum_current_constructor_state_denies_before_effect(model_db, monkeypatch, field, value):
    connection = await model_db.connection()
    dtype = headers._compiler_models.WorkBoardTask.__table__.columns["status"].type
    monkeypatch.setitem(dtype.__dict__, field, value)
    with original_effects(connection) as (queries, compiles):
        with pytest.raises(HeaderBoundsError, match="header_schema_changed"):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(conn,
                COMPOSITION_DESCRIPTORS["work_board_tasks"], _header_budget=HeaderReadBudget()))
    assert queries == [] and compiles == []


async def test_genuine_identity_factory_and_validator_paid_separately(original_auth_composition):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    class ObservedRollback(RuntimeError):
        pass
    with pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            common = await connection.run_sync(lambda conn: headers._certify_current_memory_snapshot_on_connection(
                conn, budget, _header_budget=budget))
            selection = await connection.run_sync(lambda conn: guard._select_programmes(conn, common))
            assert selection.identity_ids == () and selection.owner is guard
            with original_effects(connection) as (queries, compiles), _trace_memory_numeric_charges(budget) as trace:
                identity = await connection.run_sync(lambda conn: headers.preflight_programme_identity_component(
                    common, original_selection=selection))
            paid = [amount for appearance, amount in trace
                if appearance == ("metadata-prequery", "descriptor-compiler", "operator_identities")]
            assert paid == [58, 58] and len(compiles) == 6 and queries
            assert identity.common_certificate is common and identity.rows == {}
            raise ObservedRollback
    assert sql_state(case) == before and private_files(case) == files


async def test_locator_schema_width_cannot_escape_original_issuer(original_auth_composition):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    budget = HeaderReadBudget()
    class ObservedRollback(RuntimeError):
        pass
    with pytest.raises(ObservedRollback):
        async with get_session(header_budget=budget) as db:
            widths = []
            prior_profile = sys.getprofile()
            def observe_width(frame, action, result):
                if action == "return" and frame.f_code is headers._original_locator_schema_width.__code__:
                    if type(result) is int:
                        widths.append(result)
                if prior_profile is not None:
                    prior_profile(frame, action, result)
            sys.setprofile(observe_width)
            try:
                await begin_native_writer(db, owner="finite_service", header_budget=budget)
            finally:
                sys.setprofile(prior_profile)
            assert 130 in widths and all(0 < width <= 130 for width in widths)
            connection = await db.connection()
            spent = budget.remaining
            with original_effects(connection) as (queries, compiles):
                result = await connection.run_sync(lambda conn: headers._original_locator_schema_width(
                    conn, "operator_sessions", headers._compiler_models.OperatorSession, budget))
            assert result is None and budget.remaining == spent
            assert queries == [] and compiles == []
            raise ObservedRollback
    assert sql_state(case) == before and private_files(case) == files


async def test_locator_schema_width_denies_copied_original_scope(original_auth_composition):
    from dataclasses import replace
    budget = HeaderReadBudget()
    async with get_session(header_budget=budget) as db:
        connection = await db.connection()
        original = headers._checked_current_snapshot_scope(budget)
        copied = replace(original)
        token = headers._CURRENT_SNAPSHOT_SCOPE.set(copied)
        try:
            spent = budget.remaining
            with original_effects(connection) as (queries, compiles):
                with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
                    await connection.run_sync(lambda conn: headers._original_locator_schema_width(
                        conn, "operator_sessions", headers._compiler_models.OperatorSession, budget))
            assert budget.remaining == spent and queries == [] and compiles == []
        finally:
            headers._CURRENT_SNAPSHOT_SCOPE.reset(token)
        await db.rollback()


@pytest.mark.parametrize("table,column", [(table, column) for table, column in headers._COMPILER_ENUM_ROSTER])
async def test_all_fifteen_original_enums_admitted_and_wrong_effective_length_denied(model_db, monkeypatch, table, column):
    connection = await model_db.connection()
    descriptor = COMPOSITION_DESCRIPTORS[table]
    await connection.run_sync(lambda conn: headers.validate_descriptor_schema(conn, descriptor, _header_budget=HeaderReadBudget()))
    dtype = headers._COMPOSITION_MODELS[table].__table__.columns[column].type
    monkeypatch.setitem(dtype.__dict__, "length", dtype.__dict__["length"] + 1)
    with original_effects(connection) as (queries, compiles):
        with pytest.raises(HeaderBoundsError, match="header_schema_changed"):
            await connection.run_sync(lambda conn: headers.validate_descriptor_schema(conn, descriptor, _header_budget=HeaderReadBudget()))
    assert queries == [] and compiles == []


@pytest.mark.parametrize("scenario", ["positive"])
async def test_genuine_nonempty_identity_compiler_occurrences(genuine_live_selection, monkeypatch, scenario):
    # Existing fixture enrolls/accepts through original issuers, starts the
    # original live child, and denies every external provider transport.
    case = genuine_live_selection
    before, outputs = sql_state(case), private_outputs(case)
    observations = []
    async def boundary(**requested):
        assert requested["goal_id"] == case.goal_id
        budget = HeaderReadBudget()
        async with get_session(header_budget=budget) as db:
            factory_remaining = budget.remaining
            with _trace_memory_numeric_charges(budget) as prefix_trace:
                guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
                writer_remaining = budget.remaining
                connection = await db.connection()
                common = await connection.run_sync(lambda current:
                    headers._certify_current_memory_snapshot_on_connection(current, budget, _header_budget=budget))
            spent = budget.remaining
            compile_payment_order = []
            with _trace_memory_numeric_charges(budget) as trace, original_effects(connection,
                    on_compile=lambda: compile_payment_order.append(tuple(amount for appearance, amount in trace
                        if appearance == ("metadata-prequery", "descriptor-compiler", "operator_identities")))) as (queries, compiles):
                xinfo_order = []
                def before_query(_conn, _cursor, statement, parameters, _context, _many):
                    if "pragma_table_xinfo" in statement and "operator_identities" in parameters:
                        paid = [amount for appearance, amount in trace if appearance ==
                            ("metadata-prequery", "descriptor-compiler", "operator_identities")]
                        xinfo_order.append((tuple(paid), len(compiles)))
                event.listen(connection.sync_connection, "before_cursor_execute", before_query)
                try:
                    programme, binding, selected_common, identity = await connection.run_sync(lambda current:
                        guard._select_programme_admission(current, common, service=goal_discovery_service,
                            host=cordis_host, goal_id=case.goal_id, programme_id=case.programme["id"],
                            grant_revision=case.programme["grant_revision"]))
                except HeaderBoundsError:
                    # Original debit records the attempted charge before denial.
                    # Export numerical facts only; retain the original exception.
                    print("PROGRAMME_PREFIX_NUMERIC " + json.dumps({
                        "initial_capacity": 1048576,
                        "factory_remaining": factory_remaining,
                        "writer_remaining": writer_remaining,
                        "before_selection_remaining": spent,
                        "failure_remaining": budget.remaining,
                        "prefix_amounts": [amount for _appearance, amount in prefix_trace],
                        "selection_amounts": [amount for _appearance, amount in trace],
                        "prefix_attempted_total": sum(amount for _appearance, amount in prefix_trace),
                        "selection_attempted_total": sum(amount for _appearance, amount in trace),
                        "selection_spent": spent - budget.remaining,
                        "denied_amount": trace[-1][1] if trace else None,
                    }, sort_keys=True, separators=(",", ":")))
                    raise
                finally:
                    event.remove(connection.sync_connection, "before_cursor_execute", before_query)
                selection = identity.original_selection
                assert selection.identity_ids == (case.operator.operator_identity_id,)
                assert binding.owner_identity_id == selection.identity_ids[0]
                assert selection.owner is guard and selected_common is common
                assert identity.common_certificate is common and identity.budget is budget
                assert set(identity.rows) == {("operator_identities", binding.owner_identity_id)}
                paid = [amount for appearance, amount in trace if appearance ==
                    ("metadata-prequery", "descriptor-compiler", "operator_identities")]
                assert paid == [58, 58, 58] and len(compiles) == 9
                assert compile_payment_order == [(58,)] * 3 + [(58, 58)] * 3 + [(58, 58, 58)] * 3
                # Factory3 already ran at first xinfo; each validator's next3
                # calls are paid before its original schema query.
                assert xinfo_order == [((58, 58), 3), ((58, 58, 58), 6)]
                descriptor_trace = [amount for appearance, amount in trace if
                    appearance[0] == "metadata-prequery" and appearance[1] in
                    {"descriptor-compiler", "descriptor-xinfo", "descriptor-table"}]
                assert descriptor_trace == [58, 58, 137, 47, 58, 137, 47]
                await connection.run_sync(lambda current: headers._validate(current, identity))
                row = await connection.run_sync(lambda current:
                    headers.read_programme_identity(identity, binding.owner_identity_id))
                assert row[0] == binding.owner_identity_id and row[2] is None
                assert queries and budget.remaining == spent - sum(amount for _, amount in trace)
                observations.append((budget, budget.remaining))
            # The real original owner rolls back without admission publication.
            raise SelectionObserved("identity compiler observed; no authority returned")
    monkeypatch.setattr(goal_programme_service, "assert_authority", boundary)
    with pytest.raises(SelectionObserved, match="^identity compiler observed; no authority returned$"):
        await goal_discovery_service.admit(goal_id=case.goal_id, programme_id=case.programme["id"],
            grant_revision=case.programme["grant_revision"])
    assert len(observations) == 1 and observations[0][0].remaining == observations[0][1]
    assert sql_state(case) == before and private_outputs(case) == outputs
    assert goal_discovery_service._tasks == set() and case.contacts == []
