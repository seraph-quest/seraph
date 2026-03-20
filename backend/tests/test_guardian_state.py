"""Tests for explicit guardian-state synthesis."""

from unittest.mock import MagicMock, patch

import pytest

from src.agent.factory import create_agent
from src.agent.session import SessionManager
from src.agent.strategist import create_strategist_agent
from src.guardian.state import GuardianState, GuardianStateConfidence, build_guardian_state
from src.guardian.world_model import GuardianWorldModel, build_guardian_world_model
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
        world_model=GuardianWorldModel(
            current_focus="Ship guardian state while in VS Code",
            active_commitments=("Ship guardian state",),
            open_loops_or_pressure=('Prior roadmap: assistant said "Land guardian-state synthesis next"',),
            focus_alignment="medium",
            intervention_receptivity="high",
            active_blockers=("Need workflow write access",),
            next_up=("Close the roadmap review",),
            dominant_thread="Ship guardian state",
            active_projects=("Guardian cockpit",),
            execution_pressure=("Workflow brief-sync degraded at write_file",),
            memory_signals=("Ship guardian state", "Prefers dense dashboards"),
            continuity_threads=('Prior roadmap: assistant said "Land guardian-state synthesis next"',),
            collaborators=("Alice",),
            recurring_obligations=("Weekly roadmap review",),
            project_timeline=("Guardian cockpit", "Workflow brief-sync degraded at write_file"),
        ),
        memory_context="- [goal] Ship guardian state\n- [pattern] Prefers dense dashboards",
        current_session_history="User: What should Seraph improve next?\nAssistant: Build explicit guardian state.",
        recent_sessions_summary='- Prior roadmap: assistant said "Land guardian-state synthesis next"',
        recent_intervention_feedback="- advisory delivered, feedback=helpful, reason=available_capacity: Stretch and refocus.",
        recent_execution_summary="- Workflow brief-sync degraded at write_file",
        confidence=GuardianStateConfidence(
            overall="grounded",
            observer="grounded",
            world_model="grounded",
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
        patch(
            "src.memory.vector_store.search_with_status",
            return_value=([{"category": "goal", "text": "Ship guardian state"}], False),
        ),
        patch(
            "src.audit.repository.audit_repository.list_events",
            return_value=[
                {
                    "event_type": "tool_result",
                    "tool_name": "workflow_brief_sync",
                    "details": {
                        "workflow_name": "brief-sync",
                        "continued_error_steps": ["write_file"],
                    },
                }
            ],
        ),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Guardian cockpit", "Docs parity"],
        ),
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
    assert state.confidence.world_model == "grounded"
    assert state.confidence.memory == "grounded"
    assert state.confidence.current_session == "grounded"
    assert state.confidence.recent_sessions == "grounded"
    assert state.world_model.current_focus == "Ship guardian state while in VS Code"
    assert "Ship guardian state" in state.world_model.active_commitments
    assert "Guardian cockpit" in state.world_model.active_projects
    assert "Ship guardian state" in state.world_model.memory_signals
    assert state.world_model.active_blockers
    assert state.world_model.next_up
    assert state.world_model.dominant_thread
    assert "Prior roadmap" in state.world_model.continuity_threads[0]
    assert "Guardian cockpit" in state.world_model.project_timeline[0]
    assert any(
        "brief-sync degraded" in item
        for item in state.world_model.execution_pressure
    )
    assert state.world_model.focus_alignment == "medium"
    assert state.world_model.intervention_receptivity == "high"
    assert "Build explicit guardian state." in state.current_session_history
    assert "Prior roadmap" in state.recent_sessions_summary
    assert "Ship guardian state" in state.memory_context
    assert "feedback=helpful" in state.recent_intervention_feedback
    assert "brief-sync degraded" in state.recent_execution_summary


@pytest.mark.asyncio
async def test_build_guardian_state_lowers_overall_confidence_when_memory_is_degraded(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What should Seraph improve next?")
    await sm.add_message("current", "assistant", "Build explicit guardian state.")

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
        patch("src.memory.vector_store.search_with_status", return_value=([], True)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Guardian cockpit"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="- advisory delivered, feedback=helpful: Stretch and refocus.",
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What should Seraph improve next?")

    assert state.confidence.memory == "degraded"
    assert state.confidence.overall == "partial"


def test_guardian_state_prompt_block_exposes_confidence_and_recent_sessions():
    block = _make_guardian_state().to_prompt_block()

    assert "Overall confidence: grounded" in block
    assert "World-model confidence: grounded" in block
    assert "World model:" in block
    assert "Current focus: Ship guardian state while in VS Code" in block
    assert "Intervention receptivity: high" in block
    assert "Active projects:" in block
    assert "Active blockers:" in block
    assert "Next up:" in block
    assert "Dominant thread: Ship guardian state" in block
    assert "Memory signals:" in block
    assert "Continuity threads:" in block
    assert "Recent execution pressure:" in block
    assert "Collaborators:" in block
    assert "Recurring obligations:" in block
    assert "Project timeline:" in block
    assert "Observer model: confidence=grounded | salience=high (active_goals) | interruption_cost=low" in block
    assert "Observer snapshot:" in block
    assert "Relevant memories:" in block
    assert "Recent sessions:" in block
    assert "Recent intervention feedback:" in block
    assert "Recent execution:" in block
    assert "Ship guardian state" in block
    assert "Prior roadmap" in block
    assert "feedback=helpful" in block
    assert "brief-sync degraded" in block


def test_world_model_does_not_count_session_fallback_focus_as_observer_corroboration():
    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="",
        active_window="",
        screen_context="",
        data_quality="good",
        observer_confidence="partial",
        salience_level="medium",
        salience_reason="none",
        interruption_cost="low",
    )

    model = build_guardian_world_model(
        observer_context=ctx,
        memory_context="",
        current_session_history="User: Continue the deployment review.",
        recent_sessions_summary='- Prior thread: Continue the deployment review.',
        recent_intervention_feedback="",
    )

    assert model.current_focus == "User: Continue the deployment review."
    assert "observer" not in model.corroboration_sources
    assert set(model.corroboration_sources) == {"current_session", "recent_sessions"}


@patch("src.agent.factory.ToolCallingAgent")
@patch("src.agent.factory.get_model")
def test_create_agent_injects_guardian_state(mock_get_model, mock_agent_cls):
    mock_get_model.return_value = MagicMock()
    mock_agent_cls.return_value = MagicMock()

    create_agent(guardian_state=_make_guardian_state())

    instructions = mock_agent_cls.call_args[1]["instructions"]
    assert "GUARDIAN STATE" in instructions
    assert "Overall confidence: grounded" in instructions
    assert "Current focus: Ship guardian state while in VS Code" in instructions
    assert "Intervention receptivity: high" in instructions
    assert "USER IDENTITY" in instructions
    assert "RELEVANT MEMORIES" in instructions
    assert "CONVERSATION HISTORY" in instructions
    assert "Recent intervention feedback:" in instructions


@patch("src.agent.strategist.LiteLLMModel")
def test_create_strategist_agent_accepts_guardian_state(mock_model_cls):
    mock_model_cls.return_value = MagicMock()

    agent = create_strategist_agent(guardian_state=_make_guardian_state())

    assert "Overall confidence: grounded" in agent.instructions
    assert "Current focus: Ship guardian state while in VS Code" in agent.instructions
    assert "Focus alignment: medium" in agent.instructions
    assert "Recent sessions:" in agent.instructions
    assert "Recent intervention feedback:" in agent.instructions
