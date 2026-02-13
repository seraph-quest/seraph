"""Daily briefing — generates and delivers a morning briefing."""

import asyncio
import logging

from config.settings import settings
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


async def run_daily_briefing() -> None:
    """Generate and send the morning briefing to connected clients."""
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

        from src.memory.vector_store import search_formatted
        memories = await asyncio.to_thread(
            search_formatted,
            "daily priorities and routines",
            top_k=3,
        )
        memories_text = memories or "No relevant memories yet."

        context_text = ctx.to_prompt_block()

        prompt = _BRIEFING_PROMPT.format(
            soul=soul,
            context=context_text,
            events=events_text,
            goals=goals_text,
            memories=memories_text,
        )

        import litellm

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    litellm.completion,
                    model=settings.default_model,
                    messages=[{"role": "user", "content": prompt}],
                    api_key=settings.openrouter_api_key,
                    api_base="https://openrouter.ai/api/v1",
                    temperature=0.6,
                    max_tokens=512,
                ),
                timeout=settings.agent_briefing_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("daily_briefing: LLM timed out after %ds", settings.agent_briefing_timeout)
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
        logger.info("daily_briefing: delivered morning briefing")

    except Exception:
        logger.exception("daily_briefing failed")
