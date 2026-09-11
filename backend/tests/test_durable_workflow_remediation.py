"""Regression proof for the canonical workflow durability seams.

These tests deliberately use a small repository double so owner, failure, and
terminal-review behavior can be exercised even when the shared async SQLite
fixture is unavailable.  The process/file-backed proof remains opt-in below.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import pytest

from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.security.trust_contract import PrincipalType, TrustPrincipal
from src.workflows.manager import (
    DurableWorkflowStateUnavailable,
    _CanonicalWorkflowStateWriter,
    _admit_canonical_workflow_job,
    _assert_workflow_parent_recovery_authority,
    _run_async,
    _run_workflow_state_write,
    _workflow_canonical_lease_owner,
    _workflow_contract_fields,
    _workflow_durable_owner_fields,
    durable_lease_id,
)
from src.api.workflows import (
    _canonical_workflow_projection_input,
    _control_typed_workflow_run,
    _list_workflow_runs,
    _safe_workflow_run_projection,
    _workflow_parent_recovery_metadata,
    _workflow_resume_plan,
    _workflow_retry_from_step_draft,
    WorkflowRunControlRequest,
    control_workflow_run,
)


def _operator_context(*, principal_id: str = "operator:durable-test", session_id: str = "session-durable"):
    principal = TrustPrincipal(
        principal_id=principal_id,
        principal_type=PrincipalType.OPERATOR,
        session_id=session_id,
    )
    return set_runtime_context(session_id, "balanced", trust_principal=principal)


def _canonical_job(*, status: str = "running") -> dict:
    return {
        "job_id": "session-durable:workflow:test:run-1",
        "status": status,
        "revision": 4,
        "session_id": "session-durable",
        "owner": {
            "kind": "user",
            "principal_id": "operator:durable-test",
            "service_id": None,
        },
        "lease": {"fencing_token": 2},
    }


def _typed_operator_context(typed: dict) -> dict:
    return {
        "workflow_run_identity": typed["run_identity"],
        "goal_id": typed["goal_id"],
        "criterion_id": typed["criterion_id"],
        "goal_revision": typed["goal_revision"],
        "plan_revision": typed["plan_revision"],
        "candidate_id": typed["candidate_id"],
    }


def _typed_goal():
    return SimpleNamespace(
        id="goal-typed",
        revision=2,
        plan_revision=3,
        success_criterion_json=json.dumps({
            "criterion_id": "criterion-typed",
            "description": "Current criterion",
            "target": "done",
        }),
    )


class _RecordingRepository:
    def __init__(self, *, status: str = "running", fail_checkpoint: bool = False):
        self.job = _canonical_job(status=status)
        self.fail_checkpoint = fail_checkpoint
        self.effects: list[dict] = []
        self.uncertainty: list[dict] = []

    async def get_job(self, _job_id: str):
        return dict(self.job)

    async def record_effect(self, _job_id: str, **kwargs):
        self.effects.append(dict(kwargs))
        if kwargs.get("effect_type") == "workflow_output":
            self.job["status"] = "running"
        self.job["revision"] += 1
        return dict(self.job)

    async def record_checkpoint(self, _job_id: str, **_kwargs):
        if self.fail_checkpoint:
            raise RuntimeError("checkpoint store unavailable")
        self.job["revision"] += 1
        return dict(self.job)

    async def record_readback(self, _job_id: str, **kwargs):
        self.effects.append(dict(kwargs))
        self.job["revision"] += 1
        return dict(self.job)

    async def record_artifact(self, _job_id: str, **kwargs):
        self.effects.append(dict(kwargs))
        self.job["revision"] += 1
        return dict(self.job)

    async def transition_job(self, _job_id: str, status: str, **kwargs):
        self.uncertainty.append({"status": status, **kwargs})
        self.job["status"] = status
        self.job["revision"] += 1
        return dict(self.job)


class _AdmissionRepository:
    def __init__(self):
        self.spec = None

    async def admit_job(self, spec):
        self.spec = spec
        return {"job_id": spec.identity.job_id, "status": "accepted"}

    async def queue_job(self, job_id):
        return {"job_id": job_id, "status": "queued"}

    async def claim_job(self, job_id, *, owner, lease_seconds):
        return {
            **_canonical_job(),
            "job_id": job_id,
            "lease": {"owner": owner, "fencing_token": 3},
        }


def test_canonical_admission_persists_full_workflow_contract():
    repository = _AdmissionRepository()
    tokens = _operator_context()
    try:
        writer = _admit_canonical_workflow_job(
            repository=repository,
            run_identity="session-durable:workflow:contract:run-1",
            workflow=SimpleNamespace(name="contract-workflow"),
            tool_name="workflow_contract",
            session_id="session-durable",
            run_fingerprint="fingerprint-contract",
            audit_arguments={
                "goal_id": "goal-contract",
                "criterion_id": "criterion-contract",
                "goal_revision": 9,
                "plan_revision": 4,
                "candidate_id": "candidate-contract",
                "dependencies": ["dependency-2", "dependency-1"],
                "deadline_at": "2099-01-01T00:00:00+00:00",
                "budget_microusd": 17,
            },
            approval_context={"priority": 82, "max_attempts": 3},
            checkpoint_context_allowed=True,
            owner_fields={
                "owner_kind": "user",
                "owner_principal_id": "operator:durable-test",
            },
            parent_job_id="session-durable:workflow:parent:run-1",
            parent_fencing_token=7,
        )
    finally:
        reset_runtime_context(tokens)

    assert isinstance(writer, _CanonicalWorkflowStateWriter)
    assert repository.spec.run_fingerprint == "fingerprint-contract"
    assert repository.spec.goal_id == "goal-contract"
    assert repository.spec.goal_revision == 9
    assert repository.spec.plan_revision == 4
    assert repository.spec.candidate_id == "candidate-contract"
    assert repository.spec.dependencies == ("dependency-1", "dependency-2")
    assert repository.spec.deadline_at == "2099-01-01T00:00:00+00:00"
    assert repository.spec.budget_microusd == 17
    assert repository.spec.priority == 82
    assert repository.spec.max_attempts == 3
    assert repository.spec.parent_job_id == "session-durable:workflow:parent:run-1"
    assert repository.spec.parent_fencing_token == 7


def test_contract_projection_binds_goal_plan_candidate_dependencies_deadline_budget():
    fields = _workflow_contract_fields(
        {
            "goal_id": "goal-1",
            "criterion_id": "criterion-1",
            "goal_revision": 3,
            "plan_revision": 8,
            "candidate_id": "candidate-1",
            "dependencies": ["dep-b", "dep-a", "dep-a"],
            "deadline_at": "2099-01-01T00:00:00+00:00",
            "budget_microusd": 125,
        },
        {"priority": 87, "max_attempts": 4},
    )

    assert fields == {
        "goal_id": "goal-1",
        "criterion_id": "criterion-1",
        "goal_revision": 3,
        "plan_revision": 8,
        "candidate_id": "candidate-1",
        "dependencies": ("dep-a", "dep-b"),
        "deadline_at": "2099-01-01T00:00:00+00:00",
        "priority": 87,
        "max_attempts": 4,
        "budget_microusd": 125,
    }


def test_authenticated_workflow_owner_requires_current_matching_session():
    tokens = _operator_context()
    try:
        assert _workflow_durable_owner_fields() == {
            "owner_kind": "user",
            "owner_principal_id": "operator:durable-test",
        }
    finally:
        reset_runtime_context(tokens)

    stale_tokens = set_runtime_context(
        "session-other",
        "balanced",
        trust_principal=TrustPrincipal(
            principal_id="operator:durable-test",
            principal_type=PrincipalType.OPERATOR,
            session_id="session-durable",
        ),
    )
    try:
        with pytest.raises(DurableWorkflowStateUnavailable, match="current session-bound"):
            _workflow_durable_owner_fields()
    finally:
        reset_runtime_context(stale_tokens)


def test_unknown_authenticated_principal_type_cannot_fall_back_to_legacy_writer():
    tokens = set_runtime_context(
        "session-unknown",
        "balanced",
        trust_principal=TrustPrincipal(
            principal_id="edge:untrusted",
            principal_type="unknown",
            session_id="session-unknown",
        ),
    )
    try:
        with pytest.raises(DurableWorkflowStateUnavailable, match="unsupported"):
            _workflow_durable_owner_fields()
    finally:
        reset_runtime_context(tokens)


def _typed_api_job(*, status: str = "running", lease_owner: str | None = None) -> dict:
    run_identity = "session-durable:workflow:typed:run-1"
    lease_owner = lease_owner if lease_owner is not None else _workflow_canonical_lease_owner(run_identity)
    return {
        "job_id": run_identity,
        "run_identity": run_identity,
        "record_schema_version": 2,
        "root_run_identity": run_identity,
        "parent_job_id": None,
        "parent_fencing_token": None,
        "job_kind": "typed",
        "workflow_name": "typed-workflow",
        "tool_name": "workflow_typed",
        "session_id": "session-durable",
        "status": status,
        "owner": {"kind": "user", "principal_id": "operator:durable-test", "service_id": None},
        "declared_authority": {
            "goal_id": "goal-typed",
            "criterion_id": "criterion-typed",
            "goal_revision": 2,
            "plan_revision": 3,
            "candidate_id": "candidate-typed",
            "workflow_name": "typed-workflow",
            "risk_level": "low",
            "execution_boundaries": ["workspace_write"],
            "accepts_secret_refs": False,
            "step_tools": ["write_file"],
        },
        "dependencies": ["dependency-1"],
        "resource_claims": ["cpu"],
        "goal_id": "goal-typed",
        "criterion_id": "criterion-typed",
        "goal_revision": 2,
        "plan_revision": 3,
        "candidate_id": "candidate-typed",
        "revision": 4,
        "lease": {
            "owner": lease_owner,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "fencing_token": 7,
            "lease_id": durable_lease_id(run_identity, 7),
            "revision": 4,
        },
        "started_at": "2026-09-10T10:00:00+00:00",
        "updated_at": "2026-09-10T10:01:00+00:00",
        "finished_at": None,
        "checkpoints": [
            {
                "checkpoint_id": "step:prepare",
                "state_digest": "sha256:" + "a" * 64,
                "safe": True,
                "fencing_token": 7,
                "payload": {
                    "step_id": "prepare",
                    "step_index": 1,
                    "tool": "write_file",
                    "status": "succeeded",
                    "state": {"arguments": {"file_path": "notes/out.md"}, "result": "secret result"},
                    "artifact_paths": ["notes/out.md"],
                },
            }
        ],
        "artifacts": [
            {
                "artifact_id": "art_" + "a" * 24,
                "artifact_type": "workspace_file",
                "file_path": "notes/out.md",
                "content_sha256": "sha256:" + "b" * 64,
                "size_bytes": 12,
                "exists": True,
                "recorded_at": "2026-09-10T10:01:00+00:00",
            }
        ],
        "effects": [
            {
                "effect_id": "workflow-output",
                "receipt_kind": "readback",
                "effect_type": "workflow_output",
                "target_digest": "sha256:" + "c" * 64,
                "status": "succeeded",
                "content_sha256": "sha256:" + "c" * 64,
                "recorded_at": "2026-09-10T10:01:00+00:00",
                "fencing_token": 7,
                "details": {"secret": "must not be projected"},
            }
        ],
    }


def test_typed_projection_exposes_bounded_receipts_and_status():
    projection_input = _canonical_workflow_projection_input(_typed_api_job(status="degraded"))
    projection = _safe_workflow_run_projection(projection_input)

    assert projection is not None
    assert projection["record_schema_version"] == 2
    assert projection["status"] == "degraded"
    assert projection["artifact_paths"] == ["notes/out.md"]
    assert projection["durable_receipts"]["checkpoints"][0]["state_digest"] == "a" * 64
    assert projection["durable_receipts"]["artifacts"][0]["artifact_id_digest"]
    assert projection["durable_receipts"]["effects"][0]["effect_type"] == "workflow_output"
    assert projection_input["lease"]["lease_id"] == durable_lease_id(projection_input["run_identity"], 7)
    assert projection_input["lease"]["revision"] == projection_input["revision"]
    assert projection["lease"]["lease_id_digest"]
    assert projection["lease"]["revision"] == projection["revision"]
    assert "secret result" not in str(projection)
    assert "must not be projected" not in str(projection)


def test_typed_resume_plan_carries_derived_lease_identity_and_revision():
    run = _canonical_workflow_projection_input(_typed_api_job())
    assert run is not None
    revision, lease_id, fencing_token = _workflow_parent_recovery_metadata(run)

    assert revision == run["revision"]
    assert fencing_token == run["lease"]["fencing_token"]
    assert lease_id == durable_lease_id(run["run_identity"], fencing_token)

    plan = _workflow_resume_plan(run, approvals=[])
    assert plan["parent_revision"] == revision
    assert plan["parent_lease_id"] == lease_id
    assert plan["parent_fencing_token"] == fencing_token
    draft = _workflow_retry_from_step_draft(
        run["workflow_name"],
        step_id="prepare",
        arguments={},
        parent_run_identity=run["run_identity"],
        parent_revision=revision,
        parent_lease_id=lease_id,
        parent_fencing_token=fencing_token,
    )
    assert f'_seraph_parent_lease_id="{lease_id}"' in draft
    assert "_seraph_parent_fencing_token=7" in draft


@pytest.mark.asyncio
async def test_typed_rows_are_listed_from_canonical_repository():
    typed = _typed_api_job(status="unknown_external_effect")
    with (
        patch("src.api.workflows.audit_repository.list_events", new_callable=AsyncMock, return_value=[]),
        patch("src.api.workflows.workflow_state_repository.list_runs", new_callable=AsyncMock, return_value=[]),
        patch("src.api.workflows.durable_job_repository.list_jobs", new_callable=AsyncMock, return_value=[typed]),
        patch("src.api.workflows.approval_repository.list_pending", new_callable=AsyncMock, return_value=[]),
        patch("src.api.workflows.session_manager.list_sessions", new_callable=AsyncMock, return_value=[]),
    ):
        runs = await _list_workflow_runs(limit=10, session_id="session-durable")

    assert len(runs) == 1
    assert runs[0]["run_identity"] == typed["run_identity"]
    assert runs[0]["record_schema_version"] == 2
    assert runs[0]["status"] == "unknown_external_effect"
    assert runs[0]["typed_receipts"]["effects"]


@pytest.mark.asyncio
async def test_typed_control_uses_canonical_fence_and_rejects_stale_owner():
    typed = _typed_api_job(status="running")
    transitioned = {**typed, "status": "paused", "lease": {"owner": None, "expires_at": None, "fencing_token": 7}, "revision": 5,
                    "receipt": {"kind": "transition", "status": "recorded", "to": "paused", "revision": 5}}
    with (
        patch("src.api.workflows.durable_job_repository.get_job", new_callable=AsyncMock, return_value=typed),
        patch("src.api.workflows.durable_job_repository.pause_job", new_callable=AsyncMock, return_value=transitioned) as pause,
        patch("src.api.workflows.goal_repository.get", new_callable=AsyncMock, return_value=_typed_goal()),
    ):
        result = await _control_typed_workflow_run(
            run_identity=typed["run_identity"],
            action="pause",
            run=typed,
            principal_id="operator:durable-test",
            session_id="session-durable",
            operator_context=_typed_operator_context(typed),
        )

    pause.assert_awaited_once_with(
        typed["run_identity"],
        owner=_workflow_canonical_lease_owner(typed["run_identity"]),
        fencing_token=7,
        expected_revision=4,
    )
    assert result["status"] == "recorded"
    assert result["transition_receipt"]["status"] == "recorded"
    assert result["run"]["status"] == "paused"

    stale = _typed_api_job(status="running", lease_owner="workflow-runner:stale")
    with (
        patch("src.api.workflows.durable_job_repository.get_job", new_callable=AsyncMock, return_value=stale),
        patch("src.api.workflows.goal_repository.get", new_callable=AsyncMock, return_value=_typed_goal()),
    ):
        with pytest.raises(HTTPException) as error:
            await _control_typed_workflow_run(
                run_identity=stale["run_identity"],
                action="pause",
                run=stale,
                principal_id="operator:durable-test",
                session_id="session-durable",
                operator_context=_typed_operator_context(stale),
            )
    assert error.value.status_code == 409
    assert error.value.detail == "workflow_control_lease_blocked"


@pytest.mark.asyncio
async def test_control_route_never_sends_schema_v2_row_to_legacy_repository():
    typed = _typed_api_job(status="running")
    transitioned = {**typed, "status": "paused", "lease": {"owner": None, "expires_at": None, "fencing_token": 7}, "revision": 5,
                    "receipt": {"kind": "transition", "status": "recorded", "to": "paused", "revision": 5}}
    principal = TrustPrincipal(
        principal_id="operator:durable-test",
        principal_type=PrincipalType.OPERATOR,
        session_id="session-durable",
    )
    operator = SimpleNamespace(principal=principal, session_id="session-durable")
    request = object()
    with (
        patch("src.api.workflows._require_authenticated_capability_operator", return_value=operator),
        patch("src.api.workflows.bind_operator_principal", return_value=principal),
        patch("src.api.workflows.context_manager.get_context", return_value=SimpleNamespace(approval_mode="balanced")),
        patch("src.api.workflows._begin_rest_revocation_watch", return_value=None),
        patch("src.api.workflows._end_rest_revocation_watch", new_callable=AsyncMock),
        patch("src.api.workflows._workflow_session_fence", new_callable=AsyncMock),
        patch("src.api.workflows._find_workflow_run_for_control", new_callable=AsyncMock, return_value=typed),
        patch("src.api.workflows.durable_job_repository.get_job", new_callable=AsyncMock, return_value=typed),
        patch("src.api.workflows.durable_job_repository.pause_job", new_callable=AsyncMock, return_value=transitioned) as pause,
        patch("src.api.workflows.goal_repository.get", new_callable=AsyncMock, return_value=_typed_goal()),
        patch("src.api.workflows.workflow_state_repository.acquire_or_renew_v2_lease", new_callable=AsyncMock) as legacy_lease,
        patch("src.api.workflows.workflow_state_repository.record_v2_transition", new_callable=AsyncMock) as legacy_transition,
        patch("src.api.workflows.workflow_state_repository.record_v2_operator_recovery_control", new_callable=AsyncMock) as legacy_control,
    ):
        result = await control_workflow_run(
            typed["run_identity"],
            WorkflowRunControlRequest(
                action="pause",
                operator_context=_typed_operator_context(typed),
            ),
            request,
        )

    assert result["status"] == "recorded"
    assert result["run"]["status"] == "paused"
    pause.assert_awaited_once()
    legacy_lease.assert_not_awaited()
    legacy_transition.assert_not_awaited()
    legacy_control.assert_not_awaited()


def test_typed_checkpoint_recovery_requires_current_runner_owner_and_fence():
    parent_id = "session-durable:workflow:typed:parent-1"
    details = {
        "record_schema_version": 2,
        "state_source": "durable_workflow_state",
        "durable_run_identity": parent_id,
        "session_id": "session-durable",
        "owner_kind": "user",
        "owner_principal_id": "operator:durable-test",
        "service_id": None,
        "revision": 9,
        "lease": {
            "owner": _workflow_canonical_lease_owner(parent_id),
            "lease_id": durable_lease_id(parent_id, 11),
            "expires_at": "2099-01-01T00:00:00+00:00",
            "revision": 9,
            "fencing_token": 11,
        },
    }
    tokens = _operator_context()
    try:
        _assert_workflow_parent_recovery_authority(
            parent_run_identity=parent_id,
            details=details,
            control_inputs={
                "_seraph_parent_revision": 9,
                "_seraph_parent_lease_id": durable_lease_id(parent_id, 11),
                "_seraph_parent_fencing_token": 11,
            },
        )
        with pytest.raises(RuntimeError, match="lease identity"):
            _assert_workflow_parent_recovery_authority(
                parent_run_identity=parent_id,
                details={
                    **details,
                    "lease": {
                        **details["lease"],
                        "lease_id": "lease-forged",
                    },
                },
                control_inputs={
                    "_seraph_parent_revision": 9,
                    "_seraph_parent_lease_id": "lease-forged",
                    "_seraph_parent_fencing_token": 11,
                },
            )
        with pytest.raises(RuntimeError, match="parent fence is stale"):
            _assert_workflow_parent_recovery_authority(
                parent_run_identity=parent_id,
                details=details,
                control_inputs={
                    "_seraph_parent_revision": 9,
                    "_seraph_parent_lease_id": durable_lease_id(parent_id, 11),
                    "_seraph_parent_fencing_token": 10,
                },
            )
    finally:
        reset_runtime_context(tokens)


def test_canonical_checkpoint_payload_contains_revision_lease_and_parent_fields():
    typed = _typed_api_job()
    repository = _RecordingRepository()
    repository.job = typed
    tokens = _operator_context()
    try:
        writer = _CanonicalWorkflowStateWriter(
            repository,
            job=typed,
            owner=typed["lease"]["owner"],
            fencing_token=typed["lease"]["fencing_token"],
            checkpoint_context_allowed=True,
        )
        payload = asyncio.run(writer.get_checkpoint_payload(typed["run_identity"]))
    finally:
        reset_runtime_context(tokens)

    assert payload is not None
    assert payload["record_schema_version"] == 2
    assert payload["revision"] == typed["revision"]
    assert payload["lease"] == typed["lease"]
    assert payload["lease"]["lease_id"] == durable_lease_id(typed["run_identity"], 7)
    assert payload["lease"]["revision"] == typed["revision"]
    assert payload["parent_job_id"] is None
    assert payload["parent_fencing_token"] is None
    assert payload["step_records"][0]["id"] == "prepare"


def test_degraded_terminal_readback_preserves_execution_status_and_goal_completion():
    repository = _RecordingRepository()
    tokens = _operator_context()
    try:
        writer = _CanonicalWorkflowStateWriter(
            repository,
            job=_canonical_job(),
            owner="workflow-runner:test",
            fencing_token=2,
            checkpoint_context_allowed=True,
        )
        result = asyncio.run(
            writer.finish_run(
                status="degraded",
                artifact_paths=[],
                metadata={"summary": "one optional step degraded"},
            )
        )
    finally:
        reset_runtime_context(tokens)

    assert result["status"] == "degraded"
    output = next(item for item in repository.effects if item.get("effect_type") == "workflow_output")
    assert output["details"]["execution_status"] == "degraded"
    assert output["details"]["goal_completion"] is False


def test_terminal_artifact_review_is_explicitly_rejected_with_operator_receipt():
    repository = _RecordingRepository(status="degraded")
    tokens = _operator_context()
    try:
        writer = _CanonicalWorkflowStateWriter(
            repository,
            job=_canonical_job(status="degraded"),
            owner="workflow-runner:test",
            fencing_token=2,
            checkpoint_context_allowed=True,
        )
        result = asyncio.run(writer.record_artifact_review(artifact_path="reports/out.md"))
    finally:
        reset_runtime_context(tokens)

    assert result["receipt"] == {
        "kind": "artifact_review",
        "status": "rejected",
        "reason": "terminal_run_review_window_closed",
        "artifact_path": "reports/out.md",
        "operator_visible": True,
    }
    assert repository.effects == []


def test_required_canonical_write_marks_unknown_external_effect_before_raising():
    repository = _RecordingRepository(fail_checkpoint=True)
    tokens = _operator_context()
    try:
        writer = _CanonicalWorkflowStateWriter(
            repository,
            job=_canonical_job(),
            owner="workflow-runner:test",
            fencing_token=2,
            checkpoint_context_allowed=True,
        )
        with pytest.raises(DurableWorkflowStateUnavailable, match="unsafe workflow continuation"):
            _run_workflow_state_write(
                writer,
                writer._record_checkpoint(
                    checkpoint_id="step:one",
                    state={"result": "ok"},
                    payload={"step_id": "one"},
                ),
                phase="step_completed:one",
            )
    finally:
        reset_runtime_context(tokens)

    assert repository.uncertainty
    assert repository.uncertainty[-1]["status"] == "unknown_external_effect"


def test_sync_bridge_carries_runtime_context_into_second_event_loop():
    marker: ContextVar[str] = ContextVar("durable_test_marker", default="missing")
    marker.set("caller-context")

    async def read_marker():
        await asyncio.sleep(0)
        return marker.get()

    async def invoke_bridge():
        return _run_async(read_marker())

    assert asyncio.run(invoke_bridge()) == "caller-context"


@pytest.mark.skipif(
    os.getenv("SERAPH_RUN_FILE_BACKED_DURABLE_PROOF") != "1",
    reason="opt-in process-backed SQLite proof; shared async DB fixture may be unavailable",
)
def test_file_backed_process_restart_reconciles_unresolved_effect(tmp_path: Path):
    """Run two real Python processes against one SQLite file.

    This is opt-in because it starts a subprocess and is intentionally kept
    out of the ordinary unit shard.  It exercises the repository itself and
    fails with a bounded timeout when the host's async SQLite worker cannot
    initialize.
    """
    db_path = tmp_path / "restart-proof.sqlite"
    script = r'''
import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from src.workflows import job_runtime
from src.workflows.job_runtime import DurableJobIdentity, DurableJobRepository, DurableJobSpec

path = os.environ["DURABLE_PROOF_DB"]

async def session_factory(engine):
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    @asynccontextmanager
    async def get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    return get_session

async def run():
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"check_same_thread": False})
    if os.environ.get("DURABLE_PROOF_PHASE") == "seed":
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    get_session = await session_factory(engine)
    job_runtime.get_session = get_session
    repo = DurableJobRepository()
    if os.environ.get("DURABLE_PROOF_PHASE") == "seed":
        spec = DurableJobSpec(
            identity=DurableJobIdentity(
                job_id="restart-proof-job",
                owner_kind="service",
                owner_principal_id="service:restart-proof",
                job_kind="restart-proof",
                capability_version="1",
                idempotency_scope="restart-proof",
                idempotency_key="restart-proof",
            ),
            declared_authority={"principal": "service:restart-proof"},
        )
        admitted = await repo.admit_job(spec)
        await repo.queue_job(admitted["job_id"])
        claimed = await repo.claim_job(admitted["job_id"], owner="runner-before-restart", lease_seconds=1)
        await repo.record_effect(
            admitted["job_id"],
            effect_type="restart-proof-effect",
            target_path="controlled-target",
            target_digest="target-digest",
            status="intent",
            owner="runner-before-restart",
            fencing_token=claimed["lease"]["fencing_token"],
        )
        print(json.dumps({"phase": "seed", "status": "intent_persisted"}))
    else:
        recovered = await repo.recover_stale_jobs(
            now=datetime.now(timezone.utc) + timedelta(seconds=5)
        )
        job = next(item for item in recovered if item["job_id"] == "restart-proof-job")
        assert job["status"] == "unknown_external_effect"
        assert any(item.get("status") == "intent" for item in job["effects"])
        print(json.dumps({"phase": "recover", "status": job["status"], "receipt": job["receipt"]}))
    await engine.dispose()

asyncio.run(run())
'''
    env = os.environ.copy()
    env["DURABLE_PROOF_DB"] = str(db_path)
    seed_env = {**env, "DURABLE_PROOF_PHASE": "seed"}
    try:
        first = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).parents[1]),
            env=seed_env,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("file-backed durable seed stalled after 25s before DB receipt")
    assert first.returncode == 0, first.stderr
    recover_env = {**env, "DURABLE_PROOF_PHASE": "recover"}
    try:
        second = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).parents[1]),
            env=recover_env,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("file-backed durable recovery stalled after 25s before DB receipt")
    assert second.returncode == 0, second.stderr
    assert '"status": "unknown_external_effect"' in second.stdout
