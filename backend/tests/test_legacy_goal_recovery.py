"""Actual compatibility API and inline producer, isolated from inference."""
import asyncio
import json
import threading
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, delete

from config.settings import settings
from src.api.workflows import router, _workflow_operator_owner
from src.auth.middleware import OperatorAuthMiddleware
from src.auth.service import create_session, bind_operator_principal
from src.approval.runtime import set_runtime_context, reset_runtime_context
from src.db import engine as db_engine
from src.db.models import Goal, WorkflowRunState
from src.goals.contracts import GoalSuccessCriterion
from src.goals.repository import serialize_success_criterion
from src.workflows.durable_state import workflow_state_repository
from src.workflows.loader import Workflow, WorkflowStep
from src.workflows.manager import WorkflowTool


def get_session():
    return db_engine.get_session()


async def seed_parent(operator, schema):
    identity = f"{operator.session_id}:workflow_legacy_local_read:original:run-original"
    await workflow_state_repository.create_run(run_identity=identity, workflow_name="legacy-local-read",
        tool_name="workflow_legacy_local_read", session_id=operator.session_id,
        operator_session_id=operator.session_id, run_fingerprint="original", arguments={},
        approval_context={}, owner_kind="user", owner_principal_id=operator.principal.principal_id)
    await workflow_state_repository.record_step_started(run_identity=identity,
        workflow_name="legacy-local-read", step_id="read", step_index=1,
        tool_name="legacy_read", arguments={})
    await workflow_state_repository.record_step_failed(run_identity=identity, step_id="read",
        result_summary="interrupted", error_kind="interrupted", error_summary="interrupted",
        checkpoint={"arguments": {}})
    await workflow_state_repository.finish_run(run_identity=identity, status="failed",
        checkpoint_context={"read": {"arguments": {}}}, last_completed_step_id="read")
    # Genuine stored compatibility history: only the original row's historical
    # Goal contract/schema is imported here; every lease/child/effect is real.
    authority = {"goal_id": "legacy-goal", "goal_revision": 1,
        "criterion_id": "legacy-criterion", "plan_revision": 1, "candidate_id": None}
    async with get_session() as db:
        goal = Goal(id="legacy-goal", title="Local read", owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id, success_criterion_json=serialize_success_criterion(
                GoalSuccessCriterion(criterion_id="legacy-criterion", description="Read actual local file")))
        db.add(goal)
        row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity))).scalar_one()
        row.record_schema_version = schema
        row.goal_id = goal.id
        row.goal_revision = row.plan_revision = 1
        row.declared_authority_json = json.dumps(authority)
    return identity


class ActualLocalRead:
    name = "legacy_read"
    description = "Read one original test file"
    inputs = {}
    output_type = "string"
    def __init__(self, path):
        self.path, self.calls = path, 0
        self.after_read = None
    def __call__(self, *, sanitize_inputs_outputs=False):
        self.calls += 1
        value = self.path.read_text()
        if self.after_read is not None:
            self.after_read()
        return value


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize(("schema", "fault"), [(1, None), (0, None), (-1, None),
    (1, "delete_after_parent"), (1, "delete_before_step"), (1, "delete_after_intent"),
    (1, "missing_criterion"), (1, "malformed_metadata"), (1, "oversized_metadata"),
    (1, "expired_parent_lease"), (1, "delete_after_dispatch"), (1, "bridge_timeout"),
    (1, "issuer_ends_after_admission"), (1, "oversized_step_address"), (1, "delete_before_lease")])
async def test_real_api_metadata_lease_then_original_goal_child(async_db, monkeypatch, tmp_path, schema, fault):
    monkeypatch.setattr("src.workflows.manager.get_session", async_db)
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-legacy-auth")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    token, operator = await create_session()
    identity = await seed_parent(operator, schema)
    snapshots = []
    from src.workflows import durable_state
    original_verify = durable_state._verify_legacy_child_in_session
    async def observe_private(db, child, *, budget=None):
        snapshot = await original_verify(db, child, budget=budget)
        if snapshot is not None:
            snapshots.append((db, child, snapshot))
            # Copying the exact private fields/seal is insufficient even while
            # the original producer and canonical writer remain current.
            object.__setattr__(child, "_legacy_recovery_verified_parent", replace(snapshot))
            conditions = []
            assert durable_state._append_legacy_parent_condition(conditions, child,
                writer_db=db, now=datetime.now(timezone.utc))
            assert str(conditions[-1]) == "false"
            object.__setattr__(child, "_legacy_recovery_verified_parent", snapshot)
        return snapshot
    monkeypatch.setattr(durable_state, "_verify_legacy_child_in_session", observe_private)
    if fault == "delete_before_lease":
        from src.goals.repository import goal_repository
        original_lease = workflow_state_repository.acquire_or_renew_v2_lease
        async def delete_then_acquire(**kwargs):
            assert await goal_repository.delete("legacy-goal") is True
            return await original_lease(**kwargs)
        monkeypatch.setattr(workflow_state_repository, "acquire_or_renew_v2_lease", delete_then_acquire)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        cookies={settings.operator_auth_cookie_name: token}, headers={"Origin": "http://localhost:3001"}) as client:
        response = await client.post(f"/api/workflows/runs/{identity}/control", json={"action": "retry",
            "step_id": "read", "operator_context": {"workflow_run_identity": identity,
                "goal_id": "legacy-goal", "criterion_id": "legacy-criterion", "goal_revision": 1, "plan_revision": 1}})
    if fault == "delete_before_lease":
        assert response.status_code == 409, response.text
        async with get_session() as db:
            row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity))).scalar_one()
            assert "lease" not in json.loads(row.metadata_json or "{}").get("orchestration_v2", {})
            assert not (await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == identity))).scalars().all()
        return
    assert response.status_code == 200, response.text
    async with get_session() as db:
        parent = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity))).scalar_one()
        assert parent.finished_at is not None and parent.lease_owner is None
        assert parent.record_schema_version == schema
        metadata = json.loads(parent.metadata_json)["orchestration_v2"]
        assert metadata["lease"]["owner"] == _workflow_operator_owner(operator.principal.principal_id, operator.session_id)
    if fault in {"missing_criterion", "malformed_metadata", "oversized_metadata", "expired_parent_lease"}:
        async with get_session() as db:
            row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity))).scalar_one()
            if fault == "missing_criterion":
                authority = json.loads(row.declared_authority_json)
                authority.pop("criterion_id")
                row.declared_authority_json = json.dumps(authority)
            elif fault == "malformed_metadata":
                row.metadata_json = '{"orchestration_v2":{"lease":{},"lease":{}}}'
            elif fault == "oversized_metadata":
                row.metadata_json = json.dumps({"private": "x" * 1_048_577})
            else:
                altered = json.loads(row.metadata_json)
                altered["orchestration_v2"]["lease"]["expires_at"] = "2000-01-01T00:00:00+00:00"
                row.metadata_json = json.dumps(altered)
    if fault == "oversized_step_address":
        async with get_session() as db:
            await db.execute(text("UPDATE workflow_step_states SET id=:oversized WHERE run_identity=:identity"),
                {"oversized": "x" * 1_048_577, "identity": identity})
    deleted = []
    if fault in {"delete_after_parent", "delete_before_step", "delete_after_intent", "delete_after_dispatch"}:
        from src.goals.repository import goal_repository
        from src.workflows.job_runtime import durable_job_repository
        from src.workflows.manager import _CanonicalWorkflowStateWriter
        async def delete_original_goal():
            assert await goal_repository.delete("legacy-goal",
                expected_owner_principal_id=operator.principal.principal_id,
                expected_owner_session_id=operator.session_id, expected_revision=1)
            deleted.append(True)
        if fault == "delete_after_parent":
            original = durable_job_repository.admit_workflow_recovery_job
            async def changed(*args, **kwargs):
                await delete_original_goal()
                return await original(*args, **kwargs)
            monkeypatch.setattr(durable_job_repository, "admit_workflow_recovery_job", changed)
        elif fault == "delete_before_step":
            original = _CanonicalWorkflowStateWriter.record_step_started
            async def changed(self, **kwargs):
                await delete_original_goal()
                return await original(self, **kwargs)
            monkeypatch.setattr(_CanonicalWorkflowStateWriter, "record_step_started", changed)
        elif fault == "delete_after_intent":
            original = _CanonicalWorkflowStateWriter.assert_active_lease
            async def changed(self, **kwargs):
                await delete_original_goal()
                return await original(self, **kwargs)
            monkeypatch.setattr(_CanonicalWorkflowStateWriter, "assert_active_lease", changed)
    path = tmp_path / "original.txt"
    path.write_text("actual local readback")
    read = ActualLocalRead(path)
    release_late = threading.Event()
    late_finished = threading.Event()
    late_sources = []
    if fault == "bridge_timeout":
        from src.workflows.job_runtime import durable_job_repository
        original = durable_job_repository.admit_workflow_recovery_job
        async def delayed(*args, **kwargs):
            late_sources.append(kwargs["source"])
            try:
                assert await asyncio.to_thread(release_late.wait, 40)
                return await original(*args, **kwargs)
            finally:
                late_finished.set()
        monkeypatch.setattr(durable_job_repository, "admit_workflow_recovery_job", delayed)
    if fault == "issuer_ends_after_admission":
        from src.workflows.job_runtime import durable_job_repository
        original = durable_job_repository.admit_workflow_recovery_job
        async def ends(*args, **kwargs):
            admitted = await original(*args, **kwargs)
            kwargs["source"].close()
            return admitted
        monkeypatch.setattr(durable_job_repository, "admit_workflow_recovery_job", ends)
    if fault == "delete_after_dispatch":
        from src.workflows.manager import _run_async
        read.after_read = lambda: _run_async(delete_original_goal())
    workflow = Workflow(name="legacy-local-read", description="Original local read", inputs={},
        steps=[WorkflowStep(tool="legacy_read", id="read")])
    tool = WorkflowTool(workflow, {"legacy_read": read})
    runtime = set_runtime_context(operator.session_id, "auto",
        trust_principal=bind_operator_principal(operator, operator.session_id))
    try:
        if fault is not None:
            with pytest.raises(RuntimeError):
                if fault == "bridge_timeout":
                    tool(_seraph_parent_run_identity=identity,
                        _seraph_parent_revision=metadata["revision"],
                        _seraph_parent_lease_id=metadata["lease"]["lease_id"], _seraph_resume_from_step="read")
                else:
                    await asyncio.to_thread(tool, _seraph_parent_run_identity=identity,
                        _seraph_parent_revision=metadata["revision"],
                        _seraph_parent_lease_id=metadata["lease"]["lease_id"], _seraph_resume_from_step="read")
        else:
            result = await asyncio.to_thread(tool, _seraph_parent_run_identity=identity,
                _seraph_parent_revision=metadata["revision"],
                _seraph_parent_lease_id=metadata["lease"]["lease_id"], _seraph_resume_from_step="read")
    finally:
        if fault == "bridge_timeout":
            assert late_sources and not late_sources[0]._live.is_set()
            release_late.set()
            assert await asyncio.to_thread(late_finished.wait, 5)
        reset_runtime_context(runtime)
    if fault is not None:
        if fault.startswith("delete_"):
            assert deleted == [True], "The actual Goal owner must commit deletion for this race proof"
        assert read.calls == (1 if fault == "delete_after_dispatch" else 0)
        async with get_session() as db:
            children = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == identity))).scalars())
            assert len(children) == (1 if fault in {"delete_before_step", "delete_after_intent", "delete_after_dispatch", "issuer_ends_after_admission"} else 0)
            for child in children:
                assert child.goal_id == "legacy-goal" and child.parent_fencing_token is None
                if fault in {"delete_after_intent", "delete_after_dispatch"}:
                    assert '"status":"intent"' in child.effect_receipts_json
                if fault == "delete_after_dispatch":
                    assert child.status == "running" and child.lease_owner is not None
                if fault == "issuer_ends_after_admission":
                    assert child.status == "accepted" and json.loads(child.resource_claims_json) == ["cpu"]
                    child_identity = child.run_identity
        if fault == "issuer_ends_after_admission":
            from src.workflows.job_runtime import durable_job_repository
            with pytest.raises(RuntimeError, match="workflow_legacy_original_producer_unavailable"):
                await durable_job_repository.queue_job(child_identity)
            async with get_session() as db:
                row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == child_identity))).scalar_one()
                assert row.status == "accepted" and row.attempt_count == 0
                row.declared_authority_json = json.dumps({"oversized": "x" * 1_048_577})
            from src.memory.header_bounds import HeaderBoundsError
            with pytest.raises(HeaderBoundsError):
                await durable_job_repository.queue_job(child_identity)
            async with get_session() as db:
                row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == child_identity))).scalar_one()
                assert row.status == "accepted" and row.attempt_count == 0
        return
    assert read.calls == 1 and "actual local readback" in result
    assert snapshots
    for closed_db, original_child, snapshot in snapshots:
        conditions = []
        assert durable_state._append_legacy_parent_condition(conditions, original_child,
            writer_db=closed_db, now=datetime.now(timezone.utc))
        assert str(conditions[-1]) == "false"
    async with get_session() as db:
        children = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == identity))).scalars())
        assert len(children) == 1
        child = children[0]
        assert child.goal_id == "legacy-goal" and child.goal_revision == 1
        assert child.parent_fencing_token is None and child.status == "succeeded"
        assert "workflow-step:read" in child.effect_receipts_json
        assert "legacy_recovery_parent" in child.declared_authority_json
