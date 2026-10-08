"""Atomic native admission mechanics, no model calls or runtime readiness claims."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from config.settings import settings
from src.agent.session import SessionManager, MessageIngressConflictError
from src.agent.turn_execution import NativeTurnAdmission, NativeTurnBlocked
from src.api.chat import _bind_chat_principal, build_chat_ingress_envelope, chat_ingress_metadata
from src.auth.service import create_session
from src.db.engine import get_session
from src.db.models import Message, OperatorSession, Session, WorkBoardTask, WorkflowRunState
from src.runtime_plugins.composition import ReviewedComposition
from tests.test_runtime_composition_ownership import composition_db


@pytest.mark.asyncio
async def test_native_transport_cancel_retains_actual_thread_and_original_root_guard():
    """Callback completion is proved separately from transport cancellation."""
    import asyncio
    from threading import Event
    from types import SimpleNamespace
    from src.agent.turn_execution import NativeTurnExecution
    started, release, root = Event(), Event(), Event()
    admission = SimpleNamespace(deadline_at=datetime.now(timezone.utc) + timedelta(seconds=30))
    execution = NativeTurnExecution(admission, None, None, None)
    def callback():
        started.set()
        assert release.wait(5)
        return "actual callback completed"
    caller = asyncio.create_task(execution.execute(asyncio.to_thread(callback)))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert execution.transport_closed and execution.guard(root).is_set()
        assert not root.is_set()
        assert not execution.worker.done() and not execution.worker.cancelled()
        with pytest.raises(asyncio.TimeoutError):
            execution.remaining()
        release.set()
        assert await asyncio.wait_for(asyncio.shield(execution.worker), 5) == "actual callback completed"
        # A later callback return never reopens the original transport.
        with pytest.raises(asyncio.TimeoutError):
            execution.remaining()
    finally:
        release.set()
        if execution.worker is not None:
            await asyncio.wait_for(asyncio.shield(execution.worker), 5)


@pytest.mark.asyncio
async def test_expired_native_transport_never_starts_callback_or_renews_deadline():
    import asyncio
    from types import SimpleNamespace
    from src.agent.turn_execution import NativeTurnExecution
    deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    execution = NativeTurnExecution(SimpleNamespace(deadline_at=deadline), None, None, None)
    called = False
    async def callback():
        nonlocal called
        called = True
    with pytest.raises(asyncio.TimeoutError):
        await execution.execute(callback())
    assert not called and execution.worker is None and execution.admission.deadline_at == deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_style", ["rest", "ws"])
async def test_controlled_completion_requires_identical_original_future_exception(worker_style):
    import asyncio
    from types import SimpleNamespace
    from src.agent.exceptions import ClarificationRequired
    from src.agent.turn_execution import NativeTurnExecution
    exception = ClarificationRequired(question="Which input?")
    execution = NativeTurnExecution(SimpleNamespace(
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=30)), None, None, None)
    execution.worker = asyncio.get_running_loop().create_future()
    with pytest.raises(NativeTurnBlocked, match="completion_unproven"):
        execution.validate_completed_exception(exception)
    if worker_style == "rest":
        execution.worker.set_exception(exception)
    else:
        execution.worker.set_result(exception)
    execution.validate_completed_exception(exception)
    with pytest.raises(NativeTurnBlocked, match="exception_changed"):
        execution.validate_completed_exception(ClarificationRequired(question="Which input?"))
    execution.close_transport()
    with pytest.raises(asyncio.TimeoutError):
        execution.validate_completed_exception(exception)


async def prepare_turn(monkeypatch, *, content="Plain native turn", route="direct_turn"):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-native-turn")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    _token, operator = await create_session()
    manager = SessionManager()
    conversation = await manager.get_for_ingress(None, owner_principal_id=operator.principal.principal_id)
    principal = _bind_chat_principal(conversation.id, operator=operator)
    ingress = build_chat_ingress_envelope(message=content, session_id=conversation.id,
        principal=principal, operator_session_id=operator.session_id, transport="rest",
        client_message_id="fixed-original-native-turn")
    # Explicit typed server fixture validates SQL mechanics, not actual stock-host preflight.
    reviewed = ReviewedComposition(Path("/owned-fixture"), Path("/owned-fixture/node"),
        "v24.13.1", {}, "b" * 64, "c" * 64)
    admission = NativeTurnAdmission.capture(ingress, principal=principal,
        reviewed_composition=reviewed, native_route=route)
    return manager, ingress, admission, content


async def reserve(manager, ingress, admission, content):
    return await manager.reserve_native_turn_message(ingress.session_id, content,
        message_id=ingress.message_id, metadata_json=chat_ingress_metadata(ingress), admission=admission)


@pytest.mark.asyncio
async def test_plain_ingress_and_bound_job_commit_once_with_original_deadline(composition_db, monkeypatch):
    monkeypatch.setattr(settings, "agent_chat_timeout", 120)
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    message, duplicate, job = await reserve(manager, ingress, admission, content)
    assert not duplicate and message.id == ingress.message_id
    assert job["status"] == "accepted" and job["attempt_count"] == 0
    assert admission.deadline_at == ingress.received_at + timedelta(seconds=60)
    retry_ingress = ingress.model_copy(update={"received_at": ingress.received_at + timedelta(seconds=10)})
    retry = NativeTurnAdmission.capture(retry_ingress, principal=admission.principal,
        reviewed_composition=admission.reviewed_composition, native_route="direct_turn")
    _, duplicate, repeated = await reserve(manager, retry_ingress, retry, content)
    assert duplicate
    original_deadline = datetime.fromisoformat(job["deadline_at"]).replace(tzinfo=timezone.utc)
    assert datetime.fromisoformat(repeated["deadline_at"]).replace(tzinfo=timezone.utc) == original_deadline
    assert original_deadline == admission.deadline_at
    async with get_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message)) == 1
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState)) == 1
        row = await db.scalar(select(WorkflowRunState))
        assert row.session_id == row.operator_session_id == ingress.operator_session_id
        assert row.conversation_id == message.session_id == ingress.conversation_id
        assert row.composition_binding_json and row.max_attempts == 1


@pytest.mark.asyncio
async def test_admission_failure_rolls_back_original_message_and_job(composition_db, monkeypatch):
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    async def fail_after_real_admission(*args, **kwargs):
        await actual(*args, **kwargs)
        raise RuntimeError("injected after original same-writer admission")
    from src.workflows.job_runtime import durable_job_repository
    actual = durable_job_repository._admit_in_session
    monkeypatch.setattr(durable_job_repository, "_admit_in_session", fail_after_real_admission)
    with pytest.raises(RuntimeError, match="same-writer admission"):
        await reserve(manager, ingress, admission, content)
    async with get_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message)) == 0
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState)) == 0


@pytest.mark.asyncio
async def test_revoked_original_root_denies_both_rows(composition_db, monkeypatch):
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    async with get_session() as db:
        root = await db.get(OperatorSession, ingress.operator_session_id)
        root.revoked_at = datetime.now(timezone.utc)
    with pytest.raises(NativeTurnBlocked, match="original_root_inactive"):
        await reserve(manager, ingress, admission, content)
    async with get_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message)) == 0
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before_writer", "same_writer_after_message"])
async def test_task_continuity_native_admission_denies_and_rolls_back(composition_db, monkeypatch, timing):
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    task_id = "owned-continuity-admission-task"
    # Fixture preparation uses the real FK-on factory before the guarded ingress.
    async with composition_db[2]() as db:
        db.add(WorkBoardTask(task_id=task_id, owner_principal_id=ingress.principal_id,
            owner_session_id=ingress.operator_session_id, goal_id="owned-no-execution-goal",
            title="Existing task context", idempotency_key=task_id))
        await db.flush()
        if timing == "before_writer":
            conversation = await db.get(Session, ingress.session_id)
            conversation.continuity_task_id = task_id
        await db.commit()
    if timing == "same_writer_after_message":
        from src.workflows.job_runtime import durable_job_repository
        actual = durable_job_repository._admit_in_session
        async def link_before_original_admission(db, *args, **kwargs):
            assert await db.get(Message, ingress.message_id) is not None
            conversation = await db.get(Session, ingress.session_id)
            conversation.continuity_task_id = task_id
            await db.flush()
            return await actual(db, *args, **kwargs)
        monkeypatch.setattr(durable_job_repository, "_admit_in_session", link_before_original_admission)
    with pytest.raises(NativeTurnBlocked, match="native_turn_continuity_context_unsupported"):
        await reserve(manager, ingress, admission, content)
    async with get_session() as db:
        assert await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id)) is not None
        assert await db.scalar(select(func.count()).select_from(Message)) == 0
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState)) == 0
        conversation = await db.get(Session, ingress.session_id)
        assert conversation.continuity_task_id == (task_id if timing == "before_writer" else None)


@pytest.mark.asyncio
async def test_existing_legacy_message_never_gains_fresh_turn_provenance(composition_db, monkeypatch):
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    await manager.reserve_ingress_message(ingress.session_id, content,
        message_id=ingress.message_id, metadata_json=chat_ingress_metadata(ingress))
    with pytest.raises(NativeTurnBlocked, match="turn_admission_provenance_unavailable"):
        await reserve(manager, ingress, admission, content)
    async with get_session() as db:
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState)) == 0


@pytest.mark.asyncio
async def test_reused_message_identity_with_changed_content_rolls_back(composition_db, monkeypatch):
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    await reserve(manager, ingress, admission, content)
    with pytest.raises(MessageIngressConflictError):
        await reserve(manager, ingress, admission, content + " changed")


@pytest.mark.asyncio
async def test_concurrent_matching_ingress_reserves_one_message_and_job(composition_db, monkeypatch):
    import asyncio
    from src.workspace.production import ProductionWorkspaceReconciliationError
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    results = await asyncio.gather(*(reserve(manager, ingress, admission, content) for _ in range(2)), return_exceptions=True)
    accepted = [result for result in results if not isinstance(result, BaseException)]
    busy = [result for result in results if isinstance(result, BaseException)]
    assert len([result for result in accepted if result[1] is False]) == 1
    assert all(isinstance(error, ProductionWorkspaceReconciliationError)
        and str(error) == "accounting continuity busy" for error in busy)
    assert len(accepted) + len(busy) == 2
    original = next(result[2] for result in accepted if result[1] is False)
    # An explicit caller replay uses the same ORIGINAL captured admission,
    # never a local wait/retry, fresh host/policy capture or renewed deadline.
    _, duplicate, replay = await reserve(manager, ingress, admission, content)
    assert duplicate and replay["job_id"] == original["job_id"]
    assert replay["run_fingerprint"] == original["run_fingerprint"]
    assert datetime.fromisoformat(replay["deadline_at"]).replace(tzinfo=timezone.utc) == admission.deadline_at
    async with get_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message)) == 1
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState)) == 1


@pytest.mark.asyncio
async def test_expired_original_ingress_denies_new_message_and_job(composition_db, monkeypatch):
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    stale_ingress = ingress.model_copy(update={"received_at": datetime.now(timezone.utc) - timedelta(seconds=61)})
    stale = NativeTurnAdmission.capture(stale_ingress, principal=admission.principal,
        reviewed_composition=admission.reviewed_composition, native_route="direct_turn")
    with pytest.raises(NativeTurnBlocked, match="original_deadline_expired"):
        await reserve(manager, stale_ingress, stale, content)
    async with get_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message)) == 0
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState)) == 0


@pytest.mark.asyncio
async def test_policy_change_between_capture_and_writer_denies_admission(composition_db, monkeypatch):
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    monkeypatch.setattr(settings, "agent_chat_timeout", settings.agent_chat_timeout + 1)
    with pytest.raises(NativeTurnBlocked, match="original_policy_changed"):
        await reserve(manager, ingress, admission, content)


@pytest.mark.asyncio
async def test_capture_rejects_principal_bound_to_another_original_root(composition_db, monkeypatch):
    _, ingress, original, _ = await prepare_turn(monkeypatch)
    principal = replace(original.principal, operator_session_id="another-original-root")
    with pytest.raises(NativeTurnBlocked, match="principal_unavailable"):
        NativeTurnAdmission.capture(ingress, principal=principal,
            reviewed_composition=original.reviewed_composition, native_route="direct_turn")
    async with get_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message)) == 0
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, True, 1.0])
async def test_invalid_native_timeout_fails_before_message(composition_db, monkeypatch, timeout):
    manager, ingress, original, content = await prepare_turn(monkeypatch)
    monkeypatch.setattr(settings, "agent_chat_timeout", timeout)
    with pytest.raises(NativeTurnBlocked, match="timeout_invalid"):
        NativeTurnAdmission.capture(ingress, principal=original.principal,
            reviewed_composition=original.reviewed_composition, native_route="direct_turn")


@pytest.mark.asyncio
async def test_retained_native_conversation_delete_denies_before_private_cleanup(composition_db, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock, Mock
    from src.db.models import AudioIngressJob, Session
    from src.runtime_plugins.ownership import CompositionBindingError
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    await reserve(manager, ingress, admission, content)
    raw = tmp_path / "owned-quarantine.wav"
    raw.write_bytes(b"private retained fixture audio")
    now = datetime.now(timezone.utc)
    async with get_session() as db:
        db.add(AudioIngressJob(request_id="owned-retained-audio", request_digest="a" * 64,
            owner_principal_id=ingress.principal_id, operator_session_id=ingress.operator_session_id,
            session_id=ingress.session_id, message_id="legacy-audio-message",
            attachment_id="legacy-audio-attachment", raw_path=str(raw), captured_at=now,
            audio_payload_digest="b" * 64, raw_audio_retention_deadline=now + timedelta(minutes=1)))
    flush = AsyncMock()
    fence = Mock(return_value=True)
    stop = Mock()
    cleanup = Mock(side_effect=lambda *args: raw.unlink())
    monkeypatch.setattr("src.agent.session.flush_session_memory", flush)
    monkeypatch.setattr("src.agent.session.process_runtime_manager.begin_session_cleanup", fence)
    monkeypatch.setattr("src.agent.session.process_runtime_manager.stop_processes_for_session", stop)
    monkeypatch.setattr("src.guardian.audio_worker.cleanup_audio_job_paths", cleanup)
    with pytest.raises(CompositionBindingError, match="composition_retained_session_cleanup_denied"):
        await manager.delete(ingress.session_id, owner_principal_id=ingress.principal_id)
    flush.assert_not_awaited()
    fence.assert_not_called()
    stop.assert_not_called()
    cleanup.assert_not_called()
    assert raw.read_bytes() == b"private retained fixture audio"
    async with get_session() as db:
        assert await db.get(Session, ingress.session_id) is not None
        assert await db.get(Message, ingress.message_id) is not None
