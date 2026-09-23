import asyncio
import concurrent.futures
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Optional

from smolagents import tool

from src.approval.runtime import get_current_session_id, get_current_trust_principal
from src.auth.service import authenticate_session
from src.goals.repository import GoalOwnershipConflict, goal_repository
from src.security.trust_contract import AuthorityGrant, PrincipalType
from src.workflows.job_runtime import durable_job_repository


_GOAL_SNAPSHOT_SERVICE_ID = "service:goal-snapshot"
_GOAL_SNAPSHOT_JOB_KIND = "workflow.goal-snapshot-to-file"
_GOAL_SNAPSHOT_CAPABILITY_VERSION = "1"
_GOAL_SNAPSHOT_WORKFLOW_JOB_KIND = "goal-snapshot-to-file"
_GOAL_SNAPSHOT_WORKFLOW_CAPABILITY_VERSION = "workflow-v2"
_GOAL_SNAPSHOT_WORKFLOW_TOOL_NAME = "workflow_goal_snapshot_to_file"


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


def _text(value: object) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _positive_revision(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _future_lease_expiry(value: object) -> datetime | None:
    """Parse a lease expiry with the durable runtime's UTC-naive convention."""

    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    # SQLite may round-trip a UTC datetime without its offset. Match the
    # durable runtime's _as_utc() behavior for that canonical serialized form.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.utcoffset() is None:
        return None
    expiry = parsed.astimezone(timezone.utc)
    return expiry if expiry > datetime.now(timezone.utc) else None


def _service_goal_snapshot_context() -> tuple[str, str]:
    """Resolve the one durable child allowed to read a delegated goal.

    The workflow step runs with the service principal, while the canonical goal
    remains owned by the operator.  The child job projection is the authority
    for that delegation; this helper only extracts the current job/session
    identity and never substitutes an operator principal.
    """

    principal = get_current_trust_principal()
    principal_type = getattr(principal, "principal_type", None)
    is_service = (
        principal_type is PrincipalType.SERVICE
        if isinstance(principal_type, PrincipalType)
        else _text(principal_type).lower() == PrincipalType.SERVICE.value
    )
    principal_id = _text(getattr(principal, "principal_id", None))
    conversation_session = _text(get_current_session_id())
    principal_session = _text(getattr(principal, "session_id", None))
    operator_session = _text(getattr(principal, "operator_session_id", None))
    job_id = _text(getattr(principal, "job_id", None))
    grants = {
        _text(getattr(grant, "value", grant))
        for grant in (getattr(principal, "grants", ()) or ())
    }
    if not (
        principal is not None
        and is_service
        and principal_id == _GOAL_SNAPSHOT_SERVICE_ID
        and bool(getattr(principal, "authenticated", False))
        and not bool(getattr(principal, "revoked", False))
        and AuthorityGrant.CAPABILITY_EXECUTE.value in grants
        and conversation_session
        and principal_session == conversation_session
        and job_id
    ):
        raise PermissionError(
            "goal snapshot requires the authenticated service child execution context"
        )
    if operator_session and operator_session != conversation_session:
        raise PermissionError("goal snapshot service session is not bound to the durable child")
    return job_id, conversation_session


async def _load_delegated_goal(*, job_id: str, session_id: str):
    """Read exactly the goal delegated by a nested snapshot workflow run.

    The service principal is bound to the nested WorkflowTool run while its
    ``get_goals`` step executes.  That run is useful only when its persisted
    parent is the still-running, fenced GoalSnapshot child that carries the
    operator-owned goal delegation.  The nested workflow projection alone
    cannot grant a goal read because it intentionally has no canonical goal
    authority of its own.
    """

    try:
        projection = await durable_job_repository.get_job(job_id)
    except Exception as exc:
        raise PermissionError("goal snapshot workflow run is unavailable") from exc
    if not isinstance(projection, Mapping):
        raise PermissionError("goal snapshot workflow run is missing")

    persisted_job_id = _text(projection.get("job_id") or projection.get("run_identity"))
    workflow_owner = projection.get("owner")
    workflow_authority = projection.get("declared_authority")
    if not isinstance(workflow_owner, Mapping) or not isinstance(workflow_authority, Mapping):
        raise PermissionError("goal snapshot workflow delegation is incomplete")
    if (
        persisted_job_id != job_id
        or _text(projection.get("status")) != "running"
        or _text(workflow_owner.get("kind")) != "service"
        or _text(workflow_owner.get("principal_id")) != _GOAL_SNAPSHOT_SERVICE_ID
        or _text(workflow_owner.get("service_id")) != _GOAL_SNAPSHOT_SERVICE_ID
        or _text(projection.get("job_kind")) != _GOAL_SNAPSHOT_WORKFLOW_JOB_KIND
        or _text(projection.get("capability_version")) != _GOAL_SNAPSHOT_WORKFLOW_CAPABILITY_VERSION
        or _text(projection.get("session_id")) != session_id
        or _text(workflow_authority.get("principal")) != _GOAL_SNAPSHOT_SERVICE_ID
        or _text(workflow_authority.get("owner_kind")) != "service"
        or _text(workflow_authority.get("service_id")) != _GOAL_SNAPSHOT_SERVICE_ID
        or _text(workflow_authority.get("session_id")) != session_id
        or _text(workflow_authority.get("capability")) != _GOAL_SNAPSHOT_WORKFLOW_TOOL_NAME
    ):
        raise PermissionError("goal snapshot workflow identity is stale or mismatched")
    persisted_operator_session = _text(projection.get("operator_session_id"))
    if persisted_operator_session and persisted_operator_session != session_id:
        raise PermissionError("goal snapshot workflow operator session is stale")

    parent_job_id = _text(projection.get("parent_job_id"))
    parent_run_identity = _text(projection.get("parent_run_identity"))
    parent_fencing_token = _positive_revision(projection.get("parent_fencing_token"))
    root_run_identity = _text(projection.get("root_run_identity"))
    if (
        not parent_job_id
        or not parent_run_identity
        or parent_run_identity != parent_job_id
        or parent_fencing_token is None
        or not root_run_identity
    ):
        raise PermissionError("goal snapshot workflow lineage is incomplete")

    try:
        parent = await durable_job_repository.get_job(parent_job_id)
    except Exception as exc:
        raise PermissionError("goal snapshot parent run is unavailable") from exc
    if not isinstance(parent, Mapping):
        raise PermissionError("goal snapshot parent run is missing")

    parent_persisted_job_id = _text(parent.get("job_id") or parent.get("run_identity"))
    parent_owner = parent.get("owner")
    authority = parent.get("declared_authority")
    parent_lease = parent.get("lease")
    if (
        not isinstance(parent_owner, Mapping)
        or not isinstance(authority, Mapping)
        or not isinstance(parent_lease, Mapping)
    ):
        raise PermissionError("goal snapshot parent delegation is incomplete")
    parent_fence = _positive_revision(parent_lease.get("fencing_token"))
    parent_root_identity = _text(parent.get("root_run_identity"))
    if (
        parent_persisted_job_id != parent_job_id
        or _text(parent.get("status")) != "running"
        or _text(parent_owner.get("kind")) != "service"
        or _text(parent_owner.get("principal_id")) != _GOAL_SNAPSHOT_SERVICE_ID
        or _text(parent_owner.get("service_id")) != _GOAL_SNAPSHOT_SERVICE_ID
        or _text(parent.get("job_kind")) != _GOAL_SNAPSHOT_JOB_KIND
        or _text(parent.get("capability_version")) != _GOAL_SNAPSHOT_CAPABILITY_VERSION
        or _text(parent.get("session_id")) != session_id
        or not _text(parent_lease.get("owner"))
        or parent_fence != parent_fencing_token
        or _future_lease_expiry(parent_lease.get("expires_at")) is None
        or not parent_root_identity
        or parent_root_identity != root_run_identity
    ):
        raise PermissionError("goal snapshot parent identity or fence is stale")
    parent_operator_session = _text(parent.get("operator_session_id"))
    if parent_operator_session and parent_operator_session != session_id:
        raise PermissionError("goal snapshot parent operator session is stale")

    goal_id = _text(parent.get("goal_id"))
    goal_revision = _positive_revision(parent.get("goal_revision"))
    authority_goal_id = _text(authority.get("goal_id"))
    authority_goal_revision = _positive_revision(authority.get("goal_revision"))
    delegated_owner = _text(authority.get("goal_owner_principal_id"))
    delegated_session = _text(authority.get("goal_owner_session_id"))
    if not (
        goal_id
        and goal_revision is not None
        and authority_goal_id == goal_id
        and authority_goal_revision == goal_revision
        and _text(authority.get("capability_id")) == _GOAL_SNAPSHOT_JOB_KIND
        and _text(authority.get("capability_version")) == _GOAL_SNAPSHOT_CAPABILITY_VERSION
        and _text(authority.get("principal")) == _GOAL_SNAPSHOT_SERVICE_ID
        and _text(authority.get("owner_kind")) == "service"
        and _text(authority.get("owner_principal_id")) == _GOAL_SNAPSHOT_SERVICE_ID
        and _text(authority.get("service_id")) == _GOAL_SNAPSHOT_SERVICE_ID
        and _text(authority.get("session_id")) == session_id
        and delegated_owner
        and delegated_session
        and delegated_session == session_id
    ):
        raise PermissionError("goal snapshot parent goal binding is stale or incomplete")

    # The nested workflow normally carries no goal contract of its own.  If a
    # projection does carry one, it must agree with the parent rather than
    # widening the delegated read.
    nested_goal_id = _text(projection.get("goal_id"))
    nested_goal_revision = projection.get("goal_revision")
    nested_authority_goal_id = _text(workflow_authority.get("goal_id"))
    nested_authority_goal_revision = workflow_authority.get("goal_revision")
    if (
        (nested_goal_id and nested_goal_id != goal_id)
        or (
            nested_goal_revision is not None
            and _positive_revision(nested_goal_revision) != goal_revision
        )
        or (nested_authority_goal_id and nested_authority_goal_id != goal_id)
        or (
            nested_authority_goal_revision is not None
            and _positive_revision(nested_authority_goal_revision) != goal_revision
        )
    ):
        raise PermissionError("goal snapshot nested goal binding is stale or mismatched")

    try:
        live_session = await authenticate_session(delegated_session, touch=False)
    except Exception as exc:
        raise PermissionError("goal snapshot owner session is not valid") from exc
    live_principal = getattr(live_session, "principal", None)
    live_principal_type = getattr(live_principal, "principal_type", None)
    live_principal_id = _text(getattr(live_principal, "principal_id", None))
    live_is_operator = (
        live_principal_type is PrincipalType.OPERATOR
        if isinstance(live_principal_type, PrincipalType)
        else _text(live_principal_type).lower() == PrincipalType.OPERATOR.value
    )
    if not (
        live_principal is not None
        and live_is_operator
        and live_principal_id == delegated_owner
        and bool(getattr(live_principal, "authenticated", False))
        and not bool(getattr(live_principal, "revoked", False))
        and _text(getattr(live_session, "session_id", None)) == delegated_session
        and (
            not _text(getattr(live_principal, "session_id", None))
            or _text(getattr(live_principal, "session_id", None)) == delegated_session
        )
        and (
            not _text(getattr(live_principal, "operator_session_id", None))
            or _text(getattr(live_principal, "operator_session_id", None)) == delegated_session
        )
    ):
        raise PermissionError("goal snapshot owner session is not authenticated")

    try:
        goal = await goal_repository.get(goal_id)
    except Exception as exc:
        raise PermissionError("goal snapshot canonical goal is unavailable") from exc
    if goal is None:
        raise PermissionError("goal snapshot canonical goal is missing")
    canonical_revision = _positive_revision(getattr(goal, "revision", None))
    if (
        _text(getattr(goal, "id", None)) != goal_id
        or _text(getattr(goal, "status", None)) != "active"
        or canonical_revision != goal_revision
        or _text(getattr(goal, "owner_principal_id", None)) != delegated_owner
        or _text(getattr(goal, "owner_session_id", None)) != delegated_session
    ):
        raise PermissionError("goal snapshot canonical goal owner or revision is stale")
    return goal


def _format_goal(goal) -> str:
    due = f" (due: {goal.due_date.strftime('%Y-%m-%d')})" if goal.due_date else ""
    return f"- [{goal.level}/{goal.domain}] {goal.title} (id={goal.id}, {goal.status}){due}"


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
    principal = get_current_trust_principal()
    principal_type = getattr(principal, "principal_type", None)
    is_service = (
        principal_type is PrincipalType.SERVICE
        if isinstance(principal_type, PrincipalType)
        else _text(principal_type).lower() == PrincipalType.SERVICE.value
    )
    if is_service:
        if level or domain or status not in {"", "active"}:
            raise PermissionError("goal snapshot service reads only its delegated goal")
        job_id, session_id = _service_goal_snapshot_context()
        goal = _run(_load_delegated_goal(job_id=job_id, session_id=session_id))
        return _format_goal(goal)

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

    return "\n".join(_format_goal(g) for g in goals)


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
