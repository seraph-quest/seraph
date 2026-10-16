"""Natural bug request -> governed C1 plan -> reviewed repository_work -> local patch and pytest.

Inference is replaced only at its owned HTTP transports: the planner's
``httpx.AsyncClient`` and the repository iteration's ``httpx.Client``. Every
other socket is denied by ``forbid_external_inference``; no provider is paid.
"""
import json

import httpx
import pytest
from sqlalchemy import select

from src.db.models import InferenceCostReservation, WorkBoardTask
from src.work_board.contracts import GeneralTaskCreate, WorkBoardActionRequest, WorkBoardStatus
from src.work_board.general_task_planner import GeneralTaskPlanner
from src.workflows.repo_repair import RepoWorkInput
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_work_task_publication import actual_native_source


NATURAL_REQUEST = ("Bug: in the example repository, calculator.add(1, 2) returns -1 instead of 3. "
                   "Fix calculator.py and run the repository's tests.")


def _scripted_planner(monkeypatch, plan, calls):
    """The model's literal reply at the planner's own transport boundary."""
    class Reply(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({"id": "scripted-natural-planner", "usage": {"cost": "0"},
                "choices": [{"message": {"role": "assistant", "content": json.dumps(plan)}}]}).encode()

    class PlannerTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
            body = json.loads(request.content)
            planning = json.loads(body["messages"][1]["content"])
            calls.append(planning)
            return httpx.Response(200, request=request, stream=Reply())

    original = httpx.AsyncClient
    def owned(*args, **kwargs):
        kwargs["transport"] = PlannerTransport()
        return original(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", owned)


def _plan_review_and_accept(monkeypatch, planner_calls, reviews):
    async def create(factory, owner, service, literal):
        # The literal fixture request supplies only the server-inspected
        # RepoWorkInput the scripted model will propose for this bug report.
        work = literal.plan.steps[0]
        _scripted_planner(monkeypatch, {"schema_version": 1, "revision": 1, "steps": [{
            "step_id": "repair", "tool_id": "repository_work", "input": work.input,
            "depends_on": [], "output_contract": work.output_contract}]}, planner_calls)
        service.planner = GeneralTaskPlanner()
        request = GeneralTaskCreate(goal_revision=1, idempotency_key="natural-bug-request",
            input=literal.input.model_copy(update={"intent": NATURAL_REQUEST,
                "inference_egress_acknowledged": True}))
        assert request.plan is None and request.accept is False
        async with factory.accounting_sessions() as db:
            proposed = await service.create(db, owner, request)
        assert proposed.task.status == WorkBoardStatus.triage
        # Operator review of the inert proposal: exact repository step and input.
        async with factory.accounting_sessions() as db:
            review = await service.plan(db, owner, proposed.task.task_id)
        reviews.append(review)
        async with factory.accounting_sessions() as db:
            stage = await service.validate_acceptance(db, owner, proposed.task.task_id,
                proposed.task.task_revision)
            accepted = await service.repository.action_task(db, owner, proposed.task.task_id,
                WorkBoardActionRequest(action="promote", expected_revision=proposed.task.task_revision),
                accepted_method_stage=stage)
        assert accepted.task.status == WorkBoardStatus.todo
        return accepted
    return create


@pytest.mark.asyncio
async def test_natural_bug_request_plans_reviewed_repository_work_and_returns_patch_and_pytest(
        accounting_db, monkeypatch, repository_admission_signer):
    from src.auth.service import authenticate_session
    from src.api.workflows import RepoRepairEgressConsentRequest, RepoRepairResumeRequest
    from src.approval.repository import approval_repository, approval_decision_digest
    from src.db.models import RepoRepairProposal
    from src.llm_runtime import FallbackLiteLLMModel
    from src.workflows.repo_repair_source import (_repository_record, execute_repository_iteration,
        grant_repository_iteration_consent, prepare_repository_native_source,
        read_repository_original, repository_source_preview)

    planner_calls, reviews = [], []
    factory, owner, service, jobs, binding, literal = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False,
        work_limits={"max_iterations": 2, "max_total_seconds": 120, "max_cost_usd": 0.0010019},
        create=_plan_review_and_accept(monkeypatch, planner_calls, reviews))

    # The governed planner saw the natural request and the registered schema.
    assert len(planner_calls) == 1
    assert planner_calls[0]["intent"] == NATURAL_REQUEST
    assert [tool["tool_id"] for tool in planner_calls[0]["registered_tools"]] == ["repository_work"]
    review = reviews[0]
    assert review["accepted"] is False and review["proposal_provenance"] is not None
    (step,) = review["plan"]["steps"]
    assert step["tool_id"] == "repository_work"
    work = RepoWorkInput.model_validate(step["input"])
    assert work == RepoWorkInput.model_validate(literal.plan.steps[0].input)
    assert work.repository_ref == "example" and work.requested_checks == ["test"]
    async with factory() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        assert task.title == NATURAL_REQUEST[:200]
        planning = list((await db.scalars(select(InferenceCostReservation))).all())
        assert [row.runtime_path for row in planning] == ["general_task_planner"]
        assert planning[0].state == "settled"

    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner="natural-intent-worker", principal=operator.principal)
    root_id = prepared["repository_job_id"]
    preview = await repository_source_preview(service.repository_source_service, jobs,
        job_id=root_id, owner=owner)
    repository_review, egress, packet = preview["repository_review"], preview["egress"], preview["source_packet"]
    source = json.loads(egress["request_body"]["messages"][1]["content"])
    output = {"summary": "Correct addition",
        "base_snapshot_sha256": source["source_packet"]["base_snapshot_sha256"],
        "patch_unified_diff": "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n-    return a - b\n+    return a + b\n",
        "allowed_paths": ["calculator.py", "tests/test_calculator.py"],
        "test_args": ["pytest", "-q", "tests/test_calculator.py"],
        "expected_outcome": "The addition check passes"}
    contacted = []
    def iteration_http(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert json.loads(request.content) == egress["request_body"]
        contacted.append(request)
        return httpx.Response(200, json={"id": "scripted-natural-iteration",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": "0"},
            "choices": [{"message": {"role": "assistant", "content": json.dumps(output)}}]})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs:
        real_client(*args, **kwargs, transport=httpx.MockTransport(iteration_http)))
    deadlines = []
    real_generate = FallbackLiteLLMModel.generate
    def observed_generate(self, *args, **kwargs):
        deadlines.append(kwargs["request_context"].deadline_at)
        return real_generate(self, *args, **kwargs)
    monkeypatch.setattr(FallbackLiteLLMModel, "generate", observed_generate)

    consent = RepoRepairEgressConsentRequest(expected_job_revision=preview["revision"],
        source_packet_digest=packet["artifact_sha256"],
        expected_source_manifest_digest=packet["source_manifest_sha256"],
        expected_profile_id=egress["effective_profile_id"], acknowledged_selected_source=True,
        idempotency_key="natural-first-contact", expected_iteration_index=repository_review["iteration_index"],
        expected_iteration_id=repository_review["iteration_id"],
        expected_preparation_digest=repository_review["preparation_digest"],
        expected_request_body_digest=egress["request_body_digest"],
        expected_request_route_digest=egress["request_route_digest"],
        expected_egress_envelope_digest=egress["egress_envelope_digest"],
        expected_diagnostics_digest=egress["diagnostics_digest"],
        expected_redaction_version=egress["redaction_version"], acknowledged_diagnostics=True)
    granted = await grant_repository_iteration_consent(service.repository_source_service, jobs,
        job_id=root_id, owner=owner, request=consent, general_task_service=service,
        principal=operator.principal)
    assert granted["repository_outcome"]["awaiting_repository_wait"] is True
    assert len(contacted) == 1
    # The accepted original cutoff bounds the actual inference request.
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        from datetime import datetime
        cutoff = datetime.fromisoformat(read_repository_original(root)[0]["original_deadline_at"])
    assert len(deadlines) == 1 and deadlines[0] <= cutoff.timestamp()

    async with factory() as db:
        proposal = await db.get(RepoRepairProposal, "repository-proposal:" + repository_review["iteration_id"])
    approval = await approval_repository.get(proposal.approval_id)
    await approval_repository.resolve_exact(approval.id, "approved",
        expected_digest=approval_decision_digest(approval), owner_principal_id=owner.principal_id,
        operator_session_id=owner.session_id)
    executed = await execute_repository_iteration(service.repository_source_service, jobs,
        job_id=root_id, owner=owner, request=RepoRepairResumeRequest(approval_id=proposal.approval_id,
            proposal_id=proposal.proposal_id, expected_proposal_revision=proposal.revision,
            expected_job_revision=(await jobs.get_job(root_id))["revision"],
            idempotency_key="natural-approved-iteration"),
        principal=operator.principal)
    assert executed["status"] == "succeeded" and executed["cleanup_proven"] is True
    assert (await jobs.get_job(root_id))["status"] == "succeeded"

    # One task produced the local patch and the actual requested pytest output.
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        readback = _repository_record(root, "repository:readback:" + repository_review["iteration_id"])
    assert readback["command_results"] == [{"check": "test", "status": "succeeded", "exit_code": 0}]
    diagnostics = json.loads(service.repository_source_service._read_private_artifact(
        readback["diagnostics_artifact_ref"], expected_digest=readback["diagnostics_artifact_digest"]))
    assert "+    return a + b" in diagnostics["cumulative_diff"]
    assert "1 passed" in diagnostics["stdout"]
    # The live selected tree is never overwritten by the verified patch.
    live = accounting_db[0] / "example" / "calculator.py"
    assert live.read_text() == "def add(a, b):\n    return a - b\n"
    assert len(contacted) == 1
