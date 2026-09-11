"""Tests for goals HTTP endpoints (src/api/goals.py)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.api.goals import (
    GOAL_SNAPSHOT_MANUAL_BUDGET_BOUNDARY,
    GOAL_SNAPSHOT_SERVICE_ID,
)
from src.api.goals import inspect_goal_loop, propose_goal_loop_candidate
from src.audit.repository import audit_repository
from src.guardian.goal_snapshot_to_file import GoalSnapshotToFileResult
from src.goals.contracts import GoalCandidateRequest
from src.goals.repository import GoalRepository
from src.auth.service import AuthenticatedOperator
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal


@pytest.fixture
def repo():
    return GoalRepository()


class TestCreateGoal:
    async def test_success(self, client, async_db, repo):
        res = await client.post("/api/goals", json={
            "title": "Learn Python",
            "level": "daily",
            "domain": "growth",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["title"] == "Learn Python"
        assert "id" in data
        assert data["owner_principal_id"] == "operator:test-bypass"
        assert data["owner_session_id"] == "test-auth-bypass"
        persisted = await repo.get(data["id"])
        assert persisted.owner_principal_id == "operator:test-bypass"
        assert persisted.owner_session_id == "test-auth-bypass"

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
        owner = {"owner_principal_id": "operator:test-bypass", "owner_session_id": "test-auth-bypass"}
        await repo.create("A", level="daily", domain="health", **owner)
        await repo.create("B", level="weekly", domain="growth", **owner)
        res = await client.get("/api/goals?level=daily")
        assert res.status_code == 200
        goals = res.json()
        assert len(goals) == 1
        assert goals[0]["level"] == "daily"

    async def test_only_returns_current_owner_and_excludes_legacy_rows(
        self, client, async_db, repo, monkeypatch
    ):
        await repo.create(
            "Mine",
            owner_principal_id="operator:test-bypass",
            owner_session_id="test-auth-bypass",
        )
        await repo.create(
            "Other operator",
            owner_principal_id="operator:other",
            owner_session_id="session-other",
        )
        await repo.create("Legacy row")

        response = await client.get("/api/goals")

        assert response.status_code == 200
        assert [goal["title"] for goal in response.json()] == ["Mine"]

    async def test_requires_authenticated_operator(self, client, monkeypatch):
        def reject(_request):
            raise HTTPException(status_code=401, detail={"code": "authentication_required"})

        monkeypatch.setattr("src.api.goals._require_authenticated_operator", reject)

        response = await client.get("/api/goals")

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "authentication_required"


class TestGetTree:
    async def test_returns_tree(self, client, async_db, repo):
        owner = {"owner_principal_id": "operator:test-bypass", "owner_session_id": "test-auth-bypass"}
        parent = await repo.create("Vision", level="vision", **owner)
        await repo.create("Annual", level="annual", parent_id=parent.id, **owner)
        res = await client.get("/api/goals/tree")
        assert res.status_code == 200
        tree = res.json()
        assert len(tree) == 1
        assert len(tree[0]["children"]) == 1

    async def test_tree_is_owner_scoped_and_excludes_legacy_rows(self, client, async_db, repo):
        owner = {"owner_principal_id": "operator:test-bypass", "owner_session_id": "test-auth-bypass"}
        mine = await repo.create("Mine", **owner)
        await repo.create("Mine child", parent_id=mine.id, **owner)
        await repo.create(
            "Other operator",
            owner_principal_id="operator:other",
            owner_session_id="session-other",
        )
        await repo.create("Legacy row")

        response = await client.get("/api/goals/tree")

        assert response.status_code == 200
        assert [node["title"] for node in response.json()] == ["Mine"]
        assert [node["title"] for node in response.json()[0]["children"]] == ["Mine child"]

    async def test_requires_authenticated_operator(self, client, monkeypatch):
        def reject(_request):
            raise HTTPException(status_code=401, detail={"code": "authentication_required"})

        monkeypatch.setattr("src.api.goals._require_authenticated_operator", reject)

        response = await client.get("/api/goals/tree")

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "authentication_required"


class TestGetDashboard:
    async def test_returns_dashboard(self, client, async_db, repo):
        await repo.create(
            "A",
            domain="health",
            owner_principal_id="operator:test-bypass",
            owner_session_id="test-auth-bypass",
        )
        res = await client.get("/api/goals/dashboard")
        assert res.status_code == 200
        data = res.json()
        assert data["total_count"] == 1
        assert "health" in data["domains"]

    async def test_dashboard_is_owner_scoped_and_excludes_legacy_rows(self, client, async_db, repo):
        await repo.create(
            "Mine",
            domain="health",
            owner_principal_id="operator:test-bypass",
            owner_session_id="test-auth-bypass",
        )
        await repo.create(
            "Other operator",
            domain="growth",
            owner_principal_id="operator:other",
            owner_session_id="session-other",
        )
        await repo.create("Legacy row", domain="productivity")

        response = await client.get("/api/goals/dashboard")

        assert response.status_code == 200
        assert response.json()["total_count"] == 1
        assert set(response.json()["domains"]) == {"health"}

    async def test_requires_authenticated_operator(self, client, monkeypatch):
        def reject(_request):
            raise HTTPException(status_code=401, detail={"code": "authentication_required"})

        monkeypatch.setattr("src.api.goals._require_authenticated_operator", reject)

        response = await client.get("/api/goals/dashboard")

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "authentication_required"


class TestUpdateGoal:
    async def test_success(self, client, async_db, repo):
        goal = await _owned_goal(repo)
        res = await client.patch(f"/api/goals/{goal.id}", json={"title": "Updated"})
        assert res.status_code == 200
        updated = await repo.get(goal.id)
        assert updated.title == "Updated"

    async def test_wrong_owner_is_rejected_before_update(self, client, async_db, repo, monkeypatch):
        goal = await _owned_goal(repo)
        monkeypatch.setattr(
            "src.api.goals._require_authenticated_operator",
            lambda _request: _operator("operator:other", "session-other"),
        )
        with patch.object(
            repo,
            "update",
            new=AsyncMock(side_effect=AssertionError("update bypassed")),
        ):
            res = await client.patch(f"/api/goals/{goal.id}", json={"title": "Hijacked"})
        assert res.status_code == 403
        assert res.json()["detail"]["code"] == "goal_owner_mismatch"
        assert (await repo.get(goal.id)).title == "Snapshot goal"

    async def test_ownerless_legacy_goal_is_rejected_before_update(self, client, async_db, repo):
        goal = await repo.create("Legacy goal")
        with patch.object(
            repo,
            "update",
            new=AsyncMock(side_effect=AssertionError("legacy goal claimed")),
        ):
            res = await client.patch(
                f"/api/goals/{goal.id}",
                json={"title": "Claimed", "proactive_enabled": True},
            )
        assert res.status_code == 403
        assert res.json()["detail"]["code"] == "goal_owner_unbound"
        current = await repo.get(goal.id)
        assert current.title == "Legacy goal"
        assert current.owner_principal_id is None
        assert current.owner_session_id is None

    async def test_missing_operator_is_rejected_before_update(
        self, client, async_db, repo, monkeypatch
    ):
        goal = await _owned_goal(repo)

        def reject(_request):
            raise HTTPException(status_code=401, detail={"code": "authentication_required"})

        monkeypatch.setattr("src.api.goals._require_authenticated_operator", reject)
        with patch.object(
            repo,
            "update",
            new=AsyncMock(side_effect=AssertionError("unauthenticated update")),
        ):
            res = await client.patch(f"/api/goals/{goal.id}", json={"title": "Nope"})
        assert res.status_code == 401
        assert res.json()["detail"]["code"] == "authentication_required"

    async def test_not_found(self, client):
        res = await client.patch("/api/goals/nope", json={"title": "X"})
        assert res.status_code == 404


class TestDeleteGoal:
    async def test_success(self, client, async_db, repo):
        goal = await _owned_goal(repo)
        child = await repo.create(
            "Child",
            parent_id=goal.id,
            owner_principal_id=goal.owner_principal_id,
            owner_session_id=goal.owner_session_id,
        )
        res = await client.delete(f"/api/goals/{goal.id}")
        assert res.status_code == 200
        assert await repo.get(goal.id) is None
        assert await repo.get(child.id) is None

    async def test_wrong_owner_is_rejected_before_delete(self, client, async_db, repo, monkeypatch):
        goal = await _owned_goal(repo)
        monkeypatch.setattr(
            "src.api.goals._require_authenticated_operator",
            lambda _request: _operator("operator:other", "session-other"),
        )
        with patch.object(
            repo,
            "delete",
            new=AsyncMock(side_effect=AssertionError("delete bypassed")),
        ):
            res = await client.delete(f"/api/goals/{goal.id}")
        assert res.status_code == 403
        assert res.json()["detail"]["code"] == "goal_owner_mismatch"
        assert await repo.get(goal.id) is not None

    async def test_ownerless_legacy_goal_is_rejected_before_delete(self, client, async_db, repo):
        goal = await repo.create("Legacy goal")
        with patch.object(
            repo,
            "delete",
            new=AsyncMock(side_effect=AssertionError("legacy goal deleted")),
        ):
            res = await client.delete(f"/api/goals/{goal.id}")
        assert res.status_code == 403
        assert res.json()["detail"]["code"] == "goal_owner_unbound"
        assert await repo.get(goal.id) is not None

    async def test_missing_operator_is_rejected_before_delete(
        self, client, async_db, repo, monkeypatch
    ):
        goal = await _owned_goal(repo)

        def reject(_request):
            raise HTTPException(status_code=401, detail={"code": "authentication_required"})

        monkeypatch.setattr("src.api.goals._require_authenticated_operator", reject)
        with patch.object(
            repo,
            "delete",
            new=AsyncMock(side_effect=AssertionError("unauthenticated delete")),
        ):
            res = await client.delete(f"/api/goals/{goal.id}")
        assert res.status_code == 401
        assert res.json()["detail"]["code"] == "authentication_required"
        assert await repo.get(goal.id) is not None

    async def test_not_found(self, client):
        res = await client.delete("/api/goals/nope")
        assert res.status_code == 404


class TestPublicGoalLoopOwnership:
    async def test_unbound_goal_is_rejected_before_loop_receipt_read(self, async_db, repo):
        goal = await repo.create("Unbound goal")
        with patch("src.api.goals.list_goal_loop_receipts", new=AsyncMock(side_effect=AssertionError("read bypassed"))):
            with pytest.raises(HTTPException) as exc:
                await inspect_goal_loop(goal.id, SimpleNamespace(state=SimpleNamespace(operator=_operator("operator:test-bypass", "test-auth-bypass"))))
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "goal_owner_unbound"

    async def test_wrong_owner_is_rejected_before_candidate_mutation(self, async_db, repo):
        owner = _operator("operator:owner", "session-owner")
        goal = await repo.create(
            "Owned goal",
            owner_principal_id=owner.principal.principal_id,
            owner_session_id=owner.session_id,
        )
        with patch("src.api.goals.propose_goal_candidate", new=AsyncMock(side_effect=AssertionError("mutation bypassed"))):
            with pytest.raises(HTTPException) as exc:
                await propose_goal_loop_candidate(
                    goal.id,
                    GoalCandidateRequest(capability_id="workflow.goal-snapshot-to-file"),
                    SimpleNamespace(state=SimpleNamespace(operator=_operator("operator:other", "session-other"))),
                )
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "goal_owner_mismatch"


def _operator(principal_id: str, session_id: str) -> AuthenticatedOperator:
    principal = TrustPrincipal(
        principal_id=principal_id,
        principal_type=PrincipalType.OPERATOR,
        authenticated=True,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id=session_id,
        operator_session_id=session_id,
    )
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    return AuthenticatedOperator(
        session_id=session_id,
        principal=principal,
        idle_expires_at=expires,
        absolute_expires_at=expires,
    )


class TestGoalSnapshot:
    async def test_missing_authenticated_operator_fails_closed(self, client, async_db, repo, monkeypatch):
        goal = await _owned_goal(repo)
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
        goal = await _owned_goal(repo)

        response = await client.post(
            f"/api/goals/{goal.id}/snapshot",
            json={"expected_revision": 1, "operator_principal_id": "operator:forged"},
        )

        assert response.status_code == 422

    async def test_stale_revision_is_rejected_before_service(self, client, async_db, repo):
        goal = await _owned_goal(repo)
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
        goal = await _owned_goal(repo)

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
        goal = await _owned_goal(repo)
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
        assert payload["operator_receipt"]["budget_boundary"] == GOAL_SNAPSHOT_MANUAL_BUDGET_BOUNDARY
        assert payload["operator_receipt"]["principal_id"] == "operator:test-bypass"
        service_cls.assert_called_once()
        request = service.run.await_args.args[0]
        assert request.owner_principal_id == GOAL_SNAPSHOT_SERVICE_ID
        assert request.session_id == "test-auth-bypass"

    async def test_blocked_result_stays_visible_and_does_not_claim_verification(
        self, client, async_db, repo
    ):
        goal = await _owned_goal(repo)
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
        goal = await _owned_goal(repo)
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

    async def test_wrong_owner_is_rejected_before_snapshot_service(
        self, client, async_db, repo, monkeypatch
    ):
        goal = await _owned_goal(repo)
        monkeypatch.setattr(
            "src.api.goals._require_authenticated_operator",
            lambda _request: _operator("operator:other", "session-other"),
        )
        with patch("src.api.goals.GoalSnapshotToFileService") as service_cls:
            response = await client.post(
                f"/api/goals/{goal.id}/snapshot",
                json={"expected_revision": 1},
            )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "goal_owner_mismatch"
        service_cls.assert_not_called()


async def _owned_goal(repo: GoalRepository):
    return await repo.create(
        "Snapshot goal",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
    )
