import logging
from datetime import datetime, timedelta

from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)


async def run_calendar_scan() -> None:
    """Scan upcoming calendar events and alert user about imminent ones."""
    logger.info("calendar_scan started")

    try:
        from src.observer.manager import context_manager
        ctx = await context_manager.refresh()
    except Exception:
        logger.exception("calendar_scan: context refresh failed")
        return

    if not ctx.upcoming_events:
        logger.debug("calendar_scan: no upcoming events")
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
        return

    from src.observer.delivery import deliver_or_queue

    event_list = ", ".join(alerts)
    content = f"Heads up! Starting soon: {event_list}"

    await deliver_or_queue(
        WSResponse(
            type="proactive",
            content=content,
            intervention_type="alert",
            urgency=4,
            reasoning="Calendar event starting within 15 minutes",
        )
    )
    logger.info("calendar_scan: alerted for %d event(s)", len(alerts))
