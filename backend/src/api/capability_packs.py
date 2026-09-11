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


def _operator_identity(request: Request) -> tuple[Any, str, str]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    return operator, principal_id, operator.session_id


def _lifecycle_http_error(exc: Exception) -> HTTPException:
    """Return a redacted error; paths and approval internals never cross API."""

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
