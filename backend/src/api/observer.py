"""Observer API — context state and daemon integration endpoints."""

import logging
import time
from datetime import date, datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from src.observer.manager import context_manager

logger = logging.getLogger(__name__)

router = APIRouter()


class ScreenObservationData(BaseModel):
    app: str | None = None
    window_title: str | None = None
    activity: str | None = None
    project: str | None = None
    summary: str | None = None
    details: list[str] | None = None
    blocked: bool = False


class ScreenContextRequest(BaseModel):
    active_window: str | None = None
    screen_context: str | None = None
    observation: ScreenObservationData | None = None
    switch_timestamp: float | None = None


@router.get("/observer/state")
async def get_observer_state():
    """Return the current context snapshot."""
    return context_manager.get_context().to_dict()


@router.post("/observer/context")
async def post_screen_context(body: ScreenContextRequest):
    """Receive screen context from native daemon."""
    context_manager.update_screen_context(body.active_window, body.screen_context)

    # Persist structured observation if present
    if body.observation is not None:
        try:
            from src.observer.screen_repository import screen_observation_repo

            obs_data = body.observation
            timestamp = (
                datetime.fromtimestamp(body.switch_timestamp, tz=timezone.utc)
                if body.switch_timestamp
                else None
            )

            await screen_observation_repo.create(
                app_name=obs_data.app or "",
                window_title=obs_data.window_title or "",
                activity_type=obs_data.activity or "other",
                project=obs_data.project,
                summary=obs_data.summary,
                details=obs_data.details,
                blocked=obs_data.blocked,
                timestamp=timestamp,
            )
        except Exception:
            logger.exception("Failed to persist screen observation")

    return {"status": "ok"}


@router.get("/observer/daemon-status")
async def daemon_status():
    """Return daemon connectivity status based on heartbeat timestamp."""
    ctx = context_manager.get_context()
    connected = (
        ctx.last_daemon_post is not None
        and (time.time() - ctx.last_daemon_post) < 30
    )
    return {
        "connected": connected,
        "last_post": ctx.last_daemon_post,
        "active_window": ctx.active_window,
        "has_screen_context": bool(ctx.screen_context),
    }


@router.get("/observer/activity/today")
async def get_activity_today():
    """Return today's activity summary from screen observations."""
    try:
        from src.observer.screen_repository import screen_observation_repo

        summary = await screen_observation_repo.get_daily_summary(date.today())
        return summary
    except Exception:
        logger.exception("Failed to get daily activity summary")
        return {"error": "Failed to retrieve activity summary"}


@router.post("/observer/refresh")
async def post_refresh():
    """Debug endpoint — trigger a full context refresh."""
    ctx = await context_manager.refresh()
    return ctx.to_dict()
