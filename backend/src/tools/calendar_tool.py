"""Calendar integration tool — Google Calendar via gcsa."""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from smolagents import tool

from config.settings import settings

logger = logging.getLogger(__name__)


def _get_calendar():
    """Get a Google Calendar client, handling OAuth."""
    from gcsa.google_calendar import GoogleCalendar

    credentials_path = Path(settings.google_credentials_path)
    token_path = Path(settings.google_calendar_token_path)

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Google credentials not found at {credentials_path}. "
            "Please set up OAuth credentials for Google Calendar."
        )

    return GoogleCalendar(
        credentials_path=str(credentials_path),
        token_path=str(token_path),
    )


@tool
def get_calendar_events(days_ahead: int = 7) -> str:
    """Get upcoming events from your Google Calendar.

    Use this tool to check what's on the schedule, plan the day,
    or prepare for upcoming meetings.

    Args:
        days_ahead: Number of days to look ahead (default: 7).

    Returns:
        A formatted list of upcoming calendar events with times and details.
    """
    try:
        calendar = _get_calendar()
        start = datetime.now()
        end = start + timedelta(days=days_ahead)

        events = list(calendar.get_events(start, end, order_by="startTime"))

        if not events:
            return f"No events found in the next {days_ahead} days."

        lines = [f"📅 Upcoming events (next {days_ahead} days):\n"]
        for event in events:
            start_str = event.start.strftime("%a %b %d, %I:%M %p") if hasattr(event.start, 'strftime') else str(event.start)
            end_str = event.end.strftime("%I:%M %p") if hasattr(event.end, 'strftime') else str(event.end)
            line = f"- {start_str} – {end_str}: {event.summary}"
            if event.description:
                line += f"\n  Notes: {event.description[:100]}"
            if event.location:
                line += f"\n  Location: {event.location}"
            lines.append(line)

        return "\n".join(lines)

    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        logger.exception("Calendar fetch failed")
        return f"Error reading calendar: {e}"


@tool
def create_calendar_event(
    title: str,
    start_time: str,
    end_time: str,
    description: str = "",
) -> str:
    """Create a new event on your Google Calendar.

    Use this tool to schedule meetings, block focus time, set reminders,
    or plan activities on the calendar.

    Args:
        title: The event title/summary.
        start_time: Start time in ISO format (e.g., '2025-01-15T10:00:00').
        end_time: End time in ISO format (e.g., '2025-01-15T11:00:00').
        description: Optional event description or notes.

    Returns:
        Confirmation of the created event.
    """
    try:
        from gcsa.event import Event

        calendar = _get_calendar()

        start = datetime.fromisoformat(start_time)
        end = datetime.fromisoformat(end_time)

        event = Event(
            summary=title,
            start=start,
            end=end,
            description=description,
        )

        created = calendar.add_event(event)
        return (
            f"Event created: '{created.summary}'\n"
            f"When: {start.strftime('%a %b %d, %I:%M %p')} – {end.strftime('%I:%M %p')}"
        )

    except FileNotFoundError as e:
        return str(e)
    except ValueError as e:
        return f"Error: Invalid date format. Use ISO format (e.g., '2025-01-15T10:00:00'). Details: {e}"
    except Exception as e:
        logger.exception("Calendar event creation failed")
        return f"Error creating event: {e}"
