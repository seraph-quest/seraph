"""Screen observation repository — CRUD and aggregation for activity tracking."""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlmodel import select, func, col

from src.db.engine import get_session
from src.db.models import ScreenObservation

logger = logging.getLogger(__name__)


class ScreenObservationRepository:
    """Async CRUD and aggregation for screen observations."""

    async def create(
        self,
        app_name: str,
        window_title: str = "",
        activity_type: str = "other",
        project: str | None = None,
        summary: str | None = None,
        details: list[str] | None = None,
        blocked: bool = False,
        timestamp: datetime | None = None,
    ) -> ScreenObservation:
        """Insert a new observation and backfill duration on the previous one."""
        now = timestamp or datetime.now(timezone.utc)

        obs = ScreenObservation(
            timestamp=now,
            app_name=app_name,
            window_title=window_title,
            activity_type=activity_type,
            project=project,
            summary=summary,
            details_json=json.dumps(details) if details else None,
            blocked=blocked,
        )

        async with get_session() as db:
            # Backfill duration on the previous observation
            result = await db.execute(
                select(ScreenObservation)
                .where(col(ScreenObservation.duration_s).is_(None))
                .where(col(ScreenObservation.timestamp) < now)
                .order_by(col(ScreenObservation.timestamp).desc())
                .limit(1)
            )
            prev = result.scalar_one_or_none()
            if prev is not None:
                # SQLite strips timezone info; ensure both are tz-aware
                prev_ts = prev.timestamp
                if prev_ts.tzinfo is None:
                    prev_ts = prev_ts.replace(tzinfo=timezone.utc)
                delta = (now - prev_ts).total_seconds()
                prev.duration_s = int(delta)
                db.add(prev)

            db.add(obs)

        return obs

    async def get_daily_summary(self, target_date: date) -> dict[str, Any]:
        """Aggregate observations for a single day."""
        start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)

        async with get_session() as db:
            result = await db.execute(
                select(ScreenObservation)
                .where(col(ScreenObservation.timestamp) >= start)
                .where(col(ScreenObservation.timestamp) < end)
                .where(col(ScreenObservation.blocked) == False)  # noqa: E712
                .order_by(col(ScreenObservation.timestamp))
            )
            observations = list(result.scalars().all())

        if not observations:
            return {"date": target_date.isoformat(), "total_observations": 0}

        # Aggregate by activity type
        by_activity: dict[str, int] = {}
        by_project: dict[str, int] = {}
        by_app: dict[str, int] = {}
        total_tracked_s = 0

        for obs in observations:
            dur = obs.duration_s or 0
            total_tracked_s += dur

            by_activity[obs.activity_type] = by_activity.get(obs.activity_type, 0) + dur

            if obs.project:
                by_project[obs.project] = by_project.get(obs.project, 0) + dur

            by_app[obs.app_name] = by_app.get(obs.app_name, 0) + dur

        # Find longest focus streaks (consecutive same-activity observations)
        streaks = self._compute_streaks(observations)

        return {
            "date": target_date.isoformat(),
            "total_observations": len(observations),
            "total_tracked_minutes": total_tracked_s // 60,
            "switch_count": len(observations),
            "by_activity": dict(sorted(by_activity.items(), key=lambda x: -x[1])),
            "by_project": dict(sorted(by_project.items(), key=lambda x: -x[1])),
            "by_app": dict(sorted(by_app.items(), key=lambda x: -x[1])),
            "longest_streaks": streaks[:3],
        }

    async def get_weekly_summary(self, week_start: date) -> dict[str, Any]:
        """Aggregate observations for a 7-day period starting from week_start."""
        daily_summaries = []
        combined_activity: dict[str, int] = {}
        combined_project: dict[str, int] = {}
        total_observations = 0
        total_minutes = 0

        for i in range(7):
            day = week_start + timedelta(days=i)
            daily = await self.get_daily_summary(day)
            daily_summaries.append(daily)
            total_observations += daily.get("total_observations", 0)
            total_minutes += daily.get("total_tracked_minutes", 0)

            for act, secs in daily.get("by_activity", {}).items():
                combined_activity[act] = combined_activity.get(act, 0) + secs
            for proj, secs in daily.get("by_project", {}).items():
                combined_project[proj] = combined_project.get(proj, 0) + secs

        return {
            "week_start": week_start.isoformat(),
            "week_end": (week_start + timedelta(days=6)).isoformat(),
            "total_observations": total_observations,
            "total_tracked_minutes": total_minutes,
            "by_activity": dict(sorted(combined_activity.items(), key=lambda x: -x[1])),
            "by_project": dict(sorted(combined_project.items(), key=lambda x: -x[1])),
            "daily_breakdown": [
                {
                    "date": d.get("date"),
                    "observations": d.get("total_observations", 0),
                    "tracked_minutes": d.get("total_tracked_minutes", 0),
                }
                for d in daily_summaries
            ],
        }

    async def cleanup_old(self, retention_days: int) -> int:
        """Delete observations older than retention_days. Returns count deleted."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        async with get_session() as db:
            result = await db.execute(
                select(ScreenObservation).where(col(ScreenObservation.timestamp) < cutoff)
            )
            old = list(result.scalars().all())
            for obs in old:
                await db.delete(obs)

        if old:
            logger.info("Cleaned up %d screen observations older than %d days", len(old), retention_days)
        return len(old)

    @staticmethod
    def _compute_streaks(observations: list[ScreenObservation]) -> list[dict]:
        """Find longest consecutive same-activity streaks."""
        if not observations:
            return []

        streaks: list[dict] = []
        current_activity = observations[0].activity_type
        streak_start = observations[0].timestamp
        streak_duration = observations[0].duration_s or 0

        for obs in observations[1:]:
            if obs.activity_type == current_activity:
                streak_duration += obs.duration_s or 0
            else:
                if streak_duration > 0:
                    streaks.append({
                        "activity": current_activity,
                        "duration_minutes": streak_duration // 60,
                        "started_at": streak_start.isoformat(),
                    })
                current_activity = obs.activity_type
                streak_start = obs.timestamp
                streak_duration = obs.duration_s or 0

        # Final streak
        if streak_duration > 0:
            streaks.append({
                "activity": current_activity,
                "duration_minutes": streak_duration // 60,
                "started_at": streak_start.isoformat(),
            })

        return sorted(streaks, key=lambda s: -s["duration_minutes"])


screen_observation_repo = ScreenObservationRepository()
