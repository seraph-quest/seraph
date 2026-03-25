"""Tests for soul file persistence (src/memory/soul.py)."""

import asyncio
import os
from datetime import timedelta
from unittest.mock import patch

import pytest

import src.memory.soul as soul_mod
from src.profile.service import (
    get_or_create_profile,
    get_profile_snapshot,
    sync_soul_file_to_profile,
    update_profile_soul_section,
)
from src.audit.repository import audit_repository


@pytest.fixture
def soul_dir(tmp_path, monkeypatch):
    """Point the soul module at a temporary directory."""
    soul_path = os.path.join(str(tmp_path), "soul.md")
    monkeypatch.setattr(soul_mod, "_soul_path", soul_path)
    return tmp_path


class TestReadSoul:
    def test_returns_default_when_missing(self, soul_dir, async_db):
        text = soul_mod.read_soul()
        assert "# Guardian Record" in text
        assert "## Identity" in text
        assert "- Name: Unknown" not in text
        assert "- Role: Unknown" not in text

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_empty_result"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "soul_file:soul.md"
        assert events[0]["details"]["operation"] == "read"
        assert events[0]["details"]["used_default"] is True

    def test_returns_file_content(self, soul_dir):
        soul_mod.write_soul("custom content")
        assert soul_mod.read_soul() == "custom content"


class TestWriteSoul:
    def test_creates_file(self, soul_dir, async_db):
        soul_mod.write_soul("hello")
        assert soul_mod.read_soul() == "hello"

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_succeeded"]

        events = asyncio.run(_fetch())
        assert events
        write_event = next(e for e in events if e["details"]["operation"] == "write")
        assert write_event["tool_name"] == "soul_file:soul.md"

    def test_overwrites_file(self, soul_dir):
        soul_mod.write_soul("first")
        soul_mod.write_soul("second")
        assert soul_mod.read_soul() == "second"

    def test_write_failure_logs_runtime_audit(self, soul_dir, async_db):
        with patch("src.memory.soul.open", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError, match="denied"):
                soul_mod.write_soul("blocked")

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_failed"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "soul_file:soul.md"
        assert events[0]["details"]["operation"] == "write"
        assert events[0]["details"]["error"] == "denied"


class TestUpdateSoulSection:
    def test_updates_existing_section(self, soul_dir):
        soul_mod.write_soul("# Soul\n\n## Identity\nOld\n\n## Values\nOld values")
        result = soul_mod.update_soul_section("Identity", "New identity")
        assert "New identity" in result
        assert "Old values" in result

    def test_appends_new_section(self, soul_dir):
        soul_mod.write_soul("# Soul\n\n## Identity\nMe")
        result = soul_mod.update_soul_section("Hobbies", "Coding")
        assert "## Hobbies" in result
        assert "Coding" in result
        assert "## Identity" in result

    def test_preserves_other_sections(self, soul_dir):
        soul_mod.write_soul("# Soul\n\n## Identity\nA\n\n## Values\nB\n\n## Goals\nC")
        result = soul_mod.update_soul_section("Values", "Updated B")
        assert "## Identity" in result
        assert "A" in result
        assert "Updated B" in result
        assert "## Goals" in result
        assert "C" in result


class TestEnsureSoulExists:
    def test_creates_default(self, soul_dir):
        soul_mod.ensure_soul_exists()
        text = soul_mod.read_soul()
        assert "# Guardian Record" in text
        assert "- Name: Unknown" not in text

    def test_does_not_overwrite(self, soul_dir, async_db):
        soul_mod.write_soul("custom")
        soul_mod.ensure_soul_exists()
        assert soul_mod.read_soul() == "custom"

        async def _fetch():
            events = await audit_repository.list_events(limit=10)
            return [e for e in events if e["event_type"] == "integration_skipped"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "soul_file:soul.md"
        assert events[0]["details"]["operation"] == "ensure"
        assert events[0]["details"]["created"] is False


class TestStructuredProjection:
    @pytest.mark.asyncio
    async def test_manual_file_edits_reconcile_into_profile(self, soul_dir, async_db):
        with open(soul_mod._soul_path, "w", encoding="utf-8") as handle:
            handle.write(
                "# Guardian Record\n\n"
                "## Identity\nBuilder\n\n"
                "## Hobbies\nClimbing"
            )

        sections = await sync_soul_file_to_profile()
        profile = await get_profile_snapshot()

        assert sections["Identity"] == "Builder"
        assert sections["Hobbies"] == "Climbing"
        assert profile["soul_sections"]["Hobbies"] == "Climbing"
        assert "## Hobbies" in profile["soul_text"]

    @pytest.mark.asyncio
    async def test_missing_file_reprojects_from_structured_profile(self, soul_dir, async_db):
        await update_profile_soul_section("Goals", "- Ship Batch C")

        os.remove(soul_mod._soul_path)
        assert soul_mod.read_soul_file_text() is None

        sections = await sync_soul_file_to_profile()

        assert sections["Goals"] == "- Ship Batch C"
        assert soul_mod.read_soul_file_text() is not None
        assert "- Ship Batch C" in soul_mod.read_soul()

    @pytest.mark.asyncio
    async def test_stale_projection_does_not_override_newer_profile(self, soul_dir, async_db):
        await update_profile_soul_section("Goals", "- Structured goal")
        profile = await get_or_create_profile()

        stale_text = (
            "# Guardian Record\n\n"
            "## Identity\nBuilder\n\n"
            "## Goals\n- Stale file goal"
        )
        with open(soul_mod._soul_path, "w", encoding="utf-8") as handle:
            handle.write(stale_text)

        stale_ts = (profile.updated_at - timedelta(seconds=10)).timestamp()
        os.utime(soul_mod._soul_path, (stale_ts, stale_ts))

        sections = await sync_soul_file_to_profile()

        assert sections["Goals"] == "- Structured goal"
        assert "- Structured goal" in soul_mod.read_soul()

    @pytest.mark.asyncio
    async def test_projection_write_failure_does_not_drop_structured_profile(
        self, soul_dir, async_db
    ):
        with patch("src.profile.service.write_soul", side_effect=PermissionError("denied")):
            projected = await update_profile_soul_section(
                "Goals",
                "- Persist despite projection failure",
            )

        profile = await get_profile_snapshot()

        assert "- Persist despite projection failure" in projected
        assert (
            profile["soul_sections"]["Goals"]
            == "- Persist despite projection failure"
        )
