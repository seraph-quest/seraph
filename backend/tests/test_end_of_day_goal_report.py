"""Tests for local-screen end-of-day goal reports."""

from __future__ import annotations

import json
import stat
from pathlib import Path
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
async def test_build_report_counts_framekeeper_observation_source(async_db):
    from src.db.models import ScreenObservation
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    details = [
        "capture_artifacts:"
        + json.dumps(
            {
                "provider": "framekeeper",
                "source": "framekeeper",
                "image_path": "/tmp/framekeeper/capture.png",
                "image_sha256": "abc123",
            },
            sort_keys=True,
        )
    ]
    async with async_db() as db:
        db.add(
            ScreenObservation(
                timestamp=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
                app_name="Framekeeper",
                window_title="capture.png",
                activity_type="screen",
                project="seraph",
                summary="Framekeeper screenshot ingested from capture.png.",
                duration_s=300,
                details_json=json.dumps(details),
            )
        )

    with patch.object(settings, "user_timezone", "UTC"), patch.object(
        settings, "end_of_day_report_llm_enabled", False
    ):
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    assert report["summary"]["total_observations"] == 1
    assert report["summary"]["by_source"] == {"framekeeper": 300}
    assert "Source mix:" in report["body"]
    assert "- framekeeper: 5m" in report["body"]


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
async def test_store_report_writes_durable_artifacts(async_db, tmp_path):
    from src.db.models import MemoryEpisode
    from sqlmodel import select
    from src.scheduler.jobs.end_of_day_goal_report import store_end_of_day_goal_report

    report = {
        "date": "2026-06-20",
        "timezone": "UTC",
        "body": "Stored durable EOD report",
        "summary": {"total_observations": 2, "total_tracked_minutes": 45},
        "goal_alignment": [{"goal_id": "g1", "status": "aligned"}],
        "completed_goal_count": 0,
        "active_goal_count": 1,
        "analysis_provider": "deterministic-local",
        "artifact_schema": "seraph.end_of_day_goal_report.v1",
    }

    with patch.object(settings, "report_archive_dir", str(tmp_path / "reports")):
        episode_id = await store_end_of_day_goal_report(report)

    async with async_db() as db:
        episode = (await db.execute(select(MemoryEpisode))).scalar_one()

    assert episode.id == episode_id
    metadata = json.loads(episode.metadata_json or "{}")
    artifacts = metadata["artifacts"]
    text_path = Path(artifacts["report_text_path"])
    json_path = Path(artifacts["report_json_path"])
    assert text_path.read_text(encoding="utf-8") == "Stored durable EOD report"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["artifact_schema"] == "seraph.end_of_day_goal_report.v1"
    assert payload["report"]["analysis_provider"] == "deterministic-local"
    assert stat.S_IMODE(text_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(text_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(json_path.stat().st_mode) == 0o600
    assert artifacts["report_text_sha256"]
    assert artifacts["report_json_sha256"]


def test_archive_report_receipt_writes_private_metadata(tmp_path):
    from src.scheduler.jobs.end_of_day_goal_report import (
        EmailDeliveryResult,
        archive_end_of_day_report_receipt,
    )

    report = {
        "date": "2026-06-20",
        "analysis_provider": "deterministic-local",
        "artifacts": {"report_text_sha256": "abc123"},
    }

    with patch.object(settings, "report_archive_dir", str(tmp_path / "reports")):
        receipt = archive_end_of_day_report_receipt(
            action="manual-preview",
            report=report,
            episode_id="episode-1",
            email_result=EmailDeliveryResult(status="preview_only", reason="manual_preview"),
        )

    receipt_path = Path(receipt["receipt_path"])
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["artifact_schema"] == "seraph.end_of_day_report_receipt.v1"
    assert payload["action"] == "manual-preview"
    assert payload["episode_id"] == "episode-1"
    assert payload["email"]["status"] == "preview_only"
    assert stat.S_IMODE(receipt_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600


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


@pytest.mark.asyncio
async def test_manual_report_preview_stores_report_and_receipt(async_db, tmp_path):
    from src.scheduler.jobs.end_of_day_goal_report import run_manual_end_of_day_goal_report

    with patch.object(settings, "report_archive_dir", str(tmp_path / "reports")), patch(
        "src.scheduler.jobs.end_of_day_goal_report.build_end_of_day_goal_report",
        new=AsyncMock(
            return_value={
                "date": "2026-06-20",
                "timezone": "UTC",
                "body": "Manual preview body",
                "summary": {"total_observations": 1, "total_tracked_minutes": 5},
                "goal_alignment": [],
                "completed_goal_count": 0,
                "active_goal_count": 1,
                "analysis_provider": "deterministic-local",
                "artifact_schema": "seraph.end_of_day_goal_report.v1",
            }
        ),
    ):
        result = await run_manual_end_of_day_goal_report(send_email=False)

    assert result["action"] == "manual-preview"
    assert result["email"]["status"] == "preview_only"
    assert result["report"]["body"] == "Manual preview body"
    assert result["report"]["artifacts"]["report_text_sha256"]
    assert result["report"]["artifacts"]["report_json_sha256"]
    assert result["report"]["artifacts"]["raw_artifact_path_exposed"] is False
    assert "report_text_path" not in result["report"]["artifacts"]
    assert "report_json_path" not in result["report"]["artifacts"]
    assert result["receipt"]["receipt_id"]
    assert result["receipt"]["receipt_sha256"]
    assert result["receipt"]["raw_receipt_path_exposed"] is False
    assert "receipt_path" not in result["receipt"]


@pytest.mark.asyncio
async def test_manual_report_send_respects_preview_required(async_db, tmp_path):
    from src.scheduler.jobs.end_of_day_goal_report import run_manual_end_of_day_goal_report

    with patch.object(settings, "report_archive_dir", str(tmp_path / "reports")), patch.object(
        settings, "email_reports_enabled", True
    ), patch.object(settings, "email_reports_preview_required", True), patch(
        "src.scheduler.jobs.end_of_day_goal_report.build_end_of_day_goal_report",
        new=AsyncMock(
            return_value={
                "date": "2026-06-20",
                "timezone": "UTC",
                "body": "Manual send body",
                "summary": {"total_observations": 1, "total_tracked_minutes": 5},
                "goal_alignment": [],
                "completed_goal_count": 0,
                "active_goal_count": 1,
                "analysis_provider": "deterministic-local",
                "artifact_schema": "seraph.end_of_day_goal_report.v1",
            }
        ),
    ):
        result = await run_manual_end_of_day_goal_report(send_email=True, preview_acknowledged=False)

    assert result["action"] == "manual-send"
    assert result["email"]["status"] == "preview_required"
    assert result["email"]["reason"] == "operator_preview_required"
