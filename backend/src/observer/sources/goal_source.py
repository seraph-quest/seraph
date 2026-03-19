"""Goal context source — reads active goals from the repository."""

import logging
from collections import defaultdict

from src.audit.runtime import log_integration_event

logger = logging.getLogger(__name__)


async def gather_goals() -> dict:
    """Return a compact summary of active goals grouped by domain."""
    try:
        from src.goals.repository import goal_repository

        goals = await goal_repository.list_goals(status="active")

        if not goals:
            await log_integration_event(
                integration_type="observer_source",
                name="goals",
                outcome="empty_result",
                details={"goal_count": 0},
            )
            return {"active_goals_summary": ""}

        by_domain: dict[str, list[str]] = defaultdict(list)
        for g in goals:
            by_domain[g.domain].append(g.title)

        parts = []
        for domain, titles in by_domain.items():
            truncated = titles[:3]
            suffix = f" (+{len(titles) - 3} more)" if len(titles) > 3 else ""
            parts.append(f"{domain}: {', '.join(truncated)}{suffix}")

        await log_integration_event(
            integration_type="observer_source",
            name="goals",
            outcome="succeeded",
            details={
                "goal_count": len(goals),
                "domain_count": len(by_domain),
            },
        )
        return {"active_goals_summary": "; ".join(parts)}

    except Exception as exc:
        await log_integration_event(
            integration_type="observer_source",
            name="goals",
            outcome="failed",
            details={"error": str(exc)},
        )
        logger.exception("Goal source failed")
        return {"active_goals_summary": ""}
