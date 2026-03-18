"""Tests for strategist tick runtime audit coverage."""

from smolagents import Tool
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.audit.repository import audit_repository
from src.guardian.state import GuardianState, GuardianStateConfidence
from src.observer.context import CurrentContext
from src.observer.user_state import DeliveryDecision
from src.scheduler.jobs.strategist_tick import run_strategist_tick
from src.tools.audit import wrap_tools_for_audit


def _make_context(**overrides) -> CurrentContext:
    defaults = dict(
        time_of_day="afternoon",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="2 active goals",
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


class DummyStrategistTool(Tool):
    name = "get_goals"
    description = "Dummy strategist tool"
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        return "2 active goals"


def _make_guardian_state() -> GuardianState:
    return GuardianState(
        soul_context="# Soul\n\n## Goals\n- Ship guardian state",
        observer_context=_make_context(),
        memory_context="- [goal] Ship guardian state",
        current_session_history="",
        recent_sessions_summary='- Prior roadmap: assistant said "Land guardian-state synthesis next"',
        recent_intervention_feedback="- advisory delivered, feedback=helpful: Stretch and refocus.",
        confidence=GuardianStateConfidence(
            overall="grounded",
            observer="good",
            memory="grounded",
            current_session="not_requested",
            recent_sessions="grounded",
        ),
    )


@pytest.mark.asyncio
async def test_strategist_tick_logs_skip(async_db):
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=_make_context())
    mock_agent = MagicMock()
    mock_agent.run.return_value = (
        '{"should_intervene": false, "content": "", "intervention_type": "nudge", '
        '"urgency": 0, "reasoning": "All good"}'
    )

    with (
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=_make_guardian_state())),
        patch("src.scheduler.jobs.strategist_tick.create_strategist_agent", return_value=mock_agent),
    ):
        await run_strategist_tick()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_skipped"
        and event["tool_name"] == "strategist_tick"
        and event["details"]["reason"] == "All good"
        for event in events
    )


@pytest.mark.asyncio
async def test_strategist_tick_logs_success(async_db):
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=_make_context())
    mock_agent = MagicMock()
    mock_agent.run.return_value = (
        '{"should_intervene": true, "content": "Time to refocus.", '
        '"intervention_type": "advisory", "urgency": 3, "reasoning": "Focus drift"}'
    )
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)

    with (
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=_make_guardian_state())),
        patch("src.scheduler.jobs.strategist_tick.create_strategist_agent", return_value=mock_agent),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_strategist_tick()

    events = await audit_repository.list_events(limit=10)
    assert mock_deliver.await_args.kwargs["guardian_confidence"] == "grounded"
    assert any(
        event["event_type"] == "scheduler_job_succeeded"
        and event["tool_name"] == "strategist_tick"
        and event["details"]["delivery"] == "deliver"
        and event["details"]["policy_action"] is None
        for event in events
    )


@pytest.mark.asyncio
async def test_strategist_tick_binds_runtime_context_for_tool_audit(async_db):
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=_make_context())
    audited_tool = wrap_tools_for_audit([DummyStrategistTool()])[0]

    class DummyAgent:
        def run(self, _prompt):
            audited_tool()
            return (
                '{"should_intervene": false, "content": "", "intervention_type": "nudge", '
                '"urgency": 0, "reasoning": "No intervention"}'
            )

    with (
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=_make_guardian_state())),
        patch("src.scheduler.jobs.strategist_tick.create_strategist_agent", return_value=DummyAgent()),
    ):
        await run_strategist_tick()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "tool_call"
        and event["tool_name"] == "get_goals"
        and event["session_id"] == "scheduler:strategist_tick"
        for event in events
    )
