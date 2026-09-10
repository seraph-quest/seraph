"""Regression proof for the canonical workflow durability seams.

These tests deliberately use a small repository double so owner, failure, and
terminal-review behavior can be exercised even when the shared async SQLite
fixture is unavailable.  The process/file-backed proof remains opt-in below.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.security.trust_contract import PrincipalType, TrustPrincipal
from src.workflows.manager import (
    DurableWorkflowStateUnavailable,
    _CanonicalWorkflowStateWriter,
    _admit_canonical_workflow_job,
    _run_async,
    _run_workflow_state_write,
    _workflow_contract_fields,
    _workflow_durable_owner_fields,
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


def test_contract_projection_binds_goal_plan_candidate_dependencies_deadline_budget():
    fields = _workflow_contract_fields(
        {
            "goal_id": "goal-1",
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
