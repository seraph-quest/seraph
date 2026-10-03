"""One pending optional metadata build, with bounded shielded caller waits.

This is an in-flight coalescer, never a completed response or authority cache.
The actual worker's finally-event survives caller and event-loop cancellation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any, Callable

from fastapi import HTTPException

CALLER_WAIT_SECONDS = 4.0


@dataclass
class _PendingBuild:
    loop: Any
    worker_done: Event
    future: Any = None
    submitted: bool = False


_pending: _PendingBuild | None = None
_lock = Lock()


def _unavailable() -> HTTPException:
    # Never include callback error strings, paths, URLs, keys or credentials.
    return HTTPException(status_code=503, detail={"code": "extension_metadata_unavailable",
        "message": "Optional extension metadata is still loading or unavailable. Retry after the current worker finishes."})


async def bounded_extension_metadata(builder: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    global _pending
    loop = asyncio.get_running_loop()
    with _lock:
        current = _pending
        if current is None or current.worker_done.is_set():
            current = _PendingBuild(loop=loop, worker_done=Event())
            _pending = current

            def build():
                try:
                    result = builder()
                    if not isinstance(result, dict):
                        raise ValueError("extension metadata shape invalid")
                    return result
                finally:
                    current.worker_done.set()

            try:
                current.future = loop.run_in_executor(None, build)
                # Submission, rather than callable start, owns the slot. A
                # queued callable must keep it until its actual finally-event.
                current.submitted = True
                # Consume an abandoned caller's exception without caching it.
                def observe(future):
                    if not future.cancelled():
                        future.exception()
                current.future.add_done_callback(observe)
            except Exception:
                current.worker_done.set()
                raise _unavailable() from None
        elif current.loop is not loop:
            # A prior closed/cancelled loop cannot grant a fresh worker while
            # its actual thread is queued or still working.
            raise _unavailable()
        future = current.future
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=CALLER_WAIT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise _unavailable() from None
