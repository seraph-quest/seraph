import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.agent.session import (
    SessionOwnerMismatchError,
    SessionNotFoundError,
    session_manager,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class SessionUpdate(BaseModel):
    title: str = Field(..., min_length=1)


def _require_operator_owner(request: Request) -> tuple[object, str]:
    """Return the authenticated operator and canonical principal binding."""
    operator = getattr(request.state, "operator", None)
    owner_principal_id = getattr(getattr(operator, "principal", None), "principal_id", None)
    if not operator or not owner_principal_id:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    return operator, str(owner_principal_id)


@router.get("/sessions")
async def list_sessions(request: Request):
    """List sessions owned by the authenticated principal."""
    _, owner_principal_id = _require_operator_owner(request)
    return await session_manager.list_sessions(owner_principal_id=owner_principal_id)


@router.get("/sessions/search")
async def search_sessions(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(default=5, ge=1, le=20),
    exclude_session_id: str | None = Query(default=None),
):
    """Search prior sessions by title and message content."""
    normalized_query = q.strip()
    if not normalized_query:
        raise HTTPException(status_code=422, detail="Search query must not be empty.")
    _, owner_principal_id = _require_operator_owner(request)
    return await session_manager.search_sessions(
        normalized_query,
        limit=limit,
        exclude_session_id=exclude_session_id,
        owner_principal_id=owner_principal_id,
    )


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Get paginated message history for a session."""
    _, owner_principal_id = _require_operator_owner(request)
    try:
        await session_manager.get_for_ingress(
            session_id,
            owner_principal_id=owner_principal_id,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except SessionOwnerMismatchError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "chat_session_owner_forbidden", "session_id": exc.session_id},
        ) from exc
    return await session_manager.get_messages(session_id, limit=limit, offset=offset)


@router.get("/sessions/{session_id}/todos")
async def get_session_todos(session_id: str, request: Request):
    """Get the persisted todo list for a session."""
    _, owner_principal_id = _require_operator_owner(request)
    try:
        await session_manager.get_for_ingress(
            session_id,
            owner_principal_id=owner_principal_id,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except SessionOwnerMismatchError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "session_owner_forbidden", "session_id": exc.session_id},
        ) from exc
    return await session_manager.get_todos(session_id)


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: SessionUpdate, request: Request):
    """Update session title."""
    _, owner_principal_id = _require_operator_owner(request)
    try:
        await session_manager.get_for_ingress(
            session_id,
            owner_principal_id=owner_principal_id,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except SessionOwnerMismatchError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "session_owner_forbidden", "session_id": exc.session_id},
        ) from exc
    success = await session_manager.update_title(
        session_id,
        body.title,
        owner_principal_id=owner_principal_id,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "ok"}


@router.post("/sessions/{session_id}/generate-title")
async def generate_session_title(session_id: str, request: Request):
    """Generate a short title for a session using LLM."""
    _, owner_principal_id = _require_operator_owner(request)
    try:
        session = await session_manager.get_for_ingress(
            session_id,
            owner_principal_id=owner_principal_id,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except SessionOwnerMismatchError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "session_owner_forbidden", "session_id": exc.session_id},
        ) from exc
    title = await session_manager.generate_title(
        session_id,
        owner_principal_id=owner_principal_id,
    )
    return {"title": title or session.title}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    """Delete a session and its messages."""
    _, owner_principal_id = _require_operator_owner(request)
    try:
        await session_manager.get_for_ingress(
            session_id,
            owner_principal_id=owner_principal_id,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except SessionOwnerMismatchError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "session_owner_forbidden", "session_id": exc.session_id},
        ) from exc
    success = await session_manager.delete(
        session_id,
        owner_principal_id=owner_principal_id,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "ok"}
