"""DB-backed queue for insights deferred by the delivery gate."""

import logging
from datetime import datetime, timezone, timedelta

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import QueuedInsight

logger = logging.getLogger(__name__)

# Insights older than this are expired and cleaned up
EXPIRY_HOURS = 24


def _cutoff_time() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=EXPIRY_HOURS)


def _is_fresh(row: QueuedInsight, cutoff: datetime) -> bool:
    ts = row.created_at
    # SQLite may strip timezone info; make comparison safe
    if ts.tzinfo is None:
        return ts > cutoff.replace(tzinfo=None)
    return ts > cutoff


class InsightQueue:
    """Persistent queue for proactive messages that couldn't be delivered."""

    async def enqueue(
        self,
        content: str,
        intervention_type: str = "advisory",
        urgency: int = 3,
        reasoning: str = "",
        intervention_id: str | None = None,
    ) -> QueuedInsight:
        """Add an insight to the queue."""
        insight = QueuedInsight(
            intervention_id=intervention_id,
            content=content,
            intervention_type=intervention_type,
            urgency=urgency,
            reasoning=reasoning,
        )
        async with get_session() as db:
            db.add(insight)
        logger.info("Queued insight (type=%s, urgency=%d)", intervention_type, urgency)
        return insight

    async def drain(self) -> list[QueuedInsight]:
        """Return all non-expired items ordered by urgency desc, then delete all rows atomically."""
        cutoff = _cutoff_time()
        async with get_session() as db:
            # Single fetch of ALL rows, partition in Python, delete in same transaction
            result = await db.execute(select(QueuedInsight))
            all_rows = list(result.scalars().all())

            items = sorted(
                [r for r in all_rows if _is_fresh(r, cutoff)],
                key=lambda r: r.urgency,
                reverse=True,
            )

            for row in all_rows:
                await db.delete(row)

        logger.info("Drained %d insight(s) from queue (%d expired)", len(items), len(all_rows) - len(items))
        return items

    async def peek_all(self) -> list[QueuedInsight]:
        """Return all non-expired items ordered by urgency desc without removing them.

        Expired rows are still cleaned up opportunistically.
        """
        cutoff = _cutoff_time()
        async with get_session() as db:
            result = await db.execute(select(QueuedInsight))
            all_rows = list(result.scalars().all())
            items = sorted(
                [row for row in all_rows if _is_fresh(row, cutoff)],
                key=lambda row: row.urgency,
                reverse=True,
            )
            for row in all_rows:
                if not _is_fresh(row, cutoff):
                    await db.delete(row)
        return items

    async def delete_many(self, ids: list[str]) -> int:
        """Delete the specified queued items."""
        if not ids:
            return 0
        async with get_session() as db:
            result = await db.execute(select(QueuedInsight).where(QueuedInsight.id.in_(ids)))
            rows = list(result.scalars().all())
            for row in rows:
                await db.delete(row)
        logger.info("Deleted %d queued insight(s)", len(rows))
        return len(rows)

    async def count(self) -> int:
        """Count non-expired items."""
        cutoff = _cutoff_time()
        async with get_session() as db:
            result = await db.execute(
                select(QueuedInsight).where(QueuedInsight.created_at > cutoff)
            )
            return len(result.scalars().all())

    async def peek(self, limit: int = 5) -> list[QueuedInsight]:
        """Preview items without removing them."""
        cutoff = _cutoff_time()
        async with get_session() as db:
            result = await db.execute(
                select(QueuedInsight)
                .where(QueuedInsight.created_at > cutoff)
                .order_by(QueuedInsight.urgency.desc())
                .limit(limit)
            )
            return list(result.scalars().all())


# Singleton
insight_queue = InsightQueue()
