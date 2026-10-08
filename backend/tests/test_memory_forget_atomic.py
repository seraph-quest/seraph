"""Actual canonical forget owner and audit atomicity, without native activation."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.sql.dml import Update
from sqlmodel import select

from src.audit.repository import audit_repository
from src.api import memory as memory_api
from src.api import operator as operator_api
from src.auth.service import test_bypass_operator as bypass_operator
from src.db.models import AuditEvent, Memory, MemoryStatus, MemoryTombstone, Session
from src.memory.control import _forget_memory_in_session, forget_memory
from src.memory.repository import _begin_canonical_write


async def seed(factory, *, owner="owner"):
    async with factory() as db:
        db.add(Session(id="owner"))
        db.add(Session(id="foreign"))
        db.add(Memory(id="record", content="Private original", summary="Private summary",
                      source_session_id=owner, metadata_json='{"retained": true}'))
        await db.flush()


async def readback(factory):
    async with factory() as db:
        memory = await db.get(Memory, "record")
        events = (await db.execute(select(AuditEvent))).scalars().all()
        tombstones = (await db.execute(select(MemoryTombstone))).scalars().all()
        return memory, events, tombstones


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["archive", "redact"])
async def test_same_writer_forget_commits_actual_memory_and_audit(async_db, mode):
    await seed(async_db)
    writers = []
    actual_audit = audit_repository._log_event_in_session

    async def audit(db, **kwargs):
        writers.append(db)
        # The real canonical update already exists in this exact writer.
        assert (await db.get(Memory, "record")).status == MemoryStatus.archived
        return await actual_audit(db, **kwargs)

    async with async_db() as db:
        await _begin_canonical_write(db)
        with patch.object(audit_repository, "_log_event_in_session", audit):
            result = await _forget_memory_in_session(
                db, owner_session_id="owner", memory_id="record", mode=mode,
                privacy_boundary="private", reason="bounded operator reason")
        assert writers == [db]
        assert db.in_transaction()  # Caller still owns commit/result transaction.
    memory, events, tombstones = await readback(async_db)
    assert memory.status == MemoryStatus.archived
    assert memory.confidence == memory.importance == memory.reinforcement == 0
    assert memory.content == ("[forgotten by operator]" if mode == "redact" else "Private original")
    assert memory.summary == ("[forgotten by operator]" if mode == "redact" else "Private summary")
    assert json.loads(memory.metadata_json)["retained"] is True
    assert len(events) == 1 and events[0].id == result["audit_event_id"]
    assert events[0].session_id == "owner" and events[0].event_type == "memory_forgotten"
    details = json.loads(events[0].details_json)
    assert details["mode"] == mode and details["memory_id"] == "record"
    assert "Private original" not in events[0].details_json
    assert not tombstones  # Local archive/redact is not delete/export propagation.


@pytest.mark.asyncio
@pytest.mark.parametrize("after_flush", [False, True])
async def test_audit_failure_rolls_back_real_forget(async_db, after_flush):
    await seed(async_db)
    actual_audit = audit_repository._log_event_in_session

    async def fail(db, **kwargs):
        if after_flush:
            await actual_audit(db, **kwargs)
        raise RuntimeError("injected audit failure")

    with patch.object(audit_repository, "_log_event_in_session", fail):
        with pytest.raises(RuntimeError, match="injected audit failure"):
            await forget_memory(memory_id="record", mode="redact")
    memory, events, _ = await readback(async_db)
    assert memory.status == MemoryStatus.active and memory.content == "Private original"
    assert memory.metadata_json == '{"retained": true}' and not events


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "foreign"])
async def test_private_forget_rejects_unbound_or_foreign_owner(async_db, owner):
    await seed(async_db, owner=owner)
    with pytest.raises(ValueError, match="original owner session"):
        async with async_db() as db:
            await _begin_canonical_write(db)
            await _forget_memory_in_session(db, owner_session_id="owner", memory_id="record")
    memory, events, _ = await readback(async_db)
    assert memory.status == MemoryStatus.active and not events


@pytest.mark.asyncio
async def test_private_forget_never_creates_missing_owner_session(async_db):
    await seed(async_db, owner="missing")
    with pytest.raises(ValueError, match="original owner Session"):
        async with async_db() as db:
            await _begin_canonical_write(db)
            await _forget_memory_in_session(db, owner_session_id="missing", memory_id="record")
    async with async_db() as db:
        assert await db.get(Session, "missing") is None
    memory, events, _ = await readback(async_db)
    assert memory.status == MemoryStatus.active and not events


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", ["tombstone", "marker"])
async def test_canonical_delete_prevents_forget(async_db, deleted):
    await seed(async_db)
    async with async_db() as db:
        if deleted == "tombstone":
            db.add(MemoryTombstone(memory_id="record"))
        else:
            memory = await db.get(Memory, "record")
            memory.metadata_json = '{"archived_reason": "operator_delete_export"}'
    with pytest.raises(ValueError, match="operator delete/export redaction"):
        async with async_db() as db:
            await _begin_canonical_write(db)
            await _forget_memory_in_session(db, owner_session_id="owner", memory_id="record")
    _, events, _ = await readback(async_db)
    assert not events


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["status", "metadata_json", "updated_at", "source_session_id", "tombstone"])
async def test_actual_guarded_sql_rejects_changed_owner_row(async_db, changed):
    await seed(async_db)
    with pytest.raises(ValueError, match="memory changed before control update"):
        async with async_db() as db:
            await _begin_canonical_write(db)
            execute = db.execute
            intercepted = False

            async def race(statement, *args, **kwargs):
                nonlocal intercepted
                if isinstance(statement, Update) and statement.table.name == "memories" and not intercepted:
                    intercepted = True
                    if changed == "tombstone":
                        db.add(MemoryTombstone(memory_id="record"))
                        await db.flush()
                    else:
                        value = {"status": MemoryStatus.archived,
                                 "metadata_json": '{"winner": true}',
                                 "updated_at": datetime.now(timezone.utc) + timedelta(days=1),
                                 "source_session_id": "foreign"}[changed]
                        await execute(update(Memory).where(Memory.id == "record").values(**{changed: value}))
                return await execute(statement, *args, **kwargs)

            with patch.object(db, "execute", race):
                await _forget_memory_in_session(db, owner_session_id="owner", memory_id="record")
            assert intercepted
    memory, events, tombstones = await readback(async_db)
    assert memory.status == MemoryStatus.active and not events and not tombstones


@pytest.mark.asyncio
async def test_private_forget_rejects_absent_caller_writer(async_db):
    await seed(async_db)
    async with async_db() as db:
        with pytest.raises(RuntimeError, match="caller-owned writer"):
            await _forget_memory_in_session(db, owner_session_id="owner", memory_id="record")


@pytest.mark.asyncio
async def test_caller_result_failure_rolls_back_memory_and_flushed_audit(async_db):
    await seed(async_db)
    with pytest.raises(RuntimeError, match="original result failure"):
        async with async_db() as db:
            await _begin_canonical_write(db)
            result = await _forget_memory_in_session(
                db, owner_session_id="owner", memory_id="record", mode="redact")
            assert await db.get(AuditEvent, result["audit_event_id"]) is not None
            raise RuntimeError("original result failure")
    memory, events, _ = await readback(async_db)
    assert memory.status == MemoryStatus.active and memory.content == "Private original"
    assert not events


@pytest.mark.asyncio
async def test_legacy_unbound_permissive_mode_remains_compatible(async_db):
    await seed(async_db, owner=None)
    result = await forget_memory(memory_id="record", mode="legacy archive alias")
    memory, events, _ = await readback(async_db)
    assert memory.status == MemoryStatus.archived and memory.content == "Private original"
    assert events[0].session_id is None and result["receipt"]["action"] == "forget"


@pytest.mark.asyncio
async def test_authenticated_route_owner_change_after_early_read_fails_closed(async_db):
    operator = bypass_operator()
    await seed(async_db, owner=operator.session_id)
    async with async_db() as db:
        db.add(Session(id=operator.session_id))
    actual_early_read = memory_api._require_memory_owner

    async def change_owner(memory_id, session_id):
        await actual_early_read(memory_id, session_id)
        async with async_db() as db:
            await db.execute(update(Memory).where(Memory.id == memory_id)
                             .values(source_session_id="foreign"))

    request = SimpleNamespace(state=SimpleNamespace(operator=operator))
    with patch.object(memory_api, "_require_memory_owner", change_owner):
        with pytest.raises(HTTPException) as denied:
            await memory_api.forget_memory_item(
                "record", request, memory_api.MemoryForgetRequest(mode="redact", actor="attacker"))
    assert denied.value.status_code == 404  # Preserve existing owner ValueError mapping.
    memory, events, _ = await readback(async_db)
    assert memory.source_session_id == "foreign"
    assert memory.status == MemoryStatus.active and memory.content == "Private original"
    assert memory.metadata_json == '{"retained": true}' and not events


@pytest.mark.asyncio
async def test_authenticated_operator_route_owner_change_after_early_read_fails_closed(async_db):
    operator = bypass_operator()
    await seed(async_db, owner=operator.session_id)
    async with async_db() as db:
        db.add(Session(id=operator.session_id))
    actual_early_read = operator_api._require_memory_owner

    async def change_owner(memory_id, session_id):
        await actual_early_read(memory_id, session_id)
        async with async_db() as db:
            await db.execute(update(Memory).where(Memory.id == memory_id)
                             .values(source_session_id="foreign"))

    request = SimpleNamespace(state=SimpleNamespace(operator=operator))
    with patch.object(operator_api, "_require_memory_owner", change_owner):
        with pytest.raises(HTTPException) as denied:
            await operator_api.post_operator_memory_control(
                "record", request, operator_api.MemoryOperatorControlRequest(action="forget"))
    assert denied.value.status_code == 400  # Existing operator-route ValueError mapping.
    memory, events, _ = await readback(async_db)
    assert memory.source_session_id == "foreign"
    assert memory.status == MemoryStatus.active and memory.content == "Private original"
    assert memory.metadata_json == '{"retained": true}' and not events


@pytest.mark.asyncio
async def test_authenticated_operator_route_forget_keeps_actual_owner_and_actor(async_db):
    operator = bypass_operator()
    await seed(async_db, owner=operator.session_id)
    async with async_db() as db:
        db.add(Session(id=operator.session_id))
    request = SimpleNamespace(state=SimpleNamespace(operator=operator))
    result = await operator_api.post_operator_memory_control(
        "record", request, operator_api.MemoryOperatorControlRequest(
            action="forget", actor="attacker", session_id="foreign"))
    memory, events, _ = await readback(async_db)
    assert memory.status == MemoryStatus.archived
    assert memory.source_session_id == operator.session_id
    assert result["receipt"]["actor"] == operator.principal.principal_id
    assert len(events) == 1 and events[0].id == result["audit_event_id"]
    assert events[0].actor == operator.principal.principal_id
    assert events[0].session_id == operator.session_id
