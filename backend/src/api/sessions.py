import logging

from fastapi import APIRouter, HTTPException
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


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str, limit: int = 100, offset: int = 0
):
    """Get paginated message history for a session."""
    session = await session_manager.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return await session_manager.get_messages(session_id, limit=limit, offset=offset)


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: SessionUpdate):
    """Update session title."""
    success = await session_manager.update_title(session_id, body.title)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "ok"}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and its messages."""
    success = await session_manager.delete(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "ok"}
