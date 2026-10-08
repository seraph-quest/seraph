"""Real local MCP approval pause/continuation; all external sockets are denied."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from src.approval.repository import approval_repository, approval_state_revision
from src.db.models import ApprovalRequest, WorkBoardAttempt, WorkBoardTask, WorkflowRunState
from src.native_tools.registry import ToolRegistry
from src.work_board.general_task import GeneralTaskService
from src.work_board.dispatcher import WorkBoardDispatcher
from tests.test_general_task_adapters import mcp_registry
from tests.test_general_task_planner import accounting_db, forbid_external_inference, prepare
from tests.test_work_board_m6_provider_free_journey import _goal


@pytest_asyncio.fixture
async def approval_journey(accounting_db, monkeypatch):
    from src.auth.service import authenticate_session
    from src.api import work_board as api
    jobs, owner = await prepare(accounting_db, monkeypatch)
    workspace, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    registry = ToolRegistry()
    registry.start()
    registry, manager, tool, _path, _declaration = mcp_registry.__wrapped__(workspace, registry)
    service = GeneralTaskService(registry)
    service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    monkeypatch.setattr(api, "dispatcher", dispatcher)
    goal = _goal("goal-approval", "Continue exact approved local MCP work")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    app = FastAPI()
    @app.middleware("http")
    async def current_operator(request, call_next):
        request.state.operator = await authenticate_session(owner.session_id, touch=False)
        return await call_next(request)
    app.include_router(api.router, prefix="/api")
    from src.api.approvals import router as approvals_router
    app.include_router(approvals_router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        yield SimpleNamespace(jobs=jobs, owner=owner, workspace=workspace,
            sessions=sessions, registry=registry, manager=manager, tool=tool,
            service=service, dispatcher=dispatcher, client=client, goal=goal)
    service.stop()
    registry.stop()


async def create_and_run(journey, *, steps=1):
    descriptors, tool_digest = journey.service.snapshot()
    descriptor = next(item for item in descriptors if item.tool_id == "mcp:local:repo_read")
    plan_steps = []
    for index in range(steps):
        plan_steps.append({"step_id": f"read-{index}", "tool_id": descriptor.tool_id,
            "input": {"query": "read literal repository" if index == 0 else {
                "$dependency": {"step_id": f"read-{index-1}", "pointer": "/value"}}},
            "depends_on": [] if index == 0 else [f"read-{index-1}"],
            "output_contract": descriptor.output_schema})
    body = {"goal_revision": 1, "idempotency_key": "local-approval-journey",
        "expected_plan_revision": 1, "accept": True,
        "input": {"goal_ref": journey.goal.id, "intent": "Read through the current approved tool",
            "requested_output": descriptor.output_schema, "tool_set_digest": tool_digest},
        "plan": {"revision": 1, "steps": plan_steps}}
    created = await journey.client.post("/api/work-board/general-tasks", json=body)
    assert created.status_code == 200, created.text
    card = created.json()["task"]
    assert card["status"] == "todo"
    result = await journey.dispatcher.run_pass()
    assert journey.tool.calls == 0, result
    task_id = card["task_id"]
    return task_id, result


async def create_and_pause(journey, *, steps=1, expected_status="pending"):
    task_id, result = await create_and_run(journey, steps=steps)
    plan = await get_plan(journey, task_id)
    pause = plan["approval_pause"]
    if pause is None:
        async with journey.sessions() as db:
            roots = list((await db.execute(select(WorkflowRunState))).scalars())
            tasks = list((await db.execute(select(WorkBoardTask))).scalars())
        raise AssertionError((result, [(item.block_reason, item.result_refs_json) for item in tasks], [(item.status, item.failure_reason, item.error,
            item.checkpoint_receipts_json, item.effect_receipts_json) for item in roots]))
    assert pause and pause["approval_status"] == expected_status, (result, plan)
    assert pause["can_resume"] is (expected_status == "approved")
    detail = await journey.client.get(f"/api/work-board/tasks/{task_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["attempts"][0]["readback_status"] == "pending"
    root = await journey.jobs.get_job(pause["workflow_run_id"])
    assert root["status"] == "paused", root
    assert root["failure_reason"] == "general_task_approval_required"
    assert root["attempt_count"] == 1
    assert not any(item["status"] in {"intent", "dispatched", "unknown"} for item in root["effects"])
    async with journey.sessions() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        attempt = await db.get(WorkBoardAttempt, pause["attempt_id"])
        assert task.status.value == "blocked"
        assert task.block_reason == "awaiting_approval"
        assert task.block_kind == "needs_input"
        assert attempt.ended_at is None and attempt.lease_owner is None
        assert len(list((await db.execute(select(WorkflowRunState))).scalars())) == 1
    return task_id, plan, root


async def get_plan(journey, task_id):
    response = await journey.client.get(f"/api/work-board/tasks/{task_id}/plan")
    assert response.status_code == 200, response.text
    return response.json()


def resume_body(plan):
    pause = plan["approval_pause"]
    return {"expected_revision": plan["task_revision"], "expected_plan_revision": plan["plan"]["revision"],
        **{key: pause[key] for key in ("workflow_run_id", "attempt_id", "fencing_token", "workflow_revision", "approval_id")}}


async def resume(journey, task_id, body):
    return await journey.client.post(f"/api/work-board/tasks/{task_id}/plan/resume", json=body)


def verified_file(journey, root, step_id):
    artifact = next(item["payload"] for item in root["checkpoints"]
        if item["checkpoint_id"] == "general:verified:" + step_id)
    path = journey.workspace / artifact["file_path"]
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == artifact["content_sha256"]
    assert json.loads(content)["output"] == {"value": "local repository readback"}
    assert path.stat().st_mode & 0o077 == 0
    return path, content


@pytest.mark.asyncio
async def test_api_exact_approval_continues_same_root_attempt_once(approval_journey):
    journey = approval_journey
    task_id, plan, original = await create_and_pause(journey)
    body = resume_body(plan)
    pending = await resume(journey, task_id, body)
    assert pending.status_code == 409
    assert journey.tool.calls == 0
    unchanged = await journey.jobs.get_job(original["job_id"])
    assert unchanged["revision"] == original["revision"]
    assert unchanged["status"] == "paused"
    approved = await approval_repository.resolve(body["approval_id"], "approved")
    assert approved is not None
    current = await get_plan(journey, task_id)
    assert current["approval_pause"]["can_resume"] is True, current["approval_pause"]["reason"]
    completed = await resume(journey, task_id, resume_body(current))
    assert completed.status_code == 200, completed.text
    assert completed.json()["task"]["status"] == "review"
    root = await journey.jobs.get_job(original["job_id"])
    assert root["status"] == "succeeded"
    assert root["attempt_count"] == 1
    assert root["deadline_at"] == original["deadline_at"]
    assert root["lease"]["fencing_token"] == original["lease"]["fencing_token"] + 1
    assert journey.tool.calls == 1
    _path, content = verified_file(journey, root, "read-0")
    artifact_hash = hashlib.sha256(content).hexdigest()
    proof = journey.dispatcher._workflow_readback(root, original["job_id"])
    assert proof["content_sha256"] == artifact_hash

    detail = await journey.client.get(f"/api/work-board/tasks/{task_id}")
    assert detail.status_code == 200, detail.text
    attempt_payload = detail.json()["attempts"][0]
    assert attempt_payload["readback_status"] == "verified"
    assert any(item.get("content_sha256") == artifact_hash and item.get("receipt_kind") == "readback"
        for item in attempt_payload["receipt_refs"])
    async with journey.sessions() as db:
        roots = list((await db.execute(select(WorkflowRunState))).scalars())
        attempts = list((await db.execute(select(WorkBoardAttempt))).scalars())
        assert len(roots) == len(attempts) == 1
        assert attempts[0].attempt_id == body["attempt_id"]
        assert attempts[0].workflow_run_id == original["job_id"]
        assert (await db.get(ApprovalRequest, body["approval_id"])).status == "consumed"
    replay = await resume(journey, task_id, body)
    assert replay.status_code == 409
    assert journey.tool.calls == 1
    accounting = await journey.jobs.inference_accounting_snapshot()
    assert accounting["operation_count"] == 0
    assert accounting["committed_microusd"] == accounting["unknown_microusd"] == 0


@pytest.mark.asyncio
async def test_fast_approval_before_wait_publication_continues_same_attempt(approval_journey, monkeypatch):
    journey = approval_journey
    create = approval_repository.get_or_create_pending
    inspected = {}
    async def decide_before_publication(**kwargs):
        request = await create(**kwargs)
        root = await journey.jobs.get_job(json.loads(request.details_json)["workflow_run_identity"])
        assert root["status"] == "running"
        assert not any(item.get("payload", {}).get("phase") == "approval_precontact" for item in root["checkpoints"])
        inspected.update(fingerprint=request.fingerprint, details=json.loads(request.details_json),
            summary=request.summary, risk_level=request.risk_level, expires_at=request.expires_at)
        # Actual durable decision in the interval between request creation
        # and the owner's publication, before the wrapper returns its proof.
        decision = await journey.client.post(f"/api/approvals/{request.id}/approve")
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "approved"
        assert journey.tool.calls == 0
        return request
    monkeypatch.setattr(approval_repository, "get_or_create_pending", decide_before_publication)
    task_id, plan, original = await create_and_pause(journey, expected_status="approved")
    row = await approval_repository.get(plan["approval_pause"]["approval_id"])
    assert row.status == "approved"
    assert row.fingerprint == inspected["fingerprint"]
    assert row.summary == inspected["summary"] and row.risk_level == inspected["risk_level"]
    assert row.expires_at.replace(tzinfo=timezone.utc) == inspected["expires_at"].replace(tzinfo=timezone.utc)
    details = json.loads(row.details_json)
    assert all(details[key] == value for key, value in inspected["details"].items())
    continued = await resume(journey, task_id, resume_body(plan))
    assert continued.status_code == 200, continued.text
    root = await journey.jobs.get_job(original["job_id"])
    assert root["status"] == "succeeded" and root["attempt_count"] == 1
    assert root["deadline_at"] == original["deadline_at"]
    assert journey.tool.calls == 1
    verified_file(journey, root, "read-0")
    assert (await resume(journey, task_id, resume_body(plan))).status_code == 409
    assert journey.tool.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["denied", "revoked", "expired", "context", "fingerprint",
    "root_input", "root_authority", "root_deadline", "goal_revision"])
async def test_fast_noncurrent_decision_never_publishes_resumable_wait(approval_journey, monkeypatch, race):
    journey = approval_journey
    create = approval_repository.get_or_create_pending
    async def change_before_publication(**kwargs):
        request = await create(**kwargs)
        if race == "denied":
            assert await approval_repository.resolve(request.id, "denied")
        else:
            assert await approval_repository.resolve(request.id, "approved")
            if race == "revoked":
                current = await approval_repository.get(request.id)
                assert await approval_repository.revoke_unconsumed(request.id,
                    expected_revision=approval_state_revision(current),
                    owner_principal_id=journey.owner.principal_id,
                    operator_session_id=journey.owner.session_id) == "revoked"
            else:
                async with journey.sessions() as db:
                    current = await db.get(ApprovalRequest, request.id)
                    if race == "expired":
                        current.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    elif race == "context":
                        details = json.loads(current.details_json)
                        details["approval_context"] = {"workflow_run_identity": "unrelated"}
                        current.details_json = json.dumps(details)
                    elif race == "fingerprint":
                        current.fingerprint = "0" * 64
                    else:
                        root = (await db.execute(select(WorkflowRunState))).scalar_one()
                        if race == "root_input":
                            root.input_digest = "0" * 64
                        elif race == "root_authority":
                            root.declared_authority_json = "{}"
                        elif race == "root_deadline":
                            root.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                        else:
                            from src.db.models import Goal
                            goal = await db.get(Goal, journey.goal.id)
                            goal.revision += 1
                            db.add(goal)
                        db.add(root)
                    db.add(current)
        return request
    monkeypatch.setattr(approval_repository, "get_or_create_pending", change_before_publication)
    task_id, _result = await create_and_run(journey)
    plan = await get_plan(journey, task_id)
    assert plan["approval_pause"] is None
    assert journey.tool.calls == 0
    async with journey.sessions() as db:
        root = (await db.execute(select(WorkflowRunState))).scalar_one()
        request = (await db.execute(select(ApprovalRequest))).scalar_one()
        assert "general_task_wait_binding" not in json.loads(request.details_json)
        assert not any(item.get("payload", {}).get("phase") == "approval_precontact"
            for item in json.loads(root.checkpoint_receipts_json))
        assert request.status != "consumed"
    assert not list(journey.workspace.glob("artifacts/work-board/general-tasks/*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", ["binding", "job", "board", "event"])
async def test_wait_publication_rolls_back_and_retries_only_retained_proof(approval_journey, monkeypatch, barrier):
    from sqlalchemy.ext.asyncio import AsyncSession
    journey = approval_journey
    class PublicationInterrupted(Exception):
        pass
    fired = False
    async def interrupt():
        nonlocal fired
        if not fired:
            fired = True
            raise PublicationInterrupted()
    if barrier == "binding":
        original = approval_repository.attach_general_task_wait_binding_in_session
        async def attach(*args, **kwargs):
            result = await original(*args, **kwargs)
            await interrupt()
            return result
        monkeypatch.setattr(approval_repository, "attach_general_task_wait_binding_in_session", attach)
    elif barrier == "job":
        original = AsyncSession.execute
        async def execute(db, statement, *args, **kwargs):
            result = await original(db, statement, *args, **kwargs)
            if str(statement).startswith("UPDATE workflow_run_states") and statement.compile().params.get("failure_reason") == "general_task_approval_required":
                await interrupt()
            return result
        monkeypatch.setattr(AsyncSession, "execute", execute)
    elif barrier == "board":
        original = journey.service.repository._cas_task_update
        async def board(*args, **kwargs):
            result = await original(*args, **kwargs)
            if kwargs["values"].get("block_reason") == "awaiting_approval":
                await interrupt()
            return result
        monkeypatch.setattr(journey.service.repository, "_cas_task_update", board)
    else:
        original = journey.service.repository._event
        async def event(*args, **kwargs):
            result = await original(*args, **kwargs)
            if kwargs["kind"] == "task.approval_wait":
                await interrupt()
            return result
        monkeypatch.setattr(journey.service.repository, "_event", event)
    publish = journey.dispatcher.jobs.pause_general_task_for_approval
    async def retained_proof_retry(**kwargs):
        try:
            return await publish(**kwargs)
        except PublicationInterrupted:
            # The owner still holds the actual wrapper proof. This retries
            # publication only, never the tool invocation or an approved row.
            async with journey.sessions() as db:
                row = (await db.execute(select(ApprovalRequest))).scalar_one()
                root = (await db.execute(select(WorkflowRunState))).scalar_one()
                task = (await db.execute(select(WorkBoardTask))).scalar_one()
                attempt = (await db.execute(select(WorkBoardAttempt))).scalar_one()
                assert "general_task_wait_binding" not in json.loads(row.details_json)
                assert root.status == "running" and task.status.value == "running"
                assert attempt.lease_owner and attempt.ended_at is None
                assert all(item["status"] == "intent" for item in json.loads(root.effect_receipts_json))
                assert not any(item.get("payload", {}).get("phase") == "approval_precontact"
                    for item in json.loads(root.checkpoint_receipts_json))
            assert journey.tool.calls == 0
            return await publish(**kwargs)
    monkeypatch.setattr(journey.dispatcher.jobs, "pause_general_task_for_approval", retained_proof_retry)
    task_id, plan, root = await create_and_pause(journey)
    assert fired
    await approval_repository.resolve(plan["approval_pause"]["approval_id"], "approved")
    assert (await resume(journey, task_id, resume_body(await get_plan(journey, task_id)))).status_code == 200
    completed = await journey.jobs.get_job(root["job_id"])
    assert completed["status"] == "succeeded" and completed["attempt_count"] == 1
    assert completed["deadline_at"] == root["deadline_at"]
    assert journey.tool.calls == 1
    verified_file(journey, completed, "read-0")


@pytest.mark.asyncio
async def test_oversized_contacted_mcp_output_preserves_liability_without_replay(approval_journey):
    journey = approval_journey
    task_id, plan, original = await create_and_pause(journey)
    journey.tool.result = '{"value":"' + "x" * (64 * 1024) + '"}'
    await approval_repository.resolve(plan["approval_pause"]["approval_id"], "approved")
    current = await get_plan(journey, task_id)
    response = await resume(journey, task_id, resume_body(current))
    assert response.status_code == 409, response.text
    detail = await journey.client.get(f"/api/work-board/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["task"]["status"] == "blocked"
    assert journey.tool.calls == 1
    root = await journey.jobs.get_job(original["job_id"])
    assert any(item["status"] in {"intent", "unknown", "dispatched"} for item in root["effects"])
    assert not any(item["checkpoint_id"].startswith("general:verified:") for item in root["checkpoints"])
    assert not list(journey.workspace.glob("artifacts/work-board/general-tasks/*.json"))
    assert (await resume(journey, task_id, resume_body(current))).status_code == 409
    await journey.dispatcher.run_pass()
    assert journey.tool.calls == 1
    async with journey.sessions() as db:
        assert len(list((await db.execute(select(WorkflowRunState))).scalars())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["step_id", "input_digest", "descriptor_digest", "plan_digest", "effect_id", "deadline_at"])
async def test_approved_wait_attachment_is_exact_and_idempotent(approval_journey, monkeypatch, field):
    journey = approval_journey
    create = approval_repository.get_or_create_pending
    async def fast_approve(**kwargs):
        request = await create(**kwargs)
        assert await approval_repository.resolve(request.id, "approved")
        return request
    monkeypatch.setattr(approval_repository, "get_or_create_pending", fast_approve)
    attach = approval_repository.attach_general_task_wait_binding_in_session
    async def attach_and_check(*args, **kwargs):
        request = await attach(*args, **kwargs)
        assert request is not None and request.status == "approved"
        original_details = request.details_json
        repeated = await attach(*args, **kwargs)
        assert repeated is not None and repeated.details_json == original_details
        changed = dict(kwargs)
        changed["binding"] = {**kwargs["binding"], field: "unrelated"}
        assert await attach(*args, **changed) is None
        return request
    monkeypatch.setattr(approval_repository, "attach_general_task_wait_binding_in_session", attach_and_check)
    task_id, plan, original = await create_and_pause(journey, expected_status="approved")
    assert (await resume(journey, task_id, resume_body(plan))).status_code == 200
    assert journey.tool.calls == 1
    assert (await journey.jobs.get_job(original["job_id"]))["attempt_count"] == 1


@pytest.mark.asyncio
async def test_lost_precontact_proof_is_visible_and_cannot_resume_from_approved_row(approval_journey, monkeypatch):
    journey = approval_journey
    create = approval_repository.get_or_create_pending
    async def fast_approve(**kwargs):
        request = await create(**kwargs)
        assert await approval_repository.resolve(request.id, "approved")
        return request
    monkeypatch.setattr(approval_repository, "get_or_create_pending", fast_approve)
    async def lose_publication(**kwargs):
        raise RuntimeError("owner interrupted before publication")
    monkeypatch.setattr(journey.dispatcher.jobs, "pause_general_task_for_approval", lose_publication)
    task_id, _result = await create_and_run(journey)
    detail = await journey.client.get(f"/api/work-board/tasks/{task_id}")
    assert detail.json()["task"]["status"] == "blocked"
    assert detail.json()["task"]["block_reason"] == "reconcile_external_effect"
    assert (await get_plan(journey, task_id))["approval_pause"] is None
    async with journey.sessions() as db:
        request = (await db.execute(select(ApprovalRequest))).scalar_one()
        assert request.status == "approved"
        assert "general_task_wait_binding" not in json.loads(request.details_json)
    fresh = WorkBoardDispatcher(session_provider=journey.sessions, general_tasks=journey.service)
    await fresh.run_pass()
    assert journey.tool.calls == 0
    assert (await get_plan(journey, task_id))["approval_pause"] is None


def test_no_contact_settlement_cannot_prove_intended_task_output():
    from src.workflows.job_runtime import _verified_readback_exists
    effect = {"receipt_kind": "readback", "effect_type": "general_tool_call",
        "status": "succeeded", "content_sha256": "a" * 64,
        "readback_id": "general-precontact:fixture", "verified_at": datetime.now(timezone.utc).isoformat(),
        "details": {"verified": True, "never_contacted": True, "approval_precontact": True}}
    assert _verified_readback_exists([effect]) is False
    assert WorkBoardDispatcher._workflow_readback({"run_identity": "fixture-root", "effects": [effect]},
        "fixture-root") is None


@pytest.mark.asyncio
async def test_two_approval_steps_preserve_first_verified_output(approval_journey):
    journey = approval_journey
    task_id, first, original = await create_and_pause(journey, steps=2)
    await approval_repository.resolve(first["approval_pause"]["approval_id"], "approved")
    first_resume = await resume(journey, task_id, resume_body(await get_plan(journey, task_id)))
    assert first_resume.status_code == 200, first_resume.text
    assert first_resume.json()["task"]["status"] == "blocked"
    assert journey.tool.calls == 1
    second = await get_plan(journey, task_id)
    pause = second["approval_pause"]
    assert pause["step_id"] == "read-1" and pause["approval_status"] == "pending"
    assert pause["workflow_run_id"] == first["approval_pause"]["workflow_run_id"]
    assert pause["attempt_id"] == first["approval_pause"]["attempt_id"]
    root = await journey.jobs.get_job(original["job_id"])
    path, initial_bytes = verified_file(journey, root, "read-0")
    await approval_repository.resolve(pause["approval_id"], "approved")
    completed = await resume(journey, task_id, resume_body(await get_plan(journey, task_id)))
    assert completed.status_code == 200, completed.text
    assert completed.json()["task"]["status"] == "review"
    assert journey.tool.calls == 2
    root = await journey.jobs.get_job(original["job_id"])
    assert root["attempt_count"] == 1 and root["deadline_at"] == original["deadline_at"]
    assert path.read_bytes() == initial_bytes
    verified_file(journey, root, "read-1")
    assert len(list(journey.workspace.glob("artifacts/work-board/general-tasks/*.json"))) == 2
    async with journey.sessions() as db:
        assert len(list((await db.execute(select(WorkflowRunState))).scalars())) == 1
        attempts = list((await db.execute(select(WorkBoardAttempt))).scalars())
        assert len(attempts) == 1 and attempts[0].attempt_id == pause["attempt_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["pending", "revoked", "expired", "input_checkpoint",
    "unknown_effect", "root_owner", "root_deadline", "descriptor_revision",
    "approval_context", "fingerprint", "deadline_extension"])
async def test_changed_approval_or_binding_denies_without_contact(approval_journey, changed):
    journey = approval_journey
    task_id, plan, original = await create_and_pause(journey)
    body = resume_body(plan)
    if changed != "pending":
        assert await approval_repository.resolve(body["approval_id"], "approved")
    if changed == "revoked":
        async with journey.sessions() as db:
            row = await db.get(ApprovalRequest, body["approval_id"])
            revision = approval_state_revision(row)
        assert await approval_repository.revoke_unconsumed(body["approval_id"],
            expected_revision=revision, owner_principal_id=journey.owner.principal_id,
            operator_session_id=journey.owner.session_id) == "revoked"
    async with journey.sessions() as db:
        approval = await db.get(ApprovalRequest, body["approval_id"])
        root = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == original["job_id"]))
        if changed == "expired":
            approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif changed == "approval_context":
            details = json.loads(approval.details_json)
            details["approval_context"] = {"foreign_job": "other-root"}
            approval.details_json = json.dumps(details)
        elif changed == "fingerprint":
            approval.fingerprint = "f" * 64
        elif changed == "input_checkpoint":
            checkpoints = json.loads(root.checkpoint_receipts_json)
            pending = next(item for item in checkpoints if item["checkpoint_id"] == "general:step:read-0")
            pending["payload"]["input_digest"] = "0" * 64
            root.checkpoint_receipts_json = json.dumps(checkpoints)
        elif changed == "unknown_effect":
            effects = json.loads(root.effect_receipts_json)
            effects[0]["status"] = "unknown"
            root.effect_receipts_json = json.dumps(effects)
        elif changed == "root_owner":
            root.owner_principal_id = "operator:root:unrelated"
        elif changed == "root_deadline":
            root.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif changed == "deadline_extension":
            root.deadline_at += timedelta(minutes=1)
        db.add(approval)
        db.add(root)
    if changed == "descriptor_revision":
        journey.manager._connection_revisions["local"] += 1
    denied = await resume(journey, task_id, body)
    assert denied.status_code == 409, (changed, denied.text)
    assert journey.tool.calls == 0
    root = await journey.jobs.get_job(original["job_id"])
    assert root["status"] == "paused"
    assert root["attempt_count"] == 1 and root["revision"] == original["revision"]
    assert root["lease"]["fencing_token"] == original["lease"]["fencing_token"]
    assert not list(journey.workspace.glob("artifacts/work-board/general-tasks/*.json"))
    async with journey.sessions() as db:
        attempts = list((await db.execute(select(WorkBoardAttempt))).scalars())
        assert len(attempts) == 1 and attempts[0].ended_at is None
        approval = await db.get(ApprovalRequest, body["approval_id"])
        assert approval.status != "consumed"
        if changed == "revoked":
            assert approval.status == "denied"


@pytest.mark.asyncio
async def test_concurrent_exact_resumes_make_one_contact(approval_journey):
    journey = approval_journey
    task_id, plan, original = await create_and_pause(journey)
    assert await approval_repository.resolve(plan["approval_pause"]["approval_id"], "approved")
    body = resume_body(await get_plan(journey, task_id))
    responses = await asyncio.gather(resume(journey, task_id, body), resume(journey, task_id, body))
    assert sorted(response.status_code for response in responses) == [200, 409], [r.text for r in responses]
    assert journey.tool.calls == 1
    root = await journey.jobs.get_job(original["job_id"])
    assert root["status"] == "succeeded" and root["attempt_count"] == 1
    assert root["deadline_at"] == original["deadline_at"]
    verified_file(journey, root, "read-0")


@pytest.mark.asyncio
async def test_corrupt_prior_physical_output_blocks_second_contact(approval_journey):
    journey = approval_journey
    task_id, first, original = await create_and_pause(journey, steps=2)
    assert await approval_repository.resolve(first["approval_pause"]["approval_id"], "approved")
    first_result = await resume(journey, task_id, resume_body(await get_plan(journey, task_id)))
    assert first_result.status_code == 200, first_result.text
    assert journey.tool.calls == 1
    second = await get_plan(journey, task_id)
    assert second["approval_pause"]["step_id"] == "read-1"
    assert await approval_repository.resolve(second["approval_pause"]["approval_id"], "approved")
    root = await journey.jobs.get_job(original["job_id"])
    path, _initial_bytes = verified_file(journey, root, "read-0")
    before = await get_plan(journey, task_id)
    assert before["approval_pause"]["can_resume"] is True
    body = resume_body(before)
    path.write_bytes(b'{"step_id":"read-0","output":{"value":"tampered"}}')
    denied = await resume(journey, task_id, body)
    assert denied.status_code == 409, denied.text
    assert journey.tool.calls == 1
    unchanged = await journey.jobs.get_job(original["job_id"])
    assert unchanged["status"] == "paused" and unchanged["revision"] == root["revision"]
    assert unchanged["deadline_at"] == original["deadline_at"]
    assert unchanged["attempt_count"] == 1
    assert not any(item["checkpoint_id"] == "general:verified:read-1" for item in unchanged["checkpoints"])
    assert len(list(journey.workspace.glob("artifacts/work-board/general-tasks/*.json"))) == 1
