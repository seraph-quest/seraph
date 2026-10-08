"""Fixed native tool-child authority; generic children retain their live fence."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, false, func, select, update
from sqlalchemy.orm import aliased

from src.db.models import OperatorSession, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import (
    GENERAL_TASK_MANIFEST_KEY, GENERAL_TASK_NATIVE_CHILD_KIND,
    GeneralTaskCurrentManifestV1, GeneralTaskNativeChildBindingV1,
)


def read_manifest(run):
    """Never interpret damaged protected state as a fresh empty execution."""
    from src.workflows.job_runtime import DurableJobTransitionError, _digest
    try:
        records = json.loads(run.checkpoint_receipts_json or "[]")
        if not isinstance(records, list):
            raise ValueError()
        matching = [item for item in records if isinstance(item, dict)
                    and item.get("checkpoint_id") == GENERAL_TASK_MANIFEST_KEY]
        if not matching:
            return None
        if len(matching) != 1:
            raise ValueError()
        receipt = matching[0]
        manifest = GeneralTaskCurrentManifestV1.model_validate(receipt["payload"])
        if (receipt.get("safe") is not True
            or receipt.get("state_digest") != _digest(manifest.model_dump(mode="json"))):
            raise ValueError()
        return manifest
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableJobTransitionError("general task protected manifest is malformed") from exc


def protected_checkpoint_ids(history):
    """Protect native identities already committed by the fixed writer."""
    from types import SimpleNamespace
    from src.workflows.job_runtime import DurableJobTransitionError
    manifest = read_manifest(SimpleNamespace(checkpoint_receipts_json=json.dumps(history)))
    if manifest is None:
        return set()
    protected = {GENERAL_TASK_MANIFEST_KEY, *manifest.required_checkpoint_ids}
    present = {item.get("checkpoint_id") for item in history if isinstance(item, dict)}
    if not protected.issubset(present):
        raise DurableJobTransitionError("general task required checkpoint proof is missing")
    if len(protected) > 50:
        raise DurableJobTransitionError("general task protected checkpoint capacity reached")
    return protected


def requires_native_writer(run):
    return run.job_kind == GENERAL_TASK_NATIVE_CHILD_KIND or (
        run.job_kind == "agent.task.v1" and read_manifest(run) is not None)


async def verify_native_writer(jobs, db, run):
    """Fixed canonical owner check under the existing journal's SQL writer."""
    if run.job_kind == GENERAL_TASK_NATIVE_CHILD_KIND:
        await assert_general_task_child_current(db, run)
    elif run.job_kind == "agent.task.v1" and read_manifest(run) is not None:
        parent, task, attempt, manifest, _envelope = await _current(jobs, db, run.run_identity)
        _assert_joint_manifest(parent, task, attempt, manifest)
        if parent.status != "running" or manifest.phase not in {"native_ready", "assembly"}:
            from src.workflows.job_runtime import DurableJobLeaseError
            raise DurableJobLeaseError("general task final publication requires its joint assembly lease")


def child_binding(run):
    from src.workflows.job_runtime import DurableJobLeaseError
    try:
        authority = json.loads(run.declared_authority_json)
        binding = GeneralTaskNativeChildBindingV1.model_validate(authority["general_task_child_binding"])
        if (run.job_kind != GENERAL_TASK_NATIVE_CHILD_KIND or run.capability_version != "1"
            or run.branch_depth != 1 or run.owner_kind != "user"
            or run.parent_job_id != binding.parent_job_id
            or run.parent_run_identity != binding.parent_job_id
            or run.parent_fencing_token != binding.creation_job_fence
            or run.run_identity != binding.invocation_id
            or run.owner_principal_id != binding.owner_principal_id
            or run.operator_session_id != binding.original_root_id
            or run.session_id != binding.original_root_id
            or run.goal_id != binding.goal_id or run.goal_revision != binding.goal_revision
            or run.plan_revision != binding.plan_revision or run.max_attempts != 1):
            raise ValueError()
        return binding
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableJobLeaseError("general task native child binding is unavailable") from exc


def append_general_task_root_gate(conditions, run, *, now):
    """Fence adopted native parent phases without changing legacy root jobs."""
    if run.job_kind != "agent.task.v1":
        return
    try:
        manifest = read_manifest(run)
    except ValueError:
        conditions.append(false())
        return
    if manifest is None:
        return
    task, attempt = aliased(WorkBoardTask), aliased(WorkBoardAttempt)
    root = select(OperatorSession.id).where(
        OperatorSession.id == manifest.original_root_id,
        OperatorSession.principal_id == manifest.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now).exists()
    conditions.extend([
        WorkflowRunState.deadline_at == manifest.native_deadline_at,
        WorkflowRunState.deadline_at > now,
        WorkflowRunState.fencing_token == manifest.job_fence,
        WorkflowRunState.input_digest == manifest.original_input_digest,
        select(task.task_id).join(attempt, attempt.task_id == task.task_id).where(
            task.task_id == manifest.task_id, task.task_revision == manifest.task_revision,
            task.owner_session_id == manifest.original_root_id,
            task.owner_principal_id == manifest.owner_principal_id,
            task.input_artifact_id == manifest.original_envelope_artifact_id,
            task.typed_input_digest == manifest.original_envelope_digest,
            attempt.attempt_id == manifest.attempt_id,
            attempt.workflow_run_id == manifest.run_id,
            attempt.fencing_token == manifest.board_fence,
            attempt.ended_at.is_(None), attempt.cancel_requested_at.is_(None), root).exists(),
    ])


def append_general_task_parent_gate(conditions, run, *, now):
    """The sole paused-parent exception is an exact declared native wait."""
    if run.job_kind != GENERAL_TASK_NATIVE_CHILD_KIND:
        return False
    try:
        binding = child_binding(run)
        from src.work_board.pipelines import root_binding
        from src.work_board.pipeline_contracts import digest
        if binding.live_root_digest != digest(root_binding()):
            raise ValueError()
    except (ValueError, RuntimeError):
        conditions.append(false())
        return True
    parent, task, attempt = aliased(WorkflowRunState), aliased(WorkBoardTask), aliased(WorkBoardAttempt)
    checkpoints = func.json_each(parent.checkpoint_receipts_json).table_valued("key", "value").alias()
    payload = lambda field: func.json_extract(checkpoints.c.value, "$.payload." + field)
    invocations = func.json_each(payload("admitted_invocation_ids")).table_valued("key", "value").alias()
    native_invocation = select(invocations.c.key).where(invocations.c.value == binding.invocation_id).exists()
    manifest = select(checkpoints.c.key).where(
        func.json_extract(checkpoints.c.value, "$.checkpoint_id") == GENERAL_TASK_MANIFEST_KEY,
        func.json_extract(checkpoints.c.value, "$.safe") == 1,
        payload("schema_version") == "general_task.current_manifest.v1",
        payload("task_id") == binding.task_id, payload("attempt_id") == binding.attempt_id,
        payload("run_id") == parent.run_identity,
        payload("original_root_id") == binding.original_root_id,
        payload("owner_principal_id") == binding.owner_principal_id,
        payload("original_envelope_digest") == binding.original_envelope_digest,
        payload("original_envelope_artifact_id") == task.input_artifact_id,
        payload("original_input_digest") == parent.input_digest,
        payload("native_deadline_at") == binding.model_dump(mode="json")["native_deadline_at"],
        payload("creation_digest") == binding.creation_digest,
        payload("job_fence") == parent.fencing_token,
        payload("board_fence") == attempt.fencing_token,
        payload("task_revision") == task.task_revision,
        payload("phase") == "native_wait", payload("phase_revision") == binding.phase_revision,
        payload("phase_digest") == binding.phase_digest,
        payload("plan_revision") == binding.plan_revision,
        payload("current_plan_digest") == binding.plan_digest,
        payload("selected_grant_digest") == binding.selected_grant_digest,
        native_invocation,
    ).exists()
    original_root = select(OperatorSession.id).where(
        OperatorSession.id == binding.original_root_id,
        OperatorSession.principal_id == binding.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now).exists()
    conditions.append(select(parent.id).join(task, task.task_id == binding.task_id)
        .join(attempt, and_(attempt.attempt_id == binding.attempt_id, attempt.task_id == task.task_id))
        .where(parent.run_identity == binding.parent_job_id,
            parent.job_kind == "agent.task.v1", parent.capability_version == "1",
            parent.branch_depth == 0, parent.parent_job_id.is_(None),
            parent.root_run_identity == run.root_run_identity,
            parent.owner_kind == "user", parent.owner_principal_id == binding.owner_principal_id,
            parent.authority_digest == binding.parent_authority_digest,
            parent.operator_session_id == binding.original_root_id, parent.session_id == binding.original_root_id,
            parent.goal_id == binding.goal_id, parent.goal_revision == binding.goal_revision,
            parent.deadline_at == binding.native_deadline_at,
            parent.deadline_at <= binding.original_deadline_at, parent.deadline_at > now,
            WorkflowRunState.deadline_at <= binding.native_deadline_at,
            parent.status == "paused", parent.failure_reason == "general_task_native_wait",
            parent.lease_owner.is_(None), parent.lease_expires_at.is_(None),
            task.capability_id == "agent.task.v1", task.owner_principal_id == binding.owner_principal_id,
            task.owner_session_id == binding.original_root_id,
            task.goal_id == binding.goal_id, task.goal_revision == binding.goal_revision,
            task.status == WorkBoardStatus.blocked, task.block_reason == "general_task_native_wait",
            attempt.workflow_run_id == parent.run_identity, attempt.ended_at.is_(None),
            attempt.cancel_requested_at.is_(None), attempt.lease_owner.is_(None),
            attempt.lease_expires_at.is_(None), original_root, manifest).exists())
    return True


async def assert_general_task_child_phase_current(db, run):
    """Strict canonical check before any native tool contact or adoption."""
    from src.workflows.job_runtime import DurableJobLeaseError, _append_goal_fence_condition, _utc_now
    binding = child_binding(run)
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id))
    if parent is None or read_manifest(parent) is None:
        raise DurableJobLeaseError("general task original manifest is unavailable")
    conditions = [WorkflowRunState.run_identity == run.run_identity]
    _append_goal_fence_condition(conditions, run)
    append_general_task_parent_gate(conditions, run, now=_utc_now())
    if await db.scalar(select(WorkflowRunState.id).where(*conditions)) is None:
        raise DurableJobLeaseError("general task original native phase is unavailable")


def _step_receipt(manifest, step_id):
    from src.work_board.contracts import GeneralTaskArtifactRef
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    from src.workflows.job_runtime import DurableJobLeaseError
    try:
        index = manifest.step_ids.index(step_id)
    except ValueError as exc:
        raise DurableJobLeaseError("positive original native claim receipt is required before contact") from exc
    return read_native_artifact_reference(GeneralTaskArtifactRef(
        artifact_id=manifest.step_receipt_artifact_ids[index], digest=manifest.step_receipt_digests[index],
        schema_version="StepReceipt.v1"), parent_job_id=manifest.run_id, creation_digest=manifest.creation_digest)


async def assert_general_task_child_current(db, run):
    """Contact requires the durable original positive claim, never admission0."""
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now
    await assert_general_task_child_phase_current(db, run)
    binding = child_binding(run)
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id))
    receipt = _step_receipt(read_manifest(parent), binding.step_id)
    if (run.status != "running" or not run.lease_owner or _as_utc(run.lease_expires_at) is None
        or _as_utc(run.lease_expires_at) <= _utc_now() or _as_utc(run.deadline_at) <= _utc_now()
        or run.attempt_count != 1 or run.fencing_token <= 0 or receipt.child_attempt_count != run.attempt_count
        or receipt.child_fence != run.fencing_token or receipt.child_job_id != run.run_identity
        or receipt.invocation_id != binding.invocation_id or receipt.input_digest != binding.input_digest
        or receipt.descriptor_digest != binding.descriptor_digest or receipt.phase_digest != binding.phase_digest
        or receipt.status != "running" or receipt.contact_state not in {"not_contacted", "contact_started"}):
        raise DurableJobLeaseError("general task original positive child claim changed")


async def assert_general_task_child_terminal_current(jobs, db, run):
    """Adoption consumes verified readback; it never grants renewed contact."""
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now, _digest, _verified_readback_exists, _job_has_unsafe_effects
    from src.work_board.general_task import digest, validate_schema
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.work_board.general_task_native import current_plan
    from src.artifacts.registry import artifact_id_for
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    await assert_general_task_child_phase_current(db, run)
    binding = child_binding(run)
    _parent, _task, _attempt, manifest, envelope = await _current(jobs, db, binding.parent_job_id)
    receipt = _step_receipt(manifest, binding.step_id)
    effects = json.loads(run.effect_receipts_json or "[]")
    if (run.status != "running" or not run.lease_owner
        or _as_utc(run.lease_expires_at) is None or _as_utc(run.lease_expires_at) <= _utc_now()
        or run.attempt_count != 1 or run.fencing_token <= 0
        or receipt.child_attempt_count != run.attempt_count or receipt.child_fence != run.fencing_token
        or receipt.child_job_id != run.run_identity or receipt.invocation_id != binding.invocation_id
        or receipt.input_digest != binding.input_digest or receipt.descriptor_digest != binding.descriptor_digest
        or receipt.selected_grant_digest != binding.selected_grant_digest
        or receipt.parent_creation_digest != binding.creation_digest or receipt.phase_digest != binding.phase_digest
        or receipt.status != "verified" or receipt.contact_state != "settled"
        or receipt.effect_receipt_digest != _digest(effects)
        or not _verified_readback_exists(effects) or _job_has_unsafe_effects(effects)):
        raise DurableJobLeaseError("native terminal adoption requires original verified settled readback")
    refs = [ref for ref in receipt.artifact_refs if ref.schema_version == "GeneralTaskOutput.v1"]
    if len(refs) != 1 or len(receipt.artifact_refs) != 1:
        raise DurableJobLeaseError("native terminal adoption requires one exact output artifact")
    ref = refs[0]
    path = f"artifacts/work-board/general-tasks/{digest([run.run_identity, binding.plan_digest, binding.step_id])}-{ref.digest}.json"
    records = [item for item in json.loads(run.artifact_receipts_json or "[]")
        if item.get("artifact_id") == ref.artifact_id and item.get("content_sha256") == ref.digest]
    if (len(records) != 1 or records[0].get("file_path") != path
        or type(records[0].get("size_bytes")) is not int or not 0 < records[0]["size_bytes"] <= 65536
        or ref.artifact_id != artifact_id_for(file_path=path, artifact_type="general_task_step",
            producer=run.job_kind, run_id=run.run_identity, content_sha256=ref.digest)
        or not any(item.get("effect_type") == "general_tool_call" and item.get("status") == "succeeded"
            and item.get("content_sha256") == ref.digest and item.get("details", {}).get("verified") is True
            and item.get("details", {}).get("step_id") == binding.step_id for item in effects)):
        raise DurableJobLeaseError("native terminal output identity or readback changed")
    body = json.loads(_safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / path,
        expected_digest=ref.digest, expected_size=records[0]["size_bytes"]))
    step = next((item for item in current_plan(manifest, envelope).steps if item.step_id == binding.step_id), None)
    tool_id = json.loads(run.arguments_json)["tool_id"]
    descriptor = next((item for item in envelope.descriptors if item.tool_id == tool_id), None)
    if (set(body) != {"step_id", "output"} or body["step_id"] != binding.step_id or step is None
        or descriptor is None or digest(descriptor.model_dump(mode="json")) != binding.descriptor_digest):
        raise DurableJobLeaseError("native terminal descriptor or output contract changed")
    validate_schema(descriptor.output_schema, body["output"])
    validate_schema(step.output_contract, body["output"])


async def _current(jobs, db, parent_id, *, manifest=None):
    from src.workflows.job_runtime import (
        DurableJobLeaseError, _as_utc, _assert_canonical_goal_fence, _utc_now,
    )
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    now = _utc_now()
    parent = await jobs._fetch(db, parent_id)
    selected = manifest or read_manifest(parent)
    if selected is None:
        raise DurableJobLeaseError("general task original manifest is required")
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == selected.task_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == selected.attempt_id))
    if (parent.job_kind != "agent.task.v1" or parent.capability_version != "1"
        or parent.owner_kind != "user" or parent.branch_depth != 0 or parent.parent_job_id
        or task is None or attempt is None or attempt.ended_at or attempt.cancel_requested_at
        or attempt.task_id != task.task_id or attempt.workflow_run_id != parent_id
        or parent.session_id != parent.operator_session_id
        or parent.owner_principal_id != task.owner_principal_id
        or parent.session_id != task.owner_session_id
        or _as_utc(parent.deadline_at) != selected.native_deadline_at
        or selected.native_deadline_at > selected.original_deadline_at
        or _as_utc(parent.deadline_at) <= now):
        raise DurableJobLeaseError("general task original parent binding is unavailable")
    active_root = await db.scalar(select(OperatorSession.id).where(
        OperatorSession.id == parent.operator_session_id,
        OperatorSession.principal_id == parent.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now))
    if active_root is None:
        raise DurableJobLeaseError("general task original Root is inactive")
    await _assert_canonical_goal_fence(db, goal_id=parent.goal_id, goal_revision=parent.goal_revision,
        owner_kind=parent.owner_kind, owner_principal_id=parent.owner_principal_id,
        session_id=parent.session_id, authority=parent.declared_authority_json)
    envelope = await verify_general_task_manifest(db, parent, task, attempt, selected)
    return parent, task, attempt, selected, envelope


def _history(run):
    from src.workflows.job_runtime import DurableJobTransitionError
    try:
        history = json.loads(run.checkpoint_receipts_json or "[]")
        if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
            raise ValueError()
        return history
    except (TypeError, ValueError) as exc:
        raise DurableJobTransitionError("general task checkpoint history is malformed") from exc


def _next_manifest(parent, previous, proposed, *, task, attempt):
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(proposed) is not GeneralTaskCurrentManifestV1:
        raise DurableJobLeaseError("closed native manifest required")
    # Internal model_copy does not run Pydantic bounds or closed validators.
    # Revalidate before any writer can publish a successor.
    GeneralTaskCurrentManifestV1.model_validate(proposed.model_dump(mode="json"))
    if proposed.task_revision != task.task_revision or proposed.board_fence != attempt.fencing_token or proposed.job_fence != parent.fencing_token:
        raise DurableJobLeaseError("general task manifest counters changed")
    if previous is None:
        if (proposed.manifest_revision != 1 or proposed.plan_revision != 1
            or proposed.phase_revision != 1 or proposed.phase != "native_ready"
            or proposed.admitted_invocation_ids or proposed.step_ids):
            raise DurableJobLeaseError("general task initial manifest is not a fresh native binding")
        return
    immutable = ("task_id", "original_root_id", "owner_principal_id", "attempt_id", "run_id",
        "original_envelope_artifact_id", "original_envelope_digest", "original_input_digest",
        "selected_grant_digest", "group_id", "group_digest", "original_limits_digest",
        "creation_digest", "original_deadline_at", "native_deadline_at")
    if (proposed.manifest_revision != previous.manifest_revision + 1
        or any(getattr(proposed, name) != getattr(previous, name) for name in immutable)
        or proposed.plan_revision not in {previous.plan_revision, previous.plan_revision + 1}
        or proposed.admitted_invocation_ids[:len(previous.admitted_invocation_ids)] != previous.admitted_invocation_ids):
        raise DurableJobLeaseError("general task immutable manifest binding changed")
    for field in ("revision_numbers", "revision_artifact_ids", "revision_artifact_digests", "revision_artifact_schemas"):
        if getattr(proposed, field)[:len(getattr(previous, field))] != getattr(previous, field):
            raise DurableJobLeaseError("general task immutable revision history changed")
    if not set(previous.step_ids).issubset(proposed.step_ids):
        raise DurableJobLeaseError("general task admitted step evidence cannot disappear")
    phase_fields = ("phase", "plan_revision", "current_plan_digest", "admitted_invocation_ids", "job_fence", "board_fence")
    phase_changed = any(getattr(previous, name) != getattr(proposed, name) for name in phase_fields)
    if proposed.phase_revision != previous.phase_revision + int(phase_changed):
        raise DurableJobLeaseError("general task native phase revision changed")


def _publish(parent, manifest, *, staged_records=()):
    from src.workflows.job_runtime import _canonical, _digest, _github_recovery_history, _utc_now
    from src.work_board.general_task_runtime_artifacts import verify_staged_task_artifact
    # The fixed writer derives protection; no proposed list can grant retention
    # or silently drop an already committed original proof.
    history = _history(parent)
    required = sorted({item["checkpoint_id"] for item in history
        if isinstance(item.get("checkpoint_id"), str)
        and item["checkpoint_id"].startswith("general:")})
    manifest = GeneralTaskCurrentManifestV1.model_validate(
        manifest.model_dump(mode="json") | {"required_checkpoint_ids": required})
    artifacts = json.loads(parent.artifact_receipts_json or "[]")
    for staged in staged_records:
        _payload, record = verify_staged_task_artifact(staged,
            parent_job_id=parent.run_identity, creation_digest=manifest.creation_digest)
        artifacts = [item for item in artifacts if item.get("artifact_id") != record["artifact_id"]]
        artifacts.append({**record, "recorded_at": _utc_now().isoformat()})
    payload = manifest.model_dump(mode="json")
    receipt = {"checkpoint_id": GENERAL_TASK_MANIFEST_KEY, "state_digest": _digest(payload),
        "state_keys": sorted(payload), "safe": True, "payload": payload,
        "fencing_token": manifest.job_fence, "recorded_at": _utc_now().isoformat()}
    history = [item for item in history if item.get("checkpoint_id") != GENERAL_TASK_MANIFEST_KEY] + [receipt]
    parent.checkpoint_receipts_json = _canonical(_github_recovery_history(parent, history, kind="checkpoint"))
    parent.artifact_receipts_json = _canonical(_github_recovery_history(parent, artifacts, kind="artifact"))
    return manifest


def _validate_staged_refs(previous, manifest, staged):
    from src.work_board.general_task_runtime_artifacts import verify_staged_task_artifact
    from src.workflows.job_runtime import DurableJobLeaseError
    def refs(value):
        if value is None:
            return set()
        return set(zip(value.revision_artifact_ids[1:], value.revision_artifact_digests[1:], value.revision_artifact_schemas[1:])) | set(zip(
            value.step_receipt_artifact_ids, value.step_receipt_digests, value.step_receipt_schemas))
    added = refs(manifest) - refs(previous)
    supplied = set()
    for item in staged:
        verify_staged_task_artifact(item, parent_job_id=manifest.run_id, creation_digest=manifest.creation_digest)
        supplied.add((item.reference.artifact_id, item.reference.digest, item.reference.schema_version))
    if added != supplied:
        raise DurableJobLeaseError("new native references require exact sealed staged artifacts")


def _published_values(parent, manifest, staged):
    from types import SimpleNamespace
    staged_parent = SimpleNamespace(run_identity=parent.run_identity, job_kind=parent.job_kind,
        checkpoint_receipts_json=parent.checkpoint_receipts_json,
        artifact_receipts_json=parent.artifact_receipts_json)
    manifest = _publish(staged_parent, manifest, staged_records=staged)
    return manifest, {"checkpoint_receipts_json": staged_parent.checkpoint_receipts_json,
        "artifact_receipts_json": staged_parent.artifact_receipts_json}


async def _cas_parent(db, parent, values):
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now
    values = {**values, "revision": parent.revision + 1, "updated_at": _utc_now()}
    changed = await db.execute(update(WorkflowRunState).where(
        WorkflowRunState.run_identity == parent.run_identity,
        WorkflowRunState.revision == parent.revision,
        WorkflowRunState.status == parent.status,
        WorkflowRunState.fencing_token == parent.fencing_token,
        WorkflowRunState.lease_owner == parent.lease_owner,
        WorkflowRunState.lease_expires_at == parent.lease_expires_at,
    ).values(**values).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("general task original parent CAS changed")


def _assert_joint_manifest(parent, task, attempt, manifest):
    from src.workflows.job_runtime import DurableJobLeaseError
    if (manifest.task_revision != task.task_revision or manifest.board_fence != attempt.fencing_token
        or manifest.job_fence != parent.fencing_token):
        raise DurableJobLeaseError("general task current joint phase counters changed")


async def _cas_board(db, task, attempt, *, status, reason, owner, expiry, advance_fence):
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now
    now = _utc_now()
    changed = await db.execute(update(WorkBoardTask).where(
        WorkBoardTask.task_id == task.task_id, WorkBoardTask.task_revision == task.task_revision,
        WorkBoardTask.status == task.status, WorkBoardTask.owner_principal_id == task.owner_principal_id,
        WorkBoardTask.owner_session_id == task.owner_session_id,
    ).values(status=status, block_kind="needs_input" if reason else None,
        block_reason=reason, block_source_status=WorkBoardStatus.running.value if reason else None,
        task_revision=task.task_revision + 1, updated_at=now).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("general task original Board CAS changed")
    changed = await db.execute(update(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == attempt.attempt_id, WorkBoardAttempt.task_id == task.task_id,
        WorkBoardAttempt.workflow_run_id == attempt.workflow_run_id,
        WorkBoardAttempt.fencing_token == attempt.fencing_token,
        WorkBoardAttempt.ended_at.is_(None), WorkBoardAttempt.cancel_requested_at.is_(None),
        WorkBoardAttempt.lease_owner == attempt.lease_owner,
        WorkBoardAttempt.lease_expires_at == attempt.lease_expires_at,
    ).values(lease_owner=owner, lease_expires_at=expiry,
        fencing_token=attempt.fencing_token + int(advance_fence), outcome=reason,
        updated_at=now, heartbeat_at=now).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("general task original attempt CAS changed")


async def replace_manifest(jobs, job_id, *, manifest, owner, fencing_token, expected_revision, staged_artifacts=()):
    from src.work_board.repository import _begin_sqlite_immediate
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.workflows.job_runtime import DurableJobLeaseError, _serialize, _utc_now, _as_utc
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent, task, attempt, _selected, _envelope = await _current(jobs, db, job_id, manifest=manifest)
        previous = read_manifest(parent)
        jobs._assert_lease(parent, owner=owner, fencing_token=fencing_token)
        if (parent.status != "running" or parent.revision != expected_revision
            or task.status != WorkBoardStatus.running
            or attempt.lease_owner is None or _as_utc(attempt.lease_expires_at) is None
            or _as_utc(attempt.lease_expires_at) <= _utc_now()
            or owner != attempt.lease_owner + ":" + attempt.attempt_id):
            raise DurableJobLeaseError("general task manifest requires the current joint running lease")
        _next_manifest(parent, previous, manifest, task=task, attempt=attempt)
        if manifest.phase not in {"native_ready", "assembly"}:
            raise DurableJobLeaseError("general task manifest transition requires its paired native owner")
        await verify_general_task_manifest(db, parent, task, attempt, manifest)
        if previous is not None:
            _assert_joint_manifest(parent, task, attempt, previous)
            if any(getattr(manifest, name) != getattr(previous, name) for name in (
                "admitted_invocation_ids", "step_ids", "step_receipt_artifact_ids",
                "step_receipt_digests", "step_receipt_schemas")):
                raise DurableJobLeaseError("native admission and receipts require their fixed paired writers")
        _validate_staged_refs(previous, manifest, staged_artifacts)
        published, values = _published_values(parent, manifest, staged_artifacts)
        await _cas_parent(db, parent, values)
        return {"job": _serialize(await jobs._fetch(db, job_id)), "manifest": published.model_dump(mode="json")}


_ADMISSION_SEAL = object()


@dataclass(frozen=True)
class _ChildAdmission:
    jobs: object
    manifest: GeneralTaskCurrentManifestV1
    owner: str
    fencing_token: int
    expected_revision: int
    staged_input: object
    seal: object

    async def __call__(self, db, child):
        from src.work_board.general_task_runtime_artifacts import (
            verify_general_task_manifest, read_native_artifact_reference, verify_staged_task_artifact,
            resolve_current_native_step_inputs,
        )
        from src.work_board.contracts import GeneralTaskArtifactRef
        from src.work_board.general_task import digest
        from src.workflows.job_runtime import DurableJobLeaseError, _as_utc
        if self.seal is not _ADMISSION_SEAL:
            raise DurableJobLeaseError("fixed native child admission proof required")
        binding = child_binding(child)
        parent, task, attempt, previous, envelope = await _current(self.jobs, db, binding.parent_job_id)
        self.jobs._assert_lease(parent, owner=self.owner, fencing_token=self.fencing_token)
        _assert_joint_manifest(parent, task, attempt, previous)
        if (parent.revision != self.expected_revision or parent.status != "running"
            or task.status != WorkBoardStatus.running or previous.phase not in {"native_ready", "assembly"}
            or attempt.lease_owner is None or attempt.lease_expires_at is None
            or _as_utc(attempt.lease_expires_at) <= _as_utc(child.started_at)
            or binding.invocation_id in previous.admitted_invocation_ids
            or self.manifest.phase != "native_wait"
            or self.manifest.admitted_invocation_ids != [*previous.admitted_invocation_ids, binding.invocation_id]
            or binding.creation_digest != previous.creation_digest
            or binding.parent_authority_digest != parent.authority_digest
            or binding.original_envelope_digest != previous.original_envelope_digest
            or binding.selected_grant_digest != previous.selected_grant_digest
            or binding.creation_job_fence != parent.fencing_token
            or binding.creation_board_fence != attempt.fencing_token
            or binding.plan_revision != previous.plan_revision or binding.plan_digest != previous.current_plan_digest
            or binding.phase_revision != self.manifest.phase_revision or binding.phase_digest != self.manifest.phase_digest
            or _as_utc(child.deadline_at) > previous.native_deadline_at
            or _as_utc(child.deadline_at) > _as_utc(parent.deadline_at)
            or _as_utc(child.deadline_at) <= _as_utc(child.started_at)):
            raise DurableJobLeaseError("general task native child admission binding changed")
        plan = envelope.plan
        if previous.plan_revision > 1:
            plan = read_native_artifact_reference(GeneralTaskArtifactRef(
                artifact_id=previous.current_plan_artifact_id,
                digest=previous.revision_artifact_digests[-1], schema_version="GeneralTaskPlanRevision.v1"),
                parent_job_id=parent.run_identity, creation_digest=previous.creation_digest).plan
        step = next((item for item in plan.steps if item.step_id == binding.step_id), None)
        descriptors = [item for item in envelope.descriptors if step is not None and item.tool_id == step.tool_id]
        if len(descriptors) != 1 or digest(descriptors[0].model_dump(mode="json")) != binding.descriptor_digest:
            raise DurableJobLeaseError("general task child descriptor is outside the original selected grant")
        native_input, input_record = verify_staged_task_artifact(self.staged_input,
            parent_job_id=parent.run_identity, creation_digest=previous.creation_digest)
        resolved_inputs = await resolve_current_native_step_inputs(
            db, parent, task, attempt, previous, envelope, step)
        expected_inputs = {"step_id": binding.step_id, "tool_id": step.tool_id,
            "tool_input_digest": binding.input_digest, "descriptor_digest": binding.descriptor_digest,
            "typed_input_ref": "general-task-input:" + self.staged_input.reference.artifact_id,
            "typed_input_digest": self.staged_input.reference.digest}
        if (self.staged_input.reference.schema_version != "GeneralTaskToolInput.v1"
            or child.input_digest != digest(expected_inputs)
            or native_input.invocation_id != binding.invocation_id
            or native_input.tool_id != step.tool_id
            or native_input.descriptor_digest != binding.descriptor_digest
            or native_input.input_digest != binding.input_digest
            or digest(native_input.inputs) != binding.input_digest
            or native_input.inputs != resolved_inputs
            or digest(resolved_inputs) != binding.input_digest):
            raise DurableJobLeaseError("general task private native tool input binding changed")
        # Generic jobs retain only an input shape. This sealed owner admits
        # the closed six-key content-free private-artifact reference envelope.
        from src.workflows.job_runtime import _canonical
        child.arguments_json = _canonical(expected_inputs)
        # Any original admission freezes this step across every later revision.
        siblings = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == parent.run_identity))).scalars().all())
        if any(child_binding(item).step_id == binding.step_id for item in siblings):
            raise DurableJobLeaseError("general task admitted step cannot be repeated")
        if len(siblings) != len(previous.admitted_invocation_ids) or len(siblings) >= envelope.task_input.limits.max_steps:
            raise DurableJobLeaseError("general task original invocation allowance changed")
        proposed = self.manifest
        # Board's exact successor revision is part of the new wait manifest.
        view_task = type("TaskCounter", (), {"task_revision": task.task_revision + 1})()
        _next_manifest(parent, previous, proposed, task=view_task, attempt=attempt)
        _validate_staged_refs(previous, proposed, ())
        await verify_general_task_manifest(db, parent, task, attempt, proposed)
        published, values = _published_values(parent, proposed, ())
        await _cas_board(db, task, attempt, status=WorkBoardStatus.blocked,
            reason="general_task_native_wait", owner=None, expiry=None, advance_fence=False)
        await _cas_parent(db, parent, {**values, "status": "paused",
            "failure_reason": "general_task_native_wait", "lease_owner": None, "lease_expires_at": None})
        from src.workflows.job_runtime import _canonical, _utc_now
        child.artifact_receipts_json = _canonical([{**input_record, "recorded_at": _utc_now().isoformat()}])


def is_fixed_child_admission(value):
    return type(value) is _ChildAdmission and value.seal is _ADMISSION_SEAL


async def admit_child(jobs, spec, *, manifest, owner, fencing_token, expected_revision, staged_input):
    from src.workflows.job_runtime import DurableJobLeaseError
    if (type(manifest) is not GeneralTaskCurrentManifestV1
        or spec.identity.job_kind != GENERAL_TASK_NATIVE_CHILD_KIND
        or spec.identity.capability_version != "1" or spec.identity.owner_kind != "user"
        or spec.max_attempts != 1 or spec.max_outstanding_jobs is not None
        or spec.parent_job_id != manifest.run_id or spec.parent_fencing_token != fencing_token):
        raise DurableJobLeaseError("fixed native child spec is required")
    proof = _ChildAdmission(jobs, manifest, owner, fencing_token, expected_revision, staged_input, _ADMISSION_SEAL)
    return await jobs.admit_job(spec, admission_authority_check=proof)


async def publish_step_receipt(jobs, parent_id, *, staged_artifact, child_id, owner, fencing_token, expected_parent_revision):
    from src.work_board.repository import _begin_sqlite_immediate
    from src.work_board.general_task_runtime_artifacts import verify_staged_task_artifact, compile_phase_digest
    from src.work_board.contracts import GeneralTaskStepReceiptV1
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _serialize, _verified_readback_exists
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent, task, attempt, previous, _envelope = await _current(jobs, db, parent_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        child = await jobs._fetch(db, child_id)
        jobs._assert_lease(child, owner=owner, fencing_token=fencing_token)
        await assert_general_task_child_phase_current(db, child)
        binding = child_binding(child)
        receipt, _record = verify_staged_task_artifact(staged_artifact,
            parent_job_id=parent_id, creation_digest=previous.creation_digest)
        if (type(receipt) is not GeneralTaskStepReceiptV1 or parent.revision != expected_parent_revision
            or parent.status != "paused" or previous.phase != "native_wait" or child.status != "running"
            or child.attempt_count != 1 or child.fencing_token <= 0
            or receipt.child_attempt_count != child.attempt_count or receipt.child_fence != child.fencing_token
            or receipt.child_job_id != child_id or receipt.invocation_id != child_id
            or receipt.task_id != task.task_id or receipt.attempt_id != attempt.attempt_id
            or receipt.step_id != binding.step_id or receipt.plan_revision != binding.plan_revision
            or receipt.input_digest != binding.input_digest or receipt.descriptor_digest != binding.descriptor_digest
            or receipt.selected_grant_digest != binding.selected_grant_digest
            or receipt.phase_digest != binding.phase_digest):
            raise DurableJobLeaseError("general task receipt requires the actual original positive child claim")
        effects = json.loads(child.effect_receipts_json or "[]")
        if binding.step_id not in previous.step_ids and (receipt.status != "running"
            or receipt.contact_state != "not_contacted" or effects):
            raise DurableJobLeaseError("the first native receipt must bind a positive claim before contact")
        if receipt.status == "verified":
            if (receipt.contact_state != "settled" or receipt.effect_receipt_digest != _digest(effects)
                or not _verified_readback_exists(effects)):
                raise DurableJobLeaseError("general task verified receipt requires canonical native readback")
            artifacts = json.loads(child.artifact_receipts_json or "[]")
            for ref in receipt.artifact_refs:
                if not any(item.get("artifact_id") == ref.artifact_id and item.get("content_sha256") == ref.digest for item in artifacts):
                    raise DurableJobLeaseError("general task result artifact is not canonical child output")
        elif receipt.status not in {"running", "awaiting_approval", "failed", "blocked", "cancelled", "unknown"}:
            raise DurableJobLeaseError("admitted receipt cannot replace an actual positive claim")
        elif receipt.contact_state == "not_contacted" and effects:
            raise DurableJobLeaseError("general task cannot erase prior contact evidence")
        refs = dict(zip(previous.step_ids, zip(previous.step_receipt_artifact_ids,
            previous.step_receipt_digests, previous.step_receipt_schemas)))
        if binding.step_id in refs:
            old = _step_receipt(previous, binding.step_id)
            if old.status in {"verified", "unknown"} or old.contact_state == "unknown":
                raise DurableJobLeaseError("verified or unresolved native step evidence is frozen")
        refs[binding.step_id] = (staged_artifact.reference.artifact_id, staged_artifact.reference.digest, "StepReceipt.v1")
        steps = sorted(refs)
        proposed = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1,
            "step_ids": steps, "step_receipt_artifact_ids": [refs[key][0] for key in steps],
            "step_receipt_digests": [refs[key][1] for key in steps], "step_receipt_schemas": [refs[key][2] for key in steps]})
        _next_manifest(parent, previous, proposed, task=task, attempt=attempt)
        _validate_staged_refs(previous, proposed, (staged_artifact,))
        published, values = _published_values(parent, proposed, (staged_artifact,))
        await _cas_parent(db, parent, values)
        return {"job": _serialize(await jobs._fetch(db, parent_id)), "manifest": published.model_dump(mode="json")}


def _phase_successor(previous, *, phase, task_revision, job_fence, board_fence):
    from src.work_board.general_task_runtime_artifacts import compile_phase_digest
    value = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1,
        "phase_revision": previous.phase_revision + 1, "phase": phase,
        "task_revision": task_revision, "job_fence": job_fence, "board_fence": board_fence})
    return value.model_copy(update={"phase_digest": compile_phase_digest(value)})


async def pause_parent(jobs, parent_id, *, operator_owner, expected_task_revision, expected_revision, expected_manifest_revision):
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import DurableJobLeaseError, _serialize, _job_has_unsafe_effects, _verified_readback_exists
    if type(operator_owner) is not WorkBoardOwner:
        raise DurableJobLeaseError("current typed operator owner required")
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent, task, attempt, previous, _envelope = await _current(jobs, db, parent_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        if (operator_owner.principal_id != task.owner_principal_id or operator_owner.session_id != task.owner_session_id
            or task.task_revision != expected_task_revision or parent.revision != expected_revision
            or previous.manifest_revision != expected_manifest_revision
            or previous.phase not in {"native_ready", "assembly", "native_wait"}
            or parent.status not in {"running", "paused"}
            or task.status not in {WorkBoardStatus.running, WorkBoardStatus.blocked}):
            raise DurableJobLeaseError("general task exact operator pause binding changed")
        children = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == parent_id))).scalars().all())
        if sorted(item.run_identity for item in children) != sorted(previous.admitted_invocation_ids):
            raise DurableJobLeaseError("general task original admitted child set changed")
        for child in children:
            binding = child_binding(child)
            effects = json.loads(child.effect_receipts_json or "[]")
            if (binding.creation_digest != previous.creation_digest
                or child.lease_owner or child.lease_expires_at
                or child.status not in {"succeeded", "degraded", "cancelled"}
                or _job_has_unsafe_effects(effects)):
                raise DurableJobLeaseError("native children must close under native_wait before paired pause; unknown remains in wait")
            if child.status in {"succeeded", "degraded"}:
                receipt = _step_receipt(previous, binding.step_id)
                if (receipt.status != "verified" or receipt.contact_state != "settled"
                    or receipt.child_job_id != child.run_identity
                    or receipt.child_fence != child.fencing_token
                    or receipt.child_attempt_count != child.attempt_count
                    or not _verified_readback_exists(effects)):
                    raise DurableJobLeaseError("native child closure requires original verified readback")
            elif child.attempt_count > 0:
                # Cancellation of an asyncio.to_thread awaiter is not closure
                # of its original callback. Until the reviewed sealed native
                # completion receipt is present, this remains inspect-only.
                raise DurableJobLeaseError("claimed native cancellation requires original callback closure proof")
        proposed = _phase_successor(previous, phase="operator_paused", task_revision=task.task_revision + 1,
            job_fence=parent.fencing_token + 1, board_fence=attempt.fencing_token + 1)
        published, values = _published_values(parent, proposed, ())
        await _cas_board(db, task, attempt, status=WorkBoardStatus.blocked,
            reason="general_task_operator_paused", owner=None, expiry=None, advance_fence=True)
        await _cas_parent(db, parent, {**values, "status": "paused",
            "failure_reason": "general_task_operator_paused", "lease_owner": None, "lease_expires_at": None,
            "fencing_token": parent.fencing_token + 1})
        return {"job": _serialize(await jobs._fetch(db, parent_id)), "manifest": published.model_dump(mode="json")}


async def resume_parent(jobs, parent_id, *, owner, expected_revision, expected_manifest_revision):
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _job_has_unsafe_effects, _serialize, _utc_now, _verified_readback_exists
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent, task, attempt, previous, _envelope = await _current(jobs, db, parent_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        expected_reason = {"native_wait": "general_task_native_wait", "operator_paused": "general_task_operator_paused"}.get(previous.phase)
        if (not owner or parent.revision != expected_revision or previous.manifest_revision != expected_manifest_revision
            or expected_reason is None or parent.status != "paused" or parent.failure_reason != expected_reason
            or task.status != WorkBoardStatus.blocked or task.block_reason != expected_reason
            or parent.lease_owner or parent.lease_expires_at or attempt.lease_owner or attempt.lease_expires_at):
            raise DurableJobLeaseError("general task exact native wait changed")
        children = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent_id))).scalars().all())
        if sorted(item.run_identity for item in children) != sorted(previous.admitted_invocation_ids):
            raise DurableJobLeaseError("general task original admitted child set changed")
        for child in children:
            binding = child_binding(child)
            effects = json.loads(child.effect_receipts_json or "[]")
            if (binding.creation_digest != previous.creation_digest or child.lease_owner or child.lease_expires_at
                or child.status not in {"succeeded", "degraded", "failed", "blocked", "cancelled"}
                or _job_has_unsafe_effects(effects)):
                raise DurableJobLeaseError("general task admitted child requires exact recovery; never replay")
            if child.status in {"succeeded", "degraded"}:
                receipt = _step_receipt(previous, binding.step_id)
                if (receipt.status != "verified" or receipt.contact_state != "settled"
                    or receipt.child_job_id != child.run_identity or receipt.child_fence != child.fencing_token
                    or receipt.child_attempt_count != child.attempt_count or not _verified_readback_exists(effects)):
                    raise DurableJobLeaseError("general task successful child lacks original verified readback")
        expiry = min(previous.original_deadline_at, _as_utc(parent.deadline_at), _utc_now() + timedelta(seconds=30))
        proposed = _phase_successor(previous, phase="assembly", task_revision=task.task_revision + 1,
            job_fence=parent.fencing_token + 1, board_fence=attempt.fencing_token + 1)
        published, values = _published_values(parent, proposed, ())
        # Board uses the dispatcher owner; durable Root uses its exact
        # attempt-qualified owner, preserving existing wrapper identity.
        runtime_owner = owner if owner.endswith(":" + attempt.attempt_id) else owner + ":" + attempt.attempt_id
        board_owner = runtime_owner[:-(len(attempt.attempt_id) + 1)]
        await _cas_board(db, task, attempt, status=WorkBoardStatus.running,
            reason=None, owner=board_owner, expiry=expiry, advance_fence=True)
        await _cas_parent(db, parent, {**values, "status": "running", "failure_reason": None,
            "lease_owner": runtime_owner, "lease_expires_at": expiry,
            "fencing_token": parent.fencing_token + 1, "heartbeat_at": _utc_now()})
        return {"job": _serialize(await jobs._fetch(db, parent_id)), "manifest": published.model_dump(mode="json")}
