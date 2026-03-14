"""Strategist tick — periodic strategic reasoning via restricted agent."""

import asyncio
import logging
from time import perf_counter

from config.settings import settings
from src.agent.strategist import create_strategist_agent, parse_strategist_response
from src.audit.runtime import log_scheduler_job_event
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)


async def run_strategist_tick() -> None:
    """Review context and decide if proactive intervention is warranted."""
    started_at = perf_counter()
    try:
        from src.observer.manager import context_manager

        ctx = await context_manager.refresh()
        context_block = ctx.to_prompt_block()

        agent = create_strategist_agent(context_block)
        raw = await asyncio.wait_for(
            asyncio.to_thread(
                agent.run,
                "Analyze the current context and decide whether to intervene.",
            ),
            timeout=settings.agent_strategist_timeout,
        )

        decision = parse_strategist_response(str(raw))

        if not decision.should_intervene:
            await log_scheduler_job_event(
                job_name="strategist_tick",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": decision.reasoning,
                },
            )
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
        await log_scheduler_job_event(
            job_name="strategist_tick",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "intervention_type": decision.intervention_type,
                "urgency": decision.urgency,
                "delivery": result.value,
            },
        )
        logger.info(
            "strategist_tick: intervention sent (type=%s, urgency=%d, delivery=%s)",
            decision.intervention_type,
            decision.urgency,
            result.value,
        )

    except asyncio.TimeoutError:
        await log_scheduler_job_event(
            job_name="strategist_tick",
            outcome="timed_out",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "timeout_seconds": settings.agent_strategist_timeout,
            },
        )
        logger.warning("strategist_tick: agent timed out after %ds", settings.agent_strategist_timeout)
    except Exception as exc:
        await log_scheduler_job_event(
            job_name="strategist_tick",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
            },
        )
        logger.exception("strategist_tick failed")
