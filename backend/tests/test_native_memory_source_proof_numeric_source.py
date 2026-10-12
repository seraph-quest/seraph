"""Source-only: numerical proof observations cannot issue their own owner."""

import pytest


def test_unissued_report_record_denies_proof_width_storage_before_row_access():
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
    from src.runtime_plugins.memory_producer import _native_memory_source_proof_numeric_observe
    from src.work_board.review import _MemoryReportDependencyNumericInventory

    budget = HeaderReadBudget()
    record = object.__new__(_MemoryReportDependencyNumericInventory)
    budget._native_memory_report_dependency_numeric = record
    before = budget.remaining
    with pytest.raises(HeaderBoundsError, match="memory_source_proof_numeric_owner_changed"):
        _native_memory_source_proof_numeric_observe(budget, None, None, None)
    assert budget.remaining == before
    assert not hasattr(record, "_source_proof_numeric_dimensions")


@pytest.mark.parametrize("value", ["", "a" * 513, "a\n", "é", "a b", None, 7])
def test_scalar_safe_ref_sizing_rejects_non_original_ref_grammar(value):
    from src.runtime_plugins.memory_producer import _native_memory_source_safe_ref_numeric

    assert _native_memory_source_safe_ref_numeric(value) is False


def test_scalar_safe_ref_sizing_covers_original_punctuation_and_boundary():
    from src.runtime_plugins.memory_producer import _native_memory_source_safe_ref_numeric

    assert _native_memory_source_safe_ref_numeric("A09_.:/-z") is True
    assert _native_memory_source_safe_ref_numeric("a" * 512) is True
