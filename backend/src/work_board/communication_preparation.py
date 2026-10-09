"""Fixed source-specific composition on current C1/native owners.

All runtime access is explicit. Importing this module starts no service.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import json
from sqlalchemy import select

from src.work_board.contracts import GeneralTaskEnvelope, WorkBoardOwner
from src.work_board.communication_contracts import CommunicationPreparationBinding, TOOL_ID, METADATA_MAX_BYTES
from src.work_board.general_task import canonical, digest
from src.work_board.repository import BoardError

_PREPARATION_SEAL = object()
_ADMISSION_SEAL = object()
_PUBLICATION_SEAL = object()
_CURRENT_PREPARATION = ContextVar("communication_original_preparation", default=None)
_PHYSICAL_SEAL = object()


async def _row(db, model, field, identity):
    return await db.scalar(select(model).where(getattr(model, field) == identity)
        .execution_options(populate_existing=True))


@dataclass(frozen=True)
class _NativePhysicalWitness:
    native: object
    envelope: GeneralTaskEnvelope
    manifest: object
    input_metadata_digest: str
    parent_authority_json: str
    parent_checkpoint_json: str
    child_lease_owner: str
    step_receipt: object
    _seal: object


async def stage_native_witness(db, principal, child_id, fence):
    """Actual physical proof before any publication/accounting SQL writer."""
    from src.db.models import WorkflowRunState, WorkBoardInputArtifact
    from src.workflows.general_task_guard import read_manifest, _step_receipt
    from src.work_board.input_artifacts import _metadata_digest
    child, native, envelope = await invocation(db, principal, child_id, fence)
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == native.parent_job_id))
    manifest = read_manifest(parent)
    artifact = await _row(db, WorkBoardInputArtifact, 'artifact_id', manifest.original_envelope_artifact_id)
    witness = _NativePhysicalWitness(native, envelope, manifest, _metadata_digest(artifact),
        parent.declared_authority_json, parent.checkpoint_receipts_json, child.lease_owner,
        _step_receipt(manifest, native.step_id), _PHYSICAL_SEAL)
    return child, native, envelope, witness


async def _verify_native_sql(db, witness, fence):
    """Only canonical rows; the immutable bytes were physically staged."""
    from src.db.models import WorkflowRunState, WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact, WorkBoardStatus
    from src.workflows.general_task_guard import child_binding, read_manifest, _require_callback_reservation, assert_original_parent_authority
    from src.work_board.input_artifacts import _metadata_digest
    from src.api.mail import _assert_live_session
    from src.workflows.job_runtime import _assert_canonical_goal_fence
    if type(witness) is not _NativePhysicalWitness or witness._seal is not _PHYSICAL_SEAL:
        raise BoardError("communication_original_physical_proof_required", "Original staged native evidence required", status_code=409)
    native, manifest = witness.native, witness.manifest
    child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == native.invocation_id)
        .execution_options(populate_existing=True))
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == native.parent_job_id)
        .execution_options(populate_existing=True))
    task = await _row(db, WorkBoardTask, 'task_id', native.task_id)
    attempt = await _row(db, WorkBoardAttempt, 'attempt_id', native.attempt_id)
    artifact = await _row(db, WorkBoardInputArtifact, 'artifact_id', manifest.original_envelope_artifact_id)
    now = datetime.now(timezone.utc)
    receipt = witness.step_receipt
    if (child is None or parent is None or task is None or attempt is None or artifact is None
        or child_binding(child) != native or read_manifest(parent) != manifest
        or manifest.phase != "native_wait" or native.invocation_id not in manifest.admitted_invocation_ids
        or manifest.phase_revision != native.phase_revision or manifest.phase_digest != native.phase_digest
        or child.status != "running" or child.fencing_token != fence
        or fence <= 0 or child.attempt_count != 1 or child.lease_owner != witness.child_lease_owner
        or not child.lease_owner or child.parent_job_id != parent.run_identity
        or child.root_run_identity != parent.root_run_identity
        or child.owner_principal_id != native.owner_principal_id
        or child.session_id != native.original_root_id or child.operator_session_id != native.original_root_id
        or child.lease_expires_at is None or utc(child.lease_expires_at) <= now
        or child.deadline_at is None or utc(child.deadline_at) <= now
        or parent.status != "paused" or parent.failure_reason != "general_task_native_wait"
        or parent.lease_owner is not None or parent.lease_expires_at is not None
        or parent.deadline_at is None or utc(parent.deadline_at) <= now
        or utc(parent.deadline_at) != native.native_deadline_at
        or parent.fencing_token != manifest.job_fence or parent.input_digest != manifest.original_input_digest
        or parent.job_kind != "agent.task.v1" or parent.capability_version != "1"
        or parent.branch_depth != 0 or parent.parent_job_id is not None or parent.owner_kind != "user"
        or parent.owner_principal_id != native.owner_principal_id
        or parent.session_id != native.original_root_id or parent.operator_session_id != native.original_root_id
        or parent.authority_digest != native.parent_authority_digest
        or parent.declared_authority_json != witness.parent_authority_json
        or parent.checkpoint_receipts_json != witness.parent_checkpoint_json
        or parent.goal_id != task.goal_id or parent.goal_revision != task.goal_revision
        or task.owner_principal_id != native.owner_principal_id or task.owner_session_id != native.original_root_id
        or task.status != WorkBoardStatus.blocked or task.block_reason != "general_task_native_wait"
        or task.capability_id != "agent.task.v1"
        or task.task_revision != manifest.task_revision or attempt.fencing_token != manifest.board_fence
        or attempt.task_id != task.task_id or attempt.workflow_run_id != parent.run_identity
        or attempt.ended_at is not None or attempt.cancel_requested_at is not None
        or attempt.lease_owner is not None or attempt.lease_expires_at is not None
        or receipt.child_attempt_count != child.attempt_count or receipt.child_fence != fence
        or receipt.child_job_id != child.run_identity or receipt.invocation_id != native.invocation_id
        or receipt.input_digest != native.input_digest or receipt.descriptor_digest != native.descriptor_digest
        or receipt.phase_digest != native.phase_digest or receipt.approval_binding_digest is not None
        or receipt.status != "running" or receipt.contact_state not in {"not_contacted", "contact_started"}
        or task.input_artifact_id != artifact.artifact_id or task.typed_input_digest != artifact.payload_sha256
        or artifact.payload_sha256 != manifest.original_envelope_digest or artifact.bound_task_id != task.task_id
        or artifact.state not in {"bound", "consumed"} or artifact.metadata_digest != _metadata_digest(artifact)
        or _metadata_digest(artifact) != witness.input_metadata_digest or utc(artifact.expires_at) <= now):
        raise BoardError("communication_original_native_changed", "Original native wait, input and fences required", status_code=409)
    assert_original_parent_authority(parent)
    _require_callback_reservation(parent, native, fence)
    owner = WorkBoardOwner(principal_id=native.owner_principal_id, session_id=native.original_root_id)
    await _assert_live_session(db, owner)
    await _assert_canonical_goal_fence(db, goal_id=task.goal_id, goal_revision=task.goal_revision,
        owner_kind="user", owner_principal_id=owner.principal_id, session_id=owner.session_id)
    return child, native, witness.envelope


@contextmanager
def preparation_scope(binding):
    if type(binding) is not CommunicationPreparationBinding or binding._seal is not _PREPARATION_SEAL:
        raise PermissionError("original source producer required")
    token = _CURRENT_PREPARATION.set(binding)
    try:
        yield
    finally:
        _CURRENT_PREPARATION.reset(token)


async def assert_preparation_run_current(db, run):
    authority = json.loads(run.declared_authority_json or "{}")
    payload = authority.get("communication_preparation")
    if payload is None:
        return
    binding = _CURRENT_PREPARATION.get()
    if type(binding) is not CommunicationPreparationBinding or binding_authority(binding) != payload:
        raise BoardError("communication_original_producer_unavailable", "Inspect the original source producer; do not replay", status_code=409)
    await verify_preparation_binding(db, binding, source_run=run)


@dataclass(frozen=True)
class _SourcePublication:
    principal: object
    native: object
    fence: int
    ordinal: int
    capability_id: str
    choice_digest: str
    artifact_id: str
    artifact_digest: str
    _seal: object
    physical: _NativePhysicalWitness


async def verify_source_publication(db, witness, task=None):
    if type(witness) is not _SourcePublication or witness._seal is not _PUBLICATION_SEAL:
        raise BoardError("communication_source_publication_required", "Original source publication witness required", status_code=409)
    _child, native, envelope = await _verify_native_sql(db, witness.physical, witness.fence)
    if native != witness.native:
        raise BoardError("communication_source_publication_changed", "Original native selection required", status_code=409)
    selection = envelope.task_input.communication_selection
    choices = [("work.mail-reply-draft.v1", value) for value in selection.reply_inputs]
    choices += [("calendar.meeting-prep.v1", value) for value in selection.meeting_inputs]
    if not 0 <= witness.ordinal < len(choices):
        raise BoardError("communication_source_ordinal_changed", "Original source ordinal required", status_code=409)
    capability, inputs = choices[witness.ordinal]
    if capability != witness.capability_id or digest(inputs) != witness.choice_digest:
        raise BoardError("communication_source_choice_changed", "Original source choice required", status_code=409)
    owner = WorkBoardOwner(principal_id=native.owner_principal_id, session_id=native.original_root_id)
    await assert_source_current(db, owner, capability, inputs)
    if task is not None and (task.capability_id != capability or task.input_artifact_id != witness.artifact_id
        or task.typed_input_digest != witness.artifact_digest or task.owner_principal_id != owner.principal_id
        or task.owner_session_id != owner.session_id or task.goal_id != envelope.task_input.goal_ref
        or task.goal_revision != envelope.proposal_group.goal_revision
        or task.idempotency_scope != "communication-source"
        or task.idempotency_key != "communication:" + digest([native.invocation_id, native.creation_digest, witness.ordinal])[:64]):
        raise BoardError("communication_source_task_changed", "Exact internally prepared source task required", status_code=409)
    return owner, envelope, inputs


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def check_envelope(envelope):
    selection = envelope.task_input.communication_selection
    if selection is None:
        return
    limits = envelope.task_input.limits
    contract = descriptor()
    count = len(selection.reply_inputs) + len(selection.meeting_inputs)
    if (envelope.task_input.document_source is not None or envelope.task_input.evidence_refs
        or envelope.task_input.inference_egress_acknowledged
        or limits.max_steps != 1 or limits.max_outstanding_children != 1
        or limits.max_inference_calls != count or (count and limits.max_cost_microusd <= 0)
        or limits.max_inference_calls > 10 or envelope.plan is None or len(envelope.plan.steps) != 1
        or envelope.descriptors != [contract]
        or envelope.task_input.requested_output != contract.output_schema):
        raise BoardError("communication_fixed_plan_required", "Use the fixed private communications task", status_code=422)
    step = envelope.plan.steps[0]
    if (step.step_id != "prepare" or step.tool_id != TOOL_ID or step.depends_on
        or step.input != {"selection_digest": digest(selection.model_dump(mode="json"))}
        or step.output_contract != contract.output_schema):
        raise BoardError("communication_selection_changed", "The original selection digest is required", status_code=422)
    for value in [*selection.reply_inputs, *selection.meeting_inputs, *selection.reschedule_inputs]:
        revision = value.get("expected_goal_revision", value.get("goal_revision"))
        if value["goal_id"] != envelope.task_input.goal_ref or revision is None:
            raise BoardError("communication_goal_changed", "All sources must use the original task Goal", status_code=422)


def descriptor():
    from src.work_board.contracts import ToolDescriptor
    from src.tools.policy import get_task_policy_snapshot
    hash_schema = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    output = {"type": "object", "properties": {
        "private_plan_ref": {"type": "string", "minLength": 1, "maxLength": 256},
        "private_plan_digest": hash_schema,
        "producer_job_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "source_readbacks": {"type": "array", "maxItems": 10, "items": {
            "type": "object", "properties": {"job_id": {"type": "string", "maxLength": 256},
                "readback_id": {"type": "string", "maxLength": 256}, "artifact_digest": hash_schema},
            "required": ["job_id", "readback_id", "artifact_digest"], "additionalProperties": False}},
        "no_learning": {"const": True}},
        "required": ["private_plan_ref", "private_plan_digest", "producer_job_id", "source_readbacks", "no_learning"],
        "additionalProperties": False}
    return ToolDescriptor(tool_id=TOOL_ID, version="1",
        input_schema={"type": "object", "properties": {"selection_digest": hash_schema},
            "required": ["selection_digest"], "additionalProperties": False},
        output_schema=output, effects=["owner_private_read", "local_compute"],
        permissions=["capability_execute"], deadline=900, verifier="communication_private_readback.v1",
        policy_digest=digest({"version": 1, "metadata_bytes": METADATA_MAX_BYTES,
            "original_source_owners": ["work.mail-reply-draft.v1", "calendar.meeting-prep.v1"],
            "policy": get_task_policy_snapshot()}))


async def propose(db, owner, service, request):
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, TaskLimits, PlanSpec, PlanStep
    descriptors, tool_digest = service.snapshot()
    contract = next((value for value in descriptors if value.tool_id == TOOL_ID), None)
    if contract is None:
        raise BoardError("communication_preparation_unavailable", "Restore the current communications adapter", status_code=503)
    count = len(request.selection.reply_inputs) + len(request.selection.meeting_inputs)
    return await service.create(db, owner, GeneralTaskCreate(goal_revision=request.goal_revision,
        idempotency_key=request.idempotency_key, expected_plan_revision=1, accept=True,
        input=GeneralTaskInput(goal_ref=request.goal_id, intent="Prepare private communications",
            communication_selection=request.selection, requested_output=contract.output_schema,
            tool_set_digest=tool_digest, limits=TaskLimits(max_steps=1, max_outstanding_children=1,
                max_inference_calls=count, max_cost_microusd=request.max_cost_microusd,
                wall_seconds=request.wall_seconds)),
        plan=PlanSpec(revision=1, steps=[PlanStep(step_id="prepare", tool_id=TOOL_ID,
            input={"selection_digest": digest(request.selection.model_dump(mode="json"))},
            output_contract=contract.output_schema)])))


async def invocation(db, principal, job_id, fencing_token):
    from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkflowRunState
    from src.workflows.general_task_guard import child_binding, assert_general_task_child_current, read_manifest
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest, read_current_native_tool_input
    child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id)
        .execution_options(populate_existing=True))
    if (child is None or not principal or not principal.authenticated or principal.revoked
        or principal.job_id != job_id or child.fencing_token != fencing_token):
        raise BoardError("communication_original_child_required", "The original active communications child is required", status_code=409)
    await assert_general_task_child_current(db, child)
    binding = child_binding(child)
    if (principal.principal_id != binding.owner_principal_id or principal.session_id != binding.original_root_id
        or principal.operator_session_id != binding.original_root_id):
        raise BoardError("communication_owner_changed", "The original task owner is required", status_code=403)
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id)
        .execution_options(populate_existing=True))
    task = await _row(db, WorkBoardTask, 'task_id', binding.task_id)
    attempt = await _row(db, WorkBoardAttempt, 'attempt_id', binding.attempt_id)
    envelope = await verify_general_task_manifest(db, parent, task, attempt, read_manifest(parent))
    check_envelope(envelope)
    private = await read_current_native_tool_input(db, child)
    if (envelope.task_input.communication_selection is None or private.tool_id != TOOL_ID
        or binding.descriptor_digest != digest(descriptor().model_dump(mode="json"))
        or private.inputs != {"selection_digest": digest(envelope.task_input.communication_selection.model_dump(mode="json"))}):
        raise BoardError("communication_selection_changed", "The exact original communications selection is required", status_code=409)
    return child, binding, envelope


async def assert_source_current(db, owner, capability, inputs):
    """No contact/decryption: exact existing source rows under caller snapshot."""
    from src.db.models import (GoogleServiceConnection, MailMessageBinding, MailReadConsent,
        CalendarReadConsent, CalendarEventBinding)
    from src.api.mail import _assert_live_session
    from src.workflows.job_runtime import _assert_canonical_goal_fence
    await _assert_live_session(db, owner)
    goal_revision = inputs.get("expected_goal_revision", inputs.get("goal_revision"))
    await _assert_canonical_goal_fence(db, goal_id=inputs["goal_id"], goal_revision=goal_revision,
        owner_kind="user", owner_principal_id=owner.principal_id, session_id=owner.session_id)
    if capability == "work.mail-reply-draft.v1":
        from src.api.mail import _source_label_scope_digest
        connection = await _row(db, GoogleServiceConnection, 'connection_id', inputs["connection_id"])
        consent = await _row(db, MailReadConsent, 'consent_id', inputs["mail_consent_id"])
        binding = await _row(db, MailMessageBinding, 'message_binding_id', inputs["message_binding_id"])
        if (connection is None or consent is None or binding is None
            or connection.revision != inputs["expected_connection_revision"]
            or consent.connection_id != connection.connection_id or consent.connection_revision != connection.revision
            or consent.source_revision != inputs["expected_source_consent_revision"]
            or consent.model_revision != inputs["expected_model_consent_revision"]
            or binding.message_revision != inputs["expected_message_revision"]
            or binding.connection_id != connection.connection_id
            or binding.connection_revision != connection.revision or binding.status != "present"
            or binding.source_consent_id != consent.consent_id
            or binding.source_consent_revision != consent.source_revision
            or binding.source_label_scope_digest != _source_label_scope_digest(connection, consent)
            or not consent.source_read_allowed):
            raise BoardError("communication_mail_source_changed", "Review the affected Mail source", status_code=409)
    elif capability == "calendar.meeting-prep.v1":
        consent = await _row(db, CalendarReadConsent, 'consent_id', inputs["consent_id"])
        binding = await _row(db, CalendarEventBinding, 'event_binding_id', inputs["event_binding_id"])
        connection = await _row(db, GoogleServiceConnection, 'connection_id', binding.connection_id) if binding else None
        if (connection is None or consent is None or binding is None
            or connection.revision != inputs["expected_connection_revision"]
            or consent.revision != inputs["expected_consent_revision"]
            or binding.revision != inputs["expected_event_binding_revision"]
            or binding.event_revision != inputs["event_revision"]
            or binding.calendar_list_revision != inputs["calendar_list_revision"]
            or consent.connection_id != binding.connection_id or binding.consent_id != consent.consent_id
            or binding.state != "selected" or binding.consent_revision != consent.revision
            or consent.connection_revision != connection.revision or binding.connection_revision != connection.revision):
            raise BoardError("communication_calendar_source_changed", "Review the affected Calendar source", status_code=409)
    else:
        raise BoardError("communication_source_kind_invalid", "Only existing Mail and Calendar preparation owners are allowed", status_code=422)
    for row in (connection, consent, binding):
        if row.owner_principal_id != owner.principal_id or row.owner_session_id != owner.session_id:
            raise BoardError("communication_source_owner_changed", "The source owner changed", status_code=403)
    if (connection.state != "active" or consent.state != "active"
        or consent.goal_id != inputs["goal_id"] or consent.goal_revision != goal_revision
        or utc(consent.expires_at) <= datetime.now(timezone.utc)):
        raise BoardError("communication_source_expired", "Review the affected source permission", status_code=409)
    allowed = consent.model_egress_allowed if capability == "work.mail-reply-draft.v1" else consent.allow_remote_model
    if not allowed:
        raise BoardError("communication_source_model_permission_required", "The source requires its own model-egress permission", status_code=403)


def binding_payload(binding):
    return {name: (value.model_dump(mode="json") if hasattr(value, "model_dump") else
        value.isoformat() if isinstance(value, datetime) else value)
        for name, value in vars(binding).items() if not name.startswith("_")}


def binding_authority(binding):
    """Closed scalar provenance; the private object/current rows grant scope."""
    from src.work_board.communication_contracts import CommunicationPreparationMarker
    if type(binding) is not CommunicationPreparationBinding or binding._seal is not _PREPARATION_SEAL:
        raise BoardError("communication_preparation_seal_required", "Original source producer required", status_code=409)
    return CommunicationPreparationMarker(binding_digest=digest(binding_payload(binding)),
        native_invocation_id=binding.native.invocation_id, parent_job_id=binding.native.parent_job_id,
        group_digest=digest(binding.group.model_dump(mode="json")), ordinal=binding.ordinal,
        source_task_id=binding.source_task_id, source_attempt_id=binding.source_attempt_id,
        source_job_id=binding.source_job_id).model_dump(mode="json")


def verify_source_projection(binding, task, attempt, projection):
    if type(binding) is not CommunicationPreparationBinding or binding._seal is not _PREPARATION_SEAL:
        raise BoardError("communication_preparation_seal_required", "Original source producer required", status_code=409)
    if (task.task_id != binding.source_task_id or attempt.attempt_id != binding.source_attempt_id
        or projection.get("job_id") != binding.source_job_id
        or projection.get("parent_job_id") != binding.native.invocation_id
        or projection.get("root_run_identity") != binding.native.parent_job_id
        or projection.get("parent_fencing_token") != binding.child_fence
        or projection.get("declared_authority", {}).get("communication_preparation") != binding_authority(binding)):
        raise BoardError("communication_source_projection_changed", "Exact original source native projection required", status_code=409)


async def verify_preparation_binding(db, binding, source_run=None, *, allow_succeeded=False):
    from src.db.models import WorkflowRunState, WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact
    if type(binding) is not CommunicationPreparationBinding or binding._seal is not _PREPARATION_SEAL:
        raise BoardError("communication_preparation_seal_required", "Original source preparation issuer required", status_code=409)
    child, native, envelope = await _verify_native_sql(db, binding._physical_witness, binding.child_fence)
    if native != binding.native or child.lease_owner != binding.child_owner:
        raise BoardError("communication_preparation_fence_changed", "Original source producer fence required", status_code=409)
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == native.parent_job_id))
    task = await _row(db, WorkBoardTask, 'task_id', native.task_id)
    check_envelope(envelope)
    if envelope.proposal_group != binding.group:
        raise BoardError("communication_original_group_changed", "Original task allowance required", status_code=409)
    selection = envelope.task_input.communication_selection
    choices = [("work.mail-reply-draft.v1", value) for value in selection.reply_inputs]
    choices += [("calendar.meeting-prep.v1", value) for value in selection.meeting_inputs]
    if not 0 <= binding.ordinal < len(choices):
        raise BoardError("communication_source_ordinal_changed", "Original selected source ordinal required", status_code=409)
    capability, inputs = choices[binding.ordinal]
    if capability != binding.capability_id or digest(inputs) != binding.source_choice_digest:
        raise BoardError("communication_source_choice_changed", "Original source choice required", status_code=409)
    await assert_source_current(db, WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id), capability, inputs)
    source_task = await _row(db, WorkBoardTask, 'task_id', binding.source_task_id)
    source_attempt = await _row(db, WorkBoardAttempt, 'attempt_id', binding.source_attempt_id)
    artifact = await _row(db, WorkBoardInputArtifact, 'artifact_id', binding.input_artifact_id)
    reservation = json.loads(child.checkpoint_context_json or "{}").get("communication_preparations", {}).get(str(binding.ordinal))
    if (reservation is None or reservation.get("binding") != binding_payload(binding)
        or reservation.get("binding_digest") != digest(binding_payload(binding))
        or source_task is None or source_attempt is None or artifact is None
        or source_task.owner_principal_id != task.owner_principal_id or source_task.owner_session_id != task.owner_session_id
        or source_task.goal_id != task.goal_id or source_task.goal_revision != task.goal_revision
        or source_task.capability_id != capability or source_task.input_artifact_id != artifact.artifact_id
        or artifact.payload_sha256 != binding.input_artifact_digest or source_attempt.task_id != source_task.task_id
        or source_attempt.cancel_requested_at is not None or source_attempt.ended_at is not None
        or str(getattr(source_task.status, "value", source_task.status)) != "running"
        or artifact.owner_principal_id != task.owner_principal_id or artifact.owner_session_id != task.owner_session_id
        or artifact.goal_id != task.goal_id or artifact.goal_revision != task.goal_revision
        or artifact.capability_id != capability or artifact.capability_version != "1"
        or artifact.bound_task_id != source_task.task_id or artifact.state not in {"bound", "consumed"}
        or utc(artifact.expires_at) <= datetime.now(timezone.utc)
        or reservation.get("phase") not in {"reserved", "admitted", "running"}
        or source_attempt.workflow_run_id not in {None, "", binding.source_job_id}):
        raise BoardError("communication_original_source_task_changed", "Original source task, artifact and attempt required", status_code=409)
    if source_run is not None:
        if (json.loads(source_run.declared_authority_json or "{}").get("communication_preparation") != binding_authority(binding)
            or source_run.run_identity != binding.source_job_id or source_run.parent_job_id != child.run_identity
            or source_run.parent_fencing_token != binding.child_fence
            or source_run.root_run_identity != parent.run_identity
            or source_run.owner_principal_id != task.owner_principal_id
            or source_run.operator_session_id != task.owner_session_id
            or source_run.session_id != task.owner_session_id or source_run.owner_kind != "user"
            or source_run.job_kind != {"work.mail-reply-draft.v1": "mail_reply_draft", "calendar.meeting-prep.v1": "calendar_meeting_prep"}[capability]
            or source_run.status not in ({"accepted", "queued", "running", "succeeded"} if allow_succeeded else {"accepted", "queued", "running"})
            or (source_run.status == "running" and source_attempt.workflow_run_id != source_run.run_identity)
            or source_run.goal_id != task.goal_id or source_run.goal_revision != task.goal_revision
            or utc(source_run.deadline_at) != binding.source_deadline_at
            or binding.source_deadline_at > min(binding.group.original_deadline_at, binding.native.native_deadline_at)
            or binding.source_deadline_at <= datetime.now(timezone.utc)
            or source_run.budget_digest != digest({"budget_microusd": binding.budget_microusd})):
            raise BoardError("communication_original_source_run_changed", "Original source native lineage required", status_code=409)
    return binding


async def issue_preparation_binding(db, *, native, child_owner, child_fence, group, ordinal,
        capability_id, source_choice_digest, source_task_id, source_attempt_id,
        input_artifact_id, input_artifact_digest, source_job_id, source_deadline_at,
        budget_microusd, physical_witness):
    """Issue only against a previously persisted exact original reservation."""
    binding = CommunicationPreparationBinding(native=native, child_owner=child_owner,
        child_fence=child_fence, group=group, ordinal=ordinal, capability_id=capability_id,
        source_choice_digest=source_choice_digest, source_task_id=source_task_id,
        source_attempt_id=source_attempt_id, input_artifact_id=input_artifact_id,
        input_artifact_digest=input_artifact_digest, source_job_id=source_job_id,
        source_deadline_at=source_deadline_at, budget_microusd=budget_microusd, _seal=_PREPARATION_SEAL,
        _physical_witness=physical_witness)
    return await verify_preparation_binding(db, binding)


async def _write_child_context(db, child, checkpoint, physical):
    from sqlalchemy import update
    from src.db.models import WorkflowRunState, WorkBoardAttempt
    from src.workflows.job_runtime import _append_goal_fence_condition
    from sqlalchemy.orm import aliased
    await _verify_native_sql(db, physical, child.fencing_token)
    now = datetime.now(timezone.utc)
    conditions = [WorkflowRunState.run_identity == child.run_identity,
        WorkflowRunState.revision == child.revision, WorkflowRunState.status == "running",
        WorkflowRunState.fencing_token == child.fencing_token, WorkflowRunState.lease_owner == child.lease_owner,
        WorkflowRunState.lease_expires_at > now]
    _append_goal_fence_condition(conditions, child)
    parent = aliased(WorkflowRunState)
    conditions.append(select(parent.run_identity).where(
        parent.run_identity == physical.native.parent_job_id, parent.status == "paused",
        parent.fencing_token == physical.manifest.job_fence,
        parent.deadline_at > now).exists())
    encoded = canonical(checkpoint).decode()
    result = await db.execute(update(WorkflowRunState).where(*conditions)
        .values(checkpoint_context_json=encoded, revision=WorkflowRunState.revision + 1, updated_at=now)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise BoardError("communication_child_context_conflict", "Original source producer fence changed", status_code=409)
    await db.refresh(child)


class PreparationAdmission:
    def __init__(self, binding, *, _seal):
        if _seal is not _ADMISSION_SEAL:
            raise PermissionError("original communications admission issuer required")
        self.binding = binding
        self._seal = _seal

    async def __call__(self, db, run):
        await verify_preparation_binding(db, self.binding, source_run=run)
        from src.db.models import WorkflowRunState
        child = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == self.binding.native.invocation_id)
            .execution_options(populate_existing=True))
        checkpoint = json.loads(child.checkpoint_context_json or "{}")
        reservations = checkpoint["communication_preparations"]
        for ordinal, record in reservations.items():
            if ordinal != str(self.binding.ordinal) and record.get("phase") in {"reserved", "admitted", "running", "unknown"}:
                raise BoardError("communication_original_source_pending", "Inspect the original source producer", status_code=409)
        record = reservations[str(self.binding.ordinal)]
        if record["phase"] != "reserved":
            raise BoardError("communication_preparation_already_admitted", "Inspect the original source job; do not replay", status_code=409)
        record["phase"] = "admitted"
        # This update and the original native INSERT share admit_job's writer.
        await _write_child_context(db, child, checkpoint, self.binding._physical_witness)


def preparation_admission(binding):
    if type(binding) is not CommunicationPreparationBinding or binding._seal is not _PREPARATION_SEAL:
        raise PermissionError("original sealed source preparation required")
    return PreparationAdmission(binding, _seal=_ADMISSION_SEAL)


async def verify_admission_spec(db, guard, spec):
    if type(guard) is not PreparationAdmission or guard._seal is not _ADMISSION_SEAL:
        raise BoardError("communication_preparation_admission_required", "Original preparation admission guard required", status_code=409)
    binding = await verify_preparation_binding(db, guard.binding)
    expected_kind = {"work.mail-reply-draft.v1": "mail_reply_draft", "calendar.meeting-prep.v1": "calendar_meeting_prep"}[binding.capability_id]
    if (spec.identity.job_kind != expected_kind or spec.identity.job_id != binding.source_job_id
        or spec.parent_job_id != binding.native.invocation_id or spec.parent_fencing_token != binding.child_fence
        or spec.declared_authority.get("communication_preparation") != binding_authority(binding)):
        raise BoardError("communication_preparation_spec_changed", "Exact original source native admission required", status_code=409)
    return True


async def _publish_source(principal, child_id, fence, ordinal, dispatcher):
    """Actual source Task/Attempt and original reservation in one writer."""
    from datetime import timedelta
    from sqlalchemy import text
    from src.db.models import WorkBoardStatus
    from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardTaskCreate
    from src.work_board.input_artifacts import prepare_input_artifact, stage_input_artifact
    from src.work_board.repository import stage_safe_task_text
    from src.model_fabric.configuration import effective_workload_policy
    from src.workflows.mail_reply_draft import reply_job_id
    from src.integrations.google_calendar import calendar_job_id
    async with dispatcher.session_provider() as db:
        child, native, envelope, physical = await stage_native_witness(db, principal, child_id, fence)
        selection = envelope.task_input.communication_selection
        choices = [("work.mail-reply-draft.v1", value) for value in selection.reply_inputs]
        choices += [("calendar.meeting-prep.v1", value) for value in selection.meeting_inputs]
        capability, source_input = choices[ordinal]
        owner = WorkBoardOwner(principal_id=native.owner_principal_id, session_id=native.original_root_id)
        await assert_source_current(db, owner, capability, source_input)
        key = "communication:" + digest([child_id, native.creation_digest, ordinal])
        metadata = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(
            schema_version=1, capability_id=capability, goal_id=envelope.task_input.goal_ref,
            goal_revision=envelope.proposal_group.goal_revision, input=source_input, idempotency_key=key))
        staged = await stage_input_artifact(db, owner, artifact_id=metadata.artifact_id,
            capability_id=capability, goal_id=envelope.task_input.goal_ref,
            goal_revision=envelope.proposal_group.goal_revision)
        request = WorkBoardTaskCreate(title="Prepare selected communications source",
            body="Original bounded communications task preparation.",
            goal_id=envelope.task_input.goal_ref, goal_revision=envelope.proposal_group.goal_revision,
            capability_id=capability, input_artifact_id=metadata.artifact_id, status=WorkBoardStatus.todo,
            priority=70, idempotency_scope="communication-source", idempotency_key=key)
        staged_text = await stage_safe_task_text(db, owner, request)
        witness = _SourcePublication(principal, native, fence, ordinal, capability,
            digest(source_input), metadata.artifact_id, metadata.typed_input_digest, _PUBLICATION_SEAL, physical)
        await db.rollback()
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        await verify_source_publication(db, witness)
        child, current_native, current_envelope = await _verify_native_sql(db, physical, fence)
        checkpoint = json.loads(child.checkpoint_context_json or "{}")
        records = checkpoint.setdefault("communication_preparations", {})
        if str(ordinal) in records or any(record.get("phase") in {"reserved", "admitted", "running", "unknown"} for record in records.values()):
            raise BoardError("communication_original_source_pending", "Inspect original source work; do not replay", status_code=409)
        mutation = await dispatcher.repository._create_task(db, owner, request,
            staged_text=staged_text, staged_input=staged,
            publication_authority_check=lambda writer: verify_source_publication(writer, witness))
        if mutation.idempotent_replay:
            raise BoardError("communication_original_source_pending", "Inspect the original internal source task", status_code=409)
        # The row never becomes scheduler-visible Ready: both changes commit
        # with its actual original Attempt and fixed-parent reservation below.
        mutation.task.status = WorkBoardStatus.ready
        await db.flush()
        claim = await dispatcher.repository.claim_ready_task(db, mutation.task.task_id,
            expected_revision=mutation.task.task_revision, lease_owner=dispatcher.runner_id,
            lease_seconds=120, _communication_publication=witness)
        if claim is None:
            raise BoardError("communication_source_claim_failed", "Review the affected original source", status_code=409)
        group = current_envelope.proposal_group
        policy = effective_workload_policy("strategist_agent")
        ceiling = getattr(policy, "max_cost_microusd", None)
        if type(ceiling) is not int or ceiling <= 0 or group.max_cost_microusd <= 0:
            raise BoardError("communication_source_budget_unavailable", "Review original source inference allowance", status_code=409)
        deadline = min(datetime.now(timezone.utc) + timedelta(seconds=120),
            native.native_deadline_at, group.original_deadline_at)
        job_id = (reply_job_id(owner.principal_id, claim.task.task_id, claim.attempt.attempt_id)
            if capability == "work.mail-reply-draft.v1" else calendar_job_id(owner.principal_id, claim.task.task_id, claim.attempt.attempt_id))
        binding = CommunicationPreparationBinding(current_native, child.lease_owner, fence, group,
            ordinal, capability, digest(source_input), claim.task.task_id, claim.attempt.attempt_id,
            metadata.artifact_id, metadata.typed_input_digest, job_id, deadline,
            min(ceiling, group.max_cost_microusd), _PREPARATION_SEAL, physical)
        records[str(ordinal)] = {"phase": "reserved", "binding": binding_payload(binding),
            "binding_digest": digest(binding_payload(binding)), "producer_closed": False}
        await _write_child_context(db, child, checkpoint, physical)
        await verify_preparation_binding(db, binding)
        await db.commit()
        return claim, source_input, binding


async def _source_output(dispatcher, binding, source_input):
    """Use actual source-native receipts and literal physical owner readback."""
    import asyncio
    import hashlib
    from src.work_board.communication_contracts import CommunicationSourceRef
    from src.workflows.mail_reply_draft import read_private_draft, parse_model_output
    from src.integrations.google_calendar import read_calendar_result_bytes, MeetingPrepService
    projection = await dispatcher.jobs.get_job(binding.source_job_id)
    async with dispatcher.session_provider() as db:
        from src.db.models import WorkflowRunState
        source_run = await _row(db, WorkflowRunState, "run_identity", binding.source_job_id)
        if source_run is None:
            raise BoardError("communication_source_readback_required", "Original source native receipt required", status_code=409)
        await verify_preparation_binding(db, binding, source_run=source_run, allow_succeeded=True)
    if projection.get("status") != "succeeded":
        raise BoardError("communication_source_not_verified", "Inspect the original source native outcome", status_code=409)
    kind = "mail_reply_draft" if binding.capability_id == "work.mail-reply-draft.v1" else "calendar_meeting_prep_result"
    matches = [(artifact, effect) for artifact in projection.get("artifacts", [])
        for effect in projection.get("effects", []) if artifact.get("artifact_type") == kind
        and artifact.get("exists") is True and effect.get("receipt_kind") == "readback"
        and effect.get("status") == "succeeded" and effect.get("target_path") == artifact.get("file_path")
        and effect.get("content_sha256") == artifact.get("content_sha256")
        and effect.get("details", {}).get("verified") is True]
    if len(matches) != 1:
        raise BoardError("communication_source_readback_required", "Original source physical readback required", status_code=409)
    artifact, effect = matches[0]
    if binding.capability_id == "work.mail-reply-draft.v1":
        value = await asyncio.to_thread(read_private_draft, artifact["file_path"], artifact["content_sha256"])
        parsed = parse_model_output({"subject": value["subject"], "body": value["plainbody"], "caveats": value["caveats"]})
        if value["message_revision"] != source_input["expected_message_revision"] or value["memory_status"] != "no_learning":
            raise BoardError("communication_source_readback_changed", "Original private reply changed", status_code=409)
        value = parsed.model_dump(mode="json")
        source_id, revision = source_input["message_binding_id"], source_input["expected_message_revision"]
    else:
        from config.settings import settings
        raw = await asyncio.to_thread(read_calendar_result_bytes, artifact["file_path"], workspace_root=settings.workspace_dir)
        if raw is None or hashlib.sha256(raw).hexdigest() != artifact["content_sha256"]:
            raise BoardError("communication_source_readback_changed", "Original private meeting brief changed", status_code=409)
        value = json.loads(raw)
        result = {key: item for key, item in value.items() if key != "related_sources"}
        value = MeetingPrepService.validate_model_output(result, event_key=result["event_key"], event_revision=source_input["event_revision"])
        source_id, revision = source_input["event_binding_id"], source_input["event_revision"]
    reference = CommunicationSourceRef(source_id=source_id, capability_id=binding.capability_id,
        source_revision=revision, source_input_digest=projection["input_digest"], task_id=binding.source_task_id,
        attempt_id=binding.source_attempt_id, job_id=binding.source_job_id, artifact_path=artifact["file_path"],
        artifact_digest=artifact["content_sha256"], readback_id=effect["readback_id"])
    return reference, value, projection


async def _run_source(principal, child_id, fence, ordinal, dispatcher):
    """The original callback awaits admission, execution and physical readback."""
    from src.db.models import WorkBoardStatus
    claim, inputs, binding = await _publish_source(principal, child_id, fence, ordinal, dispatcher)
    with preparation_scope(binding):
        await dispatcher._execute_direct_adapter(claim.task, claim.attempt, inputs,
            runtime_seconds=120, admission_only=True, communication_binding=binding)
        projection = await dispatcher.jobs.get_job(binding.source_job_id)
        expected = dispatcher._canonical_identity_from_projection(claim.task, claim.attempt, inputs,
            projection, communication_binding=binding)
        async with dispatcher.session_provider() as db:
            linked = await dispatcher.repository.link_attempt_workflow_run(db, claim.task.task_id,
                claim.attempt.attempt_id, workflow_run_id=binding.source_job_id,
                expected_revision=claim.task.task_revision, board_fence=claim.attempt.fencing_token,
                lease_owner=dispatcher.runner_id, workflow_projection=projection, expected_identity=expected)
        queued = await dispatcher.jobs.queue_job(binding.source_job_id,
            expected_revision=projection["revision"], reason="communication_original_source_linked")
        await dispatcher.jobs.claim_job(binding.source_job_id, owner=dispatcher.runner_id,
            lease_seconds=120, expected_state="queued", expected_revision=queued["revision"])
        result = await dispatcher._execute_direct_adapter(linked.task, linked.attempt, inputs,
            runtime_seconds=120, admission_only=False, communication_binding=binding)
        reference, value, projection = await _source_output(dispatcher, binding, inputs)
        proof = dispatcher._direct_readback(result, projection, binding.source_job_id,
            communication_binding=binding)
        if proof is None:
            raise BoardError("communication_source_readback_required", "Original verified source native receipt required", status_code=409)
        await dispatcher._project(linked.task, linked.attempt, board_revision=linked.task.task_revision,
            status=WorkBoardStatus.review, outcome="verified", proof=proof,
            artifact_refs=projection["artifacts"], result_refs=[{"job_id": binding.source_job_id,
                "status": "succeeded", "verified": True}], communication_binding=binding)
    async with dispatcher.session_provider() as db:
        from sqlalchemy import text
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        child, _native, _envelope = await _verify_native_sql(db, binding._physical_witness, fence)
        checkpoint = json.loads(child.checkpoint_context_json or "{}")
        record = checkpoint["communication_preparations"][str(ordinal)]
        if record["binding"] != binding_payload(binding):
            raise BoardError("communication_source_reservation_changed", "Original source reservation required", status_code=409)
        record.update(phase="verified", producer_closed=True, source_ref=reference.model_dump(mode="json"))
        await _write_child_context(db, child, checkpoint, binding._physical_witness)
    return reference, value


async def invoke(principal, job_id, fencing_token, inputs, *, dispatcher):
    """Compose only this original callback's actual private source readbacks.

    A persisted reservation cannot restart this producer. Unknown source
    execution propagates to the existing native recovery owner, retaining
    its shared inference liabilities and preventing further source contact.
    """
    import asyncio
    import hashlib
    from sqlalchemy import text
    from src.work_board.communication_contracts import (
        CommunicationPlan, CommunicationReply, CommunicationMeeting,
        CommunicationReschedule, CommunicationQuestion, PLAN_MAX_BYTES)
    from src.workflows.mail_reply_draft import prepare_private_draft, publish_private_draft, read_private_draft
    if dispatcher is None:
        raise BoardError("communication_owner_unavailable", "Current native dispatcher required", status_code=409)
    async with dispatcher.session_provider() as db:
        _child, native, envelope, physical = await stage_native_witness(db, principal, job_id, fencing_token)
        selection = envelope.task_input.communication_selection
        if inputs != {"selection_digest": digest(selection.model_dump(mode="json"))}:
            raise BoardError("communication_selection_changed", "Original selection required", status_code=409)
        checkpoint = json.loads(_child.checkpoint_context_json or "{}")
        if checkpoint.get("communication_preparations") or checkpoint.get("communication_private_plan"):
            raise BoardError("communication_original_producer_unavailable", "Inspect original preparation; do not replay", status_code=409)
    owner = WorkBoardOwner(principal_id=native.owner_principal_id, session_id=native.original_root_id)
    choices = [("work.mail-reply-draft.v1", value) for value in selection.reply_inputs]
    choices += [("calendar.meeting-prep.v1", value) for value in selection.meeting_inputs]
    refs, replies, meetings, proposals, questions = [], [], [], [], []
    for ordinal, (capability, source_input) in enumerate(choices):
        source_id = source_input.get("message_binding_id", source_input.get("event_binding_id"))
        # A known source-specific drift before publication affects this entry.
        # No inference operation or producer is created for that source.
        try:
            async with dispatcher.session_provider() as db:
                await _verify_native_sql(db, physical, fencing_token)
                await assert_source_current(db, owner, capability, source_input)
        except BoardError as error:
            if error.code.startswith("communication_original_"):
                raise
            questions.append(CommunicationQuestion(source_id=source_id,
                reason=error.code, recovery="review_source"))
            continue
        reference, value = await _run_source(principal, job_id, fencing_token, ordinal, dispatcher)
        refs.append(reference)
        if capability == "work.mail-reply-draft.v1":
            replies.append(CommunicationReply(source_ref=reference, **value))
        else:
            meetings.append(CommunicationMeeting(source_ref=reference, brief=value))
            for proposal in selection.reschedule_inputs:
                if proposal["event_binding_id"] == source_id:
                    proposals.append(CommunicationReschedule(source_ref=reference, input=proposal))
    plan = CommunicationPlan(source_refs=refs, reply_drafts=replies,
        meeting_preparations=meetings, reschedule_proposals=proposals, unresolved_questions=questions)
    payload = plan.model_dump(mode="json")
    plaintext = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    if len(plaintext) > PLAN_MAX_BYTES:
        raise BoardError("communication_private_plan_too_large", "Review a smaller source selection", status_code=422)
    path, ciphertext_digest, encrypted = await asyncio.to_thread(prepare_private_draft,
        "communication-plan:" + job_id, payload)
    intent = {"file_path": path, "content_sha256": ciphertext_digest,
        "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(), "size_bytes": len(encrypted),
        "source_readbacks": [{"job_id": ref.job_id, "readback_id": ref.readback_id,
            "artifact_digest": ref.artifact_digest} for ref in refs], "no_learning": True}
    # Exact physical publication intent is durable before the private file.
    async with dispatcher.session_provider() as db:
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        child, _native, _envelope = await _verify_native_sql(db, physical, fencing_token)
        checkpoint = json.loads(child.checkpoint_context_json or "{}")
        if checkpoint.get("communication_private_plan"):
            raise BoardError("communication_plan_already_reserved", "Inspect the original private plan", status_code=409)
        checkpoint["communication_private_plan"] = {"phase": "reserved", **intent}
        await _write_child_context(db, child, checkpoint, physical)
    await asyncio.to_thread(publish_private_draft, path, encrypted)
    readback = await asyncio.to_thread(read_private_draft, path, ciphertext_digest)
    if CommunicationPlan.model_validate(readback) != plan:
        raise BoardError("communication_private_plan_changed", "Original physical private plan required", status_code=409)
    async def authority_check(db, run):
        child, _binding, _original = await _verify_native_sql(db, physical, fencing_token)
        checkpoint = json.loads(child.checkpoint_context_json or "{}")
        record = checkpoint.get("communication_private_plan", {})
        if record != {"phase": "reserved", **intent}:
            raise BoardError("communication_private_plan_changed", "Original publication intent required", status_code=409)
    await dispatcher.jobs.record_artifact(job_id, file_path=path,
        artifact_type="communication_private_plan", content=encrypted,
        owner=physical.child_lease_owner, fencing_token=fencing_token)
    await dispatcher.jobs.record_readback(job_id, target_path=path, status="succeeded",
        effect_type="communication_private_plan", content_sha256=ciphertext_digest,
        readback_id="communication-plan:" + digest([job_id, ciphertext_digest]),
        details={"verified": True, "plaintext_sha256": intent["plaintext_sha256"], "no_learning": True},
        owner=physical.child_lease_owner, fencing_token=fencing_token,
        readback_authority_check=authority_check)
    result = {"private_plan_ref": path, "private_plan_digest": ciphertext_digest,
        "producer_job_id": job_id, "source_readbacks": intent["source_readbacks"], "no_learning": True}
    if len(canonical(result)) > METADATA_MAX_BYTES:
        raise BoardError("communication_metadata_too_large", "Review a smaller source selection", status_code=422)
    return result


async def stage_plan_authority(db, principal, job_id, fence, output):
    """Stage literal private readback before the native output SQL writer."""
    import asyncio
    from src.workflows.mail_reply_draft import read_private_draft
    from src.work_board.communication_contracts import CommunicationPlan
    child, _native, _envelope, physical = await stage_native_witness(db, principal, job_id, fence)
    intent = json.loads(child.checkpoint_context_json or "{}").get("communication_private_plan")
    if (not isinstance(intent, dict) or intent.get("phase") != "reserved"
        or output != {"private_plan_ref": intent.get("file_path"),
            "private_plan_digest": intent.get("content_sha256"), "producer_job_id": job_id,
            "source_readbacks": intent.get("source_readbacks"), "no_learning": True}):
        raise BoardError("communication_private_plan_changed", "Exact original private plan metadata required", status_code=409)
    plan = CommunicationPlan.model_validate(await asyncio.to_thread(read_private_draft,
        intent["file_path"], intent["content_sha256"]))
    import hashlib
    raw = json.dumps(plan.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(raw).hexdigest() != intent["plaintext_sha256"]:
        raise BoardError("communication_private_plan_changed", "Original full plan physical readback required", status_code=409)
    async def authority(db, run):
        row, _binding, _original = await _verify_native_sql(db, physical, fence)
        if json.loads(row.checkpoint_context_json or "{}").get("communication_private_plan") != intent:
            raise BoardError("communication_private_plan_changed", "Original private plan intent required", status_code=409)
    return authority


async def read_plan(db, owner, task_id, *, service):
    """Authenticated existing Work reader; caller supplies no artifact path.

    Terminal receipts are evidence, never renewed execution authority. Fresh
    original Root/Goal and each selected source determine private visibility.
    """
    import asyncio
    from src.db.models import WorkflowRunState, WorkBoardAttempt
    from src.api.mail import _assert_live_session
    from src.workflows.job_runtime import _assert_canonical_goal_fence
    from src.workflows.general_task_guard import child_binding
    from src.work_board.general_task_runtime_artifacts import verify_readonly_native_projection
    from src.workflows.general_task_guard import read_manifest
    from src.work_board.pipelines import root_binding
    from src.workflows.mail_reply_draft import read_private_draft
    from src.work_board.communication_contracts import CommunicationPlan, CommunicationQuestion
    task = await service.repository.get_task(db, owner, task_id)
    if task.capability_id != "agent.task.v1":
        raise BoardError("communication_plan_unavailable", "Private communications plan unavailable", status_code=404)
    await _assert_live_session(db, owner)
    await _assert_canonical_goal_fence(db, goal_id=task.goal_id, goal_revision=task.goal_revision,
        owner_kind="user", owner_principal_id=owner.principal_id, session_id=owner.session_id)
    attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
        .order_by(WorkBoardAttempt.created_at.desc()))).scalars().first()
    parent = await _row(db, WorkflowRunState, "run_identity", attempt.workflow_run_id) if attempt else None
    manifest = read_manifest(parent) if parent is not None else None
    if parent is None or manifest is None:
        raise BoardError("communication_plan_not_verified", "Original retained preparation required", status_code=409)
    envelope = await verify_readonly_native_projection(db, owner, parent, task, attempt, manifest)
    check_envelope(envelope)
    if envelope.task_input.communication_selection is None:
        raise BoardError("communication_plan_unavailable", "Private communications plan unavailable", status_code=404)
    candidates = list((await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.job_kind == "general_task_native_tool_v1",
        WorkflowRunState.owner_principal_id == owner.principal_id,
        WorkflowRunState.session_id == owner.session_id))).scalars())
    matches = []
    for child in candidates:
        binding = child_binding(child)
        if (binding.task_id == task_id and binding.attempt_id == attempt.attempt_id
            and binding.parent_job_id == parent.run_identity and binding.original_envelope_digest == task.typed_input_digest):
            intent = json.loads(child.checkpoint_context_json or "{}").get("communication_private_plan")
            if intent:
                matches.append((child, binding, intent))
    if len(matches) != 1:
        raise BoardError("communication_plan_not_verified", "Inspect the original preparation", status_code=409)
    child, binding, intent = matches[0]
    if binding.live_root_digest != digest(root_binding()):
        raise BoardError("communication_root_changed", "Original private workspace Root required", status_code=409)
    artifacts = json.loads(child.artifact_receipts_json or "[]")
    effects = json.loads(child.effect_receipts_json or "[]")
    if not (any(item.get("artifact_type") == "communication_private_plan"
            and item.get("file_path") == intent["file_path"] and item.get("content_sha256") == intent["content_sha256"]
            and item.get("exists") is True for item in artifacts)
        and any(item.get("effect_type") == "communication_private_plan" and item.get("receipt_kind") == "readback"
            and item.get("status") == "succeeded" and item.get("target_path") == intent["file_path"]
            and item.get("content_sha256") == intent["content_sha256"] and item.get("details", {}).get("verified") is True
            for item in effects)):
        raise BoardError("communication_plan_not_verified", "Original private physical readback required", status_code=409)
    plan = CommunicationPlan.model_validate(await asyncio.to_thread(read_private_draft,
        intent["file_path"], intent["content_sha256"]))
    selection = envelope.task_input.communication_selection
    sources = [("work.mail-reply-draft.v1", value) for value in selection.reply_inputs]
    sources += [("calendar.meeting-prep.v1", value) for value in selection.meeting_inputs]
    visible, questions = [], list(plan.unresolved_questions)
    records = json.loads(child.checkpoint_context_json or "{}").get("communication_preparations", {})
    for ref in plan.source_refs:
        choices = [(ordinal, value) for ordinal, (capability, value) in enumerate(sources)
            if capability == ref.capability_id and value.get("message_binding_id", value.get("event_binding_id")) == ref.source_id]
        if len(choices) != 1:
            raise BoardError("communication_plan_source_changed", "Exact original selected source required", status_code=409)
        ordinal, source_input = choices[0]
        record = records.get(str(ordinal), {})
        if (record.get("phase") != "verified" or record.get("producer_closed") is not True
            or record.get("source_ref") != ref.model_dump(mode="json")):
            raise BoardError("communication_plan_source_changed", "Exact original source readback required", status_code=409)
        try:
            await assert_source_current(db, owner, ref.capability_id, source_input)
        except BoardError as error:
            questions.append(CommunicationQuestion(source_id=ref.source_id, reason=error.code,
                job_id=ref.job_id, recovery="review_source"))
        else:
            visible.append(ref)
    allowed = {ref.source_input_digest for ref in visible}
    return CommunicationPlan(source_refs=visible,
        reply_drafts=[value for value in plan.reply_drafts if value.source_ref.source_input_digest in allowed],
        meeting_preparations=[value for value in plan.meeting_preparations if value.source_ref.source_input_digest in allowed],
        reschedule_proposals=[value for value in plan.reschedule_proposals if value.source_ref.source_input_digest in allowed],
        unresolved_questions=questions)


async def review_bundle(db, owner, operator, task_id, bundle, *, service):
    """Validate independently approved existing operations, with no effects.

    The returned bundle grants no permission. Each original Mail/Calendar
    execute route performs its existing exact current approval check again.
    """
    from src.db.models import ApprovalRequest
    from src.approval.repository import approval_decision_digest
    from src.integrations import mail_reply_runtime, calendar_reschedule_runtime
    from src.work_board.input_artifacts import resolve_input_artifact_for_task
    from src.work_board.dispatcher import CalendarRescheduleInput
    plan = await read_plan(db, owner, task_id, service=service)
    replies = {value.source_ref.source_input_digest: value for value in plan.reply_drafts}
    proposals = {value.source_ref.source_input_digest: value for value in plan.reschedule_proposals}
    for action, preview_digest, approval_id in zip(bundle.selected_actions,
            bundle.exact_preview_digests, bundle.approval_ids, strict=True):
        value = (replies if action.kind == "reply" else proposals).get(action.source_input_digest)
        if value is None:
            raise BoardError("communication_selected_source_changed", "Review the affected current plan entry", status_code=409)
        runtime = mail_reply_runtime if action.kind == "reply" else calendar_reschedule_runtime
        run = await runtime.get_run(operator, action.operation_id, db=db)
        if run.job_kind != runtime.SEND_KIND:
            raise BoardError("communication_operation_kind_changed", "Original exact action preview required", status_code=409)
        await runtime.authority(db, operator, run, source_required=True)
        arguments = runtime.arguments(run)
        source_task_id = arguments.get("source", {}).get("task_id")
        if action.kind == "reply":
            if source_task_id != value.source_ref.task_id:
                raise BoardError("communication_action_source_changed", "Original prepared reply required", status_code=409)
        else:
            source_task = await service.repository.get_task(db, owner, source_task_id)
            resolved = await resolve_input_artifact_for_task(db, owner,
                artifact_id=source_task.input_artifact_id, goal_id=source_task.goal_id,
                goal_revision=source_task.goal_revision, capability_id=source_task.capability_id,
                expected_task_id=source_task_id)
            if CalendarRescheduleInput.model_validate(resolved.input).model_dump(mode="json") != value.input:
                raise BoardError("communication_action_source_changed", "Original exact reschedule proposal required", status_code=409)
        preview = runtime.state(run).get("preview", {})
        row = await db.get(ApprovalRequest, approval_id, populate_existing=True)
        if (row is None or preview.get("approval_id") != approval_id
            or row.owner_principal_id != owner.principal_id or row.operator_session_id != owner.session_id
            or row.status != "approved" or utc(row.expires_at) <= datetime.now(timezone.utc)
            or approval_decision_digest(row) != preview_digest):
            raise BoardError("communication_exact_action_approval_required", "Each action requires its own current exact approval", status_code=409)
    return bundle
