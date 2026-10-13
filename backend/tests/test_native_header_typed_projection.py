"""Original native header metadata: typed bounds, unchanged fail-closed SQL."""
import pytest
from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, MAX_BYTES

@pytest.fixture
def raw_paths(tmp_path):
    # These direct header tests require mapped Goal/Memory SQLite storage only.
    # Keep the real mapped schema and isolated per-case database; no certificate
    # or workspace owner is synthesized by this fixture.
    from sqlalchemy import create_engine
    from src.db.models import Goal, Memory, MemoryEntity
    path = tmp_path / "source.sqlite3"
    engine = create_engine("sqlite:///" + str(path))
    try:
        for model in (Goal, MemoryEntity, Memory):
            model.__table__.create(engine)
        with engine.begin() as connection:
            connection.execute(Goal.__table__.insert().values(id="same-goal", title="raw owner"))
    finally:
        engine.dispose()
    return (path,)

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



def test_metered_typed_header_exhaustion_denies_before_row_query(raw_paths, monkeypatch):
    from sqlalchemy import create_engine
    original = headers._sql
    delivered = []
    def observe(connection, query, parameters=()):
        if query.startswith("SELECT _rowid_,"):
            delivered.append(True)
        return original(connection, query, parameters)
    monkeypatch.setattr(headers, "_sql", observe)
    engine = create_engine("sqlite:///" + str(raw_paths[0]))
    try:
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql("BEGIN")
                budget = HeaderReadBudget()
                budget.debit(MAX_BYTES, appearance=("test-original-frame-exhaustion",))
                with pytest.raises(HeaderBoundsError, match="^canonical_bound_not_certified$"):
                    headers._headers(connection, headers.COMPOSITION_DESCRIPTORS["goals"],
                        ("same-goal",), _header_budget=budget)
                assert delivered == [] and budget.remaining == 0
    finally:
        engine.dispose()
