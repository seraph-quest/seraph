"""Provider-free tests of billing identity, ambiguity and charge precision."""
from dataclasses import FrozenInstanceError
import hashlib
import json
import uuid

import pytest

from src.model_fabric.near_text_billing import (
    NearBillingEvidence, NearInferenceIdentity, NearTextBillingError, derive_near_inference_id,
    parse_near_billing_evidence, validate_near_billing_evidence,
)

BODY_ID = "chatcmpl-original-response"
PROVIDER_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, BODY_ID))
OPERATION = "near-original-operation"
IDENTITY = derive_near_inference_id(body_id=BODY_ID)


def wire(cost=1001, **extra):
    return json.dumps({"requests": [{"requestId": PROVIDER_ID, "costNanoUsd": cost}], **extra}).encode()


def parse(body):
    return parse_near_billing_evidence(response_body=body,
        original_operation_id=OPERATION, inference_identity=IDENTITY)


def test_header_and_absent_header_bind_to_documented_response_identity():
    assert derive_near_inference_id(body_id=BODY_ID).provider_request_id == PROVIDER_ID
    identity = derive_near_inference_id(body_id=BODY_ID, inference_id_header=PROVIDER_ID.upper())
    assert identity.provider_request_id == PROVIDER_ID
    assert parse_near_billing_evidence(response_body=wire(), original_operation_id=OPERATION,
        inference_identity=identity).provider_request_id == PROVIDER_ID
    assert derive_near_inference_id(body_id="é" * 128).provider_request_id == str(uuid.uuid5(uuid.NAMESPACE_DNS, "é" * 128))


@pytest.mark.parametrize("body_id", [None, 0, True, "", "a" * 257, "é" * 129, "\ud800"])
def test_body_identity_is_bounded_by_utf8_bytes(body_id):
    with pytest.raises(NearTextBillingError):
        derive_near_inference_id(body_id=body_id)


@pytest.mark.parametrize("header", ["", " " + PROVIDER_ID, PROVIDER_ID.replace("-", ""),
    "{" + PROVIDER_ID + "}", "urn:uuid:" + PROVIDER_ID, str(uuid.UUID(int=0)), True, 0])
def test_present_invalid_or_mismatched_header_never_falls_back(header):
    with pytest.raises(NearTextBillingError):
        derive_near_inference_id(body_id=BODY_ID, inference_id_header=header)


@pytest.mark.parametrize("nano,micro", [(0, 0), (1, 1), (999, 1), (1000, 1), (1001, 2),
    (999_999_999_999, 1_000_000_000), (1_000_000_000_000, 1_000_000_000)])
def test_exact_charge_rounding_preserves_original_nano_amount_and_operation(nano, micro):
    body = wire(nano)
    evidence = parse(body)
    assert evidence.cost_nano_usd == nano
    assert evidence.cost_microusd == micro
    assert evidence.original_operation_id == OPERATION
    assert evidence.provider_request_id == PROVIDER_ID
    assert evidence.source == "near_billing_costs"
    assert evidence.response_sha256 == hashlib.sha256(body).hexdigest()
    assert validate_near_billing_evidence(evidence, original_operation_id=OPERATION) is evidence


@pytest.mark.parametrize("nano", [True, False, -1, 1.0, 0.001, "1000", None,
    1_000_000_000_001, 2**63 - 1])
def test_non_integer_negative_and_out_of_range_cost_is_not_charge_evidence(nano):
    with pytest.raises(NearTextBillingError):
        parse(wire(nano))


@pytest.mark.parametrize("warning", [None, ""])
def test_explicit_absent_warning_values_allow_genuine_zero(warning):
    assert parse(wire(0, warning=warning)).cost_nano_usd == 0


@pytest.mark.parametrize("warning", ["usage missing", " ", False, 0, [], {}])
def test_warning_bearing_zero_or_malformed_warning_remains_unusable(warning):
    with pytest.raises(NearTextBillingError) as failure:
        parse(wire(0, warning=warning))
    assert "usage missing" not in str(failure.value)


@pytest.mark.parametrize("payload", [
    {}, [], {"requests": []}, {"requests": None},
    {"requests": [{"requestId": PROVIDER_ID}]},
    {"requests": [{"requestId": PROVIDER_ID, "costNanoUsd": 1, "extra": 1}]},
    {"requests": [{"requestId": PROVIDER_ID, "costNanoUsd": 1}], "currency": "USD"},
    {"requests": [{"requestId": str(uuid.UUID(int=0)), "costNanoUsd": 1}]},
    {"requests": [{"requestId": PROVIDER_ID, "costNanoUsd": 1}] * 2},
])
def test_missing_ambiguous_foreign_and_extra_fields_fail_closed(payload):
    with pytest.raises(NearTextBillingError):
        parse(json.dumps(payload).encode())


@pytest.mark.parametrize("body", [b"", b"not-json", b"\xff", b'{"requests":[],"requests":[]}',
    ('{"requests":[{"requestId":"' + PROVIDER_ID + '","costNanoUsd":1,"costNanoUsd":0}]}').encode(),
    ('{"requests":[{"requestId":"' + PROVIDER_ID + '","costNanoUsd":NaN}]}').encode(),
    wire().decode().encode("utf-16"), b"[" * 2000 + b"]" * 2000,
])
def test_duplicate_keys_invalid_encoding_and_json_do_not_mint_evidence(body):
    with pytest.raises(NearTextBillingError):
        parse(body)


def test_raw_byte_cap_and_digest_include_whitespace_from_actual_wire():
    body = wire()
    bounded = body + b" " * (16 * 1024 - len(body))
    assert parse(bounded).response_sha256 == hashlib.sha256(bounded).hexdigest()
    with pytest.raises(NearTextBillingError):
        parse(bounded + b" ")
    with pytest.raises(NearTextBillingError):
        parse(bytearray(body))


def test_billing_evidence_preserves_charge_without_local_reservation_clipping():
    # Admission/overrun decisions belong to accounting, never this pure parser.
    evidence = parse(wire(9_876_543))
    assert evidence.cost_nano_usd == 9_876_543
    assert evidence.cost_microusd == 9_877


def test_evidence_is_immutable_and_cannot_cross_operation_or_unsealed_boundaries():
    evidence = parse(wire())
    with pytest.raises(TypeError):
        NearBillingEvidence()
    with pytest.raises(FrozenInstanceError):
        evidence.cost_nano_usd = 0
    with pytest.raises(NearTextBillingError):
        validate_near_billing_evidence(evidence, original_operation_id="another-operation")
    for unsealed in ({"source": "near_billing_costs"}, object.__new__(NearBillingEvidence), None):
        with pytest.raises(NearTextBillingError):
            validate_near_billing_evidence(unsealed)


@pytest.mark.parametrize("field,value", [("cost_microusd", 0), ("cost_nano_usd", True),
    ("source", "provider_account_usage"), ("response_sha256", "g" * 64),
    ("provider_request_id", PROVIDER_ID.upper()), ("_seal", object())])
def test_consumer_revalidation_detects_corrupted_evidence(field, value):
    evidence = parse(wire())
    object.__setattr__(evidence, field, value)
    with pytest.raises(NearTextBillingError):
        validate_near_billing_evidence(evidence)


@pytest.mark.parametrize("operation", ["", "é" * 129, None])
def test_operation_binding_is_required_and_bounded(operation):
    with pytest.raises(NearTextBillingError):
        parse_near_billing_evidence(response_body=wire(), original_operation_id=operation,
            inference_identity=IDENTITY)


def test_matching_billing_uuid_cannot_bypass_completion_identity_derivation():
    for identity in (PROVIDER_ID, {"provider_request_id": PROVIDER_ID},
            object.__new__(NearInferenceIdentity), derive_near_inference_id(body_id="foreign-response")):
        with pytest.raises(NearTextBillingError):
            parse_near_billing_evidence(response_body=wire(), original_operation_id=OPERATION,
                inference_identity=identity)


@pytest.mark.parametrize("field,value", [("_body_id", "changed-response"),
    ("_inference_id_header", str(uuid.UUID(int=0))), ("provider_request_id", str(uuid.UUID(int=0))),
    ("_seal", object())])
def test_corrupted_completion_identity_is_rejected_at_parser_boundary(field, value):
    identity = derive_near_inference_id(body_id=BODY_ID, inference_id_header=PROVIDER_ID)
    object.__setattr__(identity, field, value)
    with pytest.raises(NearTextBillingError):
        parse_near_billing_evidence(response_body=wire(), original_operation_id=OPERATION,
            inference_identity=identity)
