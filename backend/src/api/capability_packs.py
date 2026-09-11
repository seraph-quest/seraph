"""Operator-only readback and local execution controls for v2 capability packs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.capabilities import _require_authenticated_capability_operator
from src.extensions.capability_pack import (
    CapabilityPackLifecycle,
    CapabilityPackLifecycleError,
)


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


def _store() -> CapabilityPackLifecycle:
    return CapabilityPackLifecycle()


@router.get("/capability-packs/{pack_id}")
async def capability_pack_readback(pack_id: str, request: Request) -> dict[str, Any]:
    _require_authenticated_capability_operator(request)
    try:
        return _store().status(pack_id)
    except (CapabilityPackLifecycleError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/capability-packs/{pack_id}/reconcile")
async def capability_pack_reconcile(pack_id: str, request: Request) -> dict[str, Any]:
    _require_authenticated_capability_operator(request)
    try:
        return _store().reconcile(pack_id)
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
            goal_snapshot=req.goal_snapshot,
            intercepted_transport=intercepted_transport if req.domain in {"primary", "research", "research_brief"} else None,
        )
    except CapabilityPackLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


__all__ = ["LocalExecutionRequest", "router"]
