"""UTC normalization for persisted work-board timestamps exposed to clients."""

from __future__ import annotations

from datetime import datetime, timezone


def serialize_utc_datetime(value: datetime | None) -> str | None:
    """Serialize canonical UTC timestamps consistently after SQLite reloads."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")
