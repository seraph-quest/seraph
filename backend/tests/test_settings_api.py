"""Tests for settings API — GET/PUT interruption mode."""

import json
import stat
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from config.settings import settings
from src.api.settings import _screen_artifact_summary
from src.db.models import UserProfile
from src.observer.context import CurrentContext


@pytest.mark.asyncio
async def test_get_interruption_mode(client):
    # Reset context_manager to a fresh default so the test is time-independent
    fresh = CurrentContext()
    with patch("src.api.settings.context_manager.get_context", return_value=fresh):
        resp = await client.get("/api/settings/interruption-mode")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "balanced"
    assert data["attention_budget_remaining"] == 5
    assert data["user_state"] == "available"


@pytest.mark.asyncio
async def test_put_interruption_mode_focus(client, async_db):
    # Create a profile first
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "focus"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "focus"
    assert data["attention_budget_remaining"] == 0


@pytest.mark.asyncio
async def test_put_interruption_mode_active(client, async_db):
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "active"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "active"
    assert data["attention_budget_remaining"] == 15


@pytest.mark.asyncio
async def test_put_invalid_mode_422(client):
    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "invalid_mode"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_budget_resets_on_mode_change(client, async_db):
    """Changing mode should reset budget to the new mode's default."""
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    # Set to active (budget=15)
    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "active"},
    )
    assert resp.json()["attention_budget_remaining"] == 15

    # Switch to balanced (budget=5)
    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "balanced"},
    )
    assert resp.json()["attention_budget_remaining"] == 5


@pytest.mark.asyncio
async def test_get_reflects_put(client, async_db):
    """GET should reflect the mode set by PUT."""
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "focus"},
    )

    resp = await client.get("/api/settings/interruption-mode")
    assert resp.json()["mode"] == "focus"


@pytest.mark.asyncio
async def test_artifact_storage_settings_exposes_safe_operator_posture(client, tmp_path, monkeypatch):
    monkeypatch.setenv("SERAPH_PRESERVE_SCREEN_CAPTURES", "true")
    with (
        patch.object(settings, "screen_capture_archive_dir", str(tmp_path / "screen")),
        patch.object(settings, "report_archive_dir", str(tmp_path / "reports")),
        patch.object(settings, "end_of_day_report_enabled", True),
        patch.object(settings, "end_of_day_report_hour", 21),
        patch.object(settings, "end_of_day_report_llm_enabled", False),
        patch.object(settings, "email_reports_enabled", True),
        patch.object(settings, "email_reports_preview_required", True),
        patch.object(settings, "smtp_host", "smtp.example.test"),
        patch.object(settings, "smtp_password", "secret-password"),
        patch.object(settings, "email_reports_to", "user@example.test"),
        patch.object(settings, "email_reports_to_allowlist", "hash-value"),
        patch.object(settings, "workspace_dir", str(tmp_path / "workspace")),
        patch("src.api.settings.context_manager.is_daemon_connected", return_value=False),
    ):
        status_file = tmp_path / "workspace" / "daemon-status.json"
        status_file.parent.mkdir(parents=True)
        status_file.write_text(
            json.dumps(
                {
                    "state": "running",
                    "screen_analysis": "capture_error",
                    "capture_ready": False,
                    "last_error": "Grant Screen Recording permission.",
                    "last_error_kind": "screen_capture_permission",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert data["screen"]["analysis_enabled"] is True
    assert data["screen"]["provider"] == "codex-local"
    assert data["screen"]["daemon_connected"] is False
    assert data["screen"]["artifact_count"] == 0
    assert data["screen"]["daemon_alive"] is True
    assert data["screen"]["preservation_enabled"] is True
    assert data["screen"]["budget"]["archive_retention_days"] >= 1
    assert data["screen"]["archive_dir"].endswith("/screen")
    assert data["screen"]["exists"] is True
    assert data["screen"]["writable"] is True
    assert data["screen"]["creation_error"] is None
    assert stat.S_IMODE((tmp_path / "screen").stat().st_mode) == 0o700
    assert data["screen"]["stored_artifacts"] == ["image", "provider_output", "analysis_json"]
    assert data["screen"]["inspection_visibility"] == "localhost_only"
    assert data["screen"]["daemon_status"]["screen_analysis"] == "capture_error"
    assert data["screen"]["daemon_status"]["last_error"] == "Grant Screen Recording permission."
    assert data["screen"]["daemon_status"]["status_source"] == "daemon-status-file"
    assert "status_file" not in data["screen"]["daemon_status"]
    assert data["reports"]["archive_dir"].endswith("/reports")
    assert data["reports"]["exists"] is True
    assert data["reports"]["writable"] is True
    assert data["reports"]["creation_error"] is None
    assert stat.S_IMODE((tmp_path / "reports").stat().st_mode) == 0o700
    assert data["reports"]["analysis_provider"] == "deterministic-local"
    assert data["reports"]["receipt_count"] == 0
    assert data["email"]["enabled"] is True
    assert data["email"]["smtp_configured"] is True
    assert data["email"]["sender_configured"] is False
    assert "secret-password" not in str(data)


@pytest.mark.asyncio
async def test_artifact_storage_prefers_seraph_screen_archive_env(client, tmp_path, monkeypatch):
    preferred = tmp_path / "seraph-screen"
    fallback = tmp_path / "fallback-screen"
    monkeypatch.setenv("SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR", str(preferred))
    with patch.object(settings, "screen_capture_archive_dir", str(fallback)):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert data["screen"]["archive_dir"] == str(preferred)
    assert data["screen"]["archive_dir_source"] == "screen-analysis-settings"
    assert data["screen"]["exists"] is True


@pytest.mark.asyncio
async def test_artifact_storage_exposes_screenshot_folder_status(client, tmp_path, monkeypatch):
    screenshot_root = tmp_path / "screenshots"
    screenshot_root.mkdir()
    (screenshot_root / "capture-1.png").write_bytes(b"png bytes")
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(screenshot_root))

    resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert "framekeeper" not in data
    assert data["screenshot_folder"]["provider"] == "screenshot_folder"
    assert data["screenshot_folder"]["path"] == str(screenshot_root)
    assert data["screenshot_folder"]["path_source"] == "SERAPH_SCREENSHOT_FOLDER"
    assert data["screenshot_folder"]["status"] == "ready"
    assert data["screenshot_folder"]["image_count"] == 1
    assert data["screenshot_folder"]["stored_artifacts"] == ["image"]
    assert data["screenshot_folder"]["auto_ingest_enabled"] is True
    assert data["screenshot_folder"]["auto_ingest_interval_min"] == settings.screenshot_folder_ingest_interval_min
    assert data["screenshot_folder"]["auto_ingest_limit"] == settings.screenshot_folder_ingest_limit
    assert data["screenshot_folder"]["control_env"]["path"] == "SERAPH_SCREENSHOT_FOLDER"
    assert data["screenshot_folder"]["control_env"]["auto_ingest_enabled"] == "SCREENSHOT_FOLDER_INGEST_ENABLED"
    assert data["screenshot_folder"]["exists"] is True
    assert data["screenshot_folder"]["readable"] is True
    assert data["screenshot_folder"]["scan_endpoint"] == "/api/observer/screenshot-folder/scan"
    assert "ingest_endpoint" not in data["screenshot_folder"]


@pytest.mark.asyncio
async def test_screen_analysis_settings_persist_and_drive_artifact_storage(client, tmp_path):
    with patch.object(settings, "workspace_dir", str(tmp_path / "workspace")):
        archive = tmp_path / "captures"
        screenshot_root = tmp_path / "screenshots"
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={
                "enabled": True,
                "provider": "codex-local",
                "model": "gpt-5.5",
                "preserve_captures": True,
                "archive_dir": str(archive),
                "screenshot_folder": str(screenshot_root),
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["provider"] == "codex-local"
        assert data["model"] == "gpt-5.5"
        assert data["preserve_captures"] is True
        assert data["archive_dir"] == str(archive)
        assert data["screenshot_folder"] == str(screenshot_root)
        assert data["framekeeper_screenshot_folder"] == str(screenshot_root)
        assert data["framekeeper_artifact_root"] == str(screenshot_root)
        assert data["max_daily_captures"] == 0

        storage = (await client.get("/api/settings/artifact-storage")).json()
        assert storage["screen"]["analysis_enabled"] is True
        assert storage["screen"]["provider"] == "codex-local"
        assert storage["screen"]["archive_dir"] == str(archive)
        assert storage["screen"]["preservation_enabled"] is True
        assert storage["screenshot_folder"]["path"] == str(screenshot_root)
        assert storage["screenshot_folder"]["path_source"] == "screen-analysis-settings"

        cleared = await client.put(
            "/api/settings/screen-analysis",
            json={"screenshot_folder": ""},
        )
        assert cleared.status_code == 200
        assert "screenshot_folder" not in cleared.json()
        assert "framekeeper_screenshot_folder" not in cleared.json()
        assert "framekeeper_artifact_root" not in cleared.json()


@pytest.mark.asyncio
async def test_screen_analysis_settings_accept_legacy_framekeeper_artifact_root(client, tmp_path):
    with patch.object(settings, "workspace_dir", str(tmp_path / "workspace")):
        framekeeper_root = tmp_path / "legacy-framekeeper"
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={"framekeeper_artifact_root": str(framekeeper_root)},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["screenshot_folder"] == str(framekeeper_root)
        assert data["framekeeper_screenshot_folder"] == str(framekeeper_root)
        assert data["framekeeper_artifact_root"] == str(framekeeper_root)


@pytest.mark.asyncio
async def test_screen_analysis_settings_persist_budget_controls(client, tmp_path):
    with patch.object(settings, "workspace_dir", str(tmp_path / "workspace")):
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={
                "min_seconds_between_captures": 45,
                "max_daily_captures": 200,
                "archive_retention_days": 180,
                "archive_max_mb": 1024,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["min_seconds_between_captures"] == 45
    assert data["max_daily_captures"] == 200
    assert data["archive_retention_days"] == 180
    assert data["archive_max_mb"] == 1024


@pytest.mark.asyncio
async def test_screen_analysis_settings_reject_negative_budget(client, tmp_path):
    with patch.object(settings, "workspace_dir", str(tmp_path / "workspace")):
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={"max_daily_captures": -1},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_screen_analysis_settings_reject_invalid_provider(client, tmp_path):
    with patch.object(settings, "workspace_dir", str(tmp_path / "workspace")):
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={"provider": "not-real"},
        )

    assert resp.status_code == 422


def test_screen_artifact_summary_skips_files_deleted_during_stat(tmp_path, monkeypatch):
    image = tmp_path / "capture.png"
    image.write_bytes(b"not-really-a-png")
    original_stat = type(image).stat

    def flaky_stat(path, *args, **kwargs):
        if path == image:
            raise FileNotFoundError(str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(type(image), "is_file", lambda path: path == image)
    monkeypatch.setattr(type(image), "stat", flaky_stat)

    assert _screen_artifact_summary(tmp_path) == {"artifact_count": 0, "last_artifact_at": None}


@pytest.mark.asyncio
async def test_manual_report_endpoint_returns_safe_preview(client):
    with patch(
        "src.scheduler.jobs.end_of_day_goal_report.run_manual_end_of_day_goal_report",
        new=AsyncMock(
            return_value={
                "status": "ok",
                "action": "manual-preview",
                "report": {"date": "2026-06-20", "body": "Preview body"},
                "email": {"status": "preview_only", "reason": "manual_preview", "recipient_hash": None},
                "receipt": {"receipt_sha256": "abc123", "status": "succeeded"},
            }
        ),
    ) as manual:
        resp = await client.post("/api/settings/end-of-day-report/manual", json={"send_email": False})

    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "manual-preview"
    assert data["email"]["status"] == "preview_only"
    manual.assert_awaited_once()


@pytest.mark.asyncio
async def test_test_email_endpoint_returns_safe_status(client):
    with patch(
        "src.scheduler.jobs.end_of_day_goal_report.send_end_of_day_report_test_email",
        new=AsyncMock(
            return_value={
                "status": "blocked",
                "reason": "recipient_not_allowlisted",
                "recipient_hash": "hash123",
                "receipt": {"receipt_sha256": "def456", "status": "blocked"},
            }
        ),
    ):
        resp = await client.post("/api/settings/end-of-day-report/test-email")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "blocked"
    assert data["recipient_hash"] == "hash123"
    assert "user@example" not in str(data)
