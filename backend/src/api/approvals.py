from fastapi import APIRouter, HTTPException, Query
import json

from src.approval.repository import approval_repository
from src.approval.surfaces import approval_surface_metadata
from src.agent.session import session_manager
from src.audit.repository import audit_repository
from src.tools.policy import get_current_tool_policy_mode

router = APIRouter()


@router.get("/approvals/pending")
async def list_pending_approvals(
    session_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List pending approval requests."""
    approvals = await approval_repository.list_pending(session_id=session_id, limit=limit)
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
async def approve_request(approval_id: str):
    request = await approval_repository.resolve(approval_id, "approved")
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")

    await audit_repository.log_event(
        session_id=request.session_id,
        actor="user",
        event_type="approval_approved",
        tool_name=request.tool_name,
        risk_level=request.risk_level,
        policy_mode=get_current_tool_policy_mode(),
        summary=f"Approved high-risk action for {request.tool_name}",
    )

    details = json.loads(request.details_json) if request.details_json else {}
    resume_message = details.get("resume_message")

    response = {"status": request.status, "id": request.id}
    if request.session_id and resume_message:
        response["session_id"] = request.session_id
        response["resume_message"] = resume_message
    return response


@router.post("/approvals/{approval_id}/deny")
async def deny_request(approval_id: str):
    request = await approval_repository.resolve(approval_id, "denied")
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")

    await audit_repository.log_event(
        session_id=request.session_id,
        actor="user",
        event_type="approval_denied",
        tool_name=request.tool_name,
        risk_level=request.risk_level,
        policy_mode=get_current_tool_policy_mode(),
        summary=f"Denied high-risk action for {request.tool_name}",
    )
    return {"status": request.status, "id": request.id}
