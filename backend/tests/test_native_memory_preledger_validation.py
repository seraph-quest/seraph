"""Pre-ledger rejection checks; no fabricated positive Source or ledger."""

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [None, SimpleNamespace(
    _native_memory_source_capacity_facts=(),
    _native_memory_retention_numeric_facts=())])
async def test_preledger_rejects_missing_custody_before_prepared_allocations(monkeypatch, guard):
    from src.memory import header_bounds
    from src.memory import m5

    def unpaid_builder(*args, **kwargs):
        pytest.fail("unissued owner reached prepared forecast construction")

    async def unpaid_validation(*args, **kwargs):
        pytest.fail("unissued owner reached plan validation or writer-state query")

    monkeypatch.setattr(m5, "original_native_memory_prepared_construction_numeric_upper", unpaid_builder)
    monkeypatch.setattr(m5, "original_native_memory_prepared_validation_numeric_schedule", unpaid_builder)
    monkeypatch.setattr(m5, "validate_native_memory_mutation_plan", unpaid_validation)
    db = SimpleNamespace(info={"composition_guard": guard})
    with pytest.raises(header_bounds.HeaderBoundsError,
            match="memory_preledger_validation_owner_changed"):
        await header_bounds._validate_native_memory_preledger_plan(
            db, object(), object(), object())


def test_ledger_storage_cannot_be_enrolled_by_an_external_fee_list(monkeypatch):
    from src.memory import header_bounds
    from src.workspace import accounting_witness

    def unpaid_width(*args, **kwargs):
        pytest.fail("external caller reached ledger sizing")

    monkeypatch.setattr(accounting_witness, "_native_memory_capacity_label_width", unpaid_width)
    with pytest.raises(header_bounds.HeaderBoundsError,
            match="memory_numeric_ledger_storage_owner_changed"):
        header_bounds._prepay_native_memory_ledger_storage(
            SimpleNamespace(info={}), object(), object(), object(),
            (("effect", ("caller-fee",), 1),))
