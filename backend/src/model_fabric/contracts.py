"""Immutable public contracts for governed inference route selection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import os
from urllib.parse import urlsplit, urlunsplit

from config.settings import settings
from src.security.trust_contract import (
    ContentOrigin,
    EgressClass,
    TrustPrincipal,
    TrustProvenance,
    canonical_digest,
)


MODEL_FABRIC_SCHEMA_VERSION = "seraph.model-fabric.v1"
SUPPORTED_TRANSPORT_ADAPTERS = frozenset(
    {"openai_compatible_chat", "vlm_analyze_file"}
)
SUPPORTED_SECRET_REFS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "LLM_API_KEY",
        "LOCAL_LLM_API_KEY",
        "SERAPH_VLM_API_KEY",
    }
)
PROVIDER_CREDENTIAL_REFS: dict[str, frozenset[str]] = {
    "local": frozenset({"LOCAL_LLM_API_KEY", "SERAPH_VLM_API_KEY"}),
    "ollama": frozenset({"LOCAL_LLM_API_KEY"}),
    "openai": frozenset({"OPENAI_API_KEY"}),
    "openrouter": frozenset({"OPENROUTER_API_KEY"}),
    "openai_compatible": frozenset(
        {"LLM_API_KEY", "LOCAL_LLM_API_KEY", "SERAPH_VLM_API_KEY"}
    ),
}


class NoCompliantModelRouteError(RuntimeError):
    code = "no_compliant_route"

    def __init__(self) -> None:
        super().__init__(self.code)


class EndpointClass(str, Enum):
    LOCAL = "local"
    TRUSTED_LAN = "trusted_lan"
    REMOTE = "remote"
    INVALID = "invalid"


class InferenceWorkload(str, Enum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    REPORT = "report"
    VISION = "vision"
    CAPABILITY_PROBE = "capability_probe"


class ModelCapability(str, Enum):
    TEXT = "text"
    VISION = "vision"
    TOOL_USE = "tool_use"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"


@dataclass(frozen=True)
class ProviderProfile:
    """The single provider-profile truth used by legacy and governed routing."""

    id: str
    provider_kind: str
    model: str
    routing_model: str = ""
    api_base: str = ""
    secret_env: str = ""
    options: dict[str, object] | None = None
    capabilities: tuple[str, ...] = ()
    cost_tier: str | None = None
    latency_tier: str | None = None
    task_class: str | None = None
    task_classes: tuple[str, ...] = ()
    budget_class: str | None = None
    fallback_models: tuple[str, ...] = ()
    enabled: bool = True
    keyless: bool = False
    safety_notes: str = ""
    transport_adapter: str = "openai_compatible_chat"
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    cost_microusd: int | None = None
    cost_source: str | None = None
    cost_source_updated_at: float | None = None
    local_resource_ms: int | None = None
    max_latency_ms: int | None = None
    follow_redirects: bool = False
    schema_version: str = MODEL_FABRIC_SCHEMA_VERSION

    @property
    def api_key(self) -> str:
        if self.secret_env and self.secret_env not in SUPPORTED_SECRET_REFS:
            raise ValueError("provider profile references an unsupported secret")
        configured = {
            "OPENAI_API_KEY": settings.openai_api_key,
            "OPENROUTER_API_KEY": settings.openrouter_api_key,
            "ANTHROPIC_API_KEY": settings.anthropic_api_key,
            "LLM_API_KEY": settings.llm_api_key,
            "LOCAL_LLM_API_KEY": settings.local_llm_api_key,
            "SERAPH_VLM_API_KEY": (
                settings.seraph_vlm_api_key or settings.local_vlm_api_key or settings.local_llm_api_key
            ),
        }
        return str(configured.get(self.secret_env) or os.getenv(self.secret_env, "")) if self.secret_env else ""

    @property
    def contract_hash(self) -> str:
        payload = {
            key: value
            for key, value in self.__dict__.items()
            if key not in {"secret_env", "safety_notes"}
        }
        payload["options"] = _sanitized_options(self.options or {})
        return _digest(payload)


@dataclass(frozen=True)
class InferenceRequirements:
    capabilities: tuple[str, ...]
    context_tokens: int
    output_tokens: int
    max_cost_microusd: int | None
    max_local_resource_ms: int | None
    max_latency_ms: int
    task_class: str


@dataclass(frozen=True)
class InferenceRequestContext:
    principal: TrustPrincipal
    session_id: str
    job_id: str
    provenance: tuple[TrustProvenance, ...]
    data_digest: str
    egress_class: EgressClass
    transformation_digest: str
    request_id: str
    runtime_path: str
    workload: InferenceWorkload
    requirements: InferenceRequirements
    deadline_at: float
    fallback_allowed: bool = False
    gpu_priority: str | None = None
    allowed_profile_ids: tuple[str, ...] = ()
    allowed_provider_kinds: tuple[str, ...] = ()
    requested_profile_id: str = ""
    redaction_applied: bool = False


@dataclass(frozen=True)
class ModelRouteCandidate:
    profile: ProviderProfile
    endpoint: str
    endpoint_class: EndpointClass
    adapter: str
    source: str


@dataclass(frozen=True)
class ModelRouteProof:
    profile_schema_version: str
    profile_contract_hash: str
    profile_id: str
    model: str
    endpoint: str
    endpoint_class: EndpointClass
    adapter: str
    capability: str
    canary_version: str
    outcome: str
    checked_at: float
    expires_at: float
    proof_hash: str
    probe_receipt_id: str
    probe_receipt_hash: str
    proven_value: int | str | None = None


@dataclass(frozen=True)
class RouteRejection:
    profile_id: str
    reason_code: str


@dataclass(frozen=True)
class RouteDecision:
    selected: ModelRouteCandidate | None
    trust_request_digest: str | None
    trust_decision_id: str | None
    rejections: tuple[RouteRejection, ...]
    attempt_id: str | None = None
    replay_id: str | None = None
    route_decision_id: str | None = None
    proof_hashes: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.selected is not None


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sanitized_options(value: object) -> object:
    if isinstance(value, dict):
        result = {}
        for key, nested in value.items():
            normalized = str(key).lower()
            if any(marker in normalized for marker in ("api_key", "authorization", "token", "secret", "password")):
                result[str(key)] = "[secret_ref_required]"
            else:
                result[str(key)] = _sanitized_options(nested)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitized_options(item) for item in value]
    return value


def has_inline_secret_options(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            any(marker in str(key).lower() for marker in ("api_key", "authorization", "token", "secret", "password"))
            or has_inline_secret_options(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(has_inline_secret_options(item) for item in value)
    return False


def default_egress_for_workload(_workload: InferenceWorkload) -> EgressClass:
    """Fail-closed default; only explicit operator policy may widen cloud egress."""
    return EgressClass.LOCAL_ONLY


def bind_final_inference_payload(
    context: InferenceRequestContext,
    payload: object,
) -> InferenceRequestContext:
    """Bind trust evaluation to the exact body handed to the transport adapter."""
    transport_digest = canonical_digest(payload)
    if transport_digest == context.data_digest:
        return context
    transformation_digest = canonical_digest(
        {
            "input_digest": context.data_digest,
            "transport_digest": transport_digest,
            "runtime_path": context.runtime_path,
        }
    )
    provenance = TrustProvenance(
        origin=ContentOrigin.SERAPH_CONTROL,
        source_id="model_fabric_transport_payload",
        data_digest=transport_digest,
        egress_class=context.egress_class,
        instruction_authority=True,
    )
    return replace(
        context,
        provenance=(*context.provenance, provenance),
        data_digest=transport_digest,
        transformation_digest=transformation_digest,
    )


def transport_model_for_provider(provider_kind: str, configured_model: str) -> str:
    """Return the exact provider payload model using provider-aware normalization."""
    model = str(configured_model or "").strip()
    prefix, separator, remainder = model.partition("/")
    allowed_prefixes = {
        "openrouter": {"openrouter"},
        "openai": {"openai"},
        "ollama": {"ollama"},
        "local": {"ollama", "openai"},
    }.get(str(provider_kind), set())
    return remainder if separator and prefix in allowed_prefixes and remainder else model


def credential_ref_allowed(profile: ProviderProfile) -> bool:
    if not profile.secret_env:
        return profile.keyless
    return profile.secret_env in PROVIDER_CREDENTIAL_REFS.get(profile.provider_kind, frozenset())


_OPENAI_COMPATIBLE_RESERVED_FIELDS = frozenset(
    {"model", "messages", "temperature", "max_tokens", "stream", "api_key", "api_base"}
)


def finalized_openai_compatible_body(
    *,
    model_id: str,
    messages: list[dict[str, object]],
    options: dict[str, object] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    stream: bool | None = None,
    additional_fields: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the one canonical body used for trust binding and raw transport."""
    body = {
        key: value
        for key, value in (options or {}).items()
        if key not in _OPENAI_COMPATIBLE_RESERVED_FIELDS
    }
    body.update(
        {
            "model": str(model_id or "").strip(),
            "messages": messages,
        }
    )
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if stream is not None:
        body["stream"] = stream
    for key, value in (additional_fields or {}).items():
        if key not in _OPENAI_COMPATIBLE_RESERVED_FIELDS:
            body[key] = value
    return body


def transport_endpoint(profile: ProviderProfile) -> str:
    """Return the exact adapter destination authorized for transport."""
    parsed = urlsplit(str(profile.api_base or "").strip())
    if profile.transport_adapter not in SUPPORTED_TRANSPORT_ADAPTERS:
        raise ValueError("unsupported model-fabric transport adapter")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("model api_base must be an absolute HTTP(S) URL")
    base_path = parsed.path.rstrip("/")
    expected = "/analyze-file" if profile.transport_adapter == "vlm_analyze_file" else "/chat/completions"
    if base_path.endswith(expected):
        path = base_path
    elif base_path.endswith("/v1"):
        path = f"{base_path}{expected}"
    else:
        path = f"{base_path}/v1{expected}"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))
