"""Trust-boundary checks for LLM calls over screen-derived records."""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import settings
from src.llm_runtime import is_local_runtime_profile, resolve_runtime_profile
from src.local_runtime_profile_verifier import latest_local_runtime_profile_proof


_VERIFIED_SCREEN_DERIVED_PROFILES = {
    "end_of_day_goal_report": "local-gemma-report-thinking",
    "screenshot_observation_digest": "local-gemma-report-thinking",
}


@dataclass(frozen=True)
class ScreenDerivedLlmDecision:
    allowed: bool
    reason: str
    runtime_path: str
    runtime_profile: str
    proof_status: dict | None = None


def screen_derived_llm_decision(runtime_path: str) -> ScreenDerivedLlmDecision:
    """Return whether a screen-derived LLM prompt may be sent for this runtime path."""
    if not settings.end_of_day_report_llm_enabled:
        return ScreenDerivedLlmDecision(
            allowed=False,
            reason="llm_disabled",
            runtime_path=runtime_path,
            runtime_profile="",
        )

    runtime_profile = resolve_runtime_profile(runtime_path=runtime_path)
    if settings.screen_derived_llm_allow_remote:
        return ScreenDerivedLlmDecision(
            allowed=True,
            reason="remote_explicitly_allowed",
            runtime_path=runtime_path,
            runtime_profile=runtime_profile,
        )

    if not is_local_runtime_profile(runtime_profile):
        return ScreenDerivedLlmDecision(
            allowed=False,
            reason="local_profile_required",
            runtime_path=runtime_path,
            runtime_profile=runtime_profile,
        )

    required_profile = _VERIFIED_SCREEN_DERIVED_PROFILES.get(runtime_path)
    if required_profile and runtime_profile != required_profile:
        return ScreenDerivedLlmDecision(
            allowed=False,
            reason="verified_profile_required",
            runtime_path=runtime_path,
            runtime_profile=runtime_profile,
        )

    if not settings.screen_derived_llm_require_profile_proof:
        return ScreenDerivedLlmDecision(
            allowed=True,
            reason="local_profile_without_required_proof",
            runtime_path=runtime_path,
            runtime_profile=runtime_profile,
        )

    proof_status = latest_local_runtime_profile_proof(
        expected_base_url=settings.local_llm_api_base or settings.local_vlm_base_url,
        expected_model=settings.local_model or settings.local_vlm_model or settings.default_model,
    )
    if proof_status.get("safe_for_single_backend_profile_routing") is True:
        return ScreenDerivedLlmDecision(
            allowed=True,
            reason="local_profile_proof_safe",
            runtime_path=runtime_path,
            runtime_profile=runtime_profile,
            proof_status=proof_status,
        )

    return ScreenDerivedLlmDecision(
        allowed=False,
        reason="local_profile_proof_missing_or_unsafe",
        runtime_path=runtime_path,
        runtime_profile=runtime_profile,
        proof_status=proof_status,
    )
