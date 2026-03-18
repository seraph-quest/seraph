import time
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
import pytest_asyncio

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
        )

        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/continuity")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["daemon"]["connected"] is True
        assert payload["daemon"]["capture_mode"] == "balanced"
        assert payload["daemon"]["pending_notification_count"] == 1
        assert payload["notifications"][0]["id"] == notification.id
        assert payload["notifications"][0]["intervention_id"] == native_intervention.id
        assert payload["queued_insight_count"] == 1
        assert payload["queued_insights"][0]["intervention_id"] == bundle_intervention.id
        assert payload["queued_insights"][0]["content_excerpt"] == "Bundle this until the next browser check-in."
        assert {item["continuity_surface"] for item in payload["recent_interventions"]} >= {
            "native_notification",
            "bundle_queue",
        }
        assert any(
            item["id"] == native_intervention.id and item["notification_id"] == notification.id
            for item in payload["recent_interventions"]
        )

        await native_notification_queue.clear()

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
