"""Observer salience and confidence scoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class ObserverAssessment:
    observer_confidence: str
    salience_level: str
    salience_reason: str
    interruption_cost: str


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _has_upcoming_event_soon(upcoming_events: list[dict]) -> bool:
    now = datetime.now(timezone.utc)
    for event in upcoming_events[:3]:
        parsed = _parse_timestamp(event.get("start"))
        if parsed is None:
            continue
        delta_seconds = (parsed - now).total_seconds()
        if 0 <= delta_seconds <= 60 * 60:
            return True
    return False


def derive_observer_assessment(
    *,
    current_event: str | None,
    upcoming_events: list[dict],
    recent_git_activity: list[dict] | None,
    active_goals_summary: str,
    active_window: str | None,
    screen_context: str | None,
    data_quality: str,
    user_state: str,
    interruption_mode: str,
    attention_budget_remaining: int,
) -> ObserverAssessment:
    has_active_goals = bool(active_goals_summary.strip())
    has_recent_git_activity = bool(recent_git_activity)
    has_screen_activity = bool((screen_context or "").strip() or (active_window or "").strip())

    if data_quality == "good":
        observer_confidence = "grounded"
    elif data_quality == "degraded":
        observer_confidence = "partial"
    else:
        observer_confidence = "degraded"

    upcoming_event_soon = _has_upcoming_event_soon(upcoming_events)
    aligned_work_activity = has_active_goals and has_recent_git_activity and has_screen_activity

    if current_event:
        salience_level = "high"
        salience_reason = "current_event"
    elif upcoming_event_soon:
        salience_level = "high"
        salience_reason = "upcoming_event"
    elif aligned_work_activity:
        salience_level = "high"
        salience_reason = "aligned_work_activity"
    elif has_active_goals:
        salience_level = "medium"
        salience_reason = "active_goals"
    elif has_recent_git_activity:
        salience_level = "medium"
        salience_reason = "recent_git_activity"
    elif has_screen_activity:
        salience_level = "medium"
        salience_reason = "screen_activity"
    else:
        salience_level = "low"
        salience_reason = "background"

    if (
        user_state in {"deep_work", "in_meeting"}
        or interruption_mode == "focus"
        or current_event is not None
        or upcoming_event_soon
        or attention_budget_remaining <= 1
    ):
        interruption_cost = "high"
    elif user_state in {"away", "winding_down"}:
        interruption_cost = "medium"
    else:
        interruption_cost = "low"

    return ObserverAssessment(
        observer_confidence=observer_confidence,
        salience_level=salience_level,
        salience_reason=salience_reason,
        interruption_cost=interruption_cost,
    )
