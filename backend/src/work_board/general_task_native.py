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


async def initialize_interpreter(jobs, parent_id, *, owner, fence, service=None):
    parent, task, attempt, envelope, manifest = await current_interpreter(jobs,
        parent_id, owner=owner, fence=fence)
    if manifest is None:
        manifest = initial_native_manifest(parent, task, attempt, envelope)
        result = await jobs.replace_general_task_manifest(parent_id, manifest=manifest,
            owner=owner, fencing_token=fence, expected_revision=parent.revision, service=service)
        manifest = GeneralTaskCurrentManifestV1.model_validate(result["manifest"])
    return manifest


async def admit_native_step(jobs, parent_id, *, owner, fence, step, descriptor, inputs, service=None):
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
    native_authority = {"principal": task.owner_principal_id, "owner_kind": "user",
        "session_id": task.owner_session_id, "capability_id": GENERAL_TASK_NATIVE_CHILD_CAPABILITY,
        "general_task_child_binding": binding.model_dump(mode="json")}
    from src.workflows.specialist_delegation import is_specialist_root
    if is_specialist_root(parent):
        import json
        original = json.loads(parent.declared_authority_json)
        for key in ("specialist_delegation_invocation_id", "specialist_original_parent_id"):
            native_authority[key] = original[key]
    spec = DurableJobSpec(identity=DurableJobIdentity(binding.invocation_id, "user",
        task.owner_principal_id, GENERAL_TASK_NATIVE_CHILD_KIND, "1", "general-native-tool", binding.invocation_id),
        inputs={"step_id": step.step_id, "tool_id": descriptor.tool_id,
            "tool_input_digest": digest(inputs), "descriptor_digest": binding.descriptor_digest,
            "typed_input_ref": "general-task-input:" + staged.reference.artifact_id,
            "typed_input_digest": staged.reference.digest},
        session_id=task.owner_session_id, operator_session_id=task.owner_session_id,
        parent_job_id=parent_id, parent_fencing_token=parent.fencing_token,
        goal_id=task.goal_id, goal_revision=task.goal_revision, plan_revision=previous.plan_revision,
        declared_authority=native_authority,
        deadline_at=parent.deadline_at, max_attempts=1)
    capacity_witness = None
    if service is not None:
        from src.work_board.general_task import GeneralTaskService
        if type(service) is not GeneralTaskService or not service.started:
            raise PermissionError("owned current task service required for capacity compilation")
        compiler = getattr(service.registry, "compile_capacity", None)
        if callable(compiler):
            capacity_witness = compiler(descriptor)
    result = await jobs.admit_general_task_tool_child(spec, manifest=proposed, owner=owner,
        fencing_token=fence, expected_revision=parent.revision, staged_input=staged,
        capacity_witness=capacity_witness, service=service)
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


async def run_native_step(service, jobs, binding, *, child_owner, principal, approved_resume=False):
    """Execute through the existing registry after positive durable admission."""
    import asyncio
    import json
    from dataclasses import replace
    from datetime import datetime, timezone
    from src.work_board.general_task import write_step_artifact, validate_schema, digest
    from src.work_board.general_task_runtime_artifacts import read_current_native_tool_input
    from src.work_board.contracts import GeneralTaskStepReceiptV1
    from src.workflows.job_runtime import _digest
    from src.workflows.general_task_guard import effective_child_phase
    if approved_resume:
        async with jobs._session() as db:
            row = await jobs._fetch(db, binding.invocation_id)
            effective = await effective_child_phase(db, row)
            if not effective.approval_binding_digest or row.status != "running" or row.lease_owner != child_owner:
                raise BoardError("general_task_resume_binding_changed", "Exact canonical approved child required", status_code=409)
            from src.workflows.job_runtime import _effect_ledger_or_raise, DurableJobLeaseError
            resumed_effect = "general:" + binding.step_id + ":" + str(row.fencing_token)
            if any(item.get("effect_type") == "general_tool_call" and item.get("effect_id") == resumed_effect
                for item in _effect_ledger_or_raise(row.effect_receipts_json)):
                raise DurableJobLeaseError("original approved native invocation requires closure reconciliation")
        child = await jobs.get_job(binding.invocation_id)
    else:
        from src.workflows.job_runtime import DurableJobLeaseError
        pending = await jobs.get_job(binding.invocation_id)
        if (pending["attempt_count"] != 0 or pending["lease"]["fencing_token"] != 0
            or pending["effects"] or pending["lease"]["owner"] or pending["lease"]["expires_at"]):
            raise DurableJobLeaseError("original unclaimed native child required; never replay")
        if pending["status"] == "accepted":
            await jobs.queue_job(binding.invocation_id)
        elif pending["status"] != "queued":
            raise DurableJobLeaseError("original accepted or queued native child required")
        child = await jobs.claim_job(binding.invocation_id, owner=child_owner)
    fence = child["lease"]["fencing_token"]
    if not approved_resume:
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
    from math import ceil
    await jobs.heartbeat_job(binding.invocation_id, owner=child_owner, fencing_token=fence,
        lease_seconds=max(1, ceil(min(descriptor.deadline, remaining)) + 5))
    effect_id = "general:" + binding.step_id + ":" + str(fence)
    await jobs.record_effect(binding.invocation_id, effect_type="general_tool_call",
        effect_id=effect_id, status="intent", target_path="general-step:" + digest([binding.invocation_id, binding.step_id]),
        details={"tool_id": private.tool_id, "step_id": binding.step_id,
            "input_digest": binding.input_digest, "no_learning": True}, owner=child_owner, fencing_token=fence)
    from src.native_tools.task_adapters import TaskToolApprovalRequired
    from src.workflows.general_task_guard import assert_general_task_child_current
    async with jobs._session() as db:
        authorized = await jobs._fetch(db, binding.invocation_id)
        jobs._assert_lease(authorized, owner=child_owner, fencing_token=fence)
        await assert_general_task_child_current(db, authorized)
        from src.workflows.general_task_guard import assert_native_callback_capacity
        parent = await jobs._fetch(db, binding.parent_job_id)
        compiler = getattr(service.registry, "compile_capacity", None)
        capacity_witness = compiler(descriptor) if callable(compiler) else None
        assert_native_callback_capacity(parent, binding, fence, capacity_witness=capacity_witness)
        from src.work_board.general_task_runtime_artifacts import capture_native_cancel_output_witness
        from src.workflows.job_runtime import _effect_ledger_or_raise
        intents = [item for item in _effect_ledger_or_raise(authorized.effect_receipts_json)
            if item.get("effect_id") == effect_id and item.get("effect_type") == "general_tool_call"
            and item.get("receipt_kind") == "effect" and item.get("status") == "intent"
            and item.get("fencing_token") == fence]
        if len(intents) != 1:
            raise BoardError("general_task_native_binding_changed", "Original invocation intent required", status_code=409)
        output_root_witness = capture_native_cancel_output_witness(binding,
            fencing_token=fence, intent=intents[0])
    invocation = service.registry.begin_invocation(descriptor, private.inputs,
        principal=replace(principal, job_id=binding.invocation_id), job_id=binding.invocation_id,
        fencing_token=fence)
    service.retain_native_invocation(jobs, binding, invocation, output_root_witness=output_root_witness)
    try:
        try:
            output = await invocation.wait(timeout=min(descriptor.deadline, remaining))
        except TaskToolApprovalRequired:
            metadata = service.registry.approval_context(descriptor, private.inputs, job_id=binding.invocation_id)
            parent = await jobs.get_job(binding.parent_job_id)
            waiting = await jobs.wait_general_task_native_approval(binding.invocation_id,
                owner=child_owner, fencing_token=fence, expected_parent_revision=parent["revision"],
                producer_witness=invocation.witness, tool_name=metadata["tool_name"],
                approval_context=metadata["approval_context"])
            service.release_native_invocation(binding.invocation_id)
            return {"awaiting_approval": True, "approval_id": waiting["transition"]["approval_id"],
                "child_id": binding.invocation_id}, None, None
        validate_schema(descriptor.output_schema, output)
        validate_schema(step.output_contract, output)
        document_authority = None
        if descriptor.tool_id == "document_prepare":
            from src.work_board.document_preparation import invocation as document_invocation
            async def document_authority(db, run):
                await document_invocation(db, replace(principal, job_id=binding.invocation_id),
                    binding.invocation_id, fence)
        artifact, verified = await write_step_artifact(jobs, job_id=binding.invocation_id,
            owner=child_owner, fence=fence, plan_digest=binding.plan_digest, step_id=binding.step_id,
            output=output, authority_check=document_authority)
        await jobs.record_readback(binding.invocation_id, effect_type="general_tool_call", effect_id=effect_id,
            status="succeeded", target_path="general-step:" + digest([binding.invocation_id, binding.step_id]),
            content_sha256=artifact["content_sha256"], readback_id="general-step-readback:" + digest([binding.invocation_id, binding.step_id])[:32],
            verified_at=datetime.now(timezone.utc).isoformat(), details={"step_id": binding.step_id,
                "tool_id": private.tool_id, "verified": True, "output_exists": True,
                "file_path": artifact["file_path"], "no_learning": True,
                "input_digest": binding.input_digest, "original_intent_digest": _digest(intents[0])}, owner=child_owner, fencing_token=fence,
            **({"readback_authority_check": document_authority} if document_authority is not None else {}))
        current = await jobs.get_job(binding.invocation_id)
        matching = [item for item in current["artifacts"] if item["file_path"] == artifact["file_path"]
            and item["content_sha256"] == artifact["content_sha256"]]
        if len(matching) != 1:
            raise BoardError("general_task_artifact_changed", "Canonical child output adoption required", status_code=409)
        reference = GeneralTaskArtifactRef(artifact_id=matching[0]["artifact_id"],
            digest=artifact["content_sha256"], schema_version="GeneralTaskOutput.v1")
        parent = await jobs.get_job(binding.parent_job_id)
        cleanup = await jobs.publish_general_task_tool_closure(binding.invocation_id,
            owner=child_owner, fencing_token=fence, expected_parent_revision=parent["revision"],
            producer_witness=invocation.witness)
        async with jobs._session() as db:
            canonical_child = await jobs._fetch(db, binding.invocation_id)
            effect_digest = _digest(json.loads(canonical_child.effect_receipts_json))
            effective = await effective_child_phase(db, canonical_child)
        staged = stage_task_artifact(parent_job_id=binding.parent_job_id, creation_digest=binding.creation_digest,
            payload=GeneralTaskStepReceiptV1(step_id=binding.step_id, plan_revision=binding.plan_revision,
                invocation_id=binding.invocation_id, input_digest=binding.input_digest, contact_state="settled", status="verified",
                descriptor_digest=binding.descriptor_digest, selected_grant_digest=binding.selected_grant_digest,
                task_id=binding.task_id, attempt_id=binding.attempt_id, child_job_id=binding.invocation_id,
                child_attempt_count=current["attempt_count"], child_fence=fence,
                parent_creation_digest=binding.creation_digest, phase_digest=effective.phase_digest,
                approval_binding_digest=effective.approval_binding_digest,
                artifact_refs=[reference], effect_receipt_digest=effect_digest,
                cleanup_receipt_digest=digest(cleanup["closure"])))
        parent = await jobs.get_job(binding.parent_job_id)
        await jobs.publish_general_task_step_receipt(binding.parent_job_id, staged_artifact=staged,
            child_id=binding.invocation_id, owner=child_owner, fencing_token=fence,
            expected_parent_revision=parent["revision"])
        await jobs.transition_job(binding.invocation_id, "succeeded", owner=child_owner, fencing_token=fence,
            result={"verified": True, "artifact_refs": [reference.model_dump(mode="json")], "no_learning": True},
            result_summary="Native tool output physically read back")
        service.release_native_invocation(binding.invocation_id)
        return verified, artifact, reference
    finally:
        # The original close notification may precede cancellation. Observe
        # once more after readback/publication exits, without executing again.
        try:
            await service.observe_native_cancellation(jobs, binding.parent_job_id)
        except Exception:
            # Keep the source producer for a later explicit reconciliation.
            pass



def current_plan(manifest, envelope):
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    if manifest.plan_revision == 1:
        return envelope.plan
    return read_native_artifact_reference(GeneralTaskArtifactRef(
        artifact_id=manifest.current_plan_artifact_id,
        digest=manifest.revision_artifact_digests[-1], schema_version="GeneralTaskPlanRevision.v1"),
        parent_job_id=manifest.run_id, creation_digest=manifest.creation_digest).plan


async def retain_native_failure(service, jobs, binding, *, child_owner):
    """Retain actual invocation uncertainty without granting closure or replay."""
    import json
    from src.work_board.contracts import GeneralTaskStepReceiptV1
    from src.work_board.general_task import digest
    from src.workflows.general_task_guard import effective_child_phase
    from src.workflows.job_runtime import _digest, DurableJobError
    child = await jobs.get_job(binding.invocation_id)
    fence = child["lease"]["fencing_token"]
    if child["status"] != "running" or child["lease"]["owner"] != child_owner:
        return
    invocation = service._native_invocations.get(binding.invocation_id)
    try:
        cleanup_digest = None
        if invocation is not None and invocation.closed:
            parent = await jobs.get_job(binding.parent_job_id)
            closure = await jobs.publish_general_task_tool_closure(binding.invocation_id,
                owner=child_owner, fencing_token=fence, expected_parent_revision=parent["revision"],
                producer_witness=invocation.witness)
            cleanup_digest = digest(closure["closure"])
        await jobs.record_effect(binding.invocation_id, effect_type="general_tool_call",
            effect_id="general:" + binding.step_id + ":" + str(fence), status="unknown",
            target_path="general-step:" + digest([binding.invocation_id, binding.step_id]),
            details={"step_id": binding.step_id, "input_digest": binding.input_digest,
                "no_learning": True, "reconciliation_required": True}, owner=child_owner, fencing_token=fence)
        async with jobs._session() as db:
            row = await jobs._fetch(db, binding.invocation_id)
            effective = await effective_child_phase(db, row)
            effect_digest = _digest(json.loads(row.effect_receipts_json))
        staged = stage_task_artifact(parent_job_id=binding.parent_job_id, creation_digest=binding.creation_digest,
            payload=GeneralTaskStepReceiptV1(step_id=binding.step_id, plan_revision=binding.plan_revision,
                invocation_id=binding.invocation_id, input_digest=binding.input_digest,
                contact_state="unknown", status="unknown", descriptor_digest=binding.descriptor_digest,
                selected_grant_digest=binding.selected_grant_digest, task_id=binding.task_id,
                attempt_id=binding.attempt_id, child_job_id=binding.invocation_id,
                child_attempt_count=1, child_fence=fence, parent_creation_digest=binding.creation_digest,
                phase_digest=effective.phase_digest, approval_binding_digest=effective.approval_binding_digest,
                effect_receipt_digest=effect_digest, cleanup_receipt_digest=cleanup_digest))
        parent = await jobs.get_job(binding.parent_job_id)
        await jobs.publish_general_task_step_receipt(binding.parent_job_id, staged_artifact=staged,
            child_id=binding.invocation_id, owner=child_owner, fencing_token=fence,
            expected_parent_revision=parent["revision"])
        await jobs.transition_job(binding.invocation_id, "unknown_external_effect", owner=child_owner,
            fencing_token=fence, reason="general_task_native_unknown")
        if cleanup_digest is not None:
            service.release_native_invocation(binding.invocation_id)
    except (BoardError, DurableJobError):
        # Canonical drift invalidates the writer. Keep its original intent and
        # phase inspectable; failure never supplies substitute authority.
        return


async def _execute_interpreter_child(service, jobs, binding, *, child_owner, principal, approved_resume=False):
    try:
        return await run_native_step(service, jobs, binding, child_owner=child_owner,
            principal=principal, approved_resume=approved_resume)
    except Exception:
        await retain_native_failure(service, jobs, binding, child_owner=child_owner)
        return {"verified": False, "unknown_effect": True, "reason": "general_task_native_unknown",
            "no_learning": True, "native_execution": True}, None, None


async def continue_native_wait(service, jobs, parent_id, *, principal):
    """Execute only the original zero-attempt child admitted before a crash."""
    if not service.started:
        raise BoardError("general_task_inactive", "Task service is inactive", status_code=503)
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import (_current, _assert_joint_manifest,
        child_binding, assert_general_task_child_phase_current)
    from src.workflows.job_runtime import DurableJobLeaseError
    async with jobs._session() as db:
        parent, task, attempt, manifest, _ = await _current(jobs, db, parent_id)
        _assert_joint_manifest(parent, task, attempt, manifest)
        rows = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == parent_id))).scalars().all())
        pending = [row for row in rows if row.status in {"accepted", "queued"}]
        if (parent.status != "paused" or manifest.phase != "native_wait"
            or {row.run_identity for row in rows} != set(manifest.admitted_invocation_ids)
            or len(pending) != 1 or not principal or not principal.authenticated or principal.revoked
            or principal.principal_id != task.owner_principal_id or principal.session_id != task.owner_session_id
            or principal.operator_session_id != task.owner_session_id):
            raise DurableJobLeaseError("exact original unclaimed native wait required")
        child = pending[0]
        if (child.attempt_count != 0 or child.fencing_token != 0 or child.lease_owner
            or child.lease_expires_at or child.effect_receipts_json != "[]"):
            raise DurableJobLeaseError("original native child has prior claim or contact; never replay")
        await assert_general_task_child_phase_current(db, child)
        binding = child_binding(child)
    return await _execute_interpreter_child(service, jobs, binding,
        child_owner="general-task-native:" + binding.invocation_id, principal=principal)


async def execute_interpreter(service, jobs, *, job_id, owner, fence, principal, resume_child=None):
    """Advance the original accepted Plan through serial, durable native children."""
    from datetime import datetime, timezone
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.general_task import digest, validate_schema, write_step_artifact
    from src.work_board.general_task_runtime_artifacts import (
        read_current_native_outputs, resolve_current_native_step_inputs,
    )
    async with jobs._session() as db:
        original_attempt = await db.scalar(select(WorkBoardAttempt).where(
            WorkBoardAttempt.workflow_run_id == job_id))
        original_task = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.task_id == original_attempt.task_id)) if original_attempt else None
        if (original_task is None or not principal or not principal.authenticated or principal.revoked
            or principal.principal_id != original_task.owner_principal_id
            or principal.session_id != original_task.owner_session_id
            or principal.operator_session_id != original_task.owner_session_id):
            raise BoardError("general_task_owner_changed", "Original authenticated task operator required", status_code=403)
    if resume_child is not None:
        binding = resume_child["binding"]
        if binding.parent_job_id != job_id:
            raise BoardError("general_task_resume_binding_changed", "Original native parent required", status_code=409)
        output, _artifact, _reference = await _execute_interpreter_child(service, jobs, binding,
            child_owner=resume_child["runtime_owner"], principal=principal, approved_resume=True)
        if _artifact is None:
            return {**output, "verified": False, "no_learning": True, "native_execution": True}
        async with jobs._session() as db:
            parent = await jobs._fetch(db, job_id)
            manifest = read_manifest(parent)
        resumed = await jobs.resume_general_task_native_parent(job_id, owner=owner,
            expected_revision=parent.revision, expected_manifest_revision=manifest.manifest_revision)
        owner, fence = resumed["job"]["lease"]["owner"], resumed["job"]["lease"]["fencing_token"]
    else:
        async with jobs._session() as db:
            parent = await jobs._fetch(db, job_id)
            existing = read_manifest(parent)
        if existing is not None and parent.status == "paused" and existing.phase == "native_wait":
            # A crash after successful child adoption may precede assembly.
            # The fixed writer requires each original callback/readback; an
            # admitted or uncertain child cannot be replayed by this branch.
            from src.db.models import WorkflowRunState
            async with jobs._session() as db:
                pending = await db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.parent_job_id == job_id, WorkflowRunState.status.in_(("accepted", "queued"))))
            if pending is not None:
                output, _artifact, _reference = await continue_native_wait(service, jobs, job_id, principal=principal)
                if _artifact is None:
                    return {**output, "verified": False, "no_learning": True, "native_execution": True}
                async with jobs._session() as db:
                    parent = await jobs._fetch(db, job_id)
                    existing = read_manifest(parent)
            resumed = await jobs.resume_general_task_native_parent(job_id, owner=owner,
                expected_revision=parent.revision, expected_manifest_revision=existing.manifest_revision)
            owner, fence = resumed["job"]["lease"]["owner"], resumed["job"]["lease"]["fencing_token"]
    await initialize_interpreter(jobs, job_id, owner=owner, fence=fence, service=service)
    while True:
        parent, task, attempt, envelope, manifest = await current_interpreter(jobs, job_id,
            owner=owner, fence=fence)
        async with jobs._session() as db:
            await service.recheck_authority(db, WorkBoardOwner(principal_id=principal.principal_id,
                session_id=principal.operator_session_id), envelope)
        plan = current_plan(manifest, envelope)
        async with jobs._session() as db:
            outputs = await read_current_native_outputs(db, parent, task, attempt, manifest,
                envelope, manifest.step_ids)
        remaining = [step for step in plan.steps if step.step_id not in outputs]
        if not remaining:
            break
        if await continue_verified_plan(service, jobs, parent, task, attempt, envelope, manifest,
            owner=owner, fence=fence):
            continue
        ready = next((step for step in remaining if set(step.depends_on) <= outputs.keys()), None)
        if ready is None:
            raise BoardError("general_task_dependency_unverified", "Ready verified dependencies required", status_code=409)
        descriptor = next(item for item in envelope.descriptors if item.tool_id == ready.tool_id)
        async with jobs._session() as db:
            inputs = await resolve_current_native_step_inputs(db, parent, task, attempt,
                manifest, envelope, ready)
        binding, _admitted = await admit_native_step(jobs, job_id, owner=owner, fence=fence,
            step=ready, descriptor=descriptor, inputs=inputs, service=service)
        output, _artifact, _reference = await _execute_interpreter_child(service, jobs, binding,
            child_owner="general-task-native:" + binding.invocation_id, principal=principal)
        if _artifact is None:
            return {**output, "verified": False, "no_learning": True, "native_execution": True}
        async with jobs._session() as db:
            parent = await jobs._fetch(db, job_id)
            manifest = read_manifest(parent)
        resumed = await jobs.resume_general_task_native_parent(job_id, owner=owner,
            expected_revision=parent.revision, expected_manifest_revision=manifest.manifest_revision)
        owner, fence = resumed["job"]["lease"]["owner"], resumed["job"]["lease"]["fencing_token"]
    # Assembly consumes original successful child readbacks; no tool replay.
    active_envelope = envelope.model_copy(update={"plan": plan})
    projection = await jobs.get_job(job_id)
    recovered, artifacts = service.recovered_outputs(projection, active_envelope)
    for step in plan.steps:
        if step.step_id in recovered:
            if recovered[step.step_id] != outputs[step.step_id]:
                raise BoardError("general_task_artifact_changed", "Original native output changed", status_code=409)
            continue
        document_authority = None
        if step.tool_id == "document_prepare":
            from src.work_board.document_preparation import invocation
            async def document_authority(db, run):
                from dataclasses import replace
                await invocation(db, replace(principal, job_id=job_id), job_id, fence)
        artifact, _verified = await write_step_artifact(jobs, job_id=job_id, owner=owner, fence=fence,
            plan_digest=digest(active_envelope.model_dump(mode="json")), step_id=step.step_id,
            output=outputs[step.step_id], authority_check=document_authority)
        await jobs.record_readback(job_id, effect_type="general_tool_call", status="succeeded",
            target_path=artifact["file_path"], content_sha256=artifact["content_sha256"],
            readback_id="general-native-assembly:" + digest([job_id, step.step_id])[:32],
            verified_at=datetime.now(timezone.utc).isoformat(),
            details={"step_id": step.step_id, "verified": True, "output_exists": True,
                "file_path": artifact["file_path"], "no_learning": True}, owner=owner, fencing_token=fence,
            **({"readback_authority_check": document_authority} if document_authority is not None else {}))
        await jobs.record_checkpoint(job_id, checkpoint_id="general:verified:" + step.step_id,
            state=artifact, checkpoint_payload=artifact, owner=owner, fencing_token=fence)
        artifacts[step.step_id] = artifact
    final = outputs[plan.steps[-1].step_id]
    validate_schema(envelope.task_input.requested_output, final)
    artifact = artifacts[plan.steps[-1].step_id]
    return {"verified": True, "native_execution": True, "output_digest": digest(final),
        "step_count": len(outputs), "learning": "no_learning", "no_learning": True,
        "content_sha256": artifact["content_sha256"],
        "readback_id": "general-readback:" + digest([job_id, artifact])[:32],
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "result_refs": [artifact], "artifact_refs": [artifact]}


async def continue_verified_plan(service, jobs, parent, task, attempt, envelope, manifest, *, owner, fence):
    """Use original planning grants once per immutable verified receipt set."""
    from src.work_board.general_task import digest
    from src.work_board.contracts import WorkBoardOwner, PlanRevisionRequest
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    limits = envelope.task_input.limits
    from src.workflows.specialist_delegation import is_specialist_root
    if is_specialist_root(parent):
        # The child's initial reserved plan is immutable. Ordinary continuation
        # would lose the narrower callback/request accounting subgroup.
        return False
    if (service.planner is None or not envelope.task_input.inference_egress_acknowledged
        or limits.max_inference_calls <= 0 or limits.max_cost_microusd <= 0 or not manifest.step_ids):
        return False
    key = "verified-continuation:" + digest([manifest.creation_digest,
        sorted(zip(manifest.step_ids, manifest.step_receipt_artifact_ids, manifest.step_receipt_digests))])
    for index in range(1, len(manifest.revision_numbers)):
        retained = read_native_artifact_reference(GeneralTaskArtifactRef(
            artifact_id=manifest.revision_artifact_ids[index], digest=manifest.revision_artifact_digests[index],
            schema_version="GeneralTaskPlanRevision.v1"), parent_job_id=parent.run_identity,
            creation_digest=manifest.creation_digest)
        if retained.idempotency_key == key:
            return False
    operator = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    async with jobs._session() as db:
        from src.db.models import InferenceCostReservation, WorkflowRunState
        from src.workflows.general_task_accounting import entry_for, validate_group_owner
        from src.workflows.inference_accounting import InferenceAccountingError
        from src.model_fabric.effective_policy import current_inference_policy
        from src.model_fabric.configuration import OPENROUTER_SETUP_V2_SCHEMA_VERSION
        from src.work_board.general_task_proposal import proposal_provenance
        group = envelope.proposal_group
        await validate_group_owner(db, group)
        rows = list((await db.execute(select(InferenceCostReservation).join(WorkflowRunState,
            WorkflowRunState.run_identity == InferenceCostReservation.job_id).where(
            WorkflowRunState.owner_principal_id == task.owner_principal_id,
            WorkflowRunState.session_id == task.owner_session_id))).scalars())
        members = []
        for row in rows:
            entry = entry_for(row)
            if entry and entry["group"]["group_id"] == group.group_id:
                if entry["group"] != group.model_dump(mode="json"):
                    raise InferenceAccountingError("general_task_group_conflict")
                members.append(row)
        provenance = envelope.proposal_provenance
        if provenance is not None:
            original = next((row for row in members if row.operation_id == provenance.initial_operation_id), None)
            if original is None or proposal_provenance(original.model_dump(mode="json"), group) != provenance:
                raise BoardError("general_task_provenance_missing", "Original proposal accounting binding unavailable", status_code=409)
        if any(row.state in {"unknown", "reserved", "contact_started"} or
            row.state == "settled" and row.actual_cost_microusd is None for row in members):
            raise InferenceAccountingError("general_task_group_unknown")
        spent = sum(row.actual_cost_microusd for row in members if row.state == "settled")
        configured, _ = current_inference_policy()
        setup = configured.openrouter_setup
        route = (setup.routes or {}).get("text") if setup.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION else setup
        if route is None or not getattr(route, "enabled", True):
            raise BoardError("general_task_planning_route_unavailable", "Reviewed text route is unavailable", status_code=409)
        bound = getattr(route, "request_cost_bound_microusd", None)
        if type(bound) is not int or bound <= 0:
            raise BoardError("general_task_planning_budget_insufficient", "Original governed request bound required", status_code=409)
        exhausted = len(members) >= group.max_inference_calls or spent + bound > group.max_cost_microusd
    if exhausted:
        trace_key = "general:continuation-budget:" + digest([manifest.creation_digest, manifest.group_id])
        trace = {"reason": "general_task_continuation_budget_exhausted",
            "group_id": envelope.proposal_group.group_id, "no_learning": True, "accepted_plan_unchanged": True}
        projection = await jobs.get_job(parent.run_identity)
        existing = [item for item in projection["checkpoints"] if item.get("checkpoint_id") == trace_key]
        completed = [item for item in existing if item.get("payload") == trace
            and item.get("state_digest") == digest("budget_exhausted") and item.get("safe") is True]
        if not completed:
            await jobs.record_checkpoint(parent.run_identity, checkpoint_id=trace_key,
                state="budget_exhausted", checkpoint_payload=trace, owner=owner, fencing_token=fence)
        return False
    async with jobs._session() as db:
        proposal = await service.planner.continue_plan(db, operator, parent=parent, task=task,
            attempt=attempt, manifest=manifest, envelope=envelope, request_key=key)
    if proposal.plan is None or proposal.error:
        raise BoardError("general_task_plan_invalid", "Bounded continuation requires a valid remaining plan", status_code=409)
    if proposal.group != envelope.proposal_group or proposal.provenance != envelope.proposal_provenance:
        raise BoardError("general_task_continuation_not_bound", "Original planning group required", status_code=409)
    await publish_plan_revision(service, jobs, parent.run_identity, owner=owner, fence=fence,
        request=PlanRevisionRequest(expected_revision=task.task_revision,
            replacements=proposal.plan.steps, reason="Bounded continuation after verified native result",
            idempotency_key=key))
    return True


async def compile_paused_plan_revision(service, db, parent, task, attempt, envelope, previous, request):
    """Compile private replacements under the caller's canonical transaction."""
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import child_binding
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.general_task import canonical
    parent_id = parent.run_identity
    if (previous is None or previous.phase not in {"native_ready", "assembly", "operator_paused"}
        or task.task_revision != request.expected_revision):
        raise BoardError("general_task_plan_revision_stale", "Current bounded assembly revision required", status_code=409)
    canonical(request.model_dump(mode="json"))
    await service.recheck_authority(db, WorkBoardOwner(principal_id=task.owner_principal_id,
        session_id=task.owner_session_id), envelope)
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
            return retained.plan, None
    if previous.plan_revision >= 16:
        raise BoardError("general_task_plan_revision_stale", "Current bounded assembly revision required", status_code=409)
    original = current_plan(previous, envelope)
    proposed_plan = PlanSpec(revision=original.revision + 1, steps=request.replacements)
    original_steps = {step.step_id: step for step in original.steps}
    revised_steps = {step.step_id: step for step in proposed_plan.steps}
    siblings = list((await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.parent_job_id == parent_id))).scalars())
    if {row.run_identity for row in siblings} != set(previous.admitted_invocation_ids):
        raise BoardError("general_task_native_binding_changed", "Canonical admitted identities changed", status_code=409)
    for row in siblings:
        binding = child_binding(row)
        if original_steps.get(binding.step_id) != revised_steps.get(binding.step_id):
            raise BoardError("general_task_admitted_step_frozen", "Admitted tool inputs and contracts are immutable", status_code=409)
    await service.recheck_authority(db, WorkBoardOwner(principal_id=task.owner_principal_id,
        session_id=task.owner_session_id), envelope.model_copy(update={"plan": proposed_plan}))
    staged = stage_task_artifact(parent_job_id=parent_id, creation_digest=previous.creation_digest,
        payload=GeneralTaskPlanRevisionV1(parent_job_id=parent_id, creation_digest=previous.creation_digest,
            original_envelope_digest=previous.original_envelope_digest,
            selected_grant_digest=previous.selected_grant_digest,
            original_limits_digest=previous.original_limits_digest,
            original_deadline_at=previous.original_deadline_at, plan=proposed_plan,
            reason=request.reason, idempotency_key=request.idempotency_key))
    return proposed_plan, staged


async def publish_plan_revision(service, jobs, parent_id, *, owner, fence, request):
    """Only unadmitted steps may change within the original selected grant."""
    from src.work_board.general_task import digest
    parent, task, attempt, envelope, previous = await current_interpreter(jobs,
        parent_id, owner=owner, fence=fence)
    if previous is None or previous.phase not in {"native_ready", "assembly"}:
        raise BoardError("general_task_plan_revision_stale", "Current bounded assembly revision required", status_code=409)
    async with jobs._session() as db:
        proposed_plan, staged = await compile_paused_plan_revision(service, db, parent, task,
            attempt, envelope, previous, request)
    if staged is None:
        return {"job": await jobs.get_job(parent_id), "manifest": previous.model_dump(mode="json")}
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
