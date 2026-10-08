"""Isolated finite public authority checks, with external contacts denied."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import socket

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from src.auth.service import AuthenticatedOperator, _principal
from src.db.models import Goal, InferenceCostReservation, OperatorIdentity, OperatorSession
from sqlmodel import select
from src.goals.contracts import GoalProgrammeAccept, GoalProgrammeControl, GoalProgrammeRequest
from src.goals.repository import GoalRepository
from src.guardian.goal_programmes import CAPABILITY_IDS, GoalProgrammeError, GoalProgrammeService


NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def deny_external_contacts(monkeypatch):
    contacts = []
    def deny(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            contacts.append(address)
            raise AssertionError("external sockets forbidden")
        return original(sock, address)
    original = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", deny)
    yield contacts
    assert contacts == []


@pytest.fixture
async def programme_setup(async_db, monkeypatch):
    monkeypatch.setattr("src.guardian.goal_programmes._policy_binding", lambda: (3, "route-digest", None))
    identity = OperatorIdentity(id="identity-owned")
    root = OperatorSession(id="root-owned", token_hash="test-token-hash", principal_id="operator:root:owned1234",
        operator_identity_id=identity.id, idle_expires_at=NOW + timedelta(hours=1), absolute_expires_at=NOW + timedelta(hours=2))
    async with async_db() as db:
        db.add(identity)
        db.add(root)
    operator = AuthenticatedOperator(session_id=root.id, principal=_principal(root.id, root.principal_id),
        idle_expires_at=root.idle_expires_at, absolute_expires_at=root.absolute_expires_at, operator_identity_id=identity.id)
    goal = await GoalRepository().create("Private goal title", description="PRIVATE_SECRET_DESCRIPTION",
        owner_principal_id=root.principal_id, owner_session_id=root.id)
    clock = [NOW]
    service = GoalProgrammeService(clock=lambda: clock[0])
    await service.start()
    request = GoalProgrammeRequest(expected_goal_revision=1, public_brief="Track public FastAPI releases", budget={"max_inference_microusd": 1000})
    yield service, operator, goal, request, clock
    await service.stop()


async def accept(service, operator, goal, request):
    preview = await service.preview(operator=operator, goal_id=goal.id, request=request)
    accepted = GoalProgrammeAccept(**request.model_dump(), review_digest=preview["review_digest"],
        public_web_acknowledged=True, local_artifacts_acknowledged=True, inference_ceiling_acknowledged=True)
    return await service.accept(operator=operator, goal_id=goal.id, request=accepted)


async def authority(service, goal, programme):
    return await service.assert_authority(goal_id=goal.id, programme_id=programme["id"],
        grant_revision=programme["grant_revision"], capability_id=CAPABILITY_IDS[0])


async def test_public_brief_review_and_private_fence(programme_setup, async_db):
    service, operator, goal, request, _ = programme_setup
    preview = await service.preview(operator=operator, goal_id=goal.id, request=request)
    assert "PRIVATE_SECRET_DESCRIPTION" not in json.dumps(preview)
    assert "Private goal title" not in json.dumps(preview)
    assert preview["preview_only"] is True
    assert preview["programme"]["artifact_prefix"].startswith("goal-programmes/")
    assert preview["programme"]["budget"]["max_outstanding_runs"] == 1
    with pytest.raises(GoalProgrammeError, match="programme_not_found"):
        await authority(service, goal, preview["programme"])
    programme = await accept(service, operator, goal, request)
    assert programme["state"] == "active"
    assert (await authority(service, goal, programme)).public_brief == request.public_brief
    async with async_db() as db:
        stored_goal = await db.get(Goal, goal.id)
        assert stored_goal.guardian_policy_json is None
        assert "PRIVATE_SECRET_DESCRIPTION" not in stored_goal.goal_programmes_json


async def test_logout_25_hours_restart_and_original_expiry(programme_setup, async_db):
    service, operator, goal, request, clock = programme_setup
    programme = await accept(service, operator, goal, request)
    async with async_db() as db:
        root = await db.get(OperatorSession, operator.session_id)
        root.revoked_at = NOW
        db.add(root)
    clock[0] += timedelta(hours=25)
    await service.stop()
    restarted = GoalProgrammeService(clock=lambda: clock[0])
    await restarted.start()
    assert (await authority(restarted, goal, programme)).expires_at == datetime.fromisoformat(programme["expires_at"])
    clock[0] = NOW + timedelta(days=7)
    with pytest.raises(GoalProgrammeError, match="programme_expired"):
        await authority(restarted, goal, programme)
    await restarted.stop()


async def test_goal_correction_and_late_adoption_fenced(programme_setup, async_db):
    service, operator, goal, request, _ = programme_setup
    programme = await accept(service, operator, goal, request)
    admitted = await authority(service, goal, programme)
    await GoalRepository().update(goal.id, description="corrected private goal", expected_revision=1)
    with pytest.raises(GoalProgrammeError, match="programme_goal_review_required"):
        await authority(service, goal, programme)
    # Holding the old typed grant cannot bypass the fresh adoption check.
    with pytest.raises(GoalProgrammeError, match="programme_goal_review_required"):
        await service.assert_authority(goal_id=goal.id, programme_id=admitted.id,
            grant_revision=admitted.grant_revision, capability_id=CAPABILITY_IDS[1])
    inspected = await service.inspect(operator=operator, goal_id=goal.id)
    assert inspected["programmes"][0]["state"] == "paused"


async def test_identity_revocation_fences_new_contact_and_adoption(programme_setup, async_db):
    service, operator, goal, request, _ = programme_setup
    programme = await accept(service, operator, goal, request)
    await authority(service, goal, programme)
    async with async_db() as db:
        identity = await db.get(OperatorIdentity, "identity-owned")
        identity.revoked_at = NOW
        db.add(identity)
    with pytest.raises(GoalProgrammeError, match="programme_identity_revoked"):
        await authority(service, goal, programme)


async def test_zero_budget_and_missing_policy_configured_blocked(programme_setup, monkeypatch):
    service, operator, goal, request, _ = programme_setup
    zero = request.model_copy(update={"budget": request.budget.model_copy(update={"max_inference_microusd": 0})})
    programme = await accept(service, operator, goal, zero)
    assert programme["state"] == "blocked"
    assert programme["reason_code"] == "programme_zero_budget"
    with pytest.raises(GoalProgrammeError, match="programme_zero_budget"):
        await authority(service, goal, programme)
    monkeypatch.setattr("src.guardian.goal_programmes._policy_binding", lambda: (3, "missing", "provider_policy_unavailable"))
    request = request.model_copy(update={"expected_grant_revision": 1})
    blocked = await accept(service, operator, goal, request)
    assert blocked["state"] == "blocked"
    assert blocked["reason_code"] == "provider_policy_unavailable"


async def test_exact_review_race_and_unknown_fields(programme_setup, monkeypatch):
    service, operator, goal, request, _ = programme_setup
    preview = await service.preview(operator=operator, goal_id=goal.id, request=request)
    changed = GoalProgrammeAccept(**{**request.model_dump(), "public_brief": "different public content"},
        review_digest=preview["review_digest"], public_web_acknowledged=True,
        local_artifacts_acknowledged=True, inference_ceiling_acknowledged=True)
    with pytest.raises(GoalProgrammeError, match="programme_review_stale"):
        await service.accept(operator=operator, goal_id=goal.id, request=changed)
    changed.public_brief = request.public_brief
    monkeypatch.setattr("src.guardian.goal_programmes._policy_binding", lambda: (4, "new-route", None))
    with pytest.raises(GoalProgrammeError, match="programme_route_changed"):
        await service.accept(operator=operator, goal_id=goal.id, request=changed)
    for extra in ("query", "url", "output_path", "private_goal_description"):
        with pytest.raises(ValueError):
            GoalProgrammeRequest.model_validate({**request.model_dump(), extra: "unexpected"})


async def test_renewal_never_extends_old_attempt(programme_setup):
    service, operator, goal, request, clock = programme_setup
    first = await accept(service, operator, goal, request)
    original = first["expires_at"]
    clock[0] += timedelta(hours=1)
    renewed = await accept(service, operator, goal, request.model_copy(update={"expected_grant_revision": 1}))
    assert renewed["grant_revision"] == 2
    with pytest.raises(GoalProgrammeError, match="programme_superseded"):
        await authority(service, goal, first)
    history = await service.inspect(operator=operator, goal_id=goal.id)
    assert history["programmes"][0]["expires_at"] == original
    assert renewed["id"] != first["id"]


async def test_pause_revoke_and_wrong_owner(programme_setup, async_db):
    service, operator, goal, request, _ = programme_setup
    programme = await accept(service, operator, goal, request)
    control = GoalProgrammeControl(expected_grant_revision=1)
    paused = await service.control(operator=operator, goal_id=goal.id, programme_id=programme["id"], request=control, action="pause")
    assert paused["state"] == "paused"
    with pytest.raises(GoalProgrammeError, match="programme_paused"):
        await authority(service, goal, programme)
    revoked = await service.control(operator=operator, goal_id=goal.id, programme_id=programme["id"], request=control, action="revoke")
    assert revoked["state"] == "revoked"
    with pytest.raises(GoalProgrammeError, match="programme_revoked"):
        await service.control(operator=operator, goal_id=goal.id, programme_id=programme["id"], request=control, action="pause")
    async with async_db() as db:
        other_identity = OperatorIdentity(id="other-identity")
        other_root = OperatorSession(id="other-root", token_hash="other-token", principal_id="operator:root:other1234",
            operator_identity_id=other_identity.id, idle_expires_at=NOW + timedelta(hours=1), absolute_expires_at=NOW + timedelta(hours=2))
        db.add(other_identity)
        db.add(other_root)
    other = replace(operator, session_id=other_root.id, principal=_principal(other_root.id, other_root.principal_id), operator_identity_id=other_identity.id)
    with pytest.raises(GoalProgrammeError, match="programme_owner_mismatch"):
        await service.control(operator=other, goal_id=goal.id, programme_id=programme["id"], request=control, action="revoke")


async def test_real_api_preview_accept_and_controls(programme_setup, app, monkeypatch):
    service, operator, goal, request, _ = programme_setup
    monkeypatch.setattr("src.guardian.goal_programmes.goal_programme_service", service)
    monkeypatch.setattr("src.api.goals._require_authenticated_operator", lambda _: operator)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://isolated.local") as client:
        base = f"/api/goals/{goal.id}/programmes"
        preview = await client.post(f"{base}/preview", json=request.model_dump(mode="json"))
        assert preview.status_code == 200, preview.text
        assert "PRIVATE_SECRET_DESCRIPTION" not in preview.text
        accepted = await client.post(f"{base}/accept", json={**request.model_dump(mode="json"),
            "review_digest": preview.json()["review_digest"], "public_web_acknowledged": True,
            "local_artifacts_acknowledged": True, "inference_ceiling_acknowledged": True})
        assert accepted.status_code == 200, accepted.text
        programme = accepted.json()
        inspected = await client.get(base)
        assert inspected.status_code == 200
        assert inspected.json()["programmes"][0]["id"] == programme["id"]
        revoked = await client.post(f"{base}/{programme['id']}/revoke", json={"expected_grant_revision": 1})
        assert revoked.status_code == 200
        assert revoked.json()["state"] == "revoked"


async def test_existing_goal_migration_rerun_preserves_closed_policy(tmp_path):
    from src.db.engine import _ensure_legacy_columns

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'goals.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("CREATE TABLE goals (id VARCHAR PRIMARY KEY, title VARCHAR, guardian_policy_json VARCHAR)")
            await connection.exec_driver_sql("INSERT INTO goals VALUES ('legacy', 'Existing', ?)", ('{"existing":"policy"}',))
            await _ensure_legacy_columns(connection)
            await _ensure_legacy_columns(connection)
            row = (await connection.exec_driver_sql("SELECT goal_programmes_json, guardian_policy_json FROM goals WHERE id='legacy'")).one()
            assert row == (None, '{"existing":"policy"}')
    finally:
        await engine.dispose()


async def test_fresh_login_explicit_recovery_only_reduces_authority(programme_setup, async_db):
    service, operator, goal, request, _ = programme_setup
    programme = await accept(service, operator, goal, request)
    async with async_db() as db:
        fresh_root = OperatorSession(id="fresh-root", token_hash="fresh-token", principal_id="operator:root:fresh1234",
            operator_identity_id="identity-owned", idle_expires_at=NOW + timedelta(hours=1), absolute_expires_at=NOW + timedelta(hours=2))
        old_root = await db.get(OperatorSession, operator.session_id)
        old_root.revoked_at = NOW
        db.add(old_root)
        db.add(fresh_root)
    fresh = replace(operator, session_id=fresh_root.id, principal=_principal(fresh_root.id, fresh_root.principal_id))
    with pytest.raises(GoalProgrammeError, match="programme_owner_recovery_required"):
        await service.control(operator=fresh, goal_id=goal.id, programme_id=programme["id"],
            request=GoalProgrammeControl(expected_grant_revision=1), action="revoke")
    with pytest.raises(GoalProgrammeError, match="goal_owner_mismatch"):
        await service.preview(operator=fresh, goal_id=goal.id, request=request.model_copy(update={"expected_grant_revision": 1}))
    revoked = await service.control(operator=fresh, goal_id=goal.id, programme_id=programme["id"],
        request=GoalProgrammeControl(expected_grant_revision=1, recover_owner_acknowledged=True), action="revoke")
    assert revoked["state"] == "revoked"
    assert revoked["expires_at"] == programme["expires_at"]
    assert revoked["grant_revision"] == programme["grant_revision"]
    with pytest.raises(GoalProgrammeError, match="programme_revoked"):
        await authority(service, goal, programme)


async def test_passive_expiry_and_stopped_service_blocked(programme_setup):
    service, operator, goal, request, clock = programme_setup
    programme = await accept(service, operator, goal, request)
    clock[0] += timedelta(days=7)
    first = await service.inspect(operator=operator, goal_id=goal.id)
    second = await service.inspect(operator=operator, goal_id=goal.id)
    assert first == second
    assert first["programmes"][0]["state"] == "review_due"
    assert first["programmes"][0]["reason_code"] == "programme_expired"
    await service.stop()
    with pytest.raises(GoalProgrammeError, match="programme_service_unavailable"):
        await authority(service, goal, programme)


async def test_strict_acknowledgments_and_scalar_types(programme_setup):
    service, operator, goal, request, _ = programme_setup
    preview = await service.preview(operator=operator, goal_id=goal.id, request=request)
    body = {**request.model_dump(), "review_digest": preview["review_digest"], "public_web_acknowledged": True,
        "local_artifacts_acknowledged": True, "inference_ceiling_acknowledged": True}
    for value in (False, 1, "true"):
        with pytest.raises(ValueError):
            GoalProgrammeAccept.model_validate({**body, "public_web_acknowledged": value})
    for value in (True, 1.5, "7"):
        with pytest.raises(ValueError):
            GoalProgrammeRequest.model_validate({**request.model_dump(), "duration_days": value})


async def test_authority_configuration_creates_no_inference_or_spend(programme_setup, async_db):
    service, operator, goal, request, _ = programme_setup
    programme = await accept(service, operator, goal, request)
    await authority(service, goal, programme)
    async with async_db() as db:
        reservations = (await db.execute(select(InferenceCostReservation))).scalars().all()
        assert reservations == []


async def test_public_brief_correction_pauses_before_new_acceptance(programme_setup):
    service, operator, goal, request, _ = programme_setup
    original = await accept(service, operator, goal, request)
    corrected = request.model_copy(update={"expected_grant_revision": 1, "public_brief": "Corrected public purpose"})
    preview = await service.preview(operator=operator, goal_id=goal.id, request=corrected)
    assert preview["preview_only"] is True
    assert preview["paused_programme_ids"] == [original["id"]]
    with pytest.raises(GoalProgrammeError, match="programme_brief_review_required"):
        await authority(service, goal, original)
    with pytest.raises(GoalProgrammeError, match="programme_not_found"):
        await authority(service, goal, preview["programme"])
