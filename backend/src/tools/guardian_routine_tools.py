"""Trusted native tools for the two generated guardian routine steps.

The generated workflow carries only a routine invocation ID. These wrappers
resolve the authenticated runtime principal and delegate to RoutineService;
they never expose a generic callable, service handle, or credential input.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from typing import Any

from smolagents import tool

from src.approval.runtime import get_current_session_id, get_current_trust_principal
from src.security.trust_contract import AuthorityGrant, PrincipalType
from src.workflows.job_runtime import durable_job_repository
from src.workflows.routine_steps import RoutineStepContext


def _run(coro: Any) -> Any:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _context() -> RoutineStepContext:
    principal = get_current_trust_principal()
    principal_type = getattr(principal, "principal_type", None)
    is_operator = (
        principal_type is PrincipalType.OPERATOR
        if isinstance(principal_type, PrincipalType)
        else str(principal_type or "").lower() == PrincipalType.OPERATOR.value
    )
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    session_id = str(
        getattr(principal, "operator_session_id", "")
        or getattr(principal, "session_id", "")
        or get_current_session_id()
        or ""
    ).strip()
    grants = {
        str(getattr(grant, "value", grant))
        for grant in (getattr(principal, "grants", ()) or ())
    }
    if not (
        principal is not None
        and is_operator
        and bool(getattr(principal, "authenticated", False))
        and not bool(getattr(principal, "revoked", False))
        and principal_id
        and session_id
        and AuthorityGrant.CAPABILITY_EXECUTE.value in grants
    ):
        raise PermissionError("guardian routine requires an authenticated capability context")
    runtime_job_id = str(getattr(principal, "job_id", "") or "").strip()
    if not runtime_job_id:
        raise PermissionError("guardian routine runtime job binding is missing")
    job = _run(durable_job_repository.get_job(runtime_job_id))
    if not isinstance(job, dict) or job.get("status") != "running":
        raise PermissionError("guardian routine runtime job is not running")
    owner = job.get("owner") if isinstance(job.get("owner"), dict) else {}
    lease = job.get("lease") if isinstance(job.get("lease"), dict) else {}
    if str(owner.get("principal_id") or "") != principal_id or str(job.get("session_id") or "") != session_id:
        raise PermissionError("guardian routine runtime owner/session mismatch")
    if not str(lease.get("owner") or "") or int(lease.get("fencing_token") or 0) <= 0:
        raise PermissionError("guardian routine runtime lease is missing")
    return RoutineStepContext(
        principal_id=principal_id,
        session_id=session_id,
        lease_owner=str(lease["owner"]),
        fencing_token=int(lease["fencing_token"]),
        external_mutation_granted=AuthorityGrant.EXTERNAL_MUTATION.value in grants,
    )


def _dispatch(routine_invocation_job_id: str, step_id: str) -> str:
    value = str(routine_invocation_job_id or "").strip()
    if not value:
        raise ValueError("routine_invocation_job_id is required")
    context = _context()
    from src.workflows.routines import routine_service

    result = _run(
        routine_service.execute_generated_step(
            value,
            step_id,
            context=context,
        )
    )
    return json.dumps(result if isinstance(result, dict) else {"status": str(result)}, sort_keys=True)


@tool
def guardian_watch_run(routine_invocation_job_id: str) -> str:
    """Run the owner-bound research-watch child for a routine invocation.

    Args:
        routine_invocation_job_id: Durable owner-bound routine invocation ID.
    """

    return _dispatch(routine_invocation_job_id, "guardian_watch_run")


@tool
def github_followthrough(routine_invocation_job_id: str) -> str:
    """Run the owner-bound GitHub follow-through child for a routine invocation.

    Args:
        routine_invocation_job_id: Durable owner-bound routine invocation ID.
    """

    return _dispatch(routine_invocation_job_id, "github_followthrough")


__all__ = ["github_followthrough", "guardian_watch_run"]
