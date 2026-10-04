"""Operator-only readback and local execution controls for v2 capability packs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.capabilities import _require_authenticated_capability_operator
from src.extensions.capability_pack import (
    CapabilityPackLifecycle,
    CapabilityPackLifecycleError,
    canonical_digest,
)
from src.goals.repository import deserialize_success_criterion, goal_repository


router = APIRouter()


class LocalExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str
    job_id: str
    domain: str = Field(pattern=r"^(primary|secondary|research|research_brief|goal_snapshot|snapshot)$")
    artifact_path: str | None = None
    source_url: str | None = None
    query: str | None = None
    source_payload: dict[str, Any] | list[Any] | str | None = None
    goal_snapshot: dict[str, Any] | str | None = None


class ReconciliationResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    action: str = Field(default="cancel", pattern=r"^(cancel|recover)$")


class CapabilityPackLifecycleResponse(BaseModel):
    """Stable operator response shared by every lifecycle transition."""

    model_config = ConfigDict(extra="allow")

    status: str
    pointer: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None


class CapabilityPackVersionRequest(BaseModel):
    """Reviewed package material required for activation or update."""

    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any]
    root_path: str = Field(min_length=1, max_length=4096)
    goal_id: str = Field(min_length=1, max_length=256)
    review_id: str = Field(min_length=1, max_length=256)
    approval_id: str = Field(min_length=1, max_length=256)
    content_digest: str | None = Field(default=None, min_length=64, max_length=64)
    authority_digest: str | None = Field(default=None, min_length=64, max_length=64)


class CapabilityPackApprovalRequest(BaseModel):
    """Exact durable approval binding for an existing active version."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="operator_requested", min_length=1, max_length=256)
    content_digest: str | None = Field(default=None, min_length=64, max_length=64)
    authority_digest: str | None = Field(default=None, min_length=64, max_length=64)


class CapabilityPackApprovalPrepareRequest(BaseModel):
    """Exact reviewed request an authenticated operator is asked to decide."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(pattern=r"^(activate|update|pause|revoke|uninstall|rollback)$")
    goal_id: str = Field(min_length=1, max_length=256)
    digest: str | None = Field(default=None, min_length=64, max_length=64)
    version: str | None = Field(default=None, min_length=1, max_length=128)
    current_digest: str | None = Field(default=None, min_length=64, max_length=64)
    content_digest: str | None = Field(default=None, min_length=64, max_length=64)
    authority_digest: str | None = Field(default=None, min_length=64, max_length=64)
    authority_delta: dict[str, Any] | None = None


def _approval_response(payload: dict[str, Any]) -> dict[str, Any]:
    approval = payload.get("approval")
    return {
        "status": str(approval.get("status") if isinstance(approval, dict) else payload.get("status") or "unknown"),
        "approval": approval,
        "receipt": payload.get("receipt"),
    }


class CapabilityPackRollbackRequest(CapabilityPackApprovalRequest):
    goal_id: str | None = Field(default=None, min_length=1, max_length=256)


class CapabilityPackRevokeRequest(CapabilityPackApprovalRequest):
    digest: str | None = Field(default=None, min_length=64, max_length=64)


def _store() -> CapabilityPackLifecycle:
    return CapabilityPackLifecycle()


class FixedFormatterReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class AuthoredInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    root_path: str = Field(min_length=1, max_length=4096)


class AuthoredReviewRequest(FixedFormatterReviewRequest):
    root_path: str = Field(min_length=1, max_length=4096)
    acknowledge_unsigned_local: bool


def _authored_packet(root_path):
    from pathlib import Path
    from src.extensions.authored_adapter import read_member, load_adapter
    from src.extensions.capability_pack import parse_capability_pack_manifest, validate_capability_pack_package, capability_pack_digest
    root=Path(root_path)
    manifest=parse_capability_pack_manifest(read_member(root,"manifest.yaml",65536).decode())
    checked=validate_capability_pack_package(root,manifest=manifest)
    if not checked["ok"] or not manifest.contributes.adapters:
        raise ValueError("authored_package_static_contract_invalid")
    adapter=load_adapter(root,manifest)
    from src.execution.tool_package_profile import inspect_runtime
    from src.work_board.tool_package_native import runtime_root
    try:
        profile={"status":"available",**inspect_runtime(runtime_root())}
    except (OSError,ValueError,RuntimeError):
        profile={"status":"blocked","reason":"tool_package_profile_unavailable"}
    return {"pack_id":manifest.id,"manifest":manifest.model_dump(mode="json"),"root_path":str(root),
        "content_digest":capability_pack_digest(root),"authority_digest":manifest.authority_digest,
        "descriptor":adapter.descriptor,"code_text":adapter.code.decode(),"profile":profile,
        "publisher_verified":False,"signature_status":"unsigned-local","no_learning":True}


@router.post("/capability-packs/authored/inspect")
async def inspect_authored_package(req: AuthoredInspectRequest, request: Request):
    _operator_identity(request)
    try:
        return _authored_packet(req.root_path)
    except (OSError,ValueError,KeyError,TypeError):
        raise HTTPException(status_code=422,detail={"code":"authored_package_static_contract_invalid"})


@router.post("/capability-packs/{pack_id}/review")
async def review_authored_package(pack_id: str, req: AuthoredReviewRequest, request: Request):
    _operator,principal_id,session_id=_operator_identity(request)
    if req.acknowledge_unsigned_local is not True:
        raise HTTPException(status_code=403,detail={"code":"authored_package_unsigned_acknowledgement_required"})
    try:
        packet=_authored_packet(req.root_path)
        if (packet["pack_id"]!=pack_id or packet["content_digest"]!=req.content_digest or
            packet["authority_digest"]!=req.authority_digest):
            raise ValueError("authored_package_exact_review_changed")
        from src.db.engine import get_session
        from src.work_board.repository import WorkBoardRepository
        from src.work_board.contracts import WorkBoardOwner
        async with get_session() as db:
            await WorkBoardRepository._validate_goal(db,WorkBoardOwner(principal_id=principal_id,session_id=session_id),
                goal_id=req.goal_id,goal_revision=req.goal_revision)
        return _store().review(packet["manifest"],root_path=req.root_path,goal_id=req.goal_id,
            reviewed_by=principal_id,authority_expansion_approved=True)
    except (OSError,ValueError,KeyError,TypeError):
        raise HTTPException(status_code=409,detail={"code":"authored_package_exact_review_changed"})


@router.get("/capability-packs/seraph.tool.json-format/profile")
async def fixed_formatter_profile(request: Request):
    _operator, principal_id, session_id = _operator_identity(request)
    from src.execution.tool_package_profile import inspect_runtime, source_package, package_manifest
    from src.extensions.capability_pack import capability_pack_digest
    from src.work_board.tool_package_native import runtime_root
    root = source_package().parent
    manifest = package_manifest()
    try:
        profile = {"status":"available", **inspect_runtime(runtime_root())}
    except (OSError, ValueError, RuntimeError):
        profile = {"status":"blocked", "reason":"The optional native Linux x86_64 profile or its exact private dependencies are unavailable; core controls remain usable."}
    try:
        lifecycle = _store().status(manifest.id,owner_principal_id=principal_id,session_id=session_id)
    except CapabilityPackLifecycleError as exc:
        raise _lifecycle_http_error(exc) from exc
    return {"pack_id":manifest.id,"manifest":manifest.model_dump(mode="json"),"root_path":str(root),
        "content_digest":capability_pack_digest(root),"authority_digest":manifest.authority_digest,
        "profile":profile,"lifecycle":lifecycle,
        "no_learning":True}


@router.post("/capability-packs/seraph.tool.json-format/review")
async def fixed_formatter_review(req: FixedFormatterReviewRequest, request: Request):
    _operator, principal_id, session_id = _operator_identity(request)
    from src.execution.tool_package_profile import source_package, package_manifest
    from src.extensions.capability_pack import capability_pack_digest
    from src.db.engine import get_session
    from src.work_board.repository import WorkBoardRepository, BoardError
    from src.work_board.contracts import WorkBoardOwner
    root=source_package().parent
    manifest=package_manifest()
    if req.content_digest!=capability_pack_digest(root) or req.authority_digest!=manifest.authority_digest:
        raise HTTPException(status_code=409,detail={"code":"tool_package_exact_review_changed"})
    try:
        async with get_session() as db:
            await WorkBoardRepository._validate_goal(db,WorkBoardOwner(principal_id=principal_id,session_id=session_id),
                goal_id=req.goal_id,goal_revision=req.goal_revision)
    except BoardError as exc:
        raise HTTPException(status_code=exc.status_code,detail={"code":exc.code}) from exc
    try:
        return _store().review(manifest,root_path=root,goal_id=req.goal_id,
            reviewed_by=principal_id,authority_expansion_approved=True)
    except CapabilityPackLifecycleError as exc:
        raise _lifecycle_http_error(exc) from exc


def _operator_identity(request: Request) -> tuple[Any, str, str]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    return operator, principal_id, operator.session_id


def _lifecycle_http_error(exc: Exception) -> HTTPException:
    """Return a redacted error; paths and approval internals never cross API."""

    from src.extensions.capability_pack import CapabilityPackLifecycleBusy
    if isinstance(exc, CapabilityPackLifecycleBusy):
        return HTTPException(status_code=409,detail={"code":"capability_pack_lifecycle_busy","recovery":"Retry the exact request after the current short authority commit finishes."})
    message = str(exc).lower()
    # Expiry and CAS races are retryable state conflicts even though their
    # internal messages contain the word approval. Keep binding details redacted.
    if any(token in message for token in ("stale", "expired", "already resolved")):
        status_code = 409
        code = "capability_pack_invalid_state"
    elif any(token in message for token in ("owner", "session", "identity", "approval", "authority")):
        status_code = 403
        code = "capability_pack_authority_denied"
    elif any(token in message for token in ("requires an active", "has no active", "no rollback", "already")):
        status_code = 409
        code = "capability_pack_invalid_state"
    else:
        status_code = 422
        code = "capability_pack_lifecycle_rejected"
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "recovery": "Refresh the authenticated pack readback and submit a matching reviewed approval.",
        },
    )


def _canonical_goal_snapshot(goal: Any, *, owner_principal_id: str, session_id: str) -> dict[str, Any]:
    """Serialize only the persisted goal row for capability-pack execution.

    The request may carry a revision and identity assertion, but its mutable
    content is never copied into the artifact.  This keeps the API's snapshot
    source authoritative even when a caller submits forged title, description,
    criterion, or scheduling fields.
    """

    criterion = deserialize_success_criterion(goal)

    def _iso(value: Any) -> str | None:
        return value.isoformat() if value is not None and hasattr(value, "isoformat") else None

    return {
        "goal_id": str(goal.id),
        "parent_id": goal.parent_id,
        "path": goal.path,
        "title": goal.title,
        "description": goal.description,
        "level": str(goal.level.value if hasattr(goal.level, "value") else goal.level),
        "domain": str(goal.domain.value if hasattr(goal.domain, "value") else goal.domain),
        "status": str(goal.status.value if hasattr(goal.status, "value") else goal.status),
        "start_date": _iso(getattr(goal, "start_date", None)),
        "due_date": _iso(getattr(goal, "due_date", None)),
        "sort_order": goal.sort_order,
        "revision": max(int(goal.revision or 1), 1),
        "success_criterion": criterion.model_dump(mode="json") if criterion else None,
        "proactive_enabled": bool(getattr(goal, "proactive_enabled", False)),
        "created_at": _iso(getattr(goal, "created_at", None)),
        "updated_at": _iso(getattr(goal, "updated_at", None)),
        "owner_principal_id": owner_principal_id,
        "session_id": session_id,
        "canonical_source": "goals",
    }


@router.get("/capability-packs/{pack_id}")
async def capability_pack_readback(pack_id: str, request: Request) -> dict[str, Any]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    try:
        return _store().status(pack_id, owner_principal_id=principal_id, session_id=operator.session_id)
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/capability-packs/{pack_id}/approvals")
async def capability_pack_prepare_approval(
    pack_id: str,
    req: CapabilityPackApprovalPrepareRequest,
    request: Request,
) -> dict[str, Any]:
    """Prepare one owner-bound JSON approval for a lifecycle transition."""

    _operator, principal_id, session_id = _operator_identity(request)
    try:
        return _approval_response(
            _store().prepare_operator_approval(
                pack_id,
                action=req.action,
                goal_id=req.goal_id,
                digest=req.digest,
                version=req.version,
                current_digest=req.current_digest,
                authority_delta_payload=req.authority_delta,
                owner_principal_id=principal_id,
                session_id=session_id,
                content_digest=req.content_digest,
                authority_digest=req.authority_digest,
            )
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


@router.get("/capability-packs/{pack_id}/approvals")
async def capability_pack_list_approvals(pack_id: str, request: Request) -> list[dict[str, Any]]:
    _operator, principal_id, session_id = _operator_identity(request)
    try:
        return _store().list_operator_approvals(
            pack_id,
            owner_principal_id=principal_id,
            session_id=session_id,
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


@router.get("/capability-packs/{pack_id}/approvals/{approval_id}")
async def capability_pack_get_approval(
    pack_id: str,
    approval_id: str,
    request: Request,
) -> dict[str, Any]:
    _operator, principal_id, session_id = _operator_identity(request)
    try:
        return _store().get_operator_approval(
            pack_id,
            approval_id,
            owner_principal_id=principal_id,
            session_id=session_id,
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


async def _resolve_capability_pack_approval(
    pack_id: str,
    approval_id: str,
    request: Request,
    *,
    decision: str,
) -> dict[str, Any]:
    _operator, principal_id, session_id = _operator_identity(request)
    try:
        return _approval_response(
            _store().resolve_operator_approval(
                pack_id,
                approval_id,
                decision=decision,
                owner_principal_id=principal_id,
                session_id=session_id,
            )
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


@router.post("/capability-packs/{pack_id}/approvals/{approval_id}/approve")
async def capability_pack_approve_approval(
    pack_id: str,
    approval_id: str,
    request: Request,
) -> dict[str, Any]:
    return await _resolve_capability_pack_approval(
        pack_id,
        approval_id,
        request,
        decision="approved",
    )


@router.post("/capability-packs/{pack_id}/approvals/{approval_id}/deny")
async def capability_pack_deny_approval(
    pack_id: str,
    approval_id: str,
    request: Request,
) -> dict[str, Any]:
    return await _resolve_capability_pack_approval(
        pack_id,
        approval_id,
        request,
        decision="denied",
    )


@router.post(
    "/capability-packs/{pack_id}/activate",
    response_model=CapabilityPackLifecycleResponse,
)
async def capability_pack_activate(
    pack_id: str,
    req: CapabilityPackVersionRequest,
    request: Request,
) -> CapabilityPackLifecycleResponse:
    _operator, principal_id, session_id = _operator_identity(request)
    if str(req.manifest.get("id") or "") != pack_id:
        raise HTTPException(status_code=422, detail={"code": "pack_identity_mismatch"})
    try:
        return _store().activate(
            req.manifest,
            root_path=req.root_path,
            goal_id=req.goal_id,
            review_id=req.review_id,
            approval_id=req.approval_id,
            owner_principal_id=principal_id,
            session_id=session_id,
            content_digest=req.content_digest,
            authority_digest=req.authority_digest,
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


@router.post(
    "/capability-packs/{pack_id}/update",
    response_model=CapabilityPackLifecycleResponse,
)
async def capability_pack_update(
    pack_id: str,
    req: CapabilityPackVersionRequest,
    request: Request,
) -> CapabilityPackLifecycleResponse:
    _operator, principal_id, session_id = _operator_identity(request)
    if str(req.manifest.get("id") or "") != pack_id:
        raise HTTPException(status_code=422, detail={"code": "pack_identity_mismatch"})
    try:
        return _store().update(
            req.manifest,
            root_path=req.root_path,
            goal_id=req.goal_id,
            review_id=req.review_id,
            approval_id=req.approval_id,
            owner_principal_id=principal_id,
            session_id=session_id,
            content_digest=req.content_digest,
            authority_digest=req.authority_digest,
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


@router.post(
    "/capability-packs/{pack_id}/pause",
    response_model=CapabilityPackLifecycleResponse,
)
async def capability_pack_pause(
    pack_id: str,
    req: CapabilityPackApprovalRequest,
    request: Request,
) -> CapabilityPackLifecycleResponse:
    _operator, principal_id, session_id = _operator_identity(request)
    try:
        return _store().pause(
            pack_id,
            approval_id=req.approval_id,
            reason=req.reason,
            owner_principal_id=principal_id,
            session_id=session_id,
            content_digest=req.content_digest,
            authority_digest=req.authority_digest,
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


@router.post(
    "/capability-packs/{pack_id}/rollback",
    response_model=CapabilityPackLifecycleResponse,
)
async def capability_pack_rollback(
    pack_id: str,
    req: CapabilityPackRollbackRequest,
    request: Request,
) -> CapabilityPackLifecycleResponse:
    _operator, principal_id, session_id = _operator_identity(request)
    try:
        return _store().rollback(
            pack_id,
            goal_id=req.goal_id,
            approval_id=req.approval_id,
            owner_principal_id=principal_id,
            session_id=session_id,
            content_digest=req.content_digest,
            authority_digest=req.authority_digest,
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


@router.post(
    "/capability-packs/{pack_id}/revoke",
    response_model=CapabilityPackLifecycleResponse,
)
async def capability_pack_revoke(
    pack_id: str,
    req: CapabilityPackRevokeRequest,
    request: Request,
) -> CapabilityPackLifecycleResponse:
    _operator, principal_id, session_id = _operator_identity(request)
    try:
        return _store().revoke(
            pack_id,
            digest=req.digest,
            approval_id=req.approval_id,
            reason=req.reason,
            owner_principal_id=principal_id,
            session_id=session_id,
            content_digest=req.content_digest,
            authority_digest=req.authority_digest,
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


@router.post(
    "/capability-packs/{pack_id}/uninstall",
    response_model=CapabilityPackLifecycleResponse,
)
async def capability_pack_uninstall(
    pack_id: str,
    req: CapabilityPackApprovalRequest,
    request: Request,
) -> CapabilityPackLifecycleResponse:
    _operator, principal_id, session_id = _operator_identity(request)
    try:
        return _store().uninstall(
            pack_id,
            approval_id=req.approval_id,
            reason=req.reason,
            owner_principal_id=principal_id,
            session_id=session_id,
            content_digest=req.content_digest,
            authority_digest=req.authority_digest,
        )
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise _lifecycle_http_error(exc) from exc


@router.post("/capability-packs/{pack_id}/reconcile")
async def capability_pack_reconcile(pack_id: str, request: Request) -> dict[str, Any]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    try:
        return _store().reconcile(pack_id, owner_principal_id=principal_id, session_id=operator.session_id)
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/capability-packs/{pack_id}/execute-local")
async def capability_pack_execute_local(
    pack_id: str,
    req: LocalExecutionRequest,
    request: Request,
) -> dict[str, Any]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    source_payload = req.source_payload

    canonical_goal_snapshot: dict[str, Any] | str | None = req.goal_snapshot
    if req.domain in {"secondary", "goal_snapshot", "snapshot"}:
        try:
            goal = await goal_repository.get(req.goal_id)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "goal_snapshot_readback_unavailable", "recovery": "Retry after canonical goal storage recovers."},
            ) from exc
        if goal is None:
            raise HTTPException(status_code=404, detail={"code": "goal_not_found", "goal_id": req.goal_id})
        current_revision = max(int(goal.revision or 1), 1)
        if not isinstance(req.goal_snapshot, dict):
            raise HTTPException(status_code=422, detail="goal_snapshot must be a canonical persisted goal mapping")
        if req.goal_snapshot.get("goal_id") not in {None, goal.id}:
            raise HTTPException(status_code=422, detail="goal_snapshot identity does not match the requested goal")
        if req.goal_snapshot.get("owner_principal_id") not in {None, principal_id} or req.goal_snapshot.get("session_id") not in {None, operator.session_id}:
            raise HTTPException(status_code=403, detail="goal_snapshot operator binding conflicts with the authenticated session")
        supplied_revision = req.goal_snapshot.get("revision", req.goal_snapshot.get("goal_revision"))
        if isinstance(supplied_revision, bool) or not isinstance(supplied_revision, int) or supplied_revision != current_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "stale_goal_revision",
                    "goal_id": req.goal_id,
                    "expected_revision": supplied_revision,
                    "current_revision": current_revision,
                    "recovery": "Refresh the goal and resubmit against the current revision.",
                },
            )
        canonical_goal_snapshot = _canonical_goal_snapshot(
            goal,
            owner_principal_id=principal_id,
            session_id=operator.session_id,
        )
        if canonical_goal_snapshot["status"] != "active":
            raise HTTPException(status_code=409, detail={"code": "goal_not_active", "goal_id": req.goal_id})

    try:
        return _store().execute_local(
            pack_id,
            goal_id=req.goal_id,
            job_id=req.job_id,
            domain=req.domain,
            artifact_path=req.artifact_path,
            owner_principal_id=principal_id,
            session_id=operator.session_id,
            source_url=req.source_url or "local://intercepted/source",
            query=req.query,
            goal_snapshot=canonical_goal_snapshot,
            source_payload=source_payload,
            source_payload_digest=canonical_digest(source_payload) if source_payload is not None else None,
        )
    except CapabilityPackLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/capability-packs/{pack_id}/reconcile/resolve")
async def capability_pack_resolve_reconciliation(
    pack_id: str,
    req: ReconciliationResolutionRequest,
    request: Request,
) -> dict[str, Any]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    try:
        return _store().resolve_reconciliation(
            pack_id,
            job_id=req.job_id,
            action=req.action,
            owner_principal_id=principal_id,
            session_id=operator.session_id,
        )
    except CapabilityPackLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


__all__ = [
    "CapabilityPackApprovalRequest",
    "CapabilityPackApprovalPrepareRequest",
    "CapabilityPackLifecycleResponse",
    "CapabilityPackRevokeRequest",
    "CapabilityPackRollbackRequest",
    "CapabilityPackVersionRequest",
    "LocalExecutionRequest",
    "ReconciliationResolutionRequest",
    "router",
]
