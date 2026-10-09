"""Private terminal native-source projection for the existing lesson owner.

This reader creates no authority, task, lease, memory, or artifact. Physical
readback runs before the lesson writer; its final check is metadata only.
"""
import json

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
    except (DurableJobError, ValueError, KeyError, TypeError, OSError) as error:
        raise BoardError("lesson_native_source_unverified", "Restore the original native task and its physical output before requesting a method") from error


async def _project_completed_native_method(db, task, parent, family):
    from src.memory.task_lessons import TaskMethod, ToolStep, MethodOutput
    from src.work_board.contracts import WorkBoardOwner, GeneralTaskArtifactRef
    from src.work_board.general_task_runtime_artifacts import (verify_readonly_native_projection,
        read_native_artifact_reference, read_current_native_outputs)
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
        if (receipt.status != "verified" or receipt.contact_state != "settled"
            or receipt.step_id != step.step_id or receipt.invocation_id != child.run_identity
            or receipt.child_attempt_count != child.attempt_count or receipt.child_fence != child.fencing_token
            or binding.step_id != step.step_id or binding.plan_digest != manifest.current_plan_digest
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
