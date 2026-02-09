import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.goals.repository import goal_repository

logger = logging.getLogger(__name__)

router = APIRouter()


class GoalCreate(BaseModel):
    title: str = Field(..., min_length=1)
    level: str = "daily"
    domain: str = "productivity"
    parent_id: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None


class GoalUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[str] = None


@router.get("/goals")
async def list_goals(
    level: Optional[str] = None,
    domain: Optional[str] = None,
    status: Optional[str] = None,
):
    """List goals, optionally filtered."""
    goals = await goal_repository.list_goals(level=level, domain=domain, status=status)
    return [
        {
            "id": g.id,
            "parent_id": g.parent_id,
            "title": g.title,
            "description": g.description,
            "level": g.level,
            "domain": g.domain,
            "status": g.status,
            "due_date": g.due_date.isoformat() if g.due_date else None,
            "created_at": g.created_at.isoformat(),
        }
        for g in goals
    ]


@router.get("/goals/tree")
async def get_goal_tree():
    """Get the full goal tree as nested structure."""
    return await goal_repository.get_tree()


@router.get("/goals/dashboard")
async def get_goal_dashboard():
    """Get summary stats for quest log UI."""
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
    )
    return {
        "id": goal.id,
        "title": goal.title,
        "level": goal.level,
        "domain": goal.domain,
        "status": goal.status,
    }


@router.patch("/goals/{goal_id}")
async def update_goal(goal_id: str, body: GoalUpdate):
    """Update a goal."""
    due = datetime.fromisoformat(body.due_date) if body.due_date else None
    goal = await goal_repository.update(
        goal_id=goal_id,
        title=body.title,
        description=body.description,
        status=body.status,
        due_date=due,
    )
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"status": "ok", "id": goal.id}


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str):
    """Delete a goal and its descendants."""
    success = await goal_repository.delete(goal_id)
    if not success:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"status": "ok"}
