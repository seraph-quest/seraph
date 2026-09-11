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


def proof_is_fresh(proof: ModelRouteProof, *, now: float | None = None) -> bool:
    """Return whether a valid passing proof is fresh at the requested instant."""
    try:
        validate_model_route_proof(proof)
    except ValueError:
        return False
    checked_at = time.time() if now is None else float(now)
    return proof.outcome == "passed" and proof.checked_at <= checked_at < proof.expires_at
