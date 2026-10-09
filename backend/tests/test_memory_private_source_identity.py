"""No fabricated positive issuer: reconstruction/copy denial before SQL."""
from dataclasses import replace

import pytest

from src.runtime_plugins.dispatch import OriginalServiceInvocation, NativeServiceBlocked
from src.runtime_plugins.memory_producer import (
    _NativeMemoryDispatchSource, _MemoryOwnerSourceOperation,
    _validate_memory_dispatch_source, _validate_memory_owner_source,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("copied", [False, True])
async def test_reconstructed_memory_source_never_issues_authority(copied):
    source = _NativeMemoryDispatchSource(object(), object(), object(), object(), object())
    if copied:
        source = replace(source)
    scope = OriginalServiceInvocation({}, object(), "unissued", native_memory_source=source)
    # SQL/transaction cannot be touched: this is absence of source issuance,
    # not a fabricated passing host/claim/canonical-effect fixture.
    with pytest.raises(NativeServiceBlocked, match="native_memory_actual_source_unavailable"):
        await _validate_memory_dispatch_source(None, None, scope)


@pytest.mark.asyncio
async def test_serialized_original_scope_cannot_replace_private_memory_source():
    scope = OriginalServiceInvocation({}, object(), "unissued",
                                      native_memory_source={"source": "memory", "verified": True})
    with pytest.raises(NativeServiceBlocked, match="native_memory_actual_source_unavailable"):
        await _validate_memory_dispatch_source(None, None, scope)


@pytest.mark.asyncio
async def test_reconstructed_operation_never_certifies_an_effect():
    source = _NativeMemoryDispatchSource(object(), object(), object(), object(), object())
    operation = _MemoryOwnerSourceOperation(source, object(), None, object(), object(), None)
    with pytest.raises(NativeServiceBlocked, match="native_memory_actual_operation_unavailable"):
        await _validate_memory_owner_source(None, None, operation, effect=object())
