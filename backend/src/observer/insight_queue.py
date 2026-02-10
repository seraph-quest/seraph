"""DB-backed queue for insights deferred by the delivery gate."""

import logging
from datetime import datetime, timezone, timedelta

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import QueuedInsight

logger = logging.getLogger(__name__)

# Insights older than this are expired and cleaned up
EXPIRY_HOURS = 24


class InsightQueue:
    """Persistent queue for proactive messages that couldn't be delivered."""

    async def enqueue(
        self,
        content: str,
        intervention_type: str = "advisory",
        urgency: int = 3,
        reasoning: str = "",
    ) -> QueuedInsight:
        """Add an insight to the queue."""
        insight = QueuedInsight(
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
        """Return all non-expired items ordered by urgency desc, then delete them + expired."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_HOURS)
        async with get_session() as db:
            # Fetch non-expired, ordered by urgency desc
            result = await db.execute(
                select(QueuedInsight)
                .where(QueuedInsight.created_at > cutoff)
                .order_by(QueuedInsight.urgency.desc())
            )
            items = list(result.scalars().all())

            # Delete all rows (both fetched and expired)
            all_result = await db.execute(select(QueuedInsight))
            for row in all_result.scalars().all():
                await db.delete(row)

        logger.info("Drained %d insight(s) from queue", len(items))
        return items

    async def count(self) -> int:
        """Count non-expired items."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_HOURS)
        async with get_session() as db:
            result = await db.execute(
                select(QueuedInsight).where(QueuedInsight.created_at > cutoff)
            )
            return len(result.scalars().all())

    async def peek(self, limit: int = 5) -> list[QueuedInsight]:
        """Preview items without removing them."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_HOURS)
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
