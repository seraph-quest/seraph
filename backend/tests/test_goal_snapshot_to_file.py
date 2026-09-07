"""Bounded goal snapshot adapter contract and governed execution tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any

import pytest

from config.settings import settings
from src.db.models import Goal
from src.goals.contracts import GoalCandidateRequest, GoalSuccessCriterion
from src.goals.repository import serialize_success_criterion
from src.guardian import goal_conditioned_loop
from src.guardian.goal_conditioned_loop import build_goal_candidate_decision
from src.guardian.goal_snapshot_to_file import (
    CAPABILITY_ID,
    GoalSnapshotToFileAdapter,
    GoalSnapshotToFileRequest,
    GoalSnapshotToFileService,
    normalize_workspace_relative_path,
)


class _Goals:
    def __init__(self, goal: Goal, *later: Goal | None):
        self.values = [goal, *later]
        self.calls = 0

    async def get(self, _goal_id: str) -> Goal | None:
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[index]


class _Jobs:
    def __init__(self):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.specs: dict[str, Any] = {}
        self.transitions: list[tuple[str, str]] = []
        self.effects: list[dict[str, Any]] = []
        self.readbacks: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []

    def _projection(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        return {
            "job_id": job_id,
            "status": job["status"],
            "failure_reason": job.get("failure_reason"),
            "lease": {
                "owner": job.get("owner"),
                "fencing_token": job.get("fencing_token", 0),
            },
            "artifacts": list(job.get("artifacts", [])),
            "effects": list(job.get("effects", [])),
            "receipt": {"status": "recorded"},
        }

    async def admit_job(self, spec):
        for job_id, existing in self.jobs.items():
            old = self.specs[job_id]
            if old.identity.idempotency_key == spec.identity.idempotency_key:
                return {**self._projection(job_id), "receipt": {"status": "deduped"}}
        self.jobs[spec.identity.job_id] = {"status": "accepted", "artifacts": [], "effects": []}
        self.specs[spec.identity.job_id] = spec
        return {**self._projection(spec.identity.job_id), "receipt": {"status": "accepted"}}

    async def queue_job(self, job_id: str, **_kwargs):
        self.jobs[job_id]["status"] = "queued"
        self.transitions.append((job_id, "queued"))
        return self._projection(job_id)

    async def claim_job(self, job_id: str, *, owner: str, lease_seconds: int):
        self.jobs[job_id].update({"status": "running", "owner": owner, "fencing_token": 1})
        self.transitions.append((job_id, "running"))
        return self._projection(job_id)

    async def transition_job(self, job_id: str, status: str, **kwargs):
        self.jobs[job_id]["status"] = status
        if status in {"failed", "blocked"}:
            self.jobs[job_id]["failure_reason"] = kwargs.get("reason")
        self.transitions.append((job_id, status))
        return self._projection(job_id)

    async def record_artifact(self, job_id: str, *, file_path: str, content: bytes, **_kwargs):
        digest = hashlib.sha256(content).hexdigest()
        record = {
            "artifact_id": "art_test_snapshot",
            "artifact_type": "goal_snapshot",
            "file_path": file_path,
            "content_sha256": digest,
            "exists": True,
        }
        self.jobs[job_id]["artifacts"].append(record)
        self.artifacts.append(record)
        return {**self._projection(job_id), "receipt": record}

    async def record_effect(self, job_id: str, **kwargs):
        receipt = {"effect_id": f"effect-{len(self.effects)}", **kwargs}
        self.jobs[job_id]["effects"].append(receipt)
        self.effects.append(receipt)
        return self._projection(job_id)

    async def record_readback(self, job_id: str, **kwargs):
        receipt = {"effect_id": f"readback-{len(self.readbacks)}", "receipt_kind": "readback", **kwargs}
        self.jobs[job_id]["effects"].append(receipt)
        self.readbacks.append(receipt)
        return self._projection(job_id)


class _GovernedWorkflow:
    name = "workflow_goal_snapshot_to_file"

    def __init__(self, root: Path, *, write_output: bool = True):
        self.root = root
        self.write_output = write_output
        self.calls = 0

    def get_approval_context(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "workflow_name": "goal-snapshot-to-file",
            "risk_level": "medium",
            "execution_boundaries": ["workspace_write"],
            "step_tools": ["get_goals", "write_file"],
        }

    def __call__(self, *, file_path: str, sanitize_inputs_outputs: bool = False) -> str:
        self.calls += 1
        if self.write_output:
            target = self.root / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Goal snapshot\n- current goal (id=goal-1, active)\n", encoding="utf-8")
        return f"Saved the current goal snapshot to {file_path}."

    def get_audit_result_payload(self, _arguments: dict[str, Any], _result: Any):
        return "snapshot executed", {"durable_run_identity": "workflow-run-test"}


def _goal(*, revision: int = 1, status: str = "active") -> Goal:
    return Goal(
        id="goal-1",
        title="Keep the operator plan current",
        status=status,
        revision=revision,
        success_criterion_json=serialize_success_criterion(
            GoalSuccessCriterion(
                criterion_id="snapshot-present",
                description="A readable goal snapshot is present",
                verifier_kind="artifact_readback",
                evidence_refs=["goal-proof"],
            )
        ),
    )


def _request(**updates: Any) -> GoalSnapshotToFileRequest:
    values = {
        "goal_id": "goal-1",
        "goal_revision": 1,
        "file_path": "notes/goal-snapshot.md",
        "owner_principal_id": "service:guardian",
        "service_id": "service:goal-snapshot",
        "session_id": "session-1",
        "evidence_refs": ["goal-proof"],
        "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=60),
    }
    values.update(updates)
    return GoalSnapshotToFileRequest.model_validate(values)


def test_request_contract_binds_capability_owner_and_bounded_path():
    request = _request()
    assert request.file_path == "notes/goal-snapshot.md"
    assert request.priority <= 100
    assert request.capability_version == "1"
    assert normalize_workspace_relative_path("notes/goal-snapshot.md") == request.file_path
    with pytest.raises(ValueError):
        normalize_workspace_relative_path("../outside.md")
    with pytest.raises(ValueError):
        normalize_workspace_relative_path("/tmp/outside.md")
    with pytest.raises(ValueError):
        _request(owner_principal_id="operator:user")


def test_candidate_identity_includes_inputs():
    goal = _goal()
    first = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            evidence_refs=["goal-proof"],
            inputs={"file_path": "notes/a.md"},
        ),
    )
    second = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            evidence_refs=["goal-proof"],
            inputs={"file_path": "notes/b.md"},
        ),
    )
    assert first.dedupe_key != second.dedupe_key


async def test_service_executes_governed_boundary_records_job_receipts_and_no_learning(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    persisted: list[tuple[str, dict[str, Any]]] = []

    async def no_existing(**_kwargs):
        return None

    async def persist(*, event_type: str, summary: str, details: dict[str, Any]):
        persisted.append((event_type, details))
        return details

    monkeypatch.setattr(goal_conditioned_loop, "goal_repository", goals)
    monkeypatch.setattr(goal_conditioned_loop, "_existing_receipt", no_existing)
    monkeypatch.setattr(goal_conditioned_loop, "_persist_receipt", persist)
    result = await GoalSnapshotToFileService(
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
    ).run(_request())

    assert result.execution_status == "succeeded"
    assert result.verification == "passed"
    assert result.learning == "no_learning"
    assert result.job_id and result.artifact_ref == "art_test_snapshot"
    assert result.output_exists and result.workspace_contained and result.goal_id_read_back
    assert result.content_sha256
    assert workflow.calls == 1
    assert jobs.transitions == [(result.job_id, "queued"), (result.job_id, "running"), (result.job_id, "succeeded")]
    assert jobs.artifacts and jobs.effects
    assert any(item.get("receipt_kind") == "readback" for item in jobs.readbacks)
    assert {event_type for event_type, _details in persisted} == {"goal_loop_outcome", "goal_loop_no_learning"}
    assert persisted[0][1]["execution_status"] == "succeeded"


async def test_stale_goal_after_claim_blocks_without_workflow_or_learning(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    active = _goal()
    stale = _goal(revision=2)
    goals = _Goals(active, stale)
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    request = _request()
    candidate = build_goal_candidate_decision(
        active,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )
    result = await GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
    ).execute(goal=active, candidate=candidate)

    assert result.execution_status == "blocked"
    assert result.reason == "stale_goal_revision"
    assert workflow.calls == 0
    assert jobs.transitions[-1][1] == "blocked"
    assert any(effect.get("effect_type") == "goal_revision_guard" for effect in jobs.effects)


async def test_cancel_request_is_durable_and_does_not_invoke_workflow(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    persisted: list[tuple[str, dict[str, Any]]] = []

    async def no_existing(**_kwargs):
        return None

    async def persist(*, event_type: str, summary: str, details: dict[str, Any]):
        persisted.append((event_type, details))
        return details

    monkeypatch.setattr(goal_conditioned_loop, "goal_repository", goals)
    monkeypatch.setattr(goal_conditioned_loop, "_existing_receipt", no_existing)
    monkeypatch.setattr(goal_conditioned_loop, "_persist_receipt", persist)
    result = await GoalSnapshotToFileService(
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
    ).run(_request(cancel_requested=True))

    assert result.execution_status == "blocked"
    assert result.reason == "cancel_requested"
    assert result.durable_status == "cancelled"
    assert workflow.calls == 0
    assert jobs.transitions[-1][1] == "cancelled"
    assert any(event_type == "goal_loop_no_learning" for event_type, _details in persisted)
