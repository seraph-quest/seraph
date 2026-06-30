"""Scheduled Seraph ingestion for local screenshot image folders."""

from __future__ import annotations

import logging
from time import perf_counter

from config.settings import settings
from src.audit.runtime import log_scheduler_job_event
from src.observer.screenshot_folder_source import resolve_screenshot_folder, scan_screenshot_folder

logger = logging.getLogger(__name__)


def _clamped_limit() -> int:
    try:
        limit = int(settings.screenshot_folder_ingest_limit)
    except (TypeError, ValueError):
        return 100
    return max(1, min(limit, 1000))


async def run_screenshot_folder_ingest() -> None:
    """Scan the configured screenshot folder for new image files."""
    started_at = perf_counter()
    root = resolve_screenshot_folder()
    if not settings.screenshot_folder_ingest_enabled:
        await log_scheduler_job_event(
            job_name="screenshot_folder_ingest",
            outcome="skipped",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "reason": "disabled",
                "screenshot_folder": str(root),
            },
        )
        return

    if not root.exists() or not root.is_dir():
        await log_scheduler_job_event(
            job_name="screenshot_folder_ingest",
            outcome="skipped",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "reason": "screenshot_folder_missing",
                "screenshot_folder": str(root),
            },
        )
        return

    try:
        result = await scan_screenshot_folder(root, limit=_clamped_limit())
        outcome = "succeeded"
        if result.rejected:
            outcome = "degraded"
        elif result.ingested == 0:
            outcome = "skipped"
        await log_scheduler_job_event(
            job_name="screenshot_folder_ingest",
            outcome=outcome,
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "screenshot_folder": str(root),
                "scanned": result.scanned,
                "ingested": result.ingested,
                "skipped_duplicates": result.skipped_duplicates,
                "rejected_count": len(result.rejected),
            },
        )
        logger.info(
            "screenshot_folder_ingest: scanned=%d ingested=%d duplicates=%d rejected=%d",
            result.scanned,
            result.ingested,
            result.skipped_duplicates,
            len(result.rejected),
        )
    except Exception as exc:
        await log_scheduler_job_event(
            job_name="screenshot_folder_ingest",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "screenshot_folder": str(root),
                "error": str(exc),
            },
        )
        logger.exception("screenshot_folder_ingest failed")
