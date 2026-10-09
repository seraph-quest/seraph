"""Real Goal repository writes; seeded historical jobs are classification cases.

These tests do not certify native execution or old dispatcher quiescence.
"""
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, text

from config.settings import settings
from src.api.goals import router
from src.auth.middleware import OperatorAuthMiddleware
from src.auth.service import create_session
from src.db.models import Goal, WorkflowRunState
from src.goals.repository import goal_repository
from src.goals.source_closure import GoalSourceClosureBlocked


async def _row_bytes(db, row_id):
    return tuple((await db.execute(text(
        "SELECT * FROM workflow_run_states WHERE id=:id"), {"id": row_id})).one())


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("schema", [1, 0, -1])
async def test_historical_absent_lease_delete_preserves_entire_job(async_db, schema):
    goal = await goal_repository.create("Historical private Goal")
    row = WorkflowRunState(run_identity="historic", root_run_identity="historic",
        workflow_name="legacy", goal_id=goal.id, status="succeeded",
        record_schema_version=schema, finished_at=datetime.now(timezone.utc),
        metadata_json='{"orchestration_v2":{"lease":{}},"private":"kept"}',
        checkpoint_context_json="private checkpoint", checkpoint_receipts_json='[{"old":"journal"}]',
        fencing_token=8, revision=9)
    async with async_db() as db:
        db.add(row)
        await db.flush()
        before = await _row_bytes(db, row.id)
    assert await goal_repository.delete(goal.id)
    async with async_db() as db:
        assert await db.get(Goal, goal.id) is None
        assert await _row_bytes(db, row.id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("schema", [1, 0, -1])
async def test_finished_expired_metadata_lease_is_not_quiescence(async_db, schema):
    goal = await goal_repository.create("Retained recovery Goal")
    row = WorkflowRunState(run_identity="historic", root_run_identity="historic",
        workflow_name="legacy", goal_id=goal.id, status="succeeded", record_schema_version=schema,
        finished_at=datetime.now(timezone.utc), metadata_json=
        '{"orchestration_v2":{"lease":{"owner":"old","expires_at":"2000-01-01T00:00:00Z"}}}')
    async with async_db() as db:
        db.add(row)
        await db.flush()
        before = await _row_bytes(db, row.id)
    with pytest.raises(GoalSourceClosureBlocked, match="goal_legacy_dispatch_quiescence_unproven"):
        await goal_repository.delete(goal.id)
    async with async_db() as db:
        assert await db.get(Goal, goal.id) is not None
        assert await _row_bytes(db, row.id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_large_terminal_native_history_is_not_portability_deletion_denial(async_db):
    goal = await goal_repository.create("Native historical private Goal")
    rows = [WorkflowRunState(run_identity=f"native-{i}", root_run_identity=f"native-{i}",
        workflow_name="native", goal_id=goal.id, job_kind="runtime_service_memory_v1",
        status="succeeded", finished_at=datetime.now(timezone.utc),
        metadata_json="p"*1048576 if i == 0 else None) for i in range(130)]
    async with async_db() as db:
        db.add_all(rows)
        await db.flush()
        before = [await _row_bytes(db, row.id) for row in rows]
    assert await goal_repository.delete(goal.id)
    async with async_db() as db:
        assert [await _row_bytes(db, row.id) for row in rows] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_overbound_legacy_metadata_has_no_private_body_select(async_db):
    goal = await goal_repository.create("Uncertified recovery Goal")
    async with async_db() as db:
        row = WorkflowRunState(run_identity="overbound", root_run_identity="overbound",
            workflow_name="legacy", goal_id=goal.id, status="succeeded", record_schema_version=0,
            metadata_json="x"*1048576, finished_at=datetime.now(timezone.utc))
        db.add(row)
    # Observe all original sessions' connection statements, including the
    # actual Goal repository writer, rather than an imitation classifier.
    async with async_db() as db:
        engine = (await db.connection()).sync_connection.engine
    statements = []
    def observe(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(GoalSourceClosureBlocked, match="goal_legacy_dispatch_quiescence_unproven"):
            await goal_repository.delete(goal.id)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert any("octet_length(w.metadata_json)" in item for item in statements)
    assert not any("SELECT metadata_json FROM" in item for item in statements)
    assert not any("UPDATE workflow_run_states" in item for item in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_original_live_job_is_never_bulk_cancelled(async_db):
    goal = await goal_repository.create("Original admitted job")
    row = WorkflowRunState(run_identity="original", root_run_identity="original", workflow_name="original",
        goal_id=goal.id, job_kind="unverified-source", status="running", lease_owner="original-owner",
        metadata_json='{"private":"kept"}', attempt_count=1, fencing_token=4,
        checkpoint_receipts_json='[{"intent":"unsettled"}]')
    async with async_db() as db:
        db.add(row)
        await db.flush()
        before = await _row_bytes(db, row.id)
    with pytest.raises(GoalSourceClosureBlocked, match="goal_live_source_unproven"):
        await goal_repository.delete(goal.id)
    async with async_db() as db:
        assert await db.get(Goal, goal.id) is not None
        assert await _row_bytes(db, row.id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_authenticated_goal_delete_exposes_source_block_and_preserves_job(async_db, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-goal-closure-proof")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    token, operator = await create_session()
    goal = await goal_repository.create("Authenticated original Goal",
        owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    row = WorkflowRunState(run_identity="original-http", root_run_identity="original-http",
        workflow_name="original", goal_id=goal.id, status="running", lease_owner="physical-owner",
        checkpoint_receipts_json='[{"intent":"retained"}]')
    async with async_db() as db:
        db.add(row)
        await db.flush()
        before = await _row_bytes(db, row.id)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        cookies={settings.operator_auth_cookie_name: token}, headers={"Origin": "http://localhost:3001"}) as client:
        response = await client.delete(f"/api/goals/{goal.id}")
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "goal_live_source_unproven"
    async with async_db() as db:
        assert await db.get(Goal, goal.id) is not None
        assert await _row_bytes(db, row.id) == before
