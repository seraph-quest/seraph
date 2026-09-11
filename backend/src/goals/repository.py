import logging
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, update
from sqlmodel import select, col

from src.db.engine import get_session
from src.db.models import (
    Goal,
    GoalLevel,
    GoalDomain,
    GoalStatus,
    NativeNotificationOutbox,
    QueuedInsight,
    StrategyDelta,
    WorkflowRunState,
)
from src.goals.contracts import GoalAdmissionBudget, GoalSuccessCriterion

logger = logging.getLogger(__name__)

_VALID_LEVELS = {e.value for e in GoalLevel}
_VALID_DOMAINS = {e.value for e in GoalDomain}
_VALID_STATUSES = {e.value for e in GoalStatus}
_UNSET = object()


class GoalRevisionConflict(ValueError):
    """Raised when an optimistic goal update uses an old revision."""

    def __init__(self, goal_id: str, expected: int, current: int):
        self.goal_id = goal_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"Goal '{goal_id}' changed since revision {expected}; current revision is {current}"
        )


class GoalOwnershipConflict(ValueError):
    """Raised when a goal mutation crosses a canonical owner boundary."""

    def __init__(self, code: str = "goal_owner_mismatch"):
        self.code = code
        super().__init__(code)


def _normalized_owner(value: object) -> str | None:
    return str(value or "").strip() or None


def _owner_pair(goal: Goal | object) -> tuple[str | None, str | None]:
    return (
        _normalized_owner(getattr(goal, "owner_principal_id", None)),
        _normalized_owner(getattr(goal, "owner_session_id", None)),
    )


def _validate_owner_pair(
    owner_principal_id: str | None,
    owner_session_id: str | None,
) -> tuple[str | None, str | None]:
    principal = _normalized_owner(owner_principal_id)
    session = _normalized_owner(owner_session_id)
    if (principal is None) != (session is None):
        raise GoalOwnershipConflict("goal_owner_binding_incomplete")
    return principal, session


def _validate_parent_owner_binding(
    parent: Goal,
    *,
    owner_principal_id: str | None,
    owner_session_id: str | None,
) -> None:
    """Prevent a child from entering another owner's deletion tree.

    Both fields may be absent for legacy repository-only trees. Once either
    side is owner-bound, however, the parent and child must carry the same
    complete pair. This keeps old read-only trees usable while making every
    authenticated mutation fail closed.
    """

    child_owner = _validate_owner_pair(owner_principal_id, owner_session_id)
    parent_owner = _validate_owner_pair(*_owner_pair(parent))
    if child_owner != parent_owner and (child_owner != (None, None) or parent_owner != (None, None)):
        raise GoalOwnershipConflict("goal_parent_owner_mismatch")


def serialize_success_criterion(
    criterion: GoalSuccessCriterion | dict | None,
) -> str | None:
    if criterion is None:
        return None
    parsed = (
        criterion
        if isinstance(criterion, GoalSuccessCriterion)
        else GoalSuccessCriterion.model_validate(criterion)
    )
    return parsed.model_dump_json()


def deserialize_success_criterion(goal: Goal) -> GoalSuccessCriterion | None:
    if not goal.success_criterion_json:
        return None
    try:
        return GoalSuccessCriterion.model_validate(json.loads(goal.success_criterion_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        # Legacy or manually edited rows stay inspectable as missing/unknown
        # evidence and can never authorize execution.
        logger.warning("Invalid success criterion stored for goal %s", goal.id)
        return None


def serialize_admission_budget(
    budget: GoalAdmissionBudget | dict | None,
) -> str | None:
    if budget is None:
        return None
    parsed = budget if isinstance(budget, GoalAdmissionBudget) else GoalAdmissionBudget.model_validate(budget)
    return parsed.model_dump_json()


def deserialize_admission_budget(goal: Goal) -> GoalAdmissionBudget | None:
    raw = getattr(goal, "admission_budget_json", None)
    if not raw:
        return None
    try:
        return GoalAdmissionBudget.model_validate(json.loads(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Invalid admission budget stored for goal %s", goal.id)
        return None


class GoalRepository:
    """CRUD operations for the Goal table."""

    async def create(
        self,
        title: str,
        level: str = "daily",
        domain: str = "productivity",
        parent_id: Optional[str] = None,
        description: Optional[str] = None,
        due_date: Optional[datetime] = None,
        success_criterion: GoalSuccessCriterion | dict | None = None,
        proactive_enabled: bool = False,
        owner_principal_id: str | None = None,
        owner_session_id: str | None = None,
        operator_session_id: str | None = None,
        admission_budget: GoalAdmissionBudget | dict | None = None,
    ) -> Goal:
        if level not in _VALID_LEVELS:
            raise ValueError(f"Invalid level '{level}'. Must be one of: {_VALID_LEVELS}")
        if domain not in _VALID_DOMAINS:
            raise ValueError(f"Invalid domain '{domain}'. Must be one of: {_VALID_DOMAINS}")
        if (
            owner_session_id is not None
            and operator_session_id is not None
            and str(owner_session_id).strip() != str(operator_session_id).strip()
        ):
            raise ValueError("goal owner session aliases conflict")
        owner_session_id = owner_session_id or operator_session_id
        owner_principal_id, owner_session_id = _validate_owner_pair(
            owner_principal_id,
            owner_session_id,
        )
        async with get_session() as db:
            goal_id = uuid.uuid4().hex[:8]

            # Build materialized path
            path = "/"
            if parent_id:
                result = await db.execute(
                    select(Goal).where(Goal.id == parent_id)
                )
                parent = result.scalars().first()
                if parent is None:
                    raise ValueError("goal_parent_not_found")
                _validate_parent_owner_binding(
                    parent,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
                path = f"{parent.path}{parent.id}/"

            # Get next sort_order for siblings
            siblings = await db.execute(
                select(Goal).where(Goal.parent_id == parent_id)
            )
            sort_order = len(siblings.scalars().all())

            goal = Goal(
                id=goal_id,
                parent_id=parent_id,
                path=path,
                level=level,
                title=title,
                description=description,
                domain=domain,
                due_date=due_date,
                sort_order=sort_order,
                revision=1,
                success_criterion_json=serialize_success_criterion(success_criterion),
                proactive_enabled=bool(proactive_enabled),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                admission_budget_json=serialize_admission_budget(admission_budget),
            )
            db.add(goal)
            await db.flush()
            return goal

    async def get(self, goal_id: str) -> Optional[Goal]:
        async with get_session() as db:
            result = await db.execute(select(Goal).where(Goal.id == goal_id))
            return result.scalars().first()

    async def update(
        self,
        goal_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        level: Optional[str] = None,
        domain: Optional[str] = None,
        status: Optional[str] = None,
        due_date: Optional[datetime] = None,
        success_criterion: GoalSuccessCriterion | dict | None = None,
        proactive_enabled: bool | None = None,
        admission_budget: GoalAdmissionBudget | dict | None = None,
        owner_principal_id: str | None = None,
        owner_session_id: str | None = None,
        expected_owner_principal_id: str | None = None,
        expected_owner_session_id: str | None = None,
        parent_id: str | None | object = _UNSET,
        expected_revision: int | None = None,
    ) -> Optional[Goal]:
        if level is not None and level not in _VALID_LEVELS:
            raise ValueError(f"Invalid level '{level}'. Must be one of: {_VALID_LEVELS}")
        if domain is not None and domain not in _VALID_DOMAINS:
            raise ValueError(f"Invalid domain '{domain}'. Must be one of: {_VALID_DOMAINS}")
        if status is not None and status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of: {_VALID_STATUSES}")
        async with get_session() as db:
            result = await db.execute(select(Goal).where(Goal.id == goal_id))
            goal = result.scalars().first()
            if not goal:
                return None
            current_owner = _validate_owner_pair(*_owner_pair(goal))
            if expected_owner_principal_id is not None or expected_owner_session_id is not None:
                expected_owner = _validate_owner_pair(
                    expected_owner_principal_id,
                    expected_owner_session_id,
                )
                if current_owner != expected_owner:
                    raise GoalOwnershipConflict("goal_owner_mismatch")
            requested_owner = current_owner
            if owner_principal_id is not None or owner_session_id is not None:
                requested_owner = _validate_owner_pair(owner_principal_id, owner_session_id)
                if current_owner != (None, None) and requested_owner != current_owner:
                    raise GoalOwnershipConflict("goal_owner_rebind_forbidden")
            current_revision = max(int(goal.revision or 1), 1)
            if expected_revision is not None and expected_revision != current_revision:
                raise GoalRevisionConflict(goal_id, expected_revision, current_revision)
            changed = False
            values: dict[str, object] = {}
            descendant_rows: list[Goal] = []
            old_descendant_path = f"{goal.path}{goal.id}/"
            if parent_id is not _UNSET:
                requested_parent_id = _normalized_owner(parent_id)
                if requested_parent_id == goal.id:
                    raise ValueError("goal_parent_cycle")
                parent = None
                if requested_parent_id is not None:
                    parent_result = await db.execute(
                        select(Goal).where(Goal.id == requested_parent_id)
                    )
                    parent = parent_result.scalars().first()
                    if parent is None:
                        raise ValueError("goal_parent_not_found")
                    if parent.path.startswith(old_descendant_path):
                        raise ValueError("goal_parent_cycle")
                    _validate_parent_owner_binding(
                        parent,
                        owner_principal_id=requested_owner[0],
                        owner_session_id=requested_owner[1],
                    )
                if requested_parent_id != goal.parent_id:
                    next_path = (
                        f"{parent.path}{parent.id}/" if parent is not None else "/"
                    )
                    descendant_result = await db.execute(
                        select(Goal).where(col(Goal.path).startswith(old_descendant_path))
                    )
                    descendant_rows = list(descendant_result.scalars().all())
                    values["parent_id"] = requested_parent_id
                    values["path"] = next_path
                    changed = True
            if title is not None:
                values["title"] = title
                changed = True
            if description is not None:
                values["description"] = description
                changed = True
            if level is not None:
                values["level"] = level
                changed = True
            if domain is not None:
                values["domain"] = domain
                changed = True
            if status is not None:
                values["status"] = status
                changed = True
            if due_date is not None:
                values["due_date"] = due_date
                changed = True
            if success_criterion is not None:
                values["success_criterion_json"] = serialize_success_criterion(success_criterion)
                changed = True
            if proactive_enabled is not None:
                values["proactive_enabled"] = bool(proactive_enabled)
                changed = True
            if admission_budget is not None:
                values["admission_budget_json"] = serialize_admission_budget(admission_budget)
                changed = True
            if owner_principal_id is not None:
                values["owner_principal_id"] = str(owner_principal_id).strip() or None
                changed = True
            if owner_session_id is not None:
                values["owner_session_id"] = str(owner_session_id).strip() or None
                changed = True
            values["updated_at"] = datetime.now(timezone.utc)
            if changed:
                values["revision"] = Goal.revision + 1
            guards = [Goal.id == goal_id]
            if changed:
                # The revision predicate makes the read/check/write sequence a
                # compare-and-swap even when callers omit expected_revision.
                guards.append(Goal.revision == current_revision)
            result = await db.execute(update(Goal).where(*guards).values(**values))
            if result.rowcount != 1:
                latest_result = await db.execute(select(Goal).where(Goal.id == goal_id))
                latest = latest_result.scalars().first()
                latest_revision = max(int(latest.revision or 1), 1) if latest else current_revision
                raise GoalRevisionConflict(goal_id, expected_revision or current_revision, latest_revision)
            if descendant_rows:
                new_descendant_path = str(values["path"])
                for descendant in descendant_rows:
                    descendant.path = (
                        new_descendant_path
                        + descendant.path[len(old_descendant_path):]
                    )
                    db.add(descendant)
            await db.flush()
            refreshed_result = await db.execute(select(Goal).where(Goal.id == goal_id))
            return refreshed_result.scalars().first()

    async def delete(
        self,
        goal_id: str,
        *,
        expected_owner_principal_id: str | None = None,
        expected_owner_session_id: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """Delete a goal and all its descendants with an owner/revision CAS."""
        async with get_session() as db:
            result = await db.execute(select(Goal).where(Goal.id == goal_id))
            goal = result.scalars().first()
            if not goal:
                return False

            root_owner = _validate_owner_pair(*_owner_pair(goal))
            if expected_owner_principal_id is not None or expected_owner_session_id is not None:
                expected_owner = _validate_owner_pair(
                    expected_owner_principal_id,
                    expected_owner_session_id,
                )
                if root_owner != expected_owner:
                    raise GoalOwnershipConflict("goal_owner_mismatch")
            current_revision = max(int(goal.revision or 1), 1)
            if expected_revision is not None and expected_revision != current_revision:
                raise GoalRevisionConflict(goal_id, expected_revision, current_revision)

            # Delete descendants (path starts with this goal's full path)
            descendant_path = f"{goal.path}{goal.id}/"
            descendants = await db.execute(
                select(Goal).where(col(Goal.path).startswith(descendant_path))
            )
            descendant_rows = sorted(
                descendants.scalars().all(),
                key=lambda item: (item.path.count("/"), item.created_at),
                reverse=True,
            )
            goal_ids = [goal.id, *(item.id for item in descendant_rows)]
            for descendant in descendant_rows:
                if _owner_pair(descendant) != root_owner:
                    raise GoalOwnershipConflict("goal_descendant_owner_mismatch")

            now = datetime.now(timezone.utc)
            # Cancel durable effects before deleting the canonical goal. A
            # native outbox row remains as a receipt, while deferred insights
            # and pending strategy/job rows cannot be replayed after deletion.
            await db.execute(
                delete(QueuedInsight).where(QueuedInsight.goal_id.in_(goal_ids))
            )
            await db.execute(
                update(NativeNotificationOutbox)
                .where(
                    NativeNotificationOutbox.goal_id.in_(goal_ids),
                    NativeNotificationOutbox.status.in_({"queued", "claimed", "display_attempted"}),
                )
                .values(
                    status="cancelled",
                    cancelled_at=now,
                    last_error="goal_deleted",
                    degraded_state="goal_deleted",
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            await db.execute(
                update(StrategyDelta)
                .where(
                    StrategyDelta.goal_id.in_(goal_ids),
                    StrategyDelta.status == "proposed",
                )
                .values(
                    status="rejected",
                    updated_at=now,
                )
            )
            await db.execute(
                update(WorkflowRunState)
                .where(
                    WorkflowRunState.goal_id.in_(goal_ids),
                    WorkflowRunState.status.in_({
                        "accepted",
                        "queued",
                        "awaiting_approval",
                        "paused",
                        "blocked",
                        # Deletion is an authority revocation.  These states
                        # must be fenced as well; otherwise a stale runner or
                        # recovery request could resurrect work after the
                        # canonical goal row is gone.
                        "running",
                        "failed",
                        "unknown_external_effect",
                        "cost_liability",
                    }),
                )
                .values(
                    status="cancelled",
                    failure_reason="goal_deleted",
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=now,
                    fencing_token=WorkflowRunState.fencing_token + 1,
                    revision=WorkflowRunState.revision + 1,
                    updated_at=now,
                )
            )
            for d in descendant_rows:
                await db.delete(d)
                await db.flush()

            delete_guards = [
                Goal.id == goal_id,
                Goal.revision == current_revision,
            ]
            for column, value in zip(
                (Goal.owner_principal_id, Goal.owner_session_id),
                root_owner,
            ):
                delete_guards.append(column == value if value is not None else column.is_(None))
            deleted = await db.execute(delete(Goal).where(*delete_guards))
            if deleted.rowcount != 1:
                latest_result = await db.execute(select(Goal).where(Goal.id == goal_id))
                latest = latest_result.scalars().first()
                latest_revision = (
                    max(int(latest.revision or 1), 1)
                    if latest is not None
                    else current_revision
                )
                raise GoalRevisionConflict(
                    goal_id,
                    expected_revision if expected_revision is not None else current_revision,
                    latest_revision,
                )
            return True

    async def list_goals(
        self,
        level: Optional[str] = None,
        domain: Optional[str] = None,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
        owner_principal_id: str | None = None,
        owner_session_id: str | None = None,
    ) -> list[Goal]:
        async with get_session() as db:
            query = select(Goal)
            if level:
                query = query.where(Goal.level == level)
            if domain:
                query = query.where(Goal.domain == domain)
            if status:
                query = query.where(Goal.status == status)
            if parent_id is not None:
                query = query.where(Goal.parent_id == parent_id)
            if owner_principal_id is not None:
                query = query.where(Goal.owner_principal_id == owner_principal_id)
            if owner_session_id is not None:
                query = query.where(Goal.owner_session_id == owner_session_id)
            query = query.order_by(Goal.sort_order, col(Goal.created_at).asc())
            result = await db.execute(query)
            return list(result.scalars().all())

    async def get_children(
        self,
        goal_id: str,
        *,
        owner_principal_id: str | None = None,
        owner_session_id: str | None = None,
    ) -> list[Goal]:
        return await self.list_goals(
            parent_id=goal_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )

    async def get_tree(
        self,
        *,
        owner_principal_id: str | None = None,
        owner_session_id: str | None = None,
    ) -> list[dict]:
        """Return a goal tree, optionally restricted to one canonical owner."""
        async with get_session() as db:
            query = select(Goal)
            if owner_principal_id is not None:
                query = query.where(Goal.owner_principal_id == owner_principal_id)
            if owner_session_id is not None:
                query = query.where(Goal.owner_session_id == owner_session_id)
            result = await db.execute(query.order_by(Goal.sort_order, col(Goal.created_at).asc()))
            all_goals = result.scalars().all()

        # Build tree structure
        goal_map = {}
        for g in all_goals:
            criterion = deserialize_success_criterion(g)
            goal_map[g.id] = {
                "id": g.id,
                "parent_id": g.parent_id,
                "title": g.title,
                "description": g.description,
                "level": g.level,
                "domain": g.domain,
                "status": g.status,
                "revision": max(int(g.revision or 1), 1),
                "success_criterion": criterion.model_dump(mode="json") if criterion else None,
                "proactive_enabled": bool(getattr(g, "proactive_enabled", False)),
                "owner_principal_id": getattr(g, "owner_principal_id", None),
                "owner_session_id": getattr(g, "owner_session_id", None),
                "admission_budget": (
                    deserialize_admission_budget(g).model_dump(mode="json")
                    if deserialize_admission_budget(g) else None
                ),
                "due_date": g.due_date.isoformat() if g.due_date else None,
                "created_at": g.created_at.isoformat(),
                "children": [],
            }

        roots = []
        for g in all_goals:
            node = goal_map[g.id]
            if g.parent_id and g.parent_id in goal_map:
                goal_map[g.parent_id]["children"].append(node)
            else:
                if g.parent_id:
                    logger.warning(
                        "Orphan goal %s: parent %s not found, treating as root",
                        g.id, g.parent_id,
                    )
                roots.append(node)

        return roots

    async def get_dashboard(
        self,
        *,
        owner_principal_id: str | None = None,
        owner_session_id: str | None = None,
    ) -> dict:
        """Return summary stats for the goals UI."""
        async with get_session() as db:
            query = select(Goal)
            if owner_principal_id is not None:
                query = query.where(Goal.owner_principal_id == owner_principal_id)
            if owner_session_id is not None:
                query = query.where(Goal.owner_session_id == owner_session_id)
            result = await db.execute(query)
            all_goals = result.scalars().all()

        if not all_goals:
            return {"domains": {}, "active_count": 0, "completed_count": 0, "total_count": 0}

        domains = {}
        active_count = 0
        completed_count = 0

        for g in all_goals:
            d = g.domain
            if d not in domains:
                domains[d] = {"active": 0, "completed": 0, "total": 0}
            domains[d]["total"] += 1
            if g.status == GoalStatus.completed:
                domains[d]["completed"] += 1
                completed_count += 1
            elif g.status == GoalStatus.active:
                domains[d]["active"] += 1
                active_count += 1

        # Calculate progress percentages
        for d in domains:
            total = domains[d]["total"]
            completed = domains[d]["completed"]
            domains[d]["progress"] = round((completed / total) * 100) if total else 0

        return {
            "domains": domains,
            "active_count": active_count,
            "completed_count": completed_count,
            "total_count": len(all_goals),
        }


goal_repository = GoalRepository()
