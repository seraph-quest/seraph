from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.auth.service import AuthenticatedOperator
from src.memory.benchmark import build_guardian_memory_benchmark_report
from src.memory.control import (
    _apply_provider_quarantine_overlay,
    apply_memory_live_control_action,
    audit_memory,
    correct_memory,
    export_memory_recovery as export_memory_recovery_control,
    forget_memory,
    get_memory_live_controls_snapshot,
    list_memory_audit_receipts,
    list_memory_proposals,
    list_work_board_decision_receipts,
    create_memory_proposal,
    apply_memory_proposal_action,
    m5_registered_capability_contracts,
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


class MemoryTaskProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=512)
    expected_task_revision: int = Field(ge=1)
    attempt_id: str = Field(min_length=1, max_length=512)


class MemoryTaskProposalActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=32)
    expected_revision: int = Field(ge=1)
    expected_preview_text_digest: str | None = Field(default=None, min_length=64, max_length=64)
    expected_task_revision: int = Field(ge=1)
    expected_goal_revision: int = Field(ge=1)
    edited_text: str | None = Field(default=None, max_length=2_000)
    decision_effect: str | None = Field(default=None, max_length=64)
    preferred_capability_id: str | None = Field(default=None, min_length=1, max_length=160)
    corrects_memory_id: str | None = Field(default=None, min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=500)


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
    if not bound_session:
        raise HTTPException(
            status_code=403,
            detail={"code": "memory_owner_session_unbound"},
        )
    if bound_session != session_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "memory_owner_session_forbidden"},
        )


def _live_control_acknowledgement(request: MemoryLiveControlActionRequest) -> bool:
    if str(request.action or "").strip().lower() == "rollback_memory":
        return request.acknowledge_rollback_boundary
    return request.acknowledged or request.acknowledge_rollback_boundary


async def list_memory_providers(*, owner_session_id: str | None = None):
    """Build the provider inventory with an optional reconciliation scope.

    Internal maintenance and benchmark callers may omit the owner scope and
    retain the existing global diagnostic summary.  The HTTP route below
    always supplies the authenticated operator session, so its reconciliation
    payload is owner-filtered and content-free.
    """

    payload = list_memory_provider_inventory()
    if owner_session_id:
        payload = _apply_provider_quarantine_overlay(
            payload,
            owner_session_id=owner_session_id,
        )
    reconciliation = await summarize_memory_reconciliation_state(
        owner_session_id=owner_session_id,
        content_free=owner_session_id is not None,
    )
    payload["canonical_memory_reconciliation"] = reconciliation
    payload["guardian_memory_benchmark"] = await build_guardian_memory_benchmark_report(
        run_suite=False,
        reconciliation=reconciliation,
    )
    return payload


@router.get("/memory/providers")
async def list_memory_providers_route(http_request: Request):
    context = authenticated_memory_context(http_request)
    return await list_memory_providers(owner_session_id=context.session_id)


@router.get("/memory/operator-policy")
async def get_memory_operator_policy():
    return memory_operator_policy_payload()


@router.get("/memory/live-controls")
async def get_memory_live_controls(
    http_request: Request,
    limit: int = 8,
    owner_session_id: str | None = None,
):
    context = authenticated_memory_context(
        http_request,
        requested_owner_session_id=owner_session_id,
    )
    return await get_memory_live_controls_snapshot(
        limit=limit,
        owner_session_id=context.session_id,
    )


@router.get("/memory/guardian-memory-live-control")
async def get_guardian_memory_live_control(
    http_request: Request,
    limit: int = 8,
    owner_session_id: str | None = None,
):
    context = authenticated_memory_context(
        http_request,
        requested_owner_session_id=owner_session_id,
    )
    return await get_memory_live_controls_snapshot(
        limit=limit,
        owner_session_id=context.session_id,
    )


@router.post("/memory/live-controls/actions")
async def post_memory_live_control_action(
    http_request: Request,
    request: MemoryLiveControlActionRequest,
):
    try:
        context = authenticated_memory_context(
            http_request,
            requested_owner_session_id=request.owner_session_id,
        )
        if request.memory_id:
            await _require_memory_owner(request.memory_id, context.session_id)
        async with _bound_recovery_runtime(http_request, context):
            return await apply_memory_live_control_action(
                action=request.action,
                acknowledged=_live_control_acknowledgement(request),
                actor=context.actor,
                reason=request.reason,
                owner_session_id=context.session_id,
                memory_id=request.memory_id,
                provider_name=request.provider_name,
                outcome=request.outcome,
                privacy_boundary=request.privacy_boundary,
                authenticated_session_id=context.session_id,
                source_role=context.source_role,
            )
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "memory_authority_forbidden", "reason": str(exc)},
        ) from exc
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


@router.post("/memory/task-proposals", status_code=201)
async def create_memory_task_proposal(
    http_request: Request,
    request: MemoryTaskProposalRequest,
):
    """Create one owner/session-fenced proposal from a verified Done card."""

    context = authenticated_memory_context(http_request)
    try:
        return await create_memory_proposal(
            owner_principal_id=context.actor,
            owner_session_id=context.session_id,
            task_id=request.task_id,
            expected_task_revision=request.expected_task_revision,
            attempt_id=request.attempt_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "memory_owner_session_forbidden"}) from exc
    except ValueError as exc:
        code = str(exc)
        status = 409 if code in {
            "source_not_verified",
            "stale_task_revision",
            "proposal_source_binding_conflict",
        } else 400
        raise HTTPException(status_code=status, detail={"code": code}) from exc


@router.get("/memory/task-proposals")
async def get_memory_task_proposals(
    http_request: Request,
    task_id: str | None = None,
):
    context = authenticated_memory_context(http_request)
    return {
        "proposals": await list_memory_proposals(
            owner_principal_id=context.actor,
            owner_session_id=context.session_id,
            task_id=task_id,
        )
    }


@router.get("/memory/task-decision-capabilities")
async def get_memory_task_decision_capabilities(http_request: Request):
    """List registered typed inputs available for proposal-only comparison."""

    authenticated_memory_context(http_request)
    return {"capabilities": m5_registered_capability_contracts()}


@router.post("/memory/task-proposals/{proposal_id}/actions")
async def act_on_memory_task_proposal(
    http_request: Request,
    proposal_id: str,
    request: MemoryTaskProposalActionRequest,
):
    context = authenticated_memory_context(http_request)
    try:
        return await apply_memory_proposal_action(
            owner_principal_id=context.actor,
            owner_session_id=context.session_id,
            proposal_id=proposal_id,
            action=request.action,
            expected_revision=request.expected_revision,
            expected_preview_text_digest=request.expected_preview_text_digest,
            expected_task_revision=request.expected_task_revision,
            expected_goal_revision=request.expected_goal_revision,
            edited_text=request.edited_text,
            decision_effect=request.decision_effect,
            preferred_capability_id=request.preferred_capability_id,
            corrects_memory_id=request.corrects_memory_id,
            reason=request.reason,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": str(exc)}) from exc
    except ValueError as exc:
        code = str(exc)
        status = 422 if code in {
            "unknown_proposal_action",
            "edited_text_requires_edit_accept",
            "preview_digest_required",
            "memory_kind_invalid",
            "decision_effect_invalid",
            "preferred_capability_unregistered",
        } else 409
        raise HTTPException(status_code=status, detail={"code": code}) from exc


@router.get("/memory/task-decisions")
async def get_memory_task_decisions(
    http_request: Request,
    task_id: str | None = None,
):
    """Return content-free same-card decision mechanics for the cockpit."""

    context = authenticated_memory_context(http_request)
    return {
        "receipts": await list_work_board_decision_receipts(
            owner_principal_id=context.actor,
            owner_session_id=context.session_id,
            task_id=task_id,
        )
    }


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
async def get_memory_audit(
    http_request: Request,
    memory_id: str | None = None,
    limit: int = 20,
):
    context = authenticated_memory_context(http_request)
    if memory_id:
        await _require_memory_owner(memory_id, context.session_id)
    return await list_memory_audit_receipts(
        memory_id=memory_id,
        limit=limit,
        owner_session_id=context.session_id,
    )


def _recovery_request_context(
    http_request: Request,
    request: MemoryRecoveryRequest | MemoryRestoreRequest,
) -> AuthenticatedMemoryContext:
    return authenticated_memory_context(
        http_request,
        requested_owner_session_id=request.owner_session_id,
        requested_source_session_id=request.source_session_id,
        requested_source_role=request.source_role,
    )


@asynccontextmanager
async def _bound_recovery_runtime(
    http_request: Request,
    context: AuthenticatedMemoryContext,
):
    """Bind middleware-verified operator authority around the store call."""

    principal = http_request.state.operator.principal
    tokens = set_runtime_context(
        context.session_id,
        "off",
        trust_principal=principal,
    )
    try:
        yield
    finally:
        reset_runtime_context(tokens)


@router.post("/memory/recovery/export")
async def export_memory_recovery_route(http_request: Request, request: MemoryRecoveryRequest):
    try:
        context = _recovery_request_context(http_request, request)
        async with _bound_recovery_runtime(http_request, context):
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
        async with _bound_recovery_runtime(http_request, context):
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
        context = _recovery_request_context(http_request, request)
        async with _bound_recovery_runtime(http_request, context):
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
        async with _bound_recovery_runtime(http_request, context):
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
