"""Finite read candidate and genuine list-safe owner projection regressions."""
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import pytest

from config.settings import settings
from src.db.models import MemoryTombstone, Secret, GoogleServiceConnection, OperatorSession
from src.memory.repository import memory_repository
from src.runtime_plugins.dispatch import NativeServiceBlocked
from src.runtime_plugins.protocol import ProtocolError
from src.runtime_plugins.read_admission import NativeServiceReadAdmission, memory_read_projection, native_capability_inventory, capability_read_projection


def candidate(**changes):
    return {"schema_version": 1, "method": "memory.retrieve", "query": "", "limit": 20, "status": "active", **changes}


def test_read_candidate_is_closed_immutable_and_query_never_becomes_wire_input():
    source = candidate(query="private literal")
    value = NativeServiceReadAdmission.from_candidate(source)
    source["query"] = "changed"
    assert value.candidate()["query"] == "private literal"
    assert value.wire_inputs("job-1") == {"query_ref": "job-1", "limit": 20}
    for changes in ({"query": "é" * 101}, {"limit": True}, {"status": "archived"}, {"root_id": "other"}):
        with pytest.raises(ProtocolError):
            NativeServiceReadAdmission.from_candidate(candidate(**changes))


@pytest.mark.asyncio
async def test_memory_read_uses_actual_owner_redaction_before_digest_and_null_summary(async_db, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setattr(settings, "vault_encryption_key", key.decode())
    async with async_db() as db:
        db.add(Secret(key="read-test", encrypted_value=Fernet(key).encrypt(b"known-private-secret").decode()))
    first = await memory_repository.create_memory(content="raw detail stays private", summary="known-private-secret safe", source_session_id="original-root")
    second = await memory_repository.create_memory(content="raw absent summary", summary=None, source_session_id="original-root")
    await memory_repository.create_memory(content="foreign", summary="foreign", source_session_id="other-root")
    async with async_db() as db:
        value = await memory_read_projection(db, owner_session_id="original-root", candidate=candidate())
    records = {row["record_ref"]: row for row in value["records"]}
    assert set(records) == {first.memory_id, second.memory_id}
    assert records[first.memory_id]["text"] == "[redacted secret] safe"
    assert records[second.memory_id]["text"] == ""
    for row in records.values():
        assert row["text_digest"] == hashlib.sha256(row["text"].encode()).hexdigest()


@pytest.mark.asyncio
async def test_memory_read_blocks_complete_page_when_redaction_unavailable(async_db, monkeypatch):
    await memory_repository.create_memory(content="raw", summary="private", source_session_id="original-root")
    monkeypatch.setattr("src.vault.redaction.redact_secrets_in_text_readonly", AsyncMock(side_effect=RuntimeError("unavailable")))
    async with async_db() as db:
        with pytest.raises(NativeServiceBlocked, match="redaction_unavailable"):
            await memory_read_projection(db, owner_session_id="original-root", candidate=candidate())


@pytest.mark.asyncio
async def test_memory_read_blocks_preview_truncation_in_actual_owner(async_db):
    await memory_repository.create_memory(content="raw", summary="x" * 9000, source_session_id="original-root")
    async with async_db() as db:
        with pytest.raises(NativeServiceBlocked, match="preview_unsupported"):
            await memory_read_projection(db, owner_session_id="original-root", candidate=candidate())


@pytest.mark.asyncio
async def test_memory_read_excludes_actual_tombstone_before_projection(async_db):
    memory = await memory_repository.create_memory(content="deleted", summary="deleted", source_session_id="original-root")
    async with async_db() as db:
        db.add(MemoryTombstone(memory_id=memory.memory_id))
    async with async_db() as db:
        assert await memory_read_projection(db, owner_session_id="original-root", candidate=candidate()) == {"records": []}


@pytest.mark.asyncio
async def test_memory_read_blocks_post_redaction_byte_and_frame_overflow(async_db, monkeypatch):
    await memory_repository.create_memory(content="raw", summary="safe", source_session_id="original-root")
    monkeypatch.setattr("src.vault.redaction.redact_secrets_in_text_readonly", AsyncMock(return_value="é" * 4097))
    async with async_db() as db:
        with pytest.raises(NativeServiceBlocked, match="projection_unsupported"):
            await memory_read_projection(db, owner_session_id="original-root", candidate=candidate())
    monkeypatch.setattr("src.vault.redaction.redact_secrets_in_text_readonly", AsyncMock(return_value="safe"))
    monkeypatch.setattr("src.runtime_plugins.read_admission.MAX_FRAME", 40)
    async with async_db() as db:
        with pytest.raises(NativeServiceBlocked, match="frame_unsupported"):
            await memory_read_projection(db, owner_session_id="original-root", candidate=candidate())


def test_capability_projection_is_exact_static_inventory_and_denies_drift():
    inventory, digest = native_capability_inventory()
    assert inventory and all(row["capability_id"].startswith("native_tool:") for row in inventory)
    value = {"schema_version": 1, "method": "capabilities.list", "cursor": None, "limit": 1, "native_inventory_digest": digest}
    assert capability_read_projection(value) == {"capabilities": inventory[:1], "next_cursor": inventory[0]["capability_id"]}
    with pytest.raises(NativeServiceBlocked, match="inventory_changed"):
        capability_read_projection({**value, "native_inventory_digest": "a" * 64})
    with pytest.raises(NativeServiceBlocked, match="cursor_unavailable"):
        capability_read_projection({**value, "cursor": "native_tool:not_bundled"})


@pytest.mark.asyncio
async def test_calendar_metadata_reads_only_exact_original_owner_and_revision(async_db):
    from sqlalchemy import event
    from src.api.calendar import _native_read_connection_metadata
    from src.work_board.contracts import WorkBoardOwner
    now = datetime.now(timezone.utc)
    owner = WorkBoardOwner(principal_id="original-principal", session_id="original-session")
    async with async_db() as db:
        db.add(OperatorSession(id=owner.session_id, principal_id=owner.principal_id,
            token_hash="read-test", idle_expires_at=now + timedelta(hours=1), absolute_expires_at=now + timedelta(hours=1)))
        db.add(GoogleServiceConnection(connection_id="original-connection", owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, vault_secret_key="never-project", state="ready", revision=3))
        db.add(GoogleServiceConnection(connection_id="other-connection", owner_principal_id="foreign",
            owner_session_id="foreign", vault_secret_key="never-project-other", state="ready", revision=3))
    statements = []
    async with async_db() as db:
        engine = db.bind.sync_engine
        def collect(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT") and "google_service_connections" in statement:
                statements.append(statement)
        event.listen(engine, "before_cursor_execute", collect)
        try:
            value = await _native_read_connection_metadata(db, owner, connection_id="original-connection", expected_revision=3)
            assert value == {"connection_ref": "original-connection", "revision": 3, "state": "ready", "reason_code": None}
            for connection_id, revision in (("original-connection", 2), ("other-connection", 3)):
                with pytest.raises(NativeServiceBlocked, match="candidate_changed"):
                    await _native_read_connection_metadata(db, owner, connection_id=connection_id, expected_revision=revision)
        finally:
            event.remove(engine, "before_cursor_execute", collect)
    assert statements
    for statement in statements:
        projection = statement.split("FROM", 1)[0]
        assert "vault_secret_key" not in projection
        assert "credential_fingerprint" not in projection
        assert "label" not in projection
        assert "scopes" not in projection
