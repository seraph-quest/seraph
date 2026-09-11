"""Transport-neutral execution hooks for governed streaming and VLM adapters."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator, Awaitable, Callable
from threading import Thread
from typing import Any, Protocol, TypeVar

from .contracts import (
    InferenceRequestContext,
    ModelRouteCandidate,
    ModelRouteProof,
    NoCompliantModelRouteError,
    RouteDecision,
    bind_final_inference_payload,
    finalized_openai_compatible_body,
)
from .hooks import persist_denied_route
from .remote_inference_admission import (
    RemoteInferenceAdmissionError as GpuAdmissionError,
    RemoteInferenceAdmissionRequest as GpuAdmissionRequest,
    current_remote_inference_receipt_binding,
    prepare_bound_remote_inference,
    remote_inference_admission_broker as gpu_admission_broker,
    stable_remote_inference_operation_id,
)
from .selector import select_route


_SyncResult = TypeVar("_SyncResult")


class SyncAdapterReceiptError(RuntimeError):
    """Raised when a governed sync adapter cannot persist its route receipt."""

    code = "route_receipt_persistence_failed"


async def _persist_bound_admission_receipt(
    operation_id: str,
    *,
    receipt: Any | None = None,
    readback: bool = False,
) -> None:
    """Adopt one broker receipt into the caller's existing durable job.

    The binding is optional so short-lived request jobs keep their current
    behavior.  When present, the broker receipt is read back after the
    provider boundary and forwarded with the caller's separate durable lease
    fence.  A missing process-local receipt means admission never created an
    operation and therefore has no durable projection to write.
    """
    binding = current_remote_inference_receipt_binding()
    if binding is None:
        return
    persisted_receipt = receipt
    if persisted_receipt is None and readback:
        try:
            persisted_receipt = gpu_admission_broker.receipt_for(operation_id)
        except (AttributeError, KeyError):
            return
    if persisted_receipt is None:
        return
    await gpu_admission_broker.persist_receipt(
        persisted_receipt,
        repository=binding.repository,
        owner=binding.owner,
        fencing_token=binding.fencing_token,
    )


def _run_awaitable_sync(awaitable: Awaitable[_SyncResult]) -> _SyncResult:
    """Resolve repository coroutines from sync callers, including active loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: list[_SyncResult] = []
    error: list[BaseException] = []
    caller_context = contextvars.copy_context()

    def runner() -> None:
        try:
            result.append(caller_context.run(asyncio.run, awaitable))
        except BaseException as exc:  # pragma: no cover - defensive thread bridge
            error.append(exc)

    thread = Thread(target=runner, name="seraph-model-fabric-sync", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    if not result:
        raise RuntimeError("sync awaitable completed without a result")
    return result[0]


def _require_persisted_receipt(result: Any) -> None:
    if result is None or not bool(getattr(result, "persisted", False)):
        raise SyncAdapterReceiptError()


class RouteReceiptHooks(Protocol):
    async def attempt_started(
        self,
        *,
        context: InferenceRequestContext,
        decision: RouteDecision,
    ) -> None: ...

    async def attempt_finished(
        self,
        *,
        context: InferenceRequestContext,
        decision: RouteDecision,
        outcome: str,
        error_code: str | None,
    ) -> None: ...


StreamingTransport = Callable[
    [ModelRouteCandidate, dict[str, object], bool],
    AsyncIterator[str],
]


async def execute_streaming(
    *,
    context: InferenceRequestContext,
    candidates: tuple[ModelRouteCandidate, ...],
    proofs: tuple[ModelRouteProof, ...],
    messages: tuple[dict[str, object], ...],
    transport: StreamingTransport,
    hooks: RouteReceiptHooks,
    temperature: float,
    max_tokens: int,
    now: float | None = None,
) -> AsyncIterator[str]:
    """Preflight each attempt; fallback only before any output is emitted."""
    from .hooks import RouteReceiptSession

    eligible = candidates if context.fallback_allowed else candidates[:1]
    last_error: Exception | None = None
    attempted = False
    last_denied_context = context
    last_denied_decision = None
    denial_reasons: list[str] = []
    fallback_reason_code: str | None = None
    degradation_codes: list[str] = []
    aggregate = hooks if isinstance(hooks, RouteReceiptSession) else None
    for candidate in eligible:
        if candidate.adapter != "openai_compatible_chat":
            denial_reasons.append("adapter_workload_mismatch")
            continue
        transport_body = finalized_openai_compatible_body(
            model_id=candidate.profile.model,
            messages=[dict(message) for message in messages],
            options=candidate.profile.options,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        attempt_context = bind_final_inference_payload(context, transport_body)
        decision = select_route(attempt_context, (candidate,), proofs, now=now)
        if not decision.allowed or decision.selected is None:
            last_denied_context = attempt_context
            last_denied_decision = decision
            denial_reasons.extend(rejection.reason_code for rejection in decision.rejections)
            if context.fallback_allowed:
                fallback_reason_code = "preflight_rejected"
                degradation_codes.extend(("fallback_used", "fallback_preflight_rejected"))
            continue
        attempted = True
        proof_hashes = decision.proof_hashes
        emitted = False
        admission_request = GpuAdmissionRequest.from_inference_context(
            attempt_context,
            operation_id=stable_remote_inference_operation_id(
                attempt_context,
                profile_id=candidate.profile.id,
                fallback=decision.attempt_id or f"{attempt_context.request_id}:{candidate.profile.id}",
            ),
            uncertain_on_error=(candidate.profile.provider_kind == "openrouter"),
        )
        await prepare_bound_remote_inference(
            admission_request,
            profile_id=decision.selected.profile.id,
        )
        admission_callback_started = False

        async def admitted_transport() -> AsyncIterator[str]:
            nonlocal admission_callback_started, emitted
            admission_callback_started = True
            if aggregate is not None:
                aggregate.attempt_started(decision, capability_proof_hashes=proof_hashes)
            else:
                await hooks.attempt_started(context=attempt_context, decision=decision)
            try:
                async for delta in transport(decision.selected, transport_body, False):
                    emitted = True
                    yield delta
            except BaseException:
                if aggregate is not None:
                    aggregate.attempt_finished(
                        outcome="failed",
                        error_code="transport_failed",
                        decision=decision,
                        degradation_code="transport_failed",
                    )
                else:
                    await hooks.attempt_finished(
                        context=attempt_context,
                        decision=decision,
                        outcome="failed",
                        error_code="transport_failed",
                    )
                raise
            if not emitted:
                if aggregate is not None:
                    aggregate.attempt_finished(
                        outcome="failed",
                        error_code="stream_empty",
                        decision=decision,
                        degradation_code="stream_empty",
                    )
                else:
                    await hooks.attempt_finished(
                        context=attempt_context,
                        decision=decision,
                        outcome="failed",
                        error_code="stream_empty",
                    )
                raise RuntimeError("stream_empty")
            if aggregate is not None:
                aggregate.attempt_finished(
                    outcome="succeeded",
                    error_code=None,
                    decision=decision,
                    degradation_code=(
                        f"fallback_{fallback_reason_code}" if fallback_reason_code else None
                    ),
                )
            else:
                await hooks.attempt_finished(
                    context=attempt_context,
                    decision=decision,
                    outcome="succeeded",
                    error_code=None,
                )

        admission_error_receipt = None
        try:
            async for delta in gpu_admission_broker.stream(
                admission_request,
                admitted_transport,
                now=now,
            ):
                yield delta
        except Exception as error:
            admission_error_receipt = getattr(error, "receipt", None)
            if isinstance(error, GpuAdmissionError) and not admission_callback_started:
                # Admission is a bounded runtime decision.  A rejected,
                # expired, cancelled, or fenced request did not start this
                # provider attempt, so trying another candidate would
                # multiply queue pressure and bypass its operator-visible
                # receipt.  A provider that happens to raise the same base
                # class after the callback starts still follows normal
                # pre-output fallback semantics.
                if aggregate is not None:
                    await aggregate.finalize_denied(
                        decision=decision,
                        reason_codes=(
                            "gpu_admission_rejected",
                            f"gpu_admission_{error.code}",
                        ),
                        fallback_reason_code="gpu_admission_rejected",
                    )
                raise
            last_error = error
            if emitted or not context.fallback_allowed or aggregate is None:
                if aggregate is not None:
                    await aggregate.finalize(
                        outcome="failed",
                        fallback_reason_code=fallback_reason_code,
                        degradation_codes=tuple(dict.fromkeys(degradation_codes)),
                    )
                raise
            error_code = "stream_empty" if str(error) == "stream_empty" else "transport_failed"
            fallback_reason_code = error_code
            degradation_codes.extend(("fallback_used", f"fallback_{error_code}"))
            continue
        finally:
            await _persist_bound_admission_receipt(
                admission_request.operation_id,
                receipt=admission_error_receipt,
                # A provider exception can be raised after the broker has
                # already finalized a failed/uncertain operation. Read back
                # the broker state on every exit; a pre-admission rejection
                # simply has no operation to read.
                readback=True,
            )
        if aggregate is not None:
            await aggregate.finalize(
                outcome="succeeded",
                fallback_reason_code=fallback_reason_code,
                degradation_codes=tuple(dict.fromkeys(degradation_codes)),
            )
        return
    if aggregate is not None and attempted:
        await aggregate.finalize(
            outcome="failed",
            fallback_reason_code=fallback_reason_code,
            degradation_codes=tuple(dict.fromkeys(degradation_codes)),
        )
    if last_error is not None:
        raise last_error
    repository = getattr(hooks, "_repository", None)
    denied_kwargs = {
        "context": last_denied_context,
        "decision": last_denied_decision,
        "reason_codes": tuple(denial_reasons) or ("no_compliant_route",),
    }
    if repository is not None:
        denied_kwargs["repository"] = repository
    await persist_denied_route(**denied_kwargs)
    raise NoCompliantModelRouteError()


async def run_preflighted_adapter(
    *,
    context: InferenceRequestContext,
    decision: RouteDecision,
    adapter: Callable[[ModelRouteCandidate, bool], Awaitable[object]],
    hooks: RouteReceiptHooks,
) -> object:
    """Run a preflighted non-streaming adapter (for example VLM analyze-file)."""
    if not decision.allowed or decision.selected is None:
        repository = getattr(hooks, "_repository", None)
        denied_kwargs = {
            "context": context,
            "decision": decision,
            "reason_codes": tuple(
                rejection.reason_code for rejection in decision.rejections
            ) or ("no_compliant_route",),
        }
        if repository is not None:
            denied_kwargs["repository"] = repository
        await persist_denied_route(**denied_kwargs)
        raise NoCompliantModelRouteError()
    admission_request = GpuAdmissionRequest.from_inference_context(
        context,
        operation_id=stable_remote_inference_operation_id(
            context,
            profile_id=decision.selected.profile.id,
            fallback=decision.attempt_id or f"{context.request_id}:{decision.selected.profile.id}",
        ),
        uncertain_on_error=(decision.selected.profile.provider_kind == "openrouter"),
    )
    await prepare_bound_remote_inference(
        admission_request,
        profile_id=decision.selected.profile.id,
    )
    admission_callback_started = False

    async def admitted_adapter() -> object:
        nonlocal admission_callback_started
        admission_callback_started = True
        await hooks.attempt_started(context=context, decision=decision)
        try:
            result = await adapter(decision.selected, False)
        except BaseException:
            await hooks.attempt_finished(
                context=context,
                decision=decision,
                outcome="failed",
                error_code="transport_failed",
            )
            raise
        await hooks.attempt_finished(
            context=context,
            decision=decision,
            outcome="succeeded",
            error_code=None,
        )
        return result

    admission_error_receipt = None
    try:
        result = await gpu_admission_broker.execute(admission_request, admitted_adapter)
        return result
    except GpuAdmissionError as error:
        admission_error_receipt = getattr(error, "receipt", None)
        if not admission_callback_started:
            repository = getattr(hooks, "_repository", None)
            denied_kwargs = {
                "context": context,
                "decision": decision,
                "reason_codes": (
                    "gpu_admission_rejected",
                    f"gpu_admission_{error.code}",
                ),
                "fallback_reason_code": "gpu_admission_rejected",
            }
            if repository is not None:
                denied_kwargs["repository"] = repository
            await persist_denied_route(**denied_kwargs)
        raise
    finally:
        await _persist_bound_admission_receipt(
            admission_request.operation_id,
            receipt=admission_error_receipt,
            readback=True,
        )


def execute_sync_adapter(
    *,
    context: InferenceRequestContext,
    candidates: tuple[ModelRouteCandidate, ...],
    proofs: tuple[ModelRouteProof, ...],
    adapter: Callable[[ModelRouteCandidate, bool], _SyncResult],
    repository: Any | None = None,
    now: float | None = None,
) -> _SyncResult:
    """Select, admit, execute, and persist one bounded synchronous route.

    This is the governed seam for blocking adapters such as embeddings.  It
    performs no implicit fallback: a provider call whose outcome may be
    uncertain must be reconciled by its caller before another paid call is
    attempted.  A denied route and every started attempt receive a durable
    model-fabric receipt before the result is returned to the caller.
    """
    from .hooks import RouteReceiptSession, persist_denied_route
    from .repository import model_fabric_repository

    receipt_repository = repository or model_fabric_repository
    candidate_set = candidates[:1] if not context.fallback_allowed else candidates
    decision = select_route(context, candidate_set, proofs, now=now)
    if not decision.allowed or decision.selected is None:
        persistence = _run_awaitable_sync(
            persist_denied_route(
                context=context,
                decision=decision,
                reason_codes=tuple(
                    rejection.reason_code for rejection in decision.rejections
                ) or ("no_compliant_route",),
                repository=receipt_repository,
            )
        )
        _require_persisted_receipt(persistence)
        raise NoCompliantModelRouteError()

    session = RouteReceiptSession(
        context=context,
        repository=receipt_repository,
        capability_proof_hashes=decision.proof_hashes,
    )
    admission_request = GpuAdmissionRequest.from_inference_context(
        context,
        operation_id=stable_remote_inference_operation_id(
            context,
            profile_id=decision.selected.profile.id,
            fallback=decision.attempt_id
            or f"{context.request_id}:{decision.selected.profile.id}",
        ),
        uncertain_on_error=(decision.selected.profile.provider_kind == "openrouter"),
    )
    callback_started = False
    attempt_completed = False

    def persist_admission_receipt(
        receipt: Any | None = None,
        *,
        readback: bool = False,
    ) -> None:
        if current_remote_inference_receipt_binding() is None:
            return
        _run_awaitable_sync(
            _persist_bound_admission_receipt(
                admission_request.operation_id,
                receipt=receipt,
                readback=readback,
            )
        )

    _run_awaitable_sync(
        prepare_bound_remote_inference(
            admission_request,
            profile_id=decision.selected.profile.id,
        )
    )

    def admitted_adapter() -> _SyncResult:
        nonlocal callback_started, attempt_completed
        session.attempt_started(
            decision,
            capability_proof_hashes=decision.proof_hashes,
        )
        callback_started = True
        try:
            result = adapter(decision.selected, False)
        except BaseException:
            session.attempt_finished(
                outcome="failed",
                error_code="transport_failed",
                decision=decision,
            )
            attempt_completed = True
            raise
        session.attempt_finished(
            outcome="succeeded",
            error_code=None,
            decision=decision,
        )
        attempt_completed = True
        return result

    try:
        result = gpu_admission_broker.execute_sync(admission_request, admitted_adapter, now=now)
    except GpuAdmissionError as error:
        # Persist the broker's terminal/uncertain result while the durable
        # intent is still the authoritative operation record. Route-receipt
        # storage is a separate projection and may degrade; it must not leave
        # a successfully admitted remote operation without a durable outcome.
        persist_admission_receipt(getattr(error, "receipt", None), readback=True)
        if not callback_started:
            persistence = _run_awaitable_sync(
                session.finalize_denied(
                    decision=decision,
                    reason_codes=(
                        "remote_inference_admission_rejected",
                        f"remote_inference_admission_{error.code}",
                    ),
                    fallback_reason_code="remote_inference_admission_rejected",
                )
            )
            _require_persisted_receipt(persistence)
        elif attempt_completed:
            persistence = _run_awaitable_sync(
                session.finalize(
                    outcome="failed",
                    fallback_reason_code="remote_inference_admission_failed",
                    degradation_codes=(f"remote_inference_admission_{error.code}",),
                )
            )
            _require_persisted_receipt(persistence)
        raise
    except BaseException:
        # The provider callback may have crossed the remote boundary before a
        # non-admission exception escaped. Record that broker outcome before
        # attempting the route projection, which can independently degrade.
        persist_admission_receipt(readback=True)
        if callback_started and attempt_completed:
            persistence = _run_awaitable_sync(session.finalize(outcome="failed"))
            _require_persisted_receipt(persistence)
        elif not callback_started:
            persistence = _run_awaitable_sync(
                session.finalize_denied(
                    decision=decision,
                    reason_codes=("adapter_admission_failed",),
                )
            )
            _require_persisted_receipt(persistence)
        raise

    # The remote terminal settlement is the durable source of truth for the
    # admitted provider call. Finalizing the model-fabric route receipt after
    # it prevents a degraded route projection from stranding the intent.
    persist_admission_receipt(readback=True)
    persistence = _run_awaitable_sync(session.finalize(outcome="succeeded"))
    _require_persisted_receipt(persistence)
    return result
