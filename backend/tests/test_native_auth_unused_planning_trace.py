"""Original Auth has no planning consumer; Memory's paid trace remains owned."""
from copy import copy

import pytest

from tests.test_input_artifact_native_writer_epochs import original_artifact_owner
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_memory_composition_headers import model_db
from src.db.engine import original_auth_header_budget, _auth_header_budget
from src.memory import composition_headers as headers
from src.memory.header_bounds import (
    HeaderBoundsError, HeaderReadBudget, _MEMORY_LEDGER_PHASE,
    _NativeMemoryNumericLedger, _trace_memory_numeric_charges,
)


@pytest.mark.parametrize("foreign", ["new", "copy"])
def test_foreign_frame_cannot_select_auth_storage_removal(foreign):
    original = HeaderReadBudget()
    selected = HeaderReadBudget() if foreign == "new" else copy(original)
    with original_auth_header_budget(original):
        assert headers._complete_memory_certification_trace_unused(object(), selected) is False
    assert original.remaining == selected.remaining == 1048576


@pytest.mark.parametrize("phase", ["planning", "capture", "completion"])
def test_active_unissued_native_ledger_cannot_select_auth_storage_removal(phase):
    frame = HeaderReadBudget()
    # A real unissued ledger instance grants no phase/capacity permission.
    ledger = _NativeMemoryNumericLedger(frame, object(), object(), object(), {}, object())
    token = _MEMORY_LEDGER_PHASE.set((ledger, phase))
    try:
        with original_auth_header_budget(frame):
            assert headers._complete_memory_certification_trace_unused(object(), frame) is False
    finally:
        _MEMORY_LEDGER_PHASE.reset(token)
    assert frame.remaining == 1048576


@pytest.mark.asyncio
async def test_nested_memory_frame_retains_original_planning_trace(model_db):
    auth_frame, memory_frame = HeaderReadBudget(), HeaderReadBudget()
    connection = await model_db.connection()
    with original_auth_header_budget(auth_frame):
        with headers._memory_current_snapshot_scope(memory_frame):
            with _trace_memory_numeric_charges(memory_frame) as observed:
                certificate = await headers._certify_current_memory_snapshot(
                    model_db, memory_frame, _header_budget=memory_frame)
            retained = await connection.run_sync(lambda _connection:
                headers.native_memory_certification_numeric_trace(
                    certificate, header_budget=memory_frame))
            assert retained and all(entry in observed for entry in retained)
            assert any(label == ("metadata-prequery", "common-certification-trace", "retained-copy")
                for label, _ in observed)
    assert auth_frame.remaining == 1048576
    assert memory_frame.remaining < 1048576


@pytest.mark.asyncio
async def test_original_auth_keeps_both_certificates_without_unused_collector(
        original_artifact_owner, monkeypatch):
    from sqlalchemy import event
    from src.auth.service import authenticate_session
    owner, _request, factory, _operator = original_artifact_owner
    certificates, frames, remaining = [], [], []
    engine = factory.kw["bind"].sync_engine

    def observed_query(connection, cursor, statement, parameters, context, executemany):
        frame = _auth_header_budget.get()
        certificate = headers._CURRENT.get()
        if frame is None:
            return
        frames.append(frame)
        remaining.append(frame.remaining)
        assert headers._COMMON_TRACE_COLLECTOR_ACTIVE.get() is None
        if certificate is not None:
            assert certificate.budget is frame
            assert getattr(certificate, "_common_numeric_trace", None) is None
            if not any(item is certificate for item in certificates):
                certificates.append(certificate)

    event.listen(engine, "before_cursor_execute", observed_query)
    try:
        authenticated = await authenticate_session(owner.session_id, touch=False)
    finally:
        event.remove(engine, "before_cursor_execute", observed_query)
    assert authenticated.session_id == owner.session_id
    assert len(certificates) >= 2
    assert len({id(certificate.transaction) for certificate in certificates}) >= 2
    assert len({id(certificate.budget) for certificate in certificates}) == 1
    assert frames and all(frame is certificates[0].budget for frame in frames)
    assert min(remaining) < max(remaining) < 1048576
    assert all(certificate.budget.remaining < 1048576 for certificate in certificates)
    for certificate in certificates:
        with pytest.raises(HeaderBoundsError, match="memory_common_numeric_trace_unavailable"):
            headers.native_memory_certification_numeric_trace(
                certificate, header_budget=certificate.budget)
