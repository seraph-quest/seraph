"""Witness sizing grammar only; these tests issue no Source or reservation."""

import json

import pytest

from src.memory.header_bounds import _native_memory_ascii_string_width
from src.workspace.accounting_witness import _native_memory_row_witness_numeric_entries


@pytest.mark.parametrize("key", ["run-1", 'run-"\\', "run-é", "run-😀"])
def test_row_witness_bounds_cover_original_wire_record(key):
    table = "workflow_run_states"
    reference = {"table": table, "key": key}
    reference_width = len(json.dumps(reference, ensure_ascii=True,
        separators=(",", ":")).encode("ascii"))
    entries = _native_memory_row_witness_numeric_entries(table, key,
        encoded_upper=1048576, reference_upper=reference_width)
    assert tuple(item[0][3] for item in entries) == (
        "hash-input", "digest", "reference", "record")
    record = {"ref": reference, "tuple_digest": "f" * 64,
        "encoded_bytes": 1048576}
    actual = len(json.dumps(record, ensure_ascii=True,
        separators=(",", ":")).encode("ascii"))
    assert entries[3][1] == actual
    assert entries[2][1] == reference_width
    assert reference_width == (17 + _native_memory_ascii_string_width(table)
        + _native_memory_ascii_string_width(key))
    planning = _native_memory_row_witness_numeric_entries(table, key,
        encoded_upper=1048576, reference_upper=reference_width,
        _construction_only=True)
    returned = len(json.dumps(entries, ensure_ascii=True,
        separators=(",", ":")).encode("ascii"))
    assert planning >= returned
