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
