"""Calendar context source — Google Calendar via gcsa.

Note: Calendar tools were removed in favour of MCP servers. This observer
source remains for proactive context gathering. If Google credentials are
not present, it silently returns empty data.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from src.audit.runtime import log_integration_event

logger = logging.getLogger(__name__)

# Default credential paths (match Docker mount layout)
_CREDENTIALS_PATH = Path("/app/config/google_credentials.json")
_CALENDAR_TOKEN_PATH = Path("/app/data/google_calendar_token.json")


def _fetch_events() -> dict:
    """Synchronous calendar fetch (run via asyncio.to_thread)."""
    from gcsa.google_calendar import GoogleCalendar

    if not _CREDENTIALS_PATH.exists():
        return {"upcoming_events": [], "current_event": None}

    calendar = GoogleCalendar(
        credentials_path=str(_CREDENTIALS_PATH),
        token_path=str(_CALENDAR_TOKEN_PATH),
    )

    now = datetime.now()
    end = now + timedelta(hours=24)
    events = list(calendar.get_events(now, end, order_by="startTime"))

    upcoming = []
    current_event = None

    for event in events[:3]:
        event_dict = {
            "summary": event.summary or "Untitled",
            "start": str(event.start),
            "end": str(event.end),
        }
        if event.description:
            event_dict["description"] = event.description[:100]
        upcoming.append(event_dict)

        # Check if event is happening now
        if hasattr(event.start, "hour"):
            if event.start <= now <= event.end:
                current_event = event.summary

    return {"upcoming_events": upcoming, "current_event": current_event}


async def gather_calendar() -> dict:
    """Async wrapper — returns empty dict if credentials missing or error."""
    if not _CREDENTIALS_PATH.exists():
        await log_integration_event(
            integration_type="observer_source",
            name="calendar",
            outcome="unavailable",
            details={"reason": "missing_credentials"},
        )
        return {"upcoming_events": [], "current_event": None}

    try:
        result = await asyncio.to_thread(_fetch_events)
        upcoming_events = result.get("upcoming_events", [])
        current_event = result.get("current_event")
        if not upcoming_events and not current_event:
            await log_integration_event(
                integration_type="observer_source",
                name="calendar",
                outcome="empty_result",
                details={
                    "upcoming_event_count": 0,
                    "has_current_event": False,
                },
            )
        else:
            await log_integration_event(
                integration_type="observer_source",
                name="calendar",
                outcome="succeeded",
                details={
                    "upcoming_event_count": len(upcoming_events),
                    "has_current_event": bool(current_event),
                },
            )
        return result
    except Exception as exc:
        await log_integration_event(
            integration_type="observer_source",
            name="calendar",
            outcome="failed",
            details={"error": str(exc)},
        )
        logger.exception("Calendar source failed")
        return {"upcoming_events": [], "current_event": None}
