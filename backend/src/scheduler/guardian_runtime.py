"""Visible outcome of a scheduled guardian call on the Cordis loop (#1020).

A guardian domain that is not ready blocks the call with no fallback. The
block is persisted in the background-inference status (Settings), written to
Activity at most hourly while it lasts, and announced to the operator once
per transition into blocked. The next successful run clears it.
"""
from __future__ import annotations

import logging
from time import perf_counter

from src.runtime_plugins.guardian_turns import GuardianBlocked, guardian_completion

logger = logging.getLogger(__name__)
__all__ = ["GuardianBlocked", "guardian_completion", "report_guardian_blocked", "report_guardian_succeeded"]


async def report_guardian_blocked(job_id: str, exc: GuardianBlocked, *, started_at: float | None = None,
                                  details: dict | None = None) -> None:
    from src.audit.runtime import log_scheduler_job_event
    from src.scheduler.background_inference import _JOBS, record_background_inference_blocked
    state = record_background_inference_blocked(job_id, exc.reason_code, exc.recovery)
    logger.warning("%s blocked by the guardian runtime: %s", job_id, exc.reason_code)
    if state["activity_due"]:
        await log_scheduler_job_event(job_name=job_id, outcome="blocked", details={
            **(details or {}), "reason": exc.reason_code, "recovery": exc.recovery,
            **({"duration_ms": int((perf_counter() - started_at) * 1000)} if started_at is not None else {})})
    if state["transition"]:
        try:
            from src.models.schemas import WSResponse
            from src.observer.delivery import deliver_or_queue
            label = _JOBS[job_id].label if job_id in _JOBS else job_id
            await deliver_or_queue(WSResponse(type="proactive", content=f"{label} is blocked: {exc.recovery}",
                                              intervention_type="advisory", urgency=3,
                                              reasoning=f"Guardian background job blocked ({exc.reason_code})"),
                                   is_scheduled=True)
        except Exception:
            # The persisted status and Activity entry remain the operator-visible record.
            logger.warning("Could not announce the %s block", job_id, exc_info=True)


def report_guardian_succeeded(job_id: str) -> None:
    from src.scheduler.background_inference import record_background_inference_succeeded
    record_background_inference_succeeded(job_id)
