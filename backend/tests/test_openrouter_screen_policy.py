"""Focused policy proof for screen-derived OpenRouter-only reports."""

from unittest.mock import AsyncMock

import pytest

from config.settings import settings
from src.model_fabric.configuration import WorkloadPolicy
from src.scheduler import screen_llm_policy as module
from src.security.trust_contract import EgressClass


def _cloud_policy() -> WorkloadPolicy:
    return WorkloadPolicy(
        runtime_path="screenshot_observation_digest",
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        allowed_provider_kinds=("openrouter",),
        fallback_allowed=False,
        max_cost_microusd=100,
    )


@pytest.mark.asyncio
async def test_digest_gate_is_independent_of_end_of_day_report_flag(monkeypatch):
    monkeypatch.setattr(settings, "screenshot_observation_digest_enabled", True)
    monkeypatch.setattr(settings, "end_of_day_report_llm_enabled", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(settings, "openrouter_allowed_upstreams", "anthropic")
    monkeypatch.setattr(module, "resolve_runtime_profile", lambda **_kwargs: "openrouter")
    monkeypatch.setattr(module, "effective_workload_policy", lambda _path: _cloud_policy())
    monkeypatch.setattr(
        module,
        "_openrouter_profile_ready",
        AsyncMock(return_value=(True, "model_fabric_profile_and_proofs_ready")),
    )

    decision = await module.screen_derived_llm_decision("screenshot_observation_digest")

    assert decision.allowed is True
    assert decision.reason == "model_fabric_profile_and_proofs_ready"


@pytest.mark.asyncio
async def test_legacy_provider_flag_cannot_reactivate_local_screen_route(monkeypatch):
    monkeypatch.setattr(settings, "screenshot_observation_digest_enabled", True)
    monkeypatch.setattr(settings, "openrouter_provider_only", False)
    monkeypatch.setattr(module, "resolve_runtime_profile", lambda **_kwargs: "local-gemma-report-thinking")
    ready = AsyncMock(return_value=(True, "unexpected"))
    monkeypatch.setattr(module, "_openrouter_profile_ready", ready)

    decision = await module.screen_derived_llm_decision("screenshot_observation_digest")

    assert decision.allowed is False
    assert decision.reason == "openrouter_profile_required"
    ready.assert_not_awaited()
