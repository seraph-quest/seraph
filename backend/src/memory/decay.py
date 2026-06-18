from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import col, select

from src.db.engine import get_session
from src.db.models import Memory, MemoryEdge, MemoryEdgeType, MemoryKind, MemoryStatus
from src.memory.repository import memory_repository

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "user",
}

_POSITIVE_CUES = (
    "prefer",
    "prefers",
    "preferred",
    "likes",
    "like",
    "on track",
    "active",
    "available",
    "helpful",
    "ready",
    "works better",
)

_NEGATIVE_CUES = (
    "avoid",
    "avoids",
    "dislike",
    "dislikes",
    "delayed",
    "blocked",
    "paused",
    "cancelled",
    "canceled",
    "failed",
    "not helpful",
    "reduce direct interruptions",
)

_COMPARABLE_KINDS = {
    MemoryKind.fact,
    MemoryKind.preference,
    MemoryKind.communication_preference,
    MemoryKind.project,
    MemoryKind.timeline,
    MemoryKind.commitment,
}

_SHORT_ENTITY_CONTRADICTION_KINDS = {
    MemoryKind.preference,
    MemoryKind.communication_preference,
}

_AMBIGUOUS_STATUS_POSITIVE_CUES = {
    "active",
    "available",
}

_STALE_WINDOWS_DAYS = {
    MemoryKind.commitment: 30,
    MemoryKind.project: 45,
    MemoryKind.timeline: 30,
    MemoryKind.preference: 120,
    MemoryKind.communication_preference: 120,
    MemoryKind.procedural: 45,
    MemoryKind.collaborator: 120,
    MemoryKind.obligation: 60,
    MemoryKind.routine: 90,
    MemoryKind.goal: 180,
    MemoryKind.pattern: 180,
    MemoryKind.reflection: 180,
    MemoryKind.fact: 180,
}


@dataclass(frozen=True)
class DecayMaintenanceResult:
    contradiction_count: int = 0
    superseded_count: int = 0
    decayed_count: int = 0
    archived_count: int = 0


def memory_reconciliation_policy_payload() -> dict[str, object]:
    return {
        "authoritative_memory": "guardian",
        "reconciliation_policy": "canonical_first",
        "conflict_resolution": "supersede_lower_priority_contradictions",
        "forgetting_policy": "selective_decay_then_archive",
        "retrieval_ranking_policy": "contradiction_aware_active_only",
        "suppression_reasons": [
            "superseded_status",
            "archived_status",
            "lower_ranked_contradiction",
            "stale_provider_evidence",
            "irrelevant_provider_evidence",
        ],
        "stale_windows_days": {
            kind.value if hasattr(kind, "value") else str(kind): days
            for kind, days in _STALE_WINDOWS_DAYS.items()
        },
        "archive_thresholds": {
            "decay_step": 4,
            "max_confidence": 0.35,
            "max_reinforcement": 0.2,
        },
        "rules": [
            "Canonical guardian memory remains authoritative when contradictory memories compete.",
            "Lower-priority contradictory memories are superseded instead of silently coexisting as current truth.",
            "Stale memories decay progressively and archive only after confidence or reinforcement falls below the terminal threshold.",
            "Active retrieval suppresses archived, superseded, stale, and lower-ranked contradictory evidence before it reaches guardian recall.",
        ],
    }


async def summarize_memory_reconciliation_state(*, limit: int = 5) -> dict[str, object]:
    try:
        async with get_session() as db:
            superseded_memories = (
                await db.execute(
                    select(Memory)
                    .where(Memory.status == MemoryStatus.superseded)
                    .order_by(col(Memory.updated_at).desc(), col(Memory.created_at).desc())
                    .limit(limit)
                )
            ).scalars().all()
            archived_memories = (
                await db.execute(
                    select(Memory)
                    .where(Memory.status == MemoryStatus.archived)
                    .order_by(col(Memory.updated_at).desc(), col(Memory.created_at).desc())
                    .limit(limit)
                )
            ).scalars().all()
            for memory in (*superseded_memories, *archived_memories):
                db.expunge(memory)
            active_count = int(
                (
                    await db.execute(
                        select(func.count()).select_from(Memory).where(
                            Memory.status == MemoryStatus.active
                        )
                    )
                ).scalar_one()
                or 0
            )
            superseded_count = int(
                (
                    await db.execute(
                        select(func.count()).select_from(Memory).where(
                            Memory.status == MemoryStatus.superseded
                        )
                    )
                ).scalar_one()
                or 0
            )
            archived_count = int(
                (
                    await db.execute(
                        select(func.count()).select_from(Memory).where(
                            Memory.status == MemoryStatus.archived
                        )
                    )
                ).scalar_one()
                or 0
            )
            contradiction_edge_count = int(
                (
                    await db.execute(
                        select(func.count()).select_from(MemoryEdge).where(
                            MemoryEdge.edge_type == MemoryEdgeType.contradicts
                        )
                    )
                ).scalar_one()
                or 0
            )
    except Exception:
        return {
            "state": "unavailable",
            "active_count": 0,
            "superseded_count": 0,
            "archived_count": 0,
            "contradiction_edge_count": 0,
            "recent_conflicts": [],
            "recent_archivals": [],
            "policy": memory_reconciliation_policy_payload(),
            "error": "memory_repository_unavailable",
        }

    recent_conflicts: list[dict[str, object]] = []
    for memory in superseded_memories[:limit]:
        metadata = json.loads(memory.metadata_json or "{}")
        if not isinstance(metadata, dict):
            metadata = {}
        recent_conflicts.append(
            {
                "summary": (memory.summary or memory.content or "").strip(),
                "kind": memory.kind.value,
                "reason": str(metadata.get("superseded_reason") or "superseded"),
                "superseded_by_memory_id": metadata.get("superseded_by_memory_id"),
            }
        )

    recent_archivals: list[dict[str, object]] = []
    for memory in archived_memories[:limit]:
        metadata = json.loads(memory.metadata_json or "{}")
        if not isinstance(metadata, dict):
            metadata = {}
        recent_archivals.append(
            {
                "summary": (memory.summary or memory.content or "").strip(),
                "kind": memory.kind.value,
                "reason": str(metadata.get("archived_reason") or "archived"),
            }
        )

    if recent_conflicts and recent_archivals:
        state = "conflict_and_forgetting_active"
    elif recent_conflicts:
        state = "conflict_reconciled"
    elif recent_archivals:
        state = "selective_forgetting_active"
    else:
        state = "steady"

    return {
        "state": state,
        "active_count": active_count,
        "superseded_count": superseded_count,
        "archived_count": archived_count,
        "contradiction_edge_count": contradiction_edge_count,
        "recent_conflicts": recent_conflicts,
        "recent_archivals": recent_archivals,
        "policy": memory_reconciliation_policy_payload(),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _memory_age_anchor(memory: Memory) -> datetime:
    return (
        _normalize_timestamp(memory.last_confirmed_at)
        or _normalize_timestamp(memory.created_at)
        or _now()
    )


def _normalized_text(memory: Memory) -> str:
    parts = [memory.summary or "", memory.content or ""]
    return " ".join(" ".join(parts).lower().split())


def _contains_cue(text: str, cue: str) -> bool:
    pattern = r"\b" + r"\s+".join(re.escape(part) for part in cue.lower().split()) + r"\b"
    return re.search(pattern, text) is not None


def _cue_polarity(text: str) -> int:
    anchor_tokens = _anchor_tokens(text)
    negative = any(_contains_cue(text, cue) for cue in _NEGATIVE_CUES)
    positive_text = text
    if negative:
        for cue in _NEGATIVE_CUES:
            pattern = r"\b" + r"\s+".join(re.escape(part) for part in cue.lower().split()) + r"\b"
            positive_text = re.sub(pattern, " ", positive_text)
    positive = any(
        _contains_cue(positive_text, cue)
        for cue in _POSITIVE_CUES
        if cue not in _AMBIGUOUS_STATUS_POSITIVE_CUES or len(anchor_tokens) <= 2
    )
    if positive and not negative:
        return 1
    if negative and not positive:
        return -1
    return 0


def _anchor_tokens(text: str) -> set[str]:
    cue_words = {
        token
        for cue in (*_POSITIVE_CUES, *_NEGATIVE_CUES)
        for token in re.findall(r"[a-z0-9]+", cue)
    }
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", text)
        if token not in _STOPWORDS and token not in cue_words
    ]
    return set(tokens[:4])


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


def _priority(memory: Memory) -> tuple[float, float]:
    anchor = _memory_age_anchor(memory)
    recency = anchor.timestamp()
    score = (
        float(memory.confidence or 0.0) * 2.0
        + float(memory.importance or 0.0)
        + min(float(memory.reinforcement or 0.0), 3.0) * 0.2
    )
    return score, recency


def _staleness_step(memory: Memory, *, now: datetime) -> tuple[int, int]:
    anchor = _memory_age_anchor(memory)
    age_days = max(0, int((now - anchor).total_seconds() // 86_400))
    window = _STALE_WINDOWS_DAYS.get(memory.kind, 180)
    if age_days <= window:
        return 0, age_days
    if age_days <= window * 2:
        return 1, age_days
    if age_days <= window * 3:
        return 2, age_days
    if age_days <= window * 4:
        return 3, age_days
    return 4, age_days


async def apply_memory_decay_policies(
    *,
    now: datetime | None = None,
) -> DecayMaintenanceResult:
    resolved_now = _normalize_timestamp(now) or _now()
    contradiction_count = 0
    superseded_count = 0
    decayed_count = 0
    archived_count = 0

    async with get_session() as db:
        active_memories = (
            await db.execute(
                select(Memory)
                .where(Memory.status == MemoryStatus.active)
                .order_by(
                    col(Memory.kind).asc(),
                    col(Memory.created_at).asc(),
                )
            )
        ).scalars().all()

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
