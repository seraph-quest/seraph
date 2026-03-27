"""Delivery coordinator — single entry point for all proactive messages."""

import logging

from src.audit.runtime import log_observer_delivery_event
from src.models.schemas import WSResponse
from src.observer.intervention_policy import InterventionDecision, decide_intervention
from src.observer.native_notification_queue import native_notification_queue

logger = logging.getLogger(__name__)

_BUILTIN_CHANNEL_TRANSPORTS = {"websocket", "native_notification"}


def _transport_failure_reason(
    *,
    attempted_connections: int,
    failed_connections: int,
    websocket_enabled: bool = True,
) -> str:
    if not websocket_enabled:
        return "websocket_adapter_disabled"
    if attempted_connections <= 0:
        return "no_active_connections"
    if failed_connections >= attempted_connections:
        return "all_connections_failed"
    return "unknown_transport_failure"


def _prefer_delivery_error(current_error: str | None, next_error: str) -> str:
    if not current_error or current_error in {"no_active_route_transport", "unknown_transport_failure"}:
        return next_error
    if next_error in {"daemon_unavailable", "no_active_route_transport"}:
        return current_error
    return next_error


def _route_disabled_error(*, primary_transport: str | None) -> str:
    if primary_transport == "websocket":
        return "websocket_adapter_disabled"
    return "no_active_route_transport"


def _active_channel_adapters() -> set[str]:
    from config.settings import settings
    from src.extensions.channels import select_active_channel_adapters
    from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
    from src.extensions.state import connector_enabled_overrides, load_extension_state_payload

    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    contributions = snapshot.list_contributions("channel_adapters")
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    active_adapters = select_active_channel_adapters(
        contributions,
        enabled_overrides=connector_enabled_overrides(state_by_id),
    )
    active_transports = {item.transport for item in active_adapters}
    return active_transports | (_BUILTIN_CHANNEL_TRANSPORTS - active_transports)


def _delivery_route_name(*, message: WSResponse, is_scheduled: bool) -> str:
    if is_scheduled:
        return "scheduled_delivery"
    if message.intervention_type == "alert":
        return "alert_delivery"
    return "live_delivery"


def _route_transport_order(route_name: str, *, active_channel_adapters: set[str]) -> tuple[dict[str, str | None], list[str]]:
    from src.extensions.channel_routing import ordered_route_transports
    from src.extensions.state import load_extension_state_payload

    state_payload = load_extension_state_payload()
    binding, ordered = ordered_route_transports(
        state_payload,
        route=route_name,
        active_transports=active_channel_adapters,
    )
    return {
        "route": binding.route,
        "primary_transport": binding.primary_transport,
        "fallback_transport": binding.fallback_transport,
    }, ordered


def _apply_native_channel_preference(
    *,
    transport_order: list[str],
    message: WSResponse,
    is_scheduled: bool,
    channel_bias: str,
) -> list[str]:
    if (
        channel_bias != "prefer_native_notification"
        or "native_notification" not in transport_order
        or not _should_offer_native_notification(
            message,
            is_scheduled=is_scheduled,
            channel_bias=channel_bias,
        )
    ):
        return transport_order
    if transport_order[0] == "native_notification":
        return transport_order
    return ["native_notification", *[transport for transport in transport_order if transport != "native_notification"]]


def _should_offer_native_notification(
    message: WSResponse,
    *,
    is_scheduled: bool,
    channel_bias: str = "neutral",
) -> bool:
    if message.type != "proactive":
        return False
    if bool(message.requires_approval):
        return False
    if not message.content.strip():
        return False
    if channel_bias == "prefer_native_notification":
        return True
    if is_scheduled:
        return True
    if message.intervention_type == "alert":
        return True
    return (message.urgency or 0) >= 3


def _native_notification_title(message: WSResponse, *, is_scheduled: bool) -> str:
    if message.intervention_type == "alert":
        return "Seraph alert"
    if is_scheduled:
        return "Seraph update"
    return "Seraph"


async def _create_intervention_record(
    *,
    session_id: str | None,
    message: WSResponse,
    is_scheduled: bool,
    guardian_confidence: str | None,
    user_state: str,
    active_project: str | None,
    interruption_mode: str,
    data_quality: str,
    policy_decision: InterventionDecision,
) -> str | None:
    from src.guardian.feedback import guardian_feedback_repository

    initial_outcome = "pending" if policy_decision.action.value == "act" else policy_decision.audit_decision
    try:
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=session_id,
            message_type=message.type,
            intervention_type=message.intervention_type,
            urgency=message.urgency,
            content=message.content,
            reasoning=message.reasoning,
            is_scheduled=is_scheduled,
            guardian_confidence=guardian_confidence,
            data_quality=data_quality,
            user_state=user_state,
            active_project=active_project,
            interruption_mode=interruption_mode,
            policy_action=policy_decision.action.value,
            policy_reason=policy_decision.reason,
            delivery_decision=(
                policy_decision.delivery_decision.value
                if policy_decision.delivery_decision is not None
                else None
            ),
            latest_outcome=initial_outcome,
        )
        return intervention.id
    except Exception:
        logger.debug("Failed to persist guardian intervention record", exc_info=True)
        return None


async def _update_intervention_outcome(
    intervention_id: str | None,
    *,
    latest_outcome: str,
    transport: str | None = None,
    notification_id: str | None = None,
) -> None:
    if not intervention_id:
        return
    from src.guardian.feedback import guardian_feedback_repository

    try:
        await guardian_feedback_repository.update_outcome(
            intervention_id,
            latest_outcome=latest_outcome,
            transport=transport,
            notification_id=notification_id,
        )
    except Exception:
        logger.debug("Failed to update guardian intervention outcome", exc_info=True)


async def deliver_or_queue(
    message: WSResponse,
    is_scheduled: bool = False,
    *,
    guardian_confidence: str | None = None,
    session_id: str | None = None,
) -> InterventionDecision:
    """Route a proactive message through the delivery gate.

    Reads current context, makes an explicit intervention-policy decision,
    and executes the resulting delivery/bundle/silence path.
    """
    from src.observer.manager import context_manager
    from src.observer.insight_queue import insight_queue
    from src.scheduler.connection_manager import ws_manager
    from src.guardian.feedback import GuardianLearningSignal, guardian_feedback_repository
    from src.guardian.learning_arbitration import arbitrate_learning_signal
    from src.memory.procedural_guidance import load_procedural_memory_guidance

    ctx = context_manager.get_context()
    active_channel_adapters = _active_channel_adapters()
    intervention_type = message.intervention_type or message.type
    urgency = message.urgency or 0
    policy_decision: InterventionDecision | None = None
    learning_signal = GuardianLearningSignal.neutral(intervention_type)
    effective_learning_signal = learning_signal
    learning_signal_source = "heuristic_only"
    procedural_lesson_types: tuple[str, ...] = ()
    learning_arbitration_sources: dict[str, str] = {}
    learning_arbitration_reasons: dict[str, str] = {}
    learning_arbitration_weights: dict[str, float] = {}

    try:
        try:
            learning_signal = await guardian_feedback_repository.get_learning_signal(
                intervention_type=intervention_type,
                limit=12,
            )
        except Exception:
            logger.debug("Failed to compute guardian learning signal", exc_info=True)
        try:
            procedural_guidance = await load_procedural_memory_guidance(intervention_type)
            arbitration = arbitrate_learning_signal(
                live_signal=learning_signal,
                procedural_guidance=procedural_guidance,
            )
            effective_learning_signal = arbitration.effective_signal
            learning_signal_source = arbitration.source_label
            procedural_lesson_types = procedural_guidance.lesson_types
            learning_arbitration_sources = arbitration.selected_sources()
            learning_arbitration_reasons = arbitration.selected_reasons()
            learning_arbitration_weights = arbitration.selected_weights()
        except Exception:
            logger.debug("Failed to load procedural learning guidance", exc_info=True)
            effective_learning_signal = learning_signal

        policy_decision = decide_intervention(
            message_type=message.type,
            intervention_type=intervention_type,
            content=message.content,
            urgency=urgency,
            user_state=ctx.user_state,
            interruption_mode=ctx.interruption_mode,
            attention_budget_remaining=ctx.attention_budget_remaining,
            is_scheduled=is_scheduled,
            data_quality=ctx.data_quality,
            guardian_confidence=guardian_confidence,
            observer_confidence=ctx.observer_confidence,
            salience_level=ctx.salience_level,
            salience_reason=ctx.salience_reason,
            interruption_cost=ctx.interruption_cost,
            requires_approval=bool(message.requires_approval),
            recent_feedback_bias=effective_learning_signal.bias,
            learning_phrasing_bias=effective_learning_signal.phrasing_bias,
            learning_cadence_bias=effective_learning_signal.cadence_bias,
            learning_channel_bias=effective_learning_signal.channel_bias,
            learning_escalation_bias=effective_learning_signal.escalation_bias,
            learning_timing_bias=effective_learning_signal.timing_bias,
            learning_blocked_state_bias=effective_learning_signal.blocked_state_bias,
            learning_suppression_bias=effective_learning_signal.suppression_bias,
            learning_thread_preference_bias=effective_learning_signal.thread_preference_bias,
        )

        event_details = {
            "user_state": ctx.user_state,
            "interruption_mode": ctx.interruption_mode,
            "attention_budget_remaining": ctx.attention_budget_remaining,
            "data_quality": ctx.data_quality,
            "observer_confidence": ctx.observer_confidence,
            "salience_level": ctx.salience_level,
            "salience_reason": ctx.salience_reason,
            "interruption_cost": ctx.interruption_cost,
            "guardian_confidence": guardian_confidence,
            "learning_signal_source": learning_signal_source,
            "learning_bias": effective_learning_signal.bias,
            "learning_phrasing_bias": effective_learning_signal.phrasing_bias,
            "learning_cadence_bias": effective_learning_signal.cadence_bias,
            "learning_channel_bias": effective_learning_signal.channel_bias,
            "learning_escalation_bias": effective_learning_signal.escalation_bias,
            "learning_timing_bias": effective_learning_signal.timing_bias,
            "learning_blocked_state_bias": effective_learning_signal.blocked_state_bias,
            "learning_suppression_bias": effective_learning_signal.suppression_bias,
            "learning_thread_preference_bias": effective_learning_signal.thread_preference_bias,
            "learning_helpful_count": learning_signal.helpful_count,
            "learning_not_helpful_count": learning_signal.not_helpful_count,
            "learning_acknowledged_count": learning_signal.acknowledged_count,
            "learning_failed_count": learning_signal.failed_count,
            "learning_arbitration_mode": "evidence_weighted",
            "learning_arbitration_sources": learning_arbitration_sources,
            "learning_arbitration_reasons": learning_arbitration_reasons,
            "learning_arbitration_weights": learning_arbitration_weights,
            "policy_action": policy_decision.action.value,
            "policy_reason": policy_decision.reason,
        }
        intervention_id = await _create_intervention_record(
            session_id=session_id,
            message=message,
            is_scheduled=is_scheduled,
            guardian_confidence=guardian_confidence,
            user_state=ctx.user_state,
            active_project=ctx.active_project,
            interruption_mode=ctx.interruption_mode,
            data_quality=ctx.data_quality,
            policy_decision=policy_decision,
        )
        message.intervention_id = intervention_id
        if intervention_id is not None:
            event_details["intervention_id"] = intervention_id
        if procedural_lesson_types:
            event_details["procedural_learning_lesson_types"] = list(procedural_lesson_types)

        if policy_decision.action.value == "act":
            route_name = _delivery_route_name(message=message, is_scheduled=is_scheduled)
            route_binding, transport_order = _route_transport_order(
                route_name,
                active_channel_adapters=active_channel_adapters,
            )
            adjusted_transport_order = _apply_native_channel_preference(
                transport_order=transport_order,
                message=message,
                is_scheduled=is_scheduled,
                channel_bias=effective_learning_signal.channel_bias,
            )
            event_details.update(
                {
                    "active_channel_adapters": sorted(active_channel_adapters),
                    "channel_route": route_binding["route"],
                    "primary_transport": route_binding["primary_transport"],
                    "fallback_transport": route_binding["fallback_transport"],
                    "transport_order": adjusted_transport_order,
                }
            )
            if adjusted_transport_order != transport_order:
                event_details["transport_order_adjustment"] = "learned_native_channel_preference"
            transport_order = adjusted_transport_order

            last_error = _route_disabled_error(primary_transport=route_binding["primary_transport"])
            for transport in transport_order:
                if transport == "websocket":
                    websocket_enabled = "websocket" in active_channel_adapters
                    if websocket_enabled:
                        broadcast_result = await ws_manager.broadcast(message)
                    else:
                        from src.scheduler.connection_manager import BroadcastResult

                        broadcast_result = BroadcastResult(
                            attempted_connections=0,
                            delivered_connections=0,
                            failed_connections=0,
                        )
                    event_details.update(
                        {
                            "attempted_connections": broadcast_result.attempted_connections,
                            "delivered_connections": broadcast_result.delivered_connections,
                            "failed_connections": broadcast_result.failed_connections,
                        }
                    )
                    if broadcast_result.delivered_connections > 0:
                        if policy_decision.should_cost_budget:
                            context_manager.decrement_attention_budget()
                        logger.info("Delivered proactive message (type=%s)", message.type)
                        await _update_intervention_outcome(
                            intervention_id,
                            latest_outcome="delivered",
                            transport="websocket",
                        )
                        await log_observer_delivery_event(
                            decision="delivered",
                            message_type=message.type,
                            intervention_type=intervention_type,
                            urgency=urgency,
                            is_scheduled=is_scheduled,
                            details={**event_details, "transport": "websocket"},
                        )
                        return policy_decision
                    last_error = _prefer_delivery_error(
                        last_error,
                        _transport_failure_reason(
                        attempted_connections=broadcast_result.attempted_connections,
                        failed_connections=broadcast_result.failed_connections,
                        websocket_enabled=websocket_enabled,
                        ),
                    )
                    continue

                if transport == "native_notification":
                    if not context_manager.is_daemon_connected():
                        last_error = _prefer_delivery_error(last_error, "daemon_unavailable")
                        continue
                    notification = await native_notification_queue.enqueue(
                        intervention_id=intervention_id,
                        title=_native_notification_title(message, is_scheduled=is_scheduled),
                        body=message.content,
                        intervention_type=intervention_type,
                        urgency=urgency,
                        surface=(
                            "action_card"
                            if effective_learning_signal.escalation_bias == "prefer_async_native"
                            else "notification"
                        ),
                        session_id=session_id,
                        thread_id=session_id,
                        thread_source="session" if session_id else "ambient",
                        continuation_mode="resume_thread" if session_id else "open_thread",
                        resume_message=f"Continue from this guardian intervention: {message.content}",
                    )
                    context_manager.record_native_notification(
                        title=notification.title,
                        outcome="queued",
                    )
                    event_details.update(
                        {
                            "attempted_connections": 1,
                            "delivered_connections": 1,
                            "failed_connections": 0,
                        }
                    )
                    if policy_decision.should_cost_budget:
                        context_manager.decrement_attention_budget()
                    await _update_intervention_outcome(
                        intervention_id,
                        latest_outcome="delivered",
                        transport="native_notification",
                        notification_id=notification.id,
                    )
                    logger.info(
                        "Delivered proactive message over native notification (type=%s, notification_id=%s)",
                        message.type,
                        notification.id,
                    )
                    await log_observer_delivery_event(
                        decision="delivered",
                        message_type=message.type,
                        intervention_type=intervention_type,
                        urgency=urgency,
                        is_scheduled=is_scheduled,
                        details={
                            **event_details,
                            "transport": "native_notification",
                            "notification_id": notification.id,
                            "delivery_decision": policy_decision.delivery_decision.value
                            if policy_decision.delivery_decision is not None
                            else None,
                        },
                    )
                    return policy_decision

            logger.warning(
                "Failed to deliver proactive message (type=%s, route=%s, error=%s)",
                message.type,
                route_name,
                last_error,
            )
            await _update_intervention_outcome(
                intervention_id,
                latest_outcome="failed",
                transport=transport_order[0] if transport_order else None,
            )
            await log_observer_delivery_event(
                decision="failed",
                message_type=message.type,
                intervention_type=intervention_type,
                urgency=urgency,
                is_scheduled=is_scheduled,
                details={
                    **event_details,
                    "transport": transport_order[0] if transport_order else None,
                    "delivery_decision": policy_decision.delivery_decision.value
                    if policy_decision.delivery_decision is not None
                    else None,
                    "error": last_error,
                },
            )
            return policy_decision

        elif policy_decision.action.value == "bundle":
            await insight_queue.enqueue(
                content=message.content,
                intervention_type=intervention_type,
                urgency=urgency,
                reasoning=message.reasoning or "",
                intervention_id=intervention_id,
                session_id=session_id,
            )
            logger.info("Queued proactive message (state=%s, mode=%s)", ctx.user_state, ctx.interruption_mode)
            await log_observer_delivery_event(
                decision="queued",
                message_type=message.type,
                intervention_type=intervention_type,
                urgency=urgency,
                is_scheduled=is_scheduled,
                details=event_details,
            )

        else:
            logger.info(
                "Suppressed proactive message (type=%s, action=%s, reason=%s)",
                message.type,
                policy_decision.action.value,
                policy_decision.reason,
            )
            await log_observer_delivery_event(
                decision=policy_decision.audit_decision,
                message_type=message.type,
                intervention_type=intervention_type,
                urgency=urgency,
                is_scheduled=is_scheduled,
                details=event_details,
            )

        return policy_decision
    except Exception as exc:
        await _update_intervention_outcome(
            locals().get("intervention_id"),
            latest_outcome="failed",
        )
        await log_observer_delivery_event(
            decision="failed",
            message_type=message.type,
            intervention_type=intervention_type,
            urgency=urgency,
            is_scheduled=is_scheduled,
            details={
                "user_state": ctx.user_state,
                "interruption_mode": ctx.interruption_mode,
                "attention_budget_remaining": ctx.attention_budget_remaining,
                "data_quality": ctx.data_quality,
                "guardian_confidence": guardian_confidence,
                "policy_action": policy_decision.action.value if policy_decision is not None else None,
                "policy_reason": policy_decision.reason if policy_decision is not None else None,
                "delivery_decision": (
                    policy_decision.delivery_decision.value
                    if policy_decision is not None and policy_decision.delivery_decision is not None
                    else None
                ),
                "error": str(exc),
            },
        )
        raise


async def deliver_queued_bundle() -> int:
    """Drain the insight queue and deliver as a bundle message.

    Called on state transitions from blocked → unblocked.
    Returns the number of items delivered.
    """
    from src.observer.insight_queue import insight_queue
    from src.observer.manager import context_manager
    from src.scheduler.connection_manager import ws_manager

    items = await insight_queue.peek_all()
    if not items:
        return 0
    active_channel_adapters = _active_channel_adapters()
    route_binding, transport_order = _route_transport_order(
        "bundle_delivery",
        active_channel_adapters=active_channel_adapters,
    )
    details = {
        "bundle_item_count": len(items),
        "intervention_ids": [item.intervention_id for item in items if item.intervention_id],
        "delivery_decision": "deliver",
        "active_channel_adapters": sorted(active_channel_adapters),
        "channel_route": route_binding["route"],
        "primary_transport": route_binding["primary_transport"],
        "fallback_transport": route_binding["fallback_transport"],
        "transport_order": transport_order,
    }
    parts = [f"- {item.content}" for item in items]
    bundle_content = f"While you were away ({len(items)} update{'s' if len(items) != 1 else ''}):\n" + "\n".join(parts)
    message = WSResponse(
        type="proactive",
        content=bundle_content,
        intervention_type="proactive_bundle",
        urgency=3,
        reasoning=f"Bundle of {len(items)} queued insight(s) delivered on state transition",
    )

    last_error = _route_disabled_error(primary_transport=route_binding["primary_transport"])
    for transport in transport_order:
        if transport == "native_notification":
            if not context_manager.is_daemon_connected():
                last_error = _prefer_delivery_error(last_error, "daemon_unavailable")
                continue
            notification = await native_notification_queue.enqueue(
                intervention_id=None,
                title="Seraph update",
                body=bundle_content,
                intervention_type="proactive_bundle",
                urgency=3,
                surface="action_card",
                session_id=None,
                thread_id=None,
                thread_source="ambient",
                continuation_mode="open_thread",
                resume_message="Review the queued guardian updates.",
            )
            await insight_queue.delete_many(
                [item.id for item in items if getattr(item, "id", None)]
            )
            for item in items:
                await _update_intervention_outcome(
                    item.intervention_id,
                    latest_outcome="bundle_delivered",
                    transport="native_notification_bundle",
                    notification_id=notification.id,
                )
            details.update(
                {
                    "attempted_connections": 1,
                    "delivered_connections": 1,
                    "failed_connections": 0,
                }
            )
            await log_observer_delivery_event(
                decision="delivered",
                message_type="proactive",
                intervention_type="proactive_bundle",
                urgency=3,
                is_scheduled=False,
                details={
                    **details,
                    "transport": "native_notification",
                    "notification_id": notification.id,
                },
            )
            return len(items)

        if transport == "websocket":
            broadcast_result = await ws_manager.broadcast(message)
            details.update(
                {
                    "attempted_connections": broadcast_result.attempted_connections,
                    "delivered_connections": broadcast_result.delivered_connections,
                    "failed_connections": broadcast_result.failed_connections,
                }
            )
            if broadcast_result.delivered_connections > 0:
                await insight_queue.delete_many(
                    [item.id for item in items if getattr(item, "id", None)]
                )
                for item in items:
                    await _update_intervention_outcome(
                        item.intervention_id,
                        latest_outcome="bundle_delivered",
                        transport="websocket_bundle",
                    )
                logger.info("Delivered bundle of %d queued insight(s)", len(items))
                await log_observer_delivery_event(
                    decision="delivered",
                    message_type=message.type,
                    intervention_type=message.intervention_type,
                    urgency=message.urgency,
                    is_scheduled=False,
                    details={**details, "transport": "websocket"},
                )
                return len(items)
            last_error = _prefer_delivery_error(
                last_error,
                _transport_failure_reason(
                attempted_connections=broadcast_result.attempted_connections,
                failed_connections=broadcast_result.failed_connections,
                websocket_enabled=True,
                ),
            )

    await log_observer_delivery_event(
        decision="failed",
        message_type=message.type,
        intervention_type=message.intervention_type,
        urgency=message.urgency,
        is_scheduled=False,
        details={
            **details,
            "queue_retained": True,
            "transport": transport_order[0] if transport_order else None,
            "error": last_error,
        },
    )
    return 0
