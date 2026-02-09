import asyncio
import concurrent.futures
from typing import Optional

from smolagents import tool

from src.goals.repository import goal_repository


def _run(coro):
    """Run an async coroutine from sync context (for smolagents tools).

    Always uses a thread pool to avoid creating nested event loops
    that could conflict with the main FastAPI/SQLite event loop.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@tool
def create_goal(
    title: str,
    level: str = "daily",
    domain: str = "productivity",
    parent_id: str = "",
    description: str = "",
    due_date: str = "",
) -> str:
    """Create a new goal in the user's quest log.

    Use this when the user mentions a goal, objective, or task they want to achieve.
    Decompose large goals into smaller sub-goals by setting parent_id.

    Args:
        title: Short, clear goal title.
        level: Goal level — one of: vision, annual, quarterly, monthly, weekly, daily.
        domain: Life domain — one of: productivity, performance, health, influence, growth.
        parent_id: ID of the parent goal (for sub-goals). Leave empty for top-level goals.
        description: Optional longer description of what achieving this goal looks like.
        due_date: Optional due date in ISO format (e.g., '2026-06-30').

    Returns:
        Confirmation with the created goal's ID.
    """
    from datetime import datetime

    due = datetime.fromisoformat(due_date) if due_date else None
    pid = parent_id if parent_id else None

    goal = _run(goal_repository.create(
        title=title,
        level=level,
        domain=domain,
        parent_id=pid,
        description=description or None,
        due_date=due,
    ))
    return f"Goal created: '{goal.title}' (id={goal.id}, level={goal.level}, domain={goal.domain})"


@tool
def update_goal(goal_id: str, status: str = "", title: str = "") -> str:
    """Update a goal's status or title.

    Args:
        goal_id: The ID of the goal to update.
        status: New status — one of: active, completed, paused, abandoned. Leave empty to keep current.
        title: New title. Leave empty to keep current.

    Returns:
        Confirmation message.
    """
    goal = _run(goal_repository.update(
        goal_id=goal_id,
        status=status or None,
        title=title or None,
    ))
    if not goal:
        return f"Goal '{goal_id}' not found."
    return f"Goal updated: '{goal.title}' is now {goal.status}."


@tool
def get_goals(level: str = "", domain: str = "", status: str = "active") -> str:
    """Get the user's goals, optionally filtered.

    Args:
        level: Filter by level (vision/annual/quarterly/monthly/weekly/daily). Leave empty for all.
        domain: Filter by domain (productivity/performance/health/influence/growth). Leave empty for all.
        status: Filter by status (active/completed/paused/abandoned). Default: active.

    Returns:
        Formatted list of goals.
    """
    goals = _run(goal_repository.list_goals(
        level=level or None,
        domain=domain or None,
        status=status or None,
    ))
    if not goals:
        return "No goals found matching the criteria."

    lines = []
    for g in goals:
        due = f" (due: {g.due_date.strftime('%Y-%m-%d')})" if g.due_date else ""
        lines.append(f"- [{g.level}/{g.domain}] {g.title} (id={g.id}, {g.status}){due}")
    return "\n".join(lines)


@tool
def get_goal_progress() -> str:
    """Get a summary of goal progress across all life domains.

    Returns:
        Dashboard summary with progress per domain and overall stats.
    """
    dashboard = _run(goal_repository.get_dashboard())

    if dashboard["total_count"] == 0:
        return "No goals set yet. Let's define some goals together!"

    lines = ["Goal Progress Dashboard:"]
    lines.append(f"Total: {dashboard['total_count']} goals ({dashboard['completed_count']} completed, {dashboard['active_count']} active)")
    lines.append("")

    for domain, stats in dashboard["domains"].items():
        bar_len = 10
        filled = round(stats["progress"] / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        lines.append(f"  {domain.capitalize():14s} {bar} {stats['progress']}% ({stats['completed']}/{stats['total']})")

    return "\n".join(lines)
