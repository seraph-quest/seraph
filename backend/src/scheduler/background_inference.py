"""Operator toggles for scheduled jobs that may call the governed text route.

Every job defaults ON (the shipped behaviour). An operator can switch a job
off from Settings; a disabled job makes no model call and records a visible
``skipped`` status with reason ``disabled_by_operator``. The toggles live in
one private workspace JSON file, not in environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from threading import RLock

from config.settings import settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "seraph.background-inference.v1"
DISABLED_BY_OPERATOR = "disabled_by_operator"
SETTINGS_UNREADABLE = "background_inference_settings_unreadable"
_MAX_SETTINGS_BYTES = 16_384
_lock = RLock()
_last_status: dict[str, dict[str, str]] = {}


@dataclass(frozen=True)
class BackgroundInferenceJob:
    job_id: str
    label: str
    # ``job`` skips the whole run; ``llm_only`` keeps the deterministic part of
    # the job and skips only its model call.
    scope: str
    basis: str


BACKGROUND_INFERENCE_JOBS: tuple[BackgroundInferenceJob, ...] = (
    BackgroundInferenceJob("strategist_tick", "Strategist tick", "job",
        "one structured call per tick, regardless of goal opt-in"),
    BackgroundInferenceJob("memory_consolidation", "Memory consolidation", "job",
        "per run, one call for each recently active session (up to 10)"),
    BackgroundInferenceJob("daily_briefing", "Daily briefing", "job", "one call each morning"),
    BackgroundInferenceJob("evening_review", "Evening review", "job", "one call each evening"),
    BackgroundInferenceJob("activity_digest", "Activity digest", "job", "one call each day"),
    BackgroundInferenceJob("weekly_activity_review", "Weekly activity review", "job", "one call each Sunday"),
    BackgroundInferenceJob("end_of_day_goal_report", "End-of-day report analysis", "llm_only",
        "one call per report; the deterministic report still runs when off"),
    BackgroundInferenceJob("screenshot_observation_digest", "Screenshot observation digest", "job",
        "one call per run when new observations exist"),
)
_JOBS = {job.job_id: job for job in BACKGROUND_INFERENCE_JOBS}


def background_inference_settings_path() -> Path:
    return Path(settings.workspace_dir).expanduser().resolve() / "background-inference-settings.json"


def _interval_runs_per_day(name: str, default: int) -> float:
    raw = getattr(settings, name, default)
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = default
    return round(1440 / max(minutes, 1), 2)


def approximate_calls_per_day(job_id: str) -> float:
    """Return a conservative upper-bound estimate from the configured cadence."""
    if job_id == "strategist_tick":
        return _interval_runs_per_day("strategist_interval_min", 15)
    if job_id == "memory_consolidation":
        return _interval_runs_per_day("memory_consolidation_interval_min", 30)
    if job_id == "screenshot_observation_digest":
        return _interval_runs_per_day("screenshot_observation_digest_interval_min", 15)
    if job_id == "weekly_activity_review":
        return round(1 / 7, 2)
    return 1.0


def read_background_inference_settings() -> tuple[dict[str, bool], str | None]:
    """Return per-job enablement and an error code when the file is unreadable.

    A missing file means every job keeps its default ON state. An unreadable
    or malformed file fails closed for spend (every job is treated as off) and
    reports the error so the operator can re-save from Settings.
    """
    defaults = {job.job_id: True for job in BACKGROUND_INFERENCE_JOBS}
    path = background_inference_settings_path()
    try:
        if not path.exists():
            return defaults, None
        if path.stat().st_size > _MAX_SETTINGS_BYTES:
            raise ValueError("background inference settings are too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported background inference settings schema")
        jobs = payload.get("jobs")
        if not isinstance(jobs, dict) or any(type(value) is not bool for value in jobs.values()):
            raise ValueError("background inference job toggles must be booleans")
    except (OSError, ValueError, json.JSONDecodeError):
        logger.warning("Background inference settings are unreadable; scheduled model calls are paused")
        return {job_id: False for job_id in defaults}, SETTINGS_UNREADABLE
    return {job_id: bool(jobs.get(job_id, True)) for job_id in defaults}, None


class BackgroundInferenceSettingsUnreadable(ValueError):
    """The stored toggles cannot be read, so a partial save would guess the rest."""


def write_background_inference_settings(jobs: dict[str, bool], *, replace_unreadable: bool = False) -> dict[str, bool]:
    unknown = set(jobs) - set(_JOBS)
    if unknown:
        raise ValueError("unknown background inference job")
    if any(type(value) is not bool for value in jobs.values()):
        raise ValueError("background inference job toggles must be booleans")
    with _lock:
        current, error = read_background_inference_settings()
        if error is not None:
            # The operator's other choices are unknown. Saving one toggle must
            # not silently turn every other job back ON (or OFF): replacing
            # the file needs an explicit confirmation and every job's value.
            if not replace_unreadable:
                raise BackgroundInferenceSettingsUnreadable(SETTINGS_UNREADABLE)
            if set(jobs) != set(_JOBS):
                raise ValueError("replacing unreadable background inference settings requires every job's toggle")
            current = {}
        merged = {**current, **jobs}
        path = background_inference_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "jobs": merged,
        }, indent=2, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        return merged


def background_inference_job_enabled(job_id: str) -> bool:
    """Return whether a governed background job may make its model call."""
    if job_id not in _JOBS:
        return True
    enabled, _error = read_background_inference_settings()
    return enabled[job_id]


def is_whole_job_toggle(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    return job is not None and job.scope == "job"


def record_background_inference_skip(job_id: str, reason: str = DISABLED_BY_OPERATOR) -> None:
    with _lock:
        _last_status[job_id] = {
            "status": "skipped",
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }


def background_inference_status() -> dict[str, object]:
    enabled, error = read_background_inference_settings()
    with _lock:
        last = {job_id: dict(value) for job_id, value in _last_status.items()}
    jobs = []
    for job in BACKGROUND_INFERENCE_JOBS:
        jobs.append({
            "job_id": job.job_id,
            "label": job.label,
            "enabled": enabled[job.job_id],
            "scope": job.scope,
            "approx_calls_per_day": approximate_calls_per_day(job.job_id),
            "estimate_basis": job.basis,
            "last_skip": last.get(job.job_id),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "scheduler_enabled": bool(settings.scheduler_enabled),
        "error_code": error,
        "jobs": jobs,
    }
