"""Original envelope decoding stays identical after physical reads are staged."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _envelope(capability):
    if capability == "browser.public-task.v1":
        inputs = {"schema_version": 1, "start_url": "https://example.com/",
            "allowed_hosts": ["example.com"], "approved_url_prefixes": ["https://example.com/"],
            "actions": [{"kind": "extract", "selector": "body", "max_chars": 100,
                "expected_checks": [{"kind": "url_host", "value": "example.com"}]}],
            "final_expected_checks": [{"kind": "url_host", "value": "example.com"}]}
    else:
        inputs = {"schema_version": 1, "operation_ref": "operation:one", "plan_version": 1,
            "producer_task_ref": "task:one", "producer_attempt_ref": "attempt:one",
            "handoff_ref": "handoff:one", "producer_sha256": hashlib.sha256(b"quoted data").hexdigest(),
            "producer_schema": "browser_public_task_result" if capability == "work.evidence-dossier.v1" else "evidence_dossier.v1",
            "quoted_source_data": "quoted data", "no_learning": True}
    return json.dumps({"schema_version": 1, "capability_id": capability, "input": inputs}).encode()


@pytest.mark.parametrize("capability", ["browser.public-task.v1", "work.evidence-dossier.v1", "work.local-evidence-report.v1"])
def test_default_parser_and_held_bytes_preserve_envelope_contract(tmp_path, monkeypatch, capability):
    from config.settings import settings
    from src.work_board.dispatcher import _parse_typed_input, _decode_typed_input_payload, _typed_input_model, TypedInputError

    payload = _envelope(capability)
    (tmp_path / "input.json").write_bytes(payload)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    task = SimpleNamespace(capability_id=capability, typed_input_ref="workspace-json:input.json",
        typed_input_digest=hashlib.sha256(payload).hexdigest())
    expected = _parse_typed_input(task)
    # Resolve the original lazy finite model before forbidding physical reads.
    assert _typed_input_model(capability) is not None
    def deny_read(*args, **kwargs):
        raise AssertionError("held-byte decoding attempted a physical file read")
    monkeypatch.setattr(Path, "read_bytes", deny_read)
    assert _decode_typed_input_payload(task, payload) == expected
    with pytest.raises(AssertionError, match="physical file read"):
        _parse_typed_input(task)
    with pytest.raises(TypedInputError) as wrong_type:
        _decode_typed_input_payload(task, bytearray(payload))
    assert wrong_type.value.code == "typed_input_invalid"
    with pytest.raises(TypedInputError) as changed_bytes:
        _decode_typed_input_payload(task, payload + b" ")
    assert changed_bytes.value.code == "typed_input_digest_mismatch"


@pytest.mark.parametrize("payload, code", [
    (b"x" * (64 * 1024 + 1), "typed_input_too_large"),
    (b"\xff", "typed_input_json_invalid"),
    (b"{", "typed_input_json_invalid"),
    (b"[]", "typed_input_envelope_invalid"),
    (b'{"schema_version":1,"capability_id":"browser.public-task.v1","input":{},"authority":{}}', "typed_input_envelope_invalid"),
])
def test_held_bytes_retain_original_failures(payload, code):
    from src.work_board.dispatcher import _decode_typed_input_payload, TypedInputError
    task = SimpleNamespace(capability_id="browser.public-task.v1", typed_input_digest=hashlib.sha256(payload).hexdigest())
    with pytest.raises(TypedInputError) as error:
        _decode_typed_input_payload(task, payload)
    assert error.value.code == code
