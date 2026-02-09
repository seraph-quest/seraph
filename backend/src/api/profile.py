import logging

from fastapi import APIRouter
from sqlmodel import select

from src.db.engine import get_session as get_db
from src.db.models import UserProfile

logger = logging.getLogger(__name__)

router = APIRouter()


async def get_or_create_profile() -> UserProfile:
    """Get the singleton user profile, creating it if needed."""
    async with get_db() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.id == "singleton")
        )
        profile = result.scalars().first()
        if profile:
            return profile

        profile = UserProfile(id="singleton")
        db.add(profile)
        await db.flush()
        return profile


async def mark_onboarding_complete() -> None:
    """Mark onboarding as completed."""
    from datetime import datetime, timezone

    async with get_db() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.id == "singleton")
        )
        profile = result.scalars().first()
        if profile:
            profile.onboarding_completed = True
            profile.updated_at = datetime.now(timezone.utc)
            db.add(profile)


@router.get("/user/profile")
async def get_profile():
    """Get user profile and onboarding status."""
    profile = await get_or_create_profile()
    return {
        "name": profile.name,
        "onboarding_completed": profile.onboarding_completed,
    }
