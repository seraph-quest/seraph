"""Tests for GoalRepository (src/goals/repository.py)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.db.models import NativeNotificationOutbox, QueuedInsight, StrategyDelta, WorkflowRunState
from src.goals.repository import GoalOwnershipConflict, GoalRepository


@pytest.fixture
def repo():
    return GoalRepository()


class TestCreate:
    async def test_basic(self, async_db, repo):
        goal = await repo.create("Do homework", level="daily", domain="productivity")
        assert goal.id
        assert goal.title == "Do homework"
        assert goal.level == "daily"
        assert goal.domain == "productivity"
        assert goal.status == "active"
        assert goal.path == "/"

    async def test_with_parent(self, async_db, repo):
        parent = await repo.create("Vision", level="vision", domain="growth")
        child = await repo.create("Annual", level="annual", domain="growth", parent_id=parent.id)
        assert child.parent_id == parent.id
        assert child.path == f"/{parent.id}/"

    async def test_rejects_cross_owner_parent_before_child_insert(self, async_db, repo):
        owner_a = {"owner_principal_id": "operator:a", "owner_session_id": "session:a"}
        parent = await repo.create("Owner A", **owner_a)

        with pytest.raises(GoalOwnershipConflict) as exc:
            await repo.create(
                "Cross-owner child",
                parent_id=parent.id,
                owner_principal_id="operator:b",
                owner_session_id="session:b",
            )

        assert exc.value.code == "goal_parent_owner_mismatch"
        assert await repo.list_goals(owner_principal_id="operator:b", owner_session_id="session:b") == []

    async def test_invalid_level(self, async_db, repo):
        with pytest.raises(ValueError, match="Invalid level"):
            await repo.create("Bad", level="nope")

    async def test_invalid_domain(self, async_db, repo):
        with pytest.raises(ValueError, match="Invalid domain"):
            await repo.create("Bad", domain="nope")

    async def test_sort_order_increments(self, async_db, repo):
        g1 = await repo.create("First")
        g2 = await repo.create("Second")
        assert g2.sort_order == 1


class TestGet:
    async def test_existing(self, async_db, repo):
        created = await repo.create("Test")
        found = await repo.get(created.id)
        assert found is not None
        assert found.title == "Test"

    async def test_nonexistent(self, async_db, repo):
        assert await repo.get("nope") is None


class TestUpdate:
    async def test_title(self, async_db, repo):
        goal = await repo.create("Old")
        updated = await repo.update(goal.id, title="New")
        assert updated.title == "New"

    async def test_status(self, async_db, repo):
        goal = await repo.create("Test")
        updated = await repo.update(goal.id, status="completed")
        assert updated.status == "completed"

    async def test_level_and_domain(self, async_db, repo):
        goal = await repo.create("Test")
        updated = await repo.update(goal.id, level="weekly", domain="health")
        assert updated.level == "weekly"
        assert updated.domain == "health"

    async def test_invalid_status(self, async_db, repo):
        goal = await repo.create("Test")
        with pytest.raises(ValueError, match="Invalid status"):
            await repo.update(goal.id, status="bad")

    async def test_nonexistent(self, async_db, repo):
        assert await repo.update("nope", title="X") is None

    async def test_reparent_rejects_cross_owner_parent_before_mutation(self, async_db, repo):
        owner_a = {"owner_principal_id": "operator:a", "owner_session_id": "session:a"}
        owner_b = {"owner_principal_id": "operator:b", "owner_session_id": "session:b"}
        parent_a = await repo.create("Parent A", **owner_a)
        parent_b = await repo.create("Parent B", **owner_b)
        child = await repo.create("Child", parent_id=parent_a.id, **owner_a)

        with pytest.raises(GoalOwnershipConflict) as exc:
            await repo.update(
                child.id,
                parent_id=parent_b.id,
                expected_owner_principal_id=owner_a["owner_principal_id"],
                expected_owner_session_id=owner_a["owner_session_id"],
                expected_revision=child.revision,
            )

        assert exc.value.code == "goal_parent_owner_mismatch"
        persisted = await repo.get(child.id)
        assert persisted.parent_id == parent_a.id
        assert persisted.path == f"/{parent_a.id}/"


class TestDelete:
    async def test_success(self, async_db, repo):
        goal = await repo.create("Test")
        assert await repo.delete(goal.id) is True
        assert await repo.get(goal.id) is None

    async def test_nonexistent(self, async_db, repo):
        assert await repo.delete("nope") is False

    async def test_cascading(self, async_db, repo):
        parent = await repo.create("Parent", level="vision", domain="growth")
        child = await repo.create("Child", level="annual", domain="growth", parent_id=parent.id)
        await repo.delete(parent.id)
        assert await repo.get(child.id) is None

    async def test_delete_cancels_goal_bound_effects_before_cascade(self, async_db, repo):
        owner = {"owner_principal_id": "operator:a", "owner_session_id": "session:a"}
        goal = await repo.create("Delete me", **owner)
        now = datetime.now(timezone.utc)
        async with async_db() as db:
            db.add(
                QueuedInsight(
                    content="queued insight",
                    goal_id=goal.id,
                    owner_principal_id=owner["owner_principal_id"],
                    operator_session_id=owner["owner_session_id"],
                    budget_period_key="2026-09-11",
                    budget_limit=2,
                )
            )
            db.add(
                NativeNotificationOutbox(
                    idempotency_key="delete-goal-notification",
                    payload_digest="digest",
                    title="queued notification",
                    body="body",
                    goal_id=goal.id,
                    owner_principal_id=owner["owner_principal_id"],
                    operator_session_id=owner["owner_session_id"],
                    budget_period_key="2026-09-11",
                    budget_limit=2,
                    deadline_at=now + timedelta(hours=1),
                    status="queued",
                )
            )
            db.add(
                StrategyDelta(
                    goal_id=goal.id,
                    source_event_id="delete-goal-delta",
                    author_id=owner["owner_principal_id"],
                    status="proposed",
                )
            )
            db.add(
                WorkflowRunState(
                    run_identity="delete-goal-run",
                    root_run_identity="delete-goal-run",
                    workflow_name="goal-snapshot-to-file",
                    goal_id=goal.id,
                    status="accepted",
                )
            )

        assert await repo.delete(
            goal.id,
            expected_owner_principal_id=owner["owner_principal_id"],
            expected_owner_session_id=owner["owner_session_id"],
        ) is True
        assert await repo.get(goal.id) is None
        async with async_db() as db:
            queued = (await db.execute(QueuedInsight.__table__.select())).all()
            outbox = (
                await db.execute(
                    NativeNotificationOutbox.__table__.select().where(
                        NativeNotificationOutbox.idempotency_key == "delete-goal-notification"
                    )
                )
            ).first()
            delta = (
                await db.execute(
                    StrategyDelta.__table__.select().where(
                        StrategyDelta.source_event_id == "delete-goal-delta"
                    )
                )
            ).first()
            run = (
                await db.execute(
                    WorkflowRunState.__table__.select().where(
                        WorkflowRunState.run_identity == "delete-goal-run"
                    )
                )
            ).first()
        assert queued == []
        assert outbox.status == "cancelled"
        assert outbox.last_error == "goal_deleted"
        assert delta.status == "rejected"
        assert run.status == "cancelled"
        assert run.failure_reason == "goal_deleted"


class TestListGoals:
    async def test_all(self, async_db, repo):
        await repo.create("A")
        await repo.create("B")
        goals = await repo.list_goals()
        assert len(goals) == 2

    async def test_filtered_by_level(self, async_db, repo):
        await repo.create("Daily", level="daily")
        await repo.create("Weekly", level="weekly")
        goals = await repo.list_goals(level="daily")
        assert len(goals) == 1
        assert goals[0].level == "daily"

    async def test_filtered_by_domain(self, async_db, repo):
        await repo.create("Health", domain="health")
        await repo.create("Growth", domain="growth")
        goals = await repo.list_goals(domain="health")
        assert len(goals) == 1


class TestGetTree:
    async def test_nested_structure(self, async_db, repo):
        parent = await repo.create("Vision", level="vision")
        await repo.create(
            "Annual",
            level="annual",
            parent_id=parent.id,
            proactive_enabled=True,
        )
        tree = await repo.get_tree()
        assert len(tree) == 1
        assert tree[0]["title"] == "Vision"
        assert len(tree[0]["children"]) == 1
        assert tree[0]["children"][0]["title"] == "Annual"
        assert tree[0]["children"][0]["proactive_enabled"] is True

    async def test_empty(self, async_db, repo):
        tree = await repo.get_tree()
        assert tree == []


class TestGetDashboard:
    async def test_empty(self, async_db, repo):
        dashboard = await repo.get_dashboard()
        assert dashboard["total_count"] == 0
        assert dashboard["domains"] == {}

    async def test_with_goals(self, async_db, repo):
        await repo.create("A", domain="health")
        g = await repo.create("B", domain="health")
        await repo.update(g.id, status="completed")
        dashboard = await repo.get_dashboard()
        assert dashboard["total_count"] == 2
        assert dashboard["domains"]["health"]["active"] == 1
        assert dashboard["domains"]["health"]["completed"] == 1
        assert dashboard["domains"]["health"]["progress"] == 50
