from __future__ import annotations

import json
from dataclasses import dataclass

from src.db.models import MemoryKind
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


def _memory_text(memory) -> str:
    return (memory.content or memory.summary or "").strip()


def _memory_metadata(memory) -> dict[str, object]:
    try:
        payload = json.loads(memory.metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def load_procedural_memory_guidance(
    intervention_type: str,
) -> ProceduralMemoryGuidance:
    normalized_intervention_type = str(intervention_type or "").strip() or "advisory"
    memories = await memory_repository.list_memories_for_scope(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": normalized_intervention_type,
        },
        limit=32,
    )

    guidance_by_field: dict[str, str] = {}
    lesson_types: list[str] = []
    lessons: list[str] = []

    for lesson_type in _LESSON_ORDER:
        matching_memory = None
        matching_bias = ""
        for memory in memories:
            metadata = _memory_metadata(memory)
            if metadata.get("lesson_type") != lesson_type:
                continue
            bias_value = str(metadata.get("bias_value") or "").strip()
            if not bias_value or bias_value == "neutral":
                continue
            matching_memory = memory
            matching_bias = bias_value
            break

        if matching_memory is None:
            continue

        field_name = _LESSON_FIELD_BY_TYPE[lesson_type]
        guidance_by_field[field_name] = matching_bias
        lesson_types.append(lesson_type)
        text = _memory_text(matching_memory)
        if text:
            lessons.append(text)

    return ProceduralMemoryGuidance(
        intervention_type=normalized_intervention_type,
        lesson_types=tuple(lesson_types),
        lessons=tuple(lessons),
        **guidance_by_field,
    )
