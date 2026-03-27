from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.db.models import MemoryCategory, MemoryKind
from src.guardian.learning_evidence import evidence_count_for_axis
from src.memory.repository import memory_repository


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _intervention_phrase(intervention_type: str) -> str:
    normalized = " ".join(str(intervention_type or "advisory").replace("_", " ").split())
    return normalized or "advisory"


def _delivery_lesson(signal: Any, intervention_phrase: str) -> tuple[str, str] | None:
    if signal.bias == "reduce_interruptions":
        return (
            "reduce_interruptions",
            f"For {intervention_phrase} interventions, reduce direct interruptions after recent negative or failed outcomes.",
        )
    if signal.bias == "prefer_direct_delivery":
        return (
            "prefer_direct_delivery",
            f"For {intervention_phrase} interventions, direct delivery is usually tolerated when the user is available.",
        )
    return None


def _phrasing_lesson(signal: Any, intervention_phrase: str) -> tuple[str, str] | None:
    if signal.phrasing_bias == "be_brief_and_literal":
        return (
            "be_brief_and_literal",
            f"For {intervention_phrase} interventions, use brief, literal phrasing when interrupting.",
        )
    if signal.phrasing_bias == "be_more_direct":
        return (
            "be_more_direct",
            f"For {intervention_phrase} interventions, direct wording works better than hedging.",
        )
    return None


def _cadence_lesson(signal: Any, intervention_phrase: str) -> tuple[str, str] | None:
    if signal.cadence_bias == "bundle_more":
        return (
            "bundle_more",
            f"For {intervention_phrase} interventions, bundle lower-urgency check-ins instead of interrupting immediately.",
        )
    if signal.cadence_bias == "check_in_sooner":
        return (
            "check_in_sooner",
            f"For {intervention_phrase} interventions on aligned work, slightly faster follow-up is acceptable when confidence is grounded.",
        )
    return None


def _channel_lesson(signal: Any, intervention_phrase: str) -> tuple[str, str] | None:
    if signal.channel_bias == "prefer_native_notification":
        return (
            "prefer_native_notification",
            f"For {intervention_phrase} interventions, async native notification is usually tolerated better than browser interruption.",
        )
    return None


def _escalation_lesson(signal: Any, intervention_phrase: str) -> tuple[str, str] | None:
    if signal.escalation_bias == "prefer_async_native":
        return (
            "prefer_async_native",
            f"For {intervention_phrase} interventions, escalate through async native continuation before direct interruption when possible.",
        )
    return None


def _timing_lesson(signal: Any, intervention_phrase: str) -> tuple[str, str] | None:
    if signal.timing_bias == "avoid_focus_windows":
        return (
            "avoid_focus_windows",
            f"For {intervention_phrase} interventions, avoid direct interruption during deep-work, meeting, or away windows.",
        )
    if signal.timing_bias == "prefer_available_windows":
        return (
            "prefer_available_windows",
            f"For {intervention_phrase} interventions, prefer delivery while the user is explicitly available.",
        )
    return None


def _blocked_state_lesson(signal: Any, intervention_phrase: str) -> tuple[str, str] | None:
    if signal.blocked_state_bias == "avoid_blocked_state_interruptions":
        return (
            "avoid_blocked_state_interruptions",
            f"For {intervention_phrase} interventions, when the user is blocked, prefer bundling over direct interruption.",
        )
    if signal.blocked_state_bias == "prefer_async_for_blocked_state":
        return (
            "prefer_async_for_blocked_state",
            f"For {intervention_phrase} interventions, when the user is blocked, prefer async native continuation instead of browser interruption.",
        )
    return None


def _suppression_lesson(signal: Any, intervention_phrase: str) -> tuple[str, str] | None:
    if signal.suppression_bias == "extend_suppression":
        return (
            "extend_suppression",
            f"For {intervention_phrase} interventions, extend the suppression window after negative or failed interruptions.",
        )
    if signal.suppression_bias == "resume_faster":
        return (
            "resume_faster",
            f"For {intervention_phrase} interventions, suppression can resume faster after consistently helpful outcomes.",
        )
    return None


def _thread_lesson(signal: Any, intervention_phrase: str) -> tuple[str, str] | None:
    if signal.thread_preference_bias == "prefer_existing_thread":
        return (
            "prefer_existing_thread",
            f"For {intervention_phrase} interventions, prefer continuing the existing thread instead of starting a clean interruption.",
        )
    if signal.thread_preference_bias == "prefer_clean_thread":
        return (
            "prefer_clean_thread",
            f"For {intervention_phrase} interventions, prefer a clean thread when the prior one degraded.",
        )
    return None


_LESSON_BUILDERS = {
    "delivery": _delivery_lesson,
    "phrasing": _phrasing_lesson,
    "cadence": _cadence_lesson,
    "channel": _channel_lesson,
    "escalation": _escalation_lesson,
    "timing": _timing_lesson,
    "blocked_state": _blocked_state_lesson,
    "suppression": _suppression_lesson,
    "thread": _thread_lesson,
}


def _support_count(signal: Any, lesson_type: str) -> int:
    evidence_for_axis = getattr(signal, "evidence_for_axis", None)
    if callable(evidence_for_axis):
        try:
            return max(0, int(evidence_for_axis(lesson_type).support_count))
        except (TypeError, ValueError):
            pass
    return _fallback_evidence_count(signal, lesson_type)


def _fallback_evidence_count(signal: Any, lesson_type: str) -> int:
    return evidence_count_for_axis(
        lesson_type,
        helpful_count=signal.helpful_count,
        not_helpful_count=signal.not_helpful_count,
        acknowledged_count=signal.acknowledged_count,
        failed_count=signal.failed_count,
        blocked_direct_failure_count=signal.blocked_direct_failure_count,
        blocked_native_success_count=signal.blocked_native_success_count,
        available_direct_success_count=signal.available_direct_success_count,
    )


def _evidence_count(signal: Any, lesson_type: str) -> int:
    return _support_count(signal, lesson_type)


def _confidence_for_evidence(evidence_count: int) -> float:
    return min(0.95, 0.55 + min(evidence_count, 4) * 0.08)


def _importance_for_lesson(lesson_type: str) -> float:
    if lesson_type in {"delivery", "timing", "blocked_state", "channel"}:
        return 0.82
    return 0.74


def _reinforcement_for_evidence(evidence_count: int) -> float:
    return 1.0 + min(evidence_count, 5) * 0.2


async def sync_learning_signal_memories(
    *,
    intervention_type: str,
    signal: Any,
    source_session_id: str | None = None,
    continuity_thread_id: str | None = None,
    active_project: str | None = None,
) -> None:
    normalized_intervention_type = str(intervention_type or "").strip() or "advisory"
    intervention_phrase = _intervention_phrase(normalized_intervention_type)
    confirmed_at = _now()
    normalized_active_project = " ".join(str(active_project or "").split()) or None

    for lesson_type, builder in _LESSON_BUILDERS.items():
        lesson = builder(signal, intervention_phrase)
        scope = {
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": normalized_intervention_type,
            "lesson_type": lesson_type,
        }
        if continuity_thread_id:
            scope["continuity_thread_id"] = continuity_thread_id
        if normalized_active_project:
            scope["active_project"] = normalized_active_project
        if lesson is None:
            await memory_repository.sync_scoped_memory(
                kind=MemoryKind.procedural,
                category=MemoryCategory.preference,
                scope=scope,
                content=None,
                metadata={"bias_value": "neutral"},
            )
            continue

        bias_value, content = lesson
        support_count = _support_count(signal, lesson_type)
        evidence_count = _evidence_count(signal, lesson_type)
        await memory_repository.sync_scoped_memory(
            kind=MemoryKind.procedural,
            category=MemoryCategory.preference,
            scope=scope,
            content=content,
            summary=content,
            confidence=_confidence_for_evidence(evidence_count),
            importance=_importance_for_lesson(lesson_type),
            reinforcement=_reinforcement_for_evidence(evidence_count),
            source_session_id=source_session_id,
            last_confirmed_at=confirmed_at,
            metadata={
                "bias_value": bias_value,
                "support_count": support_count,
                "evidence_count": evidence_count,
            },
        )
