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


def _require_context(context: RoutineStepContext | None) -> RoutineStepContext:
    if not isinstance(context, RoutineStepContext):
        raise PermissionError("routine step requires trusted runtime context")
    if not all((context.principal_id, context.session_id, context.lease_owner)):
        raise PermissionError("routine step context is incomplete")
    if int(context.fencing_token) <= 0:
        raise PermissionError("routine step lease fence is invalid")
    return context


async def guardian_watch_run(
    routine_invocation_job_id: str,
    *,
    context: RoutineStepContext | None = None,
    service: Any | None = None,
) -> dict[str, Any]:
    """Run only the persisted M1 watch bound to this invocation."""

    _require_context(context)
    if not isinstance(routine_invocation_job_id, str) or not routine_invocation_job_id.strip():
        raise ValueError("routine_invocation_job_id is required")
    if service is None:
        from src.workflows.routines import routine_service

        service = routine_service
    return await service.execute_watch_step(
        routine_invocation_job_id.strip(),
        context=context,
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


__all__ = ["RoutineStepContext", "github_followthrough", "guardian_watch_run"]
