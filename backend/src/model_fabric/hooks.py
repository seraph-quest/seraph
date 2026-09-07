"""Repository-backed receipt sessions and adapter hook convenience."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from time import monotonic
from uuid import uuid4

from .contracts import EndpointClass, InferenceRequestContext, ModelRouteCandidate, RouteDecision
from .receipts import CostEstimate, ReceiptPersistenceResult, RouteAttemptReceipt, RouteReceipt, TokenUsage
from .repository import ModelFabricRepository, model_fabric_repository
from .runtime_status import publish_receipt_persistence


@dataclass(frozen=True)
class _ActiveAttempt:
    candidate: ModelRouteCandidate
    trust_decision_id: str
    route_decision_id: str
    attempt_id: str
    capability_proof_hashes: tuple[str, ...]
    started_at: datetime
    started_monotonic: float


@dataclass(frozen=True)
class _CompletedAttempt:
    candidate: ModelRouteCandidate
    receipt: RouteAttemptReceipt
    route_decision_id: str


class RouteReceiptSession:
    """Aggregate every governed attempt into one request-level final receipt."""

    def __init__(
        self,
        *,
        context: InferenceRequestContext,
        repository: ModelFabricRepository = model_fabric_repository,
        capability_proof_hashes: tuple[str, ...] = (),
        cost: CostEstimate | None = None,
    ) -> None:
        self._context = context
        self._repository = repository
        self._default_proof_hashes = capability_proof_hashes
        self._cost = cost
        self._active: _ActiveAttempt | None = None
        self._completed: list[_CompletedAttempt] = []
        self._finalized = False

    @property
    def workload(self) -> str:
        return self._context.workload.value

    @property
    def runtime_path(self) -> str:
        return self._context.runtime_path

    def attempt_started(
        self,
        decision: RouteDecision,
        *,
        capability_proof_hashes: tuple[str, ...] | None = None,
    ) -> None:
        """Begin one attempt using the exact trust-evaluated attempt identity."""
        if (
            not decision.allowed
            or decision.selected is None
            or decision.trust_decision_id is None
            or decision.attempt_id is None
            or decision.route_decision_id is None
        ):
            raise ValueError("cannot record an attempt without an allowed trust decision")
        self.start_candidate_attempt(
            candidate=decision.selected,
            attempt_id=decision.attempt_id,
            trust_decision_id=decision.trust_decision_id,
            route_decision_id=decision.route_decision_id,
            capability_proof_hashes=capability_proof_hashes,
        )

    def start_candidate_attempt(
        self,
        *,
        candidate: ModelRouteCandidate,
        attempt_id: str,
        trust_decision_id: str,
        route_decision_id: str,
        capability_proof_hashes: tuple[str, ...] | None = None,
    ) -> None:
        """Begin an attempt from a direct preflight result, including denied routes."""
        if self._finalized or self._active is not None:
            raise ValueError("route receipt session cannot start this attempt")
        self._active = _ActiveAttempt(
            candidate=candidate,
            trust_decision_id=trust_decision_id,
            route_decision_id=route_decision_id,
            attempt_id=attempt_id,
            capability_proof_hashes=(
                self._default_proof_hashes
                if capability_proof_hashes is None
                else capability_proof_hashes
            ),
            started_at=datetime.now(timezone.utc),
            started_monotonic=monotonic(),
        )

    def attempt_finished(
        self,
        *,
        outcome: str,
        error_code: str | None,
        decision: RouteDecision | None = None,
        usage: TokenUsage | None = None,
        degradation_code: str | None = None,
    ) -> None:
        active = self._active
        if active is None:
            raise ValueError("route attempt completion has no active attempt")
        if decision is not None and (
            decision.selected != active.candidate
            or decision.attempt_id != active.attempt_id
            or decision.trust_decision_id != active.trust_decision_id
            or decision.route_decision_id != active.route_decision_id
        ):
            raise ValueError("route attempt completion does not match the evaluated trust request")
        finished_at = datetime.now(timezone.utc)
        latency_ms = max(int((monotonic() - active.started_monotonic) * 1000), 0)
        receipt = RouteAttemptReceipt(
            attempt_id=active.attempt_id,
            attempt_index=len(self._completed),
            profile_id=active.candidate.profile.id,
            model=active.candidate.profile.model,
            endpoint=active.candidate.endpoint,
            adapter=active.candidate.adapter,
            destination_class=active.candidate.endpoint_class.value,
            egress_class=self._context.egress_class.value,
            trust_decision_id=active.trust_decision_id,
            capability_proof_hashes=active.capability_proof_hashes,
            outcome=outcome,
            error_code=error_code,
            usage=usage or TokenUsage(),
            cost=_candidate_cost(active.candidate),
            degradation_code=degradation_code,
            started_at=active.started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
        )
        self._completed.append(
            _CompletedAttempt(active.candidate, receipt, active.route_decision_id)
        )
        self._active = None

    async def finalize(
        self,
        *,
        outcome: str,
        fallback_reason_code: str | None = None,
        degradation_codes: tuple[str, ...] = (),
    ) -> ReceiptPersistenceResult:
        """Persist one final receipt for the complete primary/fallback attempt chain."""
        if self._finalized or self._active is not None or not self._completed:
            raise ValueError("route receipt session is not ready to finalize")
        successful = [item for item in self._completed if item.receipt.outcome == "succeeded"]
        if outcome == "succeeded" and not successful:
            raise ValueError("successful final receipt requires a successful attempt")
        actual = successful[-1] if outcome == "succeeded" else None
        first = self._completed[0].receipt
        last = self._completed[-1].receipt
        actual_candidate = actual.candidate if actual is not None else None
        actual_attempt = actual.receipt if actual is not None else None
        fallback_used = len(self._completed) > 1 or bool(
            actual_candidate is not None and actual_candidate.source != "primary"
        )
        route_decision_id = (
            actual.route_decision_id if actual is not None else self._completed[-1].route_decision_id
        )
        receipt = RouteReceipt(
            receipt_id=f"route-{uuid4().hex}",
            request_id=self._context.request_id,
            route_decision_id=route_decision_id,
            runtime_path=self._context.runtime_path,
            workload=self._context.workload.value,
            outcome=outcome,
            egress_class=self._context.egress_class.value,
            actual_profile_id=actual_candidate.profile.id if actual_candidate is not None else None,
            actual_model=actual_candidate.profile.model if actual_candidate is not None else None,
            actual_adapter=actual_candidate.adapter if actual_candidate is not None else None,
            destination_class=actual_candidate.endpoint_class.value if actual_candidate is not None else None,
            trust_decision_id=actual_attempt.trust_decision_id if actual_attempt is not None else None,
            fallback_used=fallback_used,
            fallback_reason_code=fallback_reason_code,
            degradation_codes=degradation_codes,
            cost=self._cost or _candidate_cost(actual_candidate),
            usage=_aggregate_usage(tuple(item.receipt for item in self._completed)),
            started_at=first.started_at,
            finished_at=last.finished_at,
            latency_ms=max(int((last.finished_at - first.started_at).total_seconds() * 1000), 0),
            attempts=tuple(item.receipt for item in self._completed),
        )
        result = await self._repository.persist_route_receipt(receipt)
        publish_receipt_persistence(
            runtime_path=self._context.runtime_path,
            status=result.status,
            error_code=result.error_code,
            receipt_id=result.receipt_id,
        )
        self._finalized = True
        return result

    async def finalize_denied(
        self,
        *,
        decision: RouteDecision | None,
        reason_codes: tuple[str, ...],
        fallback_reason_code: str = "no_compliant_route",
    ) -> ReceiptPersistenceResult:
        """Persist a zero-attempt denial when admission never starts an attempt."""
        if self._finalized or self._active is not None or self._completed:
            raise ValueError("route receipt session is not ready to finalize as denied")
        result = await persist_denied_route(
            context=self._context,
            decision=decision,
            reason_codes=reason_codes,
            fallback_reason_code=fallback_reason_code,
            repository=self._repository,
        )
        self._finalized = True
        return result


class PersistedRouteReceiptHooks:
    """Single-attempt convenience implementing the execution RouteReceiptHooks protocol."""

    def __init__(
        self,
        *,
        repository: ModelFabricRepository = model_fabric_repository,
        capability_proof_hashes: tuple[str, ...] = (),
        cost: CostEstimate | None = None,
    ) -> None:
        self._repository = repository
        self._proof_hashes = capability_proof_hashes
        self._cost = cost
        self._sessions: dict[str, RouteReceiptSession] = {}
        self._results: dict[str, ReceiptPersistenceResult] = {}
        self._lock = asyncio.Lock()

    async def attempt_started(self, *, context: InferenceRequestContext, decision: RouteDecision) -> None:
        session = RouteReceiptSession(
            context=context,
            repository=self._repository,
            capability_proof_hashes=self._proof_hashes,
            cost=self._cost,
        )
        session.attempt_started(
            decision,
            capability_proof_hashes=self._proof_hashes,
        )
        async with self._lock:
            if context.request_id in self._sessions:
                raise ValueError("route receipt session already exists for this request")
            self._sessions[context.request_id] = session

    async def attempt_finished(
        self,
        *,
        context: InferenceRequestContext,
        decision: RouteDecision,
        outcome: str,
        error_code: str | None,
    ) -> None:
        async with self._lock:
            session = self._sessions.pop(context.request_id, None)
        if session is None:
            raise ValueError("route receipt session is missing for this request")
        session.attempt_finished(outcome=outcome, error_code=error_code, decision=decision)
        result = await session.finalize(outcome=outcome)
        async with self._lock:
            self._results[context.request_id] = result

    async def persistence_result(self, request_id: str) -> ReceiptPersistenceResult | None:
        async with self._lock:
            return self._results.get(request_id)


def _default_cost(endpoint_class: EndpointClass | None) -> CostEstimate:
    if endpoint_class in {EndpointClass.LOCAL, EndpointClass.TRUSTED_LAN}:
        return CostEstimate(kind="local_resource")
    return CostEstimate()


def _candidate_cost(candidate: ModelRouteCandidate | None) -> CostEstimate:
    if candidate is None:
        return CostEstimate()
    if candidate.endpoint_class in {EndpointClass.LOCAL, EndpointClass.TRUSTED_LAN}:
        return CostEstimate(kind="local_resource")
    profile = candidate.profile
    if (
        profile.cost_microusd is not None
        and profile.cost_source
        and profile.cost_source_updated_at is not None
    ):
        return CostEstimate(
            kind="estimated",
            amount=profile.cost_microusd / 1_000_000,
            currency="USD",
            source=profile.cost_source,
            source_updated_at=datetime.fromtimestamp(profile.cost_source_updated_at, tz=timezone.utc),
        )
    return CostEstimate()


def _aggregate_usage(attempts: tuple[RouteAttemptReceipt, ...]) -> TokenUsage:
    def total(field_name: str) -> int | None:
        values = [getattr(attempt.usage, field_name) for attempt in attempts]
        present = [value for value in values if value is not None]
        return sum(present) if present else None

    return TokenUsage(
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        total_tokens=total("total_tokens"),
    )


async def persist_denied_route(
    *,
    context: InferenceRequestContext,
    decision: RouteDecision | None,
    reason_codes: tuple[str, ...],
    fallback_reason_code: str = "no_compliant_route",
    repository: ModelFabricRepository = model_fabric_repository,
) -> ReceiptPersistenceResult:
    """Persist an operator-visible, zero-transport receipt for unroutable work."""
    now = datetime.now(timezone.utc)
    safe_reasons = tuple(dict.fromkeys(reason_codes or ("no_compliant_route",)))
    material = ":".join((context.request_id, *safe_reasons))
    route_decision_id = (
        decision.route_decision_id
        if decision is not None and decision.route_decision_id
        else f"route:{hashlib.sha256(material.encode()).hexdigest()}"
    )
    receipt = RouteReceipt(
        receipt_id=f"route-{uuid4().hex}",
        request_id=context.request_id,
        route_decision_id=route_decision_id,
        runtime_path=context.runtime_path,
        workload=context.workload.value,
        outcome="denied",
        egress_class=context.egress_class.value,
        started_at=now,
        finished_at=now,
        latency_ms=0,
        attempts=(),
        fallback_reason_code=fallback_reason_code,
        degradation_codes=safe_reasons,
        cost=CostEstimate(),
        usage=TokenUsage(),
    )
    result = await repository.persist_route_receipt(receipt)
    publish_receipt_persistence(
        runtime_path=context.runtime_path,
        status=result.status,
        error_code=result.error_code or "no_compliant_route",
        receipt_id=result.receipt_id,
    )
    return result
