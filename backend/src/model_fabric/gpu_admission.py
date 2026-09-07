"""One-GPU admission contract for governed model-fabric execution.

The broker owns only admission and the process-local serial execution lease.
Durable workflow state remains the canonical job record when a caller has one;
this module does not create a second durable queue or scheduler.  A caller
must provide stable operation, job, and owner identities.  The model-fabric
execution seam uses the durable job identity from its inference context when
available and the request identity otherwise.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
from typing import Any, Generic, TypeVar


class GpuPriority(str, Enum):
    """The only priority classes admitted to the Seraph GPU lane."""

    INTERACTIVE_CHAT = "interactive_chat"
    APPROVED_OPERATOR = "approved_operator"
    ACCEPTED_SCHEDULED_GOAL = "accepted_scheduled_goal"
    REPORTS_RESEARCH_MEMORY = "reports_research_memory"
    SCREENSHOT_BACKGROUND = "screenshot_background"

    @property
    def rank(self) -> int:
        return {
            GpuPriority.INTERACTIVE_CHAT: 5,
            GpuPriority.APPROVED_OPERATOR: 4,
            GpuPriority.ACCEPTED_SCHEDULED_GOAL: 3,
            GpuPriority.REPORTS_RESEARCH_MEMORY: 2,
            GpuPriority.SCREENSHOT_BACKGROUND: 1,
        }[self]

    @classmethod
    def coerce(cls, value: "GpuPriority | str") -> "GpuPriority":
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "interactive": cls.INTERACTIVE_CHAT,
            "chat": cls.INTERACTIVE_CHAT,
            "onboarding": cls.INTERACTIVE_CHAT,
            "high": cls.ACCEPTED_SCHEDULED_GOAL,
            "normal": cls.REPORTS_RESEARCH_MEMORY,
            "scheduled": cls.ACCEPTED_SCHEDULED_GOAL,
            "scheduled_goal": cls.ACCEPTED_SCHEDULED_GOAL,
            "report": cls.REPORTS_RESEARCH_MEMORY,
            "research": cls.REPORTS_RESEARCH_MEMORY,
            "memory": cls.REPORTS_RESEARCH_MEMORY,
            "screenshot": cls.SCREENSHOT_BACKGROUND,
            "background": cls.SCREENSHOT_BACKGROUND,
        }
        try:
            return cls(normalized)
        except ValueError:
            try:
                return aliases[normalized]
            except KeyError as exc:
                raise ValueError(f"unknown GPU priority class: {value!r}") from exc


GPU_ADMISSION_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "expired", "rejected"}
)
GPU_ADMISSION_SCHEMA_VERSION = "seraph.gpu-admission.v1"


class GpuAdmissionError(RuntimeError):
    """Base error for bounded GPU admission failures."""

    code = "gpu_admission_failed"

    def __init__(self, message: str, *, receipt: "GpuAdmissionReceipt") -> None:
        super().__init__(message)
        self.receipt = receipt


class GpuAdmissionCapacityError(GpuAdmissionError):
    code = "capacity_exhausted"


class GpuAdmissionExpiredError(GpuAdmissionError):
    code = "deadline_expired"


class GpuAdmissionCancelledError(GpuAdmissionError):
    code = "cancelled"


class GpuAdmissionLeaseError(GpuAdmissionError):
    code = "stale_owner_or_fencing_token"


class GpuAdmissionIdentityError(ValueError):
    code = "identity_conflict"


@dataclass(frozen=True, slots=True)
class GpuAdmissionRequest:
    """Typed metadata needed before any GPU provider callback can run."""

    operation_id: str
    job_id: str
    owner_id: str
    priority: GpuPriority
    deadline_at: float
    parent_job_id: str | None = None
    runtime_path: str = ""

    def __post_init__(self) -> None:
        for field_name in ("operation_id", "job_id", "owner_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value or len(value) > 256:
                raise ValueError(f"{field_name} is required and must be <= 256 characters")
            object.__setattr__(self, field_name, value)
        if self.parent_job_id is not None:
            parent = str(self.parent_job_id).strip()
            if not parent or len(parent) > 256:
                raise ValueError("parent_job_id must be empty or <= 256 characters")
            object.__setattr__(self, "parent_job_id", parent)
        object.__setattr__(self, "priority", GpuPriority.coerce(self.priority))
        deadline = float(self.deadline_at)
        if not math.isfinite(deadline):
            raise ValueError("deadline_at must be a finite Unix timestamp")
        object.__setattr__(self, "deadline_at", deadline)
        runtime_path = str(self.runtime_path or "").strip()
        if len(runtime_path) > 128:
            raise ValueError("runtime_path must be <= 128 characters")
        object.__setattr__(self, "runtime_path", runtime_path)

    @classmethod
    def from_inference_context(
        cls,
        context: Any,
        *,
        operation_id: str | None = None,
        parent_job_id: str | None = None,
        priority: GpuPriority | str | None = None,
    ) -> "GpuAdmissionRequest":
        """Derive a stable request without copying model payloads into the queue."""
        principal = getattr(context, "principal", None)
        owner_id = str(getattr(principal, "principal_id", "") or "").strip()
        if not owner_id:
            raise ValueError("GPU admission requires an authenticated owner identity")
        request_id = str(getattr(context, "request_id", "") or "").strip()
        if not request_id:
            raise ValueError("GPU admission requires a stable request identity")
        job_id = str(getattr(context, "job_id", "") or "").strip() or request_id
        resolved_operation_id = str(operation_id or request_id).strip()
        resolved_parent = parent_job_id
        if resolved_parent is None:
            candidate_parent = str(getattr(context, "job_id", "") or "").strip()
            resolved_parent = candidate_parent if candidate_parent and candidate_parent != job_id else None
        return cls(
            operation_id=resolved_operation_id,
            job_id=job_id,
            owner_id=owner_id,
            parent_job_id=resolved_parent,
            priority=priority or priority_for_inference_context(context),
            deadline_at=float(getattr(context, "deadline_at")),
            runtime_path=str(getattr(context, "runtime_path", "") or ""),
        )


def priority_for_inference_context(context: Any) -> GpuPriority:
    """Map existing model-fabric workload metadata into the broker taxonomy."""
    runtime_path = str(getattr(context, "runtime_path", "") or "").strip().lower()
    workload = getattr(getattr(context, "workload", None), "value", getattr(context, "workload", ""))
    workload = str(workload or "").strip().lower()
    if runtime_path in {"chat_agent", "onboarding_agent", "orchestrator_agent"}:
        return GpuPriority.INTERACTIVE_CHAT
    if workload == "interactive":
        return GpuPriority.INTERACTIVE_CHAT
    if workload == "vision" or runtime_path == "screenshot_image_analysis":
        return GpuPriority.SCREENSHOT_BACKGROUND
    if workload == "report" or runtime_path in {
        "context_window_summary",
        "session_consolidation",
        "session_title_generation",
    }:
        return GpuPriority.REPORTS_RESEARCH_MEMORY
    if runtime_path == "strategist_agent":
        return GpuPriority.ACCEPTED_SCHEDULED_GOAL
    return GpuPriority.REPORTS_RESEARCH_MEMORY


@dataclass(frozen=True, slots=True)
class GpuAdmissionReceipt:
    """Operator-safe state; no payload, prompt, credential, or provider output."""

    operation_id: str
    job_id: str
    owner_id: str
    priority: GpuPriority
    status: str
    queue_position: int | None
    active_operation_id: str | None
    fencing_token: int | None
    reason_code: str | None
    queued: int
    max_queued: int
    serial_gpu: bool = True
    parent_job_id: str | None = None
    runtime_path: str = ""
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        if self.status not in GPU_ADMISSION_STATUSES:
            raise ValueError(f"unknown GPU admission status: {self.status}")
        if self.queue_position is not None and self.queue_position < 1:
            raise ValueError("queue position must be positive")
        if self.queued < 0 or self.max_queued < 1:
            raise ValueError("queue counts must be non-negative and bounded")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": GPU_ADMISSION_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "job_id": self.job_id,
            "owner_id": self.owner_id,
            "parent_job_id": self.parent_job_id,
            "runtime_path": self.runtime_path,
            "priority": self.priority.value,
            "priority_rank": self.priority.rank,
            "status": self.status,
            "queue_position": self.queue_position,
            "active_operation_id": self.active_operation_id,
            "fencing_token": self.fencing_token,
            "reason_code": self.reason_code,
            "queued": self.queued,
            "max_queued": self.max_queued,
            "serial_gpu": self.serial_gpu,
            "cancel_requested": self.cancel_requested,
            "operator_visible": True,
        }


@dataclass(frozen=True, slots=True)
class GpuAdmissionLease:
    """Single active GPU lease; the owner and token fence completion."""

    operation_id: str
    job_id: str
    owner_id: str
    fencing_token: int
    priority: GpuPriority


@dataclass(slots=True)
class _QueuedOperation:
    request: GpuAdmissionRequest
    sequence: int
    accepted_at: float
    started_at: float | None = None
    finished_at: float | None = None
    status: str = "queued"
    fencing_token: int | None = None
    reason_code: str | None = None
    cancel_requested: bool = False


T = TypeVar("T")


class GpuAdmissionBroker(Generic[T]):
    """A bounded, FIFO-within-class, one-active-operation GPU broker."""

    def __init__(
        self,
        *,
        max_queued: int = 32,
        clock: Callable[[], float] = time.time,
        history_limit: int = 128,
    ) -> None:
        if int(max_queued) < 1:
            raise ValueError("max_queued must be positive")
        if int(history_limit) < 1:
            raise ValueError("history_limit must be positive")
        self.max_queued = int(max_queued)
        self._clock = clock
        self._history_limit = int(history_limit)
        # Synchronous model callers may run in worker threads while async
        # callers use other event loops. Keep one thread-safe state gate so
        # both paths share the same serial admission lane.
        self._condition = threading.Condition()
        self._async_waiters: set[asyncio.Future[None]] = set()
        self._operations: dict[str, _QueuedOperation] = {}
        self._queue: list[str] = []
        self._active_operation_id: str | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._sequence = 0
        self._fencing_token = 0
        self._last_degraded_reason: str | None = None

    async def enqueue(
        self,
        request: GpuAdmissionRequest,
        *,
        now: float | None = None,
    ) -> GpuAdmissionReceipt:
        """Accept one bounded operation without invoking its provider callback."""
        observed_at = self._clock() if now is None else float(now)
        with self._condition:
            return self._enqueue_locked(request, observed_at)

    def _enqueue_locked(
        self,
        request: GpuAdmissionRequest,
        observed_at: float,
    ) -> GpuAdmissionReceipt:
        self._expire_locked(observed_at)
        existing = self._operations.get(request.operation_id)
        if existing is not None:
            if existing.request != request:
                raise GpuAdmissionIdentityError(
                    f"operation identity already belongs to a different request: {request.operation_id}"
                )
            if existing.status in {"succeeded", "failed", "cancelled", "expired", "rejected"}:
                return self._receipt_locked(existing)
            raise GpuAdmissionIdentityError(
                f"operation is already admitted and active: {request.operation_id}"
            )
        if request.deadline_at <= observed_at:
            operation = self._record_terminal_locked(
                request, status="expired", reason_code="deadline_expired", observed_at=observed_at
            )
            self._last_degraded_reason = "deadline_expired"
            raise GpuAdmissionExpiredError(
                "GPU operation deadline expired before admission",
                receipt=self._receipt_locked(operation),
            )
        if len(self._queue) >= self.max_queued:
            operation = self._record_terminal_locked(
                request, status="rejected", reason_code="capacity_exhausted", observed_at=observed_at
            )
            self._last_degraded_reason = "capacity_exhausted"
            raise GpuAdmissionCapacityError(
                "GPU admission queue is full",
                receipt=self._receipt_locked(operation),
            )
        self._sequence += 1
        operation = _QueuedOperation(request=request, sequence=self._sequence, accepted_at=observed_at)
        self._operations[request.operation_id] = operation
        self._queue.append(request.operation_id)
        # A newly accepted operation is a concrete recovery signal after
        # a transient rejection, expiry, or provider failure.
        self._last_degraded_reason = None
        self._notify_all_locked()
        return self._receipt_locked(operation)

    async def acquire(self, operation_id: str, *, now: float | None = None) -> GpuAdmissionLease:
        """Claim the highest-priority ready operation, or wait until it is ready."""
        operation_id = str(operation_id or "").strip()
        initial_now = self._clock() if now is None else float(now)
        first_pass = True
        while True:
            with self._condition:
                observed_at = initial_now if first_pass else self._clock()
                first_pass = False
                self._expire_locked(observed_at)
                operation = self._operations.get(operation_id)
                if operation is None:
                    raise KeyError(operation_id)
                if operation.status != "queued":
                    self._raise_terminal_locked(operation)
                if self._active_operation_id is None and self._select_next_locked() == operation_id:
                    self._queue.remove(operation_id)
                    self._fencing_token += 1
                    operation.status = "running"
                    operation.started_at = observed_at
                    operation.fencing_token = self._fencing_token
                    self._active_operation_id = operation_id
                    self._notify_all_locked()
                    return GpuAdmissionLease(
                        operation_id=operation_id,
                        job_id=operation.request.job_id,
                        owner_id=operation.request.owner_id,
                        fencing_token=self._fencing_token,
                        priority=operation.request.priority,
                    )
                remaining = operation.request.deadline_at - self._clock()
                if remaining <= 0:
                    self._expire_locked(self._clock())
                    continue
                waiter = asyncio.get_running_loop().create_future()
                self._async_waiters.add(waiter)
            try:
                await asyncio.wait_for(waiter, timeout=remaining)
            except asyncio.TimeoutError:
                # The deadline remains the hard upper bound if a condition
                # notification races event-loop wake-up.
                continue
            finally:
                with self._condition:
                    self._async_waiters.discard(waiter)

    async def release(
        self,
        lease: GpuAdmissionLease,
        *,
        outcome: str = "succeeded",
        reason_code: str | None = None,
    ) -> GpuAdmissionReceipt:
        """Release one lease, rejecting stale owners and fencing tokens."""
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("GPU release outcome must be succeeded, failed, or cancelled")
        with self._condition:
            operation = self._operations.get(lease.operation_id)
            if (
                operation is None
                or self._active_operation_id != lease.operation_id
                or operation.status != "running"
                or operation.request.owner_id != lease.owner_id
                or operation.fencing_token != lease.fencing_token
            ):
                receipt = self._receipt_for_lease_locked(lease, reason_code="stale_owner_or_fencing_token")
                raise GpuAdmissionLeaseError("GPU lease owner or fencing token is stale", receipt=receipt)
            if operation.cancel_requested:
                outcome = "cancelled"
                reason_code = operation.reason_code or reason_code or "cancelled"
            elif outcome == "cancelled" and reason_code is None:
                reason_code = "cancelled"
            observed_at = self._clock()
            operation.status = outcome
            operation.reason_code = reason_code
            operation.finished_at = observed_at
            self._active_operation_id = None
            self._active_task = None
            if outcome != "succeeded":
                self._last_degraded_reason = reason_code or outcome
            else:
                self._last_degraded_reason = None
            self._notify_all_locked()
            return self._receipt_locked(operation)

    async def cancel(
        self,
        operation_id: str,
        *,
        owner_id: str | None = None,
        fencing_token: int | None = None,
        reason_code: str = "cancelled",
    ) -> GpuAdmissionReceipt:
        """Cancel queued work; active work is marked for cancellation after release."""
        with self._condition:
            operation = self._operations.get(str(operation_id or "").strip())
            if operation is None:
                raise KeyError(operation_id)
            normalized_owner = None if owner_id is None else str(owner_id).strip()
            if operation.status == "queued":
                if normalized_owner is not None and normalized_owner != operation.request.owner_id:
                    receipt = self._receipt_locked(operation, reason_code="stale_owner_or_fencing_token")
                    raise GpuAdmissionLeaseError("GPU cancellation owner is stale", receipt=receipt)
                self._queue.remove(operation.request.operation_id)
                operation.status = "cancelled"
                operation.reason_code = reason_code
                operation.finished_at = self._clock()
                self._last_degraded_reason = reason_code
                self._notify_all_locked()
                return self._receipt_locked(operation)
            if operation.status == "running":
                if (
                    normalized_owner != operation.request.owner_id
                    or fencing_token is None
                    or operation.fencing_token != fencing_token
                ):
                    receipt = self._receipt_locked(operation, reason_code="stale_owner_or_fencing_token")
                    raise GpuAdmissionLeaseError(
                        "GPU cancellation owner or fencing token is stale",
                        receipt=receipt,
                    )
                operation.cancel_requested = True
                operation.reason_code = reason_code
                if self._active_task is not None:
                    self._cancel_active_task()
                self._notify_all_locked()
                return self._receipt_locked(operation)
            return self._receipt_locked(operation)

    async def status(self) -> dict[str, object]:
        """Return an operator-safe broker receipt with no queued payloads."""
        with self._condition:
            self._expire_locked(self._clock())
            active = self._operations.get(self._active_operation_id or "")
            queued = [
                self._receipt_locked(self._operations[operation_id]).as_dict()
                for operation_id in sorted(
                    self._queue,
                    key=lambda item: self._sort_key_locked(self._operations[item]),
                )
            ]
            return {
                "schema_version": GPU_ADMISSION_SCHEMA_VERSION,
                "status": "degraded" if self._last_degraded_reason else "ready",
                "degraded": bool(self._last_degraded_reason),
                "degradation_code": self._last_degraded_reason,
                "serial_gpu": True,
                "max_active": 1,
                "active": self._receipt_locked(active).as_dict() if active is not None else None,
                "queued": queued,
                "capacity": {
                    "max_queued": self.max_queued,
                    "queued": len(self._queue),
                    "available": max(self.max_queued - len(self._queue), 0),
                },
                "operator_visible": True,
                "claim_boundary": "process_local_admission_lease; durable_job_repository_remains_canonical",
            }

    async def execute(
        self,
        request: GpuAdmissionRequest,
        operation: Callable[[], Awaitable[T]],
        *,
        now: float | None = None,
    ) -> T:
        """Run one awaited callback under the serial broker lease."""
        await self.enqueue(request, now=now)
        try:
            lease = await self.acquire(request.operation_id, now=now)
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_after_wait(request))
            raise
        active_task = asyncio.current_task()
        self._bind_async_task(lease, active_task)
        cancellation_requests_at_start = (
            active_task.cancelling() if active_task is not None else 0
        )
        try:
            result = await operation()
        except asyncio.CancelledError:
            await asyncio.shield(
                self.release(lease, outcome="cancelled", reason_code="caller_cancelled")
            )
            raise
        except BaseException:
            await asyncio.shield(self.release(lease, outcome="failed", reason_code="provider_failed"))
            raise
        caller_cancelled = (
            active_task is not None
            and active_task.cancelling() > cancellation_requests_at_start
        )
        try:
            receipt = await self.release(
                lease,
                outcome="cancelled" if caller_cancelled else "succeeded",
                reason_code="caller_cancelled" if caller_cancelled else None,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self.release(lease, outcome="cancelled", reason_code="caller_cancelled")
            )
            raise
        if receipt.status == "cancelled":
            raise GpuAdmissionCancelledError(
                "GPU operation was cancelled before completion",
                receipt=receipt,
            )
        return result

    def execute_sync(
        self,
        request: GpuAdmissionRequest,
        operation: Callable[[], T],
        *,
        now: float | None = None,
    ) -> T:
        """Run one blocking provider callback under the shared GPU lease.

        Synchronous model-fabric callers are expected to invoke this from a
        worker thread (for example through ``asyncio.to_thread``). The blocking
        path uses the same state and condition as async callers, without
        binding the shared broker to one event loop.
        """
        observed_at = self._clock() if now is None else float(now)
        with self._condition:
            self._enqueue_locked(request, observed_at)
        lease = self._acquire_sync(request.operation_id, now=now)
        with self._condition:
            self._active_task = None
        try:
            result = operation()
        except BaseException:
            self._release_sync(lease, outcome="failed", reason_code="provider_failed")
            raise
        receipt = self._release_sync(lease, outcome="succeeded")
        if receipt.status == "cancelled":
            raise GpuAdmissionCancelledError(
                "GPU operation was cancelled before completion",
                receipt=receipt,
            )
        return result

    def _acquire_sync(
        self,
        operation_id: str,
        *,
        now: float | None = None,
    ) -> GpuAdmissionLease:
        operation_id = str(operation_id or "").strip()
        initial_now = self._clock() if now is None else float(now)
        first_pass = True
        with self._condition:
            while True:
                observed_at = initial_now if first_pass else self._clock()
                first_pass = False
                self._expire_locked(observed_at)
                operation = self._operations.get(operation_id)
                if operation is None:
                    raise KeyError(operation_id)
                if operation.status != "queued":
                    self._raise_terminal_locked(operation)
                if self._active_operation_id is None and self._select_next_locked() == operation_id:
                    self._queue.remove(operation_id)
                    self._fencing_token += 1
                    operation.status = "running"
                    operation.started_at = observed_at
                    operation.fencing_token = self._fencing_token
                    self._active_operation_id = operation_id
                    self._notify_all_locked()
                    return GpuAdmissionLease(
                        operation_id=operation_id,
                        job_id=operation.request.job_id,
                        owner_id=operation.request.owner_id,
                        fencing_token=self._fencing_token,
                        priority=operation.request.priority,
                    )
                remaining = operation.request.deadline_at - self._clock()
                if remaining <= 0:
                    self._expire_locked(self._clock())
                    continue
                self._condition.wait(timeout=remaining)

    def _release_sync(
        self,
        lease: GpuAdmissionLease,
        *,
        outcome: str = "succeeded",
        reason_code: str | None = None,
    ) -> GpuAdmissionReceipt:
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("GPU release outcome must be succeeded, failed, or cancelled")
        with self._condition:
            operation = self._operations.get(lease.operation_id)
            if (
                operation is None
                or self._active_operation_id != lease.operation_id
                or operation.status != "running"
                or operation.request.owner_id != lease.owner_id
                or operation.fencing_token != lease.fencing_token
            ):
                receipt = self._receipt_for_lease_locked(lease, reason_code="stale_owner_or_fencing_token")
                raise GpuAdmissionLeaseError("GPU lease owner or fencing token is stale", receipt=receipt)
            if operation.cancel_requested:
                outcome = "cancelled"
                reason_code = operation.reason_code or reason_code or "cancelled"
            elif outcome == "cancelled" and reason_code is None:
                reason_code = "cancelled"
            observed_at = self._clock()
            operation.status = outcome
            operation.reason_code = reason_code
            operation.finished_at = observed_at
            self._active_operation_id = None
            self._active_task = None
            if outcome != "succeeded":
                self._last_degraded_reason = reason_code or outcome
            else:
                self._last_degraded_reason = None
            self._notify_all_locked()
            return self._receipt_locked(operation)

    async def stream(
        self,
        request: GpuAdmissionRequest,
        operation: Callable[[], AsyncIterator[T]],
        *,
        now: float | None = None,
    ) -> AsyncIterator[T]:
        """Yield a streaming callback while retaining the serial GPU lease."""
        await self.enqueue(request, now=now)
        try:
            lease = await self.acquire(request.operation_id, now=now)
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_after_wait(request))
            raise
        active_task = asyncio.current_task()
        self._bind_async_task(lease, active_task)
        cancellation_requests_at_start = (
            active_task.cancelling() if active_task is not None else 0
        )
        stream_iterator: AsyncIterator[T] | None = None
        try:
            stream_iterator = operation()
            async for item in stream_iterator:
                yield item
        except BaseException as error:
            await self._close_stream_iterator(stream_iterator)
            if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                await asyncio.shield(
                    self.release(lease, outcome="cancelled", reason_code="caller_cancelled")
                )
            else:
                await asyncio.shield(
                    self.release(lease, outcome="failed", reason_code="provider_failed")
                )
            raise
        await self._close_stream_iterator(stream_iterator)
        caller_cancelled = (
            active_task is not None
            and active_task.cancelling() > cancellation_requests_at_start
        )
        try:
            receipt = await self.release(
                lease,
                outcome="cancelled" if caller_cancelled else "succeeded",
                reason_code="caller_cancelled" if caller_cancelled else None,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self.release(lease, outcome="cancelled", reason_code="caller_cancelled")
            )
            raise
        if receipt.status == "cancelled":
            raise GpuAdmissionCancelledError(
                "GPU stream was cancelled before completion",
                receipt=receipt,
            )

    async def reset(self) -> None:
        """Clear process-local state for isolated tests or a controlled restart."""
        with self._condition:
            if self._active_operation_id is not None:
                raise RuntimeError("cannot reset an active GPU admission lease")
            self._operations.clear()
            self._queue.clear()
            self._active_task = None
            self._last_degraded_reason = None
            self._notify_all_locked()

    async def _cancel_after_wait(
        self,
        request: GpuAdmissionRequest,
    ) -> GpuAdmissionReceipt | None:
        """Clean up a request whose caller was cancelled while waiting."""
        with self._condition:
            operation = self._operations.get(request.operation_id)
            if operation is None:
                return None
            if operation.status == "queued":
                self._queue.remove(operation.request.operation_id)
                operation.status = "cancelled"
                operation.reason_code = "caller_cancelled"
                operation.finished_at = self._clock()
                self._last_degraded_reason = "caller_cancelled"
                self._notify_all_locked()
                return self._receipt_locked(operation)
            if operation.status == "running" and operation.request.owner_id == request.owner_id:
                if self._active_task is None:
                    # Cancellation can race the hand-off immediately after
                    # acquire() claims the slot.  No callback has started
                    # before execute()/stream() binds its task, so close the
                    # lease here instead of leaving an orphaned active slot.
                    operation.status = "cancelled"
                    operation.reason_code = "caller_cancelled"
                    operation.finished_at = self._clock()
                    self._active_operation_id = None
                    self._last_degraded_reason = "caller_cancelled"
                else:
                    operation.cancel_requested = True
                    operation.reason_code = "caller_cancelled"
                    self._cancel_active_task()
                self._notify_all_locked()
            return self._receipt_locked(operation)

    async def _close_stream_iterator(self, iterator: AsyncIterator[T] | None) -> None:
        """Close an async provider iterator when its consumer is cancelled."""
        close = getattr(iterator, "aclose", None)
        if not callable(close):
            return
        try:
            await asyncio.shield(close())
        except BaseException:
            # Preserve the provider/consumer error.  Async task cancellation is
            # cooperative; a non-compliant iterator remains a documented risk.
            return

    def _notify_all_locked(self) -> None:
        self._condition.notify_all()
        waiters = tuple(self._async_waiters)
        self._async_waiters.clear()
        for waiter in waiters:
            if waiter.done():
                continue
            try:
                loop = waiter.get_loop()
                if loop.is_closed():
                    continue
                loop.call_soon_threadsafe(self._resolve_async_waiter, waiter)
            except (RuntimeError, AttributeError):
                # The owning event loop may be shutting down while a sync
                # caller releases the shared lease. State remains authoritative
                # under the condition; a future waiter will re-check it.
                continue

    @staticmethod
    def _resolve_async_waiter(waiter: asyncio.Future[None]) -> None:
        if not waiter.done():
            waiter.set_result(None)

    def _bind_async_task(
        self,
        lease: GpuAdmissionLease,
        task: asyncio.Task[Any] | None,
    ) -> None:
        """Bind the callback task atomically, closing a cancelled hand-off."""
        with self._condition:
            operation = self._operations.get(lease.operation_id)
            if (
                operation is None
                or self._active_operation_id != lease.operation_id
                or operation.status != "running"
                or operation.request.owner_id != lease.owner_id
                or operation.fencing_token != lease.fencing_token
            ):
                receipt = self._receipt_for_lease_locked(
                    lease,
                    reason_code="stale_owner_or_fencing_token",
                )
                raise GpuAdmissionLeaseError(
                    "GPU lease owner or fencing token is stale",
                    receipt=receipt,
                )
            if operation.cancel_requested:
                operation.status = "cancelled"
                operation.reason_code = operation.reason_code or "cancelled"
                operation.finished_at = self._clock()
                self._active_operation_id = None
                self._active_task = None
                self._last_degraded_reason = operation.reason_code
                self._notify_all_locked()
                receipt = self._receipt_locked(operation)
                raise GpuAdmissionCancelledError(
                    "GPU operation was cancelled before provider invocation",
                    receipt=receipt,
                )
            self._active_task = task

    def _cancel_active_task(self) -> None:
        task = self._active_task
        if task is None:
            return
        try:
            loop = task.get_loop()
            if loop.is_running() and loop is not asyncio.get_running_loop():
                loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()
        except (RuntimeError, AttributeError):
            task.cancel()

    def _select_next_locked(self) -> str | None:
        if not self._queue:
            return None
        return min(self._queue, key=lambda item: self._sort_key_locked(self._operations[item]))

    @staticmethod
    def _sort_key_locked(operation: _QueuedOperation) -> tuple[int, int]:
        return (-operation.request.priority.rank, operation.sequence)

    def _expire_locked(self, observed_at: float) -> None:
        expired = [
            operation_id
            for operation_id in self._queue
            if self._operations[operation_id].request.deadline_at <= observed_at
        ]
        for operation_id in expired:
            operation = self._operations[operation_id]
            self._queue.remove(operation_id)
            operation.status = "expired"
            operation.reason_code = "deadline_expired"
            operation.finished_at = observed_at
            self._last_degraded_reason = "deadline_expired"
        if expired:
            self._notify_all_locked()

    def _record_terminal_locked(
        self,
        request: GpuAdmissionRequest,
        *,
        status: str,
        reason_code: str,
        observed_at: float,
    ) -> _QueuedOperation:
        self._sequence += 1
        operation = _QueuedOperation(
            request=request,
            sequence=self._sequence,
            accepted_at=observed_at,
            finished_at=observed_at,
            status=status,
            reason_code=reason_code,
        )
        self._operations[request.operation_id] = operation
        self._trim_history_locked()
        return operation

    def _trim_history_locked(self) -> None:
        if len(self._operations) <= self._history_limit:
            return
        removable = [
            operation_id
            for operation_id, operation in self._operations.items()
            if (
                operation_id != self._active_operation_id
                and operation_id not in self._queue
                and operation.status != "queued"
            )
        ]
        for operation_id in sorted(
            removable,
            key=lambda item: self._operations[item].sequence,
        )[: max(len(self._operations) - self._history_limit, 0)]:
            self._operations.pop(operation_id, None)

    def _raise_terminal_locked(self, operation: _QueuedOperation) -> None:
        receipt = self._receipt_locked(operation)
        if operation.status == "expired":
            raise GpuAdmissionExpiredError("GPU operation deadline expired while queued", receipt=receipt)
        if operation.status == "cancelled":
            raise GpuAdmissionCancelledError(
                "GPU operation was cancelled before provider invocation",
                receipt=receipt,
            )
        if operation.status == "rejected":
            raise GpuAdmissionCapacityError("GPU operation was rejected at admission", receipt=receipt)
        if operation.status == "running":
            raise GpuAdmissionIdentityError("GPU operation is already claimed")
        raise GpuAdmissionIdentityError(f"GPU operation is already terminal: {operation.status}")

    def _receipt_locked(
        self,
        operation: _QueuedOperation | None,
        *,
        reason_code: str | None = None,
    ) -> GpuAdmissionReceipt:
        if operation is None:
            raise KeyError("missing operation")
        queue_position = None
        if operation.request.operation_id in self._queue:
            ordered = sorted(self._queue, key=lambda item: self._sort_key_locked(self._operations[item]))
            queue_position = ordered.index(operation.request.operation_id) + 1
        return GpuAdmissionReceipt(
            operation_id=operation.request.operation_id,
            job_id=operation.request.job_id,
            owner_id=operation.request.owner_id,
            parent_job_id=operation.request.parent_job_id,
            runtime_path=operation.request.runtime_path,
            priority=operation.request.priority,
            status=operation.status,
            queue_position=queue_position,
            active_operation_id=self._active_operation_id,
            fencing_token=operation.fencing_token,
            reason_code=reason_code if reason_code is not None else operation.reason_code,
            queued=len(self._queue),
            max_queued=self.max_queued,
            cancel_requested=operation.cancel_requested,
        )

    def _receipt_for_lease_locked(
        self,
        lease: GpuAdmissionLease,
        *,
        reason_code: str,
    ) -> GpuAdmissionReceipt:
        operation = self._operations.get(lease.operation_id)
        if operation is not None:
            return self._receipt_locked(operation, reason_code=reason_code)
        return GpuAdmissionReceipt(
            operation_id=lease.operation_id,
            job_id=lease.job_id,
            owner_id=lease.owner_id,
            priority=lease.priority,
            status="failed",
            queue_position=None,
            active_operation_id=self._active_operation_id,
            fencing_token=lease.fencing_token,
            reason_code=reason_code,
            queued=len(self._queue),
            max_queued=self.max_queued,
        )


gpu_admission_broker: GpuAdmissionBroker[Any] = GpuAdmissionBroker()


__all__ = [
    "GPU_ADMISSION_SCHEMA_VERSION",
    "GPU_ADMISSION_STATUSES",
    "GpuAdmissionBroker",
    "GpuAdmissionCapacityError",
    "GpuAdmissionCancelledError",
    "GpuAdmissionError",
    "GpuAdmissionExpiredError",
    "GpuAdmissionIdentityError",
    "GpuAdmissionLease",
    "GpuAdmissionLeaseError",
    "GpuAdmissionReceipt",
    "GpuAdmissionRequest",
    "GpuPriority",
    "gpu_admission_broker",
    "priority_for_inference_context",
]
