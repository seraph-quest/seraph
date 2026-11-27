"""Operator control of Cordis runtime domain owners (#1019).

Authenticated HTTP only (the global OperatorAuthMiddleware enforces the Origin
check and the session cookie); no agent tool, scheduler or workflow can reach
it. Every move is a live fenced owner transition at a higher epoch with an
audit event, needs an explicit confirmation that repeats the domain name, and
is refused while the operator's ownership continuity is under recovery.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.auth.service import AuthenticatedOperator

router = APIRouter()


class OwnerTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: Literal["legacy", "cordis"]
    expected_epoch: int = Field(ge=1)
    confirm_domain: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="operator", max_length=200)


def _operator(request: Request) -> AuthenticatedOperator:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    if (not isinstance(operator, AuthenticatedOperator) or not getattr(principal, "authenticated", False)
            or getattr(principal, "revoked", False) or principal.principal_id == "operator:test-bypass"):
        raise HTTPException(status_code=401, detail={"code": "authentication_required", "recovery_action": "login"})
    return operator


async def _domains() -> list[dict]:
    from src.db.engine import get_session
    from src.runtime_plugins.cutover import cutover_manifest
    from src.runtime_plugins.ownership import inventory
    async with get_session() as db:
        rows = await inventory(db)
    return [{"runtime_domain": entry.domain, "prerequisites": list(entry.prerequisites),
             "owner_kind": rows[entry.domain].owner_kind if entry.domain in rows else None,
             "epoch": rows[entry.domain].epoch if entry.domain in rows else None,
             "state": rows[entry.domain].state if entry.domain in rows else None}
            for entry in cutover_manifest()]


@router.get("/runtime/composition/domains")
async def list_domains(request: Request):
    _operator(request)
    return {"domains": await _domains()}


@router.get("/runtime/composition/dump")
async def composition_dump(request: Request):
    """Read-only, secret-free dump of the booted composition (ADR-036 §6, #1198)."""
    from src.runtime_plugins.bridge import cordis_host
    from src.runtime_plugins.composition_dump import live_composition_dump
    _operator(request)
    return await live_composition_dump(cordis_host)


@router.post("/runtime/composition/domains/{domain}/owner")
async def transition_domain_owner(request: Request, domain: str, body: OwnerTransition):
    from src.runtime_plugins.bridge import cordis_host
    from src.runtime_plugins.cutover import cutover_manifest, transition_domain
    from src.runtime_plugins.ownership import CompositionBindingError
    from src.runtime_plugins.trust import is_pinned_domain
    operator = _operator(request)
    if operator.ownership_continuity != "stable":
        # A rotated or recovering Root may read the owners but not move them.
        raise HTTPException(status_code=403, detail={"code": "operator_ownership_recovery_required",
                                                     "recovery_action": "complete_ownership_recovery"})
    # A pinned trust domain (ADR-036 §3) reaches ``transition_domain``, which
    # audits the refusal and answers ``composition_owner_not_transferable`` (409).
    if domain not in {entry.domain for entry in cutover_manifest()} and not is_pinned_domain(domain):
        raise HTTPException(status_code=404, detail={"code": "composition_cutover_domain_invalid",
                                                     "recovery_action": None})
    if body.confirm_domain != domain:
        raise HTTPException(status_code=422, detail={"code": "composition_confirmation_required",
                                                     "recovery_action": "confirm_domain"})
    from src.runtime_plugins.lifecycle import LIFECYCLE_DOMAINS, runtime_lifecycle
    if domain in LIFECYCLE_DOMAINS and runtime_lifecycle.managed:
        # #1025: drain at E, flip, reactivate at E+1, as one bounded background
        # lifecycle step; the drain may take the longest live attempt's own bound.
        rows = {item["runtime_domain"]: item for item in await _domains()}
        if rows.get(domain, {}).get("epoch") != body.expected_epoch:
            raise HTTPException(status_code=409, detail={"code": "composition_owner_changed",
                                                         "recovery_action": "reload_domains"})
        try:
            runtime_lifecycle.claim_transition(domain)
        except CompositionBindingError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.reason_code,
                                                         "recovery_action": "reload_domains"}) from exc
        from fastapi.responses import JSONResponse
        from src.utils.background import track_task
        track_task(runtime_lifecycle.transition(domain, body.target,
                                                actor=f"operator:{operator.principal.principal_id}",
                                                expected_epoch=body.expected_epoch, reason=body.reason,
                                                claimed=True),
                   name=f"lifecycle-transition:{domain}")
        return JSONResponse(status_code=202, content={
            "transition": {"runtime_domain": domain, "target": body.target, "state": "draining"},
            "lifecycle": runtime_lifecycle.status(domain), "domains": await _domains()})
    try:
        receipt = await transition_domain(domain, body.target, host=cordis_host,
                                          actor=f"operator:{operator.principal.principal_id}",
                                          expected_epoch=body.expected_epoch, reason=body.reason)
    except CompositionBindingError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.reason_code, "recovery_action": "reload_domains"}) from exc
    return {"transition": receipt, "domains": await _domains()}


class MaintenanceBackup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: Literal["backup"]


def _maintenance_worker(request: Request):
    from src.runtime_plugins.worker import ProviderWorker
    worker = getattr(request.app.state, "provider_worker", None)
    if type(worker) is not ProviderWorker:
        raise HTTPException(status_code=409, detail={"code": "state_maintenance_unavailable",
                                                     "recovery_action": "use_host_cli"})
    return worker


def _stable_operator(request: Request) -> AuthenticatedOperator:
    operator = _operator(request)
    if operator.ownership_continuity != "stable":
        raise HTTPException(status_code=403, detail={"code": "operator_ownership_recovery_required",
                                                     "recovery_action": "complete_ownership_recovery"})
    return operator


@router.post("/runtime/maintenance/backup", status_code=202)
async def maintenance_backup(request: Request, body: MaintenanceBackup):
    """Start an in-process workspace backup through the state provider (#1024).

    The gate closes (or the request is refused typed) before this returns 202;
    the step itself runs as a worker-owned task, so a server shutdown never
    waits on this request. ``GET /api/runtime/maintenance`` reports it and
    ``POST /api/runtime/maintenance/abort`` ends a step that is still draining.
    Restore and rollback stay with the host CLI against a stopped backend.
    """
    from src.runtime_plugins.worker import StateMaintenanceRefused
    _stable_operator(request)
    worker = _maintenance_worker(request)
    try:
        status = await worker.start_backup()
    except StateMaintenanceRefused as exc:
        raise HTTPException(status_code=409, detail={"code": exc.reason_code, "recovery_action": "use_host_cli"}) from exc
    return {"maintenance": status}


@router.get("/runtime/maintenance")
async def maintenance_status(request: Request):
    """The running or last maintenance step; authenticates even while a step drains."""
    _operator(request)
    return {"maintenance": _maintenance_worker(request).maintenance_status()}


@router.post("/runtime/maintenance/abort")
async def maintenance_abort(request: Request):
    """End a maintenance step that is still draining; a copying step is never cut."""
    _stable_operator(request)
    worker = _maintenance_worker(request)
    if not worker.abort_maintenance():
        raise HTTPException(status_code=409, detail={"code": "state_maintenance_not_abortable",
                                                     "recovery_action": "reload_maintenance"})
    return {"maintenance": worker.maintenance_status()}
