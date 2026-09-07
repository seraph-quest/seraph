import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.goals.contracts import GoalCandidateRequest, GoalSuccessCriterion
from src.goals.repository import (
    GoalRevisionConflict,
    deserialize_success_criterion,
    goal_repository,
)
from src.guardian.goal_conditioned_loop import (
    list_goal_loop_receipts,
    propose_goal_candidate,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class GoalCreate(BaseModel):
    title: str = Field(..., min_length=1)
    level: str = "daily"
    domain: str = "productivity"
    parent_id: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    success_criterion: Optional[GoalSuccessCriterion] = None


class GoalUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[str] = None
    success_criterion: Optional[GoalSuccessCriterion] = None
    expected_revision: Optional[int] = Field(default=None, ge=1)


def _goal_payload(goal) -> dict:
    criterion = deserialize_success_criterion(goal)
    return {
        "id": goal.id,
        "parent_id": goal.parent_id,
        "title": goal.title,
        "description": goal.description,
        "level": goal.level,
        "domain": goal.domain,
        "status": goal.status,
        "due_date": goal.due_date.isoformat() if goal.due_date else None,
        "created_at": goal.created_at.isoformat(),
        "revision": max(int(goal.revision or 1), 1),
        "success_criterion": criterion.model_dump(mode="json") if criterion else None,
    }


@router.get("/goals")
async def list_goals(
    level: Optional[str] = None,
    domain: Optional[str] = None,
    status: Optional[str] = None,
):
    """List goals, optionally filtered."""
    goals = await goal_repository.list_goals(level=level, domain=domain, status=status)
    return [_goal_payload(goal) for goal in goals]


@router.get("/goals/tree")
async def get_goal_tree():
    """Get the full goal tree as nested structure."""
    return await goal_repository.get_tree()


@router.get("/goals/dashboard")
async def get_goal_dashboard():
    """Get summary stats for the goals UI."""
    return await goal_repository.get_dashboard()


@router.post("/goals")
async def create_goal(body: GoalCreate):
    """Create a new goal."""
    due = datetime.fromisoformat(body.due_date) if body.due_date else None
    goal = await goal_repository.create(
        title=body.title,
        level=body.level,
        domain=body.domain,
        parent_id=body.parent_id,
        description=body.description,
        due_date=due,
        success_criterion=body.success_criterion,
    )
    return {
        "id": goal.id,
        "title": goal.title,
        "level": goal.level,
        "domain": goal.domain,
        "status": goal.status,
        "revision": max(int(goal.revision or 1), 1),
        "success_criterion": (
            body.success_criterion.model_dump(mode="json")
            if body.success_criterion
            else None
        ),
    }


@router.patch("/goals/{goal_id}")
async def update_goal(goal_id: str, body: GoalUpdate):
    """Update a goal."""
    due = datetime.fromisoformat(body.due_date) if body.due_date else None
    try:
        goal = await goal_repository.update(
            goal_id=goal_id,
            title=body.title,
            description=body.description,
            status=body.status,
            due_date=due,
            success_criterion=body.success_criterion,
            expected_revision=body.expected_revision,
        )
    except GoalRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_goal_revision",
                "goal_id": exc.goal_id,
                "expected_revision": exc.expected,
                "current_revision": exc.current,
                "recovery": "Refresh the goal and resubmit against the current revision.",
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    criterion = deserialize_success_criterion(goal)
    return {
        "status": "ok",
        "id": goal.id,
        "revision": max(int(goal.revision or 1), 1),
        "success_criterion": criterion.model_dump(mode="json") if criterion else None,
    }


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str):
    """Delete a goal and its descendants."""
    success = await goal_repository.delete(goal_id)
    if not success:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"status": "ok"}


@router.get("/goals/{goal_id}/loop")
async def inspect_goal_loop(goal_id: str):
    """Inspect a goal's criterion and candidate/outcome receipts."""
    goal = await goal_repository.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    criterion = deserialize_success_criterion(goal)
    return {
        "goal": _goal_payload(goal),
        "criterion": criterion.model_dump(mode="json") if criterion else None,
        "receipts": await list_goal_loop_receipts(goal_id),
    }


@router.post("/goals/{goal_id}/candidates")
async def propose_goal_loop_candidate(goal_id: str, body: GoalCandidateRequest):
    """Create one bounded candidate decision for operator inspection."""
    try:
        decision = await propose_goal_candidate(goal_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Goal not found") from exc
    return {
        **decision.model_dump(mode="json"),
        "execution": {
            "available": False,
            "reason": "proposal_only_until_governed_admission_prerequisites_are_available",
        },
    }
