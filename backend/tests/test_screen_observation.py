"""Tests for screen observation repository and model."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio

from src.db.models import ScreenObservation
from src.observer.screen_repository import ScreenObservationRepository


class TestScreenObservationRepository:
    @pytest.mark.asyncio
    async def test_create_observation(self, async_db):
        repo = ScreenObservationRepository()
        obs = await repo.create(
            app_name="VS Code",
            window_title="main.py",
            activity_type="coding",
            project="seraph",
            summary="Editing Python file",
        )
        assert obs.app_name == "VS Code"
        assert obs.activity_type == "coding"
        assert obs.project == "seraph"
        assert obs.blocked is False

    @pytest.mark.asyncio
    async def test_create_blocked_observation(self, async_db):
        repo = ScreenObservationRepository()
        obs = await repo.create(
            app_name="1Password",
            blocked=True,
        )
        assert obs.app_name == "1Password"
        assert obs.blocked is True
        assert obs.activity_type == "other"

    @pytest.mark.asyncio
    async def test_backfill_duration(self, async_db):
        repo = ScreenObservationRepository()
        now = datetime.now(timezone.utc)

        # Create first observation
        obs1 = await repo.create(
            app_name="VS Code",
            activity_type="coding",
            timestamp=now - timedelta(minutes=10),
        )

        # Create second observation — should backfill duration on first
        obs2 = await repo.create(
            app_name="Safari",
            activity_type="browsing",
            timestamp=now,
        )

        # Re-read obs1 from DB to check duration
        async with async_db() as db:
            from sqlmodel import select
            result = await db.execute(
                select(ScreenObservation).where(ScreenObservation.id == obs1.id)
            )
            refreshed = result.scalar_one()

        assert refreshed.duration_s is not None
        assert refreshed.duration_s == 600  # 10 minutes

    @pytest.mark.asyncio
    async def test_daily_summary_empty(self, async_db):
        repo = ScreenObservationRepository()
        summary = await repo.get_daily_summary(date.today())
        assert summary["total_observations"] == 0

    @pytest.mark.asyncio
    async def test_daily_summary_with_data(self, async_db):
        repo = ScreenObservationRepository()
        now = datetime.now(timezone.utc)
        today = now.date()
        start = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)

        # Create a few observations
        await repo.create(
            app_name="VS Code", activity_type="coding",
            project="seraph", timestamp=start,
        )
        await repo.create(
            app_name="Safari", activity_type="browsing",
            timestamp=start + timedelta(minutes=30),
        )
        await repo.create(
            app_name="VS Code", activity_type="coding",
            project="seraph", timestamp=start + timedelta(minutes=45),
        )

        summary = await repo.get_daily_summary(today)
        assert summary["total_observations"] == 3
        assert summary["switch_count"] == 3
        assert "coding" in summary["by_activity"]
        assert "browsing" in summary["by_activity"]

    @pytest.mark.asyncio
    async def test_daily_summary_excludes_blocked(self, async_db):
        repo = ScreenObservationRepository()
        now = datetime.now(timezone.utc)
        today = now.date()
        start = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)

        await repo.create(
            app_name="VS Code", activity_type="coding", timestamp=start,
        )
        await repo.create(
            app_name="1Password", blocked=True, timestamp=start + timedelta(minutes=5),
        )

        summary = await repo.get_daily_summary(today)
        # Only 1 non-blocked observation
        assert summary["total_observations"] == 1

    @pytest.mark.asyncio
    async def test_weekly_summary(self, async_db):
        repo = ScreenObservationRepository()
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        start = datetime(
            week_start.year, week_start.month, week_start.day, 9, 0, tzinfo=timezone.utc
        )

        # Create observations on two different days
        await repo.create(
            app_name="VS Code", activity_type="coding",
            project="seraph", timestamp=start,
        )
        await repo.create(
            app_name="Safari", activity_type="browsing",
            timestamp=start + timedelta(days=1, minutes=30),
        )

        summary = await repo.get_weekly_summary(week_start)
        assert summary["total_observations"] == 2
        assert len(summary["daily_breakdown"]) == 7

    @pytest.mark.asyncio
    async def test_cleanup_old(self, async_db):
        repo = ScreenObservationRepository()
        now = datetime.now(timezone.utc)

        # Create an old observation (100 days ago)
        await repo.create(
            app_name="Old App",
            timestamp=now - timedelta(days=100),
        )
        # Create a recent one
        await repo.create(
            app_name="New App",
            timestamp=now,
        )

        deleted = await repo.cleanup_old(retention_days=90)
        assert deleted == 1

        # Verify only the recent one remains
        summary = await repo.get_daily_summary(now.date())
        assert summary["total_observations"] == 1

    @pytest.mark.asyncio
    async def test_details_json_round_trip(self, async_db):
        repo = ScreenObservationRepository()
        details = ["file: main.py", "branch: feature/test"]
        obs = await repo.create(
            app_name="VS Code",
            details=details,
        )
        assert obs.details_json is not None
        import json
        assert json.loads(obs.details_json) == details
