"""Pre-#1085 memory decay, kept verbatim as the equivalence-test oracle.

Do not import from product code. The predicates and the pairwise scan are
the exact implementation that #1085 replaced; tests compare the new planner
against it on randomized corpora.
"""

from __future__ import annotations

import json

from sqlmodel import col, select

from src.db import engine as db_engine
from src.db.models import Memory, MemoryEdge, MemoryEdgeType, MemoryStatus
from src.memory.decay import (
    DecayMaintenanceResult,
    _COMPARABLE_KINDS,
    _SHORT_ENTITY_CONTRADICTION_KINDS,
    _anchor_tokens,
    _cue_polarity,
    _normalize_timestamp,
    _normalized_text,
    _now,
    _priority,
    _staleness_step,
)
from src.memory.repository import (
    _begin_canonical_write,
    _canonical_memory_deletion_marker,
    _canonical_memory_without_tombstone_clause,
)


def _shares_entity(memory: Memory, peer: Memory) -> bool:
    return (
        memory.subject_entity_id
        and memory.subject_entity_id == peer.subject_entity_id
    ) or (
        memory.project_entity_id
        and memory.project_entity_id == peer.project_entity_id
    )


def _comparable(memory: Memory, peer: Memory) -> bool:
    if memory.id == peer.id:
        return False
    if memory.kind != peer.kind or memory.kind not in _COMPARABLE_KINDS:
        return False
    if memory.status != MemoryStatus.active or peer.status != MemoryStatus.active:
        return False
    if (
        memory.subject_entity_id
        and peer.subject_entity_id
        and memory.subject_entity_id != peer.subject_entity_id
    ):
        return False
    if (
        memory.project_entity_id
        and peer.project_entity_id
        and memory.project_entity_id != peer.project_entity_id
    ):
        return False
    shared_entity = _shares_entity(memory, peer)
    if shared_entity:
        return True
    return len(_anchor_tokens(_normalized_text(memory)) & _anchor_tokens(_normalized_text(peer))) >= 2


def _contradictory(memory: Memory, peer: Memory) -> bool:
    if not _comparable(memory, peer):
        return False
    left_text = _normalized_text(memory)
    right_text = _normalized_text(peer)
    left_anchors = _anchor_tokens(left_text)
    right_anchors = _anchor_tokens(right_text)
    overlap = len(left_anchors & right_anchors)
    required_overlap = 2
    if (
        _shares_entity(memory, peer)
        and memory.kind in _SHORT_ENTITY_CONTRADICTION_KINDS
        and len(left_anchors) == 1
        and len(right_anchors) == 1
    ):
        required_overlap = 1
    if overlap < required_overlap:
        return False
    left_polarity = _cue_polarity(left_text)
    right_polarity = _cue_polarity(right_text)
    return left_polarity != 0 and right_polarity != 0 and left_polarity != right_polarity



async def legacy_apply_memory_decay_policies(
    *,
    now: datetime | None = None,
) -> DecayMaintenanceResult:
    resolved_now = _normalize_timestamp(now) or _now()
    contradiction_count = 0
    superseded_count = 0
    decayed_count = 0
    archived_count = 0

    async with db_engine.get_session() as db:
        await _begin_canonical_write(db)
        active_memories = (
            await db.execute(
                select(Memory)
                .where(Memory.status == MemoryStatus.active)
                .where(_canonical_memory_without_tombstone_clause())
                .order_by(
                    col(Memory.kind).asc(),
                    col(Memory.created_at).asc(),
                )
            )
        ).scalars().all()
        active_memories = [
            memory
            for memory in active_memories
            if _canonical_memory_deletion_marker(memory) is None
        ]

        for index, memory in enumerate(active_memories):
            if memory.status != MemoryStatus.active:
                continue
            for peer in active_memories[index + 1 :]:
                if peer.status != MemoryStatus.active or not _contradictory(memory, peer):
                    continue
                winner, loser = (memory, peer)
                if _priority(peer) > _priority(memory):
                    winner, loser = peer, memory

                loser.status = MemoryStatus.superseded
                loser.updated_at = resolved_now
                loser_metadata = json.loads(loser.metadata_json or "{}")
                loser_metadata["contradicted_by_memory_id"] = winner.id
                loser_metadata["superseded_by_memory_id"] = winner.id
                loser_metadata["superseded_reason"] = "contradiction"
                loser.metadata_json = json.dumps(loser_metadata, sort_keys=True)
                db.add(loser)

                contradiction_count += 1
                superseded_count += 1

                for edge_type in (MemoryEdgeType.contradicts, MemoryEdgeType.supersedes):
                    existing_edge = (
                        await db.execute(
                            select(MemoryEdge)
                            .where(MemoryEdge.from_memory_id == winner.id)
                            .where(MemoryEdge.to_memory_id == loser.id)
                            .where(MemoryEdge.edge_type == edge_type)
                        )
                    ).scalars().first()
                    if existing_edge is not None:
                        continue
                    db.add(
                        MemoryEdge(
                            from_memory_id=winner.id,
                            to_memory_id=loser.id,
                            edge_type=edge_type,
                            weight=1.0,
                            metadata_json=json.dumps(
                                {"writer": "memory_decay"},
                                sort_keys=True,
                            ),
                        )
                    )

        for memory in active_memories:
            if memory.status != MemoryStatus.active:
                continue
            step, age_days = _staleness_step(memory, now=resolved_now)
            metadata = json.loads(memory.metadata_json or "{}")
            prior_step = int(metadata.get("decay_step", 0) or 0)
            delta = 0
            if step > prior_step:
                delta = step - prior_step
            elif step >= 4:
                # Terminal stale rows keep decaying across later maintenance runs until they archive.
                delta = 1

            if delta > 0:
                memory.confidence = max(0.15, float(memory.confidence or 0.0) - (0.08 * delta))
                memory.reinforcement = max(
                    0.0,
                    float(memory.reinforcement or 0.0) - (0.25 * delta),
                )
                metadata["decay_step"] = step
                metadata["decay_age_days"] = age_days
                memory.metadata_json = json.dumps(metadata, sort_keys=True)
                memory.updated_at = resolved_now
                db.add(memory)
                decayed_count += 1

            if step >= 4 and (
                float(memory.confidence or 0.0) <= 0.35
                or float(memory.reinforcement or 0.0) <= 0.2
            ):
                metadata = json.loads(memory.metadata_json or "{}")
                metadata["archived_reason"] = "stale"
                metadata["archived_at"] = resolved_now.isoformat()
                memory.metadata_json = json.dumps(metadata, sort_keys=True)
                memory.status = MemoryStatus.archived
                memory.updated_at = resolved_now
                db.add(memory)
                archived_count += 1

        await db.flush()

    return DecayMaintenanceResult(
        contradiction_count=contradiction_count,
        superseded_count=superseded_count,
        decayed_count=decayed_count,
        archived_count=archived_count,
    )
