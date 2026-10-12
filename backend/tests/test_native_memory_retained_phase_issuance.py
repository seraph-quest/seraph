"""Unissued owner denial before construction; Source tests remain unrun."""
import pytest


class _UntouchableDimensions:
    def __getattribute__(self, name):
        raise AssertionError("unissued owner reached numerical dimensions")

    def __iter__(self):
        raise AssertionError("unissued owner iterated future contributions")

    def __getitem__(self, key):
        raise AssertionError("unissued owner selected a future contribution")


@pytest.mark.parametrize("scalar", [False, True])
def test_unissued_exact_operation_cannot_construct_or_rephase_future_entries(scalar):
    from src.runtime_plugins.memory_producer import _MemoryOwnerSourceOperation
    from src.memory.header_bounds import HeaderBoundsError
    from src.workspace.accounting_witness import _native_memory_retained_phase_numeric_entries
    # Exact Python class construction is insufficient: no original registry
    # issuance, sealed plan, current frame or Source capability exists here.
    unissued = object.__new__(_MemoryOwnerSourceOperation)
    poison = _UntouchableDimensions()
    with pytest.raises(HeaderBoundsError, match="memory_dispatch_validation_numeric_owner_changed"):
        _native_memory_retained_phase_numeric_entries(unissued, None,
            certificate=poison, facts=poison, retention_widths=poison,
            _construction_only=scalar)


def test_unissued_operation_cannot_invoke_body_or_witness_constructor(monkeypatch):
    from src.runtime_plugins.memory_producer import _MemoryOwnerSourceOperation
    from src.memory.header_bounds import HeaderBoundsError
    from src.workspace import accounting_witness as witness

    def forbidden(*args, **kwargs):
        raise AssertionError("constructor ran before original Source authentication")

    monkeypatch.setattr(witness, "_native_memory_generic_body_construction_numeric_upper", forbidden)
    monkeypatch.setattr(witness, "_native_memory_row_witness_collection_numeric_entries", forbidden)
    poison = _UntouchableDimensions()
    with pytest.raises(HeaderBoundsError, match="memory_dispatch_validation_numeric_owner_changed"):
        witness._native_memory_issue_body_numeric_contributions(
            object.__new__(_MemoryOwnerSourceOperation), None,
            certificate=poison, facts=poison, retention_widths=poison)
