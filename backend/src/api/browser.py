"""Structured browser session API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from src.browser.sessions import browser_session_runtime

router = APIRouter()


@router.get("/browser/sessions")
async def list_browser_sessions(session_id: str = Query(...)):
    return {"sessions": browser_session_runtime.list_sessions(owner_session_id=session_id)}


@router.get("/browser/sessions/{browser_session_id}")
async def get_browser_session(browser_session_id: str, session_id: str = Query(...)):
    session = browser_session_runtime.get_session(browser_session_id, owner_session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Browser session '{browser_session_id}' not found")
    return {"session": session}


@router.delete("/browser/sessions/{browser_session_id}")
async def close_browser_session(browser_session_id: str, session_id: str = Query(...)):
    session = browser_session_runtime.close_session(browser_session_id, owner_session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Browser session '{browser_session_id}' not found")
    return {"status": "closed", "session": session}
