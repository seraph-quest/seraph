"""Closed codec boundary tests; these fixtures grant no native authority."""
from copy import deepcopy

import pytest

from src.agent.native_turn_family import validate_family_payload, validate_family_transition
from src.agent.turn_execution import NativeTurnBlocked


def family(count=0):
    return {"schema_version": 1, "family_version": "native-turn-operation-family.v1",
        "turn_job_ref": "owned-turn", "input_message_ref": "owned-input", "native_route": "generic_turn",
        "original_claim_digest": "a" * 64, "original_deadline": "2026-10-08T12:00:00+00:00",
        "sdk_steps": 64, "max_inference_operations": 65, "max_encoded_bytes": 65536,
        "operations": [{"kind": "openrouter-accounting.v1", "operation_id": f"operation-{index}",
            "job_id": f"ephemeral-{index}", "owner_id": "operator:owned", "lease_owner": f"lease-{index}",
            "attempt_count": 1, "fencing_token": 1, "reservation_sequence": index + 1,
            "payload_digest": "b" * 64, "policy_digest": "c" * 64, "profile_id": "openrouter.text",
            "runtime_path": "chat_agent", "operation_deadline": "2026-10-08T12:00:00+00:00",
            "reservation_binding_digest": "d" * 64} for index in range(count)]}


def test_family_codec_accepts_exact_65_and_one_append_without_truncation():
    payload = family(65)
    assert validate_family_payload(payload) is payload and len(payload["operations"]) == 65
    validate_family_transition(family(64), payload)
    validate_family_transition(None, family())


@pytest.mark.parametrize("damage", ["extra", "bool_schema", "bool_steps", "step65", "limit66", "encoded_limit",
    "count66", "duplicate_operation", "duplicate_job", "bool_fence", "unknown_kind", "raw_id256plus",
    "naive_date", "noncanonical_date", "digest", "encoded_overflow"])
def test_family_codec_rejects_open_schema_and_counter_identity_date_budget_damage(damage):
    payload = family(2)
    if damage == "extra": payload["raw_ids"] = []
    elif damage == "bool_schema": payload["schema_version"] = True
    elif damage == "bool_steps": payload["sdk_steps"] = True
    elif damage == "step65": payload["sdk_steps"] = 65
    elif damage == "limit66": payload["max_inference_operations"] = 66
    elif damage == "encoded_limit": payload["max_encoded_bytes"] = 65537
    elif damage == "count66": payload = family(66)
    elif damage == "duplicate_operation": payload["operations"][1]["operation_id"] = payload["operations"][0]["operation_id"]
    elif damage == "duplicate_job": payload["operations"][1]["job_id"] = payload["operations"][0]["job_id"]
    elif damage == "bool_fence": payload["operations"][0]["fencing_token"] = True
    elif damage == "unknown_kind": payload["operations"][0]["kind"] = "foreign-effect.v1"
    elif damage == "raw_id256plus": payload["operations"][0]["operation_id"] = "x" * 257
    elif damage == "naive_date": payload["original_deadline"] = "2026-10-08T12:00:00"
    elif damage == "noncanonical_date": payload["original_deadline"] = "2026-10-08T12:00:00Z"
    elif damage == "digest": payload["operations"][0]["reservation_binding_digest"] = "foreign"
    else:
        payload = family(65)
        for index, operation in enumerate(payload["operations"]):
            for key in ("operation_id", "job_id", "lease_owner"):
                operation[key] = f"{key}-{index}".ljust(256, "x")
            operation["owner_id"] = "o" * 256
    with pytest.raises(NativeTurnBlocked):
        validate_family_payload(payload)


@pytest.mark.parametrize("damage", ["reorder", "remove", "replace", "limits", "double_append", "initial_nonempty"])
def test_family_transition_preserves_exact_old_prefix_and_fixed_budget(damage):
    before, after = family(2), family(3)
    if damage == "reorder": after["operations"][:2] = list(reversed(after["operations"][:2]))
    elif damage == "remove": after["operations"].pop(0)
    elif damage == "replace": after["operations"][0]["lease_owner"] = "foreign"
    elif damage == "limits": after["sdk_steps"], after["max_inference_operations"] = 63, 64
    elif damage == "double_append": after = family(4)
    else: before = None
    with pytest.raises(NativeTurnBlocked):
        validate_family_transition(deepcopy(before), after)
