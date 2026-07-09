"""Helpers for runtime audit events that should fail open."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from src.audit.repository import audit_repository
from src.utils.background import track_task

logger = logging.getLogger(__name__)
_SYNC_AUDIT_WAIT_SECONDS = 5.0
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


def _run_coro_on_dedicated_loop(coro, *, label: str) -> None:
    error: Exception | None = None

    def _runner() -> None:
        nonlocal error
        try:
            asyncio.run(coro)
        except Exception as exc:  # pragma: no cover - fail-open logging path
            error = exc

    worker = threading.Thread(target=_runner, name=label, daemon=True)
    worker.start()
    worker.join(timeout=_SYNC_AUDIT_WAIT_SECONDS)
    if worker.is_alive():
        logger.debug(
            "Timed out waiting for sync runtime audit %s after %.1fs",
            label,
            _SYNC_AUDIT_WAIT_SECONDS,
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
) -> None:
    """Record an external integration lifecycle event without breaking callers."""
    summary = f"{integration_type.replace('_', ' ').capitalize()} {name} {outcome.replace('_', ' ')}"
    try:
        await audit_repository.log_event(
            actor="system",
            event_type=f"integration_{outcome}",
            tool_name=f"{integration_type}:{name}",
            risk_level="low",
            policy_mode="full",
            summary=summary,
            details={
                "integration_type": integration_type,
                "name": name,
                **(details or {}),
            },
        )
    except Exception:
        logger.debug("Failed to record integration runtime audit event", exc_info=True)


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
) -> None:
    """Sync wrapper for integration runtime events used by non-async callers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _run_coro_on_dedicated_loop(
            log_integration_event(
                integration_type=integration_type,
                name=name,
                outcome=outcome,
                details=details,
            ),
            label=f"runtime_audit:integration:{integration_type}:{name}",
        )
        return

    try:
        track_task(
            log_integration_event(
                integration_type=integration_type,
                name=name,
                outcome=outcome,
                details=details,
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
