"""Scheduled Seraph analysis for already-ingested screenshot-folder images."""

from __future__ import annotations

import asyncio
import logging
import math
from time import perf_counter

from config.settings import settings
from src.audit.runtime import log_scheduler_job_event
from src.observer.screenshot_folder_source import analyze_pending_screenshot_folder_observations
from src.observer.screenshot_semantic_analysis import screenshot_semantic_analysis_accepting_background_work

logger = logging.getLogger(__name__)
_ANALYSIS_LOCK = asyncio.Lock()
_MAX_SCHEDULED_ANALYSIS_LIMIT = 100
_MAX_SCHEDULED_ANALYSIS_CONCURRENCY = 1


def _clamped_limit() -> int:
    try:
        limit = int(settings.screenshot_folder_analysis_limit)
    except (TypeError, ValueError):
        limit = _MAX_SCHEDULED_ANALYSIS_LIMIT
    return max(1, min(limit, _MAX_SCHEDULED_ANALYSIS_LIMIT))


def _clamped_concurrency() -> int:
    try:
        concurrency = int(settings.screenshot_folder_analysis_concurrency)
    except (TypeError, ValueError):
        concurrency = _MAX_SCHEDULED_ANALYSIS_CONCURRENCY
    return max(1, min(concurrency, _MAX_SCHEDULED_ANALYSIS_CONCURRENCY))


def _analysis_job_timeout_seconds(*, limit: int, concurrency: int) -> float:
    try:
        base_timeout_seconds = float(settings.screenshot_folder_analysis_job_timeout_seconds)
    except (TypeError, ValueError):
        base_timeout_seconds = 30.0
    base_timeout_seconds = max(base_timeout_seconds, 1.0)
    batches = max(1, math.ceil(max(limit, 1) / max(concurrency, 1)))
    return base_timeout_seconds * batches


async def run_screenshot_folder_analysis() -> None:
    """Analyze pending local screenshot-folder observations."""
    started_at = perf_counter()
    logger.info("screenshot_folder_analysis: starting")
    if _ANALYSIS_LOCK.locked():
        logger.info("screenshot_folder_analysis: skipped; previous analysis still running")
        return
    if not settings.screenshot_folder_ingest_enabled:
        logger.info("screenshot_folder_analysis: skipped; screenshot folder ingest disabled")
        await log_scheduler_job_event(
            job_name="screenshot_folder_analysis",
            outcome="skipped",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "reason": "disabled",
            },
        )
        return
    if not await screenshot_semantic_analysis_accepting_background_work():
        logger.info("screenshot_folder_analysis: skipped; local VLM has no background capacity")
        await log_scheduler_job_event(
            job_name="screenshot_folder_analysis",
            outcome="skipped",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "reason": "local_vlm_no_background_capacity",
            },
        )
        return

    try:
        async with _ANALYSIS_LOCK:
            limit = _clamped_limit()
            concurrency = _clamped_concurrency()
            timeout_seconds = _analysis_job_timeout_seconds(limit=limit, concurrency=concurrency)
            logger.info(
                "screenshot_folder_analysis: running limit=%d concurrency=%d timeout_seconds=%s",
                limit,
                concurrency,
                timeout_seconds,
            )
            result = await asyncio.wait_for(
                analyze_pending_screenshot_folder_observations(
                    limit=limit,
                    concurrency=concurrency,
                ),
                timeout=timeout_seconds,
            )
    except asyncio.TimeoutError:
        await log_scheduler_job_event(
            job_name="screenshot_folder_analysis",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": "analysis job timed out",
                "timeout_seconds": timeout_seconds,
                "concurrency": _clamped_concurrency(),
            },
        )
        logger.warning(
            "screenshot_folder_analysis timed out after %ss",
            timeout_seconds,
        )
        return
    except Exception as exc:
        await log_scheduler_job_event(
            job_name="screenshot_folder_analysis",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
            },
        )
        logger.exception("screenshot_folder_analysis failed")
        return

    outcome = "succeeded"
    if result.failed:
        outcome = "degraded"
    elif result.analyzed == 0:
        outcome = "skipped"
    logger.info(
        "screenshot_folder_analysis: completed outcome=%s scanned=%d analyzed=%d failed=%d skipped=%d duration_ms=%d",
        outcome,
        result.scanned,
        result.analyzed,
        result.failed,
        result.skipped,
        int((perf_counter() - started_at) * 1000),
    )
    await log_scheduler_job_event(
        job_name="screenshot_folder_analysis",
        outcome=outcome,
        details={
            "duration_ms": int((perf_counter() - started_at) * 1000),
            "scanned": result.scanned,
            "analyzed": result.analyzed,
            "failed": result.failed,
            "skipped": result.skipped,
            "concurrency": _clamped_concurrency(),
        },
    )
    logger.info(
        "screenshot_folder_analysis: scanned=%d analyzed=%d failed=%d skipped=%d",
        result.scanned,
        result.analyzed,
        result.failed,
        result.skipped,
    )
