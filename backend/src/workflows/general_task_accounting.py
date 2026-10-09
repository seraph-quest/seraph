"""Closed GeneralTask group checks inside the existing reservation writer."""
from datetime import datetime, timezone
import json
from typing import Literal
from pydantic import Field, ValidationError, model_validator, field_validator

from src.work_board.contracts import (TaskProposalGroupV1, ClosedTaskModel,
    TaskDigest, TaskIdentity, NativeInvocationIdentity)
from src.workflows.inference_accounting import InferenceAccountingError, _utc

KIND = "general_task_group_reservation.v1"


class RepositoryIterationEvidenceV1(ClosedTaskModel):
    group: TaskProposalGroupV1
    repository_job_id: NativeInvocationIdentity
    repository_attempt_id: TaskIdentity
    repository_fence: int = Field(ge=1)
    parent_task_id: TaskIdentity
    parent_attempt_id: TaskIdentity
    native_invocation_id: NativeInvocationIdentity
    iteration_index: int = Field(ge=1, le=3)
    iteration_id: TaskDigest
    operation_id: NativeInvocationIdentity
    original_deadline_at: datetime
    original_max_cost_microusd: int = Field(ge=0)
    source_checkpoint_digest: TaskDigest

    _utc_timestamp = field_validator("original_deadline_at", mode="before")(TaskProposalGroupV1.utc_timestamp.__func__)

    @model_validator(mode="after")
    def original_iteration(self):
        if (self.operation_id != f"remote:repo-work:{self.iteration_id}"
            or self.original_deadline_at.tzinfo is None
            or self.original_deadline_at > self.group.original_deadline_at):
            raise ValueError("exact original repository iteration identity and cutoff required")
        return self


class GeneralTaskGroupReservationEvidenceV1(ClosedTaskModel):
    kind: Literal["general_task_group_reservation.v1"] = KIND
    group: TaskProposalGroupV1
    group_digest: TaskDigest
    role: Literal["initial_proposal", "continuation", "repository_iteration"]
    call_ordinal: int = Field(ge=1, le=12)
    original_operation_id: NativeInvocationIdentity
    original_job_id: NativeInvocationIdentity
    initial_proposal_operation_id: NativeInvocationIdentity | None
    task_id: TaskIdentity | None
    task_attempt_id: TaskIdentity | None
    plan_revision: int = Field(ge=0, le=16)
    selected_grant_digest: TaskDigest | None
    parent_owner: NativeInvocationIdentity | None
    parent_fence: int | None = Field(ge=1)
    repository_binding: RepositoryIterationEvidenceV1 | None = None

    @model_validator(mode="after")
    def exact_role(self):
        from src.work_board.general_task import digest
        if self.group_digest != digest(self.group.model_dump(mode="json")):
            raise ValueError("original proposal group digest changed")
        if self.role != "repository_iteration" and self.repository_binding is not None:
            raise ValueError("repository binding requires its source-specific role")
        if self.role == "repository_iteration":
            repository = self.repository_binding
            if (repository is None or repository.group != self.group
                or repository.operation_id != self.original_operation_id
                or repository.repository_job_id != self.original_job_id
                or repository.parent_task_id != self.task_id or repository.parent_attempt_id != self.task_attempt_id
                or self.plan_revision != 0 or self.selected_grant_digest is not None
                or self.parent_owner is not None or self.parent_fence is not None):
                raise ValueError("repository iteration cannot carry ordinary continuation authority")
        elif self.role == "initial_proposal":
            if (self.call_ordinal != 1 or self.initial_proposal_operation_id != self.original_operation_id
                or self.task_id is not None or self.task_attempt_id is not None or self.plan_revision != 0
                or self.selected_grant_digest is not None or self.parent_owner is not None or self.parent_fence is not None):
                raise ValueError("initial proposal cannot carry continuation authority")
        elif (not self.task_id or not self.task_attempt_id or self.plan_revision < 1
            or self.selected_grant_digest is None or not self.parent_owner or self.parent_fence is None):
            raise ValueError("continuation requires its exact captured native evidence")
        return self


def entry_for(row):
    try:
        entries = json.loads(row.evidence_json)
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError()
        found = [entry for entry in entries if entry.get("kind") == KIND]
    except (ValueError, TypeError) as exc:
        raise InferenceAccountingError("general_task_group_evidence_invalid") from exc
    if len(found) > 1:
        raise InferenceAccountingError("general_task_group_evidence_invalid")
    if not found:
        return None
    try:
        entry = GeneralTaskGroupReservationEvidenceV1.model_validate(found[0])
        if (entry.original_operation_id != row.operation_id or entry.original_job_id != row.job_id
            or entry.group.owner_principal_id != row.owner_id or entry.group.goal_id != row.goal_id
            or entry.group.goal_revision != row.goal_revision):
            raise ValueError("reservation evidence belongs to another canonical row")
        return entry.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError) as exc:
        raise InferenceAccountingError("general_task_group_evidence_invalid") from exc


async def validate_group_owner(db, group):
    from src.work_board.general_task_proposal import group_identity
    from src.work_board.contracts import WorkBoardOwner
    from src.workflows.job_runtime import _assert_canonical_goal_fence
    from src.db.models import OperatorSession
    root = await db.get(OperatorSession, group.owner_session_id)
    now = datetime.now(timezone.utc)
    if (root is None or root.revoked_at is not None
        or root.principal_id != group.owner_principal_id
        or _utc(root.idle_expires_at) <= now or _utc(root.absolute_expires_at) <= now
        or group.original_deadline_at <= now or group.original_deadline_at > _utc(root.absolute_expires_at)
        or root.replaced_by_id is not None or root.is_bearer_tombstone
        or group.group_id != group_identity(WorkBoardOwner(principal_id=group.owner_principal_id,
            session_id=group.owner_session_id), group.goal_id, group.goal_revision, group.creation_request_key)):
        raise InferenceAccountingError("general_task_group_authority_invalid")
    await _assert_canonical_goal_fence(db, goal_id=group.goal_id, goal_revision=group.goal_revision,
        owner_kind="user", owner_principal_id=group.owner_principal_id, session_id=group.owner_session_id)


async def validate_group(db, run, group):
    await validate_group_owner(db, group)
    if (run.owner_kind != "user" or run.owner_principal_id != group.owner_principal_id
        or run.session_id != group.owner_session_id or run.operator_session_id != group.owner_session_id
        or run.goal_id != group.goal_id or run.goal_revision != group.goal_revision
        or _utc(run.deadline_at) > group.original_deadline_at):
        raise InferenceAccountingError("general_task_group_authority_invalid")


def reservation_liability(row):
    """Whole original liability, independent of accounting-period rollover."""
    if type(row.bound_microusd) is not int or row.bound_microusd <= 0:
        raise InferenceAccountingError("general_task_group_unknown")
    if row.state in {"reserved", "contact_started", "unknown"}:
        return row.bound_microusd
    if row.state == "settled":
        actual = row.actual_cost_microusd
        if type(actual) is not int or actual < 0:
            raise InferenceAccountingError("general_task_group_unknown")
        if actual > row.bound_microusd:
            raise InferenceAccountingError("general_task_group_cost_limit")
        return actual
    if row.state == "released" and row.contact_started_at is None:
        return 0
    raise InferenceAccountingError("general_task_group_unknown")


async def reserve_entry(db, run, rows, binding, *, operation_id, bound, runtime_path,
                        payload_digest=None, deadline_at=None):
    from src.work_board.general_task import digest
    if not isinstance(binding, dict) or not isinstance(binding.get("group"), TaskProposalGroupV1):
        raise InferenceAccountingError("general_task_group_binding_invalid")
    group = binding["group"]
    await validate_group(db, run, group)
    members = []
    for row in rows:
        entry = entry_for(row)
        if entry and entry.get("group", {}).get("group_id") == group.group_id:
            if entry.get("group") != group.model_dump(mode="json") or entry.get("group_digest") != digest(group.model_dump(mode="json")):
                raise InferenceAccountingError("general_task_group_conflict")
            members.append(row)
    if len(members) >= group.max_inference_calls:
        raise InferenceAccountingError("general_task_group_call_limit")
    if any(row.state == "unknown" for row in members):
        raise InferenceAccountingError("general_task_group_unknown")
    if sum(reservation_liability(row) for row in members) + bound > group.max_cost_microusd:
        raise InferenceAccountingError("general_task_group_cost_limit")
    role = binding.get("role")
    repository = None
    expected_route = "strategist_agent" if role == "repository_iteration" else "general_task_planner"
    if runtime_path != expected_route or role not in {"initial_proposal", "continuation", "repository_iteration"}:
        raise InferenceAccountingError("general_task_group_runtime_invalid")
    initial = next((row for row in members if entry_for(row).get("role") == "initial_proposal"), None)
    if role == "initial_proposal":
        if members or binding.get("task_id") is not None or binding.get("task_attempt_id") is not None or binding.get("plan_revision") != 0 or binding.get("selected_grant_digest") is not None:
            raise InferenceAccountingError("general_task_group_initial_conflict")
    elif role == "continuation":
        if not binding.get("task_id") or not binding.get("task_attempt_id") or not binding.get("selected_grant_digest"):
            raise InferenceAccountingError("general_task_group_provenance_missing")
        await validate_continuation(db, group, binding, initial)
    else:
        repository = await validate_repository_iteration(db, run, rows, binding,
            operation_id=operation_id, payload_digest=payload_digest, bound=bound,
            deadline_at=deadline_at, already_reserved=False)
        binding = {**binding, "task_id": repository["parent_task_id"],
            "task_attempt_id": repository["parent_attempt_id"], "plan_revision": 0,
            "selected_grant_digest": None, "parent_owner": None, "parent_fence": None}
    return {"kind": KIND, "group": group.model_dump(mode="json"),
        "group_digest": digest(group.model_dump(mode="json")), "role": role,
        "call_ordinal": len(members) + 1, "original_operation_id": operation_id,
        "original_job_id": run.run_identity,
        "initial_proposal_operation_id": operation_id if role == "initial_proposal" else initial.operation_id if initial else None,
        "task_id": binding.get("task_id"), "task_attempt_id": binding.get("task_attempt_id"),
        "plan_revision": binding.get("plan_revision"), "selected_grant_digest": binding.get("selected_grant_digest"),
        "parent_owner": binding.get("parent_owner"), "parent_fence": binding.get("parent_fence"),
        **({"repository_binding": repository} if repository is not None else {})}


async def validate_repository_iteration(db, run, rows, binding, *, operation_id,
                                       payload_digest, bound, deadline_at, already_reserved):
    from src.workflows.repo_repair_source import (assert_repository_iteration_witness,
        validate_repository_iteration_accounting)
    if not isinstance(binding, dict) or binding.get("role") != "repository_iteration":
        raise InferenceAccountingError("repository_iteration_witness_invalid")
    witness = binding.get("repository_witness")
    assert_repository_iteration_witness(witness)
    if witness.group != binding.get("group"):
        raise InferenceAccountingError("repository_iteration_witness_invalid")
    projection = await validate_repository_iteration_accounting(db, witness, run,
        rows=rows, operation_id=operation_id, payload_digest=payload_digest,
        bound_microusd=bound, deadline_at=deadline_at, already_reserved=already_reserved)
    evidence = RepositoryIterationEvidenceV1.model_validate(projection)
    if (evidence.group != witness.group or evidence.operation_id != operation_id
        or evidence.repository_job_id != run.run_identity or deadline_at is None
        or _utc(deadline_at) > evidence.original_deadline_at):
        raise InferenceAccountingError("repository_iteration_witness_invalid")
    return evidence.model_dump(mode="json")


async def validate_recovered_entry(db, run, row, rows, binding, *, runtime_path):
    """Keep an existing reserved operation inside its original live owner role."""
    entry = entry_for(row)
    if (entry is None or not isinstance(binding, dict) or binding.get("role") != entry["role"]
        or not isinstance(binding.get("group"), TaskProposalGroupV1)
        or binding["group"].model_dump(mode="json") != entry["group"]):
        raise InferenceAccountingError("general_task_group_binding_invalid")
    expected_route = "strategist_agent" if entry["role"] == "repository_iteration" else "general_task_planner"
    if runtime_path != expected_route:
        raise InferenceAccountingError("general_task_group_runtime_invalid")
    if entry["role"] == "continuation":
        for key in ("task_id", "task_attempt_id", "plan_revision", "selected_grant_digest", "parent_owner", "parent_fence"):
            if binding.get(key) != entry[key]:
                raise InferenceAccountingError("general_task_group_binding_invalid")
    await validate_contact(db, run, row, rows, binding=binding)


async def validate_continuation(db, group, binding, initial):
    from src.work_board.general_task import digest
    from sqlalchemy import select
    from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkBoardStatus, WorkflowRunState
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest, selected_grant_digest
    from src.workflows.general_task_guard import read_manifest
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding["task_id"]))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding["task_attempt_id"]))
    parent = (await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == attempt.workflow_run_id))) if attempt else None
    now = datetime.now(timezone.utc)
    if (task is None or attempt is None or parent is None or task.status != WorkBoardStatus.running
        or parent.status != "running" or parent.lease_owner != binding.get("parent_owner")
        or type(binding.get("parent_fence")) is not int or parent.fencing_token != binding["parent_fence"]
        or parent.lease_expires_at is None or _utc(parent.lease_expires_at) <= now):
        raise InferenceAccountingError("general_task_continuation_not_bound")
    manifest = read_manifest(parent)
    if (manifest is None or manifest.phase not in {"assembly", "native_ready"}
        or manifest.plan_revision != binding["plan_revision"]
        or manifest.task_revision != task.task_revision or manifest.board_fence != attempt.fencing_token
        or manifest.job_fence != parent.fencing_token or manifest.group_id != group.group_id
        or manifest.group_digest != digest(group.model_dump(mode="json"))):
        raise InferenceAccountingError("general_task_continuation_not_bound")
    envelope = await verify_general_task_manifest(db, parent, task, attempt, manifest)
    if selected_grant_digest(envelope) != binding["selected_grant_digest"] or envelope.proposal_group != group:
        raise InferenceAccountingError("general_task_group_provenance_missing")
    if envelope.proposal_provenance is None:
        if initial is not None:
            raise InferenceAccountingError("general_task_group_provenance_missing")
    else:
        from src.work_board.general_task_proposal import proposal_provenance
        if initial is None or proposal_provenance(initial.model_dump(mode="json"), group) != envelope.proposal_provenance:
            raise InferenceAccountingError("general_task_group_provenance_missing")


async def validate_contact(db, run, row, rows, *, binding=None):
    """Recheck the original group and current task phase before contact."""
    from src.work_board.general_task import digest
    entry = entry_for(row)
    group = TaskProposalGroupV1.model_validate(entry["group"])
    await validate_group(db, run, group)
    members = [member for member in rows if entry_for(member)
        and entry_for(member).get("group", {}).get("group_id") == group.group_id]
    if (len(members) > group.max_inference_calls
        or any(entry_for(member).get("group") != group.model_dump(mode="json")
            or entry_for(member).get("group_digest") != digest(group.model_dump(mode="json"))
            for member in members)):
        raise InferenceAccountingError("general_task_group_conflict")
    if any(member.state == "unknown" for member in members):
        raise InferenceAccountingError("general_task_group_unknown")
    liability = sum(reservation_liability(member) for member in members)
    if liability > group.max_cost_microusd:
        raise InferenceAccountingError("general_task_group_cost_limit")
    if entry.get("role") == "repository_iteration":
        if row.runtime_path != "strategist_agent":
            raise InferenceAccountingError("general_task_group_runtime_invalid")
        repository = await validate_repository_iteration(db, run, rows, binding,
            operation_id=row.operation_id, payload_digest=row.payload_digest,
            bound=row.bound_microusd, deadline_at=_utc(row.deadline_at), already_reserved=True)
        if repository != entry["repository_binding"]:
            raise InferenceAccountingError("repository_iteration_witness_invalid")
    elif entry.get("role") == "continuation":
        keys = {"task_id", "task_attempt_id", "plan_revision", "selected_grant_digest", "parent_owner", "parent_fence"}
        if not keys.issubset(entry):
            raise InferenceAccountingError("general_task_continuation_not_bound")
        binding = {key: entry[key] for key in keys}
        initial = next((member for member in members if entry_for(member).get("role") == "initial_proposal"), None)
        await validate_continuation(db, group, binding, initial)
    elif entry.get("role") != "initial_proposal":
        raise InferenceAccountingError("general_task_group_runtime_invalid")
