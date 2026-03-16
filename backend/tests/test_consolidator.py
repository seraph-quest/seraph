"""Tests for memory consolidator (src/memory/consolidator.py)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.session import SessionManager
from src.memory.consolidator import consolidate_session


@pytest.fixture
def sm():
    return SessionManager()


class TestConsolidateSession:
    async def test_skips_short_history(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "hi")
        # Should exit silently — no LLM call
        with patch("litellm.completion") as mock_completion:
            await consolidate_session("s1")
            mock_completion.assert_not_called()

    async def test_extracts_facts(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "My name is Alice and I work at ACME Corp as a software engineer.")
        await sm.add_message("s1", "assistant", "Nice to meet you, Alice! That sounds like a great position at ACME Corp.")

        llm_response = json.dumps({
            "facts": ["User's name is Alice", "User works at ACME Corp as a software engineer"],
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
            assert mock_add.call_count == 2

        from src.audit.repository import audit_repository

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_succeeded"
            and event["tool_name"] == "session_consolidation"
            and event["session_id"] == "s1"
            and event["details"]["stored_memory_count"] == 2
            for event in events
        )

    async def test_extracts_facts_uses_session_consolidation_runtime_path(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "My name is Alice and I work at ACME Corp as a software engineer.")
        await sm.add_message("s1", "assistant", "Nice to meet you, Alice! That sounds like a great position at ACME Corp.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "facts": [],
            "patterns": [],
            "goals": [],
            "reflections": [],
            "soul_updates": {},
        })

        with (
            patch("src.memory.consolidator.completion_with_fallback", AsyncMock(return_value=mock_resp)) as mock_completion,
            patch("src.memory.consolidator.add_memory"),
        ):
            await consolidate_session("s1")

        assert mock_completion.await_args.kwargs["runtime_path"] == "session_consolidation"

    async def test_applies_soul_updates(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "I'm passionate about machine learning and want to build an AI startup.")
        await sm.add_message("s1", "assistant", "That's ambitious! Let me note that in your goals.")

        llm_response = json.dumps({
            "facts": [],
            "patterns": [],
            "goals": [],
            "reflections": [],
            "soul_updates": {"Goals": "- Build an AI startup"},
        })
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = llm_response

        with patch("litellm.completion", return_value=mock_resp), \
             patch("src.memory.consolidator.add_memory"), \
             patch("src.memory.consolidator.update_soul_section") as mock_soul:
            await consolidate_session("s1")
            mock_soul.assert_called_once_with("Goals", "- Build an AI startup")

    async def test_handles_markdown_fences(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "I love hiking in the mountains on weekends with my dog named Max.")
        await sm.add_message("s1", "assistant", "That sounds wonderful! Max must love the outdoors too.")

        fenced = '```json\n' + json.dumps({
            "facts": ["User loves hiking with their dog Max"],
            "patterns": [],
            "goals": [],
            "reflections": [],
            "soul_updates": {},
        }) + '\n```'
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = fenced

        with patch("litellm.completion", return_value=mock_resp), \
             patch("src.memory.consolidator.add_memory") as mock_add:
            await consolidate_session("s1")
            assert mock_add.call_count == 1

    async def test_llm_failure_graceful(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Tell me about the weather in San Francisco this week.")
        await sm.add_message("s1", "assistant", "The weather in SF is typically mild with some fog.")

        with patch("litellm.completion", side_effect=RuntimeError("LLM down")):
            # Should not raise
            await consolidate_session("s1")

        from src.audit.repository import audit_repository

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_failed"
            and event["tool_name"] == "session_consolidation"
            and event["session_id"] == "s1"
            and event["details"]["error"] == "LLM down"
            for event in events
        )
