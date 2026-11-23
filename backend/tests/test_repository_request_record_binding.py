"""Repository evidence binds a request record's contact, not its charge (#1291).

Recording a charge later (ADR-037, #1284) turns an ``unknown`` (not priced
yet) row into ``settled``. These checks run without native supervision; the
full repair journeys live in ``test_repo_work_original_producer_startup.py``.
"""
from datetime import datetime, timedelta, timezone
import json

import pytest

from src.db.models import InferenceCostReservation, WorkflowRunState
from src.workflows import repo_repair_source as source
from src.workflows.job_runtime import _canonical
from src.workflows.repo_repair_source_recovery import _charge_recorded_since


def _row(**changes):
    contacted = datetime(2026, 11, 20, 12, 0, 0)
    values = dict(operation_id="remote:repo-work:iteration-2", deployment_id="deployment-1",
        job_id="repository-root", owner_id="operator:one", goal_id="goal-1", goal_revision=3,
        payload_digest="a" * 64, policy_digest="b" * 64, runtime_path="strategist_agent",
        profile_id="near_tee.text", period_id="2026-11", settings_revision=7, ceiling_microusd=0,
        bound_microusd=0, sequence=4, priority=50, deadline_at=contacted + timedelta(minutes=5),
        state="unknown", job_fencing_token=9, contact_started_at=contacted,
        evidence_json=json.dumps([{"kind": "unknown", "provenance": "unresolved_provider_contact"}]),
        recovery_reason="provider_cost_readback_required", revision=2,
        created_at=contacted, updated_at=contacted)
    values.update(changes)
    return InferenceCostReservation(**values)


def _charged(row, amount=4_321):
    """What ``settle_inference_cost`` changes when the operator records a charge."""
    evidence = json.loads(row.evidence_json) + [{"kind": "settled", "actual_cost_microusd": amount,
        "provenance": "manual_externally_unverified"}]
    return _row(**{**row.model_dump(), "state": "settled", "actual_cost_microusd": amount,
        "recovery_reason": None, "evidence_json": json.dumps(evidence), "revision": row.revision + 1,
        "updated_at": row.updated_at + timedelta(hours=1)})


def _staged(row):
    return _canonical(row.model_dump(mode="json"))


def test_identity_digest_ignores_a_recorded_charge_and_binds_the_contact():
    before = _row()
    after = _charged(before)
    assert source.request_record_digest(after) == source.request_record_digest(before)
    assert source.request_record_digest_matches(after, source.request_record_digest(before))
    for field, value in (("payload_digest", "f" * 64), ("job_fencing_token", 10), ("job_id", "other-root"),
            ("contact_started_at", before.contact_started_at + timedelta(seconds=1)), ("bound_microusd", 1),
            ("operation_id", "remote:repo-work:iteration-3")):
        changed = _row(**{**after.model_dump(), field: value})
        assert not source.request_record_digest_matches(changed, source.request_record_digest(before)), field


def test_whole_row_digest_written_before_the_fix_still_verifies_unchanged():
    before = _row()
    legacy = source._source_digest(before.model_dump(mode="json"))
    assert source.request_record_digest_matches(before, legacy)
    assert source.request_records_digest_matches([before], source._source_digest([before.model_dump(mode="json")]))
    # The legacy digest bound every field; a later charge cannot satisfy it.
    assert not source.request_record_digest_matches(_charged(before), legacy)


def test_records_digest_binds_the_set_of_contacts():
    first, second = _row(), _row(operation_id="remote:repo-work:iteration-1", state="settled",
        actual_cost_microusd=10, recovery_reason=None)
    recorded = source.request_records_digest([first, second])
    assert source.request_records_digest_matches([second, _charged(first)], recorded)
    assert not source.request_records_digest_matches([second], recorded)
    assert not source.request_records_digest_matches([second, _charged(first), _row(operation_id="remote:x")], recorded)


def test_staged_row_may_only_gain_a_charge():
    before = _row()
    staged = _staged(before)
    assert source.request_record_unchanged(before, staged)
    assert source.request_record_unchanged(_charged(before), staged)
    assert source.committed_row_unchanged(_charged(before), staged)
    assert source.request_records_unchanged([_charged(before)], {before.operation_id: staged})
    refused = {
        "settled amount changed": (_row(state="settled", actual_cost_microusd=5, recovery_reason=None),
            lambda row: _row(**{**row.model_dump(), "actual_cost_microusd": 6})),
        "back to unknown": (_row(state="settled", actual_cost_microusd=5, recovery_reason=None),
            lambda row: _row(**{**row.model_dump(), "state": "unknown", "actual_cost_microusd": None})),
        "unfinished staged row": (_row(state="contact_started", recovery_reason=None),
            lambda row: _row(**{**row.model_dump(), "state": "settled", "actual_cost_microusd": 1})),
        "evidence rewritten": (before, lambda row: _row(**{**_charged(row).model_dump(), "evidence_json": "[]"})),
        "provider request changed": (_row(provider_operation_id="near-1"),
            lambda row: _row(**{**_charged(row).model_dump(), "provider_operation_id": "near-2"})),
        "contact changed": (before, lambda row: _row(**{**_charged(row).model_dump(), "payload_digest": "f" * 64})),
    }
    for name, (staged_row, change) in refused.items():
        assert not source.request_record_unchanged(change(staged_row), _staged(staged_row)), name
    assert not source.request_records_unchanged([], {before.operation_id: staged})
    assert not source.request_records_unchanged([before, _row(operation_id="remote:x")], {before.operation_id: staged})


def test_other_staged_rows_still_compare_exactly():
    run = WorkflowRunState(run_identity="repository-root", workflow_name="repository")
    staged = _canonical(run.model_dump(mode="json"))
    assert source.committed_row_unchanged(run, staged)
    run.revision += 1
    assert not source.committed_row_unchanged(run, staged)


@pytest.mark.parametrize(("recorded", "current", "accepted"), [
    (("unknown", None), ("unknown", None), True),
    (("unknown", None), ("settled", 4_321), True),
    (("settled", 5), ("settled", 5), True),
    (("settled", 5), ("settled", 6), False),
    (("settled", 5), ("unknown", None), False),
])
def test_recovery_accepts_only_a_charge_recorded_since_the_response(recorded, current, accepted):
    record = {"state": recorded[0], "actual_cost_microusd": recorded[1]}
    row = _row(state=current[0], actual_cost_microusd=current[1])
    assert _charge_recorded_since(record, row) is accepted
