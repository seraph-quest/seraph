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
    channel_bias: str
    escalation_bias: str

    @classmethod
    def neutral(cls, intervention_type: str) -> "GuardianLearningSignal":
        return cls(
            intervention_type=intervention_type,
            helpful_count=0,
            not_helpful_count=0,
            acknowledged_count=0,
            failed_count=0,
            bias="neutral",
            channel_bias="neutral",
            escalation_bias="neutral",
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

    async def list_recent(self, *, limit: int = 5) -> list[GuardianIntervention]:
        async with get_session() as db:
            result = await db.execute(
                select(GuardianIntervention)
                .order_by(GuardianIntervention.updated_at.desc())
                .limit(limit)
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

        bias = "neutral"
        if not_helpful_count >= 2 or (
            not_helpful_count >= 1 and helpful_count == 0 and failed_count >= 1
        ):
            bias = "reduce_interruptions"
        elif helpful_count >= 2 and not_helpful_count == 0:
            bias = "prefer_direct_delivery"

        channel_bias = "neutral"
        if acknowledged_count >= 2 and not_helpful_count == 0:
            channel_bias = "prefer_native_notification"

        escalation_bias = "neutral"
        if acknowledged_count >= 2 and helpful_count >= 1 and not_helpful_count == 0:
            escalation_bias = "prefer_async_native"

        return GuardianLearningSignal(
            intervention_type=intervention_type,
            helpful_count=helpful_count,
            not_helpful_count=not_helpful_count,
            acknowledged_count=acknowledged_count,
            failed_count=failed_count,
            bias=bias,
            channel_bias=channel_bias,
            escalation_bias=escalation_bias,
        )


guardian_feedback_repository = GuardianFeedbackRepository()
