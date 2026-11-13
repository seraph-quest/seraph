"""One governed fixed-route HTTPS request; no SDK, fallback or hidden retry."""


from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
import hashlib
import json
import time
from uuid import uuid4

import httpx  # noqa: F401  (tests intercept near_text.httpx.AsyncClient)

from src.security.trust_contract import (
    AuthorityGrant, DestinationClass, DigestSentinel, TrustDestination,
    TrustOperation, TrustRequest, TrustResource, authority_scope_digest,
    canonical_digest, evaluate_trust, trust_request_digest,
)
from .contracts import EndpointClass, InferenceRequestContext, ModelRouteCandidate, RouteDecision, RouteRejection, bind_final_inference_payload
from .execution import RouteReceiptHooks, run_preflighted_adapter
from .near_client import NEAR_API_BASE, near_http_client, read_near_billing, read_near_response, strict_json_object
from .near_text_contracts import NearTextAnswer, NearTextError, NearTextInput, NearTextReceipt


PROFILE = "near.text"
MODEL = "z-ai/glm-5.3-flash"
BASE = NEAR_API_BASE


def _json_object(raw):
    return strict_json_object(raw)


def preflight_near_text(context, profile):
    """Purpose-policy authorization, never a claim of provider availability."""
    if (context.runtime_path != "near_text_native" or context.fallback_allowed
            or context.allowed_profile_ids != (PROFILE,)
            or context.allowed_provider_kinds != ("near",)
            or context.requirements.capabilities != ("text",)
            or profile.id != PROFILE or profile.provider_kind != "near"
            or profile.model != MODEL or profile.api_base != BASE
            or profile.routing_model not in ("", MODEL) or profile.fallback_models
            or profile.capabilities != ("text",) or profile.options
            or profile.transport_adapter != "openai_compatible_chat"
            or not profile.enabled or profile.follow_redirects):
        raise NearTextError("near_text_route_invalid")
    endpoint = BASE + "/chat/completions"
    candidate = ModelRouteCandidate(profile, endpoint, EndpointClass.REMOTE,
                                    "openai_compatible_chat", "primary")
    destination = TrustDestination("model:" + hashlib.sha256(endpoint.encode()).hexdigest(),
                                   DestinationClass.REMOTE_PROVIDER, endpoint)
    resource = TrustResource("model_endpoint", destination.destination_id, DigestSentinel.NO_OBJECT)
    token = uuid4().hex
    request = TrustRequest(
        principal=context.principal, provenance=context.provenance, destination=destination,
        operation=TrustOperation.MODEL_INFERENCE, required_grant=AuthorityGrant.MODEL_INFERENCE,
        capability_id="model:" + PROFILE, capability_version=profile.contract_hash,
        data_digest=context.data_digest, secret_scope_digest=DigestSentinel.NO_SECRET_SCOPE,
        resource_limits_digest=canonical_digest(asdict(context.requirements)),
        transformation_digest=context.transformation_digest, authority_scope_digest="",
        resource=resource, session_id=context.session_id, job_id=context.job_id,
        request_id=context.request_id, attempt_id="attempt:" + token, replay_id="replay:" + token,
        decision_expires_at=min(time.time() + 60, context.deadline_at),
        egress_class=context.egress_class, redaction_applied=context.redaction_applied,
    )
    request = replace(request, authority_scope_digest=authority_scope_digest(
        required_grant=request.required_grant, capability_id=request.capability_id,
        destination=request.destination, resource=request.resource))
    decision = evaluate_trust(request)
    return RouteDecision(candidate if decision.allowed else None, trust_request_digest(request),
        decision.decision_id, () if decision.allowed else (RouteRejection(PROFILE, decision.reason_code),),
        request.attempt_id, request.replay_id, "route:" + canonical_digest((context.request_id, decision.decision_id)))


async def _read_response(client, path, *, body, key, timeout, cap):
    # ADR-035: near_text rides the shared NEAR client, which sends the
    # canonical id with ``x-no-aliasing: true`` on every request.
    return await read_near_response(client, path, body=body, key=key, timeout=timeout, cap=cap,
        no_aliasing=True, error=NearTextError, prefix="near_text")


def _answer(payload):
    if not isinstance(payload, dict) or payload.get("model") != MODEL:
        raise NearTextError("near_text_model_mismatch")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise NearTextError("near_text_choice_invalid")
    choice = choices[0]
    message = choice.get("message")
    if (choice.get("finish_reason") != "stop" or not isinstance(message, dict)
            or message.get("role") != "assistant" or message.get("tool_calls") is not None
            or message.get("function_call") is not None or message.get("refusal") is not None):
        raise NearTextError("near_text_answer_invalid")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 65536:
        raise NearTextError("near_text_answer_invalid")
    return text


async def invoke_near_text(*, context: InferenceRequestContext, question: str,
        max_output_tokens: int, validate_current: Callable[[], Awaitable[None]],
        hooks: RouteReceiptHooks) -> NearTextAnswer:
    from .configuration import near_text_profile_for_setup, capture_near_text_credential
    from .effective_policy import current_near_text_policy
    from .near_text_billing import derive_near_inference_id
    from .accounting import current_near_accounting_operation_id, capture_near_billing_evidence

    inputs = NearTextInput(schema_version="seraph.near.text.input.v1", question=question,
                           max_output_tokens=max_output_tokens)
    if context.requirements.output_tokens != inputs.max_output_tokens:
        raise NearTextError("near_text_input_binding_invalid")
    await validate_current()
    configuration, policy_digest = current_near_text_policy()
    setup = configuration.near_text
    if setup is None or inputs.max_output_tokens > setup.max_output_tokens:
        raise NearTextError("near_text_output_cap_exceeded")
    key = await capture_near_text_credential(expected_revision=configuration.egress_revision,
                                           expected_fingerprint=setup.credential_fingerprint)
    body = {"model": MODEL, "messages": [{"role": "user", "content": inputs.question}],
            "max_tokens": inputs.max_output_tokens, "n": 1, "stream": False}
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > 65536:
        raise NearTextError("near_text_request_too_large")
    bound_context = bind_final_inference_payload(context, body)
    decision = preflight_near_text(bound_context, near_text_profile_for_setup(setup))

    async def adapter(candidate, fallback):
        await validate_current()
        current, current_digest = current_near_text_policy()
        if (current_digest != policy_digest or current.near_text is None
                or inputs.max_output_tokens > current.near_text.max_output_tokens):
            raise NearTextError("near_text_policy_changed")
        operation_id = current_near_accounting_operation_id()
        async with near_http_client() as client:
            raw, headers = await _read_response(client, "/chat/completions", body=encoded, key=key,
                timeout=min(45, setup.timeout_seconds, context.deadline_at - time.time()), cap=262144)
            try:
                payload = _json_object(raw)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise NearTextError("near_text_response_invalid") from None
            identity = derive_near_inference_id(body_id=payload.get("id") if isinstance(payload, dict) else None,
                                               inference_id_header=headers.get("inference-id"))
            provider_id = identity.provider_request_id
            answer_invalid = False
            try:
                text = _answer(payload)
            except (NearTextError, UnicodeEncodeError):
                text = None
                answer_invalid = True
            del raw, payload
            evidence = await read_near_billing(client, key=key, identity=identity,
                operation_id=operation_id, deadline_at=context.deadline_at, no_aliasing=True,
                error=NearTextError, prefix="near_text", capture=capture_near_billing_evidence)
            if evidence is None:
                text = None
                raise NearTextError("near_text_cost_liability")
            if answer_invalid:
                text = None
                raise NearTextError("near_text_answer_invalid") from None
            if evidence.cost_microusd > setup.request_cost_bound_microusd:
                text = None
                raise NearTextError("near_text_cost_overrun")
            await validate_current()
            _, adoption_policy = current_near_text_policy()
            if adoption_policy != policy_digest or time.time() >= context.deadline_at:
                text = None
                raise NearTextError("near_text_adoption_stale")
            return NearTextAnswer(text, NearTextReceipt(job_id=context.job_id, request_id=context.request_id,
                operation_id=operation_id, input_digest=context.data_digest,
                output_digest=hashlib.sha256(text.encode()).hexdigest(), policy_digest=policy_digest,
                provider_request_id=provider_id, cost_microusd=evidence.cost_microusd,
                cost_nano_usd=evidence.cost_nano_usd,
                billing_response_digest=evidence.response_sha256,
                cost_reference=operation_id))

    return await run_preflighted_adapter(context=bound_context, decision=decision, adapter=adapter, hooks=hooks)
