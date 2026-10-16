"""#1119: a canonical forget purges the memory's text and vector from the lance files.

Real SQLite, a real lance store under a private workspace, and a scripted embedder
(no inference). Every case asserts on the bytes of the lance files on disk.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import text, update
from sqlmodel import select

from config.settings import settings
from src.db.models import Memory, MemoryStatus, MemoryTombstone
from src.memory import tombstone_vectors, vector_store
from src.memory.embedder import EmbeddingMetadata
from src.memory.repository import memory_repository

SECRET = "LANCE-FORGET-SECRET-1119 the operator's private plan"
KEEP = "LANCE-KEEP-NEIGHBOUR-1119 an unrelated memory"
DIM = 8


def _vector(value: str) -> list[float]:
    # Scripted embedder: distinct, deterministic, never near-duplicates.
    digest = hashlib.sha256(value.encode()).digest()
    return [float(byte) / 255.0 + index for index, byte in enumerate(digest[:DIM])]


@pytest.fixture
def lance(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    metadata = EmbeddingMetadata(schema_version="seraph.memory.embedding.v1", provider="scripted",
                                 model="scripted/embedder", dimension=DIM)
    monkeypatch.setattr(vector_store, "embed", _vector)
    monkeypatch.setattr(vector_store, "embedding_metadata", lambda: metadata)
    monkeypatch.setattr(vector_store, "log_integration_event_sync", lambda **_kwargs: None)
    vector_store._reset_vector_store_state()
    yield workspace / "lance"
    vector_store._reset_vector_store_state()


def _on_disk(root: Path, marker: str) -> list[str]:
    needle = marker.encode()
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                  if path.is_file() and needle in path.read_bytes())


async def _remember(content: str) -> str:
    embedding_id = vector_store.add_memory(content, category="fact", source_session_id="owner-session")
    assert embedding_id
    created = await memory_repository.create_memory(content=content, summary=content[:40],
                                                    source_session_id="owner-session", embedding_id=embedding_id)
    return created.memory_id


async def _ledger(async_db, memory_id: str):
    async with async_db() as db:
        tombstone = (await db.execute(select(MemoryTombstone).where(MemoryTombstone.memory_id == memory_id))).scalar_one()
        memory = await db.get(Memory, memory_id)
    return tombstone, memory


async def _settle_kick() -> None:
    task = tombstone_vectors._task
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_forget_purges_lance_text_and_vector_and_reports_covered(async_db, lance):
    from src.memory.control import apply_memory_live_control_action
    secret_id = await _remember(SECRET)
    await _remember(KEEP)
    assert _on_disk(lance, SECRET) and _on_disk(lance, KEEP)
    result = await apply_memory_live_control_action(action="propagate_delete_export", acknowledged=True,
                                                    owner_session_id="owner-session", memory_id=secret_id,
                                                    reason="forget it")
    assert result["receipt"]["tombstone"]["derived_vector_purge"] == "covered"
    await _settle_kick()
    # The text is gone from every lance file, versions included; the neighbour stays.
    assert _on_disk(lance, SECRET) == [] and _on_disk(lance, KEEP)
    tombstone, memory = await _ledger(async_db, secret_id)
    assert tombstone.derived_vector_purge == "covered" and json.loads(tombstone.vector_purge_ids_json) == []
    assert tombstone.vector_purged_at is not None and memory.embedding_id is None
    assert "content" not in MemoryTombstone.__table__.columns
    hits = vector_store.search(SECRET, top_k=5)
    assert all(hit["text"] != SECRET for hit in hits) and [hit["text"] for hit in hits] == [KEEP]


@pytest.mark.asyncio
async def test_archive_then_forget_purges_lance_text(async_db, lance):
    from src.memory.control import forget_memory
    secret_id = await _remember(SECRET)
    archived = await forget_memory(memory_id=secret_id, mode="archive", reason="archive first")
    assert archived["memory"]["status"] == MemoryStatus.archived.value
    # Archiving is not a canonical forget: the derived copy is still there.
    assert _on_disk(lance, SECRET)
    written = await memory_repository.mark_memory_tombstoned(secret_id, actor="operator", reason="now forget")
    assert written.derived_vector_purge == "covered"
    await _settle_kick()
    assert _on_disk(lance, SECRET) == []


@pytest.mark.asyncio
async def test_restore_then_pending_reapply_purges_the_restored_lance_row(async_db, lance):
    secret_id = await _remember(SECRET)
    async with async_db() as db:
        embedding_id = (await db.get(Memory, secret_id)).embedding_id
    assert (await memory_repository.mark_memory_tombstoned(secret_id, actor="operator")).derived_vector_purge == "covered"
    await _settle_kick()
    assert _on_disk(lance, SECRET) == []
    # A stale backup restores the canonical row (with its lance link) and the lance row.
    async with async_db() as db:
        await db.execute(update(Memory).where(Memory.id == secret_id).values(
            content=SECRET, summary=SECRET[:40], status=MemoryStatus.active, confidence=0.9, importance=0.9,
            reinforcement=1.0, embedding_id=embedding_id))
    table = vector_store._get_or_create_table()
    assert table.count_rows() == 0
    table.add([{"id": embedding_id, "text": SECRET, "category": "fact", "source_session_id": "owner-session",
                "vector": _vector(SECRET), "created_at": "2026-10-16T00:00:00+00:00",
                "embedding_namespace": "scripted", "embedding_model": "scripted/embedder",
                "embedding_schema_version": "seraph.memory.embedding.v1", "embedding_dimension": DIM}])
    assert _on_disk(lance, SECRET)
    receipt = await memory_repository.reconcile_memory_tombstones()
    assert receipt["reapplied_count"] == 1
    # The reapply re-queued the restored link in its own transaction and kicked the purge.
    await _settle_kick()
    assert _on_disk(lance, SECRET) == []
    tombstone, memory = await _ledger(async_db, secret_id)
    assert tombstone.derived_vector_purge == "covered" and memory.embedding_id is None
    assert memory.content != SECRET and memory.status is MemoryStatus.archived


@pytest.mark.asyncio
async def test_in_session_forget_is_not_covered_until_its_commit_then_purges(async_db, lance):
    # The #1064 Cordis forget and task-method delete call the in-session owner.
    secret_id = await _remember(SECRET)
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        written = await memory_repository.mark_memory_tombstoned_in_session(db, secret_id, actor="operator")
        assert written.derived_vector_purge == "not_covered"
        assert _on_disk(lance, SECRET)  # nothing touches lance before the commit
    await _settle_kick()
    assert _on_disk(lance, SECRET) == []
    tombstone, _memory = await _ledger(async_db, secret_id)
    assert tombstone.derived_vector_purge == "covered"


@pytest.mark.asyncio
async def test_a_failed_purge_stays_not_covered_until_the_backstop_job(async_db, lance, monkeypatch):
    secret_id = await _remember(SECRET)
    original = vector_store.purge_rows

    def failing(_ids):
        raise OSError("lance store unavailable")
    monkeypatch.setattr(vector_store, "purge_rows", failing)
    written = await memory_repository.mark_memory_tombstoned(secret_id, actor="operator")
    await _settle_kick()
    # Honest receipt: the text is still at rest, so the forget is not covered.
    assert written.derived_vector_purge == "not_covered" and _on_disk(lance, SECRET)
    tombstone, memory = await _ledger(async_db, secret_id)
    assert tombstone.derived_vector_purge == "not_covered" and memory.embedding_id is None
    assert len(json.loads(tombstone.vector_purge_ids_json)) == 2  # embedding id and memory id; never text
    monkeypatch.setattr(vector_store, "purge_rows", original)
    await tombstone_vectors.run_tombstone_vector_purge()
    assert _on_disk(lance, SECRET) == []
    tombstone, _memory = await _ledger(async_db, secret_id)
    assert tombstone.derived_vector_purge == "covered"


@pytest.mark.asyncio
async def test_a_forget_from_before_the_purge_existed_is_purged_by_the_job(async_db, lance):
    secret_id = await _remember(SECRET)
    async with async_db() as db:
        embedding_id = (await db.get(Memory, secret_id)).embedding_id
        # A pre-#1119 ledger row: no queued ids, the link still on the memory row.
        db.add(MemoryTombstone(memory_id=secret_id, actor="operator", reason="legacy"))
        await db.execute(update(Memory).where(Memory.id == secret_id).values(
            content="[delete/export propagated by operator]", status=MemoryStatus.archived))
    tombstone, memory = await _ledger(async_db, secret_id)
    assert tombstone.derived_vector_purge == "not_covered" and memory.embedding_id == embedding_id
    report = await tombstone_vectors.purge_tombstoned_vectors()
    assert report == {"pending": 1, "covered": 1, "purged_rows": 1}
    assert _on_disk(lance, SECRET) == []
    tombstone, memory = await _ledger(async_db, secret_id)
    assert tombstone.derived_vector_purge == "covered" and memory.embedding_id is None


@pytest.mark.asyncio
async def test_a_rolled_back_forget_disarms_its_kick(async_db, lance, monkeypatch):
    secret_id = await _remember(SECRET)
    kicks = []
    monkeypatch.setattr(tombstone_vectors, "schedule_tombstone_vector_purge", lambda: kicks.append(1))
    async with async_db() as db:
        with pytest.raises(RuntimeError):
            async with db.begin():
                await memory_repository.mark_memory_tombstoned_in_session(db, secret_id, actor="operator")
                raise RuntimeError("writer failed after the tombstone")
        # A later commit of the same (reused) session never fires the stale kick.
        async with db.begin():
            await db.execute(text("SELECT 1"))
    await asyncio.sleep(0)
    assert kicks == []
    async with async_db() as db:
        assert (await db.execute(select(MemoryTombstone))).scalars().all() == []
    assert _on_disk(lance, SECRET)  # nothing was forgotten, so nothing was purged


@pytest.mark.asyncio
async def test_existing_ledgers_gain_pending_purge_state_on_startup(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from src.db.engine import _ensure_memory_tombstone_columns
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE memory_tombstones (id VARCHAR PRIMARY KEY, memory_id VARCHAR, "
                                       "actor VARCHAR, reason VARCHAR, created_at DATETIME)")
            await conn.exec_driver_sql("INSERT INTO memory_tombstones VALUES ('t1', 'm1', 'operator', 'forget', "
                                       "'2026-01-01 00:00:00')")
            await _ensure_memory_tombstone_columns(conn)
            await _ensure_memory_tombstone_columns(conn)  # idempotent
            row = (await conn.exec_driver_sql("SELECT vector_purge_ids_json, derived_vector_purge, vector_purged_at "
                                              "FROM memory_tombstones")).one()
    finally:
        await engine.dispose()
    # A forget from before #1119 is honestly pending until the job purges it.
    assert tuple(row) == ("[]", "not_covered", None)
