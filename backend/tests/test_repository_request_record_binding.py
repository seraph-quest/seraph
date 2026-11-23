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


def test_identity_binds_whether_the_contact_started_and_finished():
    finished = source.request_record_digest(_row())
    for state in ("released", "reserved", "contact_started"):
        assert not source.request_record_digest_matches(_row(state=state), finished), state
    assert not source.request_record_digest_matches(_row(contact_started_at=None), finished)
    assert source.request_record_identity(_row())["contacted"] is True
    assert source.request_record_identity(_row(state="released"))["finished"] is False


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
    settled = _row(state="settled", actual_cost_microusd=5, recovery_reason=None, revision=5)
    refused.update({
        "settled request id appears": (settled,
            lambda row: _row(**{**row.model_dump(), "provider_operation_id": "near-new", "revision": 6})),
        "unknown reason changed": (before,
            lambda row: _row(**{**row.model_dump(), "recovery_reason": "provider_call_failed", "revision": 3})),
        "unknown evidence appended": (before, lambda row: _row(**{**row.model_dump(), "revision": 3,
            "evidence_json": json.dumps(json.loads(row.evidence_json) + [{"kind": "unknown"}])})),
        "revision backwards": (settled, lambda row: _row(**{**row.model_dump(), "revision": 1})),
        "charge with revision backwards": (before, lambda row: _row(**{**_charged(row).model_dump(), "revision": 1})),
        "negative amount": (before, lambda row: _row(**{**_charged(row).model_dump(), "actual_cost_microusd": -7})),
        "no evidence appended": (before, lambda row: _row(**{**_charged(row).model_dump(), "evidence_json": row.evidence_json})),
        "timestamp backwards": (before, lambda row: _row(**{**_charged(row).model_dump(),
            "updated_at": row.updated_at - timedelta(days=1)})),
        "staged unknown already priced": (_row(actual_cost_microusd=3),
            lambda row: _row(**{**_charged(row).model_dump(), "actual_cost_microusd": 9})),
        "never contacted": (_row(contact_started_at=None), lambda row: _charged(row)),
        "bound field changed": (before, lambda row: _row(**{**_charged(row).model_dump(), "priority": 51})),
    })
    for name, (staged_row, change) in refused.items():
        assert not source.request_record_unchanged(change(staged_row), _staged(staged_row)), name
    # A request id first reported with the recorded charge is accepted.
    assert source.request_record_unchanged(_row(**{**_charged(before).model_dump(), "provider_operation_id": "near-1"}),
        staged)
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


class _Rows:
    """A session returning fixed current rows by primary key."""
    def __init__(self, *rows):
        self.rows = {row.operation_id if isinstance(row, InferenceCostReservation) else row.run_identity: row
            for row in rows}

    async def get(self, model, key, populate_existing=False):
        return self.rows.get(key)


def _finalizer_state(staged, monkeypatch):
    from types import SimpleNamespace
    from src.workflows import repo_repair_source_recovery as recovery
    monkeypatch.setattr(recovery, "assert_repository_scoped_completion", lambda *args, **kwargs: None)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    context = {"binding": SimpleNamespace(native_deadline_at=future), "run": SimpleNamespace(deadline_at=future)}
    state = {"rows": {(InferenceCostReservation, staged.operation_id): _staged(staged)}, "phase": 0, "pending": None,
        "bound": {"fence": SimpleNamespace(service=None), "job_id": "repository-root", "owner": None, "context": context}}
    monkeypatch.setattr(source, "_recovered_finalizer_slot", lambda witness: {"state": state})
    return state


@pytest.mark.asyncio
async def test_recovered_finalizer_row_recheck_accepts_only_a_recorded_charge(monkeypatch):
    from src.workflows.job_runtime import DurableJobLeaseError
    before = _row()
    state = _finalizer_state(before, monkeypatch)
    # Reaches the Root lookup only after every staged row passed.
    class Reached(Exception):
        pass
    class Jobs:
        async def _fetch(self, db, job_id):
            raise Reached()
    with pytest.raises(Reached):
        await source._recheck_recovered_finalizer_rows(_Rows(_charged(before)), Jobs(), state, object())
    with pytest.raises(DurableJobLeaseError, match="complete current rows changed"):
        await source._recheck_recovered_finalizer_rows(
            _Rows(_row(**{**_charged(before).model_dump(), "payload_digest": "f" * 64})), Jobs(), state, object())


@pytest.mark.asyncio
async def test_recovered_finalizer_capture_and_readback_carry_a_recorded_charge(monkeypatch):
    from contextlib import asynccontextmanager
    from src.workflows.job_runtime import DurableJobLeaseError
    before = _row()
    charged = _charged(before)
    state = _finalizer_state(before, monkeypatch)
    pending = {"entered": True, "kind": "checkpoint", "after": None, "job_id": "repository-root",
        "since": datetime.now(timezone.utc) - timedelta(hours=1)}
    state["pending"] = pending
    with pytest.raises(DurableJobLeaseError, match="exact original write delta changed"):
        await source.capture_recovered_repository_final_writer(None,
            _Rows(_row(**{**charged.model_dump(), "job_fencing_token": 10})), None,
            completion_witness=object(), kind="checkpoint")
    await source.capture_recovered_repository_final_writer(None, _Rows(charged), None,
        completion_witness=object(), kind="checkpoint")
    assert pending["after"] == {(InferenceCostReservation, before.operation_id): _staged(charged)}

    class Jobs:
        def __init__(self, row):
            self.row = row

        @asynccontextmanager
        async def _session(self):
            yield _Rows(self.row)
    # The committed readback refuses a changed contact and keeps the charged row.
    with pytest.raises(DurableJobLeaseError, match="committed readback changed"):
        await source.observe_recovered_repository_final_writer(
            Jobs(_row(**{**charged.model_dump(), "payload_digest": "f" * 64})), completion_witness=object(), kind="checkpoint")
    await source.observe_recovered_repository_final_writer(Jobs(charged), completion_witness=object(), kind="checkpoint")
    assert state["rows"] == {(InferenceCostReservation, before.operation_id): _staged(charged)}
    assert state["phase"] == 1 and state["pending"] is None


@pytest.mark.asyncio
async def test_completion_post_context_accepts_only_a_recorded_charge(monkeypatch):
    """Stop completion after producer cleanup (#1291, B2): the committed rows
    staged with the completion witness may only gain a recorded charge."""
    from src.workflows import repo_repair_source_recovery as recovery
    from src.workflows.job_runtime import DurableJobLeaseError
    before = _row()
    stop = {"schema": "repository.stop_intent.v2"}
    run = WorkflowRunState(run_identity="repository-root", workflow_name="repository", revision=8)
    staged_run = WorkflowRunState(run_identity="repository-root", workflow_name="repository", revision=7)
    staged_static = {"rows": "static"}
    monkeypatch.setattr(recovery, "assert_repository_completion_witness", lambda *args, **kwargs: None)
    monkeypatch.setattr(recovery, "repository_completion_context",
        lambda witness: {"run": staged_run, "owner": "owner", "static_rows": staged_static})
    monkeypatch.setattr(recovery, "repository_completion_post_cas", lambda witness: {
        "before_revision": 7, "post_revision": 8, "stop_digest": source._source_digest(stop)})
    monkeypatch.setattr(recovery, "repository_completion_committed_rows",
        lambda witness: ((InferenceCostReservation, before.operation_id, _staged(before)),))
    monkeypatch.setattr(source, "read_repository_inventory", lambda run: {"schema": "repository.checkpoint_inventory.v4"})
    monkeypatch.setattr(source, "_repository_record", lambda run, identity: stop)
    monkeypatch.setattr(source, "_stop_static_rows_match", lambda *args: True)
    context = {"run": run, "owner": "owner", "static_rows": staged_static}
    assert await source._validate_repository_completion_post_context_sql(_Rows(_charged(before)),
        None, None, witness=object(), context=context) is True
    with pytest.raises(DurableJobLeaseError, match="completion row changed"):
        await source._validate_repository_completion_post_context_sql(
            _Rows(_row(**{**_charged(before).model_dump(), "job_id": "other-root"})),
            None, None, witness=object(), context=context)
