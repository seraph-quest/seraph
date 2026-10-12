"""Original producer allocation contribution; no effect/factory activation."""
from dataclasses import asdict
from types import MappingProxyType

import pytest

from src.memory.header_bounds import HeaderBoundsError, _MEMORY_LEDGER_PHASE
from src.audit.repository import audit_repository
from src.runtime_plugins.memory_producer import (
    MemoryOwnerEffect,
    _canonical,
    _native_memory_mutation_allocation_entries,
    _native_memory_mutation_allocation_pay,
    _native_memory_source_value_upper,
    original_native_memory_mutation_allocation_numeric_schedule,
)


def test_mutation_schedule_rejects_source_lookalike_before_database_access():
    class PoisonOperation:
        @property
        def db(self):
            raise AssertionError("unissued Source reached database")

    with pytest.raises(HeaderBoundsError, match="memory_mutation_numeric_owner_changed"):
        original_native_memory_mutation_allocation_numeric_schedule(
            PoisonOperation(), object(), certificate=object(), facts=MappingProxyType({}),
            retention_widths=MappingProxyType({}))


def test_mutation_allocation_rejects_foreign_phase_before_source_access():
    class PoisonOperation:
        @property
        def source(self):
            raise AssertionError("foreign phase reached Source")

    token = _MEMORY_LEDGER_PHASE.set((object(), "effect"))
    try:
        with pytest.raises(HeaderBoundsError, match="memory_mutation_numeric_owner_changed"):
            _native_memory_mutation_allocation_pay(object(), PoisonOperation(), object(), "effect-return")
    finally:
        _MEMORY_LEDGER_PHASE.reset(token)


def test_mutation_output_bounds_cover_original_escaped_canonical_result():
    # Exercise the actual canonical serializer and dataclass output, including
    # DEL and a non-BMP identifier; no fabricated Source or ledger is issued.
    value = {"proposal_ref": 'proposal:"\\\x7f\U0001f642', "revision": 9, "state": "proposed"}
    effect = MemoryOwnerEffect("memory.propose", "a" * 64, "succeeded",
        _canonical(value), "audit:" + "b" * 32, value["proposal_ref"], None)
    result_upper = _native_memory_source_value_upper(value)
    effect_upper = _native_memory_source_value_upper(asdict(effect))
    audit = audit_repository._prepare_event(actor="operator", session_id="session",
        event_type="memory_learning_proposed", tool_name="memory_control", policy_mode="operator_controlled",
        summary="Operator requested verified work-board memory", details={"proposal_id": value["proposal_ref"]})
    widths = MappingProxyType({"source_upper": 1024, "audit_upper": 1024,
        "details_upper": 1024, "result_upper": result_upper, "effect_upper": effect_upper})
    entries = dict(_native_memory_mutation_allocation_entries("memory.propose", widths, audit=audit))
    assert entries[("native-memory-mutation", "result-canonical")] >= len(_canonical(value).encode("utf-8"))
    for stage in ("effect-construction", "effect-return"):
        assert entries[("native-memory-mutation", stage)] >= len(_canonical(asdict(effect)).encode("utf-8"))


def test_mutation_audit_kwargs_cover_actual_eight_argument_copy():
    details = {"proposal_id": "proposal:\U0001f642", "source_task_id": "task:\x7f",
        "source_attempt_id": "attempt:\"", "status": "proposed"}
    audit = audit_repository._prepare_event(actor="principal:" + "p" * 512,
        session_id="session:" + "s" * 512, event_type="memory_learning_proposed",
        tool_name="memory_control", policy_mode="operator_controlled",
        summary="Operator requested verified work-board memory", details=details)
    # The original audit constructor supplies the actual values consumed by
    # _log_event_in_session; encode its logical row projection for this check.
    event = audit.model_dump(mode="json")
    kwargs = {"actor": audit.actor, "session_id": audit.session_id,
        "event_type": audit.event_type, "tool_name": audit.tool_name,
        "policy_mode": audit.policy_mode, "summary": audit.summary,
        "details": details, "_prepared_event": event}
    assert len(kwargs) == 8
    widths = MappingProxyType({"source_upper": 4096,
        "audit_upper": _native_memory_source_value_upper(event),
        "details_upper": _native_memory_source_value_upper(details),
        "result_upper": 4096, "effect_upper": 4096})
    entries = dict(_native_memory_mutation_allocation_entries("memory.propose", widths, audit=audit))
    assert entries[("native-memory-mutation", "audit-event-kwargs")] >= len(_canonical(kwargs).encode("utf-8"))


def test_mutation_legacy_allocation_hook_has_no_numeric_owner_side_effect():
    class PoisonOperation:
        @property
        def source(self):
            raise AssertionError("legacy hook inspected Source")

    token = _MEMORY_LEDGER_PHASE.set(None)
    try:
        _native_memory_mutation_allocation_pay(object(), PoisonOperation(), object(), "effect-return")
    finally:
        _MEMORY_LEDGER_PHASE.reset(token)
