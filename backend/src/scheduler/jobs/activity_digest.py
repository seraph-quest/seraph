"""Activity digest — generates and delivers a daily screen activity summary."""

import asyncio
import logging
from datetime import date
from time import perf_counter

from config.settings import settings
from src.audit.runtime import log_scheduler_job_event
from src.llm_runtime import completion_with_fallback
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)

_DIGEST_PROMPT = """\
You are Seraph, a guardian intelligence. Generate a concise daily activity digest for your human.

Keep the RPG framing light. Be observational and constructive.

## User Identity
{soul}

## Today's Screen Activity
- Total tracked time: {total_minutes} minutes
- Context switches: {switch_count}

## Time by Activity Type
{activity_breakdown}

## Time by Project
{project_breakdown}

## Longest Focus Streaks
{streaks}

Write a short activity digest (4-8 sentences) covering:
1. Time distribution highlights (where did most time go?)
2. Focus patterns (long streaks? frequent switching?)
3. One concrete observation about work patterns
4. One suggestion for tomorrow

Be concise. No preamble. Just the digest text."""


async def run_activity_digest() -> None:
    """Generate and send the daily activity digest to connected clients."""
    started_at = perf_counter()
    try:
        from src.observer.screen_repository import screen_observation_repo
        from src.memory.soul import read_soul

        summary = await screen_observation_repo.get_daily_summary(date.today())

        if summary.get("total_observations", 0) == 0:
            await log_scheduler_job_event(
                job_name="activity_digest",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "no_observations",
                },
            )
            logger.info("activity_digest: no observations today — skipping")
            return

        soul = read_soul()

        # Format breakdowns
        activity_breakdown = "\n".join(
            f"- {act}: {secs // 60}m"
            for act, secs in summary.get("by_activity", {}).items()
        ) or "No data"

        project_breakdown = "\n".join(
            f"- {proj}: {secs // 60}m"
            for proj, secs in summary.get("by_project", {}).items()
        ) or "No projects detected"

        streaks = "\n".join(
            f"- {s['activity']}: {s['duration_minutes']}m (started {s['started_at'][:16]})"
            for s in summary.get("longest_streaks", [])
        ) or "No significant streaks"

        prompt = _DIGEST_PROMPT.format(
            soul=soul,
            total_minutes=summary.get("total_tracked_minutes", 0),
            switch_count=summary.get("switch_count", 0),
            activity_breakdown=activity_breakdown,
            project_breakdown=project_breakdown,
            streaks=streaks,
        )

        try:
            response = await completion_with_fallback(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=768,
                timeout=settings.agent_briefing_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("activity_digest: LLM timed out after %ds", settings.agent_briefing_timeout)
            await log_scheduler_job_event(
                job_name="activity_digest",
                outcome="timed_out",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "timeout_seconds": settings.agent_briefing_timeout,
                },
            )
            return

        digest_text = response.choices[0].message.content.strip()

        from src.observer.delivery import deliver_or_queue

        message = WSResponse(
            type="proactive",
            content=digest_text,
            intervention_type="advisory",
            urgency=2,
            reasoning="Scheduled daily activity digest",
        )
        await deliver_or_queue(message, is_scheduled=True)
        await log_scheduler_job_event(
            job_name="activity_digest",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "response_length": len(digest_text),
                "total_tracked_minutes": summary.get("total_tracked_minutes", 0),
                "switch_count": summary.get("switch_count", 0),
            },
        )
        logger.info("activity_digest: delivered daily digest")

    except Exception as exc:
        await log_scheduler_job_event(
            job_name="activity_digest",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
            },
        )
        logger.exception("activity_digest failed")
