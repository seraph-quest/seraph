"""ContextManager — singleton that maintains and refreshes CurrentContext."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from src.audit.runtime import log_background_task_event
from src.observer.context import CurrentContext
from src.observer.salience import derive_observer_assessment
from src.observer.user_state import UserState, user_state_machine

logger = logging.getLogger(__name__)

# States considered "blocked" for transition detection
_BLOCKED_STATES = {UserState.deep_work.value, UserState.in_meeting.value, UserState.away.value}
_UNBLOCKED_STATES = {UserState.available.value, UserState.transitioning.value}


async def _gather_time_source() -> dict:
    from src.observer.sources.time_source import gather_time

    return gather_time()


async def _gather_calendar_source() -> dict:
    from src.observer.sources.calendar_source import gather_calendar

    return await gather_calendar()


async def _gather_git_source() -> dict:
    from src.observer.sources.git_source import gather_git

    result = gather_git()
    return result or {}


async def _gather_goal_source() -> dict:
    from src.observer.sources.goal_source import gather_goals

    return await gather_goals()


_OBSERVER_SOURCE_RUNNERS: dict[str, Callable[[], Awaitable[dict]]] = {
    "time": _gather_time_source,
    "calendar": _gather_calendar_source,
    "git": _gather_git_source,
    "goals": _gather_goal_source,
}


def _active_observer_definitions() -> list[tuple[str, str]]:
    from config.settings import settings
    from src.extensions.observers import select_active_observer_definitions
    from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
    from src.extensions.state import connector_enabled_overrides, load_extension_state_payload

    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    contributions = snapshot.list_contributions("observer_definitions")
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    active_definitions = select_active_observer_definitions(
        contributions,
        enabled_overrides=connector_enabled_overrides(state_by_id),
    )
    if active_definitions:
        return [(item.source_type, item.name) for item in active_definitions]
    if contributions:
        return []
    return [
        ("time", "time"),
        ("calendar", "calendar"),
        ("git", "git"),
        ("goals", "goals"),
    ]


class ContextManager:
    def __init__(self) -> None:
        self._context = CurrentContext()
        self._lock = asyncio.Lock()
        self._transition_epoch = 0  # guards against duplicate bundle deliveries

    def get_context(self) -> CurrentContext:
        """Return current snapshot (sync, non-blocking)."""
        return self._context

    def is_daemon_connected(self, max_age_seconds: float = 30) -> bool:
        """Return whether the native daemon has posted recently."""
        last_post = self._context.last_daemon_post
        if last_post is None:
            return False
        return (time.time() - last_post) < max_age_seconds

    async def refresh(self) -> CurrentContext:
        """Gather all sources and merge into a new context snapshot.

        Preserves externally-managed fields (screen_context, last_interaction).
        Each source is wrapped in try/except so one failure doesn't block others.
        After gathering sources, derives user state and checks for transitions.
        """
        try:
            async with self._lock:
                old = self._context

                active_sources = _active_observer_definitions()
                source_results: dict[str, dict] = {}
                sources_ok = 0
                sources_total = len(active_sources)

                for source_type, source_name in active_sources:
                    runner = _OBSERVER_SOURCE_RUNNERS.get(source_type)
                    if runner is None:
                        logger.warning("Observer source '%s' has no runtime runner", source_type)
                        continue
                    try:
                        source_results[source_type] = await runner()
                        sources_ok += 1
                    except Exception:
                        logger.exception("Observer source '%s' (%s) failed during refresh", source_type, source_name)
                        source_results[source_type] = {}

                time_data = source_results.get("time", {})
                calendar_data = source_results.get("calendar", {})
                git_data = source_results.get("git", {})
                goal_data = source_results.get("goals", {})

                # Derive data quality
                if sources_total == 0:
                    data_quality = "stale"
                elif sources_ok == sources_total:
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

                assessment = derive_observer_assessment(
                    current_event=calendar_data.get("current_event"),
                    upcoming_events=calendar_data.get("upcoming_events", []),
                    recent_git_activity=git_data.get("recent_git_activity"),
                    active_goals_summary=goal_data.get("active_goals_summary", ""),
                    active_window=old.active_window,
                    screen_context=old.screen_context,
                    data_quality=data_quality,
                    user_state=new_user_state,
                    interruption_mode=old.interruption_mode,
                    attention_budget_remaining=budget,
                )
                try:
                    from src.observer.screen_repository import screen_observation_repo

                    recent_projects = await screen_observation_repo.get_recent_projects(limit=1)
                    active_project = recent_projects[0] if recent_projects else None
                except Exception:
                    logger.debug("Failed to load active project during observer refresh", exc_info=True)
                    active_project = old.active_project

                self._context = CurrentContext(
                    time_of_day=time_data.get("time_of_day", "unknown"),
                    day_of_week=time_data.get("day_of_week", "unknown"),
                    is_working_hours=time_data.get("is_working_hours", False),
                    upcoming_events=calendar_data.get("upcoming_events", []),
                    current_event=calendar_data.get("current_event"),
                    recent_git_activity=git_data.get("recent_git_activity"),
                    active_goals_summary=goal_data.get("active_goals_summary", ""),
                    active_project=active_project,
                    # Preserve externally-managed fields from old context
                    last_interaction=old.last_interaction,
                    user_state=new_user_state,
                    interruption_mode=old.interruption_mode,
                    attention_budget_remaining=budget,
                    active_window=old.active_window,
                    screen_context=old.screen_context,
                    last_daemon_post=old.last_daemon_post,
                    last_native_notification_at=old.last_native_notification_at,
                    last_native_notification_title=old.last_native_notification_title,
                    last_native_notification_outcome=old.last_native_notification_outcome,
                    capture_mode=old.capture_mode,
                    tool_policy_mode=old.tool_policy_mode,
                    mcp_policy_mode=old.mcp_policy_mode,
                    approval_mode=old.approval_mode,
                    # Phase 3.3 tracking
                    previous_user_state=old.user_state,
                    attention_budget_last_reset=last_reset,
                    data_quality=data_quality,
                    observer_confidence=assessment.observer_confidence,
                    salience_level=assessment.salience_level,
                    salience_reason=assessment.salience_reason,
                    interruption_cost=assessment.interruption_cost,
                )
                observer_transition_count = 0
                try:
                    from src.memory.observer_episodes import record_observer_transition_episodes

                    observer_transition_count = await record_observer_transition_episodes(
                        old_context=old,
                        new_context=self._context,
                        active_project=active_project,
                    )
                except Exception:
                    logger.debug("Failed to persist observer transition episodes", exc_info=True)

                triggered_bundle_delivery = False

                # Detect blocked → unblocked transition and deliver queued bundle
                if old.user_state in _BLOCKED_STATES and new_user_state in _UNBLOCKED_STATES:
                    self._transition_epoch += 1
                    epoch = self._transition_epoch
                    triggered_bundle_delivery = True
                    logger.info(
                        "State transition %s → %s (epoch=%d) — delivering queued bundle",
                        old.user_state, new_user_state, epoch,
                    )
                    asyncio.create_task(self._deliver_bundle(epoch))

                await log_background_task_event(
                    task_name="observer_context_refresh",
                    outcome="succeeded",
                    details={
                        "sources_ok": sources_ok,
                        "sources_total": sources_total,
                        "active_source_types": [source_type for source_type, _ in active_sources],
                        "data_quality": data_quality,
                        "observer_confidence": assessment.observer_confidence,
                        "salience_level": assessment.salience_level,
                        "salience_reason": assessment.salience_reason,
                        "interruption_cost": assessment.interruption_cost,
                        "active_project": active_project,
                        "previous_user_state": old.user_state,
                        "new_user_state": new_user_state,
                        "observer_transition_count": observer_transition_count,
                        "triggered_bundle_delivery": triggered_bundle_delivery,
                    },
                )

                return self._context
        except Exception as exc:
            await log_background_task_event(
                task_name="observer_context_refresh",
                outcome="failed",
                details={"error": str(exc)},
            )
            raise

    async def _deliver_bundle(self, epoch: int) -> None:
        """Background task to deliver queued bundle after state transition.

        Uses epoch to skip delivery if another transition happened in the meantime.
        """
        if epoch != self._transition_epoch:
            logger.info("Skipping bundle delivery — epoch %d superseded by %d", epoch, self._transition_epoch)
            await log_background_task_event(
                task_name="observer_queued_bundle_delivery",
                outcome="skipped",
                details={
                    "requested_epoch": epoch,
                    "current_epoch": self._transition_epoch,
                },
            )
            return
        try:
            from src.observer.delivery import deliver_queued_bundle
            delivered_count = await deliver_queued_bundle()
            await log_background_task_event(
                task_name="observer_queued_bundle_delivery",
                outcome="succeeded",
                details={
                    "requested_epoch": epoch,
                    "delivered_count": delivered_count,
                },
            )
        except Exception as exc:
            await log_background_task_event(
                task_name="observer_queued_bundle_delivery",
                outcome="failed",
                details={
                    "requested_epoch": epoch,
                    "error": str(exc),
                },
            )
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
        Records timestamp for daemon heartbeat tracking.
        """
        if active_window is not None:
            self._context.active_window = active_window
        if screen_context is not None:
            self._context.screen_context = screen_context
        self._context.last_daemon_post = time.time()

    def record_native_notification(
        self,
        *,
        title: str | None,
        outcome: str,
        recorded_at: datetime | None = None,
    ) -> None:
        """Record the latest native-notification state for operator surfaces."""
        self._context.last_native_notification_title = title
        self._context.last_native_notification_outcome = outcome
        self._context.last_native_notification_at = recorded_at or datetime.now(timezone.utc)

    def decrement_attention_budget(self) -> None:
        """Reduce attention budget by 1 (minimum 0)."""
        self._context.attention_budget_remaining = max(
            0, self._context.attention_budget_remaining - 1
        )

    def update_capture_mode(self, mode: str) -> None:
        """Change capture mode setting."""
        self._context.capture_mode = mode
        logger.info("Capture mode set to %s", mode)

    def update_tool_policy_mode(self, mode: str) -> None:
        """Change the tool policy mode."""
        self._context.tool_policy_mode = mode
        logger.info("Tool policy mode set to %s", mode)

    def update_mcp_policy_mode(self, mode: str) -> None:
        """Change the MCP tool policy mode."""
        self._context.mcp_policy_mode = mode
        logger.info("MCP policy mode set to %s", mode)

    def update_approval_mode(self, mode: str) -> None:
        """Change the high-risk approval mode."""
        self._context.approval_mode = mode
        logger.info("Approval mode set to %s", mode)

    def update_interruption_mode(self, mode: str) -> None:
        """Change interruption mode and reset budget to mode default."""
        self._context.interruption_mode = mode
        self._context.attention_budget_remaining = user_state_machine.get_default_budget(mode)
        self._context.attention_budget_last_reset = datetime.now(timezone.utc)
        logger.info("Interruption mode set to %s (budget=%d)", mode, self._context.attention_budget_remaining)


context_manager = ContextManager()
