"""No retained locator or reconstructed object can issue a Memory Source."""
from types import SimpleNamespace

import pytest

from src.runtime_plugins.dispatch import NativeServiceBlocked
from src.workspace.accounting_witness import (
    ProductionWorkspaceReconciliationError,
    _NativeMemoryRetention,
    _append_native_memory_original,
    begin_native_memory_effect,
    capture_native_memory_retention,
    prepare_native_memory_lifecycle,
    validate_native_memory_retention,
)


@pytest.mark.asyncio
async def test_reconstructed_operation_cannot_begin_or_capture():
    # Rejection precedes any db, plan or effect access. Poison values expose
    # attempts to substitute byte-shaped inputs for the original operation.
    class Poison:
        def __getattr__(self, name):
            raise AssertionError("forged Source reached " + name)
    poison = Poison()
    for function, args, kwargs in (
        (begin_native_memory_effect, (poison, poison, "claim", poison),
            {"source_operation": object(), "prepared": poison}),
        (capture_native_memory_retention, (poison, poison, "claim", poison),
            {"source_operation": object(), "prepared": poison, "result": poison}),
    ):
        with pytest.raises(NativeServiceBlocked, match="native_memory_actual_operation_unavailable"):
            await function(*args, **kwargs)


@pytest.mark.asyncio
async def test_cold_locator_and_copied_claim_cannot_complete():
    run = SimpleNamespace(run_identity="unissued-retention-job")
    sealed = {"retention": {"profile": "native-composition-memory-reference.v2",
        "invocation_ref": run.run_identity,
        "original_checkpoint_id": "memory:original-reference.v2",
        "original_checkpoint_digest": "0" * 64}}
    with pytest.raises(ProductionWorkspaceReconciliationError, match="memory_retention_source_unavailable"):
        await validate_native_memory_retention(object(), run, sealed)
    with pytest.raises(NativeServiceBlocked, match="native_memory_actual_claim_source_required"):
        await prepare_native_memory_lifecycle(object(), run, original_claim=object(),
            pending_changes={}, header_budget=object())


def test_unregistered_retention_never_reaches_ledger():
    class PoisonLedger:
        def _checked(self):
            raise AssertionError("unregistered retention reached ledger")
    retention = _NativeMemoryRetention(SimpleNamespace(), object(), object(), PoisonLedger(),
        {"run_identity": "forged"}, (), (), ())
    with pytest.raises(ProductionWorkspaceReconciliationError, match="memory_retention_source_unavailable"):
        retention.payload()


def test_original_append_preserves_unrelated_lexemes():
    raw = ' \n[ {"checkpoint_id":"old", "payload":"\\u0061"} \t]\n'
    original = {"checkpoint_id": "memory:original-reference.v2", "safe": True,
        "state_digest": "0" * 64, "payload": {}}
    appended = _append_native_memory_original(raw, original)
    assert appended.startswith(' \n[ {"checkpoint_id":"old", "payload":"\\u0061"} \t,')
    assert appended.endswith("]\n")
    with pytest.raises(ProductionWorkspaceReconciliationError, match="memory_retention_original_already_exists"):
        _append_native_memory_original(appended, original)
