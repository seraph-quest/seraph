"""Tests for guardian intervention feedback persistence."""

from src.guardian.feedback import guardian_feedback_repository


async def test_create_intervention_and_feedback_summary(async_db):
    intervention = await guardian_feedback_repository.create_intervention(
        session_id=None,
        message_type="proactive",
        intervention_type="advisory",
        urgency=3,
        content="Stretch and refocus before the next coding block.",
        reasoning="available_capacity",
        is_scheduled=False,
        guardian_confidence="grounded",
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        policy_action="act",
        policy_reason="available_capacity",
        delivery_decision="deliver",
        latest_outcome="pending",
    )

    await guardian_feedback_repository.update_outcome(
        intervention.id,
        latest_outcome="delivered",
        transport="websocket",
    )
    await guardian_feedback_repository.record_feedback(
        intervention.id,
        feedback_type="helpful",
        feedback_note="Good timing.",
    )

    refreshed = await guardian_feedback_repository.get(intervention.id)
    assert refreshed is not None
    assert refreshed.latest_outcome == "feedback_received"
    assert refreshed.transport == "websocket"
    assert refreshed.feedback_type == "helpful"
    assert refreshed.feedback_note == "Good timing."

    summary = await guardian_feedback_repository.summarize_recent(limit=5)
    assert "feedback=helpful" in summary
    assert "available_capacity" in summary
    assert "Stretch and refocus" in summary


async def test_record_feedback_returns_none_for_missing_id(async_db):
    result = await guardian_feedback_repository.record_feedback(
        "missing-id",
        feedback_type="not_helpful",
    )

    assert result is None
