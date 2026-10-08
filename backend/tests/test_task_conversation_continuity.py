"""Real SQLite and authenticated local readback; no provider inference."""
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from config.settings import settings
from src.artifacts.registry import artifact_id_for
from src.conversation.task_context import TaskContinuityService
from src.db.models import Session, WorkBoardAttempt, WorkBoardComment, WorkBoardEvent, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.db.engine import _ensure_legacy_columns
from src.work_board.repository import WorkBoardRepository
from src.agent.session import session_manager
from src.auth.service import authenticate_token, bind_operator_principal
from src.approval.runtime import set_runtime_context, reset_runtime_context
from src.security.trust_contract import AuthorityGrant, canonical_digest
from tests.test_operator_identity import auth, enroll, goal, login, recover, HEADERS


@pytest_asyncio.fixture(autouse=True)
async def continuity(app, async_db, monkeypatch):
    monkeypatch.setattr("src.api.sessions.get_session", async_db)
    service = TaskContinuityService(WorkBoardRepository())
    await service.start()
    app.state.task_continuity = service
    session_manager.bind_task_continuity(service)
    yield service
    session_manager.bind_task_continuity(None)
    await service.stop()


async def task(client):
    await login(client)
    g = await goal(client)
    response = await client.post("/api/work-board/tasks", headers=HEADERS, json={
        "title": "Continue durable task", "goal_id": g["id"], "goal_revision": 1, "idempotency_key": "continuity-task"})
    assert response.status_code == 200, response.text
    return g, response.json()["task"]


async def operator_comment(client, t, body):
    response = await client.post(f"/api/work-board/tasks/{t['task_id']}/comments", headers=HEADERS,
        json={"expected_revision": t["task_revision"], "body": body})
    assert response.status_code == 200, response.text
    t["task_revision"] += 1
    return response.json()["comment"]["comment_id"]


async def read(client, t):
    return await client.get(f"/api/sessions/task-context/{t['task_id']}")


async def continue_chat(client, t, identifier="new-chat", revision=None):
    return await client.post("/api/sessions/continue-task", headers=HEADERS, json={
        "task_id": t["task_id"], "expected_revision": revision or t["task_revision"], "new_conversation_id": identifier})


async def test_new_chat_same_task_reference_and_idempotent_revision(client, async_db):
    _, t = await task(client)
    first = await continue_chat(client, t)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    replay = await continue_chat(client, t)
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    other = await continue_chat(client, t, "channel-chat")
    assert other.status_code == 200
    current = await read(client, t)
    assert current.headers["cache-control"] == "no-store"
    packet = current.json()
    assert set(packet["conversation_ids"]) == {"new-chat", "channel-chat"}
    assert packet["revision"] == t["task_revision"]
    assert packet["model_context_allowed"] is False and packet["memory_status"] == "no_learning"
    assert packet["summary_kind"] == "factual_canonical_timeline"
    assert (await client.get("/api/sessions/new-chat/messages")).json() == []
    async with async_db() as db:
        assert await db.scalar(select(func.count()).select_from(WorkBoardTask)) == 1
        assert await db.scalar(select(func.count()).select_from(WorkBoardAttempt)) == 0
        conversation = await db.get(Session, "new-chat")
        assert conversation.continuity_task_id == t["task_id"]
        assert (await client.get("/api/sessions")).json()[0]["continuity_task_id"] == t["task_id"]


async def test_stale_revision_does_not_create_chat_or_replay_intent(client, async_db):
    _, t = await task(client)
    result = await continue_chat(client, t, revision=t["task_revision"] + 1)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "task_context_revision_stale"
    async with async_db() as db:
        assert await db.get(Session, "new-chat") is None


async def test_conversation_owner_and_existing_binding_conflicts(client, async_db):
    _, t = await task(client)
    async with async_db() as db:
        db.add(Session(id="foreign-chat", owner_principal_id="different-principal"))
    result = await continue_chat(client, t, "foreign-chat")
    assert result.status_code == 409
    await continue_chat(client, t)
    async with async_db() as db:
        row = await db.get(Session, "foreign-chat")
        assert row.owner_principal_id == "different-principal" and row.continuity_task_id is None


async def test_independent_login_cannot_read_or_link_another_owner_task(client, app):
    _, t = await task(client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await login(other)
        assert (await read(other, t)).status_code in {403, 404}
        assert (await continue_chat(other, t, "stolen-chat")).status_code in {403, 404}


async def test_only_selected_task_does_not_recover_goal_scope(client):
    _, t = await task(client)
    await enroll(client)
    await client.post("/api/auth/logout", headers=HEADERS)
    await login(client)
    await recover(client, [{"kind": "task", "record_id": t["task_id"]}])
    assert (await read(client, t)).status_code in {403, 404}
    assert (await continue_chat(client, t)).status_code in {403, 404}


async def test_missing_evidence_packet_keeps_factual_context_and_continuation_usable(client, async_db):
    _, t = await task(client)
    async with async_db() as db:
        row = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == t["task_id"]))).scalar_one()
        db.add(WorkBoardEvent(task_id=row.task_id, owner_principal_id=row.owner_principal_id,
            owner_session_id=row.owner_session_id, actor_principal_id=row.owner_principal_id,
            kind="task.evidence.updated", metadata_json=json.dumps({"packet_revision": 1, "packet_digest": "a" * 64})))
    result = await read(client, t)
    assert result.status_code == 200, result.text
    assert result.json()["evidence_state"] == "evidence_packet_unavailable"
    assert result.json()["summary_kind"] == "factual_canonical_timeline"
    assert (await continue_chat(client, t)).status_code == 200


async def test_new_login_selected_history_continues_read_only_without_task_adoption(client, async_db):
    g, t = await task(client)
    await enroll(client)
    old_chat = await continue_chat(client, t, "historical-chat")
    assert old_chat.status_code == 200
    async with async_db() as db:
        original = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == t["task_id"]))).scalar_one()
        old_owner = (original.owner_principal_id, original.owner_session_id, original.task_revision, original.status)
    await client.post("/api/auth/logout", headers=HEADERS)
    await login(client)
    denied = await read(client, t)
    assert denied.status_code in {403, 404}
    journal, _ = await recover(client, [{"kind": "goal", "record_id": g["id"]}, {"kind": "task", "record_id": t["task_id"]}])
    continued = await continue_chat(client, t, "recovered-chat")
    assert continued.status_code == 200, continued.text
    packet = continued.json()["task_context"]
    assert packet["ownership_access"] == "recovered_read_only"
    assert packet["conversation_ids"] == ["recovered-chat"]
    assert packet["execution_block_reason"] == "current_scope_review_required"
    assert packet["private_source_refs"] == [] and packet["verified_artifact_refs"] == []
    assert (await client.get("/api/sessions/historical-chat/messages")).status_code == 403
    async with async_db() as db:
        unchanged = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == t["task_id"]))).scalar_one()
        assert (unchanged.owner_principal_id, unchanged.owner_session_id, unchanged.task_revision, unchanged.status) == old_owner
    rolled = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/rollback", headers=HEADERS)
    assert rolled.status_code == 200, rolled.text
    assert (await read(client, t)).status_code in {403, 404}


async def test_questions_unknown_and_corrections_are_separate_bounded_metadata(client, async_db):
    _, t = await task(client)
    async with async_db() as db:
        row = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == t["task_id"]))).scalar_one()
        row.status = WorkBoardStatus.blocked
        row.block_kind = "needs_input"
        row.block_reason = "Supply the reviewed recipient"
        row.body = "Remaining: verify the recipient before scheduling"
        db.add(WorkBoardComment(comment_id="foreign-correction", task_id=row.task_id,
            owner_principal_id=row.owner_principal_id, owner_session_id=row.owner_session_id,
            author_principal_id="foreign-owner", author_session_id=row.owner_session_id,
            body="FOREIGN_CORRECTION_SENTINEL"))
        for index in range(20):
            db.add(WorkBoardEvent(task_id=row.task_id, owner_principal_id=row.owner_principal_id,
                owner_session_id=row.owner_session_id, actor_principal_id=row.owner_principal_id,
                kind="comment.created", metadata_json=json.dumps({"body": "PRIVATE", "task_revision": row.task_revision})))
        db.add(WorkBoardAttempt(task_id=row.task_id, outcome="unknown_external_effect"))
    correction_id = await operator_comment(client, t, "PRIVATE CORRECTION: change the recipient to Alice")
    response = await read(client, t)
    assert response.status_code == 200, response.text
    packet = response.json()
    assert packet["open_questions"] and packet["next_actions"]
    assert packet["unresolved_effect"] == "unknown_external_effect"
    assert packet["truncated"] and len(packet["timeline"]) == 16
    assert packet["correction_refs"] == [f"task-comment:{correction_id}"]
    assert packet["corrections"][0]["body"] == "PRIVATE CORRECTION: change the recipient to Alice"
    assert packet["corrections"][0]["model_context_allowed"] is False
    assert packet["remaining_work"] == ["Remaining: verify the recipient before scheduling"]
    assert "FOREIGN_CORRECTION_SENTINEL" not in response.text
    await continue_chat(client, t)
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    prompt = await session_manager.get_task_continuity_context("new-chat", trust_principal=bind_operator_principal(operator, "new-chat"))
    assert "Remaining: verify the recipient before scheduling" in prompt
    assert "Supply the reviewed recipient" in prompt
    assert f"task-comment:{correction_id}" in prompt and "Review operator corrections locally" in prompt
    assert "PRIVATE CORRECTION" not in prompt and "FOREIGN_CORRECTION" not in prompt
    assert packet["corrections"][0]["digest"] not in prompt and '"digest"' not in prompt
    assert len(response.content) < 16_384


async def test_output_is_verified_from_local_bytes_and_deleted_output_disappears(client, async_db, tmp_path, monkeypatch):
    _, t = await task(client)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    tmp_path.chmod(0o700)
    path = tmp_path / "artifacts" / "continuity.md"
    path.parent.mkdir(mode=0o700)
    path.write_text("Actual local readback")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    artifact = artifact_id_for(file_path="artifacts/continuity.md", artifact_type="markdown_document",
        producer="local-continuity", run_id="continuity-run", content_sha256=digest)
    async with async_db() as db:
        row = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == t["task_id"]))).scalar_one()
        attempt = WorkBoardAttempt(task_id=row.task_id, workflow_run_id="continuity-run", outcome="verified")
        db.add(attempt)
        db.add(WorkflowRunState(run_identity="continuity-run", root_run_identity="continuity-run", workflow_name="continuity", job_kind="local-continuity", status="succeeded",
            owner_kind="user", owner_principal_id=row.owner_principal_id, operator_session_id=row.owner_session_id,
            goal_id=row.goal_id, goal_revision=row.goal_revision,
            artifact_receipts_json=json.dumps([{"artifact_id": artifact, "artifact_type": "markdown_document", "producer": "local-continuity",
                "exists": True, "file_path": "artifacts/continuity.md", "content_sha256": digest}]),
            effect_receipts_json=json.dumps([{"receipt_kind": "readback", "status": "succeeded", "target_path": "artifacts/continuity.md",
                "target_digest": digest, "content_sha256": digest, "details": {"verified": True}}])))
    first = await read(client, t)
    assert first.status_code == 200, first.text
    assert first.json()["verified_artifact_refs"] == [artifact]
    path.unlink()
    second = await read(client, t)
    assert second.json()["verified_artifact_refs"] == []
    assert second.json()["evidence_state"] == "changed_or_deleted_sources"
    assert "Actual local readback" not in second.text


async def test_lifecycle_inactive_is_visible(client, continuity):
    _, t = await task(client)
    await continuity.stop()
    result = await read(client, t)
    assert result.status_code == 503 and result.json()["detail"]["code"] == "task_continuity_unavailable"


async def test_direct_async_stream_and_guardian_use_same_factual_task_handoff(client, async_db, monkeypatch, mocked_canonical_inference_context):
    from src.agent.direct_chat import run_direct_local_chat, stream_direct_local_chat
    from src.guardian.state import build_guardian_state
    _, t = await task(client)
    await continue_chat(client, t)
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    principal = bind_operator_principal(operator, "new-chat")
    runtime = set_runtime_context("new-chat", "high_risk", trust_principal=principal)
    captured = []
    def completion(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Literal local response"))])
    async def stream(**kwargs):
        captured.append(kwargs)
        yield "Literal local delta"
    monkeypatch.setattr("src.agent.direct_chat.completion_with_fallback_sync", completion)
    monkeypatch.setattr("src.agent.direct_chat.stream_completion_with_fallback", stream)
    monkeypatch.setattr("src.agent.direct_chat._uses_openrouter_profile", lambda path: True)
    try:
        expected = await session_manager.get_task_continuity_context("new-chat")
        assert t["task_id"] in expected and "no execution authority" in expected
        result = await run_direct_local_chat("Continue", runtime_path="chat_agent", is_onboarding=False, session_id="new-chat")
        assert result == "Literal local response"
        assert [part async for part in stream_direct_local_chat("Continue", runtime_path="chat_agent", is_onboarding=False, session_id="new-chat")] == ["Literal local delta"]
        for request in captured:
            assert request["messages"][0]["content"].endswith(expected)
            assert request["request_context"].data_digest == canonical_digest(request["messages"])
        with patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)):
            state = await build_guardian_state(session_id="new-chat", user_message="Continue",
                owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
        assert state.current_session_history.endswith(expected)
    finally:
        reset_runtime_context(runtime)


async def test_missing_model_grant_revoked_root_and_recovered_scope_block_assistant_context(client, async_db):
    g, t = await task(client)
    await enroll(client)
    await continue_chat(client, t)
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    principal = bind_operator_principal(operator, "new-chat")
    runtime = set_runtime_context("new-chat", "high_risk", trust_principal=replace(principal, grants=(AuthorityGrant.INGRESS,)))
    try:
        blocked = await session_manager.get_task_continuity_context("new-chat")
        assert "blocked" in blocked and t["task_id"] not in blocked
    finally:
        reset_runtime_context(runtime)
    await client.post("/api/auth/logout", headers=HEADERS)
    runtime = set_runtime_context("new-chat", "high_risk", trust_principal=principal)
    try:
        assert "blocked" in await session_manager.get_task_continuity_context("new-chat")
    finally:
        reset_runtime_context(runtime)
    await login(client)
    await recover(client, [{"kind": "goal", "record_id": g["id"]}, {"kind": "task", "record_id": t["task_id"]}])
    await continue_chat(client, t, "new-recovered-chat")
    current = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    runtime = set_runtime_context("new-recovered-chat", "high_risk", trust_principal=bind_operator_principal(current, "new-recovered-chat"))
    try:
        blocked = await session_manager.get_task_continuity_context("new-recovered-chat")
        assert "blocked" in blocked and t["task_id"] not in blocked
        assert (await read(client, t)).json()["assistant_context_state"] == "current_scope_review_required"
    finally:
        reset_runtime_context(runtime)


async def test_selected_source_ref_needs_current_packet_adoption_and_never_copies_source_text(client, async_db, tmp_path, monkeypatch):
    from src.db.models import Memory, MemorySource
    from src.memory.evidence_working_set import EvidenceRequest, EvidenceAdoptionRequest, refresh_evidence, adopt_evidence
    from src.work_board.contracts import WorkBoardOwner
    g, t = await task(client)
    await continue_chat(client, t)
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    tmp_path.chmod(0o700)
    async with async_db() as db:
        memory = Memory(content="PRIVATE SOURCE TEXT NEVER COPIED", source_session_id=owner.session_id,
            confidence=0.9, metadata_json=json.dumps({"goal_id": g["id"]}))
        db.add(memory)
        await db.flush()
        db.add(MemorySource(memory_id=memory.id, source_session_id=owner.session_id, source_type="operator"))
    async with async_db() as db:
        evidence = await refresh_evidence(db, owner, t["task_id"], EvidenceRequest(
            expected_task_revision=t["task_revision"], expected_packet_revision=0, query="PRIVATE SOURCE TEXT"), operator=operator)
    assert evidence["claims"] and evidence["claims"][0]["private_source"] is False
    source_id = evidence["claims"][0]["source_id"]
    runtime = set_runtime_context("new-chat", "high_risk", trust_principal=bind_operator_principal(operator, "new-chat"))
    try:
        not_adopted = await session_manager.get_task_continuity_context("new-chat")
        assert source_id not in not_adopted
        async with async_db() as db:
            await adopt_evidence(db, owner, t["task_id"], EvidenceAdoptionRequest(expected_task_revision=t["task_revision"],
                expected_packet_revision=evidence["revision"], expected_packet_digest=evidence["digest"], allow_model_context=True), operator=operator)
        allowed = await session_manager.get_task_continuity_context("new-chat")
        assert source_id in allowed and memory.content not in allowed
        packet = (await read(client, t)).json()
        assert packet["private_source_refs"] == [] and packet["source_egress"][0]["model_context_allowed"] is True
        async with async_db() as db:
            await adopt_evidence(db, owner, t["task_id"], EvidenceAdoptionRequest(expected_task_revision=t["task_revision"],
                expected_packet_revision=evidence["revision"], expected_packet_digest=evidence["digest"], allow_model_context=False), operator=operator)
        assert source_id not in await session_manager.get_task_continuity_context("new-chat")
    finally:
        reset_runtime_context(runtime)


@pytest.mark.parametrize("recovered", [False, True])
async def test_actual_rest_and_ws_pre_run_guardian_receive_narrowed_principal(client, async_db, monkeypatch, recovered):
    from smolagents import FinalAnswerStep
    from fastapi import WebSocketDisconnect
    from src.api.ws import websocket_chat
    g, t = await task(client)
    async with async_db() as db:
        row = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == t["task_id"]))).scalar_one()
        row.body = "TASK_REMAINING_SENTINEL verify recipient before scheduling"
        row.block_reason = "TASK_BLOCKER_SENTINEL supply recipient"
    correction_id = await operator_comment(client, t, "PRIVATE_CORRECTION_SENTINEL change recipient")
    if recovered:
        await enroll(client)
        await client.post("/api/auth/logout", headers=HEADERS)
        await login(client)
        await recover(client, [{"kind": "goal", "record_id": g["id"]}, {"kind": "task", "record_id": t["task_id"]}])
    await continue_chat(client, t)
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    prepared = []

    class LiteralAgent:
        def run(self, message, stream=False):
            if stream:
                return iter([FinalAnswerStep(output="Literal isolated result")])
            return "Literal isolated result"

    def build(*, guardian_state=None):
        assert guardian_state is not None, "A silent minimal-agent fallback cannot prove context continuity"
        # Existing agent factory consumes the prompt block and history separately.
        prepared.append(guardian_state.to_prompt_block() + "\n" + guardian_state.current_session_history)
        return LiteralAgent()

    # The canonical compiler and authority/evidence owners stay real. The
    # local execution adapter supplies a literal result without model contact.
    monkeypatch.setattr("src.api.chat.build_agent", build)
    monkeypatch.setattr("src.api.ws.build_agent", build)
    monkeypatch.setattr("src.api.chat.get_or_create_profile", AsyncMock(return_value=SimpleNamespace(onboarding_completed=True)))
    monkeypatch.setattr("src.api.ws.get_or_create_profile", AsyncMock(return_value=SimpleNamespace(onboarding_completed=True)))
    monkeypatch.setattr("src.api.chat.should_use_direct_local_chat", lambda *args, **kwargs: False)
    monkeypatch.setattr("src.api.ws.should_use_direct_local_chat", lambda *args, **kwargs: False)
    monkeypatch.setattr("src.api.ws.authenticate_websocket", AsyncMock(return_value=operator))
    monkeypatch.setattr("src.memory.hybrid_retrieval.search_with_status", lambda *args, **kwargs: ([], False))
    monkeypatch.setattr(settings, "guardian_state_timeout_seconds", 5.0)
    result = await client.post("/api/chat", headers=HEADERS, json={"session_id": "new-chat", "message": "Continue the durable task"})
    assert result.status_code == 200, result.text

    class LocalWebSocket:
        def __init__(self):
            self.messages = []
            self.received = False
        async def accept(self):
            pass
        async def close(self, **kwargs):
            pass
        async def send_text(self, text):
            self.messages.append(json.loads(text))
        async def receive_text(self):
            if self.received:
                raise WebSocketDisconnect()
            self.received = True
            return json.dumps({"session_id": "new-chat", "message": "Continue the durable task"})

    socket = LocalWebSocket()
    await websocket_chat(socket)
    assert len(prepared) == 2
    assert any(message["type"] == "final" and message["content"] == "Literal isolated result" for message in socket.messages)
    for history in prepared:
        if recovered:
            assert "Task continuation context is blocked" in history
            assert t["task_id"] not in history and g["id"] not in history
            assert "TASK_REMAINING_SENTINEL" not in history and "TASK_BLOCKER_SENTINEL" not in history
        else:
            assert "CANONICAL TASK CONTINUITY" in history and t["task_id"] in history
            assert "no execution authority" in history
            assert "TASK_REMAINING_SENTINEL verify recipient before scheduling" in history
            assert f"task-comment:{correction_id}" in history
        assert "PRIVATE SOURCE TEXT" not in history and "PRIVATE_CORRECTION_SENTINEL" not in history
        assert hashlib.sha256(b"PRIVATE_CORRECTION_SENTINEL change recipient").hexdigest() not in history


@pytest.mark.parametrize("linked", [False, True])
async def test_actual_guardian_prompt_never_selects_foreign_recent_transcripts(client, async_db, monkeypatch, linked):
    from src.guardian.state import build_guardian_state
    _, t = await task(client)
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    if linked:
        await continue_chat(client, t)
    else:
        await session_manager.get_or_create("new-chat", owner_principal_id=operator.principal.principal_id)
    await session_manager.get_or_create("foreign-history", owner_principal_id="operator:root:other-owner")
    await session_manager.add_message("foreign-history", "assistant", "FOREIGN_OWNER_TRANSCRIPT_MARKER")
    await session_manager.get_or_create("current-owned-history", owner_principal_id=operator.principal.principal_id)
    await session_manager.add_message("current-owned-history", "assistant", "CURRENT_OWNED_TRANSCRIPT_MARKER")
    monkeypatch.setattr("src.memory.hybrid_retrieval.search_with_status", lambda *args, **kwargs: ([], False))
    state = await build_guardian_state(session_id="new-chat", user_message="Continue",
        owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
        trust_principal=bind_operator_principal(operator, "new-chat"))
    prompt = state.to_prompt_block()
    assert "CURRENT_OWNED_TRANSCRIPT_MARKER" in prompt
    assert "FOREIGN_OWNER_TRANSCRIPT_MARKER" not in prompt


@pytest.mark.parametrize("denial", ["no_principal", "no_model_grant", "wrong_chat", "revoked_root"])
async def test_actual_guardian_prompt_denies_current_and_recent_history_without_live_chat_authority(client, async_db, monkeypatch, denial):
    from src.guardian.state import build_guardian_state
    await task(client)
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    for chat in ("new-chat", "owned-prior"):
        await session_manager.get_or_create(chat, owner_principal_id=operator.principal.principal_id)
        await session_manager.add_message(chat, "assistant", "DENIED_TRANSCRIPT_SENTINEL")
    principal = bind_operator_principal(operator, "new-chat")
    if denial == "no_principal":
        principal = None
    elif denial == "no_model_grant":
        principal = replace(principal, grants=(AuthorityGrant.INGRESS,))
    elif denial == "wrong_chat":
        principal = replace(principal, session_id="another-chat")
    else:
        await client.post("/api/auth/logout", headers=HEADERS)
    monkeypatch.setattr("src.memory.hybrid_retrieval.search_with_status", lambda *args, **kwargs: ([], False))
    state = await build_guardian_state(session_id="new-chat", user_message="Continue",
        owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
        trust_principal=principal)
    assert "DENIED_TRANSCRIPT_SENTINEL" not in state.to_prompt_block()
    assert state.recent_sessions_summary == "" and state.current_session_history == ""


async def test_correction_provenance_is_server_owned_and_worker_flood_cannot_hide_operator_input(client, async_db, monkeypatch):
    from src.work_board.contracts import WorkBoardCommentCreate, WorkBoardOwner
    from src.work_board.tools import WorkBoardWorkerTools, WorkBoardWorkerComment
    _, t = await task(client)
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    refs = []
    for index in range(4):
        refs.append("task-comment:" + await operator_comment(client, t, f"Explicit operator correction {index}"))
    spoof = await client.post(f"/api/work-board/tasks/{t['task_id']}/comments", headers=HEADERS,
        json={"expected_revision": t["task_revision"], "body": "client spoof", "provenance": "operator"})
    assert spoof.status_code == 422
    worker = WorkBoardWorkerTools(session_provider=async_db)
    # Admission is isolated; the actual tool/repository/event writer remains real.
    monkeypatch.setattr(worker, "_bound", AsyncMock(return_value=(owner, None, None, None)))
    for index in range(12):
        result = await worker.comment(WorkBoardWorkerComment(task_id=t["task_id"], attempt_id="literal-attempt",
            expected_task_revision=t["task_revision"], board_fencing_token=1,
            workflow_run_id="literal-workflow", workflow_fencing_token=1, body=f"WORKER_NOTE_SENTINEL {index}"))
        t["task_revision"] = result["task_revision"]
    async with async_db() as db:
        unknown, event = await WorkBoardRepository().add_comment(db, owner, t["task_id"],
            WorkBoardCommentCreate(expected_revision=t["task_revision"], body="UNKNOWN_NOTE_SENTINEL"))
        assert json.loads(event.metadata_json)["provenance"] == "unknown"
        t["task_revision"] += 1
        # Existing review writer uses this canonical owner-attributed shape,
        # without an authenticated operator comment.created provenance event.
        db.add(WorkBoardComment(task_id=t["task_id"], owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, author_principal_id=owner.principal_id,
            author_session_id=owner.session_id, body="Changes requested: REVIEW_NOTE_SENTINEL"))
        worker_events = list((await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.task_id == t["task_id"],
            WorkBoardEvent.kind == "comment.created"))).scalars())
        assert sum(json.loads(event.metadata_json).get("provenance") == "worker" for event in worker_events) == 12
    packet = (await read(client, t)).json()
    assert set(packet["correction_refs"]) == set(refs)
    assert len(packet["corrections"]) == 4
    assert all("Explicit operator correction" in c["body"] for c in packet["corrections"])
    assert "NOTE_SENTINEL" not in json.dumps(packet)
    await continue_chat(client, t)
    prompt = await session_manager.get_task_continuity_context("new-chat", trust_principal=bind_operator_principal(operator, "new-chat"))
    assert all(ref in prompt for ref in refs)
    assert "NOTE_SENTINEL" not in prompt and "Explicit operator correction" not in prompt
    assert all(c["digest"] not in prompt for c in packet["corrections"])
    # A body correction without an exact replacement provenance event invalidates
    # the old correction identity instead of sending transformed private content.
    async with async_db() as db:
        row = await db.get(WorkBoardComment, refs[0].split(":", 1)[1])
        row.body = "CHANGED_BODY_SENTINEL"
    assert refs[0] not in (await read(client, t)).json()["correction_refs"]


async def test_additive_migration_is_rerunnable_and_keeps_old_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/old.sqlite")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE sessions (id VARCHAR PRIMARY KEY, title VARCHAR)")
        await conn.exec_driver_sql("INSERT INTO sessions VALUES ('old', 'Kept')")
        await _ensure_legacy_columns(conn)
        await _ensure_legacy_columns(conn)
        rows = (await conn.exec_driver_sql("SELECT id,title,continuity_task_id FROM sessions")).all()
        assert rows == [("old", "Kept", None)]
        indexes = (await conn.exec_driver_sql("PRAGMA index_list(sessions)")).all()
        assert "ix_sessions_continuity_task_id" in [index[1] for index in indexes]
    await engine.dispose()
