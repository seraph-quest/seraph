"""Same-card review and bounded recovery orchestration for the work board.

This module owns operator review policy, while ``WorkBoardRepository`` remains
the CAS/event kernel and the durable workflow projection remains execution
authority.  The functions intentionally accept an already authenticated owner
and never trust reviewer, run, or evidence identities supplied by a browser.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import hashlib
import json
import re
import asyncio
from weakref import WeakKeyDictionary
from typing import Any, Mapping

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.db.models import (
    WorkBoardAttempt,
    WorkBoardComment,
    WorkBoardEvent,
    WorkBoardHandoff,
    WorkBoardLink,
    WorkBoardReviewIntent,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import (
    BoardError,
    BoardMutation,
    BoardRevisionConflict,
    WorkBoardRepository,
    effective_browser_limits,
    _SAFE_RESTORABLE_PHASES,
    _begin_sqlite_immediate,
    _safe_receipt_refs,
    _validate_safe_identifier,
)


REVIEW_TTL = timedelta(days=7)
_VERIFIED_RECEIPT_STATUSES = frozenset({"succeeded", "read_back", "reconciled"})
_SAFE_WORKFLOW_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,512}$")
_SAFE_RECEIPT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_SAFE_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_EFFECT_DIGEST = re.compile(r"^[0-9a-f]{16}$")
_SAFE_RISKS = frozenset(
    {
        "external_state_may_change_after_readback",
        "source_snapshot_time_bound",
        "verification_scope_bounded",
    }
)
_HANDOFF_RECONCILIATION_REASON = (
    "A completed parent handoff needs verified readback reconciliation before dispatch"
)
_LEGACY_HANDOFF_RECONCILIATION_REASON = (
    "A completed parent handoff is missing, stale, or no longer independently verified"
)
_LEGACY_HANDOFF_MISSING_REASON = (
    "Every completed parent must have a verified handoff before dispatch"
)


@dataclass(frozen=True)
class PipelineProducerWitness:
    owner_principal_id: str
    original_root_id: str
    task_id: str
    task_token: str
    attempt_id: str
    attempt_token: str
    run_identity: str
    run_token: str
    input_artifact_id: str
    input_artifact_token: str
    proof_bytes: bytes
    output_bytes: bytes
    output_reference: str
    content_sha256: str


class _DispatchReadbacks:
    """Identity-only handle for this original repository call's physical stage."""


_DISPATCH_READBACKS = WeakKeyDictionary()


async def _stage_dispatch_handoffs(db, owner, task):
    """Actual parent files before the selected repository writer, never a grant."""
    from src.work_board.pipelines import row_token
    links = list((await db.scalars(select(WorkBoardLink).where(
        WorkBoardLink.child_task_id == task.task_id,
        WorkBoardLink.owner_principal_id == owner.principal_id,
        WorkBoardLink.owner_session_id == owner.session_id))).all())
    staged = {}
    for link in links:
        parent = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == link.parent_task_id))
        if parent is None or parent.status != WorkBoardStatus.done:
            continue
        if parent.capability_id not in {"browser.public-task.v1", "work.evidence-dossier.v1", "work.local-evidence-report.v1"}:
            raise BoardError("pipeline_producer_changed", "This parent retains its original readback owner")
        witness = await stage_pipeline_producer_readback(db, owner, parent)
        handoff = await db.get(WorkBoardHandoff, link.current_handoff_id) if link.current_handoff_id else None
        staged[parent.task_id] = (witness, row_token(link), row_token(handoff) if handoff else None)
    handle = _DispatchReadbacks()
    _DISPATCH_READBACKS[handle] = (db, task.task_id, asyncio.current_task(), staged)
    return handle


async def _dispatch_handoff_proof(db, owner, parent, child, link, handle):
    from src.work_board.pipelines import row_token, canonical_bytes
    registration = _DISPATCH_READBACKS.get(handle) if type(handle) is _DispatchReadbacks else None
    if (registration is None or registration[0] is not db or registration[1] != child.task_id
        or registration[2] is not asyncio.current_task()):
        raise BoardError("pipeline_producer_changed", "Original dispatch readback stage required")
    held = registration[3].get(parent.task_id)
    if held is None:
        return None
    witness, link_token, handoff_token = held
    handoff = await db.get(WorkBoardHandoff, link.current_handoff_id, populate_existing=True) if link.current_handoff_id else None
    if row_token(link) != link_token or (row_token(handoff) if handoff else None) != handoff_token:
        raise BoardError("pipeline_producer_changed", "The exact original handoff changed")
    await recheck_pipeline_producer_readback(db, owner, witness=witness)
    proof = json.loads(witness.proof_bytes)
    if canonical_bytes(proof) != witness.proof_bytes or proof.get("content_sha256") != witness.content_sha256:
        raise BoardError("pipeline_producer_changed", "The staged original proof changed")
    return proof


async def _stage_dispatch_projection(db, owner, task, attempt, run, proof):
    """Verify actual completed output before the original projection writer."""
    from src.work_board.pipelines import row_token
    from src.work_board.pipeline_cpu import read_output
    from src.db.models import WorkBoardInputArtifact
    if (attempt is None or run is None or run.status != "succeeded"
        or not isinstance(proof, Mapping) or proof.get("workflow_run_id") != run.run_identity
        or attempt.workflow_run_id != run.run_identity):
        raise BoardError("pipeline_output_unverified", "Actual original terminal run required")
    digest = proof.get("content_sha256")
    artifacts = [item for item in _decode_list(run.artifact_receipts_json)
        if isinstance(item, dict) and item.get("exists") is True and item.get("content_sha256") == digest]
    if len(artifacts) != 1 or not any(isinstance(item, dict)
        and item.get("receipt_kind") == "readback" and item.get("status") == "succeeded"
        and item.get("target_path") == artifacts[0].get("file_path") and item.get("content_sha256") == digest
        and item.get("readback_id") == proof.get("readback_id")
        and item.get("verified_at") == proof.get("verified_at")
        for item in _decode_list(run.effect_receipts_json)):
        raise BoardError("pipeline_output_unverified", "Exact settled original readback required")
    raw = read_output(artifacts[0]["file_path"], digest)
    artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
    handle = _DispatchReadbacks()
    _DISPATCH_READBACKS[handle] = (db, task.task_id, asyncio.current_task(), {
        "projection": (row_token(task), row_token(attempt), row_token(run),
            row_token(artifact) if artifact else None, hashlib.sha256(raw).hexdigest(), dict(proof))})
    return handle


async def _recheck_dispatch_projection(db, task, attempt, run, proof, handle):
    from src.work_board.pipelines import row_token
    from src.db.models import WorkBoardInputArtifact
    registration = _DISPATCH_READBACKS.get(handle) if type(handle) is _DispatchReadbacks else None
    if (registration is None or registration[0] is not db or registration[1] != task.task_id
        or registration[2] is not asyncio.current_task()):
        raise BoardError("pipeline_output_unverified", "Original projection stage required")
    held = registration[3].get("projection")
    artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id, populate_existing=True)
    current = (row_token(task), row_token(attempt), row_token(run), row_token(artifact) if artifact else None,
        proof.get("content_sha256"), dict(proof))
    if held != current:
        raise BoardError("pipeline_output_unverified", "The actual original projection binding changed")


async def stage_pipeline_producer_readback(db, owner, producer) -> PipelineProducerWitness:
    from src.work_board.pipelines import row_token
    from src.work_board.pipeline_cpu import read_output
    from src.work_board.pipeline_contracts import canonical_bytes, MAX_QUOTED_BYTES, REPORT
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == producer.task_id)
        .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))
    proof = await _verified_workflow_readback(db, producer, attempt) if attempt else None
    if (producer.status != WorkBoardStatus.done or producer.owner_principal_id != owner.principal_id
        or producer.owner_session_id != owner.session_id or proof is None):
        raise BoardError("pipeline_output_unverified", "The real producer readback is required", status_code=409)
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
    artifacts = _decode_list(run.artifact_receipts_json)
    matching = [item for item in artifacts if isinstance(item, dict) and item.get("exists") is True
        and item.get("content_sha256") == proof["content_sha256"]]
    if len(matching) != 1:
        raise BoardError("pipeline_output_unverified", "The producer artifact is ambiguous", status_code=409)
    reference = matching[0]["file_path"]
    raw = read_output(reference, proof["content_sha256"])
    if producer.capability_id != REPORT and len(raw) > MAX_QUOTED_BYTES:
        raise BoardError("pipeline_output_too_large", "The producer exceeds the input allowance", status_code=409)
    from src.db.models import WorkBoardInputArtifact
    input_artifact = await db.get(WorkBoardInputArtifact, producer.input_artifact_id)
    if input_artifact is None:
        raise BoardError("pipeline_input_changed", "The producer input binding is unavailable", status_code=409)
    return PipelineProducerWitness(owner.principal_id, owner.session_id, producer.task_id, row_token(producer),
        attempt.attempt_id, row_token(attempt), run.run_identity, row_token(run), input_artifact.artifact_id,
        row_token(input_artifact), canonical_bytes(proof),
        raw, reference, proof["content_sha256"])


async def recheck_pipeline_producer_readback(db, owner, *, witness: PipelineProducerWitness):
    from src.work_board.pipelines import row_token
    if not isinstance(witness, PipelineProducerWitness) or (witness.owner_principal_id, witness.original_root_id) != (owner.principal_id, owner.session_id):
        raise BoardError("pipeline_producer_changed", "Staged producer proof is required", status_code=409)
    producer = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == witness.task_id).execution_options(populate_existing=True))
    attempt = await db.get(WorkBoardAttempt, witness.attempt_id, populate_existing=True)
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == witness.run_identity).execution_options(populate_existing=True))
    from src.db.models import WorkBoardInputArtifact
    input_artifact = await db.get(WorkBoardInputArtifact, witness.input_artifact_id, populate_existing=True)
    latest = await db.scalar(select(WorkBoardAttempt.attempt_id).where(WorkBoardAttempt.task_id == witness.task_id)
        .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))
    if (producer is None or attempt is None or run is None or latest != witness.attempt_id
        or row_token(producer) != witness.task_token or row_token(attempt) != witness.attempt_token
        or row_token(run) != witness.run_token or run.status != "succeeded"
        or producer.status != WorkBoardStatus.done or attempt.ended_at is None
        or input_artifact is None or row_token(input_artifact) != witness.input_artifact_token
        or hashlib.sha256(witness.output_bytes).hexdigest() != witness.content_sha256):
        raise BoardError("pipeline_producer_changed", "The staged producer binding changed", status_code=409)
    return {"file_path": witness.output_reference, "content_sha256": witness.content_sha256,
        "attempt_id": witness.attempt_id, "quoted_source_data": witness.output_bytes.decode("utf-8")}


async def _materialize_pipeline_handoff_locked(db, owner, parent, child, link, *, witness: PipelineProducerWitness):
    await recheck_pipeline_producer_readback(db, owner, witness=witness)
    existing = await db.scalar(select(WorkBoardHandoff).where(WorkBoardHandoff.link_id == link.link_id,
        WorkBoardHandoff.source_attempt_id == witness.attempt_id,
        WorkBoardHandoff.source_task_revision == parent.task_revision))
    if existing is None:
        attempt = await db.get(WorkBoardAttempt, witness.attempt_id)
        existing = await _persist_one_handoff(db, owner, parent, child, attempt,
            proof=json.loads(witness.proof_bytes), link=link)
    elif link.current_handoff_id != existing.handoff_id:
        link.current_handoff_id = existing.handoff_id
        await db.flush()
    return existing
_HANDOFF_RECONCILIATION_REASONS = (
    _HANDOFF_RECONCILIATION_REASON,
    _LEGACY_HANDOFF_RECONCILIATION_REASON,
    _LEGACY_HANDOFF_MISSING_REASON,
)


def _is_handoff_reconciliation_block(block_kind: Any, block_reason: Any) -> bool:
    """Recognize canonical handoff recovery blocks and the pre-fix wording."""
    return (
        str(block_kind or "") == "dependency"
        and str(block_reason or "") in _HANDOFF_RECONCILIATION_REASONS
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _digest(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _decode_list(value: str | None) -> list[Any]:
    try:
        payload = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return payload if isinstance(payload, list) else []


def _decode_object(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _bounded_utf8(value: str, max_bytes: int) -> str:
    raw = str(value or "").encode("utf-8")[:max(1, int(max_bytes))]
    return raw.decode("utf-8", errors="ignore")


def _verified_readback(attempt: WorkBoardAttempt) -> dict[str, Any] | None:
    for item in _decode_list(attempt.receipt_refs_json):
        if not isinstance(item, Mapping):
            continue
        # Receipt refs are persisted evidence, so the independent proof type
        # must survive the attempt projection.  A generic effect marked
        # verified is not sufficient to establish readback.
        if str(item.get("receipt_kind") or "").strip() != "readback":
            continue
        if str(item.get("status") or "") not in _VERIFIED_RECEIPT_STATUSES:
            continue
        if not bool(item.get("verified")):
            continue
        receipt_run_id = str(item.get("workflow_run_id") or "").strip()
        expected_run_id = str(attempt.workflow_run_id or "").strip()
        # A board attempt is bound to one durable root.  Do not accept a
        # syntactically valid receipt from another run merely because the
        # authoritative root itself succeeded; child evidence must be
        # correlated by a separate governed projection before it can enter a
        # board attempt receipt.
        if not receipt_run_id or receipt_run_id != expected_run_id:
            continue
        run_id = expected_run_id
        digest = str(item.get("content_sha256") or "").lower()
        if not run_id or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            continue
        proof: dict[str, Any] = {
            "receipt_kind": "readback",
            "workflow_run_id": run_id,
            "status": "succeeded",
            "verified": True,
            "content_sha256": digest,
        }
        for key in (
            "readback_id",
            "artifact_id",
            "verifier_id",
            "verification_id",
            "effect_id_digest",
        ):
            value = str(item.get(key) or "").strip()
            pattern = _SAFE_EFFECT_DIGEST if key == "effect_id_digest" else _SAFE_RECEIPT_ID
            if value and pattern.fullmatch(value):
                proof[key] = value
        verified_at = str(item.get("verified_at") or "").strip()
        if verified_at and len(verified_at) <= 64 and "\n" not in verified_at and "\r" not in verified_at:
            proof["verified_at"] = verified_at
        return proof
    return None


def _safe_verification_receipt(
    proof: Mapping[str, Any] | None,
    *,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Return the bounded typed proof persisted in a handoff/review receipt."""
    if not isinstance(proof, Mapping):
        return {}
    if str(proof.get("receipt_kind") or "").strip() != "readback":
        return {}
    run_id = str(proof.get("workflow_run_id") or "").strip()
    digest = str(proof.get("content_sha256") or "").strip().lower()
    if not run_id or not _SAFE_WORKFLOW_ID.fullmatch(run_id):
        return {}
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        return {}
    receipt: dict[str, Any] = {
        "receipt_kind": "readback",
        "status": "verified",
        "workflow_run_id": run_id,
        "content_sha256": digest,
    }
    raw_verified_at = proof.get("verified_at")
    if raw_verified_at is not None:
        verified_at = str(raw_verified_at).strip()
        if not verified_at or len(verified_at) > 64 or "\n" in verified_at or "\r" in verified_at:
            return {}
        receipt["verified_at"] = verified_at
    for key in (
        "readback_id",
        "artifact_id",
        "verifier_id",
        "verification_id",
        "effect_id_digest",
    ):
        value = str(proof.get(key) or "").strip()
        pattern = _SAFE_EFFECT_DIGEST if key == "effect_id_digest" else _SAFE_RECEIPT_ID
        if value and pattern.fullmatch(value):
            receipt[key] = value
    if require_complete and (
        not receipt.get("readback_id")
        or not receipt.get("verified_at")
    ):
        return {}
    return receipt


def _attempt_receipt_ids(attempt: WorkBoardAttempt) -> set[str]:
    values: set[str] = set()
    for item in _decode_list(attempt.receipt_refs_json):
        if not isinstance(item, Mapping):
            continue
        for key in (
            "artifact_id",
            "effect_id",
            "effect_id_digest",
            "job_id",
            "workflow_run_id",
            "readback_id",
        ):
            value = item.get(key)
            if isinstance(value, str) and value:
                values.add(value)
    return values


async def _owned_attempt(
    db: AsyncSession,
    repository: WorkBoardRepository,
    owner: WorkBoardOwner,
    task: WorkBoardTask,
    attempt_id: str,
    *,
    require_terminal: bool = True,
) -> WorkBoardAttempt:
    attempt = (
        await db.execute(
            select(WorkBoardAttempt).where(
                WorkBoardAttempt.task_id == task.task_id,
                WorkBoardAttempt.attempt_id == attempt_id,
            )
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise BoardError("attempt_not_found", "The board attempt does not exist", status_code=404)
    if not attempt.workflow_run_id:
        raise BoardError(
            "workflow_run_required",
            "The review action requires a linked durable workflow run",
            status_code=409,
        )
    if require_terminal and attempt.ended_at is None:
        raise BoardError(
            "workflow_run_not_terminal",
            "Review cannot advance a task before the fenced attempt is terminal",
            status_code=409,
        )
    if not require_terminal and attempt.ended_at is None:
        if not attempt.lease_owner or int(attempt.fencing_token or 0) <= 0:
            raise BoardError("stale_fence", "The active board attempt has no current fence", status_code=409)
        expiry = _aware(attempt.lease_expires_at)
        if expiry is None:
            raise BoardError("stale_fence", "The active board attempt lease is missing", status_code=409)
        if expiry <= _now():
            raise BoardError("stale_fence", "The active board attempt lease has expired", status_code=409)
    if task.owner_principal_id != owner.principal_id or task.owner_session_id != owner.session_id:
        raise BoardError("task_owner_mismatch", "The task is owned by another operator session", status_code=403)
    return attempt


async def _verified_workflow_readback(
    db: AsyncSession,
    task: WorkBoardTask,
    attempt: WorkBoardAttempt,
    *, native_memory_report_source=None,
) -> dict[str, Any] | None:
    """Require both the receipt proof and the authoritative terminal run."""
    if native_memory_report_source is not None:
        from src.runtime_plugins.memory_producer import recheck_memory_report_source
        return await recheck_memory_report_source(db, task, attempt, native_memory_report_source,
                                                read_current=True)
    if attempt.ended_at is None:
        return None
    proof = _verified_readback(attempt)
    if proof is None:
        return None
    run = (
        await db.execute(
            select(WorkflowRunState).where(
                WorkflowRunState.run_identity == attempt.workflow_run_id,
            )
    )
    ).scalar_one_or_none()
    if run is None or str(run.status or "") != "succeeded":
        return None
    if task.capability_id == "agent.task.v1":
        from src.workflows.specialist_delegation import is_specialist_root
        if is_specialist_root(run):
            try:
                from src.workflows.specialist_lifecycle import verify_terminal_specialist_origin
                await verify_terminal_specialist_origin(db, task, attempt, run)
            except (BoardError, ValueError, TypeError):
                return None
    native_report = task.capability_id == "work.local-evidence-report.v1" and run.composition_binding_json is not None
    if native_report:
        proof = await native_report_memory_metadata(db, task, attempt, run)
    elif not _workflow_run_binds_board_attempt(task, attempt, run):
        return None
    if task.capability_id == "memory.opportunity-preference.v1":
        from src.work_board.opportunity_preference_native import stage_output_source, recheck_done_source
        try:
            staged = db.info.get("opportunity_preference_done_source")
            if staged is None:
                staged = await stage_output_source(db,task,attempt)
            else:
                await recheck_done_source(db,witness=staged)
            if staged.output_sha256 != proof["content_sha256"]:
                return None
        except (BoardError,ValueError,TypeError,KeyError,OSError):
            return None
    if task.capability_id == "work.research-dossier.v1":
        try:
            from src.work_board.research_readback import verified_dossier
            dossier, _raw = await verified_dossier(db, task, attempt, run)
            if dossier["content_sha256"] != proof["content_sha256"]:
                return None
        except (ValueError, TypeError, KeyError, OSError, BoardError):
            return None
    if task.capability_id == "inference.near-text.v1":
        from src.work_board.near_text_native import read_output
        try:
            read_output(task,attempt,run)
        except (OSError,ValueError,TypeError,KeyError,BoardError):
            return None
    if task.capability_id == "work.json-format.v1":
        try:
            from src.work_board.tool_package_native import verified_output
            staged=db.info.get("formatter_review_readback")
            if db.info.get("formatter_review_writer") and staged is None:return None
            artifact,_raw=verified_output(task,attempt,run,staged=staged)
            if artifact["content_sha256"]!=proof["content_sha256"]:
                return None
        except (ValueError,TypeError,KeyError,OSError,BoardError):
            return None
    if task.capability_id in {"browser.public-task.v1", "work.evidence-dossier.v1", "work.local-evidence-report.v1"}:
        # A stored Succeeded label cannot authorize a downstream consumer.
        # Reopen the exact private output and correlate its settled effect.
        from src.work_board.pipeline_cpu import read_output
        artifacts = _decode_list(run.artifact_receipts_json)
        effects = _decode_list(run.effect_receipts_json)
        if task.capability_id == "browser.public-task.v1":
            from src.work_board.dispatcher import _browser_cleanup_receipt_proven
            if not _browser_cleanup_receipt_proven({"effects": effects}):
                return None
        matching = [item for item in artifacts if isinstance(item, Mapping)
            and item.get("content_sha256") == proof["content_sha256"] and item.get("exists") is True]
        if len(matching) != 1:
            return None
        artifact = matching[0]
        if not any(isinstance(effect, Mapping) and effect.get("receipt_kind") == "readback"
            and effect.get("status") == "succeeded" and effect.get("target_path") == artifact.get("file_path")
            and effect.get("content_sha256") == proof["content_sha256"] for effect in effects):
            return None
        try:
            read_output(artifact["file_path"], proof["content_sha256"])
        except (ValueError, TypeError, OSError, BoardError):
            return None
    # Review and handoff both consume the same bounded evidence contract.  A
    # digest alone is insufficient: the durable readback ID and the verifier's
    # recorded timestamp must survive projection so an operator can inspect
    # the exact proof rather than a synthesized receipt.
    return proof if _safe_verification_receipt(proof, require_complete=True) else None


async def native_report_memory_metadata(db, task, attempt, run):
    """Exact terminal Task5 source correlation; no file/configuration access."""
    from src.runtime_plugins.task_capability import read_report_candidate
    from src.runtime_plugins.ownership import validate_run
    from src.db.models import WorkBoardInputArtifact, Goal
    from src.work_board.pipelines import row_token
    if (task.capability_id != "work.local-evidence-report.v1" or task.status != WorkBoardStatus.done
        or attempt.task_id != task.task_id or attempt.ended_at is None or run.status != "succeeded"
        or attempt.workflow_run_id != run.run_identity):
        raise ValueError("native_memory_report_source_changed")
    value = read_report_candidate(run)
    await validate_run(db, run)
    input_artifact = await db.get(WorkBoardInputArtifact, value["input_id"], populate_existing=True)
    goal = await db.get(Goal, task.goal_id, populate_existing=True)
    latest = await db.scalar(select(WorkBoardAttempt.attempt_id).where(WorkBoardAttempt.task_id == task.task_id)
        .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))
    if (value["task_id"] != task.task_id or value["attempt_id"] != attempt.attempt_id
        or value["board_fencing_token"] != attempt.fencing_token or latest != attempt.attempt_id
        or value["owner_principal_id"] != task.owner_principal_id or value["original_root_id"] != task.owner_session_id
        or value["goal_id"] != task.goal_id or value["goal_revision"] != task.goal_revision
        or value["typed_input_ref"] != task.typed_input_ref or value["typed_input_digest"] != task.typed_input_digest
        or input_artifact is None or task.input_artifact_id != input_artifact.artifact_id
        or input_artifact.owner_principal_id != task.owner_principal_id
        or input_artifact.owner_session_id != task.owner_session_id
        or input_artifact.goal_id != task.goal_id or input_artifact.goal_revision != task.goal_revision
        or input_artifact.capability_id != task.capability_id or input_artifact.capability_version != "1"
        or input_artifact.bound_task_id != task.task_id or input_artifact.state != "consumed"
        or input_artifact.payload_sha256 != value["payload_sha256"]
        or input_artifact.typed_input_ref != value["typed_input_ref"]
        or row_token(input_artifact) != value["input_token"]
        or goal is None or goal.status != "active" or goal.revision != task.goal_revision
        or goal.owner_principal_id != task.owner_principal_id or goal.owner_session_id != task.owner_session_id):
        raise ValueError("native_memory_report_source_changed")
    from src.runtime_plugins import task_capability
    validator = getattr(task_capability, "validated_terminal_report_source", None)
    if not callable(validator):
        raise ValueError("native_memory_terminal_report_unavailable")
    terminal = validator(run)
    if (type(terminal) is not dict or set(terminal) != {"candidate", "output_reference", "output_sha256", "size_bytes"}
        or terminal["candidate"] != value
        or type(terminal["size_bytes"]) is not int or not 0 < terminal["size_bytes"] <= 65536
        or type(terminal["output_reference"]) is not str
        or not terminal["output_reference"].startswith("artifacts/work-board/evidence/")):
        raise ValueError("native_memory_report_source_changed")
    invoked = terminal
    proof = _verified_readback(attempt)
    if (proof is None or proof["content_sha256"] != invoked["output_sha256"]
        or not _safe_verification_receipt(proof, require_complete=True)):
        raise ValueError("native_memory_report_source_changed")
    artifacts, effects = _decode_list(run.artifact_receipts_json), _decode_list(run.effect_receipts_json)
    if (len([item for item in artifacts if isinstance(item, dict) and item.get("exists") is True
             and item.get("file_path") == invoked["output_reference"]
             and item.get("content_sha256") == invoked["output_sha256"]]) != 1
        or not any(isinstance(item, dict) and item.get("receipt_kind") == "readback" and item.get("status") == "succeeded"
                   and item.get("target_path") == invoked["output_reference"]
                   and item.get("content_sha256") == invoked["output_sha256"] for item in effects)):
        raise ValueError("native_memory_report_source_changed")
    return dict(proof)


def _workflow_run_binds_board_attempt(
    task: WorkBoardTask,
    attempt: WorkBoardAttempt,
    run: WorkflowRunState,
) -> bool:
    """Validate the durable root identity consumed by review and handoff.

    Board attempts may point at either a user-owned direct capability root or
    the service-owned wrapper root created by ``WorkBoardDispatcher``.  The
    latter delegates authority to the authenticated task owner, so comparing
    its service principal directly with the task owner would reject the real
    GoalSnapshot path.  This validator is intentionally shared by every proof
    consumer and accepts no arbitrary service-shaped row.
    """

    attempt_run_id = str(attempt.workflow_run_id or "").strip()
    if not attempt_run_id or str(run.run_identity or "") != attempt_run_id:
        return False
    from src.workflows.specialist_delegation import is_specialist_root
    specialist = task.capability_id == "agent.task.v1" and is_specialist_root(run)
    if specialist:
        authority = _decode_object(run.declared_authority_json)
        if (run.branch_depth != 2 or not task.idempotency_key.startswith("specialist:")
            or task.idempotency_scope != "general-task"
            or run.parent_job_id != task.origin_thread_id
            or run.parent_run_identity != task.origin_thread_id
            or authority.get("specialist_delegation_invocation_id") != task.origin_thread_id
            or run.root_run_identity != authority.get("specialist_original_parent_id")):
            return False
    if not specialist and str(run.root_run_identity or run.run_identity or "") != attempt_run_id:
        # A board attempt is linked to one immutable durable root.  A child run
        # may appear in receipts, but the attempt link itself remains the root.
        return False
    if not specialist and (run.parent_run_identity or run.parent_job_id):
        return False
    if str(run.goal_id or "") != str(task.goal_id or ""):
        return False
    try:
        if int(run.goal_revision or 0) != int(task.goal_revision or 0):
            return False
    except (TypeError, ValueError):
        return False
    if str(run.operator_session_id or "") != str(task.owner_session_id or ""):
        return False
    if run.session_id and str(run.session_id) != str(task.owner_session_id or ""):
        return False

    arguments = _decode_object(run.arguments_json)
    safe_digest = lambda value: bool(_SAFE_DIGEST.fullmatch(str(value or "").strip()))

    capability_id = str(task.capability_id or "").strip()
    if capability_id == "inference.near-text.v1":
        from src.work_board.near_text_native import binds
        return binds(task,attempt,run)
    if capability_id == "memory.opportunity-preference.v1":
        from src.work_board.opportunity_preference_native import binds
        return binds(task,attempt,run)
    if capability_id == "work.json-format.v1":
        from src.work_board.tool_package_native import binds
        return binds(task,attempt,run)
    if capability_id == "work.research-dossier.v1":
        from src.work_board.research_readback import binds
        return binds(task, attempt, run)
    if capability_id in {"browser.public-task.v1", "work.evidence-dossier.v1", "work.local-evidence-report.v1"}:
        try:
            from src.work_board.dispatcher import WorkBoardDispatcher, _parse_typed_input, _safe_digest
            inputs = _parse_typed_input(task)
            if capability_id == "browser.public-task.v1":
                authority = _decode_object(run.declared_authority_json)
                limits = authority.get("limits", {})
                if (run.owner_kind != "service" or run.owner_principal_id != "service:browser-task"
                    or run.service_id != "service:browser-task" or run.job_kind != "browser_public_task"
                    or type(limits.get("runtime_seconds")) is not int or not 1 <= limits["runtime_seconds"] <= 180
                    or type(limits.get("max_attempts")) is not int or not 1 <= limits["max_attempts"] <= 2
                    or type(limits.get("max_outstanding_jobs")) is not int or not 1 <= limits["max_outstanding_jobs"] <= 16
                    or (task.pipeline_operation_id and limits["max_outstanding_jobs"] != 1)):
                    return False
                immutable_task = task.model_copy(update={"task_revision": attempt.task_revision_at_claim + 1})
                projection = {"owner": {"principal_id": run.owner_principal_id, "kind": run.owner_kind, "service_id": run.service_id},
                    "job_kind": run.job_kind, "capability_version": run.capability_version,
                    "declared_authority": authority, "job_id": run.run_identity,
                    "session_id": run.session_id, "operator_session_id": run.operator_session_id,
                    "goal_id": run.goal_id, "goal_revision": run.goal_revision}
                expected = WorkBoardDispatcher._browser_expected_identity(immutable_task, attempt, inputs, projection,
                    limits["runtime_seconds"], limits["max_attempts"], limits["max_outstanding_jobs"])
                return (run.run_identity == expected["job_id"] and run.input_digest == expected["input_digest"]
                    and run.run_fingerprint == expected["run_fingerprint"]
                    and _safe_digest(authority) == expected["authority_digest"]
                    and run.idempotency_scope == "work-board-attempt"
                    and run.idempotency_key == f"{task.task_id}:{attempt.attempt_id}")
            from src.work_board.pipeline_cpu import spec_for
            spec = spec_for(task, attempt, inputs, deadline=_now())
            return (run.owner_kind == "user" and run.owner_principal_id == task.owner_principal_id
                and not run.service_id and run.job_kind == capability_id and run.capability_version == "1"
                and run.run_identity == spec.identity.job_id and run.input_digest == _safe_digest(spec.inputs)
                and run.run_fingerprint == spec.run_fingerprint
                and _decode_object(run.declared_authority_json) == spec.declared_authority
                and run.idempotency_scope == "work-board-attempt"
                and run.idempotency_key == f"{task.task_id}:{attempt.attempt_id}")
        except (ValueError, TypeError, KeyError, BoardError):
            return False

    if str(run.owner_kind or "") == "user":
        if str(run.owner_principal_id or "") != str(task.owner_principal_id or ""):
            return False
        if run.service_id:
            return False
        capability_id = str(task.capability_id or "").strip()
        if not capability_id:
            # Legacy workflow rows without a registered capability remain
            # reviewable when their owner, goal, session, and root are exact.
            return True
        try:
            from src.work_board.dispatcher import REGISTERED_CAPABILITIES

            capability = REGISTERED_CAPABILITIES.get(capability_id)
        except Exception:
            capability = None
        if capability is None:
            return False
        # The registered GitHub adapter owns its established native durable
        # kind; retain every board identity/digest fence below for that alias.
        expected_job_kind = "github_followthrough_v1" if capability_id == "work.github-followthrough.v1" else capability_id
        if (
            str(run.job_kind or "") != expected_job_kind
            or str(run.capability_version or "") != str(capability.version)
            or str(run.session_id or "") != str(task.owner_session_id or "")
            or str(run.idempotency_scope or "") != "work-board-attempt"
            or str(run.idempotency_key or "") != f"{task.task_id}:{attempt.attempt_id}"
        ):
            return False
        if not arguments.get("redacted") or arguments.get("shape") != "dict":
            return False
        return safe_digest(run.input_digest) and safe_digest(run.run_fingerprint)

    if str(run.owner_kind or "") != "service":
        return False
    capability_id = str(task.capability_id or "").strip()
    if capability_id == "guardian.research-watch.v1":
        # The source-watch adapter owns its existing durable root directly.
        # It is a distinct, fixed service identity from the board wrapper
        # service below; accept it only when the root's task/attempt
        # idempotency binding, goal/session authority, and redacted argument
        # shape all match this exact board claim.
        from src.guardian.source_watch import (
            CAPABILITY_ID as SOURCE_WATCH_CAPABILITY_ID,
            CAPABILITY_VERSION as SOURCE_WATCH_CAPABILITY_VERSION,
            SERVICE_ID as SOURCE_WATCH_SERVICE_ID,
            SERVICE_PRINCIPAL as SOURCE_WATCH_SERVICE_PRINCIPAL,
        )

        authority = _decode_object(run.declared_authority_json)
        required_authority = {
            "principal": SOURCE_WATCH_SERVICE_PRINCIPAL,
            "owner_kind": "service",
            "service_id": SOURCE_WATCH_SERVICE_ID,
            "session_id": str(task.owner_session_id or ""),
            "goal_id": str(task.goal_id or ""),
            "goal_revision": int(task.goal_revision or 0),
            "goal_owner_principal_id": str(task.owner_principal_id or ""),
            "goal_owner_session_id": str(task.owner_session_id or ""),
            "capability_id": SOURCE_WATCH_CAPABILITY_ID,
        }
        if any(authority.get(key) != value for key, value in required_authority.items()):
            return False
        if (
            str(run.owner_principal_id or "") != SOURCE_WATCH_SERVICE_PRINCIPAL
            or str(run.service_id or "") != SOURCE_WATCH_SERVICE_ID
            or str(run.job_kind or "") != "guardian_source_watch"
            or str(run.capability_version or "") != SOURCE_WATCH_CAPABILITY_VERSION
            or str(run.idempotency_scope or "") != "work-board-attempt"
            or str(run.idempotency_key or "")
            != f"{task.task_id}:{attempt.attempt_id}"
        ):
            return False
        permissions = authority.get("permissions")
        if (
            not isinstance(permissions, list)
            or any(not isinstance(item, str) for item in permissions)
            or set(permissions) != {"source_observation", "workspace_write"}
        ):
            return False
        try:
            plan_revision = int(run.plan_revision or 0)
            authority_plan_revision = int(authority.get("plan_revision") or 0)
        except (TypeError, ValueError):
            return False
        if plan_revision < 1 or authority_plan_revision != plan_revision:
            return False
        if not safe_digest(authority.get("source_set_digest")) or not safe_digest(
            authority.get("criteria_digest")
        ):
            return False
        if not arguments.get("redacted") or arguments.get("shape") != "dict":
            return False
        keys = arguments.get("keys")
        if not isinstance(keys, list):
            return False
        argument_keys = {str(key) for key in keys}
        required_keys = {"watch_id", "occurrence_id"}
        allowed_keys = required_keys | {
            "parent_handoff_context",
            "parent_handoff_digest",
        }
        if (
            not required_keys.issubset(argument_keys)
            or not argument_keys.issubset(allowed_keys)
            or ("parent_handoff_context" in argument_keys)
            != ("parent_handoff_digest" in argument_keys)
        ):
            return False
        return safe_digest(run.input_digest) and safe_digest(run.run_fingerprint)
    try:
        from src.work_board.dispatcher import (
            DISPATCHER_PRINCIPAL,
            DISPATCHER_SERVICE,
            REGISTERED_CAPABILITIES,
        )
    except Exception:
        return False
    capability = REGISTERED_CAPABILITIES.get(capability_id)
    authority = _decode_object(run.declared_authority_json)
    if capability is None:
        return False
    required_authority = {
        "principal": DISPATCHER_PRINCIPAL,
        "owner_kind": "service",
        "service_id": DISPATCHER_SERVICE,
        "session_id": str(task.owner_session_id or ""),
        "goal_owner_principal_id": str(task.owner_principal_id or ""),
        "goal_owner_session_id": str(task.owner_session_id or ""),
        "capability_id": capability_id,
        "capability_version": str(capability.version),
        "finite_authority": True,
    }
    if any(authority.get(key) != value for key, value in required_authority.items()):
        return False
    if (
        str(run.owner_principal_id or "") != DISPATCHER_PRINCIPAL
        or str(run.service_id or "") != DISPATCHER_SERVICE
        or str(run.session_id or "") != str(task.owner_session_id or "")
        or str(run.job_kind or "") != capability_id
        or str(run.capability_version or "") != str(capability.version)
        or str(run.idempotency_scope or "") != "work-board-attempt"
        or str(run.idempotency_key or "") != f"{task.task_id}:{attempt.attempt_id}"
        or not arguments.get("redacted")
        or arguments.get("shape") != "dict"
    ):
        return False
    keys = arguments.get("keys")
    if not isinstance(keys, list):
        return False
    required_keys = {
        "task_id",
        "attempt_id",
        "capability_id",
        "typed_input_ref",
        "typed_input_digest",
    }
    if not required_keys.issubset({str(key) for key in keys}):
        return False
    return safe_digest(run.input_digest) and safe_digest(run.run_fingerprint)


def _require_reviewer(task: WorkBoardTask, owner: WorkBoardOwner) -> None:
    reviewer = str(task.reviewer_id or "").strip()
    if not reviewer:
        raise BoardError("reviewer_required", "The task has no named reviewer", status_code=409)
    if reviewer != owner.principal_id:
        raise BoardError("reviewer_authority_required", "Only the named reviewer may complete this review", status_code=403)


def _require_not_self_review(task: WorkBoardTask, attempt: WorkBoardAttempt, owner: WorkBoardOwner) -> None:
    """Require the authenticated named reviewer for a review verdict.

    ``executor_id`` identifies a runtime adapter/capability, while the
    reviewer is an authenticated operator principal and session.  Comparing
    those opaque identifiers is not an authority check and can reject a
    legitimate owner review merely because two unrelated namespaces happen
    to use the same string.  Worker tools have no verdict operation; this
    function is reached only by the owner review API paths below.
    """
    _require_reviewer(task, owner)


async def _require_projected_intent(
    db: AsyncSession,
    owner: WorkBoardOwner,
    task: WorkBoardTask,
    attempt: WorkBoardAttempt,
) -> None:
    if not task.review_request_attempt_id:
        return
    intent = (
        await db.execute(
            select(WorkBoardReviewIntent).where(
                WorkBoardReviewIntent.owner_principal_id == owner.principal_id,
                WorkBoardReviewIntent.owner_session_id == owner.session_id,
                WorkBoardReviewIntent.task_id == task.task_id,
                WorkBoardReviewIntent.attempt_id == attempt.attempt_id,
                WorkBoardReviewIntent.fencing_token == int(attempt.fencing_token),
                WorkBoardReviewIntent.status == "projected",
            )
        )
    ).scalar_one_or_none()
    if intent is None:
        raise BoardError(
            "review_intent_stale",
            "The Review card is not backed by a current dispatcher review intent",
            status_code=409,
        )
    if (
        not intent.workflow_run_id
        or intent.workflow_run_id != str(attempt.workflow_run_id or "")
        or task.review_request_revision is None
        or int(task.review_request_revision) != int(intent.task_revision)
        or task.review_request_fence != int(intent.fencing_token)
        or task.review_request_digest != intent.request_digest
    ):
        raise BoardError(
            "review_intent_stale",
            "The Review card is not bound to the persisted fenced review intent",
            status_code=409,
        )


async def _require_current_review_attempt(
    db: AsyncSession,
    task: WorkBoardTask,
    attempt: WorkBoardAttempt,
) -> None:
    """Bind a reviewer verdict to the attempt that produced this Review."""
    latest = (
        await db.execute(
            select(WorkBoardAttempt)
            .where(WorkBoardAttempt.task_id == task.task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None or latest.attempt_id != attempt.attempt_id:
        raise BoardError(
            "stale_review_attempt",
            "The reviewer verdict must use the latest authoritative attempt",
            status_code=409,
        )
    # An intent is recorded against the current board revision when the
    # operator/worker asks for review.  That revision may differ from the
    # immutable claim revision because bounded coordination writes can advance
    # the task while the fenced run is still active.  The persisted intent is
    # therefore the authority for the verdict binding; comparing it to
    # task_revision_at_claim would reject a valid claim->link->intent->Review
    # lifecycle.
    if task.review_request_attempt_id:
        projected_intent = (
            await db.execute(
                select(WorkBoardReviewIntent).where(
                    WorkBoardReviewIntent.task_id == task.task_id,
                    WorkBoardReviewIntent.attempt_id == attempt.attempt_id,
                    WorkBoardReviewIntent.fencing_token == int(attempt.fencing_token),
                    WorkBoardReviewIntent.status == "projected",
                )
            )
        ).scalar_one_or_none()
        if (
            projected_intent is None
            or task.review_request_revision is None
            or int(projected_intent.task_revision) != int(task.review_request_revision)
        ):
            raise BoardError(
                "stale_review_attempt",
                "The reviewer verdict is not bound to the persisted review intent revision",
                status_code=409,
            )
    run = (
        await db.execute(
            select(WorkflowRunState).where(
                WorkflowRunState.run_identity == attempt.workflow_run_id,
            )
        )
    ).scalar_one_or_none()
    if run is None or not _workflow_run_binds_board_attempt(task, attempt, run):
        raise BoardError(
            "stale_review_attempt",
            "The review attempt no longer matches the current goal authority",
            status_code=409,
        )


async def _finish_event(
    db: AsyncSession,
    repository: WorkBoardRepository,
    task: WorkBoardTask,
    owner: WorkBoardOwner,
    *,
    expected_revision: int,
    values: dict[str, Any],
    kind: str,
    metadata: dict[str, Any],
) -> BoardMutation:
    if task.task_revision != expected_revision:
        raise BoardRevisionConflict(task.task_id, expected_revision, task.task_revision)
    values = {
        **values,
        "task_revision": expected_revision + 1,
        "updated_at": _now(),
    }
    await repository._cas_task_update(
        db,
        owner,
        task,
        expected_revision=expected_revision,
        values=values,
    )
    event = await repository._event(
        db,
        task,
        owner,
        kind=kind,
        metadata={**metadata, "status": task.status.value, "task_revision": task.task_revision},
    )
    return BoardMutation(task, event)


def _formatter_readback_writer(function):
    # Formatter physical/package readback is staged before these existing
    # review writers. The final read checks only the exact canonical rows.
    from functools import wraps
    @wraps(function)
    async def wrapped(db,owner,task_id,*args,**kwargs):
        from src.work_board.tool_package_native import stage_readback
        repository=kwargs.get("repository") or WorkBoardRepository()
        task=await repository._owned_task(db,owner,task_id)
        prior=db.info.get("formatter_review_writer",False)
        cached=db.info.get("formatter_review_readback")
        try:
            if task.capability_id=="work.json-format.v1" and not kwargs.get("transaction_locked"):
                attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id)
                    .order_by(WorkBoardAttempt.created_at.desc(),WorkBoardAttempt.attempt_id.desc()).limit(1))
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id)) if attempt else None
                if run is not None and run.status=="succeeded":
                    db.info["formatter_review_readback"]=stage_readback(task,attempt,run)
            db.info["formatter_review_writer"]=True
            return await function(db,owner,task_id,*args,**kwargs)
        finally:
            db.info["formatter_review_writer"]=prior
            if cached is None:db.info.pop("formatter_review_readback",None)
            else:db.info["formatter_review_readback"]=cached
    return wrapped


@_formatter_readback_writer
async def request_review(
    db: AsyncSession,
    owner: WorkBoardOwner,
    task_id: str,
    *,
    expected_revision: int,
    attempt_id: str,
    evidence_refs: list[str],
    repository: WorkBoardRepository | None = None,
    transaction_locked: bool = False,
) -> BoardMutation:
    repository = repository or WorkBoardRepository()
    if not transaction_locked:
        await _begin_sqlite_immediate(db)
    task = await repository._owned_task(db, owner, task_id)
    # The worker host may have read a Running task just before the dispatcher
    # acquired the writer lock and projected it.  Refresh after the lock so an
    # identity-map copy cannot turn that race into a pending intent on Review.
    await db.refresh(task)
    if task.status is not WorkBoardStatus.running:
        raise BoardError("illegal_transition", "Only a Running task can request review", status_code=409)
    if task.task_revision != int(expected_revision):
        raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
    attempt = await _owned_attempt(
        db,
        repository,
        owner,
        task,
        attempt_id,
        require_terminal=False,
    )
    await db.refresh(attempt)
    # A request made after a run has already ended is retained for backwards
    # compatibility with the operator endpoint, but it still requires the
    # same authoritative proof.  A live worker request is intent only; the
    # dispatcher performs this proof check before projecting Review.
    if attempt.ended_at is not None and await _verified_workflow_readback(db, task, attempt) is None:
        raise BoardError(
            "verified_readback_required",
            "Review requires an authoritative successful workflow readback",
            status_code=409,
        )
    safe_evidence: list[str] = []
    if evidence_refs:
        if attempt.ended_at is None:
            # Before terminal projection, evidence is still only an optional
            # intent annotation.  If a caller supplies it, accept IDs solely
            # from a complete typed readback bound to this attempt's exact
            # durable run.  Generic artifact/effect/job/result IDs must not
            # be relabeled as review evidence.
            typed_readback = _safe_verification_receipt(
                _verified_readback(attempt),
                require_complete=True,
            )
            typed_ids = {
                str(typed_readback[key])
                for key in (
                    "readback_id",
                    "artifact_id",
                    "verifier_id",
                    "verification_id",
                    "effect_id_digest",
                )
                if typed_readback.get(key)
            }
        else:
            typed_ids = _attempt_receipt_ids(attempt)
        for value in evidence_refs:
            _validate_safe_identifier(value, field="evidence_ref", max_length=512)
            if value not in typed_ids:
                raise BoardError(
                    "evidence_ref_unavailable",
                    "Review evidence must be a complete typed readback reference for this attempt",
                    status_code=422,
                )
            if value not in safe_evidence:
                safe_evidence.append(value)
    # A live request is only an intent anchor.  The linked durable run is
    # server-derived from the fenced attempt and typed readback is required
    # later, at terminal projection/reviewer verdict time.  Do not force the
    # worker or browser to present a generic artifact/effect as proof here.
    # A worker request is the explicit operator review intent for this fenced
    # attempt.  Ordinary executable cards do not need to predeclare review;
    # bind the review to the authenticated owner principal and exact session
    # represented by the durable intent row.  This still does not change the
    # phase or prove success.  Only dispatcher projection after authoritative
    # run/readback verification can enter Review.
    existing_reviewer = str(task.reviewer_id or "").strip()
    if existing_reviewer and existing_reviewer != owner.principal_id:
        raise BoardError(
            "reviewer_binding_mismatch",
            "The review intent is bound to another authenticated owner",
            status_code=403,
        )
    task.requires_review = True
    task.reviewer_id = owner.principal_id
    _require_reviewer(task, owner)
    intent = {
        "attempt_id": attempt.attempt_id,
        "fence": int(attempt.fencing_token),
        "task_revision": int(expected_revision),
        "workflow_run_id": str(attempt.workflow_run_id or ""),
        "reviewer_id": owner.principal_id,
        "reviewer_session_id": owner.session_id,
        "evidence_refs": sorted(safe_evidence),
    }
    request_digest = hashlib.sha256(
        json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    existing_intent = (
        await db.execute(
            select(WorkBoardReviewIntent).where(
                WorkBoardReviewIntent.owner_principal_id == owner.principal_id,
                WorkBoardReviewIntent.owner_session_id == owner.session_id,
                WorkBoardReviewIntent.task_id == task.task_id,
                WorkBoardReviewIntent.attempt_id == attempt.attempt_id,
                WorkBoardReviewIntent.fencing_token == int(attempt.fencing_token),
                WorkBoardReviewIntent.task_revision == int(expected_revision),
            )
        )
    ).scalar_one_or_none()
    if existing_intent is not None and existing_intent.workflow_run_id != intent["workflow_run_id"]:
        raise BoardError(
            "review_intent_conflict",
            "A different durable workflow run is already recorded for this fenced review intent",
            status_code=409,
        )
    if existing_intent is not None and existing_intent.request_digest != request_digest:
        raise BoardError(
            "review_intent_conflict",
            "A different fenced review intent is already recorded",
            status_code=409,
        )
    if task.review_request_digest:
        if task.review_request_digest != request_digest:
            raise BoardError(
                "review_intent_conflict",
                "A different fenced review intent is already recorded",
                status_code=409,
            )
        event = await repository._event(
            db,
            task,
            owner,
            kind="task.review_intent_replayed",
            metadata={
                "attempt_id": attempt.attempt_id,
                "workflow_run_id": attempt.workflow_run_id,
                "evidence_digests": [_digest(value) for value in safe_evidence],
                "status": task.status.value,
                "task_revision": task.task_revision,
            },
        )
        return BoardMutation(task, event, idempotent_replay=True)
    if existing_intent is None:
        db.add(
            WorkBoardReviewIntent(
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                workflow_run_id=intent["workflow_run_id"],
                fencing_token=int(attempt.fencing_token),
                task_revision=int(expected_revision),
                request_digest=request_digest,
                evidence_refs_json=json.dumps(safe_evidence, separators=(",", ":")),
                status="pending",
            )
        )
        await db.flush()
    # Recording an intent is deliberately revision preserving.  The worker
    # can issue it while the fenced attempt is still Running, and the
    # dispatcher may still be holding the pre-execution board revision.  The
    # task scalar fields are a compatibility projection; the dedicated row is
    # the durable idempotency/fence binding.  A surrounding SQLite immediate
    # transaction is supplied by the caller's managed session boundary and
    # the task revision check above prevents a stale request from winning.
    observed_at = _now()
    task.review_request_attempt_id = attempt.attempt_id
    task.review_request_fence = int(attempt.fencing_token)
    task.review_request_revision = int(expected_revision)
    task.review_request_digest = request_digest
    task.review_request_evidence_json = json.dumps(safe_evidence, separators=(",", ":"))
    task.review_requested_at = observed_at
    task.review_expires_at = None
    task.updated_at = observed_at
    await db.flush()
    event = await repository._event(
        db,
        task,
        owner,
        kind="task.review_intent_recorded",
        metadata={
            "attempt_id": attempt.attempt_id,
            "workflow_run_id": attempt.workflow_run_id,
            "evidence_digests": [_digest(value) for value in safe_evidence],
            "status": task.status.value,
            "task_revision": task.task_revision,
        },
    )
    return BoardMutation(task, event)


@_formatter_readback_writer
async def request_changes(
    db: AsyncSession,
    owner: WorkBoardOwner,
    task_id: str,
    *,
    expected_revision: int,
    reason: str,
    repository: WorkBoardRepository | None = None,
) -> BoardMutation:
    repository = repository or WorkBoardRepository()
    # Review verdicts reopen executable work, so serialize the live owner and
    # goal read with the final task CAS.  Without the writer lock a goal
    # revision can be revoked after preflight and before ``_finish_event``.
    await _begin_sqlite_immediate(db)
    task = await repository._owned_task(db, owner, task_id)
    await db.refresh(task)
    if task.status is not WorkBoardStatus.review:
        raise BoardError("illegal_transition", "Changes can be requested only from Review", status_code=409)
    latest = (
        await db.execute(
            select(WorkBoardAttempt)
            .where(WorkBoardAttempt.task_id == task.task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        raise BoardError("attempt_not_found", "Review has no execution attempt", status_code=409)
    _require_not_self_review(task, latest, owner)
    await _require_projected_intent(db, owner, task, latest)
    await _require_current_review_attempt(db, task, latest)
    if await _verified_workflow_readback(db, task, latest) is None:
        raise BoardError(
            "verified_readback_required",
            "Changes requested requires the latest attempt's verified durable readback",
            status_code=409,
        )
    expiry = _aware(task.review_expires_at)
    if expiry is not None and expiry <= _now():
        raise BoardError("review_expired", "Review has expired; the named reviewer must renew it", status_code=409)
    if not str(reason or "").strip():
        raise BoardError("reason_required", "Changes requested requires a bounded reason", status_code=422)
    safe_reason = await repository._safe_text(reason[:500])
    reason_digest = _digest(safe_reason)
    # A prior Running board attempt can only have been admitted from Ready.
    # Re-run every current readiness gate, including scheduled_at, before
    # reopening it; failure returns the card to Todo so M2 performs fresh
    # admission after the task is eligible.
    live_goal = await repository.validate_task_goal(db, owner, task)
    from src.work_board.dispatcher import MAX_ATTEMPTS_PER_TASK, _dispatcher

    readiness_error, _readiness_reason = await _dispatcher._current_readiness(task)
    parents = list(
        (
            await db.execute(
                select(WorkBoardTask.status)
                .join(WorkBoardLink, WorkBoardLink.parent_task_id == WorkBoardTask.task_id)
                .where(
                    WorkBoardLink.child_task_id == task.task_id,
                    WorkBoardLink.owner_principal_id == owner.principal_id,
                    WorkBoardLink.owner_session_id == owner.session_id,
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalars().all()
    )
    attempt_count = int(
        await db.scalar(
            select(func.count(WorkBoardAttempt.attempt_id)).where(
                WorkBoardAttempt.task_id == task.task_id
            )
        )
        or 0
    )
    max_attempts = MAX_ATTEMPTS_PER_TASK
    if task.capability_id == "browser.public-task.v1":
        max_attempts, _max_outstanding_jobs = effective_browser_limits(live_goal)
    attempt_exhausted = attempt_count >= max_attempts
    reopened = (
        WorkBoardStatus.blocked
        if attempt_exhausted
        else WorkBoardStatus.ready
        if not readiness_error and all(status is WorkBoardStatus.done for status in parents)
        else WorkBoardStatus.todo
    )
    exhausted_reason = "The board attempt limit has been exhausted; create a new linked task for further work."
    mutation = await _finish_event(
        db,
        repository,
        task,
        owner,
        expected_revision=expected_revision,
        values={
            "status": reopened,
            "review_expires_at": None,
            "review_request_attempt_id": None,
            "review_request_fence": None,
            "review_request_revision": None,
            "review_request_digest": None,
            "review_request_evidence_json": "[]",
            "review_requested_at": None,
            "block_kind": None,
            "block_reason": None,
            "block_source_status": None,
            **(
                {
                    "block_kind": "attempt_limit",
                    "block_reason": exhausted_reason,
                    "block_source_status": WorkBoardStatus.review.value,
                }
                if attempt_exhausted
                else {}
            ),
        },
        kind="task.changes_requested",
        metadata={
            "attempt_id": latest.attempt_id,
            "reason_code": "changes_requested",
            "reason_digest": _digest(safe_reason),
            "reopened_status": reopened.value,
            **({"block_kind": "attempt_limit"} if attempt_exhausted else {}),
        },
    )
    db.add(
        WorkBoardComment(
            task_id=task.task_id,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            author_principal_id=owner.principal_id,
            author_session_id=owner.session_id,
            body=f"Changes requested: {safe_reason}"[:2_000],
        )
    )
    await db.flush()
    return mutation


@_formatter_readback_writer
async def complete_review(
    db: AsyncSession,
    owner: WorkBoardOwner,
    task_id: str,
    *,
    expected_revision: int,
    attempt_id: str,
    repository: WorkBoardRepository | None = None,
) -> BoardMutation:
    repository = repository or WorkBoardRepository()
    # The named reviewer verdict must observe the live goal under the same
    # writer transaction that performs the Review -> Done CAS.
    await _begin_sqlite_immediate(db)
    task = await repository._owned_task(db, owner, task_id)
    await db.refresh(task)
    if task.status is not WorkBoardStatus.review:
        raise BoardError("illegal_transition", "Only a Review task can be completed", status_code=409)
    attempt = await _owned_attempt(db, repository, owner, task, attempt_id)
    await db.refresh(attempt)
    _require_not_self_review(task, attempt, owner)
    await _require_current_review_attempt(db, task, attempt)
    await repository.validate_task_goal(db, owner, task)
    await _require_projected_intent(db, owner, task, attempt)
    expiry = _aware(task.review_expires_at)
    if expiry is not None and expiry <= _now():
        raise BoardError("review_expired", "Review has expired; renewal is required", status_code=409)
    if await _verified_workflow_readback(db, task, attempt) is None:
        raise BoardError("verified_readback_required", "Review requires a current verified readback", status_code=409)
    mutation = await _finish_event(
        db,
        repository,
        task,
        owner,
        expected_revision=expected_revision,
        values={
            "status": WorkBoardStatus.done,
            "completed_at": _now(),
            "review_expires_at": None,
            "review_request_attempt_id": None,
            "review_request_fence": None,
            "review_request_revision": None,
            "review_request_digest": None,
            "review_request_evidence_json": "[]",
            "review_requested_at": None,
            "block_kind": None,
            "block_reason": None,
            "block_source_status": None,
        },
        kind="task.review_completed",
        metadata={"attempt_id": attempt.attempt_id, "workflow_run_id": attempt.workflow_run_id},
    )
    await _persist_child_handoffs(db, owner, task, attempt, proof=await _verified_workflow_readback(db, task, attempt))
    return mutation


async def block_task(
    db: AsyncSession,
    owner: WorkBoardOwner,
    task_id: str,
    *,
    expected_revision: int,
    block_kind: str,
    reason: str,
    source_status: WorkBoardStatus | None = None,
    repository: WorkBoardRepository | None = None,
) -> BoardMutation:
    repository = repository or WorkBoardRepository()
    task = await repository._owned_task(db, owner, task_id)
    if task.status is WorkBoardStatus.review:
        raise BoardError(
            "review_typed_recovery_required",
            "Review tasks use reviewer completion or typed expiry recovery",
            status_code=409,
        )
    if source_status is not None and source_status is not task.status:
        raise BoardError(
            "stale_source_status",
            "The block source status no longer matches the current task",
            status_code=409,
        )
    if task.status in {WorkBoardStatus.running, WorkBoardStatus.done, WorkBoardStatus.archived}:
        raise BoardError("illegal_transition", "This task cannot be blocked from its current status", status_code=409)
    if block_kind == "review_expired":
        raise BoardError(
            "review_expiry_dispatcher_only",
            "Only the bounded review expiry sweep may create a review_expired block",
            status_code=409,
        )
    if block_kind not in {"dependency", "needs_input", "capability", "transient", "cancelled", "unknown_effect", "operator"}:
        raise BoardError("invalid_block_kind", "The block kind is not supported", status_code=422)
    if not str(reason or "").strip():
        raise BoardError("reason_required", "Blocking a task requires a bounded reason", status_code=422)
    safe_reason = await repository._safe_text(reason[:500])
    reason_digest = _digest(safe_reason)
    return await _finish_event(
        db,
        repository,
        task,
        owner,
        expected_revision=expected_revision,
        values={
            "status": WorkBoardStatus.blocked,
            "block_kind": block_kind,
            "block_reason": safe_reason,
            "block_source_status": task.status.value,
            "review_expires_at": None,
        },
        kind="task.blocked",
        metadata={
            "block_kind": block_kind,
            "reason_code": block_kind,
            "reason_digest": reason_digest,
        },
    )


async def _reconcile_child_handoffs(
    db: AsyncSession,
    owner: WorkBoardOwner,
    child: WorkBoardTask,
) -> bool:
    """Materialize every current parent handoff under the owner transaction.

    Legacy migration can leave a child visibly blocked when the parent had no
    proof at startup.  An authenticated unblock is the explicit recovery
    action once a later readback is available.  This helper never invents
    proof and succeeds only when every parent has an immutable current pointer
    whose receipt still matches the parent's latest attempt.
    """
    links = list(
        (
            await db.execute(
                select(WorkBoardLink).where(
                    WorkBoardLink.child_task_id == child.task_id,
                    WorkBoardLink.owner_principal_id == owner.principal_id,
                    WorkBoardLink.owner_session_id == owner.session_id,
                )
            )
        ).scalars().all()
    )
    if not links:
        return False
    for link in links:
        parent = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == link.parent_task_id,
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if parent is None or parent.status is not WorkBoardStatus.done:
            return False
        await materialize_handoff_for_link(db, owner, parent, child, link)
        if not await current_handoff_is_verified(db, owner, parent, child, link):
            return False
    return True


async def unblock_task(
    db: AsyncSession,
    owner: WorkBoardOwner,
    task_id: str,
    *,
    expected_revision: int,
    resolution: str,
    repository: WorkBoardRepository | None = None,
) -> BoardMutation:
    repository = repository or WorkBoardRepository()
    # Serialize the block-kind, active-attempt, prerequisite, and CAS checks
    # with the recovery mutation.  A generic unblock must never race a typed
    # cancellation or pending-admission reconciliation path.
    await _begin_sqlite_immediate(db)
    task = await repository._owned_task(db, owner, task_id)
    await repository.require_generic_recovery_allowed(db, task)
    if task.status is not WorkBoardStatus.blocked:
        raise BoardError("illegal_transition", "Only blocked tasks can be unblocked", status_code=409)
    if not str(resolution or "").strip():
        raise BoardError("resolution_required", "Unblocking a task requires a bounded resolution", status_code=422)
    source = str(task.block_source_status or "")
    if source not in _SAFE_RESTORABLE_PHASES:
        raise BoardError("invalid_recovery_phase", "The blocked task has no safe restorable prior phase", status_code=409)
    if task.block_kind == "unknown_effect":
        raise BoardError("reconcile_external_effect", "Unknown external effects require independent reconciliation", status_code=409)
    if task.block_kind == "review_expired":
        raise BoardError(
            "review_renewal_required",
            "An expired review can be restored only through renew_review",
            status_code=409,
        )
    is_handoff_recovery = _is_handoff_reconciliation_block(
        task.block_kind,
        task.block_reason,
    )
    if task.block_kind not in {"operator", "dependency"} or (
        task.block_kind == "dependency" and not is_handoff_recovery
    ):
        raise BoardError(
            "typed_reconcile_required",
            "This typed block requires its dedicated recovery path before it can be unblocked",
            status_code=409,
        )
    active_attempt = await db.scalar(
        select(WorkBoardAttempt.attempt_id).where(
            WorkBoardAttempt.task_id == task.task_id,
            WorkBoardAttempt.ended_at.is_(None),
        )
    )
    if active_attempt is not None:
        raise BoardError(
            "attempt_reconcile_required",
            "A task with an active or pending attempt requires typed recovery before unblock",
            status_code=409,
        )
    readiness_error: str | None = None
    readiness_reason: str | None = None
    if source == WorkBoardStatus.ready.value:
        # The API preflight is only an operator-facing projection.  Recompute
        # the complete live admission result at the mutation boundary so a
        # formerly Ready card cannot be restored from stale cached readiness.
        # The M4 contract deliberately reopens a failed Ready recovery as
        # Todo; the managed dispatcher will then perform fresh admission and
        # persist a typed Blocked result if the gate is still unavailable.
        from src.work_board.dispatcher import _dispatcher

        readiness_error, readiness_reason = await _dispatcher._current_readiness(task)
    if source == WorkBoardStatus.ready.value:
        new_status = WorkBoardStatus.ready if readiness_error is None else WorkBoardStatus.todo
    else:
        new_status = WorkBoardStatus(source)
    expires = None
    if is_handoff_recovery:
        try:
            reconciled = await _reconcile_child_handoffs(db, owner, task)
        except BoardError as exc:
            if exc.code in {"handoff_materialization_required", "verified_readback_required"}:
                raise BoardError(
                    "handoff_materialization_required",
                    "The completed parent still needs an independently verified readback before this child can recover",
                    status_code=409,
                ) from exc
            raise
        if not reconciled:
            raise BoardError(
                "handoff_materialization_required",
                "The completed parent still needs an independently verified readback before this child can recover",
                status_code=409,
            )
        await db.refresh(task)
        await repository.validate_task_goal(db, owner, task)
    else:
        recent_events = list(
            (
                await db.execute(
                    select(WorkBoardEvent)
                    .where(
                        WorkBoardEvent.task_id == task.task_id,
                        WorkBoardEvent.owner_principal_id == owner.principal_id,
                        WorkBoardEvent.owner_session_id == owner.session_id,
                        WorkBoardEvent.kind == "task.blocked",
                    )
                    .order_by(WorkBoardEvent.event_id.desc())
                    .limit(8)
                )
            ).scalars().all()
        )
        same_reason_count = 0
        current_reason_digest = _digest(task.block_reason)
        for event in recent_events:
            metadata = _decode_object(event.metadata_json)
            if str(metadata.get("reason_digest") or "") == current_reason_digest:
                same_reason_count += 1
        if same_reason_count >= 2:
            raise BoardError(
                "repeated_block_reason",
                "The same blocking reason repeated; resolve the prerequisite before another retry",
                status_code=409,
            )
        await repository.validate_task_goal(db, owner, task)
        if task.block_kind == "operator" and source == WorkBoardStatus.review.value:
            await repository._validate_review_recovery(db, task)
        if task.block_kind == "dependency":
            unfinished = int(
                await db.scalar(
                    select(WorkBoardLink.link_id)
                    .join(WorkBoardTask, WorkBoardTask.task_id == WorkBoardLink.parent_task_id)
                    .where(
                        WorkBoardLink.child_task_id == task.task_id,
                        WorkBoardLink.owner_principal_id == owner.principal_id,
                        WorkBoardLink.owner_session_id == owner.session_id,
                        WorkBoardTask.status != WorkBoardStatus.done,
                    )
                    .limit(1)
                )
                is not None
            )
            if unfinished:
                raise BoardError("dependency_unfinished", "Every blocking parent must be Done before unblock", status_code=409)
    safe_resolution = await repository._safe_text(resolution[:1000])
    return await _finish_event(
        db,
        repository,
        task,
        owner,
        expected_revision=expected_revision,
        values={
            "status": new_status,
            "block_kind": None,
            "block_reason": None,
            "block_source_status": None,
            "review_expires_at": expires,
        },
        kind="task.unblocked",
        metadata={
            "resolution_digest": _digest(safe_resolution),
            "reopened_status": new_status.value,
            **(
                {
                    "readiness_code": readiness_error,
                    "readiness_reason_digest": _digest(readiness_reason),
                }
                if readiness_error
                else {}
            ),
        },
    )


@_formatter_readback_writer
async def renew_review(
    db: AsyncSession,
    owner: WorkBoardOwner,
    task_id: str,
    *,
    expected_revision: int,
    repository: WorkBoardRepository | None = None,
) -> BoardMutation:
    """Reopen only a review that the named reviewer explicitly renews."""
    repository = repository or WorkBoardRepository()
    # Renewal restores executable review authority; bind the goal check and
    # the Review -> blocked recovery CAS to one serialized live read.
    await _begin_sqlite_immediate(db)
    task = await repository._owned_task(db, owner, task_id)
    await db.refresh(task)
    if task.status is not WorkBoardStatus.blocked or task.block_kind != "review_expired":
        raise BoardError("review_renewal_not_required", "Only an expired review can be renewed", status_code=409)
    _require_reviewer(task, owner)
    await repository.validate_task_goal(db, owner, task)
    latest = (
        await db.execute(
            select(WorkBoardAttempt)
            .where(WorkBoardAttempt.task_id == task.task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None:
        await db.refresh(latest)
    if latest is None or await _verified_workflow_readback(db, task, latest) is None:
        raise BoardError("verified_readback_required", "Review renewal requires current verified evidence", status_code=409)
    await _require_current_review_attempt(db, task, latest)
    await _require_projected_intent(db, owner, task, latest)
    return await _finish_event(
        db,
        repository,
        task,
        owner,
        expected_revision=expected_revision,
        values={
            "status": WorkBoardStatus.review,
            "review_expires_at": _now() + REVIEW_TTL,
            "block_kind": None,
            "block_reason": None,
            "block_source_status": None,
        },
        kind="task.review_renewed",
        metadata={"attempt_id": latest.attempt_id, "reason_code": "review_renewed"},
    )


async def expire_review(
    db: AsyncSession,
    owner: WorkBoardOwner,
    task_id: str,
    *,
    repository: WorkBoardRepository | None = None,
) -> BoardMutation | None:
    repository = repository or WorkBoardRepository()
    guard = db.info.get("composition_read_guard")
    if guard is not None:
        from src.workspace.accounting_witness import CompositionReadGuard
        if type(guard) is not CompositionReadGuard or guard.db is not db or guard.closed:
            raise BoardError("composition_provider_invalid", "The original review session changed")
        if db.new or db.dirty or db.deleted:
            raise BoardError("dependency_write_requires_transaction_boundary", "Review expiry requires a clean original session")
        staged = await repository._owned_task(db, owner, task_id)
        if staged.capability_id not in {"browser.public-task.v1", "work.evidence-dossier.v1", "work.local-evidence-report.v1"}:
            raise BoardError("composition_native_writer_required", "This review retains its original writer")
        staged_revision = staged.task_revision
        await db.rollback()
        from src.runtime_plugins.ownership import begin_native_writer
        await begin_native_writer(db, owner="native_ingress")
        current = await repository._owned_task(db, owner, task_id)
        if current.task_revision != staged_revision:
            raise BoardRevisionConflict(task_id, staged_revision, current.task_revision)
    task = await repository._owned_task(db, owner, task_id)
    expiry = _aware(task.review_expires_at)
    if task.status is not WorkBoardStatus.review or expiry is None or expiry > _now():
        return None
    return await _finish_event(
        db,
        repository,
        task,
        owner,
        expected_revision=task.task_revision,
        values={
            "status": WorkBoardStatus.blocked,
            "block_kind": "review_expired",
            "block_reason": "The named review window expired; reviewer renewal is required.",
            "block_source_status": WorkBoardStatus.review.value,
            "review_expires_at": None,
        },
        kind="task.review_expired",
        metadata={"reason_code": "review_expired"},
    )


async def materialize_handoff_for_link(
    db: AsyncSession,
    owner: WorkBoardOwner,
    parent: WorkBoardTask,
    child: WorkBoardTask,
    link: WorkBoardLink,
) -> WorkBoardHandoff:
    """Create or select the current verified handoff for one dependency.

    This is deliberately callable from the repository's link transaction.  A
    Done parent therefore either gets a proof-bound immutable handoff before
    the link is committed, or the whole link operation fails with a typed,
    recoverable error.
    """
    if parent.status is not WorkBoardStatus.done:
        raise BoardError(
            "handoff_parent_not_done",
            "A parent handoff can be materialized only after the parent is Done",
            status_code=409,
        )
    selected_attempt = (
        await db.execute(
            select(WorkBoardAttempt)
            .where(WorkBoardAttempt.task_id == parent.task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    proof = await _verified_workflow_readback(db, parent, selected_attempt) if selected_attempt else None
    if selected_attempt is None or proof is None:
        raise BoardError(
            "handoff_materialization_required",
            "The completed parent has no independently verified readback; reconcile it before linking",
            status_code=409,
        )
    existing = (
        await db.execute(
            select(WorkBoardHandoff).where(
                WorkBoardHandoff.owner_principal_id == owner.principal_id,
                WorkBoardHandoff.owner_session_id == owner.session_id,
                WorkBoardHandoff.parent_task_id == parent.task_id,
                WorkBoardHandoff.child_task_id == child.task_id,
                WorkBoardHandoff.link_id == link.link_id,
                WorkBoardHandoff.source_attempt_id == selected_attempt.attempt_id,
                WorkBoardHandoff.source_task_revision == parent.task_revision,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = await _persist_one_handoff(
            db,
            owner,
            parent,
            child,
            selected_attempt,
            proof=proof,
            link=link,
        )
    elif link.current_handoff_id != existing.handoff_id:
        link.current_handoff_id = existing.handoff_id
        await db.flush()
    return existing


async def current_handoff_is_verified(
    db: AsyncSession,
    owner: WorkBoardOwner,
    parent: WorkBoardTask,
    child: WorkBoardTask,
    link: WorkBoardLink,
    *, _dispatch_readbacks=None,
) -> bool:
    """Check the link pointer against the parent's current verified attempt.

    Dispatch admission is read-only here: it may reject a stale or malformed
    pointer, but it never repairs one while deciding whether a child is Ready.
    """
    if (
        parent.status is not WorkBoardStatus.done
        or not link.current_handoff_id
        or parent.owner_principal_id != owner.principal_id
        or parent.owner_session_id != owner.session_id
        or child.owner_principal_id != owner.principal_id
        or child.owner_session_id != owner.session_id
    ):
        return False
    handoff = (
        await db.execute(
            select(WorkBoardHandoff).where(
                WorkBoardHandoff.handoff_id == link.current_handoff_id,
                WorkBoardHandoff.owner_principal_id == owner.principal_id,
                WorkBoardHandoff.owner_session_id == owner.session_id,
                WorkBoardHandoff.link_id == link.link_id,
                WorkBoardHandoff.parent_task_id == parent.task_id,
                WorkBoardHandoff.child_task_id == child.task_id,
                WorkBoardHandoff.source_task_revision == parent.task_revision,
            )
        )
    ).scalar_one_or_none()
    if handoff is None:
        return False
    attempt = (
        await db.execute(
            select(WorkBoardAttempt).where(
                WorkBoardAttempt.task_id == parent.task_id,
                WorkBoardAttempt.attempt_id == handoff.source_attempt_id,
                WorkBoardAttempt.workflow_run_id == handoff.workflow_run_id,
            )
        )
    ).scalar_one_or_none()
    proof = (await _dispatch_handoff_proof(db, owner, parent, child, link, _dispatch_readbacks)
        if _dispatch_readbacks is not None else await _verified_workflow_readback(db, parent, attempt)) if attempt is not None else None
    if proof is None:
        return False
    latest_attempt = (
        await db.execute(
            select(WorkBoardAttempt)
            .where(WorkBoardAttempt.task_id == parent.task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_attempt is None or latest_attempt.attempt_id != attempt.attempt_id:
        return False
    if handoff.schema_version != "work_board_handoff.v1":
        return False
    stored = _safe_verification_receipt(
        _decode_object(handoff.verification_json),
        require_complete=True,
    )
    return (
        stored.get("status") == "verified"
        and stored.get("workflow_run_id") == proof.get("workflow_run_id")
        and stored.get("content_sha256") == proof.get("content_sha256")
        and stored.get("readback_id") == proof.get("readback_id")
        and stored.get("verified_at") == proof.get("verified_at")
    )


def _handoff_payload(handoff: WorkBoardHandoff) -> dict[str, Any]:
    return {
        "handoff_id": handoff.handoff_id,
        "schema_version": handoff.schema_version,
        "parent_task_id": handoff.parent_task_id,
        "child_task_id": handoff.child_task_id,
        # Keep the immutable verified source attempt attached to the safe
        # payload.  A child must consume the exact attempt that produced the
        # independently verified receipt, rather than only a parent/revision
        # projection that could be re-bound later.
        "source_attempt_id": handoff.source_attempt_id,
        "status": "verified",
        "summary": handoff.summary,
        "artifact_refs": _handoff_receipt_refs(_decode_list(handoff.artifact_refs_json), limit=20),
        "result_refs": _handoff_receipt_refs(_decode_list(handoff.result_refs_json), limit=20),
        "verification_receipt": _safe_verification_receipt(
            _decode_object(handoff.verification_json),
            require_complete=True,
        ),
        "source_task_revision": handoff.source_task_revision,
        "risks": [
            risk for risk in _decode_list(handoff.risks_json)
            if isinstance(risk, str) and risk in _SAFE_RISKS
        ],
    }


def _blocked_handoff_payload(
    parent: WorkBoardTask,
    child: WorkBoardTask,
    reason: str,
) -> dict[str, Any]:
    return {
        "handoff_id": "",
        "schema_version": "work_board_handoff.v1",
        "parent_task_id": parent.task_id,
        "child_task_id": child.task_id,
        "source_attempt_id": None,
        "status": "blocked",
        "summary": _bounded_utf8(reason, 500),
        "artifact_refs": [],
        "result_refs": [],
        "verification_receipt": {
            "status": "blocked",
            "verified": False,
            "verification_status": "reconciliation_required",
            "reason_code": "verified_readback_required",
        },
        "source_task_revision": int(parent.task_revision),
        "risks": ["verification_scope_bounded"],
    }


async def parent_handoffs(
    db: AsyncSession,
    owner: WorkBoardOwner,
    task: WorkBoardTask,
    *, _dispatch_readbacks=None,
) -> list[dict[str, Any]]:
    """Return safe structured handoffs from completed parents only."""
    links = (
        await db.execute(
            select(WorkBoardLink)
            .where(
                WorkBoardLink.child_task_id == task.task_id,
                WorkBoardLink.owner_principal_id == owner.principal_id,
                WorkBoardLink.owner_session_id == owner.session_id,
            )
        )
    ).scalars().all()
    handoffs: list[dict[str, Any]] = []
    for link in links:
        parent_id = link.parent_task_id
        parent = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == parent_id,
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if parent is None or parent.status is not WorkBoardStatus.done:
            continue
        selected_attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == parent.task_id)
                .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        proof = (await _dispatch_handoff_proof(db, owner, parent, task, link, _dispatch_readbacks)
            if _dispatch_readbacks is not None else await _verified_workflow_readback(db, parent, selected_attempt)) if selected_attempt else None
        if selected_attempt is None or proof is None:
            handoffs.append(
                _blocked_handoff_payload(
                    parent,
                    task,
                    "The completed parent has no independently verified readback; reconcile it before dispatch",
                )
            )
            continue
        if not link.current_handoff_id:
            handoffs.append(
                _blocked_handoff_payload(
                    parent,
                    task,
                    "The completed parent handoff is pending one-time reconciliation",
                )
            )
            continue
        existing = (
            await db.execute(
                select(WorkBoardHandoff).where(
                    WorkBoardHandoff.handoff_id == link.current_handoff_id,
                    WorkBoardHandoff.owner_principal_id == owner.principal_id,
                    WorkBoardHandoff.owner_session_id == owner.session_id,
                    WorkBoardHandoff.parent_task_id == parent.task_id,
                    WorkBoardHandoff.child_task_id == task.task_id,
                    WorkBoardHandoff.link_id == link.link_id,
                    WorkBoardHandoff.source_attempt_id == selected_attempt.attempt_id,
                    WorkBoardHandoff.source_task_revision == parent.task_revision,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            handoffs.append(
                _blocked_handoff_payload(
                    parent,
                    task,
                    "The completed parent handoff is stale and requires reconciliation",
                )
            )
            continue
        if not await current_handoff_is_verified(db, owner, parent, task, link, _dispatch_readbacks=_dispatch_readbacks):
            handoffs.append(
                _blocked_handoff_payload(
                    parent,
                    task,
                    "The completed parent handoff is stale and requires reconciliation",
                )
            )
            continue
        handoffs.append(_handoff_payload(existing))
    return handoffs


def _handoff_receipt_refs(value: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """Project handoff evidence without forwarding filesystem destinations."""
    safe_refs = _safe_receipt_refs(value, limit=limit)
    return [
        {
            key: item[key]
            for key in item
            if key not in {"file_path", "target_path"}
        }
        for item in safe_refs
    ]


async def backfill_verified_handoffs(db: AsyncSession, *, limit: int = 256) -> int:
    """One-time migration helper for legacy Done-parent dependency links.

    It only considers links without a current pointer and writes a handoff
    when the latest bounded attempt has durable terminal success plus
    independent readback.  Unprovable links remain pointerless and therefore
    ineligible; the helper never invents a receipt.
    """
    parent_alias = aliased(WorkBoardTask)
    child_alias = aliased(WorkBoardTask)
    rows = list(
        (
            await db.execute(
                select(parent_alias, child_alias, WorkBoardLink)
                .join(WorkBoardLink, WorkBoardLink.parent_task_id == parent_alias.task_id)
                .join(child_alias, child_alias.task_id == WorkBoardLink.child_task_id)
                .where(
                    parent_alias.status == WorkBoardStatus.done,
                    WorkBoardLink.current_handoff_id.is_(None),
                    WorkBoardLink.owner_principal_id == parent_alias.owner_principal_id,
                    WorkBoardLink.owner_session_id == parent_alias.owner_session_id,
                    child_alias.owner_principal_id == parent_alias.owner_principal_id,
                    child_alias.owner_session_id == parent_alias.owner_session_id,
                    or_(
                        child_alias.status.in_(
                            (
                                WorkBoardStatus.triage,
                                WorkBoardStatus.todo,
                                WorkBoardStatus.ready,
                            )
                        ),
                        and_(
                            child_alias.status == WorkBoardStatus.blocked,
                            or_(
                                child_alias.block_kind.is_(None),
                                child_alias.block_kind == "dependency",
                            ),
                            or_(
                                child_alias.block_reason.is_(None),
                                child_alias.block_reason.notin_(_HANDOFF_RECONCILIATION_REASONS),
                            ),
                        ),
                        and_(
                            child_alias.status == WorkBoardStatus.blocked,
                            child_alias.block_kind == "dependency",
                            child_alias.block_reason.in_(_HANDOFF_RECONCILIATION_REASONS),
                        ),
                    ),
                )
                .order_by(WorkBoardLink.created_at.asc(), WorkBoardLink.link_id.asc())
                .limit(max(1, min(int(limit), 256)))
            )
        ).all()
    )
    count = 0
    for parent, child, link in rows:
        owner = WorkBoardOwner(
            principal_id=parent.owner_principal_id,
            session_id=parent.owner_session_id,
        )
        was_reconciliation_block = (
            child.status is WorkBoardStatus.blocked
            and child.block_kind == "dependency"
            and _is_handoff_reconciliation_block(
                child.block_kind,
                child.block_reason,
            )
        )
        try:
            await materialize_handoff_for_link(db, owner, parent, child, link)
        except BoardError as exc:
            if exc.code == "handoff_materialization_required":
                # Legacy links without proof must be visible as recoverable
                # blocked work.  Do not overwrite a Running task or another
                # stronger recovery state, and do not repeat the mutation on
                # later startups once this exact receipt is present.
                if child.status in {
                    WorkBoardStatus.triage,
                    WorkBoardStatus.todo,
                    WorkBoardStatus.ready,
                } or (
                    child.status is WorkBoardStatus.blocked
                    and child.block_kind in {None, "dependency"}
                ):
                    if not was_reconciliation_block:
                        source_status = (
                            child.block_source_status
                            if child.status is WorkBoardStatus.blocked
                            else child.status.value
                        )
                        child.status = WorkBoardStatus.blocked
                        child.block_kind = "dependency"
                        child.block_reason = _HANDOFF_RECONCILIATION_REASON
                        child.block_source_status = source_status or WorkBoardStatus.todo.value
                        child.task_revision = int(child.task_revision) + 1
                        child.updated_at = _now()
                        db.add(
                            WorkBoardEvent(
                                task_id=child.task_id,
                                owner_principal_id=owner.principal_id,
                                owner_session_id=owner.session_id,
                                actor_principal_id="work-board-migration",
                                actor_session_id="work-board-migration",
                                kind="task.handoff_reconciliation_required",
                                metadata_json=json.dumps(
                                    {
                                        "parent_task_id": parent.task_id,
                                        "reason_code": "handoff_materialization_required",
                                        "task_revision": child.task_revision,
                                    },
                                    separators=(",", ":"),
                                ),
                            )
                        )
            continue
        if was_reconciliation_block:
            # The first startup pass may have had no proof and left this
            # child blocked. A later pass may persist the exact fresh
            # readback, but startup must not restore Ready from cached
            # authority. Keep the block and its safe prior phase until an
            # authenticated operator recovery rechecks the goal, handoff,
            # capability, schedule, and current session together.
            source_status = str(child.block_source_status or "")
            if source_status in _SAFE_RESTORABLE_PHASES:
                child.task_revision = int(child.task_revision) + 1
                child.updated_at = _now()
                db.add(
                    WorkBoardEvent(
                        task_id=child.task_id,
                        owner_principal_id=owner.principal_id,
                        owner_session_id=owner.session_id,
                        actor_principal_id="work-board-migration",
                        actor_session_id="work-board-migration",
                        kind="task.handoff_reconciled",
                        metadata_json=json.dumps(
                            {
                                "parent_task_id": parent.task_id,
                                "handoff_id": link.current_handoff_id,
                                "recovery_required": True,
                                "recovery_action": "unblock",
                                "block_source_status": source_status,
                                "status": child.status.value,
                                "task_revision": child.task_revision,
                            },
                            separators=(",", ":"),
                        ),
                    )
                )
            count += 1
            continue
        count += 1
    return count


async def _persist_one_handoff(
    db: AsyncSession,
    owner: WorkBoardOwner,
    parent: WorkBoardTask,
    child: WorkBoardTask,
    attempt: WorkBoardAttempt,
    *,
    proof: Mapping[str, Any],
    link: WorkBoardLink | None = None,
) -> WorkBoardHandoff:
    if link is None:
        link = (
            await db.execute(
                select(WorkBoardLink).where(
                    WorkBoardLink.owner_principal_id == owner.principal_id,
                    WorkBoardLink.owner_session_id == owner.session_id,
                    WorkBoardLink.parent_task_id == parent.task_id,
                    WorkBoardLink.child_task_id == child.task_id,
                )
            )
        ).scalar_one_or_none()
    if link is None:
        raise BoardError("handoff_link_missing", "A persisted dependency link is required for handoff", status_code=409)
    safe_proof = _safe_verification_receipt(proof, require_complete=True)
    if not safe_proof:
        raise BoardError("verified_readback_required", "The handoff proof is not a bounded verified receipt", status_code=409)
    risks: list[str] = []
    for item in (*_decode_list(parent.result_refs_json), *_decode_list(parent.artifact_refs_json)):
        if not isinstance(item, Mapping):
            continue
        reason = str(item.get("reason_code") or "")
        if reason in _SAFE_RISKS and reason not in risks:
            risks.append(reason)
    safe_artifact_refs = _handoff_receipt_refs(
        _decode_list(parent.artifact_refs_json),
        limit=20,
    )
    safe_result_refs = _handoff_receipt_refs(
        _decode_list(parent.result_refs_json),
        limit=20,
    )
    capability_id = str(parent.capability_id or "").strip()
    if not _SAFE_RECEIPT_ID.fullmatch(capability_id):
        capability_id = "legacy.untyped"
    result_summary = (
        "Registered capability output completed and passed independent readback."
        if capability_id != "legacy.untyped"
        else "Workflow output completed and passed independent readback."
    )
    # Do not copy the parent card's free-form title or body into a cross-task
    # handoff. The outcome sentence is fixed by this server-side path after
    # the durable run and independent readback have both been verified; the
    # structured counts and capability identity give the child a useful,
    # bounded result summary without forwarding source content.
    summary = _bounded_utf8(
        json.dumps(
            {
                "kind": "verified_task_handoff",
                "result": {
                    "capability_id": capability_id,
                    "workflow_status": "succeeded",
                    "verification": "independent_readback_passed",
                    "result_summary": result_summary,
                    "result_receipt_count": len(safe_result_refs),
                    "artifact_count": len(safe_artifact_refs),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        1_000,
    )
    artifact_refs_json = json.dumps(safe_artifact_refs, separators=(",", ":"))
    result_refs_json = json.dumps(safe_result_refs, separators=(",", ":"))
    verification_json = json.dumps(safe_proof, separators=(",", ":"))
    risks_json = json.dumps(risks, separators=(",", ":"))
    if len(verification_json.encode("utf-8")) > 4_096:
        raise BoardError("handoff_proof_too_large", "The verified handoff proof exceeds its byte limit", status_code=409)
    if len(artifact_refs_json.encode("utf-8")) > 8_192 or len(result_refs_json.encode("utf-8")) > 8_192:
        raise BoardError("handoff_receipts_too_large", "The verified handoff receipts exceed their byte limit", status_code=409)
    handoff = WorkBoardHandoff(
        schema_version="work_board_handoff.v1",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        parent_task_id=parent.task_id,
        child_task_id=child.task_id,
        link_id=link.link_id,
        source_attempt_id=attempt.attempt_id,
        workflow_run_id=str(attempt.workflow_run_id),
        source_task_revision=int(parent.task_revision),
        summary=summary,
        artifact_refs_json=artifact_refs_json,
        result_refs_json=result_refs_json,
        verification_json=verification_json,
        risks_json=risks_json,
    )
    db.add(handoff)
    await db.flush()
    # Select this immutable version for the dependency in the same
    # transaction as its insert.  Historical rows remain addressable.
    link.current_handoff_id = handoff.handoff_id
    await db.flush()
    return handoff


async def _persist_child_handoffs(
    db: AsyncSession,
    owner: WorkBoardOwner,
    parent: WorkBoardTask,
    attempt: WorkBoardAttempt,
    *,
    proof: Mapping[str, Any] | None,
) -> None:
    if proof is None:
        return
    child_ids = (
        await db.execute(
            select(WorkBoardLink.child_task_id).where(
                WorkBoardLink.parent_task_id == parent.task_id,
                WorkBoardLink.owner_principal_id == owner.principal_id,
                WorkBoardLink.owner_session_id == owner.session_id,
            )
        )
    ).scalars().all()
    for child_id in child_ids:
        child = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == child_id,
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if child is None:
            continue
        link = (
            await db.execute(
                select(WorkBoardLink).where(
                    WorkBoardLink.owner_principal_id == owner.principal_id,
                    WorkBoardLink.owner_session_id == owner.session_id,
                    WorkBoardLink.parent_task_id == parent.task_id,
                    WorkBoardLink.child_task_id == child.task_id,
                )
            )
        ).scalar_one_or_none()
        if link is None:
            continue
        exists = (
            await db.execute(
                select(WorkBoardHandoff.handoff_id).where(
                    WorkBoardHandoff.owner_principal_id == owner.principal_id,
                    WorkBoardHandoff.owner_session_id == owner.session_id,
                    WorkBoardHandoff.parent_task_id == parent.task_id,
                    WorkBoardHandoff.child_task_id == child.task_id,
                    WorkBoardHandoff.link_id == link.link_id,
                    WorkBoardHandoff.source_attempt_id == attempt.attempt_id,
                    WorkBoardHandoff.source_task_revision == parent.task_revision,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            await _persist_one_handoff(db, owner, parent, child, attempt, proof=proof, link=link)


__all__ = [
    "REVIEW_TTL",
    "backfill_verified_handoffs",
    "block_task",
    "complete_review",
    "current_handoff_is_verified",
    "expire_review",
    "parent_handoffs",
    "request_changes",
    "request_review",
    "renew_review",
    "unblock_task",
]
