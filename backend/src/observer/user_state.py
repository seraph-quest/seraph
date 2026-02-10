"""User state machine and delivery gate for proactive message filtering."""

import enum
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Enums ────────────────────────────────────────────────

class UserState(str, enum.Enum):
    deep_work = "deep_work"
    in_meeting = "in_meeting"
    transitioning = "transitioning"
    available = "available"
    away = "away"
    winding_down = "winding_down"


class InterruptionMode(str, enum.Enum):
    focus = "focus"
    balanced = "balanced"
    active = "active"


class DeliveryDecision(str, enum.Enum):
    deliver = "deliver"
    queue = "queue"
    drop = "drop"


# States where messages should be blocked (queued or dropped)
_BLOCKED_STATES = {UserState.deep_work, UserState.in_meeting, UserState.away}

# States considered "unblocked" for transition detection
_UNBLOCKED_STATES = {UserState.available, UserState.transitioning}


class UserStateMachine:
    """Derives user state from context signals and gates proactive deliveries."""

    def derive_state(
        self,
        current_event: Optional[str],
        previous_state: str,
        time_of_day: str,
        is_working_hours: bool,
        last_interaction: Optional[datetime],
        active_window: Optional[str] = None,
    ) -> str:
        """Derive user state from context signals.

        Priority order:
        1. Calendar focus block (event contains "focus" or "deep work")
        2. In meeting (any current calendar event)
        3. Transition detection (previous was blocked, now unblocking)
        4. Away (no interaction for 30+ minutes)
        5. Winding down (evening hours)
        6. Available (default)
        """
        # 1. Calendar focus block
        if current_event and any(
            kw in current_event.lower() for kw in ("focus", "deep work", "do not disturb")
        ):
            return UserState.deep_work

        # 2. In meeting
        if current_event:
            return UserState.in_meeting

        # 3. Transition detection — previous state was blocked, now cleared
        if previous_state in {s.value for s in _BLOCKED_STATES} and not current_event:
            return UserState.transitioning

        # 4. Away — no interaction for 30+ minutes
        if last_interaction:
            delta = datetime.now(timezone.utc) - last_interaction
            if delta > timedelta(minutes=30):
                return UserState.away

        # 5. Winding down — evening/night
        if time_of_day in ("evening", "night"):
            return UserState.winding_down

        # 6. Default
        return UserState.available

    def should_deliver(
        self,
        user_state: str,
        interruption_mode: str,
        attention_budget_remaining: int,
        urgency: int,
        intervention_type: str,
        is_scheduled: bool = False,
    ) -> DeliveryDecision:
        """Central decision gate for proactive message delivery.

        Returns DeliveryDecision indicating whether to deliver, queue, or drop.
        """
        # Urgent messages (urgency >= 5) always deliver
        if urgency >= 5:
            return DeliveryDecision.deliver

        # Scheduled messages (like morning briefing) always deliver
        if is_scheduled:
            return DeliveryDecision.deliver

        # Blocked states → queue
        if user_state in {s.value for s in _BLOCKED_STATES}:
            return DeliveryDecision.queue

        # Focus mode blocks everything except urgent/scheduled (handled above)
        if interruption_mode == InterruptionMode.focus:
            return DeliveryDecision.queue

        # Winding down — only allow alerts, queue the rest
        if user_state == UserState.winding_down:
            if intervention_type == "alert":
                return DeliveryDecision.deliver
            return DeliveryDecision.queue

        # Budget check for costly deliveries
        if self.should_cost_budget(intervention_type, is_scheduled, urgency):
            if attention_budget_remaining <= 0:
                return DeliveryDecision.queue

        return DeliveryDecision.deliver

    def get_default_budget(self, interruption_mode: str) -> int:
        """Return the default attention budget for a given mode."""
        budgets = {
            InterruptionMode.focus: 0,
            InterruptionMode.balanced: 5,
            InterruptionMode.active: 15,
        }
        return budgets.get(interruption_mode, 5)

    def should_cost_budget(
        self,
        intervention_type: str,
        is_scheduled: bool,
        urgency: int,
    ) -> bool:
        """Determine if a delivery should cost budget.

        Free (no budget cost): ambient, scheduled, urgent (>=5), bundle.
        Everything else costs 1.
        """
        if intervention_type in ("ambient", "proactive_bundle"):
            return False
        if is_scheduled:
            return False
        if urgency >= 5:
            return False
        return True


# Singleton
user_state_machine = UserStateMachine()
