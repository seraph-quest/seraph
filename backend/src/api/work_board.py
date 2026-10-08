"""Authenticated API for the canonical operator work board."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.auth.service import AuthenticatedOperator, AuthFailure
from src.approval.repository import approval_repository
from src.db.engine import get_session
from src.db.models import (
    CalendarPrepReceipt,
    WorkBoardAttempt,
    WorkBoardComment,
    WorkBoardLink,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
    Goal,
)
from src.vault import redaction as vault_redaction
from src.work_board.contracts import (
    WorkBoardActionRequest,
    WORK_BOARD_AUTHENTICATED_BLOCK_KINDS,
    WorkBoardCommentCreate,
    WorkBoardInputArtifactCreate,
    WorkBoardInputArtifactDelete,
    WorkBoardLinkCreate,
    WorkBoardLinkDelete,
    WorkBoardOwner,
    WorkBoardTaskCreate,
    WorkBoardTaskPatch,
    WorkBoardProposalAccept,
    WorkBoardProposalReject,
    WorkBoardProposalRequest,
    WorkBoardRoutinePublicationPrepareRequest,
    WorkBoardRoutinePublicationRecoverRequest,
)
from src.work_board.repository import (
    BoardError,
    BoardMutation,
    WorkBoardRepository,
    _safe_receipt_refs,
    effective_browser_limits,
    safe_board_reference,
    safe_board_identifier,
    safe_sha256_digest,
    safe_workflow_run_id,
)
from src.work_board.input_artifacts import (
    delete_input_artifact,
    prepare_input_artifact,
    read_input_artifact_metadata,
)
from src.work_board.events import (
    _SAFE_EVENT_BLOCK_KINDS,
    _SAFE_EVENT_OUTCOMES,
    _SAFE_EVENT_STATUSES,
    _event_payload,
)
from src.goals.repository import deserialize_admission_budget
from src.work_board.dispatcher import TypedInputError, _dispatcher, _parse_typed_input
from src.work_board import review as review_service
from src.work_board import triage as triage_service
from src.work_board import pipelines as pipeline_service
from src.guardian.opportunity_contracts import OpportunityError
from src.work_board.pipeline_contracts import PipelinePreviewRequest, PipelineAcceptRequest, PipelineAdvanceRequest, PipelineRevisionRequest, PipelineReuseRequest, REPORT
from src.work_board.tool_package_contracts import ToolPackageRecoverRequest
from src.work_board.document_compare_contracts import DocumentPairReserve, DocumentPairMutation, DocumentControlRequest
from src.work_board.time import serialize_utc_datetime
from src.security.trust_contract import AuthorityGrant
from src.security.site_policy import _parse_rules
from config.settings import settings
from src.workflows.job_runtime import durable_job_repository, DurableJobError
from src.workflows.routines import (
    RoutinePublicationRequest,
    RoutineError,
    _job_checkpoint,
    routine_service,
)
from src.work_board.research_contracts import ResearchControlRequest


router = APIRouter(prefix="/work-board")
repository = WorkBoardRepository()
# Use the same managed dispatcher instance as the scheduler so cancellation
# can reach an inline GoalSnapshot worker admitted by the scheduler pass.
dispatcher = _dispatcher


@router.get("/tasks/{task_id}/tool-package")
async def read_tool_package_state(request: Request, task_id: str):
    from src.work_board.tool_package_control import snapshot
    try:
        async with get_session() as db:
            return await snapshot(dispatcher.jobs,db,_owner(_operator(request)),task_id)
    except BoardError as exc:
        _raise_board_error(exc)
    except (DurableJobError,ValueError,TypeError,KeyError,OSError):
        raise HTTPException(status_code=409,detail={"code":"tool_package_original_binding_required"})


@router.post("/tasks/{task_id}/tool-package/recover")
async def recover_tool_package(request: Request,task_id: str,body: ToolPackageRecoverRequest):
    from src.work_board.tool_package_control import recover,snapshot
    try:
        owner=_owner(_operator(request));result=await recover(dispatcher,owner,task_id,body)
        async with get_session() as db:
            return {"recovery":result,"tool_package":await snapshot(dispatcher.jobs,db,owner,task_id)}
    except BoardError as exc:
        _raise_board_error(exc)
    except (DurableJobError,ValueError,TypeError,KeyError,OSError):
        raise HTTPException(status_code=409,detail={"code":"tool_package_current_reserved_output_and_cleanup_required"})


@router.get("/tasks/{task_id}/tool-package-output")
async def read_tool_package_output(request: Request,task_id: str):
    from fastapi import Response
    from src.work_board.tool_package_control import bound
    from src.work_board.tool_package_native import private_read_guard
    try:
        async with get_session() as db:
            task,attempt,run=await bound(db,_owner(_operator(request)),task_id)
            if attempt.ended_at is None or task.status not in {WorkBoardStatus.done,WorkBoardStatus.review}:
                raise BoardError('tool_package_readback_pending','Independent formatter readback is not complete')
            async with private_read_guard(db,task,attempt,run) as staged:
                raw=staged.raw
        return Response(content=raw,media_type='text/plain',headers={'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError,TypeError,KeyError,OSError):
        raise HTTPException(status_code=409,detail={"code":"tool_package_output_readback_required"})


@router.get("/tasks/{task_id}/research")
async def read_research_state(request: Request, task_id: str):
    from src.work_board.research_control import snapshot
    try:
        async with get_session() as db:
            return await snapshot(dispatcher.jobs, db, _owner(_operator(request)), task_id)
    except BoardError as exc:
        _raise_board_error(exc)
    except (DurableJobError, AuthFailure, ValueError, TypeError, KeyError, OSError):
        raise HTTPException(status_code=409, detail={"code": "research_current_binding_unavailable"})


@router.post("/tasks/{task_id}/research/recover")
async def recover_research(request: Request, task_id: str, body: ResearchControlRequest):
    try:
        result = await dispatcher.recover_research(_owner(_operator(request)), task_id, body)
        async with get_session() as db:
            from src.work_board.research_control import snapshot
            return {"recovery": result, "research": await snapshot(dispatcher.jobs, db, _owner(_operator(request)), task_id)}
    except BoardError as exc:
        _raise_board_error(exc)
    except (DurableJobError, AuthFailure, ValueError, TypeError, KeyError, OSError):
        raise HTTPException(status_code=409, detail={"code": "research_current_authority_or_artifact_required"})


@router.post("/tasks/{task_id}/research/cancel")
async def cancel_research(request: Request, task_id: str, body: ResearchControlRequest):
    try:
        result = await dispatcher.cancel_research(_owner(_operator(request)), task_id, body)
        async with get_session() as db:
            from src.work_board.research_control import snapshot
            return {"cancellation": result, "research": await snapshot(dispatcher.jobs, db, _owner(_operator(request)), task_id)}
    except BoardError as exc:
        _raise_board_error(exc)
    except (DurableJobError, AuthFailure, ValueError, TypeError, KeyError, OSError):
        raise HTTPException(status_code=409, detail={"code": "research_current_cancellation_proof_required"})


@router.get("/tasks/{task_id}/research-report")
async def read_research_report(request: Request, task_id: str):
    from fastapi import Response
    from src.work_board.research_readback import verified_dossier
    operator = _operator(request)
    async with get_session() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id,
            WorkBoardTask.owner_principal_id == operator.principal.principal_id,
            WorkBoardTask.owner_session_id == operator.session_id,
            WorkBoardTask.capability_id == "work.research-dossier.v1"))
        if task is None:
            raise HTTPException(status_code=404, detail="Research task unavailable")
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))
        if attempt is None or attempt.ended_at is None or task.status not in {WorkBoardStatus.review, WorkBoardStatus.done}:
            raise HTTPException(status_code=409, detail="Research dossier requires completed independent readback")
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
        try:
            _binding, raw = await verified_dossier(db, task, attempt, run)
        except (ValueError, TypeError, KeyError, OSError, BoardError):
            raise HTTPException(status_code=409, detail="Research dossier readback requires recovery")
        return Response(content=raw, media_type="text/plain", headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post("/tasks/{task_id}/pipeline-preview")
async def preview_artifact_pipeline(request: Request, task_id: str, body: PipelinePreviewRequest):
    owner = _owner(_operator(request))
    try:
        async with get_session() as db:
            result = await pipeline_service.preview(db, owner, task_id, body)
            await db.commit()
            return result
    except BoardError as exc:
        _raise_board_error(exc)


@router.get("/pipelines/{operation_id}")
async def read_artifact_pipeline(request: Request, operation_id: str):
    owner = _owner(_operator(request))
    try:
        async with get_session() as db:
            return await pipeline_service.read(db, owner, operation_id)
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/pipelines/{operation_id}/accept")
async def accept_artifact_pipeline(request: Request, operation_id: str, body: PipelineAcceptRequest):
    operator = _operator(request)
    owner = _owner(operator)
    try:
        async with get_session() as linked_db:
            from src.db.models import WorkBoardProposal
            linked = await linked_db.get(WorkBoardProposal, operation_id)
            linked_plan = bool(linked and linked.opportunity_id)
        if linked_plan:
            from src.guardian.opportunity_plans import accept_report_plan
            return await accept_report_plan(operator=operator, owner=owner, operation_id=operation_id, request=body)
        async with get_session() as db:
            result = await pipeline_service.accept(db, owner, operation_id, body)
            await db.commit()
            return result
    except BoardError as exc:
        _raise_board_error(exc)
    except OpportunityError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.post("/pipelines/{operation_id}/advance")
async def advance_artifact_pipeline(request: Request, operation_id: str, body: PipelineAdvanceRequest):
    owner = _owner(_operator(request))
    try:
        failure = None
        async with get_session() as db:
            try:
                result = await pipeline_service.advance(db, owner, operation_id, body.expected_revision)
            except BoardError as exc:
                if not db.info.get("pipeline_authority_frozen"):
                    raise
                failure = exc
            await db.commit()
        if failure is not None:
            raise failure
        return result
    except BoardError as exc:
        _raise_board_error(exc)
    except OpportunityError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.post("/pipelines/{operation_id}/revision")
async def stage_artifact_pipeline_revision(request: Request, operation_id: str, body: PipelineRevisionRequest):
    owner = _owner(_operator(request))
    try:
        async with get_session() as db:
            result = await pipeline_service.stage_revision(db, owner, operation_id, body)
            await db.commit()
            return result
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/pipelines/{operation_id}/reuse-preview")
async def reuse_artifact_pipeline_output(request: Request, operation_id: str, body: PipelineReuseRequest):
    owner = _owner(_operator(request))
    try:
        async with get_session() as db:
            result = await pipeline_service.reuse_preview(db, owner, operation_id, body)
            await db.commit()
            return result
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/pipelines/{operation_id}/quiesce")
async def quiesce_artifact_pipeline_revision(request: Request, operation_id: str, body: PipelineAdvanceRequest):
    owner = _owner(_operator(request))
    try:
        return await pipeline_service.quiesce_revision(owner, operation_id, body.expected_revision,
            dispatcher=dispatcher, session_provider=get_session)
    except BoardError as exc:
        _raise_board_error(exc)


@router.get("/pipelines/{operation_id}/report")
async def read_artifact_pipeline_report(request: Request, operation_id: str):
    from fastapi.responses import Response
    from src.work_board.pipeline_cpu import read_output
    owner = _owner(_operator(request))
    try:
        async with get_session() as db:
            _row, operation = await pipeline_service.owned(db, owner, operation_id)
            if len(operation.get("steps", [])) != 3:
                raise BoardError("pipeline_output_required", "The local report is not ready", status_code=409)
            task = await repository.get_task(db, owner, operation["steps"][2]["task_ref"])
            if task.capability_id != REPORT:
                raise BoardError("pipeline_output_unverified", "The fixed report binding changed", status_code=409)
            output = await pipeline_service.verified_output(db, owner, task)
            return Response(content=read_output(output["file_path"], output["content_sha256"]),
                media_type="text/plain", headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
    except BoardError as exc:
        _raise_board_error(exc)

_RECOVERY_ACTIONS = frozenset(
    {
        "unblock",
        "retry",
        "cancel",
        "approve_existing_run",
        "reconcile_external_effect",
        "reconcile_admission_binding",
        "restore_prerequisite",
        "configure_goal_success_criterion",
        "renew_review",
        "prepare_routine_publication",
        "resume_routine_publication",
    }
)


_BROWSER_POLICY_RULE_LIMIT = 50

_BROWSER_EXECUTION_STATUSES = frozenset(
    {
        "accepted",
        "queued",
        "running",
        "paused",
        "awaiting_approval",
        "succeeded",
        "failed",
        "blocked",
        "cancelled",
        "degraded",
        "unknown_external_effect",
        "cost_liability",
    }
)

_CALENDAR_EXECUTION_STATUSES = _BROWSER_EXECUTION_STATUSES
_CALENDAR_RESULT_PATH_RE = re.compile(
    r"^artifacts/work-board/calendar/result-[0-9a-f]{32}\.json$"
)
_CALENDAR_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_CALENDAR_READ_STATUSES = frozenset({"succeeded", "blocked", "unknown"})


def _strict_bounded_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if type(value) is not int or value < minimum or value > maximum:
        return None
    return value


def _safe_browser_projection_text(value: Any, *, max_length: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    bounded = value.strip()
    if not bounded or len(bounded) > max_length or "\n" in bounded or "\r" in bounded:
        return None
    return bounded


def _browser_projection_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _browser_execution_progress(projection: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Read one current, server-retained bounded progress checkpoint.

    The runner emits action and request counters together for current browser
    checkpoints. A legacy checkpoint without the counter remains explicitly
    unknown instead of borrowing an older network receipt.
    """

    from src.browser.task_runner import observed_browser_request_receipts
    observed = observed_browser_request_receipts(projection)
    checkpoints = projection.get("checkpoints")
    if not isinstance(checkpoints, list):
        return None, len(observed) if observed else None
    # Checkpoints are append-only and bounded by the durable runtime. Walk
    # newest first and use the first recognized browser progress checkpoint as
    # one atomic projection.
    for checkpoint in reversed(checkpoints[-50:]):
        if not isinstance(checkpoint, Mapping):
            continue
        payload = checkpoint.get("payload")
        if not isinstance(payload, Mapping):
            continue
        checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if not (
            checkpoint_id.startswith("action-")
            or checkpoint_id == "network-dispatch"
            or checkpoint_id.startswith("network-progress-")
        ):
            continue
        action_index = _strict_bounded_int(payload.get("action_index"), minimum=-1, maximum=7)
        request_count = _strict_bounded_int(payload.get("request_count"), minimum=0, maximum=32)
        if observed:
            request_count = max(request_count or 0, len(observed))
        return action_index, request_count
    return None, len(observed) if observed else None


def _browser_artifact_projection(projection: Mapping[str, Any]) -> dict[str, str] | None:
    """Return one verified artifact/readback pair from a durable job."""

    artifacts = projection.get("artifacts")
    effects = projection.get("effects")
    if not isinstance(artifacts, list) or not isinstance(effects, list):
        return None
    artifact_rows: list[dict[str, str | None]] = []
    for item in reversed(artifacts[-100:]):
        if not isinstance(item, Mapping) or item.get("exists") is not True:
            continue
        digest = safe_sha256_digest(item.get("content_sha256"))
        artifact_path = _safe_receipt_refs([item], limit=1)
        if digest is None or not artifact_path:
            continue
        safe_item = artifact_path[0]
        if safe_item.get("artifact_type") != "browser_public_task_result":
            continue
        path = safe_item.get("file_path")
        if not isinstance(path, str):
            continue
        if not (
            path.startswith("artifacts/work-board/browser/")
            and re.fullmatch(r"artifacts/work-board/browser/result-[0-9a-f]{32}\.json", path)
        ):
            continue
        artifact_id = safe_board_identifier(safe_item.get("artifact_id"), max_length=256)
        artifact_rows.append({"artifact_id": artifact_id, "file_path": path, "digest": digest})

    for effect in reversed(effects[-100:]):
        if not isinstance(effect, Mapping):
            continue
        if effect.get("receipt_kind") != "readback" or effect.get("effect_type") != "browser_public_task_result":
            continue
        if effect.get("status") not in {"succeeded", "read_back", "reconciled"}:
            continue
        details = effect.get("details")
        if not isinstance(details, Mapping) or details.get("verified") is not True:
            continue
        readback_id = safe_board_identifier(effect.get("readback_id"), max_length=256)
        verified_at = _safe_browser_projection_text(effect.get("verified_at"), max_length=64)
        readback_path = _safe_receipt_refs([effect], limit=1)
        if not readback_id or not verified_at or not readback_path:
            continue
        safe_effect = readback_path[0]
        path = safe_effect.get("target_path")
        digest = safe_sha256_digest(safe_effect.get("content_sha256") or safe_effect.get("target_digest"))
        if not isinstance(path, str) or digest is None:
            continue
        if not (
            path.startswith("artifacts/work-board/browser/")
            and re.fullmatch(r"artifacts/work-board/browser/result-[0-9a-f]{32}\.json", path)
        ):
            continue
        for artifact in artifact_rows:
            if artifact["file_path"] != path or artifact["digest"] != digest:
                continue
            return {
                "readback_id": readback_id,
                "artifact_id": artifact["artifact_id"],
                "file_path": path,
                "content_sha256": digest,
            }
    return None


def _browser_cleanup_projection(projection: Mapping[str, Any]) -> tuple[str, str]:
    """Return cleanup and memory state from the typed cleanup effect only."""

    effects = projection.get("effects")
    if not isinstance(effects, list):
        return "unknown", "unknown"
    for effect in reversed(effects[-100:]):
        if not isinstance(effect, Mapping):
            continue
        if effect.get("receipt_kind") != "effect" or effect.get("effect_type") != "browser_context_cleanup":
            continue
        details = effect.get("details")
        if not isinstance(details, Mapping):
            return "unknown", "unknown"
        cleanup = details.get("cleanup_status")
        cleanup_status = cleanup if cleanup in {"cleanup_verified", "not_needed", "cleanup_unknown"} else "unknown"
        if cleanup_status == "cleanup_verified":
            if effect.get("status") != "succeeded":
                cleanup_status = "unknown"
        elif cleanup_status == "not_needed":
            if effect.get("status") != "succeeded" or details.get("context_not_started") is not True:
                cleanup_status = "unknown"
        elif effect.get("status") != "succeeded":
            cleanup_status = "unknown"
        memory_status = "no_learning" if details.get("memory_status") == "no_learning" else "unknown"
        return cleanup_status, memory_status
    return "unknown", "unknown"


async def _browser_execution_payload(
    task: WorkBoardTask,
    attempt: WorkBoardAttempt,
    *,
    projection: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Project verified browser progress from its exact durable root.

    This helper intentionally returns no browser execution object when any
    owner, goal, task/attempt, capability, or artifact binding is incomplete.
    The Work Board row is already owner-scoped; the second fence protects the
    durable job projection from being treated as a matching foreign receipt.
    """

    if task.capability_id != "browser.public-task.v1":
        return None
    attempt_id = safe_board_identifier(attempt.attempt_id, max_length=256)
    task_id = safe_board_identifier(task.task_id, max_length=256)
    workflow_run_id = safe_workflow_run_id(attempt.workflow_run_id)
    if not task_id or not attempt_id or not workflow_run_id:
        return None
    expected_job_id = f"browser-task:{task_id}:{attempt_id}"
    if workflow_run_id != expected_job_id:
        return None
    if projection is None:
        try:
            projection = await durable_job_repository.get_job(workflow_run_id)
        except Exception:
            return None
    if not isinstance(projection, Mapping):
        return None
    if attempt.task_id != task.task_id:
        return None
    status = projection.get("status")
    if status not in _BROWSER_EXECUTION_STATUSES:
        return None
    owner = projection.get("owner")
    authority = projection.get("declared_authority")
    identity = projection.get("idempotency")
    if not isinstance(owner, Mapping) or not isinstance(authority, Mapping) or not isinstance(identity, Mapping):
        return None
    owner_principal = safe_board_identifier(task.owner_principal_id, max_length=256)
    owner_session = safe_board_identifier(task.owner_session_id, max_length=512)
    task_digest = safe_sha256_digest(task.typed_input_digest)
    if (
        not owner_principal
        or not owner_session
        or not task_digest
        or not safe_board_identifier(task.input_artifact_id, max_length=512)
        or not safe_board_identifier(task.goal_id, max_length=256)
        or type(task.goal_revision) is not int
        or task.goal_revision < 1
    ):
        return None
    if (
        projection.get("job_id") != expected_job_id
        or projection.get("run_identity") != expected_job_id
        or projection.get("job_kind") != "browser_public_task"
        or projection.get("capability_version") != "1"
        or owner.get("kind") != "service"
        or owner.get("principal_id") != "service:browser-task"
        or owner.get("service_id") != "service:browser-task"
        or projection.get("session_id") != owner_session
        or projection.get("operator_session_id") != owner_session
        or projection.get("goal_id") != task.goal_id
        or projection.get("goal_revision") != task.goal_revision
        or projection.get("root_run_identity") != expected_job_id
        or projection.get("parent_run_identity") not in (None, "")
        or projection.get("parent_job_id") not in (None, "")
        or identity.get("scope") != "work-board-attempt"
        or identity.get("key") != f"{task_id}:{attempt_id}"
    ):
        return None
    if (
        authority.get("principal") != "service:browser-task"
        or authority.get("owner_kind") != "service"
        or authority.get("service_id") != "service:browser-task"
        or authority.get("capability_id") != "browser.public-task.v1"
        or authority.get("capability_version") != "1"
        or authority.get("operator_owner_principal_id") != owner_principal
        or authority.get("operator_owner_session_id") != owner_session
        or authority.get("goal_owner_principal_id") != owner_principal
        or authority.get("goal_owner_session_id") != owner_session
        or authority.get("goal_id") != task.goal_id
        or authority.get("goal_revision") != task.goal_revision
        or authority.get("board_task_revision") != int(attempt.task_revision_at_claim) + 1
        or int(task.task_revision or 0) < int(authority.get("board_task_revision") or 0)
        or authority.get("board_fencing_token") != attempt.fencing_token
        or authority.get("priority") != task.priority
        or authority.get("input_artifact_id") != task.input_artifact_id
        or authority.get("input_artifact_digest") != task_digest
        or authority.get("input_envelope_digest") != task_digest
        or safe_sha256_digest(authority.get("browser_input_digest")) is None
        or safe_sha256_digest(authority.get("action_consent_digest")) is None
    ):
        return None
    input_digest = safe_sha256_digest(projection.get("input_digest"))
    authority_digest = safe_sha256_digest(projection.get("authority_digest"))
    run_fingerprint = safe_sha256_digest(projection.get("run_fingerprint"))
    if input_digest is None or authority_digest is None or run_fingerprint != input_digest:
        return None
    if _browser_projection_digest(dict(authority)) != authority_digest:
        return None
    action_count = _strict_bounded_int(authority.get("action_count"), minimum=1, maximum=8)
    if action_count is None:
        return None
    action_index, request_count = _browser_execution_progress(projection)
    cleanup_status, memory_status = _browser_cleanup_projection(projection)
    proof = _browser_artifact_projection(projection)
    return {
        "capability_id": "browser.public-task.v1",
        "job_id": expected_job_id,
        "durable_status": status,
        "action_index": action_index,
        "action_count": action_count,
        "request_count": request_count,
        "cleanup_status": cleanup_status,
        "memory_status": memory_status,
        "readback_id": proof.get("readback_id") if proof else None,
        "artifact_id": proof.get("artifact_id") if proof else None,
        "file_path": proof.get("file_path") if proof else None,
        "content_sha256": proof.get("content_sha256") if proof else None,
    }


def _calendar_projection_text(value: Any, *, max_length: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    bounded = value.strip()
    if not bounded or len(bounded) > max_length or "\n" in bounded or "\r" in bounded:
        return None
    return bounded


def _calendar_projection_digest(value: Any, *, prefixed: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    bounded = value.strip().lower()
    if not _CALENDAR_SHA256_RE.fullmatch(bounded):
        return None
    if prefixed and not bounded.startswith("sha256:"):
        return None
    return bounded


def _calendar_load_json(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str) or len(value.encode("utf-8", errors="ignore")) > 128 * 1024:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return fallback


def _calendar_read_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    if status not in _CALENDAR_READ_STATUSES:
        return None
    request_digest = _calendar_projection_digest(value.get("request_digest"), prefixed=True)
    response_digest = value.get("response_digest")
    if response_digest is not None:
        response_digest = _calendar_projection_digest(response_digest, prefixed=True)
        if response_digest is None:
            return None
    verified_at = _calendar_projection_text(value.get("verified_at"), max_length=64)
    return {
        "status": status,
        "request_digest": request_digest,
        "response_digest": response_digest,
        "verified_at": verified_at,
    }


def _calendar_route_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    required = {
        "runtime_path",
        "provider",
        "model",
        "upstream_provider",
        "profile_id",
        "admission_digest",
        "status",
        "cost_microusd",
    }
    if set(value) != required:
        return None
    bounded = {
        key: _calendar_projection_text(value.get(key), max_length=256)
        for key in ("runtime_path", "provider", "model", "upstream_provider", "profile_id", "status")
    }
    if any(item is None for item in bounded.values()):
        return None
    admission_digest = _calendar_projection_digest(value.get("admission_digest"), prefixed=True)
    cost = value.get("cost_microusd")
    if type(cost) is not int or cost < 0:
        cost = None
    return {
        **bounded,
        "admission_digest": admission_digest,
        "cost_microusd": cost,
    }


def _calendar_receipt_artifact(
    receipt: CalendarPrepReceipt,
    projection: Mapping[str, Any],
) -> dict[str, Any] | None:
    artifact_id = safe_board_identifier(receipt.artifact_id, max_length=512)
    file_path = _calendar_projection_text(receipt.file_path, max_length=512)
    content_sha256 = safe_sha256_digest(receipt.content_sha256)
    readback_id = safe_board_identifier(receipt.readback_id, max_length=512)
    if (
        not artifact_id
        or not file_path
        or not _CALENDAR_RESULT_PATH_RE.fullmatch(file_path)
        or not content_sha256
        or not readback_id
    ):
        return None
    artifacts = projection.get("artifacts")
    effects = projection.get("effects")
    if not isinstance(artifacts, list) or not isinstance(effects, list):
        return None
    artifact_match = any(
        isinstance(item, Mapping)
        and item.get("exists") is True
        and item.get("artifact_type") == "calendar_meeting_prep_result"
        and item.get("artifact_id") == artifact_id
        and item.get("file_path") == file_path
        and item.get("content_sha256") == content_sha256
        for item in artifacts[-100:]
    )
    if not artifact_match:
        return None
    verified_at: str | None = None
    for item in reversed(effects[-100:]):
        if not isinstance(item, Mapping):
            continue
        details = item.get("details")
        if (
            item.get("receipt_kind") == "readback"
            and item.get("effect_type") == "calendar_meeting_prep_result"
            and item.get("status") in {"succeeded", "read_back", "reconciled"}
            and item.get("readback_id") == readback_id
            and item.get("target_path") == file_path
            and item.get("target_digest") == content_sha256
            and item.get("content_sha256") == content_sha256
            and isinstance(details, Mapping)
            and details.get("verified") is True
            and details.get("memory_status") == "no_learning"
        ):
            verified_at = _calendar_projection_text(item.get("verified_at"), max_length=64)
            break
    if verified_at is None:
        return None
    return {
        "artifact_id": artifact_id,
        "file_path": file_path,
        "content_sha256": content_sha256,
        "readback_id": readback_id,
        "verified_at": verified_at,
    }


async def _calendar_execution_payload(
    task: WorkBoardTask,
    attempt: WorkBoardAttempt,
    *,
    db: Any | None = None,
    projection: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Project one owner-bound Calendar prep execution and its proof fields."""

    if task.capability_id != "calendar.meeting-prep.v1":
        return None
    from src.integrations.google_calendar import calendar_job_id

    task_id = safe_board_identifier(task.task_id, max_length=256)
    attempt_id = safe_board_identifier(attempt.attempt_id, max_length=256)
    workflow_run_id = safe_workflow_run_id(attempt.workflow_run_id)
    if not task_id or not attempt_id or not workflow_run_id:
        return None
    expected_job_id = calendar_job_id(task.owner_principal_id, task_id, attempt_id)
    if workflow_run_id != expected_job_id or attempt.task_id != task.task_id:
        return None
    if projection is None:
        try:
            projection = await durable_job_repository.get_job(expected_job_id)
        except Exception:
            return None
    if not isinstance(projection, Mapping):
        return None
    status = projection.get("status")
    if status not in _CALENDAR_EXECUTION_STATUSES:
        return None
    owner = projection.get("owner")
    authority = projection.get("declared_authority")
    identity = projection.get("idempotency")
    if not isinstance(owner, Mapping) or not isinstance(authority, Mapping) or not isinstance(identity, Mapping):
        return None
    owner_principal = safe_board_identifier(task.owner_principal_id, max_length=256)
    owner_session = safe_board_identifier(task.owner_session_id, max_length=512)
    if not owner_principal or not owner_session:
        return None
    if (
        projection.get("job_id") != expected_job_id
        or projection.get("run_identity") != expected_job_id
        or projection.get("job_kind") != "calendar_meeting_prep"
        or projection.get("capability_version") != "1"
        or owner.get("kind") != "user"
        or owner.get("principal_id") != owner_principal
        or owner.get("service_id") not in (None, "")
        or projection.get("session_id") != owner_session
        or projection.get("operator_session_id") != owner_session
        or projection.get("goal_id") != task.goal_id
        or projection.get("goal_revision") != task.goal_revision
        or projection.get("root_run_identity") != expected_job_id
        or projection.get("parent_run_identity") not in (None, "")
        or projection.get("parent_job_id") not in (None, "")
        or identity.get("scope") != "work-board-attempt"
        or identity.get("key") != f"{task_id}:{attempt_id}"
        or authority.get("principal") != owner_principal
        or authority.get("owner_kind") != "user"
        or authority.get("service_id") not in (None, "")
        or authority.get("session_id") != owner_session
        or authority.get("operator_session_id") != owner_session
        or authority.get("goal_id") != task.goal_id
        or authority.get("goal_revision") != task.goal_revision
        or authority.get("capability_id") != "calendar.meeting-prep.v1"
        or authority.get("capability_version") != "1"
        or authority.get("attempt_id") != attempt_id
        or authority.get("priority") != task.priority
    ):
        return None
    result: dict[str, Any] = {
        "capability_id": "calendar.meeting-prep.v1",
        "job_id": expected_job_id,
        "durable_status": status,
        "connection_id": None,
        "connection_revision": None,
        "consent_id": None,
        "consent_revision": None,
        "event_binding_id": None,
        "event_key": None,
        "event_revision": None,
        "calendar_list_revision": None,
        "read_1": None,
        "read_2": None,
        "effective_route": None,
        "artifact_id": None,
        "file_path": None,
        "content_sha256": None,
        "readback_id": None,
        "verified_at": None,
        "memory_status": None,
        "failure_code": _calendar_projection_text(projection.get("failure_reason"), max_length=128),
        "recovery_action": None,
    }
    if db is None:
        return result
    receipt = (
        await db.execute(
            select(CalendarPrepReceipt).where(
                CalendarPrepReceipt.owner_principal_id == owner_principal,
                CalendarPrepReceipt.owner_session_id == owner_session,
                CalendarPrepReceipt.task_id == task.task_id,
                CalendarPrepReceipt.attempt_id == attempt.attempt_id,
                CalendarPrepReceipt.durable_job_id == expected_job_id,
                CalendarPrepReceipt.goal_id == task.goal_id,
                CalendarPrepReceipt.goal_revision == task.goal_revision,
            )
        )
    ).scalar_one_or_none()
    if receipt is None:
        return result
    result.update(
        connection_id=safe_board_identifier(receipt.connection_id, max_length=256),
        connection_revision=int(receipt.connection_revision) if type(receipt.connection_revision) is int and receipt.connection_revision > 0 else None,
        consent_id=safe_board_identifier(receipt.consent_id, max_length=256),
        consent_revision=int(receipt.consent_revision) if type(receipt.consent_revision) is int and receipt.consent_revision > 0 else None,
        event_binding_id=safe_board_identifier(receipt.event_binding_id, max_length=256),
        event_key=_calendar_projection_text(receipt.event_key, max_length=256),
        event_revision=_calendar_projection_text(receipt.event_revision_read_1, max_length=256),
        calendar_list_revision=_calendar_projection_text(receipt.calendar_list_revision, max_length=256),
        memory_status="no_learning" if receipt.memory_status == "no_learning" and receipt.status == "succeeded" else None,
        failure_code=_calendar_projection_text(receipt.failure_code, max_length=128),
        recovery_action=_calendar_projection_text(receipt.recovery_action, max_length=128),
    )
    read_1 = _calendar_read_projection(_calendar_load_json(receipt.read_1_json, {}))
    read_2 = _calendar_read_projection(_calendar_load_json(receipt.read_2_json, {}))
    result["read_1"] = read_1
    result["read_2"] = read_2
    result["effective_route"] = _calendar_route_projection(_calendar_load_json(receipt.effective_route_json, {}))
    if receipt.status == "succeeded":
        proof = _calendar_receipt_artifact(receipt, projection)
        if proof is None:
            result["memory_status"] = None
        else:
            result.update(proof)
    return result


def _browser_policy_rules(raw: Any) -> dict[str, Any]:
    """Expose bounded normalized site-policy metadata without doing DNS."""

    if not isinstance(raw, str):
        return {"rules": [], "truncated": False, "known": False}
    try:
        values = list(_parse_rules(raw))
    except (AttributeError, TypeError, ValueError):
        return {"rules": [], "truncated": False, "known": False}
    return {
        "rules": values[:_BROWSER_POLICY_RULE_LIMIT],
        "truncated": len(values) > _BROWSER_POLICY_RULE_LIMIT,
        "known": True,
    }


def _browser_task_policy(
    *,
    effective_runtime_seconds: int,
    max_attempts: int = 2,
    max_outstanding_jobs: int = 8,
) -> dict[str, Any]:
    """Return the read-only, operator-visible public browser policy contract."""

    allowlist = _browser_policy_rules(getattr(settings, "browser_site_allowlist", None))
    blocklist = _browser_policy_rules(getattr(settings, "browser_site_blocklist", None))
    policy_known = bool(allowlist["known"] and blocklist["known"])
    return {
        "policy_state": "confirmed" if policy_known else "unknown",
        "policy_source": "configured_site_policy" if policy_known else None,
        "allowlist": allowlist,
        "blocklist": blocklist,
        "limits": {
            "max_runtime_seconds": max(1, min(int(effective_runtime_seconds), 180)),
            "hard_max_runtime_seconds": 180,
            "max_actions": 8,
            "max_navigations": 8,
            "max_requests": 32,
            "max_extract_bytes": 65_536,
            "max_browser_contexts": 1,
            "ready_capacity": 8,
            "max_attempts": max(1, min(int(max_attempts), 2)),
            "max_outstanding_jobs": max(1, int(max_outstanding_jobs)),
            "inference": "none",
        },
    }


def _recovery_action(
    task: WorkBoardTask,
    *,
    latest_attempt: WorkBoardAttempt | None = None,
    attempt_count: int = 0,
) -> str | None:
    """Derive the only operator recovery action allowed for this projection."""

    status = _json_value(task.status)
    block_kind = str(task.block_kind or "")
    if task.capability_id in {"work.research-dossier.v1","work.json-format.v1"} and latest_attempt is not None and latest_attempt.workflow_run_id:
        # The research inspector owns explicit same-attempt controls. Generic
        # retry/unblock would discard its immutable original operation.
        return None
    if status == WorkBoardStatus.running.value:
        # A pending admission has no durable run to cancel.  The dispatcher
        # must reconcile that binding first so the card never advertises a
        # control that could guess a process or run id.
        if (
            latest_attempt is not None
            and latest_attempt.workflow_run_id
            and latest_attempt.ended_at is None
            and latest_attempt.lease_owner
        ):
            return "cancel"
        return "reconcile_admission_binding" if latest_attempt is not None else None
    if status != WorkBoardStatus.blocked.value:
        return None
    if (
        (
            block_kind == "operator"
            or review_service._is_handoff_reconciliation_block(
                block_kind,
                task.block_reason,
            )
        )
        and (latest_attempt is None or latest_attempt.ended_at is not None)
        and str(task.block_source_status or "")
        in {item.value for item in (WorkBoardStatus.triage, WorkBoardStatus.todo, WorkBoardStatus.ready, WorkBoardStatus.review)}
    ):
        return "unblock"
    if block_kind == "reconcile_admission_binding":
        return "reconcile_admission_binding"
    if block_kind in {"unknown_effect", "cost_liability"}:
        return "reconcile_external_effect"
    if block_kind == "needs_input":
        return "approve_existing_run"
    if block_kind == "capability":
        if (
            task.block_reason == "external_mutation_grant_required"
            and latest_attempt is not None
            and latest_attempt.ended_at is not None
            and attempt_count < 2
        ):
            return "retry"
        # A pre-admission capability gate has no effect to reconcile. Once
        # its prerequisite is restored, Retry re-runs the live gate before
        # returning the card to Todo; no attempt or effect is replayed here.
        if latest_attempt is None and attempt_count == 0:
            return "retry"
        return "restore_prerequisite"
    if block_kind == "review_expired":
        return "renew_review"
    if block_kind == "attempt_limit":
        # Attempt exhaustion is a terminal recovery boundary for this card.
        # Further work starts as a new linked task, so never expose retry.
        return None
    if block_kind in {"transient", "cancelled"} and latest_attempt is not None:
        if latest_attempt.ended_at is None or attempt_count >= 2:
            return None
        refs = _decode_json_list(latest_attempt.receipt_refs_json)
        if not refs or any(
            not isinstance(item, dict)
            or str(item.get("status") or "") in {"unknown", "intent", "dispatched", "unknown_external_effect", "cost_liability"}
            or str(item.get("reason_code") or item.get("outcome") or "")
            not in {"no_external_effect", "not_dispatched", "cancelled", "operator_cancelled", "transient"}
            for item in refs
        ):
            return None
        return "retry"
    return None


def _operator(request: Request) -> AuthenticatedOperator:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    session_id = str(getattr(operator, "session_id", "") or "").strip()
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    if not isinstance(operator, AuthenticatedOperator) or not principal_id or not session_id:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    if (
        not bool(getattr(principal, "authenticated", False))
        or bool(getattr(principal, "revoked", False))
        or str(getattr(principal, "session_id", "") or "") != session_id
        or str(getattr(principal, "operator_session_id", "") or "") != session_id
    ):
        raise HTTPException(status_code=401, detail={"code": "session_unavailable"})
    return operator


def _owner(operator: AuthenticatedOperator) -> WorkBoardOwner:
    return WorkBoardOwner(
        principal_id=operator.principal.principal_id,
        session_id=operator.session_id,
    )


def _input_artifact_payload(metadata, *, include_details: bool = False) -> dict[str, Any]:
    """Serialize bounded artifact metadata without ever returning input bytes."""

    payload = {
        "artifact_id": metadata.artifact_id,
        "typed_input_ref": metadata.typed_input_ref,
        "typed_input_digest": metadata.typed_input_digest,
        "capability_id": metadata.capability_id,
        "goal_id": metadata.goal_id,
        "goal_revision": metadata.goal_revision,
        "expires_at": serialize_utc_datetime(metadata.expires_at),
    }
    if include_details:
        payload.update(
            {
                "state": metadata.state,
                "size_bytes": metadata.size_bytes,
                "bound_task_id": metadata.bound_task_id,
                "bound_task_revision": metadata.bound_task_revision,
                "revision": metadata.revision,
            }
        )
    return payload


async def _operator_has_github_consent(operator: AuthenticatedOperator, context: Mapping[str, Any]) -> bool:
    authority = (context.get("parent") or {}).get("declared_authority") or {}
    try:
        from src.extensions.github_consent import require_followthrough_consent
        await require_followthrough_consent(principal=operator.principal.principal_id,
            root=operator.session_id, action=authority.get("github_action"),
            repository=authority.get("github_repository"),
            revision=authority.get("github_connection_revision"))
        return True
    except Exception:
        return False


def _raise_board_error(exc: BoardError) -> None:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    detail.update(exc.extra)
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return serialize_utc_datetime(value)
    if hasattr(value, "value"):
        return value.value
    return value


def _task_payload(
    task: WorkBoardTask,
    *,
    dependency_counts: tuple[int, int] | None = None,
    latest_attempt: WorkBoardAttempt | None = None,
    attempt_count: int = 0,
    dispatch_rank: int | None = None,
) -> dict[str, Any]:
    dependency_count, completed_dependency_count = dependency_counts or (0, 0)
    attempt_payload = _attempt_payload(latest_attempt) if latest_attempt is not None else None
    readback_status = attempt_payload.get("readback_status") if attempt_payload else "not_started"
    verification_status = attempt_payload.get("verification_status") if attempt_payload else "not_started"
    return {
        "task_id": task.task_id,
        "creation_sequence": task.creation_sequence,
        "owner_principal_id": task.owner_principal_id,
        "owner_session_id": task.owner_session_id,
        "origin_session_id": safe_board_identifier(task.origin_session_id, max_length=512),
        "origin_thread_id": safe_board_identifier(task.origin_thread_id, max_length=256),
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "title": task.title,
        "body": task.body,
        "capability_id": safe_board_identifier(task.capability_id, max_length=128),
        "input_artifact_id": safe_board_identifier(task.input_artifact_id, max_length=512),
        "pipeline_operation_id": safe_board_identifier(task.pipeline_operation_id, max_length=128),
        "pipeline_slot": safe_board_identifier(task.pipeline_slot, max_length=128),
        "typed_input_ref": safe_board_reference(task.typed_input_ref, max_length=512),
        "typed_input_digest": safe_sha256_digest(task.typed_input_digest),
        "executor_id": safe_board_identifier(task.executor_id, max_length=128),
        "assignee_id": safe_board_identifier(task.assignee_id, max_length=128),
        "priority": task.priority,
        "idempotency_scope": task.idempotency_scope,
        "idempotency_key": task.idempotency_key,
        "scheduled_at": _json_value(task.scheduled_at),
        "status": _json_value(task.status),
        "block_kind": (
            task.block_kind
            if isinstance(task.block_kind, str) and task.block_kind in _SAFE_EVENT_BLOCK_KINDS
            else None
        ),
        "block_reason": task.block_reason,
        "block_source_status": (
            task.block_source_status
            if isinstance(task.block_source_status, str)
            and task.block_source_status in _SAFE_EVENT_STATUSES
            else None
        ),
        "cancel_requested_at": _json_value(latest_attempt.cancel_requested_at) if latest_attempt is not None else None,
        "requires_review": task.requires_review,
        "reviewer_id": safe_board_identifier(task.reviewer_id, max_length=128),
        "review_expires_at": _json_value(task.review_expires_at),
        "dependency_count": dependency_count,
        "completed_dependency_count": completed_dependency_count,
        "dispatch_rank": dispatch_rank,
        "dispatch_wait_reason": None,
        "recovery_action": _recovery_action(
            task,
            latest_attempt=latest_attempt,
            attempt_count=attempt_count,
        ),
        "readback_status": readback_status,
        "verification_status": verification_status,
        "task_revision": task.task_revision,
        "result_refs": _safe_receipt_refs(_decode_json_list(task.result_refs_json)),
        "artifact_refs": _safe_receipt_refs(_decode_json_list(task.artifact_refs_json)),
        "latest_attempt": attempt_payload,
        "created_at": _json_value(task.created_at),
        "updated_at": _json_value(task.updated_at),
        "completed_at": _json_value(task.completed_at),
        "archived_at": _json_value(task.archived_at),
    }


async def _safe_task_payload(
    task: WorkBoardTask,
    *,
    db: Any | None = None,
    dependency_counts: tuple[int, int] | None = None,
    latest_attempt: WorkBoardAttempt | None = None,
    attempt_count: int = 0,
    dispatch_rank: int | None = None,
    browser_projection: Mapping[str, Any] | None = None,
    recovered_read_only: bool = False,
) -> dict[str, Any]:
    payload = _task_payload(
        task,
        dependency_counts=dependency_counts,
        latest_attempt=latest_attempt,
        attempt_count=attempt_count,
        dispatch_rank=dispatch_rank,
    )
    if (
        task.status is WorkBoardStatus.ready
        and task.capability_id == "browser.public-task.v1"
        and latest_attempt is None
    ):
        # This is a process-local, read-only projection. Ordinary lane
        # contention remains silent; only an unverified cleanup quarantine is
        # safe to explain to the owner without creating scheduler writes.
        from src.browser.task_lane import browser_task_lane_wait_reason

        payload["dispatch_wait_reason"] = browser_task_lane_wait_reason(settings.workspace_dir)
    if task.capability_id == "browser.public-task.v1" and latest_attempt is not None:
        # Keep the browser execution surface additive to the established
        # latest-attempt DTO. A missing or mismatched durable root is exposed
        # as null rather than a guessed status/count.
        payload["latest_attempt"]["browser_execution"] = await _browser_execution_payload(
            task,
            latest_attempt,
            projection=browser_projection,
        )
    if task.capability_id == "calendar.meeting-prep.v1" and latest_attempt is not None:
        payload["latest_attempt"]["calendar_execution"] = await _calendar_execution_payload(
            task,
            latest_attempt,
            db=db,
        )
    for key in ("title", "body", "block_reason"):
        value = payload.get(key)
        if isinstance(value, str):
            if db is not None:
                payload[key] = await vault_redaction.redact_secrets_in_text_readonly(
                    db,
                    value,
                    fail_closed=True,
                )
            else:
                payload[key] = await vault_redaction.redact_secrets_in_text(
                    value,
                    fail_closed=True,
                )
    if recovered_read_only:
        from src.auth.ownership import RECOVERED_FIELDS
        payload.update(RECOVERED_FIELDS)
        payload.update(recovery_action=None, dispatch_rank=None, dispatch_wait_reason=None)
        return payload
    if payload.get("recovery_action") == "retry":
        # Recovery controls are an operator projection of current authority,
        # not a cached promise from the last dispatcher pass.  Re-run the
        # provider-free retry gates before exposing a retry button; a failed
        # gate remains a bounded prerequisite recovery while the task stays
        # Blocked.
        try:
            await dispatcher.validate_retry(
                WorkBoardOwner(
                    principal_id=task.owner_principal_id,
                    session_id=task.owner_session_id,
                ),
                task.task_id,
                expected_revision=task.task_revision,
            )
        except BoardError as exc:
            payload["recovery_action"] = str(
                exc.extra.get("recovery_action") or "restore_prerequisite"
            )
    elif payload.get("recovery_action") == "unblock":
        # Generic unblock is only exposed for an owner-bound manual block or
        # an exact verified-handoff recovery. Recheck the live session, goal,
        # and reviewer boundary before advertising the action.
        try:
            await dispatcher.validate_unblock(
                WorkBoardOwner(
                    principal_id=task.owner_principal_id,
                    session_id=task.owner_session_id,
                ),
                task.task_id,
                expected_revision=task.task_revision,
            )
        except BoardError as exc:
            payload["recovery_action"] = str(
                exc.extra.get("recovery_action") or "restore_prerequisite"
            )
    return payload


def _routine_invocation_uuid(task_id: str, attempt_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"seraph:work-board-attempt:{task_id}:{attempt_id}",
        )
    )


def _routine_publication_error(
    code: str,
    message: str,
    *,
    status_code: int = 409,
    **extra: Any,
) -> BoardError:
    return BoardError(code, message, status_code=status_code, **extra)


def _safe_publication_preview(value: Any) -> dict[str, Any] | None:
    """Keep only bounded, operator-safe fields from the M3 preview receipt."""

    if not isinstance(value, Mapping):
        return None
    preview: dict[str, Any] = {}
    for key in (
        "repository",
        "action",
        "issue_number",
        "body_sha256",
        "marker",
        "dossier_artifact_id",
        "dossier_sha256",
        "source_watch_id",
        "connection_revision",
    ):
        item = value.get(key)
        if item is not None:
            preview[key] = item
    for key, maximum in (("title", 160), ("body", 32_000)):
        item = value.get(key)
        if item is not None:
            preview[key] = str(item)[:maximum]
    return preview or None


async def _routine_publication_context(
    db,
    operator: AuthenticatedOperator,
    task_id: str,
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Resolve publication recovery only from the canonical board binding.

    The browser supplies a task ID and, for mutations, the task revision. The
    attempt, parent workflow run, routine binding, package/watch authority,
    publication child, and approval are all read from durable task/runtime
    records. This intentionally accepts no caller-selected attempt, run, or
    approval identity.
    """

    owner = _owner(operator)
    detail = await repository.get_detail(db, owner, task_id)
    task = detail["task"]
    if task.capability_id != "guardian-routine.v1":
        raise _routine_publication_error(
            "routine_publication_not_supported",
            "The selected task is not a governed routine invocation",
            status_code=422,
        )
    if expected_revision is not None and int(task.task_revision) != int(expected_revision):
        raise BoardRevisionConflict(task.task_id, int(expected_revision), int(task.task_revision))
    if task.status not in {WorkBoardStatus.running, WorkBoardStatus.blocked}:
        raise _routine_publication_error(
            "routine_publication_task_state_invalid",
            "Routine publication recovery requires the same active or publication-blocked task",
        )
    attempts = [
        item
        for item in detail["attempts"]
        if item.workflow_run_id and item.ended_at is None
    ]
    if not attempts:
        raise _routine_publication_error(
            "routine_publication_attempt_missing",
            "The canonical routine task has no resumable linked durable workflow attempt",
        )
    attempt = attempts[0]
    workflow_run_id = str(attempt.workflow_run_id or "")
    if not workflow_run_id:
        raise _routine_publication_error(
            "routine_publication_attempt_missing",
            "The canonical routine attempt has no durable parent workflow run",
        )
    try:
        inputs = _parse_typed_input(task)
    except TypedInputError as exc:
        raise _routine_publication_error(
            exc.code,
            "The routine task's immutable typed input cannot be trusted for publication recovery",
        ) from exc
    routine_id = str(inputs.get("routine_id") or "")
    try:
        routine_version = int(inputs.get("version") or 0)
        routine_revision = int(inputs.get("expected_routine_revision") or 0)
        watch_revision = int(inputs.get("expected_watch_revision") or 0)
        goal_revision = int(inputs.get("expected_goal_revision") or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _routine_publication_error(
            "routine_publication_binding_invalid",
            "The routine task's typed authority is malformed",
        ) from exc
    watch_id = str(inputs.get("source_watch_id") or "")
    if (
        not routine_id
        or routine_version < 1
        or routine_revision < 1
        or not watch_id
        or watch_revision < 1
        or task.goal_id != str(inputs.get("goal_id") or "")
        or int(task.goal_revision) != goal_revision
    ):
        raise _routine_publication_error(
            "routine_publication_binding_mismatch",
            "The task goal and typed routine authority do not match",
        )

    parent = await durable_job_repository.get_job(workflow_run_id)
    if not isinstance(parent, Mapping):
        raise _routine_publication_error(
            "routine_publication_parent_missing",
            "The linked routine workflow run is unavailable",
        )
    if str(parent.get("job_id") or parent.get("run_identity") or "") != workflow_run_id:
        raise _routine_publication_error(
            "routine_publication_parent_mismatch",
            "The linked attempt does not identify the expected routine workflow",
        )
    parent_owner = parent.get("owner") if isinstance(parent.get("owner"), Mapping) else {}
    authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
    persisted_sessions = {
        str(value)
        for value in (
            authority.get("session_id"),
            parent.get("operator_session_id"),
            parent.get("session_id"),
        )
        if str(value or "")
    }
    try:
        parent_goal_revision = int(parent.get("goal_revision") or 0)
        parent_connection_revision = int(authority.get("github_connection_revision") or 0)
        authority_routine_version = int(authority.get("routine_version") or 0)
        authority_routine_revision = int(authority.get("routine_revision") or 0)
        authority_watch_revision = int(authority.get("source_watch_revision") or 0)
        binding_matches = (
            str(parent.get("job_kind") or "") == "routine_invocation"
            and str(parent_owner.get("kind") or "") == "user"
            and str(parent_owner.get("principal_id") or "") == owner.principal_id
            and str(authority.get("goal_owner_principal_id") or "") == owner.principal_id
            and str(authority.get("goal_owner_session_id") or "") == owner.session_id
            and persisted_sessions == {owner.session_id}
            and str(authority.get("capability_id") or "") == "guardian-routine.v1"
            and bool(str(authority.get("package_digest") or ""))
            and bool(str(authority.get("github_connection_id") or ""))
            and parent_connection_revision >= 1
            and str(parent.get("goal_id") or "") == task.goal_id
            and parent_goal_revision == int(task.goal_revision)
            and str(parent.get("session_id") or parent.get("operator_session_id") or "") == owner.session_id
            and (
                parent.get("operator_session_id") is None
                or str(parent.get("operator_session_id") or "") == owner.session_id
            )
            and str(authority.get("routine_id") or "") == routine_id
            and authority_routine_version == routine_version
            and authority_routine_revision == routine_revision
            and str(authority.get("source_watch_id") or "") == watch_id
            and authority_watch_revision == watch_revision
            and str(authority.get("invocation_uuid") or "")
            == _routine_invocation_uuid(task.task_id, attempt.attempt_id)
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise _routine_publication_error(
            "routine_publication_binding_invalid",
            "The linked routine workflow authority is malformed",
        ) from exc
    if not binding_matches:
        raise _routine_publication_error(
            "routine_publication_binding_mismatch",
            "The linked routine workflow authority does not match the board task",
        )

    try:
        attempt_fence = int(attempt.fencing_token or 0)
        parent_fence = int((parent.get("lease") or {}).get("fencing_token") or 0)
    except (TypeError, ValueError, AttributeError) as exc:
        raise _routine_publication_error(
            "routine_publication_fence_invalid",
            "The routine attempt fence is malformed",
        ) from exc
    parent_status = str(parent.get("status") or "")
    parent_wait_reason = str(parent.get("failure_reason") or "")
    waiting_for_operator = (
        parent_status == "blocked"
        and parent_wait_reason
        in {"awaiting_publication_preview", "awaiting_publication_approval"}
    )
    if attempt_fence <= 0 or parent_fence != attempt_fence:
        raise _routine_publication_error(
            "stale_fence",
            "The routine attempt no longer owns the linked durable workflow",
        )
    if waiting_for_operator:
        if (
            task.status is not WorkBoardStatus.blocked
            or str(task.block_reason or "") not in {
                parent_wait_reason,
                "external_mutation_grant_required",
            }
            or attempt.lease_owner is not None
            or attempt.lease_expires_at is not None
        ):
            raise _routine_publication_error(
                "routine_publication_task_state_invalid",
                "The same card must be Blocked with its board lease released while an operator decision is pending",
            )
    elif parent_status == "running":
        if task.status is not WorkBoardStatus.running or not attempt.lease_owner:
            raise _routine_publication_error(
                "routine_publication_task_state_invalid",
                "A running routine parent requires its current fenced board attempt",
            )
        lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
        if not lease.get("owner") or not lease.get("expires_at"):
            raise _routine_publication_error(
                "stale_fence",
                "The running routine workflow has no current lease",
            )
        try:
            expiry = datetime.fromisoformat(str(lease["expires_at"]).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                raise ValueError("expired")
        except (TypeError, ValueError, OverflowError) as exc:
            raise _routine_publication_error(
                "stale_fence",
                "The routine workflow lease is expired or malformed",
            ) from exc
    else:
        raise _routine_publication_error(
            "routine_publication_not_ready",
            "The linked durable routine is not waiting for publication review",
        )

    publication_checkpoint = _job_checkpoint(parent, "routine:publication_child_recorded")
    m3_job_id = str((publication_checkpoint or {}).get("m3_job_id") or "")
    approval_id = str((publication_checkpoint or {}).get("approval_id") or "")
    m3_job = await durable_job_repository.get_job(m3_job_id) if m3_job_id else None
    publication = None
    if isinstance(m3_job, Mapping):
        m3_authority = (
            m3_job.get("declared_authority")
            if isinstance(m3_job.get("declared_authority"), Mapping)
            else {}
        )
        m3_owner = m3_job.get("owner") if isinstance(m3_job.get("owner"), Mapping) else {}
        m3_sessions = {
            str(value)
            for value in (
                m3_authority.get("session_id"),
                m3_job.get("operator_session_id"),
                m3_job.get("session_id"),
            )
            if str(value or "")
        }
        try:
            m3_goal_revision = int(m3_job.get("goal_revision") or 0)
            m3_connection_revision = int(m3_authority.get("connection_revision") or 0)
            m3_matches = (
                str(m3_job.get("job_kind") or "") == "github_followthrough_v1"
                and str(m3_job.get("capability_version") or "") == "1"
                and str(m3_owner.get("principal_id") or "") == owner.principal_id
                and m3_sessions == {owner.session_id}
                and str(m3_job.get("goal_id") or "") == task.goal_id
                and m3_goal_revision == int(task.goal_revision)
                and str(m3_authority.get("connection_id") or "")
                == str(authority.get("github_connection_id") or "")
                and m3_connection_revision == parent_connection_revision
                and str(m3_authority.get("source_watch_id") or "") == watch_id
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise _routine_publication_error(
                "routine_publication_child_binding_invalid",
                "The prepared publication child authority is malformed",
            ) from exc
        if not m3_matches:
            raise _routine_publication_error(
                "routine_publication_child_binding_mismatch",
                "The prepared publication child is not bound to the current routine authority",
            )
        from src.extensions.github_followthrough import GitHubFollowthroughService

        try:
            publication = await GitHubFollowthroughService()._prepare_job_response(m3_job)
        except Exception:
            publication = None
        approval_id = approval_id or str(
            (m3_job.get("declared_authority") or {}).get("approval_id")
            if isinstance(m3_job.get("declared_authority"), Mapping)
            else ""
        )
    approval_status = None
    if approval_id:
        approval = await approval_repository.get(approval_id)
        if approval is not None:
            if (
                str(getattr(approval, "owner_principal_id", "") or "") != owner.principal_id
                or str(getattr(approval, "operator_session_id", "") or "") != owner.session_id
            ):
                raise _routine_publication_error(
                    "approval_owner_mismatch",
                    "The publication approval belongs to another operator session",
                    status_code=403,
                )
            approval_details = {}
            try:
                parsed_details = json.loads(getattr(approval, "details_json", "") or "{}")
                if isinstance(parsed_details, Mapping):
                    approval_details = dict(parsed_details)
            except (TypeError, ValueError):
                approval_details = {}
            if str(approval_details.get("durable_job_id") or "") != m3_job_id:
                raise _routine_publication_error(
                    "approval_job_binding_mismatch",
                    "The publication approval is not bound to the canonical M3 job",
                )
            approval_status = str(getattr(approval, "status", "") or "")

    parent_failure = str(parent.get("failure_reason") or "")
    if not m3_job_id and parent_failure != "awaiting_publication_preview":
        raise _routine_publication_error(
            "routine_publication_not_ready",
            "The routine parent is not waiting for a publication preview",
        )
    if m3_job_id and not isinstance(m3_job, Mapping):
        raise _routine_publication_error(
            "routine_publication_child_missing",
            "The prepared publication child is unavailable for reconciliation",
        )
    return {
        "task": task,
        "attempt": attempt,
        "detail": detail,
        "parent": dict(parent),
        "parent_effects": list(parent.get("effects") or []),
        "inputs": inputs,
        "routine_id": routine_id,
        "routine_version": routine_version,
        "routine_revision": routine_revision,
        "source_watch_id": watch_id,
        "source_watch_revision": watch_revision,
        "parent_workflow_run_id": workflow_run_id,
        "m3_job_id": m3_job_id or None,
        "m3_status": str(m3_job.get("status") or "") if isinstance(m3_job, Mapping) else None,
        "publication_effects": list(m3_job.get("effects") or []) if isinstance(m3_job, Mapping) else [],
        "approval_id": approval_id or None,
        "approval_status": approval_status,
        "preview": _safe_publication_preview((publication or {}).get("preview")),
        "publication_response": publication,
    }


async def _routine_publication_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    task = context["task"]
    attempt = context["attempt"]
    publication_response = context.get("publication_response")
    return {
        "task_id": task.task_id,
        "task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id,
        "parent_workflow_run_id": safe_workflow_run_id(context["parent_workflow_run_id"]),
        "routine_id": context["routine_id"],
        "routine_version": context["routine_version"],
        "routine_revision": context["routine_revision"],
        "source_watch_id": context["source_watch_id"],
        "source_watch_revision": context["source_watch_revision"],
        "parent_status": context["parent"].get("status"),
        "m3_job_id": safe_workflow_run_id(context.get("m3_job_id")),
        "m3_status": context.get("m3_status"),
        "approval_id": context.get("approval_id"),
        "approval_status": context.get("approval_status"),
        "preview": context.get("preview"),
        "status": (
            publication_response.get("status")
            if isinstance(publication_response, Mapping)
            else context["parent"].get("failure_reason") or context["parent"].get("status")
        ),
        "recovery_action": (
            "resume_routine_publication"
            if context.get("m3_job_id")
            else "prepare_routine_publication"
        ),
    }


async def _block_routine_for_missing_external_authority(context: Mapping[str, Any]) -> bool:
    """Safely block an unpublished routine attempt when its grant disappears.

    The routine parent is cancelled only while it is waiting for its explicit
    publication preview/approval and both durable effect ledgers are empty.
    Any uncertain or already dispatched outcome stays in normal reconciliation.
    """

    task = context["task"]
    attempt = context["attempt"]
    parent = context.get("parent") if isinstance(context.get("parent"), Mapping) else {}
    m3_status = str(context.get("m3_status") or "")
    parent_status = str(parent.get("status") or "")
    parent_reason = str(parent.get("failure_reason") or "")
    safe_parent_wait = (
        parent_status == "blocked"
        and parent_reason in {"awaiting_publication_preview", "awaiting_publication_approval"}
    )
    safe_child_wait = not context.get("m3_job_id") or m3_status in {
        "accepted",
        "queued",
        "awaiting_approval",
    }
    parent_effects = context.get("parent_effects")
    safe_parent_effects = isinstance(parent_effects, list) and all(
        isinstance(item, Mapping)
        and (
            str(item.get("status") or "") == "approved"
            or (
                str(item.get("status") or "") == "succeeded"
                and str(item.get("effect_type") or "")
                in {"guardian_routine_child", "guardian_routine_outcome"}
            )
        )
        for item in parent_effects
    )
    safe_effect_ledgers = safe_parent_effects and not context.get("publication_effects")
    if not safe_parent_wait or not safe_child_wait or not safe_effect_ledgers or attempt.ended_at is not None:
        await dispatcher.reconcile_linked_attempts()
        return False

    if task.status is WorkBoardStatus.blocked:
        # The durable routine parent is already waiting for an explicit
        # operator decision and its effect ledgers are empty. Preserve the
        # same open task attempt and workflow binding, release any board
        # lease, and expose the missing grant as the prerequisite to restore.
        # Recovery can then recheck authority and advance the fence before
        # resuming this exact durable run.
        if attempt.lease_owner is not None or attempt.lease_expires_at is not None:
            await dispatcher.reconcile_linked_attempts()
            return False
        await dispatcher._pause_routine_for_operator(
            task,
            attempt,
            context["parent"],
            reason="external_mutation_grant_required",
        )
        return True

    if task.status is not WorkBoardStatus.running:
        await dispatcher.reconcile_linked_attempts()
        return False

    cancellations = await routine_service.cancel_invocation_job_tree(
        context["parent_workflow_run_id"],
        routine_id=context["routine_id"],
        owner_principal_id=task.owner_principal_id,
        owner_session_id=task.owner_session_id,
        reason="external_mutation_authority_missing",
    )
    if cancellations:
        await dispatcher.reconcile_linked_attempts()
        return False
    receipt = {
        "workflow_run_id": context["parent_workflow_run_id"],
        "status": "cancelled",
        "reason_code": "capability",
        "recovery_action": "retry_after_prerequisite",
    }
    await dispatcher._project(
        task,
        attempt,
        board_revision=int(task.task_revision),
        status=WorkBoardStatus.blocked,
        outcome="capability",
        block_kind="capability",
        block_reason="external_mutation_grant_required",
        result_refs=[receipt],
        lease_owner=attempt.lease_owner or dispatcher.runner_id,
    )
    return True


def _decode_json_list(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _action_receipt_payload(
    task_payload: dict[str, Any],
    event: Any,
    *,
    attempt_id: str | None,
) -> dict[str, Any]:
    """Return the authoritative receipt for one persisted board mutation.

    The nested task remains the compatibility projection used by existing
    callers.  These top-level fields let an operator reconcile one action
    against the exact append-only event created by the mutation transaction.
    Only already-safe task projection values are exposed as reason and
    recovery fields.
    """

    return {
        "task_id": task_payload.get("task_id"),
        "status": task_payload.get("status"),
        "revision": task_payload.get("task_revision"),
        "attempt_id": attempt_id,
        "reason_code": task_payload.get("block_kind"),
        "recovery_action": task_payload.get("recovery_action"),
        "event_id": getattr(event, "event_id", None),
    }


def _safe_attempt_outcome(value: Any) -> str | None:
    if isinstance(value, str) and value in _SAFE_EVENT_OUTCOMES:
        return value
    return None


def _attempt_payload(attempt: WorkBoardAttempt) -> dict[str, Any]:
    receipt_refs = _decode_json_list(attempt.receipt_refs_json)
    proof = review_service._verified_readback(attempt)
    verified = bool(
        proof
        and proof.get("workflow_run_id") == str(attempt.workflow_run_id or "")
        and proof.get("readback_id")
        and proof.get("verified_at")
    )
    unresolved = any(
        isinstance(item, dict)
        and str(item.get("status") or "") in {"unknown", "intent", "dispatched", "unknown_external_effect", "cost_liability"}
        for item in receipt_refs
    )
    decisive_failure = any(
        isinstance(item, dict)
        and (
            str(item.get("readback_status") or "") == "failed"
            or str(item.get("verification_status") or "") == "failed"
        )
        for item in receipt_refs
    )
    if verified:
        readback_status = "verified"
        verification_status = "passed"
    elif unresolved:
        readback_status = "unknown"
        verification_status = "reconciliation_required"
    elif attempt.ended_at is None:
        readback_status = "pending"
        verification_status = "pending"
    elif str(attempt.outcome or "") == "cancelled":
        readback_status = "not_applicable"
        verification_status = "cancelled"
    elif decisive_failure:
        readback_status = "failed"
        verification_status = "failed"
    else:
        readback_status = "unknown"
        verification_status = "reconciliation_required"
    return {
        "attempt_id": attempt.attempt_id,
        "task_id": attempt.task_id,
        "workflow_run_id": safe_workflow_run_id(attempt.workflow_run_id),
        "task_revision_at_claim": attempt.task_revision_at_claim,
        "lease_owner": safe_board_identifier(attempt.lease_owner, max_length=512),
        "cancel_requested_at": _json_value(attempt.cancel_requested_at),
        "lease_expires_at": _json_value(attempt.lease_expires_at),
        "heartbeat_at": _json_value(attempt.heartbeat_at),
        "fencing_token": attempt.fencing_token,
        "executor_id": safe_board_identifier(attempt.executor_id, max_length=128),
        "started_at": _json_value(attempt.started_at),
        "ended_at": _json_value(attempt.ended_at),
        "outcome": _safe_attempt_outcome(attempt.outcome),
        "receipt_refs": _safe_receipt_refs(_decode_json_list(attempt.receipt_refs_json)),
        "readback_status": readback_status,
        "verification_status": verification_status,
        "created_at": _json_value(attempt.created_at),
        "updated_at": _json_value(attempt.updated_at),
    }


async def _safe_comment_payload(
    comment: WorkBoardComment,
    *,
    db: Any | None = None,
) -> dict[str, Any]:
    if db is not None:
        body = await vault_redaction.redact_secrets_in_text_readonly(
            db,
            comment.body,
            fail_closed=True,
        )
    else:
        body = await vault_redaction.redact_secrets_in_text(
            comment.body,
            fail_closed=True,
        )
    return {
        "comment_id": comment.comment_id,
        "task_id": comment.task_id,
        "author_principal_id": comment.author_principal_id,
        "author_session_id": comment.author_session_id,
        "body": body,
        "created_at": _json_value(comment.created_at),
    }


@router.get("/tasks")
async def list_work_board_tasks(
    request: Request,
    status: WorkBoardStatus | None = None,
    executor_id: str | None = Query(default=None, max_length=128),
    assignee_id: str | None = Query(default=None, max_length=128),
    q: str | None = Query(default=None, max_length=200),
    after: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
):
    operator = _operator(request)
    from src.auth.ownership import selected_read_scopes, selected_read_principal
    try:
        async with get_session() as db:
            recovered = await selected_read_scopes(operator, "task", db=db)
            page = await repository.list_tasks(
                db,
                _owner(operator),
                status=status,
                executor_id=executor_id,
                assignee_id=assignee_id,
                query=q,
                after=after,
                limit=limit,
                recovered_read_scopes=recovered,
            )
            browser_job_ids = [
                str(attempt.workflow_run_id)
                for task in page.tasks
                if task.capability_id == "browser.public-task.v1"
                for attempt in ([page.latest_attempts.get(task.task_id)] if page.latest_attempts.get(task.task_id) is not None else [])
                if attempt.workflow_run_id
            ]
            try:
                browser_projections = await durable_job_repository.get_jobs(browser_job_ids)
            except Exception:
                # Durable progress is additive operator metadata. A locked or
                # unavailable read must make only that metadata unavailable;
                # it must not make the authenticated board list unusable.
                browser_projections = {}
            return {
                "tasks": [
                    await _safe_task_payload(
                        task,
                        db=db,
                        dependency_counts=page.dependency_counts.get(task.task_id),
                        latest_attempt=page.latest_attempts.get(task.task_id),
                        attempt_count=page.attempt_counts.get(task.task_id, 0),
                        dispatch_rank=page.dispatch_ranks.get(task.task_id),
                        browser_projection=(
                            browser_projections.get(str(page.latest_attempts[task.task_id].workflow_run_id))
                            if task.task_id in page.latest_attempts and page.latest_attempts[task.task_id].workflow_run_id
                            else None
                        ),
                        recovered_read_only=task.task_id in recovered,
                    )
                    for task in page.tasks
                ],
                "next_after": page.next_after,
                "last_event_id": page.last_event_id,
            }
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.get("/goals/{goal_id}/execution-limits")
async def get_work_board_execution_limits(
    request: Request,
    goal_id: str,
    goal_revision: int = Query(..., ge=1),
):
    """Return the server-derived finite board runtime limit for one goal."""

    operator = _operator(request)
    try:
        async with get_session() as db:
            goal = (
                await db.execute(
                    select(Goal).where(
                        Goal.id == goal_id,
                        Goal.owner_principal_id == operator.principal.principal_id,
                        Goal.owner_session_id == operator.session_id,
                    )
                )
            ).scalar_one_or_none()
            if goal is None:
                raise HTTPException(status_code=404, detail={"code": "goal_not_found"})
            current_revision = max(int(getattr(goal, "revision", 1) or 1), 1)
            if current_revision != int(goal_revision):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "goal_revision_stale",
                        "goal_id": goal_id,
                        "expected_revision": int(goal_revision),
                        "current_revision": current_revision,
                    },
                )
            budget = deserialize_admission_budget(goal)
            configured = int(budget.max_runtime_seconds) if budget is not None else 300
            effective = min(max(configured, 1), 900)
            browser_max_attempts, browser_max_outstanding = effective_browser_limits(goal)
            return {
                "goal_id": goal_id,
                "goal_revision": current_revision,
                "effective_max_runtime_seconds": effective,
                "default_max_runtime_seconds": 300,
                "hard_max_runtime_seconds": 900,
                "attempt_limit": (
                    browser_max_attempts
                    if budget is not None
                    else 2
                ),
                "max_outstanding_jobs": browser_max_outstanding,
                "limit_source": "goal_admission_budget" if budget is not None else "default",
                "browser_task_policy": _browser_task_policy(
                    effective_runtime_seconds=(
                        min(effective, 180)
                        if goal is not None
                        else effective
                    ),
                    max_attempts=browser_max_attempts,
                    max_outstanding_jobs=browser_max_outstanding,
                ),
            }
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Retry after the workspace database is ready."},
        ) from exc


@router.post("/input-artifacts")
async def create_work_board_input_artifact(
    request: Request,
    body: WorkBoardInputArtifactCreate,
):
    """Reserve one owner-bound, typed input without returning its payload."""

    operator = _operator(request)
    try:
        async with get_session() as db:
            metadata = await prepare_input_artifact(db, _owner(operator), body)
            return _input_artifact_payload(metadata)
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the workspace database readiness receipt and retry."},
        ) from exc


@router.post("/document-pairs")
async def reserve_document_pair(request: Request, body: DocumentPairReserve):
    from src.work_board.document_pairs import reserve
    try:
        async with get_session() as db:
            return await reserve(db, _owner(_operator(request)), body)
    except BoardError as exc:
        _raise_board_error(exc)


@router.get("/document-pairs/{identifier}")
async def read_document_pair(request: Request, identifier: str):
    from src.work_board.document_pairs import owned, projection
    try:
        async with get_session() as db:
            row, _value = await owned(db, _owner(_operator(request)), identifier)
            return projection(row)
    except BoardError as exc:
        _raise_board_error(exc)


@router.put("/document-pairs/{identifier}/sources/{slot}")
async def upload_document_source(request: Request, identifier: str, slot: str, expected_revision: int = Query(ge=1)):
    from src.work_board.document_pairs import upload
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
        raise HTTPException(status_code=415, detail={"code": "document_raw_stream_required"})
    try:
        async with get_session() as db:
            return await upload(db, _owner(_operator(request)), identifier, expected_revision, slot, request.stream())
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/document-pairs/{identifier}/complete")
async def complete_document_pair(request: Request, identifier: str, body: DocumentPairMutation):
    from src.work_board.document_pairs import complete
    try:
        async with get_session() as db:
            return await complete(db, _owner(_operator(request)), identifier, body.expected_revision)
    except BoardError as exc:
        _raise_board_error(exc)


@router.get("/tasks/{task_id}/document-output/{slot}")
async def read_document_comparison_output(request: Request, task_id: str, slot: str):
    from src.work_board.document_compare_native import read_output
    from src.work_board.document_pairs import owned, authority
    from src.work_board.pipelines import root_binding
    if slot not in {"report","csv","manifest"}:raise HTTPException(status_code=404)
    owner=_owner(_operator(request))
    try:
        async with get_session() as db:
            task=await WorkBoardRepository().get_task(db,owner,task_id)
            if task.capability_id!="work.document-compare.v1" or task.status not in {WorkBoardStatus.done,WorkBoardStatus.review}:
                raise BoardError("document_output_unavailable","A verified comparison output is required",status_code=409)
            row,value=await owned(db,owner,task.input_artifact_id)
            await authority(db,owner,row,value,dict(root_binding()))
            attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id)
                .order_by(WorkBoardAttempt.created_at.desc()).limit(1))
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id)) if attempt else None
            if run is None or run.status!="succeeded":raise BoardError("document_output_unavailable","The original job is not verified",status_code=409)
            receipt,output=read_output(task,attempt,run)
            text=json.dumps(output[slot],sort_keys=True) if slot=="manifest" else output[slot]
            return {"text":text,"sha256":hashlib.sha256(text.encode()).hexdigest(),"cipher_sha256":receipt["cipher_sha256"],"no_learning":True}
    except BoardError as exc:_raise_board_error(exc)
    except (OSError,ValueError,TypeError,KeyError):
        raise HTTPException(status_code=409,detail={"code":"document_output_readback_required"})


@router.get("/tasks/{task_id}/document-comparison")
async def read_document_comparison_state(request: Request,task_id: str):
    from src.work_board.document_compare_control import snapshot
    try:
        async with get_session() as db:
            return await snapshot(db,_owner(_operator(request)),task_id)
    except BoardError as exc:_raise_board_error(exc)
    except (DurableJobError,OSError,ValueError,TypeError,KeyError):
        raise HTTPException(status_code=409,detail={"code":"document_original_binding_required"})


@router.post("/tasks/{task_id}/document-comparison/recover")
async def recover_document_comparison(request: Request,task_id: str,body: DocumentControlRequest):
    from src.work_board.document_compare_control import recover,snapshot
    try:
        owner=_owner(_operator(request));result=await recover(dispatcher,owner,task_id,body)
        async with get_session() as db:
            return {"recovery":result,"document_comparison":await snapshot(db,owner,task_id)}
    except BoardError as exc:_raise_board_error(exc)
    except (DurableJobError,OSError,ValueError,TypeError,KeyError):
        raise HTTPException(status_code=409,detail={"code":"document_original_output_and_reap_required"})


@router.post("/document-pairs/{identifier}/retry")
async def retry_document_pair(request: Request, identifier: str, body: DocumentPairMutation):
    from src.work_board.document_pairs import reset_unbound
    try:
        async with get_session() as db:
            return await reset_unbound(db,_owner(_operator(request)),identifier,body.expected_revision,retry=True)
    except BoardError as exc:_raise_board_error(exc)


@router.post("/tasks/{task_id}/document-comparison/retry")
async def retry_document_comparison(request: Request,task_id: str,body: DocumentControlRequest):
    from src.work_board.document_compare_control import retry,snapshot
    try:
        owner=_owner(_operator(request));result=await retry(dispatcher,owner,task_id,body)
        async with get_session() as db:
            return {"retry":result,"document_comparison":await snapshot(db,owner,task_id)}
    except BoardError as exc:_raise_board_error(exc)
    except (DurableJobError,OSError,ValueError,TypeError,KeyError):
        raise HTTPException(status_code=409,detail={"code":"document_known_terminated_interruption_required"})


@router.post("/tasks/{task_id}/document-comparison/reconcile")
async def reconcile_document_comparison(request: Request,task_id: str,body: DocumentControlRequest):
    from src.work_board.document_compare_control import reconcile,snapshot
    try:
        owner=_owner(_operator(request));result=await reconcile(dispatcher,owner,task_id,body)
        async with get_session() as db:
            return {"reconciliation":result,"document_comparison":await snapshot(db,owner,task_id)}
    except BoardError as exc:_raise_board_error(exc)
    except (DurableJobError,OSError,ValueError,TypeError,KeyError):
        raise HTTPException(status_code=409,detail={"code":"document_exact_actual_reap_required"})


@router.post("/document-pairs/{identifier}/discard")
async def discard_document_pair(request: Request, identifier: str, body: DocumentPairMutation):
    from src.work_board.document_pairs import reset_unbound
    try:
        async with get_session() as db:
            return await reset_unbound(db,_owner(_operator(request)),identifier,body.expected_revision,retry=False)
    except BoardError as exc:_raise_board_error(exc)


@router.get("/input-artifacts/{artifact_id}")
async def get_work_board_input_artifact(request: Request, artifact_id: str):
    operator = _operator(request)
    from src.auth.ownership import selected_read_scopes, selected_read_principal, RECOVERED_FIELDS
    try:
        async with get_session() as db:
            recovered = await selected_read_scopes(operator, "artifact", db=db)
            read_owner = WorkBoardOwner(principal_id=(await selected_read_principal(operator, "artifact", artifact_id, db=db)) if artifact_id in recovered else operator.principal.principal_id, session_id=recovered.get(artifact_id, operator.session_id))
            metadata = await read_input_artifact_metadata(
                db,
                read_owner,
                artifact_id=artifact_id,
            )
            payload = _input_artifact_payload(metadata, include_details=True)
            if artifact_id in recovered:
                payload.update(RECOVERED_FIELDS)
            return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Retry after the workspace database is ready."},
        ) from exc


@router.delete("/input-artifacts/{artifact_id}")
async def delete_work_board_input_artifact(
    request: Request,
    artifact_id: str,
    body: WorkBoardInputArtifactDelete,
):
    operator = _operator(request)
    try:
        async with get_session() as db:
            metadata = await delete_input_artifact(
                db,
                _owner(operator),
                artifact_id=artifact_id,
                expected_revision=body.expected_revision,
            )
            return _input_artifact_payload(metadata, include_details=True)
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Retry after the workspace database is ready."},
        ) from exc


@router.post("/tasks")
async def create_work_board_task(request: Request, body: WorkBoardTaskCreate):
    operator = _operator(request)
    try:
        async with get_session() as db:
            mutation = await repository.create_task(
                db,
                _owner(operator),
                body,
                origin_session_id=operator.session_id,
            )
            payload = {
                "task": await _safe_task_payload(mutation.task, db=db),
                "idempotent_replay": mutation.idempotent_replay,
            }
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


from src.work_board.contracts import GeneralTaskCreate, GeneralTaskPlanUpdate, GeneralTaskResume


@router.get("/general-tasks/tools")
async def general_task_tools(request: Request):
    _operator(request)
    try:
        if dispatcher.general_tasks is None:
            raise BoardError("general_task_inactive", "Task service inactive", status_code=503)
        descriptors, tool_set_digest = dispatcher.general_tasks.snapshot()
        return {"tool_set_digest": tool_set_digest,
                "tools": [item.model_dump(mode="json") for item in descriptors],
                "blocked_tools": dispatcher.general_tasks.registry.blocked_tools()}
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/general-tasks")
async def create_general_task(request: Request, body: GeneralTaskCreate):
    operator = _operator(request)
    try:
        if dispatcher.general_tasks is None:
            raise BoardError("general_task_inactive", "Task service inactive", status_code=503)
        async with get_session() as db:
            mutation = await dispatcher.general_tasks.create(db, _owner(operator), body)
            return {"task": await _safe_task_payload(mutation.task, db=db),
                    "idempotent_replay": mutation.idempotent_replay}
    except BoardError as exc:
        _raise_board_error(exc)


@router.get("/tasks/{task_id}/plan")
async def get_general_task_plan(request: Request, task_id: str):
    owner = _owner(_operator(request))
    try:
        if dispatcher.general_tasks is None:
            raise BoardError("general_task_inactive", "Task service inactive", status_code=503)
        async with get_session() as db:
            payload = await dispatcher.general_tasks.plan(db, owner, task_id)
            # Plans are data, but can still contain operator-supplied secrets.
            safe = await vault_redaction.redact_secrets_in_text_readonly(db,
                json.dumps(payload), fail_closed=True)
            return json.loads(safe)
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/tasks/{task_id}/plan")
async def update_general_task_plan(request: Request, task_id: str, body: GeneralTaskPlanUpdate):
    owner = _owner(_operator(request))
    try:
        if dispatcher.general_tasks is None:
            raise BoardError("general_task_inactive", "Task service inactive", status_code=503)
        async with get_session() as db:
            task = await dispatcher.general_tasks.update_plan(db, owner, task_id, body)
            return {"task": await _safe_task_payload(task, db=db)}
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/tasks/{task_id}/plan/resume")
async def resume_general_task_plan(request: Request, task_id: str, body: GeneralTaskResume):
    owner = _owner(_operator(request))
    try:
        task = await dispatcher.resume_general_task(owner, task_id, body)
        async with get_session() as db:
            attempt = await db.get(WorkBoardAttempt, body.attempt_id)
            return {"task": await _safe_task_payload(task, db=db, latest_attempt=attempt)}
    except BoardError as exc:
        _raise_board_error(exc)
    except Exception as exc:
        raise HTTPException(status_code=409, detail={"code": "general_task_resume_binding_changed",
            "message": "Refresh the exact original task and approval state"}) from exc


@router.get("/tasks/{task_id}")
async def get_work_board_task(request: Request, task_id: str):
    operator = _operator(request)
    from src.auth.ownership import selected_read_scopes, selected_read_principal
    try:
        async with get_session() as db:
            recovered = await selected_read_scopes(operator, "task", db=db)
            read_owner = WorkBoardOwner(principal_id=(await selected_read_principal(operator, "task", task_id, db=db)) if task_id in recovered else operator.principal.principal_id, session_id=recovered.get(task_id, operator.session_id))
            detail = await repository.get_detail(db, read_owner, task_id)
            dispatch_rank = None if task_id in recovered else await repository.dispatch_rank(db, _owner(operator), detail["task"])
            latest_attempt = detail["attempts"][0] if detail["attempts"] else None
            task_payload = await _safe_task_payload(
                detail["task"],
                db=db,
                dependency_counts=detail["dependency_counts"],
                latest_attempt=latest_attempt,
                attempt_count=len(detail["attempts"]),
                dispatch_rank=dispatch_rank,
                recovered_read_only=task_id in recovered,
            )
            if task_id not in recovered:
                from src.guardian.opportunity_plans import _linked_proposal, get_plan_projection
                linked = await _linked_proposal(db, detail["task"])
                if linked is not None and linked.opportunity_id:
                    projection = await get_plan_projection(db, linked)
                    task_payload.update({key: projection.get(key) for key in
                        ("opportunity_id", "opportunity_revision", "proposal_ref", "plan_preview")})
            attempts_payload = []
            for item in detail["attempts"]:
                item_payload = _attempt_payload(item)
                latest_payload = task_payload.get("latest_attempt")
                if (
                    isinstance(latest_payload, dict)
                    and item.attempt_id == latest_payload.get("attempt_id")
                    and "browser_execution" in latest_payload
                ):
                    item_payload["browser_execution"] = latest_payload.get("browser_execution")
                if (
                    isinstance(latest_payload, dict)
                    and item.attempt_id == latest_payload.get("attempt_id")
                    and "calendar_execution" in latest_payload
                ):
                    item_payload["calendar_execution"] = latest_payload.get("calendar_execution")
                attempts_payload.append(item_payload)
            return {
                "task": task_payload,
                "attempts": attempts_payload,
                "parents": [identifier for identifier in detail["parents"] if task_id not in recovered or identifier in recovered],
                "children": [identifier for identifier in detail["children"] if task_id not in recovered or identifier in recovered],
                "comments": [await _safe_comment_payload(item, db=db) for item in detail["comments"]],
                "events": [_event_payload(item) for item in detail["events"]],
                "parent_handoffs": [] if task_id in recovered else await review_service.parent_handoffs(
                    db,
                    _owner(operator),
                    detail["task"],
                ),
                "revision": detail["task"].task_revision,
            }
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.get("/tasks/{task_id}/routine-publication")
async def get_work_board_routine_publication(request: Request, task_id: str):
    """Read the exact publication recovery state for one board card."""

    operator = _operator(request)
    try:
        async with get_session() as db:
            context = await _routine_publication_context(db, operator, task_id)
            task_payload = await _safe_task_payload(
                context["task"],
                db=db,
                latest_attempt=context["attempt"],
                attempt_count=len(context["detail"]["attempts"]),
            )
        return {
            "task": task_payload,
            "publication": await _routine_publication_payload(context),
        }
    except (BoardError, RoutineError) as exc:
        if isinstance(exc, BoardError):
            _raise_board_error(exc)
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/routine-publication/prepare")
async def prepare_work_board_routine_publication(
    request: Request,
    task_id: str,
    body: WorkBoardRoutinePublicationPrepareRequest,
):
    """Prepare a bounded M3 preview from the task's canonical parent run.

    This route never accepts task/attempt/run/approval IDs from the browser and
    never approves or executes the publication.
    """

    operator = _operator(request)
    try:
        async with get_session() as db:
            context = await _routine_publication_context(
                db,
                operator,
                task_id,
                expected_revision=body.expected_revision,
            )
        if not await _operator_has_github_consent(operator, context):
            safely_blocked = await _block_routine_for_missing_external_authority(context)
            raise BoardError(
                "external_mutation_grant_required",
                "The current operator session has no external mutation grant; restore the grant before recovering this same run"
                if safely_blocked
                else "The current operator session has no external mutation grant; reconcile the linked run before retrying",
                status_code=403,
                recovery_action="restore_prerequisite",
            )
        existing_preview = context.get("preview")
        if context.get("m3_job_id"):
            # The exact M3 binding is immutable. A retry may redisplay it, but
            # cannot replace its approved body with a new caller-selected one.
            if isinstance(existing_preview, Mapping) and (
                str(existing_preview.get("body") or "") != body.body
                or str(existing_preview.get("title") or "") != str(body.title or "")
            ):
                raise BoardError(
                    "routine_publication_binding_conflict",
                    "The prepared publication preview is immutable; create a new governed invocation for changed text",
                )
        else:
            await dispatcher.resume_routine_attempt_for_operator_recovery(
                _owner(operator),
                context["task"],
                context["attempt"],
                context["parent"],
                expected_revision=body.expected_revision,
            )
            await routine_service.prepare_publication(
                context["routine_id"],
                context["parent_workflow_run_id"],
                RoutinePublicationRequest(title=body.title, body=body.body),
                owner_principal_id=operator.principal.principal_id,
                owner_session_id=operator.session_id,
                external_mutation_granted=True,
            )
            # Preparation resumes the durable parent only long enough to
            # create the exact M3 approval hold. Reconcile it back to
            # Blocked before returning the refreshed card and cursor.
            await dispatcher.reconcile_linked_attempts()
        async with get_session() as db:
            refreshed = await _routine_publication_context(db, operator, task_id)
            task_payload = await _safe_task_payload(
                refreshed["task"],
                db=db,
                latest_attempt=refreshed["attempt"],
                attempt_count=len(refreshed["detail"]["attempts"]),
            )
        return {
            "task": task_payload,
            "publication": await _routine_publication_payload(refreshed),
            "approval_required": True,
            "operator_action": "approve_exact_publication_in_pending_approvals",
        }
    except (BoardError, RoutineError) as exc:
        if isinstance(exc, BoardError):
            _raise_board_error(exc)
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/routine-publication/recover")
async def recover_work_board_routine_publication(
    request: Request,
    task_id: str,
    body: WorkBoardRoutinePublicationRecoverRequest,
):
    """Consume the exact approved M3 publication and reconcile the same card."""

    operator = _operator(request)
    try:
        async with get_session() as db:
            context = await _routine_publication_context(
                db,
                operator,
                task_id,
                expected_revision=body.expected_revision,
            )
        if not await _operator_has_github_consent(operator, context):
            safely_blocked = await _block_routine_for_missing_external_authority(context)
            raise BoardError(
                "external_mutation_grant_required",
                "The current operator session has no external mutation grant; restore the grant before recovering this same run"
                if safely_blocked
                else "The current operator session has no external mutation grant; reconcile the linked run before retrying",
                status_code=403,
                recovery_action="restore_prerequisite",
            )
        if not context.get("m3_job_id") or not context.get("approval_id"):
            raise BoardError(
                "routine_publication_preview_required",
                "Prepare and inspect the exact publication preview before approval",
            )
        if context.get("approval_status") != "approved":
            raise BoardError(
                "approval_not_current",
                "Approve the exact publication preview in Pending approvals before resuming",
                recovery_action="approve_existing_run",
            )
        await dispatcher.resume_routine_attempt_for_operator_recovery(
            _owner(operator),
            context["task"],
            context["attempt"],
            context["parent"],
            expected_revision=body.expected_revision,
        )
        try:
            result = await routine_service.recover(
                context["routine_id"],
                context["parent_workflow_run_id"],
                owner_principal_id=operator.principal.principal_id,
                owner_session_id=operator.session_id,
                external_mutation_granted=True,
            )
        finally:
            # The routine service remains execution authority. The board
            # dispatcher projects the same fenced attempt only after the
            # current durable run exposes a verified independent readback;
            # otherwise it returns the card to a truthful recovery Blocked
            # state or leaves an actually running run under its live lease.
            await dispatcher.reconcile_linked_attempts()
        async with get_session() as db:
            detail = await repository.get_detail(db, _owner(operator), task_id)
            latest_attempt = detail["attempts"][0] if detail["attempts"] else None
            task_payload = await _safe_task_payload(
                detail["task"],
                db=db,
                dependency_counts=detail["dependency_counts"],
                latest_attempt=latest_attempt,
                attempt_count=len(detail["attempts"]),
            )
        publication = await _routine_publication_payload(
            {
                **context,
                "parent": {
                    **context["parent"],
                    "status": result.get("status") or context["parent"].get("status"),
                },
                "publication_response": result.get("child") if isinstance(result, Mapping) else None,
            }
        )
        return {
            "task": task_payload,
            "publication": publication,
            "recovery": result,
            "readback_required": True,
        }
    except (BoardError, RoutineError) as exc:
        if isinstance(exc, BoardError):
            _raise_board_error(exc)
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.patch("/tasks/{task_id}")
async def patch_work_board_task(request: Request, task_id: str, body: WorkBoardTaskPatch):
    operator = _operator(request)
    try:
        async with get_session() as db:
            mutation = await repository.patch_task(db, _owner(operator), task_id, body)
            payload = {"task": await _safe_task_payload(mutation.task, db=db)}
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/actions")
async def action_work_board_task(request: Request, task_id: str, body: WorkBoardActionRequest):
    operator = _operator(request)
    try:
        owner = _owner(operator)
        if body.action.value in {"pause", "resume"}:
            if body.model_fields_set - {"action", "expected_revision"}:
                raise BoardError("unsupported_action_fields", "Native controls accept the current task revision only", status_code=422)
            try:
                task, attempt = await dispatcher.control_general_task(owner, task_id,
                    expected_revision=body.expected_revision, action=body.action.value)
            except BoardError:
                raise
            except Exception as exc:
                raise BoardError("general_task_control_blocked", "Refresh the original task; active or unknown tool work must close before safe pause or resume", status_code=409) from exc
            return {"task": await _safe_task_payload(task, latest_attempt=attempt),
                "attempt": _attempt_payload(attempt)}
        if body.action.value == "cancel":
            projection = await dispatcher.cancel_task(
                owner,
                task_id,
                expected_revision=body.expected_revision,
            )
            task_payload = await _safe_task_payload(
                projection.task,
                latest_attempt=projection.attempt,
                attempt_count=1,
            )
            payload = {
                **_action_receipt_payload(
                    task_payload,
                    projection.event,
                    attempt_id=projection.attempt.attempt_id,
                ),
                "task": task_payload,
                "attempt": _attempt_payload(projection.attempt),
            }
            return payload
        if body.action.value == "retry":
            await dispatcher.validate_retry(
                owner,
                task_id,
                expected_revision=body.expected_revision,
            )
        async with get_session() as db:
            if body.action.value == "request_review":
                mutation = await review_service.request_review(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    attempt_id=body.attempt_id or "",
                    evidence_refs=body.evidence_refs,
                    repository=repository,
                )
            elif body.action.value == "request_changes":
                mutation = await review_service.request_changes(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    reason=body.reason or "",
                    repository=repository,
                )
            elif body.action.value == "complete_review":
                mutation = await review_service.complete_review(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    attempt_id=body.attempt_id or "",
                    repository=repository,
                )
            elif body.action.value == "renew_review":
                mutation = await review_service.renew_review(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    repository=repository,
                )
            elif body.action.value == "block" and body.block_kind not in WORK_BOARD_AUTHENTICATED_BLOCK_KINDS:
                # Workflow/effect categories are written only after the
                # authoritative runtime has reconciled them.  The
                # authenticated generic action may record only bounded board
                # recovery categories; repository.action_task repeats this
                # check for direct callers and performs the revision/source
                # status CAS.
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_block_kind"},
                )
            elif body.action.value == "unblock":
                mutation = await review_service.unblock_task(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    resolution=body.resolution or "",
                    repository=repository,
                )
            elif body.action.value == "retry":
                mutation = await repository.retry_task(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                )
            else:
                if body.action.value == "promote":
                    promoted = await repository.get_task(db, owner, task_id)
                    if promoted.capability_id == "agent.task.v1":
                        if dispatcher.general_tasks is None:
                            raise BoardError("general_task_inactive", "Task service inactive", status_code=503)
                        await dispatcher.general_tasks.validate_acceptance(db, owner, task_id,
                            body.expected_revision)
                mutation = await repository.action_task(db, owner, task_id, body)
            latest_attempt = (
                await db.execute(
                    select(WorkBoardAttempt)
                    .where(WorkBoardAttempt.task_id == mutation.task.task_id)
                    .order_by(
                        WorkBoardAttempt.created_at.desc(),
                        WorkBoardAttempt.attempt_id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        # Recovery guidance performs fresh provider-free authority checks.
        # Build this projection only after the mutation session commits so a
        # manual Block response cannot validate the pre-mutation Todo state
        # and incorrectly hide its new Unblock action.
        task_payload = await _safe_task_payload(
            mutation.task,
            latest_attempt=latest_attempt,
        )
        payload = {
            **_action_receipt_payload(
                task_payload,
                mutation.event,
                attempt_id=(latest_attempt.attempt_id if latest_attempt is not None else None),
            ),
            "task": task_payload,
        }
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/specify")
async def specify_work_board_task(
    request: Request,
    task_id: str,
    body: WorkBoardProposalRequest,
):
    operator = _operator(request)
    try:
        return await triage_service.create_proposal(
            _owner(operator),
            task_id,
            kind="specify",
            request=body,
            operator=operator,
        )
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.get("/tasks/{task_id}/proposals")
async def list_work_board_proposals(
    request: Request,
    task_id: str,
    kind: str | None = Query(default=None),
):
    operator = _operator(request)
    try:
        return {
            "proposals": await triage_service.list_proposals(
                _owner(operator),
                task_id,
                kind=kind,
            )
        }
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Retry after the workspace database is ready."},
        ) from exc


@router.get("/proposals/{proposal_id}")
async def get_work_board_proposal(request: Request, proposal_id: str):
    operator = _operator(request)
    try:
        return await triage_service.get_proposal(_owner(operator), proposal_id, operator=operator)
    except BoardError as exc:
        _raise_board_error(exc)
    except OpportunityError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Retry after the workspace database is ready."},
        ) from exc


@router.post("/tasks/{task_id}/decompose")
async def decompose_work_board_task(
    request: Request,
    task_id: str,
    body: WorkBoardProposalRequest,
):
    operator = _operator(request)
    try:
        return await triage_service.create_proposal(
            _owner(operator),
            task_id,
            kind="decompose",
            request=body,
            operator=operator,
        )
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/proposals/{proposal_id}/accept")
async def accept_work_board_proposal(
    request: Request,
    proposal_id: str,
    body: WorkBoardProposalAccept,
):
    operator = _operator(request)
    try:
        return await triage_service.accept_proposal(_owner(operator), proposal_id, body, operator=operator)
    except BoardError as exc:
        _raise_board_error(exc)
    except OpportunityError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/proposals/{proposal_id}/reject")
async def reject_work_board_proposal(
    request: Request,
    proposal_id: str,
    body: WorkBoardProposalReject,
):
    operator = _operator(request)
    try:
        return await triage_service.reject_proposal(_owner(operator), proposal_id, body)
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/comments")
async def add_work_board_comment(request: Request, task_id: str, body: WorkBoardCommentCreate):
    operator = _operator(request)
    try:
        async with get_session() as db:
            comment, event = await repository.add_comment(db, _owner(operator), task_id, body, provenance="operator")
            payload = {"comment": await _safe_comment_payload(comment, db=db)}
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/links")
async def add_work_board_link(request: Request, body: WorkBoardLinkCreate):
    operator = _operator(request)
    try:
        async with get_session() as db:
            link, event = await repository.add_link(db, _owner(operator), body)
            payload = {
                "link": {
                    "link_id": link.link_id,
                    "parent_task_id": link.parent_task_id,
                    "child_task_id": link.child_task_id,
                    "created_at": _json_value(link.created_at),
                }
            }
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.delete("/links")
async def delete_work_board_link(request: Request, body: WorkBoardLinkDelete):
    operator = _operator(request)
    try:
        async with get_session() as db:
            event = await repository.delete_link(db, _owner(operator), body)
        return {"deleted": True, "event_id": event.event_id}
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.get("/events")
async def list_work_board_events(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
):
    operator = _operator(request)
    try:
        async with get_session() as db:
            page = await repository.list_events(db, _owner(operator), after=after, limit=limit)
            return {
                "events": [_event_payload(event) for event in page.events],
                "last_event_id": page.last_event_id,
                "gap": page.gap,
            }
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


__all__ = ["repository", "router"]


@router.get("/tasks/{task_id}/near-text/output")
async def read_near_text_output(request: Request, task_id: str):
    from src.work_board.near_text_native import read_output,ledger_before_output,_ledger
    from src.work_board.dispatcher import _parse_typed_input
    owner=_owner(_operator(request))
    try:
        async with get_session() as db:
            task=await WorkBoardRepository().get_task(db,owner,task_id)
            if task.capability_id!="inference.near-text.v1" or task.status not in {WorkBoardStatus.review,WorkBoardStatus.done}:
                raise BoardError("near_output_unavailable","A verified settled NEAR result is required")
            attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id)
                .order_by(WorkBoardAttempt.created_at.desc(),WorkBoardAttempt.attempt_id.desc()).limit(1))
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id)) if attempt else None
            if run is None or run.status!="succeeded":raise BoardError("near_output_unavailable","The actual succeeded native source is required")
            # Current authenticated ownership/Goal is mandatory; historical execution TTL cannot renew contact.
            from src.model_fabric.near_text_contracts import NearTextReceipt
            goal=await WorkBoardRepository._validate_goal(db,owner,goal_id=task.goal_id,goal_revision=task.goal_revision)
            from src.memory.procedure_recommendations import assert_current_root
            await assert_current_root(db,_operator(request))
            from src.work_board.near_text_native import finite_goal_budget
            finite_goal_budget(goal)
            checkpoints=json.loads(run.checkpoint_receipts_json or '[]')
            proof=[c.get('payload',{}) for c in checkpoints if c.get('checkpoint_id')=='near-private-output']
            if len(proof)!=1 or not isinstance(proof[0].get('operation_id'),str):
                raise BoardError("near_output_unavailable","The original charge checkpoint is required")
            await ledger_before_output(db,operation_id=proof[0]['operation_id'],job=run.run_identity)
            output=read_output(task,attempt,run)
            await _ledger(db,NearTextReceipt.model_validate(output["receipt"]),job=run.run_identity)
            return output
    except BoardError as exc:_raise_board_error(exc)
    except (OSError,ValueError,TypeError,KeyError):
        raise HTTPException(status_code=409,detail={"code":"near_output_readback_required"})
