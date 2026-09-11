import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_scheduler_loop: asyncio.AbstractEventLoop | None = None


def _async_job_wrapper(
    coro_func,
    _loop: asyncio.AbstractEventLoop,
    *,
    job_id: str,
    allow_model_inference: bool = False,
):
    """Wrap an async job function so APScheduler 3.x can run it.

    Keep the returned callable async so AsyncIOScheduler tracks the real
    coroutine lifetime without blocking the app loop.
    """
    async def _run_with_authority():
        execution_job_id = f"scheduler:{job_id}:{uuid4().hex}"
        principal = TrustPrincipal(
            principal_id=f"service:scheduler:{job_id}",
            principal_type=PrincipalType.SERVICE,
            grants=((AuthorityGrant.MODEL_INFERENCE,) if allow_model_inference else ()),
            job_id=execution_job_id,
        )
        tokens = set_runtime_context(None, "high_risk", trust_principal=principal)
        try:
            await coro_func()
        finally:
            reset_runtime_context(tokens)

    async def wrapper():
        try:
            if asyncio.get_running_loop() is _loop:
                await _run_with_authority()
            else:
                future = asyncio.run_coroutine_threadsafe(_run_with_authority(), _loop)
                await asyncio.wrap_future(future)
        except Exception:
            logger.exception("Scheduled job %s failed", getattr(coro_func, "__name__", repr(coro_func)))
            raise
    return wrapper


def _validate_timezone(tz_name: str) -> str:
    """Validate timezone string, falling back to UTC with a warning."""
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(tz_name)
        return tz_name
    except (KeyError, Exception):
        logger.warning(
            "Invalid USER_TIMEZONE %r — falling back to UTC. "
            "See: python -c 'import zoneinfo; print(sorted(zoneinfo.available_timezones()))'",
            tz_name,
        )
        return "UTC"


def _settings_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = getattr(settings, name, default)
    if not isinstance(raw, (int, str)) or isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def _startup_next_run(enabled: bool, *, delay_seconds: int = 0) -> datetime | None:
    """Return an immediate first run for enabled jobs that should catch up local state."""
    return datetime.now(timezone.utc) + timedelta(seconds=max(delay_seconds, 0)) if enabled else None


def init_scheduler() -> AsyncIOScheduler | None:
    """Create and start the background scheduler with all configured jobs.

    Returns None if scheduler is disabled via settings.
    """
    global _scheduler, _scheduler_loop

    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false)")
        return None

    _scheduler = AsyncIOScheduler()
    validated_tz = _validate_timezone(settings.user_timezone)

    loop = asyncio.get_running_loop()
    _scheduler_loop = loop

    from src.scheduler.jobs.memory_consolidation import run_memory_consolidation
    from src.scheduler.jobs.goal_check import run_goal_check
    from src.scheduler.jobs.calendar_scan import run_calendar_scan
    from src.scheduler.jobs.strategist_tick import run_strategist_tick
    from src.scheduler.jobs.daily_briefing import run_daily_briefing
    from src.scheduler.jobs.evening_review import run_evening_review
    from src.scheduler.jobs.activity_digest import run_activity_digest
    from src.scheduler.jobs.end_of_day_goal_report import run_end_of_day_goal_report
    from src.scheduler.jobs.screenshot_folder_ingest import run_screenshot_folder_ingest
    from src.scheduler.jobs.screenshot_folder_analysis import run_screenshot_folder_analysis
    from src.scheduler.jobs.screenshot_observation_digest import run_screenshot_observation_digest
    from src.scheduler.jobs.weekly_activity_review import run_weekly_activity_review
    from src.scheduler.jobs.screen_cleanup import run_screen_cleanup
    from src.scheduler.jobs.audio_ingress_cleanup import run_audio_ingress_cleanup

    jobs = [
        {
            "func": _async_job_wrapper(run_memory_consolidation, loop, job_id="memory_consolidation", allow_model_inference=True),
            "trigger": IntervalTrigger(minutes=settings.memory_consolidation_interval_min),
            "id": "memory_consolidation",
            "name": "Memory consolidation",
        },
        {
            "func": _async_job_wrapper(run_goal_check, loop, job_id="goal_check"),
            "trigger": IntervalTrigger(hours=settings.goal_check_interval_hours),
            "id": "goal_check",
            "name": "Goal check",
        },
        {
            "func": _async_job_wrapper(run_calendar_scan, loop, job_id="calendar_scan"),
            "trigger": IntervalTrigger(minutes=settings.calendar_scan_interval_min),
            "id": "calendar_scan",
            "name": "Calendar scan",
        },
        {
            "func": _async_job_wrapper(run_strategist_tick, loop, job_id="strategist_tick", allow_model_inference=True),
            "trigger": IntervalTrigger(minutes=settings.strategist_interval_min),
            "id": "strategist_tick",
            "name": "Strategist tick",
        },
        {
            "func": _async_job_wrapper(run_daily_briefing, loop, job_id="daily_briefing", allow_model_inference=True),
            "trigger": CronTrigger(
                hour=settings.morning_briefing_hour,
                timezone=validated_tz,
            ),
            "id": "daily_briefing",
            "name": "Daily briefing",
        },
        {
            "func": _async_job_wrapper(run_evening_review, loop, job_id="evening_review", allow_model_inference=True),
            "trigger": CronTrigger(
                hour=settings.evening_review_hour,
                timezone=validated_tz,
            ),
            "id": "evening_review",
            "name": "Evening review",
        },
        {
            "func": _async_job_wrapper(run_activity_digest, loop, job_id="activity_digest", allow_model_inference=True),
            "trigger": CronTrigger(
                hour=settings.activity_digest_hour,
                timezone=validated_tz,
            ),
            "id": "activity_digest",
            "name": "Activity digest",
        },
        {
            "func": _async_job_wrapper(run_end_of_day_goal_report, loop, job_id="end_of_day_goal_report", allow_model_inference=True),
            "trigger": CronTrigger(
                hour=_settings_int("end_of_day_report_hour", 21, minimum=0, maximum=23),
                timezone=validated_tz,
            ),
            "id": "end_of_day_goal_report",
            "name": "End-of-day goal report",
        },
        {
            "func": _async_job_wrapper(run_weekly_activity_review, loop, job_id="weekly_activity_review", allow_model_inference=True),
            "trigger": CronTrigger(
                day_of_week="sun",
                hour=settings.weekly_review_hour,
                timezone=validated_tz,
            ),
            "id": "weekly_activity_review",
            "name": "Weekly activity review",
        },
        {
            "func": _async_job_wrapper(run_screenshot_folder_ingest, loop, job_id="screenshot_folder_ingest"),
            "trigger": IntervalTrigger(
                minutes=_settings_int("screenshot_folder_ingest_interval_min", 5, minimum=1, maximum=1440)
            ),
            "id": "screenshot_folder_ingest",
            "name": "Screenshot folder image ingest",
            "next_run_time": _startup_next_run(bool(settings.screenshot_folder_ingest_enabled)),
            "misfire_grace_time": 120,
        },
        {
            "func": _async_job_wrapper(run_screenshot_folder_analysis, loop, job_id="screenshot_folder_analysis", allow_model_inference=True),
            "trigger": IntervalTrigger(
                seconds=_settings_int("screenshot_folder_analysis_interval_seconds", 1, minimum=1, maximum=300)
            ),
            "id": "screenshot_folder_analysis",
            "name": "Screenshot folder semantic analysis",
            "next_run_time": _startup_next_run(bool(settings.screenshot_folder_ingest_enabled), delay_seconds=10),
            "misfire_grace_time": 120,
        },
        {
            "func": _async_job_wrapper(run_screenshot_observation_digest, loop, job_id="screenshot_observation_digest", allow_model_inference=True),
            "trigger": IntervalTrigger(
                minutes=_settings_int("screenshot_observation_digest_interval_min", 15, minimum=1, maximum=1440)
            ),
            "id": "screenshot_observation_digest",
            "name": "Screenshot observation digest",
            "next_run_time": _startup_next_run(bool(settings.screenshot_observation_digest_enabled)),
            "misfire_grace_time": 120,
        },
        {
            "func": _async_job_wrapper(run_screen_cleanup, loop, job_id="screen_cleanup"),
            "trigger": CronTrigger(hour=3, timezone=validated_tz),
            "id": "screen_cleanup",
            "name": "Screen observation cleanup",
        },
        {
            "func": _async_job_wrapper(run_audio_ingress_cleanup, loop, job_id="audio_ingress_cleanup"),
            "trigger": IntervalTrigger(seconds=60),
            "id": "audio_ingress_cleanup",
            "name": "Audio ingress retention cleanup",
            "next_run_time": _startup_next_run(True, delay_seconds=60),
            "misfire_grace_time": 120,
        },
    ]

    for job in jobs:
        try:
            _scheduler.add_job(**job, replace_existing=True, coalesce=True, max_instances=1)
        except Exception:
            logger.exception("Failed to register job: %s", job["id"])

    _scheduler.start()
    logger.info("Scheduler started with %d jobs", len(_scheduler.get_jobs()))
    return _scheduler


def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler if running."""
    global _scheduler, _scheduler_loop
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down")
        _scheduler = None
        _scheduler_loop = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


async def sync_scheduled_jobs() -> None:
    if _scheduler is None:
        return

    from src.scheduler.scheduled_jobs import build_cron_trigger, execute_scheduled_job, scheduled_job_repository

    jobs = await scheduled_job_repository.list_jobs(include_disabled=True, limit=None)
    wanted_ids = {job["id"] for job in jobs if job.get("enabled", False)}
    existing_ids = {
        scheduled_job.id.removeprefix("user_cron:")
        for scheduled_job in _scheduler.get_jobs()
        if scheduled_job.id.startswith("user_cron:")
    }

    for stale_job_id in existing_ids - wanted_ids:
        try:
            _scheduler.remove_job(f"user_cron:{stale_job_id}")
        except Exception:
            logger.exception("Failed to remove stale scheduled job %s", stale_job_id)

    if _scheduler_loop is None:
        return

    for job in jobs:
        apscheduler_id = f"user_cron:{job['id']}"
        if not job.get("enabled", False):
            if apscheduler_id in {scheduled_job.id for scheduled_job in _scheduler.get_jobs()}:
                try:
                    _scheduler.remove_job(apscheduler_id)
                except Exception:
                    logger.exception("Failed to remove disabled scheduled job %s", job["id"])
            continue
        try:
            _scheduler.add_job(
                _async_job_wrapper(
                    lambda job_id=job["id"]: execute_scheduled_job(job_id),
                    _scheduler_loop,
                    job_id=apscheduler_id,
                ),
                trigger=build_cron_trigger(job),
                id=apscheduler_id,
                name=job["name"],
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        except Exception:
            logger.exception("Failed to register scheduled job %s", job["id"])


def sync_scheduled_jobs_blocking() -> None:
    if _scheduler is None:
        return
    if _scheduler_loop is not None and _scheduler_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(sync_scheduled_jobs(), _scheduler_loop)
        future.result(timeout=5)
        return
    asyncio.run(sync_scheduled_jobs())
