"""Reject counterfeit job continuations before touching their supplied DB."""
import pytest

from src.workflows.job_runtime import DurableJobRepository, DurableJobTransitionError
from tests.test_programme_prospective_live_selection import genuine_live_selection


@pytest.mark.parametrize("value", ["plain", "\"\\\n\u0001", "zażółć", "😀", "", {"a": [None, True, -9223372036854775808]}])
def test_original_encoded_upper_covers_json_escape_and_int64(value):
    import json
    from src.workflows.job_runtime import _discovery_encoded_upper
    actual = len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
    assert _discovery_encoded_upper(value) >= actual


def test_physical_schedule_keeps_every_repeated_original_rows_parse():
    from src.workflows.research_sources import _discovery_physical_function_entries
    widths = {name: 10 for name in ("run_identity", "declared_authority_json", "arguments_json",
        "checkpoint_receipts_json", "artifact_receipts_json", "effect_receipts_json")}
    entries = _discovery_physical_function_entries("original-rows", job_id="job", widths=widths)
    labels = [appearance[-1] for appearance, _ in entries]
    assert labels.count("json-declared_authority_json") == 2
    assert sum(label.startswith("json-") for label in labels) == 6
    assert labels.count("artifact-ref") == 2
    assert labels.count("digest-return") == 2


def test_del_and_mixed_controls_cover_actual_json_metadata_and_model_dump():
    import json
    from src.db.models import InferenceAccountingOwner
    from src.workflows.job_runtime import _discovery_encoded_upper
    for value in ("\x7f", {"metadata": "\x00\n\x1f\x7fż😀"},
            InferenceAccountingOwner(deployment_id="\x7f", ceiling_microusd=1).model_dump(mode="json")):
        actual = len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        assert _discovery_encoded_upper(value) >= actual
    assert _discovery_encoded_upper("\x7f") == 8


@pytest.mark.parametrize("supplied", [None, object(), {"db": object()}])
def test_raw_values_cannot_supply_discovery_session(supplied):
    jobs = DurableJobRepository()
    with pytest.raises(DurableJobTransitionError, match="^goal_discovery_session_unavailable$"):
        jobs._validate_discovery_session(supplied)


def test_duck_typed_discovery_validation_is_never_called():
    class Counterfeit:
        called = False

        def validate(self, **_kwargs):
            self.called = True
            return self

    supplied = Counterfeit()
    jobs = DurableJobRepository()
    with pytest.raises(DurableJobTransitionError, match="^goal_discovery_session_unavailable$"):
        jobs._validate_discovery_session(supplied, db=object(), header_budget=object())
    assert supplied.called is False


@pytest.mark.asyncio
async def test_original_engine_exit_publishes_before_commit_and_events(monkeypatch):
    from src.db.engine import _commit_original_session_owner
    from src.work_board import events

    observed = []

    class Guard:
        async def publish(self):
            observed.append("publish")

    class Session:
        info = {"composition_guard": Guard(), "work_board_events_after_commit": ["event"]}

        async def commit(self):
            observed.append("commit")

    async def delivered(values):
        assert values == ["event"]
        observed.append("events")

    monkeypatch.setattr(events, "publish_work_board_events", delivered)
    session = Session()
    await _commit_original_session_owner(session)
    assert observed == ["publish", "commit", "events"]
    assert "work_board_events_after_commit" not in session.info


@pytest.mark.asyncio
async def test_original_engine_exit_commit_failure_does_not_deliver_events(monkeypatch):
    from src.db.engine import _commit_original_session_owner
    from src.work_board import events

    observed = []

    class Guard:
        async def publish(self):
            observed.append("publish")

    class Session:
        info = {"composition_guard": Guard(), "work_board_events_after_commit": ["event"]}

        async def commit(self):
            observed.append("commit")
            raise RuntimeError("commit failed")

    async def forbidden(_values):
        raise AssertionError("events preceded successful original commit")

    monkeypatch.setattr(events, "publish_work_board_events", forbidden)
    session = Session()
    with pytest.raises(RuntimeError, match="^commit failed$"):
        await _commit_original_session_owner(session)
    assert observed == ["publish", "commit"]
    assert session.info["work_board_events_after_commit"] == ["event"]


@pytest.mark.asyncio
async def test_forged_continuation_cannot_enter_engine_phase_exit():
    from src.db.engine import _exit_owned_discovery_writer

    with pytest.raises(RuntimeError, match="^goal_discovery_original_exit_unavailable$"):
        await _exit_owned_discovery_writer(object(), phase="admission", job_id="forged")


@pytest.mark.asyncio
async def test_real_admission_commit_survives_event_delivery_failure_without_queue(
    genuine_live_selection, monkeypatch,
):
    from sqlalchemy import event, select
    from config.settings import settings
    from src.db import engine as database_engine
    from src.db.models import Goal, WorkflowRunState, WorkBoardEvent
    from src.guardian.goal_discovery import goal_discovery_service
    from src.scheduler import engine as scheduler_engine
    from src.work_board import events
    from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository

    case = genuine_live_selection
    # Use the original issuer and its actual persisted event, not an invented
    # delivery receipt. The test requeues this event at the failure boundary.
    async with database_engine.get_session() as db:
        goal = await db.get(Goal, case.goal_id)
        await WorkBoardRepository().create_task(db,
            WorkBoardOwner(principal_id=case.operator.principal.principal_id, session_id=case.operator.session_id),
            WorkBoardTaskCreate(title="Original event delivery boundary", goal_id=goal.id,
                goal_revision=goal.revision, idempotency_key="owned-admission-event-failure"))
        pending_event = db.info["work_board_events_after_commit"][-1]
        assert type(pending_event) is WorkBoardEvent

    service = goal_discovery_service
    original_exit = database_engine._exit_owned_discovery_writer
    observations, rollbacks, deliveries, forbidden = [], [], [], []

    async def fail_delivery(pending):
        assert pending == [pending_event]
        deliveries.append(pending_event.event_id)
        raise RuntimeError("original event delivery failed")

    async def observe_exit(owned, *, phase, job_id):
        assert phase == "admission"
        owned.validate(db=owned.db, header_budget=owned.header_budget, jobs=service.jobs)
        def rolled_back(_session):
            rollbacks.append(job_id)
        event.listen(owned.db.sync_session, "after_rollback", rolled_back)
        owned.db.info.setdefault("work_board_events_after_commit", []).append(pending_event)
        try:
            with pytest.raises(RuntimeError, match="^original event delivery failed$"):
                await original_exit(owned, phase=phase, job_id=job_id)
            proof = owned.guard._discovery_commit_exit
            assert proof is not None and proof[0] is owned and proof[1:3] == ("admission", job_id)
            assert owned._candidate is not None and owned._candidate_phase == "reader"
            assert not owned.db.in_transaction()
            observations.append((job_id, owned, proof))
            raise RuntimeError("original event delivery failed")
        finally:
            event.remove(owned.db.sync_session, "after_rollback", rolled_back)

    async def no_queue_or_execution(*args, **kwargs):
        forbidden.append(True)
        raise AssertionError("unsettled committed admission advanced to B or effects")

    monkeypatch.setattr(events, "publish_work_board_events", fail_delivery)
    monkeypatch.setattr(database_engine, "_exit_owned_discovery_writer", observe_exit)
    monkeypatch.setattr(service, "_queue", no_queue_or_execution)
    monkeypatch.setattr(service, "run", no_queue_or_execution)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    assert scheduler is not None
    scheduler.pause()
    try:
        await scheduler.get_job("goal_public_discovery").func()
        assert len(observations) == 1 and deliveries == [pending_event.event_id]
        job_id, owned, proof = observations[0]
        assert rollbacks == [] and forbidden == []
        async with case.engine.connect() as connection:
            durable = (await connection.execute(select(WorkflowRunState.run_identity,
                WorkflowRunState.status, WorkflowRunState.revision).where(
                    WorkflowRunState.run_identity == job_id))).one()
        assert tuple(durable) == (job_id, "accepted", 1)
        assert not owned.active and service._owned_discovery_session is None
        assert not service._tasks and case.contacts == []
    finally:
        scheduler_engine.shutdown_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["get", "admit", "queue", "physical"])
async def test_job_entry_rejects_forged_owner_before_database(entry):
    from src.workflows.research_sources import physical_discovery_inputs

    jobs = DurableJobRepository()
    supplied = object()
    with pytest.raises(DurableJobTransitionError, match="^goal_discovery_session_unavailable$"):
        if entry == "get":
            await jobs.get_job("unused", discovery_session=supplied)
        elif entry == "admit":
            await jobs.admit_job(object(), discovery_session=supplied)
        elif entry == "queue":
            await jobs.queue_job("unused", discovery_session=supplied)
        else:
            await physical_discovery_inputs(jobs, "unused", discovery_session=supplied)


@pytest.mark.asyncio
async def test_inactive_exact_class_cannot_supply_continuation():
    from src.guardian.goal_discovery import _OwnedDiscoverySession

    supplied = object.__new__(_OwnedDiscoverySession)
    supplied.active = False
    with pytest.raises(RuntimeError, match="^goal_discovery_session_unavailable$"):
        DurableJobRepository()._validate_discovery_session(supplied)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["db", "header_budget"])
async def test_changed_session_or_frame_denied_before_marker_access(changed):
    import asyncio
    from types import SimpleNamespace
    from src.guardian.goal_discovery import _OwnedDiscoverySession

    supplied = object.__new__(_OwnedDiscoverySession)
    supplied.active = True
    supplied.service = SimpleNamespace(_owned_discovery_session=supplied)
    supplied.task = asyncio.current_task()
    supplied.db, supplied.header_budget = object(), object()
    # An exact-class counterfeit with a matching service field still cannot
    # replace either original identity. No live marker/guard is fabricated.
    with pytest.raises(RuntimeError, match="^goal_discovery_session_unavailable$"):
        DurableJobRepository()._validate_discovery_session(supplied, **{changed: object()})
