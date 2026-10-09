"""Actual original authenticated cleanup authority, without execution renewal."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from tests.test_inference_accounting import accounting_db
from tests.test_general_task_planner import forbid_external_inference
from tests.test_document_build_storage import setup, SPEC
from src.work_board import document_build_storage as storage
from src.work_board.repository import BoardError


@pytest.mark.parametrize("goal_change", ["corrected", "deleted"])
async def test_actual_authenticated_cleanup_after_cutoff_and_goal_drift(accounting_db, monkeypatch, goal_change):
    from src.api import documents
    from src.auth.service import authenticate_token
    from src.db.models import Goal
    token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        current_goal = await db.get(Goal, goal.id)
        current_goal.due_date = datetime.now(timezone.utc)+timedelta(seconds=2)
    async with sessions() as db:
        build = await storage.create(db, owner, operator, storage.BuildCreate(goal_id=goal.id,
            goal_revision=1, spec=SPEC, idempotency_key="cleanup-expiry"))
    deadline = datetime.fromisoformat(build["original_deadline"])
    await asyncio.sleep(max(0, (deadline-datetime.now(timezone.utc)).total_seconds())+.05)
    async with sessions() as db:
        current_goal = await db.get(Goal, goal.id)
        if goal_change == "deleted":
            await db.delete(current_goal)
        else:
            current_goal.revision += 1
    monkeypatch.setattr(documents, "get_session", sessions)
    app = FastAPI()
    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.operator = await authenticate_token(token, touch=False)
        return await call_next(request)
    app.include_router(documents.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        retired = await client.request("DELETE", "/api/documents/builds/"+build["build_id"], json={
            "expected_revision": build["revision"], "idempotency_key": "exact-original-retirement"})
        assert retired.status_code == 200, retired.text
        assert retired.json()["state"] == "deleted" and retired.json()["quota_reserved_bytes"] == 0
        assert retired.json()["original_deadline"] == build["original_deadline"]
        assert (await client.get("/api/documents/builds/"+build["build_id"]+"/outputs/editable")).status_code == 409


@pytest.mark.parametrize("denial", ["owner_label", "missing_token", "revoked_root", "replaced_root", "tampered_metadata"])
async def test_cleanup_authority_denies_without_tombstone_or_charge_release(accounting_db, monkeypatch, denial):
    from src.db.models import OperatorSession
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        build = await storage.create(db, owner, operator, storage.BuildCreate(goal_id=goal.id,
            goal_revision=1, spec=SPEC, idempotency_key="cleanup-denial"))
    if denial == "owner_label":
        operator = owner
    elif denial == "missing_token":
        operator = replace(operator, _token_hash=None)
    async with sessions() as db:
        row, value = await storage.owned(db, owner, build["build_id"])
        if denial == "revoked_root":
            (await db.get(OperatorSession, owner.session_id)).revoked_at = datetime.now(timezone.utc)
        elif denial == "replaced_root":
            (await db.get(OperatorSession, owner.session_id)).replaced_by_id = "another-root"
        elif denial == "tampered_metadata":
            row.metadata_digest = "f"*64
    async with sessions() as db:
        with pytest.raises(BoardError):
            await storage.retire(db, owner, operator, build["build_id"], storage.BuildRetire(
                expected_revision=build["revision"], idempotency_key="denied"))
    async with sessions() as db:
        row, value = await storage.owned(db, owner, build["build_id"])
        assert row.document_reserved_bytes == storage.CHARGE and row.revision == build["revision"]
        assert value["phase"] == "staged" and "retirement_key" not in value


async def test_actual_fully_cancelled_zero_launch_retirement_after_cutoff(accounting_db, monkeypatch):
    import json
    from sqlalchemy import select
    from src.api import documents, work_board
    from src.auth.middleware import OperatorAuthMiddleware
    from src.db.models import Goal, WorkBoardTask, WorkBoardAttempt, WorkflowRunState
    from config.settings import settings
    from src.work_board.general_task import GeneralTaskService
    from src.native_tools.task_adapters import ToolRegistry
    from src.work_board.dispatcher import WorkBoardDispatcher
    from tests.test_general_task_planner import prepare
    from tests.test_document_build_native_capacity import admitted_build
    token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    jobs, _owner = await prepare(accounting_db, monkeypatch, existing_owner=owner)
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        (await db.get(Goal, goal.id)).due_date = datetime.now(timezone.utc)+timedelta(seconds=5)
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(jobs=jobs, session_provider=sessions, general_tasks=service)
    binding, identifier = await admitted_build(accounting_db, service, dispatcher, operator, owner, goal, "never-launch-retire")
    monkeypatch.setattr(documents, "get_session", sessions)
    monkeypatch.setattr(work_board, "dispatcher", dispatcher)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    app = FastAPI(); app.add_middleware(OperatorAuthMiddleware)
    app.include_router(documents.router, prefix="/api")
    app.include_router(work_board.router, prefix="/api")
    headers = {"authorization": "Bearer "+token, "origin": "http://localhost:3001"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers, cookies={settings.operator_auth_cookie_name: token}) as client:
        async with sessions() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            row, value = await storage.owned(db, owner, identifier)
            request = {"expected_revision": row.revision, "expected_task_revision": task.task_revision,
                "attempt_id": binding.attempt_id, "idempotency_key": "full-cancel-retire"}
            deadline = datetime.fromisoformat(value["original_deadline"])
        denied = await client.request("DELETE", f"/api/documents/builds/{identifier}", json=request)
        assert denied.status_code == 409, denied.text
        cancelled = await client.post(f"/api/work-board/tasks/{binding.task_id}/actions", json={
            "action": "cancel", "expected_revision": request["expected_task_revision"]})
        assert cancelled.status_code == 200, cancelled.text
        async with sessions() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding.attempt_id))
            parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id))
            child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.invocation_id))
            assert child.attempt_count == 0 and child.status == "cancelled"
            assert parent.status == "cancelled" and attempt.ended_at is not None and attempt.outcome == "cancelled"
            frozen = json.dumps([obj.model_dump(mode="json") for obj in (task, attempt, parent, child)], sort_keys=True)
            request["expected_task_revision"] = task.task_revision
            current_goal = await db.get(Goal, goal.id)
            await db.delete(current_goal)
        await asyncio.sleep(max(0, (deadline-datetime.now(timezone.utc)).total_seconds())+.05)
        retired = await client.request("DELETE", f"/api/documents/builds/{identifier}", json=request)
        assert retired.status_code == 200, retired.text
        assert retired.json()["state"] == "deleted" and retired.json()["quota_reserved_bytes"] == 0
        async with sessions() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding.attempt_id))
            parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id))
            child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.invocation_id))
            assert json.dumps([obj.model_dump(mode="json") for obj in (task, attempt, parent, child)], sort_keys=True) == frozen
            row, value = await storage.owned(db, owner, identifier)
            assert value["retirement_mode"] == "terminal_outputless" and not value.get("output")
        assert (await jobs.inference_accounting_snapshot())["operation_count"] == 0
    service.stop(); registry.stop()


@pytest.mark.parametrize("interruption", ["pending_only", "final_only", "both_links", "tampered_pending", "legacy_orphan", "partial_unlink"])
async def test_actual_source_reserved_publication_interruption_inventory(accounting_db, monkeypatch, interruption):
    import os
    from src.work_board import document_pairs as sources
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    request = storage.BuildCreate(goal_id=goal.id, goal_revision=1, spec=SPEC,
        idempotency_key="physical-interruption-"+interruption)
    if interruption in {"pending_only", "both_links", "tampered_pending", "final_only"}:
        with monkeypatch.context() as fault:
            if interruption in {"pending_only", "tampered_pending"}:
                fault.setattr(os, "link", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("bounded link interruption")))
            elif interruption == "both_links":
                fault.setattr(os, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("bounded unlink interruption")))
            else:
                fault.setattr(sources, "read_private", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("bounded readback interruption")))
            async with sessions() as db:
                with pytest.raises(OSError):
                    await storage.create(db, owner, operator, request)
        async with sessions() as db:
            replay = await storage.create(db, owner, operator, request)
            identifier = replay["build_id"]
            row, value = await storage.owned(db, owner, identifier)
            assert value["phase"] == "reserved" and value["pending"] and not value["sources"]
            receipt = value["pending"]["spec"]
            parent = sources.source_path(row, value, "spec").parent
            if interruption == "tampered_pending":
                path = parent/receipt["file"]
                raw = path.read_bytes(); path.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
    else:
        async with sessions() as db:
            replay = await storage.create(db, owner, operator, request)
            identifier = replay["build_id"]
            row, value = await storage.owned(db, owner, identifier)
            parent = sources.source_path(row, value, "spec").parent
            if interruption == "legacy_orphan":
                (parent/(".g1-spec.fernet."+"f"*32+".pending")).write_bytes(b"unrecognized")
    retire = storage.BuildRetire(expected_revision=replay["revision"], idempotency_key="original-reduction")
    with monkeypatch.context() as fault:
        if interruption == "partial_unlink":
            fault.setattr(os, "fsync", lambda *args: (_ for _ in ()).throw(OSError("bounded directory fsync interruption")))
        async with sessions() as db:
            if interruption in {"pending_only", "final_only"}:
                result = await storage.retire(db, owner, operator, identifier, retire)
                assert result["quota_reserved_bytes"] == 0 and not list(parent.iterdir())
            else:
                with pytest.raises(BoardError):
                    await storage.retire(db, owner, operator, identifier, retire)
    async with sessions() as db:
        row, value = await storage.owned(db, owner, identifier)
        if interruption not in {"pending_only", "final_only"}:
            assert row.document_reserved_bytes == storage.CHARGE and value["phase"] == "cleanup_tombstone"
            if interruption == "partial_unlink":
                assert not list(parent.iterdir())
                with pytest.raises(BoardError):
                    await storage.retire(db, owner, operator, identifier, retire.model_copy(update={"expected_revision":row.revision}))


async def test_actual_private_publication_rejects_copied_source_packet(accounting_db, monkeypatch):
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    original_publish = storage._publish_private_publication
    denied = []
    def checked_publish(row, value, packet):
        with pytest.raises(BoardError):
            original_publish(row, value, replace(packet))
        denied.append(True)
        return original_publish(row, value, packet)
    monkeypatch.setattr(storage, "_publish_private_publication", checked_publish)
    async with sessions() as db:
        build = await storage.create(db, owner, operator, storage.BuildCreate(goal_id=goal.id,
            goal_revision=1, spec=SPEC, idempotency_key="source-packet-copy"))
    assert denied == [True]
    async with sessions() as db:
        retired = await storage.retire(db, owner, operator, build["build_id"], storage.BuildRetire(
            expected_revision=build["revision"], idempotency_key="source-packet-copy-retire"))
        assert retired["quota_reserved_bytes"] == 0
