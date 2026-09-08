"""Bounded admission for the OpenRouter remote-inference lane.

This is the canonical admission surface for active text, vision, audio, and
embedding inference. It owns only the process-local serial lease; durable
workflow state remains the #743 job record. The module deliberately contains
no CUDA, VLM-wrapper, model-server, or network calls.

The request's budget and estimate are policy metadata supplied by the trusted
Seraph caller after route selection. Model payloads never enter this broker,
and a stricter broker-wide limit always wins over a request-level limit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn

from .gpu_admission import (
    GPU_ADMISSION_SCHEMA_VERSION,
    GPU_ADMISSION_STATUSES,
    GPU_OWNER_BUDGET_REASON,
    GPU_OWNER_CAPACITY_REASON,
    GpuAdmissionBroker,
    GpuAdmissionCapacityError,
    GpuAdmissionCancelledError,
    GpuAdmissionError,
    GpuAdmissionExpiredError,
    GpuAdmissionIdentityError,
    GpuAdmissionLease,
    GpuAdmissionLeaseError,
    GpuAdmissionOwnerBudgetError,
    GpuAdmissionOwnerCapacityError,
    GpuAdmissionReceipt,
    GpuAdmissionRequest,
    GpuAdmissionUncertainError,
    GpuPriority,
    priority_for_inference_context,
)


REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION = "seraph.remote-inference-admission.v1"
REMOTE_INFERENCE_RESOURCE_CLASS = "remote_inference"
REMOTE_INFERENCE_DEFAULT_QUEUE = 64
REMOTE_INFERENCE_DEFAULT_OWNER_OUTSTANDING = 16
REMOTE_INFERENCE_OWNER_COST_UNKNOWN_REASON = "owner_cost_unknown"


class RemoteInferenceAdmissionBroker(GpuAdmissionBroker[Any]):
    """One bounded remote slot with owner-scoped admission reservations."""

    admission_schema_version = REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION
    resource_class = REMOTE_INFERENCE_RESOURCE_CLASS
    serial_gpu = False
    serial_remote_inference = True
    uncertain_on_callback_error = True

    def __init__(
        self,
        *,
        max_queued: int = REMOTE_INFERENCE_DEFAULT_QUEUE,
        max_outstanding_per_owner: int | None = REMOTE_INFERENCE_DEFAULT_OWNER_OUTSTANDING,
        max_owner_cost_microusd: int | None = None,
        owner_budget_resolver: Callable[[str], int | None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            max_queued=max_queued,
            max_outstanding_per_owner=max_outstanding_per_owner,
            max_owner_cost_microusd=max_owner_cost_microusd,
            **kwargs,
        )
        self._owner_budget_resolver = owner_budget_resolver

    def _effective_owner_budget(self, request: GpuAdmissionRequest) -> int | None:
        """Use only a server-side broker/resolver budget as authority.

        ``request.owner_budget_microusd`` can further narrow that authority,
        but cannot create or widen a spend allowance when the trusted policy
        source has not configured one.
        """
        configured = self.max_owner_cost_microusd
        if self._owner_budget_resolver is not None:
            resolved = self._owner_budget_resolver(request.owner_id)
            if resolved is not None:
                resolved = int(resolved)
                if resolved < 0:
                    raise ValueError("owner budget resolver returned a negative budget")
                configured = resolved if configured is None else min(configured, resolved)
        if configured is None:
            return None
        requested = request.owner_budget_microusd
        return configured if requested is None else min(configured, requested)

    def _reject_owner_policy(
        self,
        request: GpuAdmissionRequest,
        *,
        observed_at: float,
        reason_code: str,
        message: str,
        budget_error: bool = False,
    ) -> NoReturn:
        operation = self._record_terminal_locked(
            request,
            status="rejected",
            reason_code=reason_code,
            observed_at=observed_at,
        )
        self._last_degraded_reason = reason_code
        receipt = self._receipt_locked(operation)
        error_type = GpuAdmissionOwnerBudgetError if budget_error else GpuAdmissionOwnerCapacityError
        raise error_type(message, receipt=receipt)

    def _validate_admission_locked(
        self,
        request: GpuAdmissionRequest,
        observed_at: float,
    ) -> None:
        """Reserve owner capacity before a request can reach a provider callback.

        This hook runs while the base broker condition is held. The usage
        snapshot and terminal rejection are therefore atomic with operation
        identity admission. Unknown estimates are allowed only when no owner
        spend budget is configured; the receipt exposes the outstanding
        liability so an operator can reconcile it before setting a budget.
        """
        usage = self._owner_usage_locked().get(request.owner_id, {})
        outstanding = int(usage.get("outstanding", 0))
        if self.max_outstanding_per_owner is not None and outstanding >= self.max_outstanding_per_owner:
            self._reject_owner_policy(
                request,
                observed_at=observed_at,
                reason_code=GPU_OWNER_CAPACITY_REASON,
                message="remote inference owner outstanding limit is exhausted",
            )

        budget = self._effective_owner_budget(request)
        estimate = request.estimated_cost_microusd
        if budget is None:
            return
        if estimate is None:
            self._reject_owner_policy(
                request,
                observed_at=observed_at,
                reason_code=REMOTE_INFERENCE_OWNER_COST_UNKNOWN_REASON,
                message="remote inference cost estimate is required for the configured owner budget",
                budget_error=True,
            )
        reserved = int(usage.get("cost_reserved_microusd", 0))
        if reserved + estimate > budget:
            self._reject_owner_policy(
                request,
                observed_at=observed_at,
                reason_code=GPU_OWNER_BUDGET_REASON,
                message="remote inference owner spend budget is exhausted",
                budget_error=True,
            )


# Compatibility aliases retain the original names while making this separate
# instance the only active broker used by model-fabric execution paths.
RemoteInferenceAdmissionCapacityError = GpuAdmissionCapacityError
RemoteInferenceAdmissionCancelledError = GpuAdmissionCancelledError
RemoteInferenceAdmissionError = GpuAdmissionError
RemoteInferenceAdmissionExpiredError = GpuAdmissionExpiredError
RemoteInferenceAdmissionIdentityError = GpuAdmissionIdentityError
RemoteInferenceAdmissionLease = GpuAdmissionLease
RemoteInferenceAdmissionLeaseError = GpuAdmissionLeaseError
RemoteInferenceAdmissionOwnerBudgetError = GpuAdmissionOwnerBudgetError
RemoteInferenceAdmissionOwnerCapacityError = GpuAdmissionOwnerCapacityError
RemoteInferenceAdmissionReceipt = GpuAdmissionReceipt
RemoteInferenceAdmissionRequest = GpuAdmissionRequest
RemoteInferenceAdmissionUncertainError = GpuAdmissionUncertainError
RemoteInferencePriority = GpuPriority

remote_inference_admission_broker: RemoteInferenceAdmissionBroker = RemoteInferenceAdmissionBroker()


__all__ = [
    "REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION",
    "REMOTE_INFERENCE_RESOURCE_CLASS",
    "REMOTE_INFERENCE_DEFAULT_QUEUE",
    "REMOTE_INFERENCE_DEFAULT_OWNER_OUTSTANDING",
    "REMOTE_INFERENCE_OWNER_COST_UNKNOWN_REASON",
    "GPU_ADMISSION_SCHEMA_VERSION",
    "GPU_ADMISSION_STATUSES",
    "RemoteInferenceAdmissionBroker",
    "RemoteInferenceAdmissionCapacityError",
    "RemoteInferenceAdmissionCancelledError",
    "RemoteInferenceAdmissionError",
    "RemoteInferenceAdmissionExpiredError",
    "RemoteInferenceAdmissionIdentityError",
    "RemoteInferenceAdmissionLease",
    "RemoteInferenceAdmissionLeaseError",
    "RemoteInferenceAdmissionOwnerBudgetError",
    "RemoteInferenceAdmissionOwnerCapacityError",
    "RemoteInferenceAdmissionReceipt",
    "RemoteInferenceAdmissionRequest",
    "RemoteInferenceAdmissionUncertainError",
    "RemoteInferencePriority",
    "remote_inference_admission_broker",
    "priority_for_inference_context",
]
