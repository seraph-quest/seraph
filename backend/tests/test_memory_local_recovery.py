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
from unittest.mock import AsyncMock, patch

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
from src.memory.repository import (
    _EMPTY_TOMBSTONE_REVISION,
    _memory_export_artifact_payload,
    _memory_export_integrity_payload,
    _recovery_json_hash,
    memory_repository,
)
from src.memory.pipeline.merge import EmbeddingWriteResult, persist_extracted_memories
from src.memory.types import ConsolidatedMemoryItem
from src.memory import control as memory_control
from src.memory.control import memory_recovery_status
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
    corrupt_envelope = json.loads(json.dumps(live_export))
    corrupt_envelope["provenance"]["actor"] = "forged-operator"
    with pytest.raises(ValueError, match="archive hash mismatch"):
        with _runtime_operator(operator):
            await memory_repository.restore_canonical_memory_state(
                corrupt_envelope,
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id=owner_session,
            )
    corrupt_artifact = json.loads(json.dumps(live_export))
    corrupt_artifact["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        with _runtime_operator(operator):
            await memory_repository.restore_canonical_memory_state(
                corrupt_artifact,
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
async def test_live_control_aliases_reject_forged_owner_before_mutation():
    operator = make_test_bypass_operator()
    request = SimpleNamespace(state=SimpleNamespace(operator=operator))
    forged = memory_api.MemoryLiveControlActionRequest(
        action="propagate_delete_export",
        acknowledged=True,
        owner_session_id="forged-owner-session",
        memory_id="memory-under-another-owner",
    )

    with patch("src.api.memory.apply_memory_live_control_action", new_callable=AsyncMock) as canonical_action:
        with pytest.raises(HTTPException) as canonical_error:
            await memory_api.post_memory_live_control_action(request, forged)
    assert canonical_error.value.status_code == 403
    canonical_action.assert_not_awaited()

    from src.api import operator as operator_api

    operator_request = operator_api.MemoryLiveControlActionRequest(
        action="propagate_delete_export",
        acknowledged=True,
        owner_session_id="forged-owner-session",
        memory_id="memory-under-another-owner",
    )
    with patch("src.api.operator.apply_memory_live_control_action", new_callable=AsyncMock) as alias_action:
        with pytest.raises(HTTPException) as alias_error:
            await operator_api.post_operator_memory_live_control_action(request, operator_request)
    assert alias_error.value.status_code == 403
    alias_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_control_reads_always_scope_to_authenticated_owner():
    operator = make_test_bypass_operator()
    request = SimpleNamespace(state=SimpleNamespace(operator=operator))

    with patch(
        "src.api.memory.get_memory_live_controls_snapshot",
        new_callable=AsyncMock,
        return_value={},
    ) as canonical_snapshot:
        await memory_api.get_memory_live_controls(request)
    canonical_snapshot.assert_awaited_once_with(
        limit=8,
        owner_session_id=operator.session_id,
    )

    from src.api import operator as operator_api

    with patch(
        "src.api.operator.get_memory_live_controls_snapshot",
        new_callable=AsyncMock,
        return_value={},
    ) as operator_snapshot:
        await operator_api.get_operator_memory_live_controls(request, owner_session_id=None)
    operator_snapshot.assert_awaited_once_with(
        limit=8,
        owner_session_id=operator.session_id,
    )

    with patch(
        "src.api.memory.list_memory_audit_receipts",
        new_callable=AsyncMock,
        return_value={},
    ) as audit_receipts:
        await memory_api.get_memory_audit(request, limit=12)
    audit_receipts.assert_awaited_once_with(
        memory_id=None,
        limit=12,
        owner_session_id=operator.session_id,
    )


@pytest.mark.asyncio
async def test_live_control_rejects_memory_bound_to_another_owner(local_memory_db):
    _get_session, _database_path = local_memory_db
    operator = make_test_bypass_operator()
    other_owner_memory = await memory_repository.create_memory(
        content="A different owner memory must not be changed.",
        source_session_id="another-owner-session",
        source_type="operator",
    )
    request = SimpleNamespace(state=SimpleNamespace(operator=operator))
    action = memory_api.MemoryLiveControlActionRequest(
        action="propagate_delete_export",
        acknowledged=True,
        memory_id=other_owner_memory.memory_id,
    )

    with pytest.raises(HTTPException) as error:
        await memory_api.post_memory_live_control_action(request, action)
    assert error.value.status_code == 403
    assert await memory_repository.get_memory(other_owner_memory.memory_id) is not None

    unbound_memory = await memory_repository.create_memory(
        content="An unbound memory must not be changed through an owner route.",
        source_type="operator",
    )
    unbound_action = memory_api.MemoryLiveControlActionRequest(
        action="propagate_delete_export",
        acknowledged=True,
        memory_id=unbound_memory.memory_id,
    )
    with pytest.raises(HTTPException) as unbound_error:
        await memory_api.post_memory_live_control_action(request, unbound_action)
    assert unbound_error.value.status_code == 403
    assert unbound_error.value.detail["code"] == "memory_owner_session_unbound"
    assert await memory_repository.get_memory(unbound_memory.memory_id) is not None


@pytest.mark.asyncio
async def test_direct_memory_live_control_rejects_other_owner_before_mutation(local_memory_db):
    _get_session, _database_path = local_memory_db
    operator = make_test_bypass_operator()
    other_memory = await memory_repository.create_memory(
        content="OTHER_OWNER_SECRET direct-control content",
        source_session_id="other-owner-session",
        source_type="operator",
    )

    with patch.object(
        memory_control.memory_repository,
        "mark_memory_tombstoned",
        new=AsyncMock(side_effect=AssertionError("cross-owner tombstone was attempted")),
    ) as tombstone:
        with pytest.raises(PermissionError, match="another owner session"):
            await memory_control.apply_memory_live_control_action(
                action="propagate_delete_export",
                acknowledged=True,
                actor=operator.principal.principal_id,
                owner_session_id=operator.session_id,
                memory_id=other_memory.memory_id,
                privacy_boundary="operator_visible",
            )

    tombstone.assert_not_awaited()
    stored = await memory_repository.get_memory(other_memory.memory_id)
    assert stored is not None
    assert stored.content == "OTHER_OWNER_SECRET direct-control content"


@pytest.mark.asyncio
async def test_owner_memory_surfaces_redact_other_owner_reconciliation_content(local_memory_db):
    get_session, _database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    await memory_repository.create_memory(
        content="OTHER_OWNER_SECRET should never cross the memory owner boundary.",
        summary="OTHER_OWNER_SECRET reconciliation summary",
        status=MemoryStatus.archived,
        source_session_id="other-owner-session",
        source_type="operator",
        metadata={"archived_reason": "OTHER_OWNER_SECRET metadata reason"},
    )
    await memory_repository.create_memory(
        content="Owner live-control candidate.",
        source_session_id=owner_session,
        source_type="operator",
    )
    request = SimpleNamespace(state=SimpleNamespace(operator=operator))

    with patch("src.memory.decay.get_session", get_session):
        snapshot = await memory_control.get_memory_live_controls_snapshot(
            limit=8,
            owner_session_id=owner_session,
        )
        providers = await memory_api.list_memory_providers_route(request)

    serialized = json.dumps({"snapshot": snapshot, "providers": providers}, sort_keys=True)
    assert "OTHER_OWNER_SECRET" not in serialized
    snapshot_reconciliation = snapshot["reconciliation"]
    provider_reconciliation = providers["canonical_memory_reconciliation"]
    assert snapshot_reconciliation["scope"] == "owner"
    assert snapshot_reconciliation["owner_session_id"] == owner_session
    assert snapshot_reconciliation["content_free"] is True
    assert provider_reconciliation["scope"] == "owner"
    assert provider_reconciliation["owner_session_id"] == owner_session
    assert provider_reconciliation["content_free"] is True
    assert provider_reconciliation["recent_conflicts"] == []
    assert provider_reconciliation["recent_archivals"] == []


@pytest.mark.asyncio
async def test_restore_rejects_record_without_owner_session(local_memory_db):
    _get_session, _database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    created = await memory_repository.create_memory(
        content="Owner-bound restore record.",
        source_session_id=owner_session,
        source_type="operator",
    )
    with _runtime_operator(operator):
        archive = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    archive = json.loads(json.dumps(archive))
    archive["memories"][0].pop("source_session_id", None)
    archive["export_hash"] = _recovery_json_hash(_memory_export_integrity_payload(archive))
    archive["artifact_path"] = (
        f"artifacts/memory-recovery/export-{archive['export_hash'][:24]}.json"
    )
    archive["artifact_sha256"] = _recovery_json_hash(_memory_export_artifact_payload(archive))
    with _runtime_operator(operator):
        with pytest.raises(PermissionError, match="requires an owner session"):
            await memory_repository.restore_canonical_memory_state(
                archive,
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id=owner_session,
            )
    assert await memory_repository.get_memory(created.memory_id) is not None


@pytest.mark.asyncio
async def test_restore_reapplies_archived_tombstone_before_writes(local_memory_db):
    get_session, database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    created = await memory_repository.create_memory(
        content="This content must remain deleted after a stale database restore.",
        source_session_id=owner_session,
        source_type="operator",
        source_snippet="secret source snippet",
    )
    await memory_repository.mark_memory_tombstoned(
        created.memory_id,
        actor=operator.principal.principal_id,
        reason="archive deletion authority",
    )
    with _runtime_operator(operator):
        archive = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    assert archive["tombstones"]
    assert created.memory_id not in archive["memory_ids"]

    stale_connection = sqlite3.connect(database_path)
    try:
        stale_connection.execute("DELETE FROM memory_tombstones WHERE memory_id = ?", (created.memory_id,))
        stale_connection.execute(
            "UPDATE memories SET content = ?, summary = ?, status = ?, confidence = ?, importance = ?, "
            "reinforcement = ?, metadata_json = ? WHERE id = ?",
            (
                "stale restored content",
                "stale restored content",
                "active",
                0.9,
                0.9,
                1.0,
                "{}",
                created.memory_id,
            ),
        )
        stale_connection.commit()
    finally:
        stale_connection.close()

    with _runtime_operator(operator):
        restored = await memory_repository.restore_canonical_memory_state(
            archive,
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    assert restored["applied_archive_tombstone_ids"]
    assert await memory_repository.get_memory(created.memory_id) is None
    assert await memory_repository.get_memory_tombstone(created.memory_id) is not None
    assert await memory_repository.list_memories_for_reindex() == []


@pytest.mark.asyncio
async def test_export_scopes_tombstones_to_authenticated_owner(local_memory_db):
    _get_session, _database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    owner_memory = await memory_repository.create_memory(
        content="Owner export row.",
        source_session_id=owner_session,
        source_type="operator",
    )
    other_memory = await memory_repository.create_memory(
        content="Other owner export row.",
        source_session_id="other-owner-session",
        source_type="operator",
    )
    await memory_repository.mark_memory_tombstoned(
        other_memory.memory_id,
        actor="operator:other",
        reason="other owner deletion",
    )
    with _runtime_operator(operator):
        archive = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    assert archive["memory_ids"] == [owner_memory.memory_id]
    assert archive["tombstone_ids"] == []
    assert other_memory.memory_id not in archive["memories"]
    assert all(
        tombstone["memory_id"] != other_memory.memory_id
        for tombstone in archive["tombstones"]
    )


@pytest.mark.asyncio
async def test_export_rejects_cross_owner_source_provenance(local_memory_db):
    _get_session, _database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    owner_memory = await memory_repository.create_memory(
        content="Owner recovery memory.",
        source_session_id=owner_session,
        source_type="operator",
    )
    await memory_repository.add_memory_source(
        memory_id=owner_memory.memory_id,
        source_type="message",
        source_session_id="other-owner-session",
        source_message_id="other-owner-message",
        snippet="OTHER_OWNER_SECRET source provenance",
    )

    with _runtime_operator(operator):
        with pytest.raises(PermissionError, match="source provenance"):
            await memory_repository.export_canonical_memory_state(
                actor=operator.principal.principal_id,
                owner_session_id=owner_session,
                authenticated_session_id=owner_session,
            )


@pytest.mark.asyncio
async def test_recovery_receipts_scope_reconciliation_and_revision_to_owner(local_memory_db):
    _get_session, _database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    other_memory = await memory_repository.create_memory(
        content="Other owner's tombstone must not appear in recovery receipts.",
        source_session_id="other-owner-session",
        source_type="operator",
    )
    await memory_repository.mark_memory_tombstoned(
        other_memory.memory_id,
        actor="operator:other",
        reason="other owner deletion",
    )

    with _runtime_operator(operator):
        status = await memory_recovery_status(
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
            actor=operator.principal.principal_id,
        )
        export = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
        restored = await memory_repository.restore_canonical_memory_state(
            export,
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )

    for receipt in (status["reconciliation"], export["reconciliation"], restored["reconciliation"]):
        assert receipt["checked_count"] == 0
        assert receipt["reapplied_count"] == 0
        assert receipt["missing_memory_count"] == 0
    assert status["canonical_tombstone_revision"] == _EMPTY_TOMBSTONE_REVISION
    assert export["canonical_tombstone_revision"] == _EMPTY_TOMBSTONE_REVISION
    assert restored["current_tombstone_revision"] == _EMPTY_TOMBSTONE_REVISION


@pytest.mark.parametrize(
    "incoming_updated_at",
    [
        "2030-01-01T00:00:00+00:00",
        "2030-01-01T00:00:00Z",
    ],
)
@pytest.mark.asyncio
async def test_restore_normalizes_naive_persisted_and_utc_archive_timestamps(
    local_memory_db,
    incoming_updated_at,
):
    _get_session, database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    created = await memory_repository.create_memory(
        content="Current canonical content.",
        source_session_id=owner_session,
        source_type="operator",
    )
    with _runtime_operator(operator):
        archive = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )

    # SQLite returns persisted DateTime values without tzinfo.  Make the row
    # older than the archive and let restore compare it with an aware UTC value.
    stale_connection = sqlite3.connect(database_path)
    try:
        stale_connection.execute(
            "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
            ("Database content before restore.", "2020-01-01 00:00:00", created.memory_id),
        )
        stale_connection.commit()
    finally:
        stale_connection.close()

    archive = json.loads(json.dumps(archive))
    archive["memories"][0]["content"] = "Restored canonical content."
    archive["memories"][0]["updated_at"] = incoming_updated_at
    archive["export_hash"] = _recovery_json_hash(_memory_export_integrity_payload(archive))
    archive["artifact_path"] = (
        f"artifacts/memory-recovery/export-{archive['export_hash'][:24]}.json"
    )
    archive["artifact_sha256"] = _recovery_json_hash(_memory_export_artifact_payload(archive))

    with _runtime_operator(operator):
        restored = await memory_repository.restore_canonical_memory_state(
            archive,
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )

    assert restored["restored_memory_ids"] == [created.memory_id]
    assert (await memory_repository.get_memory(created.memory_id)).content == (
        "Restored canonical content."
    )


@pytest.mark.asyncio
async def test_restore_quarantines_existing_memory_owned_by_another_session(local_memory_db):
    _get_session, _database_path = local_memory_db
    operator = make_test_bypass_operator()
    owner_session = operator.session_id
    other_owner = "other-owner-session"
    other_memory = await memory_repository.create_memory(
        content="Other owner's canonical content.",
        source_session_id=other_owner,
        source_type="operator",
        metadata={"provenance": {"kind": "other_owner"}},
    )
    original = await memory_repository.get_memory(other_memory.memory_id)
    assert original is not None
    original_metadata = original.metadata_json

    owner_memory = await memory_repository.create_memory(
        content="Owner archive content.",
        source_session_id=owner_session,
        source_type="operator",
    )
    with _runtime_operator(operator):
        archive = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )
    archive = json.loads(json.dumps(archive))
    archive["memories"][0]["id"] = other_memory.memory_id
    archive["memories"][0]["content"] = "Forged overwrite of another owner."
    archive["memory_ids"] = [other_memory.memory_id]
    archive["export_hash"] = _recovery_json_hash(_memory_export_integrity_payload(archive))
    archive["artifact_path"] = (
        f"artifacts/memory-recovery/export-{archive['export_hash'][:24]}.json"
    )
    archive["artifact_sha256"] = _recovery_json_hash(_memory_export_artifact_payload(archive))

    with _runtime_operator(operator):
        restored = await memory_repository.restore_canonical_memory_state(
            archive,
            actor=operator.principal.principal_id,
            owner_session_id=owner_session,
            authenticated_session_id=owner_session,
        )

    assert restored["restored_memory_ids"] == []
    assert restored["owner_conflict_memory_ids"] == [other_memory.memory_id]
    assert restored["owner_conflict_count"] == 1
    assert restored["conflict_memory_ids"] == [other_memory.memory_id]
    assert restored["conflict_count"] == 1
    assert restored["status"] == "degraded_no_learning"
    assert restored["reconciliation"]["status"] == "degraded"
    assert restored["reconciliation"]["owner_conflict_memory_ids"] == [other_memory.memory_id]
    preserved = await memory_repository.get_memory(other_memory.memory_id)
    assert preserved is not None
    assert preserved.content == "Other owner's canonical content."
    assert preserved.source_session_id == other_owner
    assert preserved.metadata_json == original_metadata
    assert await memory_repository.get_memory(owner_memory.memory_id) is not None


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
