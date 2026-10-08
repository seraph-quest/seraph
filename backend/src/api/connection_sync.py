"""Owner-only connected-source API helpers shared by existing source routers."""
from __future__ import annotations

from fastapi import HTTPException, Request

from src.integrations.connection_sync import ConnectionSyncService, SyncError, SyncReadback, SyncRecovery, SyncRequest
from src.integrations.gmail_read import GmailReadError


def runtime(request: Request) -> ConnectionSyncService:
    service = getattr(request.app.state, "connection_sync_runtime", None)
    if not isinstance(service, ConnectionSyncService) or not service.started:
        raise HTTPException(status_code=503, detail={"code": "connection_sync_inactive", "message": "Connected source synchronization is inactive", "recovery_action": "restart_backend"})
    return service


async def handle(request: Request, connection_id: str, provider: str, operation: str, *, opaque_id: str | None = None, job_id: str | None = None):
    from src.api import calendar, mail
    from src.integrations.google_calendar import CalendarIntegrationError
    from src.workflows.job_runtime import DurableJobError
    from src.work_board.repository import BoardError
    operator = mail._operator(request)
    owner = mail._owner(operator)
    service = runtime(request)
    try:
        if operation == "reconcile":
            body = await mail._json_body(request, SyncRecovery)
            return await service.reconcile(owner, connection_id, str(job_id), body.expected_job_revision, expected_cursor_revision=body.expected_cursor_revision, authenticated_token_hash=operator._token_hash, expected_provider=provider)
        from src.db import engine
        async with engine.get_session() as db:
            if provider == "gmail":
                connection = await mail._connection_for(db, owner, connection_id)
            else:
                connection = await calendar._connection_for(db, owner, connection_id)
            if connection.service != ("gmail_readonly" if provider == "gmail" else "calendar_readonly"):
                raise SyncError("connection_sync_not_found", "The selected source connection is unavailable", status_code=404)
        if operation == "sync":
            body = await mail._json_body(request, SyncRequest)
            if body.input.connection_ref.id != connection_id or body.input.source_scope.provider != provider:
                raise SyncError("connection_sync_selection_invalid", "The sync input does not match this source connection", status_code=422)
            return await service.synchronize(owner, body, authenticated_token_hash=operator._token_hash)
        if operation == "status":
            return await service.status(owner, connection_id)
        if operation == "read":
            await mail._json_body(request, SyncReadback)
            return await service.read_item(owner, connection_id, str(opaque_id))
        raise RuntimeError("Unsupported connected-source API operation")
    except (GmailReadError, CalendarIntegrationError, BoardError) as exc:
        raise HTTPException(status_code=int(getattr(exc, "status_code", 409)), detail={"code": getattr(exc, "code", "connection_sync_blocked"), "message": str(exc), "recovery_action": getattr(exc, "recovery_action", "reload_connection")}) from exc
    except DurableJobError as exc:
        raise HTTPException(status_code=409, detail={"code": "connection_sync_durable_conflict", "message": "The source synchronization requires canonical job recovery", "recovery_action": "reconcile_existing_sync"}) from exc
