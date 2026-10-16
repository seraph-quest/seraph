"""The WorkBoard dispatcher side of the #1025 lifecycle: admission and drain.

The dispatcher keeps its own capacity, priority, live-worker registry and the
single remote-inference slot. This module only answers two questions for the
``seraph.dispatch.v1`` activation: may new work start now, and how is running
work drained when the domain deactivates.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)
# After an attempt's own deadline, the settlement window it may still use.
SETTLEMENT_WINDOW_SECONDS = 30.0


def dispatch_admission_reason() -> str | None:
    """None while the dispatch domain is active; otherwise why no new work may start."""
    from .lifecycle import DISPATCH_DOMAIN, runtime_lifecycle
    if not runtime_lifecycle.enforced() or runtime_lifecycle.active(DISPATCH_DOMAIN) is not None:
        return None
    return runtime_lifecycle.blocked.get(DISPATCH_DOMAIN) or "dispatch_owner_not_active"


def assert_dispatch_admission() -> None:
    """API-launched workers (research recovery, document retry) start only while dispatch is active."""
    reason = dispatch_admission_reason()
    if reason is not None:
        from src.work_board.repository import BoardError
        from .lifecycle import lifecycle_recovery
        raise BoardError("dispatch_owner_not_active", lifecycle_recovery(reason) or "The dispatcher is not active.",
                         status_code=409, reason_code="dispatch_owner_not_active",
                         recovery_action="restore_prerequisite")


async def drain_dispatch(*, bound_seconds: float | None = None) -> list[tuple[str, str]]:
    """Wait for live WorkBoard workers within their original bounds, then cancel the rest.

    The bound is the longest attempt runtime (``MAX_RUNTIME_SECONDS``) plus the
    settlement window; no new bound is invented. A worker still running past it
    is cancelled; its own cancellation path settles the attempt, and an in-flight
    effect stays ``unknown`` (never forced or retried). Returns the cancelled keys.
    """
    from src.work_board.dispatcher import _ACTIVE_WORKER_TASKS, MAX_RUNTIME_SECONDS
    current = asyncio.current_task()
    live = {key: task for key, task in list(_ACTIVE_WORKER_TASKS.items())
            if not task.done() and task is not current}
    if not live:
        return []
    bound = float(MAX_RUNTIME_SECONDS) + SETTLEMENT_WINDOW_SECONDS if bound_seconds is None else bound_seconds
    _done, pending = await asyncio.wait(set(live.values()), timeout=bound)
    cancelled = [key for key, task in live.items() if task in pending]
    for key in cancelled:
        logger.warning("Dispatch drain cancelled worker %s past its bound", key)
        live[key].cancel()
    if pending:
        await asyncio.wait(pending, timeout=SETTLEMENT_WINDOW_SECONDS)
    return cancelled


__all__ = ["assert_dispatch_admission", "dispatch_admission_reason", "drain_dispatch"]
