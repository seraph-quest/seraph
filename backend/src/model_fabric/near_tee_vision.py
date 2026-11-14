"""NEAR TEE vision-slot request preparation (ADR-035, PR4).

Builds the one screenshot request the governed vision slot may send: the
stored canonical model id, one image as a base64 data URL in an
``image_url`` content part, and either ``response_format`` json_schema (when
the slot's current catalog receipt lists NEAR's ``structured_outputs``
feature, or, when the catalog omits it, a current passing json_schema
``structured_output`` capability proof establishes it; #1167) or the prompt's
strict-JSON instruction. All gates run here before
admission and raise ``NearTeeDenied`` (nothing reserved or sent): slot
policy and purpose consent, catalog receipt freshness, the conservative
input-token estimate against ``max_input_tokens``, the request byte cap, the
billing window inside the deadline, the fingerprint-fenced credential and
the selector with fresh ``vision``/``structured_output`` proofs.

The transport itself is the shared non-streaming NEAR TEE transport
(``near_tee_transport``): ``x-no-aliasing``, the model echo check,
reasoning never read as answer text, and ``/v1/billing/costs`` readback
inside the admitted lease (no authoritative cost -> Unknown liability).

No import-time side effects; no global credential holder.
"""

from __future__ import annotations

import base64
from dataclasses import replace
import json
import math
import time

from .contracts import NEAR_TEE_PROVIDER_KIND, bind_final_inference_payload
from .near_client import near_request_headers
from .near_tee import NEAR_TEE_BILLING_RESERVE_SECONDS, NearTeeCall, NearTeeDenied


VISION_RUNTIME_PATH = "screenshot_image_analysis"
# ADR-035 / issue scope: one screenshot request stays at or under ~8 MiB.
NEAR_TEE_VISION_REQUEST_CAP = 8 * 1024 * 1024
# Conservative image-token design basis: one token per 28x28 pixel patch of
# the original image (ViT 14px patches with 2x2 merge, as in the allowlisted
# Qwen-VL/GLM-V families), no provider-side downscaling credit, plus a fixed
# per-image overhead. Providers that downscale can only use fewer tokens.
IMAGE_TOKEN_PATCH_PX = 28
IMAGE_TOKEN_OVERHEAD = 64
MAX_IMAGE_SIDE_PX = 32768
STRUCTURED_SCHEMA_NAME = "seraph_screenshot_analysis_v1"
# The structured_output capability proof of the vision slot is bound to the
# request mode the runtime uses; dispatch requires the effective mode
# (json_schema when the current catalog receipt lists structured_outputs or a
# current json_schema proof establishes it, otherwise prompt-JSON).
STRUCTURED_MODE_JSON_SCHEMA = "vision_json_schema"
STRUCTURED_MODE_PROMPT_JSON = "vision_prompt_json"
# Why the vision slot sends json_schema (#1167); None means prompt-JSON.
STRUCTURED_BASIS_CATALOG = "catalog"
STRUCTURED_BASIS_CAPABILITY_PROOF = "capability_proof"
_CHECK_SCHEMA_NAME = "seraph_capability_check_v1"
_CHECK_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
    "additionalProperties": False}
_ONE_PIXEL_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nGQAAAAASUVORK5CYII="


def structured_mode(structured: bool) -> str:
    return STRUCTURED_MODE_JSON_SCHEMA if structured else STRUCTURED_MODE_PROMPT_JSON


def vision_structured_basis(*, catalog_lists: bool, proof, failed_proof=None,
                            now: float | None = None) -> str | None:
    """Why the vision slot may send json_schema, or None for prompt-JSON (#1167).

    The catalog's ``structured_outputs`` feature, or, when NEAR's record omits
    it, a current passing ``structured_output`` capability proof that
    exercised the json_schema mode on this exact profile and that no newer
    failed json_schema (or unknown-mode) check contradicts. Without either the
    json_schema capability stays unavailable (fail closed to prompt-JSON,
    which is strictly validated). The proof is still required at dispatch.
    """
    from .proofs import proof_is_fresh
    if catalog_lists:
        return STRUCTURED_BASIS_CATALOG
    if proof is None or proof.proven_value != STRUCTURED_MODE_JSON_SCHEMA or not proof_is_fresh(proof, now=now):
        return None
    if (failed_proof is not None and failed_proof.checked_at >= proof.checked_at
            and failed_proof.proven_value in (None, STRUCTURED_MODE_JSON_SCHEMA)):
        # A newer (or same-instant: fail closed) failed json_schema check
        # withdraws the proof-based basis.
        return None
    return STRUCTURED_BASIS_CAPABILITY_PROOF


async def latest_structured_output_proof(profile, *, failed: bool = False):
    """The newest passing (or, with ``failed``, failed) ``structured_output``
    proof bound to this exact profile, or None."""
    from .repository import model_fabric_repository
    from .selector import candidate_from_profile
    candidate = candidate_from_profile(profile)
    lookup = (model_fabric_repository.latest_failed_capability_proof if failed
        else model_fabric_repository.latest_capability_proof)
    return await lookup(
        profile_schema_version=profile.schema_version, profile_contract_hash=profile.contract_hash,
        profile_id=profile.id, model=profile.model, endpoint=candidate.endpoint,
        endpoint_class=candidate.endpoint_class, adapter=candidate.adapter, capability="structured_output")


async def current_vision_structured_basis(profile, *, catalog_lists: bool, now: float | None = None) -> str | None:
    """``vision_structured_basis`` with this profile's stored proofs."""
    if catalog_lists:
        return STRUCTURED_BASIS_CATALOG
    return vision_structured_basis(catalog_lists=False, proof=await latest_structured_output_proof(profile),
        failed_proof=await latest_structured_output_proof(profile, failed=True), now=now)


def structured_check_body(*, model: str, structured: bool, max_tokens: int) -> dict[str, object]:
    """The vision ``structured_output`` capability-check body, in the runtime's mode:
    one image part plus json_schema (strict) or the prompt-JSON instruction."""
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": 'Return strict JSON only: {"ok": true}.'},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _ONE_PIXEL_PNG_B64}},
        ]}],
        "max_tokens": max_tokens,
    }
    if structured:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": _CHECK_SCHEMA_NAME,
            "strict": True, "schema": _CHECK_SCHEMA}}
    return body


def structured_check_mode(body: dict[str, object]) -> str | None:
    """The mode a structured_output check body exercises, or None when it is
    not a vision mode body (text-only or another response format)."""
    messages = body.get("messages")
    content = messages[0].get("content") if isinstance(messages, list) and messages and isinstance(messages[0], dict) else None
    if not isinstance(content, list) or not any(isinstance(part, dict) and part.get("type") == "image_url" for part in content):
        return None
    response_format = body.get("response_format")
    if response_format is None:
        return STRUCTURED_MODE_PROMPT_JSON
    if isinstance(response_format, dict) and response_format.get("type") == "json_schema" \
            and (response_format.get("json_schema") or {}).get("strict") is True:
        return STRUCTURED_MODE_JSON_SCHEMA
    return None


def estimate_image_tokens(width: int, height: int) -> int:
    if type(width) is not int or type(height) is not int or not 0 < width <= MAX_IMAGE_SIDE_PX \
            or not 0 < height <= MAX_IMAGE_SIDE_PX:
        raise NearTeeDenied("near_tee_vision_image_dimensions_invalid")
    return math.ceil(width / IMAGE_TOKEN_PATCH_PX) * math.ceil(height / IMAGE_TOKEN_PATCH_PX) + IMAGE_TOKEN_OVERHEAD


def estimate_vision_input_tokens(prompt: str, *, width: int, height: int) -> int:
    """Text at one token per 3 UTF-8 bytes (the fabric's conservative rule) plus the image."""
    text_tokens = max(1, (len(prompt.encode("utf-8")) + 2) // 3)
    return text_tokens + estimate_image_tokens(width, height)


def _response_format(schema: dict[str, object]) -> dict[str, object]:
    return {"type": "json_schema", "json_schema": {"name": STRUCTURED_SCHEMA_NAME, "strict": True,
        "schema": schema}}


async def prepare_near_tee_vision_call(
    *,
    prompt: str,
    image_bytes: bytes,
    media_type: str,
    width: int,
    height: int,
    response_schema: dict[str, object],
    now: float | None = None,
) -> tuple[NearTeeCall, bool]:
    """All precontact gates; returns the admitted call and whether it is structured.

    ``NearTeeDenied`` carries a stable reason and, once a canonical context
    exists, that context in ``denied_context`` so the caller can persist a
    denied route receipt.
    """
    from .caller_context import build_canonical_inference_context
    from .configuration import capture_near_tee_credential, near_tee_profile_for_slot, near_tee_slot
    from .effective_policy import current_near_tee_policy
    from .near_tee_catalog import catalog_receipt_block_reason, read_catalog_receipts, receipt_structured_outputs
    from .repository import model_fabric_repository
    from .selector import candidate_from_profile, select_route

    if media_type not in {"image/png", "image/jpeg"}:
        raise NearTeeDenied("near_tee_vision_media_type_unsupported")
    try:
        configured, _digest = current_near_tee_policy("vision")
    except PermissionError as exc:
        raise NearTeeDenied(str(exc) or "near_tee_policy_unavailable") from None
    slot = near_tee_slot(configured, "vision")
    profile = near_tee_profile_for_slot("vision", slot)
    receipts = read_catalog_receipts()
    if reason := catalog_receipt_block_reason("vision", profile, now=now, receipts=receipts,
            revision=configured.egress_revision):
        raise NearTeeDenied(reason)
    structured = await current_vision_structured_basis(profile,
        catalog_lists=receipt_structured_outputs("vision", receipts=receipts), now=now) is not None
    estimate = estimate_vision_input_tokens(prompt, width=width, height=height)
    if estimate > slot.max_input_tokens:
        # Rejected before contact (ADR-035): the image counts conservatively.
        raise NearTeeDenied("near_tee_vision_input_too_large")
    data_url = f"data:{media_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    body: dict[str, object] = {
        "model": slot.model_id,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        "max_tokens": slot.max_output_tokens,
        "temperature": float(slot.temperature if slot.temperature is not None else 0.0),
        "n": 1,
        "stream": False,
    }
    if structured:
        body["response_format"] = _response_format(response_schema)
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) > NEAR_TEE_VISION_REQUEST_CAP:
        raise NearTeeDenied("near_tee_vision_request_too_large")
    # The billing window is reserved out of the original deadline.
    context_timeout = float(slot.timeout_seconds) + NEAR_TEE_BILLING_RESERVE_SECONDS
    context = build_canonical_inference_context(VISION_RUNTIME_PATH, payload=body,
        output_tokens=slot.max_output_tokens, timeout_seconds=context_timeout)
    # Replace the byte-length proxy (which would count base64 as text) with
    # the conservative image-aware estimate checked above.
    context = replace(context, requirements=replace(context.requirements, context_tokens=estimate))
    context = bind_final_inference_payload(context, body)
    if context.allowed_provider_kinds != (NEAR_TEE_PROVIDER_KIND,) or context.fallback_allowed \
            or context.allowed_profile_ids != (profile.id,):
        raise _denied("near_tee_route_invalid", context)
    checked = time.time() if now is None else now
    inference_timeout = min(float(slot.timeout_seconds),
        context.deadline_at - checked - NEAR_TEE_BILLING_RESERVE_SECONDS)
    if inference_timeout < 1:
        raise _denied("near_tee_deadline_insufficient", context)
    try:
        key = await capture_near_tee_credential(expected_revision=configured.egress_revision,
            expected_fingerprint=configured.near_tee.credential_fingerprint)
    except PermissionError as exc:
        raise _denied(str(exc) or "near_tee_credential_unavailable", context) from None
    candidate = candidate_from_profile(profile)
    proofs = []
    for capability in sorted(set(context.requirements.capabilities) | {"health", "latency_ms"}):
        proof = await model_fabric_repository.latest_capability_proof(
            profile_schema_version=profile.schema_version, profile_contract_hash=profile.contract_hash,
            profile_id=profile.id, model=profile.model, endpoint=candidate.endpoint,
            endpoint_class=candidate.endpoint_class, adapter=candidate.adapter, capability=capability)
        if proof is not None:
            proofs.append(proof)
    mode = structured_mode(structured)
    structured_proof = next((proof for proof in proofs if proof.capability == "structured_output"), None)
    from .proofs import proof_is_fresh
    if structured_proof is not None and proof_is_fresh(structured_proof, now=now) \
            and structured_proof.proven_value != mode:
        # The proof exercised another request mode than the effective one.
        raise _denied("near_tee_vision_structured_proof_mode_mismatch", context)
    decision = select_route(context, (candidate,), tuple(proofs), now=now,
        target_model_id=slot.model_id, target_api_key=key)
    if not decision.allowed:
        reason = next((item.reason_code for item in decision.rejections), "no_compliant_route")
        denied = _denied(reason, context)
        denied.decision = decision
        raise denied
    headers = near_request_headers(key=key, no_aliasing=True, json_body=True)
    call = NearTeeCall(context=context, decision=decision, proof_hashes=decision.proof_hashes, body=body,
        key=key, slot_name="vision", request_cost_bound_microusd=slot.request_cost_bound_microusd,
        inference_timeout_seconds=inference_timeout, headers=headers)
    return call, structured


def _denied(code: str, context) -> NearTeeDenied:
    denied = NearTeeDenied(code)
    denied.denied_context = context
    return denied
