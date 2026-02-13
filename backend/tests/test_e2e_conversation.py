"""E2E conversation flow: message → agent → step output → tool detection → final answer.

Uses a mocked agent to verify the full WS pipeline without hitting a real LLM.
"""

import json
from unittest.mock import MagicMock, patch

from smolagents import ToolCall, ActionStep, FinalAnswerStep
from smolagents.monitoring import Timing
from starlette.testclient import TestClient

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
