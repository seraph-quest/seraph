"""Bounded immutable native plan/receipt staging on existing private artifacts."""
from dataclasses import dataclass
import hashlib
import json

from src.work_board.contracts import (GeneralTaskArtifactRef, GeneralTaskEnvelope,
    GeneralTaskPlanRevisionV1, GeneralTaskStepReceiptV1, GeneralTaskToolInputV1, WorkBoardOwner)
from src.work_board.repository import BoardError

_STAGING_SEAL = object()
_ARTIFACT_MODELS = {"GeneralTaskPlanRevision.v1": GeneralTaskPlanRevisionV1,
    "StepReceipt.v1": GeneralTaskStepReceiptV1, "GeneralTaskToolInput.v1": GeneralTaskToolInputV1}
_ARTIFACT_KINDS = {"GeneralTaskPlanRevision.v1": "general_task_plan_revision",
    "StepReceipt.v1": "general_task_step_receipt", "GeneralTaskToolInput.v1": "general_task_tool_input"}


@dataclass(frozen=True)
class StagedTaskArtifact:
    reference: GeneralTaskArtifactRef
    file_path: str
    size_bytes: int
    producer_ref: str
    creation_digest: str
    payload: bytes
    seal: object


def stage_task_artifact(*, parent_job_id, creation_digest, payload):
    from src.work_board.general_task import canonical, digest
    from src.work_board.input_artifacts import _write_payload, _safe_file_bytes
    from src.artifacts.registry import artifact_id_for
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    if not isinstance(payload, (GeneralTaskPlanRevisionV1, GeneralTaskStepReceiptV1, GeneralTaskToolInputV1)):
        raise BoardError("general_task_artifact_schema", "Native immutable artifact schema required", status_code=409)
    if (isinstance(payload, (GeneralTaskPlanRevisionV1, GeneralTaskToolInputV1)) and (payload.parent_job_id != parent_job_id or payload.creation_digest != creation_digest)
        or isinstance(payload, GeneralTaskStepReceiptV1) and payload.parent_creation_digest != creation_digest):
        raise BoardError("general_task_artifact_binding", "Native artifact belongs to another creation", status_code=409)
    content = canonical(payload.model_dump(mode="json"))
    sha = hashlib.sha256(content).hexdigest()
    key = digest([parent_job_id, creation_digest, payload.schema_version, sha])
    path = f"artifacts/work-board/general-tasks/{key}-{sha}.json"
    absolute = canonical_workspace_root(settings.workspace_dir) / path
    _write_payload(absolute, content)
    if _safe_file_bytes(absolute, expected_digest=sha, expected_size=len(content)) != content:
        raise BoardError("general_task_artifact_changed", "Native artifact failed physical readback", status_code=409)
    kind = _ARTIFACT_KINDS[payload.schema_version]
    identifier = artifact_id_for(file_path=path, artifact_type=kind, producer="agent.task.v1",
        run_id=parent_job_id, content_sha256=sha)
    return StagedTaskArtifact(GeneralTaskArtifactRef(artifact_id=identifier, digest=sha,
        schema_version=payload.schema_version), path, len(content), parent_job_id, creation_digest, content, _STAGING_SEAL)


def verify_staged_task_artifact(staged, *, parent_job_id, creation_digest):
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    from src.artifacts.registry import build_artifact_record
    if (not isinstance(staged, StagedTaskArtifact) or staged.seal is not _STAGING_SEAL
        or staged.producer_ref != parent_job_id or staged.creation_digest != creation_digest
        or not 0 < staged.size_bytes <= 65536):
        raise BoardError("general_task_artifact_binding", "Exact native staged artifact required", status_code=409)
    content = _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / staged.file_path,
        expected_digest=staged.reference.digest, expected_size=staged.size_bytes)
    if content != staged.payload:
        raise BoardError("general_task_artifact_changed", "Native immutable artifact changed", status_code=409)
    model = _ARTIFACT_MODELS[staged.reference.schema_version]
    parsed = model.model_validate_json(content)
    kind = _ARTIFACT_KINDS[staged.reference.schema_version]
    record = build_artifact_record(file_path=staged.file_path, artifact_type=kind,
        producer="agent.task.v1", run_id=parent_job_id, content=content)
    if record["artifact_id"] != staged.reference.artifact_id or record["content_sha256"] != staged.reference.digest:
        raise BoardError("general_task_artifact_binding", "Native registry reference changed", status_code=409)
    return parsed, record


def read_native_artifact_reference(reference, *, parent_job_id, creation_digest):
    """Resolve an opaque bounded ref by the fixed native immutable name recipe."""
    from src.work_board.general_task import digest
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.workspace import canonical_workspace_root
    from src.artifacts.registry import artifact_id_for
    from config.settings import settings
    if not isinstance(reference, GeneralTaskArtifactRef) or reference.schema_version not in _ARTIFACT_MODELS:
        raise BoardError("general_task_artifact_schema", "Known immutable native reference required", status_code=409)
    key = digest([parent_job_id, creation_digest, reference.schema_version, reference.digest])
    path = f"artifacts/work-board/general-tasks/{key}-{reference.digest}.json"
    absolute = canonical_workspace_root(settings.workspace_dir) / path
    try:
        size = absolute.lstat().st_size
    except OSError as exc:
        raise BoardError("general_task_artifact_missing", "Inspect missing immutable task evidence", status_code=409) from exc
    if not 0 < size <= 65536:
        raise BoardError("general_task_artifact_schema", "Immutable native evidence exceeds its bound", status_code=409)
    content = _safe_file_bytes(absolute, expected_digest=reference.digest, expected_size=size)
    kind = _ARTIFACT_KINDS[reference.schema_version]
    if reference.artifact_id != artifact_id_for(file_path=path, artifact_type=kind,
        producer="agent.task.v1", run_id=parent_job_id, content_sha256=reference.digest):
        raise BoardError("general_task_artifact_binding", "Immutable native artifact identity changed", status_code=409)
    model = _ARTIFACT_MODELS[reference.schema_version]
    parsed = model.model_validate_json(content)
    if (model in {GeneralTaskPlanRevisionV1, GeneralTaskToolInputV1} and (parsed.parent_job_id != parent_job_id or parsed.creation_digest != creation_digest)
        or model is GeneralTaskStepReceiptV1 and parsed.parent_creation_digest != creation_digest):
        raise BoardError("general_task_artifact_binding", "Immutable native creation binding changed", status_code=409)
    return parsed


async def read_current_native_envelope(db, run, task, attempt):
    from src.work_board.input_artifacts import resolve_input_artifact_for_task
    if (task.capability_id != "agent.task.v1" or attempt.task_id != task.task_id
        or attempt.workflow_run_id != run.run_identity or attempt.ended_at is not None
        or attempt.cancel_requested_at is not None or task.owner_principal_id != run.owner_principal_id
        or task.owner_session_id != run.session_id or run.operator_session_id != task.owner_session_id
        or task.goal_id != run.goal_id or task.goal_revision != run.goal_revision):
        raise BoardError("general_task_native_binding_changed", "Original native task binding changed", status_code=409)
    resolved = await resolve_input_artifact_for_task(db,
        WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id),
        artifact_id=task.input_artifact_id, goal_id=task.goal_id, goal_revision=task.goal_revision,
        capability_id="agent.task.v1", expected_task_id=task.task_id)
    envelope = GeneralTaskEnvelope.model_validate(resolved.input)
    if envelope.proposal_group is None:
        raise BoardError("general_task_provenance_missing", "Inspect the original task allowance", status_code=409)
    return envelope


def selected_grant_digest(envelope):
    from src.work_board.general_task import digest
    return digest([item.model_dump(mode="json") for item in envelope.descriptors])


def compile_creation_digest(parent, task, attempt, envelope):
    from src.work_board.general_task import digest
    from src.work_board.pipelines import root_binding
    from src.workflows.inference_accounting import _utc
    return digest(["general-task.creation.v1", parent.run_identity, parent.input_digest,
        parent.authority_digest, task.task_id, attempt.attempt_id, task.owner_principal_id,
        task.owner_session_id, task.goal_id, task.goal_revision, task.input_artifact_id,
        task.typed_input_digest, envelope.proposal_group.group_id,
        digest(envelope.proposal_group.model_dump(mode="json")), selected_grant_digest(envelope),
        digest(root_binding()), _utc(parent.deadline_at).isoformat(),
        envelope.proposal_group.original_deadline_at.isoformat()])


def compile_phase_digest(manifest):
    from src.work_board.general_task import digest
    return digest(["general-task.phase.v1", manifest.creation_digest, manifest.phase_revision,
        manifest.phase, manifest.plan_revision, manifest.current_plan_digest,
        manifest.admitted_invocation_ids, manifest.job_fence, manifest.board_fence])


def initial_native_manifest(parent, task, attempt, envelope):
    """Freeze revision one from the canonical original publication."""
    from src.work_board.contracts import GeneralTaskCurrentManifestV1
    from src.work_board.general_task import digest
    from src.workflows.inference_accounting import _utc
    group = envelope.proposal_group
    if group is None or envelope.plan is None or envelope.plan.revision != 1:
        raise BoardError("general_task_provenance_missing", "Original published plan required", status_code=409)
    manifest = GeneralTaskCurrentManifestV1(
        task_id=task.task_id, original_root_id=task.owner_session_id,
        owner_principal_id=task.owner_principal_id, attempt_id=attempt.attempt_id,
        run_id=parent.run_identity, task_revision=task.task_revision,
        manifest_revision=1, board_fence=attempt.fencing_token, job_fence=parent.fencing_token,
        original_envelope_artifact_id=task.input_artifact_id,
        original_envelope_digest=task.typed_input_digest, original_input_digest=parent.input_digest,
        selected_grant_digest=selected_grant_digest(envelope), group_id=group.group_id,
        group_digest=digest(group.model_dump(mode="json")), original_limits_digest=group.limits_digest,
        creation_digest=compile_creation_digest(parent, task, attempt, envelope),
        original_deadline_at=group.original_deadline_at, native_deadline_at=_utc(parent.deadline_at),
        phase="native_ready", phase_revision=1,
        phase_digest="0" * 64, plan_revision=1, current_plan_artifact_id=task.input_artifact_id,
        current_plan_digest=digest(envelope.plan.model_dump(mode="json")), revision_numbers=[1],
        revision_artifact_ids=[task.input_artifact_id], revision_artifact_digests=[task.typed_input_digest],
        revision_artifact_schemas=["GeneralTaskEnvelope.v1"])
    return manifest.model_copy(update={"phase_digest": compile_phase_digest(manifest)})


def compile_native_child_binding(parent, task, attempt, manifest, step, descriptor, inputs):
    """Derive one child identity from frozen native facts, never planner text."""
    from src.work_board.contracts import GeneralTaskNativeChildBindingV1
    from src.work_board.general_task import digest
    from src.work_board.pipelines import root_binding
    invocation_id = "general-tool:" + digest([manifest.creation_digest, manifest.plan_revision,
        step.step_id, digest(inputs), digest(descriptor.model_dump(mode="json"))])[:48]
    return GeneralTaskNativeChildBindingV1(parent_job_id=parent.run_identity,
        task_id=task.task_id, attempt_id=attempt.attempt_id,
        original_root_id=task.owner_session_id, owner_principal_id=task.owner_principal_id,
        goal_id=task.goal_id, goal_revision=task.goal_revision,
        original_deadline_at=manifest.original_deadline_at,
        native_deadline_at=manifest.native_deadline_at,
        original_envelope_digest=manifest.original_envelope_digest,
        parent_authority_digest=parent.authority_digest, creation_digest=manifest.creation_digest,
        creation_job_fence=parent.fencing_token, creation_board_fence=attempt.fencing_token,
        plan_revision=manifest.plan_revision, plan_digest=manifest.current_plan_digest,
        step_id=step.step_id, invocation_id=invocation_id, input_digest=digest(inputs),
        descriptor_digest=digest(descriptor.model_dump(mode="json")),
        selected_grant_digest=manifest.selected_grant_digest,
        phase_revision=manifest.phase_revision, phase_digest=manifest.phase_digest,
        live_root_digest=digest(root_binding()))


async def read_current_native_tool_input(db, child):
    """Resolve private literals only behind the current fixed child guard."""
    from src.workflows.general_task_guard import assert_general_task_child_current, child_binding
    from src.work_board.general_task import digest
    await assert_general_task_child_current(db, child)
    binding = child_binding(child)
    arguments = json.loads(child.arguments_json)
    keys = {"step_id", "tool_id", "tool_input_digest", "descriptor_digest", "typed_input_ref", "typed_input_digest"}
    if (not isinstance(arguments, dict) or set(arguments) != keys
        or arguments["step_id"] != binding.step_id
        or arguments["tool_input_digest"] != binding.input_digest
        or arguments["descriptor_digest"] != binding.descriptor_digest
        or not arguments["typed_input_ref"].startswith("general-task-input:")):
        raise BoardError("general_task_native_input_changed", "Exact private native input required", status_code=409)
    reference = GeneralTaskArtifactRef(artifact_id=arguments["typed_input_ref"].removeprefix("general-task-input:"),
        digest=arguments["typed_input_digest"], schema_version="GeneralTaskToolInput.v1")
    payload = read_native_artifact_reference(reference, parent_job_id=binding.parent_job_id,
        creation_digest=binding.creation_digest)
    if (payload.invocation_id != child.run_identity or payload.tool_id != arguments["tool_id"]
        or payload.descriptor_digest != binding.descriptor_digest
        or payload.input_digest != binding.input_digest or digest(payload.inputs) != binding.input_digest
        or not any(item.get("artifact_id") == reference.artifact_id
            and item.get("content_sha256") == reference.digest
            for item in json.loads(child.artifact_receipts_json))):
        raise BoardError("general_task_native_input_changed", "Canonical private input readback changed", status_code=409)
    return payload


async def resolve_current_native_step_inputs(db, parent, task, attempt, manifest, envelope, step):
    """Resolve dependencies from canonical successful native readbacks only."""
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import child_binding
    from src.workflows.job_runtime import _digest
    from src.work_board.general_task import digest, resolve_input, validate_schema, validate_data
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.artifacts.registry import artifact_id_for
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    from src.work_board.general_task_native import current_plan
    plan = current_plan(manifest, envelope)
    if next((item for item in plan.steps if item.step_id == step.step_id), None) != step:
        raise BoardError("general_task_native_input_changed", "Current canonical plan step required", status_code=409)
    outputs = {}
    descriptors = {item.tool_id: item for item in envelope.descriptors}
    for dependency in step.depends_on:
        if dependency not in manifest.step_ids:
            raise BoardError("general_task_dependency_unverified", "Canonical verified predecessor required", status_code=409)
        index = manifest.step_ids.index(dependency)
        receipt = read_native_artifact_reference(GeneralTaskArtifactRef(
            artifact_id=manifest.step_receipt_artifact_ids[index], digest=manifest.step_receipt_digests[index],
            schema_version="StepReceipt.v1"), parent_job_id=parent.run_identity,
            creation_digest=manifest.creation_digest)
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == receipt.child_job_id))
        if child is None:
            raise BoardError("general_task_dependency_unverified", "Canonical predecessor invocation missing", status_code=409)
        binding = child_binding(child)
        effects = json.loads(child.effect_receipts_json)
        if (receipt.status != "verified" or receipt.contact_state != "settled"
            or child.status != "succeeded" or binding.step_id != dependency
            or binding.creation_digest != manifest.creation_digest
            or binding.selected_grant_digest != manifest.selected_grant_digest
            or binding.original_root_id != task.owner_session_id
            or receipt.input_digest != binding.input_digest
            or receipt.descriptor_digest != binding.descriptor_digest
            or receipt.effect_receipt_digest != _digest(effects)):
            raise BoardError("general_task_dependency_unverified", "Exact successful predecessor readback required", status_code=409)
        references = [ref for ref in receipt.artifact_refs if ref.schema_version == "GeneralTaskOutput.v1"]
        if len(references) != 1:
            raise BoardError("general_task_dependency_unverified", "Exactly one native output artifact required", status_code=409)
        reference = references[0]
        records = [record for record in json.loads(child.artifact_receipts_json)
            if record.get("artifact_id") == reference.artifact_id and record.get("content_sha256") == reference.digest]
        key = digest([child.run_identity, binding.plan_digest, dependency])
        path = f"artifacts/work-board/general-tasks/{key}-{reference.digest}.json"
        if (len(records) != 1 or records[0].get("file_path") != path
            or type(records[0].get("size_bytes")) is not int or not 0 < records[0]["size_bytes"] <= 65536
            or reference.artifact_id != artifact_id_for(file_path=path, artifact_type="general_task_step",
                producer=child.job_kind, run_id=child.run_identity, content_sha256=reference.digest)
            or not any(effect.get("effect_type") == "general_tool_call" and effect.get("status") == "succeeded"
                and effect.get("content_sha256") == reference.digest
                and effect.get("details", {}).get("step_id") == dependency
                and effect.get("details", {}).get("verified") is True for effect in effects)):
            raise BoardError("general_task_dependency_unverified", "Canonical output identity or physical readback changed", status_code=409)
        body = json.loads(_safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / path,
            expected_digest=reference.digest, expected_size=records[0]["size_bytes"]))
        if set(body) != {"step_id", "output"} or body["step_id"] != dependency:
            raise BoardError("general_task_dependency_unverified", "Native output identity changed", status_code=409)
        descriptor = descriptors.get(json.loads(child.arguments_json).get("tool_id"))
        if descriptor is None or digest(descriptor.model_dump(mode="json")) != binding.descriptor_digest:
            raise BoardError("general_task_dependency_unverified", "Original predecessor descriptor changed", status_code=409)
        validate_schema(descriptor.output_schema, body["output"])
        predecessor = next((item for item in plan.steps if item.step_id == dependency), None)
        if predecessor is None:
            raise BoardError("general_task_dependency_unverified", "Frozen predecessor step missing", status_code=409)
        validate_schema(predecessor.output_contract, body["output"])
        outputs[dependency] = body["output"]
    inputs = resolve_input(step.input, outputs)
    validate_data(inputs, dependencies=set())
    validate_schema(descriptors[step.tool_id].input_schema, inputs)
    return inputs


async def verify_general_task_manifest(db, parent, task, attempt, manifest):
    """Current canonical envelope/group check inside the native SQL writer.

    Revision/receipt staging is separately sealed and physically verified;
    the runtime owner supplies and validates the exact current CAS counters.
    """
    from src.work_board.contracts import GeneralTaskCurrentManifestV1
    from src.work_board.general_task import digest
    from src.workflows.inference_accounting import _utc
    envelope = await read_current_native_envelope(db, parent, task, attempt)
    return await _verify_native_manifest_data(parent, task, attempt, manifest, envelope)


async def _verify_native_manifest_data(parent, task, attempt, manifest, envelope):
    from src.work_board.contracts import GeneralTaskCurrentManifestV1
    from src.work_board.general_task import digest
    from src.workflows.inference_accounting import _utc
    if not isinstance(manifest, GeneralTaskCurrentManifestV1):
        raise BoardError("general_task_manifest_invalid", "Closed native manifest required", status_code=409)
    group = envelope.proposal_group
    if (manifest.task_id != task.task_id or manifest.attempt_id != attempt.attempt_id
        or manifest.run_id != parent.run_identity or manifest.original_root_id != task.owner_session_id
        or manifest.owner_principal_id != task.owner_principal_id
        or manifest.original_envelope_artifact_id != task.input_artifact_id
        or manifest.original_envelope_digest != task.typed_input_digest
        or manifest.original_input_digest != parent.input_digest
        or manifest.selected_grant_digest != selected_grant_digest(envelope)
        or manifest.group_id != group.group_id or manifest.group_digest != digest(group.model_dump(mode="json"))
        or manifest.original_limits_digest != group.limits_digest
        or manifest.original_deadline_at != group.original_deadline_at
        or manifest.native_deadline_at != _utc(parent.deadline_at)
        or manifest.native_deadline_at > group.original_deadline_at
        or manifest.creation_digest != compile_creation_digest(parent, task, attempt, envelope)
        or manifest.phase_digest != compile_phase_digest(manifest)
        or len(manifest.admitted_invocation_ids) > group.max_steps
        or len(manifest.step_ids) > group.max_steps
        or manifest.revision_artifact_ids[0] != task.input_artifact_id
        or manifest.revision_artifact_digests[0] != task.typed_input_digest
        or manifest.revision_artifact_schemas[0] != "GeneralTaskEnvelope.v1"):
        raise BoardError("general_task_manifest_binding_changed", "Original native manifest binding changed", status_code=409)
    if manifest.plan_revision == 1 and (manifest.current_plan_artifact_id != task.input_artifact_id
        or manifest.current_plan_digest != digest(envelope.plan.model_dump(mode="json"))):
        raise BoardError("general_task_manifest_plan_changed", "Original immutable plan changed", status_code=409)
    active_plan = envelope.plan
    for index in range(1, len(manifest.revision_numbers)):
        revision = read_native_artifact_reference(GeneralTaskArtifactRef(
            artifact_id=manifest.revision_artifact_ids[index], digest=manifest.revision_artifact_digests[index],
            schema_version=manifest.revision_artifact_schemas[index]), parent_job_id=parent.run_identity,
            creation_digest=manifest.creation_digest)
        if (revision.plan.revision != manifest.revision_numbers[index]
            or revision.original_envelope_digest != manifest.original_envelope_digest
            or revision.selected_grant_digest != manifest.selected_grant_digest
            or revision.original_limits_digest != manifest.original_limits_digest
            or revision.original_deadline_at != manifest.original_deadline_at):
            raise BoardError("general_task_manifest_plan_changed", "Immutable native revision binding changed", status_code=409)
        active_plan = revision.plan
    if manifest.current_plan_digest != digest(active_plan.model_dump(mode="json")) or manifest.current_plan_artifact_id != manifest.revision_artifact_ids[-1]:
        raise BoardError("general_task_manifest_plan_changed", "Current immutable revision reference changed", status_code=409)
    for index, step_id in enumerate(manifest.step_ids):
        receipt = read_native_artifact_reference(GeneralTaskArtifactRef(
            artifact_id=manifest.step_receipt_artifact_ids[index], digest=manifest.step_receipt_digests[index],
            schema_version=manifest.step_receipt_schemas[index]), parent_job_id=parent.run_identity,
            creation_digest=manifest.creation_digest)
        if (receipt.step_id != step_id or receipt.task_id != task.task_id or receipt.attempt_id != attempt.attempt_id
            or receipt.invocation_id not in manifest.admitted_invocation_ids
            or receipt.selected_grant_digest != manifest.selected_grant_digest):
            raise BoardError("general_task_manifest_step_changed", "Immutable native receipt binding changed", status_code=409)
    return envelope


async def verify_readonly_native_projection(db, owner, parent, task, attempt, manifest):
    """Verify retained local facts without granting execution or model egress."""
    from src.db.models import WorkBoardInputArtifact
    from src.work_board.input_artifacts import (_metadata_digest, _payload_path,
        _safe_file_bytes, _decode_and_validate_payload)
    if (task.owner_principal_id != owner.principal_id or task.owner_session_id != owner.session_id
        or parent.owner_principal_id != task.owner_principal_id or parent.session_id != task.owner_session_id
        or parent.operator_session_id != task.owner_session_id or parent.goal_id != task.goal_id
        or parent.goal_revision != task.goal_revision or attempt.task_id != task.task_id
        or attempt.workflow_run_id != parent.run_identity):
        raise BoardError("general_task_projection_owner_changed", "Exact canonical local Task scope required", status_code=403)
    artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
    if (artifact is None or artifact.owner_principal_id != task.owner_principal_id
        or artifact.owner_session_id != task.owner_session_id or artifact.goal_id != task.goal_id
        or artifact.goal_revision != task.goal_revision or artifact.capability_id != "agent.task.v1"
        or artifact.bound_task_id != task.task_id or artifact.payload_sha256 != task.typed_input_digest
        or artifact.typed_input_ref != task.typed_input_ref or artifact.metadata_digest != _metadata_digest(artifact)):
        raise BoardError("general_task_projection_source_changed", "Canonical immutable source binding required", status_code=409)
    content = _safe_file_bytes(_payload_path(artifact), expected_digest=artifact.payload_sha256,
        expected_size=artifact.size_bytes)
    envelope = GeneralTaskEnvelope.model_validate(_decode_and_validate_payload(artifact, content))
    if envelope.proposal_group is None:
        raise BoardError("general_task_provenance_missing", "Original retained source binding unavailable", status_code=409)
    return await _verify_native_manifest_data(parent, task, attempt, manifest, envelope)
