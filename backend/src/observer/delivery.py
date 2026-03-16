"""Delivery coordinator — single entry point for all proactive messages."""

import logging

from src.audit.runtime import log_observer_delivery_event
from src.models.schemas import WSResponse
from src.observer.user_state import DeliveryDecision, user_state_machine

logger = logging.getLogger(__name__)


def _transport_failure_reason(*, attempted_connections: int, failed_connections: int) -> str:
    if attempted_connections <= 0:
        return "no_active_connections"
    if failed_connections >= attempted_connections:
        return "all_connections_failed"
    return "unknown_transport_failure"


async def deliver_or_queue(
    message: WSResponse,
    is_scheduled: bool = False,
) -> DeliveryDecision:
    """Route a proactive message through the delivery gate.

    Reads current context, decides deliver/queue/drop, and acts accordingly.
    Returns the decision for callers that need to know.
    """
    from src.observer.manager import context_manager
    from src.observer.insight_queue import insight_queue
    from src.scheduler.connection_manager import ws_manager

    ctx = context_manager.get_context()
    intervention_type = message.intervention_type or message.type
    urgency = message.urgency or 0
    decision: DeliveryDecision | None = None

    try:
        decision = user_state_machine.should_deliver(
            user_state=ctx.user_state,
            interruption_mode=ctx.interruption_mode,
            attention_budget_remaining=ctx.attention_budget_remaining,
            urgency=urgency,
            intervention_type=intervention_type,
            is_scheduled=is_scheduled,
        )

        event_details = {
            "user_state": ctx.user_state,
            "interruption_mode": ctx.interruption_mode,
            "attention_budget_remaining": ctx.attention_budget_remaining,
        }

        if decision == DeliveryDecision.deliver:
            broadcast_result = await ws_manager.broadcast(message)
            event_details.update(
                {
                    "attempted_connections": broadcast_result.attempted_connections,
                    "delivered_connections": broadcast_result.delivered_connections,
                    "failed_connections": broadcast_result.failed_connections,
                }
            )
            if broadcast_result.delivered_connections <= 0:
                logger.warning(
                    "Failed to deliver proactive message over WebSocket transport (attempted=%d failed=%d)",
                    broadcast_result.attempted_connections,
                    broadcast_result.failed_connections,
                )
                await log_observer_delivery_event(
                    decision="failed",
                    message_type=message.type,
                    intervention_type=intervention_type,
                    urgency=urgency,
                    is_scheduled=is_scheduled,
                    details={
                        **event_details,
                        "delivery_decision": decision.value,
                        "error": _transport_failure_reason(
                            attempted_connections=broadcast_result.attempted_connections,
                            failed_connections=broadcast_result.failed_connections,
                        ),
                    },
                )
                return decision
            # Decrement budget if this delivery costs budget
            if user_state_machine.should_cost_budget(
                intervention_type=intervention_type,
                is_scheduled=is_scheduled,
                urgency=urgency,
            ):
                context_manager.decrement_attention_budget()
            logger.info("Delivered proactive message (type=%s)", message.type)
            await log_observer_delivery_event(
                decision="delivered",
                message_type=message.type,
                intervention_type=intervention_type,
                urgency=urgency,
                is_scheduled=is_scheduled,
                details=event_details,
            )

        elif decision == DeliveryDecision.queue:
            await insight_queue.enqueue(
                content=message.content,
                intervention_type=intervention_type,
                urgency=urgency,
                reasoning=message.reasoning or "",
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
            logger.info("Dropped proactive message (type=%s)", message.type)
            await log_observer_delivery_event(
                decision="dropped",
                message_type=message.type,
                intervention_type=intervention_type,
                urgency=urgency,
                is_scheduled=is_scheduled,
                details=event_details,
            )

        return decision
    except Exception as exc:
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
                "delivery_decision": decision.value if decision is not None else None,
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

    items = await insight_queue.drain()
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
                "error": _transport_failure_reason(
                    attempted_connections=broadcast_result.attempted_connections,
                    failed_connections=broadcast_result.failed_connections,
                ),
            },
        )
        return 0

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
