"""Operator-only readback and local execution controls for v2 capability packs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.capabilities import _require_authenticated_capability_operator
from src.extensions.capability_pack import (
    CapabilityPackLifecycle,
    CapabilityPackLifecycleError,
    canonical_digest,
)
from src.goals.repository import goal_repository


router = APIRouter()


class LocalExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str
    job_id: str
    domain: str = Field(pattern=r"^(primary|secondary|research|research_brief|goal_snapshot|snapshot)$")
    artifact_path: str | None = None
    source_url: str | None = None
    query: str | None = None
    source_payload: dict[str, Any] | list[Any] | str | None = None
    goal_snapshot: dict[str, Any] | str | None = None


class ReconciliationResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    action: str = Field(default="cancel", pattern=r"^(cancel|recover)$")


def _store() -> CapabilityPackLifecycle:
    return CapabilityPackLifecycle()


@router.get("/capability-packs/{pack_id}")
async def capability_pack_readback(pack_id: str, request: Request) -> dict[str, Any]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    try:
        return _store().status(pack_id, owner_principal_id=principal_id, session_id=operator.session_id)
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/capability-packs/{pack_id}/reconcile")
async def capability_pack_reconcile(pack_id: str, request: Request) -> dict[str, Any]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    try:
        return _store().reconcile(pack_id, owner_principal_id=principal_id, session_id=operator.session_id)
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/capability-packs/{pack_id}/execute-local")
async def capability_pack_execute_local(
    pack_id: str,
    req: LocalExecutionRequest,
    request: Request,
) -> dict[str, Any]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    source_payload = req.source_payload

    canonical_goal_snapshot: dict[str, Any] | str | None = req.goal_snapshot
    if req.domain in {"secondary", "goal_snapshot", "snapshot"}:
        try:
            goal = await goal_repository.get(req.goal_id)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "goal_snapshot_readback_unavailable", "recovery": "Retry after canonical goal storage recovers."},
            ) from exc
        if goal is None:
            raise HTTPException(status_code=404, detail={"code": "goal_not_found", "goal_id": req.goal_id})
        current_revision = max(int(goal.revision or 1), 1)
        if not isinstance(req.goal_snapshot, dict):
            raise HTTPException(status_code=422, detail="goal_snapshot must be a canonical persisted goal mapping")
        if req.goal_snapshot.get("goal_id") not in {None, goal.id}:
            raise HTTPException(status_code=422, detail="goal_snapshot identity does not match the requested goal")
        if req.goal_snapshot.get("owner_principal_id") not in {None, principal_id} or req.goal_snapshot.get("session_id") not in {None, operator.session_id}:
            raise HTTPException(status_code=403, detail="goal_snapshot operator binding conflicts with the authenticated session")
        supplied_revision = req.goal_snapshot.get("revision", req.goal_snapshot.get("goal_revision"))
        if isinstance(supplied_revision, bool) or not isinstance(supplied_revision, int) or supplied_revision != current_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "stale_goal_revision",
                    "goal_id": req.goal_id,
                    "expected_revision": supplied_revision,
                    "current_revision": current_revision,
                    "recovery": "Refresh the goal and resubmit against the current revision.",
                },
            )
        canonical_goal_snapshot = {
            **req.goal_snapshot,
            "goal_id": goal.id,
            "revision": current_revision,
            "status": str(goal.status.value if hasattr(goal.status, "value") else goal.status),
            "owner_principal_id": principal_id,
            "session_id": operator.session_id,
            "canonical_source": "goals",
        }
        if canonical_goal_snapshot["status"] != "active":
            raise HTTPException(status_code=409, detail={"code": "goal_not_active", "goal_id": req.goal_id})

    def intercepted_transport(_url: str, *, query: str | None = None) -> Any:
        # The API deliberately injects request data as an in-process fixture;
        # this path never constructs an HTTP client or permits live egress.
        if source_payload is None:
            raise CapabilityPackLifecycleError("source_payload is required for primary local execution")
        return source_payload

    try:
        return _store().execute_local(
            pack_id,
            goal_id=req.goal_id,
            job_id=req.job_id,
            domain=req.domain,
            artifact_path=req.artifact_path,
            owner_principal_id=principal_id,
            session_id=operator.session_id,
            source_url=req.source_url or "local://intercepted/source",
            query=req.query,
            goal_snapshot=canonical_goal_snapshot,
            source_payload_digest=canonical_digest(source_payload) if source_payload is not None else None,
            intercepted_transport=intercepted_transport if req.domain in {"primary", "research", "research_brief"} else None,
        )
    except CapabilityPackLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/capability-packs/{pack_id}/reconcile/resolve")
async def capability_pack_resolve_reconciliation(
    pack_id: str,
    req: ReconciliationResolutionRequest,
    request: Request,
) -> dict[str, Any]:
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(getattr(operator.principal, "principal_id", "") or "")
    try:
        return _store().resolve_reconciliation(
            pack_id,
            job_id=req.job_id,
            action=req.action,
            owner_principal_id=principal_id,
            session_id=operator.session_id,
        )
    except CapabilityPackLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


__all__ = ["LocalExecutionRequest", "ReconciliationResolutionRequest", "router"]
