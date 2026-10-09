"""Actual authenticated ordinary pipeline consume; bounded before Report admission."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import traceback
from pathlib import Path
from importlib.machinery import SourceFileLoader

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import Goal, OperatorSession, WorkBoardInputArtifact, WorkBoardProposal, WorkBoardTask, WorkBoardStatus
from src.work_board.repository import BoardError
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests import test_native_memory_report_source_vertical as original_vertical
from tests.test_consume_input_artifact_native import _raw_rows


class _OrdinaryConsumesCommitted(Exception):
    """Only raised after two actual ordinary consumers commit and read back."""


@pytest.mark.asyncio
async def test_actual_ordinary_browser_and_dossier_consume(accounting_db, real_auth, monkeypatch, record_property):
    from src.api import auth, goals, work_board, approvals
    from src.runtime_plugins import ownership
    from src.browser.pinned_transport import PinnedBrowserTransport, PinnedBrowserResponse
    from src.guardian.opportunity_plans import fixed_browser_input, stage_accepted_plan_task
    from src.work_board import input_artifacts, pipelines
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.workflows.job_runtime import durable_job_repository
    from src.work_board.contracts import WorkBoardOwner

    root, _, factory = accounting_db
    get_session = original_vertical.canonical_session
    monkeypatch.setattr(settings, "operator_auth_idle_seconds", 3600)
    monkeypatch.setattr(settings, "browser_site_allowlist", "example.com")
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    for router, prefix in ((auth.router, "/api/auth"), (goals.router, "/api"),
            (work_board.router, "/api"), (approvals.router, "/api")):
        app.include_router(router, prefix=prefix)
    state, denials, consumes = {}, [], []
    operator_cookies = [None]
    advance_receipts = []
    active = [None, None]
    physical_reads = []
    original_begin = ownership.begin_native_writer
    original_consume = input_artifacts.consume_input_artifact
    original_resolve = input_artifacts.resolve_input_artifact_for_task
    original_errors = []
    active_denial = [None]
    def record_first_error(boundary, exc):
        if active_denial[0] is not None and isinstance(exc, BoardError):
            return
        if not original_errors:
            original_errors.append({"boundary": boundary, "type": type(exc).__name__,
                "code": getattr(exc, "code", None),
                "frames": [{"filename": frame.f_code.co_filename, "function": frame.f_code.co_name, "line": line}
                    for frame, line in traceback.walk_tb(exc.__traceback__)]})
    async def observe_resolve(*args, **kwargs):
        try:
            return await original_resolve(*args, **kwargs)
        except Exception as exc:
            record_first_error("resolve_input_artifact_for_task", exc)
            raise
    monkeypatch.setattr(input_artifacts, "resolve_input_artifact_for_task", observe_resolve)
    from src.work_board import pipeline_cpu
    def observe_reader(function, name):
        def reader(*args, **kwargs):
            db, task = active
            if db is not None and asyncio.current_task() is task:
                guard = db.info.get("composition_guard")
                assert not (guard is not None and db.in_transaction() and db.info.get("native_writer_started")), name
                physical_reads.append(name)
            return function(*args, **kwargs)
        return reader
    monkeypatch.setattr(Path, "read_bytes", observe_reader(Path.read_bytes, "Path.read_bytes"))
    monkeypatch.setattr(SourceFileLoader, "get_data", observe_reader(SourceFileLoader.get_data, "SourceFileLoader.get_data"))
    monkeypatch.setattr(input_artifacts, "_safe_file_bytes", observe_reader(input_artifacts._safe_file_bytes, "_safe_file_bytes"))
    monkeypatch.setattr(pipeline_cpu, "read_output", observe_reader(pipeline_cpu.read_output, "read_output"))

    async def prepare_original_entry():
        # Genuine original API authorization and review precede fresh composition
        # activation; no principal, Goal grant, Opportunity or Run is inserted.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"origin": "http://localhost:3001"}) as client:
            response = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
            assert response.status_code == 200, response.text
            operator = response.json()
            state["owner"] = WorkBoardOwner(principal_id=operator["principal_id"], session_id=operator["session_id"])
            now = datetime.now(timezone.utc)
            response = await client.post("/api/goals", json={"title": "Ordinary reviewed public evidence",
                "proactive_enabled": True, "admission_budget": {"reviewed_grant": True,
                    "grant_id": "ordinary-review", "max_outstanding_jobs": 2, "max_attempts": 2,
                    "max_runtime_seconds": 300, "period_started_at": now.isoformat(),
                    "period_expires_at": (now + timedelta(hours=1)).isoformat()}})
            assert response.status_code == 200, response.text
            goal = response.json()
            response = await client.post("/api/work-board/input-artifacts", json={"schema_version": 1,
                "capability_id": "browser.public-task.v1", "goal_id": goal["id"],
                "goal_revision": goal["revision"], "input": fixed_browser_input("https://example.com/public"),
                "idempotency_key": "ordinary-source-input"})
            assert response.status_code == 200, response.text
            artifact = response.json()
            response = await client.post("/api/work-board/tasks", json={"title": "Ordinary public source",
                "capability_id": "browser.public-task.v1", "goal_id": goal["id"],
                "goal_revision": goal["revision"], "input_artifact_id": artifact["artifact_id"],
                "status": "todo", "idempotency_key": "ordinary-source-task"})
            assert response.status_code == 200, response.text
            task = response.json()["task"]
            response = await client.post(f"/api/work-board/tasks/{task['task_id']}/pipeline-preview", json={
                "expected_revision": task["task_revision"], "source_input_artifact_id": artifact["artifact_id"],
                "idempotency_key": "ordinary-reviewed-pipeline"})
            assert response.status_code == 200, response.text
            preview = response.json()
            response = await client.post(f"/api/work-board/pipelines/{preview['operation_id']}/accept", json={
                "expected_revision": preview["revision"], "expected_parent_revision": preview["parent_revision"],
                "expected_digest": preview["digest"]})
            assert response.status_code == 200, response.text
            state["operation_id"] = preview["operation_id"]
            operator_cookies[0] = httpx.Cookies(client.cookies)

    async def entry_before_fresh_begin(db, *, owner, fresh=False, **kwargs):
        if fresh and not state:
            await prepare_original_entry()
        return await original_begin(db, owner=owner, fresh=fresh, **kwargs)
    monkeypatch.setattr(ownership, "begin_native_writer", entry_before_fresh_begin)

    async def mutate(model, key, changes):
        async with get_session() as changed:
            await original_begin(changed, owner="native_ingress")
            row = (await changed.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == key)
                .execution_options(populate_existing=True)) if model is WorkBoardTask
                else await changed.get(model, key, populate_existing=True))
            previous = {field: getattr(row, field) for field in changes}
            for field, value in changes.items():
                setattr(row, field, value)
            await changed.flush()
        return previous

    async def deny(owner, kwargs, name, changes=None):
        before = _raw_rows(root / "seraph.db")
        active_denial[0] = name
        try:
            with pytest.raises(BoardError) as failure:
                async with get_session() as rejected:
                    await original_consume(rejected, owner, **{**kwargs, **(changes or {})})
        finally:
            active_denial[0] = None
        after = _raw_rows(root / "seraph.db")
        assert before == after, name
        denials.append({"case": name, "raw_rows_unchanged": True,
            "exception_type": type(failure.value).__name__, "code": failure.value.code,
            "snapshot_sha256": hashlib.sha256(repr(before).encode()).hexdigest()})

    async def observe_consume(db, owner, **kwargs):
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == kwargs["task_id"]))
        operation = await db.get(WorkBoardProposal, task.pipeline_operation_id)
        assert operation.status == "accepted" and operation.opportunity_id is None
        assert await stage_accepted_plan_task(db, task) is None
        artifact = await db.get(WorkBoardInputArtifact, kwargs["artifact_id"])
        before = artifact.model_dump(mode="json")
        if not denials:
            # Denials use independent canonical scopes; the actual positive
            # caller's original ReadGuard is never promoted by a failed probe.
            goal_id, operation_id, task_id = task.goal_id, operation.proposal_id, task.task_id
            goal_revision, original_operation = task.goal_revision, operation.proposal_json
            operation_revision, operation_digest = operation.revision, operation.proposal_digest
            await db.rollback()
            await deny(owner, kwargs, "missing_live_revision", {"expected_task_revision": None})
            previous = await mutate(OperatorSession, owner.session_id, {"revoked_at": datetime.now(timezone.utc)})
            try:
                await deny(owner, kwargs, "actual_root_revoked")
            finally:
                await mutate(OperatorSession, owner.session_id, previous)
            previous = await mutate(Goal, goal_id, {"revision": goal_revision + 1})
            try:
                await deny(owner, kwargs, "actual_goal_revision_changed")
            finally:
                await mutate(Goal, goal_id, previous)
            value = json.loads(original_operation)
            value["source_scope"]["allowed_hosts"] = ["unapproved.example"]
            previous = await mutate(WorkBoardProposal, operation_id, {"proposal_json": pipelines.canonical_bytes(value).decode(),
                "proposal_digest": pipelines.digest(value), "revision": operation_revision + 1})
            try:
                await deny(owner, kwargs, "actual_ordinary_source_scope_denied")
            finally:
                await mutate(WorkBoardProposal, operation_id, previous)
            previous = await mutate(WorkBoardTask, task_id, {"pipeline_operation_id": None})
            try:
                await deny(owner, kwargs, "actual_standalone_source_less_denied")
            finally:
                await mutate(WorkBoardTask, task_id, previous)
        active[:] = [db, asyncio.current_task()]
        try:
            await original_consume(db, owner, **kwargs)
        finally:
            active[:] = [None, None]
        actual = await db.get(WorkBoardInputArtifact, kwargs["artifact_id"], populate_existing=True)
        assert actual.state == "consumed" and actual.revision == before["revision"] + 1
        assert actual.metadata_digest == input_artifacts._metadata_digest(actual)
        consumes.append({"task_id": kwargs["task_id"], "artifact_id": actual.artifact_id,
            "capability_id": actual.capability_id, "before": before, "after": actual.model_dump(mode="json"),
            "original_kwargs": dict(kwargs)})
    async def observe(db, owner, **kwargs):
        try:
            return await observe_consume(db, owner, **kwargs)
        except Exception as exc:
            record_first_error("consume_input_artifact", exc)
            raise
    monkeypatch.setattr(input_artifacts, "consume_input_artifact", observe)

    async def ordinary_runtime(*args, **kwargs):
        async def public_http(request):
            assert request.url == "https://example.com/public" and request.method == "GET"
            return PinnedBrowserResponse(200, {"content-type": "text/html"},
                b"<html><body>Stable public line A relevant new public release</body></html>", request.url, "93.184.216.34")
        def named_dns(host, port):
            assert host == "example.com" and port == 443
            return ["93.184.216.34"]
        constructor = PinnedBrowserTransport.__init__
        def external_http(self, **values):
            values.setdefault("resolver", named_dns)
            values.setdefault("injected_fetch", public_http)
            constructor(self, **values)
        monkeypatch.setattr(PinnedBrowserTransport, "__init__", external_http)
        dispatcher = WorkBoardDispatcher(jobs=durable_job_repository, session_provider=factory.accounting_sessions)
        for _ in range(8):
            await dispatcher.run_pass()
            advance_revision = None
            async with get_session() as committed:
                tasks = list((await committed.scalars(select(WorkBoardTask))).all())
                completed = [task for task in tasks if task.capability_id in
                    {"browser.public-task.v1", "work.evidence-dossier.v1"}]
                if not advance_receipts and any(task.capability_id == "browser.public-task.v1"
                        and task.status == WorkBoardStatus.done for task in completed):
                    operation = await committed.get(WorkBoardProposal, state["operation_id"])
                    dossiers = [task for task in tasks if task.capability_id == "work.evidence-dossier.v1"
                        and task.pipeline_operation_id == operation.proposal_id
                        and (task.owner_principal_id, task.owner_session_id) ==
                            (state["owner"].principal_id, state["owner"].session_id)]
                    assert len(dossiers) == 1
                    assert dossiers[0].status == WorkBoardStatus.triage
                    assert dossiers[0].input_artifact_id is None
                    advance_revision = operation.revision
                if len(completed) == 2 and all(task.status == WorkBoardStatus.done for task in completed):
                    assert len(advance_receipts) == 1
                    assert advance_receipts[0]["status_code"] == 200
                    assert advance_receipts[0]["operation_id"] == state["operation_id"]
                    assert advance_receipts[0]["response"]["operation_id"] == state["operation_id"]
                    assert len(consumes) == 2 and len(denials) == 5
                    for receipt in consumes:
                        actual = await committed.get(WorkBoardInputArtifact, receipt["artifact_id"])
                        assert actual.model_dump(mode="json") == receipt["after"]
                        assert actual.metadata_digest == input_artifacts._metadata_digest(actual)
                    state["canonical_commit_readback"] = True
                    for receipt in consumes:
                        before_replay = _raw_rows(root / "seraph.db")
                        active_denial[0] = "original_replay"
                        try:
                            with pytest.raises(BoardError) as replay_failure:
                                async with get_session() as replay:
                                    await original_consume(replay, state["owner"], **receipt["original_kwargs"])
                        finally:
                            active_denial[0] = None
                        assert before_replay == _raw_rows(root / "seraph.db")
                        receipt["replay_rows_unchanged"] = True
                        receipt["replay_exception_type"] = type(replay_failure.value).__name__
                        receipt["replay_code"] = replay_failure.value.code
                    raise _OrdinaryConsumesCommitted()
            if advance_revision is not None:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"origin": "http://localhost:3001"}, cookies=operator_cookies[0]) as client:
                    response = await client.post(f"/api/work-board/pipelines/{state['operation_id']}/advance",
                        json={"expected_revision": advance_revision})
                advance_receipts.append({"operation_id": state["operation_id"],
                    "expected_revision": advance_revision, "status_code": response.status_code,
                    "response": response.json()})
                assert response.status_code == 200, response.text
        raise AssertionError("Ordinary Browser and Dossier did not complete through original owners")
    monkeypatch.setattr(original_vertical, "_actual_plan_journey", ordinary_runtime)
    try:
        with pytest.raises(_OrdinaryConsumesCommitted):
            await original_vertical.test_actual_accepted_report_source_with_mocked_browser_edge(
                accounting_db, real_auth, monkeypatch, record_property)
        assert state.get("canonical_commit_readback") is True
    finally:
        (root / "ordinary-consume-receipts.json").write_text(json.dumps({"denials": denials,
            "consumes": consumes, "canonical_commit_readback": state.get("canonical_commit_readback", False),
            "physical_reads_outside_consume_writer": physical_reads,
            "original_errors": original_errors,
            "advance_receipts": advance_receipts,
            "whole_pipeline_pass": False, "report_admission_excluded": True}, indent=2))
