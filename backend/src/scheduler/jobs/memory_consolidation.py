import logging
from datetime import datetime, timedelta, timezone
from time import perf_counter

from sqlmodel import select, col

from src.audit.runtime import log_scheduler_job_event
from src.db.engine import get_session
from src.db.models import Session, Message
from src.memory.consolidator import consolidate_session

logger = logging.getLogger(__name__)


async def run_memory_consolidation() -> None:
    """Find sessions with recent unconsolidated messages and consolidate them.

    This is a periodic catch-all — the ad-hoc trigger in ws.py handles
    immediate post-conversation consolidation.
    """
    started_at = perf_counter()
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

        if not sessions:
            await log_scheduler_job_event(
                job_name="memory_consolidation",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "no_recent_sessions",
                },
            )
            return

        consolidated = 0
        failed = 0
        for session in sessions:
            try:
                await consolidate_session(session.id)
                consolidated += 1
            except Exception:
                failed += 1
                logger.exception("Consolidation failed for session %s", session.id[:8])

        if consolidated == 0 and failed > 0:
            await log_scheduler_job_event(
                job_name="memory_consolidation",
                outcome="failed",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "recent_session_count": len(sessions),
                    "consolidated_count": consolidated,
                    "failed_session_count": failed,
                },
            )
            return

        await log_scheduler_job_event(
            job_name="memory_consolidation",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "recent_session_count": len(sessions),
                "consolidated_count": consolidated,
                "failed_session_count": failed,
            },
        )
        if consolidated:
            logger.info("Scheduled memory consolidation: %d sessions processed", consolidated)

    except Exception as exc:
        await log_scheduler_job_event(
            job_name="memory_consolidation",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
            },
        )
        logger.exception("Scheduled memory consolidation job failed")
