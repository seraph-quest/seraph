from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

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

    values = [{"id": session_id} for session_id in sorted(normalized_ids)]
    dialect_name = getattr(getattr(db, "bind", None), "dialect", None)
    dialect_name = getattr(dialect_name, "name", "")
    if dialect_name == "sqlite":
        # Do not SELECT then INSERT: concurrent durable admissions can both
        # observe a missing redacted placeholder.  Conflict-ignore changes
        # only the synthetic row and preserves any existing Session fields.
        await db.execute(
            sqlite_insert(Session)
            .values(values)
            .on_conflict_do_nothing(index_elements=[Session.id])
        )
        return
    if dialect_name == "postgresql":
        await db.execute(
            postgres_insert(Session)
            .values(values)
            .on_conflict_do_nothing(index_elements=[Session.id])
        )
        return

    # Keep an atomic fallback for dialects without a public upsert builder.
    # Each insert gets its own savepoint so a duplicate from a concurrent
    # writer does not poison the caller's transaction.
    for value in values:
        try:
            async with db.begin_nested():
                await db.execute(insert(Session).values(value))
        except IntegrityError:
            continue
