"""Closed byte parser proof; these payloads mint no Source or publication grant."""
from copy import deepcopy
import pytest

from src.memory.header_bounds import HeaderBoundsError
from src.workspace.accounting_witness import (
    MEMORY_REFERENCE_PROFILE, preflight_native_memory_reference_payload,
    _native_memory_unknown_replacement_upper_bytes,
    MEMORY_ORIGINAL_CHECKPOINT, MEMORY_CURRENT_CHECKPOINT,
    preflight_native_memory_reference_journal,
)


def original():
    return {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "invocation_ref": "job", "claim_ref": "claim", "candidate_digest": "a" * 64,
        "composition_binding_digest": "b" * 64, "method": "memory.forget",
        "owner_principal_id": "operator", "operator_session_id": "session",
        "original_deadline": "2026-10-09T00:00:00+00:00", "source_binding_digest": "c" * 64,
        "original_effect_digest": "d" * 64,
        "original_audit": {"ref": {"table": "audit_events", "key": "audit"}, "tuple_digest": "e" * 64},
        "refs": [{"table": "audit_events", "key": "audit"}, {"table": "workflow_run_states", "key": "job"}],
        "rows_digest": "f" * 64, "encoded_bytes": 100}


def unknown():
    return {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "original_checkpoint_digest": "a" * 64, "projection_revision": 1,
        "state": "unknown", "reason_code": "canonical_bound_not_certified",
        "refs": [{"table": "memories", "key": "private"}], "absences": [], "rows": [],
        "rows_digest": None, "encoded_bytes": 500, "owner_operation_kind": "memory.forget",
        "owner_events": [], "selected_delta_digest": "f" * 64}


def test_exact_closed_original_and_unknown_shapes():
    assert preflight_native_memory_reference_payload(original()) < 262_144
    assert preflight_native_memory_reference_payload(unknown(), current=True) < 262_144


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("method", {}), ("original_deadline", "2026-10-09"),
    ("candidate_digest", "A" * 64), ("encoded_bytes", True),
    ("refs", [{"table": "task_method_active", "key": "not-a-tenth-table"}]),
])
def test_original_malformed_fields_fail_closed(field, value):
    payload = original()
    payload[field] = value
    with pytest.raises(HeaderBoundsError, match="memory_reference_payload_invalid"):
        preflight_native_memory_reference_payload(payload)


@pytest.mark.parametrize("field,value", [
    ("original_checkpoint_digest", None), ("rows_digest", "a" * 64),
    ("rows", [{"private": "body"}]), ("owner_operation_kind", {}),
    ("owner_events", [{"table": "memories", "key": "fake-event"}]),
    ("projection_revision", True),
])
def test_unknown_cannot_publish_body_digest_or_invent_original(field, value):
    payload = unknown()
    payload[field] = value
    with pytest.raises(HeaderBoundsError, match="memory_reference_payload_invalid"):
        preflight_native_memory_reference_payload(payload, current=True)


def test_missing_original_is_reduction_only_shape_and_refs_are_unique_sorted():
    payload = unknown()
    payload["original_checkpoint_digest"] = None
    payload["reason_code"] = "original_projection_unavailable"
    assert preflight_native_memory_reference_payload(payload, current=True)
    payload["refs"].append(deepcopy(payload["refs"][0]))
    with pytest.raises(HeaderBoundsError, match="memory_reference_payload_invalid"):
        preflight_native_memory_reference_payload(payload, current=True)


def test_validated_rows_account_exact_bytes_and_cannot_contain_absence():
    payload = unknown()
    payload.update(state="validated", reason_code=None, rows_digest="c" * 64,
        encoded_bytes=17, rows=[{"ref": payload["refs"][0], "tuple_digest": "b" * 64, "encoded_bytes": 17}])
    assert preflight_native_memory_reference_payload(payload, current=True)
    payload["encoded_bytes"] += 1
    with pytest.raises(HeaderBoundsError, match="memory_reference_payload_invalid"):
        preflight_native_memory_reference_payload(payload, current=True)


def test_unknown_reserve_counts_absences_events_escaping_and_wrapper():
    refs = [{"table": "goals", "key": "goal"}, {"table": "memories", "key": "memory"}]
    plain = _native_memory_unknown_replacement_upper_bytes(refs,
        original_checkpoint_digest="a" * 64, owner_operation_kind="memory.forget", owner_events=[])
    escaped = [{"table": "goals", "key": "\x00" * 64}, {"table": "memories", "key": "memory"}]
    charged = _native_memory_unknown_replacement_upper_bytes(escaped,
        original_checkpoint_digest="a" * 64, owner_operation_kind="memory.forget",
        owner_events=[{"table": "audit_events", "key": "actual-planned-id"}])
    assert charged > plain > 512


def test_closed_journal_parser_checks_duplicates_payload_digest_and_missing_original():
    import json
    from src.workflows.job_runtime import _digest
    payload = original()
    record = {"checkpoint_id": MEMORY_ORIGINAL_CHECKPOINT, "state_digest": _digest(payload),
              "safe": True, "payload": payload}
    parsed, current = preflight_native_memory_reference_journal(json.dumps([record]))
    assert parsed == record and current is None
    assert preflight_native_memory_reference_journal("[]") == (None, None)
    with pytest.raises(HeaderBoundsError, match="memory_reference_journal_invalid"):
        preflight_native_memory_reference_journal(json.dumps([record, record]))
    record["state_digest"] = "0" * 64
    with pytest.raises(HeaderBoundsError, match="memory_reference_journal_invalid"):
        preflight_native_memory_reference_journal(json.dumps([record]))
    absent = unknown()
    absent.update(original_checkpoint_digest=None, reason_code="original_projection_unavailable")
    current_record = {"checkpoint_id": MEMORY_CURRENT_CHECKPOINT, "state_digest": _digest(absent),
                      "safe": True, "payload": absent}
    assert preflight_native_memory_reference_journal(json.dumps([current_record])) == (None, current_record)


def test_journal_rejects_duplicate_json_keys_and_unchecked_oversized_column():
    with pytest.raises(HeaderBoundsError, match="header_json_invalid"):
        preflight_native_memory_reference_journal('[{"checkpoint_id":"one","checkpoint_id":"two"}]')
    with pytest.raises(HeaderBoundsError, match="header_json_bound"):
        preflight_native_memory_reference_journal(" " * 1_048_577)
