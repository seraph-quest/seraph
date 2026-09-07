"""Transport-neutral execution hooks for governed streaming and VLM adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

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
from .gpu_admission import GpuAdmissionError, GpuAdmissionRequest, gpu_admission_broker
from .selector import select_route


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
            operation_id=decision.attempt_id or f"{attempt_context.request_id}:{candidate.profile.id}",
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

        try:
            async for delta in gpu_admission_broker.stream(
                admission_request,
                admitted_transport,
                now=now,
            ):
                yield delta
        except Exception as error:
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
        operation_id=decision.attempt_id or f"{context.request_id}:{decision.selected.profile.id}",
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

    try:
        return await gpu_admission_broker.execute(admission_request, admitted_adapter)
    except GpuAdmissionError as error:
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
