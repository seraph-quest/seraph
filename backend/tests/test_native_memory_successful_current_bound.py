"""Original codec recurrence evidence; no Source or reservation is issued."""
from collections import Counter

import pytest

from src.memory.header_bounds import (
    HeaderBoundsError, HeaderReadBudget, WRS_BY_RUN, _trace_memory_numeric_charges,
)
from src.workspace.accounting_witness import (
    MEMORY_ORIGINAL_CHECKPOINT, MEMORY_REFERENCE_PROFILE,
    _native_memory_prepare_current_row,
    _native_memory_successful_current_lifecycle_numeric_entries,
)
from src.workflows.job_runtime import _canonical, _digest


@pytest.mark.parametrize("foreign_count", [0, 90, 990, 9990, 99000, 900000])
def test_original_current_count_bound_preserves_every_build_parser(foreign_count):
    own_ref = {"table": "workflow_run_states", "key": "job"}
    foreign_ref = {"table": "audit_events", "key": 'escaped-"-é'}
    refs = [foreign_ref, own_ref]
    original_payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "invocation_ref": "job", "claim_ref": "claim", "candidate_digest": "a" * 64,
        "composition_binding_digest": "b" * 64, "method": "memory.forget",
        "owner_principal_id": "operator", "operator_session_id": "session",
        "original_deadline": "2026-10-09T00:00:00+00:00", "source_binding_digest": "c" * 64,
        "original_effect_digest": "d" * 64,
        "original_audit": {"ref": foreign_ref, "tuple_digest": "e" * 64},
        "refs": refs, "rows_digest": "f" * 64, "encoded_bytes": foreign_count}
    original = {"checkpoint_id": MEMORY_ORIGINAL_CHECKPOINT, "safe": True,
        "state_digest": _digest(original_payload), "payload": original_payload}
    rows = [{"ref": foreign_ref, "tuple_digest": "e" * 64, "encoded_bytes": foreign_count},
        {"ref": own_ref, "tuple_digest": "0" * 64, "encoded_bytes": 0}]
    payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "original_checkpoint_digest": _digest(original), "projection_revision": 1,
        "state": "validated", "reason_code": None, "refs": refs, "absences": [],
        "rows": rows, "rows_digest": _digest(rows), "encoded_bytes": foreign_count,
        "owner_operation_kind": "memory.forget", "owner_events": [],
        "selected_delta_digest": "f" * 64}
    raw = {name: None for name in WRS_BY_RUN.columns}
    raw.update(run_identity="job", checkpoint_receipts_json=_canonical([original]))
    budget = HeaderReadBudget()
    with _trace_memory_numeric_charges(budget) as trace:
        captured, current = _native_memory_prepare_current_row(raw, payload, header_budget=budget)
    counts = Counter(appearance for appearance, _upper in trace)
    builds = counts[("memory-current-allocation", "job", "count-update")]
    assert 2 <= builds <= 6
    assert sum(appearance is None for appearance, _upper in trace) == builds + 1
    assert counts[("memory-reference-payload", "current", "payload-field-set")] == 3 * builds + 1
    assert captured["checkpoint_receipts_json"].startswith(_canonical([original])[:-1])
    assert current["payload"]["encoded_bytes"] == sum(item["encoded_bytes"] for item in current["payload"]["rows"])
    assert payload["rows"][1]["encoded_bytes"] == 0


def test_successful_bound_does_not_accept_copied_owner_shape():
    class Unissued:
        def __getattr__(self, name):
            raise AssertionError("unissued owner read " + name)
    value = Unissued()
    with pytest.raises(HeaderBoundsError, match="memory_current_convergence_owner_unavailable"):
        _native_memory_successful_current_lifecycle_numeric_entries(value, value,
            certificate=value, retention_widths=value)
