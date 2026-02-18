import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


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
    global _scheduler

    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false)")
        return None

    _scheduler = AsyncIOScheduler()
    validated_tz = _validate_timezone(settings.user_timezone)

    loop = asyncio.get_running_loop()

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
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down")
        _scheduler = None
