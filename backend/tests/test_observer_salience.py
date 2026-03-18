"""Tests for observer salience calibration."""

from src.observer.salience import derive_observer_assessment


def test_aligned_work_activity_becomes_high_salience():
    assessment = derive_observer_assessment(
        current_event=None,
        upcoming_events=[],
        recent_git_activity=[{"msg": "ship guardian calibration"}],
        active_goals_summary="Ship guardian calibration",
        active_window="VS Code",
        screen_context="Editing guardian policy code",
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
    )

    assert assessment.observer_confidence == "grounded"
    assert assessment.salience_level == "high"
    assert assessment.salience_reason == "aligned_work_activity"
    assert assessment.interruption_cost == "low"


def test_single_signal_goal_state_stays_medium_salience():
    assessment = derive_observer_assessment(
        current_event=None,
        upcoming_events=[],
        recent_git_activity=None,
        active_goals_summary="Ship guardian calibration",
        active_window=None,
        screen_context=None,
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
    )

    assert assessment.salience_level == "medium"
    assert assessment.salience_reason == "active_goals"
