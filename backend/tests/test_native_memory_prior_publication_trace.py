"""Original private readers retain old raw cost when the next output is smaller.

These are real private-file readers and an unissued-owner negative, not a
fabricated NativeMemory Source, ledger or publication permission fixture.
"""
import json
from types import MappingProxyType
import pytest
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, _trace_memory_numeric_charges
from src.workspace.production import (
    ProductionWorkspace, prepare_lifecycle_directory, read_accounting_checkpoint,
    read_lifecycle_receipt, lifecycle_receipt_path,
)
from src.workspace.accounting_witness import _native_memory_prior_publication_numeric_entries


def test_original_checkpoint_trace_retains_old_128_delta_bytes(tmp_path):
    workspace = ProductionWorkspace(host_root=tmp_path)
    prepare_lifecycle_directory(workspace)
    delta = [{"table_id": "workflow_run_states", "key": f"prior-{index}",
        "before_digest": "a" * 64, "after_digest": "b" * 64} for index in range(128)]
    old = {"schema_version": 2, "secret_values_included": False, "composition_delta": delta}
    # Whitespace is retained in the actual reader's resource cost as well.
    raw = json.dumps(old, indent=2).encode()
    path = workspace.lifecycle_directory / "accounting-checkpoint.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    future = json.dumps({"schema_version": 2, "secret_values_included": False,
        "composition_delta": []}, separators=(",", ":")).encode()
    budget = HeaderReadBudget()
    with _trace_memory_numeric_charges(budget) as trace:
        actual = read_accounting_checkpoint(workspace, header_budget=budget)
    assert actual == old
    assert len(raw) > len(future)
    assert trace[0] == (None, len(raw) + 1)
    assert len(trace) == 4
    for appearance, amount in trace[1:]:
        assert appearance[:2] == ("composition-publication", "checkpoint")
        assert amount >= len(raw)
        assert amount > len(future)


def test_original_receipt_trace_includes_raw_decode_and_three_codec_appearances(tmp_path):
    workspace = ProductionWorkspace(host_root=tmp_path)
    prepare_lifecycle_directory(workspace)
    old = {"secret_values_included": False, "retained_note": "ł\n" * 200}
    raw = json.dumps(old, indent=2, ensure_ascii=False).encode()
    path = lifecycle_receipt_path(workspace)
    path.write_bytes(raw)
    path.chmod(0o600)
    budget = HeaderReadBudget()
    with _trace_memory_numeric_charges(budget) as trace:
        actual = read_lifecycle_receipt(workspace, header_budget=budget)
    assert actual == old
    assert trace[:2] == [(None, len(raw) + 1), (("composition-publication", "receipt", "decode"), len(raw))]
    assert [appearance[-1] for appearance, _ in trace[2:]] == ["json-parse", "materialization", "return"]


def test_prior_read_numeric_tuple_cannot_create_an_original_owner():
    class Unissued:
        @property
        def source(self):
            raise AssertionError("unissued numeric request reached private Source")
    with pytest.raises(HeaderBoundsError, match="memory_publication_numeric_shape_unavailable"):
        _native_memory_prior_publication_numeric_entries(Unissued(), object(), certificate=object(),
            retention_widths=MappingProxyType({}), publication_widths=MappingProxyType({}), prior_reads=())
