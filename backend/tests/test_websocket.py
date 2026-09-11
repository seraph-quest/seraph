import json
import os
import shutil
import tempfile
import asyncio
from contextlib import asynccontextmanager, ExitStack
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from starlette.testclient import TestClient

# Ensure models are registered in SQLModel.metadata before create_all
import src.db.models  # noqa: F401
from config.settings import settings
from src.agent.direct_chat import should_use_direct_local_chat as real_should_use_direct_local_chat
from src.api.ws import _build_agent
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.utils.background import drain_tracked_tasks


def _make_sync_client_with_db():
    """Create a sync TestClient with an isolated DB patched in.

    Patches init_db (so the lifespan creates tables on the test engine),
    close_db (so the lifespan drains tasks and disposes the test engine),
    and get_session everywhere (so queries use the test DB).

    Returns (client, cleanup_list). The TestClient is already entered as a
    context manager so the lifespan has run. Call p.stop() on each item in
    cleanup_list when done.
    """
    tmpdir = tempfile.mkdtemp(prefix="seraph-ws-test-")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{os.path.join(tmpdir, 'seraph-test.db')}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _test_init_db():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def _test_close_db():
        try:
            await drain_tracked_tasks(timeout_seconds=5.0)
        finally:
            await engine.dispose()

    targets = [
        "src.db.engine.get_session",
        "src.agent.session.get_session",
        "src.approval.repository.get_session",
        "src.audit.repository.get_session",
        "src.goals.repository.get_session",
        "src.profile.service.get_db",
        "src.vault.repository.get_session",
        "src.api.profile.get_db",
    ]
    patches = [patch(t, _get_session) for t in targets]
    patches.append(patch("src.app.init_db", _test_init_db))
    patches.append(patch("src.app.close_db", _test_close_db))
    patches.append(patch("src.app.init_scheduler", return_value=None))
    patches.append(patch("src.app.shutdown_scheduler"))
    patches.append(patch("src.memory.flush.flush_session_memory", AsyncMock(return_value=None)))
    patches.append(
        patch(
            "src.api.chat.get_current_trust_principal",
            return_value=TrustPrincipal(
                principal_id="operator:test",
                principal_type=PrincipalType.OPERATOR,
                grants=(AuthorityGrant.MODEL_INFERENCE,),
            ),
        )
    )
    patches.append(patch("src.api.ws.should_use_direct_local_chat", return_value=False))
    for p in patches:
        p.start()

    from src.app import create_app
    app = create_app()

    # Enter TestClient as context manager so the lifespan runs (init_db, etc.)
    stack = ExitStack()
    stack.callback(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    client = stack.enter_context(TestClient(app))

    # Return stack in patches list so cleanup exits the context manager too
    return client, patches, stack


def _close_sync_client_with_db(patches, stack):
    try:
        stack.close()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(drain_tracked_tasks(timeout_seconds=5.0))
    finally:
        for item in reversed(patches):
            with suppress(Exception):
                item.stop()


class TestWebSocket:
    def test_websocket_rejects_effective_legacy_runtime_override(self):
        client, patches, stack = _make_sync_client_with_db()

        async def _fake_stream(*args, **kwargs):
            yield "OpenRouter ready."

        try:
            with (
                patch.object(
                    settings,
                    "runtime_model_overrides",
                    "chat_agent=codex-local,onboarding_agent=codex-local",
                ),
                patch("litellm.completion") as completion,
                patch("src.api.ws.should_use_direct_local_chat", return_value=True),
                patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)),
                patch("src.api.ws.stream_direct_local_chat", _fake_stream),
                client.websocket_connect("/ws/chat") as ws,
            ):
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "message", "message": "Hello"}))
                received = [json.loads(ws.receive_text()) for _ in range(4)]

            assert received[0]["type"] == "status"
            assert received[-1]["type"] == "final"
            assert received[-1]["content"] == "OpenRouter ready."
            completion.assert_not_called()
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_ping(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            with client.websocket_connect("/ws/chat") as ws:
                # The server sends a proactive welcome on connect; drain it
                welcome = json.loads(ws.receive_text())
                assert welcome["type"] == "proactive"
                assert "Seraph online" in welcome["content"]
                assert "limited setup" in welcome["content"].lower()
                ws.send_text(json.dumps({"type": "ping"}))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "pong"
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_invalid_json(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            with client.websocket_connect("/ws/chat") as ws:
                # Drain the welcome message
                _ = ws.receive_text()

                ws.send_text("not json")
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "error"
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_skip_onboarding(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            with client.websocket_connect("/ws/chat") as ws:
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "final"
                assert "skipped" in resp["content"].lower()
                assert "full workspace" in resp["content"].lower()
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_message_emits_status_before_final(self):
        client, patches, stack = _make_sync_client_with_db()

        async def _fake_stream(*args, **kwargs):
            yield "Re"
            yield "ady."

        try:
            with (
                patch("src.api.ws.should_use_direct_local_chat", return_value=True),
                patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)),
                patch("src.api.ws.stream_direct_local_chat", _fake_stream),
                patch("src.api.ws.run_direct_local_chat", new=AsyncMock(return_value="Unused.")),
                client.websocket_connect("/ws/chat") as ws,
            ):
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "message", "message": "Hello"}))

                received = [json.loads(ws.receive_text()) for _ in range(5)]

            assert received[0]["type"] == "status"
            assert received[0]["content"] == "Seraph received the message."
            assert received[1]["type"] == "status"
            assert "governed OpenRouter chat runtime" in received[1]["content"]
            assert received[2]["type"] == "delta"
            assert received[2]["content"] == "Re"
            assert received[3]["type"] == "delta"
            assert received[3]["content"] == "ady."
            assert received[4]["type"] == "final"
            assert received[4]["content"] == "Ready."

            messages_response = client.get(f"/api/sessions/{received[4]['session_id']}/messages")
            assert messages_response.status_code == 200
            messages = messages_response.json()
            assistant_messages = [message for message in messages if message["role"] == "assistant"]
            assert [message["content"] for message in assistant_messages] == ["Ready."]
            assert all("Response interrupted" not in message["content"] for message in messages)
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_ingress_rejects_duplicate_before_stream_dispatch(self):
        client, patches, stack = _make_sync_client_with_db()
        stream_calls = []

        async def _fake_stream(*args, **kwargs):
            stream_calls.append(args[0])
            yield "Ready."

        try:
            with (
                patch("src.api.ws.should_use_direct_local_chat", return_value=True),
                patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)),
                patch("src.api.ws.stream_direct_local_chat", _fake_stream),
                patch("src.api.ws.run_direct_local_chat", new=AsyncMock(return_value="Unused.")),
                client.websocket_connect("/ws/chat") as ws,
            ):
                _ = ws.receive_text()
                first_payload = {
                    "type": "message",
                    "message": "Retry-safe websocket message",
                    "idempotency_key": "ws-retry-1",
                }
                ws.send_text(json.dumps(first_payload))
                first_responses = [json.loads(ws.receive_text()) for _ in range(4)]
                session_id = first_responses[-1]["session_id"]

                ws.send_text(
                    json.dumps(
                        {
                            **first_payload,
                            "session_id": session_id,
                        }
                    )
                )
                duplicate = json.loads(ws.receive_text())

            assert first_responses[-1]["type"] == "final"
            assert duplicate["type"] == "error"
            assert duplicate["reason"] == "chat_message_duplicate"
            assert duplicate["session_id"] == duplicate["conversation_id"] == duplicate["thread_id"] == session_id
            assert duplicate["message_id"] == first_responses[-1]["causation_id"]
            assert duplicate["owner_principal_id"]
            assert duplicate["transport"] == "websocket"
            assert duplicate["degraded_state"] == "duplicate_rejected"
            assert stream_calls == ["Retry-safe websocket message"]
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_ingress_rejects_ambiguous_dual_identity_before_effects(self):
        client, patches, stack = _make_sync_client_with_db()
        stream_calls = []

        async def _fake_stream(*args, **kwargs):
            stream_calls.append(args[0])
            yield "Unexpected."

        try:
            with (
                patch("src.api.ws.log_chat_ingress_event", new=AsyncMock()) as mock_log,
                patch("src.api.ws.stream_direct_local_chat", _fake_stream),
                client.websocket_connect("/ws/chat") as ws,
            ):
                _ = ws.receive_text()
                ws.send_text(
                    json.dumps(
                        {
                            "type": "message",
                            "message": "Do not reserve this",
                            "message_id": "client-message-1",
                            "idempotency_key": "ws-retry-1",
                        }
                    )
                )
                blocked = json.loads(ws.receive_text())

            assert blocked["type"] == "error"
            assert blocked["reason"] == "chat_message_identity_conflict"
            assert stream_calls == []
            mock_log.assert_not_awaited()
        finally:
            stack.close()
            for p in patches:
                p.stop()

    @pytest.mark.parametrize("message", ["", " \t "])
    def test_websocket_ingress_rejects_blank_message_before_effects(self, message):
        client, patches, stack = _make_sync_client_with_db()
        stream_calls = []

        async def _fake_stream(*args, **kwargs):
            stream_calls.append(args[0])
            yield "Unexpected."

        try:
            with (
                patch("src.api.ws.log_chat_ingress_event", new=AsyncMock()) as mock_log,
                patch("src.api.ws.stream_direct_local_chat", _fake_stream),
                client.websocket_connect("/ws/chat") as ws,
            ):
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "message", "message": message}))
                blocked = json.loads(ws.receive_text())

            assert blocked["type"] == "error"
            assert blocked["reason"] == "chat_message_invalid"
            assert stream_calls == []
            mock_log.assert_not_awaited()
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_comma_greeting_uses_real_direct_chat_classifier(self):
        client, patches, stack = _make_sync_client_with_db()

        async def _fake_stream(*args, **kwargs):
            yield "Hello."

        try:
            with (
                patch("src.api.ws.should_use_direct_local_chat", side_effect=real_should_use_direct_local_chat),
                patch("src.agent.direct_chat._uses_local_gemma_profile", return_value=True),
                patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)),
                patch("src.api.ws.stream_direct_local_chat", _fake_stream),
                patch("src.api.ws.run_direct_local_chat", new=AsyncMock(return_value="Unused.")),
                client.websocket_connect("/ws/chat") as ws,
            ):
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                _ = ws.receive_text()

                ws.send_text(json.dumps({"type": "message", "message": "Hello, reply in one short sentence."}))
                received = [json.loads(ws.receive_text()) for _ in range(4)]

            assert received[0]["type"] == "status"
            assert received[0]["content"] == "Seraph received the message."
            assert received[1]["type"] == "status"
            assert "governed OpenRouter chat runtime" in received[1]["content"]
            assert received[2]["type"] == "delta"
            assert received[2]["content"] == "Hello."
            assert received[3]["type"] == "final"
            assert received[3]["content"] == "Hello."
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_direct_chat_does_not_replay_after_streaming_failure(self):
        client, patches, stack = _make_sync_client_with_db()

        async def _fake_stream(*args, **kwargs):
            raise RuntimeError("streaming endpoint failed")
            yield "unreachable"

        try:
            with (
                patch("src.api.ws.should_use_direct_local_chat", return_value=True),
                patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)),
                patch("src.api.ws.stream_direct_local_chat", _fake_stream),
                patch("src.api.ws.run_direct_local_chat", new=AsyncMock(return_value="Fallback ready.")) as mock_direct,
                client.websocket_connect("/ws/chat") as ws,
            ):
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "message", "message": "Hello"}))

                received = [json.loads(ws.receive_text()) for _ in range(3)]

            assert received[0]["type"] == "status"
            assert received[0]["content"] == "Seraph received the message."
            assert received[1]["type"] == "status"
            assert "governed OpenRouter chat runtime" in received[1]["content"]
            assert received[2]["type"] == "error"
            assert "outcome is uncertain" in received[2]["content"]
            assert "did not retry automatically" in received[2]["content"]
            mock_direct.assert_not_awaited()
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_direct_chat_preflight_failure_emits_operator_error(self):
        client, patches, stack = _make_sync_client_with_db()

        async def _fake_stream(*args, **kwargs):
            yield "unreachable"

        route_error = (
            "Local chat runtime is unreachable from the Seraph backend at http://192.168.1.26:8001. "
            "Health endpoint http://192.168.1.26:8001/health/chat reported chat proxy connect_error."
        )
        stream_mock = MagicMock(side_effect=_fake_stream)

        try:
            with (
                patch("src.api.ws.should_use_direct_local_chat", return_value=True),
                patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=route_error)),
                patch("src.api.ws.stream_direct_local_chat", stream_mock),
                patch("src.api.ws.run_direct_local_chat", new=AsyncMock(return_value="Unused.")) as mock_direct,
                client.websocket_connect("/ws/chat") as ws,
            ):
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "message", "message": "Hello"}))

                received = [json.loads(ws.receive_text()) for _ in range(2)]

            assert received[0]["type"] == "status"
            assert received[0]["content"] == "Seraph received the message."
            assert received[1]["type"] == "error"
            assert "Local chat runtime is unreachable" in received[1]["content"]
            assert "health/chat" in received[1]["content"]
            assert "LiteLLM" not in received[1]["content"]
            assert all("local chat runtime" not in msg["content"] for msg in received if msg["type"] == "status")
            stream_mock.assert_not_called()
            mock_direct.assert_not_awaited()
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_direct_chat_error_close_does_not_record_interrupted_turn(self):
        client, patches, stack = _make_sync_client_with_db()

        async def _fake_stream(*args, **kwargs):
            raise RuntimeError("streaming endpoint failed")
            yield "unreachable"

        try:
            with (
                patch("src.api.ws.should_use_direct_local_chat", return_value=True),
                patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)),
                patch("src.api.ws.stream_direct_local_chat", _fake_stream),
                patch("src.api.ws.run_direct_local_chat", new=AsyncMock(side_effect=RuntimeError("chat failed"))) as mock_direct,
                client.websocket_connect("/ws/chat") as ws,
            ):
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "message", "message": "Hello"}))

                received = []
                for _ in range(5):
                    msg = json.loads(ws.receive_text())
                    received.append(msg)
                    if msg["type"] == "error":
                        ws.close()
                        break

            error = next(msg for msg in received if msg["type"] == "error")
            assert "outcome is uncertain" in error["content"]
            mock_direct.assert_not_awaited()
            messages_response = client.get(f"/api/sessions/{error['session_id']}/messages")
            assert messages_response.status_code == 200
            messages = messages_response.json()
            assert all("Response interrupted" not in message["content"] for message in messages)
        finally:
            stack.close()
            for p in patches:
                p.stop()


@pytest.mark.asyncio
@patch("src.api.ws.create_onboarding_agent")
@patch("src.api.ws.get_or_create_profile")
async def test_build_agent_passes_message_to_onboarding_agent(mock_profile, mock_create_onboarding_agent):
    mock_profile.return_value = SimpleNamespace(onboarding_completed=False)
    mock_agent = MagicMock()
    mock_create_onboarding_agent.return_value = mock_agent

    agent, is_onboarding, specialist_names = await _build_agent(
        "session-1",
        "Please review https://example.com/about during onboarding.",
    )

    assert agent is mock_agent
    assert is_onboarding is True
    assert specialist_names == set()
    mock_create_onboarding_agent.assert_called_once_with(
        "Please review https://example.com/about during onboarding."
    )
