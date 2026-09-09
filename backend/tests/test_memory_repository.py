import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.db.models import (
    Memory,
    MemoryEdgeType,
    MemoryEntityType,
    MemoryKind,
    MemorySnapshotKind,
    MemorySource,
    MemoryStatus,
    MemoryTombstone,
)
from src.memory.repository import memory_repository
from src.memory.retrieval_planner import plan_memory_retrieval
from src.memory.snapshots import render_bounded_guardian_snapshot


_SCOPED_LEARNING_SCOPE = {
    "writer": "guardian_feedback",
    "memory_scope": "procedural_learning",
    "intervention_type": "advisory",
    "lesson_type": "delivery",
}


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
async def test_create_memory_persists_dedupes_and_skips_malformed_sources(async_db):
    result = await memory_repository.create_memory(
        content="User prefers concise status updates.",
        kind=MemoryKind.communication_preference,
        source_session_id="sess-1",
        source_message_id="msg-1",
        source_type="message",
        additional_sources=[
            {
                "source_type": "message",
                "source_session_id": "sess-1",
                "source_message_id": "msg-2",
                "snippet": "Keep the status updates concise.",
            },
            {
                "source_type": "message",
                "source_session_id": "sess-1",
                "source_message_id": "msg-2",
                "snippet": "Duplicate source should be ignored.",
            },
            {
                "source_type": "message",
                "snippet": "Malformed source should be ignored.",
            },
        ],
    )

    sources = await memory_repository.list_sources(memory_id=result.memory_id)

    assert result.message_source_count == 2
    assert [source.source_message_id for source in sources] == ["msg-1", "msg-2"]


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
async def test_create_edge_dedupes_identical_relationship(async_db):
    first = await memory_repository.create_memory(
        content="Prepare the Atlas investor brief.",
        kind=MemoryKind.commitment,
    )
    second = await memory_repository.create_memory(
        content="Investor brief belongs to Project Atlas.",
        kind=MemoryKind.project,
    )

    first_edge = await memory_repository.create_edge(
        from_memory_id=first.memory_id,
        to_memory_id=second.memory_id,
        edge_type=MemoryEdgeType.supports,
        metadata={"writer": "test"},
    )
    second_edge = await memory_repository.create_edge(
        from_memory_id=first.memory_id,
        to_memory_id=second.memory_id,
        edge_type=MemoryEdgeType.supports,
        metadata={"writer": "second-call"},
    )
    edges = await memory_repository.list_edges(
        from_memory_id=first.memory_id,
        to_memory_id=second.memory_id,
        edge_type=MemoryEdgeType.supports,
    )

    assert second_edge.id == first_edge.id
    assert len(edges) == 1


@pytest.mark.asyncio
async def test_edges_reject_and_hide_tombstoned_endpoints(async_db):
    first = await memory_repository.create_memory(
        content="Canonical edge source.",
        kind=MemoryKind.fact,
    )
    second = await memory_repository.create_memory(
        content="Canonical edge target.",
        kind=MemoryKind.fact,
    )
    await memory_repository.create_edge(
        from_memory_id=first.memory_id,
        to_memory_id=second.memory_id,
        edge_type=MemoryEdgeType.related,
    )

    await memory_repository.mark_memory_tombstoned(
        first.memory_id,
        actor="test-operator",
    )

    assert await memory_repository.list_edges(
        from_memory_id=first.memory_id,
        to_memory_id=second.memory_id,
    ) == []
    with pytest.raises(ValueError, match="canonical memories"):
        await memory_repository.create_edge(
            from_memory_id=first.memory_id,
            to_memory_id=second.memory_id,
            edge_type=MemoryEdgeType.related,
        )


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
@pytest.mark.parametrize(
    "marker_metadata,content,summary",
    [
        ({"archived_reason": "operator_delete_export"}, "Original", "Original"),
        (
            {"operator_control": {"delete_export_state": "canonical_memory_redacted"}},
            "Original",
            "Original",
        ),
        ({"delete_export_state": "canonical_memory_redacted"}, "Original", "Original"),
        (
            {"operator_control": {"last_action": "propagate_delete_export"}},
            "Original",
            "Original",
        ),
        (
            {"operator_control": {"last_action": "operator_delete_export"}},
            "Original",
            "Original",
        ),
        ({}, "[delete/export propagated by operator]", "Original"),
        ({}, "Original", "[delete/export propagated by operator]"),
    ],
)
async def test_sync_scoped_memory_suppresses_provider_echo_for_canonical_tombstone(
    async_db,
    marker_metadata,
    content,
    summary,
):
    metadata = {**_SCOPED_LEARNING_SCOPE, **marker_metadata}
    scope_key = memory_repository._scoped_memory_key(
        kind=MemoryKind.procedural,
        scope=_SCOPED_LEARNING_SCOPE,
    )
    async with async_db() as db:
        memory = Memory(
            content=content,
            summary=summary,
            kind=MemoryKind.procedural,
            category="preference",
            status=MemoryStatus.archived,
            confidence=0.0,
            importance=0.0,
            reinforcement=0.0,
            source_session_id="delete-session",
            scope_key=scope_key,
            metadata_json=json.dumps(metadata, sort_keys=True),
        )
        db.add(memory)
        await db.flush()
        db.add(
            MemorySource(
                memory_id=memory.id,
                source_type="session",
                source_session_id="delete-session",
                snippet="redacted source",
            )
        )
        await db.commit()

    before = await memory_repository.get_memory(memory.id)
    before_sources = await memory_repository.list_sources(memory_id=memory.id)
    assert before is not None

    result = await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope=_SCOPED_LEARNING_SCOPE,
        content="Provider echo must never revive this deleted learning.",
        summary="Provider echo",
        source_session_id="provider-echo-session",
        confidence=0.99,
        importance=0.99,
        reinforcement=9.0,
        metadata={"bias_value": "revive_deleted_learning"},
    )

    after = await memory_repository.get_memory(memory.id)
    after_sources = await memory_repository.list_sources(memory_id=memory.id)
    assert result is None
    assert after is not None
    for field in (
        "content",
        "summary",
        "status",
        "category",
        "confidence",
        "importance",
        "reinforcement",
        "source_session_id",
        "scope_key",
        "metadata_json",
        "created_at",
        "updated_at",
        "last_confirmed_at",
    ):
        assert getattr(after, field) == getattr(before, field)
    assert [
        (source.source_type, source.source_session_id, source.snippet)
        for source in after_sources
    ] == [
        (source.source_type, source.source_session_id, source.snippet)
        for source in before_sources
    ]


@pytest.mark.asyncio
async def test_sync_scoped_memory_suppresses_tombstone_before_legacy_scope_backfill(async_db):
    metadata = {
        **_SCOPED_LEARNING_SCOPE,
        "operator_control": {"delete_export_state": "canonical_memory_redacted"},
    }
    async with async_db() as db:
        memory = Memory(
            content="[delete/export propagated by operator]",
            summary="[delete/export propagated by operator]",
            kind=MemoryKind.procedural,
            category="preference",
            status=MemoryStatus.archived,
            scope_key=None,
            metadata_json=json.dumps(metadata, sort_keys=True),
        )
        db.add(memory)
        await db.commit()

    before = await memory_repository.get_memory(memory.id)
    assert before is not None
    result = await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope=_SCOPED_LEARNING_SCOPE,
        content="A legacy scope match must stay deleted.",
        summary="A legacy scope match must stay deleted.",
        metadata={"bias_value": "revive_deleted_learning"},
    )
    after = await memory_repository.get_memory(memory.id)

    assert result is None
    assert after is not None
    assert after.scope_key == before.scope_key
    assert before.scope_key is None
    assert after.content == before.content
    assert after.summary == before.summary
    assert after.metadata_json == before.metadata_json
    assert after.updated_at == before.updated_at


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [MemoryStatus.archived, MemoryStatus.superseded])
async def test_sync_scoped_memory_still_refreshes_ordinary_archived_or_superseded_memory(
    async_db,
    status,
):
    metadata = dict(_SCOPED_LEARNING_SCOPE)
    scope_key = memory_repository._scoped_memory_key(
        kind=MemoryKind.procedural,
        scope=_SCOPED_LEARNING_SCOPE,
    )
    async with async_db() as db:
        memory = Memory(
            content="Ordinary historical learning.",
            summary="Ordinary historical learning.",
            kind=MemoryKind.procedural,
            category="preference",
            status=status,
            scope_key=scope_key,
            metadata_json=json.dumps(metadata, sort_keys=True),
        )
        db.add(memory)
        await db.commit()

    result = await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope=_SCOPED_LEARNING_SCOPE,
        content="Fresh ordinary learning remains writable.",
        summary="Fresh ordinary learning remains writable.",
        metadata={"bias_value": "fresh"},
    )
    after = await memory_repository.get_memory(memory.id)

    assert result is not None
    assert result.memory_id == memory.id
    assert after is not None
    assert after.status is MemoryStatus.active
    assert after.content == "Fresh ordinary learning remains writable."
    assert after.summary == "Fresh ordinary learning remains writable."
    assert json.loads(after.metadata_json or "{}")["bias_value"] == "fresh"


@pytest.mark.asyncio
async def test_sync_scoped_memory_fails_safe_for_malformed_suppressed_metadata(async_db):
    scope_key = memory_repository._scoped_memory_key(
        kind=MemoryKind.procedural,
        scope=_SCOPED_LEARNING_SCOPE,
    )
    async with async_db() as db:
        memory = Memory(
            content="Unknown archived content.",
            summary="Unknown archived content.",
            kind=MemoryKind.procedural,
            category="preference",
            status=MemoryStatus.archived,
            scope_key=scope_key,
            metadata_json='{"operator_control":',
        )
        db.add(memory)
        await db.commit()

    before = await memory_repository.get_memory(memory.id)
    assert before is not None
    result = await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope=_SCOPED_LEARNING_SCOPE,
        content="Malformed metadata must not permit a possible tombstone revival.",
        summary="Possible tombstone revival",
        metadata={"bias_value": "unsafe"},
    )
    after = await memory_repository.get_memory(memory.id)

    assert result is None
    assert after is not None
    assert after.content == before.content
    assert after.summary == before.summary
    assert after.status is MemoryStatus.archived
    assert after.metadata_json == before.metadata_json
    assert after.updated_at == before.updated_at


@pytest.mark.asyncio
async def test_sync_scoped_memory_refreshes_active_malformed_metadata(async_db):
    scope_key = memory_repository._scoped_memory_key(
        kind=MemoryKind.procedural,
        scope=_SCOPED_LEARNING_SCOPE,
    )
    async with async_db() as db:
        memory = Memory(
            content="Active legacy learning.",
            summary="Active legacy learning.",
            kind=MemoryKind.procedural,
            category="preference",
            status=MemoryStatus.active,
            scope_key=scope_key,
            metadata_json='{"operator_control":',
        )
        db.add(memory)
        await db.commit()

    result = await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope=_SCOPED_LEARNING_SCOPE,
        content="Active malformed metadata remains refreshable.",
        summary="Active malformed metadata remains refreshable.",
        metadata={"bias_value": "refreshed"},
    )
    after = await memory_repository.get_memory(memory.id)

    assert result is not None
    assert result.memory_id == memory.id
    assert after is not None
    assert after.status is MemoryStatus.active
    assert after.content == "Active malformed metadata remains refreshable."
    assert after.summary == "Active malformed metadata remains refreshable."
    assert json.loads(after.metadata_json or "{}")["bias_value"] == "refreshed"


@pytest.mark.asyncio
async def test_cas_scoped_memory_suppresses_tombstone_winning_interleaving():
    original = Memory(
        content="Ordinary content before delete/export.",
        summary="Ordinary content before delete/export.",
        kind=MemoryKind.procedural,
        category="preference",
        status=MemoryStatus.archived,
        scope_key="scoped-memory-key",
        metadata_json="{}",
    )
    tombstone = Memory(
        content="[delete/export propagated by operator]",
        summary="[delete/export propagated by operator]",
        kind=MemoryKind.procedural,
        category="preference",
        status=MemoryStatus.archived,
        scope_key="scoped-memory-key",
        metadata_json=json.dumps(
            {
                "archived_reason": "operator_delete_export",
                "operator_control": {
                    "delete_export_state": "canonical_memory_redacted"
                },
            },
            sort_keys=True,
        ),
    )

    class _Result:
        def __init__(self, *, rowcount=None, row=None):
            self.rowcount = rowcount
            self._row = row

        def scalars(self):
            return self

        def first(self):
            return self._row

    class _InterleavingSession:
        def __init__(self):
            self.execute_calls = 0
            self.rollback_calls = 0

        async def execute(self, _statement):
            self.execute_calls += 1
            if self.execute_calls == 1:
                return _Result(rowcount=0)
            return _Result(row=tombstone)

        async def rollback(self):
            self.rollback_calls += 1

    db = _InterleavingSession()
    result = await memory_repository._cas_update_scoped_memory(
        db,
        original,
        values_builder=lambda _current: {
            "content": "This update must lose to delete/export.",
            "updated_at": datetime.now(timezone.utc),
        },
    )

    assert result is None
    assert db.execute_calls == 2
    assert db.rollback_calls == 1
    assert original.content == "Ordinary content before delete/export."
    assert tombstone.content == "[delete/export propagated by operator]"
    assert tombstone.status is MemoryStatus.archived


@pytest.mark.asyncio
async def test_sync_scoped_memory_integrity_error_recovery_preserves_tombstone():
    scope = dict(_SCOPED_LEARNING_SCOPE)
    scope_key = memory_repository._scoped_memory_key(
        kind=MemoryKind.procedural,
        scope=scope,
    )
    tombstone = Memory(
        content="[delete/export propagated by operator]",
        summary="[delete/export propagated by operator]",
        kind=MemoryKind.procedural,
        category="preference",
        status=MemoryStatus.archived,
        scope_key=scope_key,
        source_session_id="delete-session",
        metadata_json=json.dumps(
            {
                **scope,
                "archived_reason": "operator_delete_export",
                "operator_control": {
                    "last_action": "operator_delete_export",
                    "delete_export_state": "canonical_memory_redacted",
                },
            },
            sort_keys=True,
        ),
    )
    snapshot = {
        field: getattr(tombstone, field)
        for field in (
            "content",
            "summary",
            "status",
            "category",
            "confidence",
            "importance",
            "reinforcement",
            "scope_key",
            "source_session_id",
            "metadata_json",
            "created_at",
            "updated_at",
            "last_confirmed_at",
        )
    }

    class _Result:
        def __init__(self, *, first=None, all_rows=None):
            self._first = first
            self._all_rows = list(all_rows or [])

        def scalars(self):
            return self

        def first(self):
            return self._first

        def all(self):
            return self._all_rows

    class _RecoverySession:
        def __init__(self):
            self.execute_calls = 0
            self.flush_calls = 0
            self.rollback_calls = 0

        async def execute(self, _statement):
            self.execute_calls += 1
            if self.execute_calls == 1:
                return _Result(first=None)
            if self.execute_calls == 2:
                return _Result(all_rows=[])
            if self.execute_calls == 3:
                return _Result(first=None)
            return _Result(first=tombstone)

        def add(self, _memory):
            return None

        async def flush(self):
            self.flush_calls += 1
            raise IntegrityError(
                "forced unique scope conflict",
                {},
                RuntimeError("forced unique scope conflict"),
            )

        async def rollback(self):
            self.rollback_calls += 1

    db = _RecoverySession()

    @asynccontextmanager
    async def fake_get_session():
        yield db

    with patch("src.memory.repository.get_session", fake_get_session):
        result = await memory_repository.sync_scoped_memory(
            kind=MemoryKind.procedural,
            scope=scope,
            content="Provider echo must not overwrite the recovered tombstone.",
            summary="Provider echo",
            source_session_id="provider-echo-session",
            metadata={"unsafe": "revival"},
        )

    assert result is None
    assert db.flush_calls == 1
    assert db.rollback_calls == 1
    assert db.execute_calls == 4
    assert {
        field: getattr(tombstone, field)
        for field in snapshot
    } == snapshot


@pytest.mark.asyncio
async def test_sync_scoped_memory_empty_echo_preserves_metadata_only_tombstone():
    scope = dict(_SCOPED_LEARNING_SCOPE)
    scope_key = memory_repository._scoped_memory_key(
        kind=MemoryKind.procedural,
        scope=scope,
    )
    tombstone = Memory(
        content="Legacy content already deleted.",
        summary="Legacy summary already deleted.",
        kind=MemoryKind.procedural,
        category="preference",
        status=MemoryStatus.archived,
        scope_key=scope_key,
        metadata_json=json.dumps(
            {**scope, "archived_reason": "operator_delete_export"},
            sort_keys=True,
        ),
    )
    snapshot = {
        field: getattr(tombstone, field)
        for field in (
            "content",
            "summary",
            "status",
            "scope_key",
            "metadata_json",
            "created_at",
            "updated_at",
        )
    }

    class _Result:
        def scalars(self):
            return self

        def first(self):
            return tombstone

    class _Session:
        def __init__(self):
            self.execute_calls = 0
            self.flush_calls = 0

        async def execute(self, _statement):
            self.execute_calls += 1
            return _Result()

        def add(self, _memory):
            raise AssertionError("tombstone must not be rewritten")

        async def flush(self):
            self.flush_calls += 1

    db = _Session()

    @asynccontextmanager
    async def fake_get_session():
        yield db

    with patch("src.memory.repository.get_session", fake_get_session):
        result = await memory_repository.sync_scoped_memory(
            kind=MemoryKind.procedural,
            scope=scope,
            content=None,
            summary=None,
            metadata={"unsafe": "metadata overwrite"},
        )

    assert result is None
    assert db.execute_calls == 1
    assert db.flush_calls == 0
    assert {
        field: getattr(tombstone, field)
        for field in snapshot
    } == snapshot


@pytest.mark.asyncio
async def test_list_memories_for_scope_filters_procedural_memories(async_db):
    await memory_repository.sync_scoped_memory(
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
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "alert",
            "lesson_type": "delivery",
        },
        content="For alert interventions, reduce direct interruptions after recent negative or failed outcomes.",
        summary="For alert interventions, reduce direct interruptions after recent negative or failed outcomes.",
        metadata={"bias_value": "reduce_interruptions"},
    )

    advisory_memories = await memory_repository.list_memories_for_scope(
        kind=MemoryKind.procedural,
        scope={
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
        },
        limit=10,
    )

    assert [memory.content for memory in advisory_memories] == [
        "For advisory interventions, reduce direct interruptions after recent negative or failed outcomes."
    ]


@pytest.mark.asyncio
async def test_list_memories_for_scope_skips_non_object_or_invalid_metadata_payloads(async_db):
    async with async_db() as db:
        db.add(
            Memory(
                content="Legacy malformed metadata payload",
                kind=MemoryKind.procedural,
                category="preference",
                metadata_json='["not", "a", "dict"]',
                status="active",
                importance=0.95,
            )
        )
        db.add(
            Memory(
                content="Legacy invalid metadata payload",
                kind=MemoryKind.procedural,
                category="preference",
                metadata_json='{"writer": "guardian_feedback"',
                status="active",
                importance=0.96,
            )
        )
        await db.commit()

    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
            "lesson_type": "channel",
        },
        content="For advisory interventions, async native notification is usually tolerated better than browser interruption.",
        summary="For advisory interventions, async native notification is usually tolerated better than browser interruption.",
        metadata={"bias_value": "prefer_native_notification"},
    )

    advisory_memories = await memory_repository.list_memories_for_scope(
        kind=MemoryKind.procedural,
        scope={
            "writer": "guardian_feedback",
            "memory_scope": "procedural_learning",
            "intervention_type": "advisory",
        },
        limit=10,
    )

    assert [memory.content for memory in advisory_memories] == [
        "For advisory interventions, async native notification is usually tolerated better than browser interruption."
    ]


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


@pytest.mark.asyncio
async def test_tombstone_reconcile_redacts_restored_row_before_local_reindex(async_db):
    created = await memory_repository.create_memory(
        content="Private restore poison must never return.",
        summary="Private restore poison",
        kind=MemoryKind.fact,
        source_session_id="owner-session",
    )
    first = await memory_repository.mark_memory_tombstoned(
        created.memory_id,
        actor="operator-1",
        reason="user deletion",
        metadata_updates={
            "operator_control": {
                "last_action": "propagate_delete_export",
                "delete_export_state": "canonical_memory_redacted",
            },
        },
    )
    second = await memory_repository.mark_memory_tombstoned(
        created.memory_id,
        actor="operator-2",
        reason="retry must be idempotent",
    )

    assert first.created is True
    assert second.created is False
    assert second.tombstone.actor == "operator-1"
    assert second.tombstone.reason == "user deletion"
    assert "content" not in MemoryTombstone.__table__.columns
    with pytest.raises(ValueError, match="cannot reactivate canonical memory"):
        await memory_repository.update_memory_control_metadata(
            created.memory_id,
            status=MemoryStatus.active,
        )

    # Simulate a stale backup restoring the row while the local deletion
    # ledger remains durable.
    async with async_db() as db:
        await db.execute(
            Memory.__table__.update()
            .where(Memory.id == created.memory_id)
            .values(
                content="Private restore poison must never return.",
                summary="Private restore poison",
                status=MemoryStatus.active,
                confidence=0.9,
                importance=0.9,
                reinforcement=1.0,
                metadata_json='{"provenance": {"kind": "stale_backup"}}',
            )
        )

    receipt = await memory_repository.reconcile_memory_tombstones()
    assert receipt == {
        "schema_version": "guardian.memory_tombstone.v1",
        "status": "ready",
        "checked_count": 1,
        "reapplied_count": 1,
        "missing_memory_count": 0,
    }
    restored = await memory_repository.get_memory(created.memory_id)
    assert restored is not None
    assert restored.status is MemoryStatus.archived
    assert restored.content == "[delete/export propagated by operator]"
    assert restored.summary == "[delete/export propagated by operator]"
    assert json.loads(restored.metadata_json or "{}")["canonical_tombstone_id"] == second.tombstone.id

    reindex_rows = await memory_repository.list_memories_for_reindex()
    assert all(memory.id != created.memory_id for memory in reindex_rows)
    tombstone = await memory_repository.get_memory_tombstone(created.memory_id)
    assert tombstone is not None
    assert tombstone.memory_id == created.memory_id


@pytest.mark.asyncio
async def test_concurrent_tombstone_requests_share_one_authoritative_row(async_db):
    created = await memory_repository.create_memory(
        content="Concurrent delete target.",
        kind=MemoryKind.fact,
    )
    results = await asyncio.gather(
        *(
            memory_repository.mark_memory_tombstoned(
                created.memory_id,
                actor=f"operator-{index}",
                reason=f"concurrent-{index}",
            )
            for index in range(2)
        )
    )

    assert sorted(result.created for result in results) == [False, True]
    assert len({result.tombstone.id for result in results}) == 1
    stored = await memory_repository.get_memory_tombstone(created.memory_id)
    assert stored is not None
    assert stored.actor == "operator-0"
    assert stored.reason == "concurrent-0"


@pytest.mark.asyncio
async def test_restored_tombstone_blocks_planner_mutations_and_source_payloads(async_db):
    created = await memory_repository.create_memory(
        content="Private restore poison must never influence a plan.",
        summary="Private restore poison",
        kind=MemoryKind.preference,
        source_session_id="private-session",
        source_message_id="private-message",
        source_snippet="The erased private preference payload.",
    )
    await memory_repository.mark_memory_tombstoned(
        created.memory_id,
        actor="operator",
        reason="privacy request",
    )

    async with async_db() as db:
        await db.execute(
            Memory.__table__.update()
            .where(Memory.id == created.memory_id)
            .values(
                content="Private restore poison must never influence a plan.",
                summary="Private restore poison",
                status=MemoryStatus.active,
                metadata_json="{}",
            )
        )
        await db.execute(
            MemorySource.__table__.update()
            .where(MemorySource.memory_id == created.memory_id)
            .values(snippet="Restored private source payload.")
        )

    sources = await memory_repository.list_sources(memory_id=created.memory_id)
    assert sources[0].snippet is None

    plan = await plan_memory_retrieval(query="")
    assert "Private restore poison" not in plan.semantic_context
    assert plan.degraded is False

    snapshot, _source_hash = await render_bounded_guardian_snapshot(
        soul_context="## Identity\n- Operator\n",
    )
    assert "Private restore poison" not in snapshot

    with pytest.raises(ValueError, match="cannot reactivate canonical memory"):
        await memory_repository.update_memory_control_metadata(
            created.memory_id,
            status=MemoryStatus.active,
        )
    with pytest.raises(ValueError, match="cannot merge canonical memory"):
        await memory_repository.merge_memory(
            created.memory_id,
            summary="A revived private preference",
        )

    await memory_repository.reconcile_memory_tombstones()
    async with async_db() as db:
        source = (
            await db.execute(
                select(MemorySource).where(MemorySource.memory_id == created.memory_id)
            )
        ).scalars().first()
        assert source is not None
        assert source.snippet is None
