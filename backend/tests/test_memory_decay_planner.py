"""#1085: the decay planner must match the pre-#1085 pairwise algorithm exactly."""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from src.db.models import Memory, MemoryEdge, MemoryEntity, MemoryKind, MemoryStatus, MemoryTombstone
from src.memory import decay
from src.memory.decay import apply_memory_decay_policies
from tests.memory_decay_legacy_oracle import legacy_apply_memory_decay_policies

_NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
_TOPICS = ["atlas", "brief", "launch", "standup", "review", "budget", "mornings", "calls", "email", "sprint"]
_CUES = ["prefers", "avoids", "is on track", "is delayed", "is blocked", "likes", "dislikes", "is active",
         "is paused", "works better", "is not helpful", "is ready", "was cancelled"]
_KINDS = [MemoryKind.fact, MemoryKind.preference, MemoryKind.communication_preference, MemoryKind.project,
          MemoryKind.timeline, MemoryKind.commitment, MemoryKind.procedural, MemoryKind.collaborator]


def _corpus(seed: int, count: int) -> tuple[list[MemoryEntity], list[Memory], list[str]]:
    rng = random.Random(seed)
    entities = [
        MemoryEntity(id=f"entity-{seed}-{index}", canonical_key=f"entity-{seed}-{index}", canonical_name=f"Entity {index}")
        for index in range(4)
    ]
    memories = []
    for index in range(count):
        topics = rng.sample(_TOPICS, rng.choice([1, 1, 2, 2, 3]))
        text = f"{' '.join(topics)} {rng.choice(_CUES)}" + (f" {rng.choice(_CUES)}" if rng.random() < 0.15 else "")
        metadata = rng.choice([None, "{}", json.dumps({"decay_step": rng.randint(0, 4)}), json.dumps({"note": "x"})])
        created = _NOW - timedelta(days=rng.randint(0, 900), seconds=index)
        memories.append(Memory(
            id=f"memory-{seed}-{index:04d}",
            content=text,
            summary=text if rng.random() < 0.7 else None,
            kind=rng.choice(_KINDS),
            subject_entity_id=rng.choice([None, None, entities[0].id, entities[1].id]),
            project_entity_id=rng.choice([None, None, entities[2].id, entities[3].id]),
            confidence=round(rng.uniform(0.1, 1.0), 3),
            importance=round(rng.uniform(0.1, 1.0), 3),
            reinforcement=round(rng.uniform(0.0, 3.0), 3),
            metadata_json=metadata,
            created_at=created,
            updated_at=created,
            last_confirmed_at=created + timedelta(days=rng.randint(0, 30)) if rng.random() < 0.3 else None,
        ))
    tombstoned = [memory.id for memory in rng.sample(memories, count // 20)]
    return entities, memories, tombstoned


async def _load(async_db, entities, memories, tombstoned):
    async with async_db() as db:
        for entity in entities:
            db.add(MemoryEntity(**entity.model_dump()))
        await db.flush()
        for memory in memories:
            db.add(Memory(**memory.model_dump()))
        await db.flush()
        for memory_id in tombstoned:
            db.add(MemoryTombstone(memory_id=memory_id))


async def _state(async_db):
    async with async_db() as db:
        memories = (await db.execute(select(Memory).order_by(Memory.id))).scalars().all()
        edges = (await db.execute(select(MemoryEdge))).scalars().all()
        return (
            [(m.id, m.status, m.confidence, m.reinforcement, m.metadata_json, m.updated_at) for m in memories],
            sorted((edge.from_memory_id, edge.to_memory_id, edge.edge_type, edge.metadata_json) for edge in edges),
        )


async def _reset(async_db):
    async with async_db() as db:
        for model in (MemoryEdge, MemoryTombstone, Memory, MemoryEntity):
            for row in (await db.execute(select(model))).scalars().all():
                await db.delete(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
async def test_planner_matches_pairwise_algorithm_on_randomized_corpus(async_db, seed):
    entities, memories, tombstoned = _corpus(seed, 260)
    outcomes = []
    for implementation in (legacy_apply_memory_decay_policies, apply_memory_decay_policies):
        await _reset(async_db)
        await _load(async_db, entities, memories, tombstoned)
        results = [await implementation(now=_NOW), await implementation(now=_NOW + timedelta(days=200))]
        outcomes.append((results, await _state(async_db)))
    (legacy_results, legacy_state), (new_results, new_state) = outcomes
    assert legacy_results[0].contradiction_count > 0, "corpus must exercise contradictions"
    assert legacy_results[0].decayed_count > 0 and legacy_results[1].archived_count > 0
    assert new_results == legacy_results
    assert new_state == legacy_state


@pytest.mark.asyncio
async def test_planner_never_compares_every_pair(async_db, monkeypatch):
    entities, memories, _ = _corpus(9, 2000)
    calls = 0
    real = decay._contradictory

    def counted(*args):
        nonlocal calls
        calls += 1
        return real(*args)

    monkeypatch.setattr(decay, "_contradictory", counted)
    rows = [decay._DecayRow.of(memory) for memory in sorted(memories, key=lambda m: (m.kind, m.created_at))]
    decay._plan_memory_decay(rows, _NOW)
    # The pairwise scan made n(n-1)/2 ~ 2M comparisons here.
    assert calls < len(rows) * 100, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_rows_edited_between_plan_and_apply_are_not_overwritten(async_db, monkeypatch):
    stale = _NOW - timedelta(days=800)
    async with async_db() as db:
        database = db.get_bind().url.database
        for name in ("edited", "tombstoned", "untouched"):
            db.add(Memory(id=name, content=f"{name} commitment for the atlas brief", kind=MemoryKind.commitment,
                          confidence=0.3, reinforcement=0.1, created_at=stale, updated_at=stale))
    real_plan = decay._plan_memory_decay

    def plan_then_concurrent_edits(rows, now):
        plan = real_plan(rows, now)
        connection = sqlite3.connect(database, isolation_level=None, timeout=30)
        try:
            # An operator confirmation and a forget commit while the plan is
            # computed outside the writer lock.
            connection.execute("UPDATE memories SET confidence = 0.95, reinforcement = 2.0, updated_at = ? WHERE id = 'edited'",
                               ((now + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),))
            connection.execute("INSERT INTO memory_tombstones (id, memory_id, actor, reason, created_at) VALUES ('t1', 'tombstoned', 'operator', 'forget', ?)",
                               (now.strftime("%Y-%m-%d %H:%M:%S.%f"),))
        finally:
            connection.close()
        return plan

    monkeypatch.setattr(decay, "_plan_memory_decay", plan_then_concurrent_edits)
    result = await apply_memory_decay_policies(now=_NOW)
    async with async_db() as db:
        edited = await db.get(Memory, "edited")
        tombstoned = await db.get(Memory, "tombstoned")
        untouched = await db.get(Memory, "untouched")
    assert (edited.status, edited.confidence, edited.reinforcement) == (MemoryStatus.active, 0.95, 2.0)
    assert tombstoned.status is MemoryStatus.active and tombstoned.metadata_json is None
    assert untouched.status is MemoryStatus.archived
    assert result.archived_count == 1
