"""DB-backed queue for insights deferred by the delivery gate."""

import logging
from datetime import datetime, timezone, timedelta

from sqlmodel import select

from src.db.engine import get_session
from src.conversation.identity import ConversationIdentityError
from src.db.models import Goal, QueuedInsight

logger = logging.getLogger(__name__)

# Insights older than this are expired and cleaned up
EXPIRY_HOURS = 24
PEEK_ALL_LIMIT = 100


def _cutoff_time() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=EXPIRY_HOURS)


def _is_fresh(row: QueuedInsight, cutoff: datetime) -> bool:
    ts = row.created_at
    # SQLite may strip timezone info; make comparison safe
    if ts.tzinfo is None:
        return ts > cutoff.replace(tzinfo=None)
    return ts > cutoff


async def _remove_invalid_goal_rows(db, rows: list[QueuedInsight]) -> list[QueuedInsight]:
    """Drop goal-bound queue rows whose canonical goal or owner was removed."""

    goal_ids = {
        str(row.goal_id).strip()
        for row in rows
        if isinstance(row.goal_id, str) and row.goal_id.strip()
    }
    if not goal_ids:
        return rows
    result = await db.execute(select(Goal).where(Goal.id.in_(goal_ids)))
    goals = {goal.id: goal for goal in result.scalars().all()}
    valid: list[QueuedInsight] = []
    for row in rows:
        if not row.goal_id:
            valid.append(row)
            continue
        goal = goals.get(row.goal_id)
        try:
            canonical_goal_revision = max(int(goal.revision or 1), 1) if goal is not None else None
        except (TypeError, ValueError, OverflowError):
            canonical_goal_revision = None
        if (
            goal is None
            or not row.owner_principal_id
            or not row.operator_session_id
            or goal.owner_principal_id != row.owner_principal_id
            or goal.owner_session_id != row.operator_session_id
            or row.goal_revision is None
            or isinstance(row.goal_revision, bool)
            or not isinstance(row.goal_revision, int)
            or row.goal_revision < 1
            or canonical_goal_revision != row.goal_revision
        ):
            await db.delete(row)
            continue
        valid.append(row)
    return valid


class InsightQueue:
    """Persistent queue for proactive messages that couldn't be delivered."""

    async def enqueue(
        self,
        content: str,
        intervention_type: str = "advisory",
        urgency: int = 3,
        reasoning: str = "",
        intervention_id: str | None = None,
        session_id: str | None = None,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
        goal_id: str | None = None,
        goal_revision: int | None = None,
        budget_period_key: str | None = None,
        budget_limit: int | None = None,
    ) -> QueuedInsight:
        """Add an insight to the queue."""
        goal_bound = bool(
            goal_id
            or goal_revision is not None
            or budget_period_key
            or budget_limit is not None
        )
        if goal_bound and (
            not isinstance(goal_id, str)
            or not goal_id.strip()
            or not isinstance(budget_period_key, str)
            or not budget_period_key.strip()
            or isinstance(budget_limit, bool)
            or not isinstance(budget_limit, int)
            or budget_limit < 0
            or not isinstance(owner_principal_id, str)
            or not owner_principal_id.strip()
            or not isinstance(operator_session_id, str)
            or not operator_session_id.strip()
            or (
                goal_revision is not None
                and (
                    isinstance(goal_revision, bool)
                    or not isinstance(goal_revision, int)
                    or goal_revision < 1
                )
            )
        ):
            raise ConversationIdentityError(
                "goal_owner_binding_missing",
                "Goal-bound queued insights require a canonical owner, operator session, and complete budget binding.",
            )
        insight = QueuedInsight(
            intervention_id=intervention_id,
            session_id=session_id,
            owner_principal_id=owner_principal_id,
            operator_session_id=operator_session_id,
            goal_id=goal_id,
            budget_period_key=budget_period_key,
            budget_limit=budget_limit,
            content=content,
            intervention_type=intervention_type,
            urgency=urgency,
            reasoning=reasoning,
        )
        async with get_session() as db:
            if goal_bound:
                goal_result = await db.execute(select(Goal).where(Goal.id == goal_id))
                goal = goal_result.scalar_one_or_none()
                if (
                    goal is None
                    or goal.owner_principal_id != owner_principal_id
                    or goal.owner_session_id != operator_session_id
                ):
                    raise ConversationIdentityError(
                        "goal_owner_mismatch",
                        "Goal-bound queued insight does not match the canonical goal owner.",
                    )
                canonical_goal_revision = max(int(goal.revision or 1), 1)
                if goal_revision is None:
                    goal_revision = canonical_goal_revision
                elif goal_revision != canonical_goal_revision:
                    raise ConversationIdentityError(
                        "goal_revision_mismatch",
                        "Goal-bound queued insight does not match the canonical goal revision.",
                    )
                insight.goal_revision = goal_revision
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
            all_rows = await _remove_invalid_goal_rows(db, all_rows)

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
            result = await db.execute(
                select(QueuedInsight)
                .where(QueuedInsight.created_at > cutoff)
                .order_by(QueuedInsight.urgency.desc())
                .limit(PEEK_ALL_LIMIT)
            )
            items = await _remove_invalid_goal_rows(db, list(result.scalars().all()))
            expired_result = await db.execute(select(QueuedInsight).where(QueuedInsight.created_at <= cutoff))
            expired_rows = list(expired_result.scalars().all())
            for row in expired_rows:
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
            rows = await _remove_invalid_goal_rows(db, list(result.scalars().all()))
            return len(rows)

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
            return await _remove_invalid_goal_rows(db, list(result.scalars().all()))


# Singleton
insight_queue = InsightQueue()
