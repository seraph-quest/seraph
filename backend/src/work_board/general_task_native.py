"""Current interpreter binding on the existing native job/artifact owners."""
from sqlalchemy import select

from src.db.models import WorkBoardTask, WorkBoardAttempt
from src.work_board.contracts import (GeneralTaskCurrentManifestV1, GeneralTaskToolInputV1,
    GeneralTaskPlanRevisionV1, GeneralTaskArtifactRef, PlanSpec,
    GENERAL_TASK_NATIVE_CHILD_KIND, GENERAL_TASK_NATIVE_CHILD_CAPABILITY)
from src.work_board.general_task_runtime_artifacts import (initial_native_manifest,
    read_current_native_envelope, compile_native_child_binding, compile_phase_digest,
    stage_task_artifact, verify_general_task_manifest)
from src.work_board.repository import BoardError
from src.workflows.general_task_guard import read_manifest
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec


async def current_interpreter(jobs, parent_id, *, owner, fence):
    """Return only a verified current joint native binding."""
    async with jobs._session() as db:
        parent = await jobs._fetch(db, parent_id)
        jobs._assert_lease(parent, owner=owner, fencing_token=fence)
        attempt = await db.scalar(select(WorkBoardAttempt).where(
            WorkBoardAttempt.workflow_run_id == parent_id))
        task = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.task_id == attempt.task_id)) if attempt else None
        if task is None or attempt is None:
            raise BoardError("general_task_native_binding_changed", "Original canonical attempt required", status_code=409)
        envelope = await read_current_native_envelope(db, parent, task, attempt)
        manifest = read_manifest(parent)
        if manifest is not None:
            await verify_general_task_manifest(db, parent, task, attempt, manifest)
        for row in (parent, task, attempt):
            db.expunge(row)
    return parent, task, attempt, envelope, manifest


async def initialize_interpreter(jobs, parent_id, *, owner, fence):
    parent, task, attempt, envelope, manifest = await current_interpreter(jobs,
        parent_id, owner=owner, fence=fence)
    if manifest is None:
        manifest = initial_native_manifest(parent, task, attempt, envelope)
        result = await jobs.replace_general_task_manifest(parent_id, manifest=manifest,
            owner=owner, fencing_token=fence, expected_revision=parent.revision)
        manifest = GeneralTaskCurrentManifestV1.model_validate(result["manifest"])
    return manifest


async def admit_native_step(jobs, parent_id, *, owner, fence, step, descriptor, inputs):
    """Publish private literals and atomically enter one exact native wait."""
    from src.work_board.general_task import digest
    parent, task, attempt, envelope, previous = await current_interpreter(jobs,
        parent_id, owner=owner, fence=fence)
    if previous is None or previous.phase not in {"native_ready", "assembly"}:
        raise BoardError("general_task_native_phase_changed", "Current native assembly required", status_code=409)
    candidate = compile_native_child_binding(parent, task, attempt, previous, step, descriptor, inputs)
    proposed = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1,
        "phase_revision": previous.phase_revision + 1, "phase": "native_wait",
        "task_revision": task.task_revision + 1,
        "admitted_invocation_ids": [*previous.admitted_invocation_ids, candidate.invocation_id]})
    proposed = proposed.model_copy(update={"phase_digest": compile_phase_digest(proposed)})
    binding = compile_native_child_binding(parent, task, attempt, proposed, step, descriptor, inputs)
    staged = stage_task_artifact(parent_job_id=parent_id, creation_digest=previous.creation_digest,
        payload=GeneralTaskToolInputV1(parent_job_id=parent_id, creation_digest=previous.creation_digest,
            invocation_id=binding.invocation_id, tool_id=descriptor.tool_id,
            descriptor_digest=binding.descriptor_digest, input_digest=binding.input_digest, inputs=inputs))
    spec = DurableJobSpec(identity=DurableJobIdentity(binding.invocation_id, "user",
        task.owner_principal_id, GENERAL_TASK_NATIVE_CHILD_KIND, "1", "general-native-tool", binding.invocation_id),
        inputs={"step_id": step.step_id, "tool_id": descriptor.tool_id,
            "tool_input_digest": digest(inputs), "descriptor_digest": binding.descriptor_digest,
            "typed_input_ref": "general-task-input:" + staged.reference.artifact_id,
            "typed_input_digest": staged.reference.digest},
        session_id=task.owner_session_id, operator_session_id=task.owner_session_id,
        parent_job_id=parent_id, parent_fencing_token=parent.fencing_token,
        goal_id=task.goal_id, goal_revision=task.goal_revision, plan_revision=previous.plan_revision,
        declared_authority={"principal": task.owner_principal_id, "owner_kind": "user",
            "session_id": task.owner_session_id, "capability_id": GENERAL_TASK_NATIVE_CHILD_CAPABILITY,
            "general_task_child_binding": binding.model_dump(mode="json")},
        deadline_at=parent.deadline_at, max_attempts=1)
    result = await jobs.admit_general_task_tool_child(spec, manifest=proposed, owner=owner,
        fencing_token=fence, expected_revision=parent.revision, staged_input=staged)
    return binding, result


async def publish_positive_claim(jobs, binding, *, child_owner, child_fence):
    """The first receipt reflects an actual claim and precedes tool contact."""
    from src.work_board.contracts import GeneralTaskStepReceiptV1
    child = await jobs.get_job(binding.invocation_id)
    parent = await jobs.get_job(binding.parent_job_id)
    if child["status"] != "running" or child["attempt_count"] != 1:
        raise BoardError("general_task_native_claim_missing", "An actual original child claim is required", status_code=409)
    staged = stage_task_artifact(parent_job_id=binding.parent_job_id,
        creation_digest=binding.creation_digest, payload=GeneralTaskStepReceiptV1(
            step_id=binding.step_id, plan_revision=binding.plan_revision,
            invocation_id=binding.invocation_id, input_digest=binding.input_digest,
            contact_state="not_contacted", status="running", descriptor_digest=binding.descriptor_digest,
            selected_grant_digest=binding.selected_grant_digest, task_id=binding.task_id,
            attempt_id=binding.attempt_id, child_job_id=binding.invocation_id,
            child_attempt_count=child["attempt_count"], child_fence=child["lease"]["fencing_token"],
            parent_creation_digest=binding.creation_digest, phase_digest=binding.phase_digest))
    return await jobs.publish_general_task_step_receipt(binding.parent_job_id,
        staged_artifact=staged, child_id=binding.invocation_id, owner=child_owner,
        fencing_token=child_fence, expected_parent_revision=parent["revision"])


async def run_native_step(service, jobs, binding, *, child_owner, principal):
    """Execute through the existing registry after positive durable admission."""
    import asyncio
    import json
    from dataclasses import replace
    from datetime import datetime, timezone
    from src.work_board.general_task import write_step_artifact, validate_schema, digest
    from src.work_board.general_task_runtime_artifacts import read_current_native_tool_input
    from src.work_board.contracts import GeneralTaskStepReceiptV1
    from src.workflows.job_runtime import _digest
    await jobs.queue_job(binding.invocation_id)
    child = await jobs.claim_job(binding.invocation_id, owner=child_owner)
    fence = child["lease"]["fencing_token"]
    await publish_positive_claim(jobs, binding, child_owner=child_owner, child_fence=fence)
    async with jobs._session() as db:
        row = await jobs._fetch(db, binding.invocation_id)
        private = await read_current_native_tool_input(db, row)
        parent_row = await jobs._fetch(db, binding.parent_job_id)
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding.attempt_id))
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        envelope = await read_current_native_envelope(db, parent_row, task, attempt)
        plan = current_plan(read_manifest(parent_row), envelope)
        step = next(item for item in plan.steps if item.step_id == binding.step_id)
    descriptors = {descriptor.tool_id: descriptor for descriptor in service.registry.descriptors()}
    descriptor = descriptors.get(private.tool_id)
    if descriptor is None or digest(descriptor.model_dump(mode="json")) != binding.descriptor_digest:
        raise BoardError("general_task_tool_contract_changed", "Original registered descriptor required", status_code=409)
    deadline = min(binding.native_deadline_at, binding.original_deadline_at)
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise BoardError("general_task_deadline", "Original native cutoff expired", status_code=409)
    effect_id = "general:" + binding.step_id + ":" + str(fence)
    await jobs.record_effect(binding.invocation_id, effect_type="general_tool_call",
        effect_id=effect_id, status="intent", target_path="general-step:" + digest([binding.invocation_id, binding.step_id]),
        details={"tool_id": private.tool_id, "step_id": binding.step_id,
            "input_digest": binding.input_digest, "no_learning": True}, owner=child_owner, fencing_token=fence)
    output = await asyncio.wait_for(service.registry.invoke(descriptor, private.inputs,
        principal=replace(principal, job_id=binding.invocation_id), job_id=binding.invocation_id,
        fencing_token=fence), timeout=min(descriptor.deadline, remaining))
    validate_schema(descriptor.output_schema, output)
    validate_schema(step.output_contract, output)
    artifact, verified = await write_step_artifact(jobs, job_id=binding.invocation_id,
        owner=child_owner, fence=fence, plan_digest=binding.plan_digest, step_id=binding.step_id, output=output)
    await jobs.record_readback(binding.invocation_id, effect_type="general_tool_call", effect_id=effect_id,
        status="succeeded", target_path="general-step:" + digest([binding.invocation_id, binding.step_id]),
        content_sha256=artifact["content_sha256"], readback_id="general-step-readback:" + digest([binding.invocation_id, binding.step_id])[:32],
        verified_at=datetime.now(timezone.utc).isoformat(), details={"step_id": binding.step_id,
            "tool_id": private.tool_id, "verified": True, "output_exists": True,
            "file_path": artifact["file_path"], "no_learning": True}, owner=child_owner, fencing_token=fence)
    current = await jobs.get_job(binding.invocation_id)
    matching = [item for item in current["artifacts"] if item["file_path"] == artifact["file_path"]
        and item["content_sha256"] == artifact["content_sha256"]]
    if len(matching) != 1:
        raise BoardError("general_task_artifact_changed", "Canonical child output adoption required", status_code=409)
    reference = GeneralTaskArtifactRef(artifact_id=matching[0]["artifact_id"],
        digest=artifact["content_sha256"], schema_version="GeneralTaskOutput.v1")
    async with jobs._session() as db:
        canonical_child = await jobs._fetch(db, binding.invocation_id)
        effect_digest = _digest(json.loads(canonical_child.effect_receipts_json))
    staged = stage_task_artifact(parent_job_id=binding.parent_job_id, creation_digest=binding.creation_digest,
        payload=GeneralTaskStepReceiptV1(step_id=binding.step_id, plan_revision=binding.plan_revision,
            invocation_id=binding.invocation_id, input_digest=binding.input_digest, contact_state="settled", status="verified",
            descriptor_digest=binding.descriptor_digest, selected_grant_digest=binding.selected_grant_digest,
            task_id=binding.task_id, attempt_id=binding.attempt_id, child_job_id=binding.invocation_id,
            child_attempt_count=current["attempt_count"], child_fence=fence,
            parent_creation_digest=binding.creation_digest, phase_digest=binding.phase_digest,
            artifact_refs=[reference], effect_receipt_digest=effect_digest))
    parent = await jobs.get_job(binding.parent_job_id)
    await jobs.publish_general_task_step_receipt(binding.parent_job_id, staged_artifact=staged,
        child_id=binding.invocation_id, owner=child_owner, fencing_token=fence,
        expected_parent_revision=parent["revision"])
    await jobs.transition_job(binding.invocation_id, "succeeded", owner=child_owner, fencing_token=fence,
        result={"verified": True, "artifact_refs": [reference.model_dump(mode="json")], "no_learning": True},
        result_summary="Native tool output physically read back")
    return verified, artifact, reference


def current_plan(manifest, envelope):
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    if manifest.plan_revision == 1:
        return envelope.plan
    return read_native_artifact_reference(GeneralTaskArtifactRef(
        artifact_id=manifest.current_plan_artifact_id,
        digest=manifest.revision_artifact_digests[-1], schema_version="GeneralTaskPlanRevision.v1"),
        parent_job_id=manifest.run_id, creation_digest=manifest.creation_digest).plan


async def publish_plan_revision(service, jobs, parent_id, *, owner, fence, request):
    """Only unadmitted steps may change within the original selected grant."""
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import child_binding
    from src.work_board.general_task import digest
    parent, task, attempt, envelope, previous = await current_interpreter(jobs,
        parent_id, owner=owner, fence=fence)
    if (previous is None or previous.phase not in {"native_ready", "assembly"}
        or task.task_revision != request.expected_revision):
        raise BoardError("general_task_plan_revision_stale", "Current bounded assembly revision required", status_code=409)
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    for index in range(1, len(previous.revision_numbers)):
        retained = read_native_artifact_reference(GeneralTaskArtifactRef(
            artifact_id=previous.revision_artifact_ids[index],
            digest=previous.revision_artifact_digests[index],
            schema_version="GeneralTaskPlanRevision.v1"), parent_job_id=parent_id,
            creation_digest=previous.creation_digest)
        if retained.idempotency_key == request.idempotency_key:
            if retained.plan.steps != request.replacements or retained.reason != request.reason:
                raise BoardError("general_task_idempotency_conflict", "Revision key identifies different task data", status_code=409)
            return {"job": await jobs.get_job(parent_id), "manifest": previous.model_dump(mode="json")}
    if previous.plan_revision >= 16:
        raise BoardError("general_task_plan_revision_stale", "Current bounded assembly revision required", status_code=409)
    original = current_plan(previous, envelope)
    proposed_plan = PlanSpec(revision=original.revision + 1, steps=request.replacements)
    original_steps = {step.step_id: step for step in original.steps}
    revised_steps = {step.step_id: step for step in proposed_plan.steps}
    async with jobs._session() as db:
        siblings = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == parent_id))).scalars())
        if {row.run_identity for row in siblings} != set(previous.admitted_invocation_ids):
            raise BoardError("general_task_native_binding_changed", "Canonical admitted identities changed", status_code=409)
        for row in siblings:
            binding = child_binding(row)
            if original_steps.get(binding.step_id) != revised_steps.get(binding.step_id):
                raise BoardError("general_task_admitted_step_frozen", "Admitted tool inputs and contracts are immutable", status_code=409)
    service.recheck(envelope.model_copy(update={"plan": proposed_plan}))
    staged = stage_task_artifact(parent_job_id=parent_id, creation_digest=previous.creation_digest,
        payload=GeneralTaskPlanRevisionV1(parent_job_id=parent_id, creation_digest=previous.creation_digest,
            original_envelope_digest=previous.original_envelope_digest,
            selected_grant_digest=previous.selected_grant_digest,
            original_limits_digest=previous.original_limits_digest,
            original_deadline_at=previous.original_deadline_at, plan=proposed_plan,
            reason=request.reason, idempotency_key=request.idempotency_key))
    proposed = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1,
        "phase_revision": previous.phase_revision + 1, "plan_revision": proposed_plan.revision,
        "current_plan_artifact_id": staged.reference.artifact_id,
        "current_plan_digest": digest(proposed_plan.model_dump(mode="json")),
        "revision_numbers": [*previous.revision_numbers, proposed_plan.revision],
        "revision_artifact_ids": [*previous.revision_artifact_ids, staged.reference.artifact_id],
        "revision_artifact_digests": [*previous.revision_artifact_digests, staged.reference.digest],
        "revision_artifact_schemas": [*previous.revision_artifact_schemas, "GeneralTaskPlanRevision.v1"]})
    proposed = proposed.model_copy(update={"phase_digest": compile_phase_digest(proposed)})
    return await jobs.replace_general_task_manifest(parent_id, manifest=proposed,
        owner=owner, fencing_token=fence, expected_revision=parent.revision, staged_artifacts=(staged,))
