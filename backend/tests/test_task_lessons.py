"""Ordinary task lesson mechanics; no model calls or improvement claims."""
from datetime import datetime, timezone
import json
import socket
import hashlib
import asyncio
import threading
import subprocess
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from config.settings import settings
from src.auth.service import create_session
from src.db.models import Goal, Memory, MemoryProposal, Session, WorkBoardAttempt, WorkBoardTask, WorkflowRunState, OperatorSession, WorkflowStepState
from src.memory.task_lessons import LessonRequest, LessonScope, TaskMethod, ResearchStrategy, create_task_lesson, inspect_task_lesson, _task
from src.work_board.repository import BoardError
from src.workflows.durable_state import workflow_state_repository
from src.memory.task_lessons import eligible_lesson_source, LessonAutoPolicyRequest, set_automatic_lesson_policy, propose_automatic_task_lesson, maybe_propose_automatic_lesson

pytestmark = pytest.mark.parametrize("async_db", ["file"], indirect=True)


@pytest.fixture(autouse=True)
def no_inference(monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("external inference is forbidden in task lesson tests")
    # Owned provider transports plus sockets are denied; SQLite uses no sockets.
    monkeypatch.setattr("litellm.completion", denied)
    monkeypatch.setattr("litellm.acompletion", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    yield calls
    assert calls == []


async def failed_local_task(async_db, monkeypatch, tmp_path):
    """Actually attempt a missing local file read and record its durable failure."""
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-lesson-test")
    monkeypatch.setattr("src.memory.m5.get_session", async_db)
    _, operator = await create_session()
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(Session(id=operator.session_id))
        db.add(Goal(id="goal", title="Read a local document", revision=1, status="active",
            owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id))
        db.add(WorkBoardTask(task_id="task", owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id, goal_id="goal", goal_revision=1, task_revision=1,
            status="blocked", title="Read local document", capability_id="", idempotency_key="local-document-task"))
        await db.flush()
        db.add(WorkBoardAttempt(attempt_id="attempt", task_id="task", workflow_run_id="run",
            fencing_token=1, started_at=now, ended_at=now, outcome="capability"))
        db.add(WorkflowRunState(run_identity="run", root_run_identity="run", workflow_name="local-read",
            operator_session_id=operator.session_id, session_id=operator.session_id,
            owner_kind="user", owner_principal_id=operator.principal.principal_id,
            goal_id="goal", goal_revision=1, status="running", record_schema_version=1))
        await db.commit()
    async with async_db() as db:
        assert await _task(db, "task") is not None
    await workflow_state_repository.record_step_started(run_identity="run", workflow_name="local-read",
        step_id="read", step_index=1, tool_name="read_file", arguments={"file_path": "missing.txt"})
    from src.tools.filesystem_tool import read_file
    result = read_file.forward(file_path="missing.txt")
    assert "Error" in result or "not found" in result.lower()
    await workflow_state_repository.record_step_failed(run_identity="run", step_id="read",
        error_kind="FileNotFoundError", error_summary="Selected local source is absent")
    await workflow_state_repository.finish_run(run_identity="run", status="failed", error="Selected local source is absent")
    async with async_db() as db:
        saved = await _task(db, "task")
        assert saved is not None
        assert (saved.owner_principal_id, saved.owner_session_id) == (operator.principal.principal_id, operator.session_id)
    from src.db import engine as db_engine
    async with db_engine.get_session() as db:
        saved = await _task(db, "task")
        assert saved is not None
    request = LessonRequest(task_id="task", attempt_id="attempt", correction="Check source existence before drafting.",
        source_refs=["run", "attempt"], scope=LessonScope(goal_id="goal", goal_revision=1, family="knowledge"), expected_revision=1)
    return operator, request


@pytest.mark.asyncio
async def test_redacted_lesson_inspection_never_opens_private_artifact_or_mirror(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    result = await create_task_lesson(operator, request)
    # Authorized live legacy inspection retains its original full wire.
    assert (await inspect_task_lesson(operator, result["proposal_id"]))["new_method"]["schema_version"] == "TaskMethod.v1"
    from src.db.models import MemoryProposalPrivacyState
    from src.memory import task_lessons
    real_mirror = task_lessons._repair_lesson_mirror
    async def redact_after_staging(row):
        mirror = await real_mirror(row)
        async with async_db() as db:
            current = await db.get(MemoryProposal, result["proposal_id"])
            current.privacy_state = MemoryProposalPrivacyState.redacted
        return mirror
    # Withdrawal after private staging must be observed by the final fresh
    # canonical read rather than the originally visible detached proposal.
    with monkeypatch.context() as staged_patch:
        staged_patch.setattr(task_lessons, "_repair_lesson_mirror", redact_after_staging)
        with pytest.raises(BoardError) as staged_denial:
            await inspect_task_lesson(operator, result["proposal_id"])
        assert staged_denial.value.code == "lesson_private_content_unavailable"
    def forbidden_private_read(*args, **kwargs):
        raise AssertionError("redacted lesson read a private artifact")
    async def forbidden_mirror(*args, **kwargs):
        raise AssertionError("redacted lesson reconstructed its mirror")
    monkeypatch.setattr(task_lessons, "read_private_proof", forbidden_private_read)
    monkeypatch.setattr(task_lessons, "_repair_lesson_mirror", forbidden_mirror)
    with pytest.raises(BoardError) as denied:
        await inspect_task_lesson(operator, result["proposal_id"])
    assert denied.value.code == "lesson_private_content_unavailable" and denied.value.status_code == 410


@pytest.mark.asyncio
async def test_failed_task_correction_is_private_inspectable_idempotent_and_inert(async_db, monkeypatch, tmp_path, no_inference):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    result = await create_task_lesson(operator, request)
    assert result["status"] == "proposed"
    assert result["behavior_changed"] is False
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["observed"]["status"] == "failed"
    assert inspected["new_method"]["steps"][0] == {"kind": "guard", "check": "source_exists"}
    assert inspected["old_method"]["registered_tool_ids"] == ["read_file"]
    assert inspected["positive_preference_vote"] is False
    assert inspected["reflection"] == {"mode": "local_projection", "provider_contacts": 0, "spend_microusd": 0}
    assert inspected["source_current"] is True
    replay = await create_task_lesson(operator, request)
    assert replay["idempotent_replay"] is True
    assert replay["proposal_id"] == result["proposal_id"]
    async with async_db() as db:
        assert not list((await db.execute(select(Memory))).scalars())
        rows = list((await db.execute(select(MemoryProposal))).scalars())
        assert len(rows) == 1
        assert rows[0].preview_text is None
        path = tmp_path / "workspace" / rows[0].artifact_ref
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_committed_proposal_receipt_is_repaired_once_by_exact_replay(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    from src.evolution.runtime import EvolutionRuntime
    actual = EvolutionRuntime.record_task_lesson
    def unavailable(*args, **kwargs):
        raise OSError("declared postcommit receipt write failure")
    monkeypatch.setattr(EvolutionRuntime, "record_task_lesson", unavailable)
    automatic = await create_task_lesson(operator, request, _automatic=True)
    # Completion uses only canonical receipts. Explicit inspection owns mirror
    # repair, so its failure cannot hold the terminal dispatcher hot path.
    degraded = await inspect_task_lesson(operator, automatic["proposal_id"])
    assert degraded["mirror"]["status"] == "degraded"
    assert degraded["new_method"] and degraded["source_current"] is True
    async with async_db() as db:
        committed = (await db.execute(select(MemoryProposal))).scalar_one()
        identity = (committed.proposal_id, committed.revision, committed.source_context_digest, committed.artifact_digest)
    monkeypatch.setattr(EvolutionRuntime, "record_task_lesson", actual)
    repaired = await create_task_lesson(operator, request, _automatic=True)
    repeated = await create_task_lesson(operator, request, _automatic=True)
    await inspect_task_lesson(operator, automatic["proposal_id"])
    assert repaired["idempotent_replay"] is repeated["idempotent_replay"] is True
    assert repaired["proposal_id"] == repeated["proposal_id"] == identity[0]
    receipt_path = EvolutionRuntime.default_path(settings.workspace_dir)
    receipts = json.loads(receipt_path.read_text())["task_lesson_receipts"]
    assert len(receipts) == 1
    assert receipts[identity[0]]["proposal_revision"] == identity[1]
    assert receipts[identity[0]]["source_digest"] == identity[2]
    assert receipts[identity[0]]["candidate_digest"] == identity[3]
    async with async_db() as db:
        assert len(list((await db.execute(select(MemoryProposal))).scalars())) == 1
        assert not list((await db.execute(select(Memory))).scalars())
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    second = await create_task_lesson(operator, request, _automatic=True)
    assert second["proposal_id"] != identity[0]
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    denied = await create_task_lesson(operator, request, _automatic=True)
    assert denied["reason_code"] == "automatic_lesson_daily_cap"


@pytest.mark.asyncio
@pytest.mark.parametrize("full_state", ["bytes", "entries"])
async def test_oversized_mirror_never_hides_canonical_api_candidate(async_db, monkeypatch, tmp_path, full_state):
    tokens = []
    actual_create = create_session
    async def capture_login(*args, **kwargs):
        issued = await actual_create(*args, **kwargs)
        tokens.append(issued[0])
        return issued
    monkeypatch.setattr(sys.modules[__name__], "create_session", capture_login)
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.evolution.runtime import EvolutionRuntime
    state = EvolutionRuntime.default_path(settings.workspace_dir)
    state.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"schema_version": 1, "proposals": {}, "task_lesson_receipts":
        {str(i): {} for i in range(4097)} if full_state == "entries" else {},
        "retained_history": "x" * (1024 * 1024) if full_state == "bytes" else ""}).encode()
    state.write_bytes(raw)
    from fastapi import FastAPI
    import httpx
    from src.api import memory
    from src.auth.middleware import OperatorAuthMiddleware
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(memory.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
        cookies={settings.operator_auth_cookie_name: tokens[0]}, headers={"Origin": "http://localhost:3001"}) as client:
        proposed = await client.post("/api/memory/task-lessons", json=request.model_dump())
        assert proposed.status_code == 201, proposed.text
        candidate = proposed.json()
        assert candidate["result"] == "candidate_inert" and candidate["mirror"]["status"] == "degraded"
        inspected = await client.get("/api/memory/task-lessons/" + candidate["proposal_id"])
        assert inspected.status_code == 200, inspected.text
        detail = inspected.json()
        assert detail["source_current"] and detail["new_method"] and detail["old_method"]
        assert detail["mirror"]["status"] == "degraded" and detail["mirror"]["recovery_action"]
        replay = await client.post("/api/memory/task-lessons", json=request.model_dump())
        assert replay.status_code == 201 and replay.json()["proposal_id"] == candidate["proposal_id"]
        assert replay.json()["idempotent_replay"] is True and replay.json()["mirror"]["status"] == "degraded"
    assert state.read_bytes() == raw
    async with async_db() as db:
        assert len(list((await db.execute(select(MemoryProposal))).scalars())) == 1
        assert not list((await db.execute(select(Memory))).scalars())


@pytest.mark.asyncio
async def test_policy_revision_cas_rejects_delayed_enable_after_confirmed_disable(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    stale_enable = LessonAutoPolicyRequest(enabled=True, expected_revision=1,
        expected_policy_revision=None, mutation_uuid=str(uuid4()))
    disabled = await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=False,
        expected_revision=1, expected_policy_revision=None, mutation_uuid=str(uuid4())))
    with pytest.raises(BoardError) as rejected:
        await set_automatic_lesson_policy(operator, "task", stale_enable)
    assert rejected.value.code == "lesson_policy_changed"
    assert (await eligible_lesson_source(operator, "task"))["automatic_policy"] == disabled
    fresh_enable = stale_enable.model_copy(update={"mutation_uuid": str(uuid4()),
        "expected_policy_revision": disabled["policy_revision"]})
    enabled = await set_automatic_lesson_policy(operator, "task", fresh_enable)
    confirmed_disable = await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=False,
        expected_revision=1, expected_policy_revision=enabled["policy_revision"], mutation_uuid=str(uuid4())))
    assert await set_automatic_lesson_policy(operator, "task", fresh_enable) == confirmed_disable
    with pytest.raises(ValidationError):
        LessonAutoPolicyRequest(enabled=True, expected_revision=1, mutation_uuid=str(uuid4()))
    # Two concurrent changes observed at one revision cannot both mutate it.
    outcomes = await asyncio.gather(*(set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(
        enabled=enabled_value, expected_revision=1, expected_policy_revision=confirmed_disable["policy_revision"],
        mutation_uuid=str(uuid4()))) for enabled_value in (False, True)), return_exceptions=True)
    assert sum(isinstance(item, BoardError) and item.code == "lesson_policy_changed" for item in outcomes) == 1
    assert sum(isinstance(item, dict) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_terminal_automatic_failure_and_replay_are_visible_without_private_content(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    async with async_db() as db:
        task = await _task(db, "task")
    from src.memory import task_lessons
    actual = task_lessons.propose_automatic_task_lesson
    async def unavailable(*args, **kwargs):
        raise OSError("private diagnostic must not enter public event")
    monkeypatch.setattr(task_lessons, "propose_automatic_task_lesson", unavailable)
    with pytest.raises(OSError):
        await maybe_propose_automatic_lesson(task, "attempt")
    blocked = await eligible_lesson_source(operator, "task")
    assert blocked["automatic_outcome"]["reason_code"] == "automatic_lesson_unavailable"
    assert blocked["automatic_outcome"]["error_type"] == "OSError"
    monkeypatch.setattr(task_lessons, "propose_automatic_task_lesson", actual)
    result = await maybe_propose_automatic_lesson(task, "attempt")
    replay = await maybe_propose_automatic_lesson(task, "attempt")
    assert result["proposal_id"] == replay["proposal_id"]
    visible = await eligible_lesson_source(operator, "task")
    assert visible["automatic_outcome"]["proposal_id"] == result["proposal_id"]
    assert visible["automatic_outcome"]["result"] == "candidate_inert"
    from src.db.models import WorkBoardEvent
    async with async_db() as db:
        events = list((await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == "task_lesson.automatic_outcome.v1"))).scalars())
        assert len(events) == 2
        assert all(event.owner_principal_id == operator.principal.principal_id for event in events)
        assert {event.actor_principal_id for event in events} == {operator.principal.principal_id, "service:task-lesson-status"}
        assert "private diagnostic" not in json.dumps([event.metadata_json for event in events])
        assert "Check source existence" not in json.dumps([event.metadata_json for event in events])
        assert len(list((await db.execute(select(MemoryProposal))).scalars())) == 1


@pytest.mark.asyncio
async def test_unresolved_effect_sibling_states_deny_failed_task_learning(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    for state in ("unknown", "contact_started", "pending", "intent", "dispatched"):
        async with async_db() as db:
            run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == "run"))).scalar_one()
            run.effect_receipts_json = json.dumps([{"status": state}])
        with pytest.raises(BoardError) as denied:
            await create_task_lesson(operator, request)
        assert denied.value.code == "lesson_outcome_unresolved"
        assert (await eligible_lesson_source(operator, "task"))["eligible"] is False
    async with async_db() as db:
        assert not list((await db.execute(select(MemoryProposal))).scalars())


@pytest.mark.asyncio
async def test_exact_timeout_retains_single_io_until_positive_finish_and_never_late_commits(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    async with async_db() as db:
        task = await _task(db, "task")
    from src.memory import task_lessons
    actual_write = task_lessons._write_lesson
    entered, release = threading.Event(), threading.Event()
    writes = []
    def blocked_write(*args):
        writes.append(args[0])
        entered.set()
        assert release.wait(10), "test must release the actual retained worker"
        return actual_write(*args)
    monkeypatch.setattr(task_lessons, "_write_lesson", blocked_write)
    callback = asyncio.create_task(asyncio.wait_for(maybe_propose_automatic_lesson(task, "attempt"), timeout=5))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # A blocked physical operation does not block an unrelated event-loop
        # tick or task-source inspection. No thread cancellation is claimed.
        await asyncio.wait_for(asyncio.sleep(.01), timeout=.1)
        with pytest.raises(TimeoutError):
            await callback
        visible = await eligible_lesson_source(operator, "task")
        assert visible["automatic_outcome"]["reason_code"] == "automatic_lesson_timeout_or_cancelled"
        assert visible["automatic_outcome"]["attempt_id"] == "attempt"
        assert len(task_lessons._AUTOMATIC_IO) == 1
        async with async_db() as db:
            assert not list((await db.execute(select(MemoryProposal))).scalars())
        duplicate = await maybe_propose_automatic_lesson(task, "attempt")
        assert duplicate["reason_code"] == "automatic_lesson_io_pending"
        assert len(writes) == 1
    finally:
        release.set()
        for _ in range(100):
            if not task_lessons._AUTOMATIC_IO:
                break
            await asyncio.sleep(.01)
    assert not task_lessons._AUTOMATIC_IO
    recovered = await maybe_propose_automatic_lesson(task, "attempt")
    assert recovered["reason_code"] == "automatic_lesson_cancelled_staged_artifact"
    async with async_db() as db:
        assert not list((await db.execute(select(MemoryProposal))).scalars())
        assert not list((await db.execute(select(Memory))).scalars())


@pytest.mark.asyncio
async def test_timeout_status_survives_revocation_and_binds_original_attempt(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    async with async_db() as db:
        task = await _task(db, "task")
    entered = asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr("src.memory.task_lessons.propose_automatic_task_lesson", blocked)
    callback = asyncio.create_task(asyncio.wait_for(maybe_propose_automatic_lesson(task, "attempt"), timeout=.2))
    await entered.wait()
    async with async_db() as db:
        original_root = await db.get(OperatorSession, operator.session_id)
        original_root.revoked_at = datetime.now(timezone.utc)
        current_task = await _task(db, "task")
        current_task.task_revision = 2
        db.add(WorkBoardAttempt(attempt_id="new-attempt", task_id="task", fencing_token=2,
            started_at=datetime.now(timezone.utc)))
    with pytest.raises(TimeoutError):
        await callback
    from src.db.models import WorkBoardEvent
    async with async_db() as db:
        receipt = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == "task_lesson.automatic_outcome.v1"))).scalar_one()
        payload = json.loads(receipt.metadata_json)
        assert payload["attempt_id"] == "attempt" and payload["task_revision"] == 1
        assert receipt.actor_principal_id == "service:task-lesson-status" and receipt.actor_session_id is None
        assert not list((await db.execute(select(MemoryProposal))).scalars())


@pytest.mark.asyncio
async def test_macos_profile_without_proc_supports_actual_same_process_proposal(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.memory import task_lessons
    from pathlib import Path
    actual_read = Path.read_text
    def no_proc(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            raise AssertionError("macOS lesson profile must not read Linux procfs")
        return actual_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", no_proc)
    monkeypatch.setattr(task_lessons, "_host_platform", lambda: "darwin")
    monkeypatch.setattr(task_lessons, "_darwin_boot_id", lambda: (_ for _ in ()).throw(OSError("native witness unavailable")))
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=None, mutation_uuid=str(uuid4())))
    async with async_db() as db:
        task = await _task(db, "task")
    result = await maybe_propose_automatic_lesson(task, "attempt")
    assert result["result"] == "candidate_inert"
    detail = await inspect_task_lesson(operator, result["proposal_id"])
    assert detail["source_current"] and detail["new_method"] and not detail["positive_preference_vote"]
    from src.db.models import WorkBoardEvent
    async with async_db() as db:
        start = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == task_lessons._IO_STARTED))).scalar_one()
        witness = json.loads(start.metadata_json)["process"]
        assert witness["kind"] == "unknown" and witness["platform"] == "darwin"
        assert task_lessons._process_ended(witness) is False
    assert not task_lessons._AUTOMATIC_IO


def test_darwin_native_witness_driver_fixture_has_exact_finite_public_abi(async_db, monkeypatch):
    """Driver contract fixture on Linux; no native macOS execution claimed."""
    import ctypes
    from types import SimpleNamespace
    from src.memory import task_lessons
    assert ctypes.sizeof(task_lessons._DarwinBsdInfo) == 136
    assert task_lessons._DarwinBsdInfo.pid.offset == 12
    assert task_lessons._DarwinBsdInfo.start_sec.offset == 120
    assert task_lessons._DarwinBsdInfo.start_usec.offset == 128
    calls = []
    def boot(name, buffer, size, new, new_size):
        assert name == b"kern.bootsessionuuid" and new is None and new_size == 0
        buffer.value = b"12345678-1234-1234-1234-123456789abc"
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = 37
        return 0
    def process(pid, flavor, argument, buffer, size):
        assert (pid, flavor, argument, size) == (42, 3, 0, 136)
        info = ctypes.cast(buffer, ctypes.POINTER(task_lessons._DarwinBsdInfo)).contents
        info.pid, info.start_sec, info.start_usec = pid, 100, 999999
        return 136
    def library(path, **kwargs):
        calls.append(path)
        assert kwargs == {"use_errno": True}
        if path == "/usr/lib/libSystem.B.dylib":
            return SimpleNamespace(sysctlbyname=boot)
        assert path == "/usr/lib/libproc.dylib"
        return SimpleNamespace(proc_pidinfo=process)
    monkeypatch.setattr(ctypes, "CDLL", library)
    assert task_lessons._darwin_boot_id() == "12345678-1234-1234-1234-123456789abc"
    assert task_lessons._darwin_start(42) == (100, 999999)
    assert calls == ["/usr/lib/libSystem.B.dylib", "/usr/lib/libproc.dylib"]
    for returned, reported_pid, seconds, microseconds in [(135, 42, 100, 1), (136, 43, 100, 1), (136, 42, 0, 1), (136, 42, 100, 1000000)]:
        def invalid(pid, flavor, argument, buffer, size):
            info = ctypes.cast(buffer, ctypes.POINTER(task_lessons._DarwinBsdInfo)).contents
            info.pid, info.start_sec, info.start_usec = reported_pid, seconds, microseconds
            return returned
        monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(proc_pidinfo=invalid))
        with pytest.raises(OSError, match="unknown"):
            task_lessons._darwin_start(42)
    def missing(*args):
        return 0
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(proc_pidinfo=missing))
    with pytest.raises(OSError, match="unknown"):
        task_lessons._darwin_start(42)


def test_darwin_restart_requires_positive_boot_or_exact_pid_replacement(async_db, monkeypatch):
    from src.memory import task_lessons
    boot = "12345678-1234-1234-1234-123456789abc"
    monkeypatch.setattr(task_lessons, "_host_platform", lambda: "darwin")
    monkeypatch.setattr(task_lessons, "_darwin_boot_id", lambda: boot)
    monkeypatch.setattr(task_lessons, "_darwin_start", lambda pid: (100, 1))
    witness = task_lessons._process_identity(42)
    assert witness["kind"] == "darwin" and witness["pid"] == 42
    assert task_lessons._process_ended(witness) is False
    monkeypatch.setattr(task_lessons, "_darwin_start", lambda pid: (101, 1))
    assert task_lessons._process_ended(witness) is True
    monkeypatch.setattr(task_lessons, "_darwin_start", lambda pid: (_ for _ in ()).throw(ProcessLookupError("missing PID")))
    assert task_lessons._process_ended(witness) is False
    assert task_lessons._process_identity(42)["kind"] == "unknown"
    monkeypatch.setattr(task_lessons, "_darwin_boot_id", lambda: "87654321-1234-1234-1234-123456789abc")
    assert task_lessons._process_ended(witness) is True
    monkeypatch.setattr(task_lessons, "_host_platform", lambda: "linux")
    assert task_lessons._process_ended(witness) is False
    assert task_lessons._process_ended({**witness, "start_usec": 1000000}) is False


@pytest.mark.asyncio
async def test_unknown_restart_witness_retains_capacity_and_projects_recovery(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.memory import task_lessons
    monkeypatch.setattr(task_lessons, "_host_platform", lambda: "darwin")
    monkeypatch.setattr(task_lessons, "_darwin_boot_id", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=None, mutation_uuid=str(uuid4())))
    async with async_db() as db:
        task = await _task(db, "task")
    async def interrupted_completion(*args):
        raise OSError("completion event interrupted")
    monkeypatch.setattr(task_lessons, "_finish_io", interrupted_completion)
    with pytest.raises(OSError, match="completion event"):
        await maybe_propose_automatic_lesson(task, "attempt")
    assert all(future.done() for future in task_lessons._AUTOMATIC_IO.values())
    task_lessons._AUTOMATIC_IO.clear()  # Declared restart: no retained future proof.
    await task_lessons._recover_ended_io()
    source = await eligible_lesson_source(operator, "task")
    assert source["restart_witness_unknown"] is True and source["eligible"]
    blocked = await maybe_propose_automatic_lesson(task, "attempt")
    assert blocked["reason_code"] == "automatic_lesson_io_pending"
    manual = await create_task_lesson(operator, request)
    assert manual["result"] == "candidate_inert"
    assert (await inspect_task_lesson(operator, manual["proposal_id"]))["source_current"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux kernel/process-exit proof; Mac profile and driver fixture are separately covered")
async def test_restart_recovery_uses_positive_process_exit_and_exact_private_stage(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    async with async_db() as db:
        task = await _task(db, "task")
    from src.memory import task_lessons
    # Actual local process identity/exit witness for the declared restart seam.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    original_identity, original_finish = task_lessons._process_identity, task_lessons._finish_io
    async def crash_before_completion_event(*args):
        raise OSError("declared private-stage-complete before durable completion crash")
    monkeypatch.setattr(task_lessons, "_finish_io", crash_before_completion_event)
    try:
        identity = original_identity(child.pid)
        assert identity["kind"] == "linux"
        monkeypatch.setattr(task_lessons, "_process_identity", lambda: identity)
        with pytest.raises(OSError, match="durable completion"):
            await maybe_propose_automatic_lesson(task, "attempt")
        assert len(task_lessons._AUTOMATIC_IO) == 1
        assert all(future.done() for future in task_lessons._AUTOMATIC_IO.values())
    finally:
        child.terminate()
        child.wait(timeout=2)
    # Restart loses the in-memory registry, but not canonical start/artifact.
    task_lessons._AUTOMATIC_IO.clear()
    monkeypatch.setattr(task_lessons, "_finish_io", original_finish)
    monkeypatch.setattr(task_lessons, "_process_identity", original_identity)
    await task_lessons._recover_ended_io()
    recovered = await eligible_lesson_source(operator, "task")
    assert recovered["automatic_outcome"]["reason_code"] == "automatic_lesson_recovered_private_stage_no_change"
    replay = await maybe_propose_automatic_lesson(task, "attempt")
    assert replay["result"] == "no_change"
    async with async_db() as db:
        assert not list((await db.execute(select(MemoryProposal))).scalars())
    # The recovered start still consumes its daily slot; only one fresh opt-in
    # start remains, and a third cannot bypass the cap via recovery/replay.
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    assert (await maybe_propose_automatic_lesson(task, "attempt"))["result"] == "candidate_inert"
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(enabled=True,
        expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    assert (await maybe_propose_automatic_lesson(task, "attempt"))["reason_code"] == "automatic_lesson_daily_cap"


def test_evolution_mirror_byte_and_entry_bounds_preserve_original_state(async_db, tmp_path):
    from src.evolution.runtime import EvolutionRuntime, EvolutionRuntimeError
    state = tmp_path / "bounded-mirror.json"
    runtime = EvolutionRuntime(state)
    values = {"proposal_id": "proposal", "owner_id": "operator", "source_digest": "a" * 64,
        "candidate_digest": "b" * 64, "result": "candidate_inert"}
    for payload, error in ((b" " * (1024 * 1024 + 1), "byte limit"),
        (json.dumps({"schema_version": 1, "proposals": {}, "task_lesson_receipts": {str(i): {} for i in range(4097)}}).encode(), "entry limit")):
        state.write_bytes(payload)
        with pytest.raises(EvolutionRuntimeError, match=error):
            runtime.record_task_lesson(**values)
        assert state.read_bytes() == payload


@pytest.mark.asyncio
async def test_source_changes_block_inspection_and_generic_memory_acceptance(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    result = await create_task_lesson(operator, request)
    from src.memory.m5 import apply_memory_proposal_action
    with pytest.raises(ValueError, match="task_method_requires_specialized_review"):
        await apply_memory_proposal_action(owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id, proposal_id=result["proposal_id"], action="accept", expected_revision=1)
    async with async_db() as db:
        row = await _task(db, "task")
        row.task_revision = 2
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["source_current"] is False
    assert inspected["status"] == "blocked"
    assert inspected["reason_code"] == "lesson_source_changed"


@pytest.mark.asyncio
async def test_no_correction_is_no_change_and_unbound_source_is_rejected(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    result = await create_task_lesson(operator, request.model_copy(update={"correction": ""}))
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["new_method"] is None
    assert inspected["reason_code"] == "no_explicit_correction"
    with pytest.raises(BoardError, match="Only references verified"):
        await create_task_lesson(operator, request.model_copy(update={"source_refs": ["private-unrelated-source"]}))


@pytest.mark.asyncio
async def test_actual_completed_read_has_no_success_inferred_preference(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    path = tmp_path / "workspace" / "ordinary.txt"
    path.write_text("Ordinary permitted local source\n")
    path.chmod(0o600)
    from src.tools.filesystem_tool import read_file
    content = read_file.forward(file_path="ordinary.txt")
    assert content == path.read_text()
    sha = hashlib.sha256(content.encode()).hexdigest()
    await workflow_state_repository.record_step_completed(run_identity="run", step_id="read", status="completed", result=content)
    await workflow_state_repository.finish_run(run_identity="run", status="succeeded")
    async with async_db() as db:
        task = await _task(db, "task")
        task.status = "done"
        attempt = await db.get(WorkBoardAttempt, "attempt")
        attempt.outcome = "verified"
        attempt.receipt_refs_json = json.dumps([{"receipt_kind": "readback", "status": "succeeded", "verified": True,
            "workflow_run_id": "run", "content_sha256": sha, "readback_id": "readback:ordinary",
            "artifact_id": "artifact:ordinary", "verified_at": datetime.now(timezone.utc).isoformat()}])
    source = await eligible_lesson_source(operator, "task")
    assert source["eligible"] is True
    assert source["observed"]["status"] == "completed"
    request = request.model_copy(update={"source_refs": source["source_refs"], "correction": ""})
    result = await create_task_lesson(operator, request)
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["new_method"] is None
    assert inspected["positive_preference_vote"] is False
    assert inspected["result"] == "no_change"


@pytest.mark.asyncio
async def test_automatic_requires_explicit_policy_and_is_idempotent_without_positive_vote(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    denied = await propose_automatic_task_lesson(operator, "task")
    assert denied["reason_code"] == "automatic_lessons_not_opted_in"
    enabled = await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(
        enabled=True, expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    assert enabled["enabled"] is True
    assert enabled["daily_cap"] == 2
    source = await eligible_lesson_source(operator, "task")
    assert source["automatic_policy"]["enabled"] is True
    async with async_db() as db:
        task = await _task(db, "task")
    result = await maybe_propose_automatic_lesson(task, "attempt")
    assert result["result"] == "candidate_inert"
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["correction_provenance"] == "observed_failure_rule"
    assert inspected["positive_preference_vote"] is False
    replay = await maybe_propose_automatic_lesson(task, "attempt")
    assert replay["idempotent_replay"] is True
    # Renewing proposal consent never renews the owner-wide daily cap.
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(
        enabled=True, expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    assert (await maybe_propose_automatic_lesson(task, "attempt"))["result"] == "candidate_inert"
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(
        enabled=True, expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    assert (await maybe_propose_automatic_lesson(task, "attempt"))["reason_code"] == "automatic_lesson_daily_cap"
    async with async_db() as db:
        rows = list((await db.execute(select(MemoryProposal))).scalars())
        assert sum(json.loads(row.provenance_json).get("automatic") is True for row in rows) == 2
    disabled = await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(
        enabled=False, expected_revision=1, expected_policy_revision=(await eligible_lesson_source(operator, "task"))["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
    assert disabled["enabled"] is False
    assert (await maybe_propose_automatic_lesson(task, "attempt"))["reason_code"] == "automatic_lessons_not_opted_in"


@pytest.mark.asyncio
async def test_missing_redaction_revoked_root_secret_and_method_tamper_fail_closed(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.memory import m5
    actual_sanitize = m5.sanitize_m5_memory_text_async
    async def unavailable_redaction(*args, **kwargs):
        raise ValueError("redaction state unavailable")
    monkeypatch.setattr(m5, "sanitize_m5_memory_text_async", unavailable_redaction)
    with pytest.raises(BoardError) as blocked:
        await create_task_lesson(operator, request)
    assert blocked.value.code == "lesson_redaction_unavailable"
    assert blocked.value.status_code == 503
    monkeypatch.setattr(m5, "sanitize_m5_memory_text_async", actual_sanitize)
    with pytest.raises(BoardError, match="secret or authority"):
        await create_task_lesson(operator, request.model_copy(update={"correction": "api_key=never-copy-this"}))
    result = await create_task_lesson(operator, request)
    async with async_db() as db:
        step = (await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == "run"))).scalar_one()
        step.tool_name = "write_file"
    assert (await inspect_task_lesson(operator, result["proposal_id"]))["source_current"] is False
    async with async_db() as db:
        root = await db.get(OperatorSession, operator.session_id)
        root.revoked_at = datetime.now(timezone.utc)
    with pytest.raises(BoardError, match="no longer current"):
        await inspect_task_lesson(operator, result["proposal_id"])


def test_closed_method_schema_cannot_install_tools_or_modify_authority(async_db):
    method = {"schema_version": "TaskMethod.v1", "family": "knowledge", "steps": [{"kind": "registered_tool", "tool_id": "read_file"}],
        "registered_tool_ids": ["read_file"], "input_parameters": {},
        "output_contract": {"artifact_type": "task_result", "required_fields": ["status"]}}
    assert TaskMethod.model_validate(method)
    for changes in ({"permissions": ["all"]}, {"registered_tool_ids": ["install_new_tool"]},
                    {"input_parameters": {"provider": "other"}}, {"steps": []}):
        with pytest.raises(ValidationError):
            TaskMethod.model_validate({**method, **changes})
    with pytest.raises(ValidationError):
        ResearchStrategy.model_validate({"query_templates": [], "source_preferences": [],
            "required_evidence_fields": [], "draft_sections": [], "stop_conditions": ["Stop when bounded"], "code": "execute"})
