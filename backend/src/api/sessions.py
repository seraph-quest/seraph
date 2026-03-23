import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.agent.session import session_manager

logger = logging.getLogger(__name__)

router = APIRouter()


class SessionUpdate(BaseModel):
    title: str = Field(..., min_length=1)


@router.get("/sessions")
async def list_sessions():
    """List all sessions with titles and last message preview."""
    return await session_manager.list_sessions()


@router.get("/sessions/search")
async def search_sessions(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=5, ge=1, le=20),
    exclude_session_id: str | None = Query(default=None),
):
    """Search prior sessions by title and message content."""
    normalized_query = q.strip()
    if not normalized_query:
        raise HTTPException(status_code=422, detail="Search query must not be empty.")
    return await session_manager.search_sessions(
        normalized_query,
        limit=limit,
        exclude_session_id=exclude_session_id,
    )


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Get paginated message history for a session."""
    session = await session_manager.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return await session_manager.get_messages(session_id, limit=limit, offset=offset)


@router.get("/sessions/{session_id}/todos")
async def get_session_todos(session_id: str):
    """Get the persisted todo list for a session."""
    session = await session_manager.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return await session_manager.get_todos(session_id)


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: SessionUpdate):
    """Update session title."""
    success = await session_manager.update_title(session_id, body.title)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "ok"}


@router.post("/sessions/{session_id}/generate-title")
async def generate_session_title(session_id: str):
    """Generate a short title for a session using LLM."""
    session = await session_manager.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    title = await session_manager.generate_title(session_id)
    return {"title": title or session.title}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and its messages."""
    success = await session_manager.delete(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "ok"}
