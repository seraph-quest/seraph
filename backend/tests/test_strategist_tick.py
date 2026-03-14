"""Tests for strategist tick runtime audit coverage."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.audit.repository import audit_repository
from src.observer.context import CurrentContext
from src.observer.user_state import DeliveryDecision
from src.scheduler.jobs.strategist_tick import run_strategist_tick


def _make_context(**overrides) -> CurrentContext:
    defaults = dict(
        time_of_day="afternoon",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="2 active goals",
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


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
        patch("src.observer.manager.context_manager", mock_cm),
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
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.scheduler.jobs.strategist_tick.create_strategist_agent", return_value=mock_agent),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_strategist_tick()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_succeeded"
        and event["tool_name"] == "strategist_tick"
        and event["details"]["delivery"] == "deliver"
        for event in events
    )
