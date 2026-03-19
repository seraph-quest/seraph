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
    observer_confidence: str = "grounded",
    salience_level: str = "medium",
    salience_reason: str = "background",
    interruption_cost: str = "medium",
    requires_approval: bool = False,
    recent_feedback_bias: str = "neutral",
    learning_phrasing_bias: str = "neutral",
    learning_cadence_bias: str = "neutral",
    learning_channel_bias: str = "neutral",
    learning_escalation_bias: str = "neutral",
    learning_timing_bias: str = "neutral",
    learning_blocked_state_bias: str = "neutral",
    learning_suppression_bias: str = "neutral",
    learning_thread_preference_bias: str = "neutral",
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

    if observer_confidence == "degraded" and urgency < 4 and not is_scheduled:
        return InterventionDecision(
            action=InterventionAction.defer,
            reason="low_observer_confidence",
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

    if (
        salience_level == "low"
        and urgency <= 1
        and not is_scheduled
        and message_type != "ambient"
        and intervention_type != "alert"
    ):
        return InterventionDecision(
            action=InterventionAction.stay_silent,
            reason="low_observer_salience",
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

    if recent_feedback_bias == "reduce_interruptions" and urgency < 4 and intervention_type != "alert":
        return InterventionDecision(
            action=InterventionAction.bundle,
            reason="recent_negative_feedback",
            delivery_decision=DeliveryDecision.queue,
            should_cost_budget=should_cost_budget,
        )

    if learning_cadence_bias == "bundle_more" and urgency < 4 and intervention_type != "alert":
        return InterventionDecision(
            action=InterventionAction.bundle,
            reason="learned_low_cadence",
            delivery_decision=DeliveryDecision.queue,
            should_cost_budget=should_cost_budget,
        )

    if learning_suppression_bias == "extend_suppression" and urgency < 4 and intervention_type != "alert":
        return InterventionDecision(
            action=InterventionAction.defer,
            reason="learned_suppression_window",
            delivery_decision=None,
            should_cost_budget=should_cost_budget,
        )

    if (
        learning_escalation_bias == "prefer_async_native"
        and learning_channel_bias == "prefer_native_notification"
        and urgency >= 3
        and intervention_type != "alert"
        and user_state in {UserState.deep_work.value, UserState.in_meeting.value, UserState.away.value}
    ):
        return InterventionDecision(
            action=InterventionAction.act,
            reason="learned_async_native_delivery",
            delivery_decision=DeliveryDecision.deliver,
            should_cost_budget=should_cost_budget,
        )

    if (
        learning_blocked_state_bias == "avoid_blocked_state_interruptions"
        and user_state in {UserState.deep_work.value, UserState.in_meeting.value, UserState.away.value}
        and urgency < 5
        and intervention_type != "alert"
    ):
        return InterventionDecision(
            action=InterventionAction.bundle,
            reason="learned_blocked_state_avoidance",
            delivery_decision=DeliveryDecision.queue,
            should_cost_budget=should_cost_budget,
        )

    if user_state in {UserState.deep_work.value, UserState.in_meeting.value, UserState.away.value}:
        return InterventionDecision(
            action=InterventionAction.bundle,
            reason="blocked_state",
            delivery_decision=DeliveryDecision.queue,
            should_cost_budget=should_cost_budget,
        )

    if (
        interruption_cost == "high"
        and urgency >= (2 if recent_feedback_bias == "prefer_direct_delivery" else 3)
        and salience_level == "high"
        and salience_reason in {"current_event", "upcoming_event", "aligned_work_activity"}
        and observer_confidence == "grounded"
        and guardian_confidence not in {"degraded", "partial", "empty"}
        and interruption_mode != InterruptionMode.focus.value
        and attention_budget_remaining > 0
        and intervention_type != "alert"
    ):
        return InterventionDecision(
            action=InterventionAction.act,
            reason=(
                "learned_direct_delivery"
                if recent_feedback_bias == "prefer_direct_delivery" and urgency < 3
                else "calibrated_high_salience"
            ),
            delivery_decision=DeliveryDecision.deliver,
            should_cost_budget=should_cost_budget,
        )

    if interruption_cost == "high" and urgency < 4 and intervention_type != "alert":
        return InterventionDecision(
            action=InterventionAction.bundle,
            reason="high_interruption_cost",
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

    if (
        learning_cadence_bias == "check_in_sooner"
        and urgency >= 2
        and interruption_cost != "high"
        and attention_budget_remaining > 0
        and intervention_type != "alert"
        and learning_phrasing_bias in {"be_brief_and_literal", "be_more_direct"}
    ):
        return InterventionDecision(
            action=InterventionAction.act,
            reason="learned_quicker_followup",
            delivery_decision=DeliveryDecision.deliver,
            should_cost_budget=should_cost_budget,
        )

    if (
        learning_timing_bias == "prefer_available_windows"
        and user_state == UserState.available.value
        and urgency >= 2
        and interruption_cost != "high"
        and attention_budget_remaining > 0
        and intervention_type != "alert"
    ):
        return InterventionDecision(
            action=InterventionAction.act,
            reason="learned_available_window",
            delivery_decision=DeliveryDecision.deliver,
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
        reason=(
            "prefer_existing_thread"
            if learning_thread_preference_bias == "prefer_existing_thread" and intervention_type != "alert"
            else "available_capacity"
        ),
        delivery_decision=DeliveryDecision.deliver,
        should_cost_budget=should_cost_budget,
    )
