import asyncio
import concurrent.futures
from typing import Optional

from smolagents import tool

from src.approval.runtime import get_current_session_id, get_current_trust_principal
from src.goals.repository import GoalOwnershipConflict, goal_repository
from src.security.trust_contract import AuthorityGrant, PrincipalType


def _run(coro):
    """Run an async coroutine from sync context (for smolagents tools).

    Always uses a thread pool to avoid creating nested event loops
    that could conflict with the main FastAPI/SQLite event loop.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _authenticated_goal_owner() -> tuple[str, str]:
    """Resolve the canonical owner from the current agent execution context."""

    principal = get_current_trust_principal()
    principal_type = getattr(principal, "principal_type", None)
    if isinstance(principal_type, PrincipalType):
        is_operator = principal_type is PrincipalType.OPERATOR
    else:
        is_operator = str(principal_type or "").lower() == PrincipalType.OPERATOR.value
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    conversation_session = str(get_current_session_id() or "").strip()
    principal_session = str(getattr(principal, "session_id", "") or "").strip()
    owner_session = str(
        getattr(principal, "operator_session_id", "")
        or principal_session
        or conversation_session
    ).strip()
    grants = set(getattr(principal, "grants", ()) or ())
    grants = {str(getattr(grant, "value", grant)) for grant in grants}
    if not (
        principal is not None
        and is_operator
        and bool(getattr(principal, "authenticated", False))
        and not bool(getattr(principal, "revoked", False))
        and principal_id
        and owner_session
        and AuthorityGrant.CAPABILITY_EXECUTE.value in grants
    ):
        raise PermissionError(
            "goal tools require an authenticated operator execution context"
        )
    if principal_session and conversation_session and principal_session != conversation_session:
        raise PermissionError("goal tools runtime session is not bound to the authenticated principal")
    return principal_id, owner_session


@tool
def create_goal(
    title: str,
    level: str = "daily",
    domain: str = "productivity",
    parent_id: str = "",
    description: str = "",
    due_date: str = "",
) -> str:
    """Create a new goal in the user's goal hierarchy.

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

    owner_principal_id, owner_session_id = _authenticated_goal_owner()
    due = datetime.fromisoformat(due_date) if due_date else None
    pid = parent_id if parent_id else None

    try:
        goal = _run(goal_repository.create(
            title=title,
            level=level,
            domain=domain,
            parent_id=pid,
            description=description or None,
            due_date=due,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        ))
    except GoalOwnershipConflict as exc:
        raise PermissionError(str(exc)) from exc
    return f"Goal created: '{goal.title}' (id={goal.id}, level={goal.level}, domain={goal.domain})"


@tool
def update_goal(
    goal_id: str,
    status: str = "",
    title: str = "",
    expected_revision: int = 0,
    parent_id: str = "",
) -> str:
    """Update a goal's status or title.

    Args:
        goal_id: The ID of the goal to update.
        status: New status — one of: active, completed, paused, abandoned. Leave empty to keep current.
        title: New title. Leave empty to keep current.
        expected_revision: Current goal revision required for the compare-and-swap update.
        parent_id: Optional new parent goal ID. Leave empty to keep the current parent.

    Returns:
        Confirmation message.
    """
    owner_principal_id, owner_session_id = _authenticated_goal_owner()
    if isinstance(expected_revision, bool) or int(expected_revision or 0) < 1:
        raise ValueError("expected_revision is required for goal updates")
    current = _run(goal_repository.get(goal_id))
    if not current:
        return f"Goal '{goal_id}' not found."
    if (
        str(getattr(current, "owner_principal_id", "") or "").strip() != owner_principal_id
        or str(getattr(current, "owner_session_id", "") or "").strip() != owner_session_id
    ):
        raise PermissionError("goal owner/session does not match the authenticated operator")
    update_kwargs = {
        "goal_id": goal_id,
        "status": status or None,
        "title": title or None,
        "expected_revision": int(expected_revision),
        "expected_owner_principal_id": owner_principal_id,
        "expected_owner_session_id": owner_session_id,
    }
    if parent_id:
        update_kwargs["parent_id"] = parent_id
    try:
        goal = _run(goal_repository.update(**update_kwargs))
    except GoalOwnershipConflict as exc:
        raise PermissionError(str(exc)) from exc
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
    owner_principal_id, owner_session_id = _authenticated_goal_owner()
    goals = _run(goal_repository.list_goals(
        level=level or None,
        domain=domain or None,
        status=status or None,
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
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
    owner_principal_id, owner_session_id = _authenticated_goal_owner()
    dashboard = _run(goal_repository.get_dashboard(
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
    ))

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
