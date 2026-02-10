"""Calendar context source — Google Calendar via gcsa."""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)


def _fetch_events() -> dict:
    """Synchronous calendar fetch (run via asyncio.to_thread)."""
    from gcsa.google_calendar import GoogleCalendar

    credentials_path = Path(settings.google_credentials_path)
    token_path = Path(settings.google_calendar_token_path)

    if not credentials_path.exists():
        return {"upcoming_events": [], "current_event": None}

    calendar = GoogleCalendar(
        credentials_path=str(credentials_path),
        token_path=str(token_path),
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
    credentials_path = Path(settings.google_credentials_path)
    if not credentials_path.exists():
        return {"upcoming_events": [], "current_event": None}

    try:
        return await asyncio.to_thread(_fetch_events)
    except Exception:
        logger.exception("Calendar source failed")
        return {"upcoming_events": [], "current_event": None}
