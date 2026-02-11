"""Memory consolidation reliability — deleted sessions, embedding failures, malformed LLM output."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.agent.session import SessionManager
from src.memory.consolidator import consolidate_session


@pytest.fixture
def sm():
    return SessionManager()


class TestConsolidationAfterSessionDelete:
    async def test_deleted_session_returns_silently(self, async_db, sm):
        """Consolidation on a deleted session should not raise."""
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "This is a long enough message for consolidation to trigger.")
        await sm.add_message("s1", "assistant", "I understand, let me help you with that request.")
        await sm.delete("s1")

        # Should not raise — history will be empty
        with patch("litellm.completion") as mock_llm:
            await consolidate_session("s1")
            mock_llm.assert_not_called()  # short/empty history → early return

    async def test_nonexistent_session_returns_silently(self, async_db, sm):
        """Consolidation on a session that never existed should not raise."""
        with patch("litellm.completion") as mock_llm:
            await consolidate_session("never-existed")
            mock_llm.assert_not_called()


class TestEmbeddingFailures:
    async def test_add_memory_failure_does_not_crash(self, async_db, sm):
        """If add_memory fails, consolidation should still complete."""
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "My name is Alice and I work at a big company.")
        await sm.add_message("s1", "assistant", "Nice to meet you Alice!")

        llm_response = json.dumps({
            "facts": ["User's name is Alice"],
            "patterns": [],
            "goals": [],
            "reflections": [],
            "soul_updates": {},
        })
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = llm_response

        with patch("litellm.completion", return_value=mock_resp), \
             patch("src.memory.consolidator.add_memory", side_effect=RuntimeError("Embedding crashed")):
            # Should not raise — consolidator catches all exceptions
            await consolidate_session("s1")


class TestMalformedLLMOutput:
    async def test_invalid_json_handled(self, async_db, sm):
        """LLM returns non-JSON — should not crash."""
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "I enjoy hiking and reading books about history.")
        await sm.add_message("s1", "assistant", "Those are great hobbies!")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "This is not JSON at all"

        with patch("litellm.completion", return_value=mock_resp):
            await consolidate_session("s1")  # should not raise

    async def test_missing_fields_handled(self, async_db, sm):
        """LLM returns valid JSON but missing expected fields."""
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Tell me about quantum computing and its future applications.")
        await sm.add_message("s1", "assistant", "Quantum computing is a fascinating field with many possibilities.")

        llm_response = json.dumps({"unexpected_key": "value"})
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = llm_response

        with patch("litellm.completion", return_value=mock_resp), \
             patch("src.memory.consolidator.add_memory") as mock_add:
            await consolidate_session("s1")
            mock_add.assert_not_called()  # no valid categories → no memories stored

    async def test_empty_lists_handled(self, async_db, sm):
        """LLM returns all empty lists — nothing stored, no error."""
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "What time is it?")
        await sm.add_message("s1", "assistant", "I don't have access to the current time.")

        llm_response = json.dumps({
            "facts": [],
            "patterns": [],
            "goals": [],
            "reflections": [],
            "soul_updates": {},
        })
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = llm_response

        with patch("litellm.completion", return_value=mock_resp), \
             patch("src.memory.consolidator.add_memory") as mock_add:
            await consolidate_session("s1")
            mock_add.assert_not_called()
