"""ContextManager — singleton that maintains and refreshes CurrentContext."""

import asyncio
import logging
from datetime import datetime, timezone

from src.observer.context import CurrentContext

logger = logging.getLogger(__name__)


class ContextManager:
    def __init__(self) -> None:
        self._context = CurrentContext()
        self._lock = asyncio.Lock()

    def get_context(self) -> CurrentContext:
        """Return current snapshot (sync, non-blocking)."""
        return self._context

    async def refresh(self) -> CurrentContext:
        """Gather all sources and merge into a new context snapshot.

        Preserves externally-managed fields (screen_context, last_interaction).
        Each source is wrapped in try/except so one failure doesn't block others.
        """
        async with self._lock:
            old = self._context

            # Time source (sync, pure computation)
            time_data: dict = {}
            try:
                from src.observer.sources.time_source import gather_time
                time_data = gather_time()
            except Exception:
                logger.exception("Time source failed")

            # Calendar source (async, I/O)
            calendar_data: dict = {}
            try:
                from src.observer.sources.calendar_source import gather_calendar
                calendar_data = await gather_calendar()
            except Exception:
                logger.exception("Calendar source failed during refresh")

            # Git source (sync, filesystem)
            git_data: dict = {}
            try:
                from src.observer.sources.git_source import gather_git
                result = gather_git()
                if result:
                    git_data = result
            except Exception:
                logger.exception("Git source failed")

            # Goal source (async, DB)
            goal_data: dict = {}
            try:
                from src.observer.sources.goal_source import gather_goals
                goal_data = await gather_goals()
            except Exception:
                logger.exception("Goal source failed")

            self._context = CurrentContext(
                time_of_day=time_data.get("time_of_day", "unknown"),
                day_of_week=time_data.get("day_of_week", "unknown"),
                is_working_hours=time_data.get("is_working_hours", False),
                upcoming_events=calendar_data.get("upcoming_events", []),
                current_event=calendar_data.get("current_event"),
                recent_git_activity=git_data.get("recent_git_activity"),
                active_goals_summary=goal_data.get("active_goals_summary", ""),
                # Preserve externally-managed fields from old context
                last_interaction=old.last_interaction,
                user_state=old.user_state,
                interruption_mode=old.interruption_mode,
                attention_budget_remaining=old.attention_budget_remaining,
                active_window=old.active_window,
                screen_context=old.screen_context,
            )

            return self._context

    def update_last_interaction(self) -> None:
        """Stamp the current time as last user interaction."""
        self._context.last_interaction = datetime.now(timezone.utc)

    def update_screen_context(self, active_window: str | None, screen_context: str | None) -> None:
        """Update screen context from native daemon POST."""
        self._context.active_window = active_window
        self._context.screen_context = screen_context


context_manager = ContextManager()
