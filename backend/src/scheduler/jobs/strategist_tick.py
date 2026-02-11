"""Strategist tick — periodic strategic reasoning via restricted agent."""

import asyncio
import logging

from src.agent.strategist import create_strategist_agent, parse_strategist_response
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)


async def run_strategist_tick() -> None:
    """Review context and decide if proactive intervention is warranted."""
    try:
        from src.observer.manager import context_manager

        ctx = await context_manager.refresh()
        context_block = ctx.to_prompt_block()

        agent = create_strategist_agent(context_block)
        raw = await asyncio.to_thread(
            agent.run,
            "Analyze the current context and decide whether to intervene.",
        )

        decision = parse_strategist_response(str(raw))

        if not decision.should_intervene:
            logger.info("strategist_tick: no intervention needed — %s", decision.reasoning)
            return

        from src.observer.delivery import deliver_or_queue

        message = WSResponse(
            type="proactive",
            content=decision.content,
            intervention_type=decision.intervention_type,
            urgency=decision.urgency,
            reasoning=decision.reasoning,
        )
        result = await deliver_or_queue(message)
        logger.info(
            "strategist_tick: intervention sent (type=%s, urgency=%d, delivery=%s)",
            decision.intervention_type,
            decision.urgency,
            result.value,
        )

    except Exception:
        logger.exception("strategist_tick failed")
