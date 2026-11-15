"""Authenticated operator API for the provider-free Telegram transport."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from src.extensions.telegram_transport import TelegramTransportError, default_telegram_transport
from src.security.trust_contract import AuthorityGrant
from src.work_board.channel_capture import TelegramCaptureSelection, ChannelTaskIngress, ChannelAction


router = APIRouter()


class TelegramPairBody(BaseModel):
    operator_id: int = Field(..., gt=0)
    chat_id: int = Field(..., gt=0)
    expires_at: datetime | None = None
    # Write-only at the API boundary.  The adapter stores it in the scoped
    # vault and exposes only a fingerprint in status/readback responses.
    bot_token: SecretStr | None = Field(default=None, min_length=1, max_length=4096, repr=False)


class TelegramConsentBody(BaseModel):
    boundary: str = Field(..., min_length=1, max_length=64)


class TelegramPollBody(BaseModel):
    limit: int = Field(default=100, ge=1, le=100)
    timeout_seconds: float | None = Field(default=None, gt=0, le=60)


class TelegramOutboundBody(BaseModel):
    content: str = Field(..., min_length=1, max_length=50_000)
    idempotency_key: str | None = Field(default=None, max_length=256)
    chat_id: int | None = Field(default=None, gt=0)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    kind: str = Field(default="text", pattern="^(text|voice)$")
    attachment_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=8)


class TelegramReconcileBody(BaseModel):
    resolution: Literal["retry", "delivered"]
    external_message_id: str | int | None = Field(default=None, max_length=256)


class TelegramTaskNoticeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")


def _operator(request: Request) -> tuple[str, str, object]:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    owner = str(getattr(principal, "principal_id", "") or "").strip()
    session = str(getattr(operator, "session_id", "") or "").strip()
    grants = {str(getattr(item, "value", item)) for item in getattr(principal, "grants", ())}
    if not operator or not principal or not getattr(principal, "authenticated", False) or getattr(principal, "revoked", False) or not owner or not session:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    if AuthorityGrant.INGRESS.value not in grants:
        raise HTTPException(status_code=403, detail={"code": "telegram_ingress_forbidden"})
    return owner, session, operator


def _error(exc: TelegramTransportError) -> HTTPException:
    status = 403 if "authority" in exc.code or "forbidden" in exc.code else 404 if exc.code.endswith("not_found") else 409 if "conflict" in exc.code or "not_active" in exc.code or "revoked" in exc.code else 422
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@router.get("/telegram/status")
async def telegram_status(request: Request) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.status(owner_principal_id=owner, operator_session_id=session)
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.put("/telegram/capture-selection")
async def select_channel_capture(body: TelegramCaptureSelection, request: Request) -> dict:
    from src.work_board.channel_capture import select_telegram_capture
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.repository import BoardError
    from src.api.work_board import _raise_board_error
    owner, session, _ = _operator(request)
    try:
        return await select_telegram_capture(default_telegram_transport,
            WorkBoardOwner(principal_id=owner, session_id=session), body)
    except BoardError as exc:
        _raise_board_error(exc)
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/task-ingress")
async def capture_channel_task(body: ChannelTaskIngress, request: Request) -> dict:
    from src.work_board.repository import BoardError
    from src.api.work_board import _raise_board_error
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.capture_task(body,
            owner_principal_id=owner, operator_session_id=session)
    except BoardError as exc:
        _raise_board_error(exc)
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/task-actions")
async def control_channel_task(body: ChannelAction, request: Request) -> dict:
    from src.work_board.repository import BoardError
    from src.api.work_board import _raise_board_error
    from src.extensions.telegram_task_controls import TelegramTaskControls
    owner, session, _ = _operator(request)
    try:
        return await TelegramTaskControls(default_telegram_transport).action(body,
            owner_principal_id=owner, operator_session_id=session)
    except BoardError as exc:
        _raise_board_error(exc)
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.get("/telegram/output-review")
async def inspect_channel_output(handle: str, request: Request) -> dict:
    from src.work_board.repository import BoardError
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.channel_capture import resolve_output_handle
    from src.api.work_board import _raise_board_error
    from src.db.engine import get_session
    owner, session, _ = _operator(request)
    try:
        async with default_telegram_transport._lock:
            async with get_session() as db:
                return await resolve_output_handle(db, WorkBoardOwner(principal_id=owner, session_id=session),
                    handle, adapter=default_telegram_transport)
    except BoardError as exc:
        _raise_board_error(exc)
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/pair")
async def pair_telegram(body: TelegramPairBody, request: Request) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.pair(
            owner_principal_id=owner,
            operator_session_id=session,
            operator_id=body.operator_id,
            chat_id=body.chat_id,
            token=body.bot_token.get_secret_value() if body.bot_token is not None else None,
            expires_at=body.expires_at,
        )
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/consent")
async def grant_telegram_consent(body: TelegramConsentBody, request: Request) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.grant_consent(
            owner_principal_id=owner,
            operator_session_id=session,
            boundary=body.boundary,
        )
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/consent/{boundary}/revoke")
async def revoke_telegram_consent(boundary: str, request: Request) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.revoke_consent(
            owner_principal_id=owner,
            operator_session_id=session,
            boundary=boundary,
        )
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/revoke")
async def revoke_telegram(request: Request) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.revoke(owner_principal_id=owner, operator_session_id=session)
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/updates")
async def receive_telegram_update(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """Receive an update only through the authenticated injected seam.

    There is intentionally no public webhook route.  A future external
    adapter must authenticate and normalize updates before calling this same
    method; this branch's default transport remains recording-only.
    """

    from src.work_board.repository import BoardError
    from src.api.work_board import _raise_board_error
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.ingest_update(
            payload,
            owner_principal_id=owner,
            operator_session_id=session,
        )
    except BoardError as exc:
        _raise_board_error(exc)
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/tasks/{task_id}/notice")
async def telegram_task_notice(task_id: str, body: TelegramTaskNoticeBody, request: Request):
    """Explicit neutral notice; an ID or chat message never grants task authority."""
    from src.extensions.telegram_task_controls import TelegramTaskControls
    from src.work_board.repository import BoardError
    from src.api.work_board import _raise_board_error
    owner, session, _ = _operator(request)
    try:
        return await TelegramTaskControls(default_telegram_transport).notice(task_id,
            owner_principal_id=owner, operator_session_id=session,
            expected_revision=body.expected_revision, idempotency_key=body.idempotency_key)
    except BoardError as exc:
        _raise_board_error(exc)
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.get("/telegram/task-review")
async def telegram_task_review(request: Request, handle: str) -> dict:
    from src.extensions.telegram_task_controls import TelegramTaskControls
    from src.work_board.repository import BoardError
    from src.api.work_board import _raise_board_error
    owner, session, _ = _operator(request)
    try:
        return await TelegramTaskControls(default_telegram_transport).read_exact_review(handle,
            owner_principal_id=owner, operator_session_id=session)
    except BoardError as exc:
        _raise_board_error(exc)
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/poll")
async def poll_telegram(body: TelegramPollBody, request: Request) -> dict[str, Any]:
    """Run one authenticated, bounded long-poll through the injected seam."""
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.poll_updates(
            owner_principal_id=owner,
            operator_session_id=session,
            limit=body.limit,
            timeout_seconds=body.timeout_seconds,
        )
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/outbox")
async def enqueue_telegram(body: TelegramOutboundBody, request: Request) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.enqueue_outbound(
            body.content,
            owner_principal_id=owner,
            operator_session_id=session,
            idempotency_key=body.idempotency_key,
            chat_id=body.chat_id,
            session_id=body.session_id,
            kind=body.kind,
            attachment_refs=body.attachment_refs,
        )
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.get("/telegram/outbox")
async def list_telegram_outbox(request: Request) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return {"outbox": await default_telegram_transport.list_outbox(owner_principal_id=owner, operator_session_id=session)}
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/outbox/{outbox_id}/deliver")
async def deliver_telegram(outbox_id: str, request: Request) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.deliver(
            outbox_id,
            owner_principal_id=owner,
            operator_session_id=session,
        )
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.get("/telegram/outbox/{outbox_id}")
async def read_telegram_outbox(outbox_id: str, request: Request) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.read_outbox(
            outbox_id,
            owner_principal_id=owner,
            operator_session_id=session,
        )
    except TelegramTransportError as exc:
        raise _error(exc) from exc


@router.post("/telegram/outbox/{outbox_id}/reconcile")
async def reconcile_telegram_outbox(
    outbox_id: str,
    body: TelegramReconcileBody,
    request: Request,
) -> dict[str, Any]:
    owner, session, _ = _operator(request)
    try:
        return await default_telegram_transport.reconcile_outbox(
            outbox_id,
            owner_principal_id=owner,
            operator_session_id=session,
            resolution=body.resolution,
            external_message_id=body.external_message_id,
        )
    except TelegramTransportError as exc:
        raise _error(exc) from exc


__all__ = ["router"]
