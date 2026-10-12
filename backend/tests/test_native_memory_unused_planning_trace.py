"""Planning-storage removal never exempts an unissued or pre-effect owner."""
import pytest

from tests.test_memory_composition_headers import model_db
from src.memory.composition_headers import (
    _complete_memory_certification_trace_unused,
    _certify_current_memory_snapshot,
    _memory_current_snapshot_scope,
    native_memory_certification_numeric_trace,
)
from src.memory.header_bounds import (
    COMPOSITION_DESCRIPTORS, HeaderReadBudget, _MEMORY_LEDGER_PHASE,
    _trace_memory_numeric_charges,
)


@pytest.mark.parametrize("active", [None, (), (object(), "capture"), (object(), "completion")])
def test_unissued_phase_cannot_skip_original_planning_storage(active):
    token = _MEMORY_LEDGER_PHASE.set(active)
    try:
        assert _complete_memory_certification_trace_unused(object(), HeaderReadBudget()) is False
    finally:
        _MEMORY_LEDGER_PHASE.reset(token)


@pytest.mark.asyncio
async def test_pre_effect_issuer_keeps_paid_full_trace_and_outer_observations(model_db):
    frame = HeaderReadBudget()
    connection = await model_db.connection()
    with _memory_current_snapshot_scope(frame):
        with _trace_memory_numeric_charges(frame) as observed:
            certificate = await _certify_current_memory_snapshot(model_db, frame, _header_budget=frame)
        retained = await connection.run_sync(lambda _connection:
            native_memory_certification_numeric_trace(certificate, header_budget=frame))
        assert len([label for label, _ in retained
            if label[:2] == ("metadata-prequery", "descriptor-compiler")]) == len(COMPOSITION_DESCRIPTORS)
        assert all(entry in observed for entry in retained)
        assert sum(label == ("metadata-prequery", "common-certification-trace", "retained-copy")
            for label, _ in observed) == 1
        assert sum(label[:3] == ("metadata-prequery", "common-certification-trace", "entry-pair")
            for label, _ in observed) == len(retained)
