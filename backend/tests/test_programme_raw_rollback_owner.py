"""Original raw handle mechanics only; no programme rollback readiness claim."""
import os
import sqlite3
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from src.db.models import Goal
from src.memory.composition_headers import _validate, snapshot_reads, charge_row, _one_scalar
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, MAX_BYTES, GOAL
from src.workspace.accounting_continuity import _RawRollbackPair


@pytest.fixture
def raw_paths(tmp_path):
    paths = (tmp_path / "source.sqlite3", tmp_path / "destination.sqlite3")
    for path in paths:
        engine = create_engine("sqlite:///" + str(path))
        try:
            SQLModel.metadata.create_all(engine)
            # Identical actual physical rowids in different databases are the
            # regression trigger. Each remains a distinct frame reference.
            with engine.begin() as connection:
                connection.execute(Goal.__table__.insert().values(id="same-goal", title="raw owner"))
        finally:
            engine.dispose()
    return paths


def test_two_actual_handles_same_rowid_do_not_false_deduplicate(raw_paths):
    budget = HeaderReadBudget()
    pair = _RawRollbackPair(*raw_paths, budget)
    try:
        pair.source.begin()
        pair.destination.begin()
        source = pair.source.certify()
        destination = pair.destination.certify()
        assert source.connection is pair.source._db
        assert destination.connection is pair.destination._db
        assert source.driver is source.connection
        assert source.budget is destination.budget is budget
        assert len(budget.physical_references) == 2
        first = budget.remaining
        pair.source.certify()
        assert len(budget.physical_references) == 2
        assert budget.remaining < first < MAX_BYTES
        with snapshot_reads(source):
            first = budget.remaining
            charge_row(source.connection, "goals", "same-goal")
            second = budget.remaining
            charge_row(source.connection, "goals", "same-goal")
            assert first > second > budget.remaining
    finally:
        pair.close()


def test_actual_fresh_writer_rejects_old_transaction_certificate(raw_paths):
    budget = HeaderReadBudget()
    pair = _RawRollbackPair(*raw_paths, budget)
    try:
        pair.destination.begin()
        old = pair.destination.certify()
        spent = budget.remaining
        pair.destination.rollback()
        pair.destination.begin(immediate=True)
        with pytest.raises(HeaderBoundsError, match="header_certificate_stale"):
            _validate(pair.destination._db, old)
        current = pair.destination.certify()
        assert current.transaction is not old.transaction
        assert current.connection is old.connection
        assert budget.remaining < spent
        pair.destination.commit()
        with pytest.raises(HeaderBoundsError, match="header_transaction_required"):
            _validate(pair.destination._db, current)
    finally:
        pair.close()


@pytest.mark.parametrize("statement", ["BEGIN", "COMMIT", "ROLLBACK", 'SAVEPOINT "foreign"',
    "ATTACH DATABASE ':memory:' AS unowned_database"])
def test_actual_authorizer_denies_unowned_control_and_poisons(raw_paths, statement):
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.destination.begin()
        with pytest.raises(sqlite3.DatabaseError):
            pair.destination._db.execute(statement)
        with pytest.raises(HeaderBoundsError, match="rollback_raw_owner_unavailable"):
            pair.destination.certify()
    finally:
        pair.close()


@pytest.mark.parametrize("callback", ["authorizer", "trace"])
def test_callback_loss_stops_before_body(raw_paths, callback):
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.destination.begin()
        if callback == "authorizer":
            pair.destination._db.set_authorizer(None)
        else:
            pair.destination._db.set_trace_callback(None)
        with pytest.raises(HeaderBoundsError, match="rollback_.*_unavailable"):
            pair.destination.certify()
    finally:
        pair.close()


def test_observed_alias_and_path_replacement_stop(raw_paths, tmp_path):
    alias = tmp_path / "alias.sqlite3"
    os.link(raw_paths[0], alias)
    with pytest.raises(HeaderBoundsError, match="rollback_database_alias"):
        _RawRollbackPair(raw_paths[0], alias, HeaderReadBudget())
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.destination.begin()
        raw_paths[1].rename(tmp_path / "old.sqlite3")
        raw_paths[1].write_bytes(b"replacement")
        with pytest.raises(HeaderBoundsError, match="rollback_database_path_changed"):
            pair.destination.certify()
    finally:
        pair.close()


def test_numeric_frame_is_identical_and_exhaustion_is_not_refunded(raw_paths):
    budget = HeaderReadBudget()
    pair = _RawRollbackPair(*raw_paths, budget)
    try:
        pair.destination.begin()
        cert = pair.destination.certify()
        with pytest.raises(HeaderBoundsError, match="rollback_budget_changed"):
            pair.destination._validate_budget(HeaderReadBudget())
        assert cert.budget is budget and budget.remaining < MAX_BYTES
    finally:
        pair.close()
    budget = HeaderReadBudget()
    pair = _RawRollbackPair(*raw_paths, budget)
    try:
        pair.destination.begin()
        budget.debit(MAX_BYTES)
        with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
            pair.destination.certify()
        assert budget.remaining == 0
    finally:
        pair.close()


def test_raw_scalar_requires_exact_one_row_one_column(raw_paths):
    with sqlite3.connect(raw_paths[0]) as connection:
        assert _one_scalar(connection, "SELECT 1") == 1
        for statement in ("SELECT 1,2", "SELECT 1 WHERE 0", "SELECT 1 UNION ALL SELECT 2"):
            with pytest.raises(HeaderBoundsError, match="header_scalar_unavailable"):
                _one_scalar(connection, statement)


@pytest.mark.parametrize("change", ["unknown-schema", "unrelated-reference-overflow"])
def test_raw_preflight_overflow_precedes_selected_numeric_headers(raw_paths, change):
    with sqlite3.connect(raw_paths[1]) as connection:
        if change == "unknown-schema":
            connection.execute("CREATE VIEW foreign_goal_view AS SELECT title FROM goals")
    if change == "unrelated-reference-overflow":
        engine = create_engine("sqlite:///" + str(raw_paths[1]))
        try:
            with engine.begin() as connection:
                connection.execute(Goal.__table__.insert(), [
                    {"id": "overflow-" + str(i), "title": "unrelated retained goal"}
                    for i in range(128)])
        finally:
            engine.dispose()
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    statements = []
    try:
        pair.destination.begin()
        original_trace = pair.destination._trace
        def observe(statement):
            statements.append(statement)
            original_trace(statement)
        pair.destination._db.set_trace_callback(observe)
        with pytest.raises(HeaderBoundsError, match="header_schema_object_unavailable|header_reference_bound"):
            pair.destination.certify()
        # _headers selects storage/length/numeric metadata, not full row bodies.
        # Detect its actual selected Goal shape, after the full-table locator.
        assert not any(statement.startswith('SELECT _rowid_,typeof("id")')
            and ' FROM "goals" WHERE "id"=' in statement for statement in statements)
    finally:
        pair.close()


@pytest.mark.parametrize("statement", [
    "CREATE TEMP TABLE forbidden_temp(value TEXT)",
    "CREATE TEMP VIEW forbidden_view AS SELECT id FROM goals",
    "CREATE VIRTUAL TABLE forbidden_virtual USING fts5(content)",
    "CREATE VIRTUAL TABLE temp.forbidden_virtual USING fts5(content)",
])
def test_actual_temp_and_virtual_schema_mutations_are_denied(raw_paths, statement):
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.destination.begin()
        before = pair.destination._db.total_changes
        with pytest.raises(sqlite3.DatabaseError):
            pair.destination._db.execute(statement)
        assert pair.destination._db.total_changes == before
        for schema in ("main", "temp"):
            assert not any(row[0].startswith("forbidden") for row in pair.destination._db.execute(
                f"SELECT name FROM {schema}.sqlite_schema").fetchall())
        pair.destination.certify()
    finally:
        pair.close()


def test_actual_full_goal_consumer_runs_after_certificate_and_body_debit(raw_paths):
    budget = HeaderReadBudget()
    pair = _RawRollbackPair(*raw_paths, budget)
    statements = []
    debits = []
    full_query = 'SELECT ' + ','.join('"' + field + '"' for field in GOAL.columns) + ' FROM "goals" WHERE "id"=? LIMIT 2'
    try:
        pair.destination.begin()
        original_trace = pair.destination._trace
        original_debit = budget.debit
        def observe_debit(amount, *, appearance=None):
            original_debit(amount, appearance=appearance)
            debits.append((appearance, amount))
        budget.debit = observe_debit
        def observe(statement):
            statements.append((statement, tuple(debits)))
            original_trace(statement)
        pair.destination._db.set_trace_callback(observe)
        cert = pair.destination.certify()
        assert any(sql.startswith('SELECT _rowid_,typeof("id")')
            and ' FROM "goals" WHERE "id"=' in sql for sql, _ in statements)
        assert not any(sql.startswith(full_query.split(' WHERE ')[0]) for sql, _ in statements)
        with snapshot_reads(cert):
            charge_row(cert.connection, GOAL.table, "same-goal")
            rows = cert.connection.execute(full_query, ("same-goal",)).fetchmany(2)
        assert len(rows) == 1 and tuple(rows[0].keys()) == GOAL.columns
        assert rows[0]["title"] == "raw owner"
        charged_before_sql = next(charges for sql, charges in statements
            if sql.startswith(full_query.split(' WHERE ')[0]))
        body_appearance = ("body", GOAL.table, "same-goal")
        assert [amount for appearance, amount in charged_before_sql
            if appearance == body_appearance] == [cert.rows[(GOAL.table, "same-goal")][1]]
    finally:
        pair.close()


def test_copied_certificate_and_wrong_thread_cannot_read(raw_paths):
    pair = _RawRollbackPair(*raw_paths, HeaderReadBudget())
    try:
        pair.destination.begin()
        cert = pair.destination.certify()
        with pytest.raises(HeaderBoundsError, match="header_certificate_unavailable"):
            _validate(cert.connection, replace(cert))
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(HeaderBoundsError, match="rollback_raw_owner_unavailable"):
                executor.submit(pair.destination.certify).result()
    finally:
        pair.close()


def test_future_reservation_cannot_resolve_under_other_database_namespace(raw_paths):
    budget = HeaderReadBudget()
    pair = _RawRollbackPair(*raw_paths, budget)
    try:
        # Use the actual destination locator, while the source has the same
        # rowid. Neither handle's numeric namespace can resolve the other's debt.
        pair.destination.begin()
        certificate = pair.destination.certify()
        rowid = certificate.rows[(GOAL.table, "same-goal")][0]
        original_physical = set(budget.physical_references)
        budget.reserve_future_row(GOAL, "same-goal", 128, database_identity=pair.destination._namespace)
        spent = budget.remaining
        budget.resolve_future(GOAL, "same-goal", rowid, database_identity=pair.source._namespace)
        assert len(budget.future_references) == 1 and budget.physical_references == original_physical
        budget.resolve_future(GOAL, "same-goal", rowid, database_identity=pair.destination._namespace)
        assert not budget.future_references
        assert budget.physical_references == {(pair.destination._namespace, GOAL.table, rowid)}
        assert budget.remaining == spent
    finally:
        pair.close()
