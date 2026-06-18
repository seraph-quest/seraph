from fastapi import APIRouter

from src.db.engine import get_session as get_db
from src.profile.service import (
    get_or_create_profile,
    get_profile_snapshot,
    mark_onboarding_complete,
    reset_onboarding,
)

router = APIRouter()


@router.get("/user/profile")
async def get_profile():
    """Get user profile, onboarding status, and structured soul projection."""
    return await get_profile_snapshot()


@router.post("/user/onboarding/skip")
async def skip_onboarding():
    """Skip onboarding and unlock the full agent."""
    await mark_onboarding_complete()
    return {"status": "ok", "onboarding_completed": True}


@router.post("/user/onboarding/restart")
async def restart_onboarding():
    """Restart onboarding from scratch."""
    await reset_onboarding()
    return {"status": "ok", "onboarding_completed": False}


__all__ = [
    "get_db",
    "get_or_create_profile",
    "get_profile_snapshot",
    "mark_onboarding_complete",
    "reset_onboarding",
    "router",
]
