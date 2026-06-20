"""Tests for settings API — GET/PUT interruption mode."""

import pytest
import pytest_asyncio
from unittest.mock import patch

from config.settings import settings
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
    ):
        resp = await client.get("/api/settings/artifact-storage")

    assert resp.status_code == 200
    data = resp.json()
    assert data["screen"]["preservation_enabled"] is True
    assert data["screen"]["archive_dir"].endswith("/screen")
    assert data["screen"]["exists"] is True
    assert data["screen"]["writable"] is True
    assert data["screen"]["creation_error"] is None
    assert data["screen"]["stored_artifacts"] == ["image", "provider_output", "analysis_json"]
    assert data["screen"]["inspection_visibility"] == "localhost_only"
    assert data["reports"]["archive_dir"].endswith("/reports")
    assert data["reports"]["exists"] is True
    assert data["reports"]["writable"] is True
    assert data["reports"]["creation_error"] is None
    assert data["reports"]["analysis_provider"] == "deterministic-local"
    assert data["email"]["enabled"] is True
    assert data["email"]["smtp_configured"] is True
    assert "secret-password" not in str(data)
