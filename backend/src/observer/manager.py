"""ContextManager — singleton that maintains and refreshes CurrentContext."""

import asyncio
import logging
from datetime import datetime, timezone

from src.observer.context import CurrentContext
from src.observer.user_state import UserState, user_state_machine

logger = logging.getLogger(__name__)

# States considered "blocked" for transition detection
_BLOCKED_STATES = {UserState.deep_work.value, UserState.in_meeting.value, UserState.away.value}
_UNBLOCKED_STATES = {UserState.available.value, UserState.transitioning.value}


class ContextManager:
    def __init__(self) -> None:
        self._context = CurrentContext()
        self._lock = asyncio.Lock()
        self._transition_epoch = 0  # guards against duplicate bundle deliveries

    def get_context(self) -> CurrentContext:
        """Return current snapshot (sync, non-blocking)."""
        return self._context

    async def refresh(self) -> CurrentContext:
        """Gather all sources and merge into a new context snapshot.

        Preserves externally-managed fields (screen_context, last_interaction).
        Each source is wrapped in try/except so one failure doesn't block others.
        After gathering sources, derives user state and checks for transitions.
        """
        async with self._lock:
            old = self._context

            # Track source success for data quality
            sources_ok = 0
            sources_total = 4

            # Time source (sync, pure computation)
            time_data: dict = {}
            try:
                from src.observer.sources.time_source import gather_time
                time_data = gather_time()
                sources_ok += 1
            except Exception:
                logger.exception("Time source failed")

            # Calendar source (async, I/O)
            calendar_data: dict = {}
            try:
                from src.observer.sources.calendar_source import gather_calendar
                calendar_data = await gather_calendar()
                sources_ok += 1
            except Exception:
                logger.exception("Calendar source failed during refresh")

            # Git source (sync, filesystem)
            git_data: dict = {}
            try:
                from src.observer.sources.git_source import gather_git
                result = gather_git()
                if result:
                    git_data = result
                sources_ok += 1
            except Exception:
                logger.exception("Git source failed")

            # Goal source (async, DB)
            goal_data: dict = {}
            try:
                from src.observer.sources.goal_source import gather_goals
                goal_data = await gather_goals()
                sources_ok += 1
            except Exception:
                logger.exception("Goal source failed")

            # Derive data quality
            if sources_ok == sources_total:
                data_quality = "good"
            elif sources_ok == 0:
                data_quality = "stale"
            else:
                data_quality = "degraded"

            # Derive user state from gathered sources
            new_user_state = user_state_machine.derive_state(
                current_event=calendar_data.get("current_event"),
                previous_state=old.user_state,
                time_of_day=time_data.get("time_of_day", "unknown"),
                is_working_hours=time_data.get("is_working_hours", False),
                last_interaction=old.last_interaction,
                active_window=old.active_window,
            )

            # Check for daily budget reset
            budget = old.attention_budget_remaining
            last_reset = old.attention_budget_last_reset
            budget, last_reset = self._maybe_reset_budget(
                old.interruption_mode, budget, last_reset,
            )

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
                user_state=new_user_state,
                interruption_mode=old.interruption_mode,
                attention_budget_remaining=budget,
                active_window=old.active_window,
                screen_context=old.screen_context,
                # Phase 3.3 tracking
                previous_user_state=old.user_state,
                attention_budget_last_reset=last_reset,
                data_quality=data_quality,
            )

            # Detect blocked → unblocked transition and deliver queued bundle
            if old.user_state in _BLOCKED_STATES and new_user_state in _UNBLOCKED_STATES:
                self._transition_epoch += 1
                epoch = self._transition_epoch
                logger.info(
                    "State transition %s → %s (epoch=%d) — delivering queued bundle",
                    old.user_state, new_user_state, epoch,
                )
                asyncio.create_task(self._deliver_bundle(epoch))

            return self._context

    async def _deliver_bundle(self, epoch: int) -> None:
        """Background task to deliver queued bundle after state transition.

        Uses epoch to skip delivery if another transition happened in the meantime.
        """
        if epoch != self._transition_epoch:
            logger.info("Skipping bundle delivery — epoch %d superseded by %d", epoch, self._transition_epoch)
            return
        try:
            from src.observer.delivery import deliver_queued_bundle
            await deliver_queued_bundle()
        except Exception:
            logger.exception("Failed to deliver queued bundle")

    def _maybe_reset_budget(
        self,
        mode: str,
        current_budget: int,
        last_reset: datetime | None,
    ) -> tuple[int, datetime | None]:
        """Reset attention budget at morning_briefing_hour if not yet reset today.

        Uses date-based comparison to be immune to NTP clock skew.
        """
        from config.settings import settings

        now = datetime.now(timezone.utc)
        reset_hour = settings.morning_briefing_hour

        if last_reset is None:
            default = user_state_machine.get_default_budget(mode)
            return default, now

        last_date = last_reset.date()
        today = now.date()

        # New day and past the reset hour
        if today > last_date and now.hour >= reset_hour:
            default = user_state_machine.get_default_budget(mode)
            return default, now

        # Same day but crossed the reset hour since last reset
        if today == last_date and last_reset.hour < reset_hour <= now.hour:
            default = user_state_machine.get_default_budget(mode)
            return default, now

        return current_budget, last_reset

    def update_last_interaction(self) -> None:
        """Stamp the current time as last user interaction."""
        self._context.last_interaction = datetime.now(timezone.utc)

    def update_screen_context(self, active_window: str | None, screen_context: str | None) -> None:
        """Update screen context from native daemon POST.

        Supports partial updates — only overwrites fields that are non-None,
        so the window loop and OCR loop don't clobber each other's data.
        """
        if active_window is not None:
            self._context.active_window = active_window
        if screen_context is not None:
            self._context.screen_context = screen_context

    def decrement_attention_budget(self) -> None:
        """Reduce attention budget by 1 (minimum 0)."""
        self._context.attention_budget_remaining = max(
            0, self._context.attention_budget_remaining - 1
        )

    def update_interruption_mode(self, mode: str) -> None:
        """Change interruption mode and reset budget to mode default."""
        self._context.interruption_mode = mode
        self._context.attention_budget_remaining = user_state_machine.get_default_budget(mode)
        self._context.attention_budget_last_reset = datetime.now(timezone.utc)
        logger.info("Interruption mode set to %s (budget=%d)", mode, self._context.attention_budget_remaining)


context_manager = ContextManager()
