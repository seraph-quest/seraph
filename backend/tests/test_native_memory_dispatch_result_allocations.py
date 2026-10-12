"""Caller result allocation bounds; no Source or complete ledger is fabricated."""
from types import MappingProxyType

import pytest

from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins.memory_producer import (
    _canonical,
    _native_memory_dispatch_result_pay,
    _native_memory_dispatch_result_upper,
    _native_memory_source_value_upper,
    original_native_memory_dispatch_result_numeric_schedule,
)


def test_receipt_seed_bounds_original_escaped_canonical_digest_input():
    invocation = 'invocation:"\\\x7f\U0001f642'
    claim = 'claim:"\\\x7f\U0001f642'
    actual = {"invocation": invocation, "claim": claim, "candidate": "c" * 64}
    for stage in ("receipt-seed-construction", "receipt-seed-canonical", "receipt-seed-utf8"):
        assert _native_memory_dispatch_result_upper(stage, MappingProxyType({}), invocation, claim) >= len(
            _canonical(actual).encode("utf-8"))


def test_readback_copy_covers_effect_and_result_with_escaped_identifiers():
    effect = {"method": "memory.forget", "candidate_digest": "c" * 64, "status": "succeeded",
        "value_json": _canonical({"record_ref": 'record:"\\\x7f\U0001f642'}),
        "audit_event_id": "audit:1", "proposal_id": None, "record_id": 'record:"\\\x7f\U0001f642'}
    result = {"status": "succeeded", "value": {"record_ref": effect["record_id"],
        "receipt_ref": "native-memory-receipt:" + "d" * 64}, "memory_status": "updated"}
    expected = {"invocation_ref": "run:1", "claim_ref": "claim:1", "candidate_digest": "c" * 64,
        "result_digest": "d" * 64, "payload": result, "effect": effect, "retention": {"profile": "original"}}
    context = {"candidate": {"method": "memory.forget"}, "result": expected}
    widths = MappingProxyType({"result_upper": _native_memory_source_value_upper(result),
        "effect_upper": _native_memory_source_value_upper(effect),
        "context_upper": _native_memory_source_value_upper(context)})
    assert _native_memory_dispatch_result_upper("expected-context-result", widths, "run:1", "claim:1") >= len(
        _canonical(expected).encode("utf-8"))
    assert _native_memory_dispatch_result_upper("effect-asdict", widths, "run:1", "claim:1") >= len(
        _canonical(effect).encode("utf-8"))


def test_result_payer_rejects_retention_clone_before_accessing_private_owner():
    class PoisonRetention:
        @property
        def ledger(self):
            raise AssertionError("unissued retention accessed ledger")

    with pytest.raises(HeaderBoundsError, match="memory_dispatch_result_numeric_owner_changed"):
        _native_memory_dispatch_result_pay(PoisonRetention(), object(), "effect", "result-value-json")


def test_result_schedule_rejects_source_clone_before_database_access():
    class PoisonOperation:
        @property
        def db(self):
            raise AssertionError("unissued operation accessed database")

    with pytest.raises(HeaderBoundsError):
        original_native_memory_dispatch_result_numeric_schedule(PoisonOperation(), object(),
            certificate=object(), facts=MappingProxyType({}), retention_widths=MappingProxyType({}))
