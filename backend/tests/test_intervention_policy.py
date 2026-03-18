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
