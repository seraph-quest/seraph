"""Metadata-only, reviewed three-leaf operation on canonical Work proposals.

The proposal never executes work. Existing tasks, native durable jobs and their
independent readbacks retain authority. All operation mutations use SQLite CAS.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping
import uuid

from sqlalchemy import func, select, update

from config.settings import settings
from src.db.engine import get_session
from src.db.models import Goal, WorkBoardAttempt, WorkBoardHandoff, WorkBoardInputArtifact, WorkBoardLink, WorkBoardProposal, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardLinkCreate, WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.pipeline_contracts import CAPABILITIES, CPU_KINDS, DOSSIER, REPORT, PIPELINE_KIND, SLOTS, EvidenceConsumerInput, MAX_QUOTED_BYTES, canonical_bytes, digest
from src.work_board.repository import BoardError, WorkBoardRepository, _begin_sqlite_immediate
from src.workspace import canonical_workspace_root, canonical_workspace_root_identity


RECOVERY_REASONS = frozenset({"input_artifact_cleanup_required", "input_artifact_write_failed",
    "pipeline_recovery_required", "pipeline_materialization_conflict", "pipeline_output_unverified",
    "pipeline_output_too_large", "pipeline_handoff_changed", "pipeline_input_changed",
    "pipeline_root_changed", "pipeline_goal_changed", "pipeline_source_changed",
    "pipeline_source_permission", "pipeline_review_required", "pipeline_expired",
    "pipeline_attempts_exhausted", "pipeline_task_changed", "pipeline_producer_changed",
    "pipeline_plan_changed", "source_stale", "goal_review_required", "opportunity_expired"})


def safe_recovery_reason(code):
    return code if code in RECOVERY_REASONS else "pipeline_recovery_required"


def recovery_reason(value):
    if (value.get("retired_input_cleanup") or {}).get("entries"):
        return "input_artifact_cleanup_required"
    code = (value.get("advance_request") or {}).get("recovery_reason")
    return safe_recovery_reason(code) if code else None


def row_token(row):
    values = row.model_dump()
    return digest({key: utc(value).isoformat() if isinstance(value, datetime) else value
                   for key, value in values.items()})


@dataclass(frozen=True)
class PipelineAcceptContext:
    operation_id: str
    owner_principal_id: str
    original_root_id: str
    proposal_request_digest: str
    operation_revision: int
    operation_digest: str
    request_bytes: bytes
    workspace_identity: bytes
    source_witness: Any
    task_texts: tuple
    input_witness: Any = None
    producer_witness: Any = None
    retirement_witnesses: tuple = ()


async def stage_accept(db, owner, operation_id, request, *, source_witness):
    from src.work_board.repository import stage_safe_task_text
    from src.work_board.input_artifacts import stage_input_artifact, InputRetirementWitness, _metadata_digest
    row, value = await owned(db, owner, operation_id)
    workspace = canonical_bytes(value["live_root"])
    if getattr(row, "opportunity_id", None) and source_witness is None:
        raise BoardError("pipeline_source_changed", "Linked plan source proof is required", status_code=409)
    pending = value.get("pending_revision")
    texts, input_witness, retirement = [], None, []
    producer = None
    tasks = [await WorkBoardRepository().get_task(db, owner, step["task_ref"]) for step in value["steps"]]
    if pending:
        artifact_id = pending["source_input_artifact_id"]
        input_witness = await stage_input_artifact(db, owner, artifact_id=artifact_id,
            capability_id=CAPABILITIES[0], goal_id=pending["goal_id"], goal_revision=pending["goal_revision"])
        requests = [WorkBoardTaskCreate(goal_id=pending["goal_id"], goal_revision=pending["goal_revision"],
            title="Reviewed replacement public source", status=WorkBoardStatus.todo,
            capability_id=CAPABILITIES[0], input_artifact_id=artifact_id, priority=tasks[0].priority,
            idempotency_scope="pipeline", idempotency_key=f"{operation_id}:{pending['proposed_plan_version']}:{SLOTS[0]}")]
        for index, task in enumerate(tasks[1:], 1):
            count = await db.scalar(select(func.count()).select_from(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id))
            if count:
                requests.append(WorkBoardTaskCreate(goal_id=pending["goal_id"], goal_revision=pending["goal_revision"],
                    title=task.title, capability_id=CAPABILITIES[index], status=WorkBoardStatus.triage,
                    priority=task.priority, idempotency_scope="pipeline",
                    idempotency_key=f"{operation_id}:{pending['proposed_plan_version']}:{SLOTS[index]}"))
            elif task.input_artifact_id:
                artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id, populate_existing=True)
                from src.work_board.input_artifacts import _payload_path, _safe_file_bytes
                _safe_file_bytes(_payload_path(artifact), expected_digest=artifact.payload_sha256, expected_size=artifact.size_bytes)
                retirement.append((SLOTS[index], InputRetirementWitness(artifact.artifact_id, artifact.revision,
                    _metadata_digest(artifact), task.task_id, task.task_revision, artifact.typed_input_ref,
                    artifact.payload_sha256, artifact.size_bytes, workspace)))
    else:
        source = tasks[0]
        requests = [WorkBoardTaskCreate(goal_id=source.goal_id, goal_revision=row.goal_revision,
            title="Evidence dossier" if capability == DOSSIER else "Local evidence report",
            body="Waiting for independently verified producer output; deterministic CPU, no_learning",
            capability_id=capability, status=WorkBoardStatus.triage, priority=source.priority,
            idempotency_scope="pipeline", idempotency_key=f"{operation_id}:{slot}")
            for slot, capability in zip(SLOTS[1:], CAPABILITIES[1:])]
        if value.get("reused_output"):
            from src.work_board.review import stage_pipeline_producer_readback
            producer = await stage_pipeline_producer_readback(db, owner, source)
    for task_request in requests:
        texts.append((task_request.idempotency_key, await stage_safe_task_text(db, owner, task_request)))
    return PipelineAcceptContext(operation_id, owner.principal_id, owner.session_id, row.request_digest,
        row.revision, row.proposal_digest, canonical_bytes(request.model_dump(mode="json")), workspace,
        source_witness, tuple(texts), input_witness, producer, tuple(retirement))


stage_revision_accept = stage_accept


def validate_retired_cleanup(intent):
    import re
    if intent is None:
        return
    header = {"schema_version", "accepted_digest", "accepted_request_digest", "owner_principal_id",
        "original_root_id", "workspace_identity_digest", "plan_version", "revision_request_key", "entries"}
    fields = {"slot", "task_ref", "task_revision", "artifact_ref", "typed_input_ref", "content_sha256",
        "size_bytes", "source_revision", "source_metadata_digest", "tombstone_revision",
        "tombstone_metadata_digest", "state", "reason_code"}
    reasons = {"cleanup_reference_invalid", "cleanup_root_changed", "cleanup_target_missing",
        "cleanup_target_unavailable", "cleanup_target_metadata_mismatch", "cleanup_digest_mismatch",
        "cleanup_target_replaced", "cleanup_binding_changed", "cleanup_unverified"}
    def invalid():
        raise BoardError("pipeline_corrupt", "The bounded retirement intent is invalid", status_code=409)
    if not isinstance(intent, dict) or set(intent) != header or len(canonical_bytes(intent)) > 4096:
        invalid()
    if type(intent["schema_version"]) is not int or intent["schema_version"] != 1:
        invalid()
    if type(intent["plan_version"]) is not int or intent["plan_version"] < 1:
        invalid()
    for key in ("accepted_digest", "accepted_request_digest", "workspace_identity_digest"):
        if not isinstance(intent[key], str) or not re.fullmatch(r"[0-9a-f]{64}", intent[key]): invalid()
    for key in ("owner_principal_id", "original_root_id", "revision_request_key"):
        if not isinstance(intent[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", intent[key]): invalid()
    entries = intent["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 2: invalid()
    slots, artifacts = set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != fields: invalid()
        if entry["slot"] not in SLOTS[1:] or entry["slot"] in slots or entry["artifact_ref"] in artifacts: invalid()
        slots.add(entry["slot"]); artifacts.add(entry["artifact_ref"])
        for key in ("task_ref", "artifact_ref"):
            if not isinstance(entry[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", entry[key]): invalid()
        for key in ("task_revision", "source_revision", "tombstone_revision", "size_bytes"):
            if type(entry[key]) is not int or entry[key] < 1: invalid()
        if entry["size_bytes"] > 65536 or entry["tombstone_revision"] != entry["source_revision"] + 1: invalid()
        for key in ("content_sha256", "source_metadata_digest", "tombstone_metadata_digest"):
            if not isinstance(entry[key], str) or not re.fullmatch(r"[0-9a-f]{64}", entry[key]): invalid()
        from src.work_board.input_artifacts import INPUT_ARTIFACT_ROOT
        ref = entry["typed_input_ref"]
        if (not isinstance(ref, str) or len(ref) > 512 or not ref.startswith(f"workspace-json:{INPUT_ARTIFACT_ROOT}/")
            or any(part in {"", ".", ".."} for part in ref.split(":", 1)[1].split("/"))): invalid()
        if entry["state"] not in {"pending", "cleanup_required", "verified"}: invalid()
        if entry["reason_code"] is not None and entry["reason_code"] not in reasons: invalid()
        if entry["state"] != "cleanup_required" and entry["reason_code"] is not None: invalid()


async def stage_retired_input_cleanup(db, owner, operation_id):
    from src.work_board.input_artifacts import RetiredInputCleanupWitness, _metadata_digest
    row, value = await owned(db, owner, operation_id)
    intent = value.get("retired_input_cleanup")
    if not intent: return ()
    workspace = canonical_bytes(value["live_root"])
    if (intent["owner_principal_id"] != owner.principal_id or intent["original_root_id"] != owner.session_id
        or intent["workspace_identity_digest"] != hashlib.sha256(workspace).hexdigest()
        or intent["accepted_digest"] != value.get("accepted_digest")):
        raise BoardError("input_artifact_cleanup_required", "The original cleanup binding changed", status_code=503)
    result = []
    for entry in intent["entries"]:
        artifact = await db.get(WorkBoardInputArtifact, entry["artifact_ref"], populate_existing=True)
        if (artifact is None or artifact.owner_principal_id != owner.principal_id
            or artifact.owner_session_id != owner.session_id or artifact.state != "revoked"
            or artifact.revision != entry["tombstone_revision"]
            or artifact.metadata_digest != entry["tombstone_metadata_digest"]
            or _metadata_digest(artifact) != entry["tombstone_metadata_digest"]
            or artifact.typed_input_ref != entry["typed_input_ref"]
            or artifact.payload_sha256 != entry["content_sha256"]):
            raise BoardError("input_artifact_cleanup_required", "The tombstone binding changed", status_code=503)
        result.append(RetiredInputCleanupWitness(operation_id, owner.principal_id, owner.session_id,
            row.request_digest, intent["accepted_request_digest"], canonical_bytes(intent), canonical_bytes(entry),
            workspace, str(canonical_workspace_root(settings.workspace_dir))))
    return tuple(result)


async def _cleanup_binding_locked(db, owner, witness):
    from src.work_board.input_artifacts import _metadata_digest
    row, value = await owned(db, owner, witness.operation_id, workspace_identity=witness.workspace_identity)
    current = value.get("retired_input_cleanup")
    original = json.loads(witness.intent_bytes)
    if current is None: return row, value, None
    if (row.request_digest != witness.proposal_request_digest
        or {k: v for k,v in current.items() if k != "entries"} != {k: v for k,v in original.items() if k != "entries"}
        or current["accepted_request_digest"] != witness.accepted_request_digest):
        raise BoardError("input_artifact_cleanup_required", "The cleanup request changed", status_code=503)
    entry = json.loads(witness.entry_bytes)
    matched = next((item for item in current["entries"] if item["artifact_ref"] == entry["artifact_ref"]), None)
    if matched is None: return row, value, None
    if {k:v for k,v in matched.items() if k not in {"state", "reason_code"}} != {k:v for k,v in entry.items() if k not in {"state", "reason_code"}}:
        raise BoardError("input_artifact_cleanup_required", "The cleanup target changed", status_code=503)
    artifact = await db.get(WorkBoardInputArtifact, matched["artifact_ref"], populate_existing=True)
    if (artifact is None or artifact.owner_principal_id != owner.principal_id
        or artifact.owner_session_id != owner.session_id or artifact.state != "revoked"
        or artifact.revision != matched["tombstone_revision"] or _metadata_digest(artifact) != matched["tombstone_metadata_digest"]
        or artifact.metadata_digest != matched["tombstone_metadata_digest"]):
        raise BoardError("input_artifact_cleanup_required", "The exact tombstone changed", status_code=503)
    return row, value, matched


async def _ack_retired_input_cleanup_locked(db, owner, *, witness, readback):
    from src.work_board.input_artifacts import RetiredInputCleanupReadback
    if (not isinstance(readback, RetiredInputCleanupReadback)
        or readback.intent_digest != hashlib.sha256(witness.intent_bytes).hexdigest()
        or readback.artifact_id != json.loads(witness.entry_bytes)["artifact_ref"]):
        raise BoardError("input_artifact_cleanup_required", "The cleanup readback changed", status_code=503)
    row, value, entry = await _cleanup_binding_locked(db, owner, witness)
    if entry is None: return
    if readback.outcome == "absent" or entry["state"] == "verified":
        # A late failure never restores a verified/compacted retirement.
        entry["state"], entry["reason_code"] = "verified", None
        value["retired_input_cleanup"]["entries"].remove(entry)
        if not value["retired_input_cleanup"]["entries"]: value["retired_input_cleanup"] = None
    elif readback.outcome == "cleanup_required":
        if entry["state"] == "cleanup_required" and entry["reason_code"] == readback.reason_code: return
        entry["state"], entry["reason_code"] = "cleanup_required", readback.reason_code
    else:
        raise BoardError("input_artifact_cleanup_required", "The cleanup outcome is unverified", status_code=503)
    validate_retired_cleanup(value.get("retired_input_cleanup"))
    await store(db, row, value)


async def resume_retired_cleanup(db, owner, operation_id):
    from src.work_board.input_artifacts import cleanup_retired_input
    witnesses = await stage_retired_input_cleanup(db, owner, operation_id)
    failure = False
    for witness in witnesses:
        await _begin_sqlite_immediate(db)
        _row, _value, entry = await _cleanup_binding_locked(db, owner, witness)
        await db.commit()
        if entry is None: continue
        readback = cleanup_retired_input(witness)
        await _begin_sqlite_immediate(db)
        await _ack_retired_input_cleanup_locked(db, owner, witness=witness, readback=readback)
        await db.commit()
        failure = failure or readback.outcome != "absent"
    if failure:
        raise BoardError("input_artifact_cleanup_required", "The accepted plan retains exact private cleanup recovery", status_code=503)


async def record_recovery(db, owner, operation_id, code):
    row, value = await owned(db, owner, operation_id)
    workspace = canonical_bytes(value["live_root"])
    revision = row.revision
    await _begin_sqlite_immediate(db)
    row, value = await owned(db, owner, operation_id, revision=revision, workspace_identity=workspace)
    request = value.setdefault("advance_request", {})
    reason = safe_recovery_reason(code)
    if request.get("recovery_reason") != reason:
        request["recovery_reason"] = reason
        await store(db, row, value)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def root_binding() -> Mapping[str, Any]:
    # Existing canonical workspace identity includes the live path/device/inode.
    return canonical_workspace_root_identity(settings.workspace_dir)


def unpack(row: WorkBoardProposal) -> dict[str, Any]:
    try:
        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError("duplicate metadata key")
                result[key] = item
            return result
        value = json.loads(row.proposal_json, object_pairs_hook=pairs)
    except (TypeError, ValueError) as exc:
        raise BoardError("pipeline_corrupt", "The reviewed operation metadata is unavailable", status_code=409) from exc
    if not isinstance(value, dict) or value.get("kind") != PIPELINE_KIND or digest(value) != row.proposal_digest:
        raise BoardError("pipeline_corrupt", "The reviewed operation digest changed", status_code=409)
    validate_retired_cleanup(value.get("retired_input_cleanup"))
    return value


async def owned(db: Any, owner: WorkBoardOwner, operation_id: str, *, revision: int | None = None,
                workspace_identity: bytes | None = None) -> tuple[WorkBoardProposal, dict[str, Any]]:
    row = await db.scalar(select(WorkBoardProposal).where(WorkBoardProposal.proposal_id == operation_id,
        WorkBoardProposal.owner_principal_id == owner.principal_id, WorkBoardProposal.owner_session_id == owner.session_id,
        WorkBoardProposal.kind == PIPELINE_KIND).execution_options(populate_existing=True))
    if row is None:
        raise BoardError("pipeline_not_found", "The reviewed operation is unavailable", status_code=404)
    if revision is not None and row.revision != revision:
        raise BoardError("pipeline_revision_conflict", "The reviewed operation changed; read back before retry", status_code=409)
    value = unpack(row)
    if canonical_bytes(value["live_root"]) != (workspace_identity if workspace_identity is not None else canonical_bytes(root_binding())):
        raise BoardError("pipeline_root_changed", "The original live workspace changed; this operation cannot move roots", status_code=409)
    return row, value


async def store(db: Any, row: WorkBoardProposal, value: dict[str, Any], *, status: str | None = None) -> None:
    revision = row.revision
    encoded = canonical_bytes(value)
    if len(encoded) > 64 * 1024:
        raise BoardError("pipeline_metadata_limit", "The finite operation metadata allowance is exhausted", status_code=409)
    result = await db.execute(update(WorkBoardProposal).where(WorkBoardProposal.proposal_id == row.proposal_id,
        WorkBoardProposal.revision == revision, WorkBoardProposal.proposal_digest == row.proposal_digest)
        .values(proposal_json=encoded.decode(), proposal_digest=digest(value), revision=revision + 1,
                status=status or row.status).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise BoardError("pipeline_revision_conflict", "The operation changed before its exact update", status_code=409)
    await db.flush()
    await db.refresh(row)


async def read(db: Any, owner: WorkBoardOwner, operation_id: str, *, workspace_identity: bytes | None = None) -> dict[str, Any]:
    row, value = await owned(db, owner, operation_id, workspace_identity=workspace_identity)
    steps = []
    for slot in value.get("steps", []):
        task = await WorkBoardRepository().get_task(db, owner, slot["task_ref"])
        steps.append({"slot": slot["slot"], "capability_id": task.capability_id, "task_id": task.task_id,
            "task_revision": task.task_revision, "status": task.status.value, "block_kind": task.block_kind,
            "block_reason": task.block_reason})
    return {"operation_id": row.proposal_id, "revision": row.revision, "digest": row.proposal_digest,
        "parent_revision": row.parent_revision,
        "status": row.status, "plan_version": value["plan_version"], "steps": steps,
        "limits": value["limits"], "deadline_at": value.get("deadline_at"),
        "source_scope": value["source_scope"], "no_learning": True,
        "authority_frozen": value.get("authority_frozen"),
        "pending_revision": value.get("pending_revision"), "reused_output": value.get("reused_output"),
        "recovery_reason": recovery_reason(value)}


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
            "allowed_hosts": source_model.get("allowed_hosts"), "approved_url_prefixes": source_model.get("approved_url_prefixes"),
            "permissions": ["public_https_browser", "workspace_read", "workspace_write"]},
        "limits": {"max_steps": 4, "max_total_seconds": 300, "max_attempts_per_leaf": 2,
            "max_attempts_total": 6, "browser_seconds": 180, "cpu_seconds": 30, "output_bytes": 65536,
            "quoted_input_bytes": MAX_QUOTED_BYTES, "model_cost": 0},
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
    from src.guardian.opportunity_plans import stage_plan_source
    initial, _value = await owned(db, owner, operation_id)
    source = await WorkBoardRepository().get_task(db, owner, initial.parent_task_id)
    source_witness = None
    if getattr(initial, "opportunity_id", None):
        from src.db.models import GuardianOpportunity
        source_witness = await stage_plan_source(db, await db.get(GuardianOpportunity, initial.opportunity_id))
    context = await stage_accept(db, owner, operation_id, request, source_witness=source_witness)
    await _begin_sqlite_immediate(db)
    result = await _accept_locked(db, owner, operation_id, request, staged_context=context)
    await db.commit()
    await resume_retired_cleanup(db, owner, operation_id)
    return await read(db, owner, operation_id)


async def _accept_locked(db, owner, operation_id, request, *, staged_context: PipelineAcceptContext):
    context = staged_context
    if (not isinstance(context, PipelineAcceptContext) or context.operation_id != operation_id
        or context.owner_principal_id != owner.principal_id or context.original_root_id != owner.session_id
        or context.request_bytes != canonical_bytes(request.model_dump(mode="json"))):
        raise BoardError("pipeline_plan_changed", "The staged acceptance binding changed", status_code=409)
    row, value = await owned(db, owner, operation_id, workspace_identity=context.workspace_identity)
    if row.request_digest != context.proposal_request_digest or row.revision != context.operation_revision or row.proposal_digest != context.operation_digest:
        raise BoardError("pipeline_revision_conflict", "The staged operation changed", status_code=409)
    if getattr(row, "opportunity_id", None):
        from src.guardian.opportunity_plans import recheck_plan_source
        from src.db.models import GuardianOpportunity
        opportunity = await db.get(GuardianOpportunity, row.opportunity_id, populate_existing=True)
        await recheck_plan_source(db, opportunity, source_witness=context.source_witness)
    if value.get("pending_revision"):
        return await accept_revision(db, owner, row, value, request, staged_context=context)
    if row.status == "accepted" and value.get("accepted_digest") == request.expected_digest:
        return await read(db, owner, operation_id, workspace_identity=context.workspace_identity)
    if row.revision != request.expected_revision or row.proposal_digest != request.expected_digest:
        raise BoardError("pipeline_revision_conflict", "Review the exact current operation version", status_code=409)
    repository = WorkBoardRepository()
    source = await repository.get_task(db, owner, row.parent_task_id)
    if source.task_revision != request.expected_parent_revision or row.parent_revision != source.task_revision:
        raise BoardError("stale_revision", "The reviewed source task changed", status_code=409)
    if row.status != "proposed" or utc(row.expires_at) <= now():
        raise BoardError("pipeline_review_expired", "The operation needs a fresh exact review", status_code=409)
    if value.get("reused_output"):
        binding = value["reused_output"]
        goal = await db.scalar(select(Goal).where(Goal.id == binding["goal_id"], Goal.revision == binding["goal_revision"],
            Goal.owner_principal_id == owner.principal_id, Goal.owner_session_id == owner.session_id))
        if goal is None or str(getattr(goal.status, "value", goal.status)) != "active":
            raise BoardError("pipeline_goal_changed", "The freshly reviewed consumer Goal changed", status_code=409)
        require_source_permission(value["source_scope"])
    else:
        goal = await repository.validate_task_goal(db, owner, source)
    from src.goals.repository import deserialize_admission_budget
    budget = deserialize_admission_budget(goal)
    if budget is None or not budget.reviewed_grant:
        raise BoardError("pipeline_goal_budget_required", "Review finite Goal limits before accepting a pipeline", status_code=409)
    admitted_at = now()
    seconds = min(300, int(budget.max_runtime_seconds))
    deadline = admitted_at + timedelta(seconds=seconds)
    if budget.period_expires_at:
        deadline = min(deadline, utc(budget.period_expires_at))
    if deadline <= admitted_at:
        raise BoardError("pipeline_goal_budget_expired", "The current Goal has no remaining grant time", status_code=409)
    value["deadline_at"] = deadline.isoformat()
    value["admitted_at"] = now().isoformat()
    value["accepted_digest"] = request.expected_digest
    previous = source
    if value.get("reused_output"):
        from src.work_board.review import recheck_pipeline_producer_readback
        output = await recheck_pipeline_producer_readback(db, owner, witness=context.producer_witness)
        if output["attempt_id"] != value["reused_output"]["producer_attempt_ref"] or output["content_sha256"] != value["reused_output"]["content_sha256"]:
            raise BoardError("pipeline_reuse_changed", "The independently verified output changed before fresh review", status_code=409)
        # The old source stays on its original operation, with all original
        # attempts, effects, reservations and liabilities. It is never replayed.
    else:
        source.pipeline_operation_id, source.pipeline_slot = operation_id, SLOTS[0]
    for slot, capability in zip(SLOTS[1:], CAPABILITIES[1:]):
        task_request = WorkBoardTaskCreate(goal_id=goal.id,
            goal_revision=goal.revision, title="Evidence dossier" if capability == DOSSIER else "Local evidence report",
            body="Waiting for independently verified producer output; deterministic CPU, no_learning",
            capability_id=capability, status=WorkBoardStatus.triage, priority=source.priority,
            idempotency_scope="pipeline", idempotency_key=f"{operation_id}:{slot}")
        created = await repository._create_task_locked(db, owner, task_request,
            staged_text=dict(context.task_texts)[task_request.idempotency_key])
        child = created.task
        child.pipeline_operation_id, child.pipeline_slot = operation_id, slot
        await db.flush()
        await repository._add_link_locked(db, owner,
            WorkBoardLinkCreate(parent_task_id=previous.task_id, child_task_id=child.task_id,
                expected_child_revision=child.task_revision), staged_context=context)
        value["steps"].append({"slot": slot, "task_ref": child.task_id})
        value["all_task_refs"].append(child.task_id)
        previous = child
    value["versions"].append({"plan_version": 1, "source": value["source"], "steps": list(value["steps"]), "review_digest": request.expected_digest})
    await store(db, row, value, status="accepted")
    return await read(db, owner, operation_id, workspace_identity=context.workspace_identity)


async def reuse_preview(db: Any, owner: WorkBoardOwner, operation_id: str, request: Any) -> dict[str, Any]:
    await _begin_sqlite_immediate(db)
    old, prior = await owned(db, owner, operation_id, revision=request.expected_revision)
    producer = await WorkBoardRepository().get_task(db, owner, prior["steps"][0]["task_ref"])
    if producer.task_revision != request.expected_parent_revision:
        raise BoardError("stale_revision", "The original producer changed before output reuse", status_code=409)
    # This fixed v1 offers completed browser output for the two unfinished CPU
    # consumers. Completed consumers require a separately selected operation.
    consumers = [await WorkBoardRepository().get_task(db, owner, step["task_ref"]) for step in prior["steps"][1:]]
    if any(task.status in {WorkBoardStatus.done, WorkBoardStatus.review} for task in consumers):
        raise BoardError("pipeline_completed_consumer", "This recovery requires unfinished consumers", status_code=409)
    goal = await db.scalar(select(Goal).where(Goal.id == producer.goal_id,
        Goal.owner_principal_id == owner.principal_id, Goal.owner_session_id == owner.session_id))
    if goal is None or str(getattr(goal.status, "value", goal.status)) != "active":
        raise BoardError("pipeline_goal_changed", "The current consumer Goal is unavailable", status_code=409)
    require_source_permission(prior["source_scope"])
    output = await verified_output(db, owner, producer)
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"seraph:pipeline-reuse:{owner.principal_id}:{owner.session_id}:{request.idempotency_key}"))
    binding = {"prior_operation_ref": old.proposal_id, "producer_task_ref": producer.task_id,
        "producer_attempt_ref": output["attempt_id"], "content_sha256": output["content_sha256"],
        "goal_id": goal.id, "goal_revision": goal.revision, "live_root": prior["live_root"]}
    existing = await db.get(WorkBoardProposal, identifier)
    if existing is not None:
        if existing.request_digest != digest(binding):
            raise BoardError("pipeline_idempotency_conflict", "The fresh operation key belongs to another exact output", status_code=409)
        return await read(db, owner, identifier)
    value = {"kind": PIPELINE_KIND, "plan_version": 1, "live_root": prior["live_root"],
        "source": prior["source"], "source_scope": prior["source_scope"], "limits": prior["limits"],
        "steps": [{"slot": SLOTS[0], "task_ref": producer.task_id}], "all_task_refs": [],
        "versions": [], "reservations": {}, "reused_output": binding, "no_learning": True}
    row = WorkBoardProposal(proposal_id=identifier, owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
        parent_task_id=producer.task_id, parent_revision=producer.task_revision, goal_revision=goal.revision,
        kind=PIPELINE_KIND, idempotency_key=request.idempotency_key, request_digest=digest(binding),
        capability_id=PIPELINE_KIND, capability_version="1", status="proposed",
        proposal_json=canonical_bytes(value).decode(), proposal_digest=digest(value), expires_at=now() + timedelta(minutes=5))
    db.add(row)
    await db.flush()
    return await read(db, owner, identifier)


async def stage_revision(db: Any, owner: WorkBoardOwner, operation_id: str, request: Any) -> dict[str, Any]:
    from src.work_board.input_artifacts import read_input_artifact_metadata, resolve_input_artifact_for_copy
    await _begin_sqlite_immediate(db)
    row, value = await owned(db, owner, operation_id)
    if row.status != "accepted" or utc(datetime.fromisoformat(value["deadline_at"])) <= now():
        raise BoardError("pipeline_expired", "A revision cannot renew the original operation deadline", status_code=409)
    if value.get("pending_revision"):
        pending = value["pending_revision"]
        if pending.get("idempotency_key") == request.idempotency_key and pending.get("source_input_artifact_id") == request.source_input_artifact_id and pending.get("request_revision") == request.expected_revision:
            return await read(db, owner, operation_id)
        raise BoardError("pipeline_revision_pending", "The exact pending revision must be resolved first", status_code=409)
    if row.revision != request.expected_revision:
        raise BoardError("pipeline_revision_conflict", "The operation changed before source freeze", status_code=409)
    tasks = [await WorkBoardRepository().get_task(db, owner, step["task_ref"]) for step in value["steps"]]
    if any(task.status in {WorkBoardStatus.done, WorkBoardStatus.review} for task in tasks[1:]):
        raise BoardError("pipeline_completed_consumer", "A completed consumer requires a distinct finite operation", status_code=409)
    goal = await db.scalar(select(Goal).where(Goal.id == tasks[0].goal_id, Goal.owner_principal_id == owner.principal_id,
        Goal.owner_session_id == owner.session_id))
    if goal is None or str(getattr(goal.status, "value", goal.status)) != "active":
        raise BoardError("pipeline_goal_changed", "The current Goal is unavailable", status_code=409)
    artifact = await read_input_artifact_metadata(db, owner, artifact_id=request.source_input_artifact_id)
    if artifact.capability_id != CAPABILITIES[0] or artifact.goal_id != goal.id or artifact.goal_revision != goal.revision or artifact.bound_task_id:
        raise BoardError("pipeline_source_invalid", "Select a fresh current-Goal public-browser input", status_code=409)
    resolved = await resolve_input_artifact_for_copy(db, owner, typed_input_ref=artifact.typed_input_ref,
        typed_input_digest=artifact.typed_input_digest, capability_id=CAPABILITIES[0], goal_id=goal.id, goal_revision=goal.revision)
    snapshots = []
    for task in tasks:
        if task.status not in {WorkBoardStatus.done, WorkBoardStatus.review, WorkBoardStatus.running} and task.block_kind not in {"unknown_effect", "cost_liability", "reconcile_admission_binding"}:
            changed = await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id,
                WorkBoardTask.task_revision == task.task_revision, WorkBoardTask.pipeline_operation_id == operation_id)
                .values(status=WorkBoardStatus.blocked, block_kind="capability", block_reason="pipeline_review_required",
                    block_source_status=task.status.value, task_revision=task.task_revision + 1, updated_at=now())
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise BoardError("pipeline_revision_conflict", "An unfinished task changed during freeze", status_code=409)
            await db.refresh(task)
        snapshots.append({"task_ref": task.task_id, "task_revision": task.task_revision})
    # Only current pointers are invalidated. Old immutable handoff rows and
    # their version/provenance remain addressable for review and recovery.
    await db.execute(update(WorkBoardLink).where(WorkBoardLink.child_task_id.in_([task.task_id for task in tasks[1:]]))
        .values(current_handoff_id=None))
    value["pending_revision"] = {"idempotency_key": request.idempotency_key, "source_input_artifact_id": artifact.artifact_id,
        "request_revision": request.expected_revision,
        "source_sha256": artifact.typed_input_digest, "goal_id": goal.id, "goal_revision": goal.revision,
        "source_scope": {"start_url": resolved.input["start_url"], "allowed_hosts": resolved.input["allowed_hosts"],
            "approved_url_prefixes": resolved.input["approved_url_prefixes"], "permissions": value["source_scope"]["permissions"]},
        "frozen_tasks": snapshots, "proposed_plan_version": value["plan_version"] + 1}
    await store(db, row, value)
    return await read(db, owner, operation_id)


async def quiesce_revision(owner: WorkBoardOwner, operation_id: str, expected_revision: int, *, dispatcher: Any, session_provider: Any) -> dict[str, Any]:
    """Use existing task cancellation; preserve every unresolved liability."""
    async with session_provider() as db:
        row, value = await owned(db, owner, operation_id)
        if not value.get("pending_revision"):
            raise BoardError("pipeline_revision_required", "Freeze a proposed revision before quiescence", status_code=409)
        if value["pending_revision"].get("quiescence_verified") and value["pending_revision"].get("quiescence_request_revision") == expected_revision:
            return await read(db, owner, operation_id)
        if row.revision != expected_revision:
            raise BoardError("pipeline_revision_conflict", "The pending operation changed before cancellation", status_code=409)
        tasks = [await WorkBoardRepository().get_task(db, owner, step["task_ref"]) for step in value["steps"]]
    for task in tasks:
        if task.status == WorkBoardStatus.running:
            await dispatcher.cancel_task(owner, task.task_id, expected_revision=task.task_revision,
                reason="pipeline_revision_requested")
    async with session_provider() as db:
        await _begin_sqlite_immediate(db)
        row, value = await owned(db, owner, operation_id, revision=expected_revision)
        snapshots = []
        for step in value["steps"]:
            task = await WorkBoardRepository().get_task(db, owner, step["task_ref"])
            active = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id,
                WorkBoardAttempt.ended_at.is_(None)))
            if active or task.block_kind in {"unknown_effect", "cost_liability", "reconcile_admission_binding"}:
                raise BoardError("pipeline_quiescence_required", "Original work remains unresolved; reconcile its exact liability", status_code=409)
            snapshots.append({"task_ref": task.task_id, "task_revision": task.task_revision})
        value["pending_revision"]["frozen_tasks"] = snapshots
        value["pending_revision"]["quiescence_verified"] = True
        value["pending_revision"]["quiescence_request_revision"] = expected_revision
        await store(db, row, value)
        await db.commit()
        return await read(db, owner, operation_id)


async def accept_revision(db: Any, owner: WorkBoardOwner, row: WorkBoardProposal, value: dict[str, Any], request: Any, *, staged_context: PipelineAcceptContext) -> dict[str, Any]:
    from src.work_board.input_artifacts import read_input_artifact_metadata, _revoke_input_artifact_locked
    context = staged_context
    if value.get("retired_input_cleanup"):
        raise BoardError("input_artifact_cleanup_required", "Resolve exact retired input cleanup first", status_code=503)
    pending = value["pending_revision"]
    if row.revision != request.expected_revision or row.proposal_digest != request.expected_digest or row.parent_revision != request.expected_parent_revision:
        raise BoardError("pipeline_revision_conflict", "Review the exact current pending plan", status_code=409)
    if utc(datetime.fromisoformat(value["deadline_at"])) <= now():
        raise BoardError("pipeline_expired", "The original operation cannot gain a new deadline", status_code=409)
    old_tasks = [await WorkBoardRepository().get_task(db, owner, step["task_ref"]) for step in value["steps"]]
    if any(task.status in {WorkBoardStatus.done, WorkBoardStatus.review} for task in old_tasks[1:]):
        raise BoardError("pipeline_completed_consumer", "The consumer completed before revision approval", status_code=409)
    active = await db.scalar(select(func.count()).select_from(WorkBoardAttempt).where(
        WorkBoardAttempt.task_id.in_([task.task_id for task in old_tasks]), WorkBoardAttempt.ended_at.is_(None)))
    if active or any(task.block_kind in {"unknown_effect", "cost_liability", "reconcile_admission_binding"} for task in old_tasks):
        raise BoardError("pipeline_quiescence_required", "Cancel and reconcile unfinished work before changing its bindings", status_code=409)
    for task in old_tasks:
        snapshot = next(item for item in pending["frozen_tasks"] if item["task_ref"] == task.task_id)
        if task.task_revision != snapshot["task_revision"] and task.status != WorkBoardStatus.done:
            raise BoardError("pipeline_revision_conflict", "A frozen task changed; read back before continuing", status_code=409)
    artifact = await read_input_artifact_metadata(db, owner, artifact_id=pending["source_input_artifact_id"])
    if artifact.typed_input_digest != pending["source_sha256"] or artifact.bound_task_id:
        raise BoardError("pipeline_source_changed", "The proposed replacement input changed", status_code=409)
    repository = WorkBoardRepository()
    task_request = WorkBoardTaskCreate(goal_id=pending["goal_id"],
        goal_revision=pending["goal_revision"], title="Reviewed replacement public source", status=WorkBoardStatus.todo,
        capability_id=CAPABILITIES[0], input_artifact_id=artifact.artifact_id, priority=old_tasks[0].priority,
        idempotency_scope="pipeline", idempotency_key=f"{row.proposal_id}:{pending['proposed_plan_version']}:{SLOTS[0]}")
    created = await repository._create_task_locked(db, owner, task_request,
        staged_text=dict(context.task_texts)[task_request.idempotency_key], staged_input=context.input_witness)
    source = created.task
    source.pipeline_operation_id, source.pipeline_slot = row.proposal_id, SLOTS[0]
    value["steps"][0] = {"slot": SLOTS[0], "task_ref": source.task_id}
    value["all_task_refs"].append(source.task_id)
    previous = source
    for index, task in enumerate(old_tasks[1:], start=1):
        attempts = await db.scalar(select(func.count()).select_from(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id))
        if attempts:
            task_request = WorkBoardTaskCreate(goal_id=pending["goal_id"],
                goal_revision=pending["goal_revision"], title=task.title, capability_id=CAPABILITIES[index],
                status=WorkBoardStatus.triage, priority=task.priority, idempotency_scope="pipeline",
                idempotency_key=f"{row.proposal_id}:{pending['proposed_plan_version']}:{SLOTS[index]}")
            created = await repository._create_task_locked(db, owner, task_request,
                staged_text=dict(context.task_texts)[task_request.idempotency_key])
            consumer = created.task
            consumer.pipeline_operation_id, consumer.pipeline_slot = row.proposal_id, SLOTS[index]
            value["all_task_refs"].append(consumer.task_id)
            await repository._add_link_locked(db, owner, WorkBoardLinkCreate(parent_task_id=previous.task_id,
                child_task_id=consumer.task_id, expected_child_revision=consumer.task_revision), staged_context=context)
        else:
            consumer = task
            if consumer.input_artifact_id:
                witness = dict(context.retirement_witnesses).get(SLOTS[index])
                revision, metadata_digest = await _revoke_input_artifact_locked(db, owner, witness=witness)
                intent = value.get("retired_input_cleanup")
                if intent is None:
                    intent = {"schema_version": 1, "accepted_digest": request.expected_digest,
                        "accepted_request_digest": digest({"operation_ref": row.proposal_id,
                            "owner_principal_id": owner.principal_id, "original_root_id": owner.session_id,
                            "accept_request": json.loads(context.request_bytes)}),
                        "owner_principal_id": owner.principal_id, "original_root_id": owner.session_id,
                        "workspace_identity_digest": hashlib.sha256(context.workspace_identity).hexdigest(),
                        "plan_version": pending["proposed_plan_version"], "revision_request_key": pending["idempotency_key"],
                        "entries": []}
                    value["retired_input_cleanup"] = intent
                intent["entries"].append({"slot": SLOTS[index], "task_ref": witness.task_id,
                    "task_revision": witness.task_revision, "artifact_ref": witness.artifact_id,
                    "typed_input_ref": witness.typed_input_ref, "content_sha256": witness.content_sha256,
                    "size_bytes": witness.size_bytes, "source_revision": witness.revision,
                    "source_metadata_digest": witness.metadata_digest, "tombstone_revision": revision,
                    "tombstone_metadata_digest": metadata_digest, "state": "pending", "reason_code": None})
                validate_retired_cleanup(intent)
            link = await db.scalar(select(WorkBoardLink).where(WorkBoardLink.child_task_id == consumer.task_id,
                WorkBoardLink.parent_task_id == old_tasks[index - 1].task_id))
            if link is None or link.current_handoff_id:
                raise BoardError("pipeline_revision_conflict", "The frozen current handoff changed", status_code=409)
            link.parent_task_id, link.current_handoff_id = previous.task_id, None
            consumer.input_artifact_id = consumer.typed_input_ref = consumer.typed_input_digest = None
            consumer.goal_revision = pending["goal_revision"]
            consumer.status = WorkBoardStatus.triage
            consumer.block_kind = consumer.block_reason = consumer.block_source_status = None
            consumer.task_revision += 1
        value["steps"][index] = {"slot": SLOTS[index], "task_ref": consumer.task_id}
        previous = consumer
    value["plan_version"] = pending["proposed_plan_version"]
    value["source"] = {"task_ref": source.task_id, "task_revision": source.task_revision, "input_ref": artifact.artifact_id,
        "input_sha256": artifact.typed_input_digest, "goal_id": source.goal_id, "goal_revision": source.goal_revision}
    value["source_scope"] = pending["source_scope"]
    value["reservations"] = {}
    value["versions"].append({"plan_version": value["plan_version"], "source": value["source"], "steps": list(value["steps"]), "review_digest": request.expected_digest})
    value["pending_revision"] = None
    value["authority_frozen"] = None
    value["accepted_digest"] = request.expected_digest
    await store(db, row, value, status="accepted")
    return await read(db, owner, row.proposal_id, workspace_identity=context.workspace_identity)


async def freeze_unfinished(db: Any, row: WorkBoardProposal, value: dict[str, Any], reason: str) -> None:
    """Freeze future claims while in-flight native controls stop and settle."""
    if value.get("authority_frozen"):
        return
    for step in value["steps"]:
        current = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == step["task_ref"]))
        if current is None or current.status in {WorkBoardStatus.done, WorkBoardStatus.review, WorkBoardStatus.running} or current.block_kind in {"unknown_effect", "cost_liability"}:
            continue
        if current.pipeline_operation_id != row.proposal_id:
            continue  # Reused completed producer retains original ownership.
        changed = await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id == current.task_id,
            WorkBoardTask.task_revision == current.task_revision, WorkBoardTask.pipeline_operation_id == row.proposal_id)
            .values(status=WorkBoardStatus.blocked, block_kind="capability", block_reason="pipeline_review_required",
                block_source_status=current.status.value, task_revision=current.task_revision + 1,
                updated_at=now()).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise BoardError("pipeline_revision_conflict", "The unfinished authority freeze lost its exact task CAS", status_code=409)
        await db.refresh(current)
    value["authority_frozen"] = {"reason": reason, "plan_version": value["plan_version"]}
    await store(db, row, value)
    db.info["pipeline_authority_frozen"] = True


async def runtime_guard(task: WorkBoardTask, *, attempt: WorkBoardAttempt | None = None, session_provider: Any = get_session) -> tuple[WorkBoardProposal, dict[str, Any]]:
    """Commit only the guard's authority freeze before returning its rejection.

    The caller owns no terminal/effect transaction here. Catching inside this
    dedicated session prevents get_session from rolling back the freeze.
    """
    failure = None
    result = None
    async with session_provider() as db:
        from src.guardian.opportunity_plans import stage_accepted_plan_task
        source_witness = await stage_accepted_plan_task(db, task, attempt=attempt)
        workspace_identity = canonical_bytes(root_binding())
        active = attempt or await db.scalar(select(WorkBoardAttempt).where(
            WorkBoardAttempt.task_id == task.task_id, WorkBoardAttempt.ended_at.is_(None)))
        try:
            result = await task_guard(db, task, attempt=active,
                workspace_identity=workspace_identity, source_witness=source_witness)
        except BoardError as exc:
            if not db.info.get("pipeline_authority_frozen"):
                raise
            failure = exc
    if failure is not None:
        raise failure
    return result


async def task_guard(db: Any, task: WorkBoardTask, *, attempt: WorkBoardAttempt | None = None,
                     workspace_identity: bytes | None = None, source_witness=None) -> tuple[WorkBoardProposal, dict[str, Any]]:
    owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    if not task.pipeline_operation_id:
        raise BoardError("pipeline_binding_required", "Evidence leaves require an exact reviewed operation", status_code=409)
    row, value = await owned(db, owner, task.pipeline_operation_id, workspace_identity=workspace_identity)
    if getattr(row, "opportunity_id", None):
        from src.guardian.opportunity_plans import recheck_accepted_plan_task
        await recheck_accepted_plan_task(db, task, attempt=attempt, source_witness=source_witness)
    if row.status != "accepted" or value.get("pending_revision") or value.get("authority_frozen"):
        raise BoardError("pipeline_review_required", "Changed unfinished work needs exact plan review", status_code=409)
    try:
        require_source_permission(value["source_scope"])
    except BoardError:
        await freeze_unfinished(db, row, value, "source_permission_changed")
        raise
    if not any(step["task_ref"] == task.task_id and step["slot"] == task.pipeline_slot for step in value["steps"]):
        raise BoardError("pipeline_task_stale", "This task is no longer a current operation step", status_code=409)
    deadline = utc(datetime.fromisoformat(value["deadline_at"]))
    if deadline <= now():
        raise BoardError("pipeline_expired", "The original absolute operation deadline expired", status_code=409)
    try:
        goal = await WorkBoardRepository().validate_task_goal(db, owner, task)
    except BoardError:
        await freeze_unfinished(db, row, value, "goal_changed")
        raise
    from src.goals.repository import deserialize_admission_budget
    budget = deserialize_admission_budget(goal)
    if budget is None or not budget.reviewed_grant:
        raise BoardError("pipeline_goal_budget_required", "The current Goal no longer grants finite work", status_code=409)
    if budget.period_expires_at and utc(budget.period_expires_at) <= now():
        raise BoardError("pipeline_goal_budget_expired", "The current Goal grant expired", status_code=409)
    if task.pipeline_slot == SLOTS[0] and task.typed_input_digest != value["source"]["input_sha256"]:
        await freeze_unfinished(db, row, value, "source_input_changed")
        raise BoardError("pipeline_source_changed", "The reviewed public-source input changed", status_code=409)
    if task.pipeline_slot in SLOTS[1:] and task.input_artifact_id:
        reservation = value["reservations"].get(task.pipeline_slot)
        if not isinstance(reservation, Mapping) or reservation.get("state") != "bound" or reservation.get("artifact_ref") != task.input_artifact_id:
            raise BoardError("pipeline_input_changed", "The reviewed consumer reservation changed", status_code=409)
    counts = await db.execute(select(WorkBoardTask.pipeline_slot, func.count()).join(
        WorkBoardAttempt, WorkBoardAttempt.task_id == WorkBoardTask.task_id).where(
        WorkBoardTask.task_id.in_(value["all_task_refs"])).group_by(WorkBoardTask.pipeline_slot))
    counts = dict(counts.all())
    allowance = 0 if attempt is not None else 1
    if sum(counts.values()) + allowance > 6 or counts.get(task.pipeline_slot, 0) + allowance > min(2, budget.max_attempts):
        raise BoardError("pipeline_attempts_exhausted", "The original finite operation attempt allowance is exhausted", status_code=409)
    return row, value


def require_source_permission(scope: Mapping[str, Any]) -> None:
    from src.security.site_policy import evaluate_site_access
    hosts = scope.get("allowed_hosts")
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 8:
        raise BoardError("pipeline_source_permission", "The reviewed source scope is invalid", status_code=409)
    if any(not isinstance(host, str) or not evaluate_site_access(host, resolve_dns=False).allowed for host in hosts):
        raise BoardError("pipeline_source_permission", "Current source policy no longer permits this output", status_code=409)


async def validate_cpu_current(task: WorkBoardTask, attempt: WorkBoardAttempt, inputs: Mapping[str, Any], *, session_provider: Any = get_session) -> None:
    failure = None
    async with session_provider() as db:
        from src.guardian.opportunity_plans import stage_accepted_plan_task
        source_witness = await stage_accepted_plan_task(db, task, attempt=attempt)
        try:
            await validate_cpu_binding(db, task, attempt, inputs, source_witness=source_witness)
        except BoardError as exc:
            if not db.info.get("pipeline_authority_frozen"):
                raise
            failure = exc
    if failure is not None:
        raise failure


async def validate_cpu_binding(db: Any, task: WorkBoardTask, attempt: WorkBoardAttempt, inputs: Mapping[str, Any], *, source_witness=None) -> None:
    from src.work_board import review
    from src.work_board.pipeline_cpu import read_output
    model = EvidenceConsumerInput.model_validate(dict(inputs))
    current = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
    active = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt.attempt_id))
    if current is None or active is None or active.ended_at or active.cancel_requested_at or current.task_revision != task.task_revision or current.status != WorkBoardStatus.running:
        raise BoardError("pipeline_task_changed", "The consumer was cancelled or changed", status_code=409)
    row, value = await task_guard(db, current, attempt=active, source_witness=source_witness)
    if model.operation_ref != row.proposal_id or model.plan_version != value["plan_version"]:
        raise BoardError("pipeline_plan_changed", "The consumer input belongs to another reviewed plan", status_code=409)
    parent = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == model.producer_task_ref))
    link = await db.scalar(select(WorkBoardLink).where(WorkBoardLink.parent_task_id == model.producer_task_ref,
        WorkBoardLink.child_task_id == task.task_id))
    owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    if parent is None or link is None or link.current_handoff_id != model.handoff_ref or not await review.current_handoff_is_verified(db, owner, parent, current, link):
        raise BoardError("pipeline_handoff_changed", "The exact verified producer binding changed", status_code=409)
    handoff = await db.get(WorkBoardHandoff, model.handoff_ref)
    if handoff is None or handoff.source_attempt_id != model.producer_attempt_ref:
        raise BoardError("pipeline_producer_changed", "The producer attempt changed", status_code=409)
    output = await verified_output(db, owner, parent)
    raw = read_output(output["file_path"], output["content_sha256"])
    if output["content_sha256"] != model.producer_sha256 or raw.decode("utf-8") != model.quoted_source_data:
        raise BoardError("pipeline_source_changed", "The admitted quoted source differs from verified producer bytes", status_code=409)


@dataclass(frozen=True)
class PipelineTerminalWitness:
    owner_principal_id: str
    original_root_id: str
    workspace_identity: bytes
    source_witness: Any
    operation_id: str
    operation_revision: int
    operation_digest: str
    task_id: str
    task_token: str
    attempt_id: str
    attempt_token: str
    input_id: str
    input_token: str
    link_id: str
    link_token: str
    handoff_id: str
    handoff_token: str
    producer_witness: Any
    run_identity: str
    run_token: str
    input_digest: str
    output_reference: str
    output_digest: str


async def stage_cpu_terminal(task, attempt, inputs, *, output_reference, output_digest, session_provider=get_session):
    from src.guardian.opportunity_plans import stage_accepted_plan_task
    from src.work_board.review import stage_pipeline_producer_readback
    from src.work_board.pipeline_cpu import read_output
    async with session_provider() as db:
        source_witness = await stage_accepted_plan_task(db, task, attempt=attempt)
        await validate_cpu_binding(db, task, attempt, inputs, source_witness=source_witness)
        current = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id).execution_options(populate_existing=True))
        active = await db.get(WorkBoardAttempt, attempt.attempt_id, populate_existing=True)
        row, value = await owned(db, WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id), task.pipeline_operation_id)
        model = EvidenceConsumerInput.model_validate(dict(inputs))
        parent = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == model.producer_task_ref).execution_options(populate_existing=True))
        producer_witness = await stage_pipeline_producer_readback(db,
            WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id), parent)
        artifact = await db.get(WorkBoardInputArtifact, current.input_artifact_id)
        link = await db.scalar(select(WorkBoardLink).where(WorkBoardLink.parent_task_id == parent.task_id, WorkBoardLink.child_task_id == current.task_id))
        handoff = await db.get(WorkBoardHandoff, model.handoff_ref)
        from src.work_board.pipeline_cpu import job_id
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id(task, attempt)))
        raw = read_output(output_reference, output_digest)
        if run is None or run.status != "running" or not any(item.get("file_path") == output_reference and item.get("content_sha256") == output_digest and item.get("exists") is True for item in json.loads(run.artifact_receipts_json)):
            raise BoardError("pipeline_output_unverified", "The actual CPU artifact is required", status_code=409)
        if not any(item.get("receipt_kind") == "readback" and item.get("status") == "succeeded" and item.get("target_path") == output_reference and item.get("content_sha256") == output_digest for item in json.loads(run.effect_receipts_json)):
            raise BoardError("pipeline_output_unverified", "The actual CPU readback is required", status_code=409)
        return PipelineTerminalWitness(task.owner_principal_id, task.owner_session_id, canonical_bytes(value["live_root"]),
            source_witness, row.proposal_id, row.revision, row.proposal_digest, current.task_id, row_token(current),
            active.attempt_id, row_token(active), artifact.artifact_id, row_token(artifact), link.link_id, row_token(link),
            handoff.handoff_id, row_token(handoff), producer_witness, run.run_identity, row_token(run), digest(inputs),
            output_reference, hashlib.sha256(raw).hexdigest())


async def recheck_cpu_terminal_locked(db, run, *, witness: PipelineTerminalWitness):
    from src.work_board.review import recheck_pipeline_producer_readback
    if not isinstance(witness, PipelineTerminalWitness):
        raise BoardError("pipeline_output_unverified", "Staged terminal proof is required", status_code=409)
    owner = WorkBoardOwner(principal_id=witness.owner_principal_id, session_id=witness.original_root_id)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == witness.task_id).execution_options(populate_existing=True))
    attempt = await db.get(WorkBoardAttempt, witness.attempt_id, populate_existing=True)
    for cls, identifier, token in ((WorkBoardTask, witness.task_id, witness.task_token),
        (WorkBoardAttempt, witness.attempt_id, witness.attempt_token),
        (WorkBoardInputArtifact, witness.input_id, witness.input_token),
        (WorkBoardLink, witness.link_id, witness.link_token), (WorkBoardHandoff, witness.handoff_id, witness.handoff_token)):
        current = await db.scalar(select(cls).where(cls.task_id == identifier).execution_options(populate_existing=True)) if cls is WorkBoardTask else await db.get(cls, identifier, populate_existing=True)
        if current is None or row_token(current) != token:
            raise BoardError("pipeline_input_changed", "The exact CPU terminal binding changed", status_code=409)
    row, value = await task_guard(db, task, attempt=attempt, workspace_identity=witness.workspace_identity,
        source_witness=witness.source_witness)
    if (row.revision != witness.operation_revision or row.proposal_digest != witness.operation_digest
        or run.run_identity != witness.run_identity or row_token(run) != witness.run_token
        or task.status != WorkBoardStatus.running or attempt.ended_at or attempt.cancel_requested_at
        or attempt.lease_expires_at is None or utc(attempt.lease_expires_at) <= now()
        or attempt.workflow_run_id != run.run_identity):
        raise BoardError("pipeline_task_changed", "The native CPU terminal fence changed", status_code=409)
    await recheck_pipeline_producer_readback(db, owner, witness=witness.producer_witness)


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
        if producer.capability_id != REPORT and len(raw) > MAX_QUOTED_BYTES:
            raise BoardError("pipeline_output_too_large", "The producer exceeds the finite consumer input allowance", status_code=409)
        return {"file_path": ref, "content_sha256": sha, "attempt_id": attempt.attempt_id,
                "quoted_source_data": raw.decode("utf-8")}
    raise BoardError("pipeline_output_unverified", "No exact settled artifact/readback pair is available", status_code=409)


async def advance(db: Any, owner: WorkBoardOwner, operation_id: str, expected_revision: int) -> dict[str, Any]:
    from src.work_board.input_artifacts import prepare_input_artifact, bind_input_artifact, stage_input_artifact, recheck_staged_input
    from src.work_board.review import stage_pipeline_producer_readback, recheck_pipeline_producer_readback, _materialize_pipeline_handoff_locked
    from src.guardian.opportunity_plans import stage_accepted_plan_task
    from src.work_board.dispatcher import registered_executor_id
    row, value = await owned(db, owner, operation_id)
    workspace = canonical_bytes(value["live_root"])
    if value.get("retired_input_cleanup"):
        raise BoardError("input_artifact_cleanup_required", "Resolve retired inputs before materialization", status_code=503)
    retained = value.get("advance_request")
    if row.revision != expected_revision:
        if not isinstance(retained, dict) or retained.get("expected_revision") != expected_revision or retained.get("plan_version") != value["plan_version"]:
            raise BoardError("pipeline_revision_conflict", "The materialization request belongs to another operation version", status_code=409)
        if retained.get("state") == "completed":
            return await read(db, owner, operation_id)
    changed_any = False
    for index in (1, 2):
        producer = await WorkBoardRepository().get_task(db, owner, value["steps"][index - 1]["task_ref"])
        consumer = await WorkBoardRepository().get_task(db, owner, value["steps"][index]["task_ref"])
        if consumer.input_artifact_id or consumer.status != WorkBoardStatus.triage:
            continue
        source_witness = await stage_accepted_plan_task(db, consumer)
        await task_guard(db, consumer, workspace_identity=workspace, source_witness=source_witness)
        if producer.status != WorkBoardStatus.done:
            continue
        producer_witness = await stage_pipeline_producer_readback(db, owner, producer)
        staged_revision, staged_digest = row.revision, row.proposal_digest
        consumer_token = row_token(consumer)
        await _begin_sqlite_immediate(db)
        row, value = await owned(db, owner, operation_id, revision=staged_revision, workspace_identity=workspace)
        if row.proposal_digest != staged_digest:
            raise BoardError("pipeline_materialization_conflict", "The operation staging changed", status_code=409)
        consumer = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == consumer.task_id).execution_options(populate_existing=True))
        if row_token(consumer) != consumer_token:
            raise BoardError("pipeline_task_changed", "The deferred consumer changed", status_code=409)
        await task_guard(db, consumer, workspace_identity=workspace, source_witness=source_witness)
        output = await recheck_pipeline_producer_readback(db, owner, witness=producer_witness)
        producer = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == producer.task_id).execution_options(populate_existing=True))
        link = await db.scalar(select(WorkBoardLink).where(WorkBoardLink.parent_task_id == producer.task_id, WorkBoardLink.child_task_id == consumer.task_id))
        if link is None:
            raise BoardError("pipeline_handoff_changed", "The fixed dependency is unavailable", status_code=409)
        handoff = await _materialize_pipeline_handoff_locked(db, owner, producer, consumer, link, witness=producer_witness)
        inputs = EvidenceConsumerInput(schema_version=1, operation_ref=operation_id, plan_version=value["plan_version"],
            producer_task_ref=producer.task_id, producer_attempt_ref=output["attempt_id"], handoff_ref=handoff.handoff_id,
            producer_sha256=output["content_sha256"], producer_schema="browser_public_task_result" if index == 1 else "evidence_dossier.v1",
            quoted_source_data=output["quoted_source_data"], no_learning=True)
        key = f"{operation_id}:{value['plan_version']}:{output['attempt_id']}:{SLOTS[index]}"
        expected_task_revision = consumer.task_revision
        reservation = {"key": key, "input_sha256": digest(inputs.model_dump(mode="json")),
            "producer_attempt_ref": output["attempt_id"], "consumer_revision": expected_task_revision, "state": "reserved"}
        old = value["reservations"].get(SLOTS[index])
        if old is not None and old != reservation:
            raise BoardError("pipeline_materialization_conflict", "The durable reservation no longer has its exact binding", status_code=409)
        value["reservations"][SLOTS[index]] = reservation
        value["advance_request"] = {"expected_revision": expected_revision, "plan_version": value["plan_version"], "state": "reserved"}
        await store(db, row, value)
        await db.commit()  # reservation precedes filesystem materialization
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(schema_version=1,
            capability_id=consumer.capability_id, goal_id=consumer.goal_id, goal_revision=consumer.goal_revision,
            input=inputs.model_dump(mode="json"), idempotency_key=key))
        input_witness = await stage_input_artifact(db, owner, artifact_id=artifact.artifact_id,
            capability_id=consumer.capability_id, goal_id=consumer.goal_id, goal_revision=consumer.goal_revision)
        source_witness = await stage_accepted_plan_task(db, consumer)
        await recheck_pipeline_producer_readback(db, owner, witness=producer_witness)
        reserved_revision = row.revision
        await _begin_sqlite_immediate(db)
        row, current = await owned(db, owner, operation_id, revision=reserved_revision, workspace_identity=workspace)
        consumer = await WorkBoardRepository().get_task(db, owner, consumer.task_id)
        await task_guard(db, consumer, workspace_identity=workspace, source_witness=source_witness)
        await recheck_pipeline_producer_readback(db, owner, witness=producer_witness)
        if current["reservations"].get(SLOTS[index]) != value["reservations"][SLOTS[index]] or consumer.task_revision != expected_task_revision or consumer.status != WorkBoardStatus.triage:
            raise BoardError("pipeline_materialization_conflict", "The exact consumer reservation changed", status_code=409)
        next_revision = expected_task_revision + 1
        input_request = WorkBoardTaskCreate(title=consumer.title, input_artifact_id=artifact.artifact_id,
            capability_id=consumer.capability_id, goal_id=consumer.goal_id, goal_revision=consumer.goal_revision)
        resolved = await recheck_staged_input(db, owner, input_request, witness=input_witness)
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
        await db.refresh(consumer)
        current["reservations"][SLOTS[index]]["state"] = "bound"
        current["reservations"][SLOTS[index]]["artifact_ref"] = artifact.artifact_id
        current["advance_request"]["state"] = "completed"
        await store(db, row, current)
        value = current
        changed_any = True
        await db.commit()
    return await read(db, owner, operation_id, workspace_identity=workspace)
