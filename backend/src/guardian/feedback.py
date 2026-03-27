"""Persistence and summaries for intervention outcomes and user feedback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import GuardianIntervention
from src.db.session_refs import ensure_sessions_exist
from src.guardian.learning_evidence import (
    GuardianLearningAxisEvidence,
    data_quality_score,
    guardian_confidence_score,
    learning_field_for_axis,
    neutral_axis_evidence,
    ordered_learning_axes,
    recency_score_for_timestamp,
)

logger = logging.getLogger(__name__)

_WEIGHTED_BIAS_THRESHOLD = 1.25
_WEIGHTED_BIAS_MARGIN = 0.1
_MEMORY_REFRESH_OUTCOMES = frozenset({"failed", "delivered", "feedback_received"})
_BIAS_CANDIDATES: dict[str, tuple[str, ...]] = {
    "delivery": ("reduce_interruptions", "prefer_direct_delivery"),
    "channel": ("prefer_native_notification",),
    "escalation": ("prefer_async_native",),
    "timing": ("avoid_focus_windows", "prefer_available_windows"),
    "blocked_state": ("avoid_blocked_state_interruptions", "prefer_async_for_blocked_state"),
    "suppression": ("extend_suppression", "resume_faster"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _excerpt(text: str, *, limit: int = 240) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _normalized_active_project(value: str | None) -> str | None:
    normalized = " ".join(str(value or "").split())
    return normalized or None


def _is_explicit_direct_transport(value: str | None) -> bool:
    normalized = str(value or "").strip()
    return bool(normalized) and normalized != "native_notification"


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
    axis_evidence: tuple[GuardianLearningAxisEvidence, ...] = ()

    def evidence_by_axis(self) -> dict[str, GuardianLearningAxisEvidence]:
        return {item.axis: item for item in self.axis_evidence}

    def evidence_for_axis(self, axis: str) -> GuardianLearningAxisEvidence:
        return self.evidence_by_axis().get(
            axis,
            neutral_axis_evidence(axis, source="live_signal"),
        )

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
            axis_evidence=tuple(
                neutral_axis_evidence(axis, source="live_signal")
                for axis in ordered_learning_axes()
            ),
        )


def _average_score(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 3)


def _intervention_reliability(item: GuardianIntervention) -> float:
    return round(
        (
            guardian_confidence_score(item.guardian_confidence)
            + data_quality_score(item.data_quality)
        )
        / 2.0,
        3,
    )


def _positive_feedback_weight(item: GuardianIntervention) -> float:
    if item.feedback_type == "helpful":
        return 1.0
    if item.feedback_type == "acknowledged":
        return 0.85
    return 0.0


def _positive_delivery_outcome_weight(item: GuardianIntervention) -> float:
    positive_feedback = _positive_feedback_weight(item)
    if positive_feedback > 0.0:
        return positive_feedback
    if item.feedback_type == "not_helpful" or item.latest_outcome == "failed":
        return 0.0
    if item.latest_outcome in {"delivered", "feedback_received"}:
        return 0.7
    return 0.0


def _negative_outcome_weight(item: GuardianIntervention) -> float:
    if item.feedback_type == "not_helpful" or item.latest_outcome == "failed":
        return 1.0
    return 0.0


def _bias_outcome_weight(axis: str, bias: str, item: GuardianIntervention) -> float:
    if bias in {
        "reduce_interruptions",
        "be_brief_and_literal",
        "bundle_more",
        "avoid_focus_windows",
        "avoid_blocked_state_interruptions",
        "extend_suppression",
        "prefer_clean_thread",
    }:
        return _negative_outcome_weight(item)
    if bias in {
        "prefer_native_notification",
        "prefer_async_native",
        "prefer_available_windows",
        "prefer_async_for_blocked_state",
    }:
        return _positive_delivery_outcome_weight(item)
    return _positive_feedback_weight(item)


def _weighted_support_for_bias(
    axis: str,
    bias: str,
    contributors: list[GuardianIntervention],
) -> float:
    return round(
        sum(
            _intervention_reliability(item) * _bias_outcome_weight(axis, bias, item)
            for item in contributors
        ),
        3,
    )


def _select_weighted_bias(
    interventions: list[GuardianIntervention],
    *,
    axis: str,
) -> str:
    candidates = list(_BIAS_CANDIDATES.get(axis, ()))
    if not candidates:
        return "neutral"

    weighted_candidates = [
        (
            bias,
            _weighted_support_for_bias(
                axis,
                bias,
                _axis_supporting_interventions(interventions, axis=axis, bias=bias),
            ),
        )
        for bias in candidates
    ]
    weighted_candidates.sort(key=lambda item: item[1], reverse=True)
    best_bias, best_weight = weighted_candidates[0]
    runner_up_weight = weighted_candidates[1][1] if len(weighted_candidates) > 1 else 0.0

    if best_weight < _WEIGHTED_BIAS_THRESHOLD:
        return "neutral"
    if runner_up_weight > 0.0 and best_weight < runner_up_weight + _WEIGHTED_BIAS_MARGIN:
        return "neutral"
    return best_bias


def _axis_supporting_interventions(
    interventions: list[GuardianIntervention],
    *,
    axis: str,
    bias: str,
) -> list[GuardianIntervention]:
    if bias == "neutral":
        return []
    if axis == "delivery":
        if bias == "reduce_interruptions":
            return [
                item
                for item in interventions
                if _is_explicit_direct_transport(item.transport)
                and (
                    item.feedback_type == "not_helpful"
                    or item.latest_outcome == "failed"
                )
            ]
        if bias == "prefer_direct_delivery":
            return [
                item
                for item in interventions
                if item.user_state == "available"
                and _is_explicit_direct_transport(item.transport)
                and item.feedback_type in {"helpful", "acknowledged"}
            ]
        return []
    if axis == "channel":
        if bias == "prefer_native_notification":
            return [
                item
                for item in interventions
                if item.transport == "native_notification"
                and _positive_delivery_outcome_weight(item) > 0.0
            ]
        return []
    if axis == "escalation":
        if bias == "prefer_async_native":
            return [
                item
                for item in interventions
                if item.transport == "native_notification"
                and item.feedback_type in {"helpful", "acknowledged"}
            ]
        return []
    if axis == "timing":
        if bias == "avoid_focus_windows":
            return [
                item
                for item in interventions
                if item.user_state in {"deep_work", "in_meeting", "away"}
                and _is_explicit_direct_transport(item.transport)
                and (
                    item.feedback_type == "not_helpful" or item.latest_outcome == "failed"
                )
            ]
        if bias == "prefer_available_windows":
            return [
                item
                for item in interventions
                if item.user_state == "available"
                and _is_explicit_direct_transport(item.transport)
                and _positive_delivery_outcome_weight(item) > 0.0
            ]
        return []
    if axis == "blocked_state":
        if bias == "avoid_blocked_state_interruptions":
            return [
                item
                for item in interventions
                if item.user_state in {"deep_work", "in_meeting", "away"}
                and _is_explicit_direct_transport(item.transport)
                and (
                    item.feedback_type == "not_helpful" or item.latest_outcome == "failed"
                )
            ]
        if bias == "prefer_async_for_blocked_state":
            return [
                item
                for item in interventions
                if item.user_state in {"deep_work", "in_meeting", "away"}
                and item.transport == "native_notification"
                and _positive_delivery_outcome_weight(item) > 0.0
            ]
        return []
    if axis == "suppression":
        if bias == "extend_suppression":
            return [
                item
                for item in interventions
                if item.feedback_type == "not_helpful" or item.latest_outcome == "failed"
            ]
        if bias == "resume_faster":
            return [item for item in interventions if item.feedback_type == "helpful"]
        return []
    return []


def _build_live_axis_evidence(
    *,
    interventions: list[GuardianIntervention],
    bias_by_axis: dict[str, str],
    helpful_count: int,
    not_helpful_count: int,
    acknowledged_count: int,
    failed_count: int,
    blocked_direct_failure_count: int,
    blocked_native_success_count: int,
    available_direct_success_count: int,
) -> tuple[GuardianLearningAxisEvidence, ...]:
    now = _now()
    evidence_items: list[GuardianLearningAxisEvidence] = []
    for axis in ordered_learning_axes():
        axis_bias = bias_by_axis[axis]
        contributors = _axis_supporting_interventions(
            interventions,
            axis=axis,
            bias=axis_bias,
        )
        support_count = len(contributors)
        weighted_support = _weighted_support_for_bias(axis, axis_bias, contributors)
        last_confirmed_at = max(
            (item.updated_at for item in contributors if item.updated_at is not None),
            default=None,
        )
        evidence_items.append(
            GuardianLearningAxisEvidence(
                axis=axis,
                field_name=learning_field_for_axis(axis),
                source="live_signal",
                bias=axis_bias,
                support_count=support_count,
                weighted_support=weighted_support,
                recency_score=round(
                    recency_score_for_timestamp(last_confirmed_at, now=now),
                    3,
                ),
                confidence_score=_average_score(
                    [
                        guardian_confidence_score(item.guardian_confidence)
                        for item in contributors
                    ]
                ),
                quality_score=_average_score(
                    [data_quality_score(item.data_quality) for item in contributors]
                ),
                last_confirmed_at=last_confirmed_at,
            )
        )
    return tuple(evidence_items)


class GuardianFeedbackRepository:
    async def _refresh_learning_memories(
        self,
        *,
        intervention_type: str,
        source_session_id: str | None,
        active_project: str | None,
    ) -> None:
        from src.memory.procedural import sync_learning_signal_memories
        from src.memory.snapshots import (
            invalidate_bounded_guardian_snapshot_cache,
            refresh_bounded_guardian_snapshot,
        )

        try:
            signal = await self.get_learning_signal(intervention_type=intervention_type)
            await sync_learning_signal_memories(
                intervention_type=intervention_type,
                signal=signal,
                source_session_id=source_session_id,
            )
            if source_session_id:
                thread_signal = await self.get_learning_signal(
                    intervention_type=intervention_type,
                    session_id=source_session_id,
                )
                await sync_learning_signal_memories(
                    intervention_type=intervention_type,
                    signal=thread_signal,
                    source_session_id=source_session_id,
                    continuity_thread_id=source_session_id,
                )
            normalized_active_project = _normalized_active_project(active_project)
            if normalized_active_project:
                project_signal = await self.get_learning_signal(
                    intervention_type=intervention_type,
                    active_project=normalized_active_project,
                )
                await sync_learning_signal_memories(
                    intervention_type=intervention_type,
                    signal=project_signal,
                    source_session_id=source_session_id,
                    active_project=normalized_active_project,
                )
                if source_session_id:
                    thread_project_signal = await self.get_learning_signal(
                        intervention_type=intervention_type,
                        session_id=source_session_id,
                        active_project=normalized_active_project,
                    )
                    await sync_learning_signal_memories(
                        intervention_type=intervention_type,
                        signal=thread_project_signal,
                        source_session_id=source_session_id,
                        continuity_thread_id=source_session_id,
                        active_project=normalized_active_project,
                    )
            invalidate_bounded_guardian_snapshot_cache()
            try:
                await refresh_bounded_guardian_snapshot()
            except Exception:
                logger.debug("Failed to refresh bounded snapshot after procedural memory update", exc_info=True)
        except Exception:
            logger.debug("Failed to refresh procedural learning memories", exc_info=True)

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
        active_project: str | None = None,
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
            active_project=_normalized_active_project(active_project),
            interruption_mode=interruption_mode,
            policy_action=policy_action,
            policy_reason=policy_reason,
            delivery_decision=delivery_decision,
            latest_outcome=latest_outcome,
            transport=transport,
            notification_id=notification_id,
        )
        async with get_session() as db:
            await ensure_sessions_exist(db, [session_id])
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
        refreshed: GuardianIntervention | None = None
        prior_outcome: str | None = None
        async with get_session() as db:
            result = await db.execute(
                select(GuardianIntervention).where(GuardianIntervention.id == intervention_id)
            )
            intervention = result.scalar_one_or_none()
            if intervention is None:
                return None
            prior_outcome = intervention.latest_outcome
            intervention.latest_outcome = latest_outcome
            intervention.updated_at = _now()
            if transport is not None:
                intervention.transport = transport
            if notification_id is not None:
                intervention.notification_id = notification_id
            db.add(intervention)
            await db.flush()
            await db.refresh(intervention)
            refreshed = intervention

        if (
            latest_outcome in _MEMORY_REFRESH_OUTCOMES
            or prior_outcome in _MEMORY_REFRESH_OUTCOMES
        ):
            await self._refresh_learning_memories(
                intervention_type=refreshed.intervention_type,
                source_session_id=refreshed.session_id,
                active_project=refreshed.active_project,
            )
        return refreshed

    async def record_feedback(
        self,
        intervention_id: str,
        *,
        feedback_type: str,
        feedback_note: str | None = None,
        latest_outcome: str = "feedback_received",
    ) -> GuardianIntervention | None:
        refreshed: GuardianIntervention | None = None
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
            refreshed = intervention

        await self._refresh_learning_memories(
            intervention_type=refreshed.intervention_type,
            source_session_id=refreshed.session_id,
            active_project=refreshed.active_project,
        )
        return refreshed

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
        session_id: str | None = None,
        active_project: str | None = None,
    ) -> GuardianLearningSignal:
        async with get_session() as db:
            stmt = select(GuardianIntervention).where(
                GuardianIntervention.intervention_type == intervention_type
            )
            if session_id is not None:
                stmt = stmt.where(GuardianIntervention.session_id == session_id)
            normalized_active_project = _normalized_active_project(active_project)
            if normalized_active_project is not None:
                stmt = stmt.where(GuardianIntervention.active_project == normalized_active_project)
            result = await db.execute(
                stmt.order_by(GuardianIntervention.updated_at.desc()).limit(limit)
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
                _is_explicit_direct_transport(item.transport)
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
            and _positive_delivery_outcome_weight(item) > 0.0
        )
        available_window_positive = sum(
            1
            for item in interventions
            if item.user_state == "available"
            and _is_explicit_direct_transport(item.transport)
            and _positive_delivery_outcome_weight(item) > 0.0
        )

        bias_by_axis = {
            axis: _select_weighted_bias(interventions, axis=axis)
            for axis in ordered_learning_axes()
        }
        bias = bias_by_axis["delivery"]
        phrasing_bias = bias_by_axis["phrasing"]
        cadence_bias = bias_by_axis["cadence"]
        channel_bias = bias_by_axis["channel"]
        escalation_bias = bias_by_axis["escalation"]
        timing_bias = bias_by_axis["timing"]
        blocked_state_bias = bias_by_axis["blocked_state"]
        suppression_bias = bias_by_axis["suppression"]
        thread_preference_bias = bias_by_axis["thread"]

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
            axis_evidence=_build_live_axis_evidence(
                interventions=interventions,
                bias_by_axis=bias_by_axis,
                helpful_count=helpful_count,
                not_helpful_count=not_helpful_count,
                acknowledged_count=acknowledged_count,
                failed_count=failed_count,
                blocked_direct_failure_count=blocked_direct_failures,
                blocked_native_success_count=blocked_state_positive_native,
                available_direct_success_count=available_window_positive,
            ),
        )


guardian_feedback_repository = GuardianFeedbackRepository()
