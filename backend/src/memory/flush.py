from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
import threading
from typing import TYPE_CHECKING

from sqlmodel import select

from src.approval.runtime import get_current_session_id
from src.db.engine import get_session
from src.db.models import Message, Session

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.agent.session import SessionManager


@dataclass(frozen=True)
class SessionFlushFingerprint:
    cache_key: str
    fingerprint: str


_last_flushed_session_fingerprints: dict[str, str] = {}
_inflight_flush_fingerprints: set[str] = set()
_inflight_flush_guard = threading.Lock()


async def _build_session_flush_fingerprint(session_id: str) -> SessionFlushFingerprint | None:
    async with get_session() as db:
        session = (
            await db.execute(select(Session).where(Session.id == session_id))
        ).scalars().first()
        if session is None:
            return None

        message_count = len(
            (
                await db.execute(
                    select(Message.id).where(Message.session_id == session_id)
                )
            ).all()
        )
        session_created_at = session.created_at.isoformat()
        session_updated_at = session.updated_at.isoformat()
        return SessionFlushFingerprint(
            cache_key=f"{session.id}:{session_created_at}",
            fingerprint=f"{session_updated_at}:{message_count}",
        )


async def flush_session_memory(
    session_id: str,
    *,
    trigger: str,
    workflow_name: str | None = None,
    manager: SessionManager | None = None,
) -> bool:
    fingerprint = await _build_session_flush_fingerprint(session_id)
    if fingerprint is None:
        return False

    previous = _last_flushed_session_fingerprints.get(fingerprint.cache_key)
    if previous == fingerprint.fingerprint:
        return False
    inflight_key = f"{fingerprint.cache_key}:{fingerprint.fingerprint}"
    with _inflight_flush_guard:
        if inflight_key in _inflight_flush_fingerprints:
            return False
        _inflight_flush_fingerprints.add(inflight_key)

    from src.memory.consolidator import consolidate_session

    try:
        result = await consolidate_session(
            session_id,
            trigger=trigger,
            workflow_name=workflow_name,
            manager=manager,
        )
        if result.should_cache_fingerprint:
            _last_flushed_session_fingerprints[fingerprint.cache_key] = fingerprint.fingerprint
            return True
        return False
    finally:
        with _inflight_flush_guard:
            _inflight_flush_fingerprints.discard(inflight_key)


def flush_session_memory_sync(
    *,
    session_id: str | None = None,
    trigger: str,
    workflow_name: str | None = None,
    manager: SessionManager | None = None,
) -> bool:
    resolved_session_id = session_id or get_current_session_id()
    if not resolved_session_id:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(
                flush_session_memory(
                    resolved_session_id,
                    trigger=trigger,
                    workflow_name=workflow_name,
                    manager=manager,
                )
            )
        except Exception:
            logger.exception(
                "Memory flush failed for session %s via trigger %s",
                resolved_session_id[:8],
                trigger,
            )
            return False

    logger.debug(
        "Skipped synchronous memory flush for session %s because a loop is already running",
        resolved_session_id[:8],
    )
    return False


def _reset_memory_flush_state() -> None:
    _last_flushed_session_fingerprints.clear()
    with _inflight_flush_guard:
        _inflight_flush_fingerprints.clear()
