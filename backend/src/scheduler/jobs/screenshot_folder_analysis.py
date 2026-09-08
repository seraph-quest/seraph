"""Scheduled Seraph analysis for already-ingested screenshot-folder images."""

from __future__ import annotations

import asyncio
import logging
import math
from time import perf_counter

from config.settings import settings
from src.audit.runtime import log_scheduler_job_event
from src.observer.screenshot_folder_source import (
    ScreenshotFolderAnalysisResult,
    analyze_pending_screenshot_folder_observations,
)
from src.observer.screenshot_semantic_analysis import screenshot_semantic_analysis_background_slots

logger = logging.getLogger(__name__)
_ANALYSIS_LOCK = asyncio.Lock()
_MAX_SCHEDULED_ANALYSIS_LIMIT = 100
_MAX_SCHEDULED_ANALYSIS_CONCURRENCY = 2


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


def _scheduled_batch_limit(*, limit: int, concurrency: int, available_slots: int) -> int:
    return max(0, min(limit, concurrency, max(available_slots, 0)))


def _log_late_analysis_task_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("screenshot_folder_analysis timed-out task failed during cancellation cleanup")


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
    timeout_seconds = 0.0
    try:
        async with _ANALYSIS_LOCK:
            limit = _clamped_limit()
            concurrency = _clamped_concurrency()
            remaining = limit
            result = ScreenshotFolderAnalysisResult(scanned=0, analyzed=0, failed=0, skipped=0)
            feeder_iterations = 0
            last_batch_limit = 0
            stopped_reason = "limit_reached"
            while remaining > 0:
                available_slots = await screenshot_semantic_analysis_background_slots()
                if available_slots <= 0:
                    stopped_reason = "remote_inference_no_background_capacity"
                    break
                batch_limit = _scheduled_batch_limit(
                    limit=remaining,
                    concurrency=concurrency,
                    available_slots=available_slots,
                )
                batch_concurrency = min(concurrency, batch_limit)
                timeout_seconds = _analysis_job_timeout_seconds(limit=batch_limit, concurrency=batch_concurrency)
                logger.info(
                    (
                        "screenshot_folder_analysis: running limit=%d remaining=%d batch_limit=%d "
                        "concurrency=%d available_slots=%d timeout_seconds=%s"
                    ),
                    limit,
                    remaining,
                    batch_limit,
                    batch_concurrency,
                    available_slots,
                    timeout_seconds,
                )
                analysis_task = asyncio.create_task(
                    analyze_pending_screenshot_folder_observations(
                        limit=batch_limit,
                        concurrency=batch_concurrency,
                    )
                )
                done, pending = await asyncio.wait({analysis_task}, timeout=timeout_seconds)
                if pending:
                    analysis_task.cancel()
                    analysis_task.add_done_callback(_log_late_analysis_task_failure)
                    raise asyncio.TimeoutError
                batch_result = next(iter(done)).result()
                feeder_iterations += 1
                last_batch_limit = batch_limit
                result = ScreenshotFolderAnalysisResult(
                    scanned=result.scanned + batch_result.scanned,
                    analyzed=result.analyzed + batch_result.analyzed,
                    failed=result.failed + batch_result.failed,
                    skipped=result.skipped + batch_result.skipped,
                )
                remaining -= batch_limit
                if batch_result.scanned == 0:
                    stopped_reason = "backlog_empty"
                    break
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
            "batch_limit": last_batch_limit,
            "feeder_iterations": feeder_iterations,
            "stopped_reason": stopped_reason,
        },
    )
    logger.info(
        "screenshot_folder_analysis: scanned=%d analyzed=%d failed=%d skipped=%d",
        result.scanned,
        result.analyzed,
        result.failed,
        result.skipped,
    )
