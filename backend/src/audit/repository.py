"""Persistence helpers for structured audit events."""

import json
from datetime import datetime
from typing import Any

from sqlmodel import select, col

from src.db.engine import get_session
from src.db.models import AuditEvent
from src.db.session_refs import ensure_sessions_exist


class AuditRepository:
    async def log_event(
        self,
        *,
        event_type: str,
        summary: str,
        session_id: str | None = None,
        actor: str = "agent",
        tool_name: str | None = None,
        risk_level: str = "low",
        policy_mode: str = "full",
        details: dict[str, Any] | None = None,
        composition_authority_check=None,
    ) -> AuditEvent:
        async with get_session() as db:
            retained_native = db.info.get("composition_read_guard") is not None
            if retained_native:
                from src.runtime_plugins.ownership import begin_native_writer
                await begin_native_writer(db, owner="native_ingress")
            if composition_authority_check is not None:
                if not retained_native:
                    from src.work_board.repository import _begin_sqlite_immediate
                    await _begin_sqlite_immediate(db)
                await composition_authority_check(db)
            await ensure_sessions_exist(db, [session_id], retained_native=retained_native)
            event = AuditEvent(
                session_id=session_id,
                actor=actor,
                event_type=event_type,
                tool_name=tool_name,
                risk_level=risk_level,
                policy_mode=policy_mode,
                summary=summary,
                details_json=json.dumps(details) if details is not None else None,
            )
            db.add(event)
            await db.flush()
            db.expunge(event)
            return event

    async def list_events(
        self,
        *,
        limit: int = 20,
        session_id: str | None = None,
        since: datetime | None = None,
    ) -> list[dict]:
        limit = min(max(limit, 1), 500)
        async with get_session() as db:
            stmt = select(AuditEvent).order_by(col(AuditEvent.created_at).desc()).limit(limit)
            if session_id:
                stmt = stmt.where(AuditEvent.session_id == session_id)
            if since:
                stmt = stmt.where(AuditEvent.created_at >= since)

            result = await db.execute(stmt)
            events = result.scalars().all()
            return [
                {
                    "id": event.id,
                    "session_id": event.session_id,
                    "actor": event.actor,
                    "event_type": event.event_type,
                    "tool_name": event.tool_name,
                    "risk_level": event.risk_level,
                    "policy_mode": event.policy_mode,
                    "summary": event.summary,
                    "details": json.loads(event.details_json) if event.details_json else None,
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]


audit_repository = AuditRepository()
