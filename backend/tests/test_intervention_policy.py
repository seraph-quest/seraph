"""Tests for explicit intervention-policy decisions."""

from src.observer.intervention_policy import (
    InterventionAction,
    decide_intervention,
)


def test_decide_intervention_acts_when_capacity_is_available():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Guardian note",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
    )

    assert decision.action == InterventionAction.act
    assert decision.reason == "available_capacity"
    assert decision.delivery_decision is not None
    assert decision.delivery_decision.value == "deliver"


def test_decide_intervention_bundles_when_blocked():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Guardian note",
        urgency=3,
        user_state="deep_work",
        interruption_mode="balanced",
        attention_budget_remaining=3,
    )

    assert decision.action == InterventionAction.bundle
    assert decision.reason == "blocked_state"
    assert decision.delivery_decision is not None
    assert decision.delivery_decision.value == "queue"


def test_decide_intervention_defers_on_low_guardian_confidence():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Guardian note",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        guardian_confidence="partial",
    )

    assert decision.action == InterventionAction.defer
    assert decision.reason == "low_guardian_confidence"
    assert decision.delivery_decision is None


def test_decide_intervention_requests_approval_when_flagged():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="alert",
        content="Guardian note",
        urgency=4,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        requires_approval=True,
    )

    assert decision.action == InterventionAction.request_approval
    assert decision.reason == "requires_approval"
    assert decision.delivery_decision is None


def test_decide_intervention_bundles_on_high_interruption_cost():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Not urgent enough to interrupt",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=1,
        observer_confidence="grounded",
        salience_level="high",
        interruption_cost="high",
    )

    assert decision.action == InterventionAction.bundle
    assert decision.reason == "high_interruption_cost"
    assert decision.delivery_decision is not None
    assert decision.delivery_decision.value == "queue"


def test_decide_intervention_stays_silent_on_low_salience_noise():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Low-value ambient nudge",
        urgency=1,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        observer_confidence="grounded",
        salience_level="low",
        interruption_cost="low",
    )

    assert decision.action == InterventionAction.stay_silent
    assert decision.reason == "low_observer_salience"
    assert decision.delivery_decision is None


def test_decide_intervention_stays_silent_for_empty_non_ambient_payload():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="",
        urgency=2,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
    )

    assert decision.action == InterventionAction.stay_silent
    assert decision.reason == "empty_content"
    assert decision.delivery_decision is None


def test_decide_intervention_bundles_on_recent_negative_feedback():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Same kind of nudge again",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        recent_feedback_bias="reduce_interruptions",
    )

    assert decision.action == InterventionAction.bundle
    assert decision.reason == "recent_negative_feedback"
    assert decision.delivery_decision is not None
    assert decision.delivery_decision.value == "queue"


def test_decide_intervention_can_learn_direct_delivery_on_high_salience():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="This is directly aligned and usually lands well.",
        urgency=2,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        guardian_confidence="grounded",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="high",
        recent_feedback_bias="prefer_direct_delivery",
    )

    assert decision.action == InterventionAction.act
    assert decision.reason == "learned_direct_delivery"


def test_decide_intervention_can_learn_async_native_delivery_when_user_is_blocked():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Leave this as a desktop-visible reminder.",
        urgency=3,
        user_state="deep_work",
        interruption_mode="balanced",
        attention_budget_remaining=2,
        data_quality="good",
        guardian_confidence="grounded",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="medium",
        recent_feedback_bias="neutral",
        learning_channel_bias="prefer_native_notification",
        learning_escalation_bias="prefer_async_native",
    )

    assert decision.action == InterventionAction.act
    assert decision.reason == "learned_async_native_delivery"
    assert decision.delivery_decision is not None
    assert decision.delivery_decision.value == "deliver"


def test_decide_intervention_can_learn_blocked_state_avoidance():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="This can wait until the user is out of deep work.",
        urgency=3,
        user_state="deep_work",
        interruption_mode="balanced",
        attention_budget_remaining=2,
        guardian_confidence="grounded",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="medium",
        learning_blocked_state_bias="avoid_blocked_state_interruptions",
    )

    assert decision.action == InterventionAction.bundle
    assert decision.reason == "learned_blocked_state_avoidance"


def test_decide_intervention_can_learn_available_window_delivery():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Now is a good time for this nudge.",
        urgency=2,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=2,
        guardian_confidence="grounded",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="medium",
        learning_timing_bias="prefer_available_windows",
    )

    assert decision.action == InterventionAction.act
    assert decision.reason == "learned_available_window"


def test_decide_intervention_acts_on_calibrated_high_salience():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="You're in the middle of the goal you're actively shipping.",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=1,
        guardian_confidence="grounded",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="high",
    )

    assert decision.action == InterventionAction.act
    assert decision.reason == "calibrated_high_salience"
    assert decision.delivery_decision is not None
    assert decision.delivery_decision.value == "deliver"


def test_decide_intervention_defers_on_low_observer_confidence():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Signal is too weak to interrupt on.",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        observer_confidence="degraded",
        salience_level="high",
        salience_reason="aligned_work_activity",
    )

    assert decision.action == InterventionAction.defer
    assert decision.reason == "low_observer_confidence"
    assert decision.delivery_decision is None


def test_decide_intervention_defers_on_degraded_observer_state():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="State is degraded.",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        data_quality="degraded",
    )

    assert decision.action == InterventionAction.defer
    assert decision.reason == "degraded_observer_state"
    assert decision.delivery_decision is None


def test_decide_intervention_keeps_focus_mode_quiet_even_when_salience_is_high():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="You're in the middle of the goal you're actively shipping.",
        urgency=3,
        user_state="available",
        interruption_mode="focus",
        attention_budget_remaining=1,
        guardian_confidence="grounded",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="high",
    )

    assert decision.action == InterventionAction.bundle
    assert decision.reason == "high_interruption_cost"
    assert decision.delivery_decision is not None
    assert decision.delivery_decision.value == "queue"


def test_decide_intervention_scheduled_overrides_low_confidence_guards():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Scheduled review.",
        urgency=2,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        is_scheduled=True,
        guardian_confidence="partial",
        observer_confidence="degraded",
        data_quality="degraded",
    )

    assert decision.action == InterventionAction.act
    assert decision.reason == "scheduled"
    assert decision.delivery_decision is not None
    assert decision.delivery_decision.value == "deliver"


def test_decide_intervention_urgent_overrides_confidence_and_interruption_guards():
    decision = decide_intervention(
        message_type="proactive",
        intervention_type="alert",
        content="Urgent issue.",
        urgency=5,
        user_state="available",
        interruption_mode="focus",
        attention_budget_remaining=0,
        guardian_confidence="partial",
        observer_confidence="degraded",
        salience_level="low",
        salience_reason="background",
        interruption_cost="high",
        data_quality="degraded",
    )

    assert decision.action == InterventionAction.act
    assert decision.reason == "urgent"
    assert decision.delivery_decision is not None
    assert decision.delivery_decision.value == "deliver"
