"""Untrusted owned-pipe service grammar, independent of native admission."""
import copy
import pytest

from src.runtime_plugins.contracts import SERVICE_METHODS, validate_request, validate_result
from src.runtime_plugins.protocol import ProtocolError, encode_frame, decode_json, validate_frame


def frame(method="goals.read", payload=None, **changes):
    value = {"protocol": 1, "boot_nonce": "a" * 64, "request_id": "c-1", "seq": 1,
        "kind": "request", "method": method, "invocation_ref": "native:owned-job",
        "composition_epoch": 1, "composition_digest": "b" * 64, "package_digest": "c" * 64,
        "deadline_at": 9999999999999, "payload": {} if payload is None else payload}
    value.update(changes)
    return value


@pytest.mark.parametrize("changes", [
    {"invocation_ref": None}, {"invocation_ref": "../private"},
    {"composition_epoch": None}, {"composition_epoch": 0}, {"composition_epoch": True},
    {"method": "sql.execute"}, {"method": "goals.write"},
    {"payload": {"authority_mode": "operator-root"}},
    {"payload": {"goal_id": "other-owner"}},
    {"payload": {"deadline": 9999999999999}},
])
def test_service_wire_rejects_missing_native_binding_and_authority_fields(changes):
    with pytest.raises(ProtocolError):
        encode_frame(frame(**changes))


def test_service_wire_preserves_literal_canonical_identity():
    original = frame("artifacts.read", {"artifact_ref": "art_abc", "max_bytes": 65536})
    assert validate_frame(decode_json(encode_frame(original)[4:])) == original
    assert len(SERVICE_METHODS) == 34


@pytest.mark.parametrize("payload", [
    {"artifact_ref": "/etc/passwd", "max_bytes": 10},
    {"artifact_ref": "https://private.example/a", "max_bytes": 10},
    {"artifact_ref": "art_abc", "max_bytes": 65537},
    {"artifact_ref": "art_abc", "max_bytes": True},
    {"artifact_ref": "art_abc", "max_bytes": 10, "path": "/private"},
])
def test_artifact_wire_never_accepts_paths_or_unbounded_read(payload):
    with pytest.raises(ProtocolError):
        validate_request("artifacts.read", payload)


def test_source_extraction_accepts_only_provenance_refs_not_raw_private_bytes():
    payload = {"artifact_ref": "art_public", "acquisition_receipt_ref": "research:source-intent:0",
        "source_slot": 0, "first_line": 1, "last_line": 2}
    validate_request("source-extraction.extract", payload)
    for changes in ({"raw_bytes": "private"}, {"provider_digest": "a" * 64},
                    {"source_slot": 4}, {"first_line": 3}, {"artifact_ref": "../attachment"}):
        invalid = {**payload, **changes}
        with pytest.raises(ProtocolError):
            validate_request("source-extraction.extract", invalid)


def test_source_extraction_result_has_bounded_evidence_and_no_implicit_learning():
    result = {"status": "succeeded", "memory_status": "no_learning", "value": {
        "artifact_ref": "art_public", "input_digest": "a" * 64,
        "provider_digest": "b" * 64, "config_digest": "c" * 64,
        "evidence": [{"source_ref": "source:0", "text": "untrusted quoted evidence"}]}}
    validate_result("source-extraction.extract", result)
    invalid = copy.deepcopy(result)
    invalid["value"]["evidence"][0]["text"] = "x" * 4097
    with pytest.raises(ProtocolError):
        validate_result("source-extraction.extract", invalid)
    invalid = {**result, "memory_status": "reviewed_update"}
    with pytest.raises(ProtocolError):
        validate_result("source-extraction.extract", invalid)


def test_reviewed_memory_result_cannot_claim_no_learning_or_invent_output():
    result = {"status": "succeeded", "memory_status": "reviewed_update",
        "value": {"record_ref": "memory:123", "receipt_ref": "memory:receipt:123"}}
    validate_result("memory.applyReviewed", result)
    for changes in ({"memory_status": "no_learning"}, {"secret": "private"},
                    {"value": {"record_ref": "memory:123", "revision": 1, "text": "private"}}):
        with pytest.raises(ProtocolError):
            validate_result("memory.applyReviewed", {**result, **changes})
