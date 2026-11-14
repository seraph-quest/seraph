"""NEAR TEE text-slot provider adapter (ADR-035).

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

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
import json
import re
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
NEAR_TEE_STREAM_CAP = 2 * 1024 * 1024
NEAR_TEE_MAX_TOOLS = 64
NEAR_TEE_MAX_TOOL_CALLS = 16
NEAR_TEE_TOOL_ARGUMENTS_CAP = 65536
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOOL_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


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
    # PR3c: streaming answer deltas, or one agent message that may carry
    # OpenAI-compatible tool calls (never both).
    stream: bool = False
    tool_names: tuple[str, ...] = ()
    tool_choice: object = None


@dataclass(frozen=True)
class NearTeeOutcome:
    text: str | None = field(repr=False)
    failure_code: str | None
    provider_request_id: str | None
    cost_microusd: int | None
    message: dict | None = field(default=None, repr=False)


@dataclass
class NearTeeStreamState:
    """Outcome of one admitted NEAR TEE stream, read after the lease closes.

    Deltas already yielded are never retracted; a known-cost failure (echo
    mismatch, deadline cut, protocol error, overrun) is recorded here so the
    broker settles the charge and releases the lease, and the caller then
    fails the operation without adopting it as a completed answer.
    """

    failure_code: str | None = None
    provider_request_id: str | None = None
    cost_microusd: int | None = None
    delivered: bool = False
    stream_ended_at: float | None = None
    # Set by the consumer side when it stops reading (closed generator):
    # the transport ends the provider stream at its next resumption and
    # still reads billing back inside the lease.
    stop_requested: bool = False
    # The caller's task was cancelled while the stream was open; billing
    # was still read back and the cancellation is re-raised by the caller.
    caller_cancelled: bool = False


def assert_no_aliasing(headers: dict[str, str]) -> None:
    """A request built without ``x-no-aliasing: true`` is rejected before contact."""
    if headers.get(NO_ALIASING_HEADER) != "true":
        raise NearTeeError("near_tee_no_aliasing_required")


def near_tee_finish_reason(payload: object) -> object:
    """The single choice's ``finish_reason`` (None when the shape is unusual)."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        return choices[0].get("finish_reason")
    return None


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
        if choice.get("finish_reason") == "length":
            # The output bound ended the answer before any content, e.g. a
            # reasoning model spent max_tokens on reasoning (#1167).
            return None, "near_tee_output_truncated"
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


def _text_content(content: object) -> str | list[dict[str, str]] | None:
    """Plain text or a list of text parts; images belong to the vision slot (PR4)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(isinstance(part, dict) and part.get("type") == "text"
            and set(part) == {"type", "text"} and isinstance(part["text"], str) for part in content):
        return [{"type": "text", "text": part["text"]} for part in content]
    return None


def _transport_tool_calls(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= NEAR_TEE_MAX_TOOL_CALLS:
        raise NearTeeDenied("near_tee_request_invalid")
    calls = []
    for item in value:
        function = item.get("function") if isinstance(item, dict) else None
        if (not isinstance(function, dict) or item.get("type", "function") != "function"
                or not isinstance(item.get("id"), str) or not isinstance(function.get("name"), str)):
            raise NearTeeDenied("near_tee_request_invalid")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False)
        calls.append({"id": item["id"], "type": "function",
            "function": {"name": function["name"], "arguments": arguments}})
    return calls


def _transport_messages(messages: list[dict[str, object]], *, tools: bool = False) -> list[dict[str, object]]:
    sanitized = []
    for message in messages:
        if not isinstance(message, dict):
            raise NearTeeDenied("near_tee_request_invalid")
        role, content = message.get("role"), _text_content(message.get("content"))
        if tools and role == "assistant" and message.get("tool_calls"):
            sanitized.append({"role": "assistant", "content": content if content else None,
                "tool_calls": _transport_tool_calls(message["tool_calls"])})
            continue
        if tools and role == "tool":
            if content is None or not isinstance(message.get("tool_call_id"), str):
                raise NearTeeDenied("near_tee_request_invalid")
            sanitized.append({"role": "tool", "tool_call_id": message["tool_call_id"], "content": content})
            continue
        if role not in {"system", "user", "assistant"} or content is None:
            # Images belong to the vision slot (PR4); tool turns need a tools request.
            raise NearTeeDenied("near_tee_request_unsupported")
        sanitized.append({"role": role, "content": content})
    if not sanitized:
        raise NearTeeDenied("near_tee_request_invalid")
    return sanitized


def _transport_tools(tools: object) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    if not isinstance(tools, list) or not 1 <= len(tools) <= NEAR_TEE_MAX_TOOLS:
        raise NearTeeDenied("near_tee_request_invalid")
    sanitized, names = [], []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if (not isinstance(function, dict) or tool.get("type") != "function"
                or not isinstance(function.get("name"), str) or _TOOL_NAME.fullmatch(function["name"]) is None
                or function["name"] in names or not isinstance(function.get("parameters", {}), dict)
                or not isinstance(function.get("description", ""), str)):
            raise NearTeeDenied("near_tee_request_invalid")
        entry = {"name": function["name"], "parameters": function.get("parameters", {"type": "object", "properties": {}})}
        if function.get("description"):
            entry["description"] = function["description"]
        sanitized.append({"type": "function", "function": entry})
        names.append(function["name"])
    return sanitized, tuple(names)


def _transport_tool_choice(choice: object, names: tuple[str, ...]) -> object:
    if choice in ("auto", "required", "none"):
        return choice
    if (isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("function"), dict)
            and choice["function"].get("name") in names):
        return {"type": "function", "function": {"name": choice["function"]["name"]}}
    raise NearTeeDenied("near_tee_request_invalid")


def near_tee_agent_message(payload: object, model: str, *, tool_names: tuple[str, ...],
                           tool_choice: object) -> tuple[dict | None, str | None]:
    """Return (assistant message, failure) for a tools-capable agent turn.

    Reasoning fields are never read. Tool-call arguments are parsed as strict
    JSON objects (duplicate keys, non-finite constants and invalid JSON are
    rejected); a call to a tool that was not offered fails closed, and a
    forced ``tool_choice`` must be honoured exactly.
    """
    from .near_client import strict_json_object

    if not isinstance(payload, dict) or payload.get("model") != model:
        return None, "near_tee_model_mismatch"
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None, "near_tee_answer_invalid"
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
        return None, "near_tee_answer_invalid"
    if message.get("function_call") is not None:
        return None, "near_tee_answer_invalid"
    if message.get("refusal") is not None:
        return None, "near_tee_answer_refused"
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        return None, "near_tee_answer_invalid"
    try:
        if content is not None and len(content.encode("utf-8")) > NEAR_TEE_ANSWER_CAP:
            return None, "near_tee_answer_invalid"
    except UnicodeEncodeError:
        return None, "near_tee_answer_invalid"
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list) or len(raw_calls) > NEAR_TEE_MAX_TOOL_CALLS:
        return None, "near_tee_answer_invalid"
    calls = []
    for item in raw_calls:
        function = item.get("function") if isinstance(item, dict) else None
        if (not isinstance(function, dict) or item.get("type", "function") != "function"
                or not isinstance(item.get("id"), str) or _TOOL_CALL_ID.fullmatch(item["id"]) is None
                or not isinstance(function.get("name"), str) or not isinstance(function.get("arguments"), str)):
            return None, "near_tee_tool_call_invalid"
        if function["name"] not in tool_names:
            return None, "near_tee_tool_call_not_offered"
        raw_arguments = function["arguments"].encode("utf-8", errors="strict") if function["arguments"] else b"{}"
        if len(raw_arguments) > NEAR_TEE_TOOL_ARGUMENTS_CAP:
            return None, "near_tee_tool_arguments_invalid"
        try:
            arguments = strict_json_object(raw_arguments)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None, "near_tee_tool_arguments_invalid"
        if not isinstance(arguments, dict):
            return None, "near_tee_tool_arguments_invalid"
        calls.append({"id": item["id"], "type": "function",
            "function": {"name": function["name"], "arguments": arguments}})
    if tool_choice == "none" and calls:
        return None, "near_tee_tool_choice_violated"
    if tool_choice == "required" and not calls:
        return None, "near_tee_tool_call_required"
    if isinstance(tool_choice, dict) and [call["function"]["name"] for call in calls] != [tool_choice["function"]["name"]]:
        return None, "near_tee_tool_choice_violated"
    finish = choice.get("finish_reason")
    if calls:
        if finish not in ("tool_calls", "stop"):
            return None, "near_tee_answer_invalid"
    else:
        if finish not in ("stop", "length"):
            return None, "near_tee_answer_invalid"
        if content is None or not content.strip():
            # Reasoning with empty answer content is an empty output.
            return None, "near_tee_empty_output"
    result: dict[str, object] = {"role": "assistant", "content": content if content else None}
    if calls:
        result["tool_calls"] = calls
    return result, None


async def prepare_near_tee_call(
    *,
    context: InferenceRequestContext,
    messages: list[dict[str, object]],
    temperature: float,
    max_tokens: int,
    now: float | None = None,
    stream: bool = False,
    tools: list[dict[str, object]] | None = None,
    tool_choice: object = None,
    stop: list[str] | None = None,
    response_format: dict[str, object] | None = None,
) -> NearTeeCall:
    """All precontact gates; raises ``NearTeeDenied`` before any admission.

    ``stream`` must match the context's streaming capability and ``tools``
    its tool-use capability. Tool requests need a current catalog receipt
    whose ``supportedFeatures`` include ``tools``; streaming with tools is
    not offered (the agent tool turn is one non-streaming message).
    """
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
        # Chat completions serve the text slot only; the vision slot has its
        # own image adapter (near_tee_vision.py) and embeddings use theirs.
        raise NearTeeDenied("near_tee_slot_adapter_mismatch")
    capabilities = set(context.requirements.capabilities)
    if (ModelCapability.STREAMING.value in capabilities) != bool(stream):
        raise NearTeeDenied("near_tee_streaming_mismatch")
    if (ModelCapability.TOOL_USE.value in capabilities) != bool(tools):
        raise NearTeeDenied("near_tee_tool_use_mismatch")
    if stream and tools:
        raise NearTeeDenied("near_tee_streaming_tools_unsupported")
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
    if tools:
        from .near_tee_catalog import catalog_receipt_feature_block_reason
        if reason := catalog_receipt_feature_block_reason(slot_name, "tools"):
            raise NearTeeDenied(reason)
    try:
        key = await capture_near_tee_credential(expected_revision=configured.egress_revision,
            expected_fingerprint=configured.near_tee.credential_fingerprint)
    except PermissionError as exc:
        raise NearTeeDenied(str(exc) or "near_tee_credential_unavailable") from None
    body = {
        "model": slot.model_id,
        "messages": _transport_messages(messages, tools=bool(tools)),
        "max_tokens": max(1, min(int(max_tokens), slot.max_output_tokens)),
        "temperature": float(slot.temperature if slot.temperature is not None else temperature),
        "n": 1,
        "stream": bool(stream),
    }
    tool_names: tuple[str, ...] = ()
    if tools:
        body["tools"], tool_names = _transport_tools(tools)
        tool_choice = _transport_tool_choice("auto" if tool_choice is None else tool_choice, tool_names)
        body["tool_choice"] = tool_choice
    else:
        tool_choice = None
    if stop:
        if (not isinstance(stop, list) or len(stop) > 4
                or any(not isinstance(item, str) or not item or len(item) > 256 for item in stop)):
            raise NearTeeDenied("near_tee_request_invalid")
        body["stop"] = list(stop)
    if response_format is not None:
        if (ModelCapability.STRUCTURED_OUTPUT.value not in capabilities or not isinstance(response_format, dict)
                or response_format.get("type") not in {"json_object", "json_schema"}):
            raise NearTeeDenied("near_tee_request_invalid")
        body["response_format"] = response_format
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
        inference_timeout_seconds=inference_timeout, headers=headers, stream=bool(stream),
        tool_names=tool_names, tool_choice=tool_choice)


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
        message = None
        if call.tool_names:
            message, failure = near_tee_agent_message(payload, model, tool_names=call.tool_names,
                tool_choice=call.tool_choice)
            text = message.get("content") if message is not None else None
        else:
            text, failure = near_tee_answer(payload, model)
            if failure is None and near_tee_finish_reason(payload) == "length" \
                    and ("response_format" in call.body or call.slot_name == "vision"):
                # A JSON answer (response_format, or the vision slot's strict
                # JSON either mode) cut at the output bound is never a valid
                # object; name it instead of a later parse error (#1167).
                failure = "near_tee_output_truncated"
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
        text = message = None
    return NearTeeOutcome(text=text, failure_code=failure, provider_request_id=identity.provider_request_id,
        cost_microusd=evidence.cost_microusd, message=message)


class _StreamReader:
    """Validates NEAR TEE stream events; answer deltas only, reasoning dropped."""

    def __init__(self, model: str):
        self.model = model
        self.body_id: str | None = None
        self.echo_checked = False
        self.finish_reason: str | None = None
        self.answer_bytes = 0

    def delta(self, event: object) -> tuple[str | None, str | None]:
        """Return (answer delta, failure code) for one parsed event."""
        if not isinstance(event, dict):
            return None, "near_tee_stream_invalid"
        if "error" in event:
            return None, "near_tee_stream_error"
        event_id = event.get("id")
        if event_id is not None:
            if not isinstance(event_id, str) or (self.body_id is not None and event_id != self.body_id):
                return None, "near_tee_stream_invalid"
            self.body_id = event_id
        if "model" in event:
            # The first chunk carrying ``model`` (and every later one) must
            # echo the canonical id; a mismatch stops the stream.
            if event["model"] != self.model:
                return None, "near_tee_model_mismatch"
            self.echo_checked = True
        choices = event.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            return None, "near_tee_stream_invalid"
        if not choices:
            return None, None
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index", 0) != 0:
            return None, "near_tee_stream_invalid"
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict) or delta.get("role", "assistant") not in ("assistant", None):
            return None, "near_tee_stream_invalid"
        if delta.get("tool_calls") or delta.get("function_call") is not None:
            return None, "near_tee_tools_unavailable"
        if delta.get("refusal") is not None:
            return None, "near_tee_answer_refused"
        finish = choice.get("finish_reason")
        if finish is not None:
            self.finish_reason = finish
        # ``reasoning_content`` / ``reasoning`` deltas are never read.
        content = delta.get("content")
        if content is None or content == "":
            return None, None
        if not isinstance(content, str):
            return None, "near_tee_stream_invalid"
        if not self.echo_checked:
            return None, "near_tee_model_echo_missing"
        try:
            self.answer_bytes += len(content.encode("utf-8"))
        except UnicodeEncodeError:
            return None, "near_tee_stream_invalid"
        if self.answer_bytes > NEAR_TEE_ANSWER_CAP:
            return None, "near_tee_answer_invalid"
        return content, None


async def _near_tee_stream_deltas(client, *, body: dict[str, object], key: str, stream_seconds: float,
                                  reader: _StreamReader, opened: list):
    """Yield validated answer deltas; raise ``NearTeeError`` on a stream failure."""
    from .near_client import stream_near_events

    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    stream_deadline = asyncio.get_running_loop().time() + stream_seconds
    async with aclosing(stream_near_events(client, "/chat/completions", body=encoded, key=key,
            stream_deadline=stream_deadline, cap=NEAR_TEE_STREAM_CAP, error=NearTeeError, prefix="near_tee",
            on_open=opened.append)) as events:
        async for event in events:
            text, failure = reader.delta(event)
            if failure is not None:
                raise NearTeeError(failure)
            if text:
                yield text


async def near_tee_stream_transport(call: NearTeeCall, state: NearTeeStreamState):
    """The admitted streaming callback; runs only inside the broker lease.

    Answer deltas are yielded as they arrive. The stream is ended no later
    than ``NEAR_TEE_BILLING_RESERVE_SECONDS`` before the original deadline,
    then NEAR billing is read back inside the same lease. A failure after
    contact with an authoritative cost is recorded on ``state`` (deltas
    already delivered stay delivered); no authoritative cost raises an
    Unknown liability and the serial lease stays blocked.

    Caller stop or cancellation once the response exists is not an
    uncertain outcome: the HTTP stream is closed, billing is read back
    (shielded, within the original deadline) and ``near_tee_stream_cancelled``
    is recorded so the known cost settles and the lease is released. A
    cancellation is flagged on ``state`` for the caller to re-raise.
    """
    from .accounting import capture_near_billing_evidence, current_near_accounting_operation_id
    from .near_client import near_http_client, read_near_billing
    from .near_text_billing import NearTextBillingError, derive_near_inference_id

    if not call.stream or call.tool_names or call.body.get("stream") is not True:
        raise NearTeeError("near_tee_stream_request_invalid")
    operation_id = current_near_accounting_operation_id()
    deadline = call.context.deadline_at
    reader = _StreamReader(str(call.body["model"]))
    opened: list = []
    failure = None
    async with near_http_client() as client:
        stream_seconds = max(0.5, min(call.inference_timeout_seconds,
            deadline - time.time() - NEAR_TEE_BILLING_RESERVE_SECONDS))
        try:
            async with aclosing(_near_tee_stream_deltas(client, body=call.body, key=call.key,
                    stream_seconds=stream_seconds, reader=reader, opened=opened)) as deltas:
                async for text in deltas:
                    state.delivered = True
                    yield text
                    if state.stop_requested:
                        # The consumer stopped reading; end the provider stream.
                        failure = "near_tee_stream_cancelled"
                        break
        except NearTeeError as exc:
            if not opened:
                # No response: the outcome of the request is uncertain.
                raise
            failure = exc.code
        except asyncio.CancelledError:
            if not opened:
                raise
            # The aclosing exit above already closed the HTTP response.
            state.caller_cancelled = True
            failure = "near_tee_stream_cancelled"
        state.stream_ended_at = time.time()
        try:
            identity = derive_near_inference_id(body_id=reader.body_id,
                inference_id_header=opened[0].get("inference-id"))
        except NearTextBillingError:
            raise NearTeeError("near_tee_billing_identity_invalid") from None
        billing = read_near_billing(client, key=call.key, identity=identity, operation_id=operation_id,
            deadline_at=deadline, no_aliasing=True, error=NearTeeError, prefix="near_tee",
            capture=capture_near_billing_evidence)
        # Shielded: a further cancellation cannot abandon the readback; if it
        # interrupts this await anyway, the outcome stays an Unknown liability.
        # Shield the readback whenever the caller stopped or cancelled: a second
        # cancel (e.g. task cleanup after a WS send failure) must not leave an
        # Unknown liability that blocks the whole lane.
        evidence = (await asyncio.shield(billing)) if (state.caller_cancelled or state.stop_requested) else await billing
    if evidence is None:
        raise NearTeeError("near_tee_cost_liability")
    if failure is None and not state.delivered:
        # Nothing delivered because the output bound ended the stream (e.g.
        # reasoning used max_tokens) is a truncation, not an empty answer.
        failure = "near_tee_output_truncated" if reader.finish_reason == "length" else "near_tee_empty_output"
    if failure is None and reader.finish_reason not in ("stop", "length"):
        failure = "near_tee_stream_incomplete"
    if failure is None and evidence.cost_microusd > call.request_cost_bound_microusd:
        failure = "near_tee_cost_overrun"
    state.failure_code = failure
    state.provider_request_id = identity.provider_request_id
    state.cost_microusd = evidence.cost_microusd


# Capability checks each chat-completions slot may run (mechanics only,
# never evals). The embedding slot's set lives in near_tee_embeddings.
NEAR_TEE_CHECK_CAPABILITIES = frozenset({"text", "structured_output", "streaming", "tool_use", "health", "latency_ms"})
NEAR_TEE_VISION_CHECK_CAPABILITIES = frozenset({"vision", "structured_output", "health", "latency_ms"})
NEAR_TEE_SLOT_CHECK_CAPABILITIES = {
    "text": NEAR_TEE_CHECK_CAPABILITIES,
    "vision": NEAR_TEE_VISION_CHECK_CAPABILITIES,
}


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

    streaming = capability == "streaming"
    allowed = NEAR_TEE_VISION_CHECK_CAPABILITIES if profile.id == "near_tee.vision" else NEAR_TEE_CHECK_CAPABILITIES
    if (capability not in allowed or body.get("model") != profile.model
            or bool(body.get("stream")) != streaming or bool(body.get("tools")) != (capability == "tool_use")):
        raise NearTeeError("near_tee_capability_check_unsupported")
    # Header, policy and billing-window checks run as the precontact guard
    # bound by the capability-check endpoint, before contact is recorded.
    operation_id = current_near_accounting_operation_id()
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    window = max(0.5, deadline_at - time.time() - NEAR_TEE_BILLING_RESERVE_SECONDS)
    async with near_http_client() as client:
        started = time.monotonic()
        if streaming:
            # Mechanics only: at least one answer delta under the same SSE
            # rules as chat streaming; the deltas are counted, never kept.
            reader = _StreamReader(profile.model)
            opened: list = []
            failure = None
            observed = False
            try:
                async with aclosing(_near_tee_stream_deltas(client, body=body, key=key, stream_seconds=window,
                        reader=reader, opened=opened)) as deltas:
                    async for _text in deltas:
                        observed = True
            except NearTeeError as exc:
                if not opened:
                    raise
                failure = exc.code
            if failure is None and not observed:
                failure = "stream_empty"
            text = None
            try:
                identity = derive_near_inference_id(body_id=reader.body_id,
                    inference_id_header=opened[0].get("inference-id"))
            except NearTextBillingError:
                raise NearTeeError("near_tee_billing_identity_invalid") from None
        else:
            raw, response_headers = await read_near_response(client, "/chat/completions", body=encoded, key=key,
                timeout=window, cap=NEAR_TEE_RESPONSE_CAP, no_aliasing=True, error=NearTeeError, prefix="near_tee")
            try:
                payload = strict_json_object(raw)
                identity = derive_near_inference_id(
                    body_id=payload.get("id") if isinstance(payload, dict) else None,
                    inference_id_header=response_headers.get("inference-id"))
            except (ValueError, UnicodeDecodeError, RecursionError, NearTextBillingError):
                raise NearTeeError("near_tee_billing_identity_invalid") from None
            if capability == "tool_use":
                names = tuple(tool["function"]["name"] for tool in body["tools"])
                message, failure = near_tee_agent_message(payload, profile.model, tool_names=names,
                    tool_choice=body.get("tool_choice", "auto"))
                if failure is None and not message.get("tool_calls"):
                    failure = "canary_shape_invalid"
                text = None
            else:
                text, failure = near_tee_answer(payload, profile.model)
                if failure is None and capability == "structured_output" \
                        and near_tee_finish_reason(payload) == "length":
                    failure = "near_tee_output_truncated"
            del raw, payload
        elapsed_ms = max(int((time.monotonic() - started) * 1000), 0)
        evidence = await read_near_billing(client, key=key, identity=identity, operation_id=operation_id,
            deadline_at=deadline_at, no_aliasing=True, error=NearTeeError, prefix="near_tee",
            capture=capture_near_billing_evidence)
    if evidence is None:
        raise NearTeeError("near_tee_cost_liability")
    if failure is not None:
        return CapabilityProbeObservation(False, error_code=failure)
    vision_mode = None
    if capability == "structured_output" and profile.id == "near_tee.vision":
        from .near_tee_vision import structured_check_mode
        vision_mode = structured_check_mode(body)
        if vision_mode is None:
            return CapabilityProbeObservation(False, error_code="near_tee_vision_check_mode_invalid")
    if capability == "structured_output":
        try:
            candidate_text = text
            if vision_mode == "vision_prompt_json":
                # Same fence tolerance as the runtime's strict contract parser.
                from src.observer.screenshot_analysis_contract import _strip_json_fence
                candidate_text = _strip_json_fence(text)
            if not isinstance(json.loads(candidate_text), dict):
                return CapabilityProbeObservation(False, error_code="canary_shape_invalid")
        except (TypeError, ValueError):
            return CapabilityProbeObservation(False, error_code="canary_shape_invalid")
    # A vision structured_output proof is bound to the request mode it exercised.
    value = vision_mode or {"latency_ms": elapsed_ms, "health": "healthy"}.get(capability, "verified")
    return CapabilityProbeObservation(True, proven_value=value)
