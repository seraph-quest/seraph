"""Tests for delivery coordinator — deliver_or_queue routing."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from src.audit.repository import audit_repository
from src.guardian.feedback import guardian_feedback_repository
from src.models.schemas import WSResponse
from src.observer.context import CurrentContext
from src.observer.delivery import deliver_or_queue, deliver_queued_bundle
from src.observer.intervention_policy import InterventionAction
from src.observer.native_notification_queue import native_notification_queue
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
    mock_cm.is_daemon_connected.return_value = False
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=1,
        delivered_connections=1,
        failed_connections=0,
    ))
    mock_iq = MagicMock()
    mock_iq.enqueue = AsyncMock()
    mock_iq.drain = AsyncMock(return_value=[])
    mock_iq.peek_all = AsyncMock(return_value=[])
    mock_iq.delete_many = AsyncMock(return_value=0)

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
        intervention = await guardian_feedback_repository.get(msg.intervention_id)

        events = await audit_repository.list_events(limit=10)
        assert intervention is not None
        assert intervention.latest_outcome == "delivered"
        assert intervention.transport == "websocket"
        assert any(
            event["event_type"] == "observer_delivery_delivered"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["intervention_type"] == "advisory"
            and event["details"]["user_state"] == "available"
            and event["details"]["policy_action"] == "act"
            and event["details"]["policy_reason"] == "available_capacity"
            and event["details"]["delivered_connections"] == 1
            and event["details"]["intervention_id"] == msg.intervention_id
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
            intervention_id=msg.intervention_id,
            session_id=None,
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
        intervention = await guardian_feedback_repository.get(msg.intervention_id)

        events = await audit_repository.list_events(limit=10)
        assert intervention is not None
        assert intervention.latest_outcome == "queued"
        assert any(
            event["event_type"] == "observer_delivery_queued"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["user_state"] == "deep_work"
            and event["details"]["interruption_mode"] == "balanced"
            and event["details"]["policy_action"] == "bundle"
            and event["details"]["policy_reason"] == "blocked_state"
            and event["details"]["intervention_id"] == msg.intervention_id
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
async def test_queue_when_recent_negative_feedback(async_db):
    first = await guardian_feedback_repository.create_intervention(
        session_id=None,
        message_type="proactive",
        intervention_type="advisory",
        urgency=2,
        content="Stretch reminder.",
        reasoning="available_capacity",
        is_scheduled=False,
        guardian_confidence="grounded",
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        policy_action="act",
        policy_reason="available_capacity",
        delivery_decision="deliver",
        latest_outcome="delivered",
    )
    await guardian_feedback_repository.record_feedback(first.id, feedback_type="not_helpful")
    second = await guardian_feedback_repository.create_intervention(
        session_id=None,
        message_type="proactive",
        intervention_type="advisory",
        urgency=2,
        content="Another stretch reminder.",
        reasoning="available_capacity",
        is_scheduled=False,
        guardian_confidence="grounded",
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        policy_action="act",
        policy_reason="available_capacity",
        delivery_decision="deliver",
        latest_outcome="delivered",
    )
    await guardian_feedback_repository.record_feedback(second.id, feedback_type="not_helpful")

    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Try this again", intervention_type="advisory", urgency=3)
        decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.bundle
        assert decision.reason == "recent_negative_feedback"
        mock_ws.broadcast.assert_not_called()
        mock_iq.enqueue.assert_called_once()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_queued"
            and event["details"]["learning_bias"] == "reduce_interruptions"
            and event["details"]["learning_not_helpful_count"] == 2
            and event["details"]["policy_reason"] == "recent_negative_feedback"
            for event in events
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_learned_direct_delivery_overrides_high_interrupt_bundle(async_db):
    for content in ("That nudge helped.", "That timing was right again."):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content=content,
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type="helpful")

    ctx = _make_context(
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="high",
    )
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Ship this while the context is fresh.", intervention_type="advisory", urgency=2)
        decision = await deliver_or_queue(msg, guardian_confidence="grounded")

        assert decision.action == InterventionAction.act
        assert decision.reason == "learned_direct_delivery"
        mock_ws.broadcast.assert_called_once_with(msg)
        mock_iq.enqueue.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_delivered"
            and event["details"]["learning_bias"] == "prefer_direct_delivery"
            and event["details"]["policy_reason"] == "learned_direct_delivery"
            for event in events
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_acknowledged_native_feedback_can_lower_notification_threshold(async_db):
    for content in ("Acked from desktop once.", "Acked from desktop twice."):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content=content,
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type="acknowledged")

    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(0, 0, 0))
    for p in patches:
        p.start()
    try:
        await native_notification_queue.clear()
        msg = WSResponse(type="proactive", content="Medium urgency desktop hint.", intervention_type="advisory", urgency=2)
        decision = await deliver_or_queue(msg, guardian_confidence="grounded")

        assert decision.action == InterventionAction.act
        notification = await native_notification_queue.peek()
        assert notification is not None
        assert notification.body == "Medium urgency desktop hint."

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_delivered"
            and event["details"]["transport"] == "native_notification"
            and event["details"]["learning_channel_bias"] == "prefer_native_notification"
            for event in events
        )
    finally:
        await native_notification_queue.clear()
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_high_salience_calibration_delivers_despite_high_interruption_cost(async_db):
    ctx = _make_context(
        attention_budget_remaining=1,
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="high",
    )
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="This work is directly on your active goal.", intervention_type="advisory", urgency=3)
        decision = await deliver_or_queue(msg, guardian_confidence="grounded")

        assert decision.action == InterventionAction.act
        assert decision.reason == "calibrated_high_salience"
        mock_ws.broadcast.assert_called_once_with(msg)
        mock_iq.enqueue.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_delivered"
            and event["details"]["policy_action"] == "act"
            and event["details"]["policy_reason"] == "calibrated_high_salience"
            and event["details"]["salience_reason"] == "aligned_work_activity"
            for event in events
        )
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_degraded_observer_confidence_defers_without_transport_or_queue(async_db):
    ctx = _make_context(
        observer_confidence="degraded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="high",
    )
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Hold off until confidence is better.", intervention_type="advisory", urgency=3)
        decision = await deliver_or_queue(msg, guardian_confidence="grounded")

        assert decision.action == InterventionAction.defer
        assert decision.reason == "low_observer_confidence"
        mock_ws.broadcast.assert_not_called()
        mock_iq.enqueue.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_deferred"
            and event["details"]["policy_reason"] == "low_observer_confidence"
            and event["details"]["salience_reason"] == "aligned_work_activity"
            for event in events
        )
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
        intervention = await guardian_feedback_repository.get(msg.intervention_id)

        assert decision.action == InterventionAction.act
        mock_cm.decrement_attention_budget.assert_not_called()
        assert intervention is not None
        assert intervention.latest_outcome == "failed"

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
async def test_delivery_reroutes_to_native_notification_when_daemon_connected(async_db):
    await native_notification_queue.clear()
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    mock_ws.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=0,
        delivered_connections=0,
        failed_connections=0,
    ))
    for p in patches:
        p.start()
    try:
        msg = WSResponse(type="proactive", content="Native fallback", intervention_type="advisory", urgency=3)
        decision = await deliver_or_queue(msg)
        intervention = await guardian_feedback_repository.get(msg.intervention_id)

        assert decision.action == InterventionAction.act
        assert await native_notification_queue.count() == 1
        mock_iq.enqueue.assert_not_called()
        mock_cm.decrement_attention_budget.assert_called_once()
        assert intervention is not None
        assert intervention.transport == "native_notification"
        assert intervention.latest_outcome == "delivered"
        assert intervention.notification_id is not None

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_delivered"
            and event["tool_name"] == "observer_delivery_gate"
            and event["details"]["transport"] == "native_notification"
            and event["details"]["notification_id"] is not None
            and event["details"]["intervention_id"] == msg.intervention_id
            for event in events
        )
    finally:
        await native_notification_queue.clear()
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
    mock_iq.peek_all = AsyncMock(return_value=[])
    mock_iq.delete_many = AsyncMock(return_value=0)
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
    mock_item1 = MagicMock(id="one", content="Calendar alert: standup")
    mock_item2 = MagicMock(id="two", content="Goal reminder: exercise")

    mock_iq = MagicMock()
    mock_iq.peek_all = AsyncMock(return_value=[mock_item1, mock_item2])
    mock_iq.delete_many = AsyncMock(return_value=2)
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
        mock_iq.delete_many.assert_called_once_with(["one", "two"])
        call_args = mock_ws.broadcast.call_args[0][0]
        assert call_args.type == "proactive"
        assert call_args.intervention_type == "proactive_bundle"
        assert "2 updates" in call_args.content
        assert "Calendar alert: standup" in call_args.content
        assert "Goal reminder: exercise" in call_args.content


@pytest.mark.asyncio
async def test_deliver_queued_bundle_single_item():
    mock_item = MagicMock(id="one", content="Single update")

    mock_iq = MagicMock()
    mock_iq.peek_all = AsyncMock(return_value=[mock_item])
    mock_iq.delete_many = AsyncMock(return_value=1)
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
        mock_iq.delete_many.assert_called_once_with(["one"])
        call_args = mock_ws.broadcast.call_args[0][0]
        assert "1 update)" in call_args.content


@pytest.mark.asyncio
async def test_deliver_queued_bundle_logs_delivery_runtime_audit(async_db):
    mock_item = MagicMock(id="one", content="Bundle update")
    mock_item.intervention_id = None
    mock_iq = MagicMock()
    mock_iq.peek_all = AsyncMock(return_value=[mock_item])
    mock_iq.delete_many = AsyncMock(return_value=1)
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
    mock_item = MagicMock(id="one", content="Bundle update")
    mock_item.intervention_id = None
    mock_iq = MagicMock()
    mock_iq.peek_all = AsyncMock(return_value=[mock_item])
    mock_iq.delete_many = AsyncMock(return_value=0)
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
    mock_iq.delete_many.assert_not_called()
    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "observer_delivery_failed"
        and event["tool_name"] == "observer_delivery_gate"
        and event["details"]["intervention_type"] == "proactive_bundle"
        and event["details"]["bundle_item_count"] == 1
        and event["details"]["error"] == "all_connections_failed"
        and event["details"]["queue_retained"] is True
        for event in events
    )
