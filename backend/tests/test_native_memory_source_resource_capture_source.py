"""Original resource capture trust-boundary cases; execution is not released."""

import pytest


def test_unregistered_dispatch_source_denies_before_budget_or_context_changes():
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, _MEMORY_CHARGE_TRACE
    from src.runtime_plugins import memory_producer as producer

    source = object.__new__(producer._NativeMemoryDispatchSource)
    budget = HeaderReadBudget()
    remaining = budget.remaining
    active = producer._MEMORY_REPORT_TRACE_ACTIVE.get()
    trace = _MEMORY_CHARGE_TRACE.get()
    with pytest.raises(HeaderBoundsError, match="memory_report_source_resource_owner_changed"):
        producer._begin_memory_report_source_numeric_trace(None, source, budget, "owner-session")
    assert budget.remaining == remaining
    assert producer._MEMORY_REPORT_TRACE_ACTIVE.get() is active
    assert _MEMORY_CHARGE_TRACE.get() is trace


def test_unregistered_exact_collector_denies_before_owner_field_access():
    from src.memory.header_bounds import HeaderBoundsError
    from src.runtime_plugins import memory_producer as producer

    collector = object.__new__(producer._NativeMemoryReportTraceCollector)
    with pytest.raises(HeaderBoundsError, match="memory_report_numeric_trace_changed"):
        collector._checked()


def test_absent_source_does_not_attach_any_success_lineage():
    from src.runtime_plugins.memory_producer import _commit_memory_report_source_numeric_trace

    assert _commit_memory_report_source_numeric_trace(None, None) is None


@pytest.mark.asyncio
async def test_original_forget_preparation_needs_no_report_capture(async_db, monkeypatch):
    from src.db.models import Memory
    from src.memory import m5
    from src.work_board.repository import _begin_sqlite_immediate
    from tests.test_native_memory_owner import source, _owner_admission, common, OWNER

    await source(async_db, monkeypatch)
    async with async_db() as db:
        db.add(Memory(id="capture-free-forget", source_session_id=OWNER.session_id,
            content="Original forget record", summary="Original summary"))
    admission = _owner_admission({**common("memory.forget"), "record_ref": "capture-free-forget",
        "mode": "archive", "privacy_boundary": "private", "reason": None, "prepared_reason": None})
    assert admission.report_source is None
    async with async_db() as db:
        await _begin_sqlite_immediate(db)
        plan = await m5.prepare_native_memory_mutation(db, admission=admission,
            native_memory_report_source=None, native_memory_source=None)
        assert not plan.consumed
        assert (await db.get(Memory, "capture-free-forget")).content == "Original forget record"
