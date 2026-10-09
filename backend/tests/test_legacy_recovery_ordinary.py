"""Compatibility Goal-NULL writer and typed-owner refusal remain original."""
import json

import pytest
from sqlalchemy import select

from src.db import engine as db_engine
from src.db.models import WorkflowRunState
from src.workflows.durable_state import workflow_state_repository


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("metadata", [None, "{invalid", json.dumps({"legacy": "x" * 1_048_577})])
async def test_original_goal_null_schema_zero_lease_preserved(async_db, metadata):
    identity = "ordinary:workflow_ordinary:input:run-ordinary"
    await workflow_state_repository.create_run(run_identity=identity, workflow_name="ordinary",
        tool_name="workflow_ordinary", session_id="ordinary", run_fingerprint="input",
        arguments={}, approval_context={})
    await workflow_state_repository.finish_run(run_identity=identity, status="failed")
    async with db_engine.get_session() as db:
        row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity))).scalar_one()
        row.record_schema_version = 0
        row.metadata_json = metadata
    lease = await workflow_state_repository.acquire_or_renew_v2_lease(run_identity=identity, owner="ordinary-original-owner")
    assert lease["receipt"]["status"] == "acquired"
    assert lease["run"]["record_schema_version"] == 1
    assert lease["run"]["finished_at"] and lease["run"]["goal_id"] is None
    async with db_engine.get_session() as db:
        row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity))).scalar_one()
        assert row.record_schema_version == 0 and row.lease_owner is None and row.finished_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize(("schema", "binding"), [(2, None), (0, "original-typed-idempotency")])
async def test_original_typed_owner_refusal_unchanged(async_db, schema, binding):
    identity = "ordinary:workflow_ordinary:input:run-typed"
    await workflow_state_repository.create_run(run_identity=identity, workflow_name="ordinary",
        tool_name="workflow_ordinary", session_id="ordinary", run_fingerprint="input",
        arguments={}, approval_context={})
    async with db_engine.get_session() as db:
        row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity))).scalar_one()
        row.record_schema_version = schema
        row.idempotency_binding = binding
    with pytest.raises(RuntimeError, match="typed durable jobs"):
        await workflow_state_repository.acquire_or_renew_v2_lease(run_identity=identity, owner="ordinary-original-owner")
