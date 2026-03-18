"""Tests for shared LLM runtime configuration and fallback behavior."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.repository import audit_repository
from src.llm_runtime import (
    FallbackLiteLLMModel,
    _fallback_targets,
    _reset_target_health,
    build_completion_kwargs,
    build_model_kwargs,
    completion_with_fallback,
    completion_with_fallback_sync,
    _mark_request_timed_out,
    _register_request,
    _finish_request,
    reset_current_llm_request_id,
    runtime_policy_scores,
    set_current_llm_request_id,
)


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


def test_completion_with_fallback_sync_prefers_capability_matched_fallback():
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


def test_completion_with_fallback_sync_prefers_highest_weighted_policy_score():
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


def test_completion_with_fallback_sync_uses_local_profile_for_runtime_path(async_db):
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


def test_completion_with_fallback_sync_uses_runtime_model_override(async_db):
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


def test_completion_with_fallback_sync_keeps_remote_fallback_base_for_local_runtime_path():
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


def test_completion_with_fallback_sync_uses_runtime_fallback_override_chain(async_db):
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
async def test_completion_with_fallback_logs_session_and_request_context(async_db):
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


def test_completion_with_fallback_prefers_alternate_runtime_profile_before_explicit_fallback(async_db):
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


def test_completion_with_fallback_logs_routing_decision(async_db):
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
    assert details["candidate_targets"][2]["model_id"] == "openai/gpt-4.1-nano"
    assert details["candidate_targets"][2]["matched_policy_intents"] == ["cheap"]
    assert details["candidate_targets"][2]["policy_score"] == 0.0


def test_completion_with_fallback_logs_weighted_policy_scores(async_db):
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


def test_completion_with_fallback_reroutes_to_guardrail_compliant_target(async_db):
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
    assert details["rerouted_from_policy_guardrails"] is True
    primary_candidate = next(
        candidate for candidate in details["candidate_targets"] if candidate["source"] == "primary"
    )
    assert primary_candidate["missing_required_intents"] == ["tool_use"]
    assert "missing_required_intents" in primary_candidate["reason_codes"]
    assert primary_candidate["decision"] == "skipped"


def test_completion_with_fallback_degrades_open_when_no_guardrail_compliant_target(async_db):
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
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "fail open when nothing is compliant"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_title_generation",
        )

    assert result is completion_response
    assert [call.kwargs["model"] for call in mock_completion.call_args_list] == [
        "openrouter/anthropic/claude-sonnet-4",
    ]

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_routing_decision"]

    events = asyncio.run(_fetch())
    details = events[0]["details"]
    assert details["selected_model"] == "openrouter/anthropic/claude-sonnet-4"
    assert details["guardrail_compliant_targets_present"] is False
    assert details["rerouted_from_policy_guardrails"] is False
    assert "no_guardrail_compliant_targets" in details["candidate_targets"][0]["reason_codes"]


def test_completion_with_fallback_applies_cost_and_latency_guardrails(async_db):
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


def test_completion_with_fallback_does_not_try_noncompliant_target_after_guardrail_skip(async_db):
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
            runtime_path="chat_agent",
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
