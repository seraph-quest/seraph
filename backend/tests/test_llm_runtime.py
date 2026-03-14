"""Tests for shared LLM runtime configuration and fallback behavior."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from config.settings import settings
from src.audit.repository import audit_repository
from src.llm_runtime import (
    FallbackLiteLLMModel,
    build_completion_kwargs,
    build_model_kwargs,
    completion_with_fallback,
    completion_with_fallback_sync,
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
