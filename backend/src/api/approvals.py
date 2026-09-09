from fastapi import APIRouter, HTTPException, Query, Request
import json

from src.approval.repository import approval_repository
from src.approval.surfaces import approval_surface_metadata
from src.agent.session import session_manager
from src.audit.repository import audit_repository
from src.tools.policy import get_current_tool_policy_mode

router = APIRouter()


def _require_approval_operator(request: Request):
    """Use the same authenticated operator gate as capability execution."""

    from src.api.capabilities import _require_authenticated_capability_operator

    return _require_authenticated_capability_operator(request)


async def _recheck_approval_operator(request: Request, expected_operator):
    """Revalidate the browser session after an approval row was read.

    Middleware authority is a snapshot.  A decision can await database work
    between that snapshot and the terminal transition, so a real cookie is
    authenticated again immediately before the repository's owner predicate.
    Test-bypass requests have no cookie and still pass through the normal
    in-memory operator gate.
    """
    from config.settings import settings
    from src.auth.service import AuthFailure, authenticate_token

    auth_cookie = request.cookies.get(settings.operator_auth_cookie_name)
    if not auth_cookie:
        return _require_approval_operator(request)
    try:
        refreshed = await authenticate_token(auth_cookie, touch=False)
    except AuthFailure as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "authentication_required"},
        ) from exc
    if refreshed.session_id != expected_operator.session_id:
        raise HTTPException(
            status_code=401,
            detail={"code": "authentication_required"},
        )
    request.state.operator = refreshed
    return _require_approval_operator(request)


def _approval_details(request) -> dict:
    if not request.details_json:
        return {}
    try:
        details = json.loads(request.details_json)
    except (TypeError, ValueError):
        return {}
    return details if isinstance(details, dict) else {}


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
    owner_principal_id = str(details.get("approval_owner_principal_id") or "").strip()
    current_principal_id = str(
        getattr(getattr(operator, "principal", None), "principal_id", "") or ""
    ).strip()
    conversation_id = str(details.get("approval_conversation_id") or "").strip()

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


def _raise_resolution_conflict(resolution) -> None:
    approval = resolution.request
    if resolution.reason == "owner_mismatch":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "approval_owner_mismatch",
                "id": approval.id if approval is not None else None,
                "status": approval.status if approval is not None else None,
                "transitioned": False,
            },
        )
    status = approval.status if approval is not None else None
    code = {
        "already_terminal": "approval_already_resolved",
        "expiry_missing": "approval_expiry_missing",
        "expired": "approval_expired",
    }.get(resolution.reason, "approval_resolution_conflict")
    raise HTTPException(
        status_code=409,
        detail={
            "code": code,
            "id": approval.id if approval is not None else None,
            "status": status,
            "transitioned": False,
            "reason": resolution.reason,
        },
    )


@router.get("/approvals/pending")
async def list_pending_approvals(
    request: Request,
    session_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List only pending approvals owned by the authenticated operator session."""
    operator = _require_approval_operator(request)
    approvals = await approval_repository.list_pending(
        session_id=session_id,
        limit=limit,
        owner_operator_session_id=operator.session_id,
    )
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions()
        if isinstance(session, dict) and session.get("id")
    }
    items = []
    for approval in approvals:
        approval_metadata = approval_surface_metadata(approval)
        items.append(
            {
                **approval,
                "thread_id": approval.get("session_id"),
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
                "approval_context": approval_metadata["approval_context"],
            }
        )
    return items


@router.post("/approvals/{approval_id}/approve")
async def approve_request(approval_id: str, request: Request):
    operator = _require_approval_operator(request)
    # Resolve only after authentication and owner binding, so a cross-session
    # caller cannot consume a pending approval as a side effect of probing it.
    pending = await approval_repository.get(approval_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    operator = await _recheck_approval_operator(request, operator)
    details = _require_approval_owner(request, pending, operator)
    resolution = await approval_repository.resolve_with_metadata(
        approval_id,
        "approved",
        owner_operator_session_id=operator.session_id,
        owner_principal_id=getattr(operator.principal, "principal_id", None),
    )
    resolved = resolution.request
    if resolved is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if not resolution.transitioned:
        _raise_resolution_conflict(resolution)

    await audit_repository.log_event(
        session_id=resolved.session_id,
        actor="user",
        event_type="approval_approved",
        tool_name=resolved.tool_name,
        risk_level=resolved.risk_level,
        policy_mode=get_current_tool_policy_mode(),
        summary=f"Approved high-risk action for {resolved.tool_name}",
    )

    details = details if isinstance(details, dict) else _approval_details(resolved)
    resume_message = details.get("resume_message")

    response = {
        "status": resolved.status,
        "id": resolved.id,
        "expires_at": resolved.expires_at.isoformat() if resolved.expires_at else None,
        "transitioned": True,
    }
    if resolved.session_id and resume_message:
        response["session_id"] = resolved.session_id
        response["resume_message"] = resume_message
    return response


@router.post("/approvals/{approval_id}/deny")
async def deny_request(approval_id: str, request: Request):
    operator = _require_approval_operator(request)
    pending = await approval_repository.get(approval_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    operator = await _recheck_approval_operator(request, operator)
    _require_approval_owner(request, pending, operator)
    resolution = await approval_repository.resolve_with_metadata(
        approval_id,
        "denied",
        owner_operator_session_id=operator.session_id,
        owner_principal_id=getattr(operator.principal, "principal_id", None),
    )
    resolved = resolution.request
    if resolved is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if not resolution.transitioned:
        _raise_resolution_conflict(resolution)

    await audit_repository.log_event(
        session_id=resolved.session_id,
        actor="user",
        event_type="approval_denied",
        tool_name=resolved.tool_name,
        risk_level=resolved.risk_level,
        policy_mode=get_current_tool_policy_mode(),
        summary=f"Denied high-risk action for {resolved.tool_name}",
    )
    return {
        "status": resolved.status,
        "id": resolved.id,
        "expires_at": resolved.expires_at.isoformat() if resolved.expires_at else None,
        "transitioned": True,
    }
