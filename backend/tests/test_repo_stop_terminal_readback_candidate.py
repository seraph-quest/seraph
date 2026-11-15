"""Actual Stop producer observations and committed-row comparison negatives.

Fault injection follows committed cancellation; it proves readback denial, not
prevention of the original cancellation. Copies below test codec mechanics only.
"""
from datetime import datetime, timedelta, timezone
from enum import Enum
import json

import pytest
from sqlalchemy import update

from src.db.models import WorkBoardEvent
from src.workflows import repo_repair_source as source
from src.workflows import repo_repair_source_recovery as recovery
from src.workflows import repo_repair_stop as stop
from src.workflows.job_runtime import DurableJobLeaseError, _canonical
from src.work_board.time import serialize_utc_datetime
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_source_recovered_finalizer import _signed_ownerless_fixture
from tests.test_repo_source_stop_knownpost_candidate import _physical_outside_sql


async def _flow(accounting_db, monkeypatch):
    return await _signed_ownerless_fixture(accounting_db, monkeypatch, "test_python",
        failed=True, stop_requested=True)


async def _reconcile(flow):
    jobs, service, owner = flow["jobs"], flow["service"], flow["kwargs"]["owner"]
    job_id = flow["kwargs"]["job_id"]
    async with jobs._session() as db:
        revision = (await jobs._fetch(db, job_id)).revision
    return await recovery._reconcile_original_repository_cleanup(service, jobs,
        job_id=job_id, owner=owner, expected_job_revision=revision)


@pytest.mark.asyncio
async def test_actual_stop_producer_aware_event_matches_fresh_sqlite_row(
        accounting_db, monkeypatch, repository_admission_signer, record_property):
    flow = await _flow(accounting_db, monkeypatch)
    jobs = flow["jobs"]
    original = jobs.cancel_general_task_native_parent
    observed = []

    async def observe(*args, **kwargs):
        result = await original(*args, **kwargs)
        expected = result["event"]
        assert isinstance(expected, WorkBoardEvent)
        assert expected.created_at.tzinfo is not None
        assert expected.created_at.utcoffset() == timedelta(0)
        async with jobs._session() as db:
            rows = []
            for key in ("task", "attempt", "event"):
                actual = await db.get(type(result[key]), stop._key(result[key]), populate_existing=True)
                assert actual is not None
                assert stop._committed_stop_row_json(actual) == stop._committed_stop_row_json(result[key])
                rows.append((key, result[key], actual))
            actual = await db.get(WorkBoardEvent, expected.event_id, populate_existing=True)
            assert actual.created_at.tzinfo is None
            assert _canonical(actual.model_dump(mode="json")) != _canonical(expected.model_dump(mode="json"))
            assert actual.created_at == expected.created_at.replace(tzinfo=None)
            assert actual.created_at.microsecond == expected.created_at.microsecond
            observed.append((expected, actual, rows))
            record_property("original_event_created_at", expected.created_at.isoformat())
            record_property("sqlite_event_created_at", actual.created_at.isoformat())
        return result  # The actual original producer result, unchanged.

    monkeypatch.setattr(jobs, "cancel_general_task_native_parent", observe)
    with _physical_outside_sql(monkeypatch, flow) as physical:
        result = await _reconcile(flow)
    assert len(observed) == 1 and physical["guard_entries"] == 1
    assert result["status"] == "cancelled" and result["no_learning"] is True
    assert result["source_recovery"]["state"] == "original_stop_committed"
    assert result["source_recovery"]["physical_hold"] is False

    # Codec mechanics on real producer rows: no copy supplies Stop authority.
    expected, actual, rows = observed[0]
    shifted_zone = expected.model_copy(update={"created_at": expected.created_at.astimezone(
        timezone(timedelta(hours=5, minutes=30)))})
    assert stop._committed_stop_row_json(shifted_zone) == stop._committed_stop_row_json(actual)
    assert serialize_utc_datetime(expected.created_at) == serialize_utc_datetime(actual.created_at)
    for row_name, returned, persisted in rows:
        for key, value in returned.model_dump(mode="python").items():
            if isinstance(value, datetime):
                changed = value + timedelta(microseconds=1)
            elif isinstance(value, bool):
                changed = not value
            elif isinstance(value, int):
                changed = value + 1
            elif isinstance(value, Enum):
                changed = "changed:" + str(value.value)
            elif value is None:
                changed = "changed:null"
            else:
                changed = str(value) + ":changed"
            assert stop._committed_stop_row_json(returned.model_copy(update={key: changed})) != stop._committed_stop_row_json(persisted), (row_name, key)
    assert stop._committed_stop_row_json(expected.model_copy(update={"created_at": None})) != stop._committed_stop_row_json(actual)
    # Date-looking private metadata text is not normalized by the typed codec.
    text = expected.model_copy(update={"metadata_json": '{"at":"2026-01-01T00:00:00Z"}'})
    other_text = text.model_copy(update={"metadata_json": '{"at":"2026-01-01T00:00:00+00:00"}'})
    assert stop._committed_stop_row_json(text) != stop._committed_stop_row_json(other_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["created_at", "metadata_json", "actor_principal_id"])
async def test_actual_stop_denies_changed_cancellation_event_after_commit(
        accounting_db, monkeypatch, repository_admission_signer, field):
    flow = await _flow(accounting_db, monkeypatch)
    jobs = flow["jobs"]
    original = jobs.cancel_general_task_native_parent
    observed = []

    async def corrupt_committed_row(*args, **kwargs):
        result = await original(*args, **kwargs)
        event = result["event"]
        assert isinstance(event, WorkBoardEvent) and event.kind == "attempt.cancel_requested"
        before = stop._committed_stop_row_json(event)
        values = {
            "created_at": event.created_at.replace(tzinfo=None) + timedelta(microseconds=1),
            "metadata_json": json.dumps({**json.loads(event.metadata_json), "changed_after_commit": True}),
            "actor_principal_id": event.actor_principal_id + ":changed",
        }
        # Private fixture fault injection, outside the original completed writer.
        async with accounting_db[2]() as db:
            persisted = await db.get(WorkBoardEvent, event.event_id)
            assert stop._committed_stop_row_json(persisted) == before
            mutation = await db.execute(update(WorkBoardEvent).where(
                WorkBoardEvent.event_id == event.event_id).values({field: values[field]}))
            assert mutation.rowcount == 1
            await db.commit()
        assert stop._committed_stop_row_json(event) == before
        observed.append((event.event_id, before))
        return result  # Never substitute a receipt or mutate the returned object.

    monkeypatch.setattr(jobs, "cancel_general_task_native_parent", corrupt_committed_row)
    with _physical_outside_sql(monkeypatch, flow):
        with pytest.raises(DurableJobLeaseError, match="committed original native Stop readback changed"):
            await _reconcile(flow)
    assert len(observed) == 1
    async with jobs._session() as db:
        actual = await db.get(WorkBoardEvent, observed[0][0])
        assert stop._committed_stop_row_json(actual) != observed[0][1]
        root = await jobs._fetch(db, flow["kwargs"]["job_id"])
        assert source._repository_record(root, "repository:terminal:v1")["schema"] == "repository.stop_terminal.v1"
        assert jobs._repo_repair_reservation_state(root)["status"] == "released"
        original_input, *_ = source.read_repository_original(root)
        for identity in (original_input["native_binding"]["parent_job_id"], original_input["native_binding"]["invocation_id"]):
            assert (await jobs._fetch(db, identity)).status == "cancelled"
