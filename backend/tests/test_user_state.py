"""Tests for UserStateMachine — derive_state, should_deliver, budget helpers."""

from datetime import datetime, timezone, timedelta

import pytest

from src.observer.user_state import (
    DeliveryDecision,
    InterruptionMode,
    UserState,
    UserStateMachine,
)


@pytest.fixture
def sm():
    return UserStateMachine()


# ─── derive_state tests ─────────────────────────────────


class TestDeriveState:
    def test_focus_block_from_calendar(self, sm):
        state = sm.derive_state(
            current_event="Focus time — deep work",
            previous_state="available",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.deep_work

    def test_do_not_disturb_calendar(self, sm):
        state = sm.derive_state(
            current_event="Do not disturb block",
            previous_state="available",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.deep_work

    def test_in_meeting(self, sm):
        state = sm.derive_state(
            current_event="Team standup",
            previous_state="available",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.in_meeting

    def test_transitioning_from_meeting(self, sm):
        state = sm.derive_state(
            current_event=None,
            previous_state="in_meeting",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.transitioning

    def test_transitioning_from_deep_work(self, sm):
        state = sm.derive_state(
            current_event=None,
            previous_state="deep_work",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.transitioning

    def test_transitioning_from_away(self, sm):
        """Away → no event + recent interaction → transitioning."""
        state = sm.derive_state(
            current_event=None,
            previous_state="away",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.transitioning

    def test_away_no_interaction(self, sm):
        old_time = datetime.now(timezone.utc) - timedelta(minutes=45)
        state = sm.derive_state(
            current_event=None,
            previous_state="available",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=old_time,
        )
        assert state == UserState.away

    def test_away_exactly_30min(self, sm):
        """30min + 1s should be away."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=30, seconds=1)
        state = sm.derive_state(
            current_event=None,
            previous_state="available",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=old_time,
        )
        assert state == UserState.away

    def test_not_away_at_29min(self, sm):
        old_time = datetime.now(timezone.utc) - timedelta(minutes=29)
        state = sm.derive_state(
            current_event=None,
            previous_state="available",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=old_time,
        )
        assert state == UserState.available

    def test_winding_down_evening(self, sm):
        state = sm.derive_state(
            current_event=None,
            previous_state="available",
            time_of_day="evening",
            is_working_hours=False,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.winding_down

    def test_winding_down_night(self, sm):
        state = sm.derive_state(
            current_event=None,
            previous_state="available",
            time_of_day="night",
            is_working_hours=False,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.winding_down

    def test_available_default(self, sm):
        state = sm.derive_state(
            current_event=None,
            previous_state="available",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.available

    def test_available_no_last_interaction(self, sm):
        state = sm.derive_state(
            current_event=None,
            previous_state="available",
            time_of_day="afternoon",
            is_working_hours=True,
            last_interaction=None,
        )
        assert state == UserState.available

    def test_focus_keyword_case_insensitive(self, sm):
        state = sm.derive_state(
            current_event="DEEP WORK session",
            previous_state="available",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=datetime.now(timezone.utc),
        )
        assert state == UserState.deep_work

    def test_calendar_priority_over_away(self, sm):
        """Calendar event should take priority over inactivity."""
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        state = sm.derive_state(
            current_event="Sprint planning",
            previous_state="available",
            time_of_day="morning",
            is_working_hours=True,
            last_interaction=old_time,
        )
        assert state == UserState.in_meeting


# ─── should_deliver tests ───────────────────────────────


class TestShouldDeliver:
    def test_urgent_always_delivers(self, sm):
        decision = sm.should_deliver(
            user_state="deep_work",
            interruption_mode="focus",
            attention_budget_remaining=0,
            urgency=5,
            intervention_type="alert",
        )
        assert decision == DeliveryDecision.deliver

    def test_scheduled_always_delivers(self, sm):
        decision = sm.should_deliver(
            user_state="in_meeting",
            interruption_mode="focus",
            attention_budget_remaining=0,
            urgency=1,
            intervention_type="advisory",
            is_scheduled=True,
        )
        assert decision == DeliveryDecision.deliver

    def test_deep_work_queues(self, sm):
        decision = sm.should_deliver(
            user_state="deep_work",
            interruption_mode="balanced",
            attention_budget_remaining=5,
            urgency=3,
            intervention_type="advisory",
        )
        assert decision == DeliveryDecision.queue

    def test_in_meeting_queues(self, sm):
        decision = sm.should_deliver(
            user_state="in_meeting",
            interruption_mode="balanced",
            attention_budget_remaining=5,
            urgency=3,
            intervention_type="advisory",
        )
        assert decision == DeliveryDecision.queue

    def test_away_queues(self, sm):
        decision = sm.should_deliver(
            user_state="away",
            interruption_mode="balanced",
            attention_budget_remaining=5,
            urgency=3,
            intervention_type="advisory",
        )
        assert decision == DeliveryDecision.queue

    def test_focus_mode_queues(self, sm):
        decision = sm.should_deliver(
            user_state="available",
            interruption_mode="focus",
            attention_budget_remaining=0,
            urgency=3,
            intervention_type="advisory",
        )
        assert decision == DeliveryDecision.queue

    def test_winding_down_allows_alert(self, sm):
        decision = sm.should_deliver(
            user_state="winding_down",
            interruption_mode="balanced",
            attention_budget_remaining=3,
            urgency=3,
            intervention_type="alert",
        )
        assert decision == DeliveryDecision.deliver

    def test_winding_down_queues_advisory(self, sm):
        decision = sm.should_deliver(
            user_state="winding_down",
            interruption_mode="balanced",
            attention_budget_remaining=3,
            urgency=3,
            intervention_type="advisory",
        )
        assert decision == DeliveryDecision.queue

    def test_available_balanced_with_budget(self, sm):
        decision = sm.should_deliver(
            user_state="available",
            interruption_mode="balanced",
            attention_budget_remaining=3,
            urgency=3,
            intervention_type="advisory",
        )
        assert decision == DeliveryDecision.deliver

    def test_available_balanced_no_budget(self, sm):
        decision = sm.should_deliver(
            user_state="available",
            interruption_mode="balanced",
            attention_budget_remaining=0,
            urgency=3,
            intervention_type="advisory",
        )
        assert decision == DeliveryDecision.queue

    def test_available_active_with_budget(self, sm):
        decision = sm.should_deliver(
            user_state="available",
            interruption_mode="active",
            attention_budget_remaining=10,
            urgency=2,
            intervention_type="nudge",
        )
        assert decision == DeliveryDecision.deliver

    def test_transitioning_delivers(self, sm):
        decision = sm.should_deliver(
            user_state="transitioning",
            interruption_mode="balanced",
            attention_budget_remaining=5,
            urgency=3,
            intervention_type="advisory",
        )
        assert decision == DeliveryDecision.deliver

    def test_ambient_no_budget_still_delivers(self, sm):
        """Ambient messages don't cost budget, so they deliver even with 0 budget."""
        decision = sm.should_deliver(
            user_state="available",
            interruption_mode="balanced",
            attention_budget_remaining=0,
            urgency=2,
            intervention_type="ambient",
        )
        assert decision == DeliveryDecision.deliver


# ─── Budget helper tests ────────────────────────────────


class TestBudgetHelpers:
    def test_default_budget_focus(self, sm):
        assert sm.get_default_budget("focus") == 0

    def test_default_budget_balanced(self, sm):
        assert sm.get_default_budget("balanced") == 5

    def test_default_budget_active(self, sm):
        assert sm.get_default_budget("active") == 15

    def test_default_budget_unknown(self, sm):
        assert sm.get_default_budget("unknown_mode") == 5

    def test_should_cost_budget_advisory(self, sm):
        assert sm.should_cost_budget("advisory", False, 3) is True

    def test_should_cost_budget_alert(self, sm):
        assert sm.should_cost_budget("alert", False, 3) is True

    def test_should_cost_budget_nudge(self, sm):
        assert sm.should_cost_budget("nudge", False, 3) is True

    def test_ambient_free(self, sm):
        assert sm.should_cost_budget("ambient", False, 3) is False

    def test_scheduled_free(self, sm):
        assert sm.should_cost_budget("advisory", True, 3) is False

    def test_urgent_free(self, sm):
        assert sm.should_cost_budget("alert", False, 5) is False

    def test_bundle_free(self, sm):
        assert sm.should_cost_budget("proactive_bundle", False, 3) is False
