from __future__ import annotations

from datetime import datetime, timezone


def strengthened_confidence(*, existing: float, candidate: float) -> float:
    return max(existing, candidate)


def strengthened_importance(*, existing: float, candidate: float) -> float:
    return max(existing, candidate)


def strengthened_reinforcement(*, existing: float, delta: float = 0.25) -> float:
    return max(0.0, existing + delta)


def latest_confirmation(
    *,
    existing: datetime | None,
    candidate: datetime | None,
) -> datetime | None:
    def _normalize(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    existing = _normalize(existing)
    candidate = _normalize(candidate)
    if existing is None:
        return candidate
    if candidate is None:
        return existing
    return candidate if candidate > existing else existing
