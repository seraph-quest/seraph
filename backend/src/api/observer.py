"""Observer API — context state and daemon integration endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel

from src.observer.manager import context_manager

router = APIRouter()


class ScreenContextRequest(BaseModel):
    active_window: str | None = None
    screen_context: str | None = None


@router.get("/observer/state")
async def get_observer_state():
    """Return the current context snapshot."""
    return context_manager.get_context().to_dict()


@router.post("/observer/context")
async def post_screen_context(body: ScreenContextRequest):
    """Receive screen context from native daemon."""
    context_manager.update_screen_context(body.active_window, body.screen_context)
    return {"status": "ok"}


@router.post("/observer/refresh")
async def post_refresh():
    """Debug endpoint — trigger a full context refresh."""
    ctx = await context_manager.refresh()
    return ctx.to_dict()
