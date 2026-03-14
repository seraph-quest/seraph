"""E2E conversation flow: message → agent → step output → tool detection → final answer.

Uses a mocked agent to verify the full WS pipeline without hitting a real LLM.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from smolagents import ToolCall, ActionStep, FinalAnswerStep
from smolagents.monitoring import Timing
from starlette.testclient import TestClient

from src.approval.exceptions import ApprovalRequired
from src.audit.repository import audit_repository
from src.vault.repository import vault_repository
from tests.test_websocket import _make_sync_client_with_db

_TIMING = Timing(start_time=0.0, end_time=1.0)


def _make_agent_steps():
    """Return a sequence of smolagents step objects simulating a real agent run."""
    return [
        ToolCall(name="web_search", arguments={"query": "weather today"}, id="tc1"),
        ActionStep(
            step_number=1,
            timing=_TIMING,
            observations="Search returned 3 results about today's weather.",
            is_final_answer=False,
        ),
        FinalAnswerStep(output="It's sunny and 72°F today!"),
    ]


class TestE2EConversation:
    def test_full_message_flow(self):
        """Verify: user message → step events → final answer with correct types and session ID."""
        client, patches, stack = _make_sync_client_with_db()
        try:
            mock_agent = MagicMock()
            mock_agent.run.return_value = iter(_make_agent_steps())

            with patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())), \
                 patch("src.memory.consolidator.consolidate_session"):
                with client.websocket_connect("/ws/chat") as ws:
                    # Drain welcome message
                    _ = ws.receive_text()

                    # Skip onboarding first so we get the full agent
                    ws.send_text(json.dumps({"type": "skip_onboarding"}))
                    _ = ws.receive_text()  # drain skip response

                    # Send a real message
                    ws.send_text(json.dumps({
                        "type": "message",
                        "message": "What's the weather?",
                        "session_id": None,
                    }))

                    messages = []
                    for _ in range(10):  # safety limit
                        raw = ws.receive_text()
                        msg = json.loads(raw)
                        messages.append(msg)
                        if msg["type"] == "final":
                            break

                    # Should have at least one step and one final
                    types = [m["type"] for m in messages]
                    assert "step" in types, f"Expected 'step' in types, got {types}"
                    assert "final" in types, f"Expected 'final' in types, got {types}"

                    # Final message should have content
                    final = next(m for m in messages if m["type"] == "final")
                    assert final["content"]

                    # All non-pong messages should have session_id
                    for m in messages:
                        if m["type"] not in ("pong",):
                            assert m.get("session_id"), f"Missing session_id in {m['type']} message"

                    # All messages should have seq numbers
                    for m in messages:
                        if m["type"] not in ("pong",):
                            assert m.get("seq") is not None, f"Missing seq in {m['type']} message"
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_seq_numbers_monotonically_increase(self):
        """Verify sequence numbers increase across all messages."""
        client, patches, stack = _make_sync_client_with_db()
        try:
            mock_agent = MagicMock()
            mock_agent.run.return_value = iter(_make_agent_steps())

            with patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())), \
                 patch("src.memory.consolidator.consolidate_session"):
                with client.websocket_connect("/ws/chat") as ws:
                    _ = ws.receive_text()  # welcome
                    ws.send_text(json.dumps({"type": "skip_onboarding"}))
                    _ = ws.receive_text()

                    ws.send_text(json.dumps({
                        "type": "message",
                        "message": "test",
                        "session_id": None,
                    }))

                    seqs = []
                    for _ in range(10):
                        raw = ws.receive_text()
                        msg = json.loads(raw)
                        if msg.get("seq") is not None:
                            seqs.append(msg["seq"])
                        if msg["type"] == "final":
                            break

                    assert len(seqs) >= 2
                    for i in range(1, len(seqs)):
                        assert seqs[i] > seqs[i - 1], f"seq not monotonic: {seqs}"
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_tool_name_in_step_content(self):
        """Verify tool calls appear in step content for frontend detection."""
        client, patches, stack = _make_sync_client_with_db()
        try:
            mock_agent = MagicMock()
            mock_agent.run.return_value = iter(_make_agent_steps())

            with patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())), \
                 patch("src.memory.consolidator.consolidate_session"):
                with client.websocket_connect("/ws/chat") as ws:
                    _ = ws.receive_text()
                    ws.send_text(json.dumps({"type": "skip_onboarding"}))
                    _ = ws.receive_text()

                    ws.send_text(json.dumps({
                        "type": "message",
                        "message": "search for weather",
                        "session_id": None,
                    }))

                    steps = []
                    for _ in range(10):
                        raw = ws.receive_text()
                        msg = json.loads(raw)
                        if msg["type"] == "step":
                            steps.append(msg)
                        if msg["type"] == "final":
                            break

                    # First step should mention web_search tool
                    assert any("web_search" in s["content"] for s in steps), \
                        f"No step mentions web_search: {[s['content'] for s in steps]}"
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_tool_calls_are_written_to_audit_log(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            mock_agent = MagicMock()
            mock_agent.run.return_value = iter(_make_agent_steps())

            with patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())), \
                 patch("src.memory.consolidator.consolidate_session"):
                with client.websocket_connect("/ws/chat") as ws:
                    _ = ws.receive_text()
                    ws.send_text(json.dumps({"type": "skip_onboarding"}))
                    _ = ws.receive_text()

                    ws.send_text(json.dumps({
                        "type": "message",
                        "message": "search for weather",
                        "session_id": None,
                    }))

                    for _ in range(10):
                        raw = ws.receive_text()
                        msg = json.loads(raw)
                        if msg["type"] == "final":
                            break

                events = client.get("/api/audit/events").json()
                assert any(
                    event["event_type"] == "tool_call" and event["tool_name"] == "web_search"
                    for event in events
                )
                assert any(
                    event["event_type"] == "tool_result" and event["tool_name"] == "web_search"
                    for event in events
                )
                assert any(
                    event["event_type"] == "agent_run_succeeded"
                    and event["tool_name"] == "chat_agent"
                    and event["details"]["transport"] == "websocket"
                    for event in events
                )
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_high_risk_tool_sends_approval_required_message(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            mock_agent = MagicMock()
            mock_agent.run.side_effect = ApprovalRequired(
                approval_id="approval-1",
                session_id="s1",
                tool_name="shell_execute",
                risk_level="high",
                summary="Calling tool: shell_execute({\"code\": \"[redacted]\"})",
            )

            with patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())), \
                 patch("src.memory.consolidator.consolidate_session"):
                with client.websocket_connect("/ws/chat") as ws:
                    _ = ws.receive_text()
                    ws.send_text(json.dumps({"type": "skip_onboarding"}))
                    _ = ws.receive_text()

                    ws.send_text(json.dumps({
                        "type": "message",
                        "message": "run this snippet",
                        "session_id": None,
                    }))

                    for _ in range(10):
                        raw = ws.receive_text()
                        msg = json.loads(raw)
                        if msg["type"] == "approval_required":
                            assert msg["approval_id"] == "approval-1"
                            assert msg["tool_name"] == "shell_execute"
                            assert msg["risk_level"] == "high"
                            assert "continue automatically" in msg["content"]
                            break
                    else:
                        raise AssertionError("Expected approval_required message")
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_timeout_logs_only_timed_out_runtime_event(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            mock_agent = MagicMock()
            mock_agent.run.return_value = iter(_make_agent_steps())

            with (
                patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())),
                patch(
                    "src.api.ws.asyncio.wait_for",
                    new=AsyncMock(side_effect=asyncio.TimeoutError),
                ),
                patch("src.memory.consolidator.consolidate_session"),
            ):
                with client.websocket_connect("/ws/chat") as ws:
                    _ = ws.receive_text()
                    ws.send_text(json.dumps({"type": "skip_onboarding"}))
                    _ = ws.receive_text()

                    ws.send_text(json.dumps({
                        "type": "message",
                        "message": "search for weather",
                        "session_id": None,
                    }))

                    for _ in range(10):
                        raw = ws.receive_text()
                        msg = json.loads(raw)
                        if msg["type"] == "final":
                            assert "taking too long" in msg["content"]
                            break
                    else:
                        raise AssertionError("Expected timeout final message")

                events = client.get("/api/audit/events").json()
                assert any(
                    event["event_type"] == "agent_run_timed_out"
                    and event["tool_name"] == "chat_agent"
                    and event["details"]["transport"] == "websocket"
                    for event in events
                )
                assert not any(
                    event["event_type"] == "agent_run_succeeded"
                    and event["tool_name"] == "chat_agent"
                    and event["details"]["transport"] == "websocket"
                    for event in events
                )
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_secret_values_are_redacted_in_streamed_messages(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            import asyncio

            asyncio.run(vault_repository.store("service_token", "super-secret-token"))

            mock_agent = MagicMock()
            mock_agent.run.return_value = iter([
                ToolCall(name="get_secret", arguments={"key": "service_token"}, id="tc1"),
                ActionStep(
                    step_number=1,
                    timing=_TIMING,
                    observations="Retrieved secret: super-secret-token",
                    is_final_answer=False,
                ),
                FinalAnswerStep(output="Using secret super-secret-token now."),
            ])

            with patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())), \
                 patch("src.memory.consolidator.consolidate_session"):
                with client.websocket_connect("/ws/chat") as ws:
                    _ = ws.receive_text()
                    ws.send_text(json.dumps({"type": "skip_onboarding"}))
                    _ = ws.receive_text()

                    ws.send_text(json.dumps({
                        "type": "message",
                        "message": "fetch the token",
                        "session_id": None,
                    }))

                    received = []
                    for _ in range(10):
                        raw = ws.receive_text()
                        msg = json.loads(raw)
                        received.append(msg)
                        if msg["type"] == "final":
                            break

                    contents = [msg["content"] for msg in received if "content" in msg]
                    assert any("[redacted secret]" in content for content in contents)
                    assert all("super-secret-token" not in content for content in contents)
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_resume_message_does_not_duplicate_user_turn(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            mock_agent = MagicMock()
            mock_agent.run.return_value = iter([
                FinalAnswerStep(output="Resumed successfully."),
            ])

            with patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())), \
                 patch("src.memory.consolidator.consolidate_session"):
                with client.websocket_connect("/ws/chat") as ws:
                    _ = ws.receive_text()
                    ws.send_text(json.dumps({"type": "skip_onboarding"}))
                    _ = ws.receive_text()

                    ws.send_text(json.dumps({
                        "type": "message",
                        "message": "run this snippet",
                        "session_id": "s-resume",
                    }))

                    for _ in range(10):
                        msg = json.loads(ws.receive_text())
                        if msg["type"] == "final":
                            break

                    ws.send_text(json.dumps({
                        "type": "resume_message",
                        "message": "run this snippet",
                        "session_id": "s-resume",
                    }))

                    for _ in range(10):
                        msg = json.loads(ws.receive_text())
                        if msg["type"] == "final":
                            break

                messages = client.get("/api/sessions/s-resume/messages").json()
                user_messages = [m for m in messages if m["role"] == "user"]
                assert len(user_messages) == 1
                assert user_messages[0]["content"] == "run this snippet"
        finally:
            stack.close()
            for p in patches:
                p.stop()
