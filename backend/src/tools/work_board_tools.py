"""Native task scoped controls exposed only inside a bound board worker run.

The public tool arguments intentionally contain no task, attempt, owner, or
fence fields. The dispatcher/worker host binds those values in a short lived
context before constructing the worker runtime. A delegated specialist never
receives these tools (see ``agent.specialists``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator

from smolagents import tool

from src.approval.runtime import get_current_trust_principal
from src.work_board.tools import (
    WorkBoardWorkerBlock,
    WorkBoardWorkerComment,
    WorkBoardWorkerEvidence,
    WorkBoardWorkerRequest,
    WorkBoardWorkerTools,
)


_BOUND_WORKER: ContextVar[tuple[WorkBoardWorkerTools, WorkBoardWorkerRequest] | None] = ContextVar(
    "work_board_worker_context",
    default=None,
)


def _run(coro: Any) -> Any:
    """Bridge the synchronous smolagents tool host to the async repository."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _bound() -> tuple[WorkBoardWorkerTools, WorkBoardWorkerRequest]:
    value = _BOUND_WORKER.get()
    if value is None:
        raise PermissionError("work-board worker authority is not bound by the server")
    _worker, request = value
    principal = get_current_trust_principal()
    if principal is None or str(getattr(principal, "job_id", "") or "") != request.workflow_run_id:
        raise PermissionError("work-board worker authority is not bound to this durable run")
    return value


def _advance_task_revision(worker: WorkBoardWorkerTools, request: WorkBoardWorkerRequest, result: Any) -> None:
    """Refresh the injected CAS revision after a successful board mutation."""
    if isinstance(result, dict) and result.get("task_revision") is not None:
        _BOUND_WORKER.set(
            (
                worker,
                request.model_copy(update={"expected_task_revision": int(result["task_revision"])}),
            )
        )


@contextmanager
def bind_work_board_worker_context(
    request: WorkBoardWorkerRequest,
    *,
    tools: WorkBoardWorkerTools | None = None,
) -> Iterator[None]:
    """Bind one server-created task/attempt request for native worker tools."""
    worker = tools or WorkBoardWorkerTools()
    token: Token = _BOUND_WORKER.set((worker, request))
    try:
        yield
    finally:
        _BOUND_WORKER.reset(token)


def get_bound_work_board_tools() -> list[Any]:
    """Return worker tools only while the server has bound an attempt host."""

    if _BOUND_WORKER.get() is None:
        return []
    return [
        work_board_show,
        work_board_heartbeat,
        work_board_comment,
        work_board_block,
        work_board_request_review,
        work_board_request_completion,
    ]


class WorkBoardWorkerHost:
    """Small governed host that binds worker controls around one agent call."""

    def __init__(
        self,
        request: WorkBoardWorkerRequest,
        *,
        tools: WorkBoardWorkerTools | None = None,
    ) -> None:
        self.request = request
        self.worker = tools or WorkBoardWorkerTools()
        self._context = None

    @property
    def tools(self) -> list[Any]:
        return [
            work_board_show,
            work_board_heartbeat,
            work_board_comment,
            work_board_block,
            work_board_request_review,
            work_board_request_completion,
        ]

    def __enter__(self) -> "WorkBoardWorkerHost":
        self._context = bind_work_board_worker_context(self.request, tools=self.worker)
        self._context.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._context is not None:
            self._context.__exit__(exc_type, exc, traceback)
            self._context = None


@tool
def work_board_show(task_id: str) -> str:
    """Read the currently bound task and authoritative attempt projection.

    Args:
        task_id: The server-bound board task identifier.
    """
    import json

    worker, request = _bound()
    if task_id != request.task_id:
        raise PermissionError("task_id is not the server-bound board task")
    return json.dumps(_run(worker.show(request)), sort_keys=True)


@tool
def work_board_heartbeat() -> str:
    """Heartbeat the currently bound board lease."""
    import json

    worker, request = _bound()
    return json.dumps(_run(worker.heartbeat(request)), sort_keys=True)


@tool
def work_board_comment(body: str) -> str:
    """Add bounded progress to the currently bound task.

    Args:
        body: Bounded progress text.
    """
    import json

    worker, request = _bound()
    comment = WorkBoardWorkerComment(**request.model_dump(), body=body)
    result = _run(worker.comment(comment))
    _advance_task_revision(worker, request, result)
    return json.dumps(result, sort_keys=True)


@tool
def work_board_block(block_kind: str) -> str:
    """Report a bounded block for the currently bound attempt.

    Args:
        block_kind: Closed worker recovery kind.
    """
    import json

    worker, request = _bound()
    block = WorkBoardWorkerBlock(**request.model_dump(), block_kind=block_kind)
    return json.dumps(_run(worker.block(block)), sort_keys=True)


@tool
def work_board_request_review(evidence_refs: list[str]) -> str:
    """Request operator review; only durable reconciliation can grant Review.

    Args:
        evidence_refs: Up to twenty safe IDs from the current attempt receipt.
    """
    import json

    worker, request = _bound()
    evidence = WorkBoardWorkerEvidence(**request.model_dump(), evidence_refs=evidence_refs)
    result = _run(worker.request_review(evidence))
    _advance_task_revision(worker, request, result)
    return json.dumps(result, sort_keys=True)


@tool
def work_board_request_completion(evidence_refs: list[str]) -> str:
    """Request completion verification; text cannot grant Done.

    Args:
        evidence_refs: Up to twenty safe IDs from the current attempt receipt.
    """
    import json

    worker, request = _bound()
    evidence = WorkBoardWorkerEvidence(**request.model_dump(), evidence_refs=evidence_refs)
    result = _run(worker.completion_request(evidence))
    _advance_task_revision(worker, request, result)
    return json.dumps(result, sort_keys=True)


__all__ = [
    "bind_work_board_worker_context",
    "get_bound_work_board_tools",
    "WorkBoardWorkerHost",
    "work_board_block",
    "work_board_comment",
    "work_board_request_completion",
    "work_board_heartbeat",
    "work_board_request_review",
    "work_board_show",
]
