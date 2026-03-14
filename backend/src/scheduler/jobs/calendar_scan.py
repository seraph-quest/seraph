import logging
from datetime import datetime, timedelta
from time import perf_counter

from src.audit.runtime import log_scheduler_job_event
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)


async def run_calendar_scan() -> None:
    """Scan upcoming calendar events and alert user about imminent ones."""
    logger.info("calendar_scan started")
    started_at = perf_counter()

    try:
        from src.observer.manager import context_manager
        ctx = await context_manager.refresh()

        if not ctx.upcoming_events:
            logger.debug("calendar_scan: no upcoming events")
            await log_scheduler_job_event(
                job_name="calendar_scan",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "no_upcoming_events",
                },
            )
            return

        now = datetime.now()
        alerts = []

        for event in ctx.upcoming_events:
            start_str = event.get("start", "")
            try:
                start = datetime.fromisoformat(start_str)
            except (ValueError, TypeError):
                continue

            delta = start - now
            if timedelta(0) < delta <= timedelta(minutes=15):
                alerts.append(event.get("summary", "Upcoming event"))

        if not alerts:
            await log_scheduler_job_event(
                job_name="calendar_scan",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "no_imminent_events",
                    "upcoming_event_count": len(ctx.upcoming_events),
                },
            )
            return

        from src.observer.delivery import deliver_or_queue

        event_list = ", ".join(alerts)
        content = f"Heads up! Starting soon: {event_list}"

        result = await deliver_or_queue(
            WSResponse(
                type="proactive",
                content=content,
                intervention_type="alert",
                urgency=4,
                reasoning="Calendar event starting within 15 minutes",
            )
        )
        await log_scheduler_job_event(
            job_name="calendar_scan",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "alert_count": len(alerts),
                "delivery": result.value,
            },
        )
        logger.info("calendar_scan: alerted for %d event(s)", len(alerts))
    except Exception as exc:
        await log_scheduler_job_event(
            job_name="calendar_scan",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
            },
        )
        logger.exception("calendar_scan failed")
