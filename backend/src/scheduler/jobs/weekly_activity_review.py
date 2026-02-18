"""Weekly activity review — generates and delivers a weekly screen activity analysis."""

import asyncio
import logging
from datetime import date, timedelta

from config.settings import settings
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)

_WEEKLY_PROMPT = """\
You are Seraph, a guardian intelligence. Generate a weekly activity review for your human.

Keep the RPG framing light. Be analytical and forward-looking.

## User Identity
{soul}

## This Week's Screen Activity ({week_start} to {week_end})
- Total tracked time: {total_minutes} minutes
- Total context switches: {total_observations}

## Weekly Activity Breakdown
{activity_breakdown}

## Project Allocation
{project_breakdown}

## Daily Breakdown
{daily_breakdown}

Write a weekly activity review (5-10 sentences) covering:
1. Weekly overview — where did time go?
2. Daily patterns — which days were most productive?
3. Project allocation — balanced or lopsided?
4. Two suggestions for next week
5. One automation or workflow idea

Be concise. No preamble. Just the review text."""


async def run_weekly_activity_review() -> None:
    """Generate and send the weekly activity review to connected clients."""
    try:
        from src.observer.screen_repository import screen_observation_repo
        from src.memory.soul import read_soul

        # Calculate the Monday of the current week
        today = date.today()
        week_start = today - timedelta(days=today.weekday())

        summary = await screen_observation_repo.get_weekly_summary(week_start)

        if summary.get("total_observations", 0) == 0:
            logger.info("weekly_activity_review: no observations this week — skipping")
            return

        soul = read_soul()

        activity_breakdown = "\n".join(
            f"- {act}: {secs // 60}m"
            for act, secs in summary.get("by_activity", {}).items()
        ) or "No data"

        project_breakdown = "\n".join(
            f"- {proj}: {secs // 60}m"
            for proj, secs in summary.get("by_project", {}).items()
        ) or "No projects detected"

        daily_breakdown = "\n".join(
            f"- {d['date']}: {d['tracked_minutes']}m tracked, {d['observations']} switches"
            for d in summary.get("daily_breakdown", [])
            if d.get("observations", 0) > 0
        ) or "No daily data"

        prompt = _WEEKLY_PROMPT.format(
            soul=soul,
            week_start=summary.get("week_start", ""),
            week_end=summary.get("week_end", ""),
            total_minutes=summary.get("total_tracked_minutes", 0),
            total_observations=summary.get("total_observations", 0),
            activity_breakdown=activity_breakdown,
            project_breakdown=project_breakdown,
            daily_breakdown=daily_breakdown,
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
                    max_tokens=1024,
                ),
                timeout=settings.agent_briefing_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("weekly_activity_review: LLM timed out after %ds", settings.agent_briefing_timeout)
            return

        review_text = response.choices[0].message.content.strip()

        from src.observer.delivery import deliver_or_queue

        message = WSResponse(
            type="proactive",
            content=review_text,
            intervention_type="advisory",
            urgency=2,
            reasoning="Scheduled weekly activity review",
        )
        await deliver_or_queue(message, is_scheduled=True)
        logger.info("weekly_activity_review: delivered weekly review")

    except Exception:
        logger.exception("weekly_activity_review failed")
