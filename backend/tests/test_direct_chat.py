from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config.settings import settings
from src.agent.direct_chat import _stream_chunk_delta, run_direct_local_chat, should_use_direct_local_chat, stream_direct_local_chat


def test_direct_local_chat_handles_onboarding_when_local_gemma_configured():
    with (
        patch.object(settings, "local_model", "openai/local-gemma"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=local-gemma-chat-thinking"),
    ):
        assert should_use_direct_local_chat(
            "Hello",
            runtime_path="onboarding_agent",
            is_onboarding=True,
        )


def test_direct_local_chat_handles_generic_local_profile():
    with (
        patch.object(settings, "local_model", "openai/local-gemma"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_runtime_paths", "onboarding_agent"),
        patch.object(settings, "runtime_profile_preferences", ""),
    ):
        assert should_use_direct_local_chat(
            "Hello",
            runtime_path="onboarding_agent",
            is_onboarding=True,
        )


def test_direct_local_chat_leaves_non_lightweight_work_to_agent():
    with (
        patch.object(settings, "local_model", "openai/local-gemma"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        assert not should_use_direct_local_chat(
            "Create a goal, inspect the current window, and summarize my open tasks.",
            runtime_path="chat_agent",
            is_onboarding=False,
        )


def test_direct_local_chat_accepts_lightweight_greeting_punctuation():
    with (
        patch.object(settings, "local_model", "openai/local-gemma"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        assert should_use_direct_local_chat(
            "Hello?",
            runtime_path="chat_agent",
            is_onboarding=False,
        )
        assert should_use_direct_local_chat(
            "Thanks!",
            runtime_path="chat_agent",
            is_onboarding=False,
        )
        assert should_use_direct_local_chat(
            "Hello, reply in one short sentence.",
            runtime_path="chat_agent",
            is_onboarding=False,
        )


def test_direct_local_chat_does_not_intercept_onboarding_bare_domain():
    with (
        patch.object(settings, "local_model", "openai/local-gemma"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=local-gemma-chat-thinking"),
    ):
        assert not should_use_direct_local_chat(
            "natgurlain.com",
            runtime_path="onboarding_agent",
            is_onboarding=True,
        )


def test_direct_local_chat_does_not_intercept_website_requests():
    with (
        patch.object(settings, "local_model", "openai/local-gemma"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=local-gemma-chat-thinking"),
    ):
        assert not should_use_direct_local_chat(
            "Check the website and get the goals from it",
            runtime_path="onboarding_agent",
            is_onboarding=True,
        )


@pytest.mark.asyncio
async def test_run_direct_local_chat_uses_bounded_local_completion():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="Hello. What should I call you?"),
            )
        ]
    )

    with (
        patch.object(settings, "model_max_tokens", 4096),
        patch("src.agent.direct_chat.completion_with_fallback_sync", return_value=response) as mock_completion,
    ):
        result = await run_direct_local_chat(
            "Hello",
            runtime_path="onboarding_agent",
            is_onboarding=True,
            request_id="direct-test",
        )

    assert result == "Hello. What should I call you?"
    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["runtime_path"] == "onboarding_agent"
    assert call_kwargs["max_tokens"] == 512
    assert call_kwargs["local_runtime_only"] is True


def test_stream_chunk_delta_reads_openai_compatible_content():
    chunk = {
        "choices": [
            {
                "delta": {
                    "content": "hel",
                }
            }
        ]
    }

    assert _stream_chunk_delta(chunk) == "hel"


@pytest.mark.asyncio
async def test_stream_direct_local_chat_yields_litellm_deltas():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
    ]

    with (
        patch.object(settings, "model_max_tokens", 4096),
        patch.object(settings, "local_model", "openai/local-gemma"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=local"),
        patch("litellm.completion", return_value=iter(chunks)) as mock_completion,
    ):
        result = [
            delta
            async for delta in stream_direct_local_chat(
                "Hello",
                runtime_path="onboarding_agent",
                is_onboarding=True,
            )
        ]

    assert result == ["Hel", "lo"]
    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["stream"] is True
    assert call_kwargs["model"] == "openai/local-gemma"
    assert call_kwargs["api_base"] == "http://127.0.0.1:8000/v1"
