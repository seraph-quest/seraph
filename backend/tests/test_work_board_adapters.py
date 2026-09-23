"""Provider-free proofs for the closed M2 adapter input and identity seams."""

from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import Goal, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.goals.contracts import CriterionVerifierKind, GoalSuccessCriterion
from src.work_board.dispatcher import (
    GOAL_SNAPSHOT_CAPABILITY,
    TypedInputError,
    WorkBoardDispatcher,
    _parse_typed_input,
)
from src.guardian.goal_snapshot_to_file import GoalSnapshotToFileService
from src.goals.contracts import GoalOutcomeReceipt
from src.work_board.repository import WorkBoardRepository


class _VerticalJobs:
    """Small durable-job protocol double; the capability itself is real."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.artifacts: list[dict] = []
        self.readbacks: list[dict] = []

    def _view(self, job_id: str) -> dict:
        row = self.jobs[job_id]
        return {
            "job_id": job_id,
            "status": row["status"],
            "revision": row.get("revision", 1),
            "lease": {"owner": row.get("owner"), "fencing_token": row.get("fence", 1)},
            "effects": list(row.get("effects", [])),
            "artifacts": list(row.get("artifacts", [])),
            "result": row.get("result"),
        }

    async def admit_job(self, spec):
        self.jobs[spec.identity.job_id] = {"status": "accepted", "revision": 1, "effects": [], "artifacts": []}
        return self._view(spec.identity.job_id)

    async def queue_job(self, job_id, **_kwargs):
        self.jobs[job_id]["status"] = "queued"
        self.jobs[job_id]["revision"] += 1
        return self._view(job_id)

    async def claim_job(self, job_id, *, owner, lease_seconds, **_kwargs):
        self.jobs[job_id].update(status="running", owner=owner, fence=1)
        self.jobs[job_id]["revision"] += 1
        return self._view(job_id)

    async def get_job(self, job_id):
        return self._view(job_id) if job_id in self.jobs else None

    async def record_artifact(self, job_id, *, file_path, content, **_kwargs):
        digest = sha256(content).hexdigest()
        item = {"artifact_id": "artifact-real-goal-snapshot", "artifact_type": "goal_snapshot", "file_path": file_path, "content_sha256": digest, "exists": True}
        self.jobs[job_id]["artifacts"].append(item)
        self.artifacts.append(item)
        return {"receipt": item, **self._view(job_id)}

    async def record_readback(self, job_id, **kwargs):
        item = {"receipt_kind": "readback", "status": "succeeded", "verified": True, **kwargs}
        self.jobs[job_id]["effects"].append(item)
        self.jobs[job_id]["revision"] += 1
        self.readbacks.append(item)
        return self._view(job_id)

    async def record_effect(self, job_id, **kwargs):
        self.jobs[job_id]["effects"].append(dict(kwargs))
        self.jobs[job_id]["revision"] += 1
        return self._view(job_id)

    async def transition_job(self, job_id, status, **_kwargs):
        self.jobs[job_id]["status"] = status
        self.jobs[job_id]["revision"] += 1
        return self._view(job_id)


def _write_input(tmp_path, envelope: dict) -> tuple[str, str]:
    path = tmp_path / "inputs" / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return "workspace-json:inputs/task.json", sha256(raw).hexdigest()


def _task(reference: str, digest: str, capability: str = GOAL_SNAPSHOT_CAPABILITY):
    return SimpleNamespace(
        typed_input_ref=reference,
        typed_input_digest=digest,
        capability_id=capability,
    )


def test_typed_input_is_workspace_bound_and_exact(monkeypatch, tmp_path):
    reference, digest = _write_input(
        tmp_path,
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/result.md"},
        },
    )
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    assert _parse_typed_input(_task(reference, digest)) == {"file_path": "artifacts/result.md"}

    extra_reference, extra_digest = _write_input(
        tmp_path,
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/result.md", "authority": "shell"},
        },
    )
    with pytest.raises(TypedInputError) as exc_info:
        _parse_typed_input(_task(extra_reference, extra_digest))
    assert exc_info.value.code == "typed_input_invalid"


def test_typed_input_rejects_traversal_and_symlink(monkeypatch, tmp_path):
    outside = tmp_path.parent / "outside-board-input.json"
    outside.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capability_id": GOAL_SNAPSHOT_CAPABILITY,
                "input": {"file_path": "artifacts/result.md"},
            }
        )
    )
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    escaped = _task(
        "workspace-json:../outside-board-input.json",
        sha256(outside.read_bytes()).hexdigest(),
    )
    with pytest.raises(TypedInputError):
        _parse_typed_input(escaped)

    reference, digest = _write_input(
        tmp_path,
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/result.md"},
        },
    )
    link = tmp_path / "inputs" / "link.json"
    link.symlink_to(outside)
    with pytest.raises(TypedInputError):
        _parse_typed_input(_task("workspace-json:inputs/link.json", digest))


def test_goal_snapshot_builds_only_the_board_wrapper_identity(monkeypatch, tmp_path):
    reference, digest = _write_input(
        tmp_path,
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/result.md"},
        },
    )
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    task = SimpleNamespace(
        task_id="task-1",
        owner_principal_id="operator:one",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=2,
        capability_id=GOAL_SNAPSHOT_CAPABILITY,
        executor_id="executor-local",
        task_revision=1,
        priority=70,
        typed_input_ref=reference,
        typed_input_digest=digest,
    )
    attempt = SimpleNamespace(attempt_id="attempt-1")

    spec, inputs, job_id, owner, runtime = WorkBoardDispatcher()._build_spec(
        task,
        attempt,
        runtime_seconds=9_999,
    )

    assert job_id == "work-board:task-1:attempt-1"
    assert owner == "service:work-board"
    assert runtime == 900
    assert inputs == {"file_path": "artifacts/result.md"}
    assert spec.identity.idempotency_scope == "work-board-attempt"
    assert spec.identity.idempotency_key == "task-1:attempt-1"
    assert spec.parent_job_id is None
    assert spec.declared_authority["finite_authority"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "inputs"),
    [
        ("guardian.research-watch.v1", {"watch_id": "watch-identity", "expected_plan_revision": 1}),
        (
            "engineering.repo-change.v1",
            {
                "candidate_id": "candidate-identity",
                "repository_path": "repo",
                "patch_artifact_id": "patch-identity",
                "patch_sha256": "a" * 64,
                "allowed_paths": ["src/example.py"],
                "test_args": ["python", "-m", "pytest"],
            },
        ),
        (
            "work.github-followthrough.v1",
            {
                "dossier_artifact_id": "dossier-identity",
                "dossier_sha256": "b" * 64,
                "connection_revision": 2,
                "action": "create_issue",
                "title": "Identity proof",
                "body": "Identity proof body",
            },
        ),
        (
            "guardian-routine.v1",
            {
                "routine_id": "routine-identity",
                "version": 1,
                "expected_routine_revision": 1,
                "source_watch_id": "watch-identity",
                "expected_watch_revision": 1,
            },
        ),
    ],
)
async def test_direct_admission_identity_uses_adapter_projection(monkeypatch, capability, inputs):
    """Each direct adapter supplies the immutable digest envelope for linking."""

    task = SimpleNamespace(
        task_id=f"task-identity-{capability.split('.')[0]}",
        owner_principal_id="operator:identity",
        owner_session_id="session-identity",
        goal_id="goal-identity",
        goal_revision=4,
        capability_id=capability,
    )
    attempt = SimpleNamespace(attempt_id="attempt-identity")
    dispatcher = WorkBoardDispatcher()
    job_id, owner, job_kind, service_id, binding_key = dispatcher._direct_job_identity(task, attempt, inputs)
    projection = {
        "job_id": job_id,
        "run_identity": job_id,
        "owner": {
            "kind": "service" if service_id else "user",
            "principal_id": owner,
            "service_id": service_id,
        },
        "job_kind": job_kind,
        "capability_version": dispatcher._direct_capability_version(task),
        "session_id": task.owner_session_id,
        "operator_session_id": task.owner_session_id,
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "input_digest": "1" * 64,
        "authority_digest": "2" * 64,
        "run_fingerprint": "3" * 64,
        "idempotency": {
            "scope": "work-board-attempt",
            "key": binding_key,
            "binding": f"binding:{capability}",
        },
        "declared_authority": (
            {"capability_id": capability}
            if capability != "engineering.repo-change.v1"
            else {}
        ),
    }
    observed_lookup: dict[str, object] = {}

    class _Jobs:
        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            return projection

        async def get_by_idempotency_binding(self, **kwargs):
            observed_lookup.update(kwargs)
            return projection

    async def _admission_only(*_args, admission_only, **_kwargs):
        assert admission_only is True
        return {"job_id": job_id, "status": "running", "admission_only": True}

    dispatcher.jobs = _Jobs()
    monkeypatch.setattr(dispatcher, "_execute_direct_adapter", _admission_only)
    response, found, expected = await dispatcher._canonical_direct_admission(
        task,
        attempt,
        inputs,
        runtime_seconds=300,
    )

    assert response["job_id"] == job_id
    assert found["job_id"] == job_id
    assert expected["job_id"] == job_id
    assert expected["input_digest"] == projection["input_digest"]
    assert expected["authority_digest"] == projection["authority_digest"]
    assert expected["run_fingerprint"] == projection["run_fingerprint"]
    assert observed_lookup["job_kind"] == job_kind
    assert observed_lookup["idempotency_key"] == binding_key
    assert observed_lookup["input_digest"] == projection["input_digest"]


@pytest.mark.asyncio
async def test_goal_snapshot_board_vertical_slice_executes_real_file_and_readback(monkeypatch, tmp_path):
    """Exercise the actual bounded capability behind the board adapter seam."""

    from src.work_board import dispatcher as dispatcher_module

    reference, digest = _write_input(
        tmp_path,
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/board-real.md"},
        },
    )
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goal = SimpleNamespace(
        id="goal-real",
        revision=1,
        status="active",
        owner_principal_id="operator:one",
        owner_session_id="session-one",
        title="Real board goal",
        success_criterion_json=None,
    )
    jobs = _VerticalJobs()
    service_holder = {}

    class _Goals:
        async def get(self, _goal_id):
            return goal

    class _Workflow:
        def get_approval_context(self, _arguments):
            return {"workflow_name": "goal-snapshot-to-file", "risk_level": "low", "execution_boundaries": ["workspace_write"], "step_tools": ["get_goals", "write_file"]}

        def __call__(self, *, file_path, sanitize_inputs_outputs=False):
            target = tmp_path / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Goal snapshot\n- current goal (id=goal-real, active)\n", encoding="utf-8")
            return f"Saved the current goal snapshot to {file_path}."

        def get_audit_result_payload(self, _arguments, _result):
            return "snapshot executed", {"durable_run_identity": "child-goal-real"}

    async def dispatch(candidate, *, adapter):
        result = await adapter.execute(goal=goal, candidate=candidate)
        service_holder["adapter_result"] = result
        service_holder["adapter_receipt"] = adapter.last_receipt
        return GoalOutcomeReceipt(
            receipt_type="outcome",
            outcome_id="outcome-real-board",
            candidate_id=candidate.candidate_id,
            dedupe_key=candidate.dedupe_key,
            goal_id=candidate.goal_id,
            goal_revision=candidate.goal_revision,
            execution_status=result.execution_status,
            verification=result.verification,
            usefulness=result.usefulness,
            learning=result.learning,
            artifact_ref=result.artifact_ref,
            evidence_refs=list(result.evidence_refs),
            reason=result.reason,
        )

    def service_factory(**kwargs):
        service = GoalSnapshotToFileService(
            goals=_Goals(),
            jobs=jobs,
            dispatcher=dispatch,
            workflow_tool_provider=lambda _name: _Workflow(),
            authority_principal=kwargs["authority_principal"],
        )
        service_holder["service"] = service
        return service

    monkeypatch.setattr(dispatcher_module, "GoalSnapshotToFileService", service_factory)
    linked = False
    projected: list[dict] = []

    class _BoardSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class _BoardRepository:
        async def link_attempt_workflow_run(self, _db, *_args, **kwargs):
            nonlocal linked
            assert kwargs["workflow_run_id"] == parent_job_id
            linked = True
            return SimpleNamespace(task=SimpleNamespace(task_revision=2))

        async def project_attempt(self, _db, *_args, **kwargs):
            assert linked is True
            projected.append(dict(kwargs))
            return SimpleNamespace(task=task, attempt=attempt, event=SimpleNamespace(event_id=len(projected)))

    task = SimpleNamespace(
        task_id="task-real",
        owner_principal_id="operator:one",
        owner_session_id="session-one",
        goal_id="goal-real",
        goal_revision=1,
        title="Write the current goal snapshot",
        task_revision=1,
        capability_id=GOAL_SNAPSHOT_CAPABILITY,
        priority=50,
        executor_id="executor-local",
        typed_input_ref=reference,
        typed_input_digest=digest,
        requires_review=False,
    )
    attempt = SimpleNamespace(attempt_id="attempt-real", fencing_token=1)
    dispatcher = WorkBoardDispatcher(jobs=jobs)
    parent_spec, inputs, parent_job_id, _owner, _runtime = dispatcher._build_spec(task, attempt, runtime_seconds=300)
    # The test enters the actual board admission path.  The service callback
    # below asserts that the immutable attempt link was committed first.
    dispatcher.repository = _BoardRepository()
    dispatcher.session_provider = lambda: _BoardSession()

    async def _runtime(_task):
        return 300

    dispatcher._effective_runtime = _runtime
    claim = SimpleNamespace(task=task, attempt=attempt, event=SimpleNamespace(event_id=1))
    outcome = await dispatcher._admit_execute_project(claim)

    assert linked is True
    assert projected and projected[-1]["status"].value == "done"
    assert any(
        effect.get("receipt_kind") == "readback"
        and (effect.get("verified") or (effect.get("details") or {}).get("verified"))
        for effect in jobs.jobs[parent_job_id]["effects"]
    )

    assert outcome["completed"] is True, json.dumps(
        {
            "outcome": outcome,
            "adapter_result": service_holder.get("adapter_result"),
            "receipt": service_holder.get("adapter_receipt"),
        },
        default=str,
    )
    assert (tmp_path / "artifacts/board-real.md").is_file()
    assert jobs.artifacts and jobs.readbacks
    parent = await jobs.get_job(parent_job_id)
    assert parent["status"] == "succeeded", parent


async def _run_real_board_goal_snapshot(async_db, monkeypatch, tmp_path, *, observe_link: bool = False):
    """Run the board path against the real SQLite and durable job stores."""

    reference, digest = _write_input(
        tmp_path,
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/managed-board-snapshot.md"},
        },
    )
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goal_id = "goal-managed-board"
    task_id = "task-managed-board"
    criterion = GoalSuccessCriterion(
        criterion_id="managed-snapshot",
        description="A readable managed snapshot exists",
        verifier_kind=CriterionVerifierKind.artifact_readback,
        target={"file_path": "artifacts/managed-board-snapshot.md"},
        evidence_refs=["operator:managed-local-proof"],
    )
    task = WorkBoardTask(
        task_id=task_id,
        owner_principal_id="operator:managed",
        owner_session_id="managed-session",
        goal_id=goal_id,
        goal_revision=1,
        title="Write a managed goal snapshot",
        idempotency_key=task_id,
        capability_id=GOAL_SNAPSHOT_CAPABILITY,
        typed_input_ref=reference,
        typed_input_digest=digest,
        executor_id="executor-local",
        priority=80,
        status=WorkBoardStatus.ready,
    )
    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Managed board goal",
                status="active",
                revision=1,
                owner_principal_id="operator:managed",
                owner_session_id="managed-session",
                success_criterion_json=criterion.model_dump_json(),
            )
        )
        db.add(task)
        await db.commit()

    class _ManagedWorkflow:
        name = "workflow_goal_snapshot_to_file"

        def get_approval_context(self, _arguments):
            return {
                "workflow_name": "goal-snapshot-to-file",
                "workflow_version": "1",
                "execution_boundaries": ["workspace_write"],
                "step_tools": ["get_goals", "write_file"],
            }

        def __call__(self, *, file_path, sanitize_inputs_outputs=False, **_kwargs):
            del sanitize_inputs_outputs
            target = tmp_path / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "Goal snapshot\n"
                f"Goal id: {goal_id}\n"
                "Status: active\n",
                encoding="utf-8",
            )
            return f"Saved {file_path}"

        def get_audit_result_payload(self, _arguments, _result):
            return "managed local workflow executed", {"durable_run_identity": "managed-local-child"}

    workflow_tool = _ManagedWorkflow()
    from src.agent import factory as agent_factory
    from src.workflows import manager as manager_module

    monkeypatch.setattr(
        agent_factory,
        "get_tools",
        lambda **_kwargs: [workflow_tool],
    )
    monkeypatch.setattr(
        manager_module.workflow_manager,
        "get_workflow",
        lambda _name: SimpleNamespace(enabled=True, tool_name=workflow_tool.name),
    )

    repository = WorkBoardRepository()
    async with async_db() as db:
        claim = await repository.claim_ready_task(
            db,
            task_id,
            expected_revision=task.task_revision,
            lease_owner="service:work-board",
            actor_principal_id="service:work-board",
            actor_session_id="service:work-board:session",
        )
        assert claim is not None
        await db.commit()

    if observe_link:
        original_run = GoalSnapshotToFileService.run

        async def observed_run(service, request, **kwargs):
            async with async_db() as db:
                linked_attempt = (
                    await db.execute(
                        select(WorkBoardAttempt).where(
                            WorkBoardAttempt.attempt_id == claim.attempt.attempt_id,
                        )
                    )
                ).scalar_one()
                assert linked_attempt.workflow_run_id == request.parent_job_id
            return await original_run(service, request, **kwargs)

        monkeypatch.setattr(GoalSnapshotToFileService, "run", observed_run)

    dispatcher = WorkBoardDispatcher(repository=repository)
    outcome = await dispatcher._admit_execute_project(claim)
    async with async_db() as db:
        stored_task = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == task_id)
            )
        ).scalar_one()
        stored_attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.attempt_id == claim.attempt.attempt_id,
                )
            )
        ).scalar_one()
        runs = list((await db.execute(select(WorkflowRunState))).scalars().all())
    return outcome, stored_task, stored_attempt, runs


@pytest.mark.asyncio
async def test_goal_snapshot_executes_and_reads_back(async_db, monkeypatch, tmp_path):
    """The board proves real durable admission, execution, artifact, and readback."""

    outcome, task, attempt, runs = await _run_real_board_goal_snapshot(
        async_db,
        monkeypatch,
        tmp_path,
    )

    assert outcome["admitted"] is True, outcome
    assert outcome["completed"] is True, outcome
    assert task.status is WorkBoardStatus.done
    assert attempt.workflow_run_id == f"work-board:{task.task_id}:{attempt.attempt_id}"
    assert (tmp_path / "artifacts/managed-board-snapshot.md").is_file()
    content = (tmp_path / "artifacts/managed-board-snapshot.md").read_text(encoding="utf-8")
    assert task.goal_id in content
    assert len(runs) == 2
    root = next(run for run in runs if run.run_identity == attempt.workflow_run_id)
    child = next(run for run in runs if run.run_identity == f"goal-snapshot-work-board:{task.task_id}:{attempt.attempt_id}")
    assert root.status == child.status == "succeeded"
    assert child.parent_run_identity == root.run_identity
    root_effects = json.loads(root.metadata_json or "{}") if root.metadata_json else {}
    assert root_effects is not None


@pytest.mark.asyncio
async def test_attempt_run_link_persisted_before_adapter_execution(async_db, monkeypatch, tmp_path):
    """A real registered GoalSnapshot adapter sees its immutable link first."""

    outcome, task, attempt, _runs = await _run_real_board_goal_snapshot(
        async_db,
        monkeypatch,
        tmp_path,
        observe_link=True,
    )
    assert outcome["completed"] is True, outcome
    assert attempt.workflow_run_id == f"work-board:{task.task_id}:{attempt.attempt_id}"
