"""Tests for delivery coordinator — deliver_or_queue routing."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from src.models.schemas import WSResponse
from src.observer.context import CurrentContext
from src.observer.delivery import deliver_or_queue, deliver_queued_bundle
from src.observer.user_state import DeliveryDecision


def _make_context(**overrides) -> CurrentContext:
    defaults = dict(
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=5,
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


def _patch_deps(ctx):
    """Patch the lazy-imported singletons at their source modules."""
    mock_cm = MagicMock()
    mock_cm.get_context.return_value = ctx
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()
    mock_iq = MagicMock()
    mock_iq.enqueue = AsyncMock()
    mock_iq.drain = AsyncMock(return_value=[])

    patches = [
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws),
        patch("src.observer.insight_queue.insight_queue", mock_iq),
    ]
    return patches, mock_cm, mock_ws, mock_iq


@pytest.mark.asyncio
async def test_deliver_broadcasts():
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Test", intervention_type="advisory", urgency=3)
        decision = await deliver_or_queue(msg)

        assert decision == DeliveryDecision.deliver
        mock_ws.broadcast.assert_called_once_with(msg)
        mock_iq.enqueue.assert_not_called()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_deliver_decrements_budget():
    ctx = _make_context(attention_budget_remaining=3)
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Test", intervention_type="advisory", urgency=3)
        await deliver_or_queue(msg)

        mock_cm.decrement_attention_budget.assert_called_once()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_deliver_ambient_no_budget_decrement():
    ctx = _make_context(attention_budget_remaining=3)
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="ambient", content="", intervention_type="ambient", state="on_track")
        await deliver_or_queue(msg)

        mock_cm.decrement_attention_budget.assert_not_called()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_queue_when_blocked():
    ctx = _make_context(user_state="deep_work")
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Queued msg", intervention_type="advisory", urgency=3)
        decision = await deliver_or_queue(msg)

        assert decision == DeliveryDecision.queue
        mock_ws.broadcast.assert_not_called()
        mock_iq.enqueue.assert_called_once_with(
            content="Queued msg",
            intervention_type="advisory",
            urgency=3,
            reasoning="",
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_queue_when_no_budget():
    ctx = _make_context(attention_budget_remaining=0)
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Budget depleted", intervention_type="advisory", urgency=3)
        decision = await deliver_or_queue(msg)

        assert decision == DeliveryDecision.queue
        mock_ws.broadcast.assert_not_called()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_urgent_delivers_through_blocked():
    ctx = _make_context(user_state="deep_work", interruption_mode="focus", attention_budget_remaining=0)
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Urgent!", intervention_type="alert", urgency=5)
        decision = await deliver_or_queue(msg)

        assert decision == DeliveryDecision.deliver
        mock_ws.broadcast.assert_called_once()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_deliver_queued_bundle_empty():
    mock_iq = MagicMock()
    mock_iq.drain = AsyncMock(return_value=[])
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()

    with (
        patch("src.observer.insight_queue.insight_queue", mock_iq),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws),
    ):
        count = await deliver_queued_bundle()

        assert count == 0
        mock_ws.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_queued_bundle_formats_correctly():
    mock_item1 = MagicMock(content="Calendar alert: standup")
    mock_item2 = MagicMock(content="Goal reminder: exercise")

    mock_iq = MagicMock()
    mock_iq.drain = AsyncMock(return_value=[mock_item1, mock_item2])
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()

    with (
        patch("src.observer.insight_queue.insight_queue", mock_iq),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws),
    ):
        count = await deliver_queued_bundle()

        assert count == 2
        mock_ws.broadcast.assert_called_once()
        call_args = mock_ws.broadcast.call_args[0][0]
        assert call_args.type == "proactive"
        assert call_args.intervention_type == "proactive_bundle"
        assert "2 updates" in call_args.content
        assert "Calendar alert: standup" in call_args.content
        assert "Goal reminder: exercise" in call_args.content


@pytest.mark.asyncio
async def test_deliver_queued_bundle_single_item():
    mock_item = MagicMock(content="Single update")

    mock_iq = MagicMock()
    mock_iq.drain = AsyncMock(return_value=[mock_item])
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()

    with (
        patch("src.observer.insight_queue.insight_queue", mock_iq),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws),
    ):
        count = await deliver_queued_bundle()

        assert count == 1
        call_args = mock_ws.broadcast.call_args[0][0]
        assert "1 update)" in call_args.content
