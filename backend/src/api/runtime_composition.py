"""Operator control of Cordis runtime domain owners (#1019).

Authenticated HTTP only (the global OperatorAuthMiddleware enforces the Origin
check and the session cookie); no agent tool, scheduler or workflow can reach
it. A request needs an explicit confirmation that repeats the domain name and
only persists an audited intent: the next boot applies it as a fenced owner
transition at a higher epoch, before chat is served, so a restart is required.
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
    from src.runtime_plugins.cutover import cutover_manifest, pending_intents
    from src.runtime_plugins.ownership import inventory
    async with get_session() as db:
        rows = await inventory(db)
    pending = await pending_intents()
    return [{"runtime_domain": entry.domain, "prerequisites": list(entry.prerequisites),
             "owner_kind": rows[entry.domain].owner_kind if entry.domain in rows else None,
             "epoch": rows[entry.domain].epoch if entry.domain in rows else None,
             "state": rows[entry.domain].state if entry.domain in rows else None,
             "pending_target": pending[entry.domain]["target"] if entry.domain in pending else None}
            for entry in cutover_manifest()]


@router.get("/runtime/composition/domains")
async def list_domains(request: Request):
    _operator(request)
    return {"domains": await _domains()}


@router.post("/runtime/composition/domains/{domain}/owner")
async def transition_domain_owner(request: Request, domain: str, body: OwnerTransition):
    from src.runtime_plugins.cutover import cutover_manifest, record_operator_intent
    from src.runtime_plugins.ownership import CompositionBindingError
    operator = _operator(request)
    if domain not in {entry.domain for entry in cutover_manifest()}:
        raise HTTPException(status_code=404, detail={"code": "composition_cutover_domain_invalid",
                                                     "recovery_action": None})
    if body.confirm_domain != domain:
        raise HTTPException(status_code=422, detail={"code": "composition_confirmation_required",
                                                     "recovery_action": "confirm_domain"})
    try:
        intent = await record_operator_intent(domain, body.target, expected_epoch=body.expected_epoch,
                                              actor=f"operator:{operator.principal.principal_id}", reason=body.reason)
    except CompositionBindingError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.reason_code, "recovery_action": "reload_domains"}) from exc
    return {"intent": {key: intent[key] for key in ("intent_id", "runtime_domain", "target", "expected_epoch")},
            "restart_required": True, "domains": await _domains()}
