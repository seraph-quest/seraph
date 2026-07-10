"""Bounded, exact-target capability probe orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import time
from uuid import uuid4

from src.security.trust_contract import TrustRequest

from .contracts import (
    InferenceRequestContext,
    InferenceRequirements,
    InferenceWorkload,
    ModelRouteCandidate,
    ModelRouteProof,
)
from .proofs import build_model_route_proof
from .receipts import (
    CostEstimate,
    ReceiptPersistenceResult,
    RouteAttemptReceipt,
    RouteReceipt,
    safe_code,
)
from .repository import ModelFabricRepository, ProofPersistenceResult, model_fabric_repository
from .selector import preflight_candidate


@dataclass(frozen=True)
class CapabilityProbeObservation:
    """Sanitized canary result; it intentionally cannot carry model output."""

    passed: bool
    proven_value: int | str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.error_code is not None:
            safe_code(self.error_code, field_name="probe error code")
        if self.passed and self.error_code is not None:
            raise ValueError("passing capability probe cannot include an error")


ProbeTransport = Callable[
    [ModelRouteCandidate, TrustRequest, InferenceRequirements],
    Awaitable[CapabilityProbeObservation],
]


@dataclass(frozen=True)
class CapabilityProbeResult:
    proof: ModelRouteProof | None
    route_persistence: ReceiptPersistenceResult
    proof_persistence: ProofPersistenceResult | None
    outcome: str
    error_code: str | None = None


async def run_capability_probe(
    *,
    context: InferenceRequestContext,
    candidate: ModelRouteCandidate,
    capability: str,
    canary_version: str,
    proof_ttl_seconds: float,
    transport: ProbeTransport,
    repository: ModelFabricRepository = model_fabric_repository,
    now: float | None = None,
) -> CapabilityProbeResult:
    """Run one no-fallback canary and persist its sanitized receipt and proof."""
    checked_at = time.time() if now is None else float(now)
    safe_code(capability, field_name="capability")
    safe_code(canary_version, field_name="canary version")
    if context.workload is not InferenceWorkload.CAPABILITY_PROBE:
        raise ValueError("capability probe requires the capability_probe workload")
    if proof_ttl_seconds <= 0:
        raise ValueError("capability proof TTL must be positive")

    trust_request, decision_id, denial = preflight_candidate(
        context,
        candidate,
        (),
        now=checked_at,
    )
    if denial is not None or trust_request is None or decision_id is None:
        return await _persist_denied_probe(
            context=context,
            candidate=candidate,
            checked_at=checked_at,
            error_code=denial or "probe_preflight_denied",
            decision_id=decision_id,
            repository=repository,
        )

    started = time.time()
    timeout_seconds = max(context.deadline_at - started, 0.0)
    try:
        if timeout_seconds <= 0:
            raise asyncio.TimeoutError
        observation = await asyncio.wait_for(
            transport(candidate, trust_request, context.requirements),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        observation = CapabilityProbeObservation(False, error_code="probe_deadline_exceeded")
    except Exception:
        observation = CapabilityProbeObservation(False, error_code="probe_transport_failed")
    finished = time.time()
    latency_ms = max(int((finished - started) * 1000), 0)
    route_outcome = "succeeded" if observation.passed else "failed"
    receipt = _probe_receipt(
        context=context,
        candidate=candidate,
        route_decision_id=_probe_route_decision_id(context.request_id, decision_id),
        trust_decision_id=decision_id,
        attempt_id=trust_request.attempt_id,
        outcome=route_outcome,
        checked_at=started,
        finished_at=finished,
        latency_ms=latency_ms,
        error_code=observation.error_code,
        proof_hash=None,
    )
    route_persistence = await repository.persist_route_receipt(receipt)
    proof = None
    proof_persistence = None
    if route_persistence.persisted and route_persistence.receipt_hash is not None:
        proof = build_model_route_proof(
            profile=candidate.profile,
            endpoint_class=candidate.endpoint_class,
            adapter=candidate.adapter,
            capability=capability,
            canary_version=canary_version,
            outcome="passed" if observation.passed else "failed",
            checked_at=checked_at,
            expires_at=checked_at + proof_ttl_seconds,
            probe_receipt_id=route_persistence.receipt_id,
            probe_receipt_hash=route_persistence.receipt_hash,
            proven_value=observation.proven_value,
        )
        proof_persistence = await repository.persist_capability_proof(
            proof,
        )
    persisted_proof = (
        proof
        if observation.passed and proof_persistence is not None and proof_persistence.persisted
        else None
    )
    persistence_error = (
        observation.error_code
        or (
            proof_persistence.error_code
            if proof_persistence is not None and not proof_persistence.persisted
            else route_persistence.error_code
            if not route_persistence.persisted
            else None
        )
    )
    return CapabilityProbeResult(
        proof=persisted_proof,
        route_persistence=route_persistence,
        proof_persistence=proof_persistence,
        outcome="passed" if observation.passed else "failed",
        error_code=persistence_error,
    )


async def _persist_denied_probe(
    *,
    context: InferenceRequestContext,
    candidate: ModelRouteCandidate,
    checked_at: float,
    error_code: str,
    decision_id: str | None,
    repository: ModelFabricRepository,
) -> CapabilityProbeResult:
    safe_error = _reason_code(error_code)
    receipt = _probe_receipt(
        context=context,
        candidate=candidate,
        route_decision_id=_probe_route_decision_id(
            context.request_id,
            decision_id or safe_error,
        ),
        trust_decision_id=decision_id or "not-evaluated",
        attempt_id=f"attempt:{uuid4().hex}",
        outcome="denied",
        checked_at=checked_at,
        finished_at=checked_at,
        latency_ms=0,
        error_code=safe_error,
        proof_hash=None,
    )
    persistence = await repository.persist_route_receipt(receipt)
    return CapabilityProbeResult(None, persistence, None, "denied", safe_error)


def _probe_receipt(
    *,
    context: InferenceRequestContext,
    candidate: ModelRouteCandidate,
    route_decision_id: str,
    trust_decision_id: str,
    attempt_id: str,
    outcome: str,
    checked_at: float,
    finished_at: float,
    latency_ms: int,
    error_code: str | None,
    proof_hash: str | None,
) -> RouteReceipt:
    from datetime import datetime, timezone

    started = datetime.fromtimestamp(checked_at, tz=timezone.utc)
    finished = datetime.fromtimestamp(finished_at, tz=timezone.utc)
    attempt = RouteAttemptReceipt(
        attempt_id=attempt_id,
        attempt_index=0,
        profile_id=candidate.profile.id,
        model=candidate.profile.model,
        endpoint=candidate.endpoint,
        adapter=candidate.adapter,
        destination_class=candidate.endpoint_class.value,
        egress_class=context.egress_class.value,
        trust_decision_id=trust_decision_id,
        capability_proof_hashes=(proof_hash,) if proof_hash else (),
        outcome=outcome,
        error_code=error_code,
        started_at=started,
        finished_at=finished,
        latency_ms=latency_ms,
        cost=_probe_cost(candidate),
    )
    succeeded = outcome == "succeeded"
    return RouteReceipt(
        receipt_id=f"probe-{uuid4().hex}",
        request_id=context.request_id,
        route_decision_id=route_decision_id,
        runtime_path=context.runtime_path,
        workload=InferenceWorkload.CAPABILITY_PROBE.value,
        outcome=outcome,
        egress_class=context.egress_class.value,
        actual_profile_id=candidate.profile.id if succeeded else None,
        actual_model=candidate.profile.model if succeeded else None,
        actual_adapter=candidate.adapter if succeeded else None,
        destination_class=candidate.endpoint_class.value if succeeded else None,
        trust_decision_id=trust_decision_id,
        cost=_probe_cost(candidate),
        started_at=started,
        finished_at=finished,
        latency_ms=latency_ms,
        attempts=(attempt,),
    )


def _reason_code(value: str) -> str:
    normalized = value.split(":", 1)[0].replace(" ", "_")
    return safe_code(normalized, field_name="probe denial code")


def _probe_cost(candidate: ModelRouteCandidate) -> CostEstimate:
    if candidate.endpoint_class.value in {"local", "trusted_lan"}:
        return CostEstimate(kind="local_resource")
    profile = candidate.profile
    if profile.cost_microusd is not None and profile.cost_source and profile.cost_source_updated_at is not None:
        return CostEstimate(
            kind="estimated",
            amount=profile.cost_microusd / 1_000_000,
            currency="USD",
            source=profile.cost_source,
            source_updated_at=datetime.fromtimestamp(profile.cost_source_updated_at, tz=timezone.utc),
        )
    return CostEstimate()


def _probe_route_decision_id(request_id: str, decision_material: str) -> str:
    digest = hashlib.sha256(f"{request_id}:{decision_material}".encode("utf-8")).hexdigest()
    return f"route:{digest}"
