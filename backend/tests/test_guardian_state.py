"""Tests for explicit guardian-state synthesis."""

from unittest.mock import MagicMock, patch

import pytest

from src.agent.factory import create_agent
from src.agent.session import SessionManager
from src.agent.strategist import create_strategist_agent
from src.guardian.state import GuardianState, GuardianStateConfidence, build_guardian_state
from src.observer.context import CurrentContext


def _make_guardian_state() -> GuardianState:
    return GuardianState(
        soul_context="# Soul\n\n## Identity\nBuilder",
        observer_context=CurrentContext(
            time_of_day="morning",
            day_of_week="Monday",
            is_working_hours=True,
            active_goals_summary="Ship guardian state",
            active_window="VS Code",
            screen_context="Editing roadmap",
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="active_goals",
            interruption_cost="low",
        ),
        memory_context="- [goal] Ship guardian state\n- [pattern] Prefers dense dashboards",
        current_session_history="User: What should Seraph improve next?\nAssistant: Build explicit guardian state.",
        recent_sessions_summary='- Prior roadmap: assistant said "Land guardian-state synthesis next"',
        recent_intervention_feedback="- advisory delivered, feedback=helpful, reason=available_capacity: Stretch and refocus.",
        confidence=GuardianStateConfidence(
            overall="grounded",
            observer="grounded",
            memory="grounded",
            current_session="grounded",
            recent_sessions="grounded",
        ),
    )


@pytest.mark.asyncio
async def test_build_guardian_state_collects_memory_and_recent_sessions(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What should Seraph improve next?")
    await sm.add_message("current", "assistant", "Build explicit guardian state.")
    await sm.get_or_create("prior")
    await sm.update_title("prior", "Prior roadmap")
    await sm.add_message("prior", "assistant", "Land guardian-state synthesis next.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Ship guardian state",
        active_window="VS Code",
        screen_context="Editing roadmap",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch("src.memory.soul.read_soul", return_value="# Soul\n\n## Identity\nBuilder"),
        patch("src.memory.vector_store.search_formatted", return_value="- [goal] Ship guardian state"),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="- advisory delivered, feedback=helpful: Stretch and refocus.",
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What should Seraph improve next?")

    assert state.soul_context == "# Soul\n\n## Identity\nBuilder"
    assert state.active_goals_summary == "Ship guardian state"
    assert state.confidence.overall == "grounded"
    assert state.confidence.observer == "grounded"
    assert state.confidence.memory == "grounded"
    assert state.confidence.current_session == "grounded"
    assert state.confidence.recent_sessions == "grounded"
    assert "Build explicit guardian state." in state.current_session_history
    assert "Prior roadmap" in state.recent_sessions_summary
    assert "Ship guardian state" in state.memory_context
    assert "feedback=helpful" in state.recent_intervention_feedback


def test_guardian_state_prompt_block_exposes_confidence_and_recent_sessions():
    block = _make_guardian_state().to_prompt_block()

    assert "Overall confidence: grounded" in block
    assert "Observer model: confidence=grounded | salience=high (active_goals) | interruption_cost=low" in block
    assert "Observer snapshot:" in block
    assert "Relevant memories:" in block
    assert "Recent sessions:" in block
    assert "Recent intervention feedback:" in block
    assert "Ship guardian state" in block
    assert "Prior roadmap" in block
    assert "feedback=helpful" in block


@patch("src.agent.factory.ToolCallingAgent")
@patch("src.agent.factory.get_model")
def test_create_agent_injects_guardian_state(mock_get_model, mock_agent_cls):
    mock_get_model.return_value = MagicMock()
    mock_agent_cls.return_value = MagicMock()

    create_agent(guardian_state=_make_guardian_state())

    instructions = mock_agent_cls.call_args[1]["instructions"]
    assert "GUARDIAN STATE" in instructions
    assert "Overall confidence: grounded" in instructions
    assert "USER IDENTITY" in instructions
    assert "RELEVANT MEMORIES" in instructions
    assert "CONVERSATION HISTORY" in instructions
    assert "Recent intervention feedback:" in instructions


@patch("src.agent.strategist.LiteLLMModel")
def test_create_strategist_agent_accepts_guardian_state(mock_model_cls):
    mock_model_cls.return_value = MagicMock()

    agent = create_strategist_agent(guardian_state=_make_guardian_state())

    assert "Overall confidence: grounded" in agent.instructions
    assert "Recent sessions:" in agent.instructions
    assert "Recent intervention feedback:" in agent.instructions
