"""Screen observation cleanup — deletes observations older than retention period.

Also prunes the private daemon capture upload store (ADR-035, #1263): files
older than the shorter of the observation retention and
``archive_retention_days`` go first, then the oldest files until the store is
at or under ``archive_max_mb`` (``0`` keeps no size cap). These local files
are not backed up by this job; private capture backup is tracked in #1141.
"""

import asyncio
import logging
from time import perf_counter

from config.settings import settings
from src.audit.runtime import log_scheduler_job_event
from src.observer.local_capture_upload import CaptureStorePruneResult

logger = logging.getLogger(__name__)


async def _cleanup_capture_uploads() -> tuple[CaptureStorePruneResult, int | None, int | None]:
    """Bounded age and size prune of daemon capture files; never fails the job.

    Returns the prune result with the size cap (MiB) and retention days used.
    """
    max_mb: int | None = None
    retention_days: int | None = None
    try:
        from src.observer.local_capture_upload import (
            MIB,
            capture_store_limits,
            prune_local_capture_uploads,
        )
        from src.observer.screenshot_folder_source import screenshot_ingest_lock

        limits = capture_store_limits()
        if limits is None:
            # A settings file exists but is unreadable: the operator's limits
            # are unknown, so nothing is pruned on defaults.
            logger.warning("screen_cleanup: capture store prune skipped, screen-analysis settings unreadable")
            return CaptureStorePruneResult(skipped_reason="settings_unreadable"), None, None
        max_bytes, retention_days = limits
        max_mb = max_bytes // MIB
        # The upload path writes and prunes under the same lock, so this run
        # never overlaps a write, a ``.partial`` rename or a capture-id check.
        async with screenshot_ingest_lock():
            result = await asyncio.to_thread(
                prune_local_capture_uploads,
                retention_days=retention_days,
                max_bytes=max_bytes,
            )
        return result, max_mb, retention_days
    except Exception:
        logger.warning("screen_cleanup: capture upload cleanup failed", exc_info=True)
        return CaptureStorePruneResult(skipped_reason="prune_failed"), max_mb, retention_days


async def run_screen_cleanup() -> None:
    """Delete screen observations older than the configured retention period."""
    started_at = perf_counter()
    try:
        from src.observer.screen_repository import screen_observation_repo

        deleted = await screen_observation_repo.cleanup_old(
            settings.screen_observation_retention_days
        )
        capture_prune, capture_cap_mb, capture_retention_days = await _cleanup_capture_uploads()
        deleted_capture_files = capture_prune.deleted_files
        if deleted == 0 and deleted_capture_files == 0:
            await log_scheduler_job_event(
                job_name="screen_cleanup",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "no_expired_observations",
                    "retention_days": settings.screen_observation_retention_days,
                    "capture_prune_over_cap": capture_prune.over_cap,
                    "capture_prune_skipped_reason": capture_prune.skipped_reason,
                },
            )
            return

        await log_scheduler_job_event(
            job_name="screen_cleanup",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "deleted_count": deleted,
                "deleted_capture_files": deleted_capture_files,
                "deleted_capture_files_for_size": capture_prune.deleted_for_size,
                "capture_store_bytes": capture_prune.remaining_bytes,
                "capture_cap_mb": capture_cap_mb,
                "capture_retention_days": capture_retention_days,
                "capture_prune_over_cap": capture_prune.over_cap,
                "capture_prune_skipped_reason": capture_prune.skipped_reason,
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
