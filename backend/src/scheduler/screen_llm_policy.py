"""Trust-boundary checks for LLM calls over screen-derived records."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from config.settings import settings
from src.llm_runtime import resolve_runtime_profile
from src.model_fabric import candidate_from_profile, model_fabric_repository
from src.model_fabric.configuration import effective_workload_policy
from src.model_fabric.caller_context import canonical_route_spec
from src.model_fabric.proofs import proof_is_fresh
from src.model_fabric.selector import active_provider_exclusion_reason
from src.security.trust_contract import EgressClass


@dataclass(frozen=True)
class ScreenDerivedLlmDecision:
    allowed: bool
    reason: str
    runtime_path: str
    runtime_profile: str
    proof_status: dict | None = None


async def _openrouter_profile_ready(runtime_path: str, profile_id: str) -> tuple[bool, str]:
    """Check the same persisted profile/proof contract used at dispatch."""
    from src.llm_runtime import provider_profiles

    profile = provider_profiles().get(profile_id)
    if profile is None:
        return False, "model_fabric_profile_missing"
    if reason := active_provider_exclusion_reason(profile):
        return False, f"model_fabric_profile_{reason}"
    if profile.cost_microusd is None or not profile.cost_source or profile.cost_source_updated_at is None:
        return False, "model_fabric_cost_bound_missing"
    candidate = candidate_from_profile(profile)
    spec = canonical_route_spec(runtime_path)
    required = {capability.value for capability in spec.capabilities}
    required.update({"latency_ms", "health"})
    for capability in sorted(required):
        try:
            proof = await asyncio.wait_for(
                model_fabric_repository.latest_capability_proof(
                    profile_schema_version=profile.schema_version,
                    profile_contract_hash=profile.contract_hash,
                    profile_id=profile.id,
                    model=profile.model,
                    endpoint=candidate.endpoint,
                    endpoint_class=candidate.endpoint_class,
                    adapter=candidate.adapter,
                    capability=capability,
                ),
                timeout=0.5,
            )
        except asyncio.TimeoutError:
            return False, f"model_fabric_proof_unavailable:{capability}"
        except Exception:
            return False, f"model_fabric_proof_unavailable:{capability}"
        if proof is None:
            return False, f"model_fabric_proof_required:{capability}"
        if not proof_is_fresh(proof):
            return False, f"model_fabric_proof_stale:{capability}"
        if (
            proof.profile_schema_version != profile.schema_version
            or proof.profile_contract_hash != profile.contract_hash
            or proof.profile_id != profile.id
            or proof.model != profile.model
            or proof.endpoint != candidate.endpoint
            or proof.endpoint_class != candidate.endpoint_class
            or proof.adapter != candidate.adapter
        ):
            return False, f"model_fabric_proof_binding_mismatch:{capability}"
        if capability == "health" and proof.proven_value != "healthy":
            return False, "model_fabric_proof_health_not_healthy"
        if capability == "latency_ms":
            try:
                if int(proof.proven_value) > int(profile.max_latency_ms or 0):
                    return False, "model_fabric_proof_latency_noncompliant"
            except (TypeError, ValueError):
                return False, "model_fabric_proof_latency_invalid"
    return True, "model_fabric_profile_and_proofs_ready"


async def screen_derived_llm_decision(runtime_path: str) -> ScreenDerivedLlmDecision:
    """Return whether a screen-derived LLM prompt may be sent for this runtime path."""
    # The digest is its own user-visible report surface.  Do not accidentally
    # inherit the end-of-day report flag and leave an explicitly enabled digest
    # permanently disabled in the OpenRouter-only deployment.
    enabled = (
        settings.screenshot_observation_digest_enabled
        if runtime_path == "screenshot_observation_digest"
        else settings.end_of_day_report_llm_enabled
    )
    if not enabled:
        return ScreenDerivedLlmDecision(
            allowed=False,
            reason="llm_disabled",
            runtime_path=runtime_path,
            runtime_profile="",
        )

    runtime_profile = resolve_runtime_profile(runtime_path=runtime_path)
    # Screen-derived prompts have one active route in this phase. A stale
    # compatibility flag or local preference must not revive the retired VLM
    # lane or an ungoverned remote fallback.
    policy = effective_workload_policy(runtime_path)
    upstreams = str(getattr(settings, "openrouter_allowed_upstreams", "") or "").strip()
    if runtime_profile != "openrouter":
        return ScreenDerivedLlmDecision(
            allowed=False,
            reason="openrouter_profile_required",
            runtime_path=runtime_path,
            runtime_profile=runtime_profile,
        )
    if (
        not bool(getattr(settings, "openrouter_provider_only", True))
        or bool(getattr(settings, "openrouter_allow_fallbacks", False))
        or bool(getattr(settings, "openrouter_require_parameters", True)) is not True
        or str(getattr(settings, "openrouter_data_collection", "deny") or "deny") != "deny"
        or not settings.openrouter_api_key.strip()
        or not upstreams
        or policy.egress_class is not EgressClass.CLOUD_ALLOWED_FULL
        or not policy.cloud_egress_acknowledged
        or set(policy.allowed_provider_kinds) != {"openrouter"}
        or policy.fallback_allowed
        or policy.max_cost_microusd is None
    ):
        return ScreenDerivedLlmDecision(
            allowed=False,
            reason="openrouter_cloud_policy_missing",
            runtime_path=runtime_path,
            runtime_profile=runtime_profile,
        )
    ready, readiness_reason = await _openrouter_profile_ready(runtime_path, runtime_profile)
    if not ready:
        return ScreenDerivedLlmDecision(
            allowed=False,
            reason=readiness_reason,
            runtime_path=runtime_path,
            runtime_profile=runtime_profile,
        )
    return ScreenDerivedLlmDecision(
        allowed=True,
        reason=readiness_reason,
        runtime_path=runtime_path,
        runtime_profile=runtime_profile,
    )
