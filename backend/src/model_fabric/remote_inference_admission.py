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

import hashlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

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
    GpuAdmissionOwnerRevokedError,
    GpuAdmissionReceipt,
    GpuAdmissionRequest,
    GpuAdmissionUncertainError,
    GpuPriority,
    GPU_OWNER_REVOCATION_REASON,
    priority_for_inference_context,
)


REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION = "seraph.remote-inference-admission.v1"
REMOTE_INFERENCE_RESOURCE_CLASS = "remote_inference"
REMOTE_INFERENCE_DEFAULT_QUEUE = 64
REMOTE_INFERENCE_DEFAULT_OWNER_OUTSTANDING = 16
REMOTE_INFERENCE_DEFAULT_MAX_INFLIGHT = 1
REMOTE_INFERENCE_DEFAULT_RETRIES = 2
REMOTE_INFERENCE_MAX_RETRIES = 2
REMOTE_INFERENCE_OWNER_COST_UNKNOWN_REASON = "owner_cost_unknown"
REMOTE_INFERENCE_OWNER_REVOCATION_REASON = GPU_OWNER_REVOCATION_REASON


class RemoteInferenceReceiptRepository(Protocol):
    """Canonical-job extension point for an already admitted remote request.

    The broker remains process-local.  A caller that already owns a durable
    ``WorkflowRunState`` row may explicitly forward a broker receipt to the
    canonical job repository through this narrow protocol.  The adapter must
    persist the supplied operator-safe mapping without creating another queue
    or changing the job's lifecycle on the broker's behalf.
    """

    async def record_remote_inference_receipt(
        self,
        receipt: Mapping[str, object],
        *,
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> Mapping[str, object]: ...

    async def record_remote_inference_intent(
        self,
        *,
        operation_id: str,
        job_id: str,
        owner_id: str,
        parent_job_id: str | None = None,
        runtime_path: str = "",
        profile_id: str = "",
        priority: str = "",
        deadline_at: float | None = None,
        capability_version: str = "",
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> Mapping[str, object]: ...


class RemoteInferenceReceiptPersistenceError(GpuAdmissionError):
    """Durable receipt adoption failed after a remote operation was admitted.

    This remains an admission error so the shared fallback loops treat the
    remote outcome as terminal for this caller. Retrying another provider
    after the canonical job fence or receipt sink failed could spend twice.
    """

    code = "durable_receipt_persistence_failed"


class RemoteInferenceBindingError(GpuAdmissionIdentityError):
    """A durable remote operation cannot be safely admitted or replayed."""

    code = "durable_remote_binding_invalid"


@dataclass(frozen=True, slots=True)
class RemoteInferenceReceiptBinding:
    """Durable caller fence carried across one governed inference call.

    The binding is deliberately separate from ``InferenceRequestContext``:
    repository objects and durable lease credentials are execution plumbing,
    not model trust payload.  Callers bind it around a request that already
    owns a canonical durable job row; the broker never creates that row.
    """

    repository: RemoteInferenceReceiptRepository
    owner: str | None = None
    fencing_token: int | None = None
    job_id: str | None = None


_current_receipt_binding: ContextVar[RemoteInferenceReceiptBinding | None] = ContextVar(
    "remote_inference_receipt_binding",
    default=None,
)


def set_remote_inference_receipt_binding(
    binding: RemoteInferenceReceiptBinding | None,
) -> Token[RemoteInferenceReceiptBinding | None]:
    """Bind one canonical-job receipt sink to the current execution context."""
    return _current_receipt_binding.set(binding)


def reset_remote_inference_receipt_binding(
    token: Token[RemoteInferenceReceiptBinding | None],
) -> None:
    """Restore the previous durable receipt sink after an inference call."""
    _current_receipt_binding.reset(token)


def current_remote_inference_receipt_binding() -> RemoteInferenceReceiptBinding | None:
    """Return the caller-owned durable receipt sink, if one is bound."""
    return _current_receipt_binding.get()


@contextmanager
def bind_remote_inference_receipt(
    *,
    repository: RemoteInferenceReceiptRepository,
    job_id: str,
    owner: str,
    fencing_token: int,
) -> Iterator[RemoteInferenceReceiptBinding]:
    """Bind one existing durable job lease around a remote inference call.

    This helper is the only supported way for a durable caller to establish
    the receipt context.  The broker still owns the process-local admission
    lease; the supplied job, runner, and fence belong to ``WorkflowRunState``.
    Validation happens before any route or transport work can run, and the
    context is restored even when a provider or persistence operation fails.
    """
    if repository is None:
        raise ValueError("durable remote inference requires a receipt repository")
    normalized_job = str(job_id or "").strip()
    normalized_owner = str(owner or "").strip()
    if not normalized_job or len(normalized_job) > 256:
        raise ValueError("durable remote inference requires a bounded job id")
    if not normalized_owner or len(normalized_owner) > 256:
        raise ValueError("durable remote inference requires a bounded lease owner")
    try:
        normalized_fence = int(fencing_token)
    except (TypeError, ValueError) as exc:
        raise ValueError("durable remote inference requires a positive fencing token") from exc
    if normalized_fence <= 0:
        raise ValueError("durable remote inference requires a positive fencing token")
    binding = RemoteInferenceReceiptBinding(
        repository=repository,
        owner=normalized_owner,
        fencing_token=normalized_fence,
        job_id=normalized_job,
    )
    token = set_remote_inference_receipt_binding(binding)
    try:
        yield binding
    finally:
        reset_remote_inference_receipt_binding(token)


def stable_remote_inference_operation_id(
    context: Any,
    *,
    profile_id: str,
    fallback: str,
) -> str:
    """Return a durable operation identity when a caller owns a durable job.

    Route decision attempt IDs intentionally contain fresh trust-attempt
    entropy.  A durable job needs a stable operation key so a restarted
    worker cannot dispatch the same remote effect under a new broker identity.
    The durable job is the identity boundary; route/profile metadata is
    persisted in the intent and a drifted resume is rejected there.  This
    keeps the operation key stable across a route/profile refresh while still
    preventing a second provider dispatch.
    """
    binding = current_remote_inference_receipt_binding()
    durable_job_id = str(getattr(binding, "job_id", "") or "").strip()
    if not durable_job_id:
        return str(fallback or "").strip()
    del profile_id
    operation_id = f"remote:{durable_job_id}"
    if len(operation_id) <= 256:
        return operation_id
    # Binding IDs are bounded, but preserve collision resistance if a caller
    # supplies one near the upper limit.
    return "remote:" + hashlib.sha256(durable_job_id.encode("utf-8")).hexdigest()


async def prepare_bound_remote_inference(
    request: GpuAdmissionRequest,
    *,
    profile_id: str | None = None,
) -> None:
    """Fence a durable operation before the shared broker can dispatch it.

    The durable repository records an intent under the same operation ID.  A
    repeated operation, stale fence, missing job, or persistence failure is a
    typed identity denial; callers must not fall through to another provider.
    Legacy receipt bindings without ``job_id`` remain compatible with the
    earlier post-dispatch projection seam.
    """
    binding = current_remote_inference_receipt_binding()
    if binding is None or binding.job_id is None:
        return
    if binding.job_id != request.job_id:
        raise RemoteInferenceBindingError(
            "durable remote inference job does not match the immutable request"
        )
    recorder = getattr(binding.repository, "record_remote_inference_intent", None)
    if not callable(recorder):
        raise RemoteInferenceBindingError(
            "durable remote inference repository lacks the intent fence"
        )
    try:
        await recorder(
            operation_id=request.operation_id,
            job_id=request.job_id,
            owner_id=request.owner_id,
            parent_job_id=request.parent_job_id,
            runtime_path=request.runtime_path,
            profile_id=str(profile_id or "").strip(),
            priority=request.priority.value,
            deadline_at=request.deadline_at,
            capability_version=request.capability_version,
            owner=binding.owner,
            fencing_token=binding.fencing_token,
        )
    except RemoteInferenceBindingError:
        raise
    except Exception as exc:
        # Do not leak a repository/provider payload into fallback logs.  The
        # durable repository has either committed the intent or rejected it;
        # in both cases the remote callback must not be attempted here.
        raise RemoteInferenceBindingError(
            "durable remote inference intent could not be fenced"
        ) from exc


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
        max_retries: int = REMOTE_INFERENCE_DEFAULT_RETRIES,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            max_queued=max_queued,
            max_outstanding_per_owner=max_outstanding_per_owner,
            max_owner_cost_microusd=max_owner_cost_microusd,
            **kwargs,
        )
        self._owner_budget_resolver = owner_budget_resolver
        self.max_inflight = REMOTE_INFERENCE_DEFAULT_MAX_INFLIGHT
        self.max_retries = self._validate_retry_limit(max_retries)

    @staticmethod
    def _validate_retry_limit(value: int) -> int:
        normalized = int(value)
        if not 0 <= normalized <= REMOTE_INFERENCE_MAX_RETRIES:
            raise ValueError("max_retries must be between 0 and 2")
        return normalized

    def configure_policy(
        self,
        *,
        max_queued: int = REMOTE_INFERENCE_DEFAULT_QUEUE,
        max_inflight: int = REMOTE_INFERENCE_DEFAULT_MAX_INFLIGHT,
        max_outstanding_per_owner: int | None = REMOTE_INFERENCE_DEFAULT_OWNER_OUTSTANDING,
        max_owner_cost_microusd: int | None = None,
        max_retries: int = REMOTE_INFERENCE_DEFAULT_RETRIES,
    ) -> None:
        """Apply persisted OpenRouter limits to the process-local lane."""
        if int(max_inflight) != REMOTE_INFERENCE_DEFAULT_MAX_INFLIGHT:
            raise ValueError("remote inference admission supports exactly one in-flight request")
        normalized_queue = int(max_queued)
        normalized_owner = (
            None if max_outstanding_per_owner is None else int(max_outstanding_per_owner)
        )
        normalized_budget = (
            None if max_owner_cost_microusd is None else int(max_owner_cost_microusd)
        )
        if normalized_queue < 1:
            raise ValueError("max_queued must be positive")
        if normalized_owner is not None and normalized_owner < 1:
            raise ValueError("max_outstanding_per_owner must be positive")
        if normalized_budget is not None and normalized_budget < 0:
            raise ValueError("max_owner_cost_microusd must be non-negative")
        normalized_retries = self._validate_retry_limit(max_retries)
        with self._condition:
            self.max_queued = normalized_queue
            self.max_inflight = REMOTE_INFERENCE_DEFAULT_MAX_INFLIGHT
            self.max_outstanding_per_owner = normalized_owner
            self.max_owner_cost_microusd = normalized_budget
            self.max_retries = normalized_retries
            self._notify_all_locked()

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

    async def persist_receipt(
        self,
        receipt: RemoteInferenceAdmissionReceipt,
        *,
        repository: RemoteInferenceReceiptRepository,
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> Mapping[str, object]:
        """Forward one redacted broker receipt to the canonical job runtime.

        Persistence is explicit because many inference requests are short
        lived request jobs without a ``WorkflowRunState`` row.  This method
        never creates one, never dispatches work, and never treats the remote
        broker fencing token as the durable job lease token.  A caller running
        under a durable job lease may pass that separate ``owner`` and
        ``fencing_token`` pair to preserve the repository's existing fence.
        """
        if receipt.resource_class != REMOTE_INFERENCE_RESOURCE_CLASS:
            raise ValueError("receipt does not belong to the remote inference resource class")
        if receipt.schema_version != REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION:
            raise ValueError("receipt schema version is not supported by the remote adapter")
        try:
            return await repository.record_remote_inference_receipt(
                receipt.as_dict(),
                owner=owner,
                fencing_token=fencing_token,
            )
        except Exception as error:
            raise RemoteInferenceReceiptPersistenceError(
                "remote inference receipt could not be adopted by the durable job",
                receipt=receipt,
            ) from error

    async def cancel_owner(
        self,
        owner_id: str,
        *,
        reason_code: str = REMOTE_INFERENCE_OWNER_REVOCATION_REASON,
    ) -> tuple[RemoteInferenceAdmissionReceipt, ...]:
        """Revoke one owner's queued and active remote inference work.

        Queued operations are terminally cancelled before a provider callback
        can run.  An active operation is only marked for cooperative
        cancellation; because this broker treats callback outcomes as
        uncertain, its lease remains blocked until the existing fenced
        ``reconcile`` method settles the actual cost.  The operation is
        intentionally scoped by owner identity and repeated revocations are
        idempotent for the same reason code.
        """
        return await self._cancel_owner_operations(
            owner_id,
            reason_code=reason_code,
        )

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
RemoteInferenceAdmissionOwnerRevokedError = GpuAdmissionOwnerRevokedError
RemoteInferenceAdmissionReceipt = GpuAdmissionReceipt
RemoteInferenceAdmissionRequest = GpuAdmissionRequest
RemoteInferenceAdmissionUncertainError = GpuAdmissionUncertainError
RemoteInferencePriority = GpuPriority

remote_inference_admission_broker: RemoteInferenceAdmissionBroker = RemoteInferenceAdmissionBroker()


def configure_remote_inference_admission(
    *,
    max_queued: int = REMOTE_INFERENCE_DEFAULT_QUEUE,
    max_inflight: int = REMOTE_INFERENCE_DEFAULT_MAX_INFLIGHT,
    max_outstanding_per_owner: int | None = REMOTE_INFERENCE_DEFAULT_OWNER_OUTSTANDING,
    max_owner_cost_microusd: int | None = None,
    max_retries: int = REMOTE_INFERENCE_DEFAULT_RETRIES,
) -> None:
    """Apply canonical setup limits to the process-local remote lane."""
    remote_inference_admission_broker.configure_policy(
        max_queued=max_queued,
        max_inflight=max_inflight,
        max_outstanding_per_owner=max_outstanding_per_owner,
        max_owner_cost_microusd=max_owner_cost_microusd,
        max_retries=max_retries,
    )


__all__ = [
    "REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION",
    "REMOTE_INFERENCE_RESOURCE_CLASS",
    "REMOTE_INFERENCE_DEFAULT_QUEUE",
    "REMOTE_INFERENCE_DEFAULT_OWNER_OUTSTANDING",
    "REMOTE_INFERENCE_DEFAULT_MAX_INFLIGHT",
    "REMOTE_INFERENCE_DEFAULT_RETRIES",
    "REMOTE_INFERENCE_MAX_RETRIES",
    "REMOTE_INFERENCE_OWNER_COST_UNKNOWN_REASON",
    "REMOTE_INFERENCE_OWNER_REVOCATION_REASON",
    "GPU_OWNER_REVOCATION_REASON",
    "RemoteInferenceReceiptRepository",
    "RemoteInferenceReceiptPersistenceError",
    "RemoteInferenceBindingError",
    "RemoteInferenceReceiptBinding",
    "bind_remote_inference_receipt",
    "stable_remote_inference_operation_id",
    "prepare_bound_remote_inference",
    "set_remote_inference_receipt_binding",
    "reset_remote_inference_receipt_binding",
    "current_remote_inference_receipt_binding",
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
    "RemoteInferenceAdmissionOwnerRevokedError",
    "RemoteInferenceAdmissionReceipt",
    "RemoteInferenceAdmissionRequest",
    "RemoteInferenceAdmissionUncertainError",
    "RemoteInferencePriority",
    "remote_inference_admission_broker",
    "configure_remote_inference_admission",
    "priority_for_inference_context",
]
