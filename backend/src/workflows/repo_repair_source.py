"""Private source bindings for the fixed repository iteration owners.

These objects are deliberately not request models. A serialized projection is
evidence, and cannot recreate an execution or accounting witness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
from typing import Any
from contextlib import asynccontextmanager
from contextvars import ContextVar

from src.work_board.contracts import (TaskProposalGroupV1,
    GeneralTaskNativeChildBindingV1, RepositoryTaskSourceBinding)

_SEAL = object()
_SHA = re.compile(r"^[0-9a-f]{64}$")
_task_publication = ContextVar("repository_task_publication", default=None)


def prepare_repository_task_publication(service, envelope, *, owner, goal_revision, replay=False):
    """Inspect the fixed source before publication; never accept caller metadata.

    The scope stages physical checks before the SQLite writer. Its callback
    validates only the original SQL authority and captured configuration.
    """
    import asyncio
    from src.workflows.repo_repair import RepoRepairService, RepoWorkInput
    from src.work_board.repository import BoardError
    from src.security.trust_contract import canonical_digest
    if replay and envelope.repository_source is None:
        raise BoardError("repository_source_unscoped_replay", "Old Tasks cannot acquire repository source authority on replay", status_code=409)
    if type(service) is not RepoRepairService or envelope.plan is None:
        raise BoardError("repository_source_owner_required", "Current fixed repository source owner required", status_code=409)
    steps = [step for step in envelope.plan.steps if step.tool_id == "repository_work"]
    if len(steps) != 1 or envelope.proposal_group is None:
        raise BoardError("repository_source_binding_invalid", "One original grouped repository step required", status_code=409)
    work = RepoWorkInput.model_validate(steps[0].input)
    group = envelope.proposal_group
    if (group.owner_principal_id != owner.principal_id or group.owner_session_id != owner.session_id
            or group.goal_id != envelope.task_input.goal_ref or group.goal_revision != goal_revision):
        raise BoardError("repository_source_binding_invalid", "Original owner and Goal required", status_code=409)
    if replay:
        binding = envelope.repository_source
        if binding is None:
            raise BoardError("repository_source_unscoped_replay", "Old Tasks cannot acquire repository source authority on replay", status_code=409)
    else:
        if envelope.repository_source is not None:
            raise BoardError("repository_source_caller_metadata", "Source metadata must be minted during original publication", status_code=409)
        binding = service.stage_task_source(work, owner=owner,
            goal_id=group.goal_id, goal_revision=goal_revision)
    projection = binding.model_dump(mode="json", exclude={"schema_version", "binding_digest"})
    if (binding.binding_digest != canonical_digest(projection)
            or binding.original_input_digest != canonical_digest(work.model_dump(mode="json"))
            or binding.owner_principal_id != owner.principal_id
            or binding.original_root_id != owner.session_id
            or binding.goal_id != group.goal_id or binding.goal_revision != goal_revision):
        raise BoardError("repository_source_binding_invalid", "Original source binding changed", status_code=409)
    witness = object()

    @asynccontextmanager
    async def scope():
        from src.model_fabric.effective_policy import configuration_mutation_lock
        async with configuration_mutation_lock:
            # Physical work precedes the publication writer, including replay.
            facts = json.loads(service._read_private_artifact(binding.source_artifact_ref,
                expected_digest=binding.source_artifact_digest))
            compiled = service.recheck_task_source_snapshot(work, facts)
            if (facts.get("original_input") != work.model_dump(mode="json")
                    or facts.get("compiled_input") != compiled.model_dump(mode="json")
                    or canonical_digest(service.sandbox.config.model_dump(mode="json")) != binding.executor_profile_digest
                    or facts.get("snapshot_manifest", {}).get("digest") != binding.snapshot_digest
                    or facts.get("owner_principal_id") != owner.principal_id
                    or facts.get("original_root_id") != owner.session_id
                    or facts.get("goal_id") != group.goal_id or facts.get("goal_revision") != goal_revision):
                raise BoardError("repository_source_changed", "Original physical source or execution profile changed", status_code=409)
            token = _task_publication.set((asyncio.current_task(), witness))
            try:
                yield
            finally:
                _task_publication.reset(token)

    async def check(db):
        from src.workflows.general_task_accounting import validate_group_owner
        if _task_publication.get() != (asyncio.current_task(), witness):
            raise BoardError("repository_source_scope_required", "Original source publication scope required", status_code=409)
        if canonical_digest(service.sandbox.config.model_dump(mode="json")) != binding.executor_profile_digest:
            raise BoardError("repository_source_changed", "Original execution profile changed", status_code=409)
        await validate_group_owner(db, group)

    return binding, check, scope


def read_repository_original(run):
    """Only the protected canonical journal supplies original compilation."""
    from src.workflows.general_task_guard import _history
    from src.workflows.job_runtime import _digest, DurableJobLeaseError
    from src.workflows.repo_repair import RepoWorkInput, RepoRepairInput
    records = [record for record in _history(run)
               if record.get("checkpoint_id") == "repository:original:v1"]
    if len(records) != 1:
        raise DurableJobLeaseError("original fixed repository source checkpoint required")
    record = records[0]
    payload = record.get("payload")
    if (record.get("safe") is not True or not isinstance(payload, dict)
            or record.get("state_digest") != _digest(payload)):
        raise DurableJobLeaseError("original fixed repository source checkpoint changed")
    required = {"schema_version", "repository_job_id", "repository_task_id", "repository_attempt_id",
        "repository_input_artifact_digest", "original_input", "compiled_input", "original_deadline_at",
        "original_max_cost_microusd", "group", "native_binding", "source_binding"}
    if set(payload) != required or payload["schema_version"] != "repository.original.v1":
        raise DurableJobLeaseError("original fixed repository source shape changed")
    try:
        work = RepoWorkInput.model_validate(payload["original_input"])
        compiled = RepoRepairInput.model_validate(payload["compiled_input"])
        group = TaskProposalGroupV1.model_validate(payload["group"])
        binding = GeneralTaskNativeChildBindingV1.model_validate(payload["native_binding"])
        source = RepositoryTaskSourceBinding.model_validate(payload["source_binding"])
        cutoff = TaskProposalGroupV1.utc_timestamp(payload["original_deadline_at"])
        from src.workflows.inference_accounting import _utc
        from src.security.trust_contract import canonical_digest
        if (payload["repository_job_id"] != run.run_identity
                or payload["repository_input_artifact_digest"] != run.input_digest
                or type(payload["original_max_cost_microusd"]) is not int
                or payload["original_max_cost_microusd"] != work.limits.max_cost_microusd
                or canonical_digest(work.model_dump(mode="json")) != source.original_input_digest
                or compiled.repository_path != work.repository_ref
                or compiled.allowed_paths != work.allowed_paths
                or cutoff > group.original_deadline_at or cutoff > binding.original_deadline_at
                or cutoff > binding.native_deadline_at
                or cutoff > _utc(run.started_at) + timedelta(seconds=work.limits.max_seconds)
                or _utc(run.deadline_at) != cutoff
                or group.owner_principal_id != binding.owner_principal_id
                or group.owner_session_id != binding.original_root_id
                or group.goal_id != binding.goal_id or group.goal_revision != binding.goal_revision
                or source.owner_principal_id != run.owner_principal_id
                or source.original_root_id != run.operator_session_id
                or source.goal_id != run.goal_id or source.goal_revision != run.goal_revision):
            raise ValueError("original source/cutoff changed")
    except (ValueError, TypeError, KeyError) as exc:
        raise DurableJobLeaseError("original fixed repository source binding changed") from exc
    return payload, work, compiled, group, binding, source


def iteration_identity(repository_job_id: str, repository_attempt_id: str,
                       original_input_digest: str, index: int) -> str:
    if (not repository_job_id or not repository_attempt_id
            or not _SHA.fullmatch(original_input_digest) or type(index) is not int
            or not 1 <= index <= 3):
        raise ValueError("original repository binding and bounded index required")
    payload = ["seraph.repository_iteration.v1", repository_job_id,
               repository_attempt_id, original_input_digest, index]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RepoIterationProcessBinding:
    repository_job_id: str
    repository_attempt_id: str
    repository_fence: int
    iteration_index: int
    iteration_id: str
    original_deadline_at: datetime
    authority_digest: str
    base_digest: str
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        return {name: (value.isoformat() if isinstance(value, datetime) else value)
                for name in self.__dataclass_fields__ if not name.startswith("_")
                for value in [getattr(self, name)]}


def assert_repo_iteration_process_binding(binding, job, *, allow_expired_for_cleanup=False) -> None:
    from src.execution.repo_sandbox import RepoSandboxError
    if (type(binding) is not RepoIterationProcessBinding or binding._seal is not _SEAL
            or type(binding.iteration_index) is not int or not 1 <= binding.iteration_index <= 3
            or not _SHA.fullmatch(binding.iteration_id)
            or binding.repository_job_id != job.job_id
            or binding.repository_attempt_id != job.attempt_id
            or type(binding.repository_fence) is not int or binding.repository_fence < 1
            or binding.repository_fence != job.fencing_token
            or binding.authority_digest != job.authority_digest
            or binding.base_digest != job.base_digest
            or binding.original_deadline_at.tzinfo is None
            or (not allow_expired_for_cleanup and binding.original_deadline_at <= datetime.now(timezone.utc))
            or not job.execution_deadline_at):
        raise RepoSandboxError("original source-issued iteration binding required")
    try:
        command_deadline = datetime.fromisoformat(job.execution_deadline_at.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise RepoSandboxError("fixed iteration command deadline required") from exc
    if command_deadline.tzinfo is None or command_deadline > binding.original_deadline_at:
        raise RepoSandboxError("command cannot extend the original repository cutoff")


@dataclass(frozen=True, slots=True)
class _CanonicalRepositorySource:
    """Immutable physical-source staging projected onto exact SQL fences."""
    parent_revision: int
    parent_checkpoint_json: str
    parent_authority_json: str
    parent_envelope_json: str
    task_revision: int
    parent_attempt_fence: int
    repository_attempt_fence: int
    child_revision: int
    child_fence: int
    child_authority_json: str
    child_arguments_json: str
    native_binding_json: str
    parent_row_json: str
    task_row_json: str
    parent_attempt_row_json: str
    repository_attempt_row_json: str
    child_row_json: str
    repository_task_row_json: str
    input_artifact_row_json: str
    consent_row_json: str
    consent_id: str
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        assert_repository_canonical_source(self)
        return {name: getattr(self, name) for name in self.__dataclass_fields__
                if not name.startswith("_")}


def assert_repository_canonical_source(source) -> None:
    """Only the actual source owner can stamp a staged SQL/physical binding."""
    from src.workflows.job_runtime import DurableJobLeaseError
    if (type(source) is not _CanonicalRepositorySource or source._seal is not _SEAL
            or any(not getattr(source, name) for name in (
                "parent_row_json", "task_row_json", "parent_attempt_row_json",
                "parent_envelope_json",
                "repository_attempt_row_json", "child_row_json", "repository_task_row_json",
                "input_artifact_row_json", "consent_row_json", "consent_id", "native_binding_json"))):
        raise DurableJobLeaseError("original producer-sealed repository canonical source required")


async def stage_repository_canonical_source(service, db, *, repository_job_id,
        native_invocation_id, consent_id):
    """Read actual physical artifacts before either canonical writer.

    This cannot bootstrap a root: the protected original handoff must already
    exist, with the same seven-field input and source-issued Task artifact.
    """
    from src.db.models import (WorkflowRunState, WorkBoardTask, WorkBoardAttempt,
        WorkBoardInputArtifact, RepoRepairEgressConsent)
    from src.workflows.repo_repair import RepoRepairService
    from src.work_board.contracts import WorkBoardOwner
    from src.workflows.general_task_guard import (child_binding, read_manifest,
        assert_general_task_child_current)
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(service) is not RepoRepairService:
        raise DurableJobLeaseError("actual fixed repository source owner required")
    run = await db.get(WorkflowRunState, repository_job_id, populate_existing=True)
    child = await db.get(WorkflowRunState, native_invocation_id, populate_existing=True)
    if run is None or child is None:
        raise DurableJobLeaseError("original repository and native child required")
    original, work, compiled, group, binding, task_source = read_repository_original(run)
    if child_binding(child) != binding:
        raise DurableJobLeaseError("original repository native child changed")
    await assert_general_task_child_current(db, child)
    parent = await db.get(WorkflowRunState, binding.parent_job_id, populate_existing=True)
    task = await db.get(WorkBoardTask, binding.task_id, populate_existing=True)
    attempt = await db.get(WorkBoardAttempt, binding.attempt_id, populate_existing=True)
    if parent is None or task is None or attempt is None:
        raise DurableJobLeaseError("original C1 Task source required")
    manifest = read_manifest(parent)
    if manifest is None:
        raise DurableJobLeaseError("original C1 manifest required")
    envelope = await verify_general_task_manifest(db, parent, task, attempt, manifest)
    if envelope.repository_source != task_source or envelope.proposal_group != group:
        raise DurableJobLeaseError("original scoped repository Task artifact required")
    repo_authority = await service._resolve_canonical_authority(db,
        owner=WorkBoardOwner(principal_id=run.owner_principal_id, session_id=run.operator_session_id),
        work_board_task_id=original["repository_task_id"],
        work_board_attempt_id=original["repository_attempt_id"], workflow_run_id=run.run_identity,
        goal_id=run.goal_id, goal_revision=run.goal_revision,
        lease_owner=run.lease_owner, fencing_token=run.fencing_token, consent_id=consent_id)
    if repo_authority.original_work_input != work or repo_authority.input != compiled:
        raise DurableJobLeaseError("original seven-field input or compilation changed")
    input_artifact = await db.get(WorkBoardInputArtifact, repo_authority.task.input_artifact_id)
    consent = await db.get(RepoRepairEgressConsent, consent_id, populate_existing=True)
    if (input_artifact is None or consent is None or consent.workflow_run_id != run.run_identity
            or not consent.iteration_id or consent.diagnostics_acknowledged is not True
            or consent.egress_envelope_schema != "seraph.repo_iteration_egress.v1"
            or not consent.diagnostics_sha256 or not consent.redaction_version
            or not consent.serialized_request_sha256
            or type(consent.combined_input_bytes) is not int
            or not 0 < consent.combined_input_bytes <= min(65536, consent.maximum_input_bytes)
            or consent.owner_principal_id != run.owner_principal_id
            or consent.owner_session_id != run.operator_session_id):
        raise DurableJobLeaseError("exact fresh repository diagnostics consent required")
    egress = json.loads(service._read_private_artifact(consent.egress_envelope_artifact_ref,
        expected_digest=consent.egress_envelope_sha256))
    from src.workflows.repo_repair import _digest_bytes, _canonical_bytes
    if (egress.get("schema") != consent.egress_envelope_schema
            or egress.get("iteration_id") != consent.iteration_id
            or _digest_bytes(_canonical_bytes(egress.get("diagnostics"))) != consent.diagnostics_sha256
            or egress.get("diagnostics", {}).get("redaction_version") != consent.redaction_version):
        raise DurableJobLeaseError("exact fresh repository diagnostics envelope changed")
    def row_json(row):
        return json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    source = _CanonicalRepositorySource(
        parent_revision=parent.revision, parent_checkpoint_json=parent.checkpoint_receipts_json,
        parent_authority_json=parent.declared_authority_json, task_revision=task.task_revision,
        parent_envelope_json=json.dumps(envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        parent_attempt_fence=attempt.fencing_token,
        repository_attempt_fence=repo_authority.attempt.fencing_token,
        child_revision=child.revision, child_fence=child.fencing_token,
        child_authority_json=child.declared_authority_json, child_arguments_json=child.arguments_json,
        native_binding_json=json.dumps(binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        parent_row_json=row_json(parent), task_row_json=row_json(task),
        parent_attempt_row_json=row_json(attempt), repository_attempt_row_json=row_json(repo_authority.attempt),
        child_row_json=row_json(child), repository_task_row_json=row_json(repo_authority.task),
        input_artifact_row_json=row_json(input_artifact), consent_row_json=row_json(consent),
        consent_id=consent.id, _seal=_SEAL)
    assert_repository_canonical_source(source)
    return source


@dataclass(frozen=True, slots=True)
class RepositoryIterationAccountingWitness:
    group: TaskProposalGroupV1
    repository_job_id: str
    repository_attempt_id: str
    repository_fence: int
    parent_task_id: str
    parent_attempt_id: str
    native_invocation_id: str
    iteration_index: int
    iteration_id: str
    operation_id: str
    original_deadline_at: datetime
    original_max_cost_microusd: int
    source_checkpoint_digest: str
    _request_body_digest: str | None = field(default=None, repr=False, compare=False)
    _request_route_digest: str | None = field(default=None, repr=False, compare=False)
    _canonical_source: _CanonicalRepositorySource | None = field(default=None, repr=False, compare=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__
                  if not name.startswith("_")}
        result["group"] = self.group.model_dump(mode="json")
        result["original_deadline_at"] = self.original_deadline_at.isoformat()
        return result


def assert_repository_iteration_witness(witness) -> None:
    from src.workflows.inference_accounting import InferenceAccountingError
    if (type(witness) is not RepositoryIterationAccountingWitness or witness._seal is not _SEAL
            or type(witness.group) is not TaskProposalGroupV1
            or type(witness.iteration_index) is not int or not 1 <= witness.iteration_index <= 3
            or not _SHA.fullmatch(witness.iteration_id)
            or not _SHA.fullmatch(witness.source_checkpoint_digest)
            or witness.operation_id != "remote:repo-work:" + witness.iteration_id
            or type(witness.repository_fence) is not int or witness.repository_fence < 1
            or type(witness.original_max_cost_microusd) is not int
            or witness.original_max_cost_microusd < 0
            or witness.original_deadline_at.tzinfo is None
            or witness.original_deadline_at > witness.group.original_deadline_at
            or any(not value for value in (witness.repository_job_id, witness.repository_attempt_id,
                witness.parent_task_id, witness.parent_attempt_id, witness.native_invocation_id))):
        raise InferenceAccountingError("repository_iteration_source_witness_required")


def repository_transport_route_digest(target: dict, runtime_path: str) -> str:
    from src.security.trust_contract import canonical_digest
    return canonical_digest({"runtime_path": runtime_path, "source": target.get("source"),
        "endpoint": str(target.get("api_base") or "").rstrip("/"),
        "profile": target.get("profile"), "model_id": target.get("model_id"),
        "options": target.get("options")})


def verify_repository_iteration_transport_body(body, *, target, runtime_path) -> None:
    """Only a genuine source context can require the fixed iteration body."""
    from src.model_fabric.accounting import current_repository_iteration_accounting_witness
    from src.security.trust_contract import canonical_digest
    from src.workflows.inference_accounting import InferenceAccountingError
    witness = current_repository_iteration_accounting_witness()
    if witness is None:
        return
    assert_repository_iteration_witness(witness)
    if (runtime_path != "strategist_agent" or target.get("source") != "primary"
            or str(target.get("api_base") or "").rstrip("/") != "https://openrouter.ai/api/v1"
            or witness._request_body_digest is None or witness._request_route_digest is None
            or canonical_digest(body) != witness._request_body_digest
            or repository_transport_route_digest(target, runtime_path) != witness._request_route_digest):
        raise InferenceAccountingError("repository_iteration_final_transport_changed")


async def validate_repository_iteration_accounting(db, witness, run, *, rows,
        operation_id, payload_digest, bound_microusd, deadline_at,
        already_reserved=False) -> dict[str, Any]:
    """Recheck private source and both original owners in the accounting writer."""
    from sqlalchemy import select
    from src.db.models import (WorkflowRunState, WorkBoardAttempt, WorkBoardTask,
        WorkBoardInputArtifact, RepoRepairEgressConsent)
    from src.workflows.general_task_guard import _history, child_binding, read_manifest
    from src.workflows.inference_accounting import InferenceAccountingError, _utc
    assert_repository_iteration_witness(witness)
    source = witness._canonical_source
    if (type(source) is not _CanonicalRepositorySource or source._seal is not _SEAL
            or any(not getattr(source, name) for name in (
                "parent_row_json", "task_row_json", "parent_attempt_row_json",
                "repository_attempt_row_json", "child_row_json", "repository_task_row_json",
                "input_artifact_row_json", "consent_row_json", "consent_id"))):
        raise InferenceAccountingError("repository_iteration_staged_source_required")
    now = datetime.now(timezone.utc)
    def blocked(code="repository_iteration_binding_changed"):
        raise InferenceAccountingError(code)
    if (run.run_identity != witness.repository_job_id or run.status != "running"
            or run.fencing_token != witness.repository_fence
            or not run.lease_owner or run.lease_expires_at is None
            or _utc(run.lease_expires_at) <= now or run.attempt_count != 1
            or operation_id != witness.operation_id or not _SHA.fullmatch(payload_digest)
            or type(bound_microusd) is not int or bound_microusd < 0
            or _utc(deadline_at) > witness.original_deadline_at
            or _utc(run.deadline_at) > witness.original_deadline_at
            or witness.original_deadline_at <= now):
        blocked()
    repo_attempt = await db.scalar(select(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == witness.repository_attempt_id))
    parent_attempt = await db.scalar(select(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == witness.parent_attempt_id))
    task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == witness.parent_task_id))
    child = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == witness.native_invocation_id))
    if (repo_attempt is None or repo_attempt.workflow_run_id != run.run_identity
            or parent_attempt is None or task is None or child is None
            or parent_attempt.task_id != task.task_id):
        blocked()
    repo_task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == repo_attempt.task_id))
    input_artifact = await db.get(WorkBoardInputArtifact, repo_task.input_artifact_id) if repo_task else None
    consent = await db.get(RepoRepairEgressConsent, source.consent_id)
    def row_json(row):
        return json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == parent_attempt.workflow_run_id))
    if (parent is None or repo_task is None or input_artifact is None or consent is None
            or parent.status != "paused"
            or row_json(parent) != source.parent_row_json or row_json(task) != source.task_row_json
            or row_json(parent_attempt) != source.parent_attempt_row_json
            or row_json(repo_attempt) != source.repository_attempt_row_json
            or row_json(child) != source.child_row_json
            or row_json(repo_task) != source.repository_task_row_json
            or row_json(input_artifact) != source.input_artifact_row_json
            or row_json(consent) != source.consent_row_json
            or repo_attempt.ended_at is not None or repo_attempt.cancel_requested_at is not None
            or parent_attempt.ended_at is not None or parent_attempt.cancel_requested_at is not None
            or parent.revision != source.parent_revision
            or parent.checkpoint_receipts_json != source.parent_checkpoint_json
            or parent.declared_authority_json != source.parent_authority_json
            or task.task_revision != source.task_revision
            or parent_attempt.fencing_token != source.parent_attempt_fence
            or repo_attempt.fencing_token != source.repository_attempt_fence
            or child.revision != source.child_revision or child.fencing_token != source.child_fence
            or child.declared_authority_json != source.child_authority_json
            or child.arguments_json != source.child_arguments_json
            or child.status != "running" or child.attempt_count != 1
            or not child.lease_owner or child.lease_expires_at is None
            or _utc(child.lease_expires_at) <= now or _utc(child.deadline_at) <= now):
        blocked()
    binding = child_binding(child)
    manifest = read_manifest(parent)
    if (manifest is None or manifest.phase != "native_wait"
            or binding.task_id != task.task_id or binding.attempt_id != parent_attempt.attempt_id
            or binding.parent_job_id != parent.run_identity
            or binding.invocation_id not in manifest.admitted_invocation_ids
            or json.loads(source.native_binding_json) != binding.model_dump(mode="json")):
        blocked()
    from src.work_board.general_task import digest
    if (manifest.group_id != witness.group.group_id
            or manifest.group_digest != digest(witness.group.model_dump(mode="json"))):
        blocked("repository_iteration_original_group_changed")
    history = _history(run)
    originals = [item for item in history if item.get("checkpoint_id") == "repository:original:v1"]
    iterations = [item for item in history if item.get("checkpoint_id") ==
                  "repository:iteration:" + witness.iteration_id]
    if len(originals) != 1 or len(iterations) != 1:
        blocked("repository_iteration_source_checkpoint_missing")
    original, iteration = originals[0], iterations[0]
    original_payload, iteration_payload = original.get("payload"), iteration.get("payload")
    from src.workflows.job_runtime import _digest
    if (original.get("safe") is not True or iteration.get("safe") is not True
            or not isinstance(original_payload, dict) or not isinstance(iteration_payload, dict)
            or original.get("state_digest") != _digest(original_payload)
            or original.get("state_digest") != witness.source_checkpoint_digest
            or iteration.get("state_digest") != _digest(iteration_payload)
            or original_payload.get("native_binding") != binding.model_dump(mode="json")
            or original_payload.get("group") != witness.group.model_dump(mode="json")
            or original_payload.get("original_max_cost_microusd") != witness.original_max_cost_microusd
            or original_payload.get("original_deadline_at") != witness.original_deadline_at.isoformat()
            or iteration_payload.get("accounting_binding") != witness.projection()
            or iteration_payload.get("payload_digest") != payload_digest
            or iteration_payload.get("phase") != "model_ready"):
        blocked()
    members = [row for row in rows if row.job_id == witness.repository_job_id]
    existing = [row for row in members if row.operation_id == operation_id]
    if already_reserved != bool(existing) or len(existing) > 1:
        blocked("repository_iteration_reservation_identity_changed")
    from src.workflows.general_task_accounting import reservation_liability, entry_for
    liability = 0
    for row in members:
        entry = entry_for(row)
        member_binding = entry.get("repository_binding") if entry else None
        if (not entry or entry.get("role") != "repository_iteration"
                or not isinstance(member_binding, dict)
                or member_binding.get("source_checkpoint_digest") != witness.source_checkpoint_digest
                or member_binding.get("repository_job_id") != witness.repository_job_id
                or member_binding.get("group") != witness.group.model_dump(mode="json")):
            blocked("repository_iteration_reservation_source_changed")
        if row.state == "unknown":
            blocked("repository_iteration_unknown")
        liability += reservation_liability(row)
    if liability + (0 if already_reserved else bound_microusd) > witness.original_max_cost_microusd:
        blocked("repository_iteration_original_cost_limit")
    return witness.projection()
