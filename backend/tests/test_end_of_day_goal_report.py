"""Tests for local-screen end-of-day goal reports."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from config.settings import settings
from src.audit.repository import audit_repository


def _llm_response(content: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


@pytest.fixture(autouse=True)
def allow_remote_screen_llm_for_existing_report_tests():
    with patch("src.scheduler.screen_llm_policy.settings.screen_derived_llm_allow_remote", True):
        yield


@pytest.mark.asyncio
async def test_build_report_uses_llm_over_screen_observations_and_goals(async_db):
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

    response = _llm_response("Useful LLM-authored report with token=hidden-value")

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(return_value=response),
    ) as completion:
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    assert report["summary"]["total_tracked_minutes"] == 60
    assert report["goal_alignment"] == []
    prompt = completion.await_args.kwargs["messages"][0]["content"]
    assert "Improve Seraph local screen parsing" in prompt
    assert "Critical Goal Comparison" not in prompt
    assert report["body"] == "Useful LLM-authored report with token=hidden-value"
    assert completion.await_args.kwargs["runtime_path"] == "end_of_day_goal_report"
    assert completion.await_args.kwargs["local_runtime_only"] is False
    assert report["analysis_provider"].startswith("llm:")


@pytest.mark.asyncio
async def test_build_report_blocks_llm_when_disabled(async_db):
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.screen_llm_policy.settings.end_of_day_report_llm_enabled",
        False,
    ), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(),
    ) as completion:
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    completion.assert_not_awaited()
    assert report["analysis_provider"] == "blocked:llm_disabled"
    assert "blocked" in report["body"].lower()


@pytest.mark.asyncio
async def test_build_report_blocks_default_remote_screen_data_routing(async_db):
    from src.db.models import ScreenObservation
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    async with async_db() as db:
        db.add(
            ScreenObservation(
                timestamp=datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
                app_name="Code",
                window_title="Private screen",
                activity_type="coding",
                project="seraph",
                summary="Screenshot-derived private summary",
                duration_s=60,
            )
        )

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.screen_llm_policy.settings.screen_derived_llm_allow_remote",
        False,
    ), patch(
        "src.scheduler.screen_llm_policy.settings.runtime_profile_preferences",
        "",
    ), patch(
        "src.scheduler.screen_llm_policy.settings.local_runtime_paths",
        "",
    ), patch(
        "src.scheduler.screen_llm_policy.settings.local_model",
        "",
    ), patch(
        "src.scheduler.screen_llm_policy.settings.local_llm_api_base",
        "",
    ), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(),
    ) as completion:
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    completion.assert_not_awaited()
    assert report["analysis_provider"] == "blocked:local_profile_required"
    assert "Configure a verified local runtime profile" in report["body"]


@pytest.mark.asyncio
async def test_build_report_blocks_generic_local_profile_even_with_safe_proof(async_db):
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.screen_llm_policy.settings.screen_derived_llm_allow_remote",
        False,
    ), patch(
        "src.scheduler.screen_llm_policy.settings.runtime_profile_preferences",
        "end_of_day_goal_report=local",
    ), patch(
        "src.scheduler.screen_llm_policy.settings.local_model",
        "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF",
    ), patch(
        "src.scheduler.screen_llm_policy.settings.local_llm_api_base",
        "http://127.0.0.1:8000/v1",
    ), patch(
        "src.scheduler.screen_llm_policy.latest_local_runtime_profile_proof",
        return_value={"safe_for_single_backend_profile_routing": True},
    ), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(),
    ) as completion:
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    completion.assert_not_awaited()
    assert report["analysis_provider"] == "blocked:verified_profile_required"


@pytest.mark.asyncio
async def test_build_report_labels_verified_local_gemma_profile(async_db):
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    response = _llm_response("Verified local Gemma report")

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.screen_llm_policy.settings.screen_derived_llm_allow_remote",
        False,
    ), patch(
        "src.scheduler.screen_llm_policy.settings.runtime_profile_preferences",
        "end_of_day_goal_report=local-gemma-report-thinking",
    ), patch(
        "src.scheduler.screen_llm_policy.settings.local_model",
        "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF",
    ), patch(
        "src.scheduler.screen_llm_policy.settings.local_llm_api_base",
        "http://127.0.0.1:8000/v1",
    ), patch(
        "src.scheduler.screen_llm_policy.latest_local_runtime_profile_proof",
        return_value={"safe_for_single_backend_profile_routing": True},
    ), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(return_value=response),
    ) as completion:
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    completion.assert_awaited_once()
    assert completion.await_args.kwargs["local_runtime_only"] is True
    assert report["analysis_provider"] == (
        "llm:local-gemma-report-thinking:openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"
    )


@pytest.mark.asyncio
async def test_build_report_uses_llm_by_default(async_db):
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    response = _llm_response("Default LLM report")
    completion = AsyncMock(return_value=response)

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=completion,
    ):
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    completion.assert_awaited_once()
    assert report["body"] == "Default LLM report"


@pytest.mark.asyncio
async def test_build_report_counts_screenshot_folder_observation_source(async_db):
    from src.db.models import ScreenObservation
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    details = [
        "capture_artifacts:"
        + json.dumps(
            {
                "provider": "screenshot_folder",
                "source": "local_image_directory",
                "image_path": "/tmp/screenshot-recorder/capture.png",
                "image_sha256": "abc123",
                "image_bytes": 67,
                "file_format": "png",
                "width": 1,
                "height": 1,
            },
            sort_keys=True,
        )
    ]
    async with async_db() as db:
        db.add(
            ScreenObservation(
                timestamp=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
                app_name="Screenshot Folder",
                window_title="capture.png",
                activity_type="screen",
                project="seraph",
                summary="Screenshot image ingested from capture.png.",
                duration_s=None,
                details_json=json.dumps(details),
            )
        )

    response = _llm_response("Screenshot folder LLM report")
    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(return_value=response),
    ):
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    assert report["summary"]["total_observations"] == 1
    assert report["summary"]["by_source"] == {"screenshot_folder": 0}
    assert report["summary"]["source_observations"] == {"screenshot_folder": 1}
    assert report["summary"]["screenshot_samples"] == ["capture.png (png, 1x1, 67 B)"]
    assert report["body"] == "Screenshot folder LLM report"


@pytest.mark.asyncio
async def test_build_report_caps_screenshot_folder_duration_from_timestamp_gaps(async_db):
    from src.db.models import ScreenObservation
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    details = [
        "capture_artifacts:"
        + json.dumps(
            {
                "provider": "screenshot_folder",
                "image_path": "/tmp/screenshot-recorder/capture.png",
                "image_sha256": "abc123",
            },
            sort_keys=True,
        )
    ]
    async with async_db() as db:
        db.add(
            ScreenObservation(
                timestamp=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
                app_name="Screenshot Folder",
                window_title="capture-1.png",
                activity_type="screen",
                duration_s=60 * 60 * 24,
                details_json=json.dumps(details),
            )
        )
        db.add(
            ScreenObservation(
                timestamp=datetime(2026, 6, 20, 12, 0, 10, tzinfo=timezone.utc),
                app_name="Screenshot Folder",
                window_title="capture-2.png",
                activity_type="screen",
                duration_s=60 * 60 * 24,
                details_json=json.dumps(details),
            )
        )
        db.add(
            ScreenObservation(
                timestamp=datetime(2026, 6, 20, 12, 30, tzinfo=timezone.utc),
                app_name="Screenshot Folder",
                window_title="capture-3.png",
                activity_type="screen",
                duration_s=60 * 60 * 24,
                details_json=json.dumps(details),
            )
        )

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(return_value=_llm_response("Duration LLM report")),
    ):
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    assert report["summary"]["total_tracked_minutes"] == 5
    assert report["summary"]["by_source"] == {"screenshot_folder": 310}
    assert report["body"] == "Duration LLM report"


@pytest.mark.asyncio
async def test_build_report_passes_llm_digest_text_to_report_llm(async_db):
    from src.db.models import Goal, MemoryEpisode, MemoryEpisodeType
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    async with async_db() as db:
        db.add(
            Goal(
                id="goal-digest",
                path="/",
                level="daily",
                title="Ship Seraph screenshot digest report",
                domain="productivity",
                status="active",
                sort_order=0,
            )
        )
        db.add(
            MemoryEpisode(
                episode_type=MemoryEpisodeType.observer,
                source_tool_name="screenshot_observation_digest",
                summary="Screenshot digest window",
                content="\n".join(
                    [
                        "Screenshot digest 2026-06-20T10:00:00+00:00 to 2026-06-20T10:30:00+00:00",
                        "Observations: 2",
                        "LLM-authored notes:",
                        "- Seraph screenshot digest report implementation was visible.",
                        "- Billing migration is blocked by an unrelated provider.",
                    ]
                ),
                metadata_json=json.dumps(
                    {
                        "artifact_schema": "seraph.screenshot_observation_digest.v1",
                        "window_start": "2026-06-20T10:00:00+00:00",
                        "window_end": "2026-06-20T10:30:00+00:00",
                        "observation_count": 2,
                        "observation_ids": ["obs-a", "obs-b"],
                    },
                    sort_keys=True,
                ),
                observed_at=datetime(2026, 6, 20, 10, 30, tzinfo=timezone.utc),
            )
        )

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(return_value=_llm_response("Report judgment written by LLM")),
    ) as completion:
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    assert report["screenshot_digests"]["count"] == 1
    assert report["goal_alignment"] == []
    prompt = completion.await_args.kwargs["messages"][0]["content"]
    assert "Seraph screenshot digest report implementation was visible." in prompt
    assert report["body"] == "Report judgment written by LLM"


@pytest.mark.asyncio
async def test_build_report_leaves_goal_judgment_to_llm(async_db):
    from src.db.models import Goal, MemoryEpisode, MemoryEpisodeType
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    async with async_db() as db:
        db.add(
            Goal(
                id="goal-blocked",
                path="/",
                level="daily",
                title="Finish Seraph screenshot reports",
                domain="productivity",
                status="active",
                sort_order=0,
            )
        )
        db.add(
            MemoryEpisode(
                episode_type=MemoryEpisodeType.observer,
                source_tool_name="screenshot_observation_digest",
                summary="Screenshot digest window",
                content="\n".join(
                    [
                        "Screenshot digest 2026-06-20T11:00:00+00:00 to 2026-06-20T11:30:00+00:00",
                        "Observations: 1",
                        "LLM-authored notes:",
                        "- Seraph screenshot reports were being tested.",
                        "- Seraph screenshot reports are blocked by local VLM provider failure.",
                    ]
                ),
                metadata_json=json.dumps(
                    {
                        "artifact_schema": "seraph.screenshot_observation_digest.v1",
                        "window_start": "2026-06-20T11:00:00+00:00",
                        "window_end": "2026-06-20T11:30:00+00:00",
                        "observation_count": 1,
                        "observation_ids": ["obs-c"],
                    },
                    sort_keys=True,
                ),
                observed_at=datetime(2026, 6, 20, 11, 30, tzinfo=timezone.utc),
            )
        )

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(return_value=_llm_response("Blocked goal written by LLM")),
    ) as completion:
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    assert report["goal_alignment"] == []
    assert "local VLM provider failure" in completion.await_args.kwargs["messages"][0]["content"]
    assert report["body"] == "Blocked goal written by LLM"


@pytest.mark.asyncio
async def test_build_report_does_not_treat_named_external_provider_as_source(async_db):
    from src.db.models import ScreenObservation
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report

    details = [
        "capture_artifacts:"
        + json.dumps(
            {
                "provider": "external_recorder",
                "image_path": "/tmp/screenshots/capture.png",
                "image_bytes": 67,
                "file_format": "png",
            },
            sort_keys=True,
        )
    ]
    async with async_db() as db:
        db.add(
            ScreenObservation(
                timestamp=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
                app_name="External Recorder",
                window_title="capture.png",
                activity_type="screen",
                summary="Producer-specific observation.",
                details_json=json.dumps(details),
            )
        )

    with patch.object(settings, "user_timezone", "UTC"), patch(
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        new=AsyncMock(return_value=_llm_response("External provider LLM report")),
    ):
        report = await build_end_of_day_goal_report(date(2026, 6, 20))

    assert report["summary"]["source_observations"] == {"observer_daemon": 1}
    assert "external_recorder" not in report["summary"]["source_observations"]
    assert report["summary"]["screenshot_samples"] == []
    assert report["body"] == "External provider LLM report"


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
        "analysis_provider": "llm:test",
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
    assert payload["report"]["analysis_provider"] == "llm:test"
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
        "analysis_provider": "llm:test",
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
        result = await deliver_report_email(
            {
                "date": "2026-06-20",
                "timezone": "UTC",
                "body": "hello",
                "summary": {
                    "total_tracked_minutes": 42,
                    "switch_count": 7,
                    "total_observations": 12,
                },
                "screenshot_digests": {"count": 2},
                "goal_alignment": [{"status": "aligned", "needle_movement": "pushed"}],
                "analysis_provider": "llm:test",
            }
        )

    assert result.status == "sent"
    assert result.recipient_hash == _recipient_hash("user@example.com")
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("user", "pass")
    smtp.send_message.assert_called_once()
    sent_message = smtp.send_message.call_args.args[0]
    assert sent_message.get_body(preferencelist=("plain",)).get_content().strip() == "hello"
    html_body = sent_message.get_body(preferencelist=("html",)).get_content()
    assert "End-of-day report" in html_body
    assert "42m" in html_body
    assert "Raw screenshots are not attached" in html_body


@pytest.mark.asyncio
async def test_email_delivery_uses_resend_template_when_configured():
    from src.scheduler.jobs.end_of_day_goal_report import _recipient_hash, deliver_report_email

    captured_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content.decode("utf-8")))
        assert request.headers["authorization"] == "Bearer re_test"
        return httpx.Response(200, json={"id": "email_123"})

    async_client_cls = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    with patch.object(settings, "email_reports_enabled", True), patch.object(
        settings, "email_reports_preview_required", False
    ), patch.object(settings, "email_reports_to", "user@example.com"), patch.object(
        settings, "email_reports_from", "Seraph <reports@example.com>"
    ), patch.object(settings, "email_reports_to_allowlist", "user@example.com"), patch.object(
        settings, "resend_api_key", "re_test"
    ), patch.object(settings, "resend_template_id", "tpl_seraph_daily"), patch.object(
        settings, "resend_api_url", "https://api.resend.test/emails"
    ), patch(
        "src.scheduler.jobs.end_of_day_goal_report.httpx.AsyncClient",
        lambda **kwargs: async_client_cls(transport=transport, **kwargs),
    ):
        result = await deliver_report_email(
            {
                "date": "2026-06-20",
                "timezone": "UTC",
                "body": "hello from template",
                "summary": {
                    "total_tracked_minutes": 42,
                    "switch_count": 7,
                    "total_observations": 12,
                },
                "screenshot_digests": {"count": 2},
                "goal_alignment": [{"status": "aligned", "needle_movement": "pushed"}],
                "analysis_provider": "llm:test",
            }
        )

    assert result.status == "sent"
    assert result.recipient_hash == _recipient_hash("user@example.com")
    assert result.provider_receipt == "email_123"
    assert captured_payload["template"] == {
        "id": "tpl_seraph_daily",
        "variables": {
            "REPORT_DATE": "2026-06-20",
            "TIMEZONE": "UTC",
            "ANALYSIS_PROVIDER": "llm:test",
            "TOTAL_TRACKED_MINUTES": 42,
            "CONTEXT_SWITCHES": 7,
            "SCREEN_OBSERVATIONS": 12,
            "SCREENSHOT_DIGESTS": 2,
            "REPORT_BODY": "hello from template",
        },
    }
    assert "html" not in captured_payload
    assert "text" not in captured_payload


@pytest.mark.asyncio
async def test_email_delivery_resend_template_missing_key_logs_skipped():
    from src.scheduler.jobs.end_of_day_goal_report import _recipient_hash, deliver_report_email

    with patch.object(settings, "email_reports_enabled", True), patch.object(
        settings, "email_reports_preview_required", False
    ), patch.object(settings, "email_reports_to", "user@example.com"), patch.object(
        settings, "email_reports_from", "Seraph <reports@example.com>"
    ), patch.object(settings, "email_reports_to_allowlist", "user@example.com"), patch.object(
        settings, "resend_api_key", ""
    ), patch.object(settings, "resend_template_id", "tpl_seraph_daily"), patch(
        "src.scheduler.jobs.end_of_day_goal_report.log_integration_event",
        new=AsyncMock(),
    ) as log_event:
        result = await deliver_report_email(
            {
                "date": "2026-06-20",
                "timezone": "UTC",
                "body": "hello from template",
                "summary": {
                    "total_tracked_minutes": 42,
                    "switch_count": 7,
                    "total_observations": 12,
                },
                "screenshot_digests": {"count": 2},
                "goal_alignment": [{"status": "aligned", "needle_movement": "pushed"}],
                "analysis_provider": "llm:test",
            }
        )

    assert result.status == "skipped"
    assert result.reason == "resend_api_key_missing"
    assert result.recipient_hash == _recipient_hash("user@example.com")
    log_event.assert_awaited_once_with(
        integration_type="email_report",
        name="resend_template",
        outcome="skipped",
        details={
            "reason": "resend_api_key_missing",
            "recipient_hash": _recipient_hash("user@example.com"),
            "provider_receipt": None,
        },
    )


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
                "analysis_provider": "llm:test",
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
                "analysis_provider": "llm:test",
                "artifact_schema": "seraph.end_of_day_goal_report.v1",
            }
        ),
    ):
        result = await run_manual_end_of_day_goal_report(send_email=True, preview_acknowledged=False)

    assert result["action"] == "manual-send"
    assert result["email"]["status"] == "preview_required"
    assert result["email"]["reason"] == "operator_preview_required"
