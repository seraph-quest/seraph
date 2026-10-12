"""A numeric vector and an armed ContextVar cannot manufacture a CAS owner."""
from types import MappingProxyType, SimpleNamespace

import pytest

from src.memory.header_bounds import HeaderBoundsError
from src.workspace.accounting_witness import (
    CompositionSessionGuard,
    ProductionWorkspaceReconciliationError,
    _ARMED_MEMORY_PUBLICATION,
    _NativeMemorySourcePublication,
    _native_memory_private_journal_numeric_entries,
)


class _UnissuedOperation:
    @property
    def source(self):
        raise AssertionError("unissued numeric request read private Source")


class _UndeliveredRetention:
    def _checked(self):
        raise AssertionError("unregistered publication read retention")


@pytest.mark.parametrize("kind", ["capture", "completion", "affected-unknown"])
def test_unissued_journal_forecast_denies_before_source_or_caller_widths(kind):
    with pytest.raises(HeaderBoundsError, match="memory_private_journal_owner_unavailable"):
        _native_memory_private_journal_numeric_entries(_UnissuedOperation(), object(),
            certificate=object(), retention_widths=MappingProxyType({}), kind=kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["capture", "completion", "affected-unknown"])
async def test_typed_but_unregistered_armed_plan_denies_before_retention_or_json(kind):
    import asyncio
    db = SimpleNamespace()
    statement = object()
    plan = _NativeMemorySourcePublication(_UndeliveredRetention(), db, object(), statement,
        {}, {}, object(), object(), object(), 0, asyncio.current_task(), (), kind)
    token = _ARMED_MEMORY_PUBLICATION.set(plan)
    try:
        with pytest.raises(ProductionWorkspaceReconciliationError,
                match="memory_private_journal_owner_changed"):
            CompositionSessionGuard._check_private_journal(SimpleNamespace(db=db),
                "invalid-json", "invalid-json", run_id="not-issued", statement=statement)
    finally:
        _ARMED_MEMORY_PUBLICATION.reset(token)
