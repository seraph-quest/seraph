"""Shared LLM configuration and fallback helpers."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
from fnmatch import fnmatchcase
from threading import Lock
from time import monotonic
from typing import Any
from uuid import uuid4

from smolagents import LiteLLMModel as BaseLiteLLMModel

from config.settings import settings
from src.approval.runtime import get_current_session_id
from src.audit.repository import audit_repository

logger = logging.getLogger(__name__)
_runtime_request_lock = Lock()
_runtime_requests: dict[str, bool] = {}
_target_health_lock = Lock()
_unhealthy_targets: dict[tuple[str, str | None, str | None], float] = {}
_KNOWN_RUNTIME_PROFILES = {"default", "local"}
_GUARDRAIL_TIERS = {"low": 0, "medium": 1, "high": 2}
_runtime_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_runtime_request_id",
    default=None,
)


def _primary_api_key() -> str:
    return settings.llm_api_key or settings.openrouter_api_key


def has_local_model_profile() -> bool:
    """Return whether a local-model profile is configured."""
    return bool(settings.local_model.strip())


def local_runtime_paths() -> set[str]:
    """Return the configured runtime-path names that should prefer the local profile."""
    return {
        path.strip()
        for path in settings.local_runtime_paths.split(",")
        if path.strip()
    }


def _runtime_path_match_kind(pattern: str, runtime_path: str | None) -> str | None:
    """Return whether a config pattern matches the runtime path exactly or via glob."""
    if runtime_path is None:
        return None
    normalized_pattern = pattern.strip()
    if not normalized_pattern:
        return None
    if normalized_pattern == runtime_path:
        return "exact"
    if any(char in normalized_pattern for char in "*?[]") and fnmatchcase(runtime_path, normalized_pattern):
        return "pattern"
    return None


def _normalize_policy_tag(raw_tag: str) -> str:
    return raw_tag.strip().lower().replace("-", "_").replace(" ", "_")


def _parse_policy_tags(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    tags: list[str] = []
    for raw_tag in raw_value.split("|"):
        normalized = _normalize_policy_tag(raw_tag)
        if normalized and normalized not in tags:
            tags.append(normalized)
    return tags


def _select_runtime_entry(raw_entries: str, *, runtime_path: str | None, separator: str) -> str | None:
    """Return the best matching config value for a runtime path.

    Exact path matches win over wildcard matches. If multiple wildcard entries
    match, the first configured wildcard wins.
    """
    wildcard_value: str | None = None
    for raw_entry in raw_entries.split(separator):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            continue
        path, _, raw_value = entry.partition("=")
        match_kind = _runtime_path_match_kind(path, runtime_path)
        if match_kind == "exact":
            return raw_value.strip()
        if match_kind == "pattern" and wildcard_value is None:
            wildcard_value = raw_value.strip()
    return wildcard_value


def prefers_local_runtime_path(runtime_path: str | None) -> bool:
    """Return whether the runtime path should prefer the local profile."""
    return any(
        _runtime_path_match_kind(path, runtime_path)
        for path in local_runtime_paths()
    )


def _runtime_model_override(runtime_path: str | None) -> tuple[str | None, str] | None:
    """Return an optional runtime-path-specific profile/model override."""
    value = _select_runtime_entry(
        settings.runtime_model_overrides,
        runtime_path=runtime_path,
        separator=",",
    )
    if not value:
        return None
    if ":" in value:
        maybe_profile, model_id = value.split(":", 1)
        normalized_profile = maybe_profile.strip()
        normalized_model_id = model_id.strip()
        if normalized_profile in {"default", "local"} and normalized_model_id:
            return normalized_profile, normalized_model_id
    return None, value


def _normalize_runtime_profile(profile: str) -> str | None:
    normalized = profile.strip()
    if normalized not in _KNOWN_RUNTIME_PROFILES:
        return None
    if normalized == "local" and not has_local_model_profile():
        return None
    return normalized


def runtime_profile_preferences(runtime_path: str | None) -> list[str]:
    """Return the configured ordered profile preferences for a runtime path."""
    value = _select_runtime_entry(
        settings.runtime_profile_preferences,
        runtime_path=runtime_path,
        separator=";",
    )
    if not value:
        return []
    preferences: list[str] = []
    for raw_profile in value.split("|"):
        normalized = _normalize_runtime_profile(raw_profile)
        if normalized and normalized not in preferences:
            preferences.append(normalized)
    return preferences


def runtime_policy_intents(runtime_path: str | None) -> list[str]:
    """Return the configured ordered policy intents for a runtime path."""
    value = _select_runtime_entry(
        settings.runtime_policy_intents,
        runtime_path=runtime_path,
        separator=";",
    )
    return _parse_policy_tags(value)


def runtime_policy_requirements(runtime_path: str | None) -> list[str]:
    """Return hard required policy intents for a runtime path."""
    value = _select_runtime_entry(
        settings.runtime_policy_requirements,
        runtime_path=runtime_path,
        separator=";",
    )
    return _parse_policy_tags(value)


def runtime_policy_scores(runtime_path: str | None) -> dict[str, float]:
    """Return optional weighted policy scores for a runtime path."""
    value = _select_runtime_entry(
        settings.runtime_policy_scores,
        runtime_path=runtime_path,
        separator=";",
    )
    if not value:
        return {}

    scores: dict[str, float] = {}
    for raw_entry in value.split("|"):
        entry = raw_entry.strip()
        if not entry or ":" not in entry:
            continue
        raw_intent, _, raw_weight = entry.partition(":")
        intent = _normalize_policy_tag(raw_intent)
        if not intent:
            continue
        try:
            weight = float(raw_weight.strip())
        except ValueError:
            continue
        if not math.isfinite(weight):
            continue
        if weight <= 0:
            continue
        scores[intent] = weight
    return scores


def _normalize_guardrail_tier(raw_value: str | None) -> str | None:
    normalized = (raw_value or "").strip().lower()
    if normalized in _GUARDRAIL_TIERS:
        return normalized
    return None


def runtime_max_cost_tier(runtime_path: str | None) -> str | None:
    return _normalize_guardrail_tier(
        _select_runtime_entry(
            settings.runtime_max_cost_tier,
            runtime_path=runtime_path,
            separator=";",
        )
    )


def runtime_max_latency_tier(runtime_path: str | None) -> str | None:
    return _normalize_guardrail_tier(
        _select_runtime_entry(
            settings.runtime_max_latency_tier,
            runtime_path=runtime_path,
            separator=";",
        )
    )


def provider_capabilities(
    model_id: str | None,
    *,
    profile: str | None = None,
) -> list[str]:
    """Return the declared capability tags for a provider target."""
    capabilities = _parse_policy_tags(
        _select_runtime_entry(
            settings.provider_capability_overrides,
            runtime_path=model_id,
            separator=";",
        )
    )
    if profile == "local" and "local" not in capabilities:
        capabilities.append("local")
    return capabilities


def provider_cost_tier(
    model_id: str | None,
    *,
    profile: str | None = None,
) -> str | None:
    return _normalize_guardrail_tier(
        _select_runtime_entry(
            settings.provider_cost_tiers,
            runtime_path=model_id,
            separator=";",
        )
    )


def provider_latency_tier(
    model_id: str | None,
    *,
    profile: str | None = None,
) -> str | None:
    return _normalize_guardrail_tier(
        _select_runtime_entry(
            settings.provider_latency_tiers,
            runtime_path=model_id,
            separator=";",
        )
    )


def runtime_profile_candidates(
    *,
    runtime_path: str | None = None,
    profile: str | None = None,
) -> list[str]:
    """Return the ordered runtime profiles to try for an implicit runtime path."""
    if profile:
        return [profile]

    candidates: list[str] = []
    override = _runtime_model_override(runtime_path)
    override_profile = override[0] if override else None
    configured_preferences = runtime_profile_preferences(runtime_path)
    ordered_preferences = configured_preferences
    policy_intents = runtime_policy_intents(runtime_path)

    if override_profile:
        if override_profile in configured_preferences:
            ordered_preferences = [
                override_profile,
                *[candidate for candidate in configured_preferences if candidate != override_profile],
            ]
        elif configured_preferences:
            ordered_preferences = [override_profile, *configured_preferences]
        else:
            ordered_preferences = [override_profile]

    for candidate in ordered_preferences:
        normalized = _normalize_runtime_profile(candidate)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    if not candidates:
        if (
            not override_profile
            and not configured_preferences
            and "local_first" in policy_intents
            and has_local_model_profile()
        ):
            candidates.extend(["local", "default"])
        elif prefers_local_runtime_path(runtime_path) and has_local_model_profile():
            candidates.append("local")
        else:
            candidates.append("default")

    return candidates


def resolve_runtime_profile(
    *,
    runtime_path: str | None = None,
    profile: str | None = None,
) -> str:
    """Resolve the runtime profile name for the current call."""
    return runtime_profile_candidates(runtime_path=runtime_path, profile=profile)[0]


def _resolved_primary_model_id(
    *,
    runtime_path: str | None = None,
    profile: str = "default",
) -> str:
    override = _runtime_model_override(runtime_path)
    if override:
        override_profile, override_model_id = override
        if override_profile is None or override_profile == profile:
            return override_model_id
    return _profile_model_id(profile)


def _profile_model_id(profile: str) -> str:
    if profile == "local" and has_local_model_profile():
        return settings.local_model
    return settings.default_model


def _profile_api_key(profile: str) -> str:
    if profile == "local" and has_local_model_profile():
        return settings.local_llm_api_key or _primary_api_key()
    return _primary_api_key()


def _profile_api_base(profile: str) -> str:
    if profile == "local" and has_local_model_profile():
        return settings.local_llm_api_base or settings.llm_api_base
    return settings.llm_api_base


def fallback_model_ids(*, runtime_path: str | None = None) -> list[str]:
    """Return the ordered list of configured fallback model ids."""
    runtime_override_ids: list[str] = []
    value = _select_runtime_entry(
        settings.runtime_fallback_overrides,
        runtime_path=runtime_path,
        separator=";",
    )
    if value:
        for model_id in value.split("|"):
            normalized = model_id.strip()
            if normalized and normalized not in runtime_override_ids:
                runtime_override_ids.append(normalized)
    if runtime_override_ids:
        return runtime_override_ids

    fallback_ids: list[str] = []
    for raw_value in (settings.fallback_model, settings.fallback_models):
        for model_id in raw_value.split(","):
            normalized = model_id.strip()
            if normalized and normalized not in fallback_ids:
                fallback_ids.append(normalized)
    return fallback_ids


def build_model_kwargs(
    *,
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
    runtime_path: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build LiteLLMModel kwargs from the shared runtime settings."""
    resolved_profile = resolve_runtime_profile(runtime_path=runtime_path, profile=profile)
    resolved_model_id = (
        _profile_model_id(resolved_profile)
        if profile is not None
        else _resolved_primary_model_id(
            runtime_path=runtime_path,
            profile=resolved_profile,
        )
    )
    kwargs: dict[str, Any] = {
        "model_id": model_id or resolved_model_id,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "runtime_profile": resolved_profile,
        "runtime_path": runtime_path,
    }
    api_key = _profile_api_key(resolved_profile)
    if api_key:
        kwargs["api_key"] = api_key
    api_base = _profile_api_base(resolved_profile)
    if api_base:
        kwargs["api_base"] = api_base
    return kwargs


def build_completion_kwargs(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
    use_fallback: bool = False,
    fallback_model_id: str | None = None,
    fallback_api_key: str | None = None,
    fallback_api_base: str | None = None,
    runtime_path: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build litellm.completion kwargs for either the primary or fallback path."""
    if use_fallback:
        fallback_model = fallback_model_id or next(iter(fallback_model_ids(runtime_path=runtime_path)), "")
        if not fallback_model:
            raise ValueError("Fallback model is not configured")
        kwargs: dict[str, Any] = {
            "model": model_id or fallback_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        api_key = fallback_api_key or settings.fallback_llm_api_key or _primary_api_key()
        api_base = fallback_api_base or settings.fallback_llm_api_base or settings.llm_api_base
    else:
        resolved_profile = resolve_runtime_profile(runtime_path=runtime_path, profile=profile)
        resolved_model_id = (
            _profile_model_id(resolved_profile)
            if profile is not None
            else _resolved_primary_model_id(
                runtime_path=runtime_path,
                profile=resolved_profile,
            )
        )
        kwargs = {
            "model": model_id or resolved_model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        api_key = _profile_api_key(resolved_profile)
        api_base = _profile_api_base(resolved_profile)

    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    return kwargs


def has_fallback_model(*, runtime_path: str | None = None) -> bool:
    """Return whether a fallback completion target is configured."""
    return bool(fallback_model_ids(runtime_path=runtime_path))


def _fallback_api_key(primary_api_key: str | None, *, primary_profile: str = "default") -> str:
    if settings.fallback_llm_api_key:
        return settings.fallback_llm_api_key
    if primary_profile == "local":
        return _primary_api_key()
    return primary_api_key or _primary_api_key()


def _fallback_api_base(primary_api_base: str | None, *, primary_profile: str = "default") -> str:
    if settings.fallback_llm_api_base:
        return settings.fallback_llm_api_base
    if primary_profile == "local":
        return settings.llm_api_base
    return primary_api_base or settings.llm_api_base


def _safe_model_name(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("model", "unknown"))


def _target_key(*, model_id: str, api_base: str | None, api_key: str | None) -> tuple[str, str | None, str | None]:
    return (model_id, api_base or None, api_key or None)


def _fallback_targets(
    *,
    primary_model_id: str,
    primary_api_base: str | None,
    primary_api_key: str | None,
    primary_profile: str = "default",
    runtime_path: str | None = None,
    profile: str | None = None,
) -> list[dict[str, str | None]]:
    seen_targets = {
        _target_key(
            model_id=primary_model_id,
            api_base=primary_api_base,
            api_key=primary_api_key,
        ),
    }
    targets: list[dict[str, str | None]] = []

    for candidate_profile in runtime_profile_candidates(
        runtime_path=runtime_path,
        profile=profile,
    )[1:]:
        candidate_api_key = _profile_api_key(candidate_profile)
        candidate_api_base = _profile_api_base(candidate_profile)
        candidate_model_id = _resolved_primary_model_id(
            runtime_path=runtime_path,
            profile=candidate_profile,
        )
        target_key = _target_key(
            model_id=candidate_model_id,
            api_base=candidate_api_base,
            api_key=candidate_api_key,
        )
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)
        targets.append(
            {
                "model_id": candidate_model_id,
                "api_base": candidate_api_base,
                "api_key": candidate_api_key,
                "profile": candidate_profile,
                "source": "alternate_profile",
            }
        )

    fallback_api_key = _fallback_api_key(primary_api_key, primary_profile=primary_profile)
    fallback_api_base = _fallback_api_base(primary_api_base, primary_profile=primary_profile)
    for fallback_model_id in fallback_model_ids(runtime_path=runtime_path):
        target_key = _target_key(
            model_id=fallback_model_id,
            api_base=fallback_api_base,
            api_key=fallback_api_key,
        )
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)
        targets.append(
            {
                "model_id": fallback_model_id,
                "api_base": fallback_api_base,
                "api_key": fallback_api_key,
                "profile": None,
                "source": "fallback_chain",
            }
        )
    return _order_targets_by_policy(targets, runtime_path=runtime_path)


def _target_cooldown_seconds() -> int:
    return max(0, settings.llm_target_cooldown_seconds)


def _reset_target_health() -> None:
    with _target_health_lock:
        _unhealthy_targets.clear()


def _mark_target_failed(
    *,
    model_id: str,
    api_base: str | None,
    api_key: str | None,
) -> None:
    cooldown_seconds = _target_cooldown_seconds()
    if cooldown_seconds <= 0:
        return
    with _target_health_lock:
        _unhealthy_targets[_target_key(model_id=model_id, api_base=api_base, api_key=api_key)] = (
            monotonic() + cooldown_seconds
        )


def _mark_target_succeeded(
    *,
    model_id: str,
    api_base: str | None,
    api_key: str | None,
) -> None:
    with _target_health_lock:
        _unhealthy_targets.pop(
            _target_key(model_id=model_id, api_base=api_base, api_key=api_key),
            None,
        )


def _is_target_healthy(
    *,
    model_id: str,
    api_base: str | None,
    api_key: str | None,
) -> bool:
    target_key = _target_key(model_id=model_id, api_base=api_base, api_key=api_key)
    with _target_health_lock:
        unhealthy_until = _unhealthy_targets.get(target_key)
        if unhealthy_until is None:
            return True
        if unhealthy_until <= monotonic():
            _unhealthy_targets.pop(target_key, None)
            return True
        return False


def _partition_targets_by_health(
    targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    healthy_targets: list[dict[str, Any]] = []
    unhealthy_targets: list[dict[str, Any]] = []
    for target in targets:
        if _is_target_healthy(
            model_id=str(target["model_id"]),
            api_base=target.get("api_base"),
            api_key=target.get("api_key"),
        ):
            healthy_targets.append(target)
        else:
            unhealthy_targets.append(target)
    return healthy_targets, unhealthy_targets


def _tier_within_guardrail(actual: str | None, maximum: str | None) -> bool:
    if maximum is None:
        return True
    if actual is None:
        return False
    return _GUARDRAIL_TIERS[actual] <= _GUARDRAIL_TIERS[maximum]


def _target_policy_assessment(
    *,
    model_id: str,
    profile: str | None,
    runtime_path: str | None,
) -> dict[str, Any]:
    requirements = runtime_policy_requirements(runtime_path)
    matched_requirements = _matched_policy_intents(
        model_id=model_id,
        profile=profile,
        policy_intents=requirements,
    )
    missing_requirements = [
        intent
        for intent in requirements
        if intent not in matched_requirements
    ]
    cost_tier = provider_cost_tier(model_id, profile=profile)
    latency_tier = provider_latency_tier(model_id, profile=profile)
    max_cost_tier = runtime_max_cost_tier(runtime_path)
    max_latency_tier = runtime_max_latency_tier(runtime_path)
    within_cost_guardrail = _tier_within_guardrail(cost_tier, max_cost_tier)
    within_latency_guardrail = _tier_within_guardrail(latency_tier, max_latency_tier)
    policy_compliant = (
        not missing_requirements
        and within_cost_guardrail
        and within_latency_guardrail
    )
    return {
        "required_policy_intents": requirements,
        "matched_required_intents": matched_requirements,
        "missing_required_intents": missing_requirements,
        "cost_tier": cost_tier,
        "latency_tier": latency_tier,
        "max_cost_tier": max_cost_tier,
        "max_latency_tier": max_latency_tier,
        "within_cost_guardrail": within_cost_guardrail,
        "within_latency_guardrail": within_latency_guardrail,
        "policy_compliant": policy_compliant,
    }


def _order_targets_by_policy(
    targets: list[dict[str, Any]],
    *,
    runtime_path: str | None,
) -> list[dict[str, Any]]:
    intents = runtime_policy_intents(runtime_path)
    score_weights = runtime_policy_scores(runtime_path)
    annotated_targets: list[tuple[int, dict[str, Any]]] = []
    any_compliant = False
    for index, target in enumerate(targets):
        annotated_target = dict(target)
        assessment = _target_policy_assessment(
            model_id=str(annotated_target["model_id"]),
            profile=annotated_target.get("profile"),
            runtime_path=runtime_path,
        )
        annotated_target["policy_assessment"] = assessment
        if assessment["policy_compliant"]:
            any_compliant = True
        annotated_targets.append((index, annotated_target))

    if not intents and not any(
        target["policy_assessment"]["required_policy_intents"]
        or target["policy_assessment"]["max_cost_tier"] is not None
        or target["policy_assessment"]["max_latency_tier"] is not None
        for _, target in annotated_targets
    ):
        return [target for _, target in annotated_targets]

    desired_capabilities = [intent for intent in intents if intent != "local_first"]
    prefer_local = "local_first" in intents

    def _sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, float, tuple[int, ...], int]:
        index, target = item
        assessment = target["policy_assessment"]
        capabilities = set(
            provider_capabilities(
                str(target["model_id"]),
                profile=target.get("profile"),
            )
        )
        local_score = score_weights.get("local_first", 1.0) if prefer_local and target.get("profile") == "local" else 0.0
        capability_priority = tuple(
            0 if capability in capabilities else 1
            for capability in desired_capabilities
        )
        capability_score = 0.0
        if score_weights:
            capability_score = sum(
                score_weights.get(capability, 0.0)
                for capability in desired_capabilities
                if capability in capabilities
            )
        compliance_penalty = 0
        safeguard_penalty = 0
        if any_compliant and not assessment["policy_compliant"]:
            compliance_penalty = 1
            safeguard_penalty = len(assessment["missing_required_intents"])
            if not assessment["within_cost_guardrail"]:
                safeguard_penalty += 1
            if not assessment["within_latency_guardrail"]:
                safeguard_penalty += 1
        return (compliance_penalty, safeguard_penalty, -local_score - capability_score, capability_priority, index)

    return [
        target
        for _, target in sorted(annotated_targets, key=_sort_key)
    ]


def _matched_policy_intents(
    *,
    model_id: str,
    profile: str | None,
    policy_intents: list[str],
) -> list[str]:
    capabilities = provider_capabilities(model_id, profile=profile)
    matched: list[str] = []
    if "local_first" in policy_intents and profile == "local":
        matched.append("local_first")
    matched.extend(
        intent
        for intent in policy_intents
        if intent != "local_first" and intent in capabilities
    )
    return matched


def _policy_score(
    *,
    model_id: str,
    profile: str | None,
    policy_intents: list[str],
    policy_scores: dict[str, float],
) -> float:
    if not policy_scores:
        return 0.0
    matched = _matched_policy_intents(
        model_id=model_id,
        profile=profile,
        policy_intents=policy_intents,
    )
    return sum(policy_scores.get(intent, 0.0) for intent in matched)


def _candidate_reason_codes(
    *,
    source: str,
    decision: str,
    healthy: bool,
    rerouted_primary: bool,
    missing_required_intents: list[str],
    within_cost_guardrail: bool,
    within_latency_guardrail: bool,
    guardrail_fallback_only: bool,
) -> list[str]:
    reasons: list[str] = []
    if source == "primary":
        reasons.append("primary_target")
    elif source == "alternate_profile":
        reasons.append("alternate_profile_candidate")
    elif source == "fallback_chain":
        reasons.append("configured_fallback_chain")

    if decision == "selected":
        reasons.append("selected_for_attempt")
    elif decision == "deferred":
        reasons.append("kept_as_standby")
    elif decision == "deprioritized":
        reasons.append("deprioritized_after_healthy_targets")
    elif decision == "skipped":
        reasons.append("skipped_for_current_attempt")

    if not healthy:
        reasons.append("unhealthy_cooldown")
    if rerouted_primary:
        reasons.append("rerouted_away_from_primary")
    if missing_required_intents:
        reasons.append("missing_required_intents")
    if not within_cost_guardrail:
        reasons.append("cost_guardrail_exceeded")
    if not within_latency_guardrail:
        reasons.append("latency_guardrail_exceeded")
    if guardrail_fallback_only:
        reasons.append("no_guardrail_compliant_targets")
    return reasons


def _ordered_candidate_targets(
    *,
    primary_target: dict[str, Any],
    fallback_targets: list[dict[str, Any]],
    runtime_path: str | None,
) -> list[dict[str, Any]]:
    policy_ordered = _order_targets_by_policy(
        [primary_target, *fallback_targets],
        runtime_path=runtime_path,
    )
    primary_candidate = next(
        target for target in policy_ordered if target["source"] == "primary"
    )
    fallback_candidates = [
        target for target in policy_ordered if target["source"] != "primary"
    ]
    healthy_fallbacks, unhealthy_fallbacks = _partition_targets_by_health(
        fallback_candidates
    )
    fallback_order = healthy_fallbacks + unhealthy_fallbacks

    primary_assessment = primary_candidate["policy_assessment"]
    guardrails_configured = bool(
        primary_assessment["required_policy_intents"]
        or primary_assessment["max_cost_tier"] is not None
        or primary_assessment["max_latency_tier"] is not None
    )
    primary_healthy = _is_target_healthy(
        model_id=str(primary_candidate["model_id"]),
        api_base=primary_candidate.get("api_base"),
        api_key=primary_candidate.get("api_key"),
    )
    compliant_targets_present = any(
        target["policy_assessment"]["policy_compliant"]
        for target in [primary_candidate, *fallback_order]
    )
    reroute_due_to_policy = (
        guardrails_configured
        and compliant_targets_present
        and not primary_assessment["policy_compliant"]
    )
    reroute_due_to_health = (
        not primary_healthy and any(healthy_fallbacks)
    )
    if reroute_due_to_policy or reroute_due_to_health:
        healthy_targets, unhealthy_targets = _partition_targets_by_health(policy_ordered)
        return healthy_targets + unhealthy_targets
    return [primary_candidate, *fallback_order]


def _build_routing_decision_details(
    *,
    runtime_path: str,
    runtime_profile: str,
    primary_model: str,
    primary_api_base: str | None,
    primary_api_key: str | None,
    primary_profile: str | None,
    ordered_targets: list[dict[str, Any]],
    rerouted: bool,
    rerouted_due_to_policy: bool,
) -> dict[str, Any]:
    policy_intents = runtime_policy_intents(runtime_path)
    required_policy_intents = runtime_policy_requirements(runtime_path)
    policy_scores = runtime_policy_scores(runtime_path)
    max_cost_tier = runtime_max_cost_tier(runtime_path)
    max_latency_tier = runtime_max_latency_tier(runtime_path)
    primary_unhealthy = not _is_target_healthy(
        model_id=primary_model,
        api_base=primary_api_base,
        api_key=primary_api_key,
    )
    selected_target = ordered_targets[0]
    selected_target_key = _target_key(
        model_id=str(selected_target["model_id"]),
        api_base=selected_target.get("api_base"),
        api_key=selected_target.get("api_key"),
    )
    compliant_targets_present = any(
        target["policy_assessment"]["policy_compliant"]
        for target in ordered_targets
    )

    candidate_targets: list[dict[str, Any]] = []
    for target in ordered_targets:
        assessment = target["policy_assessment"]
        healthy = _is_target_healthy(
            model_id=str(target["model_id"]),
            api_base=target.get("api_base"),
            api_key=target.get("api_key"),
        )
        target_key = _target_key(
            model_id=str(target["model_id"]),
            api_base=target.get("api_base"),
            api_key=target.get("api_key"),
        )
        is_selected = target_key == selected_target_key
        rerouted_primary = bool(rerouted and target["source"] == "primary")
        if is_selected:
            decision = "selected"
        elif rerouted_primary:
            decision = "skipped"
        elif not healthy:
            decision = "deprioritized"
        elif compliant_targets_present and not assessment["policy_compliant"]:
            decision = "skipped"
        else:
            decision = "deferred"

        candidate_targets.append(
            {
                "model_id": str(target["model_id"]),
                "profile": target.get("profile"),
                "source": target["source"],
                "healthy": healthy,
                "decision": decision,
                "matched_policy_intents": _matched_policy_intents(
                    model_id=str(target["model_id"]),
                    profile=target.get("profile"),
                    policy_intents=policy_intents,
                ),
                "required_policy_intents": assessment["required_policy_intents"],
                "matched_required_intents": assessment["matched_required_intents"],
                "missing_required_intents": assessment["missing_required_intents"],
                "cost_tier": assessment["cost_tier"],
                "latency_tier": assessment["latency_tier"],
                "within_cost_guardrail": assessment["within_cost_guardrail"],
                "within_latency_guardrail": assessment["within_latency_guardrail"],
                "policy_compliant": assessment["policy_compliant"],
                "policy_score": _policy_score(
                    model_id=str(target["model_id"]),
                    profile=target.get("profile"),
                    policy_intents=policy_intents,
                    policy_scores=policy_scores,
                ),
                "reason_codes": _candidate_reason_codes(
                    source=target["source"],
                    decision=decision,
                    healthy=healthy,
                    rerouted_primary=rerouted_primary,
                    missing_required_intents=assessment["missing_required_intents"],
                    within_cost_guardrail=assessment["within_cost_guardrail"],
                    within_latency_guardrail=assessment["within_latency_guardrail"],
                    guardrail_fallback_only=not compliant_targets_present and not assessment["policy_compliant"],
                ),
            }
        )

    attempt_order = [
        target["model_id"]
        for target in candidate_targets
        if target["decision"] != "skipped"
    ]

    return {
        "runtime_path": runtime_path,
        "runtime_profile": runtime_profile,
        "policy_intents": policy_intents,
        "required_policy_intents": required_policy_intents,
        "policy_scores": policy_scores,
        "max_cost_tier": max_cost_tier,
        "max_latency_tier": max_latency_tier,
        "primary_model": primary_model,
        "selected_model": str(selected_target["model_id"]),
        "selected_profile": selected_target.get("profile"),
        "selected_source": selected_target["source"],
        "attempt_order": attempt_order,
        "rerouted_from_unhealthy_primary": rerouted and primary_unhealthy,
        "rerouted_from_policy_guardrails": rerouted_due_to_policy,
        "guardrail_compliant_targets_present": compliant_targets_present,
        "candidate_targets": candidate_targets,
        "rejected_targets": [
            target
            for target in candidate_targets
            if target["decision"] != "selected"
        ],
    }


def _register_request(request_id: str) -> None:
    with _runtime_request_lock:
        _runtime_requests[request_id] = False


def _mark_request_timed_out(request_id: str) -> None:
    with _runtime_request_lock:
        if request_id in _runtime_requests:
            _runtime_requests[request_id] = True


def _finish_request(request_id: str) -> None:
    with _runtime_request_lock:
        _runtime_requests.pop(request_id, None)


def _can_log_request(request_id: str | None) -> bool:
    if request_id is None:
        return True
    with _runtime_request_lock:
        timed_out = _runtime_requests.get(request_id)
    if timed_out is None:
        return False
    return not timed_out


def set_current_llm_request_id(request_id: str) -> contextvars.Token[str | None]:
    """Bind an LLM runtime request id to the current context."""
    return _runtime_request_id_var.set(request_id)


def reset_current_llm_request_id(token: contextvars.Token[str | None]) -> None:
    """Restore the previous LLM runtime request id for the current context."""
    _runtime_request_id_var.reset(token)


def _current_llm_request_id() -> str | None:
    return _runtime_request_id_var.get()


async def _log_llm_runtime_event(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
    session_id: str | None = None,
    request_id: str | None = None,
) -> None:
    event_details = dict(details)
    effective_request_id = request_id or _current_llm_request_id()
    effective_session_id = session_id if session_id is not None else get_current_session_id()
    if effective_request_id and "request_id" not in event_details:
        event_details["request_id"] = effective_request_id
    try:
        await audit_repository.log_event(
            session_id=effective_session_id,
            event_type=event_type,
            actor="system",
            tool_name="llm_runtime",
            risk_level="low",
            policy_mode="full",
            summary=summary,
            details=event_details,
        )
    except Exception:
        logger.debug("Failed to record LLM runtime audit event", exc_info=True)


def _log_llm_runtime_event_sync(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
    request_id: str | None = None,
) -> None:
    session_id = get_current_session_id()
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_log_llm_runtime_event(
                event_type=event_type,
                summary=summary,
                details=details,
                session_id=session_id,
                request_id=request_id,
            ))
            return

        loop.create_task(_log_llm_runtime_event(
            event_type=event_type,
            summary=summary,
            details=details,
            session_id=session_id,
            request_id=request_id,
        ))
    except Exception:
        logger.debug("Failed to run LLM runtime audit logger", exc_info=True)


class FallbackLiteLLMModel(BaseLiteLLMModel):
    """LiteLLM model wrapper that retries via the configured fallback model."""

    def __init__(
        self,
        model_id: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        custom_role_conversions: dict[str, str] | None = None,
        flatten_messages_as_text: bool | None = None,
        runtime_profile: str | None = None,
        runtime_path: str | None = None,
        **kwargs,
    ):
        self._runtime_profile = runtime_profile or "default"
        self._runtime_path = runtime_path
        super().__init__(
            model_id=model_id,
            api_base=api_base,
            api_key=api_key,
            custom_role_conversions=custom_role_conversions,
            flatten_messages_as_text=flatten_messages_as_text,
            **kwargs,
        )
        self._fallback_models: tuple[BaseLiteLLMModel, ...] = ()
        self._fallback_model: BaseLiteLLMModel | None = None
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("runtime_path", None)
        if flatten_messages_as_text is not None:
            fallback_kwargs["flatten_messages_as_text"] = flatten_messages_as_text

        fallback_models: list[BaseLiteLLMModel] = []
        for target in _fallback_targets(
            primary_model_id=self.model_id,
            primary_api_base=self.api_base,
            primary_api_key=self.api_key,
            primary_profile=self._runtime_profile,
            runtime_path=self._runtime_path,
        ):
            fallback_model = BaseLiteLLMModel(
                    model_id=str(target["model_id"]),
                    api_base=target["api_base"] or None,
                    api_key=target["api_key"] or None,
                    custom_role_conversions=custom_role_conversions,
                    **fallback_kwargs,
                )
            setattr(fallback_model, "runtime_profile", target.get("profile"))
            setattr(fallback_model, "runtime_source", target.get("source"))
            fallback_models.append(fallback_model)

        self._fallback_models = tuple(fallback_models)
        self._fallback_model = self._fallback_models[0] if self._fallback_models else None

    def generate(
        self,
        messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **kwargs,
    ):
        primary_model = self.model_id
        request_id = _current_llm_request_id()
        primary_target = {
            "model_id": primary_model,
            "api_base": self.api_base,
            "api_key": self.api_key,
            "profile": self._runtime_profile,
            "source": "primary",
        }
        fallback_targets = [
            {
                "model_id": fallback_model.model_id,
                "api_base": fallback_model.api_base,
                "api_key": fallback_model.api_key,
                "model": fallback_model,
                "profile": getattr(fallback_model, "runtime_profile", None),
                "source": getattr(fallback_model, "runtime_source", "fallback_chain"),
            }
            for fallback_model in self._fallback_models
        ]
        ordered_targets = _ordered_candidate_targets(
            primary_target=primary_target,
            fallback_targets=fallback_targets,
            runtime_path="agent_generate",
        )
        primary_unhealthy = not _is_target_healthy(
            model_id=primary_model,
            api_base=self.api_base,
            api_key=self.api_key,
        )
        selected_target = ordered_targets[0]
        rerouted = selected_target["source"] != "primary"
        rerouted_due_to_policy = rerouted and not primary_unhealthy

        if _can_log_request(request_id):
            _log_llm_runtime_event_sync(
                event_type="llm_routing_decision",
                summary=(
                    f"Agent model routing selected "
                    f"{selected_target['model_id']}"
                ),
                details=_build_routing_decision_details(
                    runtime_path="agent_generate",
                    runtime_profile=self._runtime_profile,
                    primary_model=primary_model,
                    primary_api_base=self.api_base,
                    primary_api_key=self.api_key,
                    primary_profile=self._runtime_profile,
                    ordered_targets=ordered_targets,
                    rerouted=rerouted,
                    rerouted_due_to_policy=rerouted_due_to_policy,
                ),
                request_id=request_id,
            )

        if rerouted and _can_log_request(request_id):
            _log_llm_runtime_event_sync(
                event_type="llm_target_rerouted",
                summary=(
                    f"Agent model generate rerouted from {primary_model} "
                    f"to {selected_target['model_id']}"
                ),
                details={
                    "runtime_path": "agent_generate",
                    "primary_model": primary_model,
                    "rerouted_model": selected_target["model_id"],
                    "rerouted_profile": selected_target.get("profile"),
                    "reroute_reason": "policy_guardrails" if rerouted_due_to_policy else "unhealthy_primary",
                    "unhealthy_models": [primary_model] if primary_unhealthy else [],
                    "cooldown_seconds": _target_cooldown_seconds() if primary_unhealthy else 0,
                },
                request_id=request_id,
            )

        primary_attempted = False
        primary_error: Exception | None = None
        attempted_fallback_models: list[str] = []
        fallback_errors: list[dict[str, str]] = []

        last_error: Exception = RuntimeError("No fallback targets available")

        for index, target in enumerate(ordered_targets):
            is_primary = target["source"] == "primary"
            try:
                if is_primary:
                    primary_attempted = True
                    response = super().generate(
                        messages,
                        stop_sequences=stop_sequences,
                        response_format=response_format,
                        tools_to_call_from=tools_to_call_from,
                        **kwargs,
                    )
                    _mark_target_succeeded(
                        model_id=primary_model,
                        api_base=self.api_base,
                        api_key=self.api_key,
                    )
                    if _can_log_request(request_id):
                        details = {
                            "runtime_path": "agent_generate",
                            "primary_model": primary_model,
                            "used_fallback": False,
                        }
                        if attempted_fallback_models:
                            details["attempted_fallback_models"] = attempted_fallback_models
                            details["fallback_attempts"] = len(attempted_fallback_models)
                        _log_llm_runtime_event_sync(
                            event_type="llm_primary_success",
                            summary=f"Primary agent model generate succeeded via {primary_model}",
                            details=details,
                            request_id=request_id,
                        )
                    return response

                fallback_model = target["model"]
                attempted_fallback_models.append(fallback_model.model_id)
                response = fallback_model.generate(
                    messages,
                    stop_sequences=stop_sequences,
                    response_format=response_format,
                    tools_to_call_from=tools_to_call_from,
                    **kwargs,
                )
                _mark_target_succeeded(
                    model_id=fallback_model.model_id,
                    api_base=fallback_model.api_base,
                    api_key=fallback_model.api_key,
                )
                if _can_log_request(request_id):
                    details = {
                        "runtime_path": "agent_generate",
                        "primary_model": primary_model,
                        "fallback_model": fallback_model.model_id,
                        "attempted_fallback_models": attempted_fallback_models,
                        "fallback_attempts": len(attempted_fallback_models),
                        "used_fallback": True,
                        "primary_attempted": primary_attempted,
                    }
                    if primary_error is not None:
                        details["primary_error"] = str(primary_error)
                    if rerouted and primary_unhealthy:
                        details["rerouted_from_unhealthy_primary"] = True
                    if rerouted_due_to_policy:
                        details["rerouted_from_policy_guardrails"] = True
                    _log_llm_runtime_event_sync(
                        event_type="llm_fallback_success",
                        summary=f"Fallback agent model generate succeeded via {fallback_model.model_id}",
                        details=details,
                        request_id=request_id,
                    )
                return response
            except Exception as error:
                last_error = error
                if is_primary:
                    primary_error = error
                    _mark_target_failed(
                        model_id=primary_model,
                        api_base=self.api_base,
                        api_key=self.api_key,
                    )
                else:
                    fallback_model = target["model"]
                    _mark_target_failed(
                        model_id=fallback_model.model_id,
                        api_base=fallback_model.api_base,
                        api_key=fallback_model.api_key,
                    )
                    fallback_errors.append(
                        {
                            "model": fallback_model.model_id,
                            "error": str(error),
                        }
                    )
                if index + 1 < len(ordered_targets):
                    logger.warning(
                        "LLM generate failed for %s, retrying with %s",
                        target["model_id"],
                        ordered_targets[index + 1]["model_id"],
                        exc_info=True,
                    )

        if attempted_fallback_models and _can_log_request(request_id):
            details = {
                "runtime_path": "agent_generate",
                "primary_model": primary_model,
                "fallback_model": attempted_fallback_models[-1],
                "attempted_fallback_models": attempted_fallback_models,
                "fallback_attempts": len(attempted_fallback_models),
                "used_fallback": True,
                "fallback_error": str(last_error),
                "fallback_errors": fallback_errors,
                "primary_attempted": primary_attempted,
            }
            if primary_error is not None:
                details["primary_error"] = str(primary_error)
            if rerouted and primary_unhealthy:
                details["rerouted_from_unhealthy_primary"] = True
            if rerouted_due_to_policy:
                details["rerouted_from_policy_guardrails"] = True
            _log_llm_runtime_event_sync(
                event_type="llm_fallback_failure",
                summary=f"Fallback agent model generate failed via {attempted_fallback_models[-1]}",
                details=details,
                request_id=request_id,
            )
        elif primary_error is not None and _can_log_request(request_id):
            _log_llm_runtime_event_sync(
                event_type="llm_primary_failure",
                summary=f"Primary agent model generate failed via {primary_model}",
                details={
                    "runtime_path": "agent_generate",
                    "primary_model": primary_model,
                    "used_fallback": False,
                    "error": str(primary_error),
                },
                request_id=request_id,
            )
        raise last_error


def completion_with_fallback_sync(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
    request_id: str | None = None,
    runtime_path: str = "completion",
    profile: str | None = None,
):
    """Execute a litellm completion with an optional fallback target."""
    import litellm

    try:
        resolved_profile = resolve_runtime_profile(runtime_path=runtime_path, profile=profile)
        primary_kwargs = build_completion_kwargs(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model_id=model_id,
            runtime_path=runtime_path,
            profile=profile,
        )
        primary_model = _safe_model_name(primary_kwargs)
        primary_target = {
            "model_id": primary_model,
            "api_base": primary_kwargs.get("api_base"),
            "api_key": primary_kwargs.get("api_key"),
            "profile": resolved_profile,
            "source": "primary",
        }
        fallback_targets = _fallback_targets(
            primary_model_id=primary_model,
            primary_api_base=primary_kwargs.get("api_base"),
            primary_api_key=primary_kwargs.get("api_key"),
            primary_profile=resolved_profile,
            runtime_path=runtime_path,
            profile=profile,
        )
        ordered_targets = _ordered_candidate_targets(
            primary_target=primary_target,
            fallback_targets=fallback_targets,
            runtime_path=runtime_path,
        )
        primary_unhealthy = not _is_target_healthy(
            model_id=primary_model,
            api_base=primary_kwargs.get("api_base"),
            api_key=primary_kwargs.get("api_key"),
        )
        selected_target = ordered_targets[0]
        rerouted = selected_target["source"] != "primary"
        rerouted_due_to_policy = rerouted and not primary_unhealthy

        if _can_log_request(request_id):
            _log_llm_runtime_event_sync(
                event_type="llm_routing_decision",
                summary=(
                    f"LLM completion routing selected "
                    f"{selected_target['model_id']}"
                ),
                details=_build_routing_decision_details(
                    runtime_path=runtime_path,
                    runtime_profile=resolved_profile,
                    primary_model=primary_model,
                    primary_api_base=primary_kwargs.get("api_base"),
                    primary_api_key=primary_kwargs.get("api_key"),
                    primary_profile=resolved_profile,
                    ordered_targets=ordered_targets,
                    rerouted=rerouted,
                    rerouted_due_to_policy=rerouted_due_to_policy,
                ),
                request_id=request_id,
            )

        if rerouted and _can_log_request(request_id):
            _log_llm_runtime_event_sync(
                event_type="llm_target_rerouted",
                summary=(
                    f"LLM completion rerouted from {primary_model} "
                    f"to {selected_target['model_id']}"
                ),
                details={
                    "runtime_path": runtime_path,
                    "runtime_profile": resolved_profile,
                    "primary_model": primary_model,
                    "rerouted_model": selected_target["model_id"],
                    "rerouted_profile": selected_target.get("profile"),
                    "reroute_reason": "policy_guardrails" if rerouted_due_to_policy else "unhealthy_primary",
                    "unhealthy_models": [primary_model] if primary_unhealthy else [],
                    "cooldown_seconds": _target_cooldown_seconds() if primary_unhealthy else 0,
                },
                request_id=request_id,
            )

        primary_attempted = False
        primary_error: Exception | None = None
        attempted_fallback_models: list[str] = []
        fallback_errors: list[dict[str, str]] = []

        last_error: Exception = RuntimeError("No fallback targets available")

        for index, target in enumerate(ordered_targets):
            is_primary = target["source"] == "primary"
            try:
                if is_primary:
                    primary_attempted = True
                    response = litellm.completion(**primary_kwargs)
                    _mark_target_succeeded(
                        model_id=primary_model,
                        api_base=primary_kwargs.get("api_base"),
                        api_key=primary_kwargs.get("api_key"),
                    )
                    if _can_log_request(request_id):
                        details = {
                            "runtime_path": runtime_path,
                            "runtime_profile": resolved_profile,
                            "primary_model": primary_model,
                            "used_fallback": False,
                        }
                        if attempted_fallback_models:
                            details["attempted_fallback_models"] = attempted_fallback_models
                            details["fallback_attempts"] = len(attempted_fallback_models)
                        _log_llm_runtime_event_sync(
                            event_type="llm_primary_success",
                            summary=f"Primary LLM completion succeeded via {primary_model}",
                            details=details,
                            request_id=request_id,
                        )
                    return response

                fallback_kwargs = build_completion_kwargs(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    use_fallback=True,
                    fallback_model_id=str(target["model_id"]),
                    fallback_api_key=target["api_key"],
                    fallback_api_base=target["api_base"],
                    runtime_path=runtime_path,
                )
                fallback_model = _safe_model_name(fallback_kwargs)
                attempted_fallback_models.append(fallback_model)
                response = litellm.completion(**fallback_kwargs)
                _mark_target_succeeded(
                    model_id=fallback_model,
                    api_base=target["api_base"],
                    api_key=target["api_key"],
                )
                if _can_log_request(request_id):
                    details = {
                        "runtime_path": runtime_path,
                        "runtime_profile": resolved_profile,
                        "primary_model": primary_model,
                        "fallback_model": fallback_model,
                        "attempted_fallback_models": attempted_fallback_models,
                        "fallback_attempts": len(attempted_fallback_models),
                        "used_fallback": True,
                        "primary_attempted": primary_attempted,
                    }
                    if primary_error is not None:
                        details["primary_error"] = str(primary_error)
                    if rerouted and primary_unhealthy:
                        details["rerouted_from_unhealthy_primary"] = True
                    if rerouted_due_to_policy:
                        details["rerouted_from_policy_guardrails"] = True
                    _log_llm_runtime_event_sync(
                        event_type="llm_fallback_success",
                        summary=f"Fallback LLM completion succeeded via {fallback_model}",
                        details=details,
                        request_id=request_id,
                    )
                return response
            except Exception as error:
                last_error = error
                if is_primary:
                    primary_error = error
                    _mark_target_failed(
                        model_id=primary_model,
                        api_base=primary_kwargs.get("api_base"),
                        api_key=primary_kwargs.get("api_key"),
                    )
                else:
                    fallback_model = str(target["model_id"])
                    _mark_target_failed(
                        model_id=fallback_model,
                        api_base=target["api_base"],
                        api_key=target["api_key"],
                    )
                    fallback_errors.append(
                        {
                            "model": fallback_model,
                            "error": str(error),
                        }
                    )
                if index + 1 < len(ordered_targets):
                    logger.warning(
                        "LLM completion failed for model %s, retrying with %s",
                        target["model_id"],
                        ordered_targets[index + 1]["model_id"],
                        exc_info=True,
                    )

        if attempted_fallback_models and _can_log_request(request_id):
            details = {
                "runtime_path": runtime_path,
                "runtime_profile": resolved_profile,
                "primary_model": primary_model,
                "fallback_model": attempted_fallback_models[-1],
                "attempted_fallback_models": attempted_fallback_models,
                "fallback_attempts": len(attempted_fallback_models),
                "used_fallback": True,
                "fallback_error": str(last_error),
                "fallback_errors": fallback_errors,
                "primary_attempted": primary_attempted,
            }
            if primary_error is not None:
                details["primary_error"] = str(primary_error)
            if rerouted and primary_unhealthy:
                details["rerouted_from_unhealthy_primary"] = True
            if rerouted_due_to_policy:
                details["rerouted_from_policy_guardrails"] = True
            _log_llm_runtime_event_sync(
                event_type="llm_fallback_failure",
                summary=f"Fallback LLM completion failed via {attempted_fallback_models[-1]}",
                details=details,
                request_id=request_id,
            )
        elif primary_error is not None and _can_log_request(request_id):
            _log_llm_runtime_event_sync(
                event_type="llm_primary_failure",
                summary=f"Primary LLM completion failed via {primary_model}",
                details={
                    "runtime_path": runtime_path,
                    "runtime_profile": resolved_profile,
                    "primary_model": primary_model,
                    "used_fallback": False,
                    "error": str(primary_error),
                },
                request_id=request_id,
            )
        raise last_error
    finally:
        if request_id is not None:
            _finish_request(request_id)


async def completion_with_fallback(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: int | None = None,
    model_id: str | None = None,
    runtime_path: str = "completion",
    profile: str | None = None,
):
    """Async wrapper around the shared completion fallback flow."""
    request_id = uuid4().hex
    _register_request(request_id)
    resolved_profile = resolve_runtime_profile(runtime_path=runtime_path, profile=profile)
    coro = asyncio.to_thread(
        completion_with_fallback_sync,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model_id=model_id,
        request_id=request_id,
        runtime_path=runtime_path,
        profile=profile,
    )
    try:
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        _mark_request_timed_out(request_id)
        await _log_llm_runtime_event(
            event_type="llm_timed_out",
            summary=f"LLM completion timed out via {model_id or _profile_model_id(resolved_profile)}",
            details={
                "runtime_path": runtime_path,
                "runtime_profile": resolved_profile,
                "primary_model": model_id or _profile_model_id(resolved_profile),
                "timeout_seconds": timeout,
                "fallback_configured": has_fallback_model(runtime_path=runtime_path),
            },
            request_id=request_id,
        )
        raise
    finally:
        if timeout is None:
            _finish_request(request_id)
