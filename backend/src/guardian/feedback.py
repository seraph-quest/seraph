"""Persistence and summaries for intervention outcomes and user feedback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import GuardianIntervention


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _excerpt(text: str, *, limit: int = 240) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


@dataclass(frozen=True)
class GuardianLearningSignal:
    intervention_type: str
    helpful_count: int
    not_helpful_count: int
    acknowledged_count: int
    failed_count: int
    bias: str
    phrasing_bias: str
    cadence_bias: str
    channel_bias: str
    escalation_bias: str
    timing_bias: str
    blocked_state_bias: str
    suppression_bias: str
    thread_preference_bias: str
    blocked_direct_failure_count: int
    blocked_native_success_count: int
    available_direct_success_count: int

    @classmethod
    def neutral(cls, intervention_type: str) -> "GuardianLearningSignal":
        return cls(
            intervention_type=intervention_type,
            helpful_count=0,
            not_helpful_count=0,
            acknowledged_count=0,
            failed_count=0,
            bias="neutral",
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


class GuardianFeedbackRepository:
    async def create_intervention(
        self,
        *,
        session_id: str | None,
        message_type: str,
        intervention_type: str | None,
        urgency: int | None,
        content: str,
        reasoning: str | None,
        is_scheduled: bool,
        guardian_confidence: str | None,
        data_quality: str | None,
        user_state: str | None,
        interruption_mode: str | None,
        policy_action: str,
        policy_reason: str,
        delivery_decision: str | None,
        latest_outcome: str,
        transport: str | None = None,
        notification_id: str | None = None,
    ) -> GuardianIntervention:
        intervention = GuardianIntervention(
            session_id=session_id,
            message_type=message_type,
            intervention_type=intervention_type or message_type,
            urgency=urgency or 0,
            content_excerpt=_excerpt(content),
            reasoning=reasoning,
            is_scheduled=is_scheduled,
            guardian_confidence=guardian_confidence,
            data_quality=data_quality,
            user_state=user_state,
            interruption_mode=interruption_mode,
            policy_action=policy_action,
            policy_reason=policy_reason,
            delivery_decision=delivery_decision,
            latest_outcome=latest_outcome,
            transport=transport,
            notification_id=notification_id,
        )
        async with get_session() as db:
            db.add(intervention)
            await db.flush()
            await db.refresh(intervention)
        return intervention

    async def get(self, intervention_id: str) -> GuardianIntervention | None:
        async with get_session() as db:
            result = await db.execute(
                select(GuardianIntervention).where(GuardianIntervention.id == intervention_id)
            )
            return result.scalar_one_or_none()

    async def update_outcome(
        self,
        intervention_id: str,
        *,
        latest_outcome: str,
        transport: str | None = None,
        notification_id: str | None = None,
    ) -> GuardianIntervention | None:
        async with get_session() as db:
            result = await db.execute(
                select(GuardianIntervention).where(GuardianIntervention.id == intervention_id)
            )
            intervention = result.scalar_one_or_none()
            if intervention is None:
                return None
            intervention.latest_outcome = latest_outcome
            intervention.updated_at = _now()
            if transport is not None:
                intervention.transport = transport
            if notification_id is not None:
                intervention.notification_id = notification_id
            db.add(intervention)
            await db.flush()
            await db.refresh(intervention)
            return intervention

    async def record_feedback(
        self,
        intervention_id: str,
        *,
        feedback_type: str,
        feedback_note: str | None = None,
        latest_outcome: str = "feedback_received",
    ) -> GuardianIntervention | None:
        async with get_session() as db:
            result = await db.execute(
                select(GuardianIntervention).where(GuardianIntervention.id == intervention_id)
            )
            intervention = result.scalar_one_or_none()
            if intervention is None:
                return None
            intervention.feedback_type = feedback_type
            intervention.feedback_note = (feedback_note or "").strip() or None
            intervention.feedback_at = _now()
            intervention.updated_at = intervention.feedback_at
            intervention.latest_outcome = latest_outcome
            db.add(intervention)
            await db.flush()
            await db.refresh(intervention)
            return intervention

    async def list_recent(
        self,
        *,
        limit: int = 5,
        session_id: str | None = None,
    ) -> list[GuardianIntervention]:
        async with get_session() as db:
            query = select(GuardianIntervention)
            if session_id:
                query = query.where(GuardianIntervention.session_id == session_id)
            result = await db.execute(
                query.order_by(GuardianIntervention.updated_at.desc()).limit(limit)
            )
            return list(result.scalars().all())

    async def summarize_recent(self, *, limit: int = 5) -> str:
        interventions = await self.list_recent(limit=limit)
        lines: list[str] = []
        for item in interventions:
            parts = [item.intervention_type]
            if item.latest_outcome:
                parts.append(item.latest_outcome.replace("_", " "))
            if item.feedback_type:
                parts.append(f"feedback={item.feedback_type.replace('_', ' ')}")
            if item.policy_reason:
                parts.append(f"reason={item.policy_reason}")
            if item.transport:
                parts.append(f"via {item.transport}")
            summary = ", ".join(parts)
            if item.content_excerpt:
                summary += f": {item.content_excerpt}"
            lines.append(f"- {summary}")
        return "\n".join(lines)

    async def get_learning_signal(
        self,
        *,
        intervention_type: str,
        limit: int = 12,
    ) -> GuardianLearningSignal:
        async with get_session() as db:
            result = await db.execute(
                select(GuardianIntervention)
                .where(GuardianIntervention.intervention_type == intervention_type)
                .order_by(GuardianIntervention.updated_at.desc())
                .limit(limit)
            )
            interventions = list(result.scalars().all())

        helpful_count = sum(1 for item in interventions if item.feedback_type == "helpful")
        not_helpful_count = sum(1 for item in interventions if item.feedback_type == "not_helpful")
        acknowledged_count = sum(1 for item in interventions if item.feedback_type == "acknowledged")
        failed_count = sum(1 for item in interventions if item.latest_outcome == "failed")
        blocked_state_interventions = [
            item
            for item in interventions
            if item.user_state in {"deep_work", "in_meeting", "away"}
        ]
        blocked_direct_failures = sum(
            1
            for item in blocked_state_interventions
            if (
                item.transport != "native_notification"
                and (
                    item.feedback_type == "not_helpful"
                    or item.latest_outcome == "failed"
                )
            )
        )
        blocked_state_positive_native = sum(
            1
            for item in blocked_state_interventions
            if item.transport == "native_notification"
            and item.feedback_type in {"helpful", "acknowledged"}
        )
        available_window_positive = sum(
            1
            for item in interventions
            if item.user_state == "available"
            and item.transport != "native_notification"
            and item.feedback_type in {"helpful", "acknowledged"}
        )

        bias = "neutral"
        if not_helpful_count >= 2 or (
            not_helpful_count >= 1 and helpful_count == 0 and failed_count >= 1
        ):
            bias = "reduce_interruptions"
        elif available_window_positive >= 2 and not_helpful_count == 0:
            bias = "prefer_direct_delivery"

        phrasing_bias = "neutral"
        if not_helpful_count >= 2 and helpful_count == 0:
            phrasing_bias = "be_brief_and_literal"
        elif helpful_count >= 2 and not_helpful_count == 0:
            phrasing_bias = "be_more_direct"

        cadence_bias = "neutral"
        if not_helpful_count >= 2 or failed_count >= 2:
            cadence_bias = "bundle_more"
        elif helpful_count >= 2 and acknowledged_count >= 1 and not_helpful_count == 0:
            cadence_bias = "check_in_sooner"

        channel_bias = "neutral"
        if acknowledged_count >= 2 and not_helpful_count == 0:
            channel_bias = "prefer_native_notification"

        escalation_bias = "neutral"
        if acknowledged_count >= 2 and helpful_count >= 1 and not_helpful_count == 0:
            escalation_bias = "prefer_async_native"

        timing_bias = "neutral"
        if blocked_direct_failures >= 2:
            timing_bias = "avoid_focus_windows"
        elif available_window_positive >= 2 and not_helpful_count == 0:
            timing_bias = "prefer_available_windows"

        blocked_state_bias = "neutral"
        if blocked_direct_failures >= 2:
            blocked_state_bias = "avoid_blocked_state_interruptions"
        elif blocked_state_positive_native >= 2 and blocked_direct_failures == 0:
            blocked_state_bias = "prefer_async_for_blocked_state"

        suppression_bias = "neutral"
        if not_helpful_count >= 3 or failed_count >= 2:
            suppression_bias = "extend_suppression"
        elif helpful_count >= 2 and not_helpful_count == 0:
            suppression_bias = "resume_faster"

        thread_preference_bias = "neutral"
        if helpful_count + acknowledged_count >= 2 and not_helpful_count == 0:
            thread_preference_bias = "prefer_existing_thread"
        elif failed_count >= 2:
            thread_preference_bias = "prefer_clean_thread"

        return GuardianLearningSignal(
            intervention_type=intervention_type,
            helpful_count=helpful_count,
            not_helpful_count=not_helpful_count,
            acknowledged_count=acknowledged_count,
            failed_count=failed_count,
            bias=bias,
            phrasing_bias=phrasing_bias,
            cadence_bias=cadence_bias,
            channel_bias=channel_bias,
            escalation_bias=escalation_bias,
            timing_bias=timing_bias,
            blocked_state_bias=blocked_state_bias,
            suppression_bias=suppression_bias,
            thread_preference_bias=thread_preference_bias,
            blocked_direct_failure_count=blocked_direct_failures,
            blocked_native_success_count=blocked_state_positive_native,
            available_direct_success_count=available_window_positive,
        )


guardian_feedback_repository = GuardianFeedbackRepository()
