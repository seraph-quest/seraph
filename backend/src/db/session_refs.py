from __future__ import annotations

from collections.abc import Iterable

from sqlmodel import select

from src.db.models import Session


async def ensure_sessions_exist(db, session_ids: Iterable[str | None]) -> None:
    """Create placeholder sessions for referenced IDs when code writes session-bound rows."""
    normalized_ids = {
        session_id
        for session_id in session_ids
        if isinstance(session_id, str) and session_id.strip()
    }
    if not normalized_ids:
        return

    existing_rows = await db.execute(select(Session.id).where(Session.id.in_(normalized_ids)))
    existing_ids = {row[0] for row in existing_rows.all()}
    inserted = False
    for missing_id in normalized_ids - existing_ids:
        db.add(Session(id=missing_id))
        inserted = True
    if inserted:
        await db.flush()
