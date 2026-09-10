from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel, Field

from src.auth.service import AuthenticatedOperator
from src.memory.benchmark import build_guardian_memory_benchmark_report
from src.memory.control import (
    apply_memory_live_control_action,
    audit_memory,
    correct_memory,
    export_memory_recovery as export_memory_recovery_control,
    forget_memory,
    get_memory_live_controls_snapshot,
    list_memory_audit_receipts,
    memory_operator_policy_payload,
    memory_recovery_status,
    pin_memory,
    rebuild_memory_recovery as rebuild_memory_recovery_control,
    restore_memory_recovery as restore_memory_recovery_control,
)
from src.memory.decay import summarize_memory_reconciliation_state
from src.memory.providers import list_memory_provider_inventory
from src.memory.repository import memory_repository
from src.security.trust_contract import AuthorityGrant, PrincipalType

router = APIRouter()


class MemoryCorrectionRequest(BaseModel):
    content: str = Field(min_length=1)
    kind: str = "fact"
    summary: str | None = None
    corrects_memory_id: str | None = None
    source_session_id: str | None = None
    actor: str = "operator"
    source_role: str = "operator"
    reason: str | None = None
    confidence: float = 0.95
    importance: float = 0.9
    privacy_boundary: str | None = None
    metadata: dict[str, object] | None = None


class MemoryPinRequest(BaseModel):
    actor: str = "operator"
    reason: str | None = None
    privacy_boundary: str | None = None


class MemoryForgetRequest(BaseModel):
    actor: str = "operator"
    reason: str | None = None
    mode: str = "archive"
    privacy_boundary: str | None = None


class MemoryAuditRequest(BaseModel):
    actor: str = "operator"
    reason: str | None = None


class MemoryLiveControlActionRequest(BaseModel):
    action: str
    acknowledged: bool = False
    acknowledge_rollback_boundary: bool = False
    owner_session_id: str | None = None
    actor: str = "operator"
    reason: str | None = None
    memory_id: str | None = None
    provider_name: str | None = None
    outcome: str | None = None
    privacy_boundary: str | None = None


class MemoryRecoveryRequest(BaseModel):
    owner_session_id: str | None = None
    source_session_id: str | None = None
    source_role: str = "operator"
    actor: str = "operator"
    limit: int = Field(default=10_000, ge=1, le=10_000)


class MemoryRestoreRequest(BaseModel):
    archive: dict[str, Any]
    owner_session_id: str | None = None
    source_session_id: str | None = None
    source_role: str = "operator"
    actor: str = "operator"


@dataclass(frozen=True)
class AuthenticatedMemoryContext:
    actor: str
    session_id: str
    source_role: str = "operator"


def authenticated_memory_context(
    request: Request,
    *,
    requested_owner_session_id: str | None = None,
    requested_source_session_id: str | None = None,
    requested_source_role: str | None = None,
) -> AuthenticatedMemoryContext:
    """Return the middleware-bound operator identity for memory mutations.

    The actor fields remain accepted in request models for wire compatibility,
    but caller input never supplies canonical memory authority or audit
    identity. The test-only middleware bypass supplies the same principal
    contract as a real authenticated session.
    """

    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    session_id = str(getattr(operator, "session_id", "") or "").strip()
    principal_type = getattr(getattr(principal, "principal_type", None), "value", None) or str(
        getattr(principal, "principal_type", "") or ""
    ).strip()
    principal_session_id = str(getattr(principal, "session_id", "") or "").strip()
    grants = {str(getattr(grant, "value", grant)) for grant in getattr(principal, "grants", ())}
    if (
        not isinstance(operator, AuthenticatedOperator)
        or principal is None
        or principal_type != PrincipalType.OPERATOR.value
        or not bool(getattr(principal, "authenticated", False))
        or bool(getattr(principal, "revoked", False))
        or not principal_id
        or not session_id
        or principal_session_id != session_id
        or AuthorityGrant.CAPABILITY_EXECUTE.value not in grants
    ):
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    requested_sessions = {
        str(candidate).strip()
        for candidate in (requested_owner_session_id, requested_source_session_id)
        if str(candidate or "").strip()
    }
    if requested_sessions and requested_sessions != {session_id}:
        raise HTTPException(
            status_code=403,
            detail={"code": "memory_owner_session_forbidden"},
        )
    source_role = str(requested_source_role or "operator").strip().lower()
    if source_role != "operator":
        raise HTTPException(
            status_code=403,
            detail={"code": "memory_source_role_forbidden"},
        )
    return AuthenticatedMemoryContext(
        actor=principal_id,
        session_id=session_id,
        source_role=source_role,
    )


def authenticated_memory_actor(request: Request) -> str:
    return authenticated_memory_context(request).actor


async def _require_memory_owner(memory_id: str, session_id: str) -> None:
    """Reject cross-session control before invoking a canonical mutation."""

    memory = await memory_repository.get_memory(memory_id)
    if memory is None:
        return
    bound_session = str(memory.source_session_id or "").strip()
    if bound_session and bound_session != session_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "memory_owner_session_forbidden"},
        )


def _live_control_acknowledgement(request: MemoryLiveControlActionRequest) -> bool:
    if str(request.action or "").strip().lower() == "rollback_memory":
        return request.acknowledge_rollback_boundary
    return request.acknowledged or request.acknowledge_rollback_boundary


@router.get("/memory/providers")
async def list_memory_providers():
    payload = list_memory_provider_inventory()
    reconciliation = await summarize_memory_reconciliation_state()
    payload["canonical_memory_reconciliation"] = reconciliation
    payload["guardian_memory_benchmark"] = await build_guardian_memory_benchmark_report(
        run_suite=False,
        reconciliation=reconciliation,
    )
    return payload


@router.get("/memory/operator-policy")
async def get_memory_operator_policy():
    return memory_operator_policy_payload()


@router.get("/memory/live-controls")
async def get_memory_live_controls(limit: int = 8, owner_session_id: str | None = None):
    return await get_memory_live_controls_snapshot(limit=limit, owner_session_id=owner_session_id)


@router.get("/memory/guardian-memory-live-control")
async def get_guardian_memory_live_control(limit: int = 8, owner_session_id: str | None = None):
    return await get_memory_live_controls_snapshot(limit=limit, owner_session_id=owner_session_id)


@router.post("/memory/live-controls/actions")
async def post_memory_live_control_action(
    http_request: Request,
    request: MemoryLiveControlActionRequest,
):
    try:
        return await apply_memory_live_control_action(
            action=request.action,
            acknowledged=_live_control_acknowledgement(request),
            actor=authenticated_memory_actor(http_request),
            reason=request.reason,
            owner_session_id=request.owner_session_id,
            memory_id=request.memory_id,
            provider_name=request.provider_name,
            outcome=request.outcome,
            privacy_boundary=request.privacy_boundary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/memory/guardian-memory-live-control/actions")
async def post_guardian_memory_live_control_action(
    http_request: Request,
    request: MemoryLiveControlActionRequest,
):
    return await post_memory_live_control_action(http_request, request)


@router.post("/memory/corrections")
async def create_memory_correction(http_request: Request, request: MemoryCorrectionRequest):
    try:
        context = authenticated_memory_context(
            http_request,
            requested_source_session_id=request.source_session_id,
            requested_source_role=request.source_role,
        )
        if request.corrects_memory_id:
            await _require_memory_owner(request.corrects_memory_id, context.session_id)
        return await correct_memory(
            content=request.content,
            kind=request.kind,
            summary=request.summary,
            corrects_memory_id=request.corrects_memory_id,
            source_session_id=context.session_id,
            actor=context.actor,
            reason=request.reason,
            confidence=request.confidence,
            importance=request.importance,
            privacy_boundary=request.privacy_boundary,
            metadata=request.metadata,
            authenticated_session_id=context.session_id,
            source_role=context.source_role,
        )
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "memory_authority_forbidden", "reason": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/memory/{memory_id}/pin")
async def pin_memory_item(memory_id: str, http_request: Request, request: MemoryPinRequest):
    try:
        context = authenticated_memory_context(http_request)
        await _require_memory_owner(memory_id, context.session_id)
        return await pin_memory(
            memory_id=memory_id,
            actor=context.actor,
            reason=request.reason,
            privacy_boundary=request.privacy_boundary,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/memory/{memory_id}/forget")
async def forget_memory_item(memory_id: str, http_request: Request, request: MemoryForgetRequest):
    try:
        context = authenticated_memory_context(http_request)
        await _require_memory_owner(memory_id, context.session_id)
        return await forget_memory(
            memory_id=memory_id,
            actor=context.actor,
            reason=request.reason,
            mode=request.mode,
            privacy_boundary=request.privacy_boundary,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/memory/{memory_id}/audit")
async def audit_memory_item(memory_id: str, http_request: Request, request: MemoryAuditRequest):
    try:
        context = authenticated_memory_context(http_request)
        await _require_memory_owner(memory_id, context.session_id)
        return await audit_memory(
            memory_id=memory_id,
            actor=context.actor,
            reason=request.reason,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/memory/audit")
async def get_memory_audit(memory_id: str | None = None, limit: int = 20):
    return await list_memory_audit_receipts(memory_id=memory_id, limit=limit)


def _recovery_request_context(http_request: Request, request: MemoryRecoveryRequest) -> AuthenticatedMemoryContext:
    return authenticated_memory_context(
        http_request,
        requested_owner_session_id=request.owner_session_id,
        requested_source_session_id=request.source_session_id,
        requested_source_role=request.source_role,
    )


@router.post("/memory/recovery/export")
async def export_memory_recovery_route(http_request: Request, request: MemoryRecoveryRequest):
    try:
        context = _recovery_request_context(http_request, request)
        return await export_memory_recovery_control(
            actor=context.actor,
            owner_session_id=context.session_id,
            authenticated_session_id=context.session_id,
            source_role=context.source_role,
            limit=request.limit,
        )
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "memory_authority_forbidden", "reason": str(exc)}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "memory_recovery_unavailable", "reason": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/memory/recovery/rebuild")
async def rebuild_memory_recovery_route(http_request: Request, request: MemoryRecoveryRequest):
    try:
        context = _recovery_request_context(http_request, request)
        return await rebuild_memory_recovery_control(
            actor=context.actor,
            owner_session_id=context.session_id,
            authenticated_session_id=context.session_id,
            source_role=context.source_role,
            limit=request.limit,
        )
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "memory_authority_forbidden", "reason": str(exc)}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "memory_recovery_unavailable", "reason": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/memory/recovery/restore")
async def restore_memory_recovery_route(http_request: Request, request: MemoryRestoreRequest):
    try:
        context = authenticated_memory_context(
            http_request,
            requested_owner_session_id=request.owner_session_id,
            requested_source_session_id=request.source_session_id,
            requested_source_role=request.source_role,
        )
        return await restore_memory_recovery_control(
            archive=request.archive,
            actor=context.actor,
            owner_session_id=context.session_id,
            authenticated_session_id=context.session_id,
            source_role=context.source_role,
        )
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "memory_authority_forbidden", "reason": str(exc)}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "memory_recovery_unavailable", "reason": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/memory/recovery/status")
async def get_memory_recovery_status(http_request: Request, owner_session_id: str | None = None):
    try:
        context = authenticated_memory_context(
            http_request,
            requested_owner_session_id=owner_session_id,
        )
        return await memory_recovery_status(
            owner_session_id=context.session_id,
            authenticated_session_id=context.session_id,
            actor=context.actor,
            source_role=context.source_role,
        )
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "memory_authority_forbidden", "reason": str(exc)}) from exc
