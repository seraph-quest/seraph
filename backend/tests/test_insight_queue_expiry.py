"""Insight queue expiry — integration-level tests with time manipulation."""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio

from src.db.models import QueuedInsight
from src.observer.insight_queue import InsightQueue, EXPIRY_HOURS


@pytest_asyncio.fixture
async def queue(async_db):
    return InsightQueue()


class TestExpiryBoundary:
    async def test_item_at_exactly_expiry_boundary_excluded(self, queue, async_db):
        """An item created exactly EXPIRY_HOURS ago should be expired."""
        exactly_expired = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_HOURS)
        async with async_db() as db:
            item = QueuedInsight(
                content="Boundary item",
                created_at=exactly_expired,
            )
            db.add(item)

        assert await queue.count() == 0  # at boundary, excluded by > (not >=)

    async def test_item_just_before_expiry_included(self, queue, async_db):
        """An item created just under EXPIRY_HOURS ago should still be valid."""
        almost_expired = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_HOURS - 0.01)
        async with async_db() as db:
            item = QueuedInsight(
                content="Almost expired",
                created_at=almost_expired,
            )
            db.add(item)

        assert await queue.count() == 1

    async def test_item_well_past_expiry(self, queue, async_db):
        """An item from 48 hours ago should be expired."""
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        async with async_db() as db:
            item = QueuedInsight(content="Very old", created_at=old)
            db.add(item)

        assert await queue.count() == 0
        items = await queue.drain()
        assert len(items) == 0


class TestDrainWithMixedExpiry:
    async def test_drain_returns_only_fresh_items(self, queue, async_db):
        """Drain with mix of fresh and expired items returns only fresh."""
        old = datetime.now(timezone.utc) - timedelta(hours=25)

        async with async_db() as db:
            db.add(QueuedInsight(content="Expired 1", created_at=old, urgency=5))
            db.add(QueuedInsight(content="Expired 2", created_at=old, urgency=4))

        await queue.enqueue("Fresh 1", urgency=3)
        await queue.enqueue("Fresh 2", urgency=1)

        items = await queue.drain()
        assert len(items) == 2
        assert all("Fresh" in i.content for i in items)
        assert items[0].urgency > items[1].urgency  # ordered by urgency desc

    async def test_drain_cleans_all_including_expired(self, queue, async_db):
        """After drain, both fresh and expired rows should be gone."""
        old = datetime.now(timezone.utc) - timedelta(hours=30)
        async with async_db() as db:
            db.add(QueuedInsight(content="Ancient", created_at=old))

        await queue.enqueue("New")
        await queue.drain()

        # Queue should be completely empty
        assert await queue.count() == 0

        # Verify no rows at all (including expired)
        from sqlmodel import select
        from src.db.engine import get_session
        async with get_session() as db:
            result = await db.execute(select(QueuedInsight))
            assert len(result.scalars().all()) == 0


class TestPeekDoesNotRemoveExpired:
    async def test_peek_ignores_expired(self, queue, async_db):
        """Peek should not return expired items."""
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        async with async_db() as db:
            db.add(QueuedInsight(content="Old", created_at=old, urgency=5))

        await queue.enqueue("New", urgency=1)

        items = await queue.peek(limit=10)
        assert len(items) == 1
        assert items[0].content == "New"

    async def test_peek_preserves_items(self, queue):
        """Peek should not remove items from the queue."""
        await queue.enqueue("Keep me")
        await queue.peek()
        await queue.peek()
        assert await queue.count() == 1


class TestSequentialDrains:
    async def test_double_drain_returns_empty_second_time(self, queue):
        """Draining twice — second drain should return empty."""
        await queue.enqueue("Item A")
        await queue.enqueue("Item B")

        first = await queue.drain()
        assert len(first) == 2

        second = await queue.drain()
        assert len(second) == 0
