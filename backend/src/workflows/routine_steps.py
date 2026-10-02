"""The two non-generic guarded routine step wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RoutineStepContext:
    principal_id: str
    session_id: str
    lease_owner: str
    fencing_token: int
    # External publication is a separate authority from capability execution.
    # Native workflow adapters set this only after checking the authenticated
    # runtime principal's grant; ordinary M1 steps leave it false.
    external_mutation_granted: bool = False
    # Native guards bind this context to the exact requested routine
    # invocation parent, separately from the child lease.
    runtime_job_id: str | None = None


def _require_context(context: RoutineStepContext | None) -> RoutineStepContext:
    if not isinstance(context, RoutineStepContext):
        raise PermissionError("routine step requires trusted runtime context")
    if not all((context.principal_id, context.session_id, context.lease_owner)):
        raise PermissionError("routine step context is incomplete")
    if int(context.fencing_token) <= 0:
        raise PermissionError("routine step lease fence is invalid")
    if not str(context.runtime_job_id or "").strip():
        raise PermissionError("routine step runtime parent is missing")
    return context


async def guardian_watch_run(
    routine_invocation_job_id: str,
    *,
    child_job_id: str,
    context: RoutineStepContext | None = None,
    service: Any | None = None,
) -> dict[str, Any]:
    """Run only the persisted M1 watch bound to this invocation."""

    trusted = _require_context(context)
    if not isinstance(routine_invocation_job_id, str) or not routine_invocation_job_id.strip():
        raise ValueError("routine_invocation_job_id is required")
    if trusted.runtime_job_id != routine_invocation_job_id.strip():
        raise PermissionError("routine runtime parent mismatch")
    if not isinstance(child_job_id, str) or not child_job_id.strip():
        raise ValueError("child_job_id is required")
    if service is None:
        from src.workflows.routines import routine_service

        service = routine_service
    return await service.execute_watch_step(
        child_job_id.strip(),
        context=trusted,
    )


async def github_followthrough(
    routine_invocation_job_id: str,
    *,
    context: RoutineStepContext | None = None,
    service: Any | None = None,
) -> dict[str, Any]:
    """Run only the stored M3 follow-through child for this invocation."""

    trusted = _require_context(context)
    if not trusted.external_mutation_granted:
        raise PermissionError("routine follow-through requires external_mutation authority")
    if not isinstance(routine_invocation_job_id, str) or not routine_invocation_job_id.strip():
        raise ValueError("routine_invocation_job_id is required")
    if service is None:
        from src.workflows.routines import routine_service

        service = routine_service
    return await service.execute_followthrough_step(
        routine_invocation_job_id.strip(),
        context=trusted,
    )


async def _run_v2_leaf(
    routine_invocation_job_id: str,
    *,
    step_id: str,
    child_job_id: str,
    context: RoutineStepContext | None = None,
    runtime: Any | None = None,
) -> dict[str, Any]:
    """Invoke one fixed v2 leaf through the runtime-owned descriptor.

    The wrapper intentionally accepts only the persisted parent/child
    identities.  URLs, event selectors, and typed inputs are resolved from the
    reviewed v2 descriptor by the runtime; callers cannot smuggle a mutable
    action plan through a generated workflow step.
    """

    trusted = _require_context(context)
    parent_id = str(routine_invocation_job_id or "").strip()
    child_id = str(child_job_id or "").strip()
    if not parent_id or trusted.runtime_job_id != parent_id:
        raise PermissionError("routine runtime parent mismatch")
    if not child_id:
        raise ValueError("child_job_id is required")
    if step_id not in {"public_browser_check", "selected_meeting_prep", "source_watch"}:
        raise ValueError("routine v2 step is not registered")
    if runtime is None:
        from src.workflows.procedure_v2_runtime import procedure_v2_runtime

        runtime = procedure_v2_runtime
    executor = getattr(runtime, "execute_leaf", None)
    if executor is None:
        # The public runtime exposes the parent coordinator.  This guard keeps
        # a generated wrapper from inventing descriptor inputs when a caller
        # supplies an incomplete test/runtime object.
        raise PermissionError("routine v2 leaf runtime is unavailable")
    result = await executor(
        parent_job_id=parent_id,
        child_job_id=child_id,
        step_id=step_id,
        context=trusted,
    )
    return dict(result)


async def guardian_public_browser_check(
    routine_invocation_job_id: str,
    *,
    child_job_id: str,
    context: RoutineStepContext | None = None,
    runtime: Any | None = None,
) -> dict[str, Any]:
    return await _run_v2_leaf(
        routine_invocation_job_id,
        step_id="public_browser_check",
        child_job_id=child_job_id,
        context=context,
        runtime=runtime,
    )


async def guardian_selected_meeting_prep(
    routine_invocation_job_id: str,
    *,
    child_job_id: str,
    context: RoutineStepContext | None = None,
    runtime: Any | None = None,
) -> dict[str, Any]:
    return await _run_v2_leaf(
        routine_invocation_job_id,
        step_id="selected_meeting_prep",
        child_job_id=child_job_id,
        context=context,
        runtime=runtime,
    )


async def guardian_source_watch(
    routine_invocation_job_id: str,
    *,
    child_job_id: str,
    context: RoutineStepContext | None = None,
    runtime: Any | None = None,
) -> dict[str, Any]:
    return await _run_v2_leaf(
        routine_invocation_job_id,
        step_id="source_watch",
        child_job_id=child_job_id,
        context=context,
        runtime=runtime,
    )


__all__ = [
    "RoutineStepContext",
    "github_followthrough",
    "guardian_public_browser_check",
    "guardian_selected_meeting_prep",
    "guardian_source_watch",
    "guardian_watch_run",
]
