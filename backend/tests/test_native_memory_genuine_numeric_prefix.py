"""Original sandboxed REPORT Source and observation-only pre-effect denial.

UNRUN candidate. A failed sandbox/terminal owner is a failure, never fake proof.
Numeric totals are partial lower bounds, not full-U or effect permission.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth


def _save_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    assert path.stat().st_mode & 0o777 == 0o600


async def _terminal_report(sessions, operator, monkeypatch, root):
    from playwright.async_api import async_playwright
    from config.settings import settings
    from src.browser import task_runner
    from src.browser.pinned_transport import PinnedBrowserTransport
    from src.security.site_policy import SiteAccessDecision
    from src.goals.repository import GoalRepository
    from src.goals.contracts import GoalAdmissionBudget
    from src.conversation.task_context import TaskContinuityService, ContinueTaskRequest
    from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate, WorkBoardInputArtifactCreate
    from src.work_board.repository import WorkBoardRepository
    from src.work_board.input_artifacts import prepare_input_artifact
    from src.work_board.pipeline_contracts import PipelinePreviewRequest, PipelineAcceptRequest
    from src.work_board import pipelines
    from src.work_board.pipeline_cpu import read_output
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.workflows.job_runtime import DurableJobRepository
    from src.db.models import Session, WorkBoardAttempt, WorkBoardStatus, WorkflowRunState
    from src.memory.m5 import _require_original_m5_session
    from src.runtime_plugins.ownership import begin_native_writer
    from src.work_board.input_artifacts import _commit_artifact_writer
    from tests.test_browser_task_runtime import _input
    from tests.test_browser_task_lifecycle import _html_responses

    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    monkeypatch.setattr(settings, "browser_site_allowlist", "fixture.example")
    monkeypatch.setattr(settings, "browser_site_blocklist", "")
    goal = await GoalRepository().create(title="Original bounded terminal REPORT",
        owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
        admission_budget=GoalAdmissionBudget(reviewed_grant=True, grant_id="review-914",
            max_outstanding_jobs=1, max_attempts=2, max_runtime_seconds=300))
    board = WorkBoardRepository()
    continuity = TaskContinuityService(board)
    await continuity.start()
    try:
        async with sessions() as db:
            await begin_native_writer(db, owner="native_ingress")
            task = (await board.create_task(db, owner, WorkBoardTaskCreate(
                title="Original task conversation", goal_id=goal.id, goal_revision=goal.revision,
                idempotency_key="numeric-prefix-original-conversation"), origin_session_id=owner.session_id)).task
            await _commit_artifact_writer(db)
        async with sessions() as db:
            assert await db.get(Session, owner.session_id) is None
            await db.rollback()
            reply = await continuity.continue_task(db, operator, ContinueTaskRequest(
                task_id=task.task_id, expected_revision=task.task_revision,
                new_conversation_id=owner.session_id))
            assert reply["conversation_id"] == owner.session_id and reply["idempotent_replay"] is False
            await _commit_artifact_writer(db)
            await _require_original_m5_session(db, owner.principal_id, owner.session_id)
    finally:
        await continuity.stop()
    # Original physical input owner and pipeline accept; no terminal SQL seeds.
    async with sessions() as db:
        await begin_native_writer(db, owner="native_ingress")
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(
            schema_version=1, capability_id="browser.public-task.v1", goal_id=goal.id,
            goal_revision=goal.revision, input=_input(), idempotency_key="numeric-prefix-source-input"))
        await begin_native_writer(db, owner="native_ingress")
        producer = (await board.create_task(db, owner, WorkBoardTaskCreate(
            title="Finite public reference", goal_id=goal.id, goal_revision=goal.revision,
            capability_id="browser.public-task.v1", input_artifact_id=artifact.artifact_id,
            status=WorkBoardStatus.todo, priority=87, idempotency_key="numeric-prefix-source-task"))).task
        producer_id, producer_revision = producer.task_id, producer.task_revision
        await _commit_artifact_writer(db)
        await begin_native_writer(db, owner="native_ingress")
        preview = await pipelines.preview(db, owner, producer_id, PipelinePreviewRequest(
            expected_revision=producer_revision, source_input_artifact_id=artifact.artifact_id,
            idempotency_key="numeric-prefix-operation"))
        await _commit_artifact_writer(db)
        operation = await pipelines.accept(db, owner, preview["operation_id"], PipelineAcceptRequest(
            expected_revision=preview["revision"], expected_parent_revision=preview["parent_revision"],
            expected_digest=preview["digest"]))
        await _commit_artifact_writer(db)
    responses = _html_responses()
    fetches = []
    async def fetch(request):
        fetches.append(1)
        return responses[request.url]
    async def policy(url, **kwargs):
        return SiteAccessDecision(allowed=url in responses, hostname="fixture.example")
    async def resolver(*args):
        return ["93.184.216.34"]
    original_runner = task_runner.BrowserTaskRunner
    jobs = DurableJobRepository()
    dispatcher = WorkBoardDispatcher(repository=board, jobs=jobs, session_provider=sessions)
    # Observe the original callback without substituting its authority or
    # repeating its queries. It suppresses dependency errors as False, so keep
    # only bounded scalar diagnostics from calls it actually makes.
    original_current = dispatcher._browser_assert_current
    async def observe_current(**binding):
        from contextlib import asynccontextmanager
        from sqlalchemy import event
        from src.work_board import dispatcher as dispatcher_module
        from src.work_board import input_artifacts as artifact_module
        observations = {}
        def error(exc):
            code = getattr(exc, "code", None)
            return {"class": type(exc).__name__, "code": code if type(code) is str
                and len(code) <= 128 and all(c in "abcdefghijklmnopqrstuvwxyz_" for c in code) else None}
        def observe_call(label, original):
            async def forwarded(*args, **kwargs):
                try:
                    result = await original(*args, **kwargs)
                except BaseException as exc:
                    observations[label] = error(exc)
                    raise
                observations[label] = {"returned": True}
                if label == "task":
                    observations[label].update(status=result.status.value,
                        revision=result.task_revision,
                        expected_revision=binding["board_task_revision"],
                        owner_matches=result.owner_principal_id == binding["owner_principal_id"],
                        session_matches=result.owner_session_id == binding["owner_session_id"],
                        artifact_matches=result.input_artifact_id == binding["input_artifact_id"])
                return result
            return forwarded
        def loaded_attempt(attempt, context):
            if attempt.attempt_id == binding["attempt_id"]:
                observations["attempt"] = {"loaded": True,
                    "task_matches": attempt.task_id == binding["task_id"],
                    "fence": attempt.fencing_token, "expected_fence": binding["board_fencing_token"],
                    "lease_owner_matches": attempt.lease_owner == dispatcher.runner_id,
                    "ended": attempt.ended_at is not None,
                    "cancel_requested": attempt.cancel_requested_at is not None,
                    "lease_present": attempt.lease_expires_at is not None}
        original_sessions = dispatcher.session_provider
        @asynccontextmanager
        async def observed_sessions():
            try:
                async with original_sessions() as db:
                    yield db
            except BaseException as exc:
                observations["session_exit"] = error(exc)
                raise
            observations["session_exit"] = {"returned": True}
        event.listen(WorkBoardAttempt, "load", loaded_attempt)
        try:
            with monkeypatch.context() as callback_patch:
                callback_patch.setattr(dispatcher, "session_provider", observed_sessions)
                callback_patch.setattr(board, "get_task", observe_call("task", board.get_task))
                callback_patch.setattr(dispatcher_module, "authenticate_session",
                    observe_call("auth", dispatcher_module.authenticate_session))
                callback_patch.setattr(pipelines, "task_guard", observe_call("pipeline", pipelines.task_guard))
                callback_patch.setattr(artifact_module, "resolve_input_artifact_for_task",
                    observe_call("artifact", artifact_module.resolve_input_artifact_for_task))
                result = await original_current(**binding)
                observations["original_returned_true"] = result is True
                return result
        finally:
            event.remove(WorkBoardAttempt, "load", loaded_attempt)
            if observations.get("original_returned_true") is not True:
                _save_private(root / "original-browser-admission-diagnostic.private.json", observations)
    monkeypatch.setattr(dispatcher, "_browser_assert_current", observe_current)
    async with async_playwright() as playwright:
        async def launch():
            # Supported original Playwright API: actual sandbox, no fallback.
            return await playwright.chromium.launch(headless=True, chromium_sandbox=True)
        with monkeypatch.context() as browser_patch:
            browser_patch.setattr(task_runner, "BrowserTaskRunner", lambda **kwargs: original_runner(
                **kwargs, browser_launcher=launch, transport_factory=lambda: PinnedBrowserTransport(
                    resolver=resolver, injected_fetch=fetch, site_policy=policy)))
            for index in range(3):
                task_id = operation["steps"][index]["task_id"]
                async with sessions() as db:
                    task = await board.get_task(db, owner, task_id)
                    await board.promote_task_ready(db, task_id, expected_revision=task.task_revision,
                        actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
                async with sessions() as db:
                    task = await board.get_task(db, owner, task_id)
                    claim = await board.claim_ready_task(db, task_id, expected_revision=task.task_revision,
                        lease_owner=dispatcher.runner_id, lease_seconds=180 if index == 0 else 30,
                        actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
                assert claim is not None
                outcome = await dispatcher._admit_execute_project(claim)
                assert outcome["completed"] is True
                async with sessions() as db:
                    task = await board.get_task(db, owner, task_id)
                    assert task.status == WorkBoardStatus.done
                    output = await pipelines.verified_output(db, owner, task)
                    assert read_output(output["file_path"], output["content_sha256"])
                    if index < 2:
                        operation = await pipelines.read(db, owner, operation["operation_id"])
                        operation = await pipelines.advance(db, owner, operation["operation_id"], operation["revision"])
                        await _commit_artifact_writer(db)
    assert fetches
    async with sessions() as db:
        attempt = (await db.scalars(select(WorkBoardAttempt).where(
            WorkBoardAttempt.task_id == task.task_id))).one()
        run = (await db.scalars(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == attempt.workflow_run_id))).one()
        assert attempt.outcome == "verified" and attempt.ended_at is not None
        assert attempt.lease_owner is None and attempt.lease_expires_at is None
        assert run.status == "succeeded" and run.finished_at is not None
        assert task.capability_id == "work.local-evidence-report.v1"
        output = await pipelines.verified_output(db, owner, task)
        assert read_output(output["file_path"], output["content_sha256"])
    return task, attempt, jobs


@pytest.mark.asyncio
async def test_genuine_terminal_report_observes_original_pre_effect_numeric_stop(
        accounting_db, real_auth, monkeypatch, tmp_path):
    import httpx
    from fastapi import FastAPI
    from src.db import engine as original
    from src.db.engine import get_session, override_session_factory
    from src.auth.service import create_session
    from src.auth.middleware import OperatorAuthMiddleware
    from src.db.models import Memory, MemoryProposal
    from src.memory import header_bounds, m5
    from src.memory.header_bounds import MAX_BYTES, MAX_ROWS, HeaderBoundsError
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    from src.runtime_plugins.composition import reviewed_composition
    from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
    from src.workspace.production import ProductionWorkspace, maintenance_fence
    from src.api.memory import router
    from config.settings import settings

    root, engine, factory = accounting_db
    monkeypatch.setattr(original, "engine", engine)
    monkeypatch.setattr(original, "_db_path", str(root / "seraph.db"))
    monkeypatch.setattr(original, "async_session_factory", factory)
    for target in ("src.db.engine.get_session", "src.auth.service.get_session", "src.goals.repository.get_session",
            "src.agent.session.get_session", "src.work_board.dispatcher.get_session", "src.workflows.job_runtime.get_session",
            "src.workflows.durable_state.get_session", "src.memory.repository.get_session", "src.audit.repository.get_session",
            "src.vault.repository.get_session", "src.approval.repository.get_session"):
        monkeypatch.setattr(target, get_session)
    receipt = {"schema_version": 1, "acceptance": False, "full_U": None,
        "terminal_report_verified": False, "factory_calls": 0, "tuple_available": False,
        "numeric_totals": None, "effect_calls": 0}
    host = None
    try:
        with override_session_factory(factory):
            await original.init_db()
            token, operator = await create_session()
            task, attempt, jobs = await _terminal_report(get_session, operator, monkeypatch, root)
            receipt["terminal_report_verified"] = True
            monkeypatch.setattr("src.workflows.job_runtime.durable_job_repository", jobs)
            node = Path(os.environ["SERAPH_CORDIS_TEST_NODE"])
            reviewed = reviewed_composition(node_path=node)
            workspace = ProductionWorkspace(host_root=root)
            with maintenance_fence(workspace):
                async with get_session() as db:
                    await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                    await initialize_fresh_deployment(db, composition_digests={
                        domain: reviewed.composition_digest for domain in DOMAINS})
            host = CordisHost(node_path=node, service_dispatch=NativeServiceDispatcher(jobs=jobs))
            monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
            assert await host.start()
            original_factory = header_bounds._reserve_native_memory_effect_ledger
            original_apply = m5._apply_native_memory_mutation_plan
            original_debit = header_bounds.HeaderReadBudget.debit
            async def count_memory():
                async with get_session() as db:
                    return (await db.scalar(select(func.count()).select_from(Memory)),
                        await db.scalar(select(func.count()).select_from(MemoryProposal)))
            before = await count_memory()
            async def apply(*args, **kwargs):
                receipt["effect_calls"] += 1
                return await original_apply(*args, **kwargs)
            async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
                receipt["factory_calls"] += 1
                assert receipt["factory_calls"] == 1 and MAX_BYTES == 1_048_576 and MAX_ROWS == 128
                guard = db.info["composition_guard"]
                budget = guard.header_budget
                assert budget is operation.source.admission.header_budget
                def debit(frame, amount, *, appearance=None):
                    try:
                        return original_debit(frame, amount, appearance=appearance)
                    except HeaderBoundsError:
                        if frame is budget:
                            caller = sys._getframe(1)
                            receipt["failed_original_debit"] = {"amount": amount,
                                "remaining": frame.remaining, "site_function": caller.f_code.co_name,
                                "site_line": caller.f_lineno}
                        raise
                try:
                    with monkeypatch.context() as observing:
                        observing.setattr(header_bounds.HeaderReadBudget, "debit", debit)
                        ledger = await original_factory(db, run, operation, prepared,
                            existing_references=existing_references, before_row=before_row, certificate=certificate)
                except HeaderBoundsError as error:
                    receipt["factory_error_class"] = type(error).__name__
                    receipt["factory_error_sha256"] = hashlib.sha256(str(error).encode()).hexdigest()
                    known_stops = {"memory_source_authority_numeric_contribution_unavailable",
                        "memory_complete_numeric_call_inventory_unavailable", "canonical_bound_not_certified",
                        "header_reference_bound"}
                    receipt["factory_stop_code"] = str(error) if str(error) in known_stops else None
                    receipt["source_spent_at_stop"] = MAX_BYTES - budget.remaining
                    totals = getattr(guard, "_native_memory_available_numeric_totals", None)
                    if totals is not None:
                        assert type(totals) is tuple and len(totals) == 8
                        assert totals[0] is operation and totals[1] is prepared and totals[2] is certificate
                        assert all(type(value) is int and value >= 0 for value in totals[3:])
                        receipt["tuple_available"] = True
                        receipt["original_identity_matches"] = True
                        receipt["numeric_totals"] = dict(zip(("source_spent", "effect_available",
                            "capture_available", "completion_available", "phase_count"), totals[3:]))
                    assert operation._effect is None and prepared.consumed is False
                    receipt["original_plan_unconsumed"] = True
                    assert receipt["factory_stop_code"] is not None
                    assert totals is not None or "failed_original_debit" in receipt
                    raise
                else:
                    ledger.close()
                    pytest.fail("original pre-effect STOP unexpectedly issued a ledger")
            monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
            monkeypatch.setattr(m5, "_apply_native_memory_mutation_plan", apply)
            app = FastAPI()
            app.add_middleware(OperatorAuthMiddleware)
            app.include_router(router, prefix="/api")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                    cookies={settings.operator_auth_cookie_name: token}, headers={"Origin": "http://localhost:3001"}) as client:
                response = await client.post("/api/memory/task-proposals/native", json={
                    "task_id": task.task_id, "expected_task_revision": task.task_revision,
                    "attempt_id": attempt.attempt_id, "idempotency_key": "genuine-report-numeric-prefix"})
                receipt["native_http_status"] = response.status_code
                assert response.status_code == 409
            assert receipt["factory_calls"] == 1 and receipt["effect_calls"] == 0
            assert await count_memory() == before
            receipt["memory_counts_unchanged"] = True
    except BaseException as error:
        receipt["test_error_class"] = type(error).__name__
        receipt["test_error_sha256"] = hashlib.sha256(str(error).encode()).hexdigest()
        raise
    finally:
        if host is not None:
            await host.stop(preserve_blocked=host.state == "blocked")
            if host._cleanup_task is not None:
                await host._cleanup_task
            receipt["native_host_reaped"] = host.snapshot()["cleanup"]["process_reaped"]
        _save_private(tmp_path / "genuine-report-numeric-prefix.json", receipt)
