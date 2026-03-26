from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_
from sqlmodel import col, select

from src.db.engine import get_session
from src.db.models import Memory, MemoryEpisode, MemoryStatus
from src.memory.repository import memory_repository
from src.memory.types import bucket_name_for_kind
from src.memory.vector_store import search_with_status


_STOPWORDS = {
    "about",
    "after",
    "before",
    "could",
    "does",
    "from",
    "have",
    "into",
    "last",
    "matters",
    "next",
    "right",
    "should",
    "that",
    "them",
    "they",
    "this",
    "today",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "with",
}


@dataclass(frozen=True)
class HybridMemoryHit:
    text: str
    bucket: str
    source: str
    score: float
    created_at: datetime | None = None


@dataclass(frozen=True)
class HybridMemoryRetrievalResult:
    context: str
    buckets: dict[str, tuple[str, ...]]
    degraded: bool
    hits: tuple[HybridMemoryHit, ...]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_text(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def _memory_hit_text(memory: Memory) -> str:
    if memory.kind.value == "procedural":
        return _normalize_text(memory.content or memory.summary)
    return _normalize_text(memory.summary or memory.content)


def _query_terms(query: str) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", query.lower()):
        if len(raw) < 3 or raw in _STOPWORDS or raw in seen:
            continue
        seen.add(raw)
        terms.append(raw)
    return tuple(terms)


def _term_overlap_score(text: str, terms: tuple[str, ...]) -> float:
    normalized = text.lower()
    if not terms:
        return 0.0
    matches = sum(1 for term in terms if term in normalized)
    return matches / len(terms)


def _active_project_name_boost(text: str, active_projects: tuple[str, ...]) -> float:
    normalized = text.lower()
    for project in active_projects:
        project_name = _normalize_text(project)
        if project_name and project_name.lower() in normalized:
            return 0.55
    return 0.0


def _recency_boost(value: datetime | None, *, now: datetime) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    delta_days = max(0.0, (now - value).total_seconds() / 86_400)
    if delta_days <= 1:
        return 0.35
    if delta_days <= 7:
        return 0.22
    if delta_days <= 30:
        return 0.12
    return 0.0


def _dedupe_hits(hits: list[HybridMemoryHit]) -> tuple[HybridMemoryHit, ...]:
    best_by_text: dict[str, HybridMemoryHit] = {}
    for hit in hits:
        key = hit.text.lower()
        existing = best_by_text.get(key)
        if existing is None or hit.score > existing.score:
            best_by_text[key] = hit
    return tuple(
        sorted(
            best_by_text.values(),
            key=lambda hit: (
                -hit.score,
                -(hit.created_at.timestamp() if hit.created_at else 0.0),
            ),
        )
    )


def _render_result(hits: tuple[HybridMemoryHit, ...], *, limit: int, degraded: bool) -> HybridMemoryRetrievalResult:
    selected = hits[:limit]
    buckets: dict[str, list[str]] = {}
    lines: list[str] = []
    for hit in selected:
        bucket = buckets.setdefault(hit.bucket, [])
        if hit.text not in bucket:
            bucket.append(hit.text)
        line = f"- [{hit.bucket}] {hit.text}"
        if line not in lines:
            lines.append(line)
    return HybridMemoryRetrievalResult(
        context="\n".join(lines),
        buckets={key: tuple(values) for key, values in buckets.items()},
        degraded=degraded,
        hits=selected,
    )


async def retrieve_hybrid_memory(
    *,
    query: str,
    active_projects: tuple[str, ...] = (),
    limit: int = 8,
) -> HybridMemoryRetrievalResult:
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return HybridMemoryRetrievalResult(context="", buckets={}, degraded=False, hits=())

    terms = _query_terms(normalized_query)
    project_entities = await memory_repository.find_entities_by_names(
        names=active_projects,
        entity_type="project",
    )
    project_entity_ids = tuple(
        dict.fromkeys(entity.id for entity in project_entities.values())
    )
    query_term_patterns = [f"%{term}%" for term in terms]
    now = _now()

    async with get_session() as db:
        memory_text = func.lower(func.coalesce(Memory.summary, "") + " " + func.coalesce(Memory.content, ""))
        episode_text = func.lower(
            func.coalesce(MemoryEpisode.summary, "") + " " + func.coalesce(MemoryEpisode.content, "")
        )

        semantic_stmt = (
            select(Memory)
            .where(Memory.status == MemoryStatus.active)
            .order_by(
                col(Memory.importance).desc(),
                col(Memory.last_confirmed_at).desc(),
                col(Memory.created_at).desc(),
            )
            .limit(max(limit * 6, 24))
        )
        if query_term_patterns:
            semantic_stmt = semantic_stmt.where(
                or_(*[memory_text.like(pattern) for pattern in query_term_patterns])
            )
        semantic_result = await db.execute(semantic_stmt)
        semantic_memories = semantic_result.scalars().all()

        linked_memories: list[Memory] = []
        if project_entity_ids:
            linked_stmt = (
                select(Memory)
                .where(Memory.status == MemoryStatus.active)
                .where(col(Memory.project_entity_id).in_(project_entity_ids))
                .order_by(
                    col(Memory.importance).desc(),
                    col(Memory.last_confirmed_at).desc(),
                    col(Memory.created_at).desc(),
                )
                .limit(max(limit * 4, 12))
            )
            linked_result = await db.execute(linked_stmt)
            linked_memories = linked_result.scalars().all()

        episode_stmt = (
            select(MemoryEpisode)
            .order_by(
                col(MemoryEpisode.salience).desc(),
                col(MemoryEpisode.observed_at).desc(),
                col(MemoryEpisode.created_at).desc(),
            )
            .limit(max(limit * 6, 24))
        )
        if query_term_patterns:
            episode_stmt = episode_stmt.where(
                or_(*[episode_text.like(pattern) for pattern in query_term_patterns])
            )
        episode_result = await db.execute(episode_stmt)
        episodic_hits = episode_result.scalars().all()

        linked_episodes: list[MemoryEpisode] = []
        if project_entity_ids:
            linked_episode_stmt = (
                select(MemoryEpisode)
                .where(col(MemoryEpisode.project_entity_id).in_(project_entity_ids))
                .order_by(
                    col(MemoryEpisode.salience).desc(),
                    col(MemoryEpisode.observed_at).desc(),
                    col(MemoryEpisode.created_at).desc(),
                )
                .limit(max(limit * 4, 12))
            )
            linked_episode_result = await db.execute(linked_episode_stmt)
            linked_episodes = linked_episode_result.scalars().all()

    vector_hits, vector_degraded = await asyncio.to_thread(
        search_with_status,
        normalized_query,
        max(limit * 2, 8),
    )
    active_vector_ids: set[str] | None = None
    vector_hit_ids = tuple(
        str(hit.get("id") or "").strip()
        for hit in vector_hits
        if str(hit.get("id") or "").strip()
    )
    if vector_hit_ids:
        async with get_session() as db:
            active_vector_ids = set(
                (
                    await db.execute(
                        select(Memory.id)
                        .where(Memory.status == MemoryStatus.active)
                        .where(col(Memory.id).in_(vector_hit_ids))
                    )
                ).scalars().all()
            )

    combined_hits: list[HybridMemoryHit] = []
    for memory in [*semantic_memories, *linked_memories]:
        text = _memory_hit_text(memory)
        if not text:
            continue
        score = (
            (_term_overlap_score(text, terms) * 3.0)
            + (memory.importance * 1.8)
            + (memory.confidence * 0.4)
            + _recency_boost(memory.last_confirmed_at or memory.created_at, now=now)
        )
        if memory.project_entity_id in project_entity_ids:
            score += 0.7
        score += _active_project_name_boost(text, active_projects)
        combined_hits.append(
            HybridMemoryHit(
                text=text,
                bucket=bucket_name_for_kind(memory.kind),
                source="semantic",
                score=score,
                created_at=memory.last_confirmed_at or memory.created_at,
            )
        )

    for episode in [*episodic_hits, *linked_episodes]:
        text = _normalize_text(episode.summary or episode.content)
        if not text:
            continue
        score = (
            (_term_overlap_score(f"{episode.summary} {episode.content}", terms) * 3.1)
            + (episode.salience * 1.4)
            + (episode.confidence * 0.4)
            + _recency_boost(episode.observed_at or episode.created_at, now=now)
        )
        if episode.project_entity_id in project_entity_ids:
            score += 0.65
        score += _active_project_name_boost(f"{episode.summary} {episode.content}", active_projects)
        combined_hits.append(
            HybridMemoryHit(
                text=text,
                bucket="episode",
                source="episodic",
                score=score,
                created_at=episode.observed_at or episode.created_at,
            )
        )

    for hit in vector_hits:
        hit_id = str(hit.get("id") or "").strip()
        if hit_id and active_vector_ids is not None and hit_id not in active_vector_ids:
            continue
        text = _normalize_text(str(hit.get("text") or ""))
        if not text:
            continue
        distance = float(hit.get("score") or 0.0)
        created_at = None
        raw_created_at = hit.get("created_at")
        if isinstance(raw_created_at, str):
            normalized_created_at = raw_created_at.replace("Z", "+00:00")
            try:
                created_at = datetime.fromisoformat(normalized_created_at)
            except ValueError:
                created_at = None
        elif isinstance(raw_created_at, datetime):
            created_at = raw_created_at
        score = (
            max(0.0, 1.35 - distance)
            + (_term_overlap_score(text, terms) * 1.2)
            + _recency_boost(created_at, now=now)
            + _active_project_name_boost(text, active_projects)
        )
        combined_hits.append(
            HybridMemoryHit(
                text=text,
                bucket=_normalize_text(str(hit.get("category") or "fact")) or "fact",
                source="vector",
                score=score,
                created_at=created_at,
            )
        )

    return _render_result(_dedupe_hits(combined_hits), limit=limit, degraded=vector_degraded)
