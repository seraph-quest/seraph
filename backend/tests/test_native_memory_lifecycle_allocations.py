"""Closed original SQL bind/codec dimensions; no Source or grant fixtures."""
import pytest

from src.memory.header_bounds import HeaderBoundsError, WRS_BY_RUN, _native_memory_ascii_string_width
from src.workspace.accounting_witness import (
    _native_memory_lifecycle_allocation_amounts,
    _native_memory_lifecycle_allocation_numeric_entries,
    _native_memory_selected_delta_preimage,
    _native_memory_retention_locator_numeric_entries,
    _native_memory_source_rows_numeric_entries,
)
from src.workflows.job_runtime import _canonical


def test_completion_delta_covers_escaped_context_and_original_ten_column_binds():
    context = '{"quoted":"\\\"é\\\\"}'
    pending = {"status": "succeeded", "result_digest": "a" * 64,
        "result_summary": "Native Memory owner result committed", "failure_reason": None,
        "finished_at": "2026-10-12 01:02:03.123456", "updated_at": "2026-10-12 01:02:03.123456",
        "lease_owner": None, "lease_expires_at": None, "revision": 2**63 - 1,
        "checkpoint_context_json": context}
    widths = {"context_upper": _native_memory_ascii_string_width(context),
        "own_upper": 12000, "own_raw_upper": 8000, "refs_upper": 1000,
        "current_upper": 5000, "rows_upper": 2000}
    amounts = _native_memory_lifecycle_allocation_amounts("job", widths)
    delta = _native_memory_selected_delta_preimage([], [(WRS_BY_RUN, "job", pending)], [])
    assert amounts["delta-pending-bind-copy"] >= len(_canonical(pending).encode("utf-8"))
    assert amounts["delta-object"] >= len(_canonical(delta).encode("utf-8"))
    assert amounts["delta-canonical"] == amounts["delta-object"]
    assert amounts["delta-utf8"] == amounts["delta-object"]
    assert set(delta["updates"][0]["columns"]) == set(pending)


@pytest.mark.parametrize("owner", [
    _native_memory_lifecycle_allocation_numeric_entries,
    _native_memory_retention_locator_numeric_entries,
    _native_memory_source_rows_numeric_entries,
])
def test_allocation_helpers_reject_unissued_source_without_reading_private_dimensions(owner):
    class Poison:
        def __getattr__(self, name):
            raise AssertionError("unissued source read " + name)
    value = Poison()
    with pytest.raises(HeaderBoundsError):
        owner(value, value, certificate=value, facts=value, retention_widths=value)
