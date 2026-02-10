import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import select, col

from src.db.engine import get_session
from src.db.models import Session, Message
from src.memory.consolidator import consolidate_session

logger = logging.getLogger(__name__)


async def run_memory_consolidation() -> None:
    """Find sessions with recent unconsolidated messages and consolidate them.

    This is a periodic catch-all — the ad-hoc trigger in ws.py handles
    immediate post-conversation consolidation.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

        async with get_session() as db:
            result = await db.execute(
                select(Session)
                .where(col(Session.updated_at) >= cutoff)
                .order_by(col(Session.updated_at).desc())
                .limit(10)
            )
            sessions = result.scalars().all()

        consolidated = 0
        for session in sessions:
            try:
                await consolidate_session(session.id)
                consolidated += 1
            except Exception:
                logger.exception("Consolidation failed for session %s", session.id[:8])

        if consolidated:
            logger.info("Scheduled memory consolidation: %d sessions processed", consolidated)

    except Exception:
        logger.exception("Scheduled memory consolidation job failed")
