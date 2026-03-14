"""Evening review — generates and delivers an end-of-day reflection."""

import asyncio
import logging
from datetime import date
from time import perf_counter

from config.settings import settings
from src.audit.runtime import log_scheduler_job_event
from src.llm_runtime import completion_with_fallback
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)

_REVIEW_PROMPT = """\
You are Seraph, a guardian intelligence. Generate a concise evening review for your human.

Keep the RPG framing light. Be reflective and encouraging.

## User Identity
{soul}

## Today's Summary
- Messages exchanged today: {message_count}
- Goals completed today: {completed_goals}
- Git activity: {git_activity}

## Active Goals
{active_goals}

Write a short evening review (3-6 sentences) covering:
1. Acknowledge what was accomplished today
2. Note any goals that were completed or progressed
3. Gentle observation about patterns (if any)
4. A brief look-ahead at tomorrow

Be concise. No preamble. Just the review text."""


async def _count_messages_today() -> int:
    """Count messages created today."""
    try:
        from src.db.engine import get_session as get_db_session
        from src.db.models import Message
        from sqlmodel import select, func

        async with get_db_session() as db:
            today = date.today()
            result = await db.execute(
                select(func.count(Message.id)).where(
                    func.date(Message.created_at) == today
                )
            )
            return result.scalar_one_or_none() or 0
    except Exception:
        logger.exception("Failed to count today's messages")
        return 0


async def _get_completed_goals_today() -> list[str]:
    """Get titles of goals completed today."""
    try:
        from src.goals.repository import goal_repository

        goals = await goal_repository.list_goals(status="completed")
        today = date.today()
        return [
            g.title for g in goals
            if g.updated_at and g.updated_at.date() == today
        ]
    except Exception:
        logger.exception("Failed to get completed goals")
        return []


async def run_evening_review() -> None:
    """Generate and send the evening review to connected clients."""
    started_at = perf_counter()
    try:
        from src.observer.manager import context_manager

        ctx = await context_manager.refresh()

        from src.memory.soul import read_soul
        soul = read_soul()

        # Gather today's data
        message_count, completed_titles = await asyncio.gather(
            _count_messages_today(),
            _get_completed_goals_today(),
        )

        completed_text = ", ".join(completed_titles) if completed_titles else "None today"
        git_text = f"{len(ctx.recent_git_activity)} commits" if ctx.recent_git_activity else "No git activity"
        goals_text = ctx.active_goals_summary or "No active goals."

        prompt = _REVIEW_PROMPT.format(
            soul=soul,
            message_count=message_count,
            completed_goals=completed_text,
            git_activity=git_text,
            active_goals=goals_text,
        )

        try:
            response = await completion_with_fallback(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=512,
                timeout=settings.agent_briefing_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("evening_review: LLM timed out after %ds", settings.agent_briefing_timeout)
            await log_scheduler_job_event(
                job_name="evening_review",
                outcome="timed_out",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "timeout_seconds": settings.agent_briefing_timeout,
                },
            )
            return

        review_text = response.choices[0].message.content.strip()

        from src.observer.delivery import deliver_or_queue

        message = WSResponse(
            type="proactive",
            content=review_text,
            intervention_type="advisory",
            urgency=2,
            reasoning="Scheduled evening review",
        )
        await deliver_or_queue(message, is_scheduled=True)
        await log_scheduler_job_event(
            job_name="evening_review",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "response_length": len(review_text),
                "completed_goal_count": len(completed_titles),
                "message_count": message_count,
            },
        )
        logger.info("evening_review: delivered evening review")

    except Exception as exc:
        await log_scheduler_job_event(
            job_name="evening_review",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
            },
        )
        logger.exception("evening_review failed")
