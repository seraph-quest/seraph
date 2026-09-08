"""Tests for strategist agent decisions and agent construction."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from config.settings import settings
from src.agent.strategist import (
    StrategistDecision,
    create_strategist_agent,
    parse_strategist_response,
    run_strategist_decision_completion,
)
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.tools.approval import AuthorityTool
from src.tools.audit import AuditedTool
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal, canonical_digest


# ── parse_strategist_response tests ──────────────────────


def test_parse_valid_json():
    raw = '{"should_intervene": true, "content": "Time to stretch!", "intervention_type": "nudge", "urgency": 2, "reasoning": "User idle 30m"}'
    result = parse_strategist_response(raw)

    assert isinstance(result, StrategistDecision)
    assert result.should_intervene is True
    assert result.content == "Time to stretch!"
    assert result.intervention_type == "nudge"
    assert result.urgency == 2
    assert result.reasoning == "User idle 30m"


def test_parse_json_no_intervene():
    raw = '{"should_intervene": false, "content": "", "intervention_type": "nudge", "urgency": 0, "reasoning": "All good"}'
    result = parse_strategist_response(raw)

    assert result.should_intervene is False
    assert result.content == ""


def test_parse_markdown_fenced_json():
    raw = """```json
{"should_intervene": true, "content": "Goal behind!", "intervention_type": "advisory", "urgency": 3, "reasoning": "Weekly goal overdue"}
```"""
    result = parse_strategist_response(raw)

    assert result.should_intervene is True
    assert result.content == "Goal behind!"
    assert result.intervention_type == "advisory"


def test_parse_invalid_json():
    raw = "This is not JSON at all"
    result = parse_strategist_response(raw)

    assert result.should_intervene is False
    assert "Parse failure" in result.reasoning


def test_parse_empty_string():
    result = parse_strategist_response("")

    assert result.should_intervene is False
    assert "Empty response" in result.reasoning


def test_parse_none_like_string():
    result = parse_strategist_response("   ")

    assert result.should_intervene is False


def test_parse_partial_json():
    raw = '{"should_intervene": true}'
    result = parse_strategist_response(raw)

    assert result.should_intervene is True
    assert result.content == ""
    assert result.intervention_type == "nudge"
    assert result.urgency == 3


def test_parse_json_with_extra_fields():
    raw = '{"should_intervene": false, "content": "", "intervention_type": "nudge", "urgency": 0, "reasoning": "OK", "extra": "ignored"}'
    result = parse_strategist_response(raw)

    assert result.should_intervene is False


def test_parse_markdown_fenced_no_language():
    raw = """```
{"should_intervene": true, "content": "Check goals", "intervention_type": "nudge", "urgency": 2, "reasoning": "overdue"}
```"""
    result = parse_strategist_response(raw)

    assert result.should_intervene is True
    assert result.content == "Check goals"


# ── create_strategist_agent tests ────────────────────────


@patch("src.agent.strategist.LiteLLMModel")
def test_create_strategist_agent_returns_agent(mock_model_cls):
    mock_model_cls.return_value = MagicMock()
    agent = create_strategist_agent("Time: morning\nGoals: 3 active")

    assert agent is not None
    assert len(agent.tools) == 4  # view_soul, get_goals, get_goal_progress + final_answer (built-in)
    for tool_name in ("view_soul", "get_goals", "get_goal_progress"):
        assert isinstance(agent.tools[tool_name], AuthorityTool)
        assert isinstance(agent.tools[tool_name].wrapped_tool, AuditedTool)


@patch("src.agent.strategist.LiteLLMModel")
def test_strategist_state_tool_blocks_before_dispatch_without_authority(mock_model_cls):
    mock_model_cls.return_value = MagicMock()
    agent = create_strategist_agent("context")
    tool = agent.tools["get_goals"]
    dispatch = MagicMock()
    tool.wrapped_tool.wrapped_tool = dispatch

    with pytest.raises(PermissionError, match="runtime authority is unavailable"):
        tool()

    dispatch.assert_not_called()


@patch("src.agent.strategist.LiteLLMModel")
def test_strategist_state_tool_dispatches_with_runtime_authority(mock_model_cls):
    mock_model_cls.return_value = MagicMock()
    agent = create_strategist_agent("context")
    tokens = set_runtime_context(
        "strategist-test-session",
        "off",
        trust_principal=TrustPrincipal(
            principal_id="operator:strategist-test",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
            session_id="strategist-test-session",
        ),
    )
    try:
        with (
            patch.object(AuditedTool, "_log_event", return_value=None),
            patch("src.agent.strategist.get_goals.forward", return_value="goals") as dispatch,
        ):
            assert agent.tools["get_goals"]() == "goals"
    finally:
        reset_runtime_context(tokens)

    dispatch.assert_called_once_with()


@patch("src.agent.strategist.LiteLLMModel")
def test_create_strategist_agent_model_temperature(mock_model_cls):
    mock_model_cls.return_value = MagicMock()
    create_strategist_agent("context")

    call_kwargs = mock_model_cls.call_args[1]
    assert call_kwargs["temperature"] == 0.4


@patch("src.agent.strategist.LiteLLMModel")
def test_create_strategist_agent_uses_openrouter_profile_runtime_path(mock_model_cls):
    mock_model_cls.return_value = MagicMock()
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "openrouter_api_key", "openrouter-key"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "strategist_agent"),
    ):
        create_strategist_agent("context")

    call_kwargs = mock_model_cls.call_args[1]
    assert call_kwargs["model_id"] == "openrouter/anthropic/claude-sonnet-4"
    assert call_kwargs["api_base"] == "https://openrouter.ai/api/v1"
    assert call_kwargs["api_key"] == "openrouter-key"


@patch("src.agent.strategist.LiteLLMModel")
def test_create_strategist_agent_max_steps(mock_model_cls):
    mock_model_cls.return_value = MagicMock()
    agent = create_strategist_agent("context")

    assert agent.max_steps == 5


@pytest.mark.asyncio
async def test_run_strategist_decision_completion_uses_bounded_runtime_path(mocked_canonical_inference_context):
    response = MagicMock()
    response.choices = [
        MagicMock(
            message=MagicMock(
                content='{"should_intervene": false, "content": "", "reasoning": "All good"}'
            )
        )
    ]
    completion = AsyncMock(return_value=response)

    with patch("src.agent.strategist.completion_with_fallback", completion):
        raw = await run_strategist_decision_completion("Current context")

    assert raw == '{"should_intervene": false, "content": "", "reasoning": "All good"}'
    completion.assert_awaited_once()
    assert completion.await_args.kwargs["runtime_path"] == "strategist_agent"
    assert completion.await_args.kwargs["temperature"] == 0.2
    assert completion.await_args.kwargs["max_tokens"] == 512
    assert "Do not call tools" in completion.await_args.kwargs["messages"][0]["content"]
    call = completion.await_args.kwargs
    assert call["request_context"].data_digest == canonical_digest(call["messages"])
