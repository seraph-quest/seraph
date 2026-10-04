"""Real authenticated API/SQLite continuity with provider-free negative proof."""
from datetime import datetime, timedelta, timezone
import json
from contextlib import asynccontextmanager
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from config.settings import settings
from src.api.auth import _reset_login_throttle_for_tests, _continuity_cookie_name
from src.auth.service import authenticate_token, authenticate_session, AuthFailure
from src.db.models import (Goal, OperatorSession, OperatorIdentity, OperatorContinuityCredential,
    OperatorRecoveryJournal, GuardianRoutine, Memory, MemoryTombstone, WorkBoardTask,
    WorkBoardAttempt, WorkBoardStatus, ApprovalRequest)

HEADERS = {"origin": "http://localhost:3001"}
PASSWORD = "identity-only-test-password"


@pytest.fixture(autouse=True)
def auth(monkeypatch):
    _reset_login_throttle_for_tests()
    monkeypatch.setattr(settings, "operator_auth_secret", PASSWORD)
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", HEADERS["origin"])
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    yield
    _reset_login_throttle_for_tests()


async def login(client, code=None):
    body = {"password": PASSWORD}
    if code:
        body["recovery_code"] = code
    response = await client.post("/api/auth/login", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


async def enroll(client):
    response = await client.post("/api/auth/ownership/enroll", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert "Max-Age=31536000" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def recover(client, selections, key="recover-one"):
    preview = await client.post("/api/auth/ownership/recovery/preview", headers=HEADERS, json={"selections": selections})
    assert preview.status_code == 200, preview.text
    body = {"selections": selections, "preview_digest": preview.json()["preview_digest"],
            "idempotency_key": key, "acknowledge_read_only": True}
    confirmed = await client.post("/api/auth/ownership/recovery/confirm", headers=HEADERS, json=body)
    assert confirmed.status_code == 200, confirmed.text
    again = await client.post("/api/auth/ownership/recovery/confirm", headers=HEADERS, json=body)
    assert again.json() == confirmed.json()
    return confirmed.json(), body


async def goal(client):
    response = await client.post("/api/goals", headers=HEADERS, json={"title": "Stable original", "level": "daily", "domain": "productivity"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_verified_reentry_original_ids_current_authority_and_rollback(client, async_db, app):
    first = await login(client)
    old_token = client.cookies.get(settings.operator_auth_cookie_name)
    enrollment = await enroll(client)
    first_cookie = client.cookies.get(_continuity_cookie_name())
    g = await goal(client)
    task_response = await client.post("/api/work-board/tasks", headers=HEADERS, json={
        "title": "Historical intent", "body": "Never automatically replay", "goal_id": g["id"],
        "goal_revision": 1, "status": "triage", "idempotency_key": "identity-task"})
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()["task"]
    memory_response = await client.post("/api/memory/corrections", headers=HEADERS, json={
        "content": "Reviewed ownership fact", "kind": "fact", "reason": "Operator review", "confidence": 1.0})
    assert memory_response.status_code == 200, memory_response.text
    memory_id = memory_response.json()["memory"]["id"]
    async with async_db() as db:
        m = await db.get(Memory, memory_id)
        m.last_confirmed_at = datetime.now(timezone.utc)
        db.add(GuardianRoutine(id="identity-paused-routine", owner_principal_id=first["principal_id"],
            owner_session_id=first["session_id"], name="Paused retained routine", state="paused"))
        db.add(ApprovalRequest(session_id=None, operator_session_id=first["session_id"],
            owner_principal_id=first["principal_id"], tool_name="sensitive", fingerprint="old-authority", status="approved"))
    refreshed = await client.post("/api/auth/refresh", headers=HEADERS)
    assert refreshed.status_code == 200
    assert refreshed.json()["session_id"] == first["session_id"]
    await client.post("/api/auth/logout", headers=HEADERS)
    with pytest.raises(AuthFailure):
        await authenticate_token(old_token)
    with pytest.raises(AuthFailure):
        await authenticate_session(first["session_id"])
    second = await login(client)
    assert second["session_id"] != first["session_id"]
    assert second["operator_identity_id"] == enrollment["operator_identity_id"]
    assert client.cookies.get(_continuity_cookie_name()) != first_cookie
    assert g["id"] not in {item["id"] for item in (await client.get("/api/goals")).json()}
    inventory = await client.get("/api/auth/ownership/recovery")
    assert inventory.status_code == 200, inventory.text
    selections = [{"kind": kind, "record_id": identifier} for kind, identifier in (
        ("goal", g["id"]), ("task", task["task_id"]), ("memory", memory_id), ("routine", "identity-paused-routine"))]
    journal, body = await recover(client, selections)
    goals = (await client.get("/api/goals")).json()
    assert goals[0]["id"] == g["id"] and goals[0]["ownership_access"] == "recovered_read_only"
    assert (await client.get("/api/goals/tree")).json()[0]["id"] == g["id"]
    assert (await client.get("/api/goals/dashboard")).json()["total_count"] == 1
    detail = await client.get(f"/api/work-board/tasks/{task['task_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["task"]["ownership_access"] == "recovered_read_only"
    assert detail.json()["task"]["recovery_action"] is None
    assert (await client.get("/api/work-board/tasks")).json()["tasks"][0]["task_id"] == task["task_id"]
    memories = (await client.get("/api/memory/records")).json()
    assert memories["records"][0]["id"] == memory_id
    memory = await client.get(f"/api/memory/records/{memory_id}")
    assert memory.status_code == 200 and memory.json()["ownership_access"] == "recovered_read_only"
    routine = await client.get("/api/capabilities/routines/identity-paused-routine")
    assert routine.status_code == 200, routine.text
    assert routine.json()["ownership_access"] == "recovered_read_only"
    assert (await client.get("/api/capabilities/routines")).json()["routines"][0]["id"] == "identity-paused-routine"
    assert (await client.patch(f"/api/work-board/tasks/{task['task_id']}", headers=HEADERS,
                              json={"title": "forged", "expected_revision": 1})).status_code in {403, 404}
    assert (await client.patch(f"/api/goals/{g['id']}", headers=HEADERS, json={"title": "forged"})).status_code == 403
    assert (await client.post(f"/api/memory/{memory_id}/pin", headers=HEADERS, json={})).status_code == 403
    async with async_db() as db:
        assert (await db.get(Goal, g["id"])).owner_session_id == first["session_id"]
        assert (await db.get(Memory, memory_id)).source_session_id == first["session_id"]
        assert (await db.get(OperatorSession, first["session_id"])).revoked_at is not None
        approval = (await db.execute(select(ApprovalRequest).where(ApprovalRequest.fingerprint == "old-authority"))).scalar_one()
        assert approval.operator_session_id == first["session_id"]
    # A new application object represents API restart; canonical read mapping
    # lives in DB rather than process cache.
    from src.app import create_app
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test", cookies=client.cookies) as restarted:
        assert (await restarted.get("/api/goals")).json()[0]["id"] == g["id"]
    rollback = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/rollback", headers=HEADERS)
    assert rollback.status_code == 200
    assert (await client.get("/api/goals")).json() == []
    again = await client.post("/api/auth/ownership/recovery/confirm", headers=HEADERS, json=body)
    assert again.json()["state"] == "rolled_back"


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", ["idle_expires_at", "absolute_expires_at"])
async def test_expiry_requires_new_session_and_proved_data_recovery(client, async_db, expiry):
    first = await login(client)
    await enroll(client)
    g = await goal(client)
    async with async_db() as db:
        setattr(await db.get(OperatorSession, first["session_id"]), expiry, datetime.now(timezone.utc) - timedelta(seconds=1))
    assert (await client.get("/api/auth/session")).status_code == 401
    second = await login(client)
    assert first["session_id"] != second["session_id"]
    await recover(client, [{"kind": "goal", "record_id": g["id"]}])
    assert (await client.get("/api/goals")).json()[0]["id"] == g["id"]
    with pytest.raises(AuthFailure):
        await authenticate_session(first["session_id"])


@pytest.mark.asyncio
async def test_password_alone_foreign_guessed_and_forged_scopes_denied(client, app):
    await login(client)
    enrollment = await enroll(client)
    g = await goal(client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as foreign:
        outsider = await login(foreign)
        assert outsider["operator_identity_id"] is None
        assert (await foreign.post("/api/auth/ownership/recovery-code", headers=HEADERS)).status_code == 403
        assert (await foreign.get("/api/auth/ownership/recovery")).status_code == 403
        forged = await foreign.post("/api/auth/ownership/recovery/preview", headers=HEADERS, json={
            "selections": [{"kind": "goal", "record_id": g["id"]}], "identity_id": enrollment["operator_identity_id"]})
        assert forged.status_code == 422
        await enroll(foreign)
        assert (await foreign.post("/api/auth/ownership/recovery/preview", headers=HEADERS, json={
            "selections": [{"kind": "goal", "record_id": g["id"]}]})).status_code == 404
        assert (await foreign.get("/api/goals")).json() == []
    assert (await client.post("/api/auth/ownership/enroll", headers={})).status_code == 403


@pytest.mark.asyncio
async def test_recovery_code_one_time_cookie_rotation_expiry_and_revocation(client, app, async_db):
    await login(client)
    enrollment = await enroll(client)
    identity = enrollment["operator_identity_id"]
    code = enrollment["recovery_code"]
    original_cookie = client.cookies.get(_continuity_cookie_name())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as device:
        recovered = await login(device, code)
        assert recovered["operator_identity_id"] == identity
        response = await device.post("/api/auth/login", headers=HEADERS, json={"password": PASSWORD, "recovery_code": code})
        assert response.status_code == 401 and response.json()["detail"]["code"] == "ownership_proof_invalid"
        replacement = await device.post("/api/auth/ownership/recovery-code", headers=HEADERS)
        assert replacement.status_code == 200
    second = await login(client)
    assert second["operator_identity_id"] == identity
    client.cookies.set(_continuity_cookie_name(), original_cookie, domain="test.local", path="/")
    stale = await client.post("/api/auth/login", headers=HEADERS, json={"password": PASSWORD})
    assert stale.status_code == 401
    async with async_db() as db:
        proofs = (await db.execute(select(OperatorContinuityCredential).where(OperatorContinuityCredential.identity_id == identity))).scalars().all()
        assert all(code != row.token_hash and original_cookie != row.token_hash for row in proofs)
    # Identity revocation invalidates all currently bound sessions/devices,
    # whereas forgetting a device only revokes that one proof.
    _reset_login_throttle_for_tests()
    client.cookies.delete(_continuity_cookie_name())
    client.cookies.set(_continuity_cookie_name(), device.cookies.get(_continuity_cookie_name()), domain="test.local", path="/")
    await login(client)
    revoke = await client.post("/api/auth/ownership/revoke", headers=HEADERS)
    assert revoke.status_code == 204
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=device.cookies) as previous_device:
        assert (await previous_device.get("/api/auth/session")).status_code == 401


@pytest.mark.asyncio
async def test_fresh_intent_has_lineage_no_replayed_inputs_grants_or_effects(client, async_db):
    first = await login(client)
    await enroll(client)
    g = await goal(client)
    response = await client.post("/api/work-board/tasks", headers=HEADERS, json={
        "title": "Reviewed fresh intent", "goal_id": g["id"], "goal_revision": 1, "idempotency_key": "fresh-source"})
    old_task = response.json()["task"]
    await client.post("/api/auth/logout", headers=HEADERS)
    current = await login(client)
    journal, _ = await recover(client, [{"kind": "task", "record_id": old_task["task_id"]}])
    fresh = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/fresh-work", headers=HEADERS)
    assert fresh.status_code == 200, fresh.text
    new_task_id = fresh.json()["fresh_work"]["tasks"][0]["task_id"]
    detail = (await client.get(f"/api/work-board/tasks/{new_task_id}")).json()["task"]
    assert detail["owner_session_id"] == current["session_id"]
    assert detail["status"] == "triage"
    assert detail["capability_id"] is None and detail["input_artifact_id"] is None
    assert old_task["task_id"] in detail["body"]
    repeated = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/fresh-work", headers=HEADERS)
    assert repeated.json() == fresh.json()
    async with async_db() as db:
        assert (await db.get(OperatorSession, first["session_id"])).revoked_at is not None
        original = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == old_task["task_id"]))).scalar_one()
        assert original.owner_session_id == first["session_id"] and original.status == WorkBoardStatus.triage


@pytest.mark.asyncio
async def test_selected_input_artifact_is_private_metadata_not_reusable_authority(client, async_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "workspace"))
    first = await login(client)
    await enroll(client)
    g = await goal(client)
    payload = {"schema_version": 1, "start_url": "https://example.com/", "allowed_hosts": ["example.com"],
               "approved_url_prefixes": ["https://example.com/"], "actions": [{"kind": "navigate", "url": "https://example.com/",
               "expected_checks": [{"kind": "url_host", "value": "example.com"}]}],
               "final_expected_checks": [{"kind": "url_host", "value": "example.com"}]}
    response = await client.post("/api/work-board/input-artifacts", headers=HEADERS, json={
        "schema_version": 1, "capability_id": "browser.public-task.v1", "goal_id": g["id"], "goal_revision": 1,
        "input": payload, "idempotency_key": "identity-input"})
    assert response.status_code == 200, response.text
    artifact = response.json()
    await client.post("/api/auth/logout", headers=HEADERS)
    await login(client)
    assert (await client.get(f"/api/work-board/input-artifacts/{artifact['artifact_id']}")).status_code in {403, 404}
    await recover(client, [{"kind": "artifact", "record_id": artifact["artifact_id"]}])
    result = await client.get(f"/api/work-board/input-artifacts/{artifact['artifact_id']}")
    assert result.status_code == 200, result.text
    assert result.json()["artifact_id"] == artifact["artifact_id"]
    assert result.json()["ownership_access"] == "recovered_read_only"
    assert "start_url" not in result.text and "input" not in result.json()
    assert (await client.delete(f"/api/work-board/input-artifacts/{artifact['artifact_id']}", headers=HEADERS)).status_code in {403, 404, 422}


@pytest.mark.asyncio
async def test_unproved_legacy_deleted_and_uncertain_effect_records_stay_blocked(client, async_db):
    first = await login(client)
    # A legacy reverse replacement relation is not a continuity proof.
    async with async_db() as db:
        db.add(OperatorSession(token_hash="legacy-unproved-hash", replaced_by_id=first["session_id"],
            idle_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            absolute_expires_at=datetime.now(timezone.utc) + timedelta(hours=2)))
    result = await client.post("/api/auth/ownership/enroll", headers=HEADERS)
    assert result.status_code == 403 and result.json()["detail"]["code"] == "legacy_ownership_unproved"
    async with async_db() as db:
        legacy = (await db.execute(select(OperatorSession).where(OperatorSession.token_hash == "legacy-unproved-hash"))).scalar_one()
        legacy.replaced_by_id = None
    await enroll(client)
    g = await goal(client)
    response = await client.post("/api/work-board/tasks", headers=HEADERS, json={
        "title": "Uncertain intent", "goal_id": g["id"], "goal_revision": 1, "idempotency_key": "uncertain-source"})
    task_id = response.json()["task"]["task_id"]
    async with async_db() as db:
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))).scalar_one()
        task.block_kind = "unknown_effect"
        task.status = WorkBoardStatus.blocked
        memory = Memory(content="Must never revive", source_session_id=first["session_id"], last_confirmed_at=datetime.now(timezone.utc))
        db.add(memory)
        await db.flush()
        memory_id = memory.id
        db.add(MemoryTombstone(memory_id=memory_id))
    await client.post("/api/auth/logout", headers=HEADERS)
    await login(client)
    preview = await client.post("/api/auth/ownership/recovery/preview", headers=HEADERS, json={"selections": [{"kind": "memory", "record_id": memory_id}]})
    assert preview.status_code == 404 and "Must never revive" not in preview.text
    journal, _ = await recover(client, [{"kind": "task", "record_id": task_id}])
    before = (await client.get("/api/work-board/tasks")).json()
    result = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/fresh-work", headers=HEADERS)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "historical_effect_reconciliation_required"
    assert (await client.get("/api/work-board/tasks")).json() == before


@pytest.mark.asyncio
async def test_expired_cookie_device_forget_and_explicit_new_scope(client, app, async_db):
    first = await login(client)
    await enroll(client)
    private_cookie = client.cookies.get(_continuity_cookie_name())
    async with async_db() as db:
        proof = (await db.execute(select(OperatorContinuityCredential).where(OperatorContinuityCredential.kind == "cookie"))).scalar_one()
        proof.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    response = await client.post("/api/auth/login", headers=HEADERS, json={"password": PASSWORD})
    assert response.status_code == 401
    response = await client.post("/api/auth/login", headers=HEADERS, json={"password": PASSWORD, "start_new_scope": True})
    assert response.status_code == 200
    assert response.json()["operator_identity_id"] is None
    assert client.cookies.get(_continuity_cookie_name()) is None
    assert (await client.post("/api/auth/ownership/recovery-code", headers=HEADERS)).status_code == 403
    await enroll(client)
    identity = (await client.get("/api/auth/session")).json()["operator_identity_id"]
    forgotten = client.cookies.get(_continuity_cookie_name())
    response = await client.post("/api/auth/ownership/forget-device", headers=HEADERS)
    assert response.status_code == 204 and client.cookies.get(_continuity_cookie_name()) is None
    assert (await client.get("/api/auth/session")).status_code == 200
    async with async_db() as db:
        from src.auth.service import _token_hash
        proof = (await db.execute(select(OperatorContinuityCredential).where(OperatorContinuityCredential.token_hash == _token_hash(forgotten)))).scalar_one()
        assert proof.revoked_at is not None
        assert (await db.get(OperatorIdentity, identity)).revoked_at is None


@pytest.mark.asyncio
async def test_atomic_claim_failure_no_partial_scope_and_idempotency_conflict(client, async_db, monkeypatch):
    import src.db.engine as database
    first = await login(client)
    await enroll(client)
    g = await goal(client)
    await client.post("/api/auth/logout", headers=HEADERS)
    await login(client)
    selections = [{"kind": "goal", "record_id": g["id"]}]
    preview = (await client.post("/api/auth/ownership/recovery/preview", headers=HEADERS, json={"selections": selections})).json()
    body = {"selections": selections, "idempotency_key": "crash-proof", "preview_digest": preview["preview_digest"], "acknowledge_read_only": True}
    original = database.get_session
    @asynccontextmanager
    async def fail_before_commit():
        async with original() as db:
            yield db
            raise RuntimeError("injected precommit crash")
    monkeypatch.setattr(database, "get_session", fail_before_commit)
    with pytest.raises(RuntimeError, match="injected"):
        await client.post("/api/auth/ownership/recovery/confirm", headers=HEADERS, json=body)
    monkeypatch.setattr(database, "get_session", original)
    async with async_db() as db:
        assert (await db.execute(select(OperatorRecoveryJournal))).scalars().all() == []
        assert (await db.get(Goal, g["id"])).owner_session_id == first["session_id"]
    assert (await client.get("/api/goals")).json() == []
    result = await client.post("/api/auth/ownership/recovery/confirm", headers=HEADERS, json=body)
    assert result.status_code == 200
    conflict = await client.post("/api/auth/ownership/recovery/confirm", headers=HEADERS, json={**body, "preview_digest": "a" * 64})
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "recovery_idempotency_conflict"


@pytest.mark.asyncio
async def test_file_database_existing_schema_migration_and_engine_restart(client, monkeypatch, tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from sqlmodel import SQLModel
    from src.db.engine import _ensure_operator_session_columns, _ensure_operator_principals
    from unittest.mock import patch
    path = tmp_path / "persisted-owner.db"
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE operator_sessions (id VARCHAR PRIMARY KEY, token_hash VARCHAR UNIQUE, created_at DATETIME, last_seen_at DATETIME, idle_expires_at DATETIME, absolute_expires_at DATETIME, revoked_at DATETIME, replaced_by_id VARCHAR)")
        await conn.exec_driver_sql("INSERT INTO operator_sessions VALUES ('legacy-history', 'legacy-hash', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)")
        await _ensure_operator_session_columns(conn)
        await _ensure_operator_session_columns(conn)
        await _ensure_operator_principals(conn)
        await conn.run_sync(SQLModel.metadata.create_all)
        await _ensure_operator_principals(conn)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    @asynccontextmanager
    async def persisted_session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    patches = [patch(target, persisted_session) for target in (
        "src.db.engine.get_session", "src.auth.service.get_session",
        "src.goals.repository.get_session", "src.audit.repository.get_session",
    )]
    for item in patches: item.start()
    try:
        first = await login(client)
        await enroll(client)
        g = await goal(client)
        await client.post("/api/auth/logout", headers=HEADERS)
        await engine.dispose()
        engine = create_async_engine(url)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        second = await login(client)
        assert second["session_id"] != first["session_id"]
        await recover(client, [{"kind": "goal", "record_id": g["id"]}])
        await engine.dispose()
        engine = create_async_engine(url)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        assert (await client.get("/api/goals")).json()[0]["id"] == g["id"]
        async with persisted_session() as db:
            legacy = await db.get(OperatorSession, "legacy-history")
            assert legacy.operator_identity_id is None and legacy.revoked_at is not None
            assert (await db.get(Goal, g["id"])).owner_session_id == first["session_id"]
    finally:
        for item in reversed(patches): item.stop()
        await engine.dispose()
