"""Tests for guardian intervention feedback persistence."""

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from src.db.models import GuardianIntervention
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


async def test_learning_signal_biases_after_negative_feedback(async_db):
    first = await guardian_feedback_repository.create_intervention(
        session_id=None,
        message_type="proactive",
        intervention_type="advisory",
        urgency=2,
        content="Nudge to stretch.",
        reasoning="available_capacity",
        is_scheduled=False,
        guardian_confidence="grounded",
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        policy_action="act",
        policy_reason="available_capacity",
        delivery_decision="deliver",
        latest_outcome="delivered",
    )
    await guardian_feedback_repository.record_feedback(first.id, feedback_type="not_helpful")

    second = await guardian_feedback_repository.create_intervention(
        session_id=None,
        message_type="proactive",
        intervention_type="advisory",
        urgency=2,
        content="Another nudge.",
        reasoning="available_capacity",
        is_scheduled=False,
        guardian_confidence="grounded",
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        policy_action="act",
        policy_reason="available_capacity",
        delivery_decision="deliver",
        latest_outcome="delivered",
    )
    await guardian_feedback_repository.record_feedback(second.id, feedback_type="not_helpful")

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")

    assert signal.not_helpful_count == 2
    assert signal.helpful_count == 0
    assert signal.bias == "reduce_interruptions"


async def test_learning_signal_can_prefer_direct_delivery_and_native_channel(async_db):
    for feedback_type, content in (
        ("helpful", "That landed at the right moment."),
        ("helpful", "Another useful nudge."),
        ("acknowledged", "Seen on desktop and acted on it."),
        ("acknowledged", "Acknowledged from the notification center."),
    ):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content=content,
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type=feedback_type)

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")

    assert signal.helpful_count == 2
    assert signal.acknowledged_count == 2
    assert signal.not_helpful_count == 0
    assert signal.bias == "prefer_direct_delivery"
    assert signal.channel_bias == "prefer_native_notification"
    assert signal.escalation_bias == "prefer_async_native"


async def test_learning_signal_tracks_timing_and_blocked_state_biases(async_db):
    for feedback_type, user_state, transport in (
        ("not_helpful", "deep_work", "websocket"),
        ("not_helpful", "in_meeting", "websocket"),
        ("helpful", "away", "native_notification"),
        ("acknowledged", "deep_work", "native_notification"),
    ):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Respect the current focus block.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state=user_state,
            interruption_mode="focus" if user_state != "away" else "balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport=transport,
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type=feedback_type)

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")

    assert signal.timing_bias == "avoid_focus_windows"
    assert signal.blocked_state_bias == "avoid_blocked_state_interruptions"


async def test_learning_signal_keeps_blocked_state_async_bias_from_overriding_direct_delivery(async_db):
    for feedback_type in ("helpful", "helpful", "acknowledged", "acknowledged"):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Surface this asynchronously while blocked.",
            reasoning="active_goals",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="deep_work",
            interruption_mode="focus",
            policy_action="act",
            policy_reason="active_goals",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="native_notification",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type=feedback_type)

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")

    assert signal.blocked_state_bias == "prefer_async_for_blocked_state"
    assert signal.channel_bias == "prefer_native_notification"
    assert signal.bias == "neutral"


async def test_list_recent_can_scope_to_single_session(async_db):
    base_time = datetime.now(timezone.utc)

    first = await guardian_feedback_repository.create_intervention(
        session_id="session-1",
        message_type="proactive",
        intervention_type="advisory",
        urgency=2,
        content="Older session-1 intervention.",
        reasoning="available_capacity",
        is_scheduled=False,
        guardian_confidence="grounded",
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        policy_action="act",
        policy_reason="available_capacity",
        delivery_decision="deliver",
        latest_outcome="delivered",
    )
    second = await guardian_feedback_repository.create_intervention(
        session_id="session-2",
        message_type="proactive",
        intervention_type="advisory",
        urgency=2,
        content="Newer session-2 intervention.",
        reasoning="available_capacity",
        is_scheduled=False,
        guardian_confidence="grounded",
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        policy_action="act",
        policy_reason="available_capacity",
        delivery_decision="deliver",
        latest_outcome="delivered",
    )

    # Persist deterministic timestamps so session-scoped queries are not starved by newer global traffic.
    async with async_db() as db:
        stored_first = (
            await db.execute(select(GuardianIntervention).where(GuardianIntervention.id == first.id))
        ).scalar_one()
        stored_second = (
            await db.execute(select(GuardianIntervention).where(GuardianIntervention.id == second.id))
        ).scalar_one()
        stored_first.updated_at = base_time
        stored_second.updated_at = base_time + timedelta(minutes=5)
        db.add(stored_first)
        db.add(stored_second)
        await db.flush()

    recent_for_session = await guardian_feedback_repository.list_recent(limit=1, session_id="session-1")

    assert [item.id for item in recent_for_session] == [first.id]
