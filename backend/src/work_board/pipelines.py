"""Metadata-only, reviewed three-leaf operation on canonical Work proposals.

The proposal never executes work. Existing tasks, native durable jobs and their
independent readbacks retain authority. All operation mutations use SQLite CAS.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping
import uuid

from sqlalchemy import func, select, update

from config.settings import settings
from src.db.engine import get_session
from src.db.models import Goal, WorkBoardAttempt, WorkBoardHandoff, WorkBoardInputArtifact, WorkBoardLink, WorkBoardProposal, WorkBoardStatus, WorkBoardTask
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardLinkCreate, WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.pipeline_contracts import CAPABILITIES, CPU_KINDS, DOSSIER, REPORT, PIPELINE_KIND, SLOTS, EvidenceConsumerInput, MAX_QUOTED_BYTES, canonical_bytes, digest
from src.work_board.repository import BoardError, WorkBoardRepository, _begin_sqlite_immediate
from src.workspace import canonical_workspace_root, canonical_workspace_root_identity


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def root_binding() -> Mapping[str, Any]:
    # Existing canonical workspace identity includes the live path/device/inode.
    return canonical_workspace_root_identity(settings.workspace_dir)


def unpack(row: WorkBoardProposal) -> dict[str, Any]:
    try:
        value = json.loads(row.proposal_json)
    except (TypeError, ValueError) as exc:
        raise BoardError("pipeline_corrupt", "The reviewed operation metadata is unavailable", status_code=409) from exc
    if not isinstance(value, dict) or value.get("kind") != PIPELINE_KIND or digest(value) != row.proposal_digest:
        raise BoardError("pipeline_corrupt", "The reviewed operation digest changed", status_code=409)
    return value


async def owned(db: Any, owner: WorkBoardOwner, operation_id: str, *, revision: int | None = None) -> tuple[WorkBoardProposal, dict[str, Any]]:
    row = await db.scalar(select(WorkBoardProposal).where(WorkBoardProposal.proposal_id == operation_id,
        WorkBoardProposal.owner_principal_id == owner.principal_id, WorkBoardProposal.owner_session_id == owner.session_id,
        WorkBoardProposal.kind == PIPELINE_KIND).execution_options(populate_existing=True))
    if row is None:
        raise BoardError("pipeline_not_found", "The reviewed operation is unavailable", status_code=404)
    if revision is not None and row.revision != revision:
        raise BoardError("pipeline_revision_conflict", "The reviewed operation changed; read back before retry", status_code=409)
    value = unpack(row)
    if value["live_root"] != root_binding():
        raise BoardError("pipeline_root_changed", "The original live workspace changed; this operation cannot move roots", status_code=409)
    return row, value


async def store(db: Any, row: WorkBoardProposal, value: dict[str, Any], *, status: str | None = None) -> None:
    revision = row.revision
    result = await db.execute(update(WorkBoardProposal).where(WorkBoardProposal.proposal_id == row.proposal_id,
        WorkBoardProposal.revision == revision, WorkBoardProposal.proposal_digest == row.proposal_digest)
        .values(proposal_json=canonical_bytes(value).decode(), proposal_digest=digest(value), revision=revision + 1,
                status=status or row.status).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise BoardError("pipeline_revision_conflict", "The operation changed before its exact update", status_code=409)
    await db.flush()
    await db.refresh(row)


async def read(db: Any, owner: WorkBoardOwner, operation_id: str) -> dict[str, Any]:
    row, value = await owned(db, owner, operation_id)
    steps = []
    for slot in value.get("steps", []):
        task = await WorkBoardRepository().get_task(db, owner, slot["task_ref"])
        steps.append({"slot": slot["slot"], "capability_id": task.capability_id, "task_id": task.task_id,
            "task_revision": task.task_revision, "status": task.status.value, "block_kind": task.block_kind,
            "block_reason": task.block_reason})
    return {"operation_id": row.proposal_id, "revision": row.revision, "digest": row.proposal_digest,
        "status": row.status, "plan_version": value["plan_version"], "steps": steps,
        "limits": value["limits"], "deadline_at": value.get("deadline_at"),
        "source_scope": value["source_scope"], "no_learning": True,
        "pending_revision": value.get("pending_revision"), "reused_output": value.get("reused_output")}


async def preview(db: Any, owner: WorkBoardOwner, task_id: str, request: Any) -> dict[str, Any]:
    from src.work_board.input_artifacts import resolve_input_artifact_for_copy
    repository = WorkBoardRepository()
    source = await repository.get_task(db, owner, task_id)
    if source.task_revision != request.expected_revision:
        raise BoardError("stale_revision", "The source task changed", status_code=409)
    if source.capability_id != CAPABILITIES[0] or source.input_artifact_id != request.source_input_artifact_id:
        raise BoardError("pipeline_source_invalid", "Select the exact public-browser task input", status_code=409)
    if source.status not in {WorkBoardStatus.todo, WorkBoardStatus.triage} or source.pipeline_operation_id:
        raise BoardError("pipeline_source_attempted", "The source task must be unattempted and unbound", status_code=409)
    if await db.scalar(select(func.count()).select_from(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)):
        raise BoardError("pipeline_source_attempted", "Attempted sources require a distinct output-reuse review", status_code=409)
    goal = await repository.validate_task_goal(db, owner, source)
    resolved = await resolve_input_artifact_for_copy(db, owner, typed_input_ref=source.typed_input_ref,
        typed_input_digest=source.typed_input_digest,
        capability_id=source.capability_id, goal_id=source.goal_id, goal_revision=source.goal_revision)
    source_model = resolved.input
    binding = {"task_ref": task_id, "task_revision": source.task_revision, "input_ref": source.input_artifact_id,
               "input_sha256": source.typed_input_digest, "goal_id": goal.id, "goal_revision": goal.revision}
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"seraph:pipeline:{owner.principal_id}:{owner.session_id}:{request.idempotency_key}"))
    request_digest = digest(binding)
    existing = await db.get(WorkBoardProposal, identifier)
    if existing is not None:
        if existing.request_digest != request_digest:
            raise BoardError("pipeline_idempotency_conflict", "The operation key already owns another reviewed source", status_code=409)
        return await read(db, owner, identifier)
    value = {"kind": PIPELINE_KIND, "plan_version": 1, "live_root": root_binding(),
        "source": binding, "source_scope": {"start_url": source_model.get("start_url"),
            "allowed_hosts": source_model.get("allowed_hosts"), "permissions": ["public_https_browser", "workspace_read", "workspace_write"]},
        "limits": {"max_steps": 4, "max_total_seconds": 300, "max_attempts_per_leaf": 2,
            "max_attempts_total": 6, "browser_seconds": 180, "cpu_seconds": 30, "output_bytes": 65536, "model_cost": 0},
        "steps": [{"slot": SLOTS[0], "task_ref": source.task_id}], "all_task_refs": [source.task_id],
        "versions": [], "reservations": {}, "no_learning": True}
    row = WorkBoardProposal(proposal_id=identifier, owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, parent_task_id=task_id, parent_revision=source.task_revision,
        goal_revision=source.goal_revision, kind=PIPELINE_KIND, idempotency_key=request.idempotency_key,
        request_digest=request_digest, capability_id=PIPELINE_KIND, capability_version="1",
        status="proposed", proposal_json=canonical_bytes(value).decode(), proposal_digest=digest(value),
        expires_at=now() + timedelta(minutes=5))
    db.add(row)
    await db.flush()
    return await read(db, owner, identifier)


async def accept(db: Any, owner: WorkBoardOwner, operation_id: str, request: Any) -> dict[str, Any]:
    await _begin_sqlite_immediate(db)
    row, value = await owned(db, owner, operation_id)
    if row.status == "accepted" and value.get("accepted_digest") == request.expected_digest:
        return await read(db, owner, operation_id)
    if row.revision != request.expected_revision or row.proposal_digest != request.expected_digest:
        raise BoardError("pipeline_revision_conflict", "Review the exact current operation version", status_code=409)
    repository = WorkBoardRepository()
    source = await repository.get_task(db, owner, row.parent_task_id)
    if source.task_revision != request.expected_parent_revision or row.parent_revision != source.task_revision:
        raise BoardError("stale_revision", "The reviewed source task changed", status_code=409)
    if row.status != "proposed" or utc(row.expires_at) <= now():
        raise BoardError("pipeline_review_expired", "The operation needs a fresh exact review", status_code=409)
    goal = await repository.validate_task_goal(db, owner, source)
    from src.goals.repository import deserialize_admission_budget
    budget = deserialize_admission_budget(goal)
    seconds = min(300, int(budget.max_runtime_seconds))
    value["deadline_at"] = (now() + timedelta(seconds=seconds)).isoformat()
    value["admitted_at"] = now().isoformat()
    value["accepted_digest"] = request.expected_digest
    previous = source
    source.pipeline_operation_id, source.pipeline_slot = operation_id, SLOTS[0]
    for slot, capability in zip(SLOTS[1:], CAPABILITIES[1:]):
        created = await repository.create_task(db, owner, WorkBoardTaskCreate(goal_id=source.goal_id,
            goal_revision=source.goal_revision, title="Evidence dossier" if capability == DOSSIER else "Local evidence report",
            body="Waiting for independently verified producer output; deterministic CPU, no_learning",
            capability_id=capability, status=WorkBoardStatus.triage, priority=source.priority,
            idempotency_scope="pipeline", idempotency_key=f"{operation_id}:{slot}"))
        child = created.task
        child.pipeline_operation_id, child.pipeline_slot = operation_id, slot
        await db.flush()
        await repository.add_link(db, owner,
            WorkBoardLinkCreate(parent_task_id=previous.task_id, child_task_id=child.task_id,
                expected_child_revision=child.task_revision), acquire_lock=False)
        value["steps"].append({"slot": slot, "task_ref": child.task_id})
        value["all_task_refs"].append(child.task_id)
        previous = child
    value["versions"].append({"plan_version": 1, "source": value["source"], "steps": list(value["steps"]), "review_digest": request.expected_digest})
    await store(db, row, value, status="accepted")
    return await read(db, owner, operation_id)


async def task_guard(db: Any, task: WorkBoardTask, *, attempt: WorkBoardAttempt | None = None) -> tuple[WorkBoardProposal, dict[str, Any]]:
    owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    if not task.pipeline_operation_id:
        raise BoardError("pipeline_binding_required", "Evidence leaves require an exact reviewed operation", status_code=409)
    row, value = await owned(db, owner, task.pipeline_operation_id)
    if row.status != "accepted" or value.get("pending_revision"):
        raise BoardError("pipeline_review_required", "Changed unfinished work needs exact plan review", status_code=409)
    if not any(step["task_ref"] == task.task_id and step["slot"] == task.pipeline_slot for step in value["steps"]):
        raise BoardError("pipeline_task_stale", "This task is no longer a current operation step", status_code=409)
    deadline = utc(datetime.fromisoformat(value["deadline_at"]))
    if deadline <= now():
        raise BoardError("pipeline_expired", "The original absolute operation deadline expired", status_code=409)
    await WorkBoardRepository().validate_task_goal(db, owner, task)
    counts = await db.execute(select(WorkBoardAttempt.task_id, func.count()).where(
        WorkBoardAttempt.task_id.in_(value["all_task_refs"])).group_by(WorkBoardAttempt.task_id))
    counts = dict(counts.all())
    allowance = 0 if attempt is not None else 1
    if sum(counts.values()) + allowance > 6 or counts.get(task.task_id, 0) + allowance > 2:
        raise BoardError("pipeline_attempts_exhausted", "The original finite operation attempt allowance is exhausted", status_code=409)
    return row, value


async def validate_cpu_current(task: WorkBoardTask, attempt: WorkBoardAttempt, inputs: Mapping[str, Any], *, session_provider: Any = get_session) -> None:
    from src.work_board import review
    from src.work_board.pipeline_cpu import read_output
    model = EvidenceConsumerInput.model_validate(dict(inputs))
    async with session_provider() as db:
        current = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
        active = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt.attempt_id))
        if current is None or active is None or active.ended_at or active.cancel_requested_at or current.task_revision != task.task_revision or current.status != WorkBoardStatus.running:
            raise BoardError("pipeline_task_changed", "The consumer was cancelled or changed", status_code=409)
        row, value = await task_guard(db, current, attempt=active)
        if model.operation_ref != row.proposal_id or model.plan_version != value["plan_version"]:
            raise BoardError("pipeline_plan_changed", "The consumer input belongs to another reviewed plan", status_code=409)
        parent = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == model.producer_task_ref))
        link = await db.scalar(select(WorkBoardLink).where(WorkBoardLink.parent_task_id == model.producer_task_ref,
            WorkBoardLink.child_task_id == task.task_id))
        owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
        if parent is None or link is None or link.current_handoff_id != model.handoff_ref or not await review.current_handoff_is_verified(db, owner, parent, current, link):
            raise BoardError("pipeline_handoff_changed", "The exact verified producer binding changed", status_code=409)
        handoff = await db.get(WorkBoardHandoff, model.handoff_ref)
        if handoff.source_attempt_id != model.producer_attempt_ref:
            raise BoardError("pipeline_producer_changed", "The producer attempt changed", status_code=409)
        output = await verified_output(db, owner, parent)
        raw = read_output(output["file_path"], output["content_sha256"])
        if output["content_sha256"] != model.producer_sha256 or raw.decode("utf-8") != model.quoted_source_data:
            raise BoardError("pipeline_source_changed", "The admitted quoted source differs from verified producer bytes", status_code=409)


async def verified_output(db: Any, owner: WorkBoardOwner, producer: WorkBoardTask) -> dict[str, Any]:
    from src.work_board.review import _verified_workflow_readback
    from src.workflows.job_runtime import durable_job_repository
    from src.work_board.pipeline_cpu import read_output
    if producer.status != WorkBoardStatus.done or producer.owner_principal_id != owner.principal_id or producer.owner_session_id != owner.session_id:
        raise BoardError("pipeline_output_required", "The producer has no completed output", status_code=409)
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == producer.task_id)
        .order_by(WorkBoardAttempt.created_at.desc()).limit(1))
    if attempt is None or await _verified_workflow_readback(db, producer, attempt) is None:
        raise BoardError("pipeline_output_unverified", "The producer output needs independent verification", status_code=409)
    projection = await durable_job_repository.get_job(attempt.workflow_run_id)
    if not isinstance(projection, Mapping) or projection.get("status") != "succeeded":
        raise BoardError("pipeline_output_unverified", "The actual settled producer job is unavailable", status_code=409)
    effects = projection.get("effects", [])
    for artifact in projection.get("artifacts", []):
        if not isinstance(artifact, Mapping) or not artifact.get("exists"):
            continue
        sha, ref = artifact.get("content_sha256"), artifact.get("file_path")
        if not isinstance(sha, str) or not isinstance(ref, str):
            continue
        if not any(isinstance(effect, Mapping) and effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
            and effect.get("target_path") == ref and effect.get("content_sha256") == sha for effect in effects):
            continue
        raw = read_output(ref, sha)
        if len(raw) > MAX_QUOTED_BYTES:
            raise BoardError("pipeline_output_too_large", "The producer exceeds the finite consumer input allowance", status_code=409)
        return {"file_path": ref, "content_sha256": sha, "attempt_id": attempt.attempt_id,
                "quoted_source_data": raw.decode("utf-8")}
    raise BoardError("pipeline_output_unverified", "No exact settled artifact/readback pair is available", status_code=409)


async def advance(db: Any, owner: WorkBoardOwner, operation_id: str, expected_revision: int) -> dict[str, Any]:
    from src.work_board.input_artifacts import prepare_input_artifact, bind_input_artifact, resolve_input_artifact_for_task
    from src.work_board.review import materialize_handoff_for_link
    from src.work_board.dispatcher import registered_executor_id
    await _begin_sqlite_immediate(db)
    row, value = await owned(db, owner, operation_id, revision=expected_revision)
    for index in (1, 2):
        producer = await WorkBoardRepository().get_task(db, owner, value["steps"][index - 1]["task_ref"])
        consumer = await WorkBoardRepository().get_task(db, owner, value["steps"][index]["task_ref"])
        await task_guard(db, consumer)
        if consumer.input_artifact_id or consumer.status != WorkBoardStatus.triage:
            continue
        if producer.status != WorkBoardStatus.done:
            continue
        output = await verified_output(db, owner, producer)
        link = await db.scalar(select(WorkBoardLink).where(WorkBoardLink.parent_task_id == producer.task_id, WorkBoardLink.child_task_id == consumer.task_id))
        handoff = await materialize_handoff_for_link(db, owner, producer, consumer, link)
        inputs = EvidenceConsumerInput(schema_version=1, operation_ref=operation_id, plan_version=value["plan_version"],
            producer_task_ref=producer.task_id, producer_attempt_ref=output["attempt_id"], handoff_ref=handoff.handoff_id,
            producer_sha256=output["content_sha256"], producer_schema="browser_public_task_result" if index == 1 else "evidence_dossier.v1",
            quoted_source_data=output["quoted_source_data"], no_learning=True)
        key = f"{operation_id}:{value['plan_version']}:{output['attempt_id']}:{SLOTS[index]}"
        expected_task_revision = consumer.task_revision
        value["reservations"][SLOTS[index]] = {"key": key, "input_sha256": digest(inputs.model_dump(mode="json")),
            "producer_attempt_ref": output["attempt_id"], "consumer_revision": expected_task_revision, "state": "reserved"}
        await store(db, row, value)
        await db.commit()  # reservation precedes filesystem materialization
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(schema_version=1,
            capability_id=consumer.capability_id, goal_id=consumer.goal_id, goal_revision=consumer.goal_revision,
            input=inputs.model_dump(mode="json"), idempotency_key=key))
        await _begin_sqlite_immediate(db)
        row, current = await owned(db, owner, operation_id, revision=row.revision)
        consumer = await WorkBoardRepository().get_task(db, owner, consumer.task_id)
        await task_guard(db, consumer)
        if current["reservations"].get(SLOTS[index]) != value["reservations"][SLOTS[index]] or consumer.task_revision != expected_task_revision or consumer.status != WorkBoardStatus.triage:
            raise BoardError("pipeline_materialization_conflict", "The exact consumer reservation changed", status_code=409)
        next_revision = expected_task_revision + 1
        resolved = await resolve_input_artifact_for_task(db, owner, artifact_id=artifact.artifact_id,
            capability_id=consumer.capability_id, goal_id=consumer.goal_id, goal_revision=consumer.goal_revision)
        await bind_input_artifact(db, owner, artifact=resolved, task_id=consumer.task_id,
            task_revision=next_revision)
        changed = await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id == consumer.task_id,
            WorkBoardTask.task_revision == expected_task_revision, WorkBoardTask.pipeline_operation_id == operation_id,
            WorkBoardTask.status == WorkBoardStatus.triage).values(input_artifact_id=artifact.artifact_id,
                typed_input_ref=artifact.typed_input_ref, typed_input_digest=artifact.typed_input_digest,
                executor_id=registered_executor_id(consumer.capability_id), status=WorkBoardStatus.todo,
                task_revision=next_revision, updated_at=now()).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise BoardError("pipeline_materialization_conflict", "The consumer changed before adoption", status_code=409)
        current["reservations"][SLOTS[index]]["state"] = "bound"
        current["reservations"][SLOTS[index]]["artifact_ref"] = artifact.artifact_id
        await store(db, row, current)
        value = current
    return await read(db, owner, operation_id)
