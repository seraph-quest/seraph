"""Real issuer facts remain capacity-only and never replace snapshot checks."""
from dataclasses import replace

import pytest
from sqlalchemy import event, text

from tests.test_memory_composition_headers import model_db
from src.memory.composition_headers import (
    _certify_current_memory_snapshot,
    _memory_current_snapshot_scope,
    native_memory_certification_numeric_trace,
)
from src.memory.header_bounds import (
    COMPOSITION_DESCRIPTORS,
    HeaderBoundsError,
    HeaderReadBudget,
    _trace_memory_numeric_charges,
)


@pytest.mark.asyncio
async def test_cached_common_keeps_full_paid_issuer_trace(model_db):
    frame = HeaderReadBudget()
    connection = await model_db.connection()
    with _memory_current_snapshot_scope(frame):
        with _trace_memory_numeric_charges(frame) as issued_charges:
            certificate = await _certify_current_memory_snapshot(
                model_db, frame, _header_budget=frame)
        trace = await connection.run_sync(lambda _connection:
            native_memory_certification_numeric_trace(certificate, header_budget=frame))
        assert type(trace) is tuple and trace
        compiler_labels = [label for label, _amount in trace
            if label[:2] == ("metadata-prequery", "descriptor-compiler")]
        assert {label[2] for label in compiler_labels} == set(COMPOSITION_DESCRIPTORS)
        assert len(compiler_labels) == len(COMPOSITION_DESCRIPTORS)
        # Every retained original appearance also reached the outer real trace.
        assert all(entry in issued_charges for entry in trace)
        retained = [amount for label, amount in issued_charges
            if label == ("metadata-prequery", "common-certification-trace", "retained-copy")]
        assert len(retained) == 1 and retained[0] > 0
        pair_charges = [(label, amount) for label, amount in issued_charges
            if label[:3] == ("metadata-prequery", "common-certification-trace", "entry-pair")]
        slot_charges = [(label, amount) for label, amount in issued_charges
            if label[:3] == ("metadata-prequery", "common-certification-trace", "entry-list-slot")]
        assert len(pair_charges) == len(slot_charges) == len(trace)
        cursor = 0
        for index, entry in enumerate(trace):
            pair, slot = pair_charges[index], slot_charges[index]
            assert pair[0][3] == slot[0][3] == index and slot[1] == pair[1] + 1
            entry_position = next(position for position in range(cursor, len(issued_charges))
                if issued_charges[position] == entry)
            assert issued_charges.index(pair) < issued_charges.index(slot) < entry_position
            cursor = entry_position + 1
        before = frame.remaining
        with _trace_memory_numeric_charges(frame) as cached_charges:
            cached = await _certify_current_memory_snapshot(
                model_db, frame, _header_budget=frame)
        assert cached is certificate and frame.remaining < before
        assert cached_charges and not any(label[:2] in {
            ("metadata-prequery", "descriptor-compiler"),
            ("metadata-prequery", "common-certification-trace")}
            for label, _amount in cached_charges)
        assert await connection.run_sync(lambda _connection:
            native_memory_certification_numeric_trace(cached, header_budget=frame)) is trace


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_owner", ("copied-certificate", "other-frame", "expired-scope"))
async def test_common_trace_rejects_foreign_owner_before_query_or_debit(model_db, invalid_owner):
    frame = HeaderReadBudget()
    connection = await model_db.connection()
    with _memory_current_snapshot_scope(frame):
        certificate = await _certify_current_memory_snapshot(model_db, frame, _header_budget=frame)
        selected = replace(certificate) if invalid_owner == "copied-certificate" else certificate
        selected_frame = HeaderReadBudget() if invalid_owner == "other-frame" else frame
        if invalid_owner != "expired-scope":
            statements = []
            def observe(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)
            event.listen(connection.sync_connection, "before_cursor_execute", observe)
            before = (frame.remaining, selected_frame.remaining)
            try:
                with pytest.raises(HeaderBoundsError, match="memory_common_numeric_trace_unavailable"):
                    await connection.run_sync(lambda _connection:
                        native_memory_certification_numeric_trace(selected, header_budget=selected_frame))
                assert not statements and (frame.remaining, selected_frame.remaining) == before
            finally:
                event.remove(connection.sync_connection, "before_cursor_execute", observe)
            return
    statements = []
    def observe_expired(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(connection.sync_connection, "before_cursor_execute", observe_expired)
    before = frame.remaining
    try:
        with pytest.raises(HeaderBoundsError, match="memory_common_numeric_trace_unavailable"):
            await connection.run_sync(lambda _connection:
                native_memory_certification_numeric_trace(certificate, header_budget=frame))
        assert not statements and frame.remaining == before
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", observe_expired)


@pytest.mark.asyncio
async def test_issuer_trace_does_not_hide_real_snapshot_mutation(model_db):
    frame = HeaderReadBudget()
    connection = await model_db.connection()
    with _memory_current_snapshot_scope(frame):
        certificate = await _certify_current_memory_snapshot(model_db, frame, _header_budget=frame)
        await model_db.execute(text("CREATE TABLE common_trace_changed (id INTEGER PRIMARY KEY)"))
        with pytest.raises(HeaderBoundsError, match="header_certificate_stale"):
            await connection.run_sync(lambda _connection:
                native_memory_certification_numeric_trace(certificate, header_budget=frame))


@pytest.mark.asyncio
async def test_legacy_certificate_has_no_invented_mapped_issuer_trace(model_db):
    frame = HeaderReadBudget()
    connection = await model_db.connection()
    with _memory_current_snapshot_scope(frame):
        certificate = await _certify_current_memory_snapshot(model_db, frame)
        before = frame.remaining
        with pytest.raises(HeaderBoundsError, match="memory_common_numeric_trace_unavailable"):
            await connection.run_sync(lambda _connection:
                native_memory_certification_numeric_trace(certificate, header_budget=frame))
        assert frame.remaining == before


@pytest.mark.asyncio
async def test_missing_collector_capacity_denies_before_its_first_query(model_db):
    frame = HeaderReadBudget()
    frame.debit(frame.remaining)
    connection = await model_db.connection()
    statements = []
    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(connection.sync_connection, "before_cursor_execute", observe)
    try:
        with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
            await _certify_current_memory_snapshot(model_db, frame, _header_budget=frame)
        assert not statements and frame.remaining == 0
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", observe)
