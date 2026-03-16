"""Tests for shared LLM runtime configuration and fallback behavior."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from config.settings import settings
from src.audit.repository import audit_repository
from src.llm_runtime import (
    FallbackLiteLLMModel,
    _reset_target_health,
    build_completion_kwargs,
    build_model_kwargs,
    completion_with_fallback,
    completion_with_fallback_sync,
    _mark_request_timed_out,
    _register_request,
    _finish_request,
    reset_current_llm_request_id,
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
        events = await audit_repository.list_events(limit=10)
        return [e for e in events if e["event_type"] == "llm_target_rerouted"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["details"]["primary_model"] == "openrouter/anthropic/claude-sonnet-4"
    assert events[0]["details"]["rerouted_model"] == "openai/gpt-4o-mini"


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
