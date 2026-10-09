"""Closed GeneralTask group checks inside the existing reservation writer."""
from datetime import datetime, timezone
from typing import Literal
from pydantic import Field, ValidationError, model_validator, field_validator

from src.work_board.contracts import (TaskProposalGroupV1, ClosedTaskModel,
    TaskDigest, TaskIdentity, NativeInvocationIdentity, GeneralTaskNativeChildBindingV1)
from src.workflows.inference_accounting import InferenceAccountingError, _utc

KIND = "general_task_group_reservation.v1"


class CommunicationPreparationEvidenceV1(ClosedTaskModel):
    native: GeneralTaskNativeChildBindingV1
    child_owner: NativeInvocationIdentity
    child_fence: int = Field(ge=1)
    group: TaskProposalGroupV1
    ordinal: int = Field(ge=0, le=9)
    capability_id: Literal["work.mail-reply-draft.v1", "calendar.meeting-prep.v1"]
    source_choice_digest: TaskDigest
    source_task_id: TaskIdentity
    source_attempt_id: TaskIdentity
    input_artifact_id: TaskIdentity
    input_artifact_digest: TaskDigest
    source_job_id: NativeInvocationIdentity
    source_deadline_at: datetime
    budget_microusd: int = Field(ge=1)
    policy_digest: TaskDigest

    _utc_timestamp = field_validator("source_deadline_at", mode="before")(TaskProposalGroupV1.utc_timestamp.__func__)


class GeneralTaskGroupReservationEvidenceV1(ClosedTaskModel):
    kind: Literal["general_task_group_reservation.v1"] = KIND
    group: TaskProposalGroupV1
    group_digest: TaskDigest
    role: Literal["initial_proposal", "continuation", "specialist", "communication_preparation"]
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
    delegation_invocation_id: NativeInvocationIdentity | None = None
    delegation_request_digest: TaskDigest | None = None
    preparation: CommunicationPreparationEvidenceV1 | None = None

    @model_validator(mode="after")
    def exact_role(self):
        from src.work_board.general_task import digest
        if self.group_digest != digest(self.group.model_dump(mode="json")):
            raise ValueError("original proposal group digest changed")
        if self.role != "communication_preparation" and self.preparation is not None:
            raise ValueError("source preparation evidence requires its exact role")
        if self.role == "communication_preparation":
            prepared = self.preparation
            if (prepared is None or prepared.group != self.group
                or prepared.native.task_id != self.task_id or prepared.native.attempt_id != self.task_attempt_id
                or prepared.native.plan_revision != self.plan_revision
                or prepared.native.selected_grant_digest != self.selected_grant_digest
                or prepared.child_owner != self.parent_owner or prepared.child_fence != self.parent_fence
                or prepared.source_job_id != self.original_job_id
                or prepared.source_deadline_at > self.group.original_deadline_at):
                raise ValueError("source preparation requires exact original producer evidence")
        elif self.role == "initial_proposal":
            if (self.call_ordinal != 1 or self.initial_proposal_operation_id != self.original_operation_id
                or self.task_id is not None or self.task_attempt_id is not None or self.plan_revision != 0
                or self.selected_grant_digest is not None or self.parent_owner is not None or self.parent_fence is not None):
                raise ValueError("initial proposal cannot carry continuation authority")
        elif (not self.task_id or not self.task_attempt_id or self.plan_revision < 1
            or self.selected_grant_digest is None or not self.parent_owner or self.parent_fence is None):
            raise ValueError("continuation requires its exact captured native evidence")
        if self.role == "specialist":
            if not self.delegation_invocation_id or self.delegation_request_digest is None:
                raise ValueError("specialist requires its original delegation proof")
        elif self.delegation_invocation_id is not None or self.delegation_request_digest is not None:
            raise ValueError("delegation proof cannot authorize another planning role")
        return self


def entry_for(row):
    try:
        from src.workflows.inference_group_lookup import strict_evidence_entries
        entries = strict_evidence_entries(row.evidence_json)
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
        # V1 specialist stop journals hash the original typed defaults. New
        # role fields must not change those retained financial memberships.
        # Explicit fields (including null) keep their original presence.
        added = (("delegation_invocation_id", "delegation_request_digest")
            if entry.role == "communication_preparation" else ("preparation",))
        return entry.model_dump(mode="json", exclude={key for key in added if key not in found[0]})
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


def _validate_specialist_limits(members, binding, limits, *, new_bound=None):
    """Narrow request allowance inside, never instead of, the original group."""
    own = [row for row in members if entry_for(row).get("role") == "specialist"
        and entry_for(row).get("delegation_invocation_id") == binding["delegation_invocation_id"]
        and entry_for(row).get("delegation_request_digest") == binding["delegation_request_digest"]]
    if len(own) + (new_bound is not None) > limits.max_inference_calls:
        raise InferenceAccountingError("general_task_specialist_call_limit")
    liability = new_bound or 0
    for row in own:
        if row.state in {"reserved", "contact_started"}:
            liability += row.bound_microusd
        elif row.state == "settled" and row.actual_cost_microusd is not None:
            liability += row.actual_cost_microusd
        elif row.state != "released":
            raise InferenceAccountingError("general_task_group_unknown")
    if liability > limits.max_cost_microusd:
        raise InferenceAccountingError("general_task_specialist_cost_limit")


async def reserve_entry(db, run, rows, binding, *, operation_id, bound, runtime_path, deadline_at=None, policy_digest=None):
    from src.work_board.general_task import digest
    if not isinstance(binding, dict) or not isinstance(binding.get("group"), TaskProposalGroupV1):
        raise InferenceAccountingError("general_task_group_binding_invalid")
    group = binding["group"]
    if binding.get("role") != "communication_preparation" and binding.get("preparation_binding") is not None:
        raise InferenceAccountingError("general_task_group_binding_invalid")
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
    def liability(row):
        if row.state in {"reserved", "contact_started", "unknown"}:
            return row.bound_microusd
        if row.state == "settled":
            if row.actual_cost_microusd is None:
                raise InferenceAccountingError("general_task_group_unknown")
            return row.actual_cost_microusd
        return 0
    if sum(liability(row) for row in members) + bound > group.max_cost_microusd:
        raise InferenceAccountingError("general_task_group_cost_limit")
    role = binding.get("role")
    preparation = None
    expected_route = "strategist_agent" if role == "communication_preparation" else "general_task_planner"
    if runtime_path != expected_route or role not in {"initial_proposal", "continuation", "specialist", "communication_preparation"}:
        raise InferenceAccountingError("general_task_group_runtime_invalid")
    initial = next((row for row in members if entry_for(row).get("role") == "initial_proposal"), None)
    if role == "initial_proposal":
        if members or binding.get("task_id") is not None or binding.get("task_attempt_id") is not None or binding.get("plan_revision") != 0 or binding.get("selected_grant_digest") is not None:
            raise InferenceAccountingError("general_task_group_initial_conflict")
    elif role in {"continuation", "specialist"}:
        if not binding.get("task_id") or not binding.get("task_attempt_id") or not binding.get("selected_grant_digest"):
            raise InferenceAccountingError("general_task_group_provenance_missing")
        if role == "specialist":
            from src.workflows.specialist_delegation import validate_specialist_accounting
            context = await validate_specialist_accounting(db, group, binding, initial)
            _validate_specialist_limits(members, binding, context.request.limits, new_bound=bound)
        else:
            await validate_continuation(db, group, binding, initial)
    else:
        preparation = await validate_preparation(db, run, group, binding, bound=bound)
        if policy_digest != binding["preparation_binding"].policy_digest:
            raise InferenceAccountingError("communication_preparation_policy_changed")
        if deadline_at is not None and _utc(deadline_at) > binding["preparation_binding"].source_deadline_at:
            raise InferenceAccountingError("general_task_group_authority_invalid")
        if any(entry_for(member).get("role") == "communication_preparation"
            and (entry_for(member)["preparation"]["source_job_id"] == preparation["source_job_id"]
                or entry_for(member)["preparation"]["ordinal"] == preparation["ordinal"])
            for member in members):
            raise InferenceAccountingError("communication_preparation_already_reserved")
        native = binding["preparation_binding"].native
        binding = {**binding, "task_id": native.task_id, "task_attempt_id": native.attempt_id,
            "plan_revision": native.plan_revision, "selected_grant_digest": native.selected_grant_digest,
            "parent_owner": binding["preparation_binding"].child_owner,
            "parent_fence": binding["preparation_binding"].child_fence}
    return {"kind": KIND, "group": group.model_dump(mode="json"),
        "group_digest": digest(group.model_dump(mode="json")), "role": role,
        "call_ordinal": len(members) + 1, "original_operation_id": operation_id,
        "original_job_id": run.run_identity,
        "initial_proposal_operation_id": operation_id if role == "initial_proposal" else initial.operation_id if initial else None,
        "task_id": binding.get("task_id"), "task_attempt_id": binding.get("task_attempt_id"),
        "plan_revision": binding.get("plan_revision"), "selected_grant_digest": binding.get("selected_grant_digest"),
        "parent_owner": binding.get("parent_owner"), "parent_fence": binding.get("parent_fence"),
        **({"delegation_invocation_id": binding.get("delegation_invocation_id"),
            "delegation_request_digest": binding.get("delegation_request_digest")} if role == "specialist" else {}),
        **({"preparation": preparation} if preparation is not None else {})}


async def validate_preparation(db, run, group, binding, *, bound):
    from src.work_board.communication_contracts import CommunicationPreparationBinding
    from src.work_board.communication_preparation import verify_preparation_binding, binding_payload
    if (not isinstance(binding, dict) or binding.get("role") != "communication_preparation"
        or type(binding.get("preparation_binding")) is not CommunicationPreparationBinding):
        raise InferenceAccountingError("general_task_group_binding_invalid")
    prepared = await verify_preparation_binding(db, binding["preparation_binding"], source_run=run)
    if (prepared.group != group or binding.get("group") != group
        or bound > prepared.budget_microusd or prepared.source_deadline_at <= datetime.now(timezone.utc)
        or _utc(run.deadline_at) > group.original_deadline_at):
        raise InferenceAccountingError("general_task_group_authority_invalid")
    return CommunicationPreparationEvidenceV1.model_validate(binding_payload(prepared)).model_dump(mode="json")


async def validate_recovered_entry(db, run, row, rows, binding, *, runtime_path):
    """Resume the original reserved row only under its still-current role."""
    entry = entry_for(row)
    if (entry is None or not isinstance(binding, dict) or binding.get("role") != entry["role"]
        or not isinstance(binding.get("group"), TaskProposalGroupV1)
        or binding["group"].model_dump(mode="json") != entry["group"]):
        raise InferenceAccountingError("general_task_group_binding_invalid")
    expected_route = "strategist_agent" if entry["role"] == "communication_preparation" else "general_task_planner"
    if runtime_path != expected_route:
        raise InferenceAccountingError("general_task_group_runtime_invalid")
    if entry["role"] in {"continuation", "specialist"}:
        keys = ("task_id", "task_attempt_id", "plan_revision", "selected_grant_digest", "parent_owner", "parent_fence")
        if entry["role"] == "specialist":
            keys += ("delegation_invocation_id", "delegation_request_digest")
        for key in keys:
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
        or parent.status != "running" or parent.parent_job_id is not None
        or parent.lease_owner != binding.get("parent_owner")
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
    liability = 0
    for member in members:
        if member.state == "unknown":
            raise InferenceAccountingError("general_task_group_unknown")
        if member.state in {"reserved", "contact_started"}:
            liability += member.bound_microusd
        elif member.state == "settled" and member.actual_cost_microusd is not None:
            liability += member.actual_cost_microusd
        elif member.state != "released":
            raise InferenceAccountingError("general_task_group_unknown")
    if liability > group.max_cost_microusd:
        raise InferenceAccountingError("general_task_group_cost_limit")
    if entry.get("role") == "communication_preparation":
        if row.runtime_path != "strategist_agent":
            raise InferenceAccountingError("general_task_group_runtime_invalid")
        preparation = await validate_preparation(db, run, group, binding, bound=row.bound_microusd)
        if preparation != entry["preparation"] or _utc(row.deadline_at) > binding["preparation_binding"].source_deadline_at:
            raise InferenceAccountingError("general_task_group_binding_invalid")
    elif entry.get("role") in {"continuation", "specialist"}:
        keys = {"task_id", "task_attempt_id", "plan_revision", "selected_grant_digest", "parent_owner", "parent_fence"}
        if not keys.issubset(entry):
            raise InferenceAccountingError("general_task_continuation_not_bound")
        binding = {key: entry[key] for key in keys}
        initial = next((member for member in members if entry_for(member).get("role") == "initial_proposal"), None)
        if entry["role"] == "specialist":
            from src.workflows.specialist_delegation import validate_specialist_accounting
            binding.update({key: entry[key] for key in ("delegation_invocation_id", "delegation_request_digest")})
            binding["role"] = "specialist"
            context = await validate_specialist_accounting(db, group, binding, initial)
            _validate_specialist_limits(members, binding, context.request.limits)
        else:
            await validate_continuation(db, group, binding, initial)
    elif entry.get("role") != "initial_proposal":
        raise InferenceAccountingError("general_task_group_runtime_invalid")
