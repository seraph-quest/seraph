"""Final storage arithmetic without issuing a fabricated positive Source."""

import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("label", [
    ("native-memory-cold", "native-memory:" + "f" * 64, "binding"),
    ("quoted", '"\\' * 64, (None, 2**63 - 1)),
    ("unicode", "😀" * 32, (True, False)),
])
def test_complete_label_scalar_covers_original_canonical_storage(label):
    from src.workspace.accounting_witness import _native_memory_complete_label_planning_width

    expected = len(json.dumps(label, ensure_ascii=True, separators=(",", ":")).encode())
    upper = _native_memory_complete_label_planning_width(label,
        operation=None, prepared=None, certificate=None)
    assert upper >= expected


def test_complete_label_rejects_opaque_caller_object_before_output():
    from src.memory.header_bounds import HeaderBoundsError
    from src.workspace.accounting_witness import _native_memory_complete_label_planning_width

    with pytest.raises(HeaderBoundsError, match="memory_accounting_payload_label_unavailable"):
        _native_memory_complete_label_planning_width(("caller", object()),
            operation=None, prepared=None, certificate=None)


def test_complete_label_rejects_unissued_private_slot():
    from src.memory.header_bounds import HeaderBoundsError
    from src.workspace.accounting_witness import (
        _native_memory_complete_label_planning_width, _NativeMemoryFutureIntegerSlot)

    # A copied shape is not a genuine slot; no positive authority is fabricated.
    slot = object.__new__(_NativeMemoryFutureIntegerSlot)
    with pytest.raises((HeaderBoundsError, AttributeError)):
        _native_memory_complete_label_planning_width(slot,
            operation=None, prepared=None, certificate=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [None, SimpleNamespace(
    _native_memory_source_capacity_facts=(object(), object(), object(), {}),
    _native_memory_retention_numeric_facts=(object(), object(), object(), {}))])
async def test_complete_parent_rejects_missing_or_copied_custody_before_owner_calls(monkeypatch, guard):
    from src.memory.header_bounds import HeaderBoundsError
    from src.runtime_plugins import memory_producer
    from src.workspace.accounting_witness import _native_memory_effect_numeric_schedule

    def unowned_call(*args, **kwargs):
        pytest.fail("unissued parent reached owner sizing or original read")

    monkeypatch.setattr(memory_producer, "_native_memory_dispatch_validation_numeric_owner", unowned_call)
    with pytest.raises(HeaderBoundsError, match="memory_numeric_forecast_owner_changed"):
        await _native_memory_effect_numeric_schedule(
            SimpleNamespace(info={"composition_guard": guard}), object(), object(), object(),
            existing_references=(), before_row=(), certificate=object())
