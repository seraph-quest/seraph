"""Delivery coordinator — single entry point for all proactive messages."""

import logging

from src.models.schemas import WSResponse
from src.observer.user_state import DeliveryDecision, user_state_machine

logger = logging.getLogger(__name__)


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

    decision = user_state_machine.should_deliver(
        user_state=ctx.user_state,
        interruption_mode=ctx.interruption_mode,
        attention_budget_remaining=ctx.attention_budget_remaining,
        urgency=message.urgency or 0,
        intervention_type=message.intervention_type or message.type,
        is_scheduled=is_scheduled,
    )

    if decision == DeliveryDecision.deliver:
        await ws_manager.broadcast(message)
        # Decrement budget if this delivery costs budget
        if user_state_machine.should_cost_budget(
            intervention_type=message.intervention_type or message.type,
            is_scheduled=is_scheduled,
            urgency=message.urgency or 0,
        ):
            context_manager.decrement_attention_budget()
        logger.info("Delivered proactive message (type=%s)", message.type)

    elif decision == DeliveryDecision.queue:
        await insight_queue.enqueue(
            content=message.content,
            intervention_type=message.intervention_type or message.type,
            urgency=message.urgency or 0,
            reasoning=message.reasoning or "",
        )
        logger.info("Queued proactive message (state=%s, mode=%s)", ctx.user_state, ctx.interruption_mode)

    else:
        logger.info("Dropped proactive message (type=%s)", message.type)

    return decision


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

    await ws_manager.broadcast(
        WSResponse(
            type="proactive",
            content=bundle_content,
            intervention_type="proactive_bundle",
            urgency=3,
            reasoning=f"Bundle of {len(items)} queued insight(s) delivered on state transition",
        )
    )
    logger.info("Delivered bundle of %d queued insight(s)", len(items))
    return len(items)
