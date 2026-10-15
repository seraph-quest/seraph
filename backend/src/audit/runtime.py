"""Helpers for runtime audit events that should fail open."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from sqlalchemy.exc import OperationalError

from src.audit.repository import audit_repository
from src.db.engine import is_database_locked
from src.utils.background import track_task

logger = logging.getLogger(__name__)
_SYNC_AUDIT_WAIT_SECONDS = 5.0
# Diagnostic audits emitted by read and poll paths wait at most this long for
# SQLite's writer lock; under contention they are retried in the background at
# the normal wait instead of stalling the reader (#1074). Write-path and
# policy-refusal audits never pass this bound.
READ_PATH_AUDIT_WAIT_MS = 250
_DEFERRED_AUDIT_LOG_INTERVAL_SECONDS = 60.0
_READ_PATH_AUDITS: ContextVar[bool] = ContextVar("seraph_read_path_audits", default=False)
_deferred_audit_logged_at: dict[tuple[str, str, str], float] = {}
_INTEGRATION_TIMEOUT_RATE_LIMIT_SECONDS = 300.0
_integration_timeout_lock = threading.Lock()
_integration_timeout_state: dict[str, dict[str, float]] = {}


def _integration_timeout_group_key(
    *,
    integration_type: str,
    name: str,
    details: dict[str, Any] | None,
) -> str:
    action = str((details or {}).get("action") or "")
    hostname = str((details or {}).get("hostname") or "")
    return f"{integration_type}:{name}:{hostname}:{action}"


def _integration_timeout_rate_limit_details(
    *,
    integration_type: str,
    name: str,
    details: dict[str, Any] | None,
    window_seconds: float = _INTEGRATION_TIMEOUT_RATE_LIMIT_SECONDS,
) -> dict[str, Any] | None:
    now = time.monotonic()
    group_key = _integration_timeout_group_key(integration_type=integration_type, name=name, details=details)
    with _integration_timeout_lock:
        state = _integration_timeout_state.get(group_key)
        if state is not None and now - state["last_logged_at"] < window_seconds:
            state["suppressed_count"] = state.get("suppressed_count", 0.0) + 1.0
            return None
        suppressed_count = int(state.get("suppressed_count", 0.0)) if state is not None else 0
        _integration_timeout_state[group_key] = {"last_logged_at": now, "suppressed_count": 0.0}
    return {
        **(details or {}),
        "timeout_rate_limited": True,
        "timeout_rate_limit_group": group_key,
        "timeout_rate_limit_window_seconds": window_seconds,
        "suppressed_count_since_last": suppressed_count,
    }


def reset_integration_timeout_rate_limit_state() -> None:
    """Clear in-memory timeout grouping state for tests and local diagnostics."""
    with _integration_timeout_lock:
        _integration_timeout_state.clear()


@contextmanager
def read_path_audits() -> Iterator[None]:
    """Mark enclosed work (including ``asyncio.to_thread`` workers) as a read path."""
    token = _READ_PATH_AUDITS.set(True)
    try:
        yield
    finally:
        _READ_PATH_AUDITS.reset(token)


def read_path_audit_wait_ms() -> int | None:
    """The bounded audit wait inside ``read_path_audits``, else the normal wait."""
    return READ_PATH_AUDIT_WAIT_MS if _READ_PATH_AUDITS.get() else None


def _note_deferred_audit(kind: str, integration_type: str, name: str, level: int) -> None:
    key = (kind, integration_type, name)
    now = time.monotonic()
    with _integration_timeout_lock:
        last = _deferred_audit_logged_at.get(key)
        if last is not None and now - last < _DEFERRED_AUDIT_LOG_INTERVAL_SECONDS:
            return
        _deferred_audit_logged_at[key] = now
    logger.log(level, "Runtime audit %s %s:%s (database busy)", kind, integration_type, name)


def _run_coro_on_dedicated_loop(coro, *, label: str, wait_seconds: float = _SYNC_AUDIT_WAIT_SECONDS) -> None:
    error: Exception | None = None

    def _runner() -> None:
        nonlocal error
        try:
            asyncio.run(coro)
        except Exception as exc:  # pragma: no cover - fail-open logging path
            error = exc

    worker = threading.Thread(target=_runner, name=label, daemon=True)
    worker.start()
    worker.join(timeout=wait_seconds)
    if worker.is_alive():
        logger.debug(
            "Timed out waiting for sync runtime audit %s after %.1fs",
            label,
            wait_seconds,
        )
        return
    if error is not None:
        logger.debug("Failed to record sync runtime audit %s", label, exc_info=error)


async def log_agent_run_event(
    *,
    session_id: str,
    transport: str,
    is_onboarding: bool,
    outcome: str,
    policy_mode: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a chat/onboarding agent lifecycle event without breaking callers."""
    agent_name = "onboarding_agent" if is_onboarding else "chat_agent"
    summary = f"{transport.capitalize()} {agent_name} run {outcome.replace('_', ' ')}"
    try:
        await audit_repository.log_event(
            session_id=session_id,
            actor="agent",
            event_type=f"agent_run_{outcome}",
            tool_name=agent_name,
            risk_level="low",
            policy_mode=policy_mode,
            summary=summary,
            details={
                "transport": transport,
                "is_onboarding": is_onboarding,
                **(details or {}),
            },
        )
    except Exception:
        logger.debug("Failed to record agent runtime audit event", exc_info=True)


async def log_scheduler_job_event(
    *,
    job_name: str,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a scheduled job lifecycle event without breaking callers."""
    summary = f"Scheduled job {job_name} {outcome.replace('_', ' ')}"
    try:
        await audit_repository.log_event(
            actor="system",
            event_type=f"scheduler_job_{outcome}",
            tool_name=job_name,
            risk_level="low",
            policy_mode="full",
            summary=summary,
            details=details or {},
        )
    except Exception:
        logger.debug("Failed to record scheduler runtime audit event", exc_info=True)


async def log_background_task_event(
    *,
    task_name: str,
    outcome: str,
    session_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a background/helper runtime event without breaking callers."""
    summary = f"Background task {task_name} {outcome.replace('_', ' ')}"
    try:
        await audit_repository.log_event(
            session_id=session_id,
            actor="system",
            event_type=f"background_task_{outcome}",
            tool_name=task_name,
            risk_level="low",
            policy_mode="full",
            summary=summary,
            details=details or {},
        )
    except Exception:
        logger.debug("Failed to record background runtime audit event", exc_info=True)


async def log_integration_event(
    *,
    integration_type: str,
    name: str,
    outcome: str,
    details: dict[str, Any] | None = None,
    session_id: str | None = None,
    actor: str = "system",
    policy_mode: str = "full",
    principal_id: str | None = None,
    lock_wait_ms: int | None = None,
    _retry_inline: bool = False,
) -> bool:
    """Record an integration lifecycle event and report persistence state.

    Existing callers retain the fail-open behavior when they ignore the return
    value. Governed operator routes may supply authenticated lineage and fail
    closed or return an explicit degraded receipt when persistence fails.

    ``lock_wait_ms`` is for read and poll paths only: a busy database defers
    the event to one retry at the normal wait (``False`` is returned), so it
    is delayed rather than dropped.
    """
    summary = f"{integration_type.replace('_', ' ').capitalize()} {name} {outcome.replace('_', ' ')}"
    try:
        event_details: dict[str, Any] = {
            "integration_type": integration_type,
            "name": name,
            **(details or {}),
        }
        if session_id or principal_id:
            existing_lineage = event_details.get("lineage")
            lineage = dict(existing_lineage) if isinstance(existing_lineage, dict) else {}
            lineage.update(
                {
                    "principal_id": principal_id or actor,
                    "session_id": session_id,
                }
            )
            event_details["lineage"] = lineage
        await audit_repository.log_event(
            session_id=session_id,
            actor=actor,
            event_type=f"integration_{outcome}",
            tool_name=f"{integration_type}:{name}",
            risk_level="low",
            policy_mode=policy_mode,
            summary=summary,
            details=event_details,
            lock_wait_ms=lock_wait_ms,
        )
        return True
    except OperationalError as exc:
        if lock_wait_ms is None or not is_database_locked(exc):
            logger.debug("Failed to record integration runtime audit event", exc_info=True)
            return False
        _note_deferred_audit("deferred", integration_type, name, logging.INFO)
        retry = _retry_deferred_integration_event(
            integration_type=integration_type, name=name, outcome=outcome, details=details,
            session_id=session_id, actor=actor, policy_mode=policy_mode, principal_id=principal_id,
        )
        if _retry_inline:
            await retry
        else:
            track_task(retry, name=f"runtime_audit:deferred:{integration_type}:{name}")
        return False
    except Exception:
        logger.debug("Failed to record integration runtime audit event", exc_info=True)
        return False


async def _retry_deferred_integration_event(*, integration_type: str, name: str, **kwargs: Any) -> None:
    if not await log_integration_event(integration_type=integration_type, name=name, **kwargs):
        _note_deferred_audit("dropped", integration_type, name, logging.WARNING)


async def log_observer_delivery_event(
    *,
    decision: str,
    message_type: str,
    intervention_type: str | None,
    urgency: int | None,
    is_scheduled: bool,
    details: dict[str, Any] | None = None,
) -> None:
    """Record proactive delivery-gate decisions without breaking callers."""
    target = intervention_type or message_type
    summary = f"Observer delivery {decision} for {target}"
    try:
        await audit_repository.log_event(
            actor="system",
            event_type=f"observer_delivery_{decision}",
            tool_name="observer_delivery_gate",
            risk_level="low",
            policy_mode="full",
            summary=summary,
            details={
                "message_type": message_type,
                "intervention_type": intervention_type,
                "urgency": urgency,
                "is_scheduled": is_scheduled,
                **(details or {}),
            },
        )
    except Exception:
        logger.debug("Failed to record observer delivery audit event", exc_info=True)


def log_integration_event_sync(
    *,
    integration_type: str,
    name: str,
    outcome: str,
    details: dict[str, Any] | None = None,
    lock_wait_ms: int | None = None,
) -> None:
    """Sync wrapper for integration runtime events used by non-async callers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # A bounded read-path audit retries a busy write on its own thread,
        # so the caller waits only for the bounded attempt.
        _run_coro_on_dedicated_loop(
            log_integration_event(
                integration_type=integration_type,
                name=name,
                outcome=outcome,
                details=details,
                lock_wait_ms=lock_wait_ms,
                _retry_inline=True,
            ),
            label=f"runtime_audit:integration:{integration_type}:{name}",
            wait_seconds=(
                _SYNC_AUDIT_WAIT_SECONDS if lock_wait_ms is None
                else min(_SYNC_AUDIT_WAIT_SECONDS, lock_wait_ms / 1000 + 0.5)
            ),
        )
        return

    try:
        track_task(
            log_integration_event(
                integration_type=integration_type,
                name=name,
                outcome=outcome,
                details=details,
                lock_wait_ms=lock_wait_ms,
            ),
            name=f"runtime_audit:integration:{integration_type}:{name}",
        )
    except Exception:
        logger.debug("Failed to run integration runtime audit logger", exc_info=True)


def log_integration_timeout_event_sync(
    *,
    integration_type: str,
    name: str,
    details: dict[str, Any] | None = None,
    window_seconds: float = _INTEGRATION_TIMEOUT_RATE_LIMIT_SECONDS,
) -> None:
    """Record a grouped integration timeout without spamming repeated outages."""
    rate_limited_details = _integration_timeout_rate_limit_details(
        integration_type=integration_type,
        name=name,
        details=details,
        window_seconds=window_seconds,
    )
    if rate_limited_details is None:
        logger.debug("Suppressed repeated integration timeout audit for %s:%s", integration_type, name)
        return
    log_integration_event_sync(
        integration_type=integration_type,
        name=name,
        outcome="timed_out",
        details=rate_limited_details,
    )


def log_background_task_event_sync(
    *,
    task_name: str,
    outcome: str,
    session_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Sync wrapper for background/helper runtime events used by non-async callers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _run_coro_on_dedicated_loop(
            log_background_task_event(
                task_name=task_name,
                outcome=outcome,
                session_id=session_id,
                details=details,
            ),
            label=f"runtime_audit:background:{task_name}:{outcome}",
        )
        return

    try:
        track_task(
            log_background_task_event(
                task_name=task_name,
                outcome=outcome,
                session_id=session_id,
                details=details,
            ),
            name=f"runtime_audit:background:{task_name}:{outcome}",
        )
    except Exception:
        logger.debug("Failed to run background runtime audit logger", exc_info=True)
