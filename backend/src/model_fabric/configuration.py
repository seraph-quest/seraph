"""Workspace-persisted model-fabric profiles and workload policies."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from config.settings import settings
from src.security.trust_contract import EgressClass

from .contracts import (
    MODEL_FABRIC_SCHEMA_VERSION,
    OPENROUTER_API_BASE,
    OPENROUTER_PROVIDER_KIND,
    ModelCapability,
    EndpointClass,
    ProviderProfile,
    credential_ref_allowed,
    has_inline_secret_options,
    transport_endpoint,
    transport_model_for_provider,
)
from .receipts import safe_code
from .selector import active_provider_exclusion_reason, classify_endpoint, profile_exclusion_reason


CONFIG_SCHEMA_VERSION = "seraph.model-fabric.settings.v1"
OPENROUTER_SETUP_SCHEMA_VERSION = "seraph.openrouter.setup.v1"
OPENROUTER_VAULT_CREDENTIAL_REF = "vault:openrouter_api_key"
OPENROUTER_ENV_CREDENTIAL_REF = "env:OPENROUTER_API_KEY"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_OPENROUTER_UPSTREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_OPENROUTER_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_MAX_OPENROUTER_MODELS = 8
_MAX_OPENROUTER_OUTPUT_TOKENS = 131_072
_MAX_OPENROUTER_TIMEOUT_SECONDS = 120.0
_MAX_OPENROUTER_QUEUE = 64
_MAX_OPENROUTER_OWNER_OUTSTANDING = 16
_MAX_OPENROUTER_RETRIES = 2
OPENROUTER_RUNTIME_CONTROLS_KEY = "_seraph_openrouter"
_OPENROUTER_CAPABILITIES = frozenset(item.value for item in ModelCapability)
SUPPORTED_TRANSPORT_ADAPTERS = frozenset(
    {
        "openai_compatible_chat",
        "openai_compatible_embeddings",
        "vlm_analyze_file",
    }
)


@dataclass(frozen=True)
class WorkloadPolicy:
    runtime_path: str
    egress_class: EgressClass = EgressClass.LOCAL_ONLY
    cloud_egress_acknowledged: bool = False
    allowed_profile_ids: tuple[str, ...] = ()
    allowed_provider_kinds: tuple[str, ...] = ()
    fallback_allowed: bool = False
    max_cost_microusd: int | None = None


@dataclass(frozen=True)
class OpenRouterSetup:
    """Operator-supplied OpenRouter policy and bounded request settings.

    The credential itself is deliberately absent. ``credential_ref`` points to
    the trusted backend source and ``credential_fingerprint`` is only a short
    comparison value for operator status.
    """

    profile_id: str = "openrouter"
    model_ids: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    temperature: float = 0.7
    max_output_tokens: int = 4096
    timeout_seconds: float = _MAX_OPENROUTER_TIMEOUT_SECONDS
    allowed_upstreams: tuple[str, ...] = ()
    allow_fallbacks: bool = False
    require_parameters: bool = True
    data_collection: str = "deny"
    data_retention_policy: str = "deny"
    zero_data_retention: bool = False
    egress_class: EgressClass = EgressClass.LOCAL_ONLY
    cloud_egress_acknowledged: bool = False
    spend_ceiling_microusd: int | None = None
    max_queued: int = _MAX_OPENROUTER_QUEUE
    max_inflight: int = 1
    max_outstanding_per_owner: int = _MAX_OPENROUTER_OWNER_OUTSTANDING
    max_retries: int = _MAX_OPENROUTER_RETRIES
    credential_ref: str = OPENROUTER_VAULT_CREDENTIAL_REF
    credential_fingerprint: str | None = None
    schema_version: str = OPENROUTER_SETUP_SCHEMA_VERSION


@dataclass(frozen=True)
class ModelFabricConfiguration:
    profiles: tuple[ProviderProfile, ...] = ()
    workload_policies: tuple[WorkloadPolicy, ...] = ()
    status: str = "missing"
    error_code: str | None = None
    updated_at: str | None = None
    openrouter_setup: OpenRouterSetup | None = None


def model_fabric_configuration_path() -> Path:
    return Path(settings.workspace_dir).expanduser().resolve() / "model-fabric-settings.json"


def read_model_fabric_configuration() -> ModelFabricConfiguration:
    path = model_fabric_configuration_path()
    if not path.exists():
        return ModelFabricConfiguration()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _configuration_from_payload(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return ModelFabricConfiguration(status="degraded", error_code="configuration_unreadable")


async def hydrate_openrouter_credential() -> bool:
    """Load the configured vault credential before canonical route resolution.

    The vault is consulted only when the trusted process settings and env
    fallback are empty.  The secret stays in the settings object and is never
    returned, logged, or sent by this helper; the transport remains responsible
    for the eventual provider call.
    """
    configured = str(settings.openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "") or "").strip()
    if configured:
        if not settings.openrouter_api_key:
            settings.openrouter_api_key = configured
        return True
    persisted = read_model_fabric_configuration()
    setup = persisted.openrouter_setup if persisted.status == "ready" else None
    if setup is None or setup.credential_ref != OPENROUTER_VAULT_CREDENTIAL_REF:
        return False
    from src.vault.repository import vault_repository

    credential = str(await vault_repository.get("openrouter_api_key") or "").strip()
    if not credential:
        return False
    settings.openrouter_api_key = credential
    return True


def write_model_fabric_configuration(configuration: ModelFabricConfiguration) -> None:
    """Persist a structurally valid configuration, including legacy profiles.

    Active route eligibility is checked by the selector and by
    ``validate_active_model_fabric_configuration``.  Keeping this write path
    provider-agnostic lets an operator read and retain historical settings
    while the active selector fails closed on them.
    """
    validated = _configuration_from_payload(_configuration_payload(configuration))
    path = model_fabric_configuration_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(_configuration_payload(validated), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def validate_active_model_fabric_configuration(
    configuration: ModelFabricConfiguration,
) -> None:
    """Validate that a newly supplied configuration cannot activate legacy routes."""
    validated = _configuration_from_payload(_configuration_payload(configuration))
    for profile in validated.profiles:
        if reason := active_provider_exclusion_reason(profile):
            raise ValueError(f"model-fabric profile is ineligible: {reason}")
        if reason := profile_exclusion_reason(profile):
            raise ValueError(f"model-fabric profile is ineligible: {reason}")
    for policy in validated.workload_policies:
        if policy.fallback_allowed:
            raise ValueError("active model-fabric policies must disable fallback")
        if policy.allowed_provider_kinds and set(policy.allowed_provider_kinds) != {"openrouter"}:
            raise ValueError("active model-fabric policies may allow only openrouter")
    if validated.openrouter_setup is not None:
        validate_openrouter_setup(validated.openrouter_setup)


def effective_provider_profiles(legacy_profiles: dict[str, ProviderProfile]) -> dict[str, ProviderProfile]:
    """Apply persisted canonical profiles over legacy compatibility inputs."""
    configured = read_model_fabric_configuration()
    profiles = dict(legacy_profiles)
    if configured.status == "ready":
        profiles.update({profile.id: profile for profile in configured.profiles})
        setup = configured.openrouter_setup
        if setup is not None:
            # The admission broker is process-local by design, but its limits
            # are still canonical policy. Apply them on every profile
            # resolution so a restart or a settings update cannot leave the
            # active lane using stale defaults.
            from .remote_inference_admission import configure_remote_inference_admission

            configure_remote_inference_admission(
                max_queued=setup.max_queued,
                max_inflight=setup.max_inflight,
                max_outstanding_per_owner=setup.max_outstanding_per_owner,
                max_owner_cost_microusd=setup.spend_ceiling_microusd,
                max_retries=setup.max_retries,
            )
    else:
        from .remote_inference_admission import configure_remote_inference_admission

        configure_remote_inference_admission()
    return profiles


def effective_workload_policy(runtime_path: str) -> WorkloadPolicy:
    configured = read_model_fabric_configuration()
    if configured.status == "ready":
        for policy in configured.workload_policies:
            if policy.runtime_path == runtime_path:
                return policy
    return WorkloadPolicy(runtime_path=runtime_path)


def _configuration_from_payload(payload: object) -> ModelFabricConfiguration:
    if not isinstance(payload, dict) or payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError("unsupported model-fabric configuration schema")
    raw_profiles = payload.get("profiles", [])
    raw_policies = payload.get("workload_policies", [])
    if not isinstance(raw_profiles, list) or not isinstance(raw_policies, list):
        raise ValueError("model-fabric configuration collections must be lists")
    profiles = tuple(_profile_from_payload(item) for item in raw_profiles)
    policies = tuple(_policy_from_payload(item) for item in raw_policies)
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("model-fabric profile ids must be unique")
    if len({policy.runtime_path for policy in policies}) != len(policies):
        raise ValueError("model-fabric workload policies must be unique")
    raw_setup = payload.get("openrouter_setup")
    setup = _openrouter_setup_from_payload(raw_setup) if raw_setup is not None else None
    return ModelFabricConfiguration(
        profiles=profiles,
        workload_policies=policies,
        status="ready",
        updated_at=str(payload.get("updated_at") or "") or None,
        openrouter_setup=setup,
    )


def _profile_from_payload(payload: object) -> ProviderProfile:
    if not isinstance(payload, dict):
        raise ValueError("model-fabric profile must be an object")
    allowed = set(ProviderProfile.__dataclass_fields__)
    if set(payload) - allowed:
        raise ValueError("model-fabric profile contains unsupported fields")
    values = dict(payload)
    for field_name in ("capabilities", "task_classes", "fallback_models"):
        values[field_name] = tuple(values.get(field_name) or ())
    profile = ProviderProfile(**values)
    routing_model = profile.routing_model or profile.model
    exact_model = transport_model_for_provider(profile.provider_kind, routing_model)
    if profile.routing_model and profile.model != exact_model:
        raise ValueError("model-fabric profile model does not match its exact transport model")
    if not profile.routing_model:
        profile = replace(profile, model=exact_model, routing_model=routing_model)
    if profile.schema_version != MODEL_FABRIC_SCHEMA_VERSION or _SAFE_ID.fullmatch(profile.id) is None:
        raise ValueError("invalid model-fabric profile identity")
    if profile.follow_redirects:
        raise ValueError("model-fabric redirects are forbidden")
    if profile.transport_adapter not in SUPPORTED_TRANSPORT_ADAPTERS:
        raise ValueError("model-fabric transport adapter is unsupported")
    parsed_endpoint = urlsplit(str(profile.api_base or "").strip())
    if (
        parsed_endpoint.scheme not in {"http", "https"}
        or not parsed_endpoint.hostname
        or parsed_endpoint.username
        or parsed_endpoint.password
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise ValueError("model-fabric endpoint must be an absolute credential-free HTTP(S) URL")
    endpoint_class = classify_endpoint(transport_endpoint(profile))
    if endpoint_class is EndpointClass.INVALID:
        raise ValueError("model-fabric endpoint class is invalid")
    if endpoint_class is EndpointClass.REMOTE and parsed_endpoint.scheme != "https":
        raise ValueError("remote model-fabric endpoints require HTTPS")
    if profile.transport_adapter == "vlm_analyze_file" and "vision" not in profile.capabilities:
        raise ValueError("VLM transport requires the vision capability")
    if profile.transport_adapter == "openai_compatible_embeddings" and "embedding" not in profile.capabilities:
        raise ValueError("embedding transport requires the embedding capability")
    if "embedding" in profile.capabilities and profile.transport_adapter != "openai_compatible_embeddings":
        raise ValueError("embedding capability requires the embeddings transport")
    if not credential_ref_allowed(profile):
        raise ValueError("model-fabric credential reference is not allowed for this provider")
    if has_inline_secret_options(profile.options or {}):
        raise ValueError("model-fabric options must use credential references")
    pricing_fields = (
        profile.cost_microusd is not None,
        bool(profile.cost_source),
        profile.cost_source_updated_at is not None,
    )
    if any(pricing_fields) and not all(pricing_fields):
        raise ValueError("model-fabric pricing requires cost, source, and source timestamp")
    if profile.cost_source is not None:
        safe_code(profile.cost_source, field_name="cost source")
    if profile.cost_source_updated_at is not None and (
        not math.isfinite(profile.cost_source_updated_at) or profile.cost_source_updated_at < 0
    ):
        raise ValueError("model-fabric pricing timestamp must be finite and nonnegative")
    return profile


def _policy_from_payload(payload: object) -> WorkloadPolicy:
    if not isinstance(payload, dict):
        raise ValueError("workload policy must be an object")
    allowed = set(WorkloadPolicy.__dataclass_fields__)
    if set(payload) - allowed:
        raise ValueError("workload policy contains unsupported fields")
    values = dict(payload)
    values["egress_class"] = EgressClass(values.get("egress_class", EgressClass.LOCAL_ONLY))
    values["allowed_profile_ids"] = tuple(values.get("allowed_profile_ids") or ())
    values["allowed_provider_kinds"] = tuple(values.get("allowed_provider_kinds") or ())
    policy = WorkloadPolicy(**values)
    if _SAFE_ID.fullmatch(policy.runtime_path) is None:
        raise ValueError("invalid workload policy runtime path")
    if policy.egress_class is not EgressClass.LOCAL_ONLY and not policy.cloud_egress_acknowledged:
        raise ValueError("cloud egress requires explicit operator acknowledgement")
    if policy.max_cost_microusd is not None and policy.max_cost_microusd < 0:
        raise ValueError("workload policy cost ceiling must be nonnegative")
    if policy.egress_class is not EgressClass.LOCAL_ONLY and policy.max_cost_microusd is None:
        raise ValueError("cloud egress requires an explicit cost ceiling")
    return policy


def _configuration_payload(configuration: ModelFabricConfiguration) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "updated_at": configuration.updated_at or datetime.now(timezone.utc).isoformat(),
        "profiles": [_profile_payload(profile) for profile in configuration.profiles],
        "workload_policies": [
            {
                **asdict(policy),
                "egress_class": policy.egress_class.value,
                "allowed_profile_ids": list(policy.allowed_profile_ids),
                "allowed_provider_kinds": list(policy.allowed_provider_kinds),
            }
            for policy in configuration.workload_policies
        ],
    }
    if configuration.openrouter_setup is not None:
        payload["openrouter_setup"] = _openrouter_setup_payload(configuration.openrouter_setup)
    return payload


def _profile_payload(profile: ProviderProfile) -> dict[str, object]:
    payload = asdict(profile)
    payload["capabilities"] = list(profile.capabilities)
    payload["task_classes"] = list(profile.task_classes)
    payload["fallback_models"] = list(profile.fallback_models)
    return payload


def normalize_openrouter_model_id(value: object) -> str:
    """Normalize an operator model id to ``openrouter/provider/model``."""
    candidate = str(value or "").strip()
    if candidate.startswith("openrouter/"):
        candidate = candidate.removeprefix("openrouter/")
    if (
        not candidate
        or "/" not in candidate
        or len(candidate) > 256
        or candidate.count("/") != 1
        or _SAFE_OPENROUTER_MODEL.fullmatch(candidate) is None
        or any(character.isspace() or ord(character) < 32 for character in candidate)
    ):
        raise ValueError("OpenRouter model ids must be a bounded provider/model value")
    provider, _, model = candidate.partition("/")
    if not provider or not model or provider.lower() in {"local", "ollama", "openai-compatible", "openai_compatible"}:
        raise ValueError("OpenRouter model ids must name an OpenRouter provider and model")
    return f"openrouter/{candidate}"


def validate_openrouter_setup(setup: OpenRouterSetup) -> None:
    """Validate the complete user-facing OpenRouter setup contract."""
    if setup.schema_version != OPENROUTER_SETUP_SCHEMA_VERSION:
        raise ValueError("unsupported OpenRouter setup schema")
    if setup.profile_id != "openrouter":
        raise ValueError("OpenRouter setup must use the canonical openrouter profile")
    if not setup.model_ids:
        raise ValueError("one OpenRouter model id is required")
    if len(setup.model_ids) > 1:
        raise ValueError("multiple OpenRouter model ids are unsupported until governed model selection exists")
    normalized_models = tuple(normalize_openrouter_model_id(item) for item in setup.model_ids)
    if len(set(normalized_models)) != len(normalized_models):
        raise ValueError("OpenRouter model ids must be unique")
    if not setup.capabilities:
        raise ValueError("at least one model capability is required")
    if any(item not in _OPENROUTER_CAPABILITIES for item in setup.capabilities):
        raise ValueError("unsupported OpenRouter model capability")
    if len(setup.capabilities) != len(set(setup.capabilities)):
        raise ValueError("OpenRouter capabilities must be unique")
    if ModelCapability.EMBEDDING.value in setup.capabilities and len(setup.capabilities) != 1:
        raise ValueError("embedding setup must be selected as its own capability")
    if not math.isfinite(float(setup.temperature)) or not 0 <= float(setup.temperature) <= 2:
        raise ValueError("OpenRouter temperature must be between 0 and 2")
    if not 1 <= int(setup.max_output_tokens) <= _MAX_OPENROUTER_OUTPUT_TOKENS:
        raise ValueError("OpenRouter max output tokens must be between 1 and 131072")
    if not math.isfinite(float(setup.timeout_seconds)) or not 1 <= float(setup.timeout_seconds) <= _MAX_OPENROUTER_TIMEOUT_SECONDS:
        raise ValueError("OpenRouter timeout must be between 1 and 120 seconds")
    if not setup.allowed_upstreams or len(setup.allowed_upstreams) > 32:
        raise ValueError("an explicit OpenRouter upstream allow-list is required")
    if any(_SAFE_OPENROUTER_UPSTREAM.fullmatch(item) is None for item in setup.allowed_upstreams):
        raise ValueError("OpenRouter upstream ids must be bounded safe identifiers")
    if len(set(setup.allowed_upstreams)) != len(setup.allowed_upstreams):
        raise ValueError("OpenRouter upstream allow-list entries must be unique")
    if setup.allow_fallbacks is not False:
        raise ValueError("OpenRouter fallbacks must be disabled")
    if setup.require_parameters is not True:
        raise ValueError("OpenRouter requests must require declared parameters")
    if setup.data_collection != "deny" or setup.data_retention_policy != "deny":
        raise ValueError("OpenRouter data collection and retention must be denied")
    if any(item in {ModelCapability.VISION.value, ModelCapability.EMBEDDING.value} for item in setup.capabilities) and setup.zero_data_retention is not True:
        raise ValueError("vision and embedding workloads require zero-data-retention policy")
    if setup.egress_class is EgressClass.LOCAL_ONLY or not setup.cloud_egress_acknowledged:
        raise ValueError("OpenRouter setup requires explicit cloud egress acknowledgement")
    if setup.spend_ceiling_microusd is None or not 0 <= int(setup.spend_ceiling_microusd) <= 1_000_000_000:
        raise ValueError("OpenRouter setup requires a finite spend ceiling")
    if setup.max_inflight != 1:
        raise ValueError("OpenRouter admission allows exactly one in-flight request")
    if not 1 <= int(setup.max_queued) <= _MAX_OPENROUTER_QUEUE:
        raise ValueError("OpenRouter queue bound must be between 1 and 64")
    if not 1 <= int(setup.max_outstanding_per_owner) <= _MAX_OPENROUTER_OWNER_OUTSTANDING:
        raise ValueError("OpenRouter owner outstanding bound must be between 1 and 16")
    if not 0 <= int(setup.max_retries) <= _MAX_OPENROUTER_RETRIES:
        raise ValueError("OpenRouter retry bound must be between 0 and 2")
    if setup.credential_ref not in {OPENROUTER_VAULT_CREDENTIAL_REF, OPENROUTER_ENV_CREDENTIAL_REF, "OPENROUTER_API_KEY"}:
        raise ValueError("OpenRouter credential reference must use the trusted vault or env reference")
    if setup.credential_fingerprint is not None and re.fullmatch(r"[0-9a-f]{12}", setup.credential_fingerprint) is None:
        raise ValueError("OpenRouter credential fingerprint is invalid")


def _openrouter_setup_from_payload(payload: object) -> OpenRouterSetup:
    if not isinstance(payload, dict):
        raise ValueError("OpenRouter setup must be an object")
    allowed = set(OpenRouterSetup.__dataclass_fields__)
    if set(payload) - allowed:
        raise ValueError("OpenRouter setup contains unsupported fields")
    values = dict(payload)
    for field_name in ("model_ids", "capabilities", "allowed_upstreams"):
        raw_value = values.get(field_name) or ()
        if not isinstance(raw_value, (list, tuple)):
            raise ValueError(f"OpenRouter setup {field_name} must be a list")
        values[field_name] = tuple(str(item).strip() for item in raw_value)
    values["egress_class"] = EgressClass(values.get("egress_class", EgressClass.LOCAL_ONLY))
    values["credential_ref"] = str(values.get("credential_ref") or OPENROUTER_VAULT_CREDENTIAL_REF).strip()
    if values["credential_ref"] == "OPENROUTER_API_KEY":
        values["credential_ref"] = OPENROUTER_ENV_CREDENTIAL_REF
    if "model_ids" in values:
        values["model_ids"] = tuple(normalize_openrouter_model_id(item) for item in values["model_ids"])
    setup = OpenRouterSetup(**values)
    validate_openrouter_setup(setup)
    return setup


def _openrouter_setup_payload(setup: OpenRouterSetup) -> dict[str, object]:
    return {
        **asdict(setup),
        "model_ids": list(setup.model_ids),
        "capabilities": list(setup.capabilities),
        "allowed_upstreams": list(setup.allowed_upstreams),
        "egress_class": setup.egress_class.value,
    }


def openrouter_profile_for_setup(setup: OpenRouterSetup) -> ProviderProfile:
    """Build the existing canonical profile from one validated setup."""
    validate_openrouter_setup(setup)
    routing_model = setup.model_ids[0]
    transport_model = transport_model_for_provider(OPENROUTER_PROVIDER_KIND, routing_model)
    runtime_controls = {
        "model_ids": list(setup.model_ids),
        "temperature": float(setup.temperature),
        "output_limit": int(setup.max_output_tokens),
        "timeout_seconds": float(setup.timeout_seconds),
        "allowed_upstreams": list(setup.allowed_upstreams),
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "data_retention_policy": "deny",
        "zero_data_retention": bool(setup.zero_data_retention),
        "egress_class": setup.egress_class.value,
        "cloud_egress_acknowledged": bool(setup.cloud_egress_acknowledged),
        "max_queued": int(setup.max_queued),
        "max_inflight": int(setup.max_inflight),
        "max_outstanding_per_owner": int(setup.max_outstanding_per_owner),
        "max_retries": int(setup.max_retries),
        "spend_ceiling_microusd": int(setup.spend_ceiling_microusd or 0),
    }
    return ProviderProfile(
        id=setup.profile_id,
        provider_kind=OPENROUTER_PROVIDER_KIND,
        model=transport_model,
        routing_model=routing_model,
        api_base=OPENROUTER_API_BASE,
        secret_env="OPENROUTER_API_KEY",
        options={
            "provider": {
                "only": list(setup.allowed_upstreams),
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "data_retention_policy": "deny",
                "zdr": bool(setup.zero_data_retention),
            },
            OPENROUTER_RUNTIME_CONTROLS_KEY: runtime_controls,
        },
        capabilities=setup.capabilities,
        task_class="interactive_chat",
        task_classes=("interactive_chat", "vision_analysis", "report_synthesis", "memory_synthesis", "agent_reasoning"),
        budget_class="medium",
        fallback_models=(),
        enabled=True,
        keyless=False,
        safety_notes="Operator-configured OpenRouter profile; credentials stay in the trusted backend source.",
        transport_adapter=(
            "openai_compatible_embeddings"
            if setup.capabilities == (ModelCapability.EMBEDDING.value,)
            else "openai_compatible_chat"
        ),
        context_window_tokens=max(setup.max_output_tokens, 4096),
        max_output_tokens=setup.max_output_tokens,
        cost_microusd=setup.spend_ceiling_microusd,
        cost_source="operator_spend_ceiling",
        cost_source_updated_at=datetime.now(timezone.utc).timestamp(),
        max_latency_ms=max(int(setup.timeout_seconds * 1000), 1),
    )
