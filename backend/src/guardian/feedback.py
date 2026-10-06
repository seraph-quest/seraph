"""Persistence and summaries for intervention outcomes and user feedback."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace, field
from datetime import datetime, timedelta, timezone

from sqlmodel import and_, or_, select

from src.db.engine import get_session
from src.db.models import GuardianIntervention
from src.db.session_refs import ensure_sessions_exist
from src.guardian.learning_evidence import (
    GuardianLearningAxisEvidence,
    data_quality_score,
    guardian_confidence_score,
    learning_field_for_axis,
    learning_evidence_weight,
    neutral_axis_evidence,
    ordered_learning_axes,
    recency_score_for_timestamp,
)

logger = logging.getLogger(__name__)

_WEIGHTED_BIAS_THRESHOLD = 1.25
_WEIGHTED_BIAS_MARGIN = 0.1
_SCOPE_WEIGHT_TIE_TOLERANCE = 0.05
_MEMORY_REFRESH_OUTCOMES = frozenset({"failed", "delivered", "feedback_received"})
_BIAS_CANDIDATES: dict[str, tuple[str, ...]] = {
    "delivery": ("reduce_interruptions", "prefer_direct_delivery"),
    "channel": ("prefer_native_notification",),
    "escalation": ("prefer_async_native",),
    "timing": ("avoid_focus_windows", "prefer_available_windows"),
    "blocked_state": ("avoid_blocked_state_interruptions", "prefer_async_for_blocked_state"),
    "suppression": ("extend_suppression", "resume_faster"),
}
_LIVE_SCOPE_PRIORITY = {
    "global": 0,
    "thread": 1,
    "project": 2,
    "thread_project": 3,
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
    multi_day_positive_days: int = 0
    multi_day_negative_days: int = 0
    scheduled_positive_days: int = 0
    scheduled_negative_days: int = 0
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
            multi_day_positive_days=0,
            multi_day_negative_days=0,
            scheduled_positive_days=0,
            scheduled_negative_days=0,
            axis_evidence=tuple(
                neutral_axis_evidence(axis, source="live_signal")
                for axis in ordered_learning_axes()
            ),
        )


@dataclass(frozen=True)
class GuardianLearningScopeDecision:
    axis: str
    field_name: str
    selected_scope: str
    selected_bias: str
    selected_weight: float
    reason: str


@dataclass(frozen=True)
class ScopedGuardianLearningResolution:
    effective_signal: GuardianLearningSignal
    dominant_scope: str
    decisions: tuple[GuardianLearningScopeDecision, ...]

    @property
    def source_label(self) -> str:
        return "scoped_live_signal" if self.dominant_scope != "global" else "global_live_signal"

    def selected_scopes(self) -> dict[str, str]:
        return {decision.axis: decision.selected_scope for decision in self.decisions}

    def selected_reasons(self) -> dict[str, str]:
        return {decision.axis: decision.reason for decision in self.decisions}


def _average_score(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 3)


def _outcome_day_bucket(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).date().isoformat()


def _scope_priority(scope: str) -> int:
    return _LIVE_SCOPE_PRIORITY.get(scope, -1)


def _scope_filters(
    *,
    scope: str,
    session_id: str | None,
    active_project: str | None,
) -> dict[str, str | None]:
    normalized_active_project = _normalized_active_project(active_project)
    if scope == "thread":
        return {"session_id": session_id, "active_project": None}
    if scope == "project":
        return {"session_id": None, "active_project": normalized_active_project}
    if scope == "thread_project":
        return {
            "session_id": session_id,
            "active_project": normalized_active_project,
        }
    return {"session_id": None, "active_project": None}


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
    if item.intervention_type == "opportunity":
        return 0.0
    if item.feedback_type == "helpful":
        return 1.0
    if item.feedback_type == "acknowledged":
        return 0.85
    return 0.0


def _positive_delivery_outcome_weight(item: GuardianIntervention) -> float:
    if item.intervention_type == "opportunity":
        return 0.0
    positive_feedback = _positive_feedback_weight(item)
    if positive_feedback > 0.0:
        return positive_feedback
    if item.feedback_type == "not_helpful" or item.latest_outcome == "failed":
        return 0.0
    if item.latest_outcome in {"delivered", "feedback_received"}:
        return 0.7
    return 0.0


def _negative_outcome_weight(item: GuardianIntervention) -> float:
    if item.intervention_type == "opportunity":
        return 0.0
    if item.feedback_type == "not_helpful" or item.latest_outcome == "failed":
        return 1.0
    return 0.0


def _is_positive_outcome(item: GuardianIntervention) -> bool:
    return _positive_delivery_outcome_weight(item) > 0.0


def _is_negative_outcome(item: GuardianIntervention) -> bool:
    return _negative_outcome_weight(item) > 0.0


def _distinct_outcome_days(
    interventions: list[GuardianIntervention],
    *,
    predicate,
) -> int:
    return len(
        {
            bucket
            for item in interventions
            if predicate(item)
            for bucket in [_outcome_day_bucket(item.updated_at)]
            if bucket is not None
        }
    )


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
                active_day_count=_distinct_outcome_days(
                    contributors,
                    predicate=lambda _item: True,
                ),
                scheduled_day_count=_distinct_outcome_days(
                    contributors,
                    predicate=lambda item: bool(item.is_scheduled),
                ),
            )
        )
    return tuple(evidence_items)


def _normalize_live_axis_evidence(
    *,
    axis: str,
    signal: GuardianLearningSignal,
) -> GuardianLearningAxisEvidence:
    field_name = learning_field_for_axis(axis)
    evidence = signal.evidence_for_axis(axis)
    if (
        evidence.axis != axis
        or evidence.field_name != field_name
        or evidence.source != "live_signal"
        or evidence.bias != getattr(signal, field_name)
    ):
        return replace(
            evidence,
            axis=axis,
            field_name=field_name,
            source="live_signal",
            bias=getattr(signal, field_name),
        )
    return evidence


def _select_learning_scope_for_axis(
    *,
    axis: str,
    candidate_signals: dict[str, GuardianLearningSignal],
) -> tuple[GuardianLearningAxisEvidence, GuardianLearningScopeDecision]:
    field_name = learning_field_for_axis(axis)
    weighted_candidates: list[tuple[str, GuardianLearningAxisEvidence, float]] = []
    for scope, signal in candidate_signals.items():
        evidence = _normalize_live_axis_evidence(axis=axis, signal=signal)
        weight = learning_evidence_weight(evidence)
        if weight <= 0.0:
            continue
        weighted_candidates.append((scope, evidence, weight))

    if not weighted_candidates:
        neutral = neutral_axis_evidence(axis, source="live_signal")
        return neutral, GuardianLearningScopeDecision(
            axis=axis,
            field_name=field_name,
            selected_scope="global",
            selected_bias="neutral",
            selected_weight=0.0,
            reason="no_supported_bias",
        )

    weighted_candidates.sort(key=lambda item: item[2], reverse=True)
    selected_scope, selected_evidence, selected_weight = weighted_candidates[0]
    reason = "strongest_scope"
    if len(weighted_candidates) > 1:
        tie_candidates = [
            item
            for item in weighted_candidates
            if abs(selected_weight - item[2]) <= _SCOPE_WEIGHT_TIE_TOLERANCE
        ]
        if len(tie_candidates) > 1:
            original_scope, original_evidence, original_weight = selected_scope, selected_evidence, selected_weight
            tie_candidates.sort(
                key=lambda item: (
                    _scope_priority(item[0]),
                    item[1].recency_score,
                    item[1].confidence_score,
                    item[1].quality_score,
                    item[2],
                ),
                reverse=True,
            )
            selected_scope, selected_evidence, selected_weight = tie_candidates[0]
            if _scope_priority(selected_scope) > _scope_priority(original_scope):
                reason = "tie_prefers_more_specific_scope"
            elif selected_evidence.recency_score > original_evidence.recency_score:
                reason = "tie_prefers_fresher_scope"
            elif selected_scope != original_scope or selected_weight != original_weight:
                reason = "tie_prefers_stronger_runner_up"
    return selected_evidence, GuardianLearningScopeDecision(
        axis=axis,
        field_name=field_name,
        selected_scope=selected_scope,
        selected_bias=selected_evidence.bias,
        selected_weight=selected_weight,
        reason=reason,
    )


class GuardianFeedbackRepository:
    async def _refresh_learning_memories(
        self,
        *,
        intervention_type: str,
        source_session_id: str | None,
        active_project: str | None,
    ) -> None:
        if intervention_type == "opportunity":
            return
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
        owner_principal_id: str | None = None,
        original_root_id: str | None = None,
    ) -> GuardianIntervention | None:
        refreshed: GuardianIntervention | None = None
        async with get_session() as db:
            result = await db.execute(
                select(GuardianIntervention).where(GuardianIntervention.id == intervention_id)
            )
            intervention = result.scalar_one_or_none()
            from src.db.models import GuardianOpportunity
            from src.guardian.opportunity_contracts import OpportunityError
            opportunity_id = (intervention.opportunity_id if intervention and intervention.intervention_type == "opportunity"
                              else intervention_id.removeprefix("opportunity:"))
            opportunity = await db.get(GuardianOpportunity, opportunity_id)
            if opportunity is not None:
                if (opportunity.owner_principal_id != owner_principal_id
                        or opportunity.original_root_id != original_root_id):
                    raise OpportunityError("opportunity_owner_mismatch", 403)
                if opportunity.status != "proposed" or intervention is None:
                    raise OpportunityError("opportunity_not_proposed")
            if intervention is not None and intervention.intervention_type == "opportunity":
                raise OpportunityError("opportunity_feedback_requires_revision")
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

        if refreshed.intervention_type == "opportunity":
            return refreshed
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
        active_project: str | None = None,
        owner_principal_id: str | None = None,
        original_root_id: str | None = None,
    ) -> list[GuardianIntervention]:
        async with get_session() as db:
            query = select(GuardianIntervention)
            # Opportunity judgments contain Goal-derived private text and have
            # no chat session. Generic legacy consumers cannot treat them as
            # ambient; scope this population before LIMIT to preserve own rows.
            legacy = GuardianIntervention.intervention_type != "opportunity"
            if owner_principal_id and original_root_id:
                query = query.where(or_(legacy, and_(
                    GuardianIntervention.owner_principal_id == owner_principal_id,
                    GuardianIntervention.original_root_id == original_root_id,
                )))
            else:
                query = query.where(legacy)
            if session_id:
                query = query.where(GuardianIntervention.session_id == session_id)
            normalized_active_project = _normalized_active_project(active_project)
            if normalized_active_project is not None:
                query = query.where(GuardianIntervention.active_project == normalized_active_project)
            result = await db.execute(
                query.order_by(GuardianIntervention.updated_at.desc()).limit(limit)
            )
            return list(result.scalars().all())

    async def summarize_recent(
        self,
        *,
        limit: int = 5,
        session_id: str | None = None,
        active_project: str | None = None,
    ) -> str:
        interventions = await self.list_recent(
            limit=limit,
            session_id=session_id,
            active_project=active_project,
        )
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

    async def summarize_recent_for_scope(
        self,
        *,
        scope: str,
        limit: int = 5,
        session_id: str | None = None,
        active_project: str | None = None,
    ) -> str:
        return await self.summarize_recent(
            limit=limit,
            **_scope_filters(
                scope=scope,
                session_id=session_id,
                active_project=active_project,
            ),
        )

    async def resolve_learning_signal(
        self,
        *,
        intervention_type: str,
        limit: int = 12,
        session_id: str | None = None,
        active_project: str | None = None,
    ) -> ScopedGuardianLearningResolution:
        candidate_signals: dict[str, GuardianLearningSignal] = {
            "global": await self.get_learning_signal(
                intervention_type=intervention_type,
                limit=limit,
            )
        }
        normalized_active_project = _normalized_active_project(active_project)
        if session_id is not None:
            candidate_signals["thread"] = await self.get_learning_signal(
                intervention_type=intervention_type,
                limit=limit,
                session_id=session_id,
            )
        if normalized_active_project is not None:
            candidate_signals["project"] = await self.get_learning_signal(
                intervention_type=intervention_type,
                limit=limit,
                active_project=normalized_active_project,
            )
        if session_id is not None and normalized_active_project is not None:
            candidate_signals["thread_project"] = await self.get_learning_signal(
                intervention_type=intervention_type,
                limit=limit,
                session_id=session_id,
                active_project=normalized_active_project,
            )

        decisions: list[GuardianLearningScopeDecision] = []
        selected_axis_evidence: list[GuardianLearningAxisEvidence] = []
        selected_biases: dict[str, str] = {}
        scope_weights: dict[str, float] = {}
        for axis in ordered_learning_axes():
            selected_evidence, decision = _select_learning_scope_for_axis(
                axis=axis,
                candidate_signals=candidate_signals,
            )
            decisions.append(decision)
            selected_axis_evidence.append(selected_evidence)
            selected_biases[decision.field_name] = selected_evidence.bias
            if decision.selected_weight > 0.0:
                scope_weights[decision.selected_scope] = round(
                    scope_weights.get(decision.selected_scope, 0.0) + decision.selected_weight,
                    3,
                )

        dominant_scope = "global"
        for preferred_axis in ("delivery", "suppression", "blocked_state", "timing"):
            preferred_decision = next(
                (
                    item
                    for item in decisions
                    if item.axis == preferred_axis
                    and item.selected_scope != "global"
                    and item.selected_weight > 0.0
                ),
                None,
            )
            if preferred_decision is not None:
                dominant_scope = preferred_decision.selected_scope
                break
        if dominant_scope == "global" and scope_weights:
            dominant_scope = sorted(
                scope_weights.items(),
                key=lambda item: (item[1], _scope_priority(item[0])),
                reverse=True,
            )[0][0]

        effective_signal = replace(
            candidate_signals[dominant_scope],
            axis_evidence=tuple(selected_axis_evidence),
            **selected_biases,
        )
        return ScopedGuardianLearningResolution(
            effective_signal=effective_signal,
            dominant_scope=dominant_scope,
            decisions=tuple(decisions),
        )

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
                GuardianIntervention.intervention_type == intervention_type,
                GuardianIntervention.intervention_type != "opportunity",
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
            horizon_stmt = stmt.where(
                GuardianIntervention.updated_at
                >= (_now() - timedelta(days=21))
            )
            horizon_result = await db.execute(
                horizon_stmt.order_by(GuardianIntervention.updated_at.desc()).limit(max(limit * 4, 60))
            )
            long_horizon_interventions = list(horizon_result.scalars().all())

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
        multi_day_positive_days = _distinct_outcome_days(
            long_horizon_interventions,
            predicate=_is_positive_outcome,
        )
        multi_day_negative_days = _distinct_outcome_days(
            long_horizon_interventions,
            predicate=_is_negative_outcome,
        )
        scheduled_positive_days = _distinct_outcome_days(
            long_horizon_interventions,
            predicate=lambda item: bool(item.is_scheduled) and _is_positive_outcome(item),
        )
        scheduled_negative_days = _distinct_outcome_days(
            long_horizon_interventions,
            predicate=lambda item: bool(item.is_scheduled) and _is_negative_outcome(item),
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
            multi_day_positive_days=multi_day_positive_days,
            multi_day_negative_days=multi_day_negative_days,
            scheduled_positive_days=scheduled_positive_days,
            scheduled_negative_days=scheduled_negative_days,
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

# M4 explicit opportunity feedback. These witnesses never carry authority from a caller hash.
import json
from uuid import UUID
from sqlalchemy import text, update
from src.guardian.opportunity_contracts import (
    OpportunityError, OpportunityFeedbackRequest, OpportunityFeedbackReceipt, digest, json_bytes,
)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.guardian.opportunity_plans import PlanSourceWitness
    from src.work_board.review import PipelineProducerWitness


@dataclass(frozen=True)
class FeedbackHistoryWitness:
    revision: int
    canonical_bytes: bytes
    history_digest: str
    events: tuple[bytes, ...]
    tip_bytes: bytes | None
    event_count: int


@dataclass(frozen=True)
class FeedbackSourceWitness:
    owner_principal_id: str
    original_root_id: str
    opportunity_id: str
    intervention_id: str
    opportunity_token: bytes
    intervention_token: bytes
    root_token_bytes: bytes = field(repr=False)
    history: FeedbackHistoryWitness
    source: PlanSourceWitness
    proposal_id: str | None
    proposal_token: bytes | None
    blueprint_id: str | None
    outcomes: tuple[PipelineProducerWitness, ...]
    lineage_bytes: bytes
    outcome_binding_bytes: bytes
    outcome_binding_digest: str


def _feedback_row_bytes(row):
    return json_bytes(row.model_dump(mode="json"))


def _closed_feedback_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate feedback JSON key')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


async def _feedback_root_token(db, owner):
    from src.db.models import OperatorSession
    root = await db.get(OperatorSession, owner.session_id, populate_existing=True)
    if root is None or root.principal_id != owner.principal_id:
        raise OpportunityError('original_root_unavailable', 403)
    return json_bytes({'id':root.id,'principal_id':root.principal_id,'private_hash':root.token_hash,
        'absolute_expires_at':root.absolute_expires_at.isoformat(),'revoked_at':root.revoked_at.isoformat() if root.revoked_at else None,
        'replaced_by_id':root.replaced_by_id,'is_bearer_tombstone':root.is_bearer_tombstone,
        'operator_identity_id':root.operator_identity_id})


def parse_opportunity_feedback_history(intervention) -> FeedbackHistoryWitness:
    """Authenticate the entire retained chain and its current projection; never trim."""
    try:
        raw = intervention.feedback_history_json or "[]"
        if len(raw.encode("utf-8")) > 32768:
            raise ValueError("history bytes")
        events = _closed_feedback_json(raw)
        if not isinstance(events, list) or len(events) > 100 or type(intervention.feedback_revision) is not int:
            raise ValueError("history bound")
        previous, seen = None, set()
        keys = {"revision", "event_id", "feedback_type", "reason", "feedback_at", "expected_feedback_revision",
                "request_digest", "outcome_binding_digest", "previous_event_digest", "event_digest"}
        for revision, event in enumerate(events, 1):
            if not isinstance(event, dict) or set(event) != keys:
                raise ValueError("closed history")
            if (type(event['revision']) is not int or event['revision'] != revision
                    or type(event['expected_feedback_revision']) is not int or event['expected_feedback_revision'] != revision-1
                    or str(UUID(event['event_id'])) != event['event_id'] or event['event_id'] in seen
                    or event['feedback_type'] not in {'helpful', 'not_helpful'}
                    or not isinstance(event['reason'], str) or len(event['reason']) > 500
                    or event['previous_event_digest'] != previous):
                raise ValueError("history chain")
            instant = datetime.fromisoformat(event['feedback_at'])
            if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
                raise ValueError("history timestamp")
            request = OpportunityFeedbackRequest(expected_feedback_revision=revision-1,
                feedback_type=event['feedback_type'], reason=event['reason'], idempotency_key=event['event_id'])
            if event['request_digest'] != digest(json_bytes(request.model_dump(mode='json'))):
                raise ValueError("history request")
            if (not isinstance(event['outcome_binding_digest'], str) or len(event['outcome_binding_digest']) != 64
                    or any(c not in '0123456789abcdef' for c in event['outcome_binding_digest'])):
                raise ValueError("history binding")
            body = {key: value for key, value in event.items() if key != 'event_digest'}
            if event['event_digest'] != digest(json_bytes(body)):
                raise ValueError("history digest")
            previous = event['event_digest']
            seen.add(event['event_id'])
        if intervention.feedback_revision != len(events):
            raise ValueError("history revision")
        tip = events[-1] if events else None
        if tip:
            at = intervention.feedback_at
            if at is None:
                raise ValueError("tip timestamp")
            at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
            if (intervention.feedback_type != tip['feedback_type'] or intervention.feedback_note != tip['reason']
                    or at != datetime.fromisoformat(tip['feedback_at']) or intervention.outcome_binding_json is None
                    or digest(json_bytes(_closed_feedback_json(intervention.outcome_binding_json))) != tip['outcome_binding_digest']):
                raise ValueError("tip projection")
        elif intervention.feedback_type is not None or intervention.feedback_at is not None or intervention.outcome_binding_json is not None:
            raise ValueError("unrecorded feedback")
        canonical = json_bytes(events)
        return FeedbackHistoryWitness(len(events), canonical, digest(canonical), tuple(json_bytes(e) for e in events),
            json_bytes(tip) if tip else None, len(events))
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise OpportunityError('learning_population_incomplete') from exc


async def _feedback_owned_rows(db, owner, opportunity_id):
    from src.db.models import GuardianOpportunity
    opportunity = await db.get(GuardianOpportunity, opportunity_id, populate_existing=True)
    if opportunity is None:
        raise OpportunityError('opportunity_not_found', 404)
    if (opportunity.owner_principal_id, opportunity.original_root_id) != (owner.principal_id, owner.session_id):
        raise OpportunityError('opportunity_owner_mismatch', 403)
    intervention = await db.get(GuardianIntervention, opportunity.intervention_id, populate_existing=True) if opportunity.intervention_id else None
    if (opportunity.status not in {'proposed', 'planned'} or intervention is None
            or intervention.intervention_type != 'opportunity'
            or (intervention.opportunity_id, intervention.owner_principal_id, intervention.original_root_id,
                intervention.goal_id, intervention.goal_revision) != (opportunity.id, owner.principal_id,
                owner.session_id, opportunity.goal_id, opportunity.goal_revision)):
        raise OpportunityError('opportunity_not_proposed')
    return opportunity, intervention


async def _feedback_lineage(db, owner, opportunity, proposal, *, source):
    from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkBoardLink, WorkBoardHandoff
    from src.work_board import pipelines
    value = json.loads(proposal.proposal_json)
    blueprint = value.get('blueprint_id')
    if proposal.status != 'accepted':
        if opportunity.status == 'planned':
            raise OpportunityError('feedback_outcome_stale')
        return (), (), blueprint
    if opportunity.status != 'planned' or opportunity.proposal_id != proposal.proposal_id:
        raise OpportunityError('feedback_outcome_stale')
    if blueprint == 'public-browser-check':
        ids, capabilities = [proposal.parent_task_id], ['browser.public-task.v1']
    elif blueprint == 'public-evidence-report':
        _, value = await pipelines.owned(db, owner, proposal.proposal_id, workspace_identity=source.workspace_identity)
        steps = value['steps']
        ids = [step['task_ref'] for step in steps]
        from src.work_board.pipeline_contracts import SLOTS, CAPABILITIES
        capabilities = list(CAPABILITIES)
        if tuple(step['slot'] for step in steps) != SLOTS or ids[0] != proposal.parent_task_id:
            raise OpportunityError('feedback_outcome_stale')
    else:
        raise OpportunityError('feedback_outcome_stale')
    tasks = []
    for task_id, capability in zip(ids, capabilities):
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id).execution_options(populate_existing=True))
        if (task is None or task.capability_id != capability or (task.owner_principal_id, task.owner_session_id,
                task.goal_id, task.goal_revision) != (owner.principal_id, owner.session_id, opportunity.goal_id, opportunity.goal_revision)):
            raise OpportunityError('feedback_outcome_stale')
        tasks.append(task)
    attempts = list((await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id.in_(ids)))).scalars().all())
    links = []
    for parent, child in zip(ids, ids[1:]):
        link = await db.scalar(select(WorkBoardLink).where(WorkBoardLink.parent_task_id == parent, WorkBoardLink.child_task_id == child))
        if link is None:
            raise OpportunityError('feedback_outcome_stale')
        if (link.owner_principal_id,link.owner_session_id) != (owner.principal_id,owner.session_id):
            raise OpportunityError('feedback_outcome_stale')
        links.append(link)
    handoffs = []
    for link in links:
        handoff = await db.get(WorkBoardHandoff, link.current_handoff_id) if link.current_handoff_id else None
        if attempts and handoff is None:
            raise OpportunityError('feedback_outcome_stale')
        if handoff:
            parent = next(task for task in tasks if task.task_id==link.parent_task_id)
            parent_attempts = sorted((attempt for attempt in attempts if attempt.task_id==parent.task_id),
                key=lambda attempt:(attempt.created_at,attempt.attempt_id))
            if not parent_attempts:
                raise OpportunityError('feedback_outcome_stale')
            latest = parent_attempts[-1]
            if (handoff.owner_principal_id,handoff.owner_session_id,handoff.parent_task_id,handoff.child_task_id,
                    handoff.link_id,handoff.source_attempt_id,handoff.workflow_run_id,handoff.source_task_revision) != (
                    owner.principal_id,owner.session_id,link.parent_task_id,link.child_task_id,link.link_id,
                    latest.attempt_id,latest.workflow_run_id,parent.task_revision):
                raise OpportunityError('feedback_outcome_stale')
            handoffs.append(handoff)
    inventory = tuple((kind, identity, _feedback_row_bytes(row)) for kind, rows in (
        ('task', tasks), ('attempt', sorted(attempts, key=lambda r:r.attempt_id)), ('link', links), ('handoff', handoffs))
        for row in rows for identity in [getattr(row, 'task_id', None) if kind=='task' else
            getattr(row, {'attempt':'attempt_id','link':'link_id','handoff':'handoff_id'}[kind])])
    return tuple(tasks) if attempts else (), inventory, blueprint


def _validate_feedback_handoffs(inventory, outcomes):
    """Bind every stored report edge to the staged actual parent readback."""
    from src.work_board.review import _safe_verification_receipt
    producers = {outcome.task_id: outcome for outcome in outcomes}
    for kind, _, token in inventory:
        if kind != 'handoff':
            continue
        handoff = _closed_feedback_json(token.decode())
        producer = producers.get(handoff.get('parent_task_id'))
        if producer is None:
            raise OpportunityError('feedback_outcome_stale')
        expected = _safe_verification_receipt(_closed_feedback_json(producer.proof_bytes.decode()), require_complete=True)
        actual = _closed_feedback_json(handoff.get('verification_json') or '{}')
        if not expected or actual != expected:
            raise OpportunityError('feedback_outcome_stale')


async def stage_opportunity_feedback_source(db, owner, opportunity_id, *, operator) -> FeedbackSourceWitness:
    """Fresh files/native evidence outside writers; original owner comes from caller custody."""
    from src.db.models import WorkBoardProposal
    from src.guardian.opportunity_plans import stage_plan_source
    from src.work_board.review import stage_pipeline_producer_readback
    from src.work_board.repository import BoardError
    opportunity, intervention = await _feedback_owned_rows(db, owner, opportunity_id)
    if operator is None or (operator.principal.principal_id, operator.session_id, operator.ownership_continuity) != (
            owner.principal_id, owner.session_id, 'stable'):
        raise OpportunityError('opportunity_owner_mismatch', 403)
    history = parse_opportunity_feedback_history(intervention)
    root_token = await _feedback_root_token(db, owner)
    proposal = await db.get(WorkBoardProposal, opportunity.proposal_id, populate_existing=True) if opportunity.proposal_id else None
    accepted = proposal is not None and proposal.status == 'accepted'
    source = await stage_plan_source(db, opportunity, allow_planned=accepted)
    tasks, inventory, blueprint = (), (), None
    outcomes = []
    if proposal:
        if (proposal.owner_principal_id, proposal.owner_session_id, proposal.opportunity_id) != (
                owner.principal_id, owner.session_id, opportunity.id):
            raise OpportunityError('feedback_outcome_stale')
        try:
            tasks, inventory, blueprint = await _feedback_lineage(db, owner, opportunity, proposal, source=source)
            for task in tasks:
                outcomes.append(await stage_pipeline_producer_readback(db, owner, task))
            _validate_feedback_handoffs(inventory, outcomes)
        except (BoardError, OSError, KeyError, TypeError, ValueError) as exc:
            raise OpportunityError('feedback_outcome_stale') from exc
    lineage = json_bytes([[kind, identity, token.decode()] for kind, identity, token in inventory])
    binding = {'owner_principal_id':owner.principal_id, 'original_root_id':owner.session_id,
        'opportunity_id':opportunity.id, 'goal_id':opportunity.goal_id,'goal_revision':opportunity.goal_revision,
        'policy_revision':opportunity.policy_revision,'watch_id':opportunity.watch_id,'watch_revision':opportunity.watch_revision,
        'source_digest':opportunity.source_digest,'source_token_digest':digest(source.source_token_bytes),
        'root_token_digest':digest(root_token),
        'proposal_id':proposal.proposal_id if proposal else None,'blueprint_id':blueprint,
        'opportunity_token_digest':digest(_feedback_row_bytes(opportunity)),
        'proposal_token_digest':digest(_feedback_row_bytes(proposal)) if proposal else None,
        'lineage_digest':digest(lineage), 'outcomes':[
            {'task_id':p.task_id,'task_token_digest':digest(p.task_token.encode()),'attempt_id':p.attempt_id,
             'attempt_token_digest':digest(p.attempt_token.encode()),'run_identity':p.run_identity,'run_token_digest':digest(p.run_token.encode()),
             'input_artifact_id':p.input_artifact_id,'input_token_digest':digest(p.input_artifact_token.encode()),
             'content_sha256':p.content_sha256,'proof_digest':digest(p.proof_bytes)} for p in outcomes] or None}
    encoded = json_bytes(binding)
    return FeedbackSourceWitness(owner.principal_id, owner.session_id, opportunity.id, intervention.id,
        _feedback_row_bytes(opportunity), _feedback_row_bytes(intervention), root_token, history, source,
        proposal.proposal_id if proposal else None, _feedback_row_bytes(proposal) if proposal else None,
        blueprint, tuple(outcomes), lineage, encoded, digest(encoded))


async def recheck_opportunity_feedback_source(db, owner, *, witness):
    from src.db.models import WorkBoardProposal
    from src.guardian.opportunity_plans import recheck_plan_source
    from src.work_board.review import recheck_pipeline_producer_readback
    if type(witness) is not FeedbackSourceWitness or (witness.owner_principal_id, witness.original_root_id) != (owner.principal_id, owner.session_id):
        raise OpportunityError('feedback_outcome_stale')
    opportunity, intervention = await _feedback_owned_rows(db, owner, witness.opportunity_id)
    if await _feedback_root_token(db, owner) != witness.root_token_bytes:
        raise OpportunityError('original_root_unavailable', 403)
    if (_feedback_row_bytes(opportunity) != witness.opportunity_token
            or _feedback_row_bytes(intervention) != witness.intervention_token):
        raise OpportunityError('feedback_revision_stale')
    proposal = await db.get(WorkBoardProposal, witness.proposal_id, populate_existing=True) if witness.proposal_id else None
    if (opportunity.proposal_id != witness.proposal_id or (proposal and _feedback_row_bytes(proposal) != witness.proposal_token)):
        raise OpportunityError('feedback_outcome_stale')
    await recheck_plan_source(db, opportunity, source_witness=witness.source, allow_planned=proposal is not None and proposal.status=='accepted')
    if proposal:
        _, inventory, blueprint = await _feedback_lineage(db, owner, opportunity, proposal, source=witness.source)
        if json_bytes([[kind, identity, token.decode()] for kind, identity, token in inventory]) != witness.lineage_bytes or blueprint != witness.blueprint_id:
            raise OpportunityError('feedback_outcome_stale')
    for outcome in witness.outcomes:
        await recheck_pipeline_producer_readback(db, owner, witness=outcome)
    return opportunity, intervention


def _feedback_receipt(opportunity_id, intervention_id, event, history_digest, *, replay):
    return OpportunityFeedbackReceipt(opportunity_id=opportunity_id, intervention_id=intervention_id,
        feedback_revision=event['revision'], feedback_type=event['feedback_type'], feedback_at=event['feedback_at'],
        feedback_event_id=event['event_id'], feedback_event_digest=event['event_digest'],
        feedback_history_digest=history_digest, outcome_binding_digest=event['outcome_binding_digest'], idempotent_replay=replay)


async def record_opportunity_feedback(*, operator, opportunity_id, request):
    from src.work_board.contracts import WorkBoardOwner
    from src.memory.evidence_execution import _current_operator
    from src.work_board.repository import BoardError
    from src.db.models import AuditEvent
    request = OpportunityFeedbackRequest.model_validate(request)
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    request_digest = digest(json_bytes(request.model_dump(mode='json')))
    async def authenticated_request(db):
        try:
            await _current_operator(db, owner, operator)
        except BoardError as exc:
            raise OpportunityError(exc.code, exc.status_code) from exc
    async with get_session() as db:
        await authenticated_request(db)
        opportunity, intervention = await _feedback_owned_rows(db, owner, opportunity_id)
        history = parse_opportunity_feedback_history(intervention)
        for encoded in history.events:
            event = json.loads(encoded)
            if event['event_id'] == str(request.idempotency_key):
                if event['request_digest'] != request_digest:
                    raise OpportunityError('feedback_idempotency_conflict')
                original_history = json_bytes([json.loads(value) for value in history.events[:event['revision']]])
                return _feedback_receipt(opportunity_id, intervention.id, event, digest(original_history), replay=True)
        if history.revision != request.expected_feedback_revision:
            raise OpportunityError('feedback_revision_stale')
        witness = await stage_opportunity_feedback_source(db, owner, opportunity_id, operator=operator)
    if request.feedback_type == 'helpful' and not witness.outcomes:
        raise OpportunityError('feedback_outcome_stale')
    async with get_session() as db:
        await db.execute(text('BEGIN IMMEDIATE'))
        await authenticated_request(db)
        current_opportunity, current_intervention = await _feedback_owned_rows(db, owner, opportunity_id)
        current_history = parse_opportunity_feedback_history(current_intervention)
        for value in current_history.events:
            retained = json.loads(value)
            if retained['event_id'] == str(request.idempotency_key):
                if retained['request_digest'] != request_digest:
                    raise OpportunityError('feedback_idempotency_conflict')
                prefix = json_bytes([json.loads(value) for value in current_history.events[:retained['revision']]])
                return _feedback_receipt(opportunity_id, current_intervention.id, retained, digest(prefix), replay=True)
        opportunity, intervention = await recheck_opportunity_feedback_source(db, owner, witness=witness)
        event = {'revision':history.revision+1,'event_id':str(request.idempotency_key),'feedback_type':request.feedback_type,
            'reason':request.reason,'feedback_at':_now().isoformat(),'expected_feedback_revision':request.expected_feedback_revision,
            'request_digest':request_digest,'outcome_binding_digest':witness.outcome_binding_digest,
            'previous_event_digest':json.loads(history.tip_bytes)['event_digest'] if history.tip_bytes else None}
        event['event_digest'] = digest(json_bytes(event))
        events = [json.loads(value) for value in history.events]+[event]
        encoded = json_bytes(events)
        if len(events)>100 or len(encoded)>32768:
            raise OpportunityError('learning_population_incomplete')
        at = datetime.fromisoformat(event['feedback_at'])
        changed = await db.execute(update(GuardianIntervention).where(GuardianIntervention.id==intervention.id,
            GuardianIntervention.feedback_revision==history.revision).values(feedback_revision=history.revision+1,
            feedback_history_json=encoded.decode(),outcome_binding_json=witness.outcome_binding_bytes.decode(),
            feedback_type=request.feedback_type,feedback_note=request.reason,feedback_at=at,updated_at=at).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise OpportunityError('feedback_revision_stale')
        db.add(AuditEvent(id='opportunity-feedback:'+digest(json_bytes([owner.principal_id,opportunity.id,str(request.idempotency_key)])), actor=owner.principal_id,
            event_type='guardian_opportunity_feedback',details_json=json_bytes({'opportunity_id':opportunity.id,
                'intervention_id':intervention.id,'event_id':event['event_id'],'feedback_revision':event['revision'],
                'event_digest':event['event_digest'],'outcome_binding_digest':witness.outcome_binding_digest,'reason_code':'explicit_feedback'}).decode()))
        return _feedback_receipt(opportunity.id,intervention.id,event,digest(encoded),replay=False)


async def opportunity_feedback_summary(db, opportunity):
    intervention = await db.get(GuardianIntervention, opportunity.intervention_id) if opportunity.intervention_id else None
    result = {'intervention_id':intervention.id if intervention else None,'feedback_revision':0,'feedback_type':None,
        'feedback_at':None,'feedback_event_id':None,'feedback_history_digest':None,'event_count':0,'memory_status':'no_learning','reason_code':None}
    if intervention:
        try:
            history = parse_opportunity_feedback_history(intervention)
            tip = json.loads(history.tip_bytes) if history.tip_bytes else None
            result.update(feedback_revision=history.revision,feedback_type=tip['feedback_type'] if tip else None,
                feedback_at=tip['feedback_at'] if tip else None,feedback_event_id=tip['event_id'] if tip else None,
                feedback_history_digest=history.history_digest,event_count=history.event_count)
        except OpportunityError as exc:
            result['reason_code']=exc.code
    return result
