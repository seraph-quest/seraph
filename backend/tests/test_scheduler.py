import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.models.schemas import WSResponse
from src.scheduler.connection_manager import ConnectionManager


# ── ConnectionManager ──────────────────────────────────────


class TestConnectionManager:
    def test_connect_and_disconnect(self):
        mgr = ConnectionManager()
        ws = MagicMock()
        mgr.connect(ws)
        assert mgr.active_count == 1
        mgr.disconnect(ws)
        assert mgr.active_count == 0

    def test_disconnect_unknown_is_noop(self):
        mgr = ConnectionManager()
        ws = MagicMock()
        mgr.disconnect(ws)  # should not raise
        assert mgr.active_count == 0

    def test_multiple_connections(self):
        mgr = ConnectionManager()
        ws1, ws2, ws3 = MagicMock(), MagicMock(), MagicMock()
        mgr.connect(ws1)
        mgr.connect(ws2)
        mgr.connect(ws3)
        assert mgr.active_count == 3
        mgr.disconnect(ws2)
        assert mgr.active_count == 2

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self):
        mgr = ConnectionManager()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        mgr.connect(ws1)
        mgr.connect(ws2)

        msg = WSResponse(type="proactive", content="hello")
        await mgr.broadcast(msg)

        expected_payload = msg.model_dump_json()
        ws1.send_text.assert_called_once_with(expected_payload)
        ws2.send_text.assert_called_once_with(expected_payload)

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_connections(self):
        mgr = ConnectionManager()
        alive = AsyncMock()
        dead = AsyncMock()
        dead.send_text.side_effect = RuntimeError("connection closed")
        mgr.connect(alive)
        mgr.connect(dead)
        assert mgr.active_count == 2

        msg = WSResponse(type="ambient", content="tick", state="idle")
        await mgr.broadcast(msg)

        alive.send_text.assert_called_once()
        assert mgr.active_count == 1

    @pytest.mark.asyncio
    async def test_broadcast_no_connections(self):
        mgr = ConnectionManager()
        msg = WSResponse(type="proactive", content="nobody home")
        await mgr.broadcast(msg)  # should not raise


# ── Scheduler engine ───────────────────────────────────────


class TestSchedulerEngine:
    def test_disabled_returns_none(self):
        with patch("src.scheduler.engine.settings") as mock_settings:
            mock_settings.scheduler_enabled = False
            from src.scheduler.engine import init_scheduler
            result = init_scheduler()
            assert result is None

    @pytest.mark.asyncio
    async def test_enabled_starts_and_registers_jobs(self):
        with patch("src.scheduler.engine.settings") as mock_settings:
            mock_settings.scheduler_enabled = True
            mock_settings.memory_consolidation_interval_min = 30
            mock_settings.goal_check_interval_hours = 4
            mock_settings.calendar_scan_interval_min = 15
            mock_settings.strategist_interval_min = 15
            mock_settings.morning_briefing_hour = 8
            mock_settings.evening_review_hour = 21
            mock_settings.user_timezone = "UTC"

            from src.scheduler.engine import init_scheduler, shutdown_scheduler
            scheduler = init_scheduler()
            try:
                assert scheduler is not None
                assert scheduler.running
                job_ids = {j.id for j in scheduler.get_jobs()}
                assert job_ids == {
                    "memory_consolidation",
                    "goal_check",
                    "calendar_scan",
                    "strategist_tick",
                    "daily_briefing",
                    "evening_review",
                }
            finally:
                shutdown_scheduler()

    def test_shutdown_when_not_running(self):
        from src.scheduler.engine import shutdown_scheduler
        # Should not raise even if nothing is running
        shutdown_scheduler()


# ── Memory consolidation job ───────────────────────────────


class TestMemoryConsolidationJob:
    @pytest.mark.asyncio
    async def test_consolidates_recent_sessions(self, async_db):
        from src.db.models import Session, Message
        from src.scheduler.jobs.memory_consolidation import run_memory_consolidation

        # Create a session with a recent message
        async with async_db() as db:
            session = Session(id="test-session-1", title="Test")
            db.add(session)
            msg = Message(session_id="test-session-1", role="user", content="hello world")
            db.add(msg)

        with patch(
            "src.scheduler.jobs.memory_consolidation.consolidate_session",
            new_callable=AsyncMock,
        ) as mock_consolidate:
            await run_memory_consolidation()
            mock_consolidate.assert_called_once_with("test-session-1")

    @pytest.mark.asyncio
    async def test_skips_old_sessions(self, async_db):
        from datetime import datetime, timedelta, timezone
        from src.db.models import Session
        from src.scheduler.jobs.memory_consolidation import run_memory_consolidation

        # Create a session updated >1 hour ago
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        async with async_db() as db:
            session = Session(id="old-session", title="Old", updated_at=old_time)
            db.add(session)

        with patch(
            "src.scheduler.jobs.memory_consolidation.consolidate_session",
            new_callable=AsyncMock,
        ) as mock_consolidate:
            await run_memory_consolidation()
            mock_consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_on_individual_failure(self, async_db):
        from src.db.models import Session, Message
        from src.scheduler.jobs.memory_consolidation import run_memory_consolidation

        # Create two recent sessions
        async with async_db() as db:
            for i in range(2):
                session = Session(id=f"session-{i}", title=f"Test {i}")
                db.add(session)
                msg = Message(session_id=f"session-{i}", role="user", content="hi")
                db.add(msg)

        call_count = 0

        async def fail_first(session_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("consolidation error")

        with patch(
            "src.scheduler.jobs.memory_consolidation.consolidate_session",
            side_effect=fail_first,
        ):
            await run_memory_consolidation()
            # Should have attempted both despite first failing
            assert call_count == 2
