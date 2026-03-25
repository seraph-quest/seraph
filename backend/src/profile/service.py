from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select

from src.db.engine import get_session as get_db
from src.db.models import UserProfile
from src.memory.soul import (
    default_soul_sections,
    get_soul_file_mtime,
    parse_soul_sections,
    read_soul_file_text,
    render_soul_text,
    write_soul,
)

logger = logging.getLogger(__name__)

_SOUL_PROFILE_SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_soul_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _project_soul_file(soul_text: str) -> None:
    try:
        write_soul(soul_text)
    except Exception:
        logger.warning("Failed to project structured soul state to soul.md", exc_info=True)


def _serialize_soul_sections(
    sections: dict[str, str], projected_hash: str | None
) -> str:
    return json.dumps(
        {
            "schema_version": _SOUL_PROFILE_SCHEMA_VERSION,
            "soul_sections": sections,
            "projected_hash": projected_hash,
        },
        sort_keys=True,
    )


def _load_soul_state(profile: UserProfile) -> tuple[dict[str, str], str | None]:
    if isinstance(profile.preferences_json, str) and profile.preferences_json.strip():
        try:
            payload = json.loads(profile.preferences_json)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            raw_sections = payload.get("soul_sections")
            raw_hash = payload.get("projected_hash")
            if isinstance(raw_sections, dict):
                parsed_sections = {
                    str(section): str(content)
                    for section, content in raw_sections.items()
                    if str(section).strip() and str(content).strip()
                }
                if parsed_sections:
                    return (
                        parsed_sections,
                        str(raw_hash).strip() if isinstance(raw_hash, str) else None,
                    )
    if isinstance(profile.soul_text, str) and profile.soul_text.strip():
        return parse_soul_sections(profile.soul_text), _hash_soul_text(profile.soul_text)
    rendered_default = render_soul_text(default_soul_sections())
    return default_soul_sections(), _hash_soul_text(rendered_default)


async def _get_or_create_profile_record(db) -> UserProfile:
    result = await db.execute(
        select(UserProfile).where(UserProfile.id == "singleton")
    )
    profile = result.scalars().first()
    if profile is not None:
        return profile

    profile = UserProfile(id="singleton")
    db.add(profile)
    await db.flush()
    return profile


async def _persist_profile_sections(
    *,
    db,
    profile: UserProfile,
    sections: dict[str, str],
    soul_text: str,
    projected_hash: str | None = None,
) -> None:
    profile.preferences_json = _serialize_soul_sections(
        sections,
        projected_hash or _hash_soul_text(soul_text),
    )
    profile.soul_text = soul_text
    profile.updated_at = _now()
    db.add(profile)
    await db.flush()


async def get_or_create_profile() -> UserProfile:
    """Get the singleton user profile, creating it if needed."""
    async with get_db() as db:
        return await _get_or_create_profile_record(db)


async def mark_onboarding_complete() -> None:
    """Mark onboarding as completed."""
    async with get_db() as db:
        profile = await _get_or_create_profile_record(db)
        profile.onboarding_completed = True
        profile.updated_at = _now()
        db.add(profile)


async def reset_onboarding() -> None:
    """Reset onboarding so the user goes through it again."""
    async with get_db() as db:
        profile = await _get_or_create_profile_record(db)
        profile.onboarding_completed = False
        profile.updated_at = _now()
        db.add(profile)


async def sync_soul_file_to_profile() -> dict[str, str]:
    """Best-effort reconciliation from the current soul file into structured profile state."""
    live_soul_text = read_soul_file_text()
    live_soul_mtime = get_soul_file_mtime()

    async with get_db() as db:
        profile = await _get_or_create_profile_record(db)
        current_sections, projected_hash = _load_soul_state(profile)
        stored_soul_text = profile.soul_text or render_soul_text(current_sections)
        stored_hash = projected_hash or _hash_soul_text(stored_soul_text)

        if live_soul_text is None:
            if profile.soul_text != stored_soul_text or projected_hash != stored_hash:
                await _persist_profile_sections(
                    db=db,
                    profile=profile,
                    sections=current_sections,
                    soul_text=stored_soul_text,
                    projected_hash=stored_hash,
                )
            _project_soul_file(stored_soul_text)
            return current_sections

        live_hash = _hash_soul_text(live_soul_text)
        if live_hash == stored_hash:
            if profile.soul_text != live_soul_text or projected_hash != live_hash:
                await _persist_profile_sections(
                    db=db,
                    profile=profile,
                    sections=parse_soul_sections(live_soul_text),
                    soul_text=live_soul_text,
                    projected_hash=live_hash,
                )
            return parse_soul_sections(live_soul_text)

        live_sections = parse_soul_sections(live_soul_text)
        if (
            (profile.soul_text is not None or profile.preferences_json is not None)
            and projected_hash is not None
            and live_soul_mtime is not None
            and profile.updated_at is not None
            and live_soul_mtime < profile.updated_at.timestamp()
        ):
            if profile.soul_text != stored_soul_text or projected_hash != stored_hash:
                await _persist_profile_sections(
                    db=db,
                    profile=profile,
                    sections=current_sections,
                    soul_text=stored_soul_text,
                    projected_hash=stored_hash,
                )
            _project_soul_file(stored_soul_text)
            return current_sections

        if live_sections != current_sections or profile.soul_text != live_soul_text:
            await _persist_profile_sections(
                db=db,
                profile=profile,
                sections=live_sections,
                soul_text=live_soul_text,
                projected_hash=live_hash,
            )
        return live_sections


async def update_profile_soul_section(section: str, content: str) -> str:
    """Update one structured soul section, then rewrite the projected soul file."""
    normalized_section = " ".join(str(section).strip().split())
    if not normalized_section:
        raise ValueError("section must be non-empty")

    await sync_soul_file_to_profile()

    async with get_db() as db:
        profile = await _get_or_create_profile_record(db)
        sections, _ = _load_soul_state(profile)
        sections[normalized_section] = str(content).strip()
        projected_soul = render_soul_text(sections)
        await _persist_profile_sections(
            db=db,
            profile=profile,
            sections=sections,
            soul_text=projected_soul,
            projected_hash=_hash_soul_text(projected_soul),
        )

    _project_soul_file(projected_soul)

    return projected_soul


async def get_profile_snapshot() -> dict[str, Any]:
    """Return the structured profile payload exposed by the API."""
    sections = await sync_soul_file_to_profile()
    profile = await get_or_create_profile()
    soul_text = profile.soul_text or render_soul_text(sections)
    return {
        "name": profile.name,
        "onboarding_completed": profile.onboarding_completed,
        "soul_sections": sections,
        "soul_text": soul_text,
    }
