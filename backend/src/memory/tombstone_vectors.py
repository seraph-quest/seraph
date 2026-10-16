"""Purge a forgotten memory's derived lance rows (#1119).

A canonical forget (``mark_memory_tombstoned_in_session``) records, in the same
transaction, the lance row ids that may hold the memory's text and vector on its
``MemoryTombstone`` ledger row, clears ``Memory.embedding_id`` and marks the
ledger ``derived_vector_purge = "not_covered"``. After that transaction commits,
this module rewrites the lance tables without those rows and drops every older
lance version (``vector_store.purge_rows``). Only then does the ledger turn
``covered``. A failed or interrupted purge stays ``not_covered`` and is retried by
the next kick, the periodic scheduler job or the next forget.

The purge is receipt-gated: a ledger row turns ``covered`` only if, at commit
time, every id it lists is one this pass verified gone, so a restore that
re-queued another id in the meantime keeps it pending.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import event, text
from sqlalchemy.orm import Session
from sqlmodel import select

from src.db.models import Memory, MemoryTombstone

logger = logging.getLogger(__name__)

COVERED = "covered"
NOT_COVERED = "not_covered"
PURGE_BATCH = 500

# Per event loop, so a purge never waits on a lock or task from a closed loop.
_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
_task: asyncio.Task | None = None
_rerun = False


def _purge_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    for stale in [key for key in _locks if key is not loop and key.is_closed()]:
        _locks.pop(stale, None)
    return _locks.setdefault(loop, asyncio.Lock())


def get_session():
    """The canonical memory repository's session, so purge state shares its database."""
    from src.memory import repository
    return repository.get_session()


def pending_ids(tombstone: MemoryTombstone) -> set[str]:
    try:
        value = json.loads(tombstone.vector_purge_ids_json or "[]")
    except (TypeError, ValueError):
        value = []
    return {item for item in value if isinstance(item, str) and item} if isinstance(value, list) else set()


def queue_purge(tombstone: MemoryTombstone, memory: Memory | None) -> bool:
    """Move the memory's lance link onto its ledger row (same transaction as the forget).

    Returns whether a purge is now pending. The memory id is always a target,
    because retrieval matches lance rows by embedding id or memory id.
    """
    queued = pending_ids(tombstone)
    new = {tombstone.memory_id}
    if memory is not None and memory.embedding_id:
        new.add(memory.embedding_id)
        memory.embedding_id = None
    if tombstone.derived_vector_purge == COVERED and new <= {tombstone.memory_id} and not queued:
        # Already purged and nothing new links this memory to a lance row.
        return False
    tombstone.vector_purge_ids_json = json.dumps(sorted(queued | new))
    tombstone.derived_vector_purge = NOT_COVERED
    tombstone.vector_purged_at = None
    return True


def purge_after_commit(db) -> None:
    """Kick the purge once the caller's own outer transaction commits (never before).

    Registered on the sync session behind an AsyncSession. A rollback disarms
    it, so a reused session's later commit never fires a stale kick; each
    one-shot listener drops itself the first time its event fires.
    """
    session = getattr(db, "sync_session", db)
    if not isinstance(session, Session):
        return  # Not an ORM session (a test adapter); the follow-up job purges it.
    armed = [True]

    def committed(_session) -> None:
        if not armed[0]:
            return
        armed[0] = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No loop here; the periodic job purges it.
        loop.call_soon(schedule_tombstone_vector_purge)

    def rolled_back(_session) -> None:
        armed[0] = False
    event.listen(session, "after_commit", committed, once=True)
    event.listen(session, "after_rollback", rolled_back, once=True)


def schedule_tombstone_vector_purge() -> None:
    """Single-flight background purge; a kick during a run triggers one more pass."""
    global _task, _rerun
    loop = asyncio.get_running_loop()
    if _task is not None and not _task.done() and _task.get_loop() is loop:
        _rerun = True
        return
    _task = loop.create_task(_drain(), name="memory-tombstone-vector-purge")


async def _drain() -> None:
    global _rerun
    while True:
        _rerun = False
        try:
            report = await purge_tombstoned_vectors()
        except Exception:
            logger.warning("Memory tombstone vector purge deferred", exc_info=True)
            report = {}
        # A full batch that made progress may have more behind it.
        if not _rerun and not (report.get("pending") == PURGE_BATCH and report.get("covered")):
            return


async def purge_tombstoned_vectors() -> dict:
    """Purge every pending forget's lance rows, then mark the verified ones covered."""
    from src.memory import vector_store
    async with _purge_lock():
        async with get_session() as db:
            rows = (await db.execute(select(MemoryTombstone, Memory.embedding_id)
                                     .outerjoin(Memory, Memory.id == MemoryTombstone.memory_id)
                                     .where(MemoryTombstone.derived_vector_purge != COVERED)
                                     .order_by(MemoryTombstone.created_at, MemoryTombstone.id)
                                     .limit(PURGE_BATCH))).all()
        if not rows:
            return {"pending": 0, "covered": 0, "purged_rows": 0}
        # Rows forgotten before #1119 still carry their link on the memory row.
        targets = {tombstone.id: pending_ids(tombstone) | {tombstone.memory_id} | ({embedding} if embedding else set())
                   for tombstone, embedding in rows}
        purged = set().union(*targets.values())
        report = await asyncio.to_thread(vector_store.purge_rows, purged)
        covered = 0
        now = datetime.now(timezone.utc)
        async with get_session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            for tombstone_id, verified in targets.items():
                tombstone = await db.get(MemoryTombstone, tombstone_id, populate_existing=True)
                if tombstone is None or tombstone.derived_vector_purge == COVERED:
                    continue
                memory = await db.get(Memory, tombstone.memory_id, populate_existing=True)
                if not pending_ids(tombstone) <= verified or (memory is not None and memory.embedding_id
                                                              and memory.embedding_id not in verified):
                    continue  # Re-queued meanwhile; the next pass purges the new ids.
                if memory is not None and memory.embedding_id:
                    memory.embedding_id = None
                    db.add(memory)
                tombstone.vector_purge_ids_json = "[]"
                tombstone.derived_vector_purge = COVERED
                tombstone.vector_purged_at = now
                db.add(tombstone)
                covered += 1
    return {"pending": len(rows), "covered": covered, "purged_rows": report["purged_rows"]}


async def derived_vector_purge_state(memory_id: str) -> str:
    """The ledger's purge state; anything unreadable is reported ``not_covered``."""
    try:
        async with get_session() as db:
            state = (await db.execute(select(MemoryTombstone.derived_vector_purge)
                                      .where(MemoryTombstone.memory_id == memory_id))).scalar_one_or_none()
    except Exception:
        logger.warning("Memory tombstone vector purge state unavailable", exc_info=True)
        return NOT_COVERED
    return state if state == COVERED else NOT_COVERED


async def run_tombstone_vector_purge() -> None:
    """Scheduler entry: the bounded backstop for a purge a crash or failure left pending."""
    try:
        await purge_tombstoned_vectors()
    except Exception:
        logger.warning("Memory tombstone vector purge sweep failed", exc_info=True)
