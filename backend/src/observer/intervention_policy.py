"""Explicit intervention policy for proactive guardian outputs."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from src.observer.user_state import DeliveryDecision, InterruptionMode, UserState, user_state_machine


class InterventionAction(str, enum.Enum):
    act = "act"
    defer = "defer"
    bundle = "bundle"
    request_approval = "request_approval"
    stay_silent = "stay_silent"


@dataclass(frozen=True)
class InterventionDecision:
    action: InterventionAction
    reason: str
    delivery_decision: DeliveryDecision | None
    should_cost_budget: bool

    @property
    def audit_decision(self) -> str:
        match self.action:
            case InterventionAction.act:
                return "delivered"
            case InterventionAction.bundle:
                return "queued"
            case InterventionAction.defer:
                return "deferred"
            case InterventionAction.request_approval:
                return "approval_requested"
            case InterventionAction.stay_silent:
                return "silenced"


def _has_meaningful_payload(*, message_type: str, content: str) -> bool:
    if message_type == "ambient":
        return True
    return bool(content.strip())


def decide_intervention(
    *,
    message_type: str,
    intervention_type: str,
    content: str,
    urgency: int,
    user_state: str,
    interruption_mode: str,
    attention_budget_remaining: int,
    is_scheduled: bool = False,
    data_quality: str = "good",
    guardian_confidence: str | None = None,
    requires_approval: bool = False,
) -> InterventionDecision:
    """Make the explicit policy decision for a proactive intervention candidate."""
    should_cost_budget = user_state_machine.should_cost_budget(
        intervention_type=intervention_type,
        is_scheduled=is_scheduled,
        urgency=urgency,
    )

    if not _has_meaningful_payload(message_type=message_type, content=content):
        return InterventionDecision(
            action=InterventionAction.stay_silent,
            reason="empty_content",
            delivery_decision=None,
            should_cost_budget=should_cost_budget,
        )

    if requires_approval:
        return InterventionDecision(
            action=InterventionAction.request_approval,
            reason="requires_approval",
            delivery_decision=None,
            should_cost_budget=should_cost_budget,
        )

    if guardian_confidence in {"degraded", "partial", "empty"} and urgency < 4 and not is_scheduled:
        return InterventionDecision(
            action=InterventionAction.defer,
            reason="low_guardian_confidence",
            delivery_decision=None,
            should_cost_budget=should_cost_budget,
        )

    if data_quality in {"degraded", "stale"} and urgency < 4 and not is_scheduled:
        return InterventionDecision(
            action=InterventionAction.defer,
            reason="degraded_observer_state",
            delivery_decision=None,
            should_cost_budget=should_cost_budget,
        )

    if urgency >= 5:
        return InterventionDecision(
            action=InterventionAction.act,
            reason="urgent",
            delivery_decision=DeliveryDecision.deliver,
            should_cost_budget=should_cost_budget,
        )

    if is_scheduled:
        return InterventionDecision(
            action=InterventionAction.act,
            reason="scheduled",
            delivery_decision=DeliveryDecision.deliver,
            should_cost_budget=should_cost_budget,
        )

    if user_state in {UserState.deep_work.value, UserState.in_meeting.value, UserState.away.value}:
        return InterventionDecision(
            action=InterventionAction.bundle,
            reason="blocked_state",
            delivery_decision=DeliveryDecision.queue,
            should_cost_budget=should_cost_budget,
        )

    if interruption_mode == InterruptionMode.focus.value:
        return InterventionDecision(
            action=InterventionAction.bundle,
            reason="focus_mode",
            delivery_decision=DeliveryDecision.queue,
            should_cost_budget=should_cost_budget,
        )

    if user_state == UserState.winding_down.value and intervention_type != "alert":
        return InterventionDecision(
            action=InterventionAction.defer,
            reason="winding_down_quiet_hours",
            delivery_decision=None,
            should_cost_budget=should_cost_budget,
        )

    if should_cost_budget and attention_budget_remaining <= 0:
        return InterventionDecision(
            action=InterventionAction.bundle,
            reason="attention_budget_exhausted",
            delivery_decision=DeliveryDecision.queue,
            should_cost_budget=should_cost_budget,
        )

    return InterventionDecision(
        action=InterventionAction.act,
        reason="available_capacity",
        delivery_decision=DeliveryDecision.deliver,
        should_cost_budget=should_cost_budget,
    )
