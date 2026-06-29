"""Scheduled Seraph ingestion for Framekeeper screenshot image folders."""

from __future__ import annotations

import logging
from time import perf_counter

from config.settings import settings
from src.audit.runtime import log_scheduler_job_event
from src.observer.framekeeper_source import ingest_framekeeper_root, resolve_framekeeper_root

logger = logging.getLogger(__name__)


def _clamped_limit() -> int:
    try:
        limit = int(settings.framekeeper_ingest_limit)
    except (TypeError, ValueError):
        return 100
    return max(1, min(limit, 1000))


async def run_framekeeper_image_ingest() -> None:
    """Scan the configured Framekeeper screenshot folder for new image files."""
    started_at = perf_counter()
    root = resolve_framekeeper_root()
    if not settings.framekeeper_ingest_enabled:
        await log_scheduler_job_event(
            job_name="framekeeper_image_ingest",
            outcome="skipped",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "reason": "disabled",
                "artifact_root": str(root),
            },
        )
        return

    if not root.exists() or not root.is_dir():
        await log_scheduler_job_event(
            job_name="framekeeper_image_ingest",
            outcome="skipped",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "reason": "artifact_root_missing",
                "artifact_root": str(root),
            },
        )
        return

    try:
        result = await ingest_framekeeper_root(root, limit=_clamped_limit())
        outcome = "succeeded"
        if result.rejected:
            outcome = "degraded"
        elif result.ingested == 0:
            outcome = "skipped"
        await log_scheduler_job_event(
            job_name="framekeeper_image_ingest",
            outcome=outcome,
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "artifact_root": str(root),
                "scanned": result.scanned,
                "ingested": result.ingested,
                "skipped_duplicates": result.skipped_duplicates,
                "rejected_count": len(result.rejected),
            },
        )
        logger.info(
            "framekeeper_image_ingest: scanned=%d ingested=%d duplicates=%d rejected=%d",
            result.scanned,
            result.ingested,
            result.skipped_duplicates,
            len(result.rejected),
        )
    except Exception as exc:
        await log_scheduler_job_event(
            job_name="framekeeper_image_ingest",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "artifact_root": str(root),
                "error": str(exc),
            },
        )
        logger.exception("framekeeper_image_ingest failed")
