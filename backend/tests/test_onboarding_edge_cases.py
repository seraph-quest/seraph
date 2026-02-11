"""Onboarding edge cases — rapid messages, skip mid-flow, double skip."""

import json
from unittest.mock import MagicMock, patch

import pytest
from smolagents import FinalAnswerStep

from tests.test_websocket import _make_sync_client_with_db


def _mock_onboarding_agent():
    agent = MagicMock()
    agent.run.return_value = iter([
        FinalAnswerStep(output="Welcome! Tell me about yourself."),
    ])
    return agent


class TestOnboardingEdgeCases:
    def test_skip_mid_flow(self):
        """User sends a message, then skips before agent finishes onboarding."""
        client, patches, stack = _make_sync_client_with_db()
        try:
            with client.websocket_connect("/ws/chat") as ws:
                welcome = json.loads(ws.receive_text())
                assert welcome["type"] == "proactive"

                # Skip onboarding
                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "final"
                assert "skipped" in resp["content"].lower()

                # Subsequent messages should use the full agent (not onboarding)
                full_agent = MagicMock()
                full_agent.run.return_value = iter([
                    FinalAnswerStep(output="Full agent response"),
                ])
                with patch("src.api.ws._build_agent", return_value=(full_agent, False)), \
                     patch("src.memory.consolidator.consolidate_session"):
                    ws.send_text(json.dumps({
                        "type": "message",
                        "message": "Hello after skip",
                        "session_id": None,
                    }))
                    messages = []
                    for _ in range(10):
                        raw = ws.receive_text()
                        msg = json.loads(raw)
                        messages.append(msg)
                        if msg["type"] == "final":
                            break
                    final = next(m for m in messages if m["type"] == "final")
                    assert "Full agent response" in final["content"]
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_double_skip_is_idempotent(self):
        """Sending skip_onboarding twice should not error."""
        client, patches, stack = _make_sync_client_with_db()
        try:
            with client.websocket_connect("/ws/chat") as ws:
                _ = ws.receive_text()  # welcome

                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                resp1 = json.loads(ws.receive_text())
                assert resp1["type"] == "final"

                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                resp2 = json.loads(ws.receive_text())
                assert resp2["type"] == "final"
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_sequential_messages_during_onboarding(self):
        """Multiple messages during onboarding should all get responses."""
        client, patches, stack = _make_sync_client_with_db()
        try:
            def _make_agent():
                agent = MagicMock()
                agent.run.return_value = iter([
                    FinalAnswerStep(output="Onboarding response"),
                ])
                return agent

            with patch("src.api.ws.create_onboarding_agent", side_effect=_make_agent):
                with client.websocket_connect("/ws/chat") as ws:
                    _ = ws.receive_text()  # welcome

                    # Send messages one at a time, collecting each response
                    finals = []
                    for i in range(3):
                        ws.send_text(json.dumps({
                            "type": "message",
                            "message": f"Message {i}",
                            "session_id": None,
                        }))
                        # Drain until we get the final for this message
                        for _ in range(10):
                            raw = ws.receive_text()
                            msg = json.loads(raw)
                            if msg["type"] == "final":
                                finals.append(msg)
                                break

                    assert len(finals) == 3
        finally:
            stack.close()
            for p in patches:
                p.stop()
