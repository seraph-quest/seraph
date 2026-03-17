"""Strategist tick — periodic strategic reasoning via restricted agent."""

import asyncio
import contextvars
import logging
from time import perf_counter

from src.approval.runtime import reset_runtime_context, set_runtime_context
from config.settings import settings
from src.agent.strategist import create_strategist_agent, parse_strategist_response
from src.audit.runtime import log_scheduler_job_event
from src.guardian.state import build_guardian_state
from src.llm_runtime import (
    _finish_request,
    _mark_request_timed_out,
    _register_request,
    reset_current_llm_request_id,
    set_current_llm_request_id,
)
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)
_STRATEGIST_RUNTIME_SESSION_ID = "scheduler:strategist_tick"


async def run_strategist_tick() -> None:
    """Review context and decide if proactive intervention is warranted."""
    started_at = perf_counter()
    try:
        guardian_state = await build_guardian_state(
            refresh_observer=True,
            memory_query="current priorities, commitments, and recent intervention patterns",
        )
        agent = create_strategist_agent(guardian_state=guardian_state)
        llm_request_id = f"strategist_tick:{started_at}"
        _register_request(llm_request_id)
        runtime_tokens = set_runtime_context(_STRATEGIST_RUNTIME_SESSION_ID, "high_risk")
        llm_request_token = set_current_llm_request_id(llm_request_id)
        run_ctx = contextvars.copy_context()
        reset_runtime_context(runtime_tokens)
        reset_current_llm_request_id(llm_request_token)
        raw = await asyncio.wait_for(
            asyncio.to_thread(
                run_ctx.run,
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
        if "llm_request_id" in locals():
            _mark_request_timed_out(llm_request_id)
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
    finally:
        if "llm_request_id" in locals():
            _finish_request(llm_request_id)
