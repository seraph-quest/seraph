"""Private terminal native-source projection for the existing lesson owner.

This reader creates no authority, task, lease, memory, or artifact. Physical
readback runs before the lesson writer; its final check is metadata only.
"""
import json
from dataclasses import dataclass
from jsonschema.exceptions import ValidationError as SchemaValueError, SchemaError

from sqlalchemy import select

from src.db.models import WorkBoardInputArtifact, WorkflowRunState
from src.memory.procedure_recommendations import digest
from src.work_board.repository import BoardError


def _deny():
    raise BoardError("lesson_native_source_unverified", "Use the original completed native task and its verified output")


async def native_source_metadata(db, task, parent):
    from src.workflows.job_runtime import DurableJobError
    try:
        return await _native_source_metadata(db, task, parent)
    except (DurableJobError, ValueError, KeyError, TypeError) as error:
        raise BoardError("lesson_native_source_unverified", "Restore the original terminal native receipt metadata") from error


async def _native_source_metadata(db, task, parent):
    from src.workflows.general_task_guard import read_manifest, child_binding
    from src.work_board.input_artifacts import _metadata_digest
    if task.capability_id != "agent.task.v1" or parent.capability_version != "1" or parent.status != "succeeded":
        _deny()
    manifest = read_manifest(parent)
    if (manifest is None or not 1 <= len(manifest.admitted_invocation_ids) <= 16
        or len(set(manifest.admitted_invocation_ids)) != len(manifest.admitted_invocation_ids)
        or len(manifest.step_ids) != len(manifest.admitted_invocation_ids)
        or manifest.task_id != task.task_id or manifest.run_id != parent.run_identity
        or manifest.original_envelope_artifact_id != task.input_artifact_id
        or manifest.original_envelope_digest != task.typed_input_digest):
        _deny()
    children = list((await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.run_identity.in_(manifest.admitted_invocation_ids))
        .order_by(WorkflowRunState.run_identity).limit(17)
        .execution_options(populate_existing=True))).scalars())
    if {child.run_identity for child in children} != set(manifest.admitted_invocation_ids):
        _deny()
    artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id, populate_existing=True)
    if (artifact is None or artifact.bound_task_id != task.task_id
        or artifact.capability_id != "agent.task.v1" or artifact.payload_sha256 != task.typed_input_digest
        or artifact.typed_input_ref != task.typed_input_ref or artifact.metadata_digest != _metadata_digest(artifact)
        or (artifact.owner_principal_id, artifact.owner_session_id, artifact.goal_id, artifact.goal_revision)
        != (task.owner_principal_id, task.owner_session_id, task.goal_id, task.goal_revision)):
        _deny()
    step_ids = []
    for child in children:
        binding = child_binding(child)
        if (child.status != "succeeded" or child.finished_at is None
            or binding.parent_job_id != parent.run_identity or binding.task_id != task.task_id
            or binding.attempt_id != manifest.attempt_id or binding.invocation_id != child.run_identity
            or binding.original_root_id != task.owner_session_id
            or binding.owner_principal_id != task.owner_principal_id
            or binding.creation_digest != manifest.creation_digest
            or binding.selected_grant_digest != manifest.selected_grant_digest):
            _deny()
        step_ids.append(binding.step_id)
    if set(step_ids) != set(manifest.step_ids) or len(set(step_ids)) != len(step_ids):
        _deny()
    # Raw private arguments, outputs and audit details are never persisted in
    # the method. The digest commits to their original canonical metadata.
    snapshot = {"manifest": manifest.model_dump(mode="json"),
        "input_artifact": artifact.model_dump(mode="json"),
        "children": [child.model_dump(mode="json") for child in children]}
    audit = {"schema_version": "lesson_native_method_receipt.v1",
        "source_metadata_digest": digest(snapshot), "invocation_ids": list(manifest.admitted_invocation_ids),
        "manifest_digest": digest(manifest.model_dump(mode="json"))}
    return manifest, children, audit


async def project_completed_native_method(db, task, parent, family):
    from src.workflows.job_runtime import DurableJobError
    try:
        return await _project_completed_native_method(db, task, parent, family)
    except (DurableJobError, ValueError, KeyError, TypeError, OSError, SchemaValueError, SchemaError) as error:
        raise BoardError("lesson_native_source_unverified", "Restore the original native task and its physical output before requesting a method") from error


async def _project_completed_native_method(db, task, parent, family):
    from src.memory.task_lessons import TaskMethod, ToolStep, MethodOutput
    from src.work_board.contracts import WorkBoardOwner, GeneralTaskArtifactRef
    from src.work_board.general_task_runtime_artifacts import (verify_readonly_native_projection,
        read_native_artifact_reference, read_current_native_outputs,
        read_bound_native_tool_input, resolve_current_native_step_inputs)
    from src.work_board.general_task_native import current_plan
    from src.workflows.general_task_guard import assert_child_closed, child_binding
    from src.native_tools.registry import TOOL_METADATA
    if family != "general":
        _deny()
    manifest, children, audit = await native_source_metadata(db, task, parent)
    envelope = await verify_readonly_native_projection(db, WorkBoardOwner(
        principal_id=task.owner_principal_id, session_id=task.owner_session_id), parent, task,
        await _source_attempt(db, manifest.attempt_id), manifest)
    plan = current_plan(manifest, envelope)
    if [step.step_id for step in plan.steps] != manifest.step_ids:
        _deny()
    by_id = {child.run_identity: child for child in children}
    descriptors = {descriptor.tool_id: descriptor for descriptor in envelope.descriptors}
    for index, step in enumerate(plan.steps):
        receipt = read_native_artifact_reference(GeneralTaskArtifactRef(
            artifact_id=manifest.step_receipt_artifact_ids[index], digest=manifest.step_receipt_digests[index],
            schema_version="StepReceipt.v1"), parent_job_id=parent.run_identity, creation_digest=manifest.creation_digest)
        child = by_id.get(receipt.child_job_id)
        descriptor = descriptors.get(step.tool_id)
        if child is None or descriptor is None or step.tool_id not in TOOL_METADATA:
            _deny()
        binding = child_binding(child)
        # The verified manifest authenticates this complete finite revision
        # history. A continued task keeps each callback's original admitted
        # plan pin; its executed step must remain exactly frozen in the final
        # plan, rather than acquiring the latest revision's digest.
        if binding.plan_revision not in manifest.revision_numbers:
            _deny()
        revision_index = manifest.revision_numbers.index(binding.plan_revision)
        admitted_plan = envelope.plan if revision_index == 0 else read_native_artifact_reference(
            GeneralTaskArtifactRef(artifact_id=manifest.revision_artifact_ids[revision_index],
                digest=manifest.revision_artifact_digests[revision_index],
                schema_version=manifest.revision_artifact_schemas[revision_index]),
            parent_job_id=parent.run_identity, creation_digest=manifest.creation_digest).plan
        admitted_step = next((item for item in admitted_plan.steps if item.step_id == step.step_id), None)
        private = read_bound_native_tool_input(child, binding)
        resolved = await resolve_current_native_step_inputs(db, parent, task,
            await _source_attempt(db, manifest.attempt_id), manifest, envelope, step)
        if (receipt.status != "verified" or receipt.contact_state != "settled"
            or receipt.step_id != step.step_id or receipt.invocation_id != child.run_identity
            or receipt.child_attempt_count != child.attempt_count or receipt.child_fence != child.fencing_token
            or binding.step_id != step.step_id or binding.plan_digest != digest(admitted_plan.model_dump(mode="json"))
            or admitted_plan.revision != binding.plan_revision or admitted_step != step
            or private.tool_id != step.tool_id or private.inputs != resolved
            or json.loads(child.arguments_json).get("tool_id") != step.tool_id
            or binding.descriptor_digest != digest(descriptor.model_dump(mode="json"))):
            _deny()
        assert_child_closed(parent, child, receipt)
    outputs = await read_current_native_outputs(db, parent, task,
        await _source_attempt(db, manifest.attempt_id), manifest, envelope, [step.step_id for step in plan.steps])
    if set(outputs) != set(manifest.step_ids):
        _deny()
    # Detect metadata changes across the physical stage without relabelling
    # another occurrence or executing any current source work.
    _, _, current = await native_source_metadata(db, task, parent)
    if current != audit:
        _deny()
    sequence = [step.tool_id for step in plan.steps]
    method = TaskMethod(family="general", steps=[ToolStep(tool_id=tool) for tool in sequence],
        registered_tool_ids=list(dict.fromkeys(sequence)), input_parameters={},
        output_contract=MethodOutput(artifact_type="task_result", required_fields=["status", "source_refs"]))
    return method, audit


async def _source_attempt(db, attempt_id):
    from src.db.models import WorkBoardAttempt
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt_id))
    if attempt is None or attempt.ended_at is None:
        _deny()
    return attempt


_PROCEDURE_SOURCE_SEAL = object()


@dataclass(frozen=True)
class ProcedureSourceWitness:
    """Physical original-source stage; never request, grant or replay authority."""
    seal: object
    task_id: str
    task_revision: int
    parent_id: str
    plan: object
    offers: tuple[dict, ...]
    audit: dict


async def stage_completed_procedure_source(db, task, parent):
    """Read the complete original native journey outside canonical writers."""
    from src.workflows.job_runtime import DurableJobError
    try:
        return await _stage_completed_procedure_source(db, task, parent)
    except (DurableJobError, ValueError, KeyError, TypeError, OSError, SchemaValueError, SchemaError) as error:
        if isinstance(error, ValueError) and str(error) == "source_contract_review_required":
            raise BoardError("source_contract_review_required",
                "Complete a fresh task with original producer input pins before saving this method") from error
        raise BoardError("procedure_source_unverified",
            "Use the original complete native journey with safe classified inputs and verified readbacks") from error


async def _stage_completed_procedure_source(db, task, parent):
    from src.work_board.general_task import digest as native_digest, validate_schema
    from src.work_board.contracts import WorkBoardOwner, GeneralTaskArtifactRef
    from src.work_board.general_task_runtime_artifacts import (verify_readonly_native_projection,
        read_native_artifact_reference, read_current_native_outputs,
        read_bound_native_tool_input, resolve_current_native_step_inputs)
    from src.work_board.general_task_native import current_plan
    from src.workflows.general_task_guard import assert_child_closed, child_binding
    from src.workflows.procedure_contracts import (ProcedurePlanV3, procedure_tool_pin,
        procedure_permissions_digest, classify_procedure_inputs)
    if task.status.value != "done":
        raise BoardError("procedure_source_not_completed", "Complete and review the original task first")
    manifest, children, audit = await native_source_metadata(db, task, parent)
    attempt = await _source_attempt(db, manifest.attempt_id)
    envelope = await verify_readonly_native_projection(db, WorkBoardOwner(
        principal_id=task.owner_principal_id, session_id=task.owner_session_id), parent, task, attempt, manifest)
    plan = current_plan(manifest, envelope)
    if {step.step_id for step in plan.steps} != set(manifest.step_ids):
        _deny()
    descriptors = {descriptor.tool_id: descriptor for descriptor in envelope.descriptors}
    offers = classify_procedure_inputs(plan.steps, envelope.descriptors)
    by_id = {child.run_identity: child for child in children}
    for step in plan.steps:
        index = manifest.step_ids.index(step.step_id)
        receipt = read_native_artifact_reference(GeneralTaskArtifactRef(
            artifact_id=manifest.step_receipt_artifact_ids[index], digest=manifest.step_receipt_digests[index],
            schema_version="StepReceipt.v1"), parent_job_id=parent.run_identity, creation_digest=manifest.creation_digest)
        child, descriptor = by_id.get(receipt.child_job_id), descriptors.get(step.tool_id)
        if child is None or descriptor is None:
            _deny()
        binding = child_binding(child)
        if binding.plan_revision not in manifest.revision_numbers:
            _deny()
        revision_index = manifest.revision_numbers.index(binding.plan_revision)
        admitted_plan = envelope.plan if revision_index == 0 else read_native_artifact_reference(
            GeneralTaskArtifactRef(artifact_id=manifest.revision_artifact_ids[revision_index],
                digest=manifest.revision_artifact_digests[revision_index],
                schema_version=manifest.revision_artifact_schemas[revision_index]),
            parent_job_id=parent.run_identity, creation_digest=manifest.creation_digest).plan
        admitted_step = next((item for item in admitted_plan.steps if item.step_id == step.step_id), None)
        private = read_bound_native_tool_input(child, binding)
        resolved = await resolve_current_native_step_inputs(db, parent, task, attempt, manifest, envelope, step)
        validate_schema(descriptor.input_schema, resolved)
        if (receipt.status != "verified" or receipt.contact_state != "settled"
            or receipt.step_id != step.step_id or receipt.invocation_id != child.run_identity
            or receipt.child_attempt_count != child.attempt_count or receipt.child_fence != child.fencing_token
            or binding.step_id != step.step_id or binding.plan_digest != native_digest(admitted_plan.model_dump(mode="json"))
            or admitted_plan.revision != binding.plan_revision or admitted_step != step
            or private.tool_id != step.tool_id or private.inputs != resolved
            or json.loads(child.arguments_json).get("tool_id") != step.tool_id
            or binding.descriptor_digest != native_digest(descriptor.model_dump(mode="json"))):
            _deny()
        assert_child_closed(parent, child, receipt)
    outputs = await read_current_native_outputs(db, parent, task, attempt, manifest, envelope,
        [step.step_id for step in plan.steps])
    if set(outputs) != set(manifest.step_ids):
        _deny()
    _, _, current = await native_source_metadata(db, task, parent)
    if current != audit:
        _deny()
    pins = [procedure_tool_pin(descriptors[tool_id]) for tool_id in sorted({step.tool_id for step in plan.steps})]
    saved = ProcedurePlanV3(source_task_id=task.task_id, source_attempt=attempt.attempt_id,
        steps=[step.model_dump(mode="json") for step in plan.steps], parameters=[],
        tool_contract_versions=pins, output_contract=envelope.task_input.requested_output,
        permissions_digest=procedure_permissions_digest(pins))
    return ProcedureSourceWitness(_PROCEDURE_SOURCE_SEAL, task.task_id, task.task_revision,
        parent.run_identity, saved, tuple(offers), audit)


async def recheck_procedure_source(db, witness):
    """Canonical same-session DB-only source CAS; no files, registry or Vault."""
    from src.db.models import WorkBoardTask
    if not isinstance(witness, ProcedureSourceWitness) or witness.seal is not _PROCEDURE_SOURCE_SEAL:
        _deny()
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == witness.task_id)
        .execution_options(populate_existing=True))
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == witness.parent_id)
        .execution_options(populate_existing=True))
    if task is None or parent is None or task.task_revision != witness.task_revision:
        _deny()
    _, _, audit = await native_source_metadata(db, task, parent)
    if audit != witness.audit:
        _deny()
    return audit


def build_procedure_candidate(witness, selections):
    from src.workflows.procedure_contracts import (ProcedureCandidateV3, ProcedurePlanV3,
        ProcedureV3Parameter, ProcedureParameterSelection, _replace_pointer)
    if not isinstance(witness, ProcedureSourceWitness) or witness.seal is not _PROCEDURE_SOURCE_SEAL:
        _deny()
    selected = [item if isinstance(item, ProcedureParameterSelection) else
        ProcedureParameterSelection.model_validate(item) for item in selections]
    if len({item.offer_id for item in selected}) != len(selected) or len({item.name for item in selected}) != len(selected):
        raise ValueError("duplicate procedure selection")
    offered = {offer["offer_id"]: offer for offer in witness.offers}
    raw = witness.plan.model_dump(mode="json")
    by_id = {step["step_id"]: step for step in raw["steps"]}
    parameters = []
    for selection in selected:
        offer = offered[selection.offer_id]
        parameter = ProcedureV3Parameter(name=selection.name,
            **{key: value for key, value in offer.items() if key != "offer_id"})
        _replace_pointer(by_id[parameter.step_id]["input"], parameter.input_pointer,
            {"$parameter": parameter.name})
        parameters.append(parameter.model_dump(mode="json"))
    raw["parameters"] = parameters
    return ProcedureCandidateV3(plan=ProcedurePlanV3.model_validate(raw))
