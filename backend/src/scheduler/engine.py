import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_scheduler_loop: asyncio.AbstractEventLoop | None = None


def _async_job_wrapper(coro_func, loop: asyncio.AbstractEventLoop):
    """Wrap an async job function so APScheduler 3.x can run it.

    APScheduler 3.x runs jobs in a ThreadPoolExecutor, so we need to schedule
    the coroutine back onto the main event loop captured at init time.
    """
    def wrapper():
        asyncio.run_coroutine_threadsafe(coro_func(), loop)
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
    from src.scheduler.jobs.weekly_activity_review import run_weekly_activity_review
    from src.scheduler.jobs.screen_cleanup import run_screen_cleanup

    jobs = [
        {
            "func": _async_job_wrapper(run_memory_consolidation, loop),
            "trigger": IntervalTrigger(minutes=settings.memory_consolidation_interval_min),
            "id": "memory_consolidation",
            "name": "Memory consolidation",
        },
        {
            "func": _async_job_wrapper(run_goal_check, loop),
            "trigger": IntervalTrigger(hours=settings.goal_check_interval_hours),
            "id": "goal_check",
            "name": "Goal check",
        },
        {
            "func": _async_job_wrapper(run_calendar_scan, loop),
            "trigger": IntervalTrigger(minutes=settings.calendar_scan_interval_min),
            "id": "calendar_scan",
            "name": "Calendar scan",
        },
        {
            "func": _async_job_wrapper(run_strategist_tick, loop),
            "trigger": IntervalTrigger(minutes=settings.strategist_interval_min),
            "id": "strategist_tick",
            "name": "Strategist tick",
        },
        {
            "func": _async_job_wrapper(run_daily_briefing, loop),
            "trigger": CronTrigger(
                hour=settings.morning_briefing_hour,
                timezone=validated_tz,
            ),
            "id": "daily_briefing",
            "name": "Daily briefing",
        },
        {
            "func": _async_job_wrapper(run_evening_review, loop),
            "trigger": CronTrigger(
                hour=settings.evening_review_hour,
                timezone=validated_tz,
            ),
            "id": "evening_review",
            "name": "Evening review",
        },
        {
            "func": _async_job_wrapper(run_activity_digest, loop),
            "trigger": CronTrigger(
                hour=settings.activity_digest_hour,
                timezone=validated_tz,
            ),
            "id": "activity_digest",
            "name": "Activity digest",
        },
        {
            "func": _async_job_wrapper(run_weekly_activity_review, loop),
            "trigger": CronTrigger(
                day_of_week="sun",
                hour=settings.weekly_review_hour,
                timezone=validated_tz,
            ),
            "id": "weekly_activity_review",
            "name": "Weekly activity review",
        },
        {
            "func": _async_job_wrapper(run_screen_cleanup, loop),
            "trigger": CronTrigger(hour=3, timezone=validated_tz),
            "id": "screen_cleanup",
            "name": "Screen observation cleanup",
        },
    ]

    for job in jobs:
        try:
            _scheduler.add_job(**job, replace_existing=True)
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
                _async_job_wrapper(lambda job_id=job["id"]: execute_scheduled_job(job_id), _scheduler_loop),
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
