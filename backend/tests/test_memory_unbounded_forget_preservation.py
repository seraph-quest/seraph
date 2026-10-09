"""Authenticated ordinary Forget preserves full bytes; no native readiness claim."""
import json

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select, text

from config.settings import settings
from src.api.memory import router
from src.agent.session import SessionManager
from src.auth.middleware import OperatorAuthMiddleware
from src.auth.service import create_session
from src.db.models import AuditEvent, Memory, MemoryStatus
from src.memory.header_bounds import AUDIT_EVENT, HeaderBoundsError, MAX_BYTES, preflight_exact_rows
from src.memory.repository import memory_repository


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_real_authenticated_forget_preserves_overlimit_reason_and_audit(async_db, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-bounded-memory-proof")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    token, operator = await create_session()
    await SessionManager().get_or_create(operator.session_id,
                                         owner_principal_id=operator.principal.principal_id)
    created = await memory_repository.create_memory(content="Private original",
                                                     source_session_id=operator.session_id)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(router, prefix="/api")
    reason = "r" * MAX_BYTES
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                 cookies={settings.operator_auth_cookie_name: token},
                                 headers={"Origin": "http://localhost:3001"}) as client:
        response = await client.post(f"/api/memory/{created.memory_id}/forget",
                                     json={"reason": reason, "mode": "archive"})
        assert response.status_code == 200, response.text[:300]
    async with async_db() as db:
        memory = await db.get(Memory, created.memory_id)
        assert memory.status == MemoryStatus.archived and memory.content == "Private original"
        assert json.loads(memory.metadata_json)["operator_control"]["last_reason"] == reason
        audit = await db.scalar(select(AuditEvent).where(AuditEvent.id == response.json()["audit_event_id"]))
        assert audit.actor == operator.principal.principal_id and audit.session_id == operator.session_id
        assert json.loads(audit.details_json)["reason"] == reason
        audit_id = audit.id
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        statements = []
        connection = await db.connection()
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                await preflight_exact_rows(db, AUDIT_EVENT, (audit_id,), MAX_BYTES)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert all("SELECT audit_events." not in statement for statement in statements)
        assert any("octet_length" in statement and '"details_json"' in statement for statement in statements)
