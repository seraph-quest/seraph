from fastapi import APIRouter, Query

from src.audit.repository import audit_repository

router = APIRouter()


@router.get("/audit/events")
async def list_audit_events(
    limit: int = Query(default=20, ge=1, le=100),
    session_id: str | None = Query(default=None),
):
    """Return recent structured audit events, newest first."""
    return await audit_repository.list_events(limit=limit, session_id=session_id)
