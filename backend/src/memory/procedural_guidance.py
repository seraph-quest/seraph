from __future__ import annotations

import json
from dataclasses import dataclass

from src.db.models import MemoryKind
from src.guardian.learning_evidence import (
    GuardianLearningAxisEvidence,
    clamp_unit_interval,
    learning_axis_for_field,
    learning_field_for_axis,
    neutral_axis_evidence,
    ordered_learning_axes,
    recency_score_for_timestamp,
)
from src.memory.repository import memory_repository

_LESSON_FIELD_BY_TYPE = {
    "delivery": "bias",
    "phrasing": "phrasing_bias",
    "cadence": "cadence_bias",
    "channel": "channel_bias",
    "escalation": "escalation_bias",
    "timing": "timing_bias",
    "blocked_state": "blocked_state_bias",
    "suppression": "suppression_bias",
    "thread": "thread_preference_bias",
}

_LESSON_ORDER = (
    "delivery",
    "phrasing",
    "cadence",
    "channel",
    "escalation",
    "timing",
    "blocked_state",
    "suppression",
    "thread",
)

_CONTEXT_SCOPE_KEYS = ("continuity_thread_id", "active_project")


@dataclass(frozen=True)
class ProceduralMemoryGuidance:
    intervention_type: str
    bias: str = "neutral"
    phrasing_bias: str = "neutral"
    cadence_bias: str = "neutral"
    channel_bias: str = "neutral"
    escalation_bias: str = "neutral"
    timing_bias: str = "neutral"
    blocked_state_bias: str = "neutral"
    suppression_bias: str = "neutral"
    thread_preference_bias: str = "neutral"
    lesson_types: tuple[str, ...] = ()
    lessons: tuple[str, ...] = ()
    axis_evidence: tuple[GuardianLearningAxisEvidence, ...] = ()

    def bias_overrides(self) -> dict[str, str]:
        overrides: dict[str, str] = {}
        for field_name in _LESSON_FIELD_BY_TYPE.values():
            value = getattr(self, field_name)
            if value != "neutral":
                overrides[field_name] = value
        return overrides

    @property
    def has_active_guidance(self) -> bool:
        return bool(self.bias_overrides())

    def evidence_by_axis(self) -> dict[str, GuardianLearningAxisEvidence]:
        return {item.axis: item for item in self.axis_evidence}

    def evidence_for_axis(self, axis: str) -> GuardianLearningAxisEvidence:
        return self.evidence_by_axis().get(
            axis,
            neutral_axis_evidence(axis, source="procedural_memory"),
        )


def _memory_text(memory) -> str:
    return (memory.content or memory.summary or "").strip()


def _memory_metadata(memory) -> dict[str, object]:
    try:
        payload = json.loads(memory.metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _matches_context_scope(metadata: dict[str, object], scope: dict[str, object]) -> bool:
    return all(metadata.get(key) == scope.get(key) for key in _CONTEXT_SCOPE_KEYS) and all(
        (key in scope) or (metadata.get(key) is None) for key in _CONTEXT_SCOPE_KEYS
    )


def _metadata_int(metadata: dict[str, object], key: str) -> int:
    value = metadata.get(key)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip())))
        except ValueError:
            return 0
    return 0


def _metadata_support_count(metadata: dict[str, object]) -> int:
    if "support_count" in metadata:
        return _metadata_int(metadata, "support_count")
    return _metadata_int(metadata, "evidence_count")


def _procedural_quality_score(memory, metadata: dict[str, object], *, metadata_complete: bool) -> float:
    if "weighted_support" in metadata:
        try:
            return clamp_unit_interval(float(metadata["weighted_support"]) / 3.0)
        except (TypeError, ValueError):
            pass
    if memory.reinforcement is None:
        return 0.0
    score = clamp_unit_interval(float(memory.reinforcement) / 2.0)
    if metadata_complete:
        return round(score, 3)
    return round(score * 0.8, 3)


async def load_procedural_memory_guidance(
    intervention_type: str,
    *,
    continuity_thread_id: str | None = None,
    active_project: str | None = None,
) -> ProceduralMemoryGuidance:
    normalized_intervention_type = str(intervention_type or "").strip() or "advisory"
    normalized_active_project = " ".join(str(active_project or "").split()) or None
    scope_candidates: list[dict[str, object]] = []
    base_scope = {
        "writer": "guardian_feedback",
        "memory_scope": "procedural_learning",
        "intervention_type": normalized_intervention_type,
    }
    if continuity_thread_id and normalized_active_project:
        scope_candidates.append(
            {
                **base_scope,
                "continuity_thread_id": continuity_thread_id,
                "active_project": normalized_active_project,
            }
        )
    if continuity_thread_id:
        scope_candidates.append(
            {
                **base_scope,
                "continuity_thread_id": continuity_thread_id,
            }
        )
    if normalized_active_project:
        scope_candidates.append(
            {
                **base_scope,
                "active_project": normalized_active_project,
            }
        )
    scope_candidates.append(base_scope)

    scoped_memories: list[tuple[int, object]] = []
    for index, scope in enumerate(scope_candidates):
        scoped_memories.extend(
            (index, scope, memory)
            for memory in await memory_repository.list_memories_for_scope(
                kind=MemoryKind.procedural,
                scope=scope,
                limit=32,
            )
        )

    guidance_by_field: dict[str, str] = {}
    lesson_types: list[str] = []
    lessons: list[str] = []
    evidence_by_axis: dict[str, GuardianLearningAxisEvidence] = {}

    for lesson_type in _LESSON_ORDER:
        matching_memory = None
        matching_bias = ""
        matching_metadata: dict[str, object] = {}
        for scope_index, scope, memory in scoped_memories:
            metadata = _memory_metadata(memory)
            if not _matches_context_scope(metadata, scope):
                continue
            if metadata.get("lesson_type") != lesson_type:
                continue
            bias_value = str(metadata.get("bias_value") or "").strip()
            if not bias_value or bias_value == "neutral":
                continue
            matching_memory = memory
            matching_bias = bias_value
            matching_metadata = metadata
            break

        if matching_memory is None:
            continue

        field_name = _LESSON_FIELD_BY_TYPE[lesson_type]
        guidance_by_field[field_name] = matching_bias
        lesson_types.append(lesson_type)
        text = _memory_text(matching_memory)
        if text:
            lessons.append(text)
        axis = learning_axis_for_field(field_name)
        metadata_complete = "bias_value" in matching_metadata and (
            "support_count" in matching_metadata or "evidence_count" in matching_metadata
        )
        last_confirmed_at = matching_memory.last_confirmed_at or matching_memory.updated_at
        evidence_by_axis[axis] = GuardianLearningAxisEvidence(
            axis=axis,
            field_name=learning_field_for_axis(axis),
            source="procedural_memory",
            bias=matching_bias,
            support_count=_metadata_support_count(matching_metadata),
            recency_score=round(recency_score_for_timestamp(last_confirmed_at), 3),
            confidence_score=round(clamp_unit_interval(float(matching_memory.confidence or 0.0)), 3),
            quality_score=_procedural_quality_score(
                matching_memory,
                matching_metadata,
                metadata_complete=metadata_complete,
            ),
            last_confirmed_at=last_confirmed_at,
            metadata_complete=metadata_complete,
        )

    axis_evidence = tuple(
        evidence_by_axis.get(axis, neutral_axis_evidence(axis, source="procedural_memory"))
        for axis in ordered_learning_axes()
    )

    return ProceduralMemoryGuidance(
        intervention_type=normalized_intervention_type,
        lesson_types=tuple(lesson_types),
        lessons=tuple(lessons),
        axis_evidence=axis_evidence,
        **guidance_by_field,
    )
