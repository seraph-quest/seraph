from fastapi import APIRouter, Request

from src.profile.onboarding_progress import (
    FirstResultProgress,
    load_progress,
    save_progress,
    open_verified_result,
    record_setup_skip,
    prepare_starter,
)

from src.db.engine import get_session as get_db
from src.profile.service import (
    get_or_create_profile,
    get_profile_snapshot,
    mark_onboarding_complete,
    reset_onboarding,
)

router = APIRouter()


@router.post("/user/onboarding/starter")
async def prepare_first_result_starter(request: Request, body: FirstResultProgress):
    return await prepare_starter(request, body)


@router.get("/user/onboarding/progress")
async def get_first_result_progress(request: Request):
    return await load_progress(request)


@router.put("/user/onboarding/progress")
async def update_first_result_progress(request: Request, body: FirstResultProgress):
    return await save_progress(request, body)


@router.post("/user/onboarding/result/{task_id}/open")
async def get_first_result(request: Request, task_id: str):
    return await open_verified_result(request, task_id)


@router.get("/user/profile")
async def get_profile():
    """Get user profile, onboarding status, and structured soul projection."""
    return await get_profile_snapshot()


@router.post("/user/onboarding/skip")
async def skip_onboarding(request: Request):
    """Skip onboarding and unlock the full agent."""
    await record_setup_skip(request)
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
