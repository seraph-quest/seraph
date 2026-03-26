from unittest.mock import patch

import pytest

from src.agent.session import SessionManager
from src.db.models import MemoryEpisodeType, MemoryKind
from src.memory.hybrid_retrieval import retrieve_hybrid_memory
from src.memory.repository import memory_repository


@pytest.mark.asyncio
async def test_hybrid_retrieval_combines_semantic_episode_and_vector_hits(async_db):
    sm = SessionManager()
    await sm.get_or_create("s1")
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Project Atlas",
        entity_type="project",
        aliases=["Atlas"],
    )
    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.8,
        confidence=0.9,
        project_entity_id=atlas.id,
    )
    await memory_repository.create_episode(
        episode_type=MemoryEpisodeType.workflow,
        session_id="s1",
        summary="Workflow Atlas deploy failed at upload step",
        content="Workflow Atlas deploy failed at the upload artifact step.",
        project_entity_id=atlas.id,
        salience=0.9,
        confidence=0.8,
    )

    with patch(
        "src.memory.hybrid_retrieval.search_with_status",
        return_value=(
            [
                {
                    "text": "Atlas stakeholder brief needs a summary update.",
                    "category": "fact",
                    "score": 0.18,
                    "created_at": "2026-03-25T09:00:00+00:00",
                }
            ],
            False,
        ),
    ):
        result = await retrieve_hybrid_memory(
            query="Atlas upload summary",
            active_projects=("Atlas",),
            limit=6,
        )

    assert "[project] Atlas launch" in result.context
    assert "[episode] Workflow Atlas deploy failed at upload step" in result.context
    assert "[fact] Atlas stakeholder brief needs a summary update." in result.context
    assert {hit.source for hit in result.hits} == {"semantic", "episodic", "vector"}


@pytest.mark.asyncio
async def test_hybrid_retrieval_boosts_active_project_linked_memories_without_lexical_match(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas launch",
        entity_type="project",
    )
    await memory_repository.create_memory(
        content="Prepare the Hermes budget memo.",
        kind=MemoryKind.commitment,
        summary="Prepare the Hermes budget memo",
        importance=0.99,
        confidence=0.8,
    )
    await memory_repository.create_memory(
        content="Send the Atlas checklist before Friday.",
        kind=MemoryKind.commitment,
        summary="Send the Atlas checklist before Friday",
        importance=0.25,
        confidence=0.8,
        project_entity_id=atlas.id,
    )

    with patch(
        "src.memory.hybrid_retrieval.search_with_status",
        return_value=([], False),
    ):
        result = await retrieve_hybrid_memory(
            query="What should I focus on right now?",
            active_projects=("Atlas",),
            limit=4,
        )

    assert result.hits[0].text == "Send the Atlas checklist before Friday"
    assert result.hits[0].source == "semantic"


@pytest.mark.asyncio
async def test_hybrid_retrieval_marks_vector_failures_degraded_but_keeps_lexical_hits(async_db):
    await memory_repository.create_memory(
        content="Review the Atlas launch brief.",
        kind=MemoryKind.commitment,
        summary="Review the Atlas launch brief",
        importance=0.8,
        confidence=0.8,
    )

    with patch(
        "src.memory.hybrid_retrieval.search_with_status",
        return_value=([], True),
    ):
        result = await retrieve_hybrid_memory(
            query="Atlas brief",
            active_projects=(),
            limit=4,
        )

    assert result.degraded is True
    assert result.hits[0].text == "Review the Atlas launch brief"
    assert "[commitment] Review the Atlas launch brief" in result.context


@pytest.mark.asyncio
async def test_hybrid_retrieval_filters_vector_hits_for_non_active_memories(async_db):
    archived = await memory_repository.create_memory(
        content="Atlas launch is delayed.",
        kind=MemoryKind.project,
        summary="Atlas launch delayed",
        importance=0.95,
        status="superseded",
    )
    active = await memory_repository.create_memory(
        content="Atlas launch is on track.",
        kind=MemoryKind.project,
        summary="Atlas launch on track",
        importance=0.9,
    )

    with patch(
        "src.memory.hybrid_retrieval.search_with_status",
        return_value=(
            [
                {
                    "id": archived.memory_id,
                    "text": "Atlas launch is delayed.",
                    "category": "fact",
                    "score": 0.11,
                    "created_at": "2026-03-25T09:00:00+00:00",
                },
                {
                    "id": active.memory_id,
                    "text": "Atlas launch is on track.",
                    "category": "fact",
                    "score": 0.09,
                    "created_at": "2026-03-25T10:00:00+00:00",
                },
            ],
            False,
        ),
    ):
        result = await retrieve_hybrid_memory(
            query="Atlas launch status",
            active_projects=("Atlas",),
            limit=4,
        )

    assert "Atlas launch is delayed." not in result.context
    assert "Atlas launch is on track." in result.context


@pytest.mark.asyncio
async def test_hybrid_retrieval_filters_vector_hits_when_all_ids_are_non_active(async_db):
    archived = await memory_repository.create_memory(
        content="Atlas launch is delayed.",
        kind=MemoryKind.project,
        summary="Atlas launch delayed",
        importance=0.95,
        status="archived",
    )

    with patch(
        "src.memory.hybrid_retrieval.search_with_status",
        return_value=(
            [
                {
                    "id": archived.memory_id,
                    "text": "Atlas launch is delayed.",
                    "category": "fact",
                    "score": 0.11,
                    "created_at": "2026-03-25T09:00:00+00:00",
                },
            ],
            False,
        ),
    ):
        result = await retrieve_hybrid_memory(
            query="Atlas launch status",
            active_projects=("Atlas",),
            limit=4,
        )

    assert result.context == ""
    assert result.hits == ()
