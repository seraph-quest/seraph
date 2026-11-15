"""NEAR TEE text-slot model step for Work and guardian verticals (#1159, ADR-035).

The verticals (general task / specialist planning, Work Board proposals,
guardian assessment and plans, research, Mail, Calendar, communication
preparation and repository work) own their durable job, reservation,
adoption and accounting bindings and call the one shared broker directly.
This module gives them the existing NEAR TEE adapter path without a new
lane, queue, budget or ledger and without any fallback:

1. ``near_tee_vertical_route`` reads the usable NEAR TEE text slot serving a
   canonical route (local, no contact).
2. ``prepare_near_tee_vertical_call`` runs every precontact gate of
   ``prepare_near_tee_call`` (slot policy, purpose consent, catalog receipt,
   credential fence, selector and capability proofs, structured output when
   the route needs it) and persists a zero-transport denial receipt.
3. ``execute_near_tee_vertical_call`` runs the transport inside the caller's
   admitted broker lease under the sealed precontact guard
   (``x-no-aliasing``, policy revision and billing window). NEAR billing is
   read back inside that lease and settles the reservation; a missing cost
   is an Unknown liability that keeps the serial lease blocked. A known-cost
   failure (echo mismatch, empty output, overrun) settles the charge,
   releases the lease and is raised as ``NearTeeError`` so no output is
   adopted.

No import-time side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .contracts import NEAR_TEE_PROVIDER_KIND


@dataclass(frozen=True)
class NearTeeVerticalRoute:
    """The usable NEAR TEE text slot serving one canonical route."""

    runtime_path: str
    profile_id: str
    profile_contract_hash: str
    model_id: str
    request_cost_bound_microusd: int
    spend_ceiling_microusd: int
    max_output_tokens: int
    timeout_seconds: float


def near_tee_vertical_route(runtime_path: str, configured: Any = None) -> NearTeeVerticalRoute | None:
    """The NEAR TEE text slot serving ``runtime_path``, or None (local, no contact)."""
    from .caller_context import canonical_route_spec
    from .configuration import (near_tee_policy_for_runtime, near_tee_profile_for_slot, near_tee_slot,
        read_model_fabric_configuration, route_slot_for_task_class)
    try:
        configured = configured if configured is not None else read_model_fabric_configuration()
        policy = near_tee_policy_for_runtime(configured, runtime_path)
        if policy is None or tuple(policy.allowed_provider_kinds) != (NEAR_TEE_PROVIDER_KIND,):
            return None
        if route_slot_for_task_class(canonical_route_spec(runtime_path).task_class) != "text":
            # Vision and embeddings have their own adapters; verticals use text.
            return None
        slot = near_tee_slot(configured, "text")
        profile = near_tee_profile_for_slot("text", slot)
    except Exception:
        return None
    return NearTeeVerticalRoute(runtime_path=runtime_path, profile_id=profile.id,
        profile_contract_hash=profile.contract_hash, model_id=slot.model_id,
        request_cost_bound_microusd=int(slot.request_cost_bound_microusd),
        spend_ceiling_microusd=int(configured.inference_lane.spend_ceiling_microusd),
        max_output_tokens=int(slot.max_output_tokens), timeout_seconds=float(slot.timeout_seconds))


@dataclass(frozen=True)
class VerticalRouteSetup:
    """The reviewed route bounds a vertical binds into its durable authority.

    For NEAR TEE: the text slot's model, request cost bound, output bound,
    timeout and temperature, under the shared lane's spend ceiling. In the
    explicit legacy test mode the retained OpenRouter setup is used instead.
    """

    near_tee: bool
    profile_id: str
    model_id: str
    request_cost_bound_microusd: int
    spend_ceiling_microusd: int
    max_output_tokens: int
    timeout_seconds: float
    temperature: float


def vertical_inference_policy(runtime_path: str) -> tuple[Any, str, VerticalRouteSetup]:
    """``current_inference_policy`` for a vertical served by NEAR TEE (#1159).

    Returns ``(configured, policy_digest, setup)``. The NEAR TEE text slot
    serving ``runtime_path`` wins; otherwise the retained OpenRouter
    projection, in the explicit legacy test mode only. Raises
    ``PermissionError`` (with the stable route-block reason) when no reviewed
    policy is current, including OpenRouter retired with no NEAR TEE route.
    """
    from .configuration import read_model_fabric_configuration
    configured = read_model_fabric_configuration()
    route = near_tee_vertical_route(runtime_path, configured)
    if route is not None:
        from .configuration import near_tee_slot
        from .effective_policy import current_near_tee_policy
        # The canonical NEAR TEE slot authority projection; fails closed.
        configured, digest = current_near_tee_policy("text")
        slot = near_tee_slot(configured, "text")
        return configured, digest, VerticalRouteSetup(near_tee=True,
            profile_id=route.profile_id, model_id=route.model_id,
            request_cost_bound_microusd=route.request_cost_bound_microusd,
            spend_ceiling_microusd=route.spend_ceiling_microusd, max_output_tokens=route.max_output_tokens,
            timeout_seconds=route.timeout_seconds,
            temperature=float(slot.temperature) if slot.temperature is not None else 0.0)
    from .retirement import openrouter_retired
    if openrouter_retired():
        # No NEAR TEE route and OpenRouter retired: no reviewed policy is
        # current; never fall back to the retained OpenRouter projection.
        raise PermissionError(vertical_route_block(runtime_path) or "no_active_provider")
    from .effective_policy import inference_policy_from_configuration
    configured, digest = inference_policy_from_configuration(configured)
    setup = configured.openrouter_setup
    return configured, digest, VerticalRouteSetup(near_tee=False, profile_id=setup.profile_id,
        model_id="", request_cost_bound_microusd=setup.request_cost_bound_microusd,
        spend_ceiling_microusd=setup.spend_ceiling_microusd, max_output_tokens=setup.max_output_tokens,
        timeout_seconds=setup.timeout_seconds, temperature=setup.temperature)


def vertical_route_block(runtime_path: str) -> str | None:
    """Why a NEAR-adapted vertical's model step has no usable route, or None.

    The shared ``inactive_route_reason`` precedence (egress revoked, the NEAR
    TEE slot's own reason, ``openrouter_retired`` / ``no_active_provider``);
    None when the NEAR TEE text slot serves ``runtime_path`` or in the explicit
    legacy test mode. Local and provider-free.
    """
    from .retirement import NO_ACTIVE_PROVIDER_REASON, inactive_route_reason, openrouter_retired
    if reason := inactive_route_reason(runtime_path):
        return reason
    if near_tee_vertical_route(runtime_path) is None and openrouter_retired():
        return NO_ACTIVE_PROVIDER_REASON
    return None


def repository_repair_route_block() -> str | None:
    """Why repository repair cannot run, or None (legacy test mode only).

    #1159: the repository repair iteration model step (``repo_repair_source``)
    binds its consented exact request body, route digest and code-egress
    consent to the retired OpenRouter target and has no NEAR TEE adapter.
    With OpenRouter retired it is blocked before any planning, reservation or
    contact: ``near_tee_route_unsupported`` when a NEAR TEE slot serves the
    route (never implying a provider outage), else the shared inactive reason.
    """
    from .retirement import openrouter_only_route_block
    return openrouter_only_route_block("strategist_agent")


def is_near_tee_context(context: Any) -> bool:
    return tuple(getattr(context, "allowed_provider_kinds", ()) or ()) == (NEAR_TEE_PROVIDER_KIND,)


async def prepare_near_tee_vertical_call(context: Any, *, messages: list[dict[str, Any]], temperature: float,
                                         max_tokens: int, response_format: dict[str, Any] | None = None,
                                         persist_denial: bool = True):
    """All NEAR TEE precontact gates; raises ``NearTeeDenied`` before admission.

    The returned call's ``context`` is bound to the exact final body; callers
    build their ``GpuAdmissionRequest`` from it so the accounting payload
    digest is the body actually sent.
    """
    from .hooks import persist_denied_route
    from .near_tee import NearTeeDenied, prepare_near_tee_call
    try:
        return await prepare_near_tee_call(context=context, messages=messages, temperature=temperature,
            max_tokens=max_tokens, response_format=response_format)
    except NearTeeDenied as denied:
        if persist_denial:
            try:
                await persist_denied_route(context=context, decision=denied.decision, reason_codes=(denied.code,))
            except Exception:
                # The denial itself is authoritative; receipt persistence is best effort.
                pass
        raise


async def execute_near_tee_vertical_call(call, *, request, broker, hooks=None, on_started=None,
                                         before_contact=None):
    """Run one admitted NEAR TEE request inside the caller's broker lease.

    Returns ``(response, outcome)`` where ``response.choices[0].message`` is a
    literal assistant message. ``hooks`` (a ``RouteReceiptSession``) records
    the attempt; the caller finalizes it. ``on_started`` runs inside the lease
    immediately before transport (after the durable contact record);
    ``before_contact`` (async) runs first inside the lease, for callers that
    recheck their own authority and fence once admitted.
    """
    from .accounting import bind_precontact_guard
    from .near_tee import NearTeeError, near_tee_precontact_reason, near_tee_transport

    async def invoke():
        if before_contact is not None:
            await before_contact()
        if hooks is not None:
            hooks.attempt_started(call.decision, capability_proof_hashes=call.proof_hashes)
        if on_started is not None:
            on_started()
        return await near_tee_transport(call)

    with bind_precontact_guard(lambda: near_tee_precontact_reason(call)):
        outcome = await broker.execute(request, invoke)
    if outcome.failure_code is not None or outcome.text is None:
        code = outcome.failure_code or "near_tee_empty_output"
        if hooks is not None:
            hooks.attempt_finished(outcome="failed", error_code=code, decision=call.decision,
                degradation_code="near_tee_output_not_adopted")
        # Contact happened and its cost was settled from NEAR billing; no
        # output is adopted.
        raise NearTeeError(code)
    if hooks is not None:
        hooks.attempt_finished(outcome="succeeded", error_code=None, decision=call.decision)
    return near_tee_response(outcome.text, model=str(call.body["model"])), outcome


async def run_near_tee_vertical_call(call, *, priority=None, before_contact=None) -> str:
    """One prepared NEAR TEE call in the shared broker, bound to the caller's job.

    The NEAR TEE counterpart of ``run_preflighted_adapter`` for verticals that
    hold a durable job through ``bind_remote_inference_receipt``: the stable
    operation identity of that job, ``prepare_bound_remote_inference`` before
    dispatch, one final route receipt and the broker receipt adopted into the
    job. Returns the literal answer text; raises ``NearTeeError`` for a known
    cost failure (settled, nothing adopted) and the broker's uncertain error
    for an Unknown liability.
    """
    from . import execution
    from .gpu_admission import GpuAdmissionRequest
    from .hooks import RouteReceiptSession
    from .remote_inference_admission import prepare_bound_remote_inference, stable_remote_inference_operation_id
    context, decision = call.context, call.decision
    profile_id = decision.selected.profile.id
    request = GpuAdmissionRequest.from_inference_context(context, priority=priority,
        operation_id=stable_remote_inference_operation_id(context, profile_id=profile_id,
            fallback=decision.attempt_id or f"{context.request_id}:{profile_id}"),
        uncertain_on_error=True)
    await prepare_bound_remote_inference(request, profile_id=profile_id)
    hooks = RouteReceiptSession(context=context)
    error_receipt = None
    try:
        try:
            response, _outcome = await execute_near_tee_vertical_call(call, request=request,
                broker=execution.gpu_admission_broker, hooks=hooks, before_contact=before_contact)
        except BaseException as exc:
            error_receipt = getattr(exc, "receipt", None)
            if hooks.attempt_active:
                # Transport started and raised (for example an Unknown cost).
                hooks.attempt_finished(outcome="failed", error_code=near_tee_failure_code(exc) or
                    near_tee_failure_code(exc.__cause__) or "transport_failed", decision=decision,
                    degradation_code="transport_failed")
            if hooks.has_completed_attempts:
                await hooks.finalize(outcome="failed", degradation_codes=("near_tee_output_not_adopted",))
            raise
        receipt = await hooks.finalize(outcome="succeeded")
        if not receipt.persisted:
            raise RuntimeError("near_tee_route_receipt_persistence_failed")
        return str(response.choices[0].message.content)
    finally:
        await execution._persist_bound_admission_receipt(request.operation_id, receipt=error_receipt,
            readback=True)


def near_tee_response(text: str, *, model: str) -> SimpleNamespace:
    """A completion-shaped response holding only the literal answer text."""
    from smolagents.models import ChatMessage
    message = ChatMessage.from_dict({"role": "assistant", "content": text})
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], model=model)


def near_tee_failure_code(exc: BaseException) -> str | None:
    """The stable NEAR TEE code of a denial/known-cost failure, if any."""
    from .near_tee import NearTeeDenied, NearTeeError
    if isinstance(exc, (NearTeeDenied, NearTeeError)):
        return exc.code
    return None
