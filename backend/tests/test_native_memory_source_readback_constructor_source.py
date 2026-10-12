"""Prospective trust-boundary tests; no synthetic successful Source."""
import pytest

from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer


@pytest.mark.parametrize("scalar", [False, True])
def test_readback_numeric_constructor_rejects_unissued_exact_operation(scalar):
    unissued = object.__new__(memory_producer._MemoryOwnerSourceOperation)
    with pytest.raises(HeaderBoundsError, match="memory_report_dependency_numeric_owner_changed"):
        memory_producer.original_native_memory_source_readback_digest_numeric_schedule(
            unissued, None, certificate=None, facts=None, retention_widths=None,
            _construction_only=scalar)


def test_readback_numeric_constructor_rejects_integer_mode_before_owner():
    with pytest.raises(HeaderBoundsError, match="memory_source_readback_constructor_mode_changed"):
        memory_producer.original_native_memory_source_readback_digest_numeric_schedule(
            None, None, certificate=None, facts=None, retention_widths=None,
            _construction_only=1)
