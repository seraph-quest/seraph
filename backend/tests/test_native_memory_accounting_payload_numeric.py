"""Accounting owner negative component checks; no Source/effect issuance."""

import pytest


@pytest.mark.parametrize("construction_only", [False, True])
def test_unissued_accounting_owner_denies_before_vector_sizing(monkeypatch, construction_only):
    from src.memory.header_bounds import HeaderBoundsError
    from src.workspace import accounting_witness as witness

    def unpaid_label_walk(*args, **kwargs):
        pytest.fail("accounting constructor reached label sizing for an unissued owner")

    # Keep the original Source gate: no authority gate, frame, certificate,
    # callback or returned fact is mocked into existence.
    monkeypatch.setattr(witness, "_native_memory_accounting_label_planning_width", unpaid_label_walk)
    with pytest.raises(HeaderBoundsError, match="memory_dispatch_validation_numeric_owner_changed"):
        witness._native_memory_accounting_payload_numeric_entries(
            object(), object(), certificate=object(), facts=object(),
            retention_widths=object(), _construction_only=construction_only)


@pytest.mark.parametrize("label", [
    ("accounting-witness", "native-memory:" + "a" * 64, "return"),
    ("metadata-prequery", ("schema-cookie", 1, "main")),
    ("accounting-ledger", 'operation-"\\\n😀', None),
])
def test_accounting_label_upper_covers_actual_json_encoding(label):
    import json
    from src.workspace import accounting_witness as witness

    encoded = len(json.dumps(label, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
    assert witness._native_memory_accounting_label_planning_width(label) >= encoded
