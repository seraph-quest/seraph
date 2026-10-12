"""Source-only negatives: fabricated records never become readback facts."""
import pytest

@pytest.mark.parametrize("method", ["begin", "row", "finish"])
def test_unissued_readback_scan_record_denies_before_numeric_storage(method):
    from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
    from src.work_board import review
    budget = HeaderReadBudget()
    unissued = object.__new__(review._MemoryReportDependencyNumericInventory)
    budget._native_memory_report_dependency_numeric = unissued
    before = budget.remaining
    with pytest.raises(HeaderBoundsError, match="memory_report_readback_numeric_owner_changed"):
        if method == "begin":
            review._memory_report_readback_scan_begin(budget)
        elif method == "row":
            review._memory_report_readback_scan_row(budget, [], None, None, 0)
        else:
            review._memory_report_readback_scan_finish(budget, [])
    assert budget.remaining == before
    assert not hasattr(unissued, "_readback_numeric_pending")
    assert not hasattr(unissued, "_readback_numeric_scan")


def test_readback_scan_without_original_resource_frame_allocates_no_facts():
    from src.work_board import review
    assert review._memory_report_readback_scan_begin(None) is None
    assert review._memory_report_readback_scan_row(None, None, None, None, 0) is None
    assert review._memory_report_readback_scan_finish(None, None) is None
