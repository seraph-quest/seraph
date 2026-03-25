from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.db.models import MemoryCategory, MemoryKind


_LEGACY_FIELD_TO_KIND = {
    "facts": MemoryKind.fact,
    "patterns": MemoryKind.pattern,
    "goals": MemoryKind.goal,
    "reflections": MemoryKind.reflection,
}

_KIND_TO_CATEGORY = {
    MemoryKind.fact: MemoryCategory.fact,
    MemoryKind.pattern: MemoryCategory.pattern,
    MemoryKind.goal: MemoryCategory.goal,
    MemoryKind.commitment: MemoryCategory.goal,
    MemoryKind.preference: MemoryCategory.preference,
    MemoryKind.communication_preference: MemoryCategory.preference,
    MemoryKind.procedural: MemoryCategory.preference,
    MemoryKind.reflection: MemoryCategory.reflection,
    MemoryKind.project: MemoryCategory.fact,
    MemoryKind.collaborator: MemoryCategory.fact,
    MemoryKind.obligation: MemoryCategory.fact,
    MemoryKind.routine: MemoryCategory.fact,
    MemoryKind.timeline: MemoryCategory.fact,
}


@dataclass(frozen=True)
class ConsolidatedMemoryItem:
    text: str
    kind: MemoryKind
    category: MemoryCategory
    summary: str | None = None
    confidence: float = 0.5
    importance: float = 0.5
    last_confirmed_at: datetime | None = None
    subject_name: str | None = None
    project_name: str | None = None
    metadata: dict[str, Any] | None = None


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def _clamp_score(value: Any, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return default


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_memory_kind(value: MemoryKind | str) -> MemoryKind:
    if isinstance(value, MemoryKind):
        return value
    return MemoryKind(value)


def kind_to_category(kind: MemoryKind | str) -> MemoryCategory:
    return _KIND_TO_CATEGORY[normalize_memory_kind(kind)]


def bucket_name_for_kind(kind: MemoryKind | str) -> str:
    normalized = normalize_memory_kind(kind)
    if normalized == MemoryKind.communication_preference:
        return MemoryKind.preference.value
    return normalized.value


def parse_consolidated_memories(
    payload: dict[str, Any],
    *,
    fallback_confirmed_at: datetime | None = None,
) -> list[ConsolidatedMemoryItem]:
    parsed: list[ConsolidatedMemoryItem] = []
    seen: set[tuple[str, str]] = set()

    def _append(item: ConsolidatedMemoryItem) -> None:
        key = (bucket_name_for_kind(item.kind), item.text.lower())
        if key in seen:
            return
        seen.add(key)
        parsed.append(item)

    raw_memories = payload.get("memories", [])
    if isinstance(raw_memories, list):
        for raw_item in raw_memories:
            if not isinstance(raw_item, dict):
                continue
            text = _normalize_text(raw_item.get("text"))
            if len(text) <= 10:
                continue
            kind_value = raw_item.get("kind")
            if not isinstance(kind_value, str):
                continue
            try:
                kind = normalize_memory_kind(kind_value)
            except ValueError:
                continue
            _append(
                ConsolidatedMemoryItem(
                    text=text,
                    kind=kind,
                    category=kind_to_category(kind),
                    summary=_normalize_text(raw_item.get("summary")) or None,
                    confidence=_clamp_score(raw_item.get("confidence"), default=0.65),
                    importance=_clamp_score(raw_item.get("importance"), default=0.65),
                    last_confirmed_at=(
                        _parse_timestamp(raw_item.get("last_confirmed_at"))
                        or fallback_confirmed_at
                    ),
                    subject_name=_normalize_text(
                        raw_item.get("subject") or raw_item.get("subject_name")
                    ) or None,
                    project_name=_normalize_text(
                        raw_item.get("project") or raw_item.get("project_name")
                    ) or None,
                    metadata={
                        "input_schema": "typed",
                    },
                )
            )

    for field_name, kind in _LEGACY_FIELD_TO_KIND.items():
        items = payload.get(field_name, [])
        if not isinstance(items, list):
            continue
        for raw_text in items:
            text = _normalize_text(raw_text)
            if len(text) <= 10:
                continue
            _append(
                ConsolidatedMemoryItem(
                    text=text,
                    kind=kind,
                    category=kind_to_category(kind),
                    confidence=0.6,
                    importance=0.6,
                    last_confirmed_at=fallback_confirmed_at,
                    metadata={"input_schema": "legacy", "legacy_field": field_name},
                )
            )

    return parsed
