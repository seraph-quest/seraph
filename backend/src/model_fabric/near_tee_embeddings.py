"""NEAR TEE embedding-slot provider adapter (ADR-035, PR3d).

A typed adapter of the existing model fabric for ``memory_embedding``: the
caller's canonical context is preflighted here before admission, then one
OpenAI-compatible ``POST /v1/embeddings`` runs inside the one shared broker
lease with the durable accounting reservation. No fallback between slots,
models or providers; no new lane, queue, budget or ledger.

Request contract: the stored canonical id with ``x-no-aliasing: true``; the
response ``model`` must equal it; every vector must have the dimension the
capability check measured for this exact profile (a mismatch is rejected and
nothing is adopted). ``/v1/billing/costs`` readback runs inside the lease; no
authoritative cost makes the operation an Unknown liability and keeps the
serial lease blocked until fenced settlement.

Billing identity: the shared ADR-025 rule (``inference-id`` header, or the
uuid5 of the response body ``id``). An embeddings response without either
cannot be billed, so it becomes an Unknown liability, never an estimate.

ADR-035 evidence gate: the slot's block reason includes
``near_tee_embedding_evidence_required`` until source evidence shows the
embeddings endpoint and ``/v1/billing/costs`` honour ``x-no-aliasing``
(``configuration.NEAR_TEE_EMBEDDING_ALIASING_EVIDENCE``).

No import-time side effects; the write-only vault key is captured per
request with a fingerprint/revision fence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import time
from typing import Any

from .contracts import (
    NEAR_TEE_PROVIDER_KIND,
    InferenceRequestContext,
    RouteDecision,
    bind_final_inference_payload,
    finalized_openai_compatible_embeddings_body,
)
from .near_client import near_request_headers
from .near_tee import (
    NEAR_TEE_BILLING_RESERVE_SECONDS,
    NearTeeDenied,
    NearTeeError,
    precontact_reason,
)


EMBEDDING_SLOT = "embedding"
EMBEDDING_RUNTIME_PATH = "memory_embedding"
NEAR_TEE_EMBEDDING_RESPONSE_CAP = 8 * 1024 * 1024
NEAR_TEE_EMBEDDING_REQUEST_CAP = 4 * 1024 * 1024
MAX_EMBEDDING_DIMENSION = 65_536
# Conservative per-input overhead (special tokens) added to the byte bound.
_PER_INPUT_TOKEN_OVERHEAD = 4
NEAR_TEE_EMBEDDING_CHECK_CAPABILITIES = frozenset({"embedding", "health", "latency_ms"})
EMBEDDING_CANARY_TEXT = "Seraph fixed non-sensitive embedding canary."


def estimate_input_tokens(texts: list[str]) -> int:
    """Conservative upper bound: a token never covers less than one UTF-8 byte."""
    return sum(len(text.encode("utf-8")) + _PER_INPUT_TOKEN_OVERHEAD for text in texts)


@dataclass(frozen=True)
class NearTeeEmbeddingCall:
    context: InferenceRequestContext
    decision: RouteDecision
    proof_hashes: tuple[str, ...]
    body: dict[str, object] = field(repr=False)
    key: str = field(repr=False)
    dimension: int = 0
    expected_count: int = 0
    request_cost_bound_microusd: int = 0
    inference_timeout_seconds: float = 0.0
    headers: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class NearTeeEmbeddingOutcome:
    vectors: list[list[float]] | None = field(repr=False)
    failure_code: str | None
    provider_request_id: str | None
    cost_microusd: int | None
    dimension: int | None = None


def near_tee_embedding_vectors(payload: object, *, model: str, expected_count: int,
                               dimension: int | None) -> tuple[list[list[float]] | None, str | None]:
    """Return (unit vectors, failure). Nothing is adopted on any failure."""
    if not isinstance(payload, dict) or payload.get("model") != model:
        return None, "near_tee_model_mismatch"
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        return None, "near_tee_embedding_count_mismatch"
    indexed: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            return None, "near_tee_embedding_invalid"
        index, vector = item.get("index"), item.get("embedding")
        if type(index) is not int or not 0 <= index < expected_count or index in indexed:
            return None, "near_tee_embedding_invalid"
        if not isinstance(vector, list) or not 1 <= len(vector) <= MAX_EMBEDDING_DIMENSION:
            return None, "near_tee_embedding_invalid"
        if dimension is not None and len(vector) != dimension:
            return None, "near_tee_embedding_dimension_mismatch"
        values = []
        for value in vector:
            if type(value) not in (int, float) or not math.isfinite(value):
                return None, "near_tee_embedding_invalid"
            values.append(float(value))
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm <= 0.0:
            return None, "near_tee_embedding_invalid"
        indexed[index] = [value / norm for value in values]
    if dimension is None and len({len(vector) for vector in indexed.values()}) != 1:
        return None, "near_tee_embedding_dimension_mismatch"
    return [indexed[index] for index in range(expected_count)], None


def measured_dimension(proofs, *, now: float | None = None) -> int | None:
    """The fresh ``embedding`` capability proof's measured vector dimension."""
    from .proofs import proof_is_fresh
    for proof in proofs:
        if proof.capability == "embedding" and proof_is_fresh(proof, now=now):
            value = proof.proven_value
            if type(value) is int and 1 <= value <= MAX_EMBEDDING_DIMENSION:
                return value
    return None


async def load_slot_proofs(profile) -> tuple[object, ...]:
    from .repository import model_fabric_repository
    from .selector import candidate_from_profile
    candidate = candidate_from_profile(profile)
    proofs = []
    for capability in ("embedding", "health", "latency_ms"):
        proof = await model_fabric_repository.latest_capability_proof(
            profile_schema_version=profile.schema_version, profile_contract_hash=profile.contract_hash,
            profile_id=profile.id, model=profile.model, endpoint=candidate.endpoint,
            endpoint_class=candidate.endpoint_class, adapter=candidate.adapter, capability=capability)
        if proof is not None:
            proofs.append(proof)
    return tuple(proofs)


def catalog_dimension_conflict(profile, dimension: int) -> bool:
    """True when the current catalog receipt states a different dimension."""
    from .near_tee_catalog import read_catalog_receipts
    receipt = read_catalog_receipts().get(EMBEDDING_SLOT) or {}
    stated = receipt.get("catalog_embedding_dimension")
    return (receipt.get("profile_hash") == profile.contract_hash and type(stated) is int
        and stated != dimension)


async def prepare_near_tee_embedding_call(*, context: InferenceRequestContext, texts: list[str],
                                          now: float | None = None) -> NearTeeEmbeddingCall:
    """All precontact gates; raises ``NearTeeDenied`` before any admission."""
    from .configuration import (
        capture_near_tee_credential, near_tee_profile_for_slot, near_tee_slot, route_slot_for_task_class,
    )
    from .effective_policy import current_near_tee_policy
    from .near_tee_catalog import catalog_receipt_block_reason
    from .selector import candidate_from_profile, select_route

    if context.allowed_provider_kinds != (NEAR_TEE_PROVIDER_KIND,) or context.fallback_allowed:
        raise NearTeeDenied("near_tee_route_invalid")
    try:
        slot_name = route_slot_for_task_class(context.requirements.task_class)
    except ValueError as exc:
        raise NearTeeDenied(str(exc)) from None
    if slot_name != EMBEDDING_SLOT or context.runtime_path != EMBEDDING_RUNTIME_PATH:
        raise NearTeeDenied("near_tee_slot_adapter_mismatch")
    try:
        configured, _digest = current_near_tee_policy(EMBEDDING_SLOT)
    except PermissionError as exc:
        raise NearTeeDenied(str(exc) or "near_tee_policy_unavailable") from None
    slot = near_tee_slot(configured, EMBEDDING_SLOT)
    profile = near_tee_profile_for_slot(EMBEDDING_SLOT, slot)
    if context.allowed_profile_ids != (profile.id,):
        raise NearTeeDenied("profile_not_allowed")
    if not texts or not isinstance(texts, list) or any(not isinstance(text, str) or not text for text in texts):
        raise NearTeeDenied("near_tee_embedding_input_invalid")
    if len(texts) > int(slot.max_batch_size or 0):
        raise NearTeeDenied("near_tee_embedding_batch_too_large")
    if estimate_input_tokens(texts) > slot.max_input_tokens:
        # ADR-035: input over max_input_tokens is rejected before contact.
        raise NearTeeDenied("near_tee_input_too_large")
    if reason := catalog_receipt_block_reason(EMBEDDING_SLOT, profile, now=now, revision=configured.egress_revision):
        raise NearTeeDenied(reason)
    proofs = await load_slot_proofs(profile)
    dimension = measured_dimension(proofs, now=now)
    if dimension is None:
        raise NearTeeDenied("near_tee_embedding_dimension_unmeasured")
    if catalog_dimension_conflict(profile, dimension):
        raise NearTeeDenied("near_tee_embedding_dimension_mismatch")
    try:
        key = await capture_near_tee_credential(expected_revision=configured.egress_revision,
            expected_fingerprint=configured.near_tee.credential_fingerprint)
    except PermissionError as exc:
        raise NearTeeDenied(str(exc) or "near_tee_credential_unavailable") from None
    body = finalized_openai_compatible_embeddings_body(model_id=slot.model_id, inputs=list(texts))
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) > NEAR_TEE_EMBEDDING_REQUEST_CAP:
        raise NearTeeDenied("near_tee_request_too_large")
    bound = bind_final_inference_payload(context, body)
    checked = time.time() if now is None else now
    inference_timeout = min(float(slot.timeout_seconds),
        bound.deadline_at - checked - NEAR_TEE_BILLING_RESERVE_SECONDS)
    if inference_timeout < 1:
        raise NearTeeDenied("near_tee_deadline_insufficient")
    candidate = candidate_from_profile(profile)
    decision = select_route(bound, (candidate,), proofs, now=now, target_model_id=slot.model_id, target_api_key=key)
    if not decision.allowed:
        reason = next((item.reason_code for item in decision.rejections), "no_compliant_route")
        raise NearTeeDenied(reason, decision)
    return NearTeeEmbeddingCall(context=bound, decision=decision, proof_hashes=decision.proof_hashes, body=body,
        key=key, dimension=dimension, expected_count=len(texts),
        request_cost_bound_microusd=slot.request_cost_bound_microusd,
        inference_timeout_seconds=inference_timeout,
        headers=near_request_headers(key=key, no_aliasing=True, json_body=True))


def near_tee_embedding_precontact_reason(call: NearTeeEmbeddingCall, *, now: float | None = None) -> str | None:
    return precontact_reason(headers=call.headers, deadline_at=call.context.deadline_at,
        inference_timeout_seconds=call.inference_timeout_seconds, now=now)


async def _post_embeddings(client, *, body: dict[str, object], key: str, timeout: float):
    from .near_client import read_near_response, strict_json_object
    from .near_text_billing import NearTextBillingError, derive_near_inference_id
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    raw, headers = await read_near_response(client, "/embeddings", body=encoded, key=key, timeout=timeout,
        cap=NEAR_TEE_EMBEDDING_RESPONSE_CAP, no_aliasing=True, error=NearTeeError, prefix="near_tee")
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
    return payload, identity


async def near_tee_embedding_transport(call: NearTeeEmbeddingCall) -> NearTeeEmbeddingOutcome:
    """The admitted provider callback; runs only inside the broker lease."""
    from .accounting import capture_near_billing_evidence, current_near_accounting_operation_id
    from .near_client import near_http_client, read_near_billing

    operation_id = current_near_accounting_operation_id()
    model = str(call.body["model"])
    deadline = call.context.deadline_at
    async with near_http_client() as client:
        timeout = max(0.5, min(call.inference_timeout_seconds,
            deadline - time.time() - NEAR_TEE_BILLING_RESERVE_SECONDS))
        payload, identity = await _post_embeddings(client, body=call.body, key=call.key, timeout=timeout)
        vectors, failure = near_tee_embedding_vectors(payload, model=model, expected_count=call.expected_count,
            dimension=call.dimension)
        del payload
        evidence = await read_near_billing(client, key=call.key, identity=identity, operation_id=operation_id,
            deadline_at=deadline, no_aliasing=True, error=NearTeeError, prefix="near_tee",
            capture=capture_near_billing_evidence)
    if evidence is None:
        # Contact happened and no authoritative cost arrived: Unknown liability.
        raise NearTeeError("near_tee_cost_liability")
    if failure is None and evidence.cost_microusd > call.request_cost_bound_microusd:
        failure = "near_tee_cost_overrun"
    if failure is not None:
        vectors = None
    return NearTeeEmbeddingOutcome(vectors=vectors, failure_code=failure,
        provider_request_id=identity.provider_request_id, cost_microusd=evidence.cost_microusd,
        dimension=call.dimension)


async def near_tee_embedding_capability_check_transport(*, profile, capability: str, body: dict[str, object],
                                                        key: str, deadline_at: float):
    """Operator-triggered capability check for the embedding slot.

    Mechanics only: one fixed non-sensitive input, response shape, model echo
    and the measured vector dimension (the ``embedding`` proof value). Runs
    inside the probe's broker lease with billing readback; missing billing is
    an Unknown liability (raised).
    """
    from .accounting import capture_near_billing_evidence, current_near_accounting_operation_id
    from .near_client import near_http_client, read_near_billing
    from .probe import CapabilityProbeObservation

    if (capability not in NEAR_TEE_EMBEDDING_CHECK_CAPABILITIES or body.get("model") != profile.model
            or body.get("input") != EMBEDDING_CANARY_TEXT):
        raise NearTeeError("near_tee_capability_check_unsupported")
    operation_id = current_near_accounting_operation_id()
    async with near_http_client() as client:
        started = time.monotonic()
        payload, identity = await _post_embeddings(client, body=body, key=key,
            timeout=max(0.5, deadline_at - time.time() - NEAR_TEE_BILLING_RESERVE_SECONDS))
        elapsed_ms = max(int((time.monotonic() - started) * 1000), 0)
        vectors, failure = near_tee_embedding_vectors(payload, model=profile.model, expected_count=1, dimension=None)
        del payload
        evidence = await read_near_billing(client, key=key, identity=identity, operation_id=operation_id,
            deadline_at=deadline_at, no_aliasing=True, error=NearTeeError, prefix="near_tee",
            capture=capture_near_billing_evidence)
    if evidence is None:
        raise NearTeeError("near_tee_cost_liability")
    if failure is not None:
        return CapabilityProbeObservation(False, error_code=failure)
    dimension = len(vectors[0])
    if catalog_dimension_conflict(profile, dimension):
        return CapabilityProbeObservation(False, error_code="near_tee_embedding_dimension_mismatch")
    value: Any = {"latency_ms": elapsed_ms, "health": "healthy", "embedding": dimension}[capability]
    return CapabilityProbeObservation(True, proven_value=value)


def embed_texts_near_tee_sync(texts: list[str], *, principal, request_id: str) -> NearTeeEmbeddingOutcome:
    """Embed one bounded batch on the NEAR TEE embedding slot (worker thread).

    Precontact gates run before admission and become durable route denials;
    the transport runs inside the shared broker lease at the
    ``memory_embedding`` route's background priority (below chat).
    """
    from src.llm_runtime import (
        _execute_sync_with_gpu_admission, _finalize_route_receipt_sync, _new_route_receipt_session,
        _persist_denied_route_sync, _run_receipt_hook_sync,
    )
    from .accounting import bind_precontact_guard
    from .caller_context import build_canonical_inference_context
    from .configuration import near_tee_slot, near_tee_slot_block_reason, read_model_fabric_configuration
    from .execution import _run_awaitable_sync

    configured = read_model_fabric_configuration()
    slot = near_tee_slot(configured, EMBEDDING_SLOT)
    if slot is None:
        raise NearTeeDenied("near_tee_slot_unconfigured")
    if reason := near_tee_slot_block_reason(configured, EMBEDDING_SLOT):
        # The slot's own stable reason (e.g. the ADR-035 evidence gate),
        # before any context, admission or contact.
        raise NearTeeDenied(reason)
    context = build_canonical_inference_context(EMBEDDING_RUNTIME_PATH,
        payload={"model": slot.model_id, "input": list(texts)}, output_tokens=1,
        # The billing window is reserved out of the original deadline.
        timeout_seconds=float(slot.timeout_seconds) + NEAR_TEE_BILLING_RESERVE_SECONDS + 1,
        principal=principal, session_id=principal.session_id, job_id=principal.job_id, request_id=request_id)
    receipt_session = _new_route_receipt_session(context)
    try:
        call = _run_receipt_hook_sync(prepare_near_tee_embedding_call(context=context, texts=texts))
    except NearTeeDenied as denied:
        _persist_denied_route_sync(context, denied.decision, (denied.code,))
        raise
    started = False

    def operation():
        nonlocal started
        receipt_session.attempt_started(call.decision, capability_proof_hashes=call.proof_hashes)
        started = True
        return _run_awaitable_sync(near_tee_embedding_transport(call))

    try:
        with bind_precontact_guard(lambda: near_tee_embedding_precontact_reason(call)):
            outcome = _execute_sync_with_gpu_admission(context=call.context, decision=call.decision,
                operation_id=str(call.decision.attempt_id or f"{call.context.request_id}:near_tee_embedding"),
                operation=operation)
    except BaseException as error:
        if started:
            code = getattr(error, "code", None) if isinstance(error, NearTeeError) else "transport_failed"
            receipt_session.attempt_finished(outcome="failed", error_code=code or "transport_failed",
                decision=call.decision, degradation_code="transport_failed")
            _finalize_route_receipt_sync(receipt_session, outcome="failed", request_id=None,
                degradation_codes=("near_tee_transport_failed",))
        raise
    if outcome.failure_code is not None or outcome.vectors is None:
        code = outcome.failure_code or "near_tee_embedding_invalid"
        receipt_session.attempt_finished(outcome="failed", error_code=code, decision=call.decision,
            degradation_code="near_tee_output_not_adopted")
        _finalize_route_receipt_sync(receipt_session, outcome="failed", request_id=None, degradation_codes=(code,))
        # Contact happened and its cost was settled; no vector is adopted.
        error = NearTeeError(code)
        error.cost_microusd = outcome.cost_microusd
        raise error
    receipt_session.attempt_finished(outcome="succeeded", error_code=None, decision=call.decision)
    _finalize_route_receipt_sync(receipt_session, outcome="succeeded", request_id=None)
    return outcome
