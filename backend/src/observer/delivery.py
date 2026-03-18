"""Delivery coordinator — single entry point for all proactive messages."""

import logging

from src.audit.runtime import log_observer_delivery_event
from src.models.schemas import WSResponse
from src.observer.intervention_policy import InterventionDecision, decide_intervention
from src.observer.native_notification_queue import native_notification_queue

logger = logging.getLogger(__name__)


def _transport_failure_reason(*, attempted_connections: int, failed_connections: int) -> str:
    if attempted_connections <= 0:
        return "no_active_connections"
    if failed_connections >= attempted_connections:
        return "all_connections_failed"
    return "unknown_transport_failure"


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

    ctx = context_manager.get_context()
    intervention_type = message.intervention_type or message.type
    urgency = message.urgency or 0
    policy_decision: InterventionDecision | None = None
    learning_signal = GuardianLearningSignal.neutral(intervention_type)

    try:
        try:
            learning_signal = await guardian_feedback_repository.get_learning_signal(
                intervention_type=intervention_type,
                limit=12,
            )
        except Exception:
            logger.debug("Failed to compute guardian learning signal", exc_info=True)

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
            recent_feedback_bias=learning_signal.bias,
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
            "learning_bias": learning_signal.bias,
            "learning_channel_bias": learning_signal.channel_bias,
            "learning_helpful_count": learning_signal.helpful_count,
            "learning_not_helpful_count": learning_signal.not_helpful_count,
            "learning_acknowledged_count": learning_signal.acknowledged_count,
            "learning_failed_count": learning_signal.failed_count,
            "policy_action": policy_decision.action.value,
            "policy_reason": policy_decision.reason,
        }
        intervention_id = await _create_intervention_record(
            session_id=session_id,
            message=message,
            is_scheduled=is_scheduled,
            guardian_confidence=guardian_confidence,
            user_state=ctx.user_state,
            interruption_mode=ctx.interruption_mode,
            data_quality=ctx.data_quality,
            policy_decision=policy_decision,
        )
        message.intervention_id = intervention_id
        if intervention_id is not None:
            event_details["intervention_id"] = intervention_id

        if policy_decision.action.value == "act":
            broadcast_result = await ws_manager.broadcast(message)
            event_details.update(
                {
                    "attempted_connections": broadcast_result.attempted_connections,
                    "delivered_connections": broadcast_result.delivered_connections,
                    "failed_connections": broadcast_result.failed_connections,
                }
            )
            if broadcast_result.delivered_connections <= 0:
                if (
                    context_manager.is_daemon_connected()
                    and _should_offer_native_notification(
                        message,
                        is_scheduled=is_scheduled,
                        channel_bias=learning_signal.channel_bias,
                    )
                ):
                    notification = await native_notification_queue.enqueue(
                        intervention_id=intervention_id,
                        title=_native_notification_title(message, is_scheduled=is_scheduled),
                        body=message.content,
                        intervention_type=intervention_type,
                        urgency=urgency,
                    )
                    context_manager.record_native_notification(
                        title=notification.title,
                        outcome="queued",
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
                        "Rerouted proactive message to native notification (type=%s, notification_id=%s)",
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
                    "Failed to deliver proactive message over WebSocket transport (attempted=%d failed=%d)",
                    broadcast_result.attempted_connections,
                    broadcast_result.failed_connections,
                )
                await _update_intervention_outcome(
                    intervention_id,
                    latest_outcome="failed",
                    transport="websocket",
                )
                await log_observer_delivery_event(
                    decision="failed",
                    message_type=message.type,
                    intervention_type=intervention_type,
                    urgency=urgency,
                    is_scheduled=is_scheduled,
                    details={
                        **event_details,
                        "transport": "websocket",
                        "delivery_decision": policy_decision.delivery_decision.value
                        if policy_decision.delivery_decision is not None
                        else None,
                        "error": _transport_failure_reason(
                            attempted_connections=broadcast_result.attempted_connections,
                            failed_connections=broadcast_result.failed_connections,
                        ),
                    },
                )
                return policy_decision
            # Decrement budget if this delivery costs budget
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

        elif policy_decision.action.value == "bundle":
            await insight_queue.enqueue(
                content=message.content,
                intervention_type=intervention_type,
                urgency=urgency,
                reasoning=message.reasoning or "",
                intervention_id=intervention_id,
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
    from src.scheduler.connection_manager import ws_manager

    items = await insight_queue.peek_all()
    if not items:
        return 0

    # Format as a single bundle message
    parts = []
    for item in items:
        parts.append(f"- {item.content}")
    bundle_content = f"While you were away ({len(items)} update{'s' if len(items) != 1 else ''}):\n" + "\n".join(parts)

    message = WSResponse(
        type="proactive",
        content=bundle_content,
        intervention_type="proactive_bundle",
        urgency=3,
        reasoning=f"Bundle of {len(items)} queued insight(s) delivered on state transition",
    )
    broadcast_result = await ws_manager.broadcast(message)
    details = {
        "bundle_item_count": len(items),
        "intervention_ids": [item.intervention_id for item in items if item.intervention_id],
        "attempted_connections": broadcast_result.attempted_connections,
        "delivered_connections": broadcast_result.delivered_connections,
        "failed_connections": broadcast_result.failed_connections,
        "delivery_decision": "deliver",
    }
    if broadcast_result.delivered_connections <= 0:
        logger.warning(
            "Failed to deliver queued bundle over WebSocket transport (attempted=%d failed=%d)",
            broadcast_result.attempted_connections,
            broadcast_result.failed_connections,
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
                "error": _transport_failure_reason(
                    attempted_connections=broadcast_result.attempted_connections,
                    failed_connections=broadcast_result.failed_connections,
                ),
            },
        )
        return 0

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
        details=details,
    )
    return len(items)
