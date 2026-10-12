"""Codec allocation checks; these fixtures confer no Memory Source authority."""
from collections import Counter
import json

import pytest

from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, WRS_BY_RUN
from src.workspace.accounting_witness import (
    MEMORY_REFERENCE_PROFILE, MEMORY_ORIGINAL_CHECKPOINT,
    _native_memory_current_construction_numeric_entries,
    _native_memory_original_preflight_numeric_entries,
    _native_memory_prepare_current_row, _trace_memory_numeric_charges,
    preflight_native_memory_reference_payload,
)
from src.workflows.job_runtime import _canonical, _digest


@pytest.mark.parametrize("factory", [
    _native_memory_current_construction_numeric_entries,
    _native_memory_original_preflight_numeric_entries,
])
def test_numeric_factory_rejects_unissued_source_before_any_owner_access(factory):
    class Poison:
        def __getattr__(self, name):
            raise AssertionError("unissued capacity touched " + name)
    poison = Poison()
    with pytest.raises(HeaderBoundsError, match="memory_current_numeric_owner_unavailable"):
        factory(poison, poison, certificate=poison, retention_widths=poison)


def test_real_current_codec_has_three_payload_preflights_per_build():
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
    before = _canonical(row), _canonical(payload)
    budget = HeaderReadBudget()
    with _trace_memory_numeric_charges(budget) as trace:
        final, record = _native_memory_prepare_current_row(row, payload, header_budget=budget)
    counts = Counter(appearance for appearance, _upper in trace)
    builds = counts[("memory-current-allocation", "job", "count-update")]
    assert 2 <= builds <= 33
    assert counts[("memory-reference-payload", "current", "canonical")] == 3 * builds + 1
    assert counts[("memory-current-splice", "journal-decode")] == builds
    assert counts[("memory-own-journal", "job", "decode")] == 1
    assert counts[("memory-reference-payload", "original", "canonical")] == 1
    assert before == (_canonical(row), _canonical(payload))
    assert record["payload"]["encoded_bytes"] == sum(item["encoded_bytes"] for item in record["payload"]["rows"])
    assert json.loads(final["checkpoint_receipts_json"])[0] == original


def test_original_payload_exhaustion_precedes_canonical_serializer(monkeypatch):
    budget = HeaderReadBudget()
    budget.debit(budget.remaining)
    def forbidden_serializer(*args, **kwargs):
        raise AssertionError("exhausted original preflight serialized a payload")
    monkeypatch.setattr(json, "dumps", forbidden_serializer)
    with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
        preflight_native_memory_reference_payload({}, header_budget=budget)
