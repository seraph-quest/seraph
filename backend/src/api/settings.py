"""Settings API — interruption mode and capture mode management."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from src.db.engine import get_session as get_db
from src.db.models import UserProfile
from src.observer.manager import context_manager
from src.observer.user_state import InterruptionMode

logger = logging.getLogger(__name__)
router = APIRouter()


class InterruptionModeRequest(BaseModel):
    mode: str


class CaptureModeRequest(BaseModel):
    mode: str


_VALID_CAPTURE_MODES = {"on_switch", "balanced", "detailed"}


@router.get("/settings/interruption-mode")
async def get_interruption_mode():
    """Get current interruption mode, attention budget, and user state."""
    ctx = context_manager.get_context()
    return {
        "mode": ctx.interruption_mode,
        "attention_budget_remaining": ctx.attention_budget_remaining,
        "user_state": ctx.user_state,
    }


@router.put("/settings/interruption-mode")
async def set_interruption_mode(body: InterruptionModeRequest):
    """Update interruption mode. Resets attention budget to mode default."""
    # Validate mode
    valid_modes = {m.value for m in InterruptionMode}
    if body.mode not in valid_modes:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{body.mode}'. Must be one of: {', '.join(sorted(valid_modes))}",
        )

    # Update in-memory context
    context_manager.update_interruption_mode(body.mode)

    # Persist to DB
    async with get_db() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.id == "singleton")
        )
        profile = result.scalars().first()
        if profile:
            profile.interruption_mode = body.mode
            profile.updated_at = datetime.now(timezone.utc)
            db.add(profile)

    ctx = context_manager.get_context()
    return {
        "mode": ctx.interruption_mode,
        "attention_budget_remaining": ctx.attention_budget_remaining,
        "user_state": ctx.user_state,
    }


@router.get("/settings/capture-mode")
async def get_capture_mode():
    """Get current capture mode setting."""
    ctx = context_manager.get_context()
    return {"mode": ctx.capture_mode}


@router.put("/settings/capture-mode")
async def set_capture_mode(body: CaptureModeRequest):
    """Update capture mode (on_switch | balanced | detailed)."""
    if body.mode not in _VALID_CAPTURE_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{body.mode}'. Must be one of: {', '.join(sorted(_VALID_CAPTURE_MODES))}",
        )

    # Update in-memory context
    context_manager.update_capture_mode(body.mode)

    # Persist to DB
    async with get_db() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.id == "singleton")
        )
        profile = result.scalars().first()
        if profile:
            profile.capture_mode = body.mode
            profile.updated_at = datetime.now(timezone.utc)
            db.add(profile)

    return {"mode": body.mode}
