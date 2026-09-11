"""Bounded goal snapshot adapter contract and governed execution tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
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
    CAPABILITY_VERSION,
    MAX_OUTPUT_BYTES,
    WORKFLOW_NAME,
    GoalSnapshotToFileAdapter,
    GoalSnapshotToFileRequest,
    GoalSnapshotToFileService,
    normalize_workspace_relative_path,
)
from src.security.authority_envelope import (
    CapabilityPolicy,
    CapabilityScope,
    ResourceLimits,
)
from src.security.trust_contract import AuthorityGrant, EgressClass, PrincipalType, TrustPrincipal


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

    def __init__(
        self,
        root: Path,
        *,
        write_output: bool = True,
        requires_approval: bool = False,
        workflow_name: str | None = "goal-snapshot-to-file",
        step_tools: list[str] | tuple[str, ...] = ("get_goals", "write_file"),
    ):
        self.root = root
        self.write_output = write_output
        self.requires_approval = requires_approval
        self.workflow_name = workflow_name
        self.step_tools = list(step_tools)
        self.calls = 0

    def get_approval_context(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        context = {
            "workflow_name": self.workflow_name,
            "risk_level": "medium",
            "execution_boundaries": ["workspace_write"],
            "step_tools": list(self.step_tools),
        }
        if self.requires_approval:
            context["requires_approval"] = True
        return context

    def __call__(self, *, file_path: str, sanitize_inputs_outputs: bool = False) -> str:
        self.calls += 1
        if self.write_output:
            target = self.root / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Goal snapshot\n- current goal (id=goal-1, active)\n", encoding="utf-8")
        return f"Saved the current goal snapshot to {file_path}."

    def get_audit_result_payload(self, _arguments: dict[str, Any], _result: Any):
        return "snapshot executed", {"durable_run_identity": "workflow-run-test"}


def _goal(
    *,
    revision: int = 1,
    status: str = "active",
    owner_principal_id: str | None = None,
    owner_session_id: str | None = None,
) -> Goal:
    return Goal(
        id="goal-1",
        title="Keep the operator plan current",
        status=status,
        revision=revision,
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
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


def _authority_principal(*, authenticated: bool = True, job_id: str = "") -> TrustPrincipal:
    return TrustPrincipal(
        principal_id="service:guardian",
        principal_type=PrincipalType.SERVICE,
        authenticated=authenticated,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id="session-1",
        job_id=job_id,
    )


def _authority_policy(root: Path, request: GoalSnapshotToFileRequest, *, output_bytes: int = MAX_OUTPUT_BYTES) -> CapabilityPolicy:
    limits = ResourceLimits(
        cpu_seconds=60.0,
        memory_bytes=256 * 1024 * 1024,
        pid_count=4,
        output_bytes=output_bytes,
        deadline_seconds=300.0,
    )
    return CapabilityPolicy(
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        owner_id=request.owner_principal_id,
        principal_type=PrincipalType.SERVICE,
        scope=CapabilityScope(
            operations=("write_file",),
            paths=(str(root),),
            sources=("source:goal-snapshot-to-file",),
            egress_class=EgressClass.LOCAL_ONLY,
        ),
        resource_limits=limits,
        goal_id=request.goal_id,
        expires_at=request.deadline_at.timestamp(),
    )


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
    with pytest.raises(ValueError, match="goal owner delegation requires both principal and session"):
        _request(goal_owner_principal_id="operator:goal-owner")


async def test_service_authority_carries_canonical_goal_owner_delegation(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goal = _goal(
        owner_principal_id="operator:goal-owner",
        owner_session_id="operator-session:goal-owner",
    )
    request = _request(
        goal_owner_principal_id=goal.owner_principal_id,
        goal_owner_session_id=goal.owner_session_id,
    )
    jobs = _Jobs()
    candidate = build_goal_candidate_decision(
        goal,
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
        goals=_Goals(goal),
        jobs=jobs,
        workflow_tool_provider=lambda _name: _GovernedWorkflow(tmp_path),
        authority_principal=_authority_principal(),
    ).execute(goal=goal, candidate=candidate)

    assert result.execution_status == "succeeded"
    authority = jobs.specs[next(iter(jobs.specs))].declared_authority
    assert authority["goal_owner_principal_id"] == "operator:goal-owner"
    assert authority["goal_owner_session_id"] == "operator-session:goal-owner"


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
        authority_principal=_authority_principal(),
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
    assert result.authority_receipt
    assert result.authority_receipt["allowed"] is True
    binding = result.authority_receipt["workflow_binding"]
    assert binding["workflow_name"] == WORKFLOW_NAME
    assert binding["workflow_version"] == CAPABILITY_VERSION
    assert binding["step_sequence"] == ["get_goals", "write_file"]
    assert len(binding["workflow_definition_digest"]) == 64
    assert jobs.specs[result.job_id].declared_authority["workflow_binding"] == binding
    assert jobs.specs[result.job_id].inputs["workflow_binding"] == binding
    assert request_path_not_in_receipt(result.authority_receipt, _request().file_path)
    assert {event_type for event_type, _details in persisted} == {"goal_loop_outcome", "goal_loop_no_learning"}
    assert persisted[0][1]["execution_status"] == "succeeded"


async def test_scheduler_child_spec_carries_parent_fence_and_uses_stable_scheduler_identity(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    request = _request(
        parent_job_id="strategist_tick:1",
        parent_fencing_token=7,
    )

    async def no_existing(**_kwargs):
        return None

    async def persist(*, event_type: str, summary: str, details: dict[str, Any]):
        return details

    monkeypatch.setattr(goal_conditioned_loop, "goal_repository", goals)
    monkeypatch.setattr(goal_conditioned_loop, "_existing_receipt", no_existing)
    monkeypatch.setattr(goal_conditioned_loop, "_persist_receipt", persist)
    result = await GoalSnapshotToFileService(
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_principal=_authority_principal(),
    ).run(request)

    spec = jobs.specs[result.job_id]
    assert spec.parent_job_id == "strategist_tick:1"
    assert spec.parent_fencing_token == 7
    assert spec.identity.idempotency_scope == "goal-snapshot-to-file-scheduler"
    assert "strategist_tick:1" not in result.job_id


@pytest.mark.parametrize(
    ("workflow_name", "step_tools", "expected_reason"),
    [
        (None, ["get_goals", "write_file"], "workflow_name_missing"),
        ("goal-snapshot-to-file", ["get_goals", "write_file", "update_goal"], "workflow_step_sequence_extra"),
        ("goal-snapshot-to-file", ["write_file", "get_goals"], "workflow_step_sequence_reordered"),
        ("unregistered-workflow", ["get_goals", "write_file"], "workflow_name_mismatch"),
    ],
)
async def test_workflow_definition_binding_rejects_untrusted_shape_before_dispatch(
    monkeypatch,
    tmp_path,
    workflow_name,
    step_tools,
    expected_reason,
):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(
        tmp_path,
        workflow_name=workflow_name,
        step_tools=step_tools,
    )
    request = _request()
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )
    adapter = GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_principal=_authority_principal(),
    )

    result = await adapter.execute(goal=goal, candidate=candidate)

    assert result.execution_status == "blocked"
    assert result.reason == f"authority_denied:{expected_reason}"
    assert workflow.calls == 0
    job_id = next(iter(jobs.jobs))
    assert jobs.jobs[job_id]["status"] == "blocked"
    binding = jobs.specs[job_id].declared_authority["workflow_binding"]
    assert binding["binding_status"] == "rejected"
    assert binding["workflow_name"] == WORKFLOW_NAME
    assert binding["step_sequence"] == ["get_goals", "write_file"]
    assert binding["rejection_reason"] == expected_reason
    assert adapter.last_receipt["workflow_binding"] == binding


@pytest.mark.parametrize(
    ("definition_steps", "expected_reason"),
    [
        (["get_goals", "write_file", "update_goal"], "workflow_step_sequence_extra"),
        (["write_file", "get_goals"], "workflow_step_sequence_reordered"),
    ],
)
async def test_workflow_definition_binding_checks_manager_step_order_before_dispatch(
    monkeypatch,
    tmp_path,
    definition_steps,
    expected_reason,
):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    workflow.workflow = SimpleNamespace(
        name=WORKFLOW_NAME,
        tool_name=workflow.name,
        steps=[SimpleNamespace(tool=tool_name) for tool_name in definition_steps],
    )
    request = _request()
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )
    adapter = GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_principal=_authority_principal(),
    )

    result = await adapter.execute(goal=goal, candidate=candidate)

    assert result.execution_status == "blocked"
    assert result.reason == f"authority_denied:{expected_reason}"
    assert workflow.calls == 0
    job_id = next(iter(jobs.jobs))
    assert jobs.jobs[job_id]["status"] == "blocked"


async def test_workflow_definition_binding_rejects_argument_drift_before_dispatch(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    workflow.workflow = SimpleNamespace(
        name=WORKFLOW_NAME,
        tool_name=workflow.name,
        steps=[
            SimpleNamespace(id="goals", tool="get_goals", arguments={}),
            SimpleNamespace(
                id="save",
                tool="write_file",
                arguments={
                    "file_path": "{{ file_path }}",
                    "content": "untrusted replacement",
                },
            ),
        ],
    )
    request = _request()
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )
    adapter = GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_principal=_authority_principal(),
    )

    result = await adapter.execute(goal=goal, candidate=candidate)

    assert result.execution_status == "blocked"
    assert result.reason == "authority_denied:workflow_definition_digest_mismatch"
    assert workflow.calls == 0
    job_id = next(iter(jobs.jobs))
    binding = jobs.specs[job_id].declared_authority["workflow_binding"]
    assert binding["binding_status"] == "rejected"
    assert binding["rejection_reason"] == "workflow_definition_digest_mismatch"


def request_path_not_in_receipt(receipt: dict[str, Any], path: str) -> bool:
    import json

    return path not in json.dumps(receipt, sort_keys=True)


async def test_missing_authenticated_owner_blocks_before_file_effect(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    request = _request()
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
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
    ).execute(goal=goal, candidate=candidate)

    assert result.execution_status == "blocked"
    assert result.reason == "authority_denied:authority_request_invalid:ValueError"
    assert workflow.calls == 0
    assert not (tmp_path / request.file_path).exists()
    assert jobs.transitions == [(result.job_id if hasattr(result, "job_id") else next(iter(jobs.jobs)), "blocked")]


async def test_authenticated_principal_bound_to_other_job_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    request = _request()
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
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
        authority_principal=_authority_principal(job_id="job:other"),
    ).execute(goal=goal, candidate=candidate)

    assert result.execution_status == "blocked"
    assert result.reason == "authority_denied:authority_request_invalid:ValueError"
    assert workflow.calls == 0
    assert jobs.jobs[next(iter(jobs.jobs))]["status"] == "blocked"


async def test_missing_approval_is_durable_and_has_redacted_authority_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path, requires_approval=True)
    request = _request()
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )
    adapter = GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_principal=_authority_principal(),
    )
    result = await adapter.execute(goal=goal, candidate=candidate)
    job_id = next(iter(jobs.jobs))

    assert result.execution_status == "blocked"
    assert jobs.jobs[job_id]["status"] == "blocked"
    assert result.reason == "authority_denied:approval_missing"
    assert workflow.calls == 0
    assert not (tmp_path / request.file_path).exists()
    assert adapter.last_receipt and adapter.last_receipt["authority_receipt"]
    assert adapter.last_receipt["authority_receipt"]["reason_code"] == "approval_missing"
    assert adapter.last_receipt["authority_receipt"]["effect"] == "require_approval"
    assert request_path_not_in_receipt(adapter.last_receipt["authority_receipt"], request.file_path)
    assert jobs.transitions == [(job_id, "blocked")]
    assert any(effect.get("effect_type") == "authority_gate" for effect in jobs.effects)


async def test_authority_resource_limit_denies_before_workflow_effect(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    request = _request()
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )
    adapter = GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_policy=_authority_policy(tmp_path, request, output_bytes=1),
        authority_principal=_authority_principal(),
    )
    result = await adapter.execute(goal=goal, candidate=candidate)
    job_id = next(iter(jobs.jobs))

    assert result.execution_status == "blocked"
    assert jobs.jobs[job_id]["status"] == "blocked"
    assert result.reason == "authority_denied:resource_limit_exceeded"
    assert workflow.calls == 0
    assert not (tmp_path / request.file_path).exists()
    assert adapter.last_receipt and adapter.last_receipt["authority_receipt"]
    assert adapter.last_receipt["authority_receipt"]["reason_code"] == "resource_limit_exceeded"
    assert request_path_not_in_receipt(adapter.last_receipt["authority_receipt"], request.file_path)
    assert jobs.transitions == [(job_id, "blocked")]


async def test_authority_recheck_after_claim_blocks_revoked_policy_before_workflow(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    request = _request()
    policy = _authority_policy(tmp_path, request)
    workflow = _GovernedWorkflow(tmp_path)
    adapter: GoalSnapshotToFileAdapter | None = None

    class RevokingJobs(_Jobs):
        async def claim_job(self, job_id: str, *, owner: str, lease_seconds: int):
            projection = await super().claim_job(job_id, owner=owner, lease_seconds=lease_seconds)
            assert adapter is not None
            adapter.authority_policy = replace(policy, revoked=True)
            return projection

    jobs = RevokingJobs()
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )
    adapter = GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_policy=policy,
        authority_principal=_authority_principal(),
    )

    result = await adapter.execute(goal=goal, candidate=candidate)
    job_id = next(iter(jobs.jobs))

    assert result.execution_status == "blocked"
    assert jobs.jobs[job_id]["status"] == "blocked"
    assert jobs.transitions == [(job_id, "queued"), (job_id, "running"), (job_id, "blocked")]
    assert workflow.calls == 0
    assert not (tmp_path / request.file_path).exists()
    assert adapter.last_receipt and adapter.last_receipt["authority_receipt"]["reason_code"] == "policy_revoked"


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
        authority_principal=_authority_principal(),
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


async def test_candidate_path_mismatch_blocks_before_any_effect(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goal = _goal()
    goals = _Goals(goal)
    jobs = _Jobs()
    workflow = _GovernedWorkflow(tmp_path)
    request = _request(file_path="notes/request.md")
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": "notes/other.md"},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )

    result = await GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
    ).execute(goal=goal, candidate=candidate)

    assert result.execution_status == "blocked"
    assert result.reason == "candidate_output_path_mismatch"
    assert workflow.calls == 0
    assert jobs.jobs == {}


async def test_effect_receipt_failure_blocks_with_reconciliation_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    workflow = _GovernedWorkflow(tmp_path)

    class FailingEffects(_Jobs):
        async def record_effect(self, _job_id, **_kwargs):
            raise RuntimeError("receipt store unavailable")

    jobs = FailingEffects()
    request = _request()
    adapter = GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_principal=_authority_principal(),
    )
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )

    result = await adapter.execute(goal=goal, candidate=candidate)

    assert result.execution_status == "blocked"
    assert result.reason.startswith("durable_reconciliation_required:")
    assert result.execution_status != "succeeded"
    assert adapter.last_receipt["reconciliation_required"] is True
    assert adapter.last_receipt["recovery_action"]
    assert jobs.jobs[result.job_id if hasattr(result, "job_id") else next(iter(jobs.jobs))]["status"] == "blocked"


async def test_terminal_transition_failure_never_reports_success(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goals = _Goals(_goal())
    workflow = _GovernedWorkflow(tmp_path)

    class FailingTransitions(_Jobs):
        async def transition_job(self, _job_id, _status, **_kwargs):
            raise RuntimeError("durable database unavailable")

    jobs = FailingTransitions()
    request = _request()
    adapter = GoalSnapshotToFileAdapter(
        request,
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_principal=_authority_principal(),
    )
    goal = _goal()
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            expires_at=request.deadline_at,
        ),
    )

    result = await adapter.execute(goal=goal, candidate=candidate)

    assert result.execution_status == "blocked"
    assert result.reason.startswith("durable_reconciliation_required:")
    assert result.execution_status != "succeeded"
    assert adapter.last_receipt["reconciliation_required"] is True
