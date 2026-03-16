"""Time context source — pure computation, no I/O."""

from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import settings
from src.audit.runtime import log_integration_event_sync


def gather_time() -> dict:
    """Return time-of-day classification, day of week, and working hours flag."""
    try:
        tz = ZoneInfo(settings.user_timezone)
        now = datetime.now(tz)
        hour = now.hour

        if 5 <= hour < 12:
            time_of_day = "morning"
        elif 12 <= hour < 17:
            time_of_day = "afternoon"
        elif 17 <= hour < 21:
            time_of_day = "evening"
        else:
            time_of_day = "night"

        day_of_week = now.strftime("%A")
        is_weekday = now.weekday() < 5
        is_working_hours = is_weekday and settings.working_hours_start <= hour < settings.working_hours_end

        log_integration_event_sync(
            integration_type="observer_source",
            name="time",
            outcome="succeeded",
            details={
                "timezone": settings.user_timezone,
                "time_of_day": time_of_day,
                "is_working_hours": is_working_hours,
            },
        )
        return {
            "time_of_day": time_of_day,
            "day_of_week": day_of_week,
            "is_working_hours": is_working_hours,
        }
    except Exception as exc:
        log_integration_event_sync(
            integration_type="observer_source",
            name="time",
            outcome="failed",
            details={
                "timezone": settings.user_timezone,
                "error": str(exc),
            },
        )
        raise
