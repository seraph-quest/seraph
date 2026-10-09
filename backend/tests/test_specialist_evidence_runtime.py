"""Actual local evidence producer -> copied specialist tool input, no provider."""
from tests.general_task_method_lifecycle import native_admission_lifecycle
from dataclasses import replace
import json
import pytest
from sqlalchemy import select

from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION, _goal


async def copied_evidence_fixture(task_runtime, monkeypatch, *, delegate_input_mutator=None,
                                  producer_content="explicit copied bytes"):
    from config.settings import settings
    from src.native_tools.registry import ToolRegistry
    from src.work_board.general_task import GeneralTaskService, digest
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, TaskLimits, WorkBoardOwner
    from src.work_board.review import complete_review
    from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState
    from tests.test_general_task_native_guard import running_task
    from tests.general_task_test_transport import prepare_literal_planner
    from src.auth.service import authenticate_session
    sessions, workspace = task_runtime
    monkeypatch.setattr("src.workflows.job_runtime.get_session", sessions)
    monkeypatch.setattr(settings, "use_delegation", True)
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    registry = ToolRegistry()
    registry.start()
    producer_service = GeneralTaskService(registry)
    producer_service.start()
    descriptors = registry.descriptors()
    by_id = {item.tool_id:item for item in descriptors}
    tool_digest = digest([item.model_dump(mode="json") for item in sorted(descriptors,key=lambda item:item.tool_id)])
    (workspace/"selected.txt").write_text(producer_content,encoding="utf-8")
    async with sessions() as db:
        db.add(_goal("producer-goal", "Explicit evidence producer"))
    async with sessions() as db:
        producer = (await producer_service.create(db, owner, GeneralTaskCreate(goal_revision=1,
            idempotency_key="real-evidence-producer",accept=True,expected_plan_revision=1,
            input=GeneralTaskInput(goal_ref="producer-goal",intent="Read selected local source",
                requested_output=by_id["read_file"].output_schema,tool_set_digest=tool_digest,
                limits=TaskLimits(max_steps=1,max_inference_calls=0,max_cost_microusd=0,wall_seconds=600)),
            plan=PlanSpec(revision=1,steps=[{"step_id":"read","tool_id":"read_file",
                "input":{"file_path":"selected.txt"},"output_contract":by_id["read_file"].output_schema}])))).task
        producer_id = producer.task_id
    producer_dispatcher = WorkBoardDispatcher(session_provider=sessions,general_tasks=producer_service)
    receipt = await producer_dispatcher.run_pass()
    assert receipt["completed"] == 1, receipt
    async with sessions() as db:
        producer = await producer_service.repository.get_task(db,owner,producer_id)
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == producer_id))
        await complete_review(db,owner,producer_id,expected_revision=producer.task_revision,
            attempt_id=attempt.attempt_id,repository=producer_service.repository)
    producer_service.stop()
    registry.stop()
    registry.start()
    reference = "board-output:" + producer_id
    delegate = by_id["delegate_task"]
    literal = {"role":"files","instruction":"Copy selected evidence into an explicit local file",
        "evidence_refs":[reference],"allowed_tool_ids":["write_file"],
        "limits":{"max_steps":1,"max_inference_calls":1,"max_cost_microusd":100,"wall_seconds":300}}
    if delegate_input_mutator is not None:
        literal = delegate_input_mutator(literal)
    request = GeneralTaskCreate(goal_revision=1,idempotency_key="copied-evidence-parent",expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref="goal-1",intent="Delegate copied explicit evidence",
            evidence_refs=[reference],requested_output=delegate.output_schema,tool_set_digest=tool_digest,
            limits=TaskLimits(max_steps=2,max_inference_calls=1,max_cost_microusd=1000,wall_seconds=600),
            inference_egress_acknowledged=True),
        plan=PlanSpec(revision=1,steps=[{"step_id":"copy","tool_id":"delegate_task",
            "input":literal,"output_contract":delegate.output_schema}]))
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime,
        creation_request=request,registry_override=registry)
    plan = PlanSpec(revision=1,steps=[{"step_id":"write","tool_id":"write_file",
        "input":{"file_path":"copied-result.txt","content":{"from_evidence":reference,
            "pointer":"/output/content"}},"output_contract":by_id["write_file"].output_schema}])
    planner, transport = await prepare_literal_planner(sessions,workspace,monkeypatch,owner,plan)
    service.planner = planner
    return sessions, workspace, owner, dispatcher, service, envelope, original, reference, plan, planner, transport


async def reserve_evidence_specialist(fixture):
    sessions, workspace, owner, dispatcher, service, envelope, original, reference, plan, planner, transport = fixture
    from src.work_board.general_task_native import admit_native_step, publish_positive_claim
    from src.workflows.specialist_delegation import reserve_delegation, current_delegation
    from src.auth.service import authenticate_session
    step = envelope.plan.steps[0]
    descriptor = next(item for item in envelope.descriptors if item.tool_id == step.tool_id)
    binding, _ = await admit_native_step(dispatcher.jobs,original["job"]["job_id"],
        owner=original["job"]["lease"]["owner"],fence=original["job"]["lease"]["fencing_token"],
        step=step,descriptor=descriptor,inputs=step.input,service=service)
    await dispatcher.jobs.queue_job(binding.invocation_id)
    callback = await dispatcher.jobs.claim_job(binding.invocation_id,owner="original-copied-evidence-callback")
    await publish_positive_claim(dispatcher.jobs,binding,child_owner=callback["lease"]["owner"],
        child_fence=callback["lease"]["fencing_token"])
    await reserve_delegation(dispatcher.jobs,binding.invocation_id,service=service,
        owner=callback["lease"]["owner"],fence=callback["lease"]["fencing_token"])
    service.delegation_jobs = dispatcher.jobs
    operator = await authenticate_session(owner.session_id,touch=False)
    async with sessions() as db:
        context = await current_delegation(db,binding.invocation_id)
    return context, replace(operator.principal,session_id=owner.session_id,
        operator_session_id=owner.session_id,job_id=binding.invocation_id)


@pytest.mark.asyncio
async def test_real_copied_evidence_is_consumed_by_specialist_tool(task_runtime, monkeypatch, native_admission_lifecycle):
    from src.db.models import WorkBoardTask
    from src.auth.service import authenticate_session
    fixture = await copied_evidence_fixture(task_runtime,monkeypatch)
    sessions, workspace, owner, dispatcher, service, envelope, original, reference, plan, planner, transport = fixture
    operator = await authenticate_session(owner.session_id,touch=False)
    result = await service.execute(dispatcher.jobs,job_id=original["job"]["job_id"],
        owner=original["job"]["lease"]["owner"],fence=original["job"]["lease"]["fencing_token"],
        envelope=envelope,principal=operator.principal)
    assert result["verified"] is False, result
    for _ in range(4):
        await dispatcher.reconcile_linked_attempts()
        async with sessions() as db:
            parent=await dispatcher.jobs._fetch(db,original["job"]["job_id"])
        if parent.status=="succeeded":
            break
    assert parent.status=="succeeded"
    assert (workspace/"copied-result.txt").read_text() == "explicit copied bytes"
    assert len(transport["contacts"]) == 1
    serialized = json.dumps(transport["contacts"])
    assert "explicit copied bytes" not in serialized
    assert "selected.txt" not in serialized
    async with sessions() as db:
        children = (await db.execute(select(WorkBoardTask).where(
            WorkBoardTask.idempotency_key.like("specialist:%")))).scalars().all()
        assert len(children) == 1 and children[0].status.value == "done"
        from src.work_board.dispatcher import _parse_typed_input
        private = _parse_typed_input(children[0])
        assert private["specialist_handoff"]["schema_version"] == "SpecialistEvidenceHandoff.v1"
        assert "explicit copied bytes" not in json.dumps(private)
        public = await service.plan(db,owner,children[0].task_id)
        assert "specialist_handoff" not in public
