"""Tests for daily briefing job."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.observer.context import CurrentContext
from src.scheduler.jobs.daily_briefing import run_daily_briefing


def _make_context(**overrides) -> CurrentContext:
    defaults = dict(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="3 active goals",
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


def _mock_litellm_response(text: str):
    """Create a mock litellm completion response."""
    mock_choice = MagicMock()
    mock_choice.message.content = text
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    return mock_response


@pytest.mark.asyncio
async def test_daily_briefing_happy_path():
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.memory.vector_store.search_formatted", return_value="- [fact] User likes mornings"),
        patch("litellm.completion", return_value=_mock_litellm_response("Good morning, Hero! Here's your briefing...")),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()

        mock_deliver.assert_called_once()
        call_args = mock_deliver.call_args
        msg = call_args[0][0]
        assert msg.type == "proactive"
        assert msg.intervention_type == "advisory"
        assert "Good morning" in msg.content
        # is_scheduled=True
        assert call_args[1]["is_scheduled"] is True


@pytest.mark.asyncio
async def test_daily_briefing_context_refresh_failure():
    """Context refresh failure → early return (exception caught)."""
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(side_effect=Exception("DB down"))

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        # Should not raise — exception is caught internally
        await run_daily_briefing()
        mock_deliver.assert_not_called()


@pytest.mark.asyncio
async def test_daily_briefing_llm_failure():
    """LLM failure → early return (exception caught)."""
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch("src.memory.vector_store.search_formatted", return_value=""),
        patch("litellm.completion", side_effect=Exception("LLM API error")),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()
        mock_deliver.assert_not_called()


@pytest.mark.asyncio
async def test_daily_briefing_empty_calendar_goals():
    """Empty calendar/goals → still generates briefing."""
    ctx = _make_context(upcoming_events=[], active_goals_summary="")
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch("src.memory.vector_store.search_formatted", return_value=""),
        patch("litellm.completion", return_value=_mock_litellm_response("A quiet morning ahead.")),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()
        mock_deliver.assert_called_once()
        msg = mock_deliver.call_args[0][0]
        assert "quiet morning" in msg.content


@pytest.mark.asyncio
async def test_daily_briefing_with_events():
    """Calendar events are included in the prompt."""
    ctx = _make_context(upcoming_events=[
        {"summary": "Team standup", "start": "09:00"},
        {"summary": "1:1 with manager", "start": "14:00"},
    ])
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    captured_prompt = {}

    def mock_completion(**kwargs):
        captured_prompt["messages"] = kwargs.get("messages", [])
        return _mock_litellm_response("Briefing with events...")

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch("src.memory.vector_store.search_formatted", return_value=""),
        patch("litellm.completion", side_effect=mock_completion),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()

        # Check that events were included in the prompt
        prompt_text = captured_prompt["messages"][0]["content"]
        assert "Team standup" in prompt_text
        assert "1:1 with manager" in prompt_text
