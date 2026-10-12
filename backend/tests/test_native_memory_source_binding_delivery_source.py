"""Source-only: a fabricated PK certificate cannot pay as a Source reader."""

import pytest


def test_unissued_exact_certificate_denies_delivery_before_debit():
    from src.memory.header_bounds import HeaderBoundsError, HeaderCertificate, HeaderReadBudget
    from src.runtime_plugins.memory_producer import _native_memory_source_binding_delivery_pay

    budget = HeaderReadBudget()
    certificate = object.__new__(HeaderCertificate)
    before = budget.remaining
    with pytest.raises(HeaderBoundsError, match="memory_source_binding_delivery_numeric_owner_changed"):
        _native_memory_source_binding_delivery_pay(budget, certificate, None, "task", 7)
    assert budget.remaining == before


def test_absent_original_budget_skips_delivery_sizing():
    from src.runtime_plugins.memory_producer import _native_memory_source_binding_delivery_pay

    assert _native_memory_source_binding_delivery_pay(None, None, None, None, None) is None
