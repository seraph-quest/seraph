"""Authenticated API for the durable guardian inbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ConfigDict, Field
from pydantic import BaseModel as PydanticBaseModel

from src.auth.service import AuthenticatedOperator
from src.guardian import inbox as inbox_service


router = APIRouter(prefix="/guardian/inbox")


class GuardianInboxActionRequest(PydanticBaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["accept_followup", "snooze", "dismiss"]
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    until: datetime | None = None
    reason: str | None = Field(default=None, max_length=500)


def _operator(request: Request) -> AuthenticatedOperator:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    session_id = str(getattr(operator, "session_id", "") or "").strip()
    if not isinstance(operator, AuthenticatedOperator) or not principal_id or not session_id:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    if (
        not bool(getattr(principal, "authenticated", False))
        or bool(getattr(principal, "revoked", False))
        or str(getattr(principal, "session_id", "") or "") != session_id
        or str(getattr(principal, "operator_session_id", "") or "") != session_id
    ):
        raise HTTPException(status_code=401, detail={"code": "session_unavailable"})
    return operator


def _raise_inbox_error(exc: inbox_service.InboxError) -> None:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.recovery_action:
        detail["recovery_action"] = exc.recovery_action
    if exc.current_revision is not None:
        detail["current_revision"] = exc.current_revision
    if exc.state:
        detail["state"] = exc.state
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


@router.get("")
async def list_guardian_inbox(
    request: Request,
    limit: int = Query(default=50, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=512),
) -> dict[str, Any]:
    operator = _operator(request)
    try:
        return await inbox_service.list_owned_items(
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            limit=limit,
            cursor=cursor,
            operator=operator,
        )
    except inbox_service.InboxError as exc:
        _raise_inbox_error(exc)


@router.get("/{item_id}")
async def get_guardian_inbox_item(item_id: str, request: Request) -> dict[str, Any]:
    operator = _operator(request)
    try:
        return await inbox_service.get_owned_item(
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            item_id=item_id,
            operator=operator,
        )
    except inbox_service.InboxError as exc:
        _raise_inbox_error(exc)


@router.post("/{item_id}/actions")
async def apply_guardian_inbox_action(
    item_id: str,
    body: GuardianInboxActionRequest,
    request: Request,
) -> dict[str, Any]:
    operator = _operator(request)
    try:
        return await inbox_service.apply_action(
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            item_id=item_id,
            action=body.action,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
            until=body.until,
            reason=body.reason,
        )
    except inbox_service.InboxError as exc:
        _raise_inbox_error(exc)


__all__ = ["GuardianInboxActionRequest", "router"]
