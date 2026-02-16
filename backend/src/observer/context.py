"""CurrentContext dataclass — unified snapshot of all context sources."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class CurrentContext:
    # Time source
    time_of_day: str = "unknown"  # morning / afternoon / evening / night
    day_of_week: str = "unknown"  # Monday, Tuesday, ...
    is_working_hours: bool = False

    # Calendar source
    upcoming_events: list[dict] = field(default_factory=list)
    current_event: Optional[str] = None

    # Git source
    recent_git_activity: Optional[list[dict]] = None

    # Goal source
    active_goals_summary: str = ""

    # Interaction tracking
    last_interaction: Optional[datetime] = None

    # User state (externally managed)
    user_state: str = "available"
    interruption_mode: str = "balanced"
    attention_budget_remaining: int = 5

    # Screen context (from native daemon, Phase 3.4)
    active_window: Optional[str] = None
    screen_context: Optional[str] = None

    # Daemon heartbeat — Unix timestamp of last POST from daemon
    last_daemon_post: Optional[float] = None

    # Phase 3.3 — State machine tracking
    previous_user_state: str = "available"
    attention_budget_last_reset: Optional[datetime] = None

    # Data quality — "good" if all sources succeeded, "degraded" if some failed, "stale" if all failed
    data_quality: str = "good"

    def to_dict(self) -> dict:
        """Serialize for API responses."""
        data = asdict(self)
        if data["last_interaction"]:
            data["last_interaction"] = data["last_interaction"].isoformat()
        if data["attention_budget_last_reset"]:
            data["attention_budget_last_reset"] = data["attention_budget_last_reset"].isoformat()
        return data

    def to_prompt_block(self) -> str:
        """Format as a text block for agent context injection."""
        lines = [
            f"Time: {self.time_of_day} ({self.day_of_week})",
            f"Working hours: {'yes' if self.is_working_hours else 'no'}",
        ]

        if self.current_event:
            lines.append(f"Current event: {self.current_event}")

        if self.upcoming_events:
            event_strs = []
            for e in self.upcoming_events[:3]:
                event_strs.append(f"  - {e.get('summary', '?')} at {e.get('start', '?')}")
            lines.append("Upcoming events:\n" + "\n".join(event_strs))

        if self.recent_git_activity:
            lines.append(f"Recent git activity: {len(self.recent_git_activity)} commits in last hour")

        if self.active_goals_summary:
            lines.append(f"Active goals: {self.active_goals_summary}")

        if self.active_window:
            lines.append(f"User is in: {self.active_window}")

        if self.screen_context:
            sc = self.screen_context[:500] + "..." if len(self.screen_context) > 500 else self.screen_context
            lines.append(f"Screen content: {sc}")

        if self.last_interaction:
            delta = datetime.now(timezone.utc) - self.last_interaction
            minutes_ago = int(delta.total_seconds() / 60)
            lines.append(f"Last interaction: {minutes_ago}m ago")

        lines.append(f"User state: {self.user_state} | Mode: {self.interruption_mode} | Budget: {self.attention_budget_remaining}")

        return "\n".join(lines)
