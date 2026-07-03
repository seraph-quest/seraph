"""Tests for settings API — GET/PUT interruption mode."""

import asyncio
import json
import stat
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from config.settings import settings
from src.api.settings import _screen_artifact_summary
from src.db.models import UserProfile
from src.observer.context import CurrentContext


@pytest.fixture(autouse=True)
def _clear_settings_summary_cache():
    from src.api import settings as settings_api

    settings_api._SUMMARY_CACHE.clear()
    yield
    settings_api._SUMMARY_CACHE.clear()


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
    with (
        patch.object(settings, "report_archive_dir", str(tmp_path / "reports")),
        patch.object(settings, "end_of_day_report_enabled", True),
        patch.object(settings, "end_of_day_report_hour", 21),
        patch.object(settings, "end_of_day_report_llm_enabled", False),
        patch.object(settings, "email_reports_enabled", True),
        patch.object(settings, "email_reports_preview_required", True),
        patch.object(settings, "smtp_host", "smtp.example.test"),
        patch.object(settings, "smtp_password", "secret-password"),
        patch.object(settings, "email_reports_to", "user@example.test"),
        patch.object(settings, "email_reports_from", ""),
        patch.object(settings, "email_reports_to_allowlist", "hash-value"),
        patch.object(settings, "workspace_dir", str(tmp_path / "workspace")),
        patch.object(settings, "local_llm_api_base", ""),
        patch.object(settings, "local_vlm_base_url", ""),
        patch.object(settings, "seraph_vlm_base_url", ""),
        patch.object(settings, "screen_analysis_provider", ""),
    ):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert data["screen"]["analysis_enabled"] is True
    assert data["screen"]["provider"] == ""
    assert data["screen"]["model"]
    assert "capture_mode" not in data["screen"]
    assert "daemon_status" not in data["screen"]
    assert "archive_dir" not in data["screen"]
    assert "preservation_enabled" not in data["screen"]
    assert data["reports"]["archive_dir"].endswith("/reports")
    assert data["reports"]["exists"] is True
    assert data["reports"]["writable"] is True
    assert data["reports"]["creation_error"] is None
    assert stat.S_IMODE((tmp_path / "reports").stat().st_mode) == 0o700
    assert data["reports"]["analysis_provider"] == "llm_disabled"
    assert data["reports"]["receipt_count"] == 0
    assert data["local_runtime"]["gateway_configured"] is False
    assert {profile["id"] for profile in data["local_runtime"]["profiles"]} >= {
        "screenshot_fast",
        "report_thinking",
        "chat_thinking",
    }
    assert data["local_runtime"]["profile_proof"]["status"] == "missing"
    assert data["local_runtime"]["profile_proof"]["safe_for_single_backend_profile_routing"] is False
    assert data["email"]["enabled"] is True
    assert data["email"]["smtp_configured"] is True
    assert data["email"]["sender_configured"] is False
    assert "secret-password" not in str(data)


@pytest.mark.asyncio
async def test_screen_analysis_settings_exposes_env_screenshot_folder_and_local_vlm(client, tmp_path, monkeypatch):
    screenshot_root = tmp_path / "captures"
    screenshot_root.mkdir()
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(screenshot_root))
    with (
        patch.object(settings, "workspace_dir", str(tmp_path / "workspace")),
        patch.object(settings, "screen_analysis_provider", "local-vlm"),
        patch.object(settings, "local_vlm_model", "gemma-local"),
    ):
        resp = await client.get("/api/settings/screen-analysis")

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "local-vlm"
    assert data["model"] == "gemma-local"
    assert data["screenshot_folder"] == str(screenshot_root.resolve())
    assert data["screenshot_folder_source"] == "SERAPH_SCREENSHOT_FOLDER"


@pytest.mark.asyncio
async def test_artifact_storage_exposes_gpu_vlm_runtime_without_secret(client, tmp_path, monkeypatch):
    screenshot_root = tmp_path / "captures"
    screenshot_root.mkdir()
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(screenshot_root))
    with (
        patch.object(settings, "workspace_dir", str(tmp_path / "workspace")),
        patch.object(settings, "screen_analysis_provider", "local-vlm"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", ""),
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_backend_url", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "seraph_vlm_api_key", "secret-token"),
    ):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    runtime = data["local_runtime"]["vlm_runtime"]
    assert runtime["mode"] == "gpu-server"
    assert runtime["base_url"] == "http://192.168.1.26:8001"
    assert runtime["backend_url"] == "http://192.168.1.26:8000/v1"
    assert runtime["chat_api_base"] == "http://192.168.1.26:8001/v1"
    assert runtime["chat_completion_endpoint"] == "http://192.168.1.26:8001/v1/chat/completions"
    assert runtime["chat_health_endpoint"] == "http://192.168.1.26:8001/health/chat"
    assert runtime["queue_status_endpoint"] == "http://192.168.1.26:8001/queue/status"
    assert runtime["api_key_configured"] is True
    assert "live_probe" not in runtime
    assert data["screenshot_folder"]["analysis"]["runtime"] == runtime
    assert data["local_runtime"]["gateway_configured"] is True
    assert data["local_runtime"]["vlm_base_url_configured"] is True
    assert "secret-token" not in str(data)


@pytest.mark.asyncio
async def test_artifact_storage_does_not_wait_for_live_vlm_probe(client, tmp_path, monkeypatch):
    screenshot_root = tmp_path / "captures"
    screenshot_root.mkdir()
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(screenshot_root))

    async def _blocked_probe(*, timeout_seconds: float = 0.75):
        raise AssertionError("artifact storage metadata must not run live VLM probes inline")

    with (
        patch("src.api.settings.probe_effective_vlm_runtime", _blocked_probe),
        patch.object(settings, "workspace_dir", str(tmp_path / "workspace")),
        patch.object(settings, "screen_analysis_provider", "local-vlm"),
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_backend_url", "http://192.168.1.26:8000/v1"),
    ):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert data["screenshot_folder"]["path"] == str(screenshot_root)
    assert data["screenshot_folder"]["analysis"]["runtime"]["configured"] is True
    assert "live_probe" not in data["screenshot_folder"]["analysis"]["runtime"]


@pytest.mark.asyncio
async def test_artifact_storage_returns_env_folder_when_pipeline_summary_times_out(
    client,
    tmp_path,
    monkeypatch,
):
    from src.observer import screenshot_folder_source

    screenshot_root = tmp_path / "captures"
    screenshot_root.mkdir()
    (screenshot_root / "capture.png").write_bytes(b"png bytes")
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(screenshot_root))
    monkeypatch.setitem(screenshot_folder_source._PERSISTENCE_STATS, "db_lock_retries", 7)
    monkeypatch.setitem(screenshot_folder_source._PERSISTENCE_STATS, "selection_db_lock_failures", 2)

    async def slow_pipeline_summary(root=None):
        await asyncio.sleep(2)
        return {}

    with (
        patch.object(settings, "workspace_dir", str(tmp_path / "workspace")),
        patch.object(settings, "screen_analysis_provider", "local-vlm"),
        patch("src.api.settings._SCREENSHOT_PIPELINE_SUMMARY_TIMEOUT_S", 0.01),
        patch("src.api.settings._screenshot_folder_pipeline_summary", slow_pipeline_summary),
    ):
        started_at = time.monotonic()
        resp = await client.get("/api/settings/artifact-storage")
        elapsed = time.monotonic() - started_at

    assert resp.status_code == 200
    assert elapsed < 0.5
    data = resp.json()
    assert data["screenshot_folder"]["path"] == str(screenshot_root.resolve())
    assert data["screenshot_folder"]["path_source"] == "SERAPH_SCREENSHOT_FOLDER"
    assert data["screenshot_folder"]["image_count"] == 1
    assert data["screenshot_folder"]["analysis"]["latest_failure"] == "analysis metadata timed out"
    assert data["screenshot_folder"]["analysis"]["persistence"]["db_lock_retries"] == 7
    assert data["screenshot_folder"]["analysis"]["persistence"]["selection_db_lock_failures"] == 2


@pytest.mark.asyncio
async def test_screenshot_folder_summary_singleflights_slow_refresh(tmp_path, monkeypatch):
    from src.api import settings as settings_api

    screenshot_root = tmp_path / "screenshots"
    screenshot_root.mkdir()
    calls = 0

    def slow_summary(root):
        nonlocal calls
        calls += 1
        time.sleep(0.15)
        return {
            "status": "ready",
            "image_count": 9,
            "last_image_at": "2026-07-03T20:00:00+00:00",
            "last_image_at_source": "file_mtime",
            "exists": True,
            "readable": True,
        }

    monkeypatch.setattr(settings_api, "_SCREENSHOT_FOLDER_SUMMARY_TIMEOUT_S", 0.01)
    monkeypatch.setattr(settings_api, "_screenshot_folder_summary", slow_summary)

    started_at = time.monotonic()
    first, second = await asyncio.gather(
        settings_api._screenshot_folder_summary_fast(screenshot_root),
        settings_api._screenshot_folder_summary_fast(screenshot_root),
    )
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.1
    assert calls == 1
    assert first["status"] == "summary_timeout"
    assert second["status"] == "summary_timeout"

    await asyncio.sleep(0.2)
    cached = await settings_api._screenshot_folder_summary_fast(screenshot_root)
    assert cached["status"] == "ready"
    assert cached["image_count"] == 9
    assert calls == 1


def test_screenshot_folder_analysis_candidate_ignores_stale_folder_root(tmp_path):
    from src.observer.screenshot_folder_source import _analysis_candidate_ready

    current_root = tmp_path / "screenshots"
    old_root = tmp_path / "screenshots" / "captures"
    details = [
        "capture_artifacts:"
        + json.dumps(
            {
                "provider": "screenshot_folder",
                "screenshot_folder": str(old_root),
                "image_path": str(old_root / "old.png"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "screenshot_analysis_status:"
        + json.dumps(
            {"status": "pending"},
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]

    assert _analysis_candidate_ready(details, current_root=current_root.resolve()) is False


@pytest.mark.asyncio
async def test_artifact_storage_prefers_seraph_screen_archive_env(client, tmp_path, monkeypatch):
    preferred = tmp_path / "seraph-screen"
    fallback = tmp_path / "fallback-screen"
    monkeypatch.setenv("SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR", str(preferred))
    with (
        patch.object(settings, "workspace_dir", str(tmp_path / "workspace")),
        patch.object(settings, "screen_capture_archive_dir", str(fallback)),
    ):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert "archive_dir" not in data["screen"]
    assert str(preferred) not in str(data["screen"])
    assert str(fallback) not in str(data["screen"])


@pytest.mark.asyncio
async def test_artifact_storage_exposes_screenshot_folder_status(client, async_db, tmp_path, monkeypatch):
    from src.db.models import MemoryEpisode, MemoryEpisodeType, ScreenObservation
    from src.observer import screenshot_folder_source

    screenshot_root = tmp_path / "screenshots"
    screenshot_root.mkdir()
    (screenshot_root / "capture-1.png").write_bytes(b"png bytes")
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(screenshot_root))
    monkeypatch.setitem(screenshot_folder_source._PERSISTENCE_STATS, "db_lock_retries", 3)
    monkeypatch.setitem(screenshot_folder_source._PERSISTENCE_STATS, "persistence_db_lock_failures", 1)
    observed_at = datetime(2026, 6, 30, 9, 5, tzinfo=timezone.utc)
    async with async_db() as db:
        db.add(
            ScreenObservation(
                timestamp=observed_at,
                app_name="Screenshot Folder",
                window_title="capture-1.png",
                activity_type="screen",
                summary="Screenshot image ingested.",
                details_json=json.dumps(
                    [
                        "capture_artifacts:"
                        + json.dumps(
                            {
                                "provider": "screenshot_folder",
                                "image_path": str(screenshot_root / "capture-1.png"),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "screenshot_visual_run:"
                        + json.dumps(
                            {
                                "schema_version": "seraph.screenshot_visual_dedupe.v1",
                                "representative_path": str(screenshot_root / "capture-1.png"),
                                "first_seen": observed_at.isoformat(),
                                "last_seen": datetime(2026, 6, 30, 9, 10, tzinfo=timezone.utc).isoformat(),
                                "suppressed_count": 4,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "screenshot_analysis_status:"
                        + json.dumps(
                            {
                                "status": "failed",
                                "provider": "local-vlm",
                                "model": "gemma",
                                "reason": "provider unavailable",
                                "recorded_at": observed_at.isoformat(),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ]
                ),
            )
        )
        old_root = screenshot_root / "captures"
        db.add(
            ScreenObservation(
                timestamp=observed_at,
                app_name="Screenshot Folder",
                window_title="old-root.png",
                activity_type="screen",
                summary="Old root screenshot.",
                details_json=json.dumps(
                    [
                        "capture_artifacts:"
                        + json.dumps(
                            {
                                "provider": "screenshot_folder",
                                "screenshot_folder": str(old_root),
                                "image_path": str(old_root / "old-root.png"),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "screenshot_analysis_status:"
                        + json.dumps(
                            {
                                "status": "pending",
                                "reason": "queued_for_analysis",
                                "recorded_at": observed_at.isoformat(),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ]
                ),
            )
        )
        db.add(
            ScreenObservation(
                timestamp=observed_at,
                app_name="Screenshot Folder",
                window_title="missing.png",
                activity_type="screen",
                summary="Missing screenshot.",
                details_json=json.dumps(
                    [
                        "capture_artifacts:"
                        + json.dumps(
                            {
                                "provider": "screenshot_folder",
                                "screenshot_folder": str(screenshot_root),
                                "image_path": str(screenshot_root / "missing.png"),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "screenshot_analysis_status:"
                        + json.dumps(
                            {
                                "status": "source_missing",
                                "reason": "image file not found",
                                "recorded_at": observed_at.isoformat(),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ]
                ),
            )
        )
        db.add(
            MemoryEpisode(
                episode_type=MemoryEpisodeType.observer,
                source_tool_name="screenshot_observation_digest",
                summary="Screenshot digest",
                content="Screenshot digest",
                observed_at=datetime(2026, 6, 30, 9, 30, tzinfo=timezone.utc),
            )
        )

    with patch.object(settings, "screenshot_folder_ingest_enabled", True):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert "framekeeper" not in data
    assert data["screenshot_folder"]["provider"] == "screenshot_folder"
    assert data["screenshot_folder"]["path"] == str(screenshot_root)
    assert data["screenshot_folder"]["path_source"] == "SERAPH_SCREENSHOT_FOLDER"
    assert data["screenshot_folder"]["status"] == "ready"
    assert data["screenshot_folder"]["image_count"] == 1
    assert data["screenshot_folder"]["last_image_at_source"] == "file_mtime"
    assert data["screenshot_folder"]["stored_artifacts"] == ["image"]
    assert data["screenshot_folder"]["analysis"]["observation_count"] == 1
    assert data["screenshot_folder"]["analysis"]["total_observation_count"] == 3
    assert data["screenshot_folder"]["analysis"]["analysis_failures"] == 1
    assert data["screenshot_folder"]["analysis"]["analysis_backlog"] == 0
    assert data["screenshot_folder"]["analysis"]["stale_count"] == 2
    assert data["screenshot_folder"]["analysis"]["source_missing_count"] == 1
    assert data["screenshot_folder"]["analysis"]["stale_root_count"] == 1
    assert data["screenshot_folder"]["analysis"]["visual_run_count"] == 1
    assert data["screenshot_folder"]["analysis"]["visual_suppressed_count"] == 4
    assert data["screenshot_folder"]["analysis"]["persistence"]["db_lock_retries"] == 3
    assert data["screenshot_folder"]["analysis"]["persistence"]["persistence_db_lock_failures"] == 1
    assert data["screenshot_folder"]["analysis"]["folder_image_count"] == 1
    assert data["screenshot_folder"]["analysis"]["ingested_count"] == 1
    assert data["screenshot_folder"]["analysis"]["remaining_to_ingest"] == 0
    assert data["screenshot_folder"]["analysis"]["processed_count"] == 0
    assert data["screenshot_folder"]["analysis"]["remaining_to_analyze"] == 0
    assert data["screenshot_folder"]["analysis"]["folder_remaining_to_analyze"] == 1
    assert data["screenshot_folder"]["analysis"]["analysis_status"]["failed"] == 1
    assert data["screenshot_folder"]["analysis"]["analysis_status"]["source_missing"] == 1
    assert data["screenshot_folder"]["analysis"]["analysis_status"]["stale_root"] == 1
    assert data["screenshot_folder"]["analysis"]["latest_failure"] == "provider unavailable"
    assert data["screenshot_folder"]["analysis"]["digest_count"] == 1
    assert data["screenshot_folder"]["analysis"]["latest_digest_at"] == "2026-06-30T09:30:00+00:00"
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
async def test_clear_stale_screenshot_folder_archives_only_incomplete_rows(client, async_db, tmp_path, monkeypatch):
    from sqlmodel import select

    from src.db.models import ScreenObservation

    screenshot_root = tmp_path / "screenshots"
    screenshot_root.mkdir()
    analyzed_image = screenshot_root / "analyzed.png"
    analyzed_image.write_bytes(b"png bytes")
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(screenshot_root))
    observed_at = datetime(2026, 6, 30, 9, 5, tzinfo=timezone.utc)

    def details(root: str, image_path: str, status: str) -> str:
        return json.dumps(
            [
                "capture_artifacts:"
                + json.dumps(
                    {
                        "provider": "screenshot_folder",
                        "screenshot_folder": root,
                        "image_path": image_path,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "screenshot_analysis_status:"
                + json.dumps(
                    {
                        "status": status,
                        "reason": "image file not found" if status == "source_missing" else "test",
                        "recorded_at": observed_at.isoformat(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )

    async with async_db() as db:
        db.add(
            ScreenObservation(
                id="succeeded-row",
                timestamp=observed_at,
                app_name="Screenshot Folder",
                window_title="analyzed.png",
                activity_type="screen",
                summary="Analyzed.",
                details_json=details(str(screenshot_root), str(screenshot_root / "deleted-after-analysis.png"), "succeeded"),
            )
        )
        db.add(
            ScreenObservation(
                id="missing-row",
                timestamp=observed_at,
                app_name="Screenshot Folder",
                window_title="missing.png",
                activity_type="screen",
                summary="Missing.",
                details_json=details(str(screenshot_root), str(screenshot_root / "missing.png"), "source_missing"),
            )
        )
        db.add(
            ScreenObservation(
                id="old-root-row",
                timestamp=observed_at,
                app_name="Screenshot Folder",
                window_title="old-root.png",
                activity_type="screen",
                summary="Old root.",
                details_json=details(str(screenshot_root / "captures"), str(screenshot_root / "captures" / "old-root.png"), "pending"),
            )
        )

    resp = await client.post("/api/settings/screen-analysis/screenshot-folder/clear-stale")

    assert resp.status_code == 200
    assert resp.json()["archived"] == 2
    async with async_db() as db:
        result = await db.execute(select(ScreenObservation).order_by(ScreenObservation.id))
        rows = {row.id: row for row in result.scalars().all()}
    assert rows["succeeded-row"].blocked is False
    assert rows["missing-row"].blocked is True
    assert rows["old-root-row"].blocked is True


@pytest.mark.asyncio
async def test_artifact_storage_exposes_latest_local_runtime_profile_proof(client, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    receipts = workspace / "local-runtime-profile-receipts"
    receipts.mkdir(parents=True)
    (receipts / "proof.json").write_text(
        json.dumps(
            {
                "schema_version": "seraph.local_runtime_profiles.proof.v1",
                "sha256": "proof-sha",
                "conclusion": {
                    "per_request_reasoning_control": "failed",
                    "safe_for_single_backend_profile_routing": False,
                    "notes": ["screenshot_fast emitted visible reasoning markers"],
                },
            }
        ),
        encoding="utf-8",
    )

    with (
        patch.object(settings, "workspace_dir", str(workspace)),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_model", "openai/unsloth/gemma"),
    ):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    proof = data["local_runtime"]["profile_proof"]
    assert data["local_runtime"]["gateway_configured"] is True
    assert data["local_runtime"]["model"] == "openai/unsloth/gemma"
    assert proof["status"] == "unsafe"
    assert proof["per_request_reasoning_control"] == "failed"
    assert proof["safe_for_single_backend_profile_routing"] is False
    assert proof["last_receipt_sha256"] == "proof-sha"
    assert "screenshot_fast emitted visible reasoning markers" in proof["notes"]
    assert "latest local runtime profile proof receipt hash did not verify" in proof["notes"]
    assert "latest local runtime profile proof receipt does not match the current profile contract" in proof["notes"]
    assert (
        "latest local runtime profile proof receipt does not match the configured local base URL"
        in proof["notes"]
    )
    assert "latest local runtime profile proof receipt does not match the configured local model" in proof["notes"]


@pytest.mark.asyncio
async def test_artifact_storage_defaults_to_seraph_owned_screenshot_folder(client, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.delenv("SERAPH_SCREENSHOT_FOLDER", raising=False)
    with patch.object(settings, "workspace_dir", str(workspace)):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert data["screenshot_folder"]["path"] == str(workspace / "artifacts" / "screenshot-folder")
    assert data["screenshot_folder"]["path_source"] == "default"
    assert "Framekeeper" not in data["screenshot_folder"]["path"]


@pytest.mark.asyncio
async def test_screen_analysis_settings_persist_and_drive_artifact_storage(client, tmp_path):
    with patch.object(settings, "workspace_dir", str(tmp_path / "workspace")):
        archive = tmp_path / "captures"
        screenshot_root = tmp_path / "screenshots"
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={
                "enabled": True,
                "provider": "local-vlm",
                "model": "unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M",
                "preserve_captures": True,
                "archive_dir": str(archive),
                "screenshot_folder": str(screenshot_root),
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["provider"] == "local-vlm"
        assert data["model"] == "unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M"
        assert data["preserve_captures"] is True
        assert data["archive_dir"] == str(archive)
        assert data["screenshot_folder"] == str(screenshot_root)
        assert "framekeeper_screenshot_folder" not in data
        assert "framekeeper_artifact_root" not in data
        assert data["max_daily_captures"] == 0

        storage = (await client.get("/api/settings/artifact-storage")).json()
        assert storage["screen"]["analysis_enabled"] is True
        assert storage["screen"]["provider"] == "local-vlm"
        assert "archive_dir" not in storage["screen"]
        assert "preservation_enabled" not in storage["screen"]
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
async def test_screenshot_folder_picker_persists_native_selection(client, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    screenshot_root = tmp_path / "picked-screenshots"
    screenshot_root.mkdir()
    monkeypatch.delenv("SERAPH_SCREENSHOT_FOLDER", raising=False)
    with (
        patch.object(settings, "workspace_dir", str(workspace)),
        patch("src.api.settings._choose_screenshot_folder_with_native_dialog", new=AsyncMock(return_value=str(screenshot_root))),
    ):
        resp = await client.post("/api/settings/screen-analysis/screenshot-folder/pick")

    assert resp.status_code == 200
    data = resp.json()
    assert data["screenshot_folder"] == str(screenshot_root.resolve())
    assert data["screenshot_folder_source"] == "screen-analysis-settings"

    saved = json.loads((workspace / "screen-analysis-settings.json").read_text(encoding="utf-8"))
    assert saved["screenshot_folder"] == str(screenshot_root.resolve())


@pytest.mark.asyncio
async def test_screenshot_folder_picker_refuses_env_locked_folder(client, tmp_path, monkeypatch):
    screenshot_root = tmp_path / "env-screenshots"
    screenshot_root.mkdir()
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(screenshot_root))
    resp = await client.post("/api/settings/screen-analysis/screenshot-folder/pick")

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_screen_analysis_settings_reject_public_legacy_framekeeper_artifact_root(client, tmp_path):
    with patch.object(settings, "workspace_dir", str(tmp_path / "workspace")):
        framekeeper_root = tmp_path / "legacy-framekeeper"
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={"framekeeper_artifact_root": str(framekeeper_root)},
        )

        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_screen_analysis_settings_reject_public_legacy_framekeeper_screenshot_folder(client, tmp_path):
    with patch.object(settings, "workspace_dir", str(tmp_path / "workspace")):
        framekeeper_root = tmp_path / "legacy-framekeeper"
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={"framekeeper_screenshot_folder": str(framekeeper_root)},
        )

        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_screen_analysis_settings_ignores_legacy_framekeeper_local_file_keys(client, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    screenshot_root = tmp_path / "legacy-screenshots"
    (workspace / "screen-analysis-settings.json").write_text(
        json.dumps({"framekeeper_artifact_root": str(screenshot_root)}),
        encoding="utf-8",
    )

    with patch.object(settings, "workspace_dir", str(workspace)):
        resp = await client.get("/api/settings/screen-analysis")

    assert resp.status_code == 200
    data = resp.json()
    assert "screenshot_folder" not in data
    assert "framekeeper_screenshot_folder" not in data
    assert "framekeeper_artifact_root" not in data


@pytest.mark.asyncio
async def test_artifact_storage_ignores_legacy_framekeeper_env_keys(client, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    legacy_root = tmp_path / "legacy-screenshots"
    monkeypatch.delenv("SERAPH_SCREENSHOT_FOLDER", raising=False)
    monkeypatch.setenv("SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER", str(legacy_root))
    monkeypatch.setenv("SERAPH_FRAMEKEEPER_ARTIFACT_ROOT", str(legacy_root))

    with patch.object(settings, "workspace_dir", str(workspace)):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert data["screenshot_folder"]["path"] == str(workspace / "artifacts" / "screenshot-folder")
    assert data["screenshot_folder"]["path_source"] == "default"


@pytest.mark.asyncio
async def test_screen_analysis_settings_reject_relative_screenshot_folder(client, tmp_path):
    with patch.object(settings, "workspace_dir", str(tmp_path / "workspace")):
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={"screenshot_folder": "relative/screenshots"},
        )

        assert resp.status_code == 422
        assert "absolute path" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_screen_analysis_settings_reject_broad_screenshot_folder(client, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        patch.object(settings, "workspace_dir", str(workspace)),
        patch("src.observer.screenshot_folder_source.settings.workspace_dir", str(workspace)),
    ):
        resp = await client.put(
            "/api/settings/screen-analysis",
            json={"screenshot_folder": str(workspace)},
        )

        assert resp.status_code == 422
        assert "dedicated image directory" in resp.json()["detail"]


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
