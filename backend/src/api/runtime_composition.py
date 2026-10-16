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


@router.post("/runtime/composition/domains/{domain}/owner")
async def transition_domain_owner(request: Request, domain: str, body: OwnerTransition):
    from src.runtime_plugins.bridge import cordis_host
    from src.runtime_plugins.cutover import cutover_manifest, transition_domain
    from src.runtime_plugins.ownership import CompositionBindingError
    operator = _operator(request)
    if operator.ownership_continuity != "stable":
        # A rotated or recovering Root may read the owners but not move them.
        raise HTTPException(status_code=403, detail={"code": "operator_ownership_recovery_required",
                                                     "recovery_action": "complete_ownership_recovery"})
    if domain not in {entry.domain for entry in cutover_manifest()}:
        raise HTTPException(status_code=404, detail={"code": "composition_cutover_domain_invalid",
                                                     "recovery_action": None})
    if body.confirm_domain != domain:
        raise HTTPException(status_code=422, detail={"code": "composition_confirmation_required",
                                                     "recovery_action": "confirm_domain"})
    try:
        receipt = await transition_domain(domain, body.target, host=cordis_host,
                                          actor=f"operator:{operator.principal.principal_id}",
                                          expected_epoch=body.expected_epoch, reason=body.reason)
    except CompositionBindingError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.reason_code, "recovery_action": "reload_domains"}) from exc
    return {"transition": receipt, "domains": await _domains()}
