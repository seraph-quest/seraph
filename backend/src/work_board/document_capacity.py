"""One bounded SQLite document-process gate on the existing rows/jobs.

Physical candidate proofs are staged before BEGIN. A changed candidate inventory
requires restaging; uncertain process ownership never expires into free capacity.
"""
from dataclasses import dataclass, field
from types import MappingProxyType
import json
import re

from sqlalchemy import select, or_, func

from src.db.models import WorkBoardAttempt, WorkBoardInputArtifact, WorkBoardTask, WorkflowRunState
from src.work_board.repository import BoardError
from src.work_board.document_compare_parser import JOB_KIND, canonical, sha256

_SEAL = object()
_HEX = re.compile(r"^[0-9a-f]{64}$")


def _blocked(code, message):
    return BoardError(code, message, status_code=409)


def _records(run):
    try:
        items = json.loads(run.checkpoint_receipts_json)
        if type(items) is not list or len(items) > 100 or any(type(item) is not dict for item in items):
            raise ValueError()
        result = {}
        for item in items:
            name = item.get("checkpoint_id")
            if name in {"document-capacity", "document-reaped", "document-child"}:
                if name in result or type(item.get("payload")) is not dict:
                    raise ValueError()
                result[name] = item["payload"]
        return result
    except (ValueError, TypeError):
        raise _blocked("document_capacity_inventory_invalid", "Reconcile malformed document capacity history") from None


def _held(run, *, cancellation=None):
    records = _records(run)
    capacity, reaped = records.get("document-capacity"), records.get("document-reaped")
    if capacity is None:
        if reaped is not None:
            raise _blocked("document_capacity_inventory_invalid", "A reap has no original reservation")
        return False
    authority = json.loads(run.declared_authority_json)
    if run.job_kind == JOB_KIND:
        if (set(capacity) != {"job_id", "input_digest", "input_artifact_id", "generation", "nonce"}
                or capacity["job_id"] != run.run_identity
                or capacity["input_digest"] != authority.get("typed_input_digest")
                or capacity["input_artifact_id"] != authority.get("input_artifact_id")
                or type(capacity["generation"]) is not int or capacity["generation"] not in {1, 2}
                or not re.fullmatch(r"[0-9a-f]{32}", str(capacity["nonce"]))):
            raise _blocked("document_capacity_inventory_invalid", "The original comparison reservation changed")
    else:
        from src.work_board.document_build_native import validate_capacity_record
        validate_capacity_record(run, capacity, _cancel_context=cancellation)
    if reaped is None:
        return True
    if (set(reaped) != {"binding", "witness_sha256", "wait_reaped", "parser_exit"}
            or reaped["binding"] != records.get("document-child", capacity) or reaped["wait_reaped"] is not True
            or type(reaped["parser_exit"]) is not int
            or type(reaped["witness_sha256"]) is not str or not _HEX.fullmatch(reaped["witness_sha256"])):
        raise _blocked("document_capacity_inventory_invalid", "The original positive reap binding changed")
    if any(reaped["binding"].get(key) != value for key, value in capacity.items()):
        raise _blocked("document_capacity_inventory_invalid", "Positive reap cannot replace the original reservation")
    return False


async def _inventory(db):
    from src.work_board.document_pairs import metadata
    charged = list((await db.scalars(select(WorkBoardInputArtifact).where(
        WorkBoardInputArtifact.document_reserved_bytes > 0))).all())
    # metadata dispatch fails closed for unknown/malformed charged families.
    builds = []
    for row in charged:
        metadata(row)
        if row.capability_id == "document.build.v1" and row.bound_task_id:
            builds.append(row.bound_task_id)
    parent_ids = select(WorkBoardAttempt.workflow_run_id).where(WorkBoardAttempt.task_id.in_(builds))
    safe_arguments = func.json_extract(func.iif(func.json_valid(WorkflowRunState.arguments_json),
        WorkflowRunState.arguments_json, "{}"), "$.tool_id")
    rows = list((await db.scalars(select(WorkflowRunState).where(or_(
        WorkflowRunState.job_kind == JOB_KIND,
        (WorkflowRunState.job_kind == "general_task_native_tool_v1") & or_(
            safe_arguments == "document_build", WorkflowRunState.parent_job_id.in_(parent_ids)))
    ).limit(4097))).all())
    if len(rows) > 4096:
        raise _blocked("document_capacity_history_full", "The bounded shared document inventory needs reconciliation")
    return charged, rows


def _identity(run):
    return sha256(canonical({"job": run.run_identity, "kind": run.job_kind,
        "authority": run.declared_authority_json, "arguments": run.arguments_json,
        "input_digest": run.input_digest, "artifacts": run.artifact_receipts_json,
        "effects": run.effect_receipts_json,
        "checkpoints": run.checkpoint_receipts_json, "attempt_count": run.attempt_count,
        "fence": run.fencing_token, "status": run.status,
        "priority": run.priority, "started_at": run.started_at.isoformat()}))


@dataclass(frozen=True)
class DocumentCapacitySnapshot:
    _seal: object
    identities: dict
    ready_proofs: dict
    _issued_id: int = field(default=0, repr=False)


def _comparison_ready(run):
    if run.lease_owner or run.lease_expires_at:
        return False
    if run.attempt_count == 0 and run.fencing_token == 0:
        return True
    # Preserve the existing fixed, positively reaped first-generation retry.
    history = json.loads(run.checkpoint_receipts_json)
    retry = [item.get("payload") for item in history if item.get("checkpoint_id") == "document-parser-retry"]
    prior = [item.get("payload") for item in history if item.get("checkpoint_id") == "document-history:1"]
    if run.attempt_count != 1 or run.fencing_token != 1 or run.max_attempts != 2 or len(retry) != 1 or len(prior) != 1:
        return False
    binding = prior[0].get("document-child", prior[0].get("document-capacity", {}))
    reaped = prior[0].get("document-reaped", {})
    return (retry[0].get("generation") == 2 and retry[0].get("no_learning") is True
        and retry[0].get("original_nonce") == binding.get("nonce")
        and retry[0].get("witness_sha256") == reaped.get("witness_sha256")
        and reaped.get("wait_reaped") is True and reaped.get("binding") == binding)


async def stage_capacity(db, *, service=None):
    """Stage only actual comparison/build contenders, never arbitrary C1 jobs."""
    from src.work_board import document_compare_native as comparison
    _charged, rows = await _inventory(db)
    proofs = {}
    for run in rows:
        if run.job_kind == JOB_KIND:
            _held(run)
        else:
            from src.work_board.document_build_native import held_capacity
            await held_capacity(db,run)
        if run.status not in {"accepted", "queued"}:
            continue
        if run.job_kind == JOB_KIND:
            try:
                authority = json.loads(run.declared_authority_json)
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == authority["board_task_id"]))
                attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == authority["board_attempt_id"]))
                if task is None or attempt is None or comparison.job_id(task, attempt) != run.run_identity:
                    raise ValueError()
                # Generic durable arguments are intentionally redacted. The
                # canonical sealed pair owns the original reference-only input.
                source = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
                from src.work_board.document_pairs import metadata
                inputs = metadata(source)["input"]
                staged = await comparison.stage(db, task, attempt, run, inputs)
                await comparison.current(db, task, attempt, run, staged)
                if run.priority != task.priority or not _comparison_ready(run):
                    raise ValueError()
                if run.attempt_count:
                    from src.work_board.document_compare_control import witness
                    history = {item["checkpoint_id"]: item.get("payload")
                        for item in json.loads(run.checkpoint_receipts_json)}
                    prior = history["document-history:1"]
                    original = prior.get("document-child", prior.get("document-capacity"))
                    actual, witness_sha = witness(original)
                    if (actual["reason"] != "document_supervisor_interrupted" or actual["parser_exit"] != -9
                            or witness_sha != history["document-parser-retry"]["witness_sha256"]):
                        raise ValueError()
                proofs[run.run_identity] = (task, attempt, staged)
            except (ValueError, KeyError, TypeError):
                raise _blocked("document_capacity_inventory_invalid", "A queued comparison lacks its original binding") from None
        else:
            from src.work_board.document_build_native import stage_candidate
            proofs[run.run_identity] = await stage_candidate(db, run, service=service)
    result = DocumentCapacitySnapshot(_SEAL,
        MappingProxyType({run.run_identity: _identity(run) for run in rows}), MappingProxyType(proofs))
    object.__setattr__(result, "_issued_id", id(result))
    return result


async def assert_capacity(db, *, snapshot, run=None):
    """Metadata-only gate in the SAME serialized reservation/claim writer."""
    from src.work_board.document_pairs import metadata
    from src.work_board import document_compare_native as comparison
    if type(snapshot) is not DocumentCapacitySnapshot or snapshot._seal is not _SEAL or snapshot._issued_id != id(snapshot):
        raise _blocked("document_capacity_stage_required", "Original private candidate staging is required")
    charged, rows = await _inventory(db)
    if {item.run_identity: _identity(item) for item in rows} != snapshot.identities:
        raise _blocked("document_capacity_inventory_changed", "Restage the changed shared document candidates")
    if any(metadata(item).get("live_writer") for item in charged):
        raise _blocked("document_parser_capacity_held", "An original document writer still requires positive closure")
    for other in rows:
        if other.job_kind == JOB_KIND:
            held = _held(other)
        else:
            from src.work_board.document_build_native import held_capacity
            held = await held_capacity(db,other)
        if held:
            raise _blocked("document_parser_capacity_held", "An original document process still requires positive reap")
        if other.status != "queued":
            continue
        proof = snapshot.ready_proofs.get(other.run_identity)
        if proof is None:
            raise _blocked("document_capacity_inventory_changed", "Restage a newly queued document contender")
        if other.job_kind == JOB_KIND:
            task, attempt, staged = proof
            await comparison.current(db, task, attempt, other, staged)
            if other.priority != task.priority or not _comparison_ready(other):
                raise _blocked("document_capacity_inventory_invalid", "The original queued comparison changed")
        else:
            from src.work_board.document_build_native import recheck_candidate
            await recheck_candidate(db, other, proof)
        if run is not None and other.run_identity != run.run_identity and (
                other.priority, other.started_at, other.run_identity) < (run.priority, run.started_at, run.run_identity):
            raise _blocked("document_higher_priority_ready", "A higher priority original document job owns the next turn")
