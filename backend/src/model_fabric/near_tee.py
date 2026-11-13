"""NEAR TEE text-slot provider adapter (ADR-035, non-streaming).

A typed adapter of the existing model fabric: the caller's canonical context
(``completion_with_fallback`` -> selector) is preflighted here before any
admission, then the transport runs inside the one shared broker lease with
the durable accounting reservation. There is no fallback between slots,
models or providers and no new lane, queue, budget or ledger.

Request contract: the stored canonical id with ``x-no-aliasing: true``; the
response ``model`` must equal it; ``reasoning_content`` (or any reasoning
field) is never answer text and is never retained. NEAR ``/v1/billing/costs``
readback runs inside the lease; no authoritative cost makes the operation an
Unknown liability and keeps the serial lease blocked until fenced
settlement. Known-cost failures (echo mismatch, empty output, overrun) are
returned as outcomes so the lease is released with the settled charge and
no output is adopted.

No import-time side effects; no global credential holder: the write-only
vault key is captured per request with a fingerprint/revision fence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time

from .contracts import (
    NEAR_TEE_PROVIDER_KIND,
    InferenceRequestContext,
    ModelCapability,
    NoCompliantModelRouteError,
    RouteDecision,
    bind_final_inference_payload,
)
from .near_client import NO_ALIASING_HEADER, near_request_headers


NEAR_TEE_BILLING_RESERVE_SECONDS = 16.0
NEAR_TEE_RESPONSE_CAP = 262144
NEAR_TEE_REQUEST_CAP = 4 * 1024 * 1024
NEAR_TEE_ANSWER_CAP = 262144


class NearTeeError(RuntimeError):
    """Content-safe NEAR TEE failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class NearTeeDenied(NoCompliantModelRouteError):
    """Precontact denial: nothing was reserved or sent."""

    def __init__(self, code: str, decision: RouteDecision | None = None):
        super().__init__((code,))
        self.code = code
        self.decision = decision


@dataclass(frozen=True)
class NearTeeCall:
    context: InferenceRequestContext
    decision: RouteDecision
    proof_hashes: tuple[str, ...]
    body: dict[str, object] = field(repr=False)
    key: str = field(repr=False)
    slot_name: str = "text"
    request_cost_bound_microusd: int = 0
    inference_timeout_seconds: float = 0.0
    headers: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class NearTeeOutcome:
    text: str | None = field(repr=False)
    failure_code: str | None
    provider_request_id: str | None
    cost_microusd: int | None


def assert_no_aliasing(headers: dict[str, str]) -> None:
    """A request built without ``x-no-aliasing: true`` is rejected before contact."""
    if headers.get(NO_ALIASING_HEADER) != "true":
        raise NearTeeError("near_tee_no_aliasing_required")


def near_tee_answer(payload: object, model: str) -> tuple[str | None, str | None]:
    """Return (answer, failure). Reasoning fields are never read as answer text."""
    if not isinstance(payload, dict) or payload.get("model") != model:
        return None, "near_tee_model_mismatch"
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None, "near_tee_answer_invalid"
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
        return None, "near_tee_answer_invalid"
    if message.get("tool_calls") or message.get("function_call") is not None:
        return None, "near_tee_tools_unavailable"
    if message.get("refusal") is not None:
        return None, "near_tee_answer_refused"
    if choice.get("finish_reason") not in ("stop", "length"):
        return None, "near_tee_answer_invalid"
    content = message.get("content")
    if content is None or isinstance(content, str) and not content.strip():
        # Reasoning with empty answer content is an empty output.
        return None, "near_tee_empty_output"
    if not isinstance(content, str):
        return None, "near_tee_answer_invalid"
    try:
        if len(content.encode("utf-8")) > NEAR_TEE_ANSWER_CAP:
            return None, "near_tee_answer_invalid"
    except UnicodeEncodeError:
        return None, "near_tee_answer_invalid"
    return content, None


def _transport_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    sanitized = []
    for message in messages:
        if not isinstance(message, dict):
            raise NearTeeDenied("near_tee_request_invalid")
        role, content = message.get("role"), message.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            # Images, tool messages and tool calls belong to later slots (PR3c/PR4).
            raise NearTeeDenied("near_tee_request_unsupported")
        sanitized.append({"role": role, "content": content})
    if not sanitized:
        raise NearTeeDenied("near_tee_request_invalid")
    return sanitized


async def prepare_near_tee_call(
    *,
    context: InferenceRequestContext,
    messages: list[dict[str, object]],
    temperature: float,
    max_tokens: int,
    now: float | None = None,
) -> NearTeeCall:
    """All precontact gates; raises ``NearTeeDenied`` before any admission."""
    from .configuration import (
        NEAR_TEE_IMPLEMENTED_SLOTS, capture_near_tee_credential, near_tee_profile_for_slot,
        near_tee_slot, route_slot_for_task_class,
    )
    from .effective_policy import current_near_tee_policy
    from .near_tee_catalog import catalog_receipt_block_reason
    from .repository import model_fabric_repository
    from .selector import candidate_from_profile, select_route

    if context.allowed_provider_kinds != (NEAR_TEE_PROVIDER_KIND,) or context.fallback_allowed:
        raise NearTeeDenied("near_tee_route_invalid")
    try:
        slot_name = route_slot_for_task_class(context.requirements.task_class)
    except ValueError as exc:
        raise NearTeeDenied(str(exc)) from None
    if slot_name not in NEAR_TEE_IMPLEMENTED_SLOTS:
        raise NearTeeDenied("near_tee_slot_not_implemented")
    if slot_name != "text":
        # The vision slot has its own image adapter (near_tee_vision.py).
        raise NearTeeDenied("near_tee_slot_adapter_mismatch")
    capabilities = set(context.requirements.capabilities)
    if ModelCapability.STREAMING.value in capabilities:
        raise NearTeeDenied("near_tee_streaming_unavailable")
    if ModelCapability.TOOL_USE.value in capabilities:
        raise NearTeeDenied("near_tee_tools_unavailable")
    try:
        configured, _digest = current_near_tee_policy(slot_name)
    except PermissionError as exc:
        raise NearTeeDenied(str(exc) or "near_tee_policy_unavailable") from None
    slot = near_tee_slot(configured, slot_name)
    profile = near_tee_profile_for_slot(slot_name, slot)
    if context.allowed_profile_ids != (profile.id,):
        raise NearTeeDenied("profile_not_allowed")
    if reason := catalog_receipt_block_reason(slot_name, profile, now=now, revision=configured.egress_revision):
        raise NearTeeDenied(reason)
    try:
        key = await capture_near_tee_credential(expected_revision=configured.egress_revision,
            expected_fingerprint=configured.near_tee.credential_fingerprint)
    except PermissionError as exc:
        raise NearTeeDenied(str(exc) or "near_tee_credential_unavailable") from None
    body = {
        "model": slot.model_id,
        "messages": _transport_messages(messages),
        "max_tokens": max(1, min(int(max_tokens), slot.max_output_tokens)),
        "temperature": float(slot.temperature if slot.temperature is not None else temperature),
        "n": 1,
        "stream": False,
    }
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) > NEAR_TEE_REQUEST_CAP:
        raise NearTeeDenied("near_tee_request_too_large")
    bound = bind_final_inference_payload(context, body)
    checked = time.time() if now is None else now
    inference_timeout = min(float(slot.timeout_seconds),
        bound.deadline_at - checked - NEAR_TEE_BILLING_RESERVE_SECONDS)
    if inference_timeout < 1:
        # The billing window is reserved out of the original deadline.
        raise NearTeeDenied("near_tee_deadline_insufficient")
    candidate = candidate_from_profile(profile)
    proofs = []
    for capability in sorted(capabilities | {"health", "latency_ms"}):
        proof = await model_fabric_repository.latest_capability_proof(
            profile_schema_version=profile.schema_version, profile_contract_hash=profile.contract_hash,
            profile_id=profile.id, model=profile.model, endpoint=candidate.endpoint,
            endpoint_class=candidate.endpoint_class, adapter=candidate.adapter, capability=capability)
        if proof is not None:
            proofs.append(proof)
    decision = select_route(bound, (candidate,), tuple(proofs), now=now,
        target_model_id=slot.model_id, target_api_key=key)
    if not decision.allowed:
        reason = next((item.reason_code for item in decision.rejections), "no_compliant_route")
        raise NearTeeDenied(reason, decision)
    headers = near_request_headers(key=key, no_aliasing=True, json_body=True)
    return NearTeeCall(context=bound, decision=decision, proof_hashes=decision.proof_hashes, body=body,
        key=key, slot_name=slot_name, request_cost_bound_microusd=slot.request_cost_bound_microusd,
        inference_timeout_seconds=inference_timeout, headers=headers)


def near_tee_precontact_reason(call: NearTeeCall, *, now: float | None = None) -> str | None:
    return precontact_reason(headers=call.headers, deadline_at=call.context.deadline_at,
        inference_timeout_seconds=call.inference_timeout_seconds, now=now)


def precontact_reason(*, headers: dict[str, str], deadline_at: float, inference_timeout_seconds: float,
                      now: float | None = None) -> str | None:
    """Checks that must hold when bytes would leave; run under the contact writer.

    Evaluated before contact is recorded, so a failure is a durable
    never-contacted denial (reservation released, lease not blocked), never
    an Unknown liability.
    """
    from .accounting import assert_current_inference_policy
    try:
        assert_no_aliasing(headers)
    except NearTeeError as exc:
        return exc.code
    try:
        assert_current_inference_policy()
    except Exception as exc:
        return getattr(exc, "code", None) or "provider_policy_revision_changed"
    checked = time.time() if now is None else now
    if min(inference_timeout_seconds, deadline_at - checked - NEAR_TEE_BILLING_RESERVE_SECONDS) < 1:
        # Admitted too late to leave the billing window inside the deadline.
        return "near_tee_deadline_insufficient"
    return None


async def near_tee_transport(call: NearTeeCall) -> NearTeeOutcome:
    """The admitted provider callback; runs only inside the broker lease."""
    from .accounting import capture_near_billing_evidence, current_near_accounting_operation_id
    from .near_client import near_http_client, read_near_billing, read_near_response, strict_json_object
    from .near_text_billing import NearTextBillingError, derive_near_inference_id

    # The precontact guard already ran under the contact writer; the header
    # is fixed here and the accounting layer rechecks policy after contact.
    operation_id = current_near_accounting_operation_id()
    encoded = json.dumps(call.body, ensure_ascii=False, separators=(",", ":")).encode()
    model = str(call.body["model"])
    deadline = call.context.deadline_at
    async with near_http_client() as client:
        # The guard proved >= 1s at contact; never let elapsed contact
        # bookkeeping turn into a zero/negative pre-send timeout.
        timeout = max(0.5, min(call.inference_timeout_seconds,
            deadline - time.time() - NEAR_TEE_BILLING_RESERVE_SECONDS))
        raw, headers = await read_near_response(client, "/chat/completions", body=encoded, key=call.key,
            timeout=timeout, cap=NEAR_TEE_RESPONSE_CAP, no_aliasing=True, error=NearTeeError, prefix="near_tee")
        try:
            payload = strict_json_object(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise NearTeeError("near_tee_response_invalid") from None
        try:
            identity = derive_near_inference_id(
                body_id=payload.get("id") if isinstance(payload, dict) else None,
                inference_id_header=headers.get("inference-id"))
        except NearTextBillingError:
            raise NearTeeError("near_tee_billing_identity_invalid") from None
        text, failure = near_tee_answer(payload, model)
        del raw, payload
        evidence = await read_near_billing(client, key=call.key, identity=identity, operation_id=operation_id,
            deadline_at=deadline, no_aliasing=True, error=NearTeeError, prefix="near_tee",
            capture=capture_near_billing_evidence)
    if evidence is None:
        # Contact happened and no authoritative cost arrived: Unknown liability.
        raise NearTeeError("near_tee_cost_liability")
    if failure is None and evidence.cost_microusd > call.request_cost_bound_microusd:
        failure = "near_tee_cost_overrun"
    if failure is not None:
        text = None
    return NearTeeOutcome(text=text, failure_code=failure, provider_request_id=identity.provider_request_id,
        cost_microusd=evidence.cost_microusd)


# Capability checks each slot may run (mechanics only, never evals).
NEAR_TEE_SLOT_CHECK_CAPABILITIES = {
    "text": frozenset({"text", "structured_output", "health", "latency_ms"}),
    "vision": frozenset({"vision", "structured_output", "health", "latency_ms"}),
}
NEAR_TEE_CHECK_CAPABILITIES = frozenset().union(*NEAR_TEE_SLOT_CHECK_CAPABILITIES.values())


async def near_tee_capability_check_transport(*, profile, capability: str, body: dict[str, object],
                                              key: str, deadline_at: float):
    """Operator-triggered capability check mechanics for a NEAR TEE slot.

    Runs inside the probe's broker lease with its reservation; request and
    response mechanics only (no quality score, corpus or judge). Missing
    billing is an Unknown liability (raised); known-cost failures are failed
    observations.
    """
    from .accounting import capture_near_billing_evidence, current_near_accounting_operation_id
    from .near_client import near_http_client, read_near_billing, read_near_response, strict_json_object
    from .near_text_billing import NearTextBillingError, derive_near_inference_id
    from .probe import CapabilityProbeObservation

    if capability not in NEAR_TEE_CHECK_CAPABILITIES or body.get("model") != profile.model or body.get("stream"):
        raise NearTeeError("near_tee_capability_check_unsupported")
    # Header, policy and billing-window checks run as the precontact guard
    # bound by the capability-check endpoint, before contact is recorded.
    operation_id = current_near_accounting_operation_id()
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    async with near_http_client() as client:
        started = time.monotonic()
        raw, response_headers = await read_near_response(client, "/chat/completions", body=encoded, key=key,
            timeout=max(0.5, deadline_at - time.time() - NEAR_TEE_BILLING_RESERVE_SECONDS), cap=NEAR_TEE_RESPONSE_CAP,
            no_aliasing=True, error=NearTeeError, prefix="near_tee")
        elapsed_ms = max(int((time.monotonic() - started) * 1000), 0)
        try:
            payload = strict_json_object(raw)
            identity = derive_near_inference_id(
                body_id=payload.get("id") if isinstance(payload, dict) else None,
                inference_id_header=response_headers.get("inference-id"))
        except (ValueError, UnicodeDecodeError, RecursionError, NearTextBillingError):
            raise NearTeeError("near_tee_billing_identity_invalid") from None
        text, failure = near_tee_answer(payload, profile.model)
        del raw, payload
        evidence = await read_near_billing(client, key=key, identity=identity, operation_id=operation_id,
            deadline_at=deadline_at, no_aliasing=True, error=NearTeeError, prefix="near_tee",
            capture=capture_near_billing_evidence)
    if evidence is None:
        raise NearTeeError("near_tee_cost_liability")
    if failure is not None:
        return CapabilityProbeObservation(False, error_code=failure)
    if capability == "structured_output":
        try:
            if not isinstance(json.loads(text), dict):
                return CapabilityProbeObservation(False, error_code="canary_shape_invalid")
        except (TypeError, ValueError):
            return CapabilityProbeObservation(False, error_code="canary_shape_invalid")
    value = {"latency_ms": elapsed_ms, "health": "healthy"}.get(capability, "verified")
    return CapabilityProbeObservation(True, proven_value=value)
