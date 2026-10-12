"""Source-only authored checks; execution awaits the offline release packet."""
import json

import pytest

from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, WRS_BY_RUN
from src.workspace.accounting_witness import (
    _native_memory_closed_value_width,
    _native_memory_current_tuple_upper,
    _native_memory_lexical_token_upper,
    _native_memory_prepare_current_row,
)


@pytest.mark.parametrize("text", ['quote"\\\n', "\x7f", "ż😀"])
def test_current_tuple_numeric_fee_preserves_original_serializer_formula(text):
    row = {name: None for name in WRS_BY_RUN.columns}
    row.update(run_identity="native-memory:" + "a" * 32,
        checkpoint_receipts_json='[{"retained":"' + text + '"}]', revision=9)
    skeleton = json.dumps(["native-composition-memory.v1", WRS_BY_RUN.table,
        row["run_identity"], [[name, None] for name in WRS_BY_RUN.columns]],
        ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    original_fee = len(skeleton) + sum(
        6 * len(value.encode("utf-8")) + 2 if type(value) is str
        else 32 if value is not None else 0 for value in row.values())
    assert _native_memory_current_tuple_upper(row,
        len(row["checkpoint_receipts_json"].encode("utf-8"))) == original_fee


def test_closed_payload_width_covers_original_canonical_encoded_units():
    value = {"ref": {"table": "workflow_run_states", "key": "ż😀\x7f"},
        "tuple_digest": "0" * 64, "encoded_bytes": 1048576,
        "optional": None, "safe": True, "rows": []}
    actual = json.dumps(value, ensure_ascii=True, separators=(",", ":"),
        sort_keys=True, allow_nan=False).encode("utf-8")
    assert _native_memory_closed_value_width(value) >= len(actual)


def test_closed_payload_numeric_width_rejects_unsupported_objects():
    class Poison:
        def __str__(self):
            raise AssertionError("numeric capacity must never stringify an object")
    with pytest.raises(HeaderBoundsError, match="memory_numeric_shape_unavailable"):
        _native_memory_closed_value_width({"not_a_closed_scalar": Poison()})


@pytest.mark.parametrize("value", ['escape"\\\n', "\x7f", "ż😀", {"x": [1, True, None]}])
def test_lexical_precharge_covers_each_actual_decoder_token(value):
    token = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    raw = "[" + token + ",null]"
    upper = _native_memory_lexical_token_upper(raw, 1)
    decoded, _ = json.JSONDecoder().raw_decode(raw, 1)
    encoded = json.dumps(decoded, ensure_ascii=True, separators=(",", ":")).encode()
    assert upper >= len(encoded)


def test_current_exhaustion_precedes_initial_payload_copy(monkeypatch):
    import copy
    row = {name: None for name in WRS_BY_RUN.columns}
    row.update(run_identity="native-memory:" + "a" * 32, checkpoint_receipts_json="[]")
    budget = HeaderReadBudget()
    budget.debit(budget.remaining)
    def forbidden_copy(_value):
        raise AssertionError("an exhausted frame must stop before payload deepcopy")
    monkeypatch.setattr(copy, "deepcopy", forbidden_copy)
    with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
        _native_memory_prepare_current_row(row, {"rows": []}, header_budget=budget)
