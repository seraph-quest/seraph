"""Helpers for runtime audit events that should fail open."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from src.audit.repository import audit_repository
from src.utils.background import track_task

logger = logging.getLogger(__name__)
_SYNC_AUDIT_WAIT_SECONDS = 5.0


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
