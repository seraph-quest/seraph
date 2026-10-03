"""Actual auth/SQLite/Work/outbox with synthetic Telegram HTTP only."""
import json
import asyncio
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from sqlmodel import select

from config.settings import settings
from src.app import create_app
from src.approval.repository import approval_repository
from src.db.models import (ApprovalRequest, TelegramTaskCallback, TelegramTransportOutbox,
    TelegramTransportState, WorkBoardAttempt, WorkBoardEvent, WorkBoardTask, WorkflowRunState)
from src.extensions.telegram_task_controls import now
from src.extensions.telegram_transport import TelegramTransportAdapter
from tests.test_first_result_setup import authenticated_setup_operator, setup_workspace, create_snapshot

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("async_db", ["file"], indirect=True)]


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    async def deny_async(*args, **kwargs):
        raise AssertionError("Unintercepted live HTTP is forbidden in Telegram control proof")
    def deny_sync(*args, **kwargs):
        raise AssertionError("Unintercepted live HTTP is forbidden in Telegram control proof")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync)


class SyntheticTelegramHTTP:
    """Exercise Bot API JSON through an HTTP client; no network transport exists."""
    def __init__(self):
        self.messages = []
        self.updates = []
        self.acks = 0
        self.lose_send = False
        self.lose_ack = False
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        assert request.url.host == "api.telegram.org"
        data = json.loads(request.content)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "sendMessage":
            self.messages.append(data)
            if self.lose_send:
                raise httpx.ReadTimeout("synthetic response loss", request=request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.messages)}})
        if method == "answerCallbackQuery":
            self.acks += 1
            if self.lose_ack:
                raise httpx.ReadTimeout("synthetic acknowledgment loss", request=request)
            return httpx.Response(200, json={"ok": True, "result": True})
        assert method == "getUpdates"
        results = list(self.updates)
        self.updates.clear()
        return httpx.Response(200, json={"ok": True, "result": results})

    async def send_message(self, *, token, chat_id, text, idempotency_key, reply_markup=None):
        response = await self.http.post("https://api.telegram.org/botSynthetic/sendMessage",
            json={"chat_id": chat_id, "text": text, **({"reply_markup": reply_markup} if reply_markup else {})})
        return {"status_code": response.status_code, "message_id": response.json()["result"]["message_id"]}

    async def answer_callback_query(self, *, token, callback_query_id):
        response = await self.http.post("https://api.telegram.org/botSynthetic/answerCallbackQuery",
            json={"callback_query_id": callback_query_id})
        return response.json()

    async def get_updates(self, *, token, offset, timeout_seconds, limit):
        return (await self.http.post("https://api.telegram.org/botSynthetic/getUpdates",
            json={"offset": offset, "timeout": timeout_seconds, "limit": limit})).json()


async def prepare(client, async_db, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    boundary = SyntheticTelegramHTTP()
    adapter = TelegramTransportAdapter(transport=boundary)
    monkeypatch.setattr("src.api.telegram.default_telegram_transport", adapter)
    goal = await client.post("/api/goals", json={"title": "PRIVATE_GOAL"})
    assert goal.status_code == 200, goal.text
    created = await client.post("/api/work-board/tasks", json={"title": "PRIVATE_TITLE",
        "body": "PRIVATE_BODY", "goal_id": goal.json()["id"], "goal_revision": 1,
        "status": "triage", "idempotency_key": "telegram-test-task"})
    assert created.status_code == 200, created.text
    task = created.json()["task"]
    pairing = await client.post("/api/telegram/pair", json={"operator_id": 42,
        "chat_id": 77, "bot_token": "synthetic-only"})
    assert pairing.status_code == 200, pairing.text
    consent = await client.post("/api/telegram/consent", json={"boundary": "telegram_transit"})
    assert consent.status_code == 200, consent.text
    approval = await approval_repository.get_or_create_pending(
        session_id=task["owner_session_id"], tool_name="telegram-test-decision", risk_level="high",
        summary="PRIVATE_APPROVAL", fingerprint="exact-telegram-decision",
        details={"approval_owner_principal_id": task["owner_principal_id"],
            "approval_owner_operator_session_id": task["owner_session_id"],
            "work_board_task_id": task["task_id"], "arguments": {"private": "PRIVATE_ARGUMENT"}})
    return boundary, adapter, task, approval


async def send_notice(client, task, key="notice-a"):
    notice = await client.post(f"/api/telegram/tasks/{task['task_id']}/notice",
        json={"expected_revision": task["task_revision"], "idempotency_key": key})
    assert notice.status_code == 200, notice.text
    delivered = await client.post(f"/api/telegram/outbox/{notice.json()['id']}/deliver")
    assert delivered.status_code == 200, delivered.text
    return notice.json(), delivered.json()


def callback(boundary, index, *, update_id=1, query_id="query-a", actor=42, chat=77, button=0):
    wire = boundary.messages[index]["reply_markup"]["inline_keyboard"][0][button]["callback_data"]
    assert len(wire.encode()) <= 64
    return {"update_id": update_id, "callback_query": {"id": query_id,
        "from": {"id": actor}, "message": {"message_id": index+1, "date": 123,
            "chat": {"id": chat, "type": "private"}}, "data": wire}}


async def review(client, boundary, task):
    await send_notice(client, task)
    response = await client.post("/api/telegram/updates", json=callback(boundary, 0))
    assert response.status_code == 200, response.text
    assert response.json()["memory_status"] == "no_learning"
    delivered = await client.post(f"/api/telegram/outbox/{response.json()['outbox_id']}/deliver")
    assert delivered.status_code == 200, delivered.text
    return response


async def test_actual_http_pairing_task_deny_replay_restart(client, async_db, setup_workspace, monkeypatch):
    boundary, adapter, task, approval = await prepare(client, async_db, monkeypatch)
    await review(client, boundary, task)
    deny = callback(boundary, 1, update_id=2, query_id="deny-a")
    boundary.updates.append(deny)
    polled = await client.post("/api/telegram/poll", json={"limit": 1, "timeout_seconds": 1})
    assert polled.status_code == 200, polled.text
    assert (await approval_repository.get(approval.id)).status == "denied"
    assert boundary.acks >= 2
    monkeypatch.setattr("src.api.telegram.default_telegram_transport", TelegramTransportAdapter(transport=boundary))
    client._transport = httpx.ASGITransport(app=create_app())
    replay = await client.post("/api/telegram/updates", json=deny)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "denied"
    forged = {**deny, "callback_query": {**deny["callback_query"], "id": "fresh-query"}}
    assert (await client.post("/api/telegram/updates", json=forged)).status_code != 200
    detail = await client.get(f"/api/work-board/tasks/{task['task_id']}")
    assert detail.status_code == 200 and detail.json()["task"]["task_id"] == task["task_id"]
    async with async_db() as db:
        rows = (await db.execute(select(TelegramTaskCallback))).scalars().all()
        assert sum(row.effect == "deny" and row.status == "consumed" for row in rows) == 1
    for sent in boundary.messages:
        assert "PRIVATE_" not in sent["text"] and len(sent["text"].encode()) <= 1024
    await boundary.http.aclose()


@pytest.mark.parametrize("change", ["actor", "chat", "task", "goal", "details", "pairing", "expiry", "out_of_order"])
async def test_exact_authority_rejects_without_consuming_approval(client, async_db, setup_workspace, monkeypatch, change):
    boundary, adapter, task, approval = await prepare(client, async_db, monkeypatch)
    await review(client, boundary, task)
    deny = callback(boundary, 1, update_id=2, query_id="deny-a")
    if change in {"actor", "chat"}:
        deny = callback(boundary, 1, update_id=2, query_id="deny-a",
            actor=43 if change == "actor" else 42, chat=78 if change == "chat" else 77)
    async with async_db() as db:
        if change == "task":
            row = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task["task_id"]))
            row.task_revision += 1; db.add(row)
        elif change == "goal":
            from src.db.models import Goal
            row = await db.get(Goal, task["goal_id"]); row.revision += 1; db.add(row)
        elif change == "details":
            row = await db.get(ApprovalRequest, approval.id); row.details_json += " "; db.add(row)
        elif change == "pairing":
            row = await db.get(TelegramTransportState, "telegram"); row.pairing_state = "revoked"; db.add(row)
        elif change == "expiry":
            row = await db.scalar(select(TelegramTaskCallback).where(TelegramTaskCallback.effect == "deny"))
            row.expires_at = now()-timedelta(seconds=1); db.add(row)
        elif change == "out_of_order":
            deny["update_id"] = 1
    refused = await client.post("/api/telegram/updates", json=deny)
    assert refused.status_code != 200, refused.text
    assert (await approval_repository.get(approval.id)).status == "pending"
    async with async_db() as db:
        row = await db.scalar(select(TelegramTaskCallback).where(TelegramTaskCallback.effect == "deny"))
        assert row.status == "pending"
    await boundary.http.aclose()


async def test_unknown_send_blocks_callback_and_fresh_notice_retires_nonce(client, async_db, setup_workspace, monkeypatch):
    boundary, adapter, task, approval = await prepare(client, async_db, monkeypatch)
    boundary.lose_send = True
    notice, delivery = await send_notice(client, task)
    assert delivery["status"] == "unknown"
    refused = await client.post("/api/telegram/updates", json=callback(boundary, 0))
    assert refused.status_code != 200
    assert refused.json()["detail"]["code"] == "telegram_delivery_unverified"
    repeat = await client.post(f"/api/telegram/outbox/{notice['id']}/deliver")
    assert repeat.json()["status"] == "unknown" and len(boundary.messages) == 1
    boundary.lose_send = False
    await send_notice(client, task, key="fresh-notice")
    refused = await client.post("/api/telegram/updates", json=callback(boundary, 0))
    assert refused.status_code != 200
    assert (await approval_repository.get(approval.id)).status == "pending"
    await boundary.http.aclose()


async def test_deny_and_nonce_roll_back_together_and_ack_loss_never_replays(client, async_db, setup_workspace, monkeypatch):
    boundary, adapter, task, approval = await prepare(client, async_db, monkeypatch)
    await review(client, boundary, task)
    deny = callback(boundary, 1, update_id=2, query_id="deny-a")
    from src.extensions.telegram_task_controls import TelegramTaskControls
    original = TelegramTaskControls._reply
    async def crash(*args, **kwargs):
        raise RuntimeError("synthetic process interruption before commit")
    with patch.object(TelegramTaskControls, "_reply", crash):
        with pytest.raises(RuntimeError):
            await adapter.ingest_update(deny, owner_principal_id=task["owner_principal_id"], operator_session_id=task["owner_session_id"])
    assert (await approval_repository.get(approval.id)).status == "pending"
    async with async_db() as db:
        row = await db.scalar(select(TelegramTaskCallback).where(TelegramTaskCallback.effect == "deny"))
        assert row.status == "pending"
    boundary.lose_ack = True
    response = await client.post("/api/telegram/updates", json=deny)
    assert response.status_code == 200 and response.json()["ack_status"] == "unknown"
    replay = await client.post("/api/telegram/updates", json=deny)
    assert replay.status_code == 200 and replay.json()["outbox_id"] == response.json()["outbox_id"]
    assert (await approval_repository.get(approval.id)).status == "denied"
    await boundary.http.aclose()


@pytest.mark.parametrize("change", ["authenticated_owner", "root", "large_payload", "rate", "concurrent_nonce"])
async def test_current_root_owner_and_finite_callback_admission(client, async_db, setup_workspace, monkeypatch, change):
    boundary, adapter, task, approval = await prepare(client, async_db, monkeypatch)
    await review(client, boundary, task)
    deny = callback(boundary, 1, update_id=2, query_id="deny-a")
    if change == "authenticated_owner":
        login = await client.post("/api/auth/login", json={"password": "first-result-test-secret", "start_new_scope": True})
        assert login.status_code == 200, login.text
    elif change == "root":
        replacement = setup_workspace / "replacement-root"
        replacement.mkdir()
        monkeypatch.setattr(settings, "workspace_dir", str(replacement))
    elif change == "large_payload":
        deny["ignored_payload"] = "x" * 8192
    elif change == "rate":
        # Drive actual successful Review callbacks; no rate receipt is seeded.
        for index in range(19):
            await send_notice(client, task, key=f"rate-{index}")
            response = await client.post("/api/telegram/updates", json=callback(boundary,
                len(boundary.messages)-1, update_id=index+2, query_id=f"review-{index}"))
            assert response.status_code == 200, response.text
        await send_notice(client, task, key="rate-rejected")
        deny = callback(boundary, len(boundary.messages)-1, update_id=30, query_id="rate-rejected")
    elif change == "concurrent_nonce":
        sibling = {**deny, "callback_query": {**deny["callback_query"], "id": "competing-query"}}
        responses = await asyncio.gather(client.post("/api/telegram/updates", json=deny),
            client.post("/api/telegram/updates", json=sibling))
        assert sum(response.status_code == 200 for response in responses) == 1
        assert (await approval_repository.get(approval.id)).status == "denied"
        async with async_db() as db:
            rows = (await db.execute(select(TelegramTaskCallback).where(TelegramTaskCallback.effect == "deny"))).scalars().all()
            assert sum(row.status == "consumed" for row in rows) == 1
        await boundary.http.aclose()
        return
    refused = await client.post("/api/telegram/updates", json=deny)
    assert refused.status_code != 200, refused.text
    assert (await approval_repository.get(approval.id)).status == "pending"
    await boundary.http.aclose()


@pytest.mark.parametrize("mode", ["cancel", "unknown", "fence_changed"])
async def test_native_original_cancel_intent_and_restart_readback(client, async_db, setup_workspace, monkeypatch, mode):
    """Real task admission/child lease, cancellation/cleanup/tree and persisted readback.

    Pause only the native workflow invocation before local effects so the
    operator can inspect its admitted original attempt. No job/task/effect
    receipt is seeded. Unknown mode loses the cleanup response after the real
    durable job tree cancellation, preserving uncertainty rather than replay.
    """
    boundary, adapter, _, _ = await prepare(client, async_db, monkeypatch)
    goal, task = await create_snapshot(client, journey=f"telegram-cancel-{mode.replace('_', '-')}")
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.guardian.goal_snapshot_to_file import GoalSnapshotToFileAdapter, GoalSnapshotToFileService
    from src.workflows.job_runtime import durable_job_repository
    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    monkeypatch.setattr("src.api.work_board.dispatcher", dispatcher)
    invoked = asyncio.Event()
    async def pause_native_invocation(self, *args, **kwargs):
        invoked.set()
        await asyncio.Event().wait()
    if mode == "cancel":
        # Parent admitted/leased, child dispatch not entered: cleanup can prove
        # cancellation with no child external-effect uncertainty.
        monkeypatch.setattr(GoalSnapshotToFileService, "run", pause_native_invocation)
    else:
        monkeypatch.setattr(GoalSnapshotToFileAdapter, "_invoke_workflow", pause_native_invocation)
    worker = asyncio.create_task(dispatcher.run_pass())
    try:
        await asyncio.wait_for(invoked.wait(), timeout=15)
        detail = (await client.get(f"/api/work-board/tasks/{task['task_id']}")).json()
        task = detail["task"]
        assert task["status"] == "running", detail
        attempt_id = detail["attempts"][0]["attempt_id"]
        job_id = detail["attempts"][0]["workflow_run_id"]
        async with async_db() as db:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            children = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == job_id))).scalars().all()
            assert run.status == "running" and run.lease_owner and run.fencing_token > 0
            if mode == "cancel":
                assert children == []
            else:
                assert children and any(child.status == "running" for child in children)
        await send_notice(client, task)
        # prepare() has not delivered anything; this is the first notice.
        review_response = await client.post("/api/telegram/updates", json=callback(boundary, 0))
        assert review_response.status_code == 200, review_response.text
        delivered = await client.post(f"/api/telegram/outbox/{review_response.json()['outbox_id']}/deliver")
        assert delivered.status_code == 200, delivered.text
        cancel_callback = callback(boundary, 1, update_id=2, query_id="native-cancel")
        cleanup_calls = []
        original_cancel_tree = durable_job_repository.cancel_job_tree
        async def cancel_tree(*args, **kwargs):
            cleanup_calls.append(args[0])
            receipt = await original_cancel_tree(*args, **kwargs)
            if mode == "unknown":
                raise TimeoutError("synthetic cleanup response loss after actual cancellation")
            return receipt
        monkeypatch.setattr(durable_job_repository, "cancel_job_tree", cancel_tree)
        if mode == "fence_changed":
            async with async_db() as db:
                attempt = await db.get(WorkBoardAttempt, attempt_id)
                attempt.fencing_token += 1
                db.add(attempt)
        response = await client.post("/api/telegram/updates", json=cancel_callback)
        if mode == "fence_changed":
            assert response.status_code != 200, response.text
            async with async_db() as db:
                attempt = await db.get(WorkBoardAttempt, attempt_id)
                callback_row = await db.scalar(select(TelegramTaskCallback).where(TelegramTaskCallback.effect == "cancel"))
                assert attempt.cancel_requested_at is None and callback_row.status == "pending"
            assert cleanup_calls == []
            return
        assert response.status_code == 200, response.text
        assert response.json()["status"] == ("unknown" if mode == "unknown" else "cancelled")
        assert response.json()["memory_status"] == "no_learning"
        async with async_db() as db:
            events = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task["task_id"],
                WorkBoardEvent.kind == "attempt.cancel_requested"))).scalars().all()
            assert len(events) == 1
            callback_row = await db.scalar(select(TelegramTaskCallback).where(TelegramTaskCallback.effect == "cancel"))
            assert json.loads(events[0].metadata_json)["request_identity"] == callback_row.id
            assert callback_row.status == ("cancel_intent" if mode == "unknown" else "consumed")
        calls_before_restart = list(cleanup_calls)
        monkeypatch.setattr("src.api.telegram.default_telegram_transport", TelegramTransportAdapter(transport=boundary))
        client._transport = httpx.ASGITransport(app=create_app())
        replay = await client.post("/api/telegram/updates", json=cancel_callback)
        assert replay.status_code == 200, replay.text
        assert replay.json()["status"] == response.json()["status"]
        assert cleanup_calls == calls_before_restart
        readback = (await client.get(f"/api/work-board/tasks/{task['task_id']}")).json()
        assert readback["task"]["block_kind"] == ("unknown_effect" if mode == "unknown" else "cancelled")
        assert readback["attempts"][0]["attempt_id"] == attempt_id
        assert not (setup_workspace / f"artifacts/first-result/telegram-cancel-{mode}.md").exists()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await boundary.http.aclose()
