"""Tests for shared LLM runtime configuration and fallback behavior."""

import asyncio
import json
import time
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from smolagents.models import ChatMessage

from config.settings import settings
from src.agent.context_window import _count_tokens
from src.agent.prompt_compaction import local_runtime_prompt_budget
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.repository import audit_repository
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.llm_runtime import (
    FallbackLiteLLMModel,
    NoCompliantModelRouteError,
    ProviderProfileConfigurationError,
    _build_routing_decision_details,
    _feedback_snapshot,
    _governed_openai_chat_completion,
    _profile_from_payload,
    _fallback_targets,
    _mark_target_failed,
    _ordered_candidate_targets,
    _order_targets_by_policy,
    _reset_target_health,
    _strict_local_inference_decision,
    build_completion_kwargs,
    build_model_kwargs,
    completion_with_fallback,
    completion_with_fallback_sync,
    _mark_request_timed_out,
    _register_request,
    _finish_request,
    reset_current_llm_request_id,
    provider_profile_statuses,
    runtime_policy_scores,
    _safe_error,
    set_current_llm_request_id,
)
from src.operators.local_codex import ExternalAgentRuntimeRemovedError


def test_governed_http_transport_does_not_start_after_deadline():
    decision = MagicMock(allowed=True)
    decision.selected = MagicMock(adapter="openai_compatible_chat", endpoint="https://models.example/v1/chat/completions")
    context = MagicMock(deadline_at=99.0)

    with (
        patch("src.llm_runtime.time.time", return_value=100.0),
        patch("httpx.Client") as client,
    ):
        with pytest.raises(TimeoutError, match="model_fabric_deadline_exceeded"):
            _governed_openai_chat_completion(
                decision=decision,
                context=context,
                body={"model": "m", "messages": []},
                api_key=None,
            )

    client.assert_not_called()


def test_governed_http_transport_uses_remaining_deadline_timeout():
    decision = MagicMock(allowed=True)
    decision.selected = MagicMock(adapter="openai_compatible_chat", endpoint="https://models.example/v1/chat/completions")
    context = MagicMock(deadline_at=104.25)
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    client_instance = MagicMock()
    client_instance.__enter__.return_value = client_instance
    client_instance.post.return_value = response

    with (
        patch("src.llm_runtime.time.time", return_value=100.0),
        patch("httpx.Client", return_value=client_instance) as client,
    ):
        result, payload = _governed_openai_chat_completion(
            decision=decision,
            context=context,
            body={"model": "m", "messages": []},
            api_key="secret",
        )

    timeout = client.call_args.kwargs["timeout"]
    assert timeout.read == pytest.approx(4.25)
    assert result.choices[0].message.content == "ok"
    assert payload["usage"]["total_tokens"] == 3
    client_instance.post.assert_called_once_with(
        decision.selected.endpoint,
        headers={"content-type": "application/json", "authorization": "Bearer secret"},
        json={"model": "m", "messages": []},
    )


@pytest.mark.parametrize(
    "api_base",
    [
        "https://user:secret@models.example/v1",
        "https://models.example/v1?token=secret",
        "https://models.example/v1#secret",
        "not-a-url",
    ],
)
def test_legacy_profile_ingestion_rejects_unsafe_api_base(api_base):
    assert _profile_from_payload(
        "unsafe",
        {
            "provider_kind": "openai_compatible",
            "model": "openai/model",
            "api_base": api_base,
            "secret_env": "LLM_API_KEY",
        },
    ) is None


def test_legacy_profile_ingestion_rejects_unsafe_cost_source_before_transport():
    assert _profile_from_payload(
        "unsafe-cost-source",
        {
            "provider_kind": "openai_compatible",
            "model": "exact-model",
            "api_base": "https://models.example/v1",
            "keyless": True,
            "capabilities": ["text"],
            "transport_adapter": "openai_compatible_chat",
            "context_window_tokens": 8192,
            "max_output_tokens": 1024,
            "max_latency_ms": 2000,
            "cost_microusd": 100,
            "cost_source": "https://evil.example/pricing?token=secret",
            "cost_source_updated_at": time.time(),
            "task_classes": ["interactive_chat"],
        },
    ) is None


def test_provider_profile_status_does_not_expose_unsafe_legacy_api_base():
    secret = "userinfo-secret"
    with (
        patch.object(settings, "llm_api_base", f"https://operator:{secret}@models.example/v1?x={secret}"),
        patch.object(settings, "llm_provider_profiles", ""),
    ):
        status = next(item for item in provider_profile_statuses() if item["id"] == "openai-compatible")

    assert status["api_base"] == ""
    assert secret not in json.dumps(status)


@pytest.fixture(autouse=True)
def clear_ambient_runtime_profile_preferences(monkeypatch):
    # This module exercises the historical provider-matrix and LiteLLM
    # compatibility helpers.  Canonical production-route enforcement is
    # covered by the dedicated model-fabric and chat suites; keep these
    # synthetic helper tests off that boundary so their explicit local,
    # fallback, and provider-profile assertions remain meaningful.
    from src.model_fabric import caller_context

    monkeypatch.setattr(caller_context, "is_canonical_inference_route", lambda _path: False)
    with (
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "local_runtime_paths", ""),
        patch.object(settings, "seraph_vlm_api_key", ""),
        patch.object(settings, "local_vlm_api_key", ""),
        # These tests exercise the historical provider-matrix and routing
        # compatibility contract.  Active OpenRouter-only behavior has
        # dedicated focused coverage in test_model_fabric_openrouter_policy.
        patch.object(settings, "openrouter_provider_only", False),
    ):
        yield


@pytest.fixture
def transitional_legacy_completion_path(monkeypatch):
    """Keep named legacy LiteLLM tests off canonical governed routing."""
    from src.model_fabric import caller_context

    monkeypatch.setattr(caller_context, "is_canonical_inference_route", lambda _path: False)
    monkeypatch.setattr(settings, "openrouter_provider_only", False)


@pytest.fixture
def canonical_openrouter_route(monkeypatch):
    """Opt one test into the active canonical boundary despite the legacy fixture."""
    from src.model_fabric import caller_context

    monkeypatch.setattr(
        caller_context,
        "is_canonical_inference_route",
        lambda path: path in {"chat_agent", "mcp_specialist_calendar"},
    )
    monkeypatch.setattr(settings, "openrouter_provider_only", True)


def test_build_model_kwargs_uses_provider_agnostic_settings():
    with (
        patch.object(settings, "default_model", "openai/gpt-4o-mini"),
        patch.object(settings, "llm_api_key", "test-key"),
        patch.object(settings, "llm_api_base", "http://localhost:11434/v1"),
    ):
        kwargs = build_model_kwargs(temperature=0.4, max_tokens=512)

    assert kwargs["model_id"] == "openai/gpt-4o-mini"
    assert kwargs["api_key"] == "test-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"
    assert kwargs["temperature"] == 0.4
    assert kwargs["max_tokens"] == 512


def test_default_profile_does_not_attach_provider_scoped_cloud_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with (
        patch.object(settings, "llm_api_key", ""),
        patch.object(settings, "openrouter_api_key", ""),
        patch.object(settings, "openai_api_key", ""),
        patch.object(settings, "anthropic_api_key", ""),
        patch.object(settings, "runtime_profile_preferences", ""),
    ):
        kwargs = build_model_kwargs(temperature=0.4, max_tokens=512)

    assert "api_key" not in kwargs


def test_build_model_kwargs_uses_named_codex_openai_profile_with_reasoning_effort(monkeypatch):
    from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-key")
    with (
        patch.object(settings, "llm_provider_profiles", ""),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=codex-openai|openrouter"),
        patch.object(settings, "openai_api_key", ""),
        patch.object(settings, "openrouter_api_key", ""),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )

    assert kwargs["model_id"] == "openai/gpt-5.5"
    assert kwargs["runtime_profile"] == "codex-openai"
    assert kwargs["api_key"] == "openai-secret-key"
    assert kwargs["api_base"] == "https://api.openai.com/v1"
    assert kwargs["reasoning_effort"] == "low"
    _, provider, _, _ = get_llm_provider(
        model=kwargs["model_id"],
        api_base=kwargs["api_base"],
    )
    assert provider == "openai"


def test_build_model_kwargs_supports_gpt55_low_alias_without_encoding_reasoning_in_model(monkeypatch):
    from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-key")
    with (
        patch.object(settings, "llm_provider_profiles", ""),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=gpt-5.5-low"),
        patch.object(settings, "openai_api_key", ""),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )

    assert kwargs["model_id"] == "openai/gpt-5.5"
    assert kwargs["runtime_profile"] == "gpt-5.5-low"
    assert kwargs["reasoning_effort"] == "low"
    _, provider, _, _ = get_llm_provider(
        model=kwargs["model_id"],
        api_base=kwargs["api_base"],
    )
    assert provider == "openai"


def test_build_model_kwargs_named_profile_missing_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with (
        patch.object(settings, "llm_provider_profiles", ""),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=gpt-5.5-low"),
        patch.object(settings, "openai_api_key", ""),
    ):
        with pytest.raises(ProviderProfileConfigurationError, match="missing required credential"):
            build_model_kwargs(
                temperature=0.2,
                max_tokens=256,
                runtime_path="chat_agent",
            )


def test_build_model_kwargs_uses_custom_openai_compatible_profile(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "custom-secret-key")
    profile_config = {
        "profiles": {
            "team-router": {
                "provider_kind": "openai_compatible",
                "model": "openai-compatible/team-model",
                "api_base": "https://llm.example.test/v1",
                "env_secret": "LLM_API_KEY",
                "options": {"reasoning_effort": "low"},
                "capabilities": ["reasoning", "tool_use"],
                "cost": "medium",
                "latency": "low",
                "task": "coding",
                "budget": "medium",
                "fallback": ["openai-compatible/team-small"],
                "enabled": True,
                "safety_notes": "team scoped profile",
            }
        }
    }
    with (
        patch.object(settings, "llm_provider_profiles", json.dumps(profile_config)),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=team-router"),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )
        statuses = provider_profile_statuses()

    assert kwargs["model"] == "openai-compatible/team-model"
    assert kwargs["api_key"] == "custom-secret-key"
    assert kwargs["api_base"] == "https://llm.example.test/v1"
    assert kwargs["reasoning_effort"] == "low"
    team_profile = next(item for item in statuses if item["id"] == "team-router")
    assert team_profile["secret_configured"] is True
    assert team_profile["env_secret"] == "LLM_API_KEY"
    assert team_profile["capabilities"] == ["reasoning", "tool_use"]
    assert team_profile["cost"] == "medium"
    assert team_profile["latency"] == "low"
    assert team_profile["task"] == "coding"
    assert team_profile["budget"] == "medium"
    assert team_profile["fallback"] == ["openai-compatible/team-small"]
    assert team_profile["fallback_models"] == ["openai-compatible/team-small"]
    assert team_profile["enabled"] is True
    assert team_profile["safety_notes"] == "team scoped profile"


def test_provider_profile_status_redacts_nested_option_secrets(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "custom-secret-key")
    profile_config = {
        "profiles": {
            "team-router": {
                "provider_kind": "openai_compatible",
                "model": "openai-compatible/team-model",
                "api_base": "https://llm.example.test/v1",
                "env_secret": "LLM_API_KEY",
                "options": {
                    "api_key": "inline-secret",
                    "extra_headers": {"Authorization": "Bearer header-secret"},
                    "trace": "custom-secret-key",
                },
            }
        }
    }
    with patch.object(settings, "llm_provider_profiles", json.dumps(profile_config)):
        statuses = provider_profile_statuses()

    team_profile = next(item for item in statuses if item["id"] == "team-router")
    serialized = json.dumps(team_profile)
    assert "inline-secret" not in serialized
    assert "header-secret" not in serialized
    assert "custom-secret-key" not in serialized
    assert team_profile["options"]["api_key"] == "[redacted]"
    assert team_profile["options"]["extra_headers"]["Authorization"] == "[redacted]"
    assert team_profile["options"]["trace"] == "[redacted]"


def test_built_in_profile_statuses_include_expected_operator_profiles(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with (
        patch.object(settings, "llm_provider_profiles", ""),
        patch.object(settings, "openai_api_key", ""),
        patch.object(settings, "anthropic_api_key", ""),
        patch.object(settings, "llm_api_key", ""),
    ):
        profiles = {item["id"]: item for item in provider_profile_statuses()}

    assert {"openrouter", "openai-compatible", "codex-openai", "claude-anthropic", "gpt-5.5-low"} <= profiles.keys()
    assert profiles["codex-openai"]["model"] == "openai/gpt-5.5"
    assert profiles["codex-openai"]["options"] == {"reasoning_effort": "low"}
    assert profiles["codex-openai"]["missing_secret"] is True
    assert profiles["openai-compatible"]["secret_ref"] == "LLM_API_KEY"
    assert all("api_key" not in profile for profile in profiles.values())


def test_built_in_local_gemma_profiles_resolve_with_runtime_options(monkeypatch):
    with (
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "llm_provider_profiles", ""),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )
        profiles = {item["id"]: item for item in provider_profile_statuses()}

    assert "local-gemma-screenshot-fast" in profiles
    assert "local-gemma-report-thinking" in profiles
    assert "local-gemma-chat-thinking" in profiles
    assert profiles["local-gemma-screenshot-fast"]["task_class"] == "screenshot_image_analysis"
    assert profiles["local-gemma-screenshot-fast"]["options"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert profiles["local-gemma-screenshot-fast"]["context_window_tokens"] == settings.local_runtime_context_window_tokens
    assert profiles["local-gemma-screenshot-fast"]["prompt_budget_tokens"] >= 0
    assert profiles["local-gemma-chat-thinking"]["options"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert profiles["local-gemma-chat-thinking"]["keyless"] is True
    assert kwargs["model"] == "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"
    assert kwargs["api_base"] == "http://127.0.0.1:8000/v1"
    assert kwargs["chat_template_kwargs"] == {"enable_thinking": True}
    assert kwargs["reasoning"] is True
    assert kwargs["metadata"] == {
        "runtime_profile": "chat_thinking",
        "runtime_path": "chat_agent",
        "priority": "interactive",
    }
    assert kwargs["extra_headers"]["X-Seraph-Runtime-Profile"] == "chat_thinking"
    assert kwargs["extra_headers"]["X-Seraph-Runtime-Path"] == "chat_agent"
    assert kwargs["extra_headers"]["X-Seraph-Priority"] == "interactive"
    assert "api_key" not in kwargs


def test_built_in_local_gemma_profile_uses_local_key_from_settings(monkeypatch):
    with (
        patch.object(settings, "local_model", "openai/local"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_llm_api_key", "local-secret"),
        patch.object(settings, "llm_provider_profiles", ""),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )
        profiles = {item["id"]: item for item in provider_profile_statuses()}

    assert kwargs["api_key"] == "local-secret"
    assert profiles["local-gemma-chat-thinking"]["keyless"] is False
    assert profiles["local-gemma-chat-thinking"]["secret_configured"] is True


def test_delegated_orchestrator_can_route_to_local_gemma_chat_profile(monkeypatch):
    with (
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_llm_api_key", "local-secret"),
        patch.object(settings, "llm_provider_profiles", ""),
        patch.object(settings, "runtime_profile_preferences", "orchestrator_agent=local-gemma-chat-thinking"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="orchestrator_agent",
        )

    assert kwargs["runtime_profile"] == "local-gemma-chat-thinking"
    assert kwargs["model_id"] == "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"
    assert kwargs["api_base"] == "http://127.0.0.1:8000/v1"
    assert kwargs["api_key"] == "local-secret"
    assert kwargs["metadata"] == {
        "runtime_profile": "chat_thinking",
        "runtime_path": "chat_agent",
        "priority": "interactive",
    }
    assert kwargs["extra_headers"]["X-Seraph-Runtime-Profile"] == "chat_thinking"
    assert kwargs["extra_headers"]["X-Seraph-Runtime-Path"] == "chat_agent"
    assert kwargs["extra_headers"]["X-Seraph-Priority"] == "interactive"


def test_all_interactive_paths_can_route_to_local_gemma_chat_profile(monkeypatch):
    with (
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_llm_api_key", "local-secret"),
        patch.object(settings, "llm_provider_profiles", ""),
        patch.object(
            settings,
            "runtime_profile_preferences",
            (
                "chat_agent=local-gemma-chat-thinking;"
                "onboarding_agent=local-gemma-chat-thinking;"
                "orchestrator_agent=local-gemma-chat-thinking"
            ),
        ),
    ):
        routed = {
            runtime_path: build_model_kwargs(
                temperature=0.2,
                max_tokens=256,
                runtime_path=runtime_path,
            )
            for runtime_path in ("chat_agent", "onboarding_agent", "orchestrator_agent")
        }

    for kwargs in routed.values():
        assert kwargs["runtime_profile"] == "local-gemma-chat-thinking"
        assert kwargs["model_id"] == "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"
        assert kwargs["api_base"] == "http://127.0.0.1:8000/v1"
        assert kwargs["api_key"] == "local-secret"
        assert kwargs["metadata"]["priority"] == "interactive"
        assert kwargs["extra_headers"]["X-Seraph-Priority"] == "interactive"


def test_built_in_claude_anthropic_profile_resolves_with_litellm(monkeypatch):
    from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-key")
    with (
        patch.object(settings, "llm_provider_profiles", ""),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=claude-anthropic"),
        patch.object(settings, "anthropic_api_key", ""),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )

    assert kwargs["model_id"] == "anthropic/claude-sonnet-4-20250514"
    assert kwargs["runtime_profile"] == "claude-anthropic"
    assert kwargs["api_key"] == "anthropic-secret-key"
    assert "metadata" not in kwargs
    assert "extra_headers" not in kwargs
    resolved_model, provider, _, _ = get_llm_provider(model=kwargs["model_id"])
    assert resolved_model == "claude-sonnet-4-20250514"
    assert provider == "anthropic"


def test_safe_error_redacts_local_and_fallback_keys():
    with (
        patch.object(settings, "local_llm_api_key", "local-secret-key"),
        patch.object(settings, "fallback_llm_api_key", "fallback-secret-key"),
    ):
        error = _safe_error(
            "failed with local-secret-key then fallback-secret-key"
        )

    assert "local-secret-key" not in error
    assert "fallback-secret-key" not in error
    assert error.count("[redacted]") == 2


def test_named_profile_fallback_chain_uses_profile_credentials(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "profile-fallback-secret")
    profile_config = {
        "profiles": {
            "team-router": {
                "provider_kind": "openai_compatible",
                "model": "openai-compatible/team-model",
                "api_base": "https://llm.example.test/v1",
                "secret_env": "LLM_API_KEY",
                "fallback_models": ["team-router-small"],
            },
            "team-router-small": {
                "provider_kind": "openai_compatible",
                "model": "openai-compatible/team-small",
                "api_base": "https://llm.example.test/v1",
                "secret_env": "LLM_API_KEY",
            }
        }
    }
    with (
        patch.object(settings, "llm_provider_profiles", json.dumps(profile_config)),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=team-router"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )
        targets = _fallback_targets(
            primary_model_id=kwargs["model"],
            primary_api_base=kwargs["api_base"],
            primary_api_key=kwargs["api_key"],
            primary_profile="team-router",
            runtime_path="chat_agent",
        )

    assert [target["model_id"] for target in targets] == ["openai-compatible/team-small"]
    assert targets[0]["api_key"] == "profile-fallback-secret"
    assert targets[0]["api_base"] == "https://llm.example.test/v1"
    assert targets[0]["source"] == "profile_fallback_chain"


def test_raw_model_profile_fallback_is_excluded_before_governed_transport(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "profile-fallback-secret")
    profile_config = {
        "profiles": {
            "team-router": {
                "provider_kind": "openai_compatible",
                "model": "model-A",
                "api_base": "https://llm.example.test/v1",
                "secret_env": "LLM_API_KEY",
                "fallback_models": ["model-B"],
            }
        }
    }
    with (
        patch.object(settings, "llm_provider_profiles", json.dumps(profile_config)),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
    ):
        targets = _fallback_targets(
            primary_model_id="model-A",
            primary_api_base="https://llm.example.test/v1",
            primary_api_key="profile-fallback-secret",
            primary_profile="team-router",
            runtime_path="chat_agent",
        )

    assert targets == []


def test_misconfigured_profile_fallback_does_not_block_primary(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "profile-primary-secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile_config = {
        "profiles": {
            "team-router": {
                "provider_kind": "openai_compatible",
                "model": "openai-compatible/team-model",
                "api_base": "https://llm.example.test/v1",
                "secret_env": "LLM_API_KEY",
                "fallback_models": ["broken-fallback"],
            },
            "broken-fallback": {
                "provider_kind": "openai_compatible",
                "model": "openai-compatible/team-small",
                "api_base": "https://llm.example.test/v1",
                "secret_env": "ANTHROPIC_API_KEY",
            },
        }
    }
    with (
        patch.object(settings, "llm_provider_profiles", json.dumps(profile_config)),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
    ):
        targets = _fallback_targets(
            primary_model_id="openai-compatible/team-model",
            primary_api_base="https://llm.example.test/v1",
            primary_api_key="profile-primary-secret",
            primary_profile="team-router",
            runtime_path="chat_agent",
        )

    assert targets == []


def test_local_first_prefers_named_local_ollama_profile():
    with (
        patch.object(settings, "local_model", "ollama/qwen2.5-coder:7b"),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_policy_intents", "chat_agent=local_first"),
        patch.object(settings, "runtime_policy_scores", ""),
        patch.object(settings, "runtime_policy_requirements", ""),
        patch.object(settings, "runtime_max_cost_tier", ""),
        patch.object(settings, "runtime_max_latency_tier", ""),
        patch.object(settings, "runtime_task_class", ""),
        patch.object(settings, "runtime_max_budget_class", ""),
    ):
        ordered = _order_targets_by_policy(
            [
                {
                    "model_id": "openrouter/anthropic/claude-sonnet-4",
                    "api_base": "https://openrouter.ai/api/v1",
                    "api_key": "openrouter-key",
                    "profile": "openrouter",
                    "source": "primary",
                },
                {
                    "model_id": "ollama/qwen2.5-coder:7b",
                    "api_base": "http://localhost:11434/v1",
                    "api_key": "",
                    "profile": "local-ollama",
                    "source": "alternate_profile",
                },
            ],
            runtime_path="chat_agent",
        )

    assert ordered[0]["profile"] == "local-ollama"
    assert ordered[0]["priority_components"]["local_preference_score"] == 1.0


def test_healthy_primary_is_retained_over_named_local_ollama_preference():
    with (
        patch.object(settings, "local_model", "ollama/qwen2.5-coder:7b"),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_policy_intents", "chat_agent=local_first"),
        patch.object(settings, "runtime_policy_scores", ""),
        patch.object(settings, "runtime_policy_requirements", ""),
        patch.object(settings, "runtime_max_cost_tier", ""),
        patch.object(settings, "runtime_max_latency_tier", ""),
        patch.object(settings, "runtime_task_class", ""),
        patch.object(settings, "runtime_max_budget_class", ""),
    ):
        ordered = _ordered_candidate_targets(
            primary_target={
                "model_id": "openrouter/anthropic/claude-sonnet-4",
                "api_base": "https://openrouter.ai/api/v1",
                "api_key": "openrouter-key",
                "profile": "openrouter",
                "source": "primary",
            },
            fallback_targets=[
                {
                    "model_id": "ollama/qwen2.5-coder:7b",
                    "api_base": "http://localhost:11434/v1",
                    "api_key": "",
                    "profile": "local-ollama",
                    "source": "alternate_profile",
                }
            ],
            runtime_path="chat_agent",
        )

    assert ordered[0]["profile"] == "openrouter"
    assert ordered[1]["profile"] == "local-ollama"
    assert ordered[1]["priority_components"]["local_preference_score"] == 1.0


def test_routing_decision_details_redacts_api_keys(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-secret-redaction")
    profile_config = {
        "profiles": {
            "team-router": {
                "provider_kind": "openai_compatible",
                "model": "openai-compatible/team-model",
                "api_base": "https://llm.example.test/v1",
                "secret_env": "LLM_API_KEY",
                "fallback_models": ["openai-compatible/team-small"],
            }
        }
    }
    with (
        patch.object(settings, "llm_provider_profiles", json.dumps(profile_config)),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=team-router"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
    ):
        primary_kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )
        primary_target = {
            "model_id": primary_kwargs["model"],
            "api_base": primary_kwargs["api_base"],
            "api_key": primary_kwargs["api_key"],
            "profile": "team-router",
            "source": "primary",
        }
        fallback_targets = _fallback_targets(
            primary_model_id=primary_kwargs["model"],
            primary_api_base=primary_kwargs["api_base"],
            primary_api_key=primary_kwargs["api_key"],
            primary_profile="team-router",
            runtime_path="chat_agent",
        )
        ordered_targets = _ordered_candidate_targets(
            primary_target=primary_target,
            fallback_targets=fallback_targets,
            runtime_path="chat_agent",
        )
        details = _build_routing_decision_details(
            runtime_path="chat_agent",
            runtime_profile="team-router",
            primary_model=primary_kwargs["model"],
            primary_api_base=primary_kwargs["api_base"],
            primary_api_key=primary_kwargs["api_key"],
            primary_profile="team-router",
            ordered_targets=ordered_targets,
            rerouted=False,
            rerouted_due_to_policy=False,
        )

    serialized = json.dumps(details)
    assert "sk-test-secret-redaction" not in serialized
    assert "route_key" not in serialized
    assert "route_id" in serialized


def test_build_model_kwargs_uses_local_profile_settings():
    with (
        patch.object(settings, "default_model", "openai/gpt-4o-mini"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            profile="local",
        )

    assert kwargs["model_id"] == "ollama/llama3.2"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_model_kwargs_uses_local_profile_for_runtime_path():
    with (
        patch.object(settings, "default_model", "openai/gpt-4o-mini"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "chat_agent"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )

    assert kwargs["model_id"] == "ollama/llama3.2"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_model_kwargs_uses_runtime_model_override():
    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "runtime_model_overrides", "chat_agent=openai/gpt-4.1-mini"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )

    assert kwargs["model_id"] == "openai/gpt-4.1-mini"
    assert kwargs["runtime_profile"] == "default"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


def test_build_model_kwargs_uses_runtime_profile_preferences_for_primary_target():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local|default"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )

    assert kwargs["model_id"] == "ollama/llama3.2"
    assert kwargs["runtime_profile"] == "local"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_model_kwargs_routes_strategist_agent_to_local_profile():
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "llm_api_key", ""),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_key", "not-needed"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(
            settings,
            "runtime_profile_preferences",
            "strategist_agent=local-gemma-strategist-fast",
        ),
    ):
        kwargs = build_model_kwargs(
            temperature=0.4,
            max_tokens=4096,
            runtime_path="strategist_agent",
        )

    assert kwargs["model_id"] == "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"
    assert kwargs["runtime_profile"] == "local-gemma-strategist-fast"
    assert kwargs["api_key"] == "not-needed"
    assert kwargs["api_base"] == "http://127.0.0.1:8000/v1"
    assert kwargs["max_tokens"] == 512


def test_local_runtime_profiles_clamp_output_contracts_for_all_local_paths():
    with (
        patch.object(settings, "local_model", "openai/unsloth/gemma-local"),
        patch.object(settings, "local_llm_api_key", "local-secret"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(
            settings,
            "runtime_profile_preferences",
            (
                "chat_agent=local-gemma-chat-thinking;"
                "onboarding_agent=local-gemma-chat-thinking;"
                "orchestrator_agent=local-gemma-chat-thinking;"
                "end_of_day_goal_report=local-gemma-report-thinking;"
                "screenshot_image_analysis=local-gemma-screenshot-fast"
            ),
        ),
    ):
        interactive = {
            path: build_model_kwargs(
                temperature=0.2,
                max_tokens=8192,
                runtime_path=path,
            )
            for path in ("chat_agent", "onboarding_agent", "orchestrator_agent")
        }
        report = build_completion_kwargs(
            messages=[{"role": "user", "content": "report"}],
            temperature=0.2,
            max_tokens=8192,
            runtime_path="end_of_day_goal_report",
        )
        screenshot = build_completion_kwargs(
            messages=[{"role": "user", "content": "analyze image"}],
            temperature=0.0,
            max_tokens=8192,
            runtime_path="screenshot_image_analysis",
        )

    assert {kwargs["runtime_profile"] for kwargs in interactive.values()} == {
        "local-gemma-chat-thinking"
    }
    assert {kwargs["max_tokens"] for kwargs in interactive.values()} == {settings.model_max_tokens}
    assert report["max_tokens"] == 4096
    assert screenshot["max_tokens"] == 1400


def _oversized_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "\n".join("system context " * 20 for _ in range(700))},
        {"role": "user", "content": "\n".join("older user context " * 20 for _ in range(700))},
        {"role": "assistant", "content": "\n".join("older assistant context " * 20 for _ in range(700))},
        {"role": "user", "content": "Current request must survive compaction."},
    ]


def _formatted_message_tokens(messages: list[dict[str, str]]) -> int:
    return _count_tokens(
        "\n\n".join(f"{message['role'].capitalize()}: {message['content']}" for message in messages)
    )


def test_fallback_litellm_model_compacts_legacy_local_messages_before_generate():
    success_response = ChatMessage.from_dict({"role": "assistant", "content": "compacted response"})
    with (
        patch.object(settings, "local_model", "openai/unsloth/gemma-local"),
        patch.object(settings, "local_llm_api_key", "local-secret"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
        patch.object(settings, "local_runtime_context_window_tokens", 4096),
        patch.object(settings, "local_runtime_prompt_safety_ratio", 1.0),
        patch.object(settings, "local_runtime_tool_reserve_tokens", 512),
        patch.object(settings, "local_runtime_min_section_tokens", 64),
        patch("src.agent.prompt_compaction.log_background_task_event_sync") as mock_receipt,
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch(
            "src.llm_runtime.get_current_trust_principal",
            return_value=TrustPrincipal(
                principal_id="operator-1",
                principal_type=PrincipalType.OPERATOR,
                grants=(AuthorityGrant.MODEL_INFERENCE,),
                session_id="session-1",
            ),
        ),
        patch(
            "src.llm_runtime._governed_preflight_target",
            return_value=(MagicMock(allowed=True), ()),
        ),
        patch("src.llm_runtime._new_route_receipt_session", return_value=None),
        patch("src.llm_runtime.BaseLiteLLMModel.generate", return_value=success_response) as mock_transport,
    ):
        model = FallbackLiteLLMModel(**build_model_kwargs(
            temperature=0.2,
            max_tokens=512,
            runtime_path="chat_agent",
        ))
        result = model.generate(_oversized_messages())

    sent_messages = mock_transport.call_args.args[1]
    assert result.content == "compacted response"
    assert _formatted_message_tokens(sent_messages) <= local_runtime_prompt_budget(
        reserved_output_tokens=512
    )
    assert sent_messages[-1]["content"] == "Current request must survive compaction."
    assert any("compacted for local model context budget" in item["content"] for item in sent_messages)
    mock_receipt.assert_called_once()


def test_completion_with_fallback_sync_compacts_local_runtime_messages_before_litellm(transitional_legacy_completion_path):
    success_response = MagicMock()
    with (
        patch.object(settings, "local_model", "openai/unsloth/gemma-local"),
        patch.object(settings, "local_llm_api_key", "local-secret"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
        patch.object(settings, "local_runtime_context_window_tokens", 4096),
        patch.object(settings, "local_runtime_prompt_safety_ratio", 1.0),
        patch.object(settings, "local_runtime_tool_reserve_tokens", 512),
        patch.object(settings, "local_runtime_min_section_tokens", 64),
        patch("src.agent.prompt_compaction.log_background_task_event_sync") as mock_receipt,
        patch("litellm.completion", return_value=success_response) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=_oversized_messages(),
            temperature=0.2,
            max_tokens=512,
            runtime_path="chat_agent",
        )

    sent_messages = mock_completion.call_args.kwargs["messages"]
    assert result is success_response
    assert _formatted_message_tokens(sent_messages) <= local_runtime_prompt_budget(
        reserved_output_tokens=512
    )
    assert sent_messages[-1]["content"] == "Current request must survive compaction."
    assert any("compacted for local model context budget" in item["content"] for item in sent_messages)
    mock_receipt.assert_called_once()


def test_build_model_kwargs_uses_runtime_profile_preference_glob():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_profile_preferences", "mcp_*=local|default"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="mcp_github_actions",
        )

    assert kwargs["model_id"] == "ollama/llama3.2"
    assert kwargs["runtime_profile"] == "local"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_model_kwargs_uses_local_first_policy_intent():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_policy_intents", "chat_agent=local_first|reasoning"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )

    assert kwargs["model_id"] == "ollama/llama3.2"
    assert kwargs["runtime_profile"] == "local"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_model_kwargs_honors_unscoped_runtime_override_for_local_first_path():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local|default"),
        patch.object(settings, "runtime_model_overrides", "chat_agent=openai/gpt-4.1-mini"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )

    assert kwargs["model_id"] == "openai/gpt-4.1-mini"
    assert kwargs["runtime_profile"] == "local"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_model_kwargs_uses_runtime_model_override_glob():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_profile_preferences", "mcp_*=local|default"),
        patch.object(settings, "runtime_model_overrides", "mcp_*=openai/gpt-4.1-mini"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="mcp_linear",
        )

    assert kwargs["model_id"] == "openai/gpt-4.1-mini"
    assert kwargs["runtime_profile"] == "local"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_model_kwargs_runtime_override_can_force_default_profile_over_local_path():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", "local-key"),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "chat_agent"),
        patch.object(settings, "runtime_model_overrides", "chat_agent=default:openai/gpt-4.1-mini"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
        )

    assert kwargs["model_id"] == "openai/gpt-4.1-mini"
    assert kwargs["runtime_profile"] == "default"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


def test_build_model_kwargs_exact_override_wins_over_runtime_glob():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_profile_preferences", "mcp_*=local|default"),
        patch.object(
            settings,
            "runtime_model_overrides",
            "mcp_*=openai/gpt-4.1-mini,mcp_github_actions=local:ollama/coder",
        ),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="mcp_github_actions",
        )

    assert kwargs["model_id"] == "ollama/coder"
    assert kwargs["runtime_profile"] == "local"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_model_kwargs_explicit_profile_wins_over_runtime_override():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", "local-key"),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_model_overrides", "chat_agent=default:openai/gpt-4.1-mini"),
    ):
        kwargs = build_model_kwargs(
            temperature=0.2,
            max_tokens=256,
            runtime_path="chat_agent",
            profile="local",
        )

    assert kwargs["model_id"] == "ollama/llama3.2"
    assert kwargs["runtime_profile"] == "local"
    assert kwargs["api_key"] == "local-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_completion_with_fallback_sync_prefers_capability_matched_fallback(transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="fast path won"))]

    _reset_target_health()
    try:
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "fallback_model", ""),
            patch.object(settings, "fallback_models", "openai/gpt-4.1-nano,openai/gpt-4o-mini"),
            patch.object(
                settings,
                "provider_capability_overrides",
                (
                    "openrouter/anthropic/claude-sonnet-4=reasoning|tool_use;"
                    "openai/gpt-4.1-nano=cheap;"
                    "openai/gpt-4o-mini=fast|cheap"
                ),
            ),
            patch.object(settings, "runtime_policy_intents", "session_title_generation=fast|cheap"),
            patch(
                "litellm.completion",
                side_effect=[RuntimeError("primary down"), completion_response],
            ) as mock_completion,
        ):
            response = completion_with_fallback_sync(
                messages=[{"role": "user", "content": "pick the fastest fallback"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="session_title_generation",
            )
    finally:
        _reset_target_health()

    attempted_models = [call.kwargs["model"] for call in mock_completion.call_args_list]
    assert attempted_models == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
    ]
    assert response.choices[0].message.content == "fast path won"


def test_fallback_litellm_model_orders_targets_by_capability_policy():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-mini"),
        patch.object(
            settings,
            "provider_capability_overrides",
            (
                "openrouter/anthropic/claude-sonnet-4=reasoning|tool_use;"
                "openai/gpt-4.1-mini=reasoning|tool_use;"
                "openai/gpt-4o-mini=fast|cheap"
            ),
        ),
        patch.object(settings, "runtime_policy_intents", "chat_agent=local_first|reasoning|tool_use"),
    ):
        model = FallbackLiteLLMModel(
            model_id="ollama/llama3.2",
            api_key="primary-key",
            api_base="http://localhost:11434/v1",
            runtime_profile="local",
            runtime_path="chat_agent",
        )

    assert [fallback.model_id for fallback in model._fallback_models] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-mini",
        "openai/gpt-4o-mini",
    ]


def test_fallback_policy_respects_intent_priority_before_config_order():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4.1-nano,openai/gpt-4o-mini"),
        patch.object(
            settings,
            "provider_capability_overrides",
            (
                "openai/gpt-4.1-nano=cheap;"
                "openai/gpt-4o-mini=fast|cheap"
            ),
        ),
        patch.object(settings, "runtime_policy_intents", "session_title_generation=fast|cheap"),
    ):
        targets = _fallback_targets(
            runtime_path="session_title_generation",
            primary_model_id="openrouter/anthropic/claude-sonnet-4",
            primary_api_key="primary-key",
            primary_api_base="https://openrouter.ai/api/v1",
        )

    assert [target["model_id"] for target in targets] == [
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-nano",
    ]


def test_completion_with_fallback_sync_prefers_highest_weighted_policy_score(transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="weighted score won"))]

    _reset_target_health()
    try:
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "fallback_model", ""),
            patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-nano"),
            patch.object(
                settings,
                "provider_capability_overrides",
                (
                    "openrouter/anthropic/claude-sonnet-4=reasoning|tool_use;"
                    "openai/gpt-4o-mini=fast;"
                    "openai/gpt-4.1-nano=cheap|tool_use"
                ),
            ),
            patch.object(settings, "runtime_policy_intents", "session_title_generation=fast|cheap|tool_use"),
            patch.object(
                settings,
                "runtime_policy_scores",
                "session_title_generation=fast:5|cheap:4|tool_use:4",
            ),
            patch(
                "litellm.completion",
                side_effect=[RuntimeError("primary down"), completion_response],
            ) as mock_completion,
        ):
            response = completion_with_fallback_sync(
                messages=[{"role": "user", "content": "pick the highest weighted fallback"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="session_title_generation",
            )
    finally:
        _reset_target_health()

    attempted_models = [call.kwargs["model"] for call in mock_completion.call_args_list]
    assert attempted_models == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-nano",
    ]
    assert response.choices[0].message.content == "weighted score won"


def test_runtime_policy_scores_ignores_non_finite_weights():
    with patch.object(
        settings,
        "runtime_policy_scores",
        "session_title_generation=fast:nan|cheap:inf|tool_use:4|reasoning:-1",
    ):
        scores = runtime_policy_scores("session_title_generation")

    assert scores == {"tool_use": 4.0}


def test_build_completion_kwargs_uses_fallback_settings():
    with (
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=128,
            use_fallback=True,
        )

    assert kwargs["model"] == "ollama/llama3.2"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_completion_kwargs_uses_local_profile_settings():
    with (
        patch.object(settings, "default_model", "openai/gpt-4o-mini"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_consolidation",
            profile="local",
        )

    assert kwargs["model"] == "ollama/llama3.2"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_completion_kwargs_uses_first_model_from_fallback_chain():
    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-mini"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=128,
            use_fallback=True,
        )

    assert kwargs["model"] == "openai/gpt-4o-mini"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


def test_build_completion_kwargs_uses_runtime_fallback_override_chain():
    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-mini"),
        patch.object(
            settings,
            "runtime_fallback_overrides",
            "session_title_generation=openai/gpt-4.1-mini|openai/gpt-4.1-nano",
        ),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=128,
            use_fallback=True,
            runtime_path="session_title_generation",
        )

    assert kwargs["model"] == "openai/gpt-4.1-mini"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


def test_build_completion_kwargs_uses_runtime_fallback_override_glob():
    with (
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(
            settings,
            "runtime_fallback_overrides",
            "mcp_*=openai/gpt-4.1-mini|openai/gpt-4.1-nano",
        ),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=128,
            use_fallback=True,
            runtime_path="mcp_github_actions",
        )

    assert kwargs["model"] == "openai/gpt-4.1-mini"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


def test_build_completion_kwargs_exact_fallback_override_wins_over_runtime_glob():
    with (
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(
            settings,
            "runtime_fallback_overrides",
            (
                "mcp_*=openai/gpt-4.1-mini|openai/gpt-4.1-nano;"
                "mcp_github_actions=openai/gpt-4o-mini|openai/gpt-4.1-mini"
            ),
        ),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=128,
            use_fallback=True,
            runtime_path="mcp_github_actions",
        )

    assert kwargs["model"] == "openai/gpt-4o-mini"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


def test_build_completion_kwargs_uses_runtime_model_override():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(
            settings,
            "runtime_model_overrides",
            "session_title_generation=openai/gpt-4.1-mini",
        ),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )

    assert kwargs["model"] == "openai/gpt-4.1-mini"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


def test_build_completion_kwargs_honors_unscoped_runtime_override_for_local_first_path():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(
            settings,
            "runtime_profile_preferences",
            "session_title_generation=local|default",
        ),
        patch.object(
            settings,
            "runtime_model_overrides",
            "session_title_generation=openai/gpt-4.1-mini",
        ),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )

    assert kwargs["model"] == "openai/gpt-4.1-mini"
    assert kwargs["api_key"] == "primary-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_build_completion_kwargs_explicit_profile_wins_over_runtime_override():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", "local-key"),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(
            settings,
            "runtime_model_overrides",
            "session_title_generation=default:openai/gpt-4.1-mini",
        ),
    ):
        kwargs = build_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
            profile="local",
        )

    assert kwargs["model"] == "ollama/llama3.2"
    assert kwargs["api_key"] == "local-key"
    assert kwargs["api_base"] == "http://localhost:11434/v1"


def test_completion_with_fallback_sync_uses_local_profile_for_runtime_path(async_db, transitional_legacy_completion_path):
    success_response = MagicMock()

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "session_consolidation"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion", return_value=success_response) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
            runtime_path="session_consolidation",
        )

    assert result is success_response
    assert mock_completion.call_args.kwargs["model"] == "ollama/llama3.2"
    assert mock_completion.call_args.kwargs["api_base"] == "http://localhost:11434/v1"

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_primary_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["runtime_path"] == "session_consolidation"
    assert events[0]["details"]["runtime_profile"] == "local"
    assert events[0]["details"]["primary_model"] == "ollama/llama3.2"


def test_completion_with_fallback_sync_rejects_codex_local_without_fallback(async_db, transitional_legacy_completion_path):
    with (
        patch.object(settings, "default_model", "codex-local"),
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "runtime_model_overrides", ""),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion") as mock_completion,
    ):
        with pytest.raises(ExternalAgentRuntimeRemovedError, match="external agent runtimes were removed"):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "Name this session"}],
                temperature=0.3,
                max_tokens=64,
                runtime_path="session_title_generation",
            )

    mock_completion.assert_not_called()


async def test_completion_with_fallback_sync_rejects_codex_local_inside_running_loop(async_db, transitional_legacy_completion_path):
    with (
        patch.object(settings, "default_model", "codex-local"),
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "runtime_model_overrides", ""),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion") as mock_completion,
    ):
        with pytest.raises(ExternalAgentRuntimeRemovedError):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "Name this session"}],
                temperature=0.3,
                max_tokens=64,
                runtime_path="session_title_generation",
            )

    mock_completion.assert_not_called()


def test_fallback_litellm_model_generate_rejects_codex_local(async_db):
    with (
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "runtime_model_overrides", ""),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion") as mock_completion,
    ):
        model = FallbackLiteLLMModel(model_id="codex-local")
        with pytest.raises(ExternalAgentRuntimeRemovedError):
            model.generate(
                [{"role": "user", "content": "Say hello"}],
                stop_sequences=["<stop>"],
            )

    mock_completion.assert_not_called()


def test_completion_with_fallback_sync_uses_runtime_model_override(async_db, transitional_legacy_completion_path):
    success_response = MagicMock()

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(
            settings,
            "runtime_model_overrides",
            "session_title_generation=openai/gpt-4.1-mini",
        ),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion", return_value=success_response) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
            runtime_path="session_title_generation",
        )

    assert result is success_response
    assert mock_completion.call_args.kwargs["model"] == "openai/gpt-4.1-mini"
    assert mock_completion.call_args.kwargs["api_base"] == "https://openrouter.ai/api/v1"

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_primary_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["runtime_path"] == "session_title_generation"
    assert events[0]["details"]["runtime_profile"] == "default"
    assert events[0]["details"]["primary_model"] == "openai/gpt-4.1-mini"


def test_completion_with_fallback_sync_keeps_remote_fallback_base_for_local_runtime_path(transitional_legacy_completion_path):
    fallback_response = MagicMock()

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", "local-key"),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "session_consolidation"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
        patch(
            "litellm.completion",
            side_effect=[RuntimeError("local down"), fallback_response],
        ) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
            runtime_path="session_consolidation",
        )

    assert result is fallback_response
    assert mock_completion.call_args_list[0].kwargs["model"] == "ollama/llama3.2"
    assert mock_completion.call_args_list[0].kwargs["api_key"] == "local-key"
    assert mock_completion.call_args_list[0].kwargs["api_base"] == "http://localhost:11434/v1"
    assert mock_completion.call_args_list[1].kwargs["model"] == "openai/gpt-4o-mini"
    assert mock_completion.call_args_list[1].kwargs["api_key"] == "primary-key"
    assert mock_completion.call_args_list[1].kwargs["api_base"] == "https://openrouter.ai/api/v1"


def test_completion_with_fallback_sync_local_runtime_only_blocks_remote_fallback(async_db, transitional_legacy_completion_path):
    secret_canary = "SERAPH-PRIVATE-CANARY-9c7d2f"
    private_prompt = f"private screenshot summary containing {secret_canary}"
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", "local-key"),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "screenshot_observation_digest"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
        patch("litellm.completion", side_effect=RuntimeError("local down")) as mock_completion,
    ):
        with pytest.raises(RuntimeError, match="local down"):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": private_prompt}],
                temperature=0.2,
                max_tokens=256,
                runtime_path="screenshot_observation_digest",
                local_runtime_only=True,
            )

    assert mock_completion.call_count == 1
    assert mock_completion.call_args.kwargs["model"] == "ollama/llama3.2"
    assert mock_completion.call_args.kwargs["api_key"] == "local-key"
    assert mock_completion.call_args.kwargs["api_base"] == "http://localhost:11434/v1"

    async def _fetch_denials():
        events = await audit_repository.list_events(limit=20)
        return [event for event in events if event["event_type"] == "llm_target_policy_denied"]

    denials = asyncio.run(_fetch_denials())
    assert denials
    assert denials[0]["details"]["trust_schema_version"] == "seraph.trust.v1"
    assert denials[0]["details"]["trust_reason"] == "local_only_egress_blocked"
    assert denials[0]["details"]["destination_class"] == "remote_provider"
    assert set(denials[0]["details"]) == {
        "destination_class",
        "model",
        "runtime_path",
        "runtime_profile",
        "target_source",
        "trust_decision_id",
        "trust_reason",
        "trust_schema_version",
    }
    serialized_denial = json.dumps(denials[0], sort_keys=True)
    assert private_prompt not in serialized_denial
    assert secret_canary not in serialized_denial


def test_completion_with_fallback_sync_local_runtime_only_denies_remote_primary_before_transport(async_db, transitional_legacy_completion_path):
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion") as mock_completion,
    ):
        with pytest.raises(
            ProviderProfileConfigurationError,
            match="strict-local inference denied primary target: local_only_egress_blocked",
        ):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "private operator context"}],
                temperature=0.2,
                max_tokens=256,
                runtime_path="session_consolidation",
                profile="default",
                local_runtime_only=True,
            )

    mock_completion.assert_not_called()


def test_strict_local_inference_decision_rejects_public_endpoint_on_local_profile():
    decision = _strict_local_inference_decision(
        {
            "model_id": "openai-compatible/local-alias",
            "api_base": "https://models.example/v1",
            "profile": "local",
            "source": "primary",
        },
        messages=[{"role": "user", "content": "private context"}],
        runtime_path="session_consolidation",
    )

    assert decision.allowed is False
    assert decision.reason_code == "local_only_egress_blocked"
    assert decision.destination_class == "remote_provider"


@pytest.mark.parametrize(
    "api_base",
    [
        "http://attacker.internal:8000/v1",
        "http://model.local:8000/v1",
        "http://model-server:8000/v1",
        "https://8.8.8.8/v1",
        "http://0.0.0.0:8000/v1",
    ],
)
def test_strict_local_inference_rejects_hostname_suffix_single_label_public_and_unspecified(api_base):
    decision = _strict_local_inference_decision(
        {
            "model_id": "openai-compatible/untrusted-alias",
            "api_base": api_base,
            "profile": "local",
            "source": "primary",
        },
        messages=[{"role": "user", "content": "private context"}],
        runtime_path="session_consolidation",
    )

    assert decision.allowed is False
    assert decision.reason_code == "local_only_egress_blocked"


def test_strict_local_inference_rejects_private_endpoint_without_explicit_local_profile():
    decision = _strict_local_inference_decision(
        {
            "model_id": "openai-compatible/profile-spoof",
            "api_base": "http://192.168.1.26:8000/v1",
            "profile": "default",
            "source": "primary",
        },
        messages=[{"role": "user", "content": "private context"}],
        runtime_path="session_consolidation",
    )

    assert decision.allowed is False
    assert decision.reason_code == "local_only_egress_blocked"


def test_strict_local_inference_rejects_capability_spoofed_openai_compatible_profile_before_transport(transitional_legacy_completion_path):
    profile_config = {
        "profiles": {
            "spoof-local": {
                "provider_kind": "openai_compatible",
                "model": "openai-compatible/attacker",
                "api_base": "http://192.168.1.25:8000/v1",
                "capabilities": ["local"],
                "keyless": True,
            }
        }
    }
    with (
        patch.object(settings, "llm_provider_profiles", json.dumps(profile_config)),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion") as mock_completion,
    ):
        with pytest.raises(
            ProviderProfileConfigurationError,
            match="strict-local inference denied primary target: local_only_egress_blocked",
        ):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "private context"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="session_consolidation",
                profile="spoof-local",
                local_runtime_only=True,
            )

    mock_completion.assert_not_called()


def test_strict_local_inference_accepts_configured_explicit_local_provider_kind():
    profile_config = {
        "profiles": {
            "trusted-local": {
                "provider_kind": "local",
                "model": "openai-compatible/local-model",
                "api_base": "http://192.168.1.26:8000/v1",
                "capabilities": [],
                "keyless": True,
            }
        }
    }
    with patch.object(settings, "llm_provider_profiles", json.dumps(profile_config)):
        decision = _strict_local_inference_decision(
            {
                "model_id": "openai-compatible/local-model",
                "api_base": "http://192.168.1.26:8000/v1",
                "profile": "trusted-local",
                "source": "primary",
            },
            messages=[{"role": "user", "content": "private context"}],
            runtime_path="session_consolidation",
        )

    assert decision.allowed is True


@pytest.mark.parametrize(
    "api_base",
    ["http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://192.168.1.26:8000/v1"],
)
def test_strict_local_inference_accepts_localhost_and_literal_private_ip_with_local_profile(api_base):
    decision = _strict_local_inference_decision(
        {
            "model_id": "openai-compatible/local",
            "api_base": api_base,
            "profile": "local",
            "source": "primary",
        },
        messages=[{"role": "user", "content": "private context"}],
        runtime_path="session_consolidation",
    )

    assert decision.allowed is True


def test_fallback_litellm_model_keeps_remote_fallback_base_for_local_runtime_path():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", "local-key"),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "chat_agent"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
    ):
        model = FallbackLiteLLMModel(**build_model_kwargs(
            temperature=0.3,
            max_tokens=256,
            runtime_path="chat_agent",
        ))

    assert model.model_id == "ollama/llama3.2"
    assert model.api_key == "local-key"
    assert model.api_base == "http://localhost:11434/v1"
    assert model._fallback_model is not None
    assert model._fallback_model.model_id == "openai/gpt-4o-mini"
    assert model._fallback_model.api_key == "primary-key"
    assert model._fallback_model.api_base == "https://openrouter.ai/api/v1"


def test_fallback_litellm_model_runtime_override_can_force_default_profile_over_local_path():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", "local-key"),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "chat_agent"),
        patch.object(settings, "runtime_model_overrides", "chat_agent=default:openai/gpt-4.1-mini"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
    ):
        model = FallbackLiteLLMModel(**build_model_kwargs(
            temperature=0.3,
            max_tokens=256,
            runtime_path="chat_agent",
        ))

    assert model.model_id == "openai/gpt-4.1-mini"
    assert model.api_key == "primary-key"
    assert model.api_base == "https://openrouter.ai/api/v1"
    assert model._runtime_profile == "default"
    assert model._fallback_model is not None
    assert model._fallback_model.model_id == "openai/gpt-4o-mini"
    assert model._fallback_model.api_key == "primary-key"
    assert model._fallback_model.api_base == "https://openrouter.ai/api/v1"


def test_completion_with_fallback_sync_retries_with_fallback():
    primary_error = RuntimeError("primary down")
    fallback_response = MagicMock()

    with patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"), \
         patch.object(settings, "llm_api_key", "primary-key"), \
         patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"), \
         patch.object(settings, "fallback_model", "ollama/llama3.2"), \
         patch.object(settings, "fallback_llm_api_key", ""), \
         patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"), \
         patch("litellm.completion", side_effect=[primary_error, fallback_response]) as mock_completion:
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
        )

    assert result is fallback_response
    assert mock_completion.call_args_list[0].kwargs["model"] == "openrouter/anthropic/claude-sonnet-4"
    assert mock_completion.call_args_list[1].kwargs["model"] == "ollama/llama3.2"
    assert mock_completion.call_args_list[1].kwargs["api_base"] == "http://localhost:11434/v1"


def test_governed_completion_reauthorizes_fallback_after_primary_failure():
    primary_decision = MagicMock(allowed=True)
    fallback_decision = MagicMock(allowed=True)
    receipt_session = MagicMock()
    receipt_session.finalize = AsyncMock(return_value=None)
    request_context = MagicMock(request_id="request-1")
    fallback_response = MagicMock()

    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch("src.model_fabric.bind_final_inference_payload", side_effect=lambda context, _payload: context),
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch(
            "src.llm_runtime._governed_preflight_target",
            side_effect=[
                (primary_decision, ("a" * 64,)),
                (fallback_decision, ("b" * 64,)),
            ],
        ) as preflight,
        patch("src.llm_runtime._new_route_receipt_session", return_value=receipt_session),
        patch(
            "src.llm_runtime._governed_openai_chat_completion",
            side_effect=[RuntimeError("primary down"), (fallback_response, {})],
        ) as transport,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
            request_context=request_context,
        )

    assert result is fallback_response
    assert preflight.call_count == 2
    assert transport.call_count == 2
    receipt_session.attempt_started.assert_any_call(
        primary_decision,
        capability_proof_hashes=("a" * 64,),
    )
    receipt_session.attempt_started.assert_any_call(
        fallback_decision,
        capability_proof_hashes=("b" * 64,),
    )
    receipt_session.attempt_finished.assert_any_call(
        outcome="failed",
        error_code="transport_failed",
        decision=primary_decision,
        degradation_code="transport_failed",
    )
    receipt_session.attempt_finished.assert_any_call(
        outcome="succeeded",
        error_code=None,
        decision=fallback_decision,
        usage=ANY,
        degradation_code="fallback_used",
    )
    receipt_session.finalize.assert_awaited_once_with(
        outcome="succeeded",
        fallback_reason_code="primary_transport_failed",
        degradation_codes=("fallback_used",),
    )


def test_governed_fallback_receipt_distinguishes_primary_preflight_rejection():
    primary_denied = MagicMock(
        allowed=False,
        rejections=(MagicMock(reason_code="proof_missing:text"),),
    )
    fallback_decision = MagicMock(allowed=True)
    receipt_session = MagicMock()
    receipt_session.finalize = AsyncMock(return_value=None)
    request_context = MagicMock(request_id="request-1")
    response = MagicMock()

    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch("src.model_fabric.bind_final_inference_payload", side_effect=lambda context, _payload: context),
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch(
            "src.llm_runtime._profile_options",
            return_value={"reasoning_effort": "low", "model": "must-not-override"},
        ),
        patch(
            "src.llm_runtime._governed_preflight_target",
            side_effect=[(primary_denied, ()), (fallback_decision, ("b" * 64,))],
        ),
        patch("src.llm_runtime._new_route_receipt_session", return_value=receipt_session),
        patch(
            "src.llm_runtime._governed_openai_chat_completion",
            return_value=(response, {}),
        ) as transport,
    ):
        assert completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
            request_context=request_context,
        ) is response

    transport.assert_called_once()
    transported_body = transport.call_args.kwargs["body"]
    assert not transported_body["model"].startswith(("openai/", "openrouter/", "ollama/"))
    receipt_session.attempt_finished.assert_called_once_with(
        outcome="succeeded",
        error_code=None,
        decision=fallback_decision,
        usage=ANY,
        degradation_code="preflight_fallback",
    )
    receipt_session.finalize.assert_awaited_once_with(
        outcome="succeeded",
        fallback_reason_code="primary_preflight_rejected",
        degradation_codes=("preflight_fallback",),
    )


def test_governed_primary_success_finalizes_bound_receipt():
    decision = MagicMock(allowed=True)
    receipt_session = MagicMock()
    receipt_session.finalize = AsyncMock(return_value=None)
    response = MagicMock()
    context = MagicMock(request_id="request-1")

    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch("src.model_fabric.bind_final_inference_payload", side_effect=lambda context, _payload: context),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch(
            "src.llm_runtime._profile_options",
            return_value={"reasoning_effort": "low", "model": "must-not-override"},
        ),
        patch("src.llm_runtime._governed_preflight_target", return_value=(decision, ("a" * 64,))),
        patch("src.llm_runtime._new_route_receipt_session", return_value=receipt_session),
        patch("src.llm_runtime._governed_openai_chat_completion", return_value=(response, {})) as transport,
    ):
        assert completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.2,
            max_tokens=64,
            request_context=context,
        ) is response

    transport.assert_called_once()
    transported_body = transport.call_args.kwargs["body"]
    assert transported_body["reasoning_effort"] == "low"
    assert transported_body["model"] != "must-not-override"
    assert not transported_body["model"].startswith(("openai/", "openrouter/", "ollama/"))
    receipt_session.attempt_finished.assert_called_once_with(
        outcome="succeeded", error_code=None, decision=decision, usage=ANY
    )
    receipt_session.finalize.assert_awaited_once_with(
        outcome="succeeded", fallback_reason_code=None, degradation_codes=()
    )


def test_governed_all_denied_returns_stable_error_and_zero_transport():
    context = MagicMock(request_id="request-1")
    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("src.llm_runtime._governed_preflight_target", return_value=(MagicMock(allowed=False), ())),
        patch("src.llm_runtime._governed_openai_chat_completion") as transport,
        patch(
            "src.model_fabric.persist_denied_route",
            new=AsyncMock(return_value=MagicMock(persisted=True)),
        ) as denied_receipt,
    ):
        with pytest.raises(NoCompliantModelRouteError, match="no_compliant_route"):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.2,
                max_tokens=64,
                request_context=context,
            )

    transport.assert_not_called()
    denied_receipt.assert_awaited_once()


@pytest.mark.parametrize("runtime_path", ["chat_agent", "mcp_specialist_calendar"])
def test_canonical_completion_without_principal_is_zero_legacy_transport(
    runtime_path, canonical_openrouter_route
):
    with (
        patch("src.llm_runtime.get_current_trust_principal", return_value=None),
        patch("litellm.completion") as legacy_transport,
    ):
        with pytest.raises(PermissionError, match="explicit runtime principal"):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.2,
                max_tokens=64,
                runtime_path=runtime_path,
            )

    legacy_transport.assert_not_called()


def test_canonical_completion_builds_context_before_route_selection(canonical_openrouter_route):
    principal = TrustPrincipal(
        principal_id="operator-1",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-1",
    )
    context = MagicMock(request_id="request-1")
    denied = MagicMock(allowed=False, rejections=(MagicMock(reason_code="proof_missing:text"),))
    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch("src.llm_runtime.get_current_trust_principal", return_value=principal),
        patch(
            "src.model_fabric.caller_context.build_canonical_inference_context",
            return_value=context,
        ) as build_context,
        patch("src.model_fabric.bind_final_inference_payload", side_effect=lambda value, _payload: value),
        patch("src.llm_runtime._governed_preflight_target", return_value=(denied, ())),
        patch("src.model_fabric.persist_denied_route", new=AsyncMock(return_value=MagicMock(persisted=True))),
        patch("litellm.completion") as legacy_transport,
    ):
        with pytest.raises(NoCompliantModelRouteError):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.2,
                max_tokens=64,
                runtime_path="chat_agent",
            )

    build_context.assert_called_once()
    legacy_transport.assert_not_called()


def test_governed_agent_primary_success_finalizes_bound_receipt():
    decision = MagicMock(allowed=True)
    receipt_session = MagicMock(workload="interactive")
    receipt_session.finalize = AsyncMock(return_value=None)
    context = MagicMock(request_id="request-1")
    tool_message = ChatMessage.from_dict(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_primary",
                    "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": '{"task":"inspect"}',
                    },
                }
            ],
        }
    )
    response = MagicMock()
    response.choices = [MagicMock(message=tool_message)]
    model = FallbackLiteLLMModel(
        model_id="openrouter/anthropic/claude-sonnet-4",
        api_key="primary-key",
        api_base="https://openrouter.ai/api/v1",
        runtime_profile="default",
        runtime_path="chat_agent",
    )

    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch("src.model_fabric.bind_final_inference_payload", side_effect=lambda context, _payload: context),
        patch("src.llm_runtime._governed_preflight_target", return_value=(decision, ("a" * 64,))),
        patch("src.llm_runtime._new_route_receipt_session", return_value=receipt_session),
        patch("src.llm_runtime._governed_openai_chat_completion", return_value=(response, {})) as transport,
    ):
        result = model.generate(
            [{"role": "user", "content": "hello"}],
            request_context=context,
        )

    assert result.content is None
    assert result.tool_calls[0].id == "call_primary"
    assert result.tool_calls[0].function.name == "delegate_task"

    transport.assert_called_once()
    receipt_session.attempt_finished.assert_called_once_with(
        outcome="succeeded", error_code=None, decision=decision, usage=ANY
    )
    receipt_session.finalize.assert_awaited_once_with(
        outcome="succeeded", fallback_reason_code=None, degradation_codes=()
    )


def test_governed_agent_fallback_preserves_tool_only_response():
    primary_decision = MagicMock(allowed=True)
    fallback_decision = MagicMock(allowed=True)
    context = MagicMock(request_id="request-fallback-tools")
    tool_message = ChatMessage.from_dict(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_fallback",
                    "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": '{"task":"recover"}',
                    },
                }
            ],
        }
    )
    fallback_response = MagicMock()
    fallback_response.choices = [MagicMock(message=tool_message)]

    with (
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_models", ""),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://127.0.0.1:11434/v1"),
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch(
            "src.model_fabric.bind_final_inference_payload",
            side_effect=lambda request_context, _payload: request_context,
        ),
        patch(
            "src.llm_runtime._governed_preflight_target",
            side_effect=[(primary_decision, ()), (fallback_decision, ())],
        ),
        patch("src.llm_runtime._new_route_receipt_session", return_value=None),
        patch(
            "src.llm_runtime._governed_openai_chat_completion",
            side_effect=[RuntimeError("primary down"), (fallback_response, {})],
        ) as transport,
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            runtime_profile="default",
            runtime_path="chat_agent",
        )
        result = model.generate(
            [{"role": "user", "content": "use a tool"}],
            request_context=context,
        )

    assert transport.call_count == 2
    assert result.content is None
    assert result.tool_calls[0].id == "call_fallback"
    assert result.tool_calls[0].function.name == "delegate_task"


def test_governed_agent_all_denied_is_zero_transport():
    context = MagicMock(request_id="request-1")
    model = FallbackLiteLLMModel(
        model_id="openrouter/anthropic/claude-sonnet-4",
        api_key="primary-key",
        api_base="https://openrouter.ai/api/v1",
        runtime_profile="default",
        runtime_path="chat_agent",
    )
    with (
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch("src.llm_runtime._governed_preflight_target", return_value=(MagicMock(allowed=False), ())),
        patch("src.llm_runtime._governed_openai_chat_completion") as transport,
        patch(
            "src.model_fabric.persist_denied_route",
            new=AsyncMock(return_value=MagicMock(persisted=True)),
        ) as denied_receipt,
    ):
        with pytest.raises(NoCompliantModelRouteError, match="no_compliant_route"):
            model.generate(
                [{"role": "user", "content": "hello"}],
                request_context=context,
            )

    transport.assert_not_called()
    denied_receipt.assert_awaited_once()


def test_completion_with_fallback_sync_walks_fallback_chain(async_db):
    completion_response = MagicMock()

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "fallback_model", ""),
        patch.object(
            settings,
            "fallback_models",
            "openai/gpt-4o-mini,openai/gpt-4.1-mini",
        ),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch(
            "litellm.completion",
            side_effect=[
                RuntimeError("primary down"),
                RuntimeError("first fallback down"),
                completion_response,
            ],
        ) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
        )

    assert result is completion_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_fallback_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["fallback_model"] == "openai/gpt-4.1-mini"
    assert events[0]["details"]["attempted_fallback_models"] == [
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]
    assert events[0]["details"]["fallback_attempts"] == 2


def test_completion_with_fallback_sync_uses_runtime_fallback_override_chain(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(
            settings,
            "runtime_fallback_overrides",
            "session_title_generation=openai/gpt-4.1-mini|openai/gpt-4.1-nano",
        ),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch(
            "litellm.completion",
            side_effect=[
                RuntimeError("primary down"),
                RuntimeError("first runtime fallback down"),
                completion_response,
            ],
        ) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
            runtime_path="session_title_generation",
        )

    assert result is completion_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_fallback_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["runtime_path"] == "session_title_generation"
    assert events[0]["details"]["fallback_model"] == "openai/gpt-4.1-nano"
    assert events[0]["details"]["attempted_fallback_models"] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
    ]
    assert events[0]["details"]["fallback_attempts"] == 2


def test_completion_with_fallback_sync_reroutes_away_from_unhealthy_primary(async_db):
    first_fallback_response = MagicMock()
    rerouted_response = MagicMock()

    _reset_target_health()
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "llm_target_cooldown_seconds", 300),
        patch(
            "litellm.completion",
            side_effect=[
                RuntimeError("primary down"),
                first_fallback_response,
                rerouted_response,
            ],
        ) as mock_completion,
    ):
        first_result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
        )
        second_result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello again"}],
            temperature=0.3,
            max_tokens=256,
        )

    assert first_result is first_fallback_response
    assert second_result is rerouted_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4o-mini",
    ]

    async def _fetch():
        return await audit_repository.list_events(limit=20)

    events = asyncio.run(_fetch())
    reroute_events = [e for e in events if e["event_type"] == "llm_target_rerouted"]
    assert reroute_events
    assert reroute_events[0]["details"]["primary_model"] == "openrouter/anthropic/claude-sonnet-4"
    assert reroute_events[0]["details"]["rerouted_model"] == "openai/gpt-4o-mini"

    routing_events = [e for e in events if e["event_type"] == "llm_routing_decision"]
    assert routing_events
    rerouted_details = routing_events[0]["details"]
    assert rerouted_details["runtime_path"] == "completion"
    assert rerouted_details["selected_model"] == "openai/gpt-4o-mini"
    assert rerouted_details["rerouted_from_unhealthy_primary"] is True
    assert rerouted_details["attempt_order"] == ["openai/gpt-4o-mini"]
    primary_candidate = next(
        candidate
        for candidate in rerouted_details["candidate_targets"]
        if candidate["source"] == "primary"
    )
    assert primary_candidate["decision"] == "skipped"
    assert "unhealthy_cooldown" in primary_candidate["reason_codes"]


def test_completion_with_fallback_uses_live_feedback_to_deprioritize_recently_failing_target(async_db, transitional_legacy_completion_path):
    first_success = MagicMock()
    second_success = MagicMock()

    _reset_target_health()
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-nano"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "llm_target_cooldown_seconds", 300),
        patch.object(
            settings,
            "provider_capability_overrides",
            "openai/gpt-4o-mini=fast;openai/gpt-4.1-nano=fast",
        ),
        patch.object(settings, "runtime_policy_intents", "session_title_generation=fast"),
        patch(
            "litellm.completion",
            side_effect=[
                RuntimeError("primary down"),
                RuntimeError("fallback timed out"),
                first_success,
                second_success,
            ],
        ) as mock_completion,
    ):
        first_result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "pick a healthy fast route"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )
        second_result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "use recent provider feedback"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )

    assert first_result is first_success
    assert second_result is second_success
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-nano",
        "openai/gpt-4.1-nano",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=20)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    assert events
    details = events[0]["details"]
    assert details["selected_model"] == "openai/gpt-4.1-nano"
    assert details["attempt_order"] == ["openai/gpt-4.1-nano", "openai/gpt-4o-mini"]
    assert details["selected_production_readiness"] == "ready"
    assert details["rejected_target_summaries"][0]["model_id"] == "openai/gpt-4o-mini"
    assert details["route_explanation"].startswith("selected openai/gpt-4.1-nano")
    unstable_candidate = next(
        candidate
        for candidate in details["candidate_targets"]
        if candidate["model_id"] == "openai/gpt-4o-mini"
    )
    assert unstable_candidate["feedback_state"] in {"cooldown", "recovering", "unstable"}
    assert unstable_candidate["failure_risk_score"] > 0
    assert unstable_candidate["live_feedback"]["last_failure_kind"] == "timeout"


def test_feedback_snapshot_expires_stale_failures_from_live_routing_state(async_db):
    _reset_target_health()

    with patch("src.llm_runtime.monotonic", side_effect=[100.0, 100.0, 100.0 + 901.0]):
        _mark_target_failed(
            model_id="openai/gpt-4o-mini",
            api_base="https://api.openai.test/v1",
            api_key="test-key",
            error=RuntimeError("provider timed out"),
        )
        _mark_target_failed(
            model_id="openai/gpt-4o-mini",
            api_base="https://api.openai.test/v1",
            api_key="test-key",
            error=RuntimeError("provider timed out"),
        )
        snapshot = _feedback_snapshot(
            model_id="openai/gpt-4o-mini",
            api_base="https://api.openai.test/v1",
            api_key="test-key",
        )

    assert snapshot["consecutive_failures"] == 0
    assert snapshot["recent_failure_count"] == 0
    assert snapshot["failure_risk_score"] == 0.0
    assert snapshot["production_readiness"] == "ready"
    assert snapshot["feedback_state"] == "clear"
    assert snapshot["last_failure_kind"] is None
    assert snapshot["last_error"] is None


def test_completion_with_fallback_sync_logs_primary_success(async_db):
    success_response = MagicMock()

    with (
        patch.object(settings, "default_model", "openai/gpt-4o-mini"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "fallback_model", ""),
        patch("litellm.completion", return_value=success_response),
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
        )

    assert result is success_response

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_primary_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["runtime_path"] == "completion"
    assert events[0]["details"]["used_fallback"] is False
    assert events[0]["details"]["primary_model"] == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_completion_with_fallback_logs_session_and_request_context(async_db, transitional_legacy_completion_path):
    success_response = MagicMock()
    runtime_tokens = set_runtime_context("session-123", "high_risk")

    try:
        with (
            patch.object(settings, "default_model", "openai/gpt-4o-mini"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "http://localhost:11434/v1"),
            patch.object(settings, "fallback_model", ""),
            patch.object(settings, "fallback_models", ""),
            patch("litellm.completion", return_value=success_response),
        ):
            result = await completion_with_fallback(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.3,
                max_tokens=256,
                runtime_path="session_title_generation",
            )
    finally:
        reset_runtime_context(runtime_tokens)

    assert result is success_response
    events = await audit_repository.list_events(limit=5, session_id="session-123")
    success_events = [e for e in events if e["event_type"] == "llm_primary_success"]
    assert success_events
    assert success_events[0]["session_id"] == "session-123"
    assert success_events[0]["details"]["runtime_path"] == "session_title_generation"
    assert success_events[0]["details"]["request_id"]


@pytest.mark.asyncio
async def test_completion_with_fallback_sync_logs_inside_running_loop(async_db):
    success_response = MagicMock()

    with (
        patch.object(settings, "default_model", "openai/gpt-4o-mini"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion", return_value=success_response),
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
        )
        await asyncio.sleep(0)

    assert result is success_response
    events = await audit_repository.list_events(limit=5)
    success_events = [e for e in events if e["event_type"] == "llm_primary_success"]
    assert success_events
    assert success_events[0]["details"]["primary_model"] == "openai/gpt-4o-mini"


def test_fallback_litellm_model_logs_session_and_request_context(async_db):
    success_response = MagicMock()
    runtime_tokens = set_runtime_context("session-agent", "high_risk")
    _register_request("req-agent-123")
    request_token = set_current_llm_request_id("req-agent-123")

    try:
        with (
            patch.object(settings, "fallback_model", ""),
            patch.object(settings, "fallback_models", ""),
            patch(
                "src.llm_runtime.BaseLiteLLMModel.generate",
                autospec=True,
                return_value=success_response,
            ),
        ):
            model = FallbackLiteLLMModel(
                model_id="openai/gpt-4o-mini",
                api_key="primary-key",
                api_base="http://localhost:11434/v1",
                temperature=0.3,
                max_tokens=256,
            )
            result = model.generate([{"role": "user", "content": "hello"}])
    finally:
        reset_current_llm_request_id(request_token)
        _finish_request("req-agent-123")
        reset_runtime_context(runtime_tokens)

    assert result is success_response

    async def _fetch():
        events = await audit_repository.list_events(limit=5, session_id="session-agent")
        return [e for e in events if e["event_type"] == "llm_primary_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["session_id"] == "session-agent"
    assert events[0]["details"]["runtime_path"] == "agent_generate"
    assert events[0]["details"]["request_id"] == "req-agent-123"


def test_completion_with_fallback_sync_logs_fallback_success(async_db):
    primary_error = RuntimeError("primary down")
    fallback_response = MagicMock()

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch("litellm.completion", side_effect=[primary_error, fallback_response]),
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            max_tokens=256,
        )

    assert result is fallback_response

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_fallback_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["runtime_path"] == "completion"
    assert events[0]["details"]["used_fallback"] is True
    assert events[0]["details"]["fallback_model"] == "ollama/llama3.2"
    assert "primary down" in events[0]["details"]["primary_error"]


def test_completion_with_fallback_sync_logs_final_chain_failure(async_db):
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "fallback_model", ""),
        patch.object(
            settings,
            "fallback_models",
            "openai/gpt-4o-mini,openai/gpt-4.1-mini",
        ),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch(
            "litellm.completion",
            side_effect=[
                RuntimeError("primary down"),
                RuntimeError("first fallback down"),
                RuntimeError("final fallback down"),
            ],
        ),
    ):
        with pytest.raises(RuntimeError, match="final fallback down"):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.3,
                max_tokens=256,
            )

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_fallback_failure"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["fallback_model"] == "openai/gpt-4.1-mini"
    assert events[0]["details"]["attempted_fallback_models"] == [
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]
    assert events[0]["details"]["fallback_errors"] == [
        {"model": "openai/gpt-4o-mini", "error": "first fallback down"},
        {"model": "openai/gpt-4.1-mini", "error": "final fallback down"},
    ]


def test_fallback_litellm_model_retries_generate_with_fallback(async_db):
    primary_error = RuntimeError("primary down")
    fallback_response = MagicMock()

    with (
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
    ):
        with patch(
            "src.llm_runtime.BaseLiteLLMModel.generate",
            autospec=True,
            side_effect=[primary_error, fallback_response],
        ) as mock_generate:
            model = FallbackLiteLLMModel(
                model_id="openrouter/anthropic/claude-sonnet-4",
                api_key="primary-key",
                api_base="https://openrouter.ai/api/v1",
                temperature=0.3,
                max_tokens=256,
            )
            result = model.generate([{"role": "user", "content": "hello"}])

    assert result is fallback_response
    assert mock_generate.call_count == 2
    assert mock_generate.call_args_list[0].args[0].model_id == "openrouter/anthropic/claude-sonnet-4"
    assert mock_generate.call_args_list[1].args[0].model_id == "ollama/llama3.2"

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_fallback_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["runtime_path"] == "agent_generate"
    assert events[0]["details"]["fallback_model"] == "ollama/llama3.2"
    assert "primary down" in events[0]["details"]["primary_error"]


def test_fallback_litellm_model_walks_fallback_chain(async_db):
    fallback_response = MagicMock()

    with (
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-mini"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
        patch(
            "src.llm_runtime.BaseLiteLLMModel.generate",
            autospec=True,
            side_effect=[
                RuntimeError("primary down"),
                RuntimeError("first fallback down"),
                fallback_response,
            ],
        ) as mock_generate,
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=256,
        )
        result = model.generate([{"role": "user", "content": "hello"}])

    assert result is fallback_response
    assert len(model._fallback_models) == 2
    assert [call.args[0].model_id for call in mock_generate.call_args_list] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_fallback_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["fallback_model"] == "openai/gpt-4.1-mini"
    assert events[0]["details"]["attempted_fallback_models"] == [
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]
    assert events[0]["details"]["fallback_attempts"] == 2


def test_fallback_litellm_model_uses_runtime_fallback_override_chain():
    with (
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(
            settings,
            "runtime_fallback_overrides",
            "chat_agent=openai/gpt-4.1-mini|openai/gpt-4.1-nano",
        ),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
    ):
        model = FallbackLiteLLMModel(
            **build_model_kwargs(
                temperature=0.3,
                max_tokens=256,
                runtime_path="chat_agent",
            )
        )

    assert [fallback.model_id for fallback in model._fallback_models] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
    ]


def test_fallback_litellm_model_uses_runtime_profile_preferences_before_fallback_chain():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local|default"),
        patch.object(settings, "fallback_model", "openai/gpt-4.1-mini"),
        patch.object(settings, "fallback_models", ""),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
    ):
        model = FallbackLiteLLMModel(
            **build_model_kwargs(
                temperature=0.3,
                max_tokens=256,
                runtime_path="chat_agent",
            )
        )

    assert model.model_id == "ollama/llama3.2"
    assert model._runtime_profile == "local"
    assert [fallback.model_id for fallback in model._fallback_models] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-mini",
    ]
    assert getattr(model._fallback_models[0], "runtime_profile") == "default"
    assert getattr(model._fallback_models[1], "runtime_profile") is None


def test_fallback_litellm_model_reroutes_away_from_unhealthy_primary(async_db):
    first_fallback_response = MagicMock()
    rerouted_response = MagicMock()

    _reset_target_health()
    with (
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "llm_target_cooldown_seconds", 300),
        patch(
            "src.llm_runtime.BaseLiteLLMModel.generate",
            autospec=True,
            side_effect=[
                RuntimeError("primary down"),
                first_fallback_response,
                rerouted_response,
            ],
        ) as mock_generate,
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=256,
        )
        first_result = model.generate([{"role": "user", "content": "hello"}])

        rerouted_model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=256,
        )
        second_result = rerouted_model.generate([{"role": "user", "content": "hello again"}])

    assert first_result is first_fallback_response
    assert second_result is rerouted_response
    assert [call.args[0].model_id for call in mock_generate.call_args_list] == [
        "openrouter/anthropic/claude-sonnet-4",
        "ollama/llama3.2",
        "ollama/llama3.2",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_target_rerouted"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["primary_model"] == "openrouter/anthropic/claude-sonnet-4"
    assert events[0]["details"]["rerouted_model"] == "ollama/llama3.2"


def test_completion_with_fallback_prefers_alternate_runtime_profile_before_explicit_fallback(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(
            settings,
            "runtime_profile_preferences",
            "session_consolidation=local|default",
        ),
        patch.object(settings, "fallback_model", "openai/gpt-4.1-mini"),
        patch.object(settings, "fallback_models", ""),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
        patch(
            "litellm.completion",
            side_effect=[RuntimeError("local down"), completion_response],
        ) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "prefer local before remote"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_consolidation",
        )

    assert result is completion_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "ollama/llama3.2",
        "openrouter/anthropic/claude-sonnet-4",
    ]
    assert mock_completion.call_args_list[1].kwargs["api_base"] == "https://openrouter.ai/api/v1"

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_fallback_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["fallback_model"] == "openrouter/anthropic/claude-sonnet-4"
    assert events[0]["details"]["attempted_fallback_models"] == [
        "openrouter/anthropic/claude-sonnet-4",
    ]


def test_fallback_litellm_model_orders_targets_by_weighted_policy_score():
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-mini"),
        patch.object(
            settings,
            "provider_capability_overrides",
            (
                "openrouter/anthropic/claude-sonnet-4=reasoning|tool_use;"
                "openai/gpt-4o-mini=fast;"
                "openai/gpt-4.1-mini=reasoning|tool_use"
            ),
        ),
        patch.object(settings, "runtime_policy_intents", "chat_agent=fast|reasoning|tool_use"),
        patch.object(
            settings,
            "runtime_policy_scores",
            "chat_agent=fast:6|reasoning:4|tool_use:4",
        ),
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            runtime_profile="default",
            runtime_path="chat_agent",
        )

    assert [fallback.model_id for fallback in model._fallback_models] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4o-mini",
    ]


def test_completion_with_fallback_logs_routing_decision(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="fast path won"))]

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4.1-nano,openai/gpt-4o-mini"),
        patch.object(
            settings,
            "provider_capability_overrides",
            "openai/gpt-4.1-nano=cheap;openai/gpt-4o-mini=fast|cheap",
        ),
        patch.object(settings, "runtime_policy_intents", "session_title_generation=fast|cheap"),
        patch(
            "litellm.completion",
            side_effect=[RuntimeError("primary down"), completion_response],
        ),
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "pick the fastest fallback"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )

    assert result is completion_response

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    assert events
    details = events[0]["details"]
    assert details["runtime_path"] == "session_title_generation"
    assert details["selected_model"] == "openrouter/anthropic/claude-sonnet-4"
    assert details["attempt_order"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-nano",
    ]
    assert details["policy_intents"] == ["fast", "cheap"]
    assert details["policy_scores"] == {}
    assert details["candidate_targets"][1]["model_id"] == "openai/gpt-4o-mini"
    assert details["candidate_targets"][1]["matched_policy_intents"] == ["fast", "cheap"]
    assert details["candidate_targets"][1]["policy_score"] == 0.0
    assert details["candidate_targets"][1]["decision"] == "deferred"
    assert details["candidate_targets"][1]["feedback_state"] == "clear"
    assert details["candidate_targets"][1]["failure_risk_score"] == 0.0
    assert details["candidate_targets"][2]["model_id"] == "openai/gpt-4.1-nano"
    assert details["candidate_targets"][2]["matched_policy_intents"] == ["cheap"]
    assert details["candidate_targets"][2]["policy_score"] == 0.0
    assert details["selected_failure_risk_score"] == 0.0
    assert details["selected_production_readiness"] == "ready"
    assert details["selection_policy_mode"] == "retain_primary_until_reroute"
    assert details["planning_winner_model"] == "openai/gpt-4o-mini"
    assert details["planning_winner_selected"] is False
    assert details["best_alternate_model"] == "openai/gpt-4o-mini"
    assert details["selected_vs_best_alternate_margin"] < 0.0
    assert details["route_explanation"].startswith("selected openrouter/anthropic/claude-sonnet-4")
    assert details["route_comparison_summary"].startswith(
        "retained primary openrouter/anthropic/claude-sonnet-4 even though openai/gpt-4o-mini"
    )
    assert len(details["rejected_target_summaries"]) == 2


def test_build_routing_decision_details_disambiguates_same_model_routes():
    with (
        patch.object(settings, "runtime_policy_intents", ""),
        patch.object(settings, "runtime_policy_requirements", ""),
        patch.object(settings, "runtime_policy_scores", ""),
        patch.object(settings, "runtime_max_cost_tier", "high"),
        patch.object(settings, "runtime_max_latency_tier", "slow"),
        patch.object(settings, "runtime_task_class", "analysis"),
        patch.object(settings, "runtime_max_budget_class", "high"),
        patch("src.llm_runtime._is_target_healthy", return_value=True),
    ):
        details = _build_routing_decision_details(
            runtime_path="session_title_generation",
            runtime_profile="default",
            primary_model="openai/gpt-4o-mini",
            primary_api_base="https://api.primary.example/v1",
            primary_api_key="primary-key",
            primary_profile="default",
            ordered_targets=[
                {
                    "model_id": "openai/gpt-4o-mini",
                    "api_base": "https://api.primary.example/v1",
                    "api_key": "primary-key",
                    "profile": "default",
                    "source": "primary",
                    "live_feedback": _feedback_snapshot(
                        model_id="openai/gpt-4o-mini",
                        api_base="https://api.primary.example/v1",
                        api_key="primary-key",
                    ),
                    "policy_assessment": {
                        "required_policy_intents": [],
                        "matched_required_intents": [],
                        "missing_required_intents": [],
                        "cost_tier": "medium",
                        "latency_tier": "medium",
                        "task_class": "analysis",
                        "budget_class": "standard",
                        "within_cost_guardrail": True,
                        "within_latency_guardrail": True,
                        "required_task_class": "analysis",
                        "matched_task_class": True,
                        "max_budget_class": "high",
                        "within_budget_guardrail": True,
                        "policy_compliant": True,
                    },
                    "priority_components": {
                        "local_preference_score": 0.0,
                        "policy_score": 0.0,
                        "capability_priority": (),
                        "capability_gap_count": 0,
                        "capability_gap_penalty": 0.0,
                        "budget_preference_score": 0.0,
                        "preference_score": 0.0,
                        "live_feedback_penalty": 0.0,
                        "health_penalty": 0.0,
                        "guardrail_penalty": 0.0,
                        "compliance_penalty": 0.0,
                        "planning_score": -1.0,
                    },
                },
                {
                    "model_id": "openai/gpt-4o-mini",
                    "api_base": "http://localhost:11434/v1",
                    "api_key": "",
                    "profile": "local",
                    "source": "runtime_profile",
                    "live_feedback": _feedback_snapshot(
                        model_id="openai/gpt-4o-mini",
                        api_base="http://localhost:11434/v1",
                        api_key="",
                    ),
                    "policy_assessment": {
                        "required_policy_intents": [],
                        "matched_required_intents": [],
                        "missing_required_intents": [],
                        "cost_tier": "medium",
                        "latency_tier": "medium",
                        "task_class": "analysis",
                        "budget_class": "standard",
                        "within_cost_guardrail": True,
                        "within_latency_guardrail": True,
                        "required_task_class": "analysis",
                        "matched_task_class": True,
                        "max_budget_class": "high",
                        "within_budget_guardrail": True,
                        "policy_compliant": True,
                    },
                    "priority_components": {
                        "local_preference_score": 1.0,
                        "policy_score": 0.0,
                        "capability_priority": (),
                        "capability_gap_count": 0,
                        "capability_gap_penalty": 0.0,
                        "budget_preference_score": 0.0,
                        "preference_score": 1.0,
                        "live_feedback_penalty": 0.0,
                        "health_penalty": 0.0,
                        "guardrail_penalty": 0.0,
                        "compliance_penalty": 0.0,
                        "planning_score": 1.0,
                    },
                },
            ],
            rerouted=False,
            rerouted_due_to_policy=False,
        )

    assert details["selection_policy_mode"] == "retain_primary_until_reroute"
    assert details["planning_winner_model"] == "openai/gpt-4o-mini"
    assert details["planning_winner_profile"] == "local"
    assert details["planning_winner_source"] == "runtime_profile"
    assert details["planning_winner_selected"] is False
    assert details["best_alternate_model"] == "openai/gpt-4o-mini"
    assert details["best_alternate_profile"] == "local"
    assert details["best_alternate_source"] == "runtime_profile"
    assert "openai/gpt-4o-mini (local/runtime_profile)" in details["route_comparison_summary"]
    assert "openai/gpt-4o-mini (default/primary)" in details["route_comparison_summary"]


def test_build_routing_decision_details_marks_legacy_order_when_planning_winner_differs():
    with (
        patch.object(settings, "runtime_policy_intents", ""),
        patch.object(settings, "runtime_policy_requirements", ""),
        patch.object(settings, "runtime_policy_scores", ""),
        patch.object(settings, "runtime_max_cost_tier", "high"),
        patch.object(settings, "runtime_max_latency_tier", "slow"),
        patch.object(settings, "runtime_task_class", "analysis"),
        patch.object(settings, "runtime_max_budget_class", "high"),
        patch("src.llm_runtime._is_target_healthy", return_value=True),
    ):
        details = _build_routing_decision_details(
            runtime_path="chat_agent",
            runtime_profile="default",
            primary_model="openrouter/anthropic/claude-sonnet-4",
            primary_api_base="https://openrouter.ai/api/v1",
            primary_api_key="primary-key",
            primary_profile="default",
            ordered_targets=[
                {
                    "model_id": "openai/gpt-4.1-nano",
                    "api_base": "https://api.openai.com/v1",
                    "api_key": "fallback-key",
                    "profile": "default",
                    "source": "fallback_chain",
                    "live_feedback": _feedback_snapshot(
                        model_id="openai/gpt-4.1-nano",
                        api_base="https://api.openai.com/v1",
                        api_key="fallback-key",
                    ),
                    "policy_assessment": {
                        "required_policy_intents": [],
                        "matched_required_intents": [],
                        "missing_required_intents": [],
                        "cost_tier": "medium",
                        "latency_tier": "medium",
                        "task_class": "analysis",
                        "budget_class": "standard",
                        "within_cost_guardrail": True,
                        "within_latency_guardrail": True,
                        "required_task_class": "analysis",
                        "matched_task_class": True,
                        "max_budget_class": "high",
                        "within_budget_guardrail": True,
                        "policy_compliant": True,
                    },
                    "priority_components": {
                        "local_preference_score": 0.0,
                        "policy_score": 1.0,
                        "capability_priority": (),
                        "capability_gap_count": 0,
                        "capability_gap_penalty": 0.0,
                        "budget_preference_score": 0.0,
                        "preference_score": 1.0,
                        "live_feedback_penalty": 0.0,
                        "health_penalty": 0.0,
                        "guardrail_penalty": 0.0,
                        "compliance_penalty": 0.0,
                        "planning_score": 1.0,
                    },
                },
                {
                    "model_id": "openai/gpt-4o-mini",
                    "api_base": "https://api.openai.com/v1",
                    "api_key": "standby-key",
                    "profile": "default",
                    "source": "fallback_chain",
                    "live_feedback": _feedback_snapshot(
                        model_id="openai/gpt-4o-mini",
                        api_base="https://api.openai.com/v1",
                        api_key="standby-key",
                    ),
                    "policy_assessment": {
                        "required_policy_intents": [],
                        "matched_required_intents": [],
                        "missing_required_intents": [],
                        "cost_tier": "medium",
                        "latency_tier": "medium",
                        "task_class": "analysis",
                        "budget_class": "standard",
                        "within_cost_guardrail": True,
                        "within_latency_guardrail": True,
                        "required_task_class": "analysis",
                        "matched_task_class": True,
                        "max_budget_class": "high",
                        "within_budget_guardrail": True,
                        "policy_compliant": True,
                    },
                    "priority_components": {
                        "local_preference_score": 0.0,
                        "policy_score": 3.0,
                        "capability_priority": (),
                        "capability_gap_count": 0,
                        "capability_gap_penalty": 0.0,
                        "budget_preference_score": 0.0,
                        "preference_score": 3.0,
                        "live_feedback_penalty": 0.0,
                        "health_penalty": 0.0,
                        "guardrail_penalty": 0.0,
                        "compliance_penalty": 0.0,
                        "planning_score": 3.0,
                    },
                },
            ],
            rerouted=True,
            rerouted_due_to_policy=False,
        )

    assert details["selection_policy_mode"] == "legacy_ordered_attemptable"
    assert details["planning_winner_model"] == "openai/gpt-4o-mini"
    assert details["planning_winner_selected"] is False
    assert details["route_comparison_summary"].startswith(
        "selected openai/gpt-4.1-nano by legacy ordering even though openai/gpt-4o-mini"
    )


def test_completion_with_fallback_logs_weighted_policy_scores(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="weighted policy path"))]

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-nano"),
        patch.object(
            settings,
            "provider_capability_overrides",
            "openai/gpt-4o-mini=fast;openai/gpt-4.1-nano=cheap|tool_use",
        ),
        patch.object(settings, "runtime_policy_intents", "session_title_generation=fast|cheap|tool_use"),
        patch.object(
            settings,
            "runtime_policy_scores",
            "session_title_generation=fast:5|cheap:4|tool_use:4",
        ),
        patch(
            "litellm.completion",
            side_effect=[RuntimeError("primary down"), completion_response],
        ),
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "pick the highest weighted fallback"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )

    assert result is completion_response

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    assert events
    details = events[0]["details"]
    assert details["policy_scores"] == {"fast": 5.0, "cheap": 4.0, "tool_use": 4.0}
    assert details["attempt_order"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-nano",
        "openai/gpt-4o-mini",
    ]
    assert details["candidate_targets"][1]["model_id"] == "openai/gpt-4.1-nano"
    assert details["candidate_targets"][1]["policy_score"] == 8.0
    assert details["candidate_targets"][2]["model_id"] == "openai/gpt-4o-mini"
    assert details["candidate_targets"][2]["policy_score"] == 5.0


def test_completion_with_fallback_reroutes_to_guardrail_compliant_target(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="guardrail-compliant path"))]

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-nano"),
        patch.object(
            settings,
            "provider_capability_overrides",
            (
                "openrouter/anthropic/claude-sonnet-4=reasoning;"
                "openai/gpt-4o-mini=tool_use|fast;"
                "openai/gpt-4.1-nano=cheap"
            ),
        ),
        patch.object(settings, "runtime_policy_intents", "chat_agent=tool_use|fast"),
        patch.object(settings, "runtime_policy_requirements", "chat_agent=tool_use"),
        patch("litellm.completion", return_value=completion_response) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "use a tool-safe provider"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="chat_agent",
        )

    assert result is completion_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openai/gpt-4o-mini",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    details = events[0]["details"]
    assert details["selected_model"] == "openai/gpt-4o-mini"
    assert details["selected_source"] == "fallback_chain"
    assert "configured_fallback_chain" in details["selected_reason_codes"]
    assert "selected_for_attempt" in details["selected_reason_codes"]
    assert details["selected_policy_score"] == 0.0
    assert details["reroute_cause"] == "policy_guardrails"
    assert details["rerouted_from_policy_guardrails"] is True
    assert details["rejected_target_count"] == 2
    primary_candidate = next(
        candidate for candidate in details["candidate_targets"] if candidate["source"] == "primary"
    )
    assert primary_candidate["missing_required_intents"] == ["tool_use"]
    assert "missing_required_intents" in primary_candidate["reason_codes"]
    assert primary_candidate["decision"] == "skipped"


def test_completion_with_fallback_prefers_lower_budget_compliant_route_on_score_tie(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="budget-steered path"))]

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-nano"),
        patch.object(
            settings,
            "provider_capability_overrides",
            (
                "openrouter/anthropic/claude-sonnet-4=reasoning;"
                "openai/gpt-4o-mini=tool_use|fast;"
                "openai/gpt-4.1-nano=tool_use|fast"
            ),
        ),
        patch.object(
            settings,
            "provider_budget_classes",
            (
                "openrouter/anthropic/claude-sonnet-4=high;"
                "openai/gpt-4o-mini=medium;"
                "openai/gpt-4.1-nano=low"
            ),
        ),
        patch.object(settings, "runtime_policy_intents", "session_title_generation=fast"),
        patch.object(settings, "runtime_policy_requirements", "session_title_generation=tool_use"),
        patch.object(settings, "runtime_policy_scores", "session_title_generation=fast:1"),
        patch.object(settings, "runtime_max_budget_class", "session_title_generation=medium"),
        patch("litellm.completion", return_value=completion_response) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "pick the lower-budget compliant route"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )

    assert result is completion_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openai/gpt-4.1-nano",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    assert events
    details = events[0]["details"]
    assert details["selected_model"] == "openai/gpt-4.1-nano"
    assert details["budget_steering_mode"] == "preserve_budget_headroom"
    assert details["selected_budget_headroom"] == 1
    assert details["selected_budget_preference_score"] == 1.0
    assert details["selected_route_score"] == 2.0
    assert details["attempt_order"] == [
        "openai/gpt-4.1-nano",
        "openai/gpt-4o-mini",
    ]
    assert details["planning_winner_model"] == "openai/gpt-4.1-nano"
    assert details["planning_winner_selected"] is True
    assert details["best_alternate_model"] == "openai/gpt-4o-mini"
    assert details["selected_vs_best_alternate_margin"] == 1.0
    assert details["simulated_routes"][0]["entry_model"] == "openai/gpt-4.1-nano"
    assert details["simulated_routes"][0]["selected"] is True
    assert details["simulated_routes"][1]["entry_model"] == "openai/gpt-4o-mini"
    assert details["simulated_routes"][1]["route_score"] == 1.0


def test_completion_with_fallback_keeps_intent_match_ahead_of_budget_steering(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="intent-first path"))]

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-nano"),
        patch.object(
            settings,
            "provider_capability_overrides",
            (
                "openrouter/anthropic/claude-sonnet-4=reasoning;"
                "openai/gpt-4o-mini=fast;"
                "openai/gpt-4.1-nano=tool_use"
            ),
        ),
        patch.object(
            settings,
            "provider_budget_classes",
            (
                "openrouter/anthropic/claude-sonnet-4=high;"
                "openai/gpt-4o-mini=medium;"
                "openai/gpt-4.1-nano=low"
            ),
        ),
        patch.object(settings, "runtime_policy_intents", "session_title_generation=fast"),
        patch.object(settings, "runtime_max_budget_class", "session_title_generation=medium"),
        patch("litellm.completion", return_value=completion_response) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "prefer the fast route, not just the cheapest one"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )

    assert result is completion_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openai/gpt-4o-mini",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    assert events
    details = events[0]["details"]
    assert details["selected_model"] == "openai/gpt-4o-mini"
    assert details["budget_steering_mode"] == "preserve_budget_headroom"
    assert details["attempt_order"] == [
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-nano",
    ]
    assert details["planning_winner_model"] == "openai/gpt-4o-mini"
    assert details["planning_winner_selected"] is True
    assert details["best_alternate_model"] == "openai/gpt-4.1-nano"
    assert details["simulated_routes"][0]["entry_model"] == "openai/gpt-4o-mini"
    assert details["simulated_routes"][0]["selected"] is True
    assert details["simulated_routes"][1]["entry_model"] == "openai/gpt-4.1-nano"
    assert details["simulated_routes"][1]["selected"] is False


def test_completion_with_fallback_fails_closed_when_no_guardrail_compliant_target(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="degrade-open path"))]

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(settings, "provider_capability_overrides", "openai/gpt-4o-mini=fast"),
        patch.object(settings, "runtime_policy_intents", "session_title_generation=tool_use"),
        patch.object(settings, "runtime_policy_requirements", "session_title_generation=tool_use"),
        patch("litellm.completion", return_value=completion_response) as mock_completion,
    ):
        with pytest.raises(RuntimeError, match="No fallback targets available"):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "fail closed when nothing is compliant"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="session_title_generation",
            )

    mock_completion.assert_not_called()

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    details = events[0]["details"]
    assert details["guardrail_compliant_targets_present"] is False
    assert details["rerouted_from_policy_guardrails"] is False
    assert "no_guardrail_compliant_targets" in details["candidate_targets"][0]["reason_codes"]


def test_completion_with_fallback_applies_cost_and_latency_guardrails(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="guardrail tier path"))]

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(settings, "provider_cost_tiers", "openrouter/anthropic/claude-sonnet-4=high;openai/gpt-4o-mini=low"),
        patch.object(settings, "provider_latency_tiers", "openrouter/anthropic/claude-sonnet-4=high;openai/gpt-4o-mini=low"),
        patch.object(settings, "runtime_max_cost_tier", "session_title_generation=medium"),
        patch.object(settings, "runtime_max_latency_tier", "session_title_generation=medium"),
        patch("litellm.completion", return_value=completion_response) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "stay under the guardrails"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )

    assert result is completion_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openai/gpt-4o-mini",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    details = events[0]["details"]
    assert details["selected_model"] == "openai/gpt-4o-mini"
    assert details["max_cost_tier"] == "medium"
    assert details["max_latency_tier"] == "medium"
    primary_candidate = next(
        candidate for candidate in details["candidate_targets"] if candidate["source"] == "primary"
    )
    assert primary_candidate["within_cost_guardrail"] is False
    assert primary_candidate["within_latency_guardrail"] is False
    assert "cost_guardrail_exceeded" in primary_candidate["reason_codes"]
    assert "latency_guardrail_exceeded" in primary_candidate["reason_codes"]


def test_completion_with_fallback_applies_task_and_budget_guardrails(async_db, transitional_legacy_completion_path):
    completion_response = MagicMock()
    completion_response.choices = [MagicMock(message=MagicMock(content="task-safe provider"))]

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
        patch.object(settings, "provider_task_classes", "openrouter/anthropic/claude-sonnet-4=reasoning;openai/gpt-4o-mini=tool_execution"),
        patch.object(settings, "provider_budget_classes", "openrouter/anthropic/claude-sonnet-4=high;openai/gpt-4o-mini=low"),
        patch.object(settings, "runtime_task_class", "chat_agent=tool_execution"),
        patch.object(settings, "runtime_max_budget_class", "chat_agent=medium"),
        patch("litellm.completion", return_value=completion_response) as mock_completion,
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "pick the safe tool execution provider"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="chat_agent",
        )

    assert result is completion_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openai/gpt-4o-mini",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    details = events[0]["details"]
    assert details["selected_model"] == "openai/gpt-4o-mini"
    assert details["required_task_class"] == "tool_execution"
    assert details["max_budget_class"] == "medium"
    primary_candidate = next(
        candidate for candidate in details["candidate_targets"] if candidate["source"] == "primary"
    )
    assert primary_candidate["matched_task_class"] is False
    assert primary_candidate["within_budget_guardrail"] is False
    assert "task_class_mismatch" in primary_candidate["reason_codes"]
    assert "budget_guardrail_exceeded" in primary_candidate["reason_codes"]


def test_completion_with_fallback_does_not_try_noncompliant_target_after_guardrail_skip(async_db, transitional_legacy_completion_path):
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-nano"),
        patch.object(
            settings,
            "provider_capability_overrides",
            (
                "openrouter/anthropic/claude-sonnet-4=reasoning;"
                "openai/gpt-4o-mini=tool_use|fast;"
                "openai/gpt-4.1-nano=cheap"
            ),
        ),
        patch.object(settings, "runtime_policy_intents", "chat_agent=tool_use|fast"),
        patch.object(settings, "runtime_policy_requirements", "chat_agent=tool_use"),
        patch(
            "litellm.completion",
            side_effect=[RuntimeError("compliant fallback down"), MagicMock()],
        ) as mock_completion,
    ):
        with pytest.raises(RuntimeError, match="compliant fallback down"):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "stay within guardrails"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="chat_agent",
            )

    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openai/gpt-4o-mini",
    ]


def test_fallback_litellm_model_does_not_try_noncompliant_target_after_guardrail_skip(async_db):
    with (
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-nano"),
        patch.object(
            settings,
            "provider_capability_overrides",
            (
                "openrouter/anthropic/claude-sonnet-4=reasoning;"
                "openai/gpt-4o-mini=tool_use|fast;"
                "openai/gpt-4.1-nano=cheap"
            ),
        ),
        patch.object(settings, "runtime_policy_intents", "agent_generate=tool_use|fast"),
        patch.object(settings, "runtime_policy_requirements", "agent_generate=tool_use"),
        patch(
            "src.llm_runtime.get_current_trust_principal",
            return_value=TrustPrincipal(
                principal_id="operator-1",
                principal_type=PrincipalType.OPERATOR,
                grants=(AuthorityGrant.MODEL_INFERENCE,),
                session_id="session-1",
            ),
        ),
        patch(
            "src.llm_runtime._governed_preflight_target",
            return_value=(MagicMock(allowed=True), ()),
        ),
        patch("src.llm_runtime._new_route_receipt_session", return_value=None),
        patch(
            "src.llm_runtime.BaseLiteLLMModel.generate",
            autospec=True,
            side_effect=AssertionError("primary target should be skipped"),
        ),
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            runtime_profile="default",
            runtime_path="agent_generate",
        )
        compliant_fallback = model._fallback_models[0]
        noncompliant_fallback = model._fallback_models[1]

        with (
            patch.object(
                compliant_fallback,
                "generate",
                side_effect=RuntimeError("compliant fallback down"),
            ) as mock_compliant_generate,
            patch.object(
                noncompliant_fallback,
                "generate",
                return_value=MagicMock(),
            ) as mock_noncompliant_generate,
        ):
            with pytest.raises(RuntimeError, match="compliant fallback down"):
                model.generate([{"role": "user", "content": "stay within guardrails"}])

        assert mock_compliant_generate.call_count == 1
        mock_noncompliant_generate.assert_not_called()


def test_fallback_litellm_model_skips_duplicate_fallback_target():
    with (
        patch.object(settings, "fallback_model", "openai/gpt-4o-mini"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
    ):
        model = FallbackLiteLLMModel(
            model_id="openai/gpt-4o-mini",
            api_key="primary-key",
            api_base="http://localhost:11434/v1",
            temperature=0.3,
            max_tokens=256,
        )

    assert model._fallback_model is None


def test_fallback_litellm_model_deduplicates_chain_targets():
    with (
        patch.object(settings, "fallback_model", "openai/gpt-4o-mini"),
        patch.object(
            settings,
            "fallback_models",
            "openai/gpt-4o-mini,openai/gpt-4o-mini,openai/gpt-4.1-mini",
        ),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=256,
        )

    assert [candidate.model_id for candidate in model._fallback_models] == [
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]


@pytest.mark.asyncio
async def test_completion_with_fallback_timeout_does_not_log_late_success(async_db):
    success_response = MagicMock()

    def _slow_success(**_kwargs):
        time.sleep(0.1)
        return success_response

    with (
        patch.object(settings, "default_model", "openai/gpt-4o-mini"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "fallback_model", ""),
        patch("litellm.completion", side_effect=_slow_success),
    ):
        with pytest.raises(asyncio.TimeoutError):
            await completion_with_fallback(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.3,
                max_tokens=256,
                timeout=0.01,
            )
        await asyncio.sleep(0.15)

    events = await audit_repository.list_events(limit=10)
    success_events = [e for e in events if e["event_type"] == "llm_primary_success"]
    timeout_events = [e for e in events if e["event_type"] == "llm_timed_out"]
    assert success_events == []
    assert timeout_events
    assert timeout_events[0]["details"]["runtime_path"] == "completion"
    assert timeout_events[0]["details"]["timeout_seconds"] == 0.01


def test_fallback_litellm_model_logs_primary_success(async_db):
    success_response = MagicMock()

    with (
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch(
            "src.llm_runtime.BaseLiteLLMModel.generate",
            autospec=True,
            return_value=success_response,
        ),
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=256,
        )
        result = model.generate([{"role": "user", "content": "hello"}])

    assert result is success_response

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_primary_success"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["runtime_path"] == "agent_generate"
    assert events[0]["details"]["used_fallback"] is False


def test_fallback_litellm_model_logs_primary_failure_without_fallback(async_db):
    primary_error = RuntimeError("primary down")

    with (
        patch.object(settings, "fallback_model", ""),
        patch(
            "src.llm_runtime.BaseLiteLLMModel.generate",
            autospec=True,
            side_effect=primary_error,
        ),
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=256,
        )
        with pytest.raises(RuntimeError, match="primary down"):
            model.generate([{"role": "user", "content": "hello"}])

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_primary_failure"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["runtime_path"] == "agent_generate"
    assert events[0]["details"]["used_fallback"] is False
    assert events[0]["details"]["error"] == "primary down"


def test_fallback_litellm_model_logs_fallback_failure(async_db):
    with (
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch(
            "src.llm_runtime.BaseLiteLLMModel.generate",
            autospec=True,
            side_effect=[RuntimeError("primary down"), RuntimeError("fallback down")],
        ),
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=256,
        )
        with pytest.raises(RuntimeError, match="fallback down"):
            model.generate([{"role": "user", "content": "hello"}])

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_fallback_failure"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["runtime_path"] == "agent_generate"
    assert events[0]["details"]["fallback_model"] == "ollama/llama3.2"
    assert events[0]["details"]["fallback_error"] == "fallback down"


def test_fallback_litellm_model_logs_final_chain_failure(async_db):
    with (
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-mini"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", ""),
        patch(
            "src.llm_runtime.BaseLiteLLMModel.generate",
            autospec=True,
            side_effect=[
                RuntimeError("primary down"),
                RuntimeError("first fallback down"),
                RuntimeError("final fallback down"),
            ],
        ),
    ):
        model = FallbackLiteLLMModel(
            model_id="openrouter/anthropic/claude-sonnet-4",
            api_key="primary-key",
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=256,
        )
        with pytest.raises(RuntimeError, match="final fallback down"):
            model.generate([{"role": "user", "content": "hello"}])

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "llm_fallback_failure"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["fallback_model"] == "openai/gpt-4.1-mini"
    assert events[0]["details"]["attempted_fallback_models"] == [
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]
    assert events[0]["details"]["fallback_errors"] == [
        {"model": "openai/gpt-4o-mini", "error": "first fallback down"},
        {"model": "openai/gpt-4.1-mini", "error": "final fallback down"},
    ]


def test_fallback_litellm_model_skips_late_success_after_timeout(async_db):
    success_response = MagicMock()
    request_id = "agent-timeout-1"
    _register_request(request_id)
    token = set_current_llm_request_id(request_id)

    try:
        _mark_request_timed_out(request_id)
        with (
            patch.object(settings, "fallback_model", "ollama/llama3.2"),
            patch.object(settings, "fallback_llm_api_key", ""),
            patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
            patch(
                "src.llm_runtime.BaseLiteLLMModel.generate",
                autospec=True,
                return_value=success_response,
            ),
        ):
            model = FallbackLiteLLMModel(
                model_id="openrouter/anthropic/claude-sonnet-4",
                api_key="primary-key",
                api_base="https://openrouter.ai/api/v1",
                temperature=0.3,
                max_tokens=256,
            )
            result = model.generate([{"role": "user", "content": "hello"}])
    finally:
        reset_current_llm_request_id(token)
        _finish_request(request_id)

    assert result is success_response

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_primary_success"]

    events = asyncio.run(_fetch())
    assert events == []
