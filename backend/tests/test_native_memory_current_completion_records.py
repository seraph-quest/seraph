"""Original codec counts only; these fixtures issue no Memory Source."""
from collections import Counter
import json

import pytest

from src.memory.header_bounds import (
    HeaderBoundsError, HeaderReadBudget, WRS_BY_RUN, _trace_memory_numeric_charges,
)
from src.workspace.accounting_witness import (
    MEMORY_ORIGINAL_CHECKPOINT, MEMORY_REFERENCE_PROFILE,
    _native_memory_current_lifecycle_numeric_entries,
    _native_memory_prepare_current_row,
)
from src.workflows.job_runtime import _canonical, _digest


def test_completion_splice_decodes_old_current_in_each_actual_build():
    refs = [{"table": "audit_events", "key": "audit"},
        {"table": "workflow_run_states", "key": "job"}]
    original_payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "invocation_ref": "job", "claim_ref": "claim", "candidate_digest": "a" * 64,
        "composition_binding_digest": "b" * 64, "method": "memory.forget",
        "owner_principal_id": "operator", "operator_session_id": "session",
        "original_deadline": "2026-10-09T00:00:00+00:00", "source_binding_digest": "c" * 64,
        "original_effect_digest": "d" * 64,
        "original_audit": {"ref": refs[0], "tuple_digest": "e" * 64},
        "refs": refs, "rows_digest": "f" * 64, "encoded_bytes": 100}
    original = {"checkpoint_id": MEMORY_ORIGINAL_CHECKPOINT, "safe": True,
        "state_digest": _digest(original_payload), "payload": original_payload}
    rows = [{"ref": ref, "tuple_digest": "e" * 64, "encoded_bytes": 1} for ref in refs]
    payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "original_checkpoint_digest": _digest(original), "projection_revision": 1,
        "state": "validated", "reason_code": None, "refs": refs, "absences": [],
        "rows": rows, "rows_digest": _digest(rows), "encoded_bytes": 2,
        "owner_operation_kind": "memory.forget", "owner_events": [],
        "selected_delta_digest": "f" * 64}
    row = {name: None for name in WRS_BY_RUN.columns}
    row.update(run_identity="job", checkpoint_receipts_json=_canonical([original]))
    budget = HeaderReadBudget()
    captured, old_current = _native_memory_prepare_current_row(row, payload, header_budget=budget)
    prior = captured["checkpoint_receipts_json"]
    completion = dict(old_current["payload"], projection_revision=2, owner_events=[])
    with _trace_memory_numeric_charges(budget) as trace:
        final, current = _native_memory_prepare_current_row(captured, completion, header_budget=budget)
    counts = Counter(appearance for appearance, _upper in trace)
    builds = counts[("memory-current-allocation", "job", "count-update")]
    assert 2 <= builds <= 33
    assert counts[("memory-current-splice", "record-decode")] == 2 * builds
    assert captured["checkpoint_receipts_json"] == prior
    decoded = json.loads(final["checkpoint_receipts_json"])
    assert decoded[0] == original
    assert decoded[1] == current


def test_lifecycle_numeric_factory_rejects_unissued_source_before_widths():
    class Poison:
        def __getattr__(self, name):
            raise AssertionError("unissued Source accessed " + name)
    poison = Poison()
    with pytest.raises(HeaderBoundsError, match="memory_current_numeric_owner_unavailable"):
        _native_memory_current_lifecycle_numeric_entries(poison, poison,
            certificate=poison, retention_widths=poison)
