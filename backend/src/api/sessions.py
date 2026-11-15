import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel, Field

from src.agent.session import (
    SessionOwnerMismatchError,
    SessionNotFoundError,
    session_manager,
)
from src.conversation.task_context import ContinueTaskRequest
from src.db.engine import get_session
from src.work_board.repository import BoardError
from src.auth.service import AuthFailure

logger = logging.getLogger(__name__)

router = APIRouter()


def _continuity_service(request):
    service = getattr(request.app.state, "task_continuity", None)
    if service is None:
        raise HTTPException(status_code=503, detail={"code": "task_continuity_unavailable"})
    return service


@router.get("/sessions/task-context/{task_id}")
async def read_task_context(task_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    operator, _ = _require_operator_owner(request)
    try:
        async with get_session() as db:
            return await _continuity_service(request).packet(db, operator, task_id)
    except BoardError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    except AuthFailure as exc:
        raise HTTPException(status_code=401, detail={"code": exc.code}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail={"code": "task_context_storage_unavailable"}) from exc


@router.post("/sessions/continue-task")
async def continue_task(body: ContinueTaskRequest, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    operator, _ = _require_operator_owner(request)
    try:
        async with get_session() as db:
            return await _continuity_service(request).continue_task(db, operator, body)
    except BoardError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    except AuthFailure as exc:
        raise HTTPException(status_code=401, detail={"code": exc.code}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail={"code": "task_context_storage_unavailable"}) from exc


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
