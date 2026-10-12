"""Private accounting/REPORT traces deny unissued collectors and subclasses."""
import pytest

from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, _MEMORY_CHARGE_TRACE
from src.workspace.accounting_witness import _NativeMemoryAccountingTraceCollector
from src.runtime_plugins.memory_producer import _NativeMemoryReportTraceCollector


def _collector_type(kind):
    return (_NativeMemoryAccountingTraceCollector if kind == "accounting"
        else _NativeMemoryReportTraceCollector)


@pytest.mark.parametrize("kind", ["accounting", "report"])
def test_unissued_exact_collector_denies_before_trace_pair_or_original_debit(kind):
    # Deliberately bypass construction only as a hostile input: no Source,
    # registry entry, certificate, grant or successful observation is issued.
    collector = object.__new__(_collector_type(kind))
    budget = HeaderReadBudget()
    before = budget.remaining
    token = _MEMORY_CHARGE_TRACE.set((budget, collector))
    try:
        with pytest.raises(HeaderBoundsError):
            budget.debit(7, appearance=("accounting-recognition-test", "unissued"))
    finally:
        _MEMORY_CHARGE_TRACE.reset(token)
    assert budget.remaining == before
    assert not hasattr(collector, "entries")


@pytest.mark.parametrize("kind", ["accounting", "report"])
def test_collector_subclass_cannot_supply_append_charge_callback(kind):
    called = []
    class PoisonSubclass(_collector_type(kind)):
        def append_charge(self, appearance, amount):
            called.append(True)
            raise AssertionError("unissued subclass callback must never be invoked")
    collector = object.__new__(PoisonSubclass)
    budget = HeaderReadBudget()
    before = budget.remaining
    token = _MEMORY_CHARGE_TRACE.set((budget, collector))
    try:
        # Exacttype rejection retains the original list-trace fallback. This
        # unsupported object has no list.append and cannot reach debit.
        with pytest.raises(AttributeError):
            budget.debit(7, appearance=("accounting-recognition-test", "subclass"))
    finally:
        _MEMORY_CHARGE_TRACE.reset(token)
    assert called == []
    assert budget.remaining == before
