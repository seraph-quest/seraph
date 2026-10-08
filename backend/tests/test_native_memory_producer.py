"""Authenticated stock-host Memory mutations from a genuinely executed source."""
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from sqlmodel import SQLModel

from config.settings import settings
from src.db.engine import get_session, override_session_factory
from src.db.models import Goal, WorkBoardTask, WorkBoardAttempt, WorkBoardStatus, MemoryProposal, Memory, AuditEvent
from src.goals.contracts import GoalSuccessCriterion, CriterionVerifierKind
from src.runtime_plugins.ownership import begin_native_writer, initialize_fresh_deployment, DOMAINS
from src.work_board.dispatcher import WorkBoardDispatcher, registered_executor_id
from src.work_board.repository import WorkBoardRepository
from tests.test_work_board_adapters import _write_input


@pytest_asyncio.fixture
async def actual_memory(tmp_path, monkeypatch):
    """Produce a real retained legacy source before first composition activation."""
    from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory, maintenance_fence
    from src.auth.service import create_session
    from src.auth.middleware import OperatorAuthMiddleware
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    from src.workflows.job_runtime import DurableJobRepository
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "deployment"))
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-real-memory-auth")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    workspace = ProductionWorkspace(host_root=root)
    prepare_lifecycle_directory(workspace)
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}")
    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # The production synchronous Goal tool opens its own thread, which does
    # not inherit the task-scoped override. Bind that canonical factory too;
    # every actual owner still reads/writes the same isolated database.
    monkeypatch.setattr("src.db.engine.async_session_factory", factory)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    @asynccontextmanager
    async def sessions():
        async with get_session() as db:
            db.info["composition_writer_owner"] = "durable_jobs"
            yield db
    monkeypatch.setattr("src.workflows.job_runtime.get_session", sessions)
    with override_session_factory(factory):
        token, operator = await create_session()
        jobs = DurableJobRepository()
        monkeypatch.setattr("src.workflows.job_runtime.durable_job_repository", jobs)
        # No Done/Attempt receipt is seeded. Existing source owners perform the
        # actual canonical write/readback while this deployment is still legacy.
        task, attempt, output = await executed_source(operator, jobs, monkeypatch)
        with maintenance_fence(workspace):
            async with get_session() as db:
                await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                await initialize_fresh_deployment(db, composition_digests={domain: "a" * 64 for domain in DOMAINS})
        dispatcher = NativeServiceDispatcher(jobs=jobs)
        calls = []
        actual = dispatcher.dispatch
        async def observe(frame, **kwargs):
            calls.append(json.loads(json.dumps(frame)))
            return await actual(frame, **kwargs)
        monkeypatch.setattr(dispatcher, "dispatch", observe)
        host = CordisHost(node_path=Path(os.environ["SERAPH_CORDIS_TEST_NODE"]), service_dispatch=dispatcher)
        monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
        from src.api.memory import router
        app = FastAPI()
        app.add_middleware(OperatorAuthMiddleware)
        app.include_router(router, prefix="/api")
        try:
            assert await host.start(), host.snapshot()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                cookies={settings.operator_auth_cookie_name: token}, headers={"Origin": "http://localhost:3001"}) as client:
                yield client, operator, jobs, host, calls, task, attempt, output
        finally:
            await host.stop(preserve_blocked=host.state == "blocked")
            if host._cleanup_task is not None:
                await host._cleanup_task
            assert host.snapshot()["cleanup"]["process_reaped"] is True
    await engine.dispose()


async def executed_source(operator, jobs, monkeypatch):
    """Fresh Ready intent; production owners create Done/Attempt/readback."""
    from src.workflows.manager import workflow_manager
    from src.skills.manager import skill_manager
    from src.extensions.registry import default_manifest_roots_for_workspace
    from src.agent.factory import get_tools
    root = Path(settings.workspace_dir)
    reference, digest = _write_input(root, {"schema_version": 1,
        "capability_id": "workflow.goal-snapshot-to-file", "input": {"file_path": "artifacts/native-memory-source.md"}})
    roots = default_manifest_roots_for_workspace(str(root))
    for manager, names in ((workflow_manager, ("_workflows", "_load_errors", "_shared_manifest_errors", "_workflows_dir", "_manifest_roots", "_config_path", "_disabled", "_registry")),
        (skill_manager, ("_skills", "_load_errors", "_skills_dir", "_manifest_roots", "_config_path", "_disabled", "_registry"))):
        for name in names:
            monkeypatch.setattr(manager, name, getattr(manager, name))
    for directory in ("workflows", "skills"):
        (root / directory).mkdir(exist_ok=True)
    workflow_manager.init(str(root / "workflows"), manifest_roots=roots)
    skill_manager.init(str(root / "skills"), manifest_roots=roots)
    workflow = workflow_manager.get_workflow("goal-snapshot-to-file")
    assert workflow is not None and workflow.enabled
    assert any(getattr(tool, "name", None) == workflow.tool_name for tool in get_tools(include_bound_worker=True))
    criterion = GoalSuccessCriterion(criterion_id="native-memory-source", description="Readable source snapshot",
        verifier_kind=CriterionVerifierKind.artifact_readback, target={"file_path": "artifacts/native-memory-source.md"},
        evidence_refs=["operator:native-memory-source"])
    async with get_session() as db:
        await begin_native_writer(db, owner="native_ingress")
        goal = Goal(id="native-memory-source-goal", title="Native Memory original source", status="active", revision=1,
            owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
            success_criterion_json=criterion.model_dump_json())
        task = WorkBoardTask(task_id="native-memory-source-task", owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id, goal_id=goal.id, goal_revision=goal.revision,
            title="Write original Memory source", idempotency_key="native-memory-source",
            capability_id="workflow.goal-snapshot-to-file", typed_input_ref=reference, typed_input_digest=digest,
            executor_id=registered_executor_id("workflow.goal-snapshot-to-file"), status=WorkBoardStatus.ready)
        db.add(goal)
        db.add(task)
    board = WorkBoardRepository()
    async with get_session() as db:
        await begin_native_writer(db, owner="native_ingress")
        claim = await board.claim_ready_task(db, task.task_id, expected_revision=task.task_revision,
            lease_owner="service:work-board", actor_principal_id="service:work-board",
            actor_session_id="service:work-board:session")
        assert claim is not None
    outcome = await WorkBoardDispatcher(repository=board, jobs=jobs)._admit_execute_project(claim)
    assert outcome["admitted"] is True and outcome["completed"] is True, outcome
    async with get_session() as db:
        stored = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
        attempt = await db.get(WorkBoardAttempt, claim.attempt.attempt_id)
        assert stored.status == WorkBoardStatus.done and attempt.ended_at is not None
        from src.memory.m5 import _verified_source
        proof = await _verified_source(db, stored, requested_attempt_id=attempt.attempt_id)
        assert proof.readback["verified"] is True
    output = root / "artifacts/native-memory-source.md"
    assert output.is_file() and goal.id in output.read_text()
    return stored, attempt, output


@pytest.mark.asyncio
async def test_real_source_executes_before_native_memory_mutations(actual_memory):
    _client, operator, jobs, _host, calls, task, attempt, output = actual_memory
    assert task.status == WorkBoardStatus.done and attempt.workflow_run_id and output.is_file()
    assert calls == []  # Source capability is not a fake Memory service handler.


@pytest.mark.asyncio
async def test_authenticated_all_three_actual_stock_memory_producers(actual_memory):
    client, operator, jobs, host, calls, task, attempt, output = actual_memory
    proposal_body = {"task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "proposal-original"}
    response = await client.post("/api/memory/task-proposals/native", json=proposal_body)
    assert response.status_code == 200, response.text
    proposed = response.json()
    assert proposed["job"]["status"] == "succeeded" and proposed["result"]["memory_status"] == "proposal_only"
    proposal_id = proposed["result"]["value"]["proposal_ref"]
    async with get_session() as db:
        proposal = await db.get(MemoryProposal, proposal_id)
        assert proposal.status.value == "proposed" and proposal.source_attempt_id == attempt.attempt_id
        assert proposal.owner_session_id == operator.session_id
    review_body = {"action": "accept", "expected_revision": proposal.revision,
        "expected_preview_text_digest": proposal.preview_text_digest, "expected_task_revision": task.task_revision,
        "expected_goal_revision": task.goal_revision, "idempotency_key": "review-original"}
    response = await client.post(f"/api/memory/task-proposals/{proposal_id}/native-review", json=review_body)
    assert response.status_code == 200, response.text
    adopted = response.json()
    assert adopted["job"]["status"] == "succeeded" and adopted["result"]["memory_status"] == "reviewed_update"
    memory_id = adopted["result"]["value"]["record_ref"]
    assert adopted["result"]["value"]["receipt_ref"].startswith("native-memory-receipt:")
    response = await client.post(f"/api/memory/records/{memory_id}/native-forget",
        json={"mode": "redact", "privacy_boundary": "private", "idempotency_key": "forget-original"})
    assert response.status_code == 200, response.text
    forgotten = response.json()
    assert forgotten["job"]["status"] == "succeeded" and forgotten["result"]["memory_status"] == "forgotten"
    async with get_session() as db:
        memory = await db.get(Memory, memory_id)
        assert memory.source_session_id == operator.session_id and memory.status.value == "archived"
        assert memory.content == "[forgotten by operator]"
        assert len(list((await db.execute(select(AuditEvent).where(AuditEvent.event_type.in_(
            {"memory_learning_proposed", "memory_learning_accepted", "memory_forgotten"})))).scalars())) == 3
    assert [frame["method"] for frame in calls] == ["memory.propose", "memory.applyReviewed", "memory.forget"]
    assert all(set(frame["payload"]) in ({"request_ref"}, {"review_ref"}) for frame in calls)
    for result in (proposed, adopted, forgotten):
        assert result["job"]["attempt_count"] == 1 and result["job"]["max_attempts"] == 1
        assert "prepared_text" not in json.dumps(result) and "sanitized_text" not in json.dumps(result)
    retried = await client.post("/api/memory/task-proposals/native", json=proposal_body)
    assert retried.status_code == 200 and retried.json()["replayed"] is True
    assert "result" not in retried.json() and len(calls) == 3
