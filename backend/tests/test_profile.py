"""Tests for profile/onboarding (src/api/profile.py)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.memory.soul import default_soul_sections, render_soul_text
from src.api.profile import get_or_create_profile, mark_onboarding_complete, reset_onboarding


class TestGetOrCreateProfile:
    async def test_creates_new(self, async_db):
        profile = await get_or_create_profile()
        assert profile.id == "singleton"
        assert profile.name == "Unknown"
        assert profile.onboarding_completed is False

    async def test_returns_existing(self, async_db):
        p1 = await get_or_create_profile()
        p2 = await get_or_create_profile()
        assert p1.id == p2.id


class TestOnboarding:
    async def test_mark_complete(self, async_db):
        await get_or_create_profile()
        await mark_onboarding_complete()
        profile = await get_or_create_profile()
        assert profile.onboarding_completed is True

    async def test_reset(self, async_db):
        await get_or_create_profile()
        await mark_onboarding_complete()
        await reset_onboarding()
        profile = await get_or_create_profile()
        assert profile.onboarding_completed is False


class TestProfileEndpoints:
    async def test_get_profile(self, client):
        res = await client.get("/api/user/profile")
        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "Unknown"
        assert "onboarding_completed" in data
        assert "soul_sections" in data
        assert "soul_text" in data
        assert "Identity" in data["soul_sections"]
        assert "## Identity" in data["soul_text"]

    async def test_get_profile_degrades_on_snapshot_timeout(self, client):
        with patch(
            "src.api.profile.get_profile_snapshot",
            AsyncMock(side_effect=asyncio.TimeoutError("private timeout details")),
        ):
            res = await client.get("/api/user/profile")

        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "Unknown"
        assert data["onboarding_completed"] is None
        assert data["soul_sections"] == default_soul_sections()
        assert data["soul_text"] == render_soul_text(default_soul_sections())
        assert data["status"] == "degraded"
        assert data["degraded"] is True
        assert data["degradation_codes"] == ["profile_snapshot_timeout"]
        assert data["claim_boundary"] == "default_profile_only"
        assert "private timeout details" not in res.text

    async def test_get_profile_degrades_on_database_failure(self, client):
        with patch(
            "src.api.profile.get_profile_snapshot",
            AsyncMock(side_effect=SQLAlchemyError("private database details")),
        ):
            res = await client.get("/api/user/profile")

        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "Unknown"
        assert data["onboarding_completed"] is None
        assert data["soul_sections"] == default_soul_sections()
        assert data["soul_text"] == render_soul_text(default_soul_sections())
        assert data["status"] == "degraded"
        assert data["degraded"] is True
        assert data["degradation_codes"] == ["profile_database_unavailable"]
        assert data["claim_boundary"] == "default_profile_only"
        assert "private database details" not in res.text

    async def test_skip_onboarding(self, client):
        res = await client.post("/api/user/onboarding/skip")
        assert res.status_code == 200
        assert res.json()["onboarding_completed"] is True

    async def test_restart_onboarding(self, client):
        await client.post("/api/user/onboarding/skip")
        res = await client.post("/api/user/onboarding/restart")
        assert res.status_code == 200
        assert res.json()["onboarding_completed"] is False
