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
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
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
class ModelFabricConfiguration:
    profiles: tuple[ProviderProfile, ...] = ()
    workload_policies: tuple[WorkloadPolicy, ...] = ()
    status: str = "missing"
    error_code: str | None = None
    updated_at: str | None = None


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


def effective_provider_profiles(legacy_profiles: dict[str, ProviderProfile]) -> dict[str, ProviderProfile]:
    """Apply persisted canonical profiles over legacy compatibility inputs."""
    configured = read_model_fabric_configuration()
    profiles = dict(legacy_profiles)
    if configured.status == "ready":
        profiles.update({profile.id: profile for profile in configured.profiles})
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
    return ModelFabricConfiguration(
        profiles=profiles,
        workload_policies=policies,
        status="ready",
        updated_at=str(payload.get("updated_at") or "") or None,
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
    return {
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


def _profile_payload(profile: ProviderProfile) -> dict[str, object]:
    payload = asdict(profile)
    payload["capabilities"] = list(profile.capabilities)
    payload["task_classes"] = list(profile.task_classes)
    payload["fallback_models"] = list(profile.fallback_models)
    return payload
