"""Tests for memory consolidator (src/memory/consolidator.py)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.models import MemoryKind, MemorySnapshotKind
from src.agent.session import SessionManager
from src.memory.consolidator import consolidate_session
from src.memory.decay import DecayMaintenanceResult
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

    async def test_duplicate_memory_merges_and_reuses_existing_vector_row(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message(
            "s1",
            "user",
            "Please remember that I prefer concise morning briefings for status updates.",
        )
        await sm.add_message(
            "s1",
            "assistant",
            "I will keep the morning briefing preference in memory.",
        )

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "User prefers concise morning briefings.",
                    "kind": "communication_preference",
                    "summary": "Prefers concise morning briefings",
                    "confidence": 0.9,
                    "importance": 0.8,
                }
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
            return_value="vec-1",
        ) as mock_add_memory:
            await consolidate_session("s1")
            await sm.add_message(
                "s1",
                "user",
                "One more time: keep those morning briefings concise.",
            )
            await consolidate_session("s1")

        grouped = await memory_repository.list_memories_by_kinds(
            kinds=(MemoryKind.communication_preference,),
            limit_per_kind=2,
        )
        memory = grouped["communication_preference"][0]
        sources = await memory_repository.list_sources(memory_id=memory.id)

        assert len(grouped["communication_preference"]) == 1
        assert memory.embedding_id == "vec-1"
        assert memory.reinforcement == pytest.approx(1.25)
        assert mock_add_memory.call_count == 1
        assert len([source for source in sources if source.source_message_id]) >= 2

    async def test_consolidation_audit_reports_created_and_merged_counts(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Atlas launch is active and needs concise check-ins.")
        await sm.add_message("s1", "assistant", "I will remember Atlas and the briefing preference.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "Atlas launch is the active release project.",
                    "kind": "project",
                    "summary": "Atlas launch",
                    "confidence": 0.9,
                    "importance": 0.9,
                    "project": "Atlas",
                },
                {
                    "text": "User prefers concise morning briefings.",
                    "kind": "communication_preference",
                    "summary": "Prefers concise morning briefings",
                    "confidence": 0.9,
                    "importance": 0.8,
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
            return_value="vec-1",
        ):
            await consolidate_session("s1")
            await sm.add_message("s1", "user", "Again: concise morning briefings, please.")
            await consolidate_session("s1")

        from src.audit.repository import audit_repository

        events = await audit_repository.list_events(limit=20)
        consolidation_events = [
            event
            for event in events
            if event["tool_name"] == "session_consolidation"
            and event["event_type"] in {"background_task_succeeded", "background_task_partially_succeeded"}
        ]

        assert any(
            event["details"]["created_memory_count"] == 2
            and event["details"]["merged_memory_count"] == 0
            and event["details"]["stored_memory_count"] == 2
            and event["details"]["captured_source_message_count"] >= 2
            for event in consolidation_events
        )
        assert any(
            event["details"]["created_memory_count"] == 0
            and event["details"]["merged_memory_count"] == 2
            and event["details"]["stored_memory_count"] == 2
            and event["details"]["source_link_count"] >= 1
            for event in consolidation_events
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
        assert memories_by_kind["collaborator"].subject_entity_id is not None
        assert memories_by_kind["collaborator"].project_entity_id is not None
        assert memories_by_kind["collaborator"].metadata_json == (
            '{"input_schema": "typed", "project_name": "Investor updates", '
            '"source": "llm_extract", "subject_name": "Alice", "writer": "session_consolidation"}'
        )
        assert memories_by_kind["communication_preference"].summary == (
            "Prefers concise morning briefings"
        )
        assert memories_by_kind["communication_preference"].confidence == pytest.approx(0.95)
        assert memories_by_kind["communication_preference"].importance == pytest.approx(0.85)

    async def test_typed_project_and_commitment_keep_structured_kinds_but_link_vector_categories(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Atlas launch needs a project memory and a clear commitment.")
        await sm.add_message("s1", "assistant", "I will store both the project and the commitment.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "Atlas launch is the current release project.",
                    "kind": "project",
                    "summary": "Atlas launch",
                    "confidence": 0.91,
                    "importance": 0.9,
                    "project": "Atlas",
                },
                {
                    "text": "Send the Atlas launch checklist before Friday.",
                    "kind": "commitment",
                    "summary": "Send Atlas checklist before Friday",
                    "confidence": 0.88,
                    "importance": 0.87,
                    "project": "Atlas",
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
            side_effect=["vec-project", "vec-commitment"],
        ) as mock_add_memory:
            await consolidate_session("s1")

        memories = await memory_repository.list_memories_by_kinds(
            kinds=(MemoryKind.project, MemoryKind.commitment),
            limit_per_kind=2,
        )

        project_memory = memories["project"][0]
        commitment_memory = memories["commitment"][0]

        assert project_memory.kind == MemoryKind.project
        assert commitment_memory.kind == MemoryKind.commitment
        assert project_memory.project_entity_id == commitment_memory.project_entity_id
        assert project_memory.project_entity_id is not None
        assert [call.kwargs["category"] for call in mock_add_memory.call_args_list] == ["fact", "goal"]

    async def test_entity_link_failure_does_not_abort_consolidation(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Alice owns Atlas launch communications.")
        await sm.add_message("s1", "assistant", "I will keep that collaborator memory.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "Alice owns Atlas launch communications.",
                    "kind": "collaborator",
                    "summary": "Alice owns Atlas launch communications",
                    "confidence": 0.92,
                    "importance": 0.85,
                    "subject": "Alice",
                    "project": "Atlas",
                }
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
            return_value="vec-1",
        ), patch(
            "src.memory.consolidator.resolve_memory_links",
            AsyncMock(side_effect=RuntimeError("entity linking down")),
        ):
            await consolidate_session("s1")

        memories = await memory_repository.list_memories(limit=5)

        assert memories == []

        from src.audit.repository import audit_repository

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_partially_succeeded"
            and event["tool_name"] == "session_consolidation"
            and event["session_id"] == "s1"
            and event["details"]["stored_memory_count"] == 0
            and event["details"]["created_memory_count"] == 0
            and event["details"]["merged_memory_count"] == 0
            and event["details"]["vector_memory_count"] == 0
            and event["details"]["partial_write_count"] == 1
            and event["details"]["write_failure_count"] == 0
            and event["details"]["snapshot_refresh_failed"] is False
            for event in events
        )

    async def test_consolidation_refreshes_bounded_snapshot(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Atlas launch is the active release project.")
        await sm.add_message("s1", "assistant", "I will keep that project context in bounded recall.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "Atlas launch is the active release project.",
                    "kind": "project",
                    "summary": "Atlas launch",
                    "confidence": 0.9,
                    "importance": 0.88,
                    "project": "Atlas",
                }
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
        ), patch("src.memory.consolidator.add_memory", return_value="vec-1"), patch(
            "src.memory.consolidator.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ):
            await consolidate_session("s1")

        snapshot = await memory_repository.get_snapshot(MemorySnapshotKind.bounded_guardian_context)

        assert snapshot is not None
        assert "Identity: Builder" in snapshot.content
        assert "Atlas launch" in snapshot.content

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
            "src.memory.consolidator.update_profile_soul_section",
            AsyncMock(return_value="# Guardian Record\n\n## Goals\n- Build an AI startup"),
        ) as mock_soul:
            await consolidate_session("s1")
            mock_soul.assert_awaited_once_with("Goals", "- Build an AI startup")

    async def test_soul_updates_persist_structured_profile_projection(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "I want to build an AI startup.")
        await sm.add_message("s1", "assistant", "I will record that as a durable goal.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "facts": [],
            "patterns": [],
            "goals": [],
            "reflections": [],
            "soul_updates": {"Goals": "- Build an AI startup"},
        })

        with patch(
            "src.memory.consolidator.completion_with_fallback",
            AsyncMock(return_value=mock_resp),
        ), patch("src.memory.consolidator.add_memory"):
            await consolidate_session("s1")

        from src.api.profile import get_profile

        profile = await get_profile()

        assert profile["soul_sections"]["Goals"] == "- Build an AI startup"
        assert "## Goals" in profile["soul_text"]
        assert "- Build an AI startup" in profile["soul_text"]

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

    async def test_snapshot_refresh_failure_is_reported_as_partial_success(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Atlas launch is the active release project.")
        await sm.add_message("s1", "assistant", "I will keep that project context in memory.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "Atlas launch is the active release project.",
                    "kind": "project",
                    "summary": "Atlas launch",
                    "confidence": 0.9,
                    "importance": 0.88,
                    "project": "Atlas",
                }
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
            return_value="vec-1",
        ), patch(
            "src.memory.consolidator.refresh_bounded_guardian_snapshot",
            AsyncMock(side_effect=RuntimeError("snapshot down")),
        ):
            result = await consolidate_session("s1")

        memories = await memory_repository.list_memories(limit=5)

        assert result.outcome == "partially_succeeded"
        assert result.should_cache_fingerprint is False
        assert len(memories) == 1

        from src.audit.repository import audit_repository

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_partially_succeeded"
            and event["tool_name"] == "session_consolidation"
            and event["session_id"] == "s1"
            and event["details"]["stored_memory_count"] == 1
            and event["details"]["partial_write_count"] == 1
            and event["details"]["snapshot_refresh_failed"] is True
            for event in events
        )

    async def test_consolidation_reports_decay_maintenance_counts(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Atlas launch is on track.")
        await sm.add_message("s1", "assistant", "I will keep the latest project status in memory.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "Atlas launch is on track.",
                    "kind": "project",
                    "summary": "Atlas launch on track",
                    "confidence": 0.9,
                    "importance": 0.9,
                    "project": "Atlas",
                }
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
            return_value="vec-1",
        ), patch(
            "src.memory.consolidator.apply_memory_decay_policies",
            AsyncMock(
                return_value=DecayMaintenanceResult(
                    contradiction_count=1,
                    superseded_count=1,
                    decayed_count=2,
                    archived_count=1,
                )
            ),
        ) as mock_decay:
            await consolidate_session("s1")

        mock_decay.assert_awaited_once()

        from src.audit.repository import audit_repository

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_succeeded"
            and event["tool_name"] == "session_consolidation"
            and event["session_id"] == "s1"
            and event["details"]["contradiction_count"] == 1
            and event["details"]["superseded_memory_count"] == 1
            and event["details"]["decayed_memory_count"] == 2
            and event["details"]["archived_memory_count"] == 1
            and event["details"]["decay_maintenance_failed"] is False
            for event in events
        )

    async def test_source_capture_uses_newest_session_window(self, async_db, sm):
        await sm.get_or_create("s1")
        for index in range(60):
            await sm.add_message(
                "s1",
                "user" if index % 2 == 0 else "assistant",
                f"Hermes archive note {index} about the old project backlog.",
            )
        await sm.add_message("s1", "user", "Atlas launch deadline is Friday afternoon.")
        await sm.add_message("s1", "assistant", "I will remember the Atlas Friday deadline.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "Atlas launch deadline is Friday afternoon.",
                    "kind": "timeline",
                    "summary": "Atlas deadline Friday afternoon",
                    "confidence": 0.91,
                    "importance": 0.86,
                    "project": "Atlas",
                }
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
        ), patch("src.memory.consolidator.add_memory", return_value="vec-1"):
            await consolidate_session("s1")

        memories = await memory_repository.list_memories_by_kinds(
            kinds=(MemoryKind.timeline,),
            limit_per_kind=1,
        )
        sources = await memory_repository.list_sources(memory_id=memories["timeline"][0].id)
        message_sources = [source for source in sources if source.source_type == "message"]

        assert message_sources
        assert all(source.source_message_id for source in message_sources)
        assert all("Atlas" in (source.snippet or "") for source in message_sources)

    async def test_zero_overlap_memory_does_not_claim_message_source_links(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Atlas launch needs a cleaner checklist.")
        await sm.add_message("s1", "assistant", "I will keep the Atlas checklist in memory.")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "User spends every weekend hiking remote mountain trails.",
                    "kind": "preference",
                    "summary": "Weekend mountain hiking",
                    "confidence": 0.7,
                    "importance": 0.5,
                }
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
        ), patch("src.memory.consolidator.add_memory", return_value="vec-1"):
            await consolidate_session("s1")

        memories = await memory_repository.list_memories_by_kinds(
            kinds=(MemoryKind.preference,),
            limit_per_kind=1,
        )
        sources = await memory_repository.list_sources(memory_id=memories["preference"][0].id)

        assert all(source.source_message_id is None for source in sources)

        from src.audit.repository import audit_repository

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["tool_name"] == "session_consolidation"
            and event["event_type"] == "background_task_succeeded"
            and event["details"]["source_link_count"] == 0
            for event in events
        )

    async def test_duplicate_merge_repairs_missing_embedding(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message(
            "s1",
            "user",
            "Please remember that I prefer concise morning briefings for updates.",
        )
        await sm.add_message(
            "s1",
            "assistant",
            "I will keep the morning briefing preference in memory.",
        )
        await memory_repository.create_memory(
            content="User prefers concise morning briefings.",
            kind=MemoryKind.communication_preference,
            summary="Prefers concise morning briefings",
            source_session_id="older-session",
            embedding_id=None,
        )

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "User prefers concise morning briefings.",
                    "kind": "communication_preference",
                    "summary": "Prefers concise morning briefings",
                    "confidence": 0.92,
                    "importance": 0.82,
                }
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
            return_value="vec-repaired",
        ) as mock_add_memory:
            await consolidate_session("s1")

        memories = await memory_repository.list_memories_by_kinds(
            kinds=(MemoryKind.communication_preference,),
            limit_per_kind=1,
        )

        assert memories["communication_preference"][0].embedding_id == "vec-repaired"
        assert memories["communication_preference"][0].reinforcement == pytest.approx(1.25)
        assert mock_add_memory.call_count == 1

    async def test_duplicate_merge_records_session_provenance_when_message_overlap_is_zero(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Keep updates short at dawn.")
        await sm.add_message("s1", "assistant", "I will keep updates short at dawn.")
        created = await memory_repository.create_memory(
            content="User prefers concise morning briefings.",
            kind=MemoryKind.communication_preference,
            summary="Prefers concise morning briefings",
            source_session_id="older-session",
            embedding_id="vec-existing",
        )

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "memories": [
                {
                    "text": "User prefers concise morning briefings.",
                    "kind": "communication_preference",
                    "summary": "Prefers concise morning briefings",
                    "confidence": 0.92,
                    "importance": 0.83,
                }
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
            return_value="vec-ignored",
        ) as mock_add_memory:
            await consolidate_session("s1")

        memories = await memory_repository.list_memories_by_kinds(
            kinds=(MemoryKind.communication_preference,),
            limit_per_kind=1,
        )
        sources = await memory_repository.list_sources(memory_id=created.memory_id)

        assert memories["communication_preference"][0].reinforcement == pytest.approx(1.25)
        assert any(
            source.source_type == "session"
            and source.source_session_id == "s1"
            and source.source_message_id is None
            for source in sources
        )
        assert not any(
            source.source_type == "message"
            and source.source_session_id == "s1"
            for source in sources
        )
        assert mock_add_memory.call_count == 0
