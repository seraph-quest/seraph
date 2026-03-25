"""Tests for memory consolidator (src/memory/consolidator.py)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.session import SessionManager
from src.memory.consolidator import consolidate_session
from src.memory.repository import memory_repository


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

        with patch("litellm.completion", return_value=mock_resp), patch(
            "src.memory.consolidator.add_memory",
            side_effect=["vec-1", "vec-2"],
        ) as mock_add:
            await consolidate_session("s1")
            assert mock_add.call_count == 2

        from src.audit.repository import audit_repository

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_succeeded"
            and event["tool_name"] == "session_consolidation"
            and event["session_id"] == "s1"
            and event["details"]["stored_memory_count"] == 2
            and event["details"]["vector_memory_count"] == 2
            and event["details"]["partial_write_count"] == 0
            for event in events
        )
        assert any(
            event["event_type"] == "llm_primary_success"
            and event["tool_name"] == "llm_runtime"
            and event["session_id"] == "s1"
            and event["details"]["runtime_path"] == "session_consolidation"
            and event["details"]["request_id"]
            for event in events
        )

    async def test_extracts_facts_also_persists_structured_memory(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "My name is Alice and I prefer concise daily summaries.")
        await sm.add_message("s1", "assistant", "I will keep that in mind.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "facts": ["User's name is Alice"],
            "patterns": [],
            "goals": [],
            "reflections": [],
            "soul_updates": {},
        })

        with patch(
            "src.memory.consolidator.completion_with_fallback",
            AsyncMock(return_value=mock_resp),
        ), patch("src.memory.consolidator.add_memory", return_value="vec-1"):
            await consolidate_session("s1")

        memories = await memory_repository.list_memories(limit=5)

        assert len(memories) == 1
        assert memories[0].content == "User's name is Alice"
        assert memories[0].kind.value == "fact"
        assert memories[0].embedding_id == "vec-1"
        assert memories[0].source_session_id == "s1"
        assert memories[0].metadata_json == (
            '{"input_schema": "legacy", "legacy_field": "facts", "source": "llm_extract", '
            '"writer": "session_consolidation"}'
        )

    async def test_extracts_typed_memory_objects_with_provenance(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Alice owns investor updates, and I want concise morning briefings.")
        await sm.add_message("s1", "assistant", "I will remember both the collaborator and your preference.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "Alice owns the investor update thread.",
                    "kind": "collaborator",
                    "summary": "Alice owns investor updates",
                    "confidence": 0.92,
                    "importance": 0.88,
                    "subject": "Alice",
                    "project": "Investor updates",
                    "last_confirmed_at": "2026-03-25T08:30:00Z",
                },
                {
                    "text": "User prefers concise morning briefings.",
                    "kind": "communication_preference",
                    "summary": "Prefers concise morning briefings",
                    "confidence": 0.95,
                    "importance": 0.85,
                },
            ],
            "facts": [],
            "patterns": [],
            "goals": [],
            "reflections": [],
            "soul_updates": {},
        })

        with patch(
            "src.memory.consolidator.completion_with_fallback",
            AsyncMock(return_value=mock_resp),
        ), patch(
            "src.memory.consolidator.add_memory",
            side_effect=["vec-1", "vec-2"],
        ):
            await consolidate_session("s1")

        memories = await memory_repository.list_memories(limit=5)
        memories_by_kind = {memory.kind.value: memory for memory in memories}

        assert memories_by_kind["collaborator"].summary == "Alice owns investor updates"
        assert memories_by_kind["collaborator"].confidence == pytest.approx(0.92)
        assert memories_by_kind["collaborator"].importance == pytest.approx(0.88)
        assert memories_by_kind["collaborator"].metadata_json == (
            '{"input_schema": "typed", "project_name": "Investor updates", '
            '"source": "llm_extract", "subject_name": "Alice", "writer": "session_consolidation"}'
        )
        assert memories_by_kind["communication_preference"].summary == (
            "Prefers concise morning briefings"
        )
        assert memories_by_kind["communication_preference"].confidence == pytest.approx(0.95)
        assert memories_by_kind["communication_preference"].importance == pytest.approx(0.85)

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

        with patch(
            "src.memory.consolidator.completion_with_fallback",
            AsyncMock(return_value=mock_resp),
        ) as mock_completion, patch("src.memory.consolidator.add_memory"):
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

        with patch(
            "src.memory.consolidator.completion_with_fallback",
            AsyncMock(return_value=mock_resp),
        ), patch("src.memory.consolidator.add_memory"), patch(
            "src.memory.consolidator.update_soul_section"
        ) as mock_soul:
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

        with patch(
            "src.memory.consolidator.completion_with_fallback",
            AsyncMock(return_value=mock_resp),
        ), patch("src.memory.consolidator.add_memory", return_value="vec-1") as mock_add:
            await consolidate_session("s1")
            assert mock_add.call_count == 1

    async def test_llm_failure_graceful(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Tell me about the weather in San Francisco this week.")
        await sm.add_message("s1", "assistant", "The weather in SF is typically mild with some fog.")

        with patch(
            "src.memory.consolidator.completion_with_fallback",
            AsyncMock(side_effect=RuntimeError("LLM down")),
        ):
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

    async def test_vector_failure_does_not_block_structured_memory_write(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "I need to finish the Atlas launch brief this week.")
        await sm.add_message("s1", "assistant", "I can remember that commitment.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "facts": [],
            "patterns": [],
            "goals": ["Finish the Atlas launch brief this week"],
            "reflections": [],
            "soul_updates": {},
        })

        with patch(
            "src.memory.consolidator.completion_with_fallback",
            AsyncMock(return_value=mock_resp),
        ), patch(
            "src.memory.consolidator.add_memory",
            side_effect=RuntimeError("Embedding crashed"),
        ):
            await consolidate_session("s1")

        memories = await memory_repository.list_memories(limit=5)

        assert len(memories) == 1
        assert memories[0].content == "Finish the Atlas launch brief this week"
        assert memories[0].kind.value == "goal"
        assert memories[0].embedding_id is None

    async def test_structured_failure_is_reported_as_partial_success(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Remember that I need to review the Atlas brief tomorrow.")
        await sm.add_message("s1", "assistant", "I will keep that reminder.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "facts": [],
            "patterns": [],
            "goals": ["Review the Atlas brief tomorrow"],
            "reflections": [],
            "soul_updates": {},
        })

        with patch(
            "src.memory.consolidator.completion_with_fallback",
            AsyncMock(return_value=mock_resp),
        ), patch(
            "src.memory.consolidator.add_memory",
            return_value="vec-1",
        ), patch.object(
            memory_repository,
            "create_memory",
            AsyncMock(side_effect=RuntimeError("sqlite write failed")),
        ):
            await consolidate_session("s1")

        from src.audit.repository import audit_repository

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_partially_succeeded"
            and event["tool_name"] == "session_consolidation"
            and event["session_id"] == "s1"
            and event["details"]["stored_memory_count"] == 0
            and event["details"]["vector_memory_count"] == 1
            and event["details"]["partial_write_count"] == 1
            for event in events
        )
