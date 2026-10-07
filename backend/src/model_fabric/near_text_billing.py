"""Strict NEAR billing evidence parsing; no transport or settlement authority.

The HTTPS adapter supplies bytes only after its fixed TLS endpoint checks. This
module authenticates no provider, attestation, or response signature itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import uuid


MAX_BILLING_BODY_BYTES = 16 * 1024
MAX_IDENTIFIER_BYTES = 256
MAX_COST_NANO_USD = 1_000_000_000_000
_UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_SEAL = object()
_IDENTITY_SEAL = object()


class NearTextBillingError(ValueError):
    """Content-safe failure code; never include provider body values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, init=False)
class NearInferenceIdentity:
    """Derived completion identity; raw UUID strings cannot create evidence."""

    provider_request_id: str
    _body_id: str = field(repr=False, compare=False)
    _inference_id_header: str | None = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("NearInferenceIdentity must be derived from the completion")


@dataclass(frozen=True, slots=True, init=False)
class NearBillingEvidence:
    """Parser-created cost facts; consumers must revalidate original binding.

    The seal rejects arbitrary dictionaries/unvalidated objects. It is an
    internal trusted-code marker, not a hostile-Python security boundary.
    """

    original_operation_id: str
    provider_request_id: str
    cost_nano_usd: int
    cost_microusd: int
    response_sha256: str
    source: str
    _seal: object = field(repr=False, compare=False)
    _identity: NearInferenceIdentity = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("NearBillingEvidence must be created by the billing parser")


def _bounded_identifier(value: object, code: str) -> str:
    if type(value) is not str or not value:
        raise NearTextBillingError(code)
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise NearTextBillingError(code) from None
    if length > MAX_IDENTIFIER_BYTES:
        raise NearTextBillingError(code)
    return value


def _strict_uuid(value: object, code: str) -> str:
    if type(value) is not str or _UUID_PATTERN.fullmatch(value) is None:
        raise NearTextBillingError(code)
    return str(uuid.UUID(value))


def _derived_uuid(body_id: object, inference_id_header: object) -> str:
    body = _bounded_identifier(body_id, "near_billing_identity_invalid")
    computed = str(uuid.uuid5(uuid.NAMESPACE_DNS, body))
    if inference_id_header is None:
        return computed
    header = _strict_uuid(inference_id_header, "near_billing_identity_invalid")
    if header != computed:
        raise NearTextBillingError("near_billing_identity_mismatch")
    return computed


def derive_near_inference_id(
    *, body_id: object, inference_id_header: object = None,
) -> NearInferenceIdentity:
    """Seal documented UUIDv5 body identity and present strict header binding."""
    computed = _derived_uuid(body_id, inference_id_header)
    identity = object.__new__(NearInferenceIdentity)
    for key, value in {"provider_request_id": computed, "_body_id": body_id,
            "_inference_id_header": inference_id_header, "_seal": _IDENTITY_SEAL}.items():
        object.__setattr__(identity, key, value)
    return identity


def _validate_identity(identity: object) -> NearInferenceIdentity:
    code = "near_billing_identity_invalid"
    if type(identity) is not NearInferenceIdentity or getattr(identity, "_seal", None) is not _IDENTITY_SEAL:
        raise NearTextBillingError(code)
    try:
        if identity.provider_request_id != _derived_uuid(identity._body_id, identity._inference_id_header):
            raise NearTextBillingError("near_billing_identity_mismatch")
    except AttributeError:
        raise NearTextBillingError(code) from None
    return identity


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise NearTextBillingError("near_billing_response_invalid")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise NearTextBillingError("near_billing_response_invalid")


def parse_near_billing_evidence(
    *, response_body: bytes, original_operation_id: str, inference_identity: NearInferenceIdentity,
) -> NearBillingEvidence:
    """Parse exactly one matching authoritative nanoUSD row from bounded JSON.

    Caller owns actual fixed HTTPS read provenance. No dollar amount is inferred
    from token usage, pricing, a missing record, or a warning-bearing zero.
    """
    operation = _bounded_identifier(original_operation_id, "near_billing_evidence_invalid")
    identity = _validate_identity(inference_identity)
    expected = identity.provider_request_id
    if type(response_body) is not bytes or not 1 <= len(response_body) <= MAX_BILLING_BODY_BYTES:
        raise NearTextBillingError("near_billing_response_invalid")
    try:
        payload = json.loads(response_body.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise NearTextBillingError("near_billing_response_invalid") from None
    if type(payload) is not dict or set(payload) not in ({"requests"}, {"requests", "warning"}):
        raise NearTextBillingError("near_billing_response_invalid")
    warning = payload.get("warning")
    if warning is not None and not (type(warning) is str and warning == ""):
        raise NearTextBillingError("near_billing_response_invalid")
    rows = payload["requests"]
    if type(rows) is not list or len(rows) != 1:
        raise NearTextBillingError("near_billing_response_invalid")
    row = rows[0]
    if type(row) is not dict or set(row) != {"requestId", "costNanoUsd"}:
        raise NearTextBillingError("near_billing_response_invalid")
    actual = _strict_uuid(row["requestId"], "near_billing_identity_invalid")
    if actual != expected:
        raise NearTextBillingError("near_billing_identity_mismatch")
    nano = row["costNanoUsd"]
    if type(nano) is not int or not 0 <= nano <= MAX_COST_NANO_USD:
        raise NearTextBillingError("near_billing_response_invalid")
    evidence = object.__new__(NearBillingEvidence)
    for key, value in {
        "original_operation_id": operation, "provider_request_id": actual,
        "cost_nano_usd": nano, "cost_microusd": (nano + 999) // 1000,
        "response_sha256": hashlib.sha256(response_body).hexdigest(),
        "source": "near_billing_costs", "_seal": _EVIDENCE_SEAL, "_identity": identity,
    }.items():
        object.__setattr__(evidence, key, value)
    return validate_near_billing_evidence(evidence, original_operation_id=operation)


def validate_near_billing_evidence(
    evidence: object, *, original_operation_id: str | None = None,
) -> NearBillingEvidence:
    """Recheck the typed internal seal, exact fields and optional operation."""
    code = "near_billing_evidence_invalid"
    if type(evidence) is not NearBillingEvidence or getattr(evidence, "_seal", None) is not _EVIDENCE_SEAL:
        raise NearTextBillingError(code)
    try:
        operation = _bounded_identifier(evidence.original_operation_id, code)
        provider = _strict_uuid(evidence.provider_request_id, code)
        identity = _validate_identity(evidence._identity)
        if evidence.provider_request_id != provider or provider != identity.provider_request_id:
            raise NearTextBillingError(code)
        if (type(evidence.cost_nano_usd) is not int
                or not 0 <= evidence.cost_nano_usd <= MAX_COST_NANO_USD
                or type(evidence.cost_microusd) is not int
                or evidence.cost_microusd != (evidence.cost_nano_usd + 999) // 1000
                or type(evidence.source) is not str
                or evidence.source != "near_billing_costs"
                or type(evidence.response_sha256) is not str
                or _DIGEST_PATTERN.fullmatch(evidence.response_sha256) is None):
            raise NearTextBillingError(code)
        if original_operation_id is not None and operation != _bounded_identifier(original_operation_id, code):
            raise NearTextBillingError(code)
    except AttributeError:
        raise NearTextBillingError(code) from None
    return evidence
