"""Creation and exact-binding validation for empirical model capability proofs."""

from __future__ import annotations

import math
import re
import time

from .contracts import EndpointClass, ModelRouteProof, ProviderProfile, transport_endpoint
from .receipts import canonical_hash, safe_code, sanitized_endpoint


_PROOF_OUTCOMES = frozenset({"passed", "failed"})


def build_model_route_proof(
    *,
    profile: ProviderProfile,
    endpoint_class: EndpointClass,
    adapter: str,
    capability: str,
    canary_version: str,
    outcome: str,
    checked_at: float,
    expires_at: float,
    probe_receipt_id: str,
    probe_receipt_hash: str,
    proven_value: int | str | None = None,
) -> ModelRouteProof:
    """Build one hashed proof bound to the exact profile and canary contract."""
    endpoint = sanitized_endpoint(transport_endpoint(profile))
    values = {
        "profile_schema_version": profile.schema_version,
        "profile_contract_hash": profile.contract_hash,
        "profile_id": profile.id,
        "model": profile.model,
        "endpoint": endpoint,
        "endpoint_class": endpoint_class,
        "adapter": adapter,
        "capability": capability,
        "canary_version": canary_version,
        "outcome": outcome,
        "checked_at": float(checked_at),
        "expires_at": float(expires_at),
        "probe_receipt_id": probe_receipt_id,
        "probe_receipt_hash": probe_receipt_hash,
        "proven_value": proven_value,
    }
    proof = ModelRouteProof(proof_hash="", **values)
    proof = ModelRouteProof(**{**values, "proof_hash": canonical_proof_hash(proof)})
    validate_model_route_proof(proof)
    return proof


def canonical_proof_hash(proof: ModelRouteProof) -> str:
    return canonical_hash(
        {
            "profile_schema_version": proof.profile_schema_version,
            "profile_contract_hash": proof.profile_contract_hash,
            "profile_id": proof.profile_id,
            "model": proof.model,
            "endpoint": sanitized_endpoint(proof.endpoint),
            "endpoint_class": proof.endpoint_class.value,
            "adapter": proof.adapter,
            "capability": proof.capability,
            "canary_version": proof.canary_version,
            "outcome": proof.outcome,
            "checked_at": float(proof.checked_at),
            "expires_at": float(proof.expires_at),
            "probe_receipt_id": proof.probe_receipt_id,
            "probe_receipt_hash": proof.probe_receipt_hash,
            "proven_value": proof.proven_value,
        }
    )


def validate_model_route_proof(proof: ModelRouteProof) -> None:
    """Reject malformed, unhashed, or non-sanitized proof input."""
    for field_name in (
        "profile_schema_version", "profile_id", "adapter", "capability", "canary_version",
        "probe_receipt_id",
    ):
        safe_code(getattr(proof, field_name), field_name=field_name)
    if not proof.model or len(proof.model) > 256:
        raise ValueError("proof model must be present and bounded")
    if proof.endpoint != sanitized_endpoint(proof.endpoint):
        raise ValueError("proof endpoint must not contain credentials, query parameters, or fragments")
    if proof.outcome not in _PROOF_OUTCOMES:
        raise ValueError("unsupported capability-proof outcome")
    if not all(math.isfinite(value) for value in (proof.checked_at, proof.expires_at)):
        raise ValueError("proof timestamps must be finite")
    if proof.expires_at <= proof.checked_at:
        raise ValueError("capability proof must expire after it is checked")
    for field_name, value in (
        ("profile_contract_hash", proof.profile_contract_hash),
        ("proof_hash", proof.proof_hash),
        ("probe_receipt_hash", proof.probe_receipt_hash),
    ):
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"{field_name} must be a lowercase SHA-256 value")
    if proof.proof_hash != canonical_proof_hash(proof):
        raise ValueError("capability proof hash does not match its exact binding")


# NEAR TEE slot profiles (ADR-035 amendment 2026-11-14, #1178): a passing
# capability check is a one-time setup gate. It stays valid until its binding
# changes (profile hash: model and slot settings; credential, see
# ``near_tee_credential_bound_since``), never by age. ``expires_at`` is still
# stored for schema compatibility and is advisory only for these profiles.
NEAR_TEE_PROOF_PROFILE_PREFIX = "near_tee."


def proof_expires_by_time(proof: ModelRouteProof) -> bool:
    """Whether ``expires_at`` gates this proof (every profile but NEAR TEE slots)."""
    return not str(proof.profile_id).startswith(NEAR_TEE_PROOF_PROFILE_PREFIX)


def proof_is_fresh(proof: ModelRouteProof, *, now: float | None = None) -> bool:
    """Return whether a valid passing proof is current at the requested instant.

    NEAR TEE slot proofs do not age out (#1178); other profiles keep their
    proof expiry.
    """
    try:
        validate_model_route_proof(proof)
    except ValueError:
        return False
    if proof.outcome != "passed":
        return False
    if not proof_expires_by_time(proof):
        return True
    checked_at = time.time() if now is None else float(now)
    return proof.checked_at <= checked_at < proof.expires_at


def proof_aged_out(proof: ModelRouteProof, *, now: float | None = None) -> bool:
    """A passing, exactly bound proof that only expired by time (never NEAR TEE)."""
    checked_at = time.time() if now is None else float(now)
    return proof.outcome == "passed" and proof_expires_by_time(proof) and proof.expires_at <= checked_at
