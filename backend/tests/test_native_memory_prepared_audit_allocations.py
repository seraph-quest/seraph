"""Genuine sealed audit rows: numerical forecast and original validation."""
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from src.audit.repository import audit_repository, original_native_memory_prepared_audit_numeric_schedule
from src.db.models import AuditEvent, Memory, MemoryStatus
from src.memory import m5
from src.work_board.repository import _begin_sqlite_immediate
from tests.test_native_memory_owner import source, _owner_admission, common, OWNER


async def _forget_plan(async_db, monkeypatch):
    await source(async_db, monkeypatch)
    async with async_db() as db:
        db.add(Memory(id="prepared-audit-memory", source_session_id=OWNER.session_id,
            content="Original audit subject", summary="Original audit summary"))
    return _owner_admission({**common("memory.forget"), "record_ref": "prepared-audit-memory",
        "mode": "archive", "privacy_boundary": "private", "reason": None, "prepared_reason": None})


@pytest.mark.asyncio
async def test_genuine_prepared_audit_forecast_covers_both_original_allocations_and_preserves_write(async_db, monkeypatch):
    admission = await _forget_plan(async_db, monkeypatch)
    async with async_db() as db:
        await _begin_sqlite_immediate(db)
        plan = await m5.prepare_native_memory_mutation(db, admission=admission)
        event = plan.audit_event
        before = await m5._native_memory_writer_state(db)
        remaining = admission.header_budget.remaining
        entries = original_native_memory_prepared_audit_numeric_schedule(plan)
        amounts = {appearance: upper for phase, appearance, upper in entries if phase == "effect"}
        details = json.loads(event.details_json)
        expected = {"session_id": event.session_id, "actor": event.actor,
            "event_type": event.event_type, "tool_name": event.tool_name,
            "risk_level": event.risk_level, "policy_mode": event.policy_mode,
            "summary": event.summary, "details_json": json.dumps(details)}
        assert set(amounts) == {("memory-prepared-audit", "details-json"),
            ("memory-prepared-audit", "expected-map")}
        assert len(json.dumps(details).encode()) <= amounts[("memory-prepared-audit", "details-json")]
        assert len(json.dumps(expected, separators=(",", ":")).encode()) <= amounts[("memory-prepared-audit", "expected-map")]
        assert await m5._native_memory_writer_state(db) == before
        assert admission.header_budget.remaining == remaining
        assert not plan.consumed and not db.new and not db.dirty and not db.deleted
        with pytest.raises(ValueError, match="prepared_memory_mutation_changed"):
            original_native_memory_prepared_audit_numeric_schedule(replace(plan))
        from src.memory.control import _forget_memory_in_session
        result = await _forget_memory_in_session(db, owner_session_id=OWNER.session_id,
            memory_id="prepared-audit-memory", native_prepared_mutation=plan)
        assert result == plan.original_result() and plan.consumed
    async with async_db() as db:
        assert (await db.get(Memory, "prepared-audit-memory")).status is MemoryStatus.archived
        persisted = await db.get(AuditEvent, event.id)
        assert persisted.details_json == event.details_json
        assert persisted.event_type == "memory_forgotten"


@pytest.mark.asyncio
async def test_original_prepared_audit_mismatch_never_inserts_event(async_db, monkeypatch):
    admission = await _forget_plan(async_db, monkeypatch)
    async with async_db() as db:
        await _begin_sqlite_immediate(db)
        plan = await m5.prepare_native_memory_mutation(db, admission=admission)
        event = plan.audit_event
        before = await m5._native_memory_writer_state(db)
        with pytest.raises(ValueError, match="prepared_audit_event_changed"):
            await audit_repository._log_event_in_session(db, actor=event.actor,
                event_type=event.event_type, summary=event.summary,
                session_id=event.session_id, tool_name=event.tool_name,
                risk_level=event.risk_level, policy_mode=event.policy_mode,
                details={"different_original_details": True}, _prepared_event=event)
        assert not plan.consumed and not db.new and not db.dirty and not db.deleted
        assert await m5._native_memory_writer_state(db) == before
        assert not list((await db.execute(select(AuditEvent))).scalars())


@pytest.mark.asyncio
async def test_genuine_prepared_audit_quote_heavy_mismatch_cannot_insert_larger_expected_value(async_db, monkeypatch):
    admission = await _forget_plan(async_db, monkeypatch)
    async with async_db() as db:
        await _begin_sqlite_immediate(db)
        plan = await m5.prepare_native_memory_mutation(db, admission=admission)
        event = plan.audit_event
        from src.audit.repository import _native_prepared_audit_details_upper
        planned = m5._native_prepared_value_width(event.details_json)
        changed = None
        for count in range(1, planned + 1):
            candidate = {"x": '"' * count}
            serialized = json.dumps(candidate)
            if (_native_prepared_audit_details_upper(candidate) <= planned
                    and m5._native_prepared_value_width(serialized) > planned):
                changed = candidate
                break
        assert changed is not None
        before = await m5._native_memory_writer_state(db)
        original_dumps = json.dumps
        serialized_calls = []
        def observe_dumps(value, *args, **kwargs):
            result = original_dumps(value, *args, **kwargs)
            if value is changed:
                serialized_calls.append(len(result.encode()))
            return result
        monkeypatch.setattr(json, "dumps", observe_dumps)
        # This real owner test injects no Source/ledger or fake capacity payer.
        # Active Source debit-order execution awaits the complete original factory.
        with pytest.raises(ValueError, match="prepared_audit_event_changed"):
            await audit_repository._log_event_in_session(db, actor=event.actor,
                event_type=event.event_type, summary=event.summary,
                session_id=event.session_id, tool_name=event.tool_name,
                risk_level=event.risk_level, policy_mode=event.policy_mode,
                details=changed, _prepared_event=event)
        assert len(serialized_calls) == 1
        assert not db.new and not db.dirty and not db.deleted and not plan.consumed
        assert await m5._native_memory_writer_state(db) == before
        assert not list((await db.execute(select(AuditEvent))).scalars())
