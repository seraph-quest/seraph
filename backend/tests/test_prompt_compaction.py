from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config.settings import settings
from src.agent.context_window import _count_tokens
from src.agent.factory import create_agent
from src.agent.prompt_compaction import (
    PromptCompactionConfigurationError,
    PromptSection,
    compact_messages_for_local_runtime,
    compact_prompt_sections,
    local_runtime_prompt_budget,
)


def _large_block(prefix: str, count: int = 900) -> str:
    return "\n".join(f"{prefix} line {index}: " + ("context " * 12) for index in range(count))


def test_local_runtime_prompt_budget_reserves_output_and_tool_tokens():
    with (
        patch.object(settings, "local_runtime_context_window_tokens", 4096),
        patch.object(settings, "local_runtime_prompt_safety_ratio", 1.0),
        patch.object(settings, "local_runtime_tool_reserve_tokens", 512),
        patch.object(settings, "local_runtime_min_section_tokens", 64),
    ):
        assert local_runtime_prompt_budget(reserved_output_tokens=512) == 3072


def test_local_runtime_prompt_budget_rejects_impossible_reserve_configuration():
    with (
        patch.object(settings, "local_runtime_context_window_tokens", 4096),
        patch.object(settings, "local_runtime_prompt_safety_ratio", 0.9),
        patch.object(settings, "local_runtime_tool_reserve_tokens", 3200),
        patch.object(settings, "local_runtime_min_section_tokens", 256),
    ):
        try:
            local_runtime_prompt_budget(reserved_output_tokens=1024)
        except PromptCompactionConfigurationError as exc:
            assert "too small after reserves" in str(exc)
        else:
            raise AssertionError("Expected impossible local runtime budget to fail loudly")


def test_compact_prompt_sections_preserves_fixed_prompt_and_fits_budget():
    with (
        patch.object(settings, "local_runtime_context_window_tokens", 4096),
        patch.object(settings, "local_runtime_prompt_safety_ratio", 1.0),
        patch.object(settings, "local_runtime_tool_reserve_tokens", 512),
        patch.object(settings, "local_runtime_min_section_tokens", 64),
        patch("src.agent.prompt_compaction.log_background_task_event_sync") as mock_log,
    ):
        sections = [
            PromptSection("base", "Seraph must answer the current user.", shrinkable=False),
            PromptSection("history", _large_block("history"), min_tokens=128),
            PromptSection("memory", _large_block("memory"), min_tokens=128),
        ]

        result = compact_prompt_sections(
            sections,
            runtime_path="chat_agent",
            runtime_profile="local-gemma-chat-thinking",
            reserved_output_tokens=512,
        )

    assert result.compacted is True
    assert result.original_tokens > result.compacted_tokens
    assert result.compacted_tokens <= result.budget_tokens
    assert "Seraph must answer the current user." in result.text
    assert {"history", "memory"}.intersection(result.compacted_sections)
    mock_log.assert_called_once()


def test_compact_messages_for_local_runtime_preserves_current_user_and_fits_budget():
    messages = [
        {"role": "system", "content": _large_block("system", count=400)},
        {"role": "user", "content": _large_block("old-user", count=400)},
        {"role": "assistant", "content": _large_block("old-answer", count=400)},
        {"role": "user", "content": "Current request: answer this exact question."},
    ]

    with (
        patch.object(settings, "local_runtime_context_window_tokens", 4096),
        patch.object(settings, "local_runtime_prompt_safety_ratio", 1.0),
        patch.object(settings, "local_runtime_tool_reserve_tokens", 512),
        patch.object(settings, "local_runtime_min_section_tokens", 64),
        patch("src.agent.prompt_compaction.log_background_task_event_sync") as mock_log,
    ):
        compacted_messages, result = compact_messages_for_local_runtime(
            messages,
            runtime_path="chat_agent",
            runtime_profile="local-gemma-chat-thinking",
            reserved_output_tokens=512,
            session_id="session-1",
        )

    assert result.compacted is True
    assert result.compacted_tokens <= result.budget_tokens
    assert compacted_messages[-1]["content"] == "Current request: answer this exact question."
    assert any("compacted for local model context budget" in item["content"] for item in compacted_messages)
    mock_log.assert_called_once()


def test_create_agent_compacts_prompt_for_local_runtime_profile():
    model = SimpleNamespace(_runtime_profile="local-gemma-chat-thinking")
    captured_agent = MagicMock()

    with (
        patch.object(settings, "model_max_tokens", 256),
        patch.object(settings, "local_runtime_context_window_tokens", 4096),
        patch.object(settings, "local_runtime_prompt_safety_ratio", 1.0),
        patch.object(settings, "local_runtime_tool_reserve_tokens", 512),
        patch.object(settings, "local_runtime_min_section_tokens", 64),
        patch("src.agent.factory.get_model", return_value=model),
        patch("src.agent.factory.get_tools", return_value=[]),
        patch("src.agent.factory.skill_manager") as mock_skill_manager,
        patch("src.agent.factory.is_local_runtime_profile", return_value=True),
        patch("src.agent.factory.ToolCallingAgent", return_value=captured_agent) as mock_agent_cls,
        patch("src.agent.prompt_compaction.log_background_task_event_sync"),
    ):
        mock_skill_manager.get_active_skills.return_value = []

        agent = create_agent(
            additional_context=_large_block("conversation"),
            soul_context=_large_block("soul", count=300),
            memory_context=_large_block("memory", count=300),
        )

    instructions = mock_agent_cls.call_args[1]["instructions"]
    budget = local_runtime_prompt_budget(reserved_output_tokens=settings.model_max_tokens)
    assert agent is captured_agent
    assert _count_tokens(instructions) <= budget
    assert "productivity, performance, health, influence, and growth" in instructions
    assert "compacted for local model context budget" in instructions


def test_create_agent_does_not_compact_prompt_for_non_local_runtime_profile():
    model = SimpleNamespace(_runtime_profile="openrouter")

    with (
        patch("src.agent.factory.get_model", return_value=model),
        patch("src.agent.factory.get_tools", return_value=[]),
        patch("src.agent.factory.skill_manager") as mock_skill_manager,
        patch("src.agent.factory.is_local_runtime_profile", return_value=False),
        patch("src.agent.factory.compact_prompt_sections") as mock_compact,
        patch("src.agent.factory.ToolCallingAgent") as mock_agent_cls,
    ):
        mock_skill_manager.get_active_skills.return_value = []
        create_agent(additional_context="User: Hello\nAssistant: Hi")

    instructions = mock_agent_cls.call_args[1]["instructions"]
    assert "CONVERSATION HISTORY" in instructions
    assert "User: Hello" in instructions
    mock_compact.assert_not_called()
