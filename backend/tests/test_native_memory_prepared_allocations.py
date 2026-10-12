"""Real sealed constructor plans, without issuing an execution Source."""
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from src.db.models import AuditEvent, Memory, MemoryProposal, MemoryStatus
from src.memory import m5
from src.work_board.repository import _begin_sqlite_immediate
from tests.test_native_memory_owner import source, proposal_candidate, _owner_admission, common, OWNER


@pytest.mark.asyncio
async def test_real_prepared_forecast_is_readonly_and_rejects_copied_plan(async_db, monkeypatch):
    _, task, attempt = await source(async_db, monkeypatch)
    admission = await proposal_candidate(async_db, task, attempt)
    async with async_db() as db:
        await _begin_sqlite_immediate(db)
        plan = await m5.prepare_native_memory_mutation(db, admission=admission)
        before = await m5._native_memory_writer_state(db)
        remaining = admission.header_budget.remaining
        schedule = m5.original_native_memory_prepared_numeric_schedule(plan)
        actual = json.dumps(plan.original_result(), ensure_ascii=True, separators=(",", ":")).encode()
        result_upper = next(upper for phase, name, upper in schedule
            if phase == "effect" and name == ("memory-prepared", "result-copy"))
        assert len(actual) <= result_upper
        assert await m5._native_memory_writer_state(db) == before
        assert admission.header_budget.remaining == remaining
        assert not plan.consumed and not db.new and not db.dirty and not db.deleted
        assert not list((await db.execute(select(MemoryProposal))).scalars())
        assert not list((await db.execute(select(AuditEvent))).scalars())
        with pytest.raises(ValueError, match="prepared_memory_mutation_changed"):
            m5.original_native_memory_prepared_numeric_schedule(replace(plan))
        outer = m5.original_native_memory_prepared_validation_numeric_schedule(plan)
        assert any(name == ("memory-prepared", "plan-validation-values") for _, name, _ in outer)
        completed = m5.original_native_memory_prepared_validation_numeric_schedule(plan, consumed=True)
        assert not any(name == ("memory-prepared", "plan-validation-values") for _, name, _ in completed)
        with pytest.raises(ValueError, match="prepared_memory_mutation_changed"):
            m5.original_native_memory_prepared_validation_numeric_schedule(replace(plan))


@pytest.mark.asyncio
async def test_real_forget_plan_forecasts_one_result_copy_and_preserves_owner_result(async_db, monkeypatch):
    await source(async_db, monkeypatch)
    async with async_db() as db:
        db.add(Memory(id="prepared-forget", source_session_id=OWNER.session_id,
            content="Original record", summary="Original summary"))
    admission = _owner_admission({**common("memory.forget"), "record_ref": "prepared-forget",
        "mode": "archive", "privacy_boundary": "private", "reason": None, "prepared_reason": None})
    async with async_db() as db:
        await _begin_sqlite_immediate(db)
        plan = await m5.prepare_native_memory_mutation(db, admission=admission)
        before = await m5._native_memory_writer_state(db)
        schedule = m5.original_native_memory_prepared_numeric_schedule(plan)
        copies = [(name, upper) for _, name, upper in schedule
            if name in {("memory-prepared", "result-copy"), ("memory-prepared", "forget-result-copy")}]
        assert len(copies) == 1 and copies[0][0] == ("memory-prepared", "forget-result-copy")
        assert len(json.dumps(plan.original_result(), ensure_ascii=True, separators=(",", ":")).encode()) <= copies[0][1]
        assert await m5._native_memory_writer_state(db) == before
        from src.memory.control import _forget_memory_in_session
        result = await _forget_memory_in_session(db, owner_session_id=OWNER.session_id,
            memory_id="prepared-forget", native_prepared_mutation=plan)
        assert result == plan.original_result()
        assert plan.consumed
    async with async_db() as db:
        assert (await db.get(Memory, "prepared-forget")).status is MemoryStatus.archived
        assert (await db.get(AuditEvent, plan.audit_event.id)).event_type == "memory_forgotten"
