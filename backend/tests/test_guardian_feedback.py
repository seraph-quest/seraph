"""Tests for guardian intervention feedback persistence."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from src.agent.session import SessionManager
from src.db.models import GuardianIntervention, MemoryKind
from src.guardian.feedback import GuardianLearningSignal, guardian_feedback_repository
from src.memory.procedural import sync_learning_signal_memories
from src.memory.procedural_guidance import load_procedural_memory_guidance
from src.memory.repository import memory_repository


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
        transport="websocket",
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
        transport="websocket",
    )
    await guardian_feedback_repository.record_feedback(second.id, feedback_type="not_helpful")

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")

    assert signal.not_helpful_count == 2
    assert signal.helpful_count == 0
    assert signal.bias == "reduce_interruptions"


async def test_learning_signal_can_prefer_direct_delivery_and_native_channel(async_db):
    for feedback_type, content, transport in (
        ("helpful", "That landed at the right moment.", "websocket"),
        ("helpful", "Another useful nudge.", "websocket"),
        ("helpful", "That native ping still helped.", "native_notification"),
        ("acknowledged", "Acknowledged from the notification center.", "native_notification"),
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
            transport=transport,
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type=feedback_type)

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")

    assert signal.helpful_count == 3
    assert signal.acknowledged_count == 1
    assert signal.not_helpful_count == 0
    assert signal.bias == "prefer_direct_delivery"
    assert signal.channel_bias == "prefer_native_notification"
    assert signal.escalation_bias == "prefer_async_native"
    assert signal.evidence_for_axis("channel").support_count == 2
    assert signal.evidence_for_axis("escalation").support_count == 2


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
    sm = SessionManager()
    await sm.get_or_create("session-1")
    await sm.get_or_create("session-2")

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


async def test_feedback_updates_materialize_procedural_memories(async_db):
    sm = SessionManager()
    await sm.get_or_create("session-procedural")

    for feedback_type, user_state, transport in (
        ("not_helpful", "deep_work", "websocket"),
        ("not_helpful", "in_meeting", "websocket"),
        ("helpful", "away", "native_notification"),
        ("acknowledged", "deep_work", "native_notification"),
    ):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id="session-procedural",
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Respect the focus block.",
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

    memories = await memory_repository.list_memories(kind=MemoryKind.procedural, limit=20)
    procedural_by_lesson = {
        json.loads(memory.metadata_json or "{}").get("lesson_type"): memory
        for memory in memories
    }

    assert "timing" in procedural_by_lesson
    assert "blocked_state" in procedural_by_lesson
    assert "avoid direct interruption" in procedural_by_lesson["timing"].content.lower()
    assert "prefer bundling" in procedural_by_lesson["blocked_state"].content.lower()


async def test_procedural_memory_sync_updates_in_place_and_archives_neutral_lessons(async_db):
    first_signal = GuardianLearningSignal(
        intervention_type="advisory",
        helpful_count=0,
        not_helpful_count=2,
        acknowledged_count=0,
        failed_count=0,
        bias="reduce_interruptions",
        phrasing_bias="neutral",
        cadence_bias="neutral",
        channel_bias="neutral",
        escalation_bias="neutral",
        timing_bias="avoid_focus_windows",
        blocked_state_bias="neutral",
        suppression_bias="neutral",
        thread_preference_bias="neutral",
        blocked_direct_failure_count=2,
        blocked_native_success_count=0,
        available_direct_success_count=0,
    )
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=first_signal,
        source_session_id="session-procedural",
    )

    initial_memories = await memory_repository.list_memories(kind=MemoryKind.procedural, limit=20)
    timing_memory = next(
        memory
        for memory in initial_memories
        if json.loads(memory.metadata_json or "{}").get("lesson_type") == "timing"
    )

    second_signal = GuardianLearningSignal(
        intervention_type="advisory",
        helpful_count=2,
        not_helpful_count=0,
        acknowledged_count=0,
        failed_count=0,
        bias="neutral",
        phrasing_bias="neutral",
        cadence_bias="neutral",
        channel_bias="neutral",
        escalation_bias="neutral",
        timing_bias="prefer_available_windows",
        blocked_state_bias="neutral",
        suppression_bias="neutral",
        thread_preference_bias="neutral",
        blocked_direct_failure_count=0,
        blocked_native_success_count=0,
        available_direct_success_count=2,
    )
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=second_signal,
        source_session_id="session-procedural",
    )

    active_memories = await memory_repository.list_memories(kind=MemoryKind.procedural, limit=20)
    updated_timing_memory = next(
        memory
        for memory in active_memories
        if json.loads(memory.metadata_json or "{}").get("lesson_type") == "timing"
    )

    assert updated_timing_memory.id == timing_memory.id
    assert "explicitly available" in updated_timing_memory.content.lower()

    neutral_signal = GuardianLearningSignal.neutral("advisory")
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=neutral_signal,
        source_session_id="session-procedural",
    )

    archived_memories = await memory_repository.list_memories(
        kind=MemoryKind.procedural,
        limit=20,
        status="archived",
    )
    archived_lesson_types = {
        json.loads(memory.metadata_json or "{}").get("lesson_type")
        for memory in archived_memories
    }

    assert "timing" in archived_lesson_types


async def test_procedural_memory_sync_uses_intervention_specific_wording(async_db):
    signal = GuardianLearningSignal(
        intervention_type="alert",
        helpful_count=0,
        not_helpful_count=2,
        acknowledged_count=0,
        failed_count=0,
        bias="reduce_interruptions",
        phrasing_bias="neutral",
        cadence_bias="neutral",
        channel_bias="neutral",
        escalation_bias="neutral",
        timing_bias="neutral",
        blocked_state_bias="neutral",
        suppression_bias="neutral",
        thread_preference_bias="neutral",
        blocked_direct_failure_count=0,
        blocked_native_success_count=0,
        available_direct_success_count=0,
    )

    await sync_learning_signal_memories(
        intervention_type="alert",
        signal=signal,
        source_session_id="session-alert",
    )

    delivery_memory = next(
        memory
        for memory in await memory_repository.list_memories(kind=MemoryKind.procedural, limit=20)
        if json.loads(memory.metadata_json or "{}").get("lesson_type") == "delivery"
    )

    assert "for alert interventions" in delivery_memory.content.lower()
    assert "advisory" not in delivery_memory.content.lower()
    assert delivery_memory.summary == delivery_memory.content


async def test_update_outcome_recomputes_procedural_memories_when_failure_is_cleared(async_db):
    sm = SessionManager()
    await sm.get_or_create("session-outcome-refresh")

    interventions = []
    for _ in range(2):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id="session-outcome-refresh",
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Retry this later.",
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
            transport="websocket",
        )
        interventions.append(intervention)
        await guardian_feedback_repository.update_outcome(
            intervention.id,
            latest_outcome="failed",
            transport="websocket",
        )

    active_after_failures = await memory_repository.list_memories(
        kind=MemoryKind.procedural,
        limit=20,
    )
    active_lesson_types = {
        json.loads(memory.metadata_json or "{}").get("lesson_type")
        for memory in active_after_failures
    }
    assert "suppression" in active_lesson_types

    for intervention in interventions:
        await guardian_feedback_repository.update_outcome(
            intervention.id,
            latest_outcome="delivered",
            transport="websocket",
        )

    active_after_recovery = await memory_repository.list_memories(
        kind=MemoryKind.procedural,
        limit=20,
    )
    recovered_lesson_types = {
        json.loads(memory.metadata_json or "{}").get("lesson_type")
        for memory in active_after_recovery
    }
    archived_after_recovery = await memory_repository.list_memories(
        kind=MemoryKind.procedural,
        limit=20,
        status="archived",
    )
    archived_lesson_types = {
        json.loads(memory.metadata_json or "{}").get("lesson_type")
        for memory in archived_after_recovery
    }

    assert "suppression" not in recovered_lesson_types
    assert "suppression" in archived_lesson_types


async def test_sync_scoped_memory_serializes_concurrent_same_scope_writes(async_db):
    scope = {
        "writer": "guardian_feedback",
        "memory_scope": "procedural_learning",
        "intervention_type": "advisory",
        "lesson_type": "delivery",
    }
    content = "For advisory interventions, reduce direct interruptions after recent negative or failed outcomes."

    await asyncio.gather(
        *[
            memory_repository.sync_scoped_memory(
                kind=MemoryKind.procedural,
                scope=scope,
                content=content,
                summary=content,
            )
            for _ in range(5)
        ]
    )

    matching = [
        memory
        for memory in await memory_repository.list_memories(kind=MemoryKind.procedural, limit=20)
        if json.loads(memory.metadata_json or "{}").get("lesson_type") == "delivery"
    ]

    assert len(matching) == 1
    assert matching[0].scope_key is not None


async def test_load_procedural_memory_guidance_ignores_other_writers(async_db):
    guardian_scope = {
        "writer": "guardian_feedback",
        "memory_scope": "procedural_learning",
        "intervention_type": "advisory",
        "lesson_type": "channel",
    }
    foreign_scope = {
        "writer": "other_writer",
        "memory_scope": "procedural_learning",
        "intervention_type": "advisory",
        "lesson_type": "channel",
    }

    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope=guardian_scope,
        content="For advisory interventions, async native notification is usually tolerated better than browser interruption.",
        summary="For advisory interventions, async native notification is usually tolerated better than browser interruption.",
        metadata={"bias_value": "prefer_native_notification"},
    )
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope=foreign_scope,
        content="Foreign writer guidance should not override guardian feedback lessons.",
        summary="Foreign writer guidance should not override guardian feedback lessons.",
        metadata={"bias_value": "prefer_browser_interrupt"},
        importance=0.99,
    )

    guidance = await load_procedural_memory_guidance("advisory")

    assert guidance.channel_bias == "prefer_native_notification"
    assert guidance.lesson_types == ("channel",)


async def test_learning_signal_exposes_comparable_live_axis_evidence(async_db):
    base_time = datetime.now(timezone.utc)
    interventions = []
    for offset_days in (2, 1):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Helpful direct delivery.",
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
            transport="websocket",
        )
        interventions.append(intervention)
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type="helpful")

        async with async_db() as db:
            stored = (
                await db.execute(
                    select(GuardianIntervention).where(GuardianIntervention.id == intervention.id)
                )
            ).scalar_one()
            stored.updated_at = base_time - timedelta(days=offset_days)
            db.add(stored)
            await db.flush()

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")
    delivery_evidence = signal.evidence_for_axis("delivery")

    assert len(signal.axis_evidence) == 9
    assert delivery_evidence.bias == "prefer_direct_delivery"
    assert delivery_evidence.support_count == 2
    assert delivery_evidence.confidence_score == pytest.approx(1.0)
    assert delivery_evidence.quality_score == pytest.approx(1.0)
    assert delivery_evidence.recency_score == pytest.approx(29 / 30, abs=0.02)


async def test_neutral_live_axis_evidence_stays_neutral(async_db):
    base_time = datetime.now(timezone.utc)
    for offset_days, feedback_type in ((2, "helpful"), (1, "not_helpful")):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Mixed direct delivery signal.",
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
            transport="websocket",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type=feedback_type)

        async with async_db() as db:
            stored = (
                await db.execute(
                    select(GuardianIntervention).where(GuardianIntervention.id == intervention.id)
                )
            ).scalar_one()
            stored.updated_at = base_time - timedelta(days=offset_days)
            db.add(stored)
            await db.flush()

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")
    delivery_evidence = signal.evidence_for_axis("delivery")

    assert signal.bias == "neutral"
    assert delivery_evidence.bias == "neutral"
    assert delivery_evidence.support_count == 0
    assert delivery_evidence.confidence_score == pytest.approx(0.0)
    assert delivery_evidence.quality_score == pytest.approx(0.0)
    assert delivery_evidence.recency_score == pytest.approx(0.0)


async def test_live_axis_evidence_ignores_newer_rows_that_support_the_other_bias(async_db):
    base_time = datetime.now(timezone.utc)
    for offset_days, feedback_type in ((6, "not_helpful"), (5, "not_helpful"), (1, "helpful")):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Keep the learning direction honest.",
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
            transport="websocket",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type=feedback_type)

        async with async_db() as db:
            stored = (
                await db.execute(
                    select(GuardianIntervention).where(GuardianIntervention.id == intervention.id)
                )
            ).scalar_one()
            stored.updated_at = base_time - timedelta(days=offset_days)
            db.add(stored)
            await db.flush()

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")
    delivery_evidence = signal.evidence_for_axis("delivery")
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=signal,
        source_session_id=None,
    )

    assert signal.bias == "reduce_interruptions"
    assert delivery_evidence.bias == "reduce_interruptions"
    assert delivery_evidence.support_count == 2
    assert delivery_evidence.recency_score == pytest.approx(25 / 30, abs=0.02)

    delivery_memory = (
        await memory_repository.list_memories_for_scope(
            kind=MemoryKind.procedural,
            scope={
                "writer": "guardian_feedback",
                "memory_scope": "procedural_learning",
                "intervention_type": "advisory",
                "lesson_type": "delivery",
            },
            limit=1,
        )
    )[0]
    delivery_metadata = json.loads(delivery_memory.metadata_json or "{}")
    assert delivery_metadata["support_count"] == 2
    assert delivery_metadata["evidence_count"] == 3


async def test_learning_signal_prefers_grounded_delivery_evidence_over_degraded_negative_volume(async_db):
    base_time = datetime.now(timezone.utc)
    for offset_days, feedback_type, guardian_confidence, data_quality in (
        (7, "not_helpful", "degraded", "degraded"),
        (6, "not_helpful", "degraded", "degraded"),
        (5, "not_helpful", "degraded", "degraded"),
        (2, "helpful", "grounded", "good"),
        (1, "helpful", "grounded", "good"),
    ):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Weight grounded evidence higher than noisy degraded history.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence=guardian_confidence,
            data_quality=data_quality,
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="websocket",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type=feedback_type)

        async with async_db() as db:
            stored = (
                await db.execute(
                    select(GuardianIntervention).where(GuardianIntervention.id == intervention.id)
                )
            ).scalar_one()
            stored.updated_at = base_time - timedelta(days=offset_days)
            db.add(stored)
            await db.flush()

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")
    delivery_evidence = signal.evidence_for_axis("delivery")

    assert signal.bias == "prefer_direct_delivery"
    assert delivery_evidence.bias == "prefer_direct_delivery"
    assert delivery_evidence.support_count == 2
    assert delivery_evidence.weighted_support == pytest.approx(2.0)


async def test_learning_signal_ignores_unobserved_phrasing_cadence_and_thread_axes(async_db):
    for feedback_type in ("helpful", "helpful", "not_helpful", "not_helpful", "failed", "failed"):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id="existing-thread",
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Do not manufacture unsupported learning axes.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="failed" if feedback_type == "failed" else "delivered",
            transport="websocket",
        )
        if feedback_type != "failed":
            await guardian_feedback_repository.record_feedback(
                intervention.id,
                feedback_type=feedback_type,
            )

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")

    assert signal.phrasing_bias == "neutral"
    assert signal.cadence_bias == "neutral"
    assert signal.thread_preference_bias == "neutral"
    assert signal.evidence_for_axis("phrasing").support_count == 0
    assert signal.evidence_for_axis("cadence").support_count == 0
    assert signal.evidence_for_axis("thread").support_count == 0


async def test_learning_signal_does_not_treat_native_failures_as_direct_delivery_evidence(async_db):
    for _ in range(2):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="A bad native ping should not become a direct-delivery lesson.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="failed",
            transport="native_notification",
        )
        await guardian_feedback_repository.record_feedback(
            intervention.id,
            feedback_type="not_helpful",
        )

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")

    assert signal.bias == "neutral"
    assert signal.evidence_for_axis("delivery").support_count == 0
    assert signal.evidence_for_axis("delivery").weighted_support == pytest.approx(0.0)


async def test_delivery_outcomes_can_strengthen_timing_and_blocked_state_learning(async_db):
    for user_state, transport in (
        ("available", "websocket"),
        ("available", "websocket"),
        ("deep_work", "native_notification"),
        ("deep_work", "native_notification"),
    ):
        await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Use actual delivery outcomes to shape timing and channel guidance.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state=user_state,
            interruption_mode="focus" if user_state == "deep_work" else "balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport=transport,
        )

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")
    timing_evidence = signal.evidence_for_axis("timing")
    blocked_state_evidence = signal.evidence_for_axis("blocked_state")
    channel_evidence = signal.evidence_for_axis("channel")

    assert signal.timing_bias == "prefer_available_windows"
    assert signal.blocked_state_bias == "prefer_async_for_blocked_state"
    assert signal.channel_bias == "prefer_native_notification"
    assert timing_evidence.support_count == 2
    assert timing_evidence.weighted_support == pytest.approx(1.4)
    assert blocked_state_evidence.support_count == 2
    assert blocked_state_evidence.weighted_support == pytest.approx(1.4)
    assert channel_evidence.support_count == 2
    assert channel_evidence.weighted_support == pytest.approx(1.4)


async def test_sync_learning_signal_memories_skips_unobserved_axis_lessons(async_db):
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=2,
            not_helpful_count=2,
            acknowledged_count=0,
            failed_count=0,
            bias="prefer_direct_delivery",
            phrasing_bias="be_more_direct",
            cadence_bias="bundle_more",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="prefer_available_windows",
            blocked_state_bias="neutral",
            suppression_bias="resume_faster",
            thread_preference_bias="prefer_existing_thread",
            blocked_direct_failure_count=0,
            blocked_native_success_count=0,
            available_direct_success_count=2,
        ),
    )

    lesson_types = {
        json.loads(memory.metadata_json or "{}").get("lesson_type")
        for memory in await memory_repository.list_memories(kind=MemoryKind.procedural, limit=20)
    }

    assert "delivery" in lesson_types
    assert "timing" in lesson_types
    assert "suppression" in lesson_types
    assert "phrasing" not in lesson_types
    assert "cadence" not in lesson_types
    assert "thread" not in lesson_types


async def test_load_procedural_memory_guidance_handles_partial_stale_metadata(async_db):
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "channel",
        },
        content="For advisory interventions, async native notification is usually tolerated better than browser interruption.",
        summary="For advisory interventions, async native notification is usually tolerated better than browser interruption.",
        confidence=0.82,
        reinforcement=1.4,
        last_confirmed_at=datetime.now(timezone.utc) - timedelta(days=60),
        metadata={"bias_value": "prefer_native_notification"},
    )

    guidance = await load_procedural_memory_guidance("advisory")
    channel_evidence = guidance.evidence_for_axis("channel")

    assert len(guidance.axis_evidence) == 9
    assert guidance.channel_bias == "prefer_native_notification"
    assert channel_evidence.support_count == 0
    assert channel_evidence.metadata_complete is False
    assert channel_evidence.confidence_score == pytest.approx(0.82)
    assert channel_evidence.recency_score == pytest.approx(0.0)


async def test_load_procedural_memory_guidance_prefers_explicit_support_count(async_db):
    confirmed_at = datetime.now(timezone.utc) - timedelta(days=1)
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "delivery",
        },
        content="For advisory interventions, direct delivery is usually tolerated when the user is available.",
        summary="For advisory interventions, direct delivery is usually tolerated when the user is available.",
        confidence=0.9,
        reinforcement=1.6,
        last_confirmed_at=confirmed_at,
        metadata={
            "bias_value": "prefer_direct_delivery",
            "support_count": 2,
            "weighted_support": 1.7,
            "evidence_count": 3,
            "evidence_confidence_score": 0.84,
            "evidence_quality_score": 0.91,
            "evidence_last_confirmed_at": confirmed_at.isoformat(),
        },
    )

    guidance = await load_procedural_memory_guidance("advisory")
    delivery_evidence = guidance.evidence_for_axis("delivery")

    assert guidance.bias == "prefer_direct_delivery"
    assert delivery_evidence.support_count == 2
    assert delivery_evidence.weighted_support == pytest.approx(1.7)
    assert delivery_evidence.metadata_complete is True
    assert delivery_evidence.confidence_score == pytest.approx(0.84)
    assert delivery_evidence.quality_score == pytest.approx(0.91)


async def test_load_procedural_memory_guidance_falls_back_to_legacy_evidence_count(async_db):
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "delivery",
        },
        content="Legacy advisory delivery guidance without explicit comparable evidence metadata.",
        summary="Legacy advisory delivery guidance without explicit comparable evidence metadata.",
        confidence=0.88,
        reinforcement=1.4,
        last_confirmed_at=datetime.now(timezone.utc) - timedelta(days=2),
        metadata={
            "bias_value": "prefer_direct_delivery",
            "evidence_count": 3,
        },
    )

    guidance = await load_procedural_memory_guidance("advisory")
    delivery_evidence = guidance.evidence_for_axis("delivery")

    assert guidance.bias == "prefer_direct_delivery"
    assert delivery_evidence.support_count == 3
    assert delivery_evidence.weighted_support == pytest.approx(3.0)
    assert delivery_evidence.metadata_complete is False
    assert delivery_evidence.confidence_score == pytest.approx(0.88)


async def test_live_and_procedural_axis_evidence_use_matching_support_counts(async_db):
    for feedback_type, content, transport in (
        ("helpful", "That landed at the right moment.", "websocket"),
        ("helpful", "Another useful nudge.", "websocket"),
        ("helpful", "That native ping still helped.", "native_notification"),
        ("acknowledged", "Acknowledged from the notification center.", "native_notification"),
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
            transport=transport,
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type=feedback_type)

    signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")
    await sync_learning_signal_memories(intervention_type="advisory", signal=signal)
    guidance = await load_procedural_memory_guidance("advisory")
    live_delivery = signal.evidence_for_axis("delivery")
    live_channel = signal.evidence_for_axis("channel")
    live_escalation = signal.evidence_for_axis("escalation")
    memory_delivery = guidance.evidence_for_axis("delivery")
    memory_channel = guidance.evidence_for_axis("channel")
    memory_escalation = guidance.evidence_for_axis("escalation")

    assert signal.channel_bias == guidance.channel_bias == "prefer_native_notification"
    assert signal.escalation_bias == guidance.escalation_bias == "prefer_async_native"
    assert signal.bias == guidance.bias == "prefer_direct_delivery"
    assert live_delivery.support_count == memory_delivery.support_count
    assert live_channel.support_count == memory_channel.support_count
    assert live_escalation.support_count == memory_escalation.support_count
    assert live_delivery.weighted_support == pytest.approx(memory_delivery.weighted_support, abs=0.001)
    assert live_channel.weighted_support == pytest.approx(memory_channel.weighted_support, abs=0.001)
    assert live_escalation.weighted_support == pytest.approx(memory_escalation.weighted_support, abs=0.001)
    assert live_delivery.confidence_score == pytest.approx(memory_delivery.confidence_score, abs=0.001)
    assert live_channel.confidence_score == pytest.approx(memory_channel.confidence_score, abs=0.001)
    assert live_escalation.confidence_score == pytest.approx(memory_escalation.confidence_score, abs=0.001)
    assert live_delivery.quality_score == pytest.approx(memory_delivery.quality_score, abs=0.001)
    assert live_channel.quality_score == pytest.approx(memory_channel.quality_score, abs=0.001)
    assert live_escalation.quality_score == pytest.approx(memory_escalation.quality_score, abs=0.001)
    assert live_delivery.recency_score == pytest.approx(memory_delivery.recency_score, abs=0.001)
    assert live_channel.recency_score == pytest.approx(memory_channel.recency_score, abs=0.001)
    assert live_escalation.recency_score == pytest.approx(memory_escalation.recency_score, abs=0.001)
    assert memory_delivery.metadata_complete is True
    assert memory_channel.metadata_complete is True
    assert memory_escalation.metadata_complete is True

    delivery_memory = (
        await memory_repository.list_memories_for_scope(
            kind=MemoryKind.procedural,
            scope={
                "writer": "guardian_feedback",
                "memory_scope": "procedural_learning",
                "intervention_type": "advisory",
                "lesson_type": "delivery",
            },
            limit=1,
        )
    )[0]
    delivery_metadata = json.loads(delivery_memory.metadata_json or "{}")
    assert delivery_metadata["weighted_support"] == pytest.approx(live_delivery.weighted_support, abs=0.001)


async def test_load_procedural_memory_guidance_prefers_thread_and_project_scope(async_db):
    for _index in range(2):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id="atlas-thread",
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Helpful Atlas delivery.",
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
            transport="websocket",
            active_project="Atlas",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type="helpful")

    for _index in range(2):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id="hermes-thread",
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="This interruption landed badly.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="deep_work",
            interruption_mode="focus",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="websocket",
            active_project="Hermes",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type="not_helpful")

    scoped_guidance = await load_procedural_memory_guidance(
        "advisory",
        continuity_thread_id="atlas-thread",
        active_project="Atlas",
    )
    project_guidance = await load_procedural_memory_guidance(
        "advisory",
        active_project="Atlas",
    )
    global_guidance = await load_procedural_memory_guidance("advisory")

    assert scoped_guidance.bias == "prefer_direct_delivery"
    assert project_guidance.bias == "prefer_direct_delivery"
    assert global_guidance.bias == "neutral"


async def test_load_procedural_memory_guidance_prefers_thread_scope_over_project_scope(async_db):
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "delivery",
            "active_project": "Atlas",
        },
        content="Project guidance prefers direct delivery while working on Atlas.",
        summary="Project guidance prefers direct delivery while working on Atlas.",
        confidence=0.85,
        reinforcement=1.4,
        metadata={
            "bias_value": "prefer_direct_delivery",
            "support_count": 3,
            "evidence_count": 3,
        },
    )
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "delivery",
            "continuity_thread_id": "session-1",
        },
        content="This thread has gone badly; reduce interruptions.",
        summary="This thread has gone badly; reduce interruptions.",
        confidence=0.9,
        reinforcement=1.6,
        metadata={
            "bias_value": "reduce_interruptions",
            "support_count": 2,
            "evidence_count": 2,
        },
    )

    guidance = await load_procedural_memory_guidance(
        "advisory",
        continuity_thread_id="session-1",
        active_project="Atlas",
    )

    assert guidance.bias == "reduce_interruptions"
    assert guidance.evidence_for_axis("delivery").support_count == 2


async def test_load_procedural_memory_guidance_selects_first_nonempty_scope_tier(async_db):
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "timing",
        },
        content="Global guidance prefers available windows.",
        summary="Global guidance prefers available windows.",
        confidence=0.7,
        reinforcement=1.2,
        metadata={
            "bias_value": "prefer_available_windows",
            "support_count": 2,
            "evidence_count": 2,
        },
    )
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "timing",
            "active_project": "Atlas",
        },
        content="Atlas-specific timing avoids focus windows.",
        summary="Atlas-specific timing avoids focus windows.",
        confidence=0.8,
        reinforcement=1.4,
        metadata={
            "bias_value": "avoid_focus_windows",
            "support_count": 3,
            "evidence_count": 3,
        },
    )

    scoped = await load_procedural_memory_guidance(
        "advisory",
        continuity_thread_id="session-missing",
        active_project="Atlas",
    )
    global_only = await load_procedural_memory_guidance(
        "advisory",
        continuity_thread_id="session-missing",
        active_project="Hermes",
    )

    assert scoped.timing_bias == "avoid_focus_windows"
    assert scoped.evidence_for_axis("timing").support_count == 3
    assert global_only.timing_bias == "prefer_available_windows"
    assert global_only.evidence_for_axis("timing").support_count == 2


async def test_load_procedural_memory_guidance_prefers_scoped_thread_and_project_context(async_db):
    base_scope = {
        "writer": "guardian_feedback",
        "memory_scope": "procedural_learning",
        "intervention_type": "advisory",
        "lesson_type": "timing",
    }

    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope=base_scope,
        content="Global: prefer available windows.",
        summary="Global: prefer available windows.",
        confidence=0.7,
        reinforcement=1.2,
        metadata={"bias_value": "prefer_available_windows", "support_count": 2},
    )
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={**base_scope, "continuity_thread_id": "thread-1"},
        content="Thread: avoid focus windows.",
        summary="Thread: avoid focus windows.",
        confidence=0.8,
        reinforcement=1.4,
        metadata={"bias_value": "avoid_focus_windows", "support_count": 2},
    )
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={**base_scope, "active_project": "Atlas"},
        content="Project: prefer available windows.",
        summary="Project: prefer available windows.",
        confidence=0.85,
        reinforcement=1.4,
        metadata={"bias_value": "prefer_available_windows", "support_count": 2},
    )
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            **base_scope,
            "continuity_thread_id": "thread-1",
            "active_project": "Atlas",
        },
        content="Thread and project: avoid focus windows.",
        summary="Thread and project: avoid focus windows.",
        confidence=0.9,
        reinforcement=1.6,
        metadata={"bias_value": "avoid_focus_windows", "support_count": 3},
    )

    combined = await load_procedural_memory_guidance(
        "advisory",
        continuity_thread_id="thread-1",
        active_project="Atlas",
    )
    project_only = await load_procedural_memory_guidance(
        "advisory",
        continuity_thread_id="thread-2",
        active_project="Atlas",
    )
    thread_only = await load_procedural_memory_guidance(
        "advisory",
        continuity_thread_id="thread-1",
        active_project="Other",
    )
    global_only = await load_procedural_memory_guidance(
        "advisory",
        continuity_thread_id="thread-2",
        active_project="Other",
    )

    assert combined.timing_bias == "avoid_focus_windows"
    assert project_only.timing_bias == "prefer_available_windows"
    assert thread_only.timing_bias == "avoid_focus_windows"
    assert global_only.timing_bias == "prefer_available_windows"


async def test_load_procedural_memory_guidance_does_not_mix_broader_lessons_into_selected_scope(async_db):
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=0,
            not_helpful_count=2,
            acknowledged_count=0,
            failed_count=0,
            bias="reduce_interruptions",
            phrasing_bias="neutral",
            cadence_bias="bundle_more",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="neutral",
            blocked_state_bias="neutral",
            suppression_bias="neutral",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=0,
            blocked_native_success_count=0,
            available_direct_success_count=0,
        ),
    )
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=2,
            not_helpful_count=0,
            acknowledged_count=0,
            failed_count=0,
            bias="prefer_direct_delivery",
            phrasing_bias="be_more_direct",
            cadence_bias="neutral",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="prefer_available_windows",
            blocked_state_bias="neutral",
            suppression_bias="resume_faster",
            thread_preference_bias="prefer_existing_thread",
            blocked_direct_failure_count=0,
            blocked_native_success_count=0,
            available_direct_success_count=2,
        ),
        continuity_thread_id="atlas-thread",
        active_project="Atlas",
    )

    guidance = await load_procedural_memory_guidance(
        "advisory",
        continuity_thread_id="atlas-thread",
        active_project="Atlas",
    )

    assert guidance.bias == "prefer_direct_delivery"
    assert guidance.cadence_bias == "neutral"
    assert guidance.timing_bias == "prefer_available_windows"


async def test_feedback_refresh_writes_thread_and_project_scoped_memories(async_db):
    for _ in range(2):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id="atlas-thread",
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Atlas work should not be interrupted directly.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            active_project="Atlas",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="websocket",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type="not_helpful")

    scoped_memories = await memory_repository.list_memories_for_scope(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "delivery",
            "continuity_thread_id": "atlas-thread",
            "active_project": "Atlas",
        },
        limit=4,
    )

    assert len(scoped_memories) == 1
    assert json.loads(scoped_memories[0].metadata_json or "{}")["bias_value"] == "reduce_interruptions"
