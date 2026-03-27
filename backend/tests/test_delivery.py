"""Tests for delivery coordinator — deliver_or_queue routing."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from config.settings import settings
from src.extensions.state import save_extension_state_payload
from src.audit.repository import audit_repository
from src.guardian.feedback import GuardianLearningSignal, guardian_feedback_repository
from src.guardian.learning_evidence import (
    GuardianLearningAxisEvidence,
    learning_field_for_axis,
    neutral_axis_evidence,
    ordered_learning_axes,
)
from src.memory.procedural import sync_learning_signal_memories
from src.memory.procedural_guidance import ProceduralMemoryGuidance
from src.models.schemas import WSResponse
from src.observer.context import CurrentContext
from src.observer.delivery import _active_channel_adapters, deliver_or_queue, deliver_queued_bundle
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


def _patch_deps(ctx, *, use_actual_learning_signal: bool = False):
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
    if not use_actual_learning_signal:
        patches.append(
            patch(
                "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
                AsyncMock(side_effect=lambda intervention_type, limit=12: GuardianLearningSignal.neutral(intervention_type)),
            )
        )
    return patches, mock_cm, mock_ws, mock_iq


def _axis_evidence_tuple(
    axis: str,
    *,
    source: str,
    bias: str,
    support_count: int,
    recency_score: float,
    confidence_score: float,
    quality_score: float,
    metadata_complete: bool = True,
) -> tuple[GuardianLearningAxisEvidence, ...]:
    evidence_by_axis = {
        axis: GuardianLearningAxisEvidence(
            axis=axis,
            field_name=learning_field_for_axis(axis),
            source=source,
            bias=bias,
            support_count=support_count,
            recency_score=recency_score,
            confidence_score=confidence_score,
            quality_score=quality_score,
            metadata_complete=metadata_complete,
        )
    }
    return tuple(
        evidence_by_axis.get(item_axis, neutral_axis_evidence(item_axis, source=source))
        for item_axis in ordered_learning_axes()
    )


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
async def test_native_channel_adapter_can_deliver_without_websocket():
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    for p in patches:
        p.start()
    try:
        await native_notification_queue.clear()
        msg = WSResponse(type="proactive", content="Native path", intervention_type="alert", urgency=4)
        with patch("src.observer.delivery._active_channel_adapters", return_value={"native_notification"}):
            decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.act
        mock_ws.broadcast.assert_not_called()
        notification = await native_notification_queue.peek()
        assert notification is not None
        assert notification.body == "Native path"
    finally:
        await native_notification_queue.clear()
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_channel_routing_can_prefer_native_notification_for_live_delivery(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    original_workspace_dir = settings.workspace_dir
    settings.workspace_dir = str(workspace_dir)
    save_extension_state_payload(
        {
            "extensions": {},
            "channel_routing": {
                "bindings": {
                    "live_delivery": {
                        "primary_transport": "native_notification",
                        "fallback_transport": "websocket",
                    }
                }
            },
        }
    )
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    for p in patches:
        p.start()
    try:
        await native_notification_queue.clear()
        msg = WSResponse(type="proactive", content="Route to native", intervention_type="advisory", urgency=2)
        with patch("src.observer.delivery._active_channel_adapters", return_value={"websocket", "native_notification"}):
            decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.act
        mock_ws.broadcast.assert_not_called()
        notification = await native_notification_queue.peek()
        assert notification is not None
        assert notification.body == "Route to native"
    finally:
        await native_notification_queue.clear()
        settings.workspace_dir = original_workspace_dir
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_native_channel_adapter_can_deliver_queued_bundle_without_websocket():
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    mock_iq.peek_all = AsyncMock(
        return_value=[
            MagicMock(id="queued-1", content="Queued update", intervention_id="intervention-1"),
        ]
    )
    for p in patches:
        p.start()
    try:
        await native_notification_queue.clear()
        with patch("src.observer.delivery._active_channel_adapters", return_value={"native_notification"}):
            delivered = await deliver_queued_bundle()

        assert delivered == 1
        mock_ws.broadcast.assert_not_called()
        mock_iq.delete_many.assert_called_once_with(["queued-1"])
        notification = await native_notification_queue.peek()
        assert notification is not None
        assert "Queued update" in notification.body
    finally:
        await native_notification_queue.clear()
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_channel_routing_can_prefer_native_notification_for_bundle_delivery(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    original_workspace_dir = settings.workspace_dir
    settings.workspace_dir = str(workspace_dir)
    save_extension_state_payload(
        {
            "extensions": {},
            "channel_routing": {
                "bindings": {
                    "bundle_delivery": {
                        "primary_transport": "native_notification",
                        "fallback_transport": "websocket",
                    }
                }
            },
        }
    )
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    mock_iq.peek_all = AsyncMock(
        return_value=[
            MagicMock(id="queued-1", content="Queued update", intervention_id="intervention-1"),
        ]
    )
    for p in patches:
        p.start()
    try:
        await native_notification_queue.clear()
        with patch("src.observer.delivery._active_channel_adapters", return_value={"websocket", "native_notification"}):
            delivered = await deliver_queued_bundle()

        assert delivered == 1
        mock_ws.broadcast.assert_not_called()
        notification = await native_notification_queue.peek()
        assert notification is not None
        assert "Queued update" in notification.body
    finally:
        await native_notification_queue.clear()
        settings.workspace_dir = original_workspace_dir
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_channel_routing_can_prefer_websocket_for_scheduled_delivery(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    original_workspace_dir = settings.workspace_dir
    settings.workspace_dir = str(workspace_dir)
    save_extension_state_payload(
        {
            "extensions": {},
            "channel_routing": {
                "bindings": {
                    "scheduled_delivery": {
                        "primary_transport": "websocket",
                        "fallback_transport": "native_notification",
                    }
                }
            },
        }
    )
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    for p in patches:
        p.start()
    try:
        await native_notification_queue.clear()
        msg = WSResponse(type="proactive", content="Scheduled route", intervention_type="advisory", urgency=2)
        with patch("src.observer.delivery._active_channel_adapters", return_value={"websocket", "native_notification"}):
            decision = await deliver_or_queue(msg, is_scheduled=True)

        assert decision.action == InterventionAction.act
        mock_ws.broadcast.assert_called_once_with(msg)
        assert await native_notification_queue.count() == 0
    finally:
        await native_notification_queue.clear()
        settings.workspace_dir = original_workspace_dir
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_channel_routing_can_prefer_native_notification_for_alert_delivery(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    original_workspace_dir = settings.workspace_dir
    settings.workspace_dir = str(workspace_dir)
    save_extension_state_payload(
        {
            "extensions": {},
            "channel_routing": {
                "bindings": {
                    "alert_delivery": {
                        "primary_transport": "native_notification",
                        "fallback_transport": "websocket",
                    }
                }
            },
        }
    )
    ctx = _make_context()
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    for p in patches:
        p.start()
    try:
        await native_notification_queue.clear()
        msg = WSResponse(type="proactive", content="Alert route", intervention_type="alert", urgency=5)
        with patch("src.observer.delivery._active_channel_adapters", return_value={"websocket", "native_notification"}):
            decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.act
        mock_ws.broadcast.assert_not_called()
        notification = await native_notification_queue.peek()
        assert notification is not None
        assert notification.body == "Alert route"
    finally:
        await native_notification_queue.clear()
        settings.workspace_dir = original_workspace_dir
        for p in patches:
            p.stop()


def test_active_channel_adapters_fall_back_to_builtin_transports_when_none_are_active(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    original_workspace_dir = settings.workspace_dir
    settings.workspace_dir = str(workspace_dir)
    try:
        save_extension_state_payload(
            {
                "extensions": {
                    "seraph.core-channel-adapters": {
                        "connector_state": {
                            "channels/websocket.yaml": {"enabled": False},
                            "channels/native-notification.yaml": {"enabled": False},
                        }
                    }
                }
            }
        )
        assert _active_channel_adapters() == {"websocket", "native_notification"}
    finally:
        settings.workspace_dir = original_workspace_dir


def test_active_channel_adapters_keep_builtin_transport_for_unclaimed_route(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    original_workspace_dir = settings.workspace_dir
    settings.workspace_dir = str(workspace_dir)
    try:
        save_extension_state_payload({"extensions": {}})
        with (
            patch(
                "src.extensions.channels.select_active_channel_adapters",
                return_value=[
                    type("Adapter", (), {"transport": "native_notification"})(),
                ],
            ),
            patch(
                "src.extensions.registry.ExtensionRegistry",
                return_value=type(
                    "Registry",
                    (),
                    {
                        "snapshot": lambda self: type(
                            "Snapshot",
                            (),
                            {"list_contributions": lambda _self, _kind: [object()]},
                        )()
                    },
                )(),
            ),
        ):
            assert _active_channel_adapters() == {"websocket", "native_notification"}
    finally:
        settings.workspace_dir = original_workspace_dir


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
async def test_deliver_uses_procedural_memory_guidance_when_heuristic_signal_is_neutral(async_db):
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=1,
            not_helpful_count=0,
            acknowledged_count=2,
            failed_count=0,
            bias="neutral",
            phrasing_bias="neutral",
            cadence_bias="neutral",
            channel_bias="prefer_native_notification",
            escalation_bias="neutral",
            timing_bias="neutral",
            blocked_state_bias="prefer_async_for_blocked_state",
            suppression_bias="neutral",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=0,
            blocked_native_success_count=2,
            available_direct_success_count=0,
        ),
    )

    ctx = _make_context(user_state="deep_work")
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    for p in patches:
        p.start()
    try:
        await native_notification_queue.clear()
        msg = WSResponse(
            type="proactive",
            content="Keep this as an async reminder.",
            intervention_type="advisory",
            urgency=3,
        )
        with patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ), patch(
            "src.observer.delivery._active_channel_adapters",
            return_value={"native_notification"},
        ):
            decision = await deliver_or_queue(msg)

        intervention = await guardian_feedback_repository.get(msg.intervention_id)

        assert decision.action == InterventionAction.act
        assert decision.reason == "learned_blocked_state_async"
        assert mock_ws.broadcast.call_count == 0
        assert intervention is not None
        assert intervention.policy_reason == "learned_blocked_state_async"
        notification = await native_notification_queue.peek()
        assert notification is not None
        assert notification.body == "Keep this as an async reminder."

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_delivered"
            and event["details"]["learning_signal_source"] == "heuristic_plus_procedural_memory"
            and event["details"]["learning_channel_bias"] == "prefer_native_notification"
            and event["details"]["learning_blocked_state_bias"] == "prefer_async_for_blocked_state"
            and event["details"]["policy_reason"] == "learned_blocked_state_async"
            and event["details"]["procedural_learning_lesson_types"] == ["channel", "blocked_state"]
            and event["details"]["attempted_connections"] == 1
            and event["details"]["delivered_connections"] == 1
            and event["details"]["failed_connections"] == 0
            for event in events
        )
    finally:
        await native_notification_queue.clear()
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_deliver_prefers_native_transport_when_procedural_memory_promotes_async_delivery(async_db):
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=1,
            not_helpful_count=0,
            acknowledged_count=2,
            failed_count=0,
            bias="neutral",
            phrasing_bias="neutral",
            cadence_bias="neutral",
            channel_bias="prefer_native_notification",
            escalation_bias="prefer_async_native",
            timing_bias="neutral",
            blocked_state_bias="prefer_async_for_blocked_state",
            suppression_bias="neutral",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=0,
            blocked_native_success_count=2,
            available_direct_success_count=0,
        ),
    )

    ctx = _make_context(user_state="deep_work")
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx)
    mock_cm.is_daemon_connected.return_value = True
    mock_ws.broadcast = AsyncMock(
        return_value=BroadcastResult(
            attempted_connections=1,
            delivered_connections=1,
            failed_connections=0,
        )
    )
    for p in patches:
        p.start()
    try:
        await native_notification_queue.clear()
        msg = WSResponse(
            type="proactive",
            content="Keep this native even when the browser is connected.",
            intervention_type="advisory",
            urgency=3,
        )
        with patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ), patch(
            "src.observer.delivery._active_channel_adapters",
            return_value={"websocket", "native_notification"},
        ):
            decision = await deliver_or_queue(msg)

        intervention = await guardian_feedback_repository.get(msg.intervention_id)

        assert decision.action == InterventionAction.act
        assert decision.reason == "learned_async_native_delivery"
        mock_ws.broadcast.assert_not_called()
        notification = await native_notification_queue.peek()
        assert notification is not None
        assert notification.body == "Keep this native even when the browser is connected."
        assert intervention is not None
        assert intervention.transport == "native_notification"

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_delivered"
            and event["details"]["transport"] == "native_notification"
            and event["details"]["learning_signal_source"] == "heuristic_plus_procedural_memory"
            and event["details"]["transport_order"] == ["native_notification", "websocket"]
            and event["details"]["transport_order_adjustment"] == "learned_native_channel_preference"
            and event["details"]["attempted_connections"] == 1
            and event["details"]["delivered_connections"] == 1
            and event["details"]["failed_connections"] == 0
            for event in events
        )
    finally:
        await native_notification_queue.clear()
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_deliver_uses_live_signal_when_conflicting_procedural_memory_is_stale(async_db):
    live_signal = GuardianLearningSignal(
        intervention_type="advisory",
        helpful_count=0,
        not_helpful_count=3,
        acknowledged_count=0,
        failed_count=1,
        bias="reduce_interruptions",
        phrasing_bias="neutral",
        cadence_bias="neutral",
        channel_bias="neutral",
        escalation_bias="neutral",
        timing_bias="neutral",
        blocked_state_bias="neutral",
        suppression_bias="extend_suppression",
        thread_preference_bias="neutral",
        blocked_direct_failure_count=0,
        blocked_native_success_count=0,
        available_direct_success_count=0,
        axis_evidence=_axis_evidence_tuple(
            "delivery",
            source="live_signal",
            bias="reduce_interruptions",
            support_count=4,
            recency_score=0.95,
            confidence_score=1.0,
            quality_score=1.0,
        ),
    )
    procedural_guidance = ProceduralMemoryGuidance(
        intervention_type="advisory",
        bias="prefer_direct_delivery",
        lesson_types=("delivery",),
        axis_evidence=_axis_evidence_tuple(
            "delivery",
            source="procedural_memory",
            bias="prefer_direct_delivery",
            support_count=1,
            recency_score=0.0,
            confidence_score=0.63,
            quality_score=0.4,
        ),
    )

    ctx = _make_context(user_state="available")
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx, use_actual_learning_signal=True)
    for p in patches:
        p.start()
    try:
        msg = WSResponse(
            type="proactive",
            content="This should still queue because the live signal is stronger.",
            intervention_type="advisory",
            urgency=2,
        )
        with patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=live_signal),
        ), patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=procedural_guidance),
        ):
            decision = await deliver_or_queue(msg)

        assert decision.action == InterventionAction.bundle
        assert decision.reason == "recent_negative_feedback"
        mock_ws.broadcast.assert_not_called()
        mock_iq.enqueue.assert_called_once()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "observer_delivery_queued"
            and event["details"]["learning_signal_source"] == "heuristic_plus_procedural_memory"
            and event["details"]["learning_bias"] == "reduce_interruptions"
            and event["details"]["learning_arbitration_mode"] == "evidence_weighted"
            and event["details"]["learning_arbitration_sources"]["delivery"] == "live_signal"
            and event["details"]["learning_arbitration_reasons"]["delivery"] == "live_signal_stronger"
            for event in events
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
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx, use_actual_learning_signal=True)
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
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx, use_actual_learning_signal=True)
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
    patches, mock_cm, mock_ws, mock_iq = _patch_deps(ctx, use_actual_learning_signal=True)
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
            and event["details"]["delivered_connections"] == 1
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
            and event["details"]["attempted_connections"] == 1
            and event["details"]["delivered_connections"] == 1
            and event["details"]["failed_connections"] == 0
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


@pytest.mark.asyncio
async def test_deliver_queued_bundle_with_no_active_channel_adapters_retains_queue(async_db):
    mock_item = MagicMock(id="one", content="Bundle update")
    mock_item.intervention_id = None
    mock_iq = MagicMock()
    mock_iq.peek_all = AsyncMock(return_value=[mock_item])
    mock_iq.delete_many = AsyncMock(return_value=0)
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()

    with (
        patch("src.observer.insight_queue.insight_queue", mock_iq),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws),
        patch("src.observer.delivery._active_channel_adapters", return_value=set()),
    ):
        count = await deliver_queued_bundle()

    assert count == 0
    mock_ws.broadcast.assert_not_called()
    mock_iq.delete_many.assert_not_called()
    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "observer_delivery_failed"
        and event["tool_name"] == "observer_delivery_gate"
        and event["details"]["intervention_type"] == "proactive_bundle"
        and event["details"]["bundle_item_count"] == 1
        and event["details"]["error"] == "websocket_adapter_disabled"
        and event["details"]["queue_retained"] is True
        and event["details"]["active_channel_adapters"] == []
        for event in events
    )
