"""Goal context source — reads active goals from the repository."""

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


async def gather_goals() -> dict:
    """Return a compact summary of active goals grouped by domain."""
    try:
        from src.goals.repository import goal_repository

        goals = await goal_repository.list_goals(status="active")

        if not goals:
            return {"active_goals_summary": ""}

        by_domain: dict[str, list[str]] = defaultdict(list)
        for g in goals:
            by_domain[g.domain].append(g.title)

        parts = []
        for domain, titles in by_domain.items():
            truncated = titles[:3]
            suffix = f" (+{len(titles) - 3} more)" if len(titles) > 3 else ""
            parts.append(f"{domain}: {', '.join(truncated)}{suffix}")

        return {"active_goals_summary": "; ".join(parts)}

    except Exception:
        logger.exception("Goal source failed")
        return {"active_goals_summary": ""}
