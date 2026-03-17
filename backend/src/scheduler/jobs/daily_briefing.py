"""Daily briefing — generates and delivers a morning briefing."""

import asyncio
import logging
from time import perf_counter

from config.settings import settings
from src.audit.runtime import log_background_task_event, log_scheduler_job_event
from src.llm_runtime import completion_with_fallback
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)

_BRIEFING_PROMPT = """\
You are Seraph, a guardian intelligence. Generate a concise morning briefing for your human.

Keep the RPG framing light. Be warm but efficient. Use bullet points for clarity.

## User Identity
{soul}

## Today's Context
{context}

## Upcoming Events
{events}

## Active Goals
{goals}

## Relevant Memories
{memories}

Write a short morning briefing (3-6 sentences) covering:
1. A warm greeting appropriate to the time/day
2. Key events or deadlines today
3. Goal check-in — what should they focus on?
4. Any relevant memories or patterns worth noting

Be concise. No preamble. Just the briefing text."""


async def _get_relevant_memories() -> tuple[str, bool]:
    """Fetch memory context for the briefing while preserving fail-open behavior."""
    from src.memory.vector_store import search_with_status

    results, degraded = await asyncio.to_thread(
        search_with_status,
        "daily priorities and routines",
        top_k=3,
    )
    if not results:
        if degraded:
            await log_background_task_event(
                task_name="daily_briefing_inputs",
                outcome="degraded",
                details={
                    "source": "relevant_memories",
                    "fallback_value": "No relevant memories yet.",
                    "error": "vector_store_search_failed",
                },
            )
            return "No relevant memories yet.", True
        return "No relevant memories yet.", False

    lines = [f"- [{result['category']}] {result['text']}" for result in results]
    return "\n".join(lines), False


async def run_daily_briefing() -> None:
    """Generate and send the morning briefing to connected clients."""
    started_at = perf_counter()
    try:
        from src.observer.manager import context_manager

        ctx = await context_manager.refresh()

        # Gather context data
        from src.memory.soul import read_soul
        soul = read_soul()

        events_text = "No events scheduled."
        if ctx.upcoming_events:
            lines = []
            for e in ctx.upcoming_events:
                lines.append(f"- {e.get('summary', '?')} at {e.get('start', '?')}")
            events_text = "\n".join(lines)

        goals_text = ctx.active_goals_summary or "No active goals."

        memories_text, memories_degraded = await _get_relevant_memories()
        degraded_inputs = []
        if memories_degraded:
            degraded_inputs.append("relevant_memories")
        data_quality = "degraded" if degraded_inputs else "good"

        context_text = ctx.to_prompt_block()

        prompt = _BRIEFING_PROMPT.format(
            soul=soul,
            context=context_text,
            events=events_text,
            goals=goals_text,
            memories=memories_text,
        )

        try:
            response = await completion_with_fallback(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=512,
                timeout=settings.agent_briefing_timeout,
                runtime_path="daily_briefing",
            )
        except asyncio.TimeoutError:
            logger.warning("daily_briefing: LLM timed out after %ds", settings.agent_briefing_timeout)
            await log_scheduler_job_event(
                job_name="daily_briefing",
                outcome="timed_out",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "timeout_seconds": settings.agent_briefing_timeout,
                },
            )
            return

        briefing_text = response.choices[0].message.content.strip()

        from src.observer.delivery import deliver_or_queue

        message = WSResponse(
            type="proactive",
            content=briefing_text,
            intervention_type="advisory",
            urgency=3,
            reasoning="Scheduled morning briefing",
        )
        await deliver_or_queue(message, is_scheduled=True)
        await log_scheduler_job_event(
            job_name="daily_briefing",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "response_length": len(briefing_text),
                "upcoming_event_count": len(ctx.upcoming_events),
                "data_quality": data_quality,
                "degraded_inputs": degraded_inputs,
            },
        )
        logger.info("daily_briefing: delivered morning briefing")

    except Exception as exc:
        await log_scheduler_job_event(
            job_name="daily_briefing",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
            },
        )
        logger.exception("daily_briefing failed")
