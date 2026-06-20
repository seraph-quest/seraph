"""Tests for local-screen end-of-day goal reports."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import settings
from src.audit.repository import audit_repository


@pytest.mark.asyncio
async def test_build_report_uses_screen_observations_goals_and_redacts(async_db):
    from src.db.models import Goal, ScreenObservation
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    async with async_db() as db:
        db.add(
            ScreenObservation(
                timestamp=datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
                app_name="Code",
                window_title="Seraph",
                activity_type="coding",
                project="seraph",
                summary="Implemented local Codex parsing with api_key=secret-value",
                duration_s=3600,
            )
        )
        db.add(
            Goal(
                id="goal1",
                path="/",
                level="daily",
                title="Improve Seraph local screen parsing",
                domain="productivity",
                status="active",
                sort_order=0,
            )
        )

    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content="Useful report with token=hidden-value"))]

    with patch.object(settings, "user_timezone", "UTC"), patch.object(
        settings, "end_of_day_report_llm_enabled", True
    ), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(return_value=response),
    ) as completion:
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    assert report["summary"]["total_tracked_minutes"] == 60
    assert report["goal_alignment"][0]["status"] == "aligned"
    assert "secret-value" not in completion.await_args.kwargs["messages"][0]["content"]
    assert "hidden-value" not in report["body"]
    assert "[redacted]" in report["body"]
    assert completion.await_args.kwargs["runtime_path"] == "end_of_day_goal_report"


@pytest.mark.asyncio
async def test_build_report_is_deterministic_by_default(async_db):
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    completion = AsyncMock()

    with patch.object(settings, "user_timezone", "UTC"), patch.object(
        settings, "end_of_day_report_llm_enabled", False
    ), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=completion,
    ):
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    completion.assert_not_awaited()
    assert report["body"].startswith("End-of-day report for 2026-06-20")


@pytest.mark.asyncio
async def test_run_report_stores_episode_and_keeps_email_preview_only(async_db):
    from src.db.models import MemoryEpisode
    from sqlmodel import select
    from src.scheduler.jobs.end_of_day_goal_report import run_end_of_day_goal_report

    with patch.object(settings, "end_of_day_report_enabled", True), patch.object(
        settings, "email_reports_enabled", True
    ), patch.object(settings, "email_reports_preview_required", True), patch(
        "src.scheduler.jobs.end_of_day_goal_report.build_end_of_day_goal_report",
        new=AsyncMock(
            return_value={
                "date": "2026-06-20",
                "timezone": "UTC",
                "body": "Stored EOD report",
                "summary": {"total_observations": 2, "total_tracked_minutes": 45},
                "goal_alignment": [],
                "completed_goal_count": 0,
                "active_goal_count": 1,
            }
        ),
    ):
        await run_end_of_day_goal_report()

    async with async_db() as db:
        episodes = list((await db.execute(select(MemoryEpisode))).scalars().all())

    assert len(episodes) == 1
    assert episodes[0].source_tool_name == "end_of_day_goal_report"
    assert episodes[0].content == "Stored EOD report"

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_succeeded"
        and event["tool_name"] == "end_of_day_goal_report"
        and event["details"]["email_status"] == "preview_required"
        for event in events
    )


@pytest.mark.asyncio
async def test_email_delivery_requires_allowlisted_recipient_hash():
    from src.scheduler.jobs.end_of_day_goal_report import deliver_report_email

    with patch.object(settings, "email_reports_enabled", True), patch.object(
        settings, "email_reports_preview_required", False
    ), patch.object(settings, "email_reports_to", "user@example.com"), patch.object(
        settings, "email_reports_from", "seraph@example.com"
    ), patch.object(settings, "email_reports_to_allowlist", "other@example.com"), patch.object(
        settings, "smtp_host", "smtp.example.com"
    ):
        result = await deliver_report_email({"date": "2026-06-20", "body": "hello"})

    assert result.status == "blocked"
    assert result.reason == "recipient_not_allowlisted"
    assert result.recipient_hash is not None


@pytest.mark.asyncio
async def test_email_delivery_sends_only_when_configured_and_preview_disabled():
    from src.scheduler.jobs.end_of_day_goal_report import _recipient_hash, deliver_report_email

    smtp = MagicMock()
    smtp_context = MagicMock()
    smtp_context.__enter__.return_value = smtp

    with patch.object(settings, "email_reports_enabled", True), patch.object(
        settings, "email_reports_preview_required", False
    ), patch.object(settings, "email_reports_to", "user@example.com"), patch.object(
        settings, "email_reports_from", "seraph@example.com"
    ), patch.object(settings, "email_reports_to_allowlist", "user@example.com"), patch.object(
        settings, "smtp_host", "smtp.example.com"
    ), patch.object(settings, "smtp_port", 587), patch.object(
        settings, "smtp_use_tls", True
    ), patch.object(settings, "smtp_username", "user"), patch.object(
        settings, "smtp_password", "pass"
    ), patch("src.scheduler.jobs.end_of_day_goal_report.smtplib.SMTP", return_value=smtp_context):
        result = await deliver_report_email({"date": "2026-06-20", "body": "hello"})

    assert result.status == "sent"
    assert result.recipient_hash == _recipient_hash("user@example.com")
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("user", "pass")
    smtp.send_message.assert_called_once()
