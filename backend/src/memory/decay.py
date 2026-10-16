from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import bindparam, exists, func, insert, text, update
from sqlmodel import col, select

from src.db.engine import get_session
from src.db.models import (
    Memory,
    MemoryEdge,
    MemoryEdgeType,
    MemoryKind,
    MemoryStatus,
    MemoryTombstone,
)
from src.memory.repository import (
    _begin_canonical_write,
    _canonical_memory_deletion_marker,
    _canonical_memory_without_tombstone_clause,
    memory_repository,
)

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


async def summarize_memory_reconciliation_state(
    *,
    limit: int = 5,
    owner_session_id: str | None = None,
    content_free: bool = False,
) -> dict[str, object]:
    """Return reconciliation state for either the global or one owner scope.

    Global callers retain the existing diagnostic rows for trusted internal
    maintenance and benchmark surfaces.  Operator-facing owner scopes use a
    content-free preview so a reconciliation receipt cannot become a side
    channel for another memory's summary, content, or metadata reason.
    """

    bounded_limit = min(max(int(limit or 5), 1), 50)
    normalized_owner = str(owner_session_id or "").strip() or None

    def _owner_filter(statement):
        if normalized_owner:
            return statement.where(Memory.source_session_id == normalized_owner)
        return statement

    try:
        async with get_session() as db:
            superseded_statement = _owner_filter(
                select(Memory)
                .where(Memory.status == MemoryStatus.superseded)
                .where(_canonical_memory_without_tombstone_clause())
                .order_by(col(Memory.updated_at).desc(), col(Memory.created_at).desc())
            ).limit(bounded_limit)
            superseded_memories = (await db.execute(superseded_statement)).scalars().all()
            archived_statement = _owner_filter(
                select(Memory)
                .where(Memory.status == MemoryStatus.archived)
                .where(_canonical_memory_without_tombstone_clause())
                .order_by(col(Memory.updated_at).desc(), col(Memory.created_at).desc())
            ).limit(bounded_limit)
            archived_memories = (await db.execute(archived_statement)).scalars().all()
            superseded_memories = [
                memory
                for memory in superseded_memories
                if _canonical_memory_deletion_marker(memory) is None
            ]
            archived_memories = [
                memory
                for memory in archived_memories
                if _canonical_memory_deletion_marker(memory) is None
            ]
            for memory in (*superseded_memories, *archived_memories):
                db.expunge(memory)

            async def _count_status(status: MemoryStatus) -> int:
                statement = _owner_filter(
                    select(func.count())
                    .select_from(Memory)
                    .where(Memory.status == status)
                    .where(_canonical_memory_without_tombstone_clause())
                )
                return int((await db.execute(statement)).scalar_one() or 0)

            active_count = await _count_status(MemoryStatus.active)
            superseded_count = await _count_status(MemoryStatus.superseded)
            archived_count = await _count_status(MemoryStatus.archived)
            contradiction_statement = (
                select(func.count())
                .select_from(MemoryEdge)
                .where(MemoryEdge.edge_type == MemoryEdgeType.contradicts)
                .where(
                    ~exists().where(
                        MemoryTombstone.memory_id == MemoryEdge.from_memory_id
                    )
                )
                .where(
                    ~exists().where(
                        MemoryTombstone.memory_id == MemoryEdge.to_memory_id
                    )
                )
            )
            if normalized_owner:
                contradiction_statement = contradiction_statement.where(
                    exists().where(
                        Memory.id == MemoryEdge.from_memory_id,
                        Memory.source_session_id == normalized_owner,
                    )
                ).where(
                    exists().where(
                        Memory.id == MemoryEdge.to_memory_id,
                        Memory.source_session_id == normalized_owner,
                    )
                )
            contradiction_edge_count = int(
                (await db.execute(contradiction_statement)).scalar_one() or 0
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
            "scope": "owner" if normalized_owner else "global",
            "owner_session_id": normalized_owner,
            "content_free": bool(content_free),
        }

    recent_conflicts: list[dict[str, object]] = []
    if not content_free:
        for memory in superseded_memories[:bounded_limit]:
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
    if not content_free:
        for memory in archived_memories[:bounded_limit]:
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

    has_conflicts = bool(recent_conflicts) if not content_free else superseded_count > 0
    has_archivals = bool(recent_archivals) if not content_free else archived_count > 0
    if has_conflicts and has_archivals:
        state = "conflict_and_forgetting_active"
    elif has_conflicts:
        state = "conflict_reconciled"
    elif has_archivals:
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
        "scope": "owner" if normalized_owner else "global",
        "owner_session_id": normalized_owner,
        "content_free": bool(content_free),
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


@dataclass(frozen=True)
class _ContradictionFeatures:
    anchors: frozenset[str]
    polarity: int


def _contradiction_features(memory) -> _ContradictionFeatures:
    text = _normalized_text(memory)
    return _ContradictionFeatures(frozenset(_anchor_tokens(text)), _cue_polarity(text))


def _shares_entity(memory, peer) -> bool:
    return (
        memory.subject_entity_id
        and memory.subject_entity_id == peer.subject_entity_id
    ) or (
        memory.project_entity_id
        and memory.project_entity_id == peer.project_entity_id
    )


def _contradictory(memory, features: _ContradictionFeatures, peer, peer_features: _ContradictionFeatures) -> bool:
    """Whether two active memories contradict, from features computed once each."""
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
    overlap = len(features.anchors & peer_features.anchors)
    if not shared_entity and overlap < 2:
        return False
    required_overlap = 2
    if (
        shared_entity
        and memory.kind in _SHORT_ENTITY_CONTRADICTION_KINDS
        and len(features.anchors) == 1
        and len(peer_features.anchors) == 1
    ):
        required_overlap = 1
    if overlap < required_overlap:
        return False
    return (
        features.polarity != 0
        and peer_features.polarity != 0
        and features.polarity != peer_features.polarity
    )


def _priority(memory) -> tuple[float, float]:
    anchor = _memory_age_anchor(memory)
    recency = anchor.timestamp()
    score = (
        float(memory.confidence or 0.0) * 2.0
        + float(memory.importance or 0.0)
        + min(float(memory.reinforcement or 0.0), 3.0) * 0.2
    )
    return score, recency


def _staleness_step(memory, *, now: datetime) -> tuple[int, int]:
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


# Columns decay rewrites. With the text they are also the per-row
# compare-and-set witness between planning and applying (#1085): any edit
# through the repository changes at least ``updated_at``, and the deletion
# marker depends only on these columns.
_DECAY_COLUMNS = ("status", "confidence", "reinforcement", "metadata_json", "updated_at")
_WITNESS_COLUMNS = (*_DECAY_COLUMNS, "content", "summary")


@dataclass
class _DecayRow:
    """Detached copy of one active memory, safe to plan on a worker thread."""

    id: str
    kind: MemoryKind
    subject_entity_id: str | None
    project_entity_id: str | None
    summary: str | None
    content: str
    confidence: float
    importance: float
    reinforcement: float
    status: MemoryStatus
    metadata_json: str | None
    created_at: datetime | None
    updated_at: datetime | None
    last_confirmed_at: datetime | None

    @classmethod
    def of(cls, memory: Memory) -> "_DecayRow":
        return cls(**{name: getattr(memory, name) for name in cls.__dataclass_fields__})


@dataclass
class _DecayPlan:
    supersedes: list[tuple[str, str]] = field(default_factory=list)
    before: dict[str, tuple] = field(default_factory=dict)
    after: dict[str, dict[str, object]] = field(default_factory=dict)
    decayed: set[str] = field(default_factory=set)
    archived: set[str] = field(default_factory=set)
    edges: list[dict[str, object]] = field(default_factory=list)


def _plan_memory_decay(rows: list[_DecayRow], now: datetime) -> _DecayPlan:
    """Pure decay plan: the pre-#1085 pairwise algorithm, without its n² scan.

    Rows must arrive in (kind, created_at) order. A pair can only contradict
    when both are in a comparable kind, have opposite non-zero cue polarity
    and share an anchor token, so an inverted index over those features finds
    every candidate. Candidates are visited in the original pair order, and
    statuses still change as the scan goes, so the greedy outcome (who
    supersedes whom) is identical.
    """
    plan = _DecayPlan()
    witness = {row.id: tuple(getattr(row, name) for name in _WITNESS_COLUMNS) for row in rows}
    features = [_contradiction_features(row) for row in rows]
    postings: dict[tuple[MemoryKind, str], list[int]] = {}
    for index, row in enumerate(rows):
        if row.kind in _COMPARABLE_KINDS and features[index].polarity != 0:
            for token in features[index].anchors:
                postings.setdefault((row.kind, token), []).append(index)

    for index, memory in enumerate(rows):
        if (
            memory.status != MemoryStatus.active
            or memory.kind not in _COMPARABLE_KINDS
            or features[index].polarity == 0
        ):
            continue
        candidates = sorted({
            peer_index
            for token in features[index].anchors
            for peer_index in postings.get((memory.kind, token), ())
            if peer_index > index
        })
        for peer_index in candidates:
            peer = rows[peer_index]
            if peer.status != MemoryStatus.active or not _contradictory(
                memory, features[index], peer, features[peer_index]
            ):
                continue
            winner, loser = (memory, peer)
            if _priority(peer) > _priority(memory):
                winner, loser = peer, memory
            loser.status = MemoryStatus.superseded
            loser.updated_at = now
            loser_metadata = json.loads(loser.metadata_json or "{}")
            loser_metadata["contradicted_by_memory_id"] = winner.id
            loser_metadata["superseded_by_memory_id"] = winner.id
            loser_metadata["superseded_reason"] = "contradiction"
            loser.metadata_json = json.dumps(loser_metadata, sort_keys=True)
            plan.supersedes.append((winner.id, loser.id))

    for memory in rows:
        if memory.status != MemoryStatus.active:
            continue
        step, age_days = _staleness_step(memory, now=now)
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
            memory.updated_at = now
            plan.decayed.add(memory.id)

        if step >= 4 and (
            float(memory.confidence or 0.0) <= 0.35
            or float(memory.reinforcement or 0.0) <= 0.2
        ):
            metadata = json.loads(memory.metadata_json or "{}")
            metadata["archived_reason"] = "stale"
            metadata["archived_at"] = now.isoformat()
            memory.metadata_json = json.dumps(metadata, sort_keys=True)
            memory.status = MemoryStatus.archived
            memory.updated_at = now
            plan.archived.add(memory.id)

    for row in rows:
        current = tuple(getattr(row, name) for name in _WITNESS_COLUMNS)
        if current != witness[row.id]:
            plan.before[row.id] = witness[row.id]
            plan.after[row.id] = {name: getattr(row, name) for name in _DECAY_COLUMNS}
    created_at = _now()
    for winner_id, loser_id in plan.supersedes:
        for edge_type in (MemoryEdgeType.contradicts, MemoryEdgeType.supersedes):
            plan.edges.append({
                "id": uuid.uuid4().hex,
                "from_memory_id": winner_id,
                "to_memory_id": loser_id,
                "edge_type": edge_type,
                "weight": 1.0,
                "metadata_json": json.dumps({"writer": "memory_decay"}, sort_keys=True),
                "created_at": created_at,
            })
    return plan


def _plan_from_records(records: list, now: datetime) -> _DecayPlan:
    rows = [
        _DecayRow.of(record)
        for record in records
        if _canonical_memory_deletion_marker(record) is None
    ]
    return _plan_memory_decay(rows, now)


# Rows applied per writer transaction: inside SQLite's bound-parameter limit
# and short enough (~tens of ms) that other writers interleave (#1085).
_DECAY_APPLY_CHUNK = 500


async def apply_memory_decay_policies(
    *,
    now: datetime | None = None,
) -> DecayMaintenanceResult:
    """Supersede contradicted memories and decay or archive stale ones.

    #1085: the scan used to run inside ``BEGIN IMMEDIATE`` on the event loop
    and compared every pair (16s of writer lock and loop stall at 5,000
    memories). It now plans from a deferred snapshot on a worker thread and
    takes the writer lock only to apply the plan; a row edited since it was
    read is left untouched for the next run.
    """
    resolved_now = _normalize_timestamp(now) or _now()

    table = Memory.__table__
    async with get_session() as db:
        await db.execute(text("BEGIN"))
        records = (
            await db.execute(
                select(*(table.c[name] for name in _DecayRow.__dataclass_fields__))
                .where(table.c.status == MemoryStatus.active)
                .where(_canonical_memory_without_tombstone_clause())
                .order_by(table.c.kind.asc(), table.c.created_at.asc())
            )
        ).all()
    plan = await asyncio.to_thread(_plan_from_records, records, resolved_now)
    if not plan.after:
        return DecayMaintenanceResult()

    applied: set[str] = set()
    edges_by_loser: dict[str, list[dict[str, object]]] = {}
    for edge in plan.edges:
        edges_by_loser.setdefault(str(edge["to_memory_id"]), []).append(edge)
    ids = list(plan.after)
    witness_columns = [table.c.id, *(table.c[name] for name in _WITNESS_COLUMNS)]
    for start in range(0, len(ids), _DECAY_APPLY_CHUNK):
        # Each chunk is its own short writer transaction: rows are
        # independent compare-and-set updates, and a superseded row commits
        # together with its edges, so other writers interleave between chunks.
        chunk = ids[start:start + _DECAY_APPLY_CHUNK]
        async with get_session() as db:
            await _begin_canonical_write(db)
            current = await db.execute(
                select(*witness_columns)
                .where(table.c.id.in_(chunk))
                .where(_canonical_memory_without_tombstone_clause())
            )
            chunk_applied = [row[0] for row in current if tuple(row[1:]) == plan.before[row[0]]]
            if not chunk_applied:
                continue
            await db.execute(
                update(table)
                .where(table.c.id == bindparam("target_id"))
                .values({name: bindparam(f"new_{name}") for name in _DECAY_COLUMNS}),
                [
                    {"target_id": memory_id, **{f"new_{name}": value for name, value in plan.after[memory_id].items()}}
                    for memory_id in chunk_applied
                ],
            )
            applied.update(chunk_applied)
            losers = [memory_id for memory_id in chunk_applied if memory_id in edges_by_loser]
            if losers:
                existing_edges = {
                    (row[0], row[1], MemoryEdgeType(row[2]))
                    for row in await db.execute(
                        select(MemoryEdge.from_memory_id, MemoryEdge.to_memory_id, MemoryEdge.edge_type)
                        .where(col(MemoryEdge.to_memory_id).in_(losers))
                    )
                }
                new_edges = [
                    edge
                    for loser in losers
                    for edge in edges_by_loser[loser]
                    if (edge["from_memory_id"], edge["to_memory_id"], edge["edge_type"]) not in existing_edges
                ]
                if new_edges:
                    await db.execute(insert(MemoryEdge.__table__), new_edges)
    contradiction_count = sum(1 for _winner, loser in plan.supersedes if loser in applied)

    return DecayMaintenanceResult(
        contradiction_count=contradiction_count,
        superseded_count=contradiction_count,
        decayed_count=len(plan.decayed & applied),
        archived_count=len(plan.archived & applied),
    )
