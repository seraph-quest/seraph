"""Actual native delegate claim/reservation and literal governed planning."""
from tests.general_task_method_lifecycle import native_admission_lifecycle
import json

import pytest
from sqlalchemy import select, update

from config.settings import settings
from src.db.models import InferenceCostReservation, WorkflowRunState
from src.native_tools.task_adapters import ToolRegistry
from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, TaskLimits, WorkBoardOwner
from src.work_board.general_task import digest
from src.work_board.repository import BoardError
from src.workflows.inference_accounting import InferenceAccountingError
from tests.test_general_task_native_guard import running_task
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.general_task_test_transport import prepare_literal_planner


async def specialist_fixture(task_runtime, monkeypatch):
    monkeypatch.setattr(settings, "use_delegation", True)
    registry = ToolRegistry()
    registry.start()
    # The production service attaches itself before exposing the descriptor.
    from src.work_board.general_task import GeneralTaskService
    bootstrap = GeneralTaskService(registry)
    bootstrap.start()
    descriptors = registry.descriptors()
    by_id = {item.tool_id: item for item in descriptors}
    delegate = by_id["delegate_task"]
    read = by_id["read_file"]
    literal = {"role": "files", "instruction": "Read notes.txt through the explicitly granted tool",
        "evidence_refs": [], "allowed_tool_ids": ["read_file"],
        "limits": {"max_steps": 1, "max_inference_calls": 1, "max_cost_microusd": 100, "wall_seconds": 300}}
    creation = GeneralTaskCreate(goal_revision=1, idempotency_key="specialist-accounting-root",
        expected_plan_revision=1, input=GeneralTaskInput(goal_ref="goal-1", intent="Delegate one bounded file task",
            requested_output=delegate.output_schema,
            tool_set_digest=digest([item.model_dump(mode="json") for item in sorted(descriptors,key=lambda item:item.tool_id)]),
            limits=TaskLimits(max_steps=4, max_inference_calls=2, max_cost_microusd=1000, wall_seconds=600),
            inference_egress_acknowledged=True),
        plan=PlanSpec(revision=1, steps=[{"step_id": "delegate", "tool_id": "delegate_task",
            "input": literal, "output_contract": delegate.output_schema}]))
    bootstrap.stop()  # Release the metadata-only lifecycle before the real owner starts.
    registry.stop()
    registry.start()
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime,
        creation_request=creation, registry_override=registry)
    from src.work_board.general_task_native import admit_native_step, publish_positive_claim
    binding, _ = await admit_native_step(dispatcher.jobs, original["job"]["job_id"],
        owner=original["job"]["lease"]["owner"], fence=original["job"]["lease"]["fencing_token"],
        step=envelope.plan.steps[0], descriptor=delegate, inputs=literal, service=service)
    await dispatcher.jobs.queue_job(binding.invocation_id)
    callback = await dispatcher.jobs.claim_job(binding.invocation_id, owner="original-specialist-callback")
    await publish_positive_claim(dispatcher.jobs, binding, child_owner=callback["lease"]["owner"],
        child_fence=callback["lease"]["fencing_token"])
    from src.workflows.specialist_delegation import reserve_delegation, current_delegation
    await reserve_delegation(dispatcher.jobs, binding.invocation_id, service=service,
        owner=callback["lease"]["owner"], fence=callback["lease"]["fencing_token"])
    async with sessions() as db:
        context = await current_delegation(db, binding.invocation_id)
        group = context.envelope.proposal_group
        accounting = {"task_id": context.task.task_id, "task_attempt_id": context.attempt.attempt_id,
            "plan_revision": context.manifest.plan_revision, "selected_grant_digest": context.manifest.selected_grant_digest,
            "parent_owner": context.callback.lease_owner, "parent_fence": context.callback.fencing_token,
            "delegation_invocation_id": context.callback.run_identity,
            "delegation_request_digest": digest(context.request.model_dump(mode="json"))}
        owner = WorkBoardOwner(principal_id=group.owner_principal_id, session_id=group.owner_session_id)
        child_input = GeneralTaskInput(goal_ref=group.goal_id, intent=context.request.instruction,
            evidence_refs=context.request.evidence_refs, requested_output={"type": "object"},
            limits=context.envelope.task_input.limits, inference_egress_acknowledged=True)
    plan = PlanSpec(revision=1, steps=[{"step_id":"read", "tool_id":"read_file",
        "input":{"file_path":"notes.txt"}, "output_contract":read.output_schema}])
    planner, transport = await prepare_literal_planner(sessions, task_runtime[1], monkeypatch, owner, plan)
    return sessions, dispatcher, planner, transport, owner, group, accounting, child_input, [read], context.envelope.proposal_provenance


@pytest.mark.asyncio
async def test_actual_specialist_reserve_contact_and_restart_keep_original_group(task_runtime, monkeypatch, native_admission_lifecycle):
    sessions, dispatcher, planner, transport, owner, group, binding, inputs, descriptors, provenance = await specialist_fixture(task_runtime, monkeypatch)
    async with sessions() as db:
        result = await planner.propose_specialist(db, owner, group=group, binding=binding,
            task_input=inputs, descriptors=descriptors, original_provenance=provenance, request_key="first")
    assert result.plan.revision == 1 and result.group == group and result.provenance is provenance
    assert len(transport["contacts"]) == 1
    from src.work_board.general_task_planner import GeneralTaskPlanner
    async with sessions() as db:
        with pytest.raises(BoardError, match="operation exists"):
            await GeneralTaskPlanner().propose_specialist(db, owner, group=group, binding=binding,
                task_input=inputs, descriptors=descriptors, original_provenance=provenance, request_key="restart-new-key")
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        assert len(rows) == 1 and rows[0].state == "settled" and rows[0].actual_cost_microusd == 0
        evidence = next(item for item in json.loads(rows[0].evidence_json) if item.get("kind") == "general_task_group_reservation.v1")
        assert evidence["group"] == group.model_dump(mode="json") and evidence["call_ordinal"] == 1
        assert evidence["role"] == "specialist" and evidence["delegation_invocation_id"] == binding["delegation_invocation_id"]
        assert evidence["delegation_request_digest"] == binding["delegation_request_digest"]
        callback = await dispatcher.jobs._fetch(db, binding["delegation_invocation_id"])
        assert callback.attempt_count == 1 and callback.fencing_token == binding["parent_fence"]
    assert len(transport["contacts"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["callback", "request_digest", "fence"])
async def test_foreign_original_specialist_binding_denies_before_reservation_or_contact(task_runtime, monkeypatch, drift, native_admission_lifecycle):
    sessions, dispatcher, planner, transport, owner, group, binding, inputs, descriptors, provenance = await specialist_fixture(task_runtime, monkeypatch)
    changed = {**binding, **({"delegation_invocation_id":"foreign-original-callback"} if drift=="callback"
        else {"delegation_request_digest":"f"*64} if drift=="request_digest" else {"parent_fence":binding["parent_fence"]+1})}
    async with sessions() as db:
        with pytest.raises((BoardError, InferenceAccountingError)):
            await planner.propose_specialist(db, owner, group=group, binding=changed,
                task_input=inputs, descriptors=descriptors, original_provenance=provenance, request_key="foreign")
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
        callback = await dispatcher.jobs._fetch(db, binding["delegation_invocation_id"])
        assert callback.fencing_token == binding["parent_fence"] and callback.attempt_count == 1
    assert transport["contacts"] == []


@pytest.mark.asyncio
async def test_actual_callback_drift_after_reserve_denies_scripted_contact(task_runtime, monkeypatch, native_admission_lifecycle):
    sessions, dispatcher, planner, transport, owner, group, binding, inputs, descriptors, provenance = await specialist_fixture(task_runtime, monkeypatch)
    from src.workflows.job_runtime import DurableJobRepository
    original = DurableJobRepository.contact_inference_provider
    async def drift_before_contact(repository, operation_id, **kwargs):
        async with sessions() as db:
            await db.execute(update(WorkflowRunState).where(
                WorkflowRunState.run_identity == binding["delegation_invocation_id"])
                .values(fencing_token=binding["parent_fence"] + 1))
            await db.commit()
        return await original(repository, operation_id, **kwargs)
    monkeypatch.setattr(DurableJobRepository, "contact_inference_provider", drift_before_contact)
    async with sessions() as db:
        with pytest.raises((BoardError, InferenceAccountingError)):
            await planner.propose_specialist(db, owner, group=group, binding=binding,
                task_input=inputs, descriptors=descriptors, original_provenance=provenance, request_key="drift")
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        assert len(rows) == 1 and rows[0].contact_started_at is None and rows[0].bound_microusd == 100
    assert transport["contacts"] == []


@pytest.mark.asyncio
async def test_actual_specialist_child_cap_and_unknown_debt_cannot_reset_group(task_runtime, monkeypatch, native_admission_lifecycle):
    sessions, dispatcher, planner, transport, owner, group, binding, inputs, descriptors, provenance = await specialist_fixture(task_runtime, monkeypatch)
    async with sessions() as db:
        await planner.propose_specialist(db, owner, group=group, binding=binding,
            task_input=inputs, descriptors=descriptors, original_provenance=provenance, request_key="one")
    from src.workflows.general_task_accounting import reserve_entry
    async with sessions() as db:
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        run = await dispatcher.jobs._fetch(db, rows[0].job_id)
        original_evidence = rows[0].evidence_json
        with pytest.raises(InferenceAccountingError, match="specialist_call_limit"):
            await reserve_entry(db, run, rows, {"group":group,"role":"specialist",**binding},
                operation_id="forbidden-second-child-call", bound=100, runtime_path="general_task_planner")
        assert rows[0].evidence_json == original_evidence
    from pathlib import Path
    from src.workflows.inference_accounting import _continuity_lock
    async with sessions() as db:
        await dispatcher.jobs._accounting_begin(db)
        account, rows = await dispatcher.jobs._accounting_rows(db)
        with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
            dispatcher.jobs._assert_accounting_continuity(workspace, account, rows)
            rows[0].state = "unknown"
            await dispatcher.jobs._persist_accounting_witness(db, workspace, account, rows)
            await db.commit()
    async with sessions() as db:
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        run = await dispatcher.jobs._fetch(db, rows[0].job_id)
        with pytest.raises(InferenceAccountingError, match="group_unknown"):
            await reserve_entry(db, run, rows, {"group":group,"role":"specialist",**binding},
                operation_id="forbidden-restart-call", bound=100, runtime_path="general_task_planner")
        assert len(rows) == 1 and rows[0].state == "unknown" and rows[0].bound_microusd == 100
    assert len(transport["contacts"]) == 1
