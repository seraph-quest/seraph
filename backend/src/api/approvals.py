from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
import json

from src.approval.repository import approval_repository
from src.approval.metadata import approval_wire_metadata
from src.approval.surfaces import approval_surface_metadata
from src.agent.session import session_manager
from src.audit.repository import audit_repository
from src.conversation.identity import validate_attachment_refs
from src.tools.policy import get_current_tool_policy_mode

router = APIRouter()


def _require_approval_operator(request: Request):
    """Use the same authenticated operator gate as capability execution."""

    from src.api.capabilities import _require_authenticated_capability_operator

    return _require_authenticated_capability_operator(request)


def _approval_details(request) -> dict:
    if not request.details_json:
        return {}
    try:
        details = json.loads(request.details_json)
    except (TypeError, ValueError):
        return {}
    return details if isinstance(details, dict) else {}


_SAFE_PENDING_FIELDS = frozenset(
    {
        "id",
        "session_id",
        "tool_name",
        "risk_level",
        "status",
        "fingerprint",
        "summary",
        "conversation_id",
        "thread_id",
        "owner_principal_id",
        "operator_session_id",
        "device_id",
        "channel",
        "transport",
        "correlation_id",
        "causation_id",
        "attachment_refs",
        "challenge",
        "action",
        "expires_at",
        "created_at",
        "resume_message",
        "extension_id",
        "extension_display_name",
        "extension_action",
        "package_path",
        "permissions",
    }
)


def _safe_approval_context(value) -> dict | None:
    """Keep only bounded display context; never return tool arguments."""

    if not isinstance(value, dict):
        return None
    allowed = {
        "risk_level",
        "execution_boundaries",
        "authenticated_source",
        "workflow_name",
        "requires_lifecycle_approval",
        "executor_kind",
        "executor_profile",
        "executor_posture_digest",
        "required_permissions",
        "local_host_execution_required",
        "preparation_ready",
        "execution_ready",
        "operator_visible",
    }
    result = {}
    typed = approval_wire_metadata(value)
    for key in allowed:
        item = value.get(key)
        if key in {
            "executor_kind",
            "executor_profile",
            "executor_posture_digest",
            "required_permissions",
            "local_host_execution_required",
            "preparation_ready",
            "execution_ready",
            "operator_visible",
        }:
            if key in typed:
                result[key] = typed[key]
            continue
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, str) and len(item) <= 256:
            result[key] = item
        elif isinstance(item, list):
            strings = [entry for entry in item[:16] if isinstance(entry, str) and len(entry) <= 128]
            if len(strings) == len(item[:16]):
                result[key] = strings
    return result or None


def _safe_pending_approval(approval: dict, approval_metadata: dict) -> dict:
    """Project a pending row without spreading private details JSON."""

    safe = {key: approval.get(key) for key in _SAFE_PENDING_FIELDS if key in approval}
    safe.update(
        {
            "lifecycle_boundaries": approval_metadata["lifecycle_boundaries"],
            "requires_lifecycle_approval": approval_metadata["requires_lifecycle_approval"],
            "approval_scope": approval_metadata["approval_scope"],
            "approval_context": _safe_approval_context(approval_metadata["approval_context"]),
        }
    )
    safe.update(approval_wire_metadata(approval))
    return safe


def _approval_attachment_refs(request) -> list[dict]:
    """Read durable attachment metadata without exposing malformed legacy data."""
    try:
        parsed = json.loads(getattr(request, "attachment_refs_json", "[]") or "[]")
        owner_principal_id = str(
            getattr(request, "owner_principal_id", None) or ""
        ).strip() or None
        return validate_attachment_refs(parsed, owner_principal_id=owner_principal_id)
    except Exception:
        return []


def _require_approval_owner(request: Request, approval, operator) -> dict:
    """Bind a decision to its authenticated owner and conversation context.

    ``approval_owner_operator_session_id`` is the browser authentication
    session that created the request.  ``approval_conversation_id`` is only
    the execution context and is checked separately against the repository
    session id.  The old ``approval_owner_session_id`` field was ambiguous;
    it is accepted only for rows whose conversation id is exactly the same
    value, otherwise the decision fails closed.
    """

    details = _approval_details(approval)
    owner_operator_session_id = str(
        details.get("approval_owner_operator_session_id")
        or details.get("approval_owner_auth_session_id")
        or ""
    ).strip()
    legacy_owner_session_id = str(details.get("approval_owner_session_id") or "").strip()
    owner_principal_id = str(
        getattr(approval, "owner_principal_id", None)
        or details.get("approval_owner_principal_id")
        or ""
    ).strip()
    current_principal_id = str(
        getattr(getattr(operator, "principal", None), "principal_id", "") or ""
    ).strip()
    conversation_id = str(
        getattr(approval, "conversation_id", None)
        or details.get("approval_conversation_id")
        or approval.session_id
        or ""
    ).strip()
    model_operator_session_id = str(getattr(approval, "operator_session_id", None) or "").strip()
    if model_operator_session_id and not owner_operator_session_id:
        owner_operator_session_id = model_operator_session_id

    if conversation_id and conversation_id != str(approval.session_id or ""):
        allowed = False
    elif owner_operator_session_id:
        allowed = owner_operator_session_id == operator.session_id
    elif legacy_owner_session_id:
        # Migration rule for pre-contract rows: the legacy value can only be
        # treated as an auth session when it was also the repository session.
        allowed = (
            legacy_owner_session_id == operator.session_id
            and approval.session_id == operator.session_id
        )
    elif owner_principal_id:
        # Principal-only rows cannot prove which revocable browser session
        # authorized the request, so do not widen their authority.
        allowed = False
    else:
        # Older auth-session-only rows retain their exact repository session
        # binding.  Conversation-bound rows without migration metadata fail
        # closed because their owner cannot be established safely.
        allowed = bool(approval.session_id) and approval.session_id == operator.session_id

    if owner_principal_id and owner_principal_id != current_principal_id:
        allowed = False
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "approval_owner_mismatch"},
        )
    return details


@router.get("/approvals/pending")
async def list_pending_approvals(
    request: Request,
    session_id: str | None = Query(default=None),
    approval_id: str | None = Query(default=None, min_length=1, max_length=256),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List pending approval requests."""
    operator = _require_approval_operator(request)
    approvals = await approval_repository.list_pending(
        session_id=session_id,
        approval_id=approval_id,
        limit=limit,
    )
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions(
            owner_principal_id=operator.principal.principal_id,
        )
        if isinstance(session, dict) and session.get("id")
    }
    owned_session_ids = set(session_titles)
    items = []
    for approval in approvals:
        # ``list_pending`` returns dictionaries for compatibility.  Explicit
        # owner fields are sufficient to filter here; legacy ownerless rows
        # remain visible only to the existing authenticated test/operator
        # session binding handled by the route's decision endpoint.
        owner_principal_id = str(approval.get("owner_principal_id") or "").strip()
        owner_operator_session_id = str(approval.get("operator_session_id") or "").strip()
        current_principal_id = str(
            getattr(getattr(operator, "principal", None), "principal_id", "") or ""
        ).strip()
        if owner_principal_id and owner_principal_id != current_principal_id:
            continue
        if owner_operator_session_id and owner_operator_session_id != operator.session_id:
            continue
        conversation_id = str(
            approval.get("conversation_id") or approval.get("session_id") or ""
        ).strip()
        if conversation_id and approval.get("session_id") and conversation_id != str(approval["session_id"]):
            continue
        # Ownerless legacy rows remain visible only when their canonical
        # session is already owned by this authenticated principal. A bound
        # row with no resolvable owner fails closed; truly ambient approvals
        # have no conversation/session binding and remain visible.
        if conversation_id and not owner_principal_id and conversation_id not in owned_session_ids:
            continue
        if conversation_id and owner_principal_id and conversation_id not in owned_session_ids:
            continue
        approval_metadata = approval_surface_metadata(approval)
        item = _safe_pending_approval(approval, approval_metadata)
        item.update(
            {
                "thread_id": approval.get("session_id"),
                "conversation_id": approval.get("conversation_id") or approval.get("session_id"),
                "owner_principal_id": approval.get("owner_principal_id"),
                "operator_session_id": approval.get("operator_session_id"),
                "device_id": approval.get("device_id"),
                "channel": approval.get("channel"),
                "transport": approval.get("transport"),
                "correlation_id": approval.get("correlation_id"),
                "causation_id": approval.get("causation_id"),
                "attachment_refs": approval.get("attachment_refs") or [],
                "thread_label": (
                    session_titles.get(str(approval["session_id"]))
                    if approval.get("session_id")
                    else None
                ),
                "extension_id": approval.get("extension_id"),
                "extension_display_name": approval.get("extension_display_name"),
                "extension_action": approval.get("action"),
                "package_path": approval.get("package_path"),
                "lifecycle_boundaries": approval_metadata["lifecycle_boundaries"],
                "permissions": approval.get("permissions"),
                "requires_lifecycle_approval": approval_metadata["requires_lifecycle_approval"],
                "approval_scope": approval_metadata["approval_scope"],
            }
        )
        item["approval_context"] = _safe_approval_context(approval_metadata["approval_context"])
        items.append(item)
    return items


@router.post("/approvals/{approval_id}/approve")
async def approve_request(approval_id: str, request: Request):
    operator = _require_approval_operator(request)
    # Resolve only after authentication and owner binding, so a cross-session
    # caller cannot consume a pending approval as a side effect of probing it.
    pending = await approval_repository.get(approval_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    details = _require_approval_owner(request, pending, operator)
    from src.approval.repository import approval_decision_digest
    try:
        request = await approval_repository.resolve_exact(approval_id, "approved",
            expected_digest=approval_decision_digest(pending),
            owner_principal_id=operator.principal.principal_id, operator_session_id=operator.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "approval_binding_changed"}) from exc
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if request.status != "approved":
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_expired", "status": request.status},
        )

    await audit_repository.log_event(
        session_id=request.session_id,
        actor="user",
        event_type="approval_approved",
        tool_name=request.tool_name,
        risk_level=request.risk_level,
        policy_mode=get_current_tool_policy_mode(),
        summary=f"Approved high-risk action for {request.tool_name}",
    )

    details = details if isinstance(details, dict) else _approval_details(request)
    resume_message = details.get("resume_message")

    response = {
        "status": request.status,
        "id": request.id,
        "approval_id": request.id,
        "conversation_id": getattr(request, "conversation_id", None) or request.session_id,
        "thread_id": getattr(request, "thread_id", None) or request.session_id,
        "owner_principal_id": getattr(request, "owner_principal_id", None),
        "operator_session_id": getattr(request, "operator_session_id", None),
        "device_id": getattr(request, "device_id", None),
        "channel": getattr(request, "channel", None),
        "transport": getattr(request, "transport", None),
        "correlation_id": getattr(request, "correlation_id", None),
        "causation_id": getattr(request, "causation_id", None),
        "attachment_refs": _approval_attachment_refs(request),
    }
    if request.session_id and resume_message:
        response["session_id"] = request.session_id
        response["resume_message"] = resume_message
    return response


@router.post("/approvals/{approval_id}/deny")
async def deny_request(approval_id: str, request: Request):
    operator = _require_approval_operator(request)
    pending = await approval_repository.get(approval_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    _require_approval_owner(request, pending, operator)
    from src.approval.repository import approval_decision_digest
    try:
        request = await approval_repository.resolve_exact(approval_id, "denied",
            expected_digest=approval_decision_digest(pending),
            owner_principal_id=operator.principal.principal_id, operator_session_id=operator.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "approval_binding_changed"}) from exc
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if request.status != "denied":
        raise HTTPException(
            status_code=409,
            detail={"code": "approval_expired", "status": request.status},
        )

    await audit_repository.log_event(
        session_id=request.session_id,
        actor="user",
        event_type="approval_denied",
        tool_name=request.tool_name,
        risk_level=request.risk_level,
        policy_mode=get_current_tool_policy_mode(),
        summary=f"Denied high-risk action for {request.tool_name}",
    )
    return {
        "status": request.status,
        "id": request.id,
        "approval_id": request.id,
        "conversation_id": getattr(request, "conversation_id", None) or request.session_id,
        "thread_id": getattr(request, "thread_id", None) or request.session_id,
        "owner_principal_id": getattr(request, "owner_principal_id", None),
        "operator_session_id": getattr(request, "operator_session_id", None),
        "device_id": getattr(request, "device_id", None),
        "channel": getattr(request, "channel", None),
        "transport": getattr(request, "transport", None),
        "correlation_id": getattr(request, "correlation_id", None),
        "causation_id": getattr(request, "causation_id", None),
        "attachment_refs": _approval_attachment_refs(request),
    }


class ApprovalRevokeRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)

@router.post("/approvals/{approval_id}/revoke")
async def revoke_unconsumed_approval(approval_id: str, body: ApprovalRevokeRequest, request: Request):
    operator=_require_approval_operator(request)
    row=await approval_repository.get(approval_id)
    if row is None:
        raise HTTPException(404,detail={"code":"approval_not_found"})
    _require_approval_owner(request,row,operator)
    try:
        outcome=await approval_repository.revoke_unconsumed(approval_id,expected_revision=body.expected_revision,
            owner_principal_id=operator.principal.principal_id,operator_session_id=operator.session_id)
    except LookupError as exc:
        raise HTTPException(404,detail={"code":"approval_not_found"}) from exc
    except ValueError as exc:
        raise HTTPException(409,detail={"code":"approval_revision_stale"}) from exc
    return {"approval_id":approval_id,"outcome":outcome,"effect_revocation":"not_confirmed","no_learning":True}
