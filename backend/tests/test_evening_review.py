"""Tests for evening review job."""

from datetime import datetime, date, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.observer.context import CurrentContext
from src.scheduler.jobs.evening_review import (
    run_evening_review,
    _count_messages_today,
    _get_completed_goals_today,
)


def _make_context(**overrides) -> CurrentContext:
    defaults = dict(
        time_of_day="evening",
        day_of_week="Monday",
        is_working_hours=False,
        active_goals_summary="2 active goals",
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


def _mock_litellm_response(text: str):
    mock_choice = MagicMock()
    mock_choice.message.content = text
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    return mock_response


@pytest.mark.asyncio
async def test_evening_review_happy_path():
    ctx = _make_context(recent_git_activity=[{"msg": "fix bug"}])
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.scheduler.jobs.evening_review._count_messages_today", AsyncMock(return_value=15)),
        patch("src.scheduler.jobs.evening_review._get_completed_goals_today", AsyncMock(return_value=["Exercise"])),
        patch("litellm.completion", return_value=_mock_litellm_response("Great day, Hero! You completed Exercise.")),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_evening_review()

        mock_deliver.assert_called_once()
        call_args = mock_deliver.call_args
        msg = call_args[0][0]
        assert msg.type == "proactive"
        assert msg.intervention_type == "advisory"
        assert msg.urgency == 2
        assert "Great day" in msg.content
        assert call_args[1]["is_scheduled"] is True


@pytest.mark.asyncio
async def test_evening_review_no_completed_goals():
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    captured_prompt = {}

    def mock_completion(**kwargs):
        captured_prompt["messages"] = kwargs.get("messages", [])
        return _mock_litellm_response("Rest well tonight.")

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch("src.scheduler.jobs.evening_review._count_messages_today", AsyncMock(return_value=5)),
        patch("src.scheduler.jobs.evening_review._get_completed_goals_today", AsyncMock(return_value=[])),
        patch("litellm.completion", side_effect=mock_completion),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_evening_review()

        mock_deliver.assert_called_once()
        prompt_text = captured_prompt["messages"][0]["content"]
        assert "None today" in prompt_text


@pytest.mark.asyncio
async def test_evening_review_no_messages_today():
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    captured_prompt = {}

    def mock_completion(**kwargs):
        captured_prompt["messages"] = kwargs.get("messages", [])
        return _mock_litellm_response("Quiet day.")

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch("src.scheduler.jobs.evening_review._count_messages_today", AsyncMock(return_value=0)),
        patch("src.scheduler.jobs.evening_review._get_completed_goals_today", AsyncMock(return_value=[])),
        patch("litellm.completion", side_effect=mock_completion),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_evening_review()

        mock_deliver.assert_called_once()
        prompt_text = captured_prompt["messages"][0]["content"]
        assert "0" in prompt_text


@pytest.mark.asyncio
async def test_evening_review_db_query_failure():
    """DB failure in context refresh → graceful catch."""
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(side_effect=Exception("DB error"))

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_evening_review()
        mock_deliver.assert_not_called()


@pytest.mark.asyncio
async def test_evening_review_llm_failure():
    """LLM failure → graceful catch."""
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch("src.scheduler.jobs.evening_review._count_messages_today", AsyncMock(return_value=0)),
        patch("src.scheduler.jobs.evening_review._get_completed_goals_today", AsyncMock(return_value=[])),
        patch("litellm.completion", side_effect=Exception("LLM down")),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_evening_review()
        mock_deliver.assert_not_called()


@pytest.mark.asyncio
async def test_count_messages_today_db_failure():
    """_count_messages_today returns 0 on DB failure."""
    with patch(
        "src.db.engine.get_session",
        side_effect=Exception("DB error"),
    ):
        count = await _count_messages_today()
        assert count == 0


@pytest.mark.asyncio
async def test_get_completed_goals_today_db_failure():
    """_get_completed_goals_today returns [] on failure."""
    with patch(
        "src.goals.repository.goal_repository",
        MagicMock(list_goals=AsyncMock(side_effect=Exception("DB error"))),
    ):
        result = await _get_completed_goals_today()
        assert result == []


@pytest.mark.asyncio
async def test_get_completed_goals_today_filters_by_date():
    """Only returns goals completed today."""
    today = date.today()
    yesterday = date(today.year, today.month, today.day - 1) if today.day > 1 else today

    goal_today = MagicMock()
    goal_today.title = "Today goal"
    goal_today.updated_at = datetime(today.year, today.month, today.day, 15, 0, tzinfo=timezone.utc)

    goal_yesterday = MagicMock()
    goal_yesterday.title = "Yesterday goal"
    goal_yesterday.updated_at = datetime(yesterday.year, yesterday.month, yesterday.day, 10, 0, tzinfo=timezone.utc)

    with patch(
        "src.goals.repository.goal_repository",
        MagicMock(list_goals=AsyncMock(return_value=[goal_today, goal_yesterday])),
    ):
        result = await _get_completed_goals_today()

        assert "Today goal" in result
        # Yesterday's goal should only be excluded if it's actually a different date
        if yesterday != today:
            assert "Yesterday goal" not in result
