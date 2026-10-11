"""Selected census construction mechanisms; no admission or certificate grant."""
import pytest
from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from tests.test_memory_composition_headers import model_db

pytestmark = pytest.mark.asyncio


async def test_paid_flat_constructor_matches_original_full_bytes(model_db, monkeypatch):
    connection = await model_db.connection()
    original = headers._selected_schema_census_sql()
    assert len(original) == 74731 and original.isascii()
    observed = []
    actual = headers._selected_schema_census_flat_sql
    budget = HeaderReadBudget()
    before = budget.remaining
    def build():
        observed.append(budget.remaining)
        return actual()
    monkeypatch.setattr(headers, "_selected_schema_census_flat_sql", build)
    queries = []
    real_sql = headers._sql
    def sql(conn, query, params=()):
        queries.append(query)
        return real_sql(conn, query, params)
    monkeypatch.setattr(headers, "_sql", sql)
    await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert queries == [original]
    assert len(observed) == 1
    # Independent source ledger, not the candidate's own price as an oracle.
    assert observed[0] == before - 357891 - 114243


@pytest.mark.parametrize("reserve", ["input", "delivery"])
async def test_insufficient_budget_never_constructs_or_queries(model_db, monkeypatch, reserve):
    connection = await model_db.connection()
    budget = HeaderReadBudget()
    # Input and one result appearance are independently source-derived literals.
    upper = 357891
    remaining = upper - 1 if reserve == "input" else upper + 38081 - 1
    budget.debit(budget.remaining - remaining)
    reached = []
    def forbidden(*_args, **_kwargs):
        reached.append(True)
        raise AssertionError("Unpaid construction/query")
    monkeypatch.setattr(headers, "_selected_schema_census_flat_sql", forbidden)
    monkeypatch.setattr(headers, "_sql", forbidden)
    with pytest.raises(HeaderBoundsError):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert not reached
    assert budget.remaining <= remaining


@pytest.mark.parametrize("leaf", ["apostrophe'", 'quote"', "slash\\", "nul\x00", "非ascii"])
async def test_dynamic_fts_table_leaf_denies_before_debit_or_build(model_db, monkeypatch, leaf):
    connection = await model_db.connection()
    values = dict(headers._FTS_SQL)
    name = next(name for name, spec in values.items() if spec[0] == "trigger")
    old = values[name]
    values[name] = (old[0], leaf, old[2])
    monkeypatch.setattr(headers, "_FTS_SQL", values)
    budget = HeaderReadBudget()
    before = budget.remaining
    def forbidden():
        raise AssertionError("Unpaid invalid-source construction")
    monkeypatch.setattr(headers, "_selected_schema_census_flat_sql", forbidden)
    with pytest.raises(HeaderBoundsError, match="^header_schema_source_unavailable$"):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert budget.remaining == before


@pytest.mark.parametrize("row", [(True, 1, 0), (2**63, 1, 0), (-(2**63)-1, 1, 0),
    (1, True, 0), (1, 2, 0), (1, 1, True), (1, 1, -1), (1, 1, 16), (1, 1)])
async def test_closed_integer_delivery_rejects_wrong_types_and_domains(model_db, monkeypatch, row):
    connection = await model_db.connection()
    # Mechanism negative only: deliberately corrupt driver delivery, not positive authority.
    monkeypatch.setattr(headers, "_sql", lambda *_args: [row])
    with pytest.raises(HeaderBoundsError, match="^header_schema_object_unavailable$"):
        await _negative_delivery(connection, row)


async def _negative_delivery(connection, row):
    budget = HeaderReadBudget()
    return await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))


@pytest.mark.parametrize("count,expected", [(1359, None), (1360, "header_schema_object_bound"), (1361, "header_schema_object_bound")])
async def test_complete_count_and_sentinel_not_filtered(model_db, monkeypatch, count, expected):
    connection = await model_db.connection()
    monkeypatch.setattr(headers, "_sql", lambda *_args: [(index, 1, 0) for index in range(count)])
    budget = HeaderReadBudget()
    operation = lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget)
    if expected:
        with pytest.raises(HeaderBoundsError, match="^" + expected + "$"):
            await connection.run_sync(operation)
    else:
        assert await connection.run_sync(operation) == set()


async def test_foreign_active_original_frame_denies_before_build(model_db, monkeypatch):
    connection = await model_db.connection()
    def check(conn):
        original_budget = HeaderReadBudget()
        certificate = headers.preflight_composition_superset(conn, original_budget)
        selected_budget = HeaderReadBudget()
        before = selected_budget.remaining
        def forbidden(*_args, **_kwargs):
            raise AssertionError("Foreign frame constructed SQL")
        with headers.snapshot_reads(certificate):
            with monkeypatch.context() as patch:
                for name in ("_selected_schema_census_input_upper", "_census_source_leaves",
                        "_selected_schema_census_parts", "_census_decimal_width",
                        "_selected_schema_census_flat_sql", "_sql"):
                    patch.setattr(headers, name, forbidden)
                with pytest.raises(HeaderBoundsError, match="^header_metadata_owner_unavailable$"):
                    headers._projected_schema_objects(conn, selected_budget, _header_budget=selected_budget)
        assert selected_budget.remaining == before
    await connection.run_sync(check)


async def test_missing_transaction_denies_before_any_source_walk(model_db, monkeypatch):
    connection = await model_db.connection()
    await connection.rollback()
    budget = HeaderReadBudget()
    before = budget.remaining
    reached = []
    def forbidden(*_args, **_kwargs):
        reached.append(True)
        raise AssertionError("Invalid transaction reached sizing/construction/query")
    for name in ("_selected_schema_census_input_upper", "_census_source_leaves",
            "_selected_schema_census_parts", "_census_decimal_width",
            "_selected_schema_census_flat_sql", "_sql"):
        monkeypatch.setattr(headers, name, forbidden)
    with pytest.raises(HeaderBoundsError, match="^header_metadata_transaction_required$"):
        await connection.run_sync(lambda conn: headers._projected_schema_objects(conn, budget, _header_budget=budget))
    assert not reached and budget.remaining == before


async def test_independent_source_occurrence_ledger_pins_numeric_upper():
    # Independent R193 AST/closed-source recurrence: L=74731, N=7405.
    # NOT computed by invoking the production generator or its pricing helpers.
    expected = (96947 + 149466 + 6 + 17840 + 48500 + 13380 + 960
        + 16755 + 12150 + 1365 + 15 + 51 + 228 + 228)
    assert expected == 357891
    assert headers._selected_schema_census_input_upper() == expected
