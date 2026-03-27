from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

_AXIS_FIELD_PAIRS = (
    ("delivery", "bias"),
    ("phrasing", "phrasing_bias"),
    ("cadence", "cadence_bias"),
    ("channel", "channel_bias"),
    ("escalation", "escalation_bias"),
    ("timing", "timing_bias"),
    ("blocked_state", "blocked_state_bias"),
    ("suppression", "suppression_bias"),
    ("thread", "thread_preference_bias"),
)

_FIELD_BY_AXIS = {axis: field_name for axis, field_name in _AXIS_FIELD_PAIRS}
_AXIS_BY_FIELD = {field_name: axis for axis, field_name in _AXIS_FIELD_PAIRS}

_GUARDIAN_CONFIDENCE_SCORE = {
    "grounded": 1.0,
    "partial": 0.6,
    "degraded": 0.25,
    "empty": 0.0,
}

_DATA_QUALITY_SCORE = {
    "good": 1.0,
    "stale": 0.55,
    "degraded": 0.25,
}


def ordered_learning_axes() -> tuple[str, ...]:
    return tuple(axis for axis, _field_name in _AXIS_FIELD_PAIRS)


def learning_axis_for_field(field_name: str) -> str:
    return _AXIS_BY_FIELD[field_name]


def learning_field_for_axis(axis: str) -> str:
    return _FIELD_BY_AXIS[axis]


def evidence_count_for_axis(
    axis: str,
    *,
    helpful_count: int,
    not_helpful_count: int,
    acknowledged_count: int,
    failed_count: int,
    blocked_direct_failure_count: int,
    blocked_native_success_count: int,
    available_direct_success_count: int,
) -> int:
    if axis in {"delivery", "phrasing", "cadence", "suppression"}:
        return int(helpful_count + not_helpful_count + failed_count)
    if axis in {"channel", "escalation"}:
        return int(acknowledged_count + helpful_count)
    if axis == "timing":
        return int(blocked_direct_failure_count + available_direct_success_count)
    if axis == "blocked_state":
        return int(blocked_direct_failure_count + blocked_native_success_count)
    if axis == "thread":
        return int(helpful_count + acknowledged_count + failed_count)
    return int(helpful_count + not_helpful_count + acknowledged_count + failed_count)


def guardian_confidence_score(value: str | None) -> float:
    if value is None:
        return 0.5
    return _GUARDIAN_CONFIDENCE_SCORE.get(str(value).strip().lower(), 0.5)


def data_quality_score(value: str | None) -> float:
    if value is None:
        return 0.5
    return _DATA_QUALITY_SCORE.get(str(value).strip().lower(), 0.5)


def clamp_unit_interval(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def recency_score_for_timestamp(
    value: datetime | None,
    *,
    now: datetime | None = None,
    max_age_days: float = 30.0,
) -> float:
    if value is None:
        return 0.0
    if now is None:
        now = datetime.now(timezone.utc)
    normalized_value = value
    if normalized_value.tzinfo is None:
        normalized_value = normalized_value.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (now - normalized_value.astimezone(timezone.utc)).total_seconds())
    age_days = age_seconds / 86400.0
    if max_age_days <= 0:
        return 0.0
    return clamp_unit_interval(1.0 - (age_days / max_age_days))


@dataclass(frozen=True)
class GuardianLearningAxisEvidence:
    axis: str
    field_name: str
    source: str
    bias: str = "neutral"
    support_count: int = 0
    recency_score: float = 0.0
    confidence_score: float = 0.0
    quality_score: float = 0.0
    last_confirmed_at: datetime | None = None
    metadata_complete: bool = True


def neutral_axis_evidence(axis: str, *, source: str) -> GuardianLearningAxisEvidence:
    return GuardianLearningAxisEvidence(
        axis=axis,
        field_name=learning_field_for_axis(axis),
        source=source,
    )
