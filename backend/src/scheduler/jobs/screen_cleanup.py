"""Screen observation cleanup — deletes observations older than retention period."""

import logging
from time import perf_counter

from config.settings import settings
from src.audit.runtime import log_scheduler_job_event

logger = logging.getLogger(__name__)


async def run_screen_cleanup() -> None:
    """Delete screen observations older than the configured retention period."""
    started_at = perf_counter()
    try:
        from src.observer.screen_repository import screen_observation_repo

        deleted = await screen_observation_repo.cleanup_old(
            settings.screen_observation_retention_days
        )
        if deleted == 0:
            await log_scheduler_job_event(
                job_name="screen_cleanup",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "no_expired_observations",
                    "retention_days": settings.screen_observation_retention_days,
                },
            )
            return

        await log_scheduler_job_event(
            job_name="screen_cleanup",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "deleted_count": deleted,
                "retention_days": settings.screen_observation_retention_days,
            },
        )
        logger.info(
            "screen_cleanup: removed %d observations older than %d days",
            deleted,
            settings.screen_observation_retention_days,
        )
    except Exception as exc:
        await log_scheduler_job_event(
            job_name="screen_cleanup",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
                "retention_days": settings.screen_observation_retention_days,
            },
        )
        logger.exception("screen_cleanup failed")
