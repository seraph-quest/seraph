"""Real bounded continuation reservation/contact with literal HTTP transport."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select, update

from src.db.models import WorkflowRunState, WorkBoardTask, WorkBoardAttempt, InferenceCostReservation
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError
from src.workflows.general_task_guard import read_manifest
from tests.test_general_task_contract import request, Registry
from tests.test_general_task_native_guard import running_task
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.general_task_test_transport import prepare_literal_planner


async def continuation_fixture(task_runtime, monkeypatch, *, max_calls=2):
    creation = request()
    creation = creation.model_copy(update={"input": creation.input.model_copy(update={
        "inference_egress_acknowledged": True,
        "limits": creation.input.limits.model_copy(update={"wall_seconds": 600,
            "max_inference_calls": max_calls, "max_cost_microusd": 1000})})})
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime, creation_request=creation)
    owner = WorkBoardOwner(principal_id=current["manifest"]["owner_principal_id"],
        session_id=current["manifest"]["original_root_id"])
    planner, transport = await prepare_literal_planner(sessions, task_runtime[1], monkeypatch, owner,
        envelope.plan.model_copy(update={"revision": 2}))
    async with sessions() as db:
        parent = await dispatcher.jobs._fetch(db, current["job"]["job_id"])
        manifest = read_manifest(parent)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == manifest.task_id))
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == manifest.attempt_id))
        for row in (parent, task, attempt):
            db.expunge(row)
    return sessions, dispatcher, planner, transport, owner, parent, task, attempt, manifest, envelope


@pytest.mark.asyncio
async def test_manual_group_two_continuations_share_original_clock_and_call_ceiling(task_runtime, monkeypatch):
    sessions, dispatcher, planner, transport, owner, parent, task, attempt, manifest, envelope = await continuation_fixture(task_runtime, monkeypatch)
    assert manifest.native_deadline_at < manifest.original_deadline_at
    for key in ("continuation-one", "continuation-two"):
        async with sessions() as db:
            result = await planner.continue_plan(db, owner, parent=parent, task=task, attempt=attempt,
                manifest=manifest, envelope=envelope, request_key=key)
        assert result.plan.revision == 2 and result.provenance is None
        assert result.group == envelope.proposal_group
    async with sessions() as db:
        with pytest.raises(BoardError, match="call_limit"):
            await planner.continue_plan(db, owner, parent=parent, task=task, attempt=attempt,
                manifest=manifest, envelope=envelope, request_key="continuation-three")
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        assert len(rows) == 2
        for row in rows:
            entry = next(item for item in json.loads(row.evidence_json) if item["kind"] == "general_task_group_reservation.v1")
            assert entry["parent_owner"] == parent.lease_owner
            assert entry["parent_fence"] == parent.fencing_token
            assert entry["group"] == envelope.proposal_group.model_dump(mode="json")
    assert len(transport["contacts"]) == 2
    assert "step_statuses" in transport["contacts"][0]["messages"][-1]["content"]
    assert "general_task_child_binding" not in json.dumps(transport["contacts"])


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["lease", "fence", "deadline"])
async def test_parent_drift_after_reservation_denies_actual_provider_contact(task_runtime, monkeypatch, drift):
    sessions, dispatcher, planner, transport, owner, parent, task, attempt, manifest, envelope = await continuation_fixture(task_runtime, monkeypatch)
    from src.workflows.job_runtime import DurableJobRepository
    from datetime import timedelta
    original = DurableJobRepository.contact_inference_provider
    async def drift_before_contact(repository, operation_id, **kwargs):
        async with sessions() as db:
            values = {"lease_owner": "foreign-runtime-owner"} if drift == "lease" else (
                {"fencing_token": parent.fencing_token + 1} if drift == "fence" else
                {"deadline_at": parent.deadline_at + timedelta(seconds=10)})
            await db.execute(update(WorkflowRunState).where(
                WorkflowRunState.run_identity == parent.run_identity).values(**values))
            await db.commit()
        return await original(repository, operation_id, **kwargs)
    monkeypatch.setattr(DurableJobRepository, "contact_inference_provider", drift_before_contact)
    async with sessions() as db:
        with pytest.raises(BoardError):
            await planner.continue_plan(db, owner, parent=parent, task=task, attempt=attempt,
                manifest=manifest, envelope=envelope, request_key="drift-after-reserve")
    assert transport["contacts"] == []
    async with sessions() as db:
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        assert len(rows) == 1 and rows[0].contact_started_at is None
        evidence = next(item for item in json.loads(rows[0].evidence_json)
            if item["kind"] == "general_task_group_reservation.v1")
        assert evidence["parent_owner"] == parent.lease_owner
        assert evidence["parent_fence"] == parent.fencing_token


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["foreign_job", "duplicate", "extra_field", "wrong_role"])
async def test_corrupted_canonical_reservation_evidence_denies_contact(task_runtime, monkeypatch, corruption):
    sessions, dispatcher, planner, transport, owner, parent, task, attempt, manifest, envelope = await continuation_fixture(task_runtime, monkeypatch)
    from src.workflows.job_runtime import DurableJobRepository
    original = DurableJobRepository.contact_inference_provider
    from src.workflows.inference_accounting import InferenceAccountingError
    observed = {}

    async def corrupt_before_contact(repository, operation_id, **kwargs):
        async with sessions() as db:
            row = await db.scalar(select(InferenceCostReservation).where(
                InferenceCostReservation.operation_id == operation_id))
            entries = json.loads(row.evidence_json)
            entry = next(item for item in entries if item.get("kind") == "general_task_group_reservation.v1")
            if corruption == "foreign_job":
                entry["original_job_id"] = "foreign-original-job"
            elif corruption == "duplicate":
                entries.append(dict(entry))
            elif corruption == "extra_field":
                entry["caller_authorizes_contact"] = True
            else:
                entry["role"] = "initial_proposal"
            row.evidence_json = json.dumps(entries)
            observed["bound"] = row.bound_microusd
            from src.workflows.general_task_accounting import entry_for
            with pytest.raises(InferenceAccountingError, match="evidence_invalid"):
                entry_for(row)
            observed["closed_evidence"] = True
            await db.commit()
        try:
            return await original(repository, operation_id, **kwargs)
        except InferenceAccountingError as exc:
            observed["error"] = str(exc)
            raise

    monkeypatch.setattr(DurableJobRepository, "contact_inference_provider", corrupt_before_contact)
    async with sessions() as db:
        with pytest.raises(BoardError):
            await planner.continue_plan(db, owner, parent=parent, task=task, attempt=attempt,
                manifest=manifest, envelope=envelope, request_key="corrupted-canonical-reservation")
    assert transport["contacts"] == []
    assert observed["closed_evidence"] is True
    assert observed["error"] in {"general_task_group_evidence_invalid", "accounting_continuity_unavailable"}
    async with sessions() as db:
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        assert len(rows) == 1 and rows[0].contact_started_at is None
        assert rows[0].bound_microusd == observed["bound"]


@pytest.mark.asyncio
@pytest.mark.parametrize("liability", ["unknown", "missing_settled_cost", "prior_period_cost"])
async def test_group_liability_survives_accounting_period_change(task_runtime, monkeypatch, liability):
    sessions, dispatcher, planner, transport, owner, parent, task, attempt, manifest, envelope = await continuation_fixture(task_runtime, monkeypatch)
    async with sessions() as db:
        await planner.continue_plan(db, owner, parent=parent, task=task, attempt=attempt,
            manifest=manifest, envelope=envelope, request_key="original-period-contact")
    async with sessions() as db:
        from pathlib import Path
        from config.settings import settings
        from src.workflows.inference_accounting import _continuity_lock
        await dispatcher.jobs._accounting_begin(db)
        account, rows = await dispatcher.jobs._accounting_rows(db)
        with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
            dispatcher.jobs._assert_accounting_continuity(workspace, account, rows)
            row = rows[0]
            row.period_id = "2000-01"
            if liability == "unknown":
                row.state = "unknown"
            elif liability == "missing_settled_cost":
                row.actual_cost_microusd = None
            else:
                row.actual_cost_microusd = envelope.proposal_group.max_cost_microusd + 1
            await dispatcher.jobs._persist_accounting_witness(db, workspace, account, rows)
            await db.commit()
    async with sessions() as db:
        from src.workflows.general_task_accounting import reserve_entry
        from src.workflows.inference_accounting import InferenceAccountingError
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        original_run = await dispatcher.jobs._fetch(db, rows[0].job_id)
        with pytest.raises(InferenceAccountingError, match="group_unknown|group_cost_limit"):
            await reserve_entry(db, original_run, rows, {"group": envelope.proposal_group},
                operation_id="new-period-probe", bound=0, runtime_path="general_task_planner")
        with pytest.raises(BoardError):
            await planner.continue_plan(db, owner, parent=parent, task=task, attempt=attempt,
                manifest=manifest, envelope=envelope, request_key="new-period-cannot-reset-group")
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        assert len(rows) == 1 and rows[0].period_id == "2000-01"
    assert len(transport["contacts"]) == 1
