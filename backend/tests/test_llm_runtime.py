"""Tests for shared LLM runtime configuration and fallback behavior."""

from unittest.mock import MagicMock, patch

from config.settings import settings
from src.llm_runtime import (
    build_completion_kwargs,
    build_model_kwargs,
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
