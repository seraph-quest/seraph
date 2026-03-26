import json
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from src.db.models import MemoryEdgeType, MemoryEntityType, MemoryKind, MemorySnapshotKind
from src.memory.repository import memory_repository


@pytest.mark.asyncio
async def test_create_memory_persists_structured_fields(async_db):
    result = await memory_repository.create_memory(
        content="User prefers concise status updates.",
        category="preference",
        kind=MemoryKind.communication_preference,
        source_session_id="sess-1",
        summary="Prefers concise updates",
        confidence=0.9,
        importance=0.8,
        reinforcement=1.2,
        metadata={"writer": "test"},
    )

    memories = await memory_repository.list_memories(limit=5)

    assert result.memory_id
    assert len(memories) == 1
    assert memories[0].kind == MemoryKind.communication_preference
    assert memories[0].summary == "Prefers concise updates"
    assert memories[0].confidence == pytest.approx(0.9)
    assert memories[0].importance == pytest.approx(0.8)
    assert memories[0].reinforcement == pytest.approx(1.2)
    assert memories[0].source_session_id == "sess-1"
    assert memories[0].metadata_json == '{"writer": "test"}'


@pytest.mark.asyncio
async def test_create_memory_preserves_explicit_message_source_snippet(async_db):
    result = await memory_repository.create_memory(
        content="User prefers concise status updates.",
        kind=MemoryKind.communication_preference,
        source_session_id="sess-1",
        source_message_id="msg-1",
        source_type="message",
        source_snippet="Please keep the status updates concise.",
        summary="Prefers concise updates",
    )

    sources = await memory_repository.list_sources(memory_id=result.memory_id)

    assert len(sources) == 1
    assert sources[0].source_type == "message"
    assert sources[0].source_message_id == "msg-1"
    assert sources[0].snippet == "Please keep the status updates concise."


@pytest.mark.asyncio
async def test_get_or_create_entity_merges_aliases(async_db):
    first = await memory_repository.get_or_create_entity(
        canonical_name="Project Atlas",
        entity_type=MemoryEntityType.project,
        aliases=["Atlas"],
    )
    second = await memory_repository.get_or_create_entity(
        canonical_name="Project Atlas",
        entity_type=MemoryEntityType.project,
        aliases=["Atlas", "The Atlas rewrite"],
    )

    assert first.id == second.id
    assert second.aliases_json == '["Atlas", "The Atlas rewrite"]'


@pytest.mark.asyncio
async def test_get_or_create_entity_normalizes_case_and_alias_matches(async_db):
    first = await memory_repository.get_or_create_entity(
        canonical_name="Project Atlas",
        entity_type=MemoryEntityType.project,
        aliases=["Atlas"],
    )
    second = await memory_repository.get_or_create_entity(
        canonical_name="project atlas",
        entity_type=MemoryEntityType.project,
    )
    third = await memory_repository.get_or_create_entity(
        canonical_name="Atlas",
        entity_type=MemoryEntityType.project,
        aliases=["Project Atlas"],
    )

    assert first.id == second.id == third.id


@pytest.mark.asyncio
async def test_create_memory_rejects_invalid_kind(async_db):
    with pytest.raises(ValueError, match="Invalid MemoryKind"):
        await memory_repository.create_memory(
            content="Broken kind",
            kind="commmitment",
        )


@pytest.mark.asyncio
async def test_save_snapshot_upserts_by_kind(async_db):
    first = await memory_repository.save_snapshot(
        kind=MemorySnapshotKind.bounded_guardian_context,
        content="- Identity: Builder",
        source_hash="abc",
    )
    second = await memory_repository.save_snapshot(
        kind=MemorySnapshotKind.bounded_guardian_context,
        content="- Identity: Builder\n- Goal memory: Ship batch A",
        source_hash="def",
    )
    stored = await memory_repository.get_snapshot(MemorySnapshotKind.bounded_guardian_context)

    assert first.id == second.id
    assert stored is not None
    assert stored.content.endswith("Ship batch A")
    assert stored.source_hash == "def"


@pytest.mark.asyncio
async def test_create_edge_persists_structured_relationship(async_db):
    first = await memory_repository.create_memory(
        content="Prepare the Atlas investor brief.",
        kind=MemoryKind.commitment,
    )
    second = await memory_repository.create_memory(
        content="Investor brief belongs to Project Atlas.",
        kind=MemoryKind.project,
    )

    edge = await memory_repository.create_edge(
        from_memory_id=first.memory_id,
        to_memory_id=second.memory_id,
        edge_type=MemoryEdgeType.supports,
        metadata={"writer": "test"},
    )

    assert edge.from_memory_id == first.memory_id
    assert edge.to_memory_id == second.memory_id
    assert edge.edge_type == MemoryEdgeType.supports
    assert edge.metadata_json == '{"writer": "test"}'


@pytest.mark.asyncio
async def test_create_memory_rejects_unknown_entity_links(async_db):
    with pytest.raises(IntegrityError):
        await memory_repository.create_memory(
            content="Orphan collaborator link",
            kind=MemoryKind.collaborator,
            subject_entity_id="missing-entity",
        )


@pytest.mark.asyncio
async def test_list_memories_by_kinds_groups_richer_memory_types(async_db):
    await memory_repository.create_memory(
        content="Review the Atlas brief tomorrow morning.",
        kind=MemoryKind.commitment,
        importance=0.9,
    )
    await memory_repository.create_memory(
        content="Alice owns the investor update thread.",
        kind=MemoryKind.collaborator,
        importance=0.8,
    )
    await memory_repository.create_memory(
        content="Prefers concise morning briefings.",
        kind=MemoryKind.communication_preference,
        importance=0.7,
    )

    grouped = await memory_repository.list_memories_by_kinds(
        kinds=(
            MemoryKind.commitment,
            MemoryKind.collaborator,
            MemoryKind.communication_preference,
        ),
        limit_per_kind=1,
    )

    assert grouped["commitment"][0].content == "Review the Atlas brief tomorrow morning."
    assert grouped["collaborator"][0].content == "Alice owns the investor update thread."
    assert grouped["communication_preference"][0].content == "Prefers concise morning briefings."


@pytest.mark.asyncio
async def test_find_entities_by_names_matches_aliases(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Project Atlas",
        entity_type=MemoryEntityType.project,
        aliases=["Atlas"],
    )

    resolved = await memory_repository.find_entities_by_names(
        names=("Atlas", "Project Atlas", "Unknown"),
        entity_type=MemoryEntityType.project,
    )

    assert resolved["Atlas"].id == atlas.id
    assert resolved["Project Atlas"].id == atlas.id
    assert "Unknown" not in resolved


@pytest.mark.asyncio
async def test_find_entities_by_names_supports_unique_project_token_fallback(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas launch",
        entity_type=MemoryEntityType.project,
    )

    resolved = await memory_repository.find_entities_by_names(
        names=("Atlas",),
        entity_type=MemoryEntityType.project,
    )

    assert resolved["Atlas"].id == atlas.id


@pytest.mark.asyncio
async def test_list_memories_for_entities_supports_project_filters(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Project Atlas",
        entity_type=MemoryEntityType.project,
        aliases=["Atlas"],
    )
    other = await memory_repository.get_or_create_entity(
        canonical_name="Project Hermes",
        entity_type=MemoryEntityType.project,
    )
    await memory_repository.create_memory(
        content="Review the Atlas brief tomorrow morning.",
        kind=MemoryKind.commitment,
        project_entity_id=atlas.id,
        importance=0.9,
    )
    await memory_repository.create_memory(
        content="Review the Hermes brief next week.",
        kind=MemoryKind.commitment,
        project_entity_id=other.id,
        importance=0.95,
    )

    linked = await memory_repository.list_memories_for_entities(
        project_entity_ids=(atlas.id,),
        kinds=(MemoryKind.commitment,),
    )

    assert [memory.content for memory in linked] == ["Review the Atlas brief tomorrow morning."]


@pytest.mark.asyncio
async def test_sync_scoped_memory_backfills_scope_key_for_legacy_metadata_match(async_db):
    created = await memory_repository.create_memory(
        content="For advisory interventions, reduce direct interruptions after recent negative or failed outcomes.",
        kind=MemoryKind.procedural,
        summary="For advisory interventions, reduce direct interruptions after recent negative or failed outcomes.",
        metadata={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "delivery",
            "bias_value": "reduce_interruptions",
        },
    )

    result = await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "delivery",
        },
        content="For advisory interventions, reduce direct interruptions after recent negative or failed outcomes.",
        summary="For advisory interventions, reduce direct interruptions after recent negative or failed outcomes.",
        metadata={"bias_value": "reduce_interruptions"},
    )

    memories = await memory_repository.list_memories(kind=MemoryKind.procedural, limit=10)

    assert result is not None
    assert result.memory_id == created.memory_id
    assert len(memories) == 1
    assert memories[0].scope_key is not None
    assert json.loads(memories[0].metadata_json or "{}")["lesson_type"] == "delivery"


@pytest.mark.asyncio
async def test_merge_memory_strengthens_existing_record_and_dedupes_sources(async_db):
    created = await memory_repository.create_memory(
        content="User prefers concise morning briefings.",
        kind=MemoryKind.communication_preference,
        summary="Prefers concise morning briefings",
        confidence=0.7,
        importance=0.6,
        reinforcement=1.0,
        source_session_id="sess-1",
    )

    candidate = await memory_repository.find_merge_candidate(
        kind=MemoryKind.communication_preference,
        summary="Prefers concise morning briefings",
        content="User prefers concise morning briefings.",
    )

    assert candidate is not None
    assert candidate.id == created.memory_id

    await memory_repository.merge_memory(
        created.memory_id,
        summary="Prefers concise morning briefings",
        confidence=0.9,
        importance=0.8,
        metadata={"writer": "merge-test"},
    )
    first_source = await memory_repository.add_memory_source(
        memory_id=created.memory_id,
        source_type="message",
        source_session_id="sess-1",
        source_message_id="msg-1",
        snippet="Please keep briefings concise.",
    )
    second_source = await memory_repository.add_memory_source(
        memory_id=created.memory_id,
        source_type="message",
        source_session_id="sess-1",
        source_message_id="msg-1",
        snippet="Please keep briefings concise.",
    )

    memories = await memory_repository.list_memories_by_kinds(
        kinds=(MemoryKind.communication_preference,),
        limit_per_kind=1,
    )
    sources = await memory_repository.list_sources(memory_id=created.memory_id)

    assert memories["communication_preference"][0].confidence == pytest.approx(0.9)
    assert memories["communication_preference"][0].importance == pytest.approx(0.8)
    assert memories["communication_preference"][0].reinforcement == pytest.approx(1.25)
    assert memories["communication_preference"][0].metadata_json == '{"writer": "merge-test"}'
    assert first_source.created is True
    assert second_source.created is False
    assert [source.source_message_id for source in sources if source.source_message_id] == ["msg-1"]


@pytest.mark.asyncio
async def test_merge_memory_rolls_back_reinforcement_when_source_write_fails(async_db):
    created = await memory_repository.create_memory(
        content="User prefers concise morning briefings.",
        kind=MemoryKind.communication_preference,
        summary="Prefers concise morning briefings",
        reinforcement=1.0,
    )

    with patch(
        "src.memory.repository.MemoryRepository._normalize_source_snippet",
        side_effect=RuntimeError("source write failed"),
    ):
        with pytest.raises(RuntimeError, match="source write failed"):
            await memory_repository.merge_memory(
                created.memory_id,
                reinforcement_delta=0.25,
                message_sources=[
                    {
                        "source_session_id": "sess-1",
                        "source_message_id": "msg-1",
                        "snippet": "Please keep briefings concise.",
                    }
                ],
            )

    memories = await memory_repository.list_memories_by_kinds(
        kinds=(MemoryKind.communication_preference,),
        limit_per_kind=1,
    )

    assert memories["communication_preference"][0].reinforcement == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_find_merge_candidate_allows_backfilling_entity_links(async_db):
    created = await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
    )
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas",
        entity_type=MemoryEntityType.project,
    )

    candidate = await memory_repository.find_merge_candidate(
        kind=MemoryKind.project,
        summary="Atlas launch",
        content="Atlas launch is the active release project.",
        project_entity_id=atlas.id,
    )

    assert candidate is not None
    assert candidate.id == created.memory_id


@pytest.mark.asyncio
async def test_find_merge_candidate_does_not_merge_unlinked_input_into_linked_memory(async_db):
    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas",
        entity_type=MemoryEntityType.project,
    )
    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        project_entity_id=atlas.id,
    )

    candidate = await memory_repository.find_merge_candidate(
        kind=MemoryKind.project,
        summary="Atlas launch",
        content="Atlas launch is the active release project.",
    )

    assert candidate is None
