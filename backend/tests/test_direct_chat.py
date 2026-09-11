from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("mocked_canonical_inference_context")

from config.settings import settings
from src.security.trust_contract import canonical_digest
from src.agent.direct_chat import (
    _stream_chunk_delta,
    looks_like_tool_or_web_request,
    run_direct_local_chat,
    should_use_direct_local_chat,
    stream_direct_local_chat,
)


def test_direct_chat_handles_onboarding_when_openrouter_configured():
    with (
        patch.object(settings, "openrouter_provider_only", True),
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=openrouter"),
    ):
        assert should_use_direct_local_chat(
            "Hello",
            runtime_path="onboarding_agent",
            is_onboarding=True,
        )


def test_direct_chat_ignores_legacy_local_profile():
    with (
        patch.object(settings, "openrouter_provider_only", False),
        patch.object(settings, "local_model", "openai/local-gemma"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=local-gemma-chat-thinking"),
    ):
        assert should_use_direct_local_chat(
            "Hello",
            runtime_path="onboarding_agent",
            is_onboarding=True,
        )


def test_direct_chat_ignores_stale_local_preference_in_openrouter_only_mode():
    with (
        patch.object(settings, "openrouter_provider_only", True),
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=local-gemma-chat-thinking"),
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


def test_direct_chat_accepts_lightweight_greeting_punctuation_on_openrouter():
    with (
        patch.object(settings, "runtime_profile_preferences", "chat_agent=openrouter"),
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


def test_direct_chat_does_not_intercept_onboarding_bare_domain():
    with (
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=openrouter"),
    ):
        assert not should_use_direct_local_chat(
            "natgurlain.com",
            runtime_path="onboarding_agent",
            is_onboarding=True,
        )


def test_direct_chat_does_not_intercept_website_requests():
    with (
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=openrouter"),
    ):
        assert not should_use_direct_local_chat(
            "Check the website and get the goals from it",
            runtime_path="onboarding_agent",
            is_onboarding=True,
        )


def test_tool_or_web_request_classifier_uses_word_boundaries():
    assert looks_like_tool_or_web_request("Check natgurlain.com and summarize it")
    assert looks_like_tool_or_web_request("Open https://example.com/about")
    assert looks_like_tool_or_web_request("Please inspect the website")
    assert not looks_like_tool_or_web_request("Hello, I am ready for onboarding")


@pytest.mark.asyncio
async def test_run_direct_chat_uses_bounded_governed_completion():
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
    assert call_kwargs["local_runtime_only"] is False


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
async def test_stream_direct_local_chat_uses_governed_streaming_facade():
    async def governed_stream(**_kwargs):
        yield "Hel"
        yield "lo"

    with (
        patch.object(settings, "model_max_tokens", 4096),
        patch.object(settings, "runtime_profile_preferences", "onboarding_agent=openrouter"),
        patch(
            "src.agent.direct_chat.stream_completion_with_fallback",
            side_effect=governed_stream,
        ) as mock_completion,
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
    assert call_kwargs["runtime_path"] == "onboarding_agent"
    assert call_kwargs["request_context"].request_id == "test-inference-request"
    assert call_kwargs["request_context"].data_digest == canonical_digest(call_kwargs["messages"])
    assert call_kwargs["request_id"] == "test-inference-request"
