"""Real local canonical-memory recovery checks.

These tests use a file-backed SQLite database and the real repository methods.
No model, embedding, provider, or network seam is involved.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select
from sqlmodel.orm.session import Session

from config.settings import settings
from src.api import memory as memory_api
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.db.models import (
    AuditEvent,
    Memory,
    MemoryCategory,
    MemoryEntity,
    MemoryKind,
    MemorySnapshot,
    MemorySource,
    MemoryStatus,
    MemoryTombstone,
    Session as SessionModel,
)
from src.memory.repository import memory_repository
from src.memory.pipeline.merge import EmbeddingWriteResult, persist_extracted_memories
from src.memory.types import ConsolidatedMemoryItem
from src.auth.service import test_bypass_operator as make_test_bypass_operator


@contextmanager
def _runtime_operator(operator, *, revoked: bool | None = None):
    principal = operator.principal
    if revoked is not None:
        principal = replace(principal, revoked=revoked)
    tokens = set_runtime_context(
        operator.session_id,
        "off",
        trust_principal=principal,
    )
    try:
        yield
    finally:
        reset_runtime_context(tokens)


class _SyncAsyncSession:
    """Small async-shaped adapter around a real synchronous SQLite session.

    The project fixture's aiosqlite worker can stall in constrained hosts.  A
    synchronous SQLModel session still exercises the repository's SQL/CAS and
    tombstone code against a real file-backed SQLite database.
    """

    def __init__(self, session: Session):
        self._session = session

    async def execute(self, statement, *args, **kwargs):
        return self._session.execute(statement, *args, **kwargs)

    async def flush(self):
        self._session.flush()

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    def add(self, value):
        self._session.add(value)

    def expunge(self, value):
        self._session.expunge(value)


@pytest.fixture
def local_memory_db(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    database_path = tmp_path / "canonical-memory.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    factory = sessionmaker(engine, class_=Session, expire_on_commit=False)
    SQLModel.metadata.create_all(
        engine,
        tables=[
            MemoryEntity.__table__,
            Memory.__table__,
            MemorySource.__table__,
            MemoryTombstone.__table__,
            MemorySnapshot.__table__,
            AuditEvent.__table__,
            SessionModel.__table__,
        ],
    )

    @asynccontextmanager
    async def get_session():
        session = factory()
        wrapped = _SyncAsyncSession(session)
        try:
            yield wrapped
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    with (
        patch("src.memory.repository.get_session", get_session),
        patch("src.audit.repository.get_session", get_session),
    ):
        yield get_session, database_path
    engine.dispose()


async def _memory_row(get_session, memory_id: str) -> Memory | None:
    async with get_session() as db:
        return (await db.execute(select(Memory).where(Memory.id == memory_id))).scalars().first()


@pytest.mark.asyncio
async def test_export_rebuild_and_restore_keep_current_tombstone_authoritative(local_memory_db):
    get_session, database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    created = await memory_repository.create_memory(
        content="A private recovery fact that must survive a missing-row restore.",
        source_session_id=owner_session,
        source_type="message",
        source_message_id="message-recovery-1",
        source_snippet="private recovery fact",
        metadata={"privacy_boundary": "private", "provenance": {"kind": "inferred"}},
    )
    with _runtime_operator(operator):
        export = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    assert export["status"] == "ready"
    assert created.memory_id in export["memory_ids"]
    assert export["memories"][0]["content"].startswith("A private recovery fact")
    assert export["artifact_path"].startswith("artifacts/memory-recovery/")
    export_file = Path(settings.workspace_dir) / export["artifact_path"]
    assert export_file.is_file()
    assert json.loads(export_file.read_text())["export_hash"] == export["export_hash"]

    deleted = await memory_repository.mark_memory_tombstoned(
        created.memory_id,
        actor="operator:test",
        reason="operator requested deletion",
    )
    assert deleted.created is True
    with _runtime_operator(operator):
        rebuild_after_delete = await memory_repository.rebuild_canonical_memory_index(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    assert rebuild_after_delete["status"] == "ready"
    assert created.memory_id not in rebuild_after_delete["memory_ids"]
    assert rebuild_after_delete["semantic_index_status"] == "unavailable"
    index_file = Path(settings.workspace_dir) / rebuild_after_delete["artifact_path"]
    assert created.memory_id not in index_file.read_text()

    # Simulate an older snapshot restore while the current deletion ledger and
    # redacted row remain in place.  Restore must not revive it.
    with _runtime_operator(operator):
        restored_deleted = await memory_repository.restore_canonical_memory_state(
            export,
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    assert restored_deleted["tombstone_suppressed_memory_ids"] == [created.memory_id]
    assert await memory_repository.get_memory(created.memory_id) is None

    async with get_session() as db:
        tombstone = (
            await db.execute(
                select(MemoryTombstone).where(MemoryTombstone.memory_id == created.memory_id)
            )
        ).scalars().one()
    assert tombstone.reason == "operator requested deletion"
    deleted_sources = await memory_repository.list_sources(memory_id=created.memory_id)
    assert deleted_sources
    assert all(source.snippet is None for source in deleted_sources)
    redacted = await _memory_row(get_session, created.memory_id)
    assert redacted is not None
    assert redacted.content == "[delete/export propagated by operator]"
    assert "A private recovery fact" not in redacted.content

    # The same archive can repair a missing non-deleted row, preserving the
    # original canonical identity and source linkage.
    live = await memory_repository.create_memory(
        content="A live recovery fact for readback.",
        source_session_id=owner_session,
        source_type="session",
        source_snippet="live recovery fact",
    )
    with _runtime_operator(operator):
        live_export = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    corrupt_export = json.loads(json.dumps(live_export))
    corrupt_export["memories"][0]["content"] = "tampered archive content"
    with pytest.raises(ValueError, match="archive hash mismatch"):
        with _runtime_operator(operator):
            await memory_repository.restore_canonical_memory_state(
                corrupt_export,
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id=owner_session,
            )
    stale_restore_connection = sqlite3.connect(database_path)
    try:
        stale_restore_connection.execute("PRAGMA foreign_keys=OFF")
        stale_restore_connection.execute(
            "DELETE FROM memory_sources WHERE memory_id = ?", (live.memory_id,)
        )
        stale_restore_connection.execute("DELETE FROM memories WHERE id = ?", (live.memory_id,))
        stale_restore_connection.commit()
    finally:
        stale_restore_connection.close()
    with _runtime_operator(operator):
        live_restored = await memory_repository.restore_canonical_memory_state(
            live_export,
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    assert live.memory_id in live_restored["restored_memory_ids"]
    assert (await memory_repository.get_memory(live.memory_id)).content == "A live recovery fact for readback."
    restored_sources = await memory_repository.list_sources(memory_id=live.memory_id)
    assert {source.id for source in restored_sources} == {
        source["id"] for source in live_export["memories"][-1]["sources"]
    }
    with _runtime_operator(operator):
        assert live.memory_id in (await memory_repository.rebuild_canonical_memory_index(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        ))["memory_ids"]
        restarted_repository = type(memory_repository)()
        restarted_index = await restarted_repository.rebuild_canonical_memory_index(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    assert live.memory_id in restarted_index["memory_ids"]


@pytest.mark.asyncio
async def test_recovery_authority_and_archive_validation_fail_closed_before_writes(local_memory_db):
    _get_session, _database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    with pytest.raises(PermissionError, match="current authenticated operator runtime"):
        await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )

    archive = {
        "schema_version": "guardian.memory.export.v1",
        "owner_session_id": owner_session,
        "memories": [],
        "tombstones": [],
    }
    with _runtime_operator(operator):
        with pytest.raises(PermissionError, match="does not match the authenticated session"):
            await memory_repository.export_canonical_memory_state(
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id="forged-session",
            )
        with pytest.raises(PermissionError, match="source role"):
            await memory_repository.export_canonical_memory_state(
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id=owner_session,
                source_role="assistant",
            )
        with pytest.raises(PermissionError, match="archive owner"):
            await memory_repository.restore_canonical_memory_state(
                {**archive, "owner_session_id": "forged-owner"},
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id=owner_session,
            )
        with pytest.raises(ValueError, match="unknown memory restore archive version"):
            await memory_repository.restore_canonical_memory_state(
                {**archive, "schema_version": "guardian.memory.export.v0"},
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id=owner_session,
            )
        with pytest.raises(ValueError, match="export hash is required"):
            await memory_repository.restore_canonical_memory_state(
                archive,
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id=owner_session,
            )

    with _runtime_operator(operator, revoked=True):
        with pytest.raises(PermissionError, match="current authenticated operator runtime"):
            await memory_repository.export_canonical_memory_state(
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id=owner_session,
            )

    request = SimpleNamespace(state=SimpleNamespace(operator=operator))
    with pytest.raises(HTTPException) as owner_error:
        memory_api.authenticated_memory_context(
            request,
            requested_owner_session_id="forged-owner",
        )
    assert owner_error.value.status_code == 403
    with pytest.raises(HTTPException) as role_error:
        memory_api.authenticated_memory_context(
            request,
            requested_source_role="assistant",
        )
    assert role_error.value.status_code == 403

    await memory_repository.create_memory(
        content="Route authority must use the middleware operator.",
        source_session_id=operator.session_id,
        source_type="operator",
    )
    route_result = await memory_api.export_memory_recovery_route(
        request,
        memory_api.MemoryRecoveryRequest(
            owner_session_id=operator.session_id,
            actor="attacker",
        ),
    )
    assert route_result["status"] == "ready"
    assert route_result["provenance"]["actor"] == operator.principal.principal_id
    assert route_result["audit_event_id"]


@pytest.mark.asyncio
async def test_concurrent_merge_cannot_undo_delete_and_restart_reconciles(local_memory_db):
    get_session, _database_path = local_memory_db
    owner_session = "owner-concurrency-session"
    created = await memory_repository.create_memory(
        content="Concurrent deletion must win over merge.",
        source_session_id=owner_session,
        source_type="session",
        source_snippet="concurrency candidate",
    )
    second_repository = type(memory_repository)()

    async def delete_memory():
        return await memory_repository.mark_memory_tombstoned(
            created.memory_id,
            actor="operator:test",
            reason="concurrency delete",
        )

    async def merge_memory():
        try:
            await second_repository.merge_memory(
                created.memory_id,
                summary="forged merge echo",
                confidence=1.0,
                importance=1.0,
                metadata={"echo": "must not revive"},
            )
        except ValueError:
            return "rejected"
        return "merged"

    delete_result, merge_result = await asyncio.wait_for(
        asyncio.gather(delete_memory(), merge_memory()),
        timeout=20,
    )
    assert delete_result.created is True
    assert merge_result in {"rejected", "merged"}
    assert await memory_repository.get_memory(created.memory_id) is None
    tombstone = await memory_repository.get_memory_tombstone(created.memory_id)
    assert tombstone is not None
    assert tombstone.reason == "concurrency delete"
    assert await memory_repository.list_memories_for_reindex() == []


@pytest.mark.asyncio
async def test_inferred_extraction_cannot_claim_operator_provenance(local_memory_db):
    _get_session, _database_path = local_memory_db

    async def link_resolver(_item):
        return SimpleNamespace(subject_entity_id=None, project_entity_id=None)

    item = ConsolidatedMemoryItem(
        text="An inferred memory must remain an untrusted proposal.",
        kind=MemoryKind.fact,
        category=MemoryCategory.fact,
        metadata={
            "operator_control": {"last_action": "attacker_override"},
            "provenance": {"kind": "operator_correction", "actor": "attacker"},
            "privacy_boundary": "sensitive",
        },
    )
    async def no_embedding(**_kwargs):
        return EmbeddingWriteResult()

    with patch("src.memory.pipeline.merge._write_embedding", no_embedding):
        result = await persist_extracted_memories(
            extracted_memories=(item,),
            session_id="inferred-session",
            source_messages=(),
            vector_writer=lambda **_kwargs: "",
            link_resolver=link_resolver,
        )
    assert result.created_count == 1
    memories = await memory_repository.list_memories(limit=4)
    assert len(memories) == 1
    metadata = json.loads(memories[0].metadata_json or "{}")
    assert metadata["source_role"] == "inferred"
    assert metadata["provenance"]["kind"] == "inferred_extraction"
    assert "operator_control" not in metadata
