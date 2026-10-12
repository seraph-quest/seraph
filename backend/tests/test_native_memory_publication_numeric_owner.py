"""Numeric payloads cannot create the original registered publication owner."""
from types import MappingProxyType
import pytest
from src.memory.header_bounds import HeaderBoundsError
from src.workspace.accounting_witness import (
    _native_memory_source_publication_numeric_entries,
    _native_memory_engine_publication_numeric_entries,
)

class _UnissuedOperation:
    @property
    def source(self):
        raise AssertionError("unissued numeric request reached private Source")

@pytest.mark.parametrize("helper", ["sql", "engine"])
def test_unissued_owner_denies_before_source_or_numeric_field_delivery(helper):
    owner = _UnissuedOperation()
    widths = MappingProxyType({})
    call = (_native_memory_source_publication_numeric_entries if helper == "sql"
        else _native_memory_engine_publication_numeric_entries)
    kwargs = {"kind": "capture"} if helper == "sql" else {"publication_widths": widths}
    with pytest.raises(HeaderBoundsError, match="memory_publication_numeric_shape_unavailable"):
        call(owner, object(), certificate=object(), retention_widths=widths, **kwargs)
