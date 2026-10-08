"""Private byte boundary tests; these do not mint inference authority."""
import hashlib
import os
import pytest

from src.agent.turn_execution import NativeTurnBlocked
from src.runtime_plugins.inference_output import (
    MAX_BYTES, _FIELDS, _write_output, output_path, read_output_bytes,
    validate_payload, validate_output_publication,
)


def payload(data):
    result = {key: "value" for key in _FIELDS}
    for key in ("reservation_binding_digest", "turn_claim_digest", "composition_binding_digest", "payload_digest", "policy_digest", "result_content_sha256", "route_receipt_hash", "route_attempts_digest", "accounting_readback_digest"):
        result[key] = "a" * 64
    result.update(schema_version=1, attempt_count=1, fencing_token=1, reservation_sequence=1,
        family_call_index=0, purpose_deadline_at=1000, operation_deadline_at=1000, turn_deadline_at=1000,
        actual_cost_microusd=0, size_bytes=len(data), content_sha256=hashlib.sha256(data).hexdigest(),
        output_ref="native-output-" + "b" * 48, output_codec="native-inference-output-bytes.v1", memory_status="no_learning")
    result["file_ref"] = output_path(result["accounting_job_id"], result["content_sha256"])
    return result


@pytest.mark.parametrize("size", [65537, MAX_BYTES])
def test_exact_private_large_file(tmp_path, size):
    data = b"x" * size
    state = payload(data)
    _write_output(tmp_path, state, data)
    path = tmp_path / state["file_ref"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_nlink == 1
    assert read_output_bytes(tmp_path, state) == data
    with pytest.raises(FileExistsError):
        _write_output(tmp_path, state, data)


@pytest.mark.parametrize("mutation", ["byte", "mode", "hardlink", "symlink", "directory_symlink"])
def test_actual_file_mutations_fail_closed(tmp_path, mutation):
    data = b"original-private-bytes"
    state = payload(data)
    _write_output(tmp_path, state, data)
    path = tmp_path / state["file_ref"]
    if mutation == "byte":
        path.write_bytes(b"X" + data[1:])
    elif mutation == "mode":
        path.chmod(0o644)
    elif mutation == "hardlink":
        os.link(path, tmp_path / "second-link")
    elif mutation == "symlink":
        target = tmp_path / "foreign"
        path.rename(target)
        path.symlink_to(target)
    else:
        parent = path.parent
        moved = tmp_path / "foreign-directory"
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(NativeTurnBlocked):
        read_output_bytes(tmp_path, state)


@pytest.mark.parametrize("mutation", ["overflow", "bool_size", "unknown", "foreign_job", "path_escape"])
def test_closed_binding_and_bound(mutation):
    state = payload(b"x")
    if mutation == "overflow":
        state["size_bytes"] = MAX_BYTES + 1
    elif mutation == "bool_size":
        state["size_bytes"] = True
    elif mutation == "unknown":
        state["arbitrary"] = "denied"
    elif mutation == "foreign_job":
        state["accounting_job_id"] = "other"
    else:
        state["file_ref"] = "../outside"
    with pytest.raises(NativeTurnBlocked):
        validate_payload(state)


def test_plain_permission_cannot_mint_output():
    from types import SimpleNamespace
    from src.runtime_plugins.inference_output import INTENT_ID
    entry = {"checkpoint_id": INTENT_ID, "payload": payload(b"x")}
    db = SimpleNamespace(info={"composition_native_inference_output_permission": ("job", entry)})
    with pytest.raises(NativeTurnBlocked):
        validate_output_publication(db, {}, {INTENT_ID: entry}, run_id="job")
    with pytest.raises(NativeTurnBlocked):
        validate_output_publication(db, {INTENT_ID: entry}, {}, run_id="job")


@pytest.mark.parametrize("number", ["0.000001", "100000000000000000000000.123456789", "-0.0"])
def test_private_numeric_envelope_preserves_decimal_and_route_hash(number):
    from decimal import Decimal
    from src.runtime_plugins.inference_output import _bytes, _digest, verify_output_envelope
    from src.model_fabric.receipts import canonical_hash
    content = {"provider_response": {"charge": Decimal(number)}}
    route = {"receipt_id": "real-route-ref", "request_id": "actual-request", "outcome": "succeeded", "attempts": [], "amount": 1e-6}
    envelope = {"schema_version": 1, "result_codec": "sdk_chat_message", "result_content": content,
        "result_content_sha256": _digest(content), "route_receipt": route,
        "route_attempts": [], "accounting_readback": {"actual_cost_microusd": 0}}
    data = _bytes(envelope)
    state = payload(data)
    state.update(result_content_sha256=_digest(content), route_receipt_id=route["receipt_id"],
        route_request_id=route["request_id"], route_receipt_hash=canonical_hash(route),
        route_attempts_digest=_digest([]), accounting_readback_digest=_digest(envelope["accounting_readback"]))
    assert verify_output_envelope(data, state)["result_content"] == content


def test_marker_without_sealed_output_cannot_be_succeeded():
    from src.runtime_plugins.inference_output import checked_candidate_record, CANDIDATE_ID
    # Malformed consumed marker must fail closed instead of preserving success.
    import json
    row = {"checkpoint_receipts_json": json.dumps([{"checkpoint_id": CANDIDATE_ID, "safe": True, "state_digest": "a" * 64, "payload": {}}]), "status": "succeeded"}
    with pytest.raises(NativeTurnBlocked):
        checked_candidate_record(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["inference:original-candidate.v1", "inference:owned-output-intent.v1", "inference:owned-output.v1"])
async def test_generic_and_recovery_checkpoint_cannot_mint_private_output(identifier):
    from src.workflows.job_runtime import DurableJobRepository, DurableJobTransitionError
    repository = DurableJobRepository()
    with pytest.raises(DurableJobTransitionError):
        await repository.record_checkpoint("forged-owner", checkpoint_id=identifier, state={}, owner="forger", fencing_token=1)
    with pytest.raises(DurableJobTransitionError):
        await repository.record_recovery_checkpoint("forged-owner", checkpoint_id=identifier, state={},
            owner_kind="user", owner_principal_id="operator:forger")
