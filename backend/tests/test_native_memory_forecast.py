"""The numeric factory cannot turn attribute-shaped data into original Source."""
import pytest
import json

from src.memory.header_bounds import _reserve_native_memory_effect_ledger
from src.runtime_plugins.dispatch import NativeServiceBlocked


class PoisonOwner:
    def __init__(self, touches):
        object.__setattr__(self, "touches", touches)

    def __getattribute__(self, name):
        if name == "touches":
            return object.__getattribute__(self, name)
        object.__getattribute__(self, "touches").append(name)
        raise AssertionError("untrusted owner callback accessed")


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute_shaped", [False, True])
async def test_forged_forecast_source_denies_before_driver_or_capacity_callbacks(attribute_shaped):
    touches = []
    poison = PoisonOwner(touches)
    operation = poison if attribute_shaped else object()
    # There is deliberately no fake certificate, prepared plan or SQL driver.
    # Those capacity operands cannot authorize examination of untrusted data.
    with pytest.raises(NativeServiceBlocked) as caught:
        await _reserve_native_memory_effect_ledger(poison, poison, operation, poison,
            existing_references=poison, before_row=poison, certificate=poison)
    assert caught.value.reason_code == "native_memory_actual_operation_unavailable"
    assert touches == []


@pytest.mark.asyncio
async def test_programme_numeric_factory_denies_attribute_shaped_owners_before_callbacks():
    from src.memory.header_bounds import (
        HeaderBoundsError, _reserve_native_programme_start_numeric_ledger)
    touches = []
    poison = PoisonOwner(touches)
    with pytest.raises(HeaderBoundsError) as caught:
        await _reserve_native_programme_start_numeric_ledger(poison, poison, poison, poison, poison)
    assert caught.value.code == "programme_start_numeric_owner_changed"
    assert touches == []


@pytest.mark.parametrize("summary", ["ordinary ASCII", "DEL:\x7f", "mixed:\x00\n\"\\é😀"])
def test_original_audit_row_numeric_width_preserves_fee_without_serializing(summary, monkeypatch):
    from src.audit.repository import audit_repository
    from src.memory.header_bounds import COMPOSITION_DESCRIPTORS, _native_memory_resolved_row_widths
    from src.workspace.accounting_witness import (
        _native_memory_transient_sql_row, _native_memory_row_bytes)
    event = audit_repository._prepare_event(event_type="memory_learning_proposed",
        summary=summary, actor="operator:width", details={"summary": summary})
    descriptor = COMPOSITION_DESCRIPTORS["audit_events"]
    row = _native_memory_transient_sql_row(event)
    encoded = _native_memory_row_bytes(descriptor, event.id, row)
    skeleton = json.dumps(["native-composition-memory.v1", descriptor.table, event.id,
        [[name, None] for name in descriptor.columns]], ensure_ascii=True, separators=(",", ":")).encode()
    original_fee = len(skeleton) + 128 + sum(
        6 * len(value.encode("utf-8")) + 2 - 4 if type(value) is str
        else 20 - 4 if type(value) is int else 64 - 4 if type(value) is float else 0
        for value in row.values())
    def refuse_serializer(*args, **kwargs):
        raise AssertionError("numeric forecast constructed JSON before preallocation")
    monkeypatch.setattr(json, "dumps", refuse_serializer)
    header_upper, tuple_upper = _native_memory_resolved_row_widths(descriptor, event.id, row)
    assert header_upper == original_fee
    assert tuple_upper >= len(encoded)


def test_numeric_width_rejects_unencodable_surrogate_without_utf8_or_json_copy():
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    with pytest.raises(HeaderBoundsError) as caught:
        _native_memory_ascii_string_width("\ud800")
    assert caught.value.code == "memory_numeric_scalar_unavailable"


@pytest.mark.parametrize("reason", [None, "original_projection_unavailable"])
def test_current_numeric_shape_covers_original_json_counts_and_escaping(reason):
    from src.memory.header_bounds import MAX_BYTES, _native_memory_current_payload_upper
    from src.workspace.accounting_witness import MEMORY_REFERENCE_PROFILE, MEMORY_CURRENT_CHECKPOINT
    key = "capacity-only:\x7f\"😀"
    refs = (("workflow_run_states", key),)
    payload_upper, record_upper = _native_memory_current_payload_upper(refs, (),
        method="memory.propose", unknown_reason=reason, original_present=reason is None)
    # Capacity grammar only: this is not published or supplied as authority.
    ref = {"table": "workflow_run_states", "key": key}
    payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "original_checkpoint_digest": "a" * 64 if reason is None else None,
        "projection_revision": 2**63 - 1, "state": "validated" if reason is None else "unknown",
        "reason_code": reason, "refs": [ref], "absences": [],
        "rows": [{"ref": ref, "tuple_digest": "b" * 64, "encoded_bytes": MAX_BYTES}]
            if reason is None else [],
        "rows_digest": "c" * 64 if reason is None else None,
        "encoded_bytes": MAX_BYTES, "owner_operation_kind": "memory.propose",
        "owner_events": [], "selected_delta_digest": "d" * 64}
    record = {"checkpoint_id": MEMORY_CURRENT_CHECKPOINT, "safe": True,
        "state_digest": "e" * 64, "payload": payload}
    assert payload_upper >= len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode())
    assert record_upper >= len(json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode())
