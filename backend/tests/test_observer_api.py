import time
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.api.observer import _observer_presence_surface_payload
from src.audit.repository import audit_repository
from src.guardian.feedback import guardian_feedback_repository
from src.observer.context import CurrentContext
from src.observer.manager import ContextManager
from src.observer.native_notification_queue import native_notification_queue
from src.observer.screen_repository import ScreenObservationRepository


class TestObserverAPI:
    @pytest.mark.asyncio
    async def test_get_state(self, client):
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/state")

        assert resp.status_code == 200
        data = resp.json()
        assert "time_of_day" in data
        assert "is_working_hours" in data
        assert "upcoming_events" in data

    @pytest.mark.asyncio
    async def test_post_screen_context(self, client):
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": "Terminal",
                "screen_context": "Running tests",
            })

        assert resp.status_code == 200
        assert mgr.get_context().active_window == "Terminal"
        assert mgr.get_context().screen_context == "Running tests"

    @pytest.mark.asyncio
    async def test_post_screen_context_logs_runtime_audit(self, async_db, client):
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": "Terminal",
                "screen_context": "Running tests",
            })

        assert resp.status_code == 200

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_received"
            and event["tool_name"] == "observer_daemon:screen_context"
            and event["details"]["has_active_window"] is True
            and event["details"]["has_screen_context"] is True
            and event["details"]["has_observation"] is False
            for event in events
        )

    @pytest.mark.asyncio
    async def test_post_screen_context_null_preserves(self, client):
        """Posting None fields should not overwrite existing values (partial update)."""
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", "Editing")
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": None,
                "screen_context": None,
            })

        assert resp.status_code == 200
        # None means "don't overwrite" — previous values preserved
        assert mgr.get_context().active_window == "VS Code"
        assert mgr.get_context().screen_context == "Editing"

    @pytest.mark.asyncio
    async def test_daemon_status_disconnected(self, client):
        """Daemon status returns disconnected when no POST received."""
        mgr = ContextManager()
        mgr.update_capture_mode("balanced")
        await native_notification_queue.clear()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/daemon-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False
        assert data["last_post"] is None
        assert data["active_window"] is None
        assert data["has_screen_context"] is False
        assert data["capture_mode"] == "balanced"
        assert data["pending_notification_count"] == 0
        assert data["last_native_notification_at"] is None
        assert data["last_native_notification_title"] is None
        assert data["last_native_notification_outcome"] is None

    @pytest.mark.asyncio
    async def test_daemon_status_connected(self, client):
        """Daemon status returns connected after a recent POST."""
        mgr = ContextManager()
        mgr.update_screen_context("VS Code — main.py", None)
        mgr.record_native_notification(title="Seraph desktop shell", outcome="queued_test")
        await native_notification_queue.clear()
        await native_notification_queue.enqueue(
            intervention_id=None,
            title="Seraph desktop shell",
            body="Test pending native notification.",
            intervention_type="test",
            urgency=1,
        )
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/daemon-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is True
        assert data["last_post"] is not None
        assert data["active_window"] == "VS Code — main.py"
        assert data["capture_mode"] == "on_switch"
        assert data["pending_notification_count"] == 1
        assert data["last_native_notification_title"] == "Seraph desktop shell"
        assert data["last_native_notification_outcome"] == "queued_test"
        assert data["last_native_notification_at"] is not None
        await native_notification_queue.clear()

    @pytest.mark.asyncio
    async def test_daemon_status_stale(self, client):
        """Daemon status returns disconnected when last POST is too old."""
        mgr = ContextManager()
        mgr.update_screen_context("Terminal", None)
        # Simulate stale timestamp (60 seconds ago)
        mgr._context.last_daemon_post = time.time() - 60
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/daemon-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False

    @pytest.mark.asyncio
    async def test_post_refresh(self, client):
        mgr = ContextManager()
        mgr.refresh = AsyncMock(return_value=CurrentContext(time_of_day="evening"))
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/refresh")

        assert resp.status_code == 200
        data = resp.json()
        assert data["time_of_day"] == "evening"
        mgr.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_structured_observation(self, async_db, client):
        """Posting with observation field should persist to screen_observations table."""
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": "VS Code — main.py",
                "observation": {
                    "app": "VS Code",
                    "window_title": "main.py",
                    "activity": "coding",
                    "project": "seraph",
                    "summary": "Editing Python file",
                    "details": ["file: main.py"],
                    "blocked": False,
                },
                "switch_timestamp": 1700000000.0,
            })

        assert resp.status_code == 200
        assert mgr.get_context().active_window == "VS Code — main.py"

        # Verify observation was persisted
        from src.db.models import ScreenObservation
        from sqlmodel import select
        async with async_db() as db:
            result = await db.execute(select(ScreenObservation))
            obs = result.scalar_one()

        assert obs.app_name == "VS Code"
        assert obs.activity_type == "coding"
        assert obs.project == "seraph"

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_persisted"
            and event["tool_name"] == "observer_daemon:screen_context"
            and event["details"]["app"] == "VS Code"
            and event["details"]["activity_type"] == "coding"
            and event["details"]["blocked"] is False
            for event in events
        )

    @pytest.mark.asyncio
    async def test_post_blocked_observation(self, async_db, client):
        """Blocked observations should be persisted with blocked=True."""
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": "1Password — Vault",
                "observation": {
                    "app": "1Password",
                    "blocked": True,
                },
            })

        assert resp.status_code == 200

        from src.db.models import ScreenObservation
        from sqlmodel import select
        async with async_db() as db:
            result = await db.execute(select(ScreenObservation))
            obs = result.scalar_one()

        assert obs.app_name == "1Password"
        assert obs.blocked is True
        assert obs.activity_type == "other"

    @pytest.mark.asyncio
    async def test_post_structured_observation_logs_persist_failure_runtime_audit(self, async_db, client):
        mgr = ContextManager()
        mock_repo = MagicMock()
        mock_repo.create = AsyncMock(side_effect=RuntimeError("db down"))
        with (
            patch("src.api.observer.context_manager", mgr),
            patch("src.observer.screen_repository.screen_observation_repo", mock_repo),
        ):
            resp = await client.post("/api/observer/context", json={
                "active_window": "VS Code — main.py",
                "observation": {
                    "app": "VS Code",
                    "activity": "coding",
                    "blocked": False,
                },
            })

        assert resp.status_code == 200

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_persist_failed"
            and event["tool_name"] == "observer_daemon:screen_context"
            and event["details"]["app"] == "VS Code"
            and event["details"]["error"] == "db down"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_legacy_compat_no_observation(self, client):
        """Legacy POST without observation field should still work."""
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": "Terminal",
                "screen_context": "Running tests",
            })

        assert resp.status_code == 200
        assert mgr.get_context().active_window == "Terminal"
        assert mgr.get_context().screen_context == "Running tests"

    @pytest.mark.asyncio
    async def test_get_next_native_notification(self, async_db, client):
        await native_notification_queue.clear()
        notification = await native_notification_queue.enqueue(
            intervention_id=None,
            title="Seraph alert",
            body="Browser is closed; use the daemon path.",
            intervention_type="alert",
            urgency=5,
        )

        resp = await client.get("/api/observer/notifications/next")

        assert resp.status_code == 200
        payload = resp.json()["notification"]
        assert payload["id"] == notification.id
        assert payload["intervention_id"] is None
        assert payload["title"] == "Seraph alert"
        assert await native_notification_queue.count() == 1

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_succeeded"
            and event["tool_name"] == "observer_daemon:notifications"
            and event["details"]["notification_id"] == notification.id
            for event in events
        )

        await native_notification_queue.clear()

    @pytest.mark.asyncio
    async def test_get_next_native_notification_empty(self, async_db, client):
        await native_notification_queue.clear()

        resp = await client.get("/api/observer/notifications/next")

        assert resp.status_code == 200
        assert resp.json()["notification"] is None

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_empty_result"
            and event["tool_name"] == "observer_daemon:notifications"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_list_native_notifications(self, client):
        await native_notification_queue.clear()
        first = await native_notification_queue.enqueue(
            intervention_id=None,
            title="Seraph alert",
            body="Browser is closed; use the daemon path.",
            intervention_type="alert",
            urgency=5,
        )
        second = await native_notification_queue.enqueue(
            intervention_id=None,
            title="Seraph follow-up",
            body="Second desktop notification.",
            intervention_type="advisory",
            urgency=3,
        )

        resp = await client.get("/api/observer/notifications")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["pending_count"] == 2
        assert [item["id"] for item in payload["notifications"]] == [first.id, second.id]
        assert payload["notifications"][0]["title"] == "Seraph alert"
        assert payload["notifications"][1]["title"] == "Seraph follow-up"

        await native_notification_queue.clear()

    @pytest.mark.asyncio
    async def test_observer_continuity_snapshot(self, async_db, client):
        await native_notification_queue.clear()
        mgr = ContextManager()
        mgr.update_screen_context("Arc — Guardian Cockpit", "Reviewing cross-surface continuity.")
        mgr.update_capture_mode("balanced")
        mgr.record_native_notification(title="Seraph alert", outcome="queued")

        native_intervention = await guardian_feedback_repository.create_intervention(
            session_id="session-1",
            message_type="proactive",
            intervention_type="alert",
            urgency=5,
            content="Desktop fallback is active.",
            reasoning="browser_unavailable",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="native_notification",
        )
        bundle_intervention = await guardian_feedback_repository.create_intervention(
            session_id="session-2",
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Bundle this until the next browser check-in.",
            reasoning="high_interruption_cost",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="deep_work",
            interruption_mode="focus",
            policy_action="bundle",
            policy_reason="high_interruption_cost",
            delivery_decision="queue",
            latest_outcome="queued",
        )
        notification = await native_notification_queue.enqueue(
            intervention_id=native_intervention.id,
            title="Seraph alert",
            body="Desktop fallback is active.",
            intervention_type="alert",
            urgency=5,
            session_id="session-1",
            thread_id="session-1",
            thread_source="session",
            continuation_mode="resume_thread",
            resume_message="Continue from this guardian intervention: Desktop fallback is active.",
        )
        await guardian_feedback_repository.update_outcome(
            native_intervention.id,
            latest_outcome="notification_acked",
            transport="native_notification",
            notification_id=notification.id,
        )
        from src.observer.insight_queue import insight_queue

        await insight_queue.enqueue(
            content="Bundle this until the next browser check-in.",
            intervention_type="advisory",
            urgency=3,
            reasoning="high_interruption_cost",
            intervention_id=bundle_intervention.id,
            session_id="session-2",
        )

        with (
            patch("src.api.observer.context_manager", mgr),
            patch(
                "src.api.observer.session_manager.list_sessions",
                AsyncMock(
                    return_value=[
                        {"id": "session-1", "title": "Native thread"},
                        {"id": "session-2", "title": "Bundle thread"},
                    ]
                ),
            ),
        ):
            resp = await client.get("/api/observer/continuity")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["daemon"]["connected"] is True
        assert payload["daemon"]["capture_mode"] == "balanced"
        assert payload["daemon"]["pending_notification_count"] == 1
        assert payload["notifications"][0]["id"] == notification.id
        assert payload["notifications"][0]["intervention_id"] == native_intervention.id
        assert payload["notifications"][0]["thread_id"] == "session-1"
        assert payload["notifications"][0]["thread_label"] == "Native thread"
        assert payload["notifications"][0]["continuation_mode"] == "resume_thread"
        assert payload["queued_insight_count"] == 1
        assert payload["queued_insights"][0]["intervention_id"] == bundle_intervention.id
        assert payload["queued_insights"][0]["content_excerpt"] == "Bundle this until the next browser check-in."
        assert payload["queued_insights"][0]["session_id"] == "session-2"
        assert payload["queued_insights"][0]["thread_id"] == "session-2"
        assert payload["queued_insights"][0]["thread_label"] == "Bundle thread"
        assert payload["queued_insights"][0]["thread_source"] == "session"
        assert payload["queued_insights"][0]["continuation_mode"] == "resume_thread"
        assert payload["summary"]["continuity_health"] == "attention"
        assert payload["summary"]["recommended_focus"] == "Bundle delivery"
        assert payload["summary"]["actionable_thread_count"] == 2
        assert payload["summary"]["degraded_route_count"] >= 1
        assert payload["threads"][0]["thread_id"] == "session-1"
        assert payload["threads"][0]["pending_notification_count"] == 1
        assert payload["threads"][0]["continue_message"] == "Continue from this guardian intervention: Desktop fallback is active."
        assert any(
            item["kind"] == "thread_follow_up"
            and item["thread_id"] == "session-2"
            and item["continue_message"] == "Follow up on this deferred guardian item: Bundle this until the next browser check-in."
            for item in payload["recovery_actions"]
        )
        assert any(
            item["kind"] == "reach_repair"
            and item["route"] == "live_delivery"
            for item in payload["recovery_actions"]
        )
        assert {item["continuity_surface"] for item in payload["recent_interventions"]} >= {
            "native_notification",
            "bundle_queue",
        }
        assert any(
            item["id"] == native_intervention.id and item["notification_id"] == notification.id
            for item in payload["recent_interventions"]
        )
        assert any(
            item["id"] == native_intervention.id
            and item["thread_id"] == "session-1"
            and item["thread_label"] == "Native thread"
            and item["continuation_mode"] == "resume_thread"
            for item in payload["recent_interventions"]
        )
        live_route = next(item for item in payload["reach"]["route_statuses"] if item["route"] == "live_delivery")
        assert live_route["status"] == "fallback_active"
        assert live_route["selected_transport"] == "native_notification"

        await native_notification_queue.clear()

    @pytest.mark.asyncio
    async def test_observer_continuity_recovers_queued_thread_from_intervention_outside_recent_window(self, async_db, client):
        await native_notification_queue.clear()
        target_intervention = await guardian_feedback_repository.create_intervention(
            session_id="session-fallback",
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Recover the older queued thread.",
            reasoning="high_interruption_cost",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="deep_work",
            interruption_mode="focus",
            policy_action="bundle",
            policy_reason="high_interruption_cost",
            delivery_decision="queue",
            latest_outcome="queued",
        )
        recent_intervention = await guardian_feedback_repository.create_intervention(
            session_id="session-recent",
            message_type="proactive",
            intervention_type="alert",
            urgency=5,
            content="Newer intervention still in recent window.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="native_notification",
        )
        from src.observer.insight_queue import insight_queue

        await insight_queue.enqueue(
            content="Recover the older queued thread.",
            intervention_type="advisory",
            urgency=3,
            reasoning="high_interruption_cost",
            intervention_id=target_intervention.id,
            session_id=None,
        )

        with (
            patch(
                "src.guardian.feedback.guardian_feedback_repository.list_recent",
                AsyncMock(return_value=[recent_intervention]),
            ),
            patch(
                "src.api.observer.session_manager.list_sessions",
                AsyncMock(return_value=[{"id": "session-fallback", "title": "Fallback thread"}]),
            ),
        ):
            resp = await client.get("/api/observer/continuity")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["queued_insights"][0]["intervention_id"] == target_intervention.id
        assert payload["queued_insights"][0]["thread_id"] == "session-fallback"
        assert payload["queued_insights"][0]["thread_label"] == "Fallback thread"
        assert payload["queued_insights"][0]["thread_source"] == "intervention_session"
        assert payload["queued_insights"][0]["continuation_mode"] == "resume_thread"

        queued_ids = [item.id for item in await insight_queue.peek_all()]
        if queued_ids:
            await insight_queue.delete_many(queued_ids)
        await native_notification_queue.clear()

    @pytest.mark.asyncio
    async def test_observer_continuity_surfaces_imported_reach_and_source_adapter_attention(self, client):
        await native_notification_queue.clear()
        with (
            patch(
                "src.api.observer._observer_reach_payload",
                return_value={
                    "transport_statuses": [],
                    "route_statuses": [
                        {
                            "route": "live_delivery",
                            "label": "Live delivery",
                            "status": "ready",
                            "summary": "Browser and desktop delivery are available.",
                            "selected_transport": "websocket",
                            "repair_hint": None,
                        }
                    ],
                },
            ),
            patch(
                "src.api.observer._observer_imported_reach_payload",
                return_value={
                    "summary": {
                        "family_count": 1,
                        "active_family_count": 1,
                        "attention_family_count": 1,
                        "approval_family_count": 0,
                    },
                    "families": [
                        {
                            "type": "messaging_connectors",
                            "label": "messaging",
                            "total": 1,
                            "installed": 1,
                            "ready": 0,
                            "attention": 1,
                            "approval": 0,
                            "packages": ["Seraph Relay Pack"],
                        }
                    ],
                },
            ),
            patch(
                "src.api.observer._observer_source_adapter_payload",
                return_value={
                    "summary": {
                        "adapter_count": 1,
                        "ready_adapter_count": 0,
                        "degraded_adapter_count": 1,
                        "authenticated_adapter_count": 1,
                        "authenticated_ready_adapter_count": 0,
                        "authenticated_degraded_adapter_count": 1,
                    },
                    "adapters": [
                        {
                            "name": "github-managed",
                            "provider": "github",
                            "source_kind": "managed_connector",
                            "authenticated": True,
                            "runtime_state": "requires_runtime",
                            "adapter_state": "degraded",
                            "contracts": ["work_items.read", "code_activity.read"],
                            "degraded_reason": "runtime_adapter_missing",
                            "next_best_sources": [{"name": "web_search", "reason": "fallback", "description": "Use public context."}],
                        }
                    ],
                },
            ),
            patch(
                "src.api.observer._observer_presence_surface_payload",
                return_value={
                    "summary": {
                        "surface_count": 2,
                        "active_surface_count": 1,
                        "ready_surface_count": 1,
                        "attention_surface_count": 1,
                    },
                    "surfaces": [
                        {
                            "id": "messaging_connectors:seraph.relay:connectors/messaging/telegram.yaml",
                            "kind": "messaging_connector",
                            "label": "Telegram relay",
                            "package_label": "Seraph Relay Pack",
                            "package_id": "seraph.relay",
                            "status": "requires_config",
                            "active": False,
                            "ready": False,
                            "attention": True,
                            "detail": "Seraph Relay Pack exposes Telegram relay on telegram (requires config).",
                            "repair_hint": "Finish connector configuration in the operator surface before routing follow-through here.",
                            "follow_up_hint": None,
                            "follow_up_prompt": None,
                            "transport": None,
                            "source_type": None,
                        },
                        {
                            "id": "channel_adapters:seraph.native:channels/native.yaml",
                            "kind": "channel_adapter",
                            "label": "native notification channel",
                            "package_label": "Seraph Native Pack",
                            "package_id": "seraph.native",
                            "status": "ready",
                            "active": True,
                            "ready": True,
                            "attention": False,
                            "detail": "Seraph Native Pack exposes native notification channel for native notification delivery (ready).",
                            "repair_hint": None,
                            "follow_up_hint": "Use operator review before routing external follow-through through this surface.",
                            "follow_up_prompt": "Plan guarded follow-through for native notification channel. Confirm the audience, target reference, channel scope, and approval boundaries before acting.",
                            "transport": "native_notification",
                            "source_type": None,
                        },
                    ],
                },
            ),
            patch(
                "src.api.observer.session_manager.list_sessions",
                AsyncMock(return_value=[]),
            ),
        ):
            resp = await client.get("/api/observer/continuity")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["imported_reach"]["summary"]["attention_family_count"] == 1
        assert payload["source_adapters"]["summary"]["degraded_adapter_count"] == 1
        assert payload["presence_surfaces"]["summary"]["surface_count"] == 2
        assert payload["summary"]["presence_surface_count"] == 2
        assert payload["summary"]["attention_presence_surface_count"] == 1
        assert payload["summary"]["continuity_health"] == "attention"
        assert payload["summary"]["primary_surface"] == "source_adapter"
        assert payload["summary"]["recommended_focus"] == "github-managed"
        assert any(
            item["kind"] == "source_adapter_repair"
            and item["surface"] == "source_adapter"
            and item["label"] == "Restore source adapter github-managed"
            for item in payload["recovery_actions"]
        )
        assert any(
            item["kind"] == "imported_reach_attention"
            and item["surface"] == "imported_reach"
            and item["label"] == "Review imported messaging"
            for item in payload["recovery_actions"]
        )
        assert any(
            item["kind"] == "presence_repair"
            and item["surface"] == "presence"
            and item["label"] == "Review presence surface Telegram relay"
            for item in payload["recovery_actions"]
        )
        assert any(
            item["kind"] == "presence_follow_up"
            and item["surface"] == "presence"
            and item["label"] == "Plan follow-up via native notification channel"
            for item in payload["recovery_actions"]
        )

    @pytest.mark.asyncio
    async def test_observer_continuity_tolerates_partial_namespace_items(self, client):
        await native_notification_queue.clear()
        created_at = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)
        created_at_iso = created_at.isoformat()
        with (
            patch(
                "src.observer.native_notification_queue.native_notification_queue.list",
                AsyncMock(
                    return_value=[
                        types.SimpleNamespace(
                            id="notification-1",
                            intervention_id="intervention-1",
                            title="Seraph alert",
                            body="Pick up the saved brief draft.",
                            session_id="thread-1",
                            resume_message="Continue from native notification.",
                            created_at=created_at_iso,
                            intervention_type="advisory",
                            urgency=3,
                        )
                    ]
                ),
            ),
            patch(
                "src.observer.insight_queue.insight_queue.peek_all",
                AsyncMock(
                    return_value=[
                        types.SimpleNamespace(
                            id="queued-1",
                            intervention_id="intervention-1",
                            intervention_type="advisory",
                            content="Bundle the research notes for later.",
                            urgency=2,
                            reasoning="available_capacity",
                            created_at=created_at_iso,
                        )
                    ]
                ),
            ),
            patch(
                "src.guardian.feedback.guardian_feedback_repository.list_recent",
                AsyncMock(
                    return_value=[
                        types.SimpleNamespace(
                            id="intervention-1",
                            intervention_type="advisory",
                            updated_at=created_at,
                        )
                    ]
                ),
            ),
            patch(
                "src.api.observer.session_manager.list_sessions",
                AsyncMock(return_value=[{"id": "thread-1", "title": "Research thread"}]),
            ),
        ):
            resp = await client.get("/api/observer/continuity")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["queued_insights"][0]["thread_id"] is None
        assert payload["recent_interventions"][0]["thread_id"] is None
        assert payload["recent_interventions"][0]["content_excerpt"] == ""
        assert payload["recent_interventions"][0]["policy_action"] == ""
        assert payload["recent_interventions"][0]["latest_outcome"] == ""
        assert payload["recent_interventions"][0]["resume_message"] == "Continue from this guardian intervention."

    @pytest.mark.asyncio
    async def test_ack_native_notification(self, async_db, client):
        await native_notification_queue.clear()
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Ack me",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="native_notification",
        )
        notification = await native_notification_queue.enqueue(
            intervention_id=intervention.id,
            title="Seraph",
            body="Ack me",
            intervention_type="advisory",
            urgency=3,
        )

        resp = await client.post(f"/api/observer/notifications/{notification.id}/ack")

        assert resp.status_code == 200
        assert resp.json() == {"acked": True}
        assert await native_notification_queue.count() == 0
        updated = await guardian_feedback_repository.get(intervention.id)
        assert updated is not None
        assert updated.latest_outcome == "notification_acked"
        assert updated.feedback_type == "acknowledged"

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_acked"
            and event["tool_name"] == "observer_daemon:notifications"
            and event["details"]["notification_id"] == notification.id
            and event["details"]["intervention_id"] == intervention.id
            for event in events
        )

    @pytest.mark.asyncio
    async def test_dismiss_native_notification(self, async_db, client):
        await native_notification_queue.clear()
        mgr = ContextManager()
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Dismiss me",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="native_notification",
        )
        notification = await native_notification_queue.enqueue(
            intervention_id=intervention.id,
            title="Seraph",
            body="Dismiss me",
            intervention_type="advisory",
            urgency=3,
        )

        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post(f"/api/observer/notifications/{notification.id}/dismiss")
            status = await client.get("/api/observer/daemon-status")

        assert resp.status_code == 200
        assert resp.json() == {"dismissed": True}
        assert await native_notification_queue.count() == 0
        updated = await guardian_feedback_repository.get(intervention.id)
        assert updated is not None
        assert updated.latest_outcome == "notification_dismissed"
        assert updated.transport == "native_notification"

        status_payload = status.json()
        assert status_payload["pending_notification_count"] == 0
        assert status_payload["last_native_notification_outcome"] == "dismissed"

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_dismissed"
            and event["tool_name"] == "observer_daemon:notifications"
            and event["details"]["notification_id"] == notification.id
            and event["details"]["source"] == "browser_controls"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_dismiss_all_native_notifications(self, async_db, client):
        await native_notification_queue.clear()
        mgr = ContextManager()
        first = await native_notification_queue.enqueue(
            intervention_id=None,
            title="Seraph one",
            body="First desktop notification.",
            intervention_type="test",
            urgency=1,
        )
        second = await native_notification_queue.enqueue(
            intervention_id=None,
            title="Seraph two",
            body="Second desktop notification.",
            intervention_type="test",
            urgency=1,
        )

        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/notifications/dismiss-all")
            status = await client.get("/api/observer/daemon-status")

        assert resp.status_code == 200
        assert resp.json() == {"dismissed_count": 2}
        assert await native_notification_queue.count() == 0

        status_payload = status.json()
        assert status_payload["pending_notification_count"] == 0
        assert status_payload["last_native_notification_title"] == second.title
        assert status_payload["last_native_notification_outcome"] == "dismissed"

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_dismissed_all"
            and event["tool_name"] == "observer_daemon:notifications"
            and event["details"]["dismissed_count"] == 2
            and event["details"]["source"] == "browser_controls"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_enqueue_test_native_notification(self, async_db, client):
        await native_notification_queue.clear()
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/notifications/test")
            status = await client.get("/api/observer/daemon-status")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["title"] == "Seraph desktop shell"
        assert payload["intervention_type"] == "test"
        assert await native_notification_queue.count() == 1

        status_payload = status.json()
        assert status_payload["pending_notification_count"] == 1
        assert status_payload["last_native_notification_title"] == "Seraph desktop shell"
        assert status_payload["last_native_notification_outcome"] == "queued_test"

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_queued"
            and event["tool_name"] == "observer_daemon:notifications"
            and event["details"]["notification_id"] == payload["id"]
            and event["details"]["source"] == "test_endpoint"
            for event in events
        )

        await native_notification_queue.clear()

    @pytest.mark.asyncio
    async def test_post_intervention_feedback(self, async_db, client):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Stretch and refocus.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="websocket",
        )

        resp = await client.post(
            f"/api/observer/interventions/{intervention.id}/feedback",
            json={"feedback_type": "helpful", "note": "Good timing"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"recorded": True}
        updated = await guardian_feedback_repository.get(intervention.id)
        assert updated is not None
        assert updated.feedback_type == "helpful"
        assert updated.feedback_note == "Good timing"
        assert updated.latest_outcome == "feedback_received"

    @pytest.mark.asyncio
    async def test_post_intervention_feedback_missing_returns_false(self, async_db, client):
        resp = await client.post(
            "/api/observer/interventions/missing-id/feedback",
            json={"feedback_type": "not_helpful"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"recorded": False}

    @pytest.mark.asyncio
    async def test_get_activity_today(self, async_db, client):
        """Activity today endpoint should return daily summary."""
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/activity/today")

        assert resp.status_code == 200
        data = resp.json()
        assert "total_observations" in data

    def test_observer_presence_surface_payload_keeps_disabled_and_planned_surfaces_visible(self):
        with patch(
            "src.extensions.lifecycle.list_extensions",
            return_value={
                "extensions": [
                    {
                        "id": "seraph.presence.pack",
                        "display_name": "Presence Pack",
                        "contributions": [
                            {
                                "type": "messaging_connectors",
                                "name": "Telegram relay",
                                "platform": "telegram",
                                "reference": "connectors/messaging/telegram.yaml",
                                "status": "disabled",
                                "active": False,
                            },
                            {
                                "type": "observer_definitions",
                                "name": "Calendar observer",
                                "source_type": "calendar",
                                "reference": "observers/calendar.yaml",
                                "status": "planned",
                                "active": False,
                            },
                        ],
                    }
                ]
            },
        ):
            payload = _observer_presence_surface_payload()

        assert payload["summary"]["surface_count"] == 2
        assert payload["summary"]["attention_surface_count"] == 2
        assert any(
            item["status"] == "disabled"
            and item["repair_hint"] == "Re-enable this packaged contribution in extension lifecycle state."
            for item in payload["surfaces"]
        )
        assert any(
            item["status"] == "planned"
            and item["repair_hint"] == "Enable the packaged contribution and confirm its runtime prerequisites in the operator surface."
            for item in payload["surfaces"]
        )

    def test_observer_presence_surface_payload_adds_browser_provider_and_node_adapter_inventory(self):
        registry_snapshot = SimpleNamespace(list_contributions=lambda contribution_type: [])
        registry_instance = SimpleNamespace(snapshot=lambda: registry_snapshot)
        with (
            patch(
                "src.extensions.lifecycle.list_extensions",
                return_value={
                    "extensions": [
                        {"id": "seraph.browserbase", "display_name": "Browserbase Pack", "contributions": []},
                        {"id": "seraph.device", "display_name": "Device Pack", "contributions": []},
                    ]
                },
            ),
            patch("src.extensions.state.load_extension_state_payload", return_value={"extensions": {}}),
            patch("src.extensions.state.connector_enabled_overrides", return_value={}),
            patch("src.extensions.registry.default_manifest_roots_for_workspace", return_value=[]),
            patch("src.extensions.registry.ExtensionRegistry", return_value=registry_instance),
            patch(
                "src.extensions.browser_providers.list_browser_provider_inventory",
                return_value=[
                    SimpleNamespace(
                        extension_id="seraph.browserbase",
                        name="browserbase",
                        provider_kind="browserbase",
                        description="Managed browser provider",
                        default_enabled=True,
                        enabled=True,
                        reference="connectors/browser/browserbase.yaml",
                        resolved_path=None,
                        manifest_root_index=0,
                        configured=True,
                        config_keys=("api_key",),
                        requires_network=True,
                        requires_daemon=False,
                        capabilities=("extract", "screenshot"),
                        execution_mode="remote_runtime",
                        runtime_state="ready",
                        selected=True,
                    ),
                    SimpleNamespace(
                        extension_id="seraph.browserbase",
                        name="browserbase-remote",
                        provider_kind="browserbase",
                        description="Managed remote browser provider",
                        default_enabled=True,
                        enabled=True,
                        reference="connectors/browser/browserbase-remote.yaml",
                        resolved_path=None,
                        manifest_root_index=0,
                        configured=True,
                        config_keys=("api_key",),
                        requires_network=True,
                        requires_daemon=False,
                        capabilities=("extract",),
                        execution_mode="local_fallback",
                        runtime_state="staged_local_fallback",
                        selected=False,
                    ),
                ],
            ),
            patch(
                "src.extensions.node_adapters.list_node_adapter_inventory",
                return_value=[
                    SimpleNamespace(
                        extension_id="seraph.device",
                        name="Atlas companion bridge",
                        adapter_kind="companion",
                        description="Companion node bridge",
                        enabled=True,
                        configured=True,
                        config_keys=("endpoint",),
                        capabilities=("observe", "handoff"),
                        requires_network=True,
                        requires_daemon=True,
                        runtime_state="staged_link",
                        reference="connectors/nodes/atlas-companion.yaml",
                    ),
                ],
            ),
        ):
            payload = _observer_presence_surface_payload()

        assert payload["summary"]["surface_count"] == 3
        assert payload["summary"]["ready_surface_count"] == 2
        assert payload["summary"]["attention_surface_count"] == 1

        browser_follow_up = next(
            item for item in payload["surfaces"] if item["id"] == "browser_providers:seraph.browserbase:connectors/browser/browserbase.yaml"
        )
        assert browser_follow_up["kind"] == "browser_provider"
        assert browser_follow_up["selected"] is True
        assert browser_follow_up["provider_kind"] == "browserbase"
        assert browser_follow_up["execution_mode"] == "remote_runtime"
        assert browser_follow_up["follow_up_prompt"].startswith("Plan guarded browser-assisted follow-through via browserbase")

        browser_attention = next(
            item for item in payload["surfaces"] if item["id"] == "browser_providers:seraph.browserbase:connectors/browser/browserbase-remote.yaml"
        )
        assert browser_attention["attention"] is True
        assert browser_attention["repair_hint"] == "Inspect remote browser transport prerequisites before relying on this packaged browser reach."

        node_follow_up = next(
            item for item in payload["surfaces"] if item["id"] == "node_adapters:seraph.device:connectors/nodes/atlas-companion.yaml"
        )
        assert node_follow_up["kind"] == "node_adapter"
        assert node_follow_up["adapter_kind"] == "companion"
        assert node_follow_up["requires_network"] is True
        assert node_follow_up["requires_daemon"] is True
        assert node_follow_up["follow_up_prompt"].startswith("Plan guarded companion follow-through via Atlas companion bridge")
