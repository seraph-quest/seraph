"""Tests for InsightQueue — enqueue, drain, expiry, count, peek."""

from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio

from src.db.models import QueuedInsight
from src.observer.insight_queue import InsightQueue


@pytest_asyncio.fixture
async def queue(async_db):
    return InsightQueue()


@pytest.mark.asyncio
async def test_enqueue(queue):
    item = await queue.enqueue("Test insight", "advisory", 3, "test reasoning")
    assert item.content == "Test insight"
    assert item.intervention_type == "advisory"
    assert item.urgency == 3
    assert item.reasoning == "test reasoning"
    assert item.id  # has an ID


@pytest.mark.asyncio
async def test_count_empty(queue):
    assert await queue.count() == 0


@pytest.mark.asyncio
async def test_count_after_enqueue(queue):
    await queue.enqueue("One")
    await queue.enqueue("Two")
    assert await queue.count() == 2


@pytest.mark.asyncio
async def test_drain_returns_ordered_by_urgency(queue):
    await queue.enqueue("Low", urgency=1)
    await queue.enqueue("High", urgency=5)
    await queue.enqueue("Medium", urgency=3)

    items = await queue.drain()
    assert len(items) == 3
    assert items[0].urgency == 5
    assert items[1].urgency == 3
    assert items[2].urgency == 1


@pytest.mark.asyncio
async def test_drain_empties_queue(queue):
    await queue.enqueue("Item 1")
    await queue.enqueue("Item 2")
    items = await queue.drain()
    assert len(items) == 2
    assert await queue.count() == 0


@pytest.mark.asyncio
async def test_drain_empty_queue(queue):
    items = await queue.drain()
    assert items == []


@pytest.mark.asyncio
async def test_peek_returns_items_without_removing(queue):
    await queue.enqueue("Peek me")
    items = await queue.peek(limit=5)
    assert len(items) == 1
    assert items[0].content == "Peek me"
    # Still in queue
    assert await queue.count() == 1


@pytest.mark.asyncio
async def test_peek_respects_limit(queue):
    for i in range(5):
        await queue.enqueue(f"Item {i}")
    items = await queue.peek(limit=2)
    assert len(items) == 2


@pytest.mark.asyncio
async def test_peek_ordered_by_urgency(queue):
    await queue.enqueue("Low", urgency=1)
    await queue.enqueue("High", urgency=5)
    items = await queue.peek(limit=5)
    assert items[0].urgency == 5


@pytest.mark.asyncio
async def test_expired_items_excluded_from_count(queue, async_db):
    """Manually create an old insight and verify it's not counted."""
    old_time = datetime.now(timezone.utc) - timedelta(hours=25)
    async with async_db() as db:
        old_insight = QueuedInsight(
            content="Old item",
            created_at=old_time,
        )
        db.add(old_insight)

    assert await queue.count() == 0


@pytest.mark.asyncio
async def test_expired_items_excluded_from_drain(queue, async_db):
    """Old items should not appear in drain results."""
    old_time = datetime.now(timezone.utc) - timedelta(hours=25)
    async with async_db() as db:
        old_insight = QueuedInsight(
            content="Old item",
            created_at=old_time,
        )
        db.add(old_insight)

    await queue.enqueue("Fresh item")
    items = await queue.drain()
    assert len(items) == 1
    assert items[0].content == "Fresh item"


@pytest.mark.asyncio
async def test_drain_cleans_expired(queue, async_db):
    """Drain should delete expired items from DB."""
    old_time = datetime.now(timezone.utc) - timedelta(hours=25)
    async with async_db() as db:
        old_insight = QueuedInsight(
            content="Old item",
            created_at=old_time,
        )
        db.add(old_insight)

    await queue.drain()
    # Even the expired row should be gone
    assert await queue.count() == 0
