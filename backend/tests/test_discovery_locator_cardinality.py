"""Original SQLite locator differentials; no admission or authority claim."""
import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import event, text

from src.db.models import Goal, WorkBoardTask, WorkBoardEvent
from src.memory import composition_headers as headers
from src.memory.header_bounds import (
    HeaderReadBudget, HeaderBoundsError, COMPOSITION_DESCRIPTORS,
    _trace_memory_numeric_charges,
)
from tests.test_memory_composition_headers import model_db

pytestmark = pytest.mark.asyncio
GOALS = COMPOSITION_DESCRIPTORS["goals"]


@contextmanager
def observed(connection, budget=None, on_delivery=None):
    queries = []
    def before(conn, _cursor, statement, parameters, _context, _many):
        queries.append((statement, parameters, None if budget is None else budget.remaining))
        if on_delivery is not None and statement.startswith("SELECT _rowid_,typeof("):
            on_delivery(conn)
    event.listen(connection.sync_connection, "before_cursor_execute", before)
    try:
        yield queries
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", before)


async def discover(connection, budget, *, selected, remaining=None, descriptor=GOALS, key=None):
    return await connection.run_sync(lambda conn: headers._discover(
        conn, descriptor, budget, key=key, remaining=remaining,
        _header_budget=budget if selected else None))


def locator_queries(queries):
    return [(sql, parameters) for sql, parameters, _ in queries
        if sql.startswith("SELECT _rowid_,typeof(")]


@pytest.mark.parametrize("identities", [(), ("short",),
    ('quote"slash\\control\n\x00', "Zażółć😀"), ("x" * 512,)])
async def test_selected_returns_original_ordered_ids_and_unchanged_delivery_sql(model_db, identities):
    model_db.add_all(Goal(id=value, title="locator metadata fixture") for value in identities)
    await model_db.flush()
    connection = await model_db.connection()
    ordinary, selected = HeaderReadBudget(), HeaderReadBudget()
    with observed(connection) as old_queries:
        original = await discover(connection, ordinary, selected=False)
    with observed(connection, selected) as new_queries, _trace_memory_numeric_charges(selected) as trace:
        result = await discover(connection, selected, selected=True)
    assert result == original == identities
    assert locator_queries(new_queries) == locator_queries(old_queries)
    assert selected.physical_references == ordinary.physical_references
    numeric = [(appearance, amount) for appearance, amount in trace
        if appearance[:2] == ("metadata-prequery", "row-locator-numeric")]
    assert [amount for _, amount in numeric] == [43, 89, 89, 89]
    assert [appearance[-1] for appearance, _ in numeric] == [
        "facts-copy", "delivery", "row-copy", "encoded-copy"]
    delivery = [amount for appearance, amount in trace
        if appearance[:2] == ("metadata-prequery", "row-locator")]
    assert len(delivery) == 3 and len(set(delivery)) == 1
    if not identities:
        assert delivery == [2, 2, 2]
    assert 0 < selected.remaining < 1_048_576


@pytest.mark.parametrize("remaining,count", [(0, 1), (1, 2), (128, 129)])
async def test_count_preserves_overflow_sentinel_before_identity_delivery(model_db, remaining, count):
    model_db.add_all(Goal(id=f"overflow-{i}", title="counted sentinel") for i in range(count))
    await model_db.flush()
    # Include a malformed key in the counted prefix: overflow still wins.
    await model_db.execute(text("UPDATE goals SET id=:bad WHERE id='overflow-0'"), {"bad": b"blob"})
    connection = await model_db.connection()
    with pytest.raises(HeaderBoundsError, match="^header_reference_bound$"):
        await discover(connection, HeaderReadBudget(), selected=False, remaining=remaining)
    budget = HeaderReadBudget()
    with observed(connection, budget) as queries:
        with pytest.raises(HeaderBoundsError, match="^header_reference_bound$"):
            await discover(connection, budget, selected=True, remaining=remaining)
    assert any(sql.startswith("SELECT COUNT(*)") and params == (remaining + 1,)
        for sql, params, _ in queries)
    assert not locator_queries(queries)
    assert not budget.physical_references and budget.remaining < 1_048_576


@pytest.mark.parametrize("bad", [b"blob", "", "x" * 513])
async def test_in_bound_malformed_id_retains_original_denial_after_delivery(model_db, bad):
    model_db.add(Goal(id="original-key", title="malformed locator"))
    await model_db.flush()
    await model_db.execute(text("UPDATE goals SET id=:bad WHERE id='original-key'"), {"bad": bad})
    connection = await model_db.connection()
    for selected in (False, True):
        budget = HeaderReadBudget()
        with observed(connection) as queries:
            with pytest.raises(HeaderBoundsError, match="^header_scalar_unavailable$"):
                await discover(connection, budget, selected=selected)
        assert len(locator_queries(queries)) == 1
        assert not budget.physical_references


async def test_integer_event_identity_matches_original_and_physical_enrollment(model_db):
    goal = Goal(title="integer locator")
    task = WorkBoardTask(owner_principal_id="owner", owner_session_id="session",
        goal_id=goal.id, idempotency_key="integer-locator")
    model_db.add_all([goal, task])
    await model_db.flush()
    row = WorkBoardEvent(task_id=task.task_id, owner_principal_id="owner", owner_session_id="session",
        actor_principal_id="owner", actor_session_id="session", kind="actual")
    model_db.add(row)
    await model_db.flush()
    connection = await model_db.connection()
    descriptor = COMPOSITION_DESCRIPTORS["work_board_events"]
    old, new = HeaderReadBudget(), HeaderReadBudget()
    assert await discover(connection, new, selected=True, descriptor=descriptor) == await discover(
        connection, old, selected=False, descriptor=descriptor) == (row.event_id,)
    assert new.physical_references == old.physical_references == {("work_board_events", row.event_id)}


@pytest.mark.parametrize("drift", ["write", "schema", "cancel"])
async def test_delivery_boundary_drift_or_cancellation_never_enrolls(model_db, drift):
    model_db.add(Goal(id="stable-key", title="before"))
    await model_db.flush()
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    def change(conn):
        if drift == "write":
            conn.exec_driver_sql("UPDATE goals SET title='after' WHERE id='stable-key'")
        elif drift == "schema":
            conn.exec_driver_sql("CREATE TABLE locator_drift(value INTEGER)")
        else:
            raise asyncio.CancelledError()
    error, reason = ((asyncio.CancelledError, None) if drift == "cancel" else
        (HeaderBoundsError, "^header_snapshot_changed$" if drift == "write" else "^header_schema_cookie_changed$"))
    with observed(connection, budget, change) as queries:
        with pytest.raises(error, match=reason):
            await discover(connection, budget, selected=True)
    assert len(locator_queries(queries)) == 1
    assert not budget.physical_references and budget.remaining < 1_048_576


@pytest.mark.parametrize("phase", ["facts-copy", "numeric-delivery", "numeric-row-copy",
    "numeric-encoded-copy", "id-delivery", "id-row-copy", "id-encoded-copy", "final-state"])
async def test_each_new_and_delivery_precharge_denies_before_downstream_sql_without_refund(model_db, phase):
    connection = await model_db.connection()
    probe = HeaderReadBudget()
    with _trace_memory_numeric_charges(probe) as trace:
        assert await discover(connection, probe, selected=True) == ()
    states = 0
    prefix = 0
    for appearance, amount in trace:
        if appearance[:2] == ("metadata-prequery", "state-changes") and appearance[-1] == "delivery":
            states += 1
        is_target = (phase == "facts-copy" and appearance[-1] == "facts-copy"
            or phase.startswith("numeric-") and appearance[:2] == ("metadata-prequery", "row-locator-numeric")
                and appearance[-1] == phase.removeprefix("numeric-")
            or phase.startswith("id-") and appearance[:2] == ("metadata-prequery", "row-locator")
                and appearance[-1] == phase.removeprefix("id-")
            or phase == "final-state" and states == 2 and appearance[:2] == ("metadata-prequery", "state-changes"))
        if is_target:
            break
        prefix += amount
    else:
        pytest.fail("required original payer phase was not observed")
    budget = HeaderReadBudget()
    remaining = prefix + amount - 1
    budget.debit(budget.remaining - remaining)
    with observed(connection, budget) as queries:
        with pytest.raises(HeaderBoundsError, match="^canonical_bound_not_certified$"):
            await discover(connection, budget, selected=True)
    assert budget.remaining == amount - 1
    assert not budget.physical_references
    if phase in {"facts-copy", "numeric-delivery", "numeric-row-copy", "numeric-encoded-copy"}:
        assert not any(sql.startswith("SELECT COUNT(*)") for sql, _, _ in queries)
    if phase != "final-state":
        assert not locator_queries(queries)


async def test_repeat_pays_again_and_keeps_original_duplicate_physical_enrollment(model_db):
    model_db.add(Goal(id="repeated", title="same original physical row"))
    await model_db.flush()
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    before = budget.remaining
    first = await discover(connection, budget, selected=True)
    middle, physical = budget.remaining, set(budget.physical_references)
    second = await discover(connection, budget, selected=True)
    assert first == second == ("repeated",)
    assert before - middle == middle - budget.remaining > 0
    assert len(physical) == 1 and budget.physical_references == physical


async def test_selected_exact_key_does_not_enter_whole_table_numeric_seam(model_db):
    model_db.add(Goal(id="exact", title="original exact locator"))
    await model_db.flush()
    connection = await model_db.connection()
    with observed(connection) as queries:
        assert await discover(connection, HeaderReadBudget(), selected=True, key="exact") == ("exact",)
    assert not any(sql.startswith("SELECT COUNT(*)") for sql, _, _ in queries)


@pytest.mark.parametrize("rowid", [-(2**63), 2**63 - 1])
async def test_signed_rowid_extrema_keep_original_locator_result(model_db, rowid):
    model_db.add(Goal(id="rowid-extreme", title="signed locator metadata"))
    await model_db.flush()
    await model_db.execute(text("UPDATE goals SET rowid=:rowid WHERE id='rowid-extreme'"), {"rowid": rowid})
    connection = await model_db.connection()
    old, new = HeaderReadBudget(), HeaderReadBudget()
    assert await discover(connection, new, selected=True) == await discover(
        connection, old, selected=False) == ("rowid-extreme",)
    assert new.physical_references == old.physical_references == {("goals", rowid)}


async def test_schema_denial_precedes_new_numeric_and_original_identity_queries(model_db):
    await model_db.execute(text("ALTER TABLE goals ADD COLUMN unrelated_extra TEXT"))
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    with observed(connection) as queries:
        with pytest.raises(HeaderBoundsError, match="^header_schema_changed$"):
            await discover(connection, budget, selected=True)
    assert queries and all("pragma_table_xinfo" in sql for sql, _, _ in queries)
    assert not any(sql.startswith("SELECT COUNT(*)") for sql, _, _ in queries)
    assert not locator_queries(queries) and not budget.physical_references


@pytest.mark.parametrize("phase,amount", [("numeric-construction", 887),
    ("delivery-construction", 821), ("state-cookie-input", 516)])
async def test_input_payment_exhaustion_precedes_new_construction_and_sql(model_db, monkeypatch, phase, amount):
    connection = await model_db.connection()
    probe = HeaderReadBudget()
    with _trace_memory_numeric_charges(probe) as trace:
        assert await discover(connection, probe, selected=True) == ()
    prefix = 0
    for appearance, debit in trace:
        if appearance == ("metadata-prequery", "row-locator-input", phase):
            assert debit == amount
            break
        prefix += debit
    else:
        pytest.fail("new input payer was not reached")
    budget = HeaderReadBudget()
    budget.debit(budget.remaining - (prefix + amount - 1))
    reached = []
    def forbidden(*args, **kwargs):
        reached.append(True)
        raise AssertionError("unpaid numeric constructor reached")
    monkeypatch.setattr(headers, "_selected_whole_locator_bound", forbidden)
    with observed(connection, budget) as queries:
        with pytest.raises(HeaderBoundsError, match="^canonical_bound_not_certified$"):
            await discover(connection, budget, selected=True)
    assert budget.remaining == amount - 1
    assert not reached and not budget.physical_references
    assert len(queries) == 2 and all("pragma_table_" in query for query, _, _ in queries)


@pytest.mark.parametrize("rows", [[], [(0,)], [(0, 0, 0)], [(True, 0)],
    [(0, False)], [(1.0, 0)], [(0, 0.0)], [(-1, 0)], [(130, 0)],
    [(1, -1)], [(1, 3185)], [(0, 1)], [(1, 0)], [(1, 1)], [(1, 97)], [(2, 195)]])
async def test_invalid_numeric_delivery_denies_before_identity_or_enrollment(model_db, monkeypatch, rows):
    connection = await model_db.connection()
    original_sql = headers._sql
    delivered = []
    def damaged(conn, query, parameters=()):
        if query.startswith("SELECT COUNT(*)"):
            delivered.append(query)
            return rows
        return original_sql(conn, query, parameters)
    monkeypatch.setattr(headers, "_sql", damaged)
    budget = HeaderReadBudget()
    with observed(connection, budget) as queries:
        with pytest.raises(HeaderBoundsError, match="^header_metadata_bound$"):
            await discover(connection, budget, selected=True)
    assert len(delivered) == 1 and not locator_queries(queries)
    assert not budget.physical_references and budget.remaining < 1_048_576


@pytest.mark.parametrize("boundary", ["numeric-budget", "reference-overflow"])
async def test_actual_overflow_preserves_competing_budget_denial_boundary(model_db, boundary):
    model_db.add_all(Goal(id=f"ordered-{i}", title="overflow boundary") for i in range(129))
    await model_db.flush()
    connection = await model_db.connection()
    probe = HeaderReadBudget()
    with _trace_memory_numeric_charges(probe) as trace:
        with pytest.raises(HeaderBoundsError, match="^header_reference_bound$"):
            await discover(connection, probe, selected=True)
    paid = sum(amount for _, amount in trace)
    assert trace[-1][0] == ("metadata-prequery", "row-locator-numeric", "encoded-copy")
    assert paid < 410866 * 3
    budget = HeaderReadBudget()
    budget.debit(budget.remaining - (paid - int(boundary == "numeric-budget")))
    reason = "canonical_bound_not_certified" if boundary == "numeric-budget" else "header_reference_bound"
    with observed(connection, budget) as queries:
        with pytest.raises(HeaderBoundsError, match=f"^{reason}$"):
            await discover(connection, budget, selected=True)
    assert not locator_queries(queries) and not budget.physical_references
    assert any(query.startswith("SELECT COUNT(*)") for query, _, _ in queries) == (boundary == "reference-overflow")
    assert budget.remaining == (88 if boundary == "numeric-budget" else 0)


async def test_default_none_never_enters_selected_input_or_numeric_path(model_db, monkeypatch):
    model_db.add(Goal(id="default-original", title="unchanged default"))
    await model_db.flush()
    connection = await model_db.connection()
    def forbidden(*args, **kwargs):
        raise AssertionError("default entered selected construction")
    monkeypatch.setattr(headers, "_prepay_selected_whole_locator_inputs", forbidden)
    monkeypatch.setattr(headers, "_selected_whole_locator_bound", forbidden)
    budget = HeaderReadBudget()
    with observed(connection, budget) as queries:
        assert await discover(connection, budget, selected=False) == ("default-original",)
    assert len(locator_queries(queries)) == 1 and not any(query.startswith("SELECT COUNT(*)") for query, _, _ in queries)
    assert budget.physical_references and budget.remaining < 1_048_576


async def test_distinct_actual_budget_denies_before_schema_or_input_payment(model_db):
    connection = await model_db.connection()
    actual, foreign = HeaderReadBudget(), HeaderReadBudget()
    with observed(connection, actual) as queries:
        with pytest.raises(HeaderBoundsError, match="^header_metadata_owner_unavailable$"):
            await connection.run_sync(lambda conn: headers._discover(
                conn, GOALS, actual, _header_budget=foreign))
    assert queries == [] and actual.remaining == foreign.remaining == 1_048_576
    assert not actual.physical_references and not foreign.physical_references
