"""Persistence helpers for structured audit events."""

import json
from datetime import datetime
from typing import Any

from sqlmodel import select, col

from src.db.engine import get_session
from src.db.models import AuditEvent
from src.db.session_refs import ensure_sessions_exist


_PREPARED_AUDIT_EXPECTED_FIELDS = (
    "session_id", "actor", "event_type", "tool_name", "risk_level",
    "policy_mode", "summary", "details_json",
)


def _native_prepared_audit_expected_upper(event):
    from src.memory.m5 import _native_prepared_value_width
    return 2 + 7 + sum(_native_prepared_value_width(name) + 1
        + _native_prepared_value_width(getattr(event, name))
        for name in _PREPARED_AUDIT_EXPECTED_FIELDS)


def _native_prepared_audit_details_upper(value):
    """Default json.dumps separators add one space per colon and comma."""
    from src.memory.m5 import _native_prepared_value_width
    def spaces(item):
        if type(item) is dict:
            return len(item) + max(0, len(item) - 1) + sum(spaces(v) for v in item.values())
        if type(item) in {tuple, list}:
            return max(0, len(item) - 1) + sum(spaces(v) for v in item)
        return 0
    return _native_prepared_value_width(value) + spaces(value)


def original_native_memory_prepared_audit_numeric_schedule(plan):
    """One original prepared audit validation; capacity, never permission."""
    from src.memory.m5 import (PreparedNativeMemoryMutation, _NATIVE_MUTATION_PLANS,
        _NATIVE_MUTATION_PLAN_SEAL, _native_prepared_value_width)
    if (type(plan) is not PreparedNativeMemoryMutation
            or _NATIVE_MUTATION_PLANS.get(id(plan)) is not plan
            or plan._seal is not _NATIVE_MUTATION_PLAN_SEAL or plan.consumed):
        raise ValueError("prepared_memory_mutation_changed")
    event = plan.audit_event
    if event is None:
        return ()
    if type(event) is not AuditEvent:
        raise ValueError("prepared_audit_event_changed")
    expected_upper = _native_prepared_audit_expected_upper(event)
    if event.details_json is None:
        return (("effect", ("memory-prepared-audit", "expected-map"), expected_upper),)
    return (("effect", ("memory-prepared-audit", "details-json"),
            _native_prepared_value_width(event.details_json)),
        ("effect", ("memory-prepared-audit", "expected-map"), expected_upper))


class AuditRepository:
    def _prepare_event(
        self, *, event_type: str, summary: str, session_id: str | None = None,
        actor: str = "agent", tool_name: str | None = None, risk_level: str = "low",
        policy_mode: str = "full", details: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> AuditEvent:
        """Construct the original complete transient row; confer no authority."""
        return AuditEvent(
            session_id=session_id, actor=actor, event_type=event_type,
            tool_name=tool_name, risk_level=risk_level, policy_mode=policy_mode,
            summary=summary, details_json=json.dumps(details) if details is not None else None,
            **({"id": event_id} if event_id is not None else {}),
        )

    async def _log_event_in_session(
        self, db, *, event_type: str, summary: str, session_id: str | None = None,
        actor: str = "agent", tool_name: str | None = None, risk_level: str = "low",
        policy_mode: str = "full", details: dict[str, Any] | None = None,
        event_id: str | None = None, flush: bool = True, _prepared_event: AuditEvent | None = None,
    ) -> AuditEvent:
        """Caller owns authority, Session FK, one writer and operation receipt."""
        if _prepared_event is None:
            event = self._prepare_event(event_type=event_type, summary=summary,
                session_id=session_id, actor=actor, tool_name=tool_name,
                risk_level=risk_level, policy_mode=policy_mode, details=details,
                event_id=event_id)
        else:
            from sqlalchemy import inspect
            from src.memory.header_bounds import (_MEMORY_LEDGER_PHASE,
                _NativeMemoryNumericLedger, HeaderBoundsError)
            active = _MEMORY_LEDGER_PHASE.get()
            if active is not None:
                from src.memory.m5 import (_native_prepared_numeric_frame,
                    _native_prepared_value_width)
                ledger, _phase = active
                if type(ledger) is not _NativeMemoryNumericLedger:
                    raise HeaderBoundsError("memory_prepared_numeric_owner_changed")
                frame = _native_prepared_numeric_frame(db, ledger.prepared)
                if (type(_prepared_event) is not AuditEvent
                        or ledger.prepared.audit_event is not _prepared_event
                        or session_id != _prepared_event.session_id
                        or actor != _prepared_event.actor
                        or event_type != _prepared_event.event_type
                        or tool_name != _prepared_event.tool_name
                        or risk_level != _prepared_event.risk_level
                        or policy_mode != _prepared_event.policy_mode
                        or summary != _prepared_event.summary
                        or ((details is None) != (_prepared_event.details_json is None))):
                    raise ValueError("prepared_audit_event_changed")
                if details is not None:
                    details_upper = _native_prepared_value_width(_prepared_event.details_json)
                    if _native_prepared_audit_details_upper(details) > details_upper:
                        raise ValueError("prepared_audit_event_changed")
                    frame.debit(details_upper,
                        appearance=("memory-prepared-audit", "details-json"))
            serialized_details = json.dumps(details) if details is not None else None
            if active is not None:
                if serialized_details != _prepared_event.details_json:
                    raise ValueError("prepared_audit_event_changed")
                frame.debit(_native_prepared_audit_expected_upper(_prepared_event),
                    appearance=("memory-prepared-audit", "expected-map"))
            expected = {"session_id": session_id, "actor": actor, "event_type": event_type,
                "tool_name": tool_name, "risk_level": risk_level, "policy_mode": policy_mode,
                "summary": summary, "details_json": serialized_details}
            if (type(_prepared_event) is not AuditEvent or not inspect(_prepared_event).transient
                or (event_id is not None and _prepared_event.id != event_id)
                or any(getattr(_prepared_event, key) != value for key, value in expected.items())):
                raise ValueError("prepared_audit_event_changed")
            event = _prepared_event
        db.add(event)
        if flush:
            await db.flush()
        return event

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
            event = await self._log_event_in_session(db, event_type=event_type,
                summary=summary, session_id=session_id, actor=actor, tool_name=tool_name,
                risk_level=risk_level, policy_mode=policy_mode, details=details)
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
