"""Own-WRS byte mechanics only: no fabricated Source/effect/publication grant."""
from copy import deepcopy
import json
import pytest

from src.memory.header_bounds import HeaderBoundsError
from src.workflows.job_runtime import _digest
from src.workspace.accounting_witness import (
    MEMORY_CURRENT_CHECKPOINT, MEMORY_REFERENCE_PROFILE,
    _native_memory_current_rows_digest, _native_memory_own_journal_projection,
)


def journal():
    rows = [{"ref": {"table": "memories", "key": "memory"},
             "tuple_digest": "b" * 64, "encoded_bytes": 17},
            {"ref": {"table": "workflow_run_states", "key": "job"},
             "tuple_digest": "c" * 64, "encoded_bytes": 19}]
    payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "original_checkpoint_digest": "a" * 64, "projection_revision": 1,
        "state": "validated", "reason_code": None,
        "refs": [item["ref"] for item in rows], "absences": [], "rows": rows,
        "rows_digest": _native_memory_current_rows_digest(rows, "job"),
        "encoded_bytes": 36, "owner_operation_kind": "memory.forget",
        "owner_events": [], "selected_delta_digest": "d" * 64}
    record = {"checkpoint_id": MEMORY_CURRENT_CHECKPOINT, "safe": True,
              "state_digest": _digest(payload), "payload": payload}
    # Deliberate formatting and unrelated escaped bytes must survive exactly.
    raw = '[ \n {"unrelated":"keep\\u0061", "nested":[1,2]},\t' + json.dumps(record, indent=2) + '  ]'
    return raw, record


def test_projection_changes_only_three_literal_spans_and_preserves_other_bytes():
    raw, record = journal()
    projected = _native_memory_own_journal_projection(raw, "job")
    expected = raw.replace('"' + record["state_digest"] + '"', '"' + "0" * 64 + '"', 1)
    expected = expected.replace('"' + "c" * 64 + '"', '"' + "0" * 64 + '"', 1)
    # The non-own count17 remains untouched, including all whitespace.
    expected = expected.replace('"encoded_bytes": 19', '"encoded_bytes": 0', 1)
    assert projected == expected
    assert projected.startswith('[ \n {"unrelated":"keep\\u0061", "nested":[1,2]},\t')


def test_aggregate_is_independent_of_own_slots_but_binds_every_other_row():
    _, record = journal()
    rows = record["payload"]["rows"]
    changed = deepcopy(rows)
    changed[1].update(tuple_digest="e" * 64, encoded_bytes=999)
    assert _native_memory_current_rows_digest(rows, "job") == _native_memory_current_rows_digest(changed, "job")
    changed[0]["encoded_bytes"] += 1
    assert _native_memory_current_rows_digest(rows, "job") != _native_memory_current_rows_digest(changed, "job")


def test_masked_slot_tamper_still_fails_full_actual_current_envelope():
    raw, record = journal()
    with pytest.raises(HeaderBoundsError, match="memory_reference_journal_invalid"):
        _native_memory_own_journal_projection(raw.replace('"' + "c" * 64 + '"', '"' + "e" * 64 + '"'), "job")
    with pytest.raises(HeaderBoundsError):
        _native_memory_own_journal_projection(raw, "different-job")


def test_unmasked_lexical_change_survives_normalization():
    raw, _ = journal()
    changed = raw.replace('keep\\u0061', 'keepa')
    assert _native_memory_own_journal_projection(raw, "job") != _native_memory_own_journal_projection(changed, "job")


def test_actual_constructor_current_counts_and_normalized_digest_converge_without_mutation():
    import hashlib
    from src.db.models import WorkflowRunState
    from src.memory.header_bounds import HeaderReadBudget, WRS_BY_RUN
    from src.workspace.accounting_witness import (
        _native_memory_transient_sql_row, _native_memory_prepare_current_row,
        _native_memory_row_bytes, preflight_native_memory_reference_journal,
    )
    run = WorkflowRunState(run_identity="job", root_run_identity="job", workflow_name="byte-evidence")
    before = _native_memory_transient_sql_row(run)
    _, template = journal()
    frame = HeaderReadBudget()
    candidate, record = _native_memory_prepare_current_row(before, template["payload"], header_budget=frame)
    assert run.checkpoint_receipts_json == before["checkpoint_receipts_json"]
    own = record["payload"]["rows"][1]
    assert own["encoded_bytes"] == len(_native_memory_row_bytes(WRS_BY_RUN, "job", candidate))
    normalized = dict(candidate, checkpoint_receipts_json=_native_memory_own_journal_projection(
        candidate["checkpoint_receipts_json"], "job"))
    assert own["tuple_digest"] == hashlib.sha256(_native_memory_row_bytes(WRS_BY_RUN, "job", normalized)).hexdigest()
    assert record["payload"]["rows_digest"] == _native_memory_current_rows_digest(record["payload"]["rows"], "job")
    assert preflight_native_memory_reference_journal(candidate["checkpoint_receipts_json"])[1] == record
    assert frame.remaining < 1_048_576


def test_current_count_preparation_exhaustion_does_not_mutate_actual_constructor():
    from src.db.models import WorkflowRunState
    from src.memory.header_bounds import HeaderReadBudget
    from src.workspace.accounting_witness import _native_memory_transient_sql_row, _native_memory_prepare_current_row
    run = WorkflowRunState(run_identity="job", root_run_identity="job", workflow_name="byte-evidence")
    before = _native_memory_transient_sql_row(run)
    _, template = journal()
    frame = HeaderReadBudget(); frame.debit(1_048_575)
    with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
        _native_memory_prepare_current_row(before, template["payload"], header_budget=frame)
    assert _native_memory_transient_sql_row(run) == before
    assert frame.remaining == 1


@pytest.mark.parametrize("patch", [
    {"rows": []}, {"current_digest": "b" * 64},
    {"original_checkpoint_digest": "A" * 64},
    {"original_checkpoint_id": "memory:current-reference.v2"},
    {"invocation_ref": "other-job"}, {"profile": "other-profile"},
])
def test_durable_locator_rejects_extra_projection_or_changed_original_address(patch):
    from src.runtime_plugins.memory_producer import validate_memory_retention_locator
    from src.workspace.accounting_witness import MEMORY_ORIGINAL_CHECKPOINT
    locator = {"profile": MEMORY_REFERENCE_PROFILE, "invocation_ref": "job",
               "original_checkpoint_id": MEMORY_ORIGINAL_CHECKPOINT,
               "original_checkpoint_digest": "a" * 64}
    assert validate_memory_retention_locator(locator, "job") is locator
    with pytest.raises(ValueError, match="native_memory_retention_locator_invalid"):
        validate_memory_retention_locator({**locator, **patch}, "job")


@pytest.mark.asyncio
async def test_caller_constructed_claim_cannot_resolve_lifecycle_source():
    from src.runtime_plugins.memory_producer import _validate_memory_lifecycle_source
    from src.runtime_plugins.dispatch import NativeServiceBlocked
    from src.workflows.job_runtime import NativeServiceClaim
    # Negative caller DTO only: no synthetic issuer, effect or positive Source.
    claim = NativeServiceClaim(job={}, checkpoint={}, binding=None, host_boot_nonce="caller")
    with pytest.raises(NativeServiceBlocked, match="native_memory_actual_claim_source_unavailable"):
        await _validate_memory_lifecycle_source(None, None, claim)
    with pytest.raises(NativeServiceBlocked, match="native_memory_actual_claim_source_required"):
        await _validate_memory_lifecycle_source(None, None, {})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, object()])
def test_causal_bind_preimage_rejects_non_sqlite_or_nonfinite_values(value):
    from src.memory.header_bounds import MEMORY_DESCRIPTORS
    from src.workspace.accounting_witness import _native_memory_selected_delta_preimage
    with pytest.raises(HeaderBoundsError, match="memory_reference_row_invalid"):
        _native_memory_selected_delta_preimage((),
            ((MEMORY_DESCRIPTORS["memories"], "memory", {"confidence": value}),), ())


def test_causal_bind_preimage_preserves_finite_sqlite_float_and_entire_original_journal():
    from src.memory.header_bounds import MEMORY_DESCRIPTORS, WRS_BY_RUN
    from src.workspace.accounting_witness import _native_memory_selected_delta_preimage
    raw = '[ {"checkpoint_id":"original", "payload":"keep\\u0061"} ]'
    value = _native_memory_selected_delta_preimage((), (
        (WRS_BY_RUN, "run", {"checkpoint_receipts_json": raw, "status": "succeeded", "revision": 7}),
        (MEMORY_DESCRIPTORS["memories"], "memory", {"confidence": 0.125}),
    ), (("audit_events", "audit"),))
    assert value["updates"][0]["columns"]["confidence"] == 0.125
    assert type(value["updates"][0]["columns"]["confidence"]) is float
    assert value["updates"][1]["columns"] == {
        "checkpoint_receipts_json": raw, "status": "succeeded", "revision": 7}
