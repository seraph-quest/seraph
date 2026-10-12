"""Unissued Runtime numerical observers must never touch a private owner."""
import pytest

from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
from src.runtime_plugins.ownership import (
    _NativeMemoryReportRuntimeTraceCollector,
    _native_memory_report_runtime_owner,
)
from src.work_board.review import _MEMORY_REPORT_PRINCIPAL_SCOPE


class PoisonOwner:
    def __getattribute__(self, name):
        raise AssertionError("private owner touched before registration")


class CopiedCollector(_NativeMemoryReportRuntimeTraceCollector):
    pass


@pytest.mark.parametrize("collector_type", [
    _NativeMemoryReportRuntimeTraceCollector, CopiedCollector,
])
def test_unissued_runtime_collector_denies_before_owner_or_debit(collector_type):
    collector = object.__new__(collector_type)
    collector.owner = PoisonOwner()
    budget = HeaderReadBudget()
    before = budget.remaining
    with pytest.raises(HeaderBoundsError, match="memory_report_runtime_numeric_trace_changed"):
        collector.append_charge(("original-private-appearance",), 1)
    assert budget.remaining == before


def test_foreign_report_scope_denies_before_private_connection():
    token = _MEMORY_REPORT_PRINCIPAL_SCOPE.set(PoisonOwner())
    try:
        with pytest.raises(HeaderBoundsError, match="memory_report_runtime_numeric_owner_changed"):
            _native_memory_report_runtime_owner(PoisonOwner())
    finally:
        _MEMORY_REPORT_PRINCIPAL_SCOPE.reset(token)
