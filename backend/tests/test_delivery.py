"""Tests for delivery coordinator — deliver_or_queue routing."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from src.audit.repository import audit_repository
from src.models.schemas import WSResponse
from src.observer.context import CurrentContext
from src.observer.delivery import deliver_or_queue, deliver_queued_bundle
from src.observer.intervention_policy import InterventionAction
from src.scheduler.connection_manager import BroadcastResult


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
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=1,
        delivered_connections=1,
        failed_connections=0,
    ))
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

        assert decision.action == InterventionAction.act
        assert decision.delivery_decision is not None
        assert decision.delivery_decision.value == "deliver"
        mock_ws.broadcast.assert_called_once_with(msg)
        mock_iq.enqueue.assert_not_called()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_deliver_logs_runtime_audit(async_db):
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Test", intervention_type="advisory", urgency=3)
        await deliver_or_queue(msg)

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_delivered"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["intervention_type"] == "advisory"
            and event["details"]["user_state"] == "available"
            and event["details"]["policy_action"] == "act"
            and event["details"]["policy_reason"] == "available_capacity"
            and event["details"]["delivered_connections"] == 1
            for event in events
        )
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
        decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.act
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

        assert decision.action == InterventionAction.bundle
        assert decision.delivery_decision is not None
        assert decision.delivery_decision.value == "queue"
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
async def test_queue_logs_runtime_audit(async_db):
    ctx = _make_context(user_state="deep_work")
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Queued msg", intervention_type="advisory", urgency=3)
        await deliver_or_queue(msg)

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_queued"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["user_state"] == "deep_work"
            and event["details"]["interruption_mode"] == "balanced"
            and event["details"]["policy_action"] == "bundle"
            and event["details"]["policy_reason"] == "blocked_state"
            for event in events
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

        assert decision.action == InterventionAction.bundle
        assert decision.reason == "attention_budget_exhausted"
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

        assert decision.action == InterventionAction.act
        assert decision.reason == "urgent"
        mock_ws.broadcast.assert_called_once()
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_delivery_failure_logs_runtime_audit(async_db):
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_ws.broadcast.side_effect = RuntimeError("socket down")
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Test", intervention_type="advisory", urgency=3)
        with pytest.raises(RuntimeError, match="socket down"):
            await deliver_or_queue(msg)

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_failed"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["delivery_decision"] == "deliver"
            and event["details"]["error"] == "socket down"
            for event in events
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_delivery_transport_failure_logs_runtime_audit(async_db):
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=1,
        delivered_connections=0,
        failed_connections=1,
    ))
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Test", intervention_type="advisory", urgency=3)
        decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.act
        mock_cm.decrement_attention_budget.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_failed"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["delivery_decision"] == "deliver"
            and event["details"]["error"] == "all_connections_failed"
            and event["details"]["attempted_connections"] == 1
            and event["details"]["delivered_connections"] == 0
            for event in events
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_delivery_audit_fails_open():
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Test", intervention_type="advisory", urgency=3)
        with patch("src.audit.runtime.audit_repository.log_event", new_callable=AsyncMock, side_effect=RuntimeError("audit down")):
            decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.act
        mock_ws.broadcast.assert_called_once_with(msg)
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_defer_when_winding_down(async_db):
    ctx = _make_context(user_state="winding_down")
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Wrap up the inbox.", intervention_type="advisory", urgency=3)
        decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.defer
        assert decision.reason == "winding_down_quiet_hours"
        mock_ws.broadcast.assert_not_called()
        mock_iq.enqueue.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_deferred"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["policy_action"] == "defer"
            and event["details"]["policy_reason"] == "winding_down_quiet_hours"
            for event in events
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_stay_silent_for_empty_proactive_message(async_db):
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="", intervention_type="advisory", urgency=2)
        decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.stay_silent
        assert decision.reason == "empty_content"
        mock_ws.broadcast.assert_not_called()
        mock_iq.enqueue.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_silenced"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["policy_action"] == "stay_silent"
            for event in events
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_request_approval_policy_action_is_logged(async_db):
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(
            type="proactive",
            content="Open a high-risk action suggestion.",
            intervention_type="alert",
            urgency=4,
            reasoning="Needs confirmation",
            requires_approval=True,
        )
        decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.request_approval
        assert decision.reason == "requires_approval"
        mock_ws.broadcast.assert_not_called()
        mock_iq.enqueue.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_approval_requested"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["policy_action"] == "request_approval"
            for event in events
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_deliver_queued_bundle_empty():
    mock_iq = MagicMock()
    mock_iq.drain = AsyncMock(return_value=[])
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=1,
        delivered_connections=1,
        failed_connections=0,
    ))

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
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=1,
        delivered_connections=1,
        failed_connections=0,
    ))

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
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=1,
        delivered_connections=1,
        failed_connections=0,
    ))

    with (
        patch("src.observer.insight_queue.insight_queue", mock_iq),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws),
    ):
        count = await deliver_queued_bundle()

        assert count == 1
        call_args = mock_ws.broadcast.call_args[0][0]
        assert "1 update)" in call_args.content


@pytest.mark.asyncio
async def test_deliver_queued_bundle_logs_delivery_runtime_audit(async_db):
    mock_item = MagicMock(content="Bundle update")
    mock_iq = MagicMock()
    mock_iq.drain = AsyncMock(return_value=[mock_item])
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=2,
        delivered_connections=2,
        failed_connections=0,
    ))

    with (
        patch("src.observer.insight_queue.insight_queue", mock_iq),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws),
    ):
        count = await deliver_queued_bundle()

    assert count == 1
    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "observer_delivery_delivered"
        and event["tool_name"] == "observer_delivery_gate"
        and event["details"]["intervention_type"] == "proactive_bundle"
        and event["details"]["bundle_item_count"] == 1
        and event["details"]["delivered_connections"] == 2
        for event in events
    )


@pytest.mark.asyncio
async def test_deliver_queued_bundle_logs_transport_failure(async_db):
    mock_item = MagicMock(content="Bundle update")
    mock_iq = MagicMock()
    mock_iq.drain = AsyncMock(return_value=[mock_item])
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=1,
        delivered_connections=0,
        failed_connections=1,
    ))

    with (
        patch("src.observer.insight_queue.insight_queue", mock_iq),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws),
    ):
        count = await deliver_queued_bundle()

    assert count == 0
    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "observer_delivery_failed"
        and event["tool_name"] == "observer_delivery_gate"
        and event["details"]["intervention_type"] == "proactive_bundle"
        and event["details"]["bundle_item_count"] == 1
        and event["details"]["error"] == "all_connections_failed"
        for event in events
    )
