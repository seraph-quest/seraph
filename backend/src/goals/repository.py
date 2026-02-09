import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import select, col

from src.db.engine import get_session
from src.db.models import Goal, GoalStatus


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
    ) -> Goal:
        async with get_session() as db:
            goal_id = uuid.uuid4().hex[:8]

            # Build materialized path
            path = "/"
            if parent_id:
                result = await db.exec(
                    select(Goal).where(Goal.id == parent_id)
                )
                parent = result.first()
                if parent:
                    path = f"{parent.path}{parent.id}/"

            # Get next sort_order for siblings
            siblings = await db.exec(
                select(Goal).where(Goal.parent_id == parent_id)
            )
            sort_order = len(siblings.all())

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
            )
            db.add(goal)
            await db.flush()
            return goal

    async def get(self, goal_id: str) -> Optional[Goal]:
        async with get_session() as db:
            result = await db.exec(select(Goal).where(Goal.id == goal_id))
            return result.first()

    async def update(
        self,
        goal_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        due_date: Optional[datetime] = None,
    ) -> Optional[Goal]:
        async with get_session() as db:
            result = await db.exec(select(Goal).where(Goal.id == goal_id))
            goal = result.first()
            if not goal:
                return None
            if title is not None:
                goal.title = title
            if description is not None:
                goal.description = description
            if status is not None:
                goal.status = status
            if due_date is not None:
                goal.due_date = due_date
            goal.updated_at = datetime.utcnow()
            db.add(goal)
            return goal

    async def delete(self, goal_id: str) -> bool:
        """Delete a goal and all its descendants."""
        async with get_session() as db:
            result = await db.exec(select(Goal).where(Goal.id == goal_id))
            goal = result.first()
            if not goal:
                return False

            # Delete descendants (path starts with this goal's full path)
            descendant_path = f"{goal.path}{goal.id}/"
            descendants = await db.exec(
                select(Goal).where(col(Goal.path).startswith(descendant_path))
            )
            for d in descendants.all():
                await db.delete(d)

            await db.delete(goal)
            return True

    async def list_goals(
        self,
        level: Optional[str] = None,
        domain: Optional[str] = None,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
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
            query = query.order_by(Goal.sort_order, col(Goal.created_at).asc())
            result = await db.exec(query)
            return list(result.all())

    async def get_children(self, goal_id: str) -> list[Goal]:
        return await self.list_goals(parent_id=goal_id)

    async def get_tree(self) -> list[dict]:
        """Return the full goal tree as nested dicts."""
        async with get_session() as db:
            result = await db.exec(
                select(Goal).order_by(Goal.sort_order, col(Goal.created_at).asc())
            )
            all_goals = result.all()

        # Build tree structure
        goal_map = {}
        for g in all_goals:
            goal_map[g.id] = {
                "id": g.id,
                "parent_id": g.parent_id,
                "title": g.title,
                "description": g.description,
                "level": g.level,
                "domain": g.domain,
                "status": g.status,
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
                roots.append(node)

        return roots

    async def get_dashboard(self) -> dict:
        """Return summary stats for the quest log UI."""
        async with get_session() as db:
            result = await db.exec(select(Goal))
            all_goals = result.all()

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
