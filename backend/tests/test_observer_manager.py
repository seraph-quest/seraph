import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from config.settings import settings
from src.extensions.state import save_extension_state_payload
from src.audit.repository import audit_repository
from src.db.models import MemoryEpisodeType
from src.memory.repository import memory_repository
from src.observer.context import CurrentContext
from src.observer.manager import ContextManager, _active_observer_definitions


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
    def test_active_observer_definitions_can_disable_all_packaged_sources(self, tmp_path):
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        original_workspace_dir = settings.workspace_dir
        settings.workspace_dir = str(workspace_dir)
        try:
            save_extension_state_payload(
                {
                    "extensions": {
                        "seraph.core-observer-sources": {
                            "connector_state": {
                                "observers/definitions/time.yaml": {"enabled": False},
                                "observers/definitions/calendar.yaml": {"enabled": False},
                                "observers/definitions/git.yaml": {"enabled": False},
                                "observers/definitions/goals.yaml": {"enabled": False},
                            }
                        }
                    }
                }
            )
            assert _active_observer_definitions() == []
        finally:
            settings.workspace_dir = original_workspace_dir

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
    async def test_refresh_uses_extension_backed_observer_source_selection(self):
        mgr = ContextManager()
        calendar_mock = AsyncMock(return_value={"upcoming_events": [], "current_event": None})
        git_mock = MagicMock(return_value={"recent_git_activity": [{"message": "should not run"}]})
        goals_mock = AsyncMock(return_value={"active_goals_summary": "should not run"})

        with patch("src.observer.manager._active_observer_definitions", return_value=[("time", "time")]), \
             patch("src.observer.sources.time_source.gather_time", return_value={
                 "time_of_day": "morning",
                 "day_of_week": "Monday",
                 "is_working_hours": True,
             }), \
             patch("src.observer.sources.calendar_source.gather_calendar", calendar_mock), \
             patch("src.observer.sources.git_source.gather_git", git_mock), \
             patch("src.observer.sources.goal_source.gather_goals", goals_mock):
            ctx = await mgr.refresh()

        assert ctx.time_of_day == "morning"
        calendar_mock.assert_not_called()
        git_mock.assert_not_called()
        goals_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_with_no_active_packaged_sources_marks_data_quality_stale(self):
        mgr = ContextManager()

        with patch("src.observer.manager._active_observer_definitions", return_value=[]), \
             patch("src.observer.sources.time_source.gather_time") as time_mock, \
             patch("src.observer.sources.calendar_source.gather_calendar", new_callable=AsyncMock) as calendar_mock, \
             patch("src.observer.sources.git_source.gather_git") as git_mock, \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock) as goals_mock:
            ctx = await mgr.refresh()

        assert ctx.data_quality == "stale"
        time_mock.assert_not_called()
        calendar_mock.assert_not_called()
        git_mock.assert_not_called()
        goals_mock.assert_not_called()

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
    async def test_refresh_derives_salience_confidence_and_interruption_cost(self):
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", "Editing roadmap")

        soon = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()

        with patch("src.observer.sources.time_source.gather_time", return_value={
            "time_of_day": "afternoon", "day_of_week": "Tuesday", "is_working_hours": True,
        }), \
             patch("src.observer.sources.calendar_source.gather_calendar", new_callable=AsyncMock, return_value={
                 "upcoming_events": [{"summary": "Design review", "start": soon}], "current_event": None
             }), \
             patch("src.observer.sources.git_source.gather_git", return_value={
                 "recent_git_activity": [{"msg": "ship salience model"}]
             }), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": "Ship observer salience"
             }), \
             patch("src.observer.manager.user_state_machine.derive_state", return_value="available"):
            ctx = await mgr.refresh()

        assert ctx.observer_confidence == "grounded"
        assert ctx.salience_level == "high"
        assert ctx.salience_reason == "upcoming_event"
        assert ctx.interruption_cost == "high"

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

    @pytest.mark.asyncio
    async def test_refresh_logs_runtime_audit_event(self, async_db):
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
            await mgr.refresh()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_succeeded"
            and event["tool_name"] == "observer_context_refresh"
            and event["details"]["data_quality"] == "good"
            and event["details"]["sources_ok"] == 4
            and event["details"]["observer_confidence"] == "grounded"
            and event["details"]["salience_level"] == "low"
            and event["details"]["salience_reason"] == "background"
            and event["details"]["interruption_cost"] == "low"
            and event["details"]["triggered_bundle_delivery"] is False
            for event in events
        )

    @pytest.mark.asyncio
    async def test_refresh_logs_degraded_runtime_audit_details(self, async_db):
        mgr = ContextManager()

        with patch("src.observer.sources.time_source.gather_time", side_effect=RuntimeError("boom")), \
             patch("src.observer.sources.calendar_source.gather_calendar", new_callable=AsyncMock, return_value={
                 "upcoming_events": [], "current_event": None
             }), \
             patch("src.observer.sources.git_source.gather_git", return_value=None), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": ""
             }):
            await mgr.refresh()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_succeeded"
            and event["tool_name"] == "observer_context_refresh"
            and event["details"]["data_quality"] == "degraded"
            and event["details"]["sources_ok"] == 3
            for event in events
        )

    @pytest.mark.asyncio
    async def test_refresh_records_observer_transition_episodes(self, async_db):
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", "Editing Atlas rollout notes")

        with patch("src.observer.manager._active_observer_definitions", return_value=[("time", "time"), ("goals", "goals")]), \
             patch("src.observer.sources.time_source.gather_time", return_value={
                 "time_of_day": "afternoon",
                 "day_of_week": "Tuesday",
                 "is_working_hours": True,
             }), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": "Ship Atlas launch"
             }), \
             patch("src.observer.manager.user_state_machine.derive_state", return_value="deep_work"), \
             patch("src.observer.screen_repository.screen_observation_repo.get_recent_projects", new_callable=AsyncMock, return_value=["Atlas"]):
            ctx = await mgr.refresh()

        episodes = await memory_repository.list_episodes(
            episode_types=(MemoryEpisodeType.observer,),
            limit=10,
        )

        assert ctx.active_project == "Atlas"
        assert len(episodes) == 3
        assert [json.loads(item.metadata_json)["observer_transition"] for item in episodes] == [
            "activity",
            "focus",
            "project",
        ]

    @pytest.mark.asyncio
    async def test_refresh_logs_observer_transition_details(self, async_db):
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", "Editing Atlas rollout notes")

        with patch("src.observer.manager._active_observer_definitions", return_value=[("time", "time"), ("goals", "goals")]), \
             patch("src.observer.sources.time_source.gather_time", return_value={
                 "time_of_day": "afternoon",
                 "day_of_week": "Tuesday",
                 "is_working_hours": True,
             }), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": "Ship Atlas launch"
             }), \
             patch("src.observer.manager.user_state_machine.derive_state", return_value="deep_work"), \
             patch("src.observer.screen_repository.screen_observation_repo.get_recent_projects", new_callable=AsyncMock, return_value=["Atlas"]):
            await mgr.refresh()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_succeeded"
            and event["tool_name"] == "observer_context_refresh"
            and event["details"]["active_project"] == "Atlas"
            and event["details"]["observer_transition_count"] == 3
            for event in events
        )

    @pytest.mark.asyncio
    async def test_refresh_does_not_duplicate_observer_transition_episodes_without_change(self, async_db):
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", "Editing Atlas rollout notes")

        with patch("src.observer.manager._active_observer_definitions", return_value=[("time", "time"), ("goals", "goals")]), \
             patch("src.observer.sources.time_source.gather_time", return_value={
                 "time_of_day": "afternoon",
                 "day_of_week": "Tuesday",
                 "is_working_hours": True,
             }), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": "Ship Atlas launch"
             }), \
             patch("src.observer.manager.user_state_machine.derive_state", return_value="deep_work"), \
             patch("src.observer.screen_repository.screen_observation_repo.get_recent_projects", new_callable=AsyncMock, return_value=["Atlas"]):
            await mgr.refresh()
        with patch("src.observer.manager._active_observer_definitions", return_value=[("time", "time"), ("goals", "goals")]), \
             patch("src.observer.sources.time_source.gather_time", return_value={
                 "time_of_day": "afternoon",
                 "day_of_week": "Tuesday",
                 "is_working_hours": True,
             }), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": "Ship Atlas launch"
             }), \
             patch("src.observer.manager.user_state_machine.derive_state", return_value="deep_work"), \
             patch("src.observer.screen_repository.screen_observation_repo.get_recent_projects", new_callable=AsyncMock, return_value=["Atlas"]):
            await mgr.refresh()

        episodes = await memory_repository.list_episodes(
            episode_types=(MemoryEpisodeType.observer,),
            limit=10,
        )

        assert len(episodes) == 3

    @pytest.mark.asyncio
    async def test_refresh_survives_observer_episode_write_failure(self, async_db):
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", "Editing Atlas rollout notes")

        with patch("src.observer.manager._active_observer_definitions", return_value=[("time", "time"), ("goals", "goals")]), \
             patch("src.observer.sources.time_source.gather_time", return_value={
                 "time_of_day": "afternoon",
                 "day_of_week": "Tuesday",
                 "is_working_hours": True,
             }), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": "Ship Atlas launch"
             }), \
             patch("src.observer.manager.user_state_machine.derive_state", return_value="deep_work"), \
             patch("src.observer.screen_repository.screen_observation_repo.get_recent_projects", new_callable=AsyncMock, return_value=["Atlas"]), \
             patch("src.memory.observer_episodes.memory_repository.create_episode_batch", new_callable=AsyncMock, side_effect=RuntimeError("episode write failed")):
            ctx = await mgr.refresh()

        episodes = await memory_repository.list_episodes(
            episode_types=(MemoryEpisodeType.observer,),
            limit=10,
        )

        assert ctx.active_project == "Atlas"
        assert episodes == []

    @pytest.mark.asyncio
    async def test_refresh_records_project_clear_transition(self, async_db):
        mgr = ContextManager()
        mgr._context.active_project = "Atlas"
        mgr._context.active_goals_summary = "Ship Atlas launch"
        mgr._context.active_window = "VS Code"
        mgr._context.user_state = "deep_work"
        mgr.update_screen_context("VS Code", "Reviewing launch wrap-up")

        with patch("src.observer.manager._active_observer_definitions", return_value=[("time", "time"), ("goals", "goals")]), \
             patch("src.observer.sources.time_source.gather_time", return_value={
                 "time_of_day": "evening",
                 "day_of_week": "Tuesday",
                 "is_working_hours": False,
             }), \
             patch("src.observer.sources.goal_source.gather_goals", new_callable=AsyncMock, return_value={
                 "active_goals_summary": ""
             }), \
             patch("src.observer.manager.user_state_machine.derive_state", return_value="available"), \
             patch("src.observer.screen_repository.screen_observation_repo.get_recent_projects", new_callable=AsyncMock, return_value=[]):
            ctx = await mgr.refresh()

        episodes = await memory_repository.list_episodes(
            episode_types=(MemoryEpisodeType.observer,),
            limit=10,
        )

        assert ctx.active_project is None
        assert any(
            json.loads(item.metadata_json)["observer_transition"] == "project"
            and json.loads(item.metadata_json)["previous_project"] == "Atlas"
            and json.loads(item.metadata_json)["current_project"] is None
            for item in episodes
        )

    @pytest.mark.asyncio
    async def test_refresh_logs_failure_runtime_audit_event(self, async_db):
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
             }), \
             patch("src.observer.manager.user_state_machine.derive_state", side_effect=RuntimeError("derive failed")):
            with pytest.raises(RuntimeError, match="derive failed"):
                await mgr.refresh()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_failed"
            and event["tool_name"] == "observer_context_refresh"
            and event["details"]["error"] == "derive failed"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_refresh_transition_logs_bundle_trigger(self, async_db):
        mgr = ContextManager()
        mgr._context.user_state = "deep_work"

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
             }), \
             patch.object(mgr, "_deliver_bundle", new_callable=AsyncMock):
            await mgr.refresh()
            await asyncio.sleep(0)

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_succeeded"
            and event["tool_name"] == "observer_context_refresh"
            and event["details"]["triggered_bundle_delivery"] is True
            and event["details"]["previous_user_state"] == "deep_work"
            and event["details"]["new_user_state"] == "transitioning"
            for event in events
        )


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


class TestQueuedBundleAudit:
    @pytest.mark.asyncio
    async def test_deliver_bundle_logs_success_runtime_audit(self, async_db):
        mgr = ContextManager()

        with patch("src.observer.delivery.deliver_queued_bundle", new_callable=AsyncMock, return_value=2):
            await mgr._deliver_bundle(0)

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_succeeded"
            and event["tool_name"] == "observer_queued_bundle_delivery"
            and event["details"]["requested_epoch"] == 0
            and event["details"]["delivered_count"] == 2
            for event in events
        )

    @pytest.mark.asyncio
    async def test_deliver_bundle_logs_skipped_runtime_audit(self, async_db):
        mgr = ContextManager()
        mgr._transition_epoch = 2

        await mgr._deliver_bundle(1)

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_skipped"
            and event["tool_name"] == "observer_queued_bundle_delivery"
            and event["details"]["requested_epoch"] == 1
            and event["details"]["current_epoch"] == 2
            for event in events
        )

    @pytest.mark.asyncio
    async def test_deliver_bundle_logs_failure_runtime_audit(self, async_db):
        mgr = ContextManager()

        with patch("src.observer.delivery.deliver_queued_bundle", new_callable=AsyncMock, side_effect=RuntimeError("ws down")):
            await mgr._deliver_bundle(0)

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_failed"
            and event["tool_name"] == "observer_queued_bundle_delivery"
            and event["details"]["error"] == "ws down"
            for event in events
        )


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
