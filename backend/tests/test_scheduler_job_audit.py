"""Tests for runtime audit coverage across the remaining scheduler jobs."""

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.audit.repository import audit_repository
from src.observer.context import CurrentContext
from src.observer.user_state import DeliveryDecision


def _make_context(**overrides) -> CurrentContext:
    defaults = dict(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="3 active goals",
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


def _mock_llm_response(text: str):
    mock_choice = MagicMock()
    mock_choice.message.content = text
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    return mock_response


@pytest.mark.asyncio
async def test_calendar_scan_logs_skip_without_upcoming_events(async_db):
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=_make_context(upcoming_events=[]))

    with patch("src.observer.manager.context_manager", mock_cm):
        from src.scheduler.jobs.calendar_scan import run_calendar_scan

        await run_calendar_scan()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_skipped"
        and event["tool_name"] == "calendar_scan"
        and event["details"]["reason"] == "no_upcoming_events"
        for event in events
    )


@pytest.mark.asyncio
async def test_calendar_scan_logs_success(async_db):
    soon = (datetime.now() + timedelta(minutes=10)).isoformat()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(
        return_value=_make_context(upcoming_events=[{"summary": "Standup", "start": soon}])
    )
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        from src.scheduler.jobs.calendar_scan import run_calendar_scan

        await run_calendar_scan()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_succeeded"
        and event["tool_name"] == "calendar_scan"
        and event["details"]["alert_count"] == 1
        and event["details"]["delivery"] == "deliver"
        for event in events
    )


@pytest.mark.asyncio
async def test_goal_check_logs_skip_without_goals(async_db):
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=_make_context())
    mock_goals = MagicMock()
    mock_goals.get_dashboard = AsyncMock(return_value={"total_count": 0, "completed_count": 0})

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.goals.repository.goal_repository", mock_goals),
    ):
        from src.scheduler.jobs.goal_check import run_goal_check

        await run_goal_check()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_skipped"
        and event["tool_name"] == "goal_check"
        and event["details"]["reason"] == "no_goals"
        for event in events
    )


@pytest.mark.asyncio
async def test_goal_check_logs_success(async_db):
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=_make_context())
    mock_goals = MagicMock()
    mock_goals.get_dashboard = AsyncMock(return_value={"total_count": 5, "completed_count": 1})
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.goals.repository.goal_repository", mock_goals),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        from src.scheduler.jobs.goal_check import run_goal_check

        await run_goal_check()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_succeeded"
        and event["tool_name"] == "goal_check"
        and event["details"]["state"] == "goal_behind"
        and event["details"]["delivery"] == "deliver"
        for event in events
    )


@pytest.mark.asyncio
async def test_memory_consolidation_logs_skip_without_recent_sessions(async_db):
    from src.scheduler.jobs.memory_consolidation import run_memory_consolidation

    await run_memory_consolidation()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_skipped"
        and event["tool_name"] == "memory_consolidation"
        and event["details"]["reason"] == "no_recent_sessions"
        for event in events
    )


@pytest.mark.asyncio
async def test_memory_consolidation_logs_failure_when_all_sessions_fail(async_db):
    from src.db.models import Message, Session
    from src.scheduler.jobs.memory_consolidation import run_memory_consolidation

    async with async_db() as db:
        session = Session(id="session-a", title="Test")
        db.add(session)
        db.add(Message(session_id="session-a", role="user", content="hello"))

    with patch(
        "src.scheduler.jobs.memory_consolidation.consolidate_session",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        await run_memory_consolidation()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_failed"
        and event["tool_name"] == "memory_consolidation"
        and event["details"]["failed_session_count"] == 1
        and event["details"]["consolidated_count"] == 0
        for event in events
    )


@pytest.mark.asyncio
async def test_screen_cleanup_logs_skip_when_nothing_deleted(async_db):
    mock_repo = MagicMock()
    mock_repo.cleanup_old = AsyncMock(return_value=0)

    with patch("src.observer.screen_repository.screen_observation_repo", mock_repo):
        from src.scheduler.jobs.screen_cleanup import run_screen_cleanup

        await run_screen_cleanup()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_skipped"
        and event["tool_name"] == "screen_cleanup"
        and event["details"]["reason"] == "no_expired_observations"
        for event in events
    )


@pytest.mark.asyncio
async def test_weekly_activity_review_logs_success(async_db):
    mock_repo = MagicMock()
    mock_repo.get_weekly_summary = AsyncMock(
        return_value={
            "week_start": "2026-03-09",
            "week_end": "2026-03-15",
            "total_observations": 12,
            "total_tracked_minutes": 180,
            "by_activity": {"coding": 7200},
            "by_project": {"seraph": 5400},
            "daily_breakdown": [
                {"date": date.today().isoformat(), "tracked_minutes": 90, "observations": 6}
            ],
        }
    )
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)

    with (
        patch("src.observer.screen_repository.screen_observation_repo", mock_repo),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch(
            "src.scheduler.jobs.weekly_activity_review.completion_with_fallback",
            new=AsyncMock(return_value=_mock_llm_response("Solid week. Keep momentum.")),
        ),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        from src.scheduler.jobs.weekly_activity_review import run_weekly_activity_review

        await run_weekly_activity_review()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_succeeded"
        and event["tool_name"] == "weekly_activity_review"
        and event["details"]["delivery"] == "deliver"
        and event["details"]["total_observations"] == 12
        for event in events
    )


@pytest.mark.asyncio
async def test_weekly_activity_review_uses_named_runtime_path():
    mock_repo = MagicMock()
    mock_repo.get_weekly_summary = AsyncMock(
        return_value={
            "week_start": "2026-03-09",
            "week_end": "2026-03-15",
            "total_observations": 12,
            "total_tracked_minutes": 180,
            "by_activity": {"coding": 7200},
            "by_project": {"seraph": 5400},
            "daily_breakdown": [
                {"date": date.today().isoformat(), "tracked_minutes": 90, "observations": 6}
            ],
        }
    )

    with (
        patch("src.observer.screen_repository.screen_observation_repo", mock_repo),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch(
            "src.scheduler.jobs.weekly_activity_review.completion_with_fallback",
            new=AsyncMock(return_value=_mock_llm_response("Solid week. Keep momentum.")),
        ) as mock_completion,
        patch("src.observer.delivery.deliver_or_queue", AsyncMock(return_value=DeliveryDecision.deliver)),
    ):
        from src.scheduler.jobs.weekly_activity_review import run_weekly_activity_review

        await run_weekly_activity_review()

    assert mock_completion.await_args.kwargs["runtime_path"] == "weekly_activity_review"


@pytest.mark.asyncio
async def test_weekly_activity_review_logs_timeout(async_db):
    mock_repo = MagicMock()
    mock_repo.get_weekly_summary = AsyncMock(
        return_value={
            "week_start": "2026-03-09",
            "week_end": "2026-03-15",
            "total_observations": 4,
            "total_tracked_minutes": 60,
            "by_activity": {},
            "by_project": {},
            "daily_breakdown": [],
        }
    )

    with (
        patch("src.observer.screen_repository.screen_observation_repo", mock_repo),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch(
            "src.scheduler.jobs.weekly_activity_review.completion_with_fallback",
            new=AsyncMock(side_effect=TimeoutError),
        ),
    ):
        from src.scheduler.jobs.weekly_activity_review import run_weekly_activity_review

        await run_weekly_activity_review()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_timed_out"
        and event["tool_name"] == "weekly_activity_review"
        for event in events
    )
