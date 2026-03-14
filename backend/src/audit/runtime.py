"""Helpers for runtime audit events that should fail open."""

from __future__ import annotations

import logging
from typing import Any

from src.audit.repository import audit_repository

logger = logging.getLogger(__name__)


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
