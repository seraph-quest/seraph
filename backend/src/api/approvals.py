from fastapi import APIRouter, HTTPException, Query

from src.approval.repository import approval_repository
from src.audit.repository import audit_repository
from src.tools.policy import get_current_tool_policy_mode

router = APIRouter()


@router.get("/approvals/pending")
async def list_pending_approvals(
    session_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List pending approval requests."""
    return await approval_repository.list_pending(session_id=session_id, limit=limit)


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
    return {"status": request.status, "id": request.id}


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
