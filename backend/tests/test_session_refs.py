from sqlmodel import select

from src.db.models import Session
from src.db.session_refs import ensure_sessions_exist


async def test_ensure_sessions_exist_preserves_exact_session_ids(async_db):
    async with async_db() as db:
        db.add(Session(id=" s1 ", title="Whitespace session"))
        await db.flush()

    async with async_db() as db:
        await ensure_sessions_exist(db, [" s1 "])
        rows = (
            await db.execute(select(Session).order_by(Session.id))
        ).scalars().all()

    assert [row.id for row in rows] == [" s1 "]
