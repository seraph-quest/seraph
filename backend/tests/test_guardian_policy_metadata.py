"""Authenticated Goal tree policy metadata over real private file SQLite."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func

from config.settings import settings
from src.api.goals import put_guardian_policy
from src.auth.service import _verify_secret_sync
from src.db.models import Goal, GuardianOpportunity, WorkflowRunState
from tests.test_guardian_opportunity_policy import setup_policy
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, _goal, SESSION
from tests.test_operator_identity import enroll, login, recover, HEADERS, PASSWORD


@pytest.fixture
async def metadata_client(isolated_runtime, app, monkeypatch):
    sessions, _ = isolated_runtime
    async with sessions() as db:
        monkeypatch.setattr(type(db), "bind", property(lambda session: session.get_bind()), raising=False)
    monkeypatch.setattr("src.goals.repository.get_session", sessions)
    monkeypatch.setattr(settings, "operator_auth_secret", PASSWORD)
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", HEADERS["origin"])
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    # Preserve actual password verification without the restricted runner's
    # executor shutdown path; authentication/session/recovery SQL remain real.
    async def verify_secret(value):
        return _verify_secret_sync(value)
    monkeypatch.setattr("src.api.auth.verify_secret", verify_secret)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set(settings.operator_auth_cookie_name, "m6-provider-free-root")
        yield client


def flatten(tree):
    return {node["id"]: node for root in tree for node in [root, *flatten(root["children"]).values()]}


@pytest.mark.parametrize("state", ["enabled", "null", "edited", "expired"])
async def test_tree_list_policy_parity_after_canonical_save(isolated_runtime, metadata_client, monkeypatch, state):
    sessions, goal, _, request, body = await setup_policy(isolated_runtime)
    if state != "null":
        saved = await put_guardian_policy(goal.id, body, request)
    async with sessions() as db:
        stored = await db.get(Goal, goal.id)
        if state == "edited":
            stored.revision += 1
        child = _goal(str(uuid4()), "Nested policy metadata")
        child.parent_id = goal.id
        db.add_all([stored, child])
    if state == "expired":
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        monkeypatch.setattr("src.guardian.opportunities.now", lambda: later)
    tree_response = await metadata_client.get("/api/goals/tree")
    list_response = await metadata_client.get("/api/goals")
    assert tree_response.status_code == list_response.status_code == 200
    tree, listed = flatten(tree_response.json()), {row["id"]: row for row in list_response.json()}
    assert set(tree) == set(listed) == {goal.id, child.id}
    for identifier in tree:
        for field in ("guardian_policy", "guardian_policy_revision", "guardian_assessment_state", "revision"):
            assert tree[identifier][field] == listed[identifier][field]
    expected = "disabled" if state == "null" else "enabled" if state == "enabled" else "goal_review_required"
    assert tree[goal.id]["guardian_assessment_state"] == expected
    assert tree[goal.id]["guardian_policy_revision"] == (0 if state == "null" else 1)
    if state != "null":
        assert tree[goal.id]["guardian_policy"]["goal_revision"] == saved["goal_revision"] == 1
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(GuardianOpportunity))).scalar() == 0
        assert (await db.execute(select(func.count()).select_from(WorkflowRunState))).scalar() == 0


async def test_tree_and_list_recovered_policy_never_restore_authority(isolated_runtime, metadata_client):
    sessions, goal, _, request, body = await setup_policy(isolated_runtime)
    saved = await put_guardian_policy(goal.id, body, request)
    enrollment = await enroll(metadata_client)
    logout = await metadata_client.post("/api/auth/logout", headers=HEADERS)
    assert logout.status_code == 204
    current = await login(metadata_client, enrollment["recovery_code"])
    assert current["session_id"] != SESSION
    await recover(metadata_client, [{"kind": "goal", "record_id": goal.id}])
    tree_response = await metadata_client.get("/api/goals/tree")
    list_response = await metadata_client.get("/api/goals")
    assert tree_response.status_code == list_response.status_code == 200
    tree, listed = tree_response.json()[0], list_response.json()[0]
    for row in (tree, listed):
        assert row["id"] == goal.id
        assert row["guardian_policy"] == saved["guardian_policy"]
        assert row["guardian_policy_revision"] == 1
        assert row["guardian_assessment_state"] == "goal_review_required"
        assert row["ownership_access"] == "recovered_read_only"
        assert row["proactive_enabled"] is False and row["admission_budget"] is None
    async with sessions() as db:
        original = await db.get(Goal, goal.id)
        assert original.owner_session_id == SESSION and original.proactive_enabled
        assert original.admission_budget_json is not None


async def test_tree_and_list_exclude_foreign_principal_and_root(isolated_runtime, metadata_client):
    sessions, _ = isolated_runtime
    own = _goal(str(uuid4()), "Own")
    foreign = _goal(str(uuid4()), "Foreign principal")
    foreign.owner_principal_id = "operator:foreign"
    other_root = _goal(str(uuid4()), "Unselected Root")
    other_root.owner_session_id = "other-root"
    async with sessions() as db:
        db.add_all([own, foreign, other_root])
    for path in ("/api/goals", "/api/goals/tree"):
        response = await metadata_client.get(path)
        assert response.status_code == 200
        assert [row["id"] for row in response.json()] == [own.id]
    metadata_client.cookies.clear()
    for path in ("/api/goals", "/api/goals/tree"):
        assert (await metadata_client.get(path)).status_code == 401
