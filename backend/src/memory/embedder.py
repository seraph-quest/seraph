"""OpenRouter-backed embeddings for canonical local memory.

The source records remain local and canonical. This module only sends the
text required to derive a vector after the model-fabric cloud-egress policy
has explicitly allowed the memory-embedding workload.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import threading
import time
from typing import Any
from uuid import uuid4

import httpx

from config.settings import settings
from src.approval.runtime import get_current_trust_principal
from src.audit.runtime import log_integration_event_sync
from src.model_fabric.configuration import effective_workload_policy
from src.model_fabric.remote_inference_admission import (
    RemoteInferenceAdmissionError,
)
from src.model_fabric import (
    ModelRouteCandidate,
    NoCompliantModelRouteError,
    ProviderProfile,
    active_provider_exclusion_reason,
    bind_final_inference_payload,
    candidate_from_profile,
    execute_sync_adapter,
    finalized_openai_compatible_embeddings_body,
)
from src.model_fabric.caller_context import build_canonical_inference_context
from src.model_fabric.execution import SyncAdapterReceiptError, _run_awaitable_sync
from src.model_fabric.repository import model_fabric_repository
from src.security.trust_contract import EgressClass, TrustPrincipal

EMBEDDING_SCHEMA_VERSION = "seraph.memory.embedding.v1"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_EMBEDDINGS_ENDPOINT = f"{OPENROUTER_API_BASE}/embeddings"
EMBEDDING_WORKLOAD_PATH = "memory_embedding"
EMBEDDING_ADMISSION_OWNER = "seraph-memory-embedding"

# These bounds are deliberately local to the adapter until the common remote
# admission policy owns them. They prevent an accidental backlog or an
# unbounded single request from becoming paid provider traffic.
MAX_BATCH_SIZE = 64
MAX_TEXT_CHARS = 16_000
REQUEST_TIMEOUT_SECONDS = 30.0
# A timeout, transport error, 429, or 5xx response can leave provider-side
# processing uncertain. Until durable usage reconciliation exists, replaying
# the same paid embedding request automatically could duplicate cost or write
# a vector whose outcome is unknown. Leave retries to an explicit operator or
# durable-job reconciliation path.
MAX_RETRIES = 0
RETRY_BACKOFF_SECONDS = 0.25


class EmbeddingError(RuntimeError):
    """Base class for typed, operator-visible embedding failures."""

    code = "embedding_unavailable"

    def __init__(
        self,
        reason_code: str,
        *,
        stage: str,
        request_id: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        retry_count: int = 0,
    ) -> None:
        self.reason_code = reason_code
        self.stage = stage
        self.request_id = request_id
        self.status_code = status_code
        self.retryable = retryable
        self.retry_count = retry_count
        # Keep the exception text safe: provider response bodies can contain
        # request data and must never be copied to logs or audit receipts.
        super().__init__(f"{self.code}:{reason_code}")


class EmbeddingUnavailableError(EmbeddingError):
    """Raised when OpenRouter embeddings are unavailable or rejected."""


class EmbeddingConfigurationError(EmbeddingUnavailableError):
    """Raised when the trusted OpenRouter route is not configured."""


class EmbeddingResponseError(EmbeddingUnavailableError):
    """Raised when the provider response cannot be used safely."""


@dataclass(frozen=True)
class EmbeddingMetadata:
    """Identity of the vector space currently admitted by this process."""

    schema_version: str
    provider: str
    model: str
    dimension: int

    @property
    def namespace(self) -> str:
        digest = hashlib.sha256(
            f"{self.schema_version}:{self.provider}:{self.model}:{self.dimension}".encode("utf-8")
        ).hexdigest()[:16]
        return f"memory-{digest}"


_metadata: EmbeddingMetadata | None = None
_metadata_lock = threading.Lock()
_LOAD_EVENT_EMITTED = False


def _embedder_name() -> str:
    """Return the configured route label used by runtime audit events."""
    return str(settings.embedding_model or "").strip() or "openrouter_embedding"


def _log_embedding_event(outcome: str, details: dict[str, Any] | None = None) -> None:
    log_integration_event_sync(
        integration_type="embedding_model",
        name=_embedder_name(),
        outcome=outcome,
        details=details,
    )


def _safe_details(
    *,
    stage: str,
    reason_code: str,
    batch_size: int,
    request_id: str | None = None,
    model: str | None = None,
    status_code: int | None = None,
    retry_count: int = 0,
) -> dict[str, Any]:
    """Build an audit payload without provider bodies or source text."""
    details: dict[str, Any] = {
        "stage": stage,
        "reason_code": reason_code,
        "batch_size": batch_size,
        "retry_count": retry_count,
        "schema_version": EMBEDDING_SCHEMA_VERSION,
    }
    if request_id:
        details["request_id"] = request_id
    if model:
        details["model"] = model
    if status_code is not None:
        details["status_code"] = status_code
    return details


def _raise_configuration(
    reason_code: str,
    *,
    batch_size: int,
    model: str | None = None,
) -> None:
    _log_embedding_event(
        "failed",
        details=_safe_details(
            stage="configuration",
            reason_code=reason_code,
            batch_size=batch_size,
            model=model,
        ),
    )
    raise EmbeddingConfigurationError(reason_code, stage="configuration")


def _configured_model(*, batch_size: int) -> str:
    """Resolve only an explicitly OpenRouter-qualified model identifier."""
    configured = str(settings.embedding_model or "").strip()
    if not configured:
        _raise_configuration("embedding_model_required", batch_size=batch_size)
    if not configured.startswith("openrouter/"):
        _raise_configuration(
            "embedding_model_must_be_openrouter_qualified",
            batch_size=batch_size,
        )
    model = configured.removeprefix("openrouter/").strip()
    if not model or "/" not in model or any(character.isspace() for character in model):
        _raise_configuration(
            "embedding_model_invalid",
            batch_size=batch_size,
            model=model or None,
        )
    return model


def _validate_route_policy(*, batch_size: int, model: str) -> None:
    """Require explicit full cloud egress for canonical memory text."""
    try:
        policy = effective_workload_policy(EMBEDDING_WORKLOAD_PATH)
    except Exception:
        _raise_configuration(
            "embedding_policy_unavailable",
            batch_size=batch_size,
            model=model,
        )

    egress_class = getattr(policy, "egress_class", EgressClass.LOCAL_ONLY)
    if getattr(egress_class, "value", egress_class) != EgressClass.CLOUD_ALLOWED_FULL.value:
        _raise_configuration(
            "cloud_egress_not_authorized_for_canonical_memory",
            batch_size=batch_size,
            model=model,
        )
    if not bool(getattr(policy, "cloud_egress_acknowledged", False)):
        _raise_configuration(
            "cloud_egress_acknowledgement_required",
            batch_size=batch_size,
            model=model,
        )
    if getattr(policy, "max_cost_microusd", None) is None:
        _raise_configuration(
            "embedding_cost_ceiling_required",
            batch_size=batch_size,
            model=model,
        )
    allowed_provider_kinds = tuple(getattr(policy, "allowed_provider_kinds", ()) or ())
    if set(allowed_provider_kinds) != {"openrouter"}:
        _raise_configuration(
            "openrouter_provider_policy_required",
            batch_size=batch_size,
            model=model,
        )
    if not bool(getattr(settings, "openrouter_provider_only", True)):
        _raise_configuration(
            "openrouter_provider_only_required",
            batch_size=batch_size,
            model=model,
        )
    upstreams = tuple(
        item.strip()
        for item in str(getattr(settings, "openrouter_allowed_upstreams", "") or "").split(",")
        if item.strip()
    )
    if not upstreams:
        _raise_configuration(
            "openrouter_upstream_allowlist_required",
            batch_size=batch_size,
            model=model,
        )
    if bool(getattr(settings, "openrouter_allow_fallbacks", False)):
        _raise_configuration(
            "openrouter_fallbacks_forbidden",
            batch_size=batch_size,
            model=model,
        )
    if bool(getattr(settings, "openrouter_require_parameters", True)) is not True:
        _raise_configuration(
            "openrouter_parameters_required",
            batch_size=batch_size,
            model=model,
        )
    if str(getattr(settings, "openrouter_data_collection", "deny") or "deny") != "deny":
        _raise_configuration(
            "openrouter_data_policy_must_deny",
            batch_size=batch_size,
            model=model,
        )


def _embedding_provider_options() -> dict[str, object]:
    """Return the exact provider policy that the embedding body will carry."""
    upstreams = [
        item.strip()
        for item in str(getattr(settings, "openrouter_allowed_upstreams", "") or "").split(",")
        if item.strip()
    ]
    return {
        "provider": {
            "only": upstreams,
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }
    }


def _embedding_profile(*, model: str, batch_size: int) -> ProviderProfile:
    """Resolve the exact builtin profile for the configured model."""
    from src.llm_runtime import provider_profiles

    profile = provider_profiles().get(EMBEDDING_WORKLOAD_PATH)
    if profile is None:
        _raise_configuration(
            "embedding_profile_required",
            batch_size=batch_size,
            model=model,
        )
    expected_options = _embedding_provider_options()
    if (
        profile.provider_kind != "openrouter"
        or profile.model != model
        or profile.api_base != OPENROUTER_API_BASE
        or profile.secret_env != "OPENROUTER_API_KEY"
        or profile.keyless
        or profile.transport_adapter != "openai_compatible_embeddings"
        or "embedding" not in profile.capabilities
        or profile.options != expected_options
        or active_provider_exclusion_reason(profile) is not None
    ):
        _raise_configuration(
            "embedding_profile_not_compliant",
            batch_size=batch_size,
            model=model,
        )
    return profile


def _load_embedding_proofs(
    *,
    profile: ProviderProfile,
    candidate: ModelRouteCandidate,
    context,
    batch_size: int,
) -> tuple[object, ...]:
    """Load only persisted proofs bound to the exact embedding profile."""
    capabilities = set(context.requirements.capabilities)
    capabilities.update({"latency_ms", "health"})

    async def _load() -> tuple[object, ...]:
        proofs = []
        for capability in sorted(capabilities):
            proof = await model_fabric_repository.latest_capability_proof(
                profile_schema_version=profile.schema_version,
                profile_contract_hash=profile.contract_hash,
                profile_id=profile.id,
                model=profile.model,
                endpoint=candidate.endpoint,
                endpoint_class=candidate.endpoint_class,
                adapter=candidate.adapter,
                capability=capability,
            )
            if proof is not None:
                proofs.append(proof)
        return tuple(proofs)

    try:
        return _run_awaitable_sync(_load())
    except Exception:
        _raise_configuration(
            "embedding_proofs_unavailable",
            batch_size=batch_size,
            model=profile.model,
        )
    return ()


def _validate_inputs(texts: list[str]) -> None:
    if not isinstance(texts, list):
        _log_embedding_event(
            "failed",
            details=_safe_details(
                stage="validation",
                reason_code="input_must_be_list",
                batch_size=0,
            ),
        )
        raise EmbeddingUnavailableError("input_must_be_list", stage="validation")
    if len(texts) > MAX_BATCH_SIZE:
        _log_embedding_event(
            "failed",
            details=_safe_details(
                stage="validation",
                reason_code="batch_size_exceeded",
                batch_size=len(texts),
            ),
        )
        raise EmbeddingUnavailableError(
            "batch_size_exceeded",
            stage="validation",
        )
    for text in texts:
        if not isinstance(text, str):
            _log_embedding_event(
                "failed",
                details=_safe_details(
                    stage="validation",
                    reason_code="input_must_be_text",
                    batch_size=len(texts),
                ),
            )
            raise EmbeddingUnavailableError(
                "input_must_be_text",
                stage="validation",
            )
        if len(text) > MAX_TEXT_CHARS:
            _log_embedding_event(
                "failed",
                details=_safe_details(
                    stage="validation",
                    reason_code="input_length_exceeded",
                    batch_size=len(texts),
                ),
            )
            raise EmbeddingUnavailableError(
                "input_length_exceeded",
                stage="validation",
            )


def _response_reason_code(status_code: int) -> tuple[str, bool]:
    if status_code in {401, 403}:
        return "credentials_rejected", False
    if status_code == 402:
        return "credits_unavailable", False
    if status_code == 404:
        return "embedding_model_unavailable", False
    if status_code == 429:
        return "rate_limited", True
    if 500 <= status_code <= 599:
        return "upstream_unavailable", True
    if 400 <= status_code <= 499:
        return "request_rejected", False
    return "provider_http_error", False


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        value = float(getattr(response, "headers", {}).get("retry-after", ""))
    except (TypeError, ValueError):
        return RETRY_BACKOFF_SECONDS
    return min(max(value, 0.0), 1.0)


def _request_embeddings(
    *,
    model: str,
    texts: list[str],
    request_id: str,
) -> object:
    """Dispatch one bounded, idempotent embedding request to OpenRouter."""
    payload = finalized_openai_compatible_embeddings_body(
        model_id=model,
        inputs=texts[0] if len(texts) == 1 else texts,
        options=_embedding_provider_options(),
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "X-Seraph-Request-ID": request_id,
    }

    last_reason = "provider_transport_failed"
    last_status: int | None = None
    with httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
    ) as client:
        for retry_count in range(MAX_RETRIES + 1):
            try:
                response = client.post(
                    OPENROUTER_EMBEDDINGS_ENDPOINT,
                    headers=headers,
                    json=payload,
                )
            except httpx.TimeoutException:
                last_reason = "provider_timeout"
                if retry_count < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                details = _safe_details(
                    stage="request",
                    reason_code=last_reason,
                    batch_size=len(texts),
                    request_id=request_id,
                    model=model,
                    retry_count=retry_count,
                )
                _log_embedding_event("failed", details=details)
                raise EmbeddingUnavailableError(
                    last_reason,
                    stage="request",
                    request_id=request_id,
                    retryable=True,
                    retry_count=retry_count,
                )
            except httpx.TransportError:
                last_reason = "provider_transport_failed"
                if retry_count < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                details = _safe_details(
                    stage="request",
                    reason_code=last_reason,
                    batch_size=len(texts),
                    request_id=request_id,
                    model=model,
                    retry_count=retry_count,
                )
                _log_embedding_event("failed", details=details)
                raise EmbeddingUnavailableError(
                    last_reason,
                    stage="request",
                    request_id=request_id,
                    retryable=True,
                    retry_count=retry_count,
                )
            except Exception:
                last_reason = "provider_transport_failed"
                if retry_count < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                details = _safe_details(
                    stage="request",
                    reason_code=last_reason,
                    batch_size=len(texts),
                    request_id=request_id,
                    model=model,
                    retry_count=retry_count,
                )
                _log_embedding_event("failed", details=details)
                raise EmbeddingUnavailableError(
                    last_reason,
                    stage="request",
                    request_id=request_id,
                    retryable=True,
                    retry_count=retry_count,
                )

            last_status = response.status_code
            if 200 <= response.status_code < 300:
                return response.json()

            last_reason, retryable = _response_reason_code(response.status_code)
            if retryable and retry_count < MAX_RETRIES:
                time.sleep(_retry_after_seconds(response))
                continue

            details = _safe_details(
                stage="request",
                reason_code=last_reason,
                batch_size=len(texts),
                request_id=request_id,
                model=model,
                status_code=response.status_code,
                retry_count=retry_count,
            )
            _log_embedding_event("failed", details=details)
            raise EmbeddingUnavailableError(
                last_reason,
                stage="request",
                request_id=request_id,
                status_code=response.status_code,
                retryable=retryable,
                retry_count=retry_count,
            )

    # The loop always returns or raises. Keep a typed guard for unusual test
    # doubles or future transport changes.
    raise EmbeddingUnavailableError(
        last_reason,
        stage="request",
        request_id=request_id,
        status_code=last_status,
        retryable=True,
        retry_count=MAX_RETRIES,
    )


def _response_failure(reason_code: str, *, expected_count: int, model: str, request_id: str) -> None:
    _log_embedding_event(
        "failed",
        details=_safe_details(
            stage="response",
            reason_code=reason_code,
            batch_size=expected_count,
            request_id=request_id,
            model=model,
        ),
    )
    raise EmbeddingResponseError(reason_code, stage="response", request_id=request_id)


def _parse_vectors(
    payload: object,
    *,
    expected_count: int,
    model: str,
    request_id: str,
) -> list[list[float]]:
    if not isinstance(payload, dict):
        _response_failure("response_not_object", expected_count=expected_count, model=model, request_id=request_id)

    response_model = payload.get("model")
    if response_model is not None and response_model != model:
        _response_failure("response_model_mismatch", expected_count=expected_count, model=model, request_id=request_id)

    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        _response_failure(
            "response_item_count_mismatch",
            expected_count=expected_count,
            model=model,
            request_id=request_id,
        )

    indexed: dict[int, list[float]] = {}
    dimension: int | None = None
    for item in data:
        if not isinstance(item, dict):
            _response_failure("response_item_invalid", expected_count=expected_count, model=model, request_id=request_id)
        index = item.get("index")
        vector = item.get("embedding")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= expected_count:
            _response_failure("response_index_invalid", expected_count=expected_count, model=model, request_id=request_id)
        if index in indexed or not isinstance(vector, list) or not vector:
            _response_failure("response_vector_invalid", expected_count=expected_count, model=model, request_id=request_id)

        numeric_vector: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                _response_failure(
                    "response_vector_value_invalid",
                    expected_count=expected_count,
                    model=model,
                    request_id=request_id,
                )
            numeric_vector.append(float(value))
        if dimension is None:
            dimension = len(numeric_vector)
        elif len(numeric_vector) != dimension:
            _response_failure(
                "response_dimension_mismatch",
                expected_count=expected_count,
                model=model,
                request_id=request_id,
            )
        norm = math.sqrt(sum(value * value for value in numeric_vector))
        if not math.isfinite(norm) or norm <= 0.0:
            _response_failure("response_zero_vector", expected_count=expected_count, model=model, request_id=request_id)
        indexed[index] = [value / norm for value in numeric_vector]

    if set(indexed) != set(range(expected_count)):
        _response_failure(
            "response_indices_incomplete",
            expected_count=expected_count,
            model=model,
            request_id=request_id,
        )
    return [indexed[index] for index in range(expected_count)]


def _remember_metadata(*, model: str, dimension: int, batch_size: int, request_id: str) -> None:
    global _metadata, _LOAD_EVENT_EMITTED
    candidate = EmbeddingMetadata(
        schema_version=EMBEDDING_SCHEMA_VERSION,
        provider="openrouter",
        model=model,
        dimension=dimension,
    )
    with _metadata_lock:
        if _metadata is not None and _metadata != candidate:
            _log_embedding_event(
                "failed",
                details=_safe_details(
                    stage="metadata",
                    reason_code="embedding_space_changed",
                    batch_size=batch_size,
                    request_id=request_id,
                    model=model,
                ),
            )
            raise EmbeddingResponseError(
                "embedding_space_changed",
                stage="metadata",
                request_id=request_id,
            )
        _metadata = candidate
        if not _LOAD_EVENT_EMITTED:
            _log_embedding_event(
                "loaded",
                details={
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "dimension": candidate.dimension,
                    "schema_version": candidate.schema_version,
                    "namespace": candidate.namespace,
                },
            )
            _LOAD_EVENT_EMITTED = True


def _embed_texts(
    texts: list[str],
    *,
    principal: TrustPrincipal | None = None,
) -> list[list[float]]:
    _validate_inputs(texts)
    if not texts:
        return []
    model = _configured_model(batch_size=len(texts))
    _validate_route_policy(batch_size=len(texts), model=model)
    effective_principal = principal or get_current_trust_principal()
    if effective_principal is None:
        _raise_configuration(
            "authenticated_principal_required",
            batch_size=len(texts),
            model=model,
        )
    if not settings.openrouter_api_key:
        _raise_configuration(
            "openrouter_api_key_required",
            batch_size=len(texts),
            model=model,
        )
    configured_base = str(settings.llm_api_base or "").strip().rstrip("/")
    if configured_base != OPENROUTER_API_BASE:
        _raise_configuration(
            "openrouter_api_base_required",
            batch_size=len(texts),
            model=model,
        )

    profile = _embedding_profile(model=model, batch_size=len(texts))
    request_id = f"embedding:{uuid4().hex}"
    try:
        input_value = texts[0] if len(texts) == 1 else texts
        context = build_canonical_inference_context(
            EMBEDDING_WORKLOAD_PATH,
            payload={"model": model, "input": input_value},
            output_tokens=1,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            principal=effective_principal,
            session_id=effective_principal.session_id,
            job_id=effective_principal.job_id,
            request_id=request_id,
        )
        transport_body = finalized_openai_compatible_embeddings_body(
            model_id=model,
            inputs=input_value,
            options=profile.options,
        )
        context = bind_final_inference_payload(context, transport_body)
        candidate = candidate_from_profile(profile)
        proofs = _load_embedding_proofs(
            profile=profile,
            candidate=candidate,
            context=context,
            batch_size=len(texts),
        )
        vectors = execute_sync_adapter(
            context=context,
            candidates=(candidate,),
            proofs=proofs,
            repository=model_fabric_repository,
            adapter=lambda selected, _follow_redirects: _parse_vectors(
                _request_embeddings(
                    model=selected.profile.model,
                    texts=texts,
                    request_id=request_id,
                ),
                expected_count=len(texts),
                model=selected.profile.model,
                request_id=request_id,
            ),
        )
    except EmbeddingError:
        raise
    except RemoteInferenceAdmissionError as exc:
        reason_code = str(getattr(exc, "code", "remote_inference_admission_failed"))
        _log_embedding_event(
            "failed",
            details=_safe_details(
                stage="admission",
                reason_code=reason_code,
                batch_size=len(texts),
                request_id=request_id,
                model=model,
            ),
        )
        raise EmbeddingUnavailableError(
            reason_code,
            stage="admission",
            request_id=request_id,
            retryable=False,
        ) from exc
    except NoCompliantModelRouteError as exc:
        reason_code = "embedding_route_unavailable"
        _log_embedding_event(
            "failed",
            details=_safe_details(
                stage="route",
                reason_code=reason_code,
                batch_size=len(texts),
                request_id=request_id,
                model=model,
            ),
        )
        raise EmbeddingUnavailableError(
            reason_code,
            stage="route",
            request_id=request_id,
        ) from exc
    except SyncAdapterReceiptError as exc:
        _log_embedding_event(
            "failed",
            details=_safe_details(
                stage="receipt",
                reason_code=SyncAdapterReceiptError.code,
                batch_size=len(texts),
                request_id=request_id,
                model=model,
            ),
        )
        raise EmbeddingUnavailableError(
            SyncAdapterReceiptError.code,
            stage="receipt",
            request_id=request_id,
        ) from exc
    except PermissionError as exc:
        reason_code = "model_inference_authority_required"
        _log_embedding_event(
            "failed",
            details=_safe_details(
                stage="configuration",
                reason_code=reason_code,
                batch_size=len(texts),
                request_id=request_id,
                model=model,
            ),
        )
        raise EmbeddingConfigurationError(
            reason_code,
            stage="configuration",
            request_id=request_id,
        ) from exc
    except (ValueError, TypeError, KeyError):
        reason_code = "response_json_invalid"
        _log_embedding_event(
            "failed",
            details=_safe_details(
                stage="response",
                reason_code=reason_code,
                batch_size=len(texts),
                request_id=request_id,
                model=model,
            ),
        )
        raise EmbeddingResponseError(reason_code, stage="response", request_id=request_id)
    except Exception:
        reason_code = "response_unreadable"
        _log_embedding_event(
            "failed",
            details=_safe_details(
                stage="response",
                reason_code=reason_code,
                batch_size=len(texts),
                request_id=request_id,
                model=model,
            ),
        )
        raise EmbeddingResponseError(reason_code, stage="response", request_id=request_id)

    _remember_metadata(
        model=model,
        dimension=len(vectors[0]),
        batch_size=len(texts),
        request_id=request_id,
    )
    return vectors


def embed(text: str, *, principal: TrustPrincipal | None = None) -> list[float]:
    """Embed one text through the explicitly authorized OpenRouter route."""
    return _embed_texts([text], principal=principal)[0]


def embed_batch(
    texts: list[str],
    *,
    principal: TrustPrincipal | None = None,
) -> list[list[float]]:
    """Embed a bounded batch through one OpenRouter embeddings request."""
    return _embed_texts(texts, principal=principal)


def embedding_metadata() -> EmbeddingMetadata | None:
    """Return the process-local vector-space identity, if one is loaded."""
    with _metadata_lock:
        return _metadata


def _reset_embedder_state() -> None:
    """Reset cached embedding metadata for tests and deterministic evals."""
    global _metadata, _LOAD_EVENT_EMITTED
    with _metadata_lock:
        _metadata = None
        _LOAD_EVENT_EMITTED = False
