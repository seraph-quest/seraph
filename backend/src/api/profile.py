import asyncio
import logging

from fastapi import APIRouter
from sqlalchemy.exc import SQLAlchemyError

from src.db.engine import get_session as get_db
from src.memory.soul import default_soul_sections, render_soul_text
from src.profile.service import (
    get_or_create_profile,
    get_profile_snapshot,
    mark_onboarding_complete,
    reset_onboarding,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_PROFILE_SNAPSHOT_TIMEOUT_SECONDS = 2.0


def _degraded_profile_snapshot(*, degradation_code: str) -> dict[str, object]:
    """Return a bounded profile envelope when local profile state is unavailable."""
    sections = default_soul_sections()
    return {
        "name": "Unknown",
        "onboarding_completed": None,
        "soul_sections": sections,
        "soul_text": render_soul_text(sections),
        "status": "degraded",
        "degraded": True,
        "degradation_codes": [degradation_code],
        "claim_boundary": "default_profile_only",
    }


@router.get("/user/profile")
async def get_profile():
    """Get user profile, onboarding status, and structured soul projection."""
    try:
        return await asyncio.wait_for(
            get_profile_snapshot(),
            timeout=_PROFILE_SNAPSHOT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Profile snapshot degraded: local profile read timed out")
        return _degraded_profile_snapshot(degradation_code="profile_snapshot_timeout")
    except (SQLAlchemyError, OSError) as exc:
        logger.warning(
            "Profile snapshot degraded: local profile database unavailable (%s)",
            type(exc).__name__,
        )
        return _degraded_profile_snapshot(degradation_code="profile_database_unavailable")


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
