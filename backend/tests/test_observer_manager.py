import time
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from src.observer.context import CurrentContext
from src.observer.manager import ContextManager


class TestContextManagerDefaults:
    def test_default_context(self):
        mgr = ContextManager()
        ctx = mgr.get_context()
        assert ctx.time_of_day == "unknown"
        assert ctx.is_working_hours is False
        assert ctx.upcoming_events == []
        assert ctx.active_goals_summary == ""
        assert ctx.last_interaction is None
        assert ctx.active_window is None

    def test_to_dict_has_all_fields(self):
        mgr = ContextManager()
        d = mgr.get_context().to_dict()
        assert "time_of_day" in d
        assert "is_working_hours" in d
        assert "upcoming_events" in d
        assert "active_window" in d


class TestContextManagerRefresh:
    @pytest.mark.asyncio
    async def test_refresh_populates_time(self):
        mgr = ContextManager()

        with patch("src.observer.sources.time_source.gather_time", return_value={
            "time_of_day": "morning",
            "day_of_week": "Monday",
            "is_working_hours": True,
        }), \
             patch("src.observer.sources.calendar_source.gather_calendar", new_callable=AsyncMock, return_value={
                 "upcoming_events": [], "current_event": None
             }), \
             patch("src.observer.sources.git_source.gather_git", return_value=None), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": ""
             }):
            ctx = await mgr.refresh()

        assert ctx.time_of_day == "morning"
        assert ctx.day_of_week == "Monday"
        assert ctx.is_working_hours is True

    @pytest.mark.asyncio
    async def test_refresh_preserves_screen_context(self):
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", "Editing Python")

        with patch("src.observer.sources.time_source.gather_time", return_value={
            "time_of_day": "afternoon", "day_of_week": "Tuesday", "is_working_hours": True,
        }), \
             patch("src.observer.sources.calendar_source.gather_calendar", new_callable=AsyncMock, return_value={
                 "upcoming_events": [], "current_event": None
             }), \
             patch("src.observer.sources.git_source.gather_git", return_value=None), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": ""
             }):
            ctx = await mgr.refresh()

        assert ctx.active_window == "VS Code"
        assert ctx.screen_context == "Editing Python"

    @pytest.mark.asyncio
    async def test_refresh_preserves_heartbeat(self):
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", None)
        ts = mgr.get_context().last_daemon_post

        with patch("src.observer.sources.time_source.gather_time", return_value={
            "time_of_day": "afternoon", "day_of_week": "Tuesday", "is_working_hours": True,
        }), \
             patch("src.observer.sources.calendar_source.gather_calendar", new_callable=AsyncMock, return_value={
                 "upcoming_events": [], "current_event": None
             }), \
             patch("src.observer.sources.git_source.gather_git", return_value=None), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": ""
             }):
            ctx = await mgr.refresh()

        assert ctx.last_daemon_post == ts

    @pytest.mark.asyncio
    async def test_survives_source_failure(self):
        mgr = ContextManager()

        with patch("src.observer.sources.time_source.gather_time", side_effect=RuntimeError("boom")), \
             patch("src.observer.sources.calendar_source.gather_calendar", new_callable=AsyncMock, return_value={
                 "upcoming_events": [], "current_event": None
             }), \
             patch("src.observer.sources.git_source.gather_git", return_value=None), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": ""
             }):
            ctx = await mgr.refresh()

        # Time defaults preserved, other sources still work
        assert ctx.time_of_day == "unknown"
        assert ctx.upcoming_events == []


class TestContextManagerUpdates:
    def test_update_last_interaction(self):
        mgr = ContextManager()
        assert mgr.get_context().last_interaction is None
        mgr.update_last_interaction()
        assert mgr.get_context().last_interaction is not None
        delta = datetime.now(timezone.utc) - mgr.get_context().last_interaction
        assert delta.total_seconds() < 2

    def test_update_screen_context(self):
        mgr = ContextManager()
        mgr.update_screen_context("Safari", "Reading docs")
        ctx = mgr.get_context()
        assert ctx.active_window == "Safari"
        assert ctx.screen_context == "Reading docs"

    def test_update_screen_context_partial_preserves_fields(self):
        """None means 'don't touch' — partial updates don't clobber each other."""
        mgr = ContextManager()
        mgr.update_screen_context("Safari", "Reading docs")
        mgr.update_screen_context(None, None)
        ctx = mgr.get_context()
        assert ctx.active_window == "Safari"
        assert ctx.screen_context == "Reading docs"

    def test_update_screen_context_partial_window_only(self):
        mgr = ContextManager()
        mgr.update_screen_context("Safari", "Reading docs")
        mgr.update_screen_context("VS Code", None)
        ctx = mgr.get_context()
        assert ctx.active_window == "VS Code"
        assert ctx.screen_context == "Reading docs"

    def test_update_screen_context_partial_screen_only(self):
        mgr = ContextManager()
        mgr.update_screen_context("Safari", "Reading docs")
        mgr.update_screen_context(None, "New OCR text")
        ctx = mgr.get_context()
        assert ctx.active_window == "Safari"
        assert ctx.screen_context == "New OCR text"

    def test_update_screen_context_records_heartbeat(self):
        """Every update_screen_context call records a heartbeat timestamp."""
        mgr = ContextManager()
        assert mgr.get_context().last_daemon_post is None
        before = time.time()
        mgr.update_screen_context("Terminal", None)
        after = time.time()
        ts = mgr.get_context().last_daemon_post
        assert ts is not None
        assert before <= ts <= after

    def test_heartbeat_updates_on_every_post(self):
        """Heartbeat timestamp updates on each call, even partial."""
        mgr = ContextManager()
        mgr.update_screen_context("App1", None)
        ts1 = mgr.get_context().last_daemon_post
        mgr.update_screen_context(None, None)
        ts2 = mgr.get_context().last_daemon_post
        assert ts2 >= ts1


class TestCurrentContextSerialization:
    def test_to_dict_with_interaction(self):
        ctx = CurrentContext(last_interaction=datetime(2025, 6, 2, 10, 0, tzinfo=timezone.utc))
        d = ctx.to_dict()
        assert d["last_interaction"] == "2025-06-02T10:00:00+00:00"

    def test_to_dict_without_interaction(self):
        ctx = CurrentContext()
        d = ctx.to_dict()
        assert d["last_interaction"] is None

    def test_to_prompt_block_basic(self):
        ctx = CurrentContext(time_of_day="morning", day_of_week="Monday", is_working_hours=True)
        block = ctx.to_prompt_block()
        assert "morning" in block
        assert "Monday" in block
        assert "Working hours: yes" in block

    def test_to_prompt_block_with_events(self):
        ctx = CurrentContext(
            upcoming_events=[{"summary": "Standup", "start": "10:00"}],
            current_event="Focus Time",
        )
        block = ctx.to_prompt_block()
        assert "Standup" in block
        assert "Focus Time" in block

    def test_to_prompt_block_with_git(self):
        ctx = CurrentContext(recent_git_activity=[{"message": "commit: fix"}, {"message": "commit: add"}])
        block = ctx.to_prompt_block()
        assert "2 commits" in block
