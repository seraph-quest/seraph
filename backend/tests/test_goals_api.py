"""Tests for goals HTTP endpoints (src/api/goals.py)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.api.goals import GOAL_SNAPSHOT_SERVICE_ID
from src.audit.repository import audit_repository
from src.guardian.goal_snapshot_to_file import GoalSnapshotToFileResult
from src.goals.repository import GoalRepository


@pytest.fixture
def repo():
    return GoalRepository()


class TestCreateGoal:
    async def test_success(self, client, async_db):
        res = await client.post("/api/goals", json={
            "title": "Learn Python",
            "level": "daily",
            "domain": "growth",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["title"] == "Learn Python"
        assert "id" in data

    async def test_missing_title(self, client):
        res = await client.post("/api/goals", json={
            "title": "",
            "level": "daily",
        })
        assert res.status_code == 422


class TestListGoals:
    async def test_empty(self, client):
        res = await client.get("/api/goals")
        assert res.status_code == 200
        assert res.json() == []

    async def test_with_filter(self, client, async_db, repo):
        await repo.create("A", level="daily", domain="health")
        await repo.create("B", level="weekly", domain="growth")
        res = await client.get("/api/goals?level=daily")
        assert res.status_code == 200
        goals = res.json()
        assert len(goals) == 1
        assert goals[0]["level"] == "daily"


class TestGetTree:
    async def test_returns_tree(self, client, async_db, repo):
        parent = await repo.create("Vision", level="vision")
        await repo.create("Annual", level="annual", parent_id=parent.id)
        res = await client.get("/api/goals/tree")
        assert res.status_code == 200
        tree = res.json()
        assert len(tree) == 1
        assert len(tree[0]["children"]) == 1


class TestGetDashboard:
    async def test_returns_dashboard(self, client, async_db, repo):
        await repo.create("A", domain="health")
        res = await client.get("/api/goals/dashboard")
        assert res.status_code == 200
        data = res.json()
        assert data["total_count"] == 1
        assert "health" in data["domains"]


class TestUpdateGoal:
    async def test_success(self, client, async_db, repo):
        goal = await repo.create("Test")
        res = await client.patch(f"/api/goals/{goal.id}", json={"title": "Updated"})
        assert res.status_code == 200

    async def test_not_found(self, client):
        res = await client.patch("/api/goals/nope", json={"title": "X"})
        assert res.status_code == 404


class TestDeleteGoal:
    async def test_success(self, client, async_db, repo):
        goal = await repo.create("Test")
        res = await client.delete(f"/api/goals/{goal.id}")
        assert res.status_code == 200

    async def test_not_found(self, client):
        res = await client.delete("/api/goals/nope")
        assert res.status_code == 404


class TestGoalSnapshot:
    async def test_missing_authenticated_operator_fails_closed(self, client, async_db, repo, monkeypatch):
        goal = await repo.create("Snapshot goal")
        monkeypatch.setattr(
            "src.api.goals._require_authenticated_operator",
            lambda _request: (_ for _ in ()).throw(
                HTTPException(
                    status_code=401,
                    detail={"code": "authentication_required"},
                )
            ),
        )

        response = await client.post(
            f"/api/goals/{goal.id}/snapshot",
            json={"expected_revision": 1},
        )

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "authentication_required"

    async def test_body_cannot_supply_actor_identity(self, client, async_db, repo):
        goal = await repo.create("Snapshot goal")

        response = await client.post(
            f"/api/goals/{goal.id}/snapshot",
            json={"expected_revision": 1, "operator_principal_id": "operator:forged"},
        )

        assert response.status_code == 422

    async def test_stale_revision_is_rejected_before_service(self, client, async_db, repo):
        goal = await repo.create("Snapshot goal")
        await repo.update(goal.id, title="Changed", expected_revision=1)

        with patch("src.api.goals.GoalSnapshotToFileService") as service_cls:
            response = await client.post(
                f"/api/goals/{goal.id}/snapshot",
                json={"expected_revision": 1},
            )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "stale_goal_revision"
        service_cls.assert_not_called()

    async def test_unsafe_output_path_is_rejected_before_service(self, client, async_db, repo):
        goal = await repo.create("Snapshot goal")

        with patch("src.api.goals.GoalSnapshotToFileService") as service_cls:
            response = await client.post(
                f"/api/goals/{goal.id}/snapshot",
                json={"expected_revision": 1, "file_path": "../outside.md"},
            )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_goal_snapshot_request"
        service_cls.assert_not_called()

    async def test_successful_run_returns_separate_receipts_and_fixed_service_identity(
        self, client, async_db, repo
    ):
        goal = await repo.create("Snapshot goal")
        result = GoalSnapshotToFileResult(
            goal_id=goal.id,
            goal_revision=1,
            file_path="goal-snapshots/output.md",
            execution_status="succeeded",
            verification="passed",
            learning="no_learning",
            job_id="job-snapshot",
            durable_status="succeeded",
            artifact_ref="artifact-1",
            content_sha256="digest",
            output_exists=True,
            workspace_contained=True,
            goal_id_read_back=True,
            reason="goal_snapshot_executed_and_verified",
        )
        service = SimpleNamespace(run=AsyncMock(return_value=result))
        with patch("src.api.goals.GoalSnapshotToFileService", return_value=service) as service_cls:
            response = await client.post(
                f"/api/goals/{goal.id}/snapshot",
                json={"expected_revision": 1},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["execution_status"] == "succeeded"
        assert payload["verification"] == "passed"
        assert payload["learning"] == "no_learning"
        assert payload["artifact_ref"] == "artifact-1"
        assert payload["operator_receipt"]["delegated_service_id"] == GOAL_SNAPSHOT_SERVICE_ID
        assert payload["operator_receipt"]["principal_id"] == "operator:single"
        service_cls.assert_called_once()
        request = service.run.await_args.args[0]
        assert request.owner_principal_id == GOAL_SNAPSHOT_SERVICE_ID
        assert request.session_id == "test-auth-bypass"

    async def test_blocked_result_stays_visible_and_does_not_claim_verification(
        self, client, async_db, repo
    ):
        goal = await repo.create("Snapshot goal")
        result = GoalSnapshotToFileResult(
            goal_id=goal.id,
            goal_revision=1,
            file_path="goal-snapshots/output.md",
            execution_status="blocked",
            verification="unknown",
            learning="no_learning",
            durable_status="blocked",
            reason="workflow_unavailable",
        )
        with patch(
            "src.api.goals.GoalSnapshotToFileService",
            return_value=SimpleNamespace(run=AsyncMock(return_value=result)),
        ):
            response = await client.post(
                f"/api/goals/{goal.id}/snapshot",
                json={"expected_revision": 1},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["execution_status"] == "blocked"
        assert payload["verification"] == "unknown"
        assert payload["learning"] == "no_learning"
        assert payload["reason"] == "workflow_unavailable"

    async def test_audit_persistence_failure_is_visible_as_degraded(self, client, async_db, repo):
        goal = await repo.create("Snapshot goal")
        result = GoalSnapshotToFileResult(
            goal_id=goal.id,
            goal_revision=1,
            file_path="goal-snapshots/output.md",
            execution_status="blocked",
            verification="unknown",
            learning="no_learning",
            durable_status="blocked",
            reason="workflow_unavailable",
        )
        service = SimpleNamespace(run=AsyncMock(return_value=result))
        with (
            patch("src.api.goals.GoalSnapshotToFileService", return_value=service),
            patch.object(
                audit_repository,
                "log_event",
                new=AsyncMock(side_effect=RuntimeError("audit store unavailable")),
            ),
        ):
            response = await client.post(
                f"/api/goals/{goal.id}/snapshot",
                json={"expected_revision": 1},
            )

        assert response.status_code == 503
        payload = response.json()
        assert payload["execution_status"] == "blocked"
        assert payload["audit_receipt"] == {
            "status": "degraded",
            "reason": "audit_persistence_failed",
        }
