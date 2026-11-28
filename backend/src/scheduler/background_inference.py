"""Operator toggles for scheduled jobs that may call the governed text route.

Every job defaults ON (the shipped behaviour). An operator can switch a job
off from Settings; a disabled job makes no model call and records a visible
``skipped`` status with reason ``disabled_by_operator``. The toggles live in
one private workspace JSON file, not in environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
# A switched-off job polled every second would otherwise write one audit row
# per poll. The audit records each transition into a skip reason, then at most
# hourly while it persists; the visible ``last_skip`` still updates every run.
SKIP_AUDIT_INTERVAL = timedelta(hours=1)


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
    BackgroundInferenceJob("guardian_opportunity_assessment", "Guardian opportunity assessment", "llm_only",
        "one call per queued opportunity, at most 16 per tick; queued candidates stay visible until their deadline when off"),
    BackgroundInferenceJob("screenshot_folder_analysis", "Screenshot folder analysis (vision route)", "job",
        "one vision call per pending screenshot, within the configured limit and concurrency"),
)
# Event-driven jobs: their call count follows queued work, not a fixed cadence.
_EVENT_DRIVEN_JOBS = frozenset({"guardian_opportunity_assessment", "screenshot_folder_analysis"})
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


def approximate_calls_per_day(job_id: str) -> float | None:
    """Return a conservative upper-bound estimate from the configured cadence.

    ``None`` means the job is event-driven and its basis text is the estimate.
    """
    if job_id in _EVENT_DRIVEN_JOBS:
        return None
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


def background_inference_skip_reason(job_id: str) -> str | None:
    """Return why a toggled job may not make its model call now, or ``None``."""
    if background_inference_job_enabled(job_id):
        return None
    _enabled, error = read_background_inference_settings()
    return SETTINGS_UNREADABLE if error else DISABLED_BY_OPERATOR


def is_whole_job_toggle(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    return job is not None and job.scope == "job"


# Activity entries for a job that stays blocked are written at most this often.
BLOCKED_ACTIVITY_INTERVAL_SECONDS = 3600
LIFECYCLE_SOURCE = "lifecycle"

# The per-job status record is the durable ``background_job_status`` table
# (#1025 D2, ``src.scheduler.job_status``). #1020 PR1's JSON status file is
# folded into it once per workspace and removed.


def status_job_known(job_id: object) -> bool:
    """Inference toggles plus every lifecycle-gated scheduler job (#1025) share this one status record."""
    if not isinstance(job_id, str):
        return False
    if job_id in _JOBS:
        return True
    from src.runtime_plugins.lifecycle import LifecycleBlocked, job_domain
    try:
        return job_domain(job_id) is not None
    except LifecycleBlocked:
        return False


async def record_background_inference_skip(job_id: str, reason: str = DISABLED_BY_OPERATOR) -> bool:
    """Record a visible skip (persisted); return whether it is due an audit event."""
    from src.scheduler import job_status
    return await job_status.record_skip(job_id, reason, audit_interval=SKIP_AUDIT_INTERVAL)


async def record_background_inference_run(job_id: str) -> None:
    """Mark that the job ran, so its next skip is a new transition."""
    from src.scheduler import job_status
    await job_status.record_run(job_id)


async def record_background_inference_blocked(job_id: str, reason: str, recovery: str) -> dict[str, bool]:
    """Persist a runtime block; report whether this is a transition into blocked (its one notification
    is claimed in the same transaction) and whether an Activity entry is due (at most hourly)."""
    from src.scheduler import job_status
    state = await job_status.record_blocked(
        job_id, reason, recovery, source=job_status.SOURCE_GUARDIAN,
        activity_interval=timedelta(seconds=BLOCKED_ACTIVITY_INTERVAL_SECONDS))
    return {"transition": state["notify"], "activity_due": state["activity_due"]}


async def record_background_inference_succeeded(job_id: str) -> None:
    """A completed run clears a persisted block."""
    from src.scheduler import job_status
    await job_status.record_ok(job_id)


async def record_scheduler_job_blocked(job_id: str, reason: str, recovery: str, *, epoch: int | None = None) -> None:
    """A tick refused by its lifecycle domain (#1025): persisted, survives restart.

    The domain announces its own transition into blocked once; repeated blocked
    ticks only refresh the time. A guardian or operator status of the same job
    is replaced by the newer lifecycle block.
    """
    if not status_job_known(job_id):
        return
    from src.scheduler import job_status
    await job_status.record_blocked(job_id, reason, recovery, source=job_status.SOURCE_LIFECYCLE, epoch=epoch,
                                    keep_since_on_reason_change=False)


async def record_scheduler_job_ok(job_id: str, *, epoch: int | None = None) -> None:
    """The first tick that ran under an active domain clears only a lifecycle block."""
    from src.scheduler import job_status
    await job_status.record_ok(job_id, only_source=job_status.SOURCE_LIFECYCLE, clear_states=("blocked", "skipped"),
                               epoch=epoch)


async def record_scheduler_job_missed(job_id: str) -> None:
    """APScheduler dropped a fire past its misfire grace; a later run replaces this."""
    if not status_job_known(job_id):
        return
    from src.scheduler import job_status
    await job_status.record_missed(job_id)


async def scheduler_job_blocks() -> list[dict[str, object]]:
    """Every scheduler job whose last recorded status is a lifecycle block."""
    from src.scheduler import job_status
    rows = await job_status.statuses(lambda row: row.state == "blocked" and row.source == LIFECYCLE_SOURCE
                                     and not row.job_id.startswith(job_status.DOMAIN_KEY_PREFIX))
    return [{"job_id": job_id, **entry} for job_id, entry in rows.items()]


async def background_inference_status() -> dict[str, object]:
    from src.scheduler import job_status
    enabled, error = read_background_inference_settings()
    last = await job_status.statuses(lambda row: row.job_id in _JOBS)
    jobs = []
    for job in BACKGROUND_INFERENCE_JOBS:
        jobs.append({
            "job_id": job.job_id,
            "label": job.label,
            "enabled": enabled[job.job_id],
            "scope": job.scope,
            "approx_calls_per_day": approximate_calls_per_day(job.job_id),
            "estimate_basis": job.basis,
            # Historical key: the last skipped or blocked run (with its fix).
            "last_skip": last.get(job.job_id),
            "blocked": (last.get(job.job_id) or {}).get("status") == "blocked",
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "scheduler_enabled": _scheduler_enabled(),
        "error_code": error,
        "jobs": jobs,
    }


def _scheduler_enabled() -> bool:
    # The effective value after ADR-036 patch layers (#1204).
    from src.runtime_plugins.profiles import scheduler_enabled
    return scheduler_enabled(settings.scheduler_enabled)
