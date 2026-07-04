"""Strategist tick — periodic strategic reasoning via restricted agent."""

import asyncio
import logging
from time import perf_counter

from config.settings import settings
from src.agent.strategist import parse_strategist_response, run_strategist_decision_completion
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


def _delivery_value(result) -> str | None:
    delivery_decision = getattr(result, "delivery_decision", None)
    if delivery_decision is not None:
        return delivery_decision.value
    return getattr(result, "value", None)


def _policy_action_value(result) -> str | None:
    action = getattr(result, "action", None)
    if action is not None:
        return action.value
    return None


async def run_strategist_tick() -> None:
    """Review context and decide if proactive intervention is warranted."""
    started_at = perf_counter()
    llm_request_id: str | None = None
    try:
        guardian_state = await build_guardian_state(
            refresh_observer=True,
            memory_query="current priorities, commitments, and recent intervention patterns",
        )
        llm_request_id = f"strategist_tick:{started_at}"
        _register_request(llm_request_id)
        llm_request_token = set_current_llm_request_id(llm_request_id)
        reset_current_llm_request_id(llm_request_token)
        raw = await run_strategist_decision_completion(
            guardian_state=guardian_state,
        )

        decision = parse_strategist_response(str(raw))

        if not decision.should_intervene:
            await log_scheduler_job_event(
                job_name="strategist_tick",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": decision.reasoning,
                    "request_id": llm_request_id,
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
        result = await deliver_or_queue(
            message,
            guardian_confidence=guardian_state.confidence.overall,
        )
        await log_scheduler_job_event(
            job_name="strategist_tick",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "intervention_type": decision.intervention_type,
                "urgency": decision.urgency,
                "delivery": _delivery_value(result),
                "policy_action": _policy_action_value(result),
                "request_id": llm_request_id,
            },
        )
        logger.info(
            "strategist_tick: intervention handled (type=%s, urgency=%d, delivery=%s, action=%s)",
            decision.intervention_type,
            decision.urgency,
            _delivery_value(result),
            _policy_action_value(result),
        )

    except asyncio.TimeoutError:
        if llm_request_id is not None:
            _mark_request_timed_out(llm_request_id)
        await log_scheduler_job_event(
            job_name="strategist_tick",
            outcome="timed_out",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "timeout_seconds": settings.agent_strategist_timeout,
                "request_id": llm_request_id,
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
                "request_id": llm_request_id,
            },
        )
        logger.exception("strategist_tick failed")
    finally:
        if llm_request_id is not None:
            _finish_request(llm_request_id)
