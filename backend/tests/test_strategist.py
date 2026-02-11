"""Tests for strategist agent — parse_strategist_response and create_strategist_agent."""

from unittest.mock import patch, MagicMock

from src.agent.strategist import (
    StrategistDecision,
    create_strategist_agent,
    parse_strategist_response,
)


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


@patch("src.agent.strategist.LiteLLMModel")
def test_create_strategist_agent_model_temperature(mock_model_cls):
    mock_model_cls.return_value = MagicMock()
    create_strategist_agent("context")

    call_kwargs = mock_model_cls.call_args[1]
    assert call_kwargs["temperature"] == 0.4


@patch("src.agent.strategist.LiteLLMModel")
def test_create_strategist_agent_max_steps(mock_model_cls):
    mock_model_cls.return_value = MagicMock()
    agent = create_strategist_agent("context")

    assert agent.max_steps == 5
