from __future__ import annotations

from dataclasses import dataclass

from src.db.models import MemoryEntityType, MemoryKind
from src.memory.hybrid_retrieval import HybridMemoryHit, retrieve_hybrid_memory
from src.memory.repository import memory_repository
from src.memory.types import bucket_name_for_kind


_EPISODIC_CUES = (
    "did we",
    "happened",
    "last ",
    "timeline",
    "when ",
    "yesterday",
    "earlier",
    "history",
    "recently",
)


@dataclass(frozen=True)
class MemoryRetrievalPlanResult:
    semantic_context: str
    episodic_context: str
    memory_buckets: dict[str, tuple[str, ...]]
    degraded: bool
    lane: str


def _append_structured_memory_line(
    *,
    bucketed: dict[str, list[str]],
    lines: list[str],
    text: str,
    bucket_name: str,
) -> None:
    normalized = text.strip()
    if not normalized:
        return
    bucket = bucketed.setdefault(bucket_name, [])
    if normalized not in bucket:
        bucket.append(normalized)
    line = f"- [{bucket_name}] {normalized}"
    if line not in lines:
        lines.append(line)


def _merge_contexts(*contexts: str) -> str:
    lines: list[str] = []
    for context in contexts:
        for raw_line in context.splitlines():
            line = raw_line.strip()
            if not line or line in lines:
                continue
            lines.append(line)
    return "\n".join(lines)


def _render_hits(hits: list[HybridMemoryHit], *, limit: int) -> tuple[str, dict[str, tuple[str, ...]]]:
    lines: list[str] = []
    buckets: dict[str, list[str]] = {}
    for hit in hits[:limit]:
        bucket = buckets.setdefault(hit.bucket, [])
        if hit.text not in bucket:
            bucket.append(hit.text)
        line = f"- [{hit.bucket}] {hit.text}"
        if line not in lines:
            lines.append(line)
    return "\n".join(lines), {key: tuple(values) for key, values in buckets.items()}


def _merge_buckets(
    *bucket_maps: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    merged: dict[str, list[str]] = {}
    for bucket_map in bucket_maps:
        for bucket, texts in bucket_map.items():
            values = merged.setdefault(bucket, [])
            for text in texts:
                if text not in values:
                    values.append(text)
    return {key: tuple(values) for key, values in merged.items()}


def _prefer_episodic_lane(query: str) -> bool:
    normalized = query.strip().lower()
    return any(cue in normalized for cue in _EPISODIC_CUES)


async def build_structured_memory_context_bundle(
    *,
    active_projects: tuple[str, ...] = (),
) -> tuple[str, dict[str, tuple[str, ...]]]:
    grouped = await memory_repository.list_memories_by_kinds(
        kinds=(
            MemoryKind.goal,
            MemoryKind.commitment,
            MemoryKind.preference,
            MemoryKind.communication_preference,
            MemoryKind.procedural,
            MemoryKind.pattern,
            MemoryKind.project,
            MemoryKind.collaborator,
            MemoryKind.obligation,
            MemoryKind.routine,
            MemoryKind.timeline,
        ),
        limit_per_kind=2,
    )

    bucketed: dict[str, list[str]] = {}
    lines: list[str] = []
    for kind_name, memories in grouped.items():
        bucket_name = bucket_name_for_kind(kind_name)
        for memory in memories:
            text = (memory.summary or memory.content or "").strip()
            _append_structured_memory_line(
                bucketed=bucketed,
                lines=lines,
                text=text,
                bucket_name=bucket_name,
            )

    linked_project_entities = await memory_repository.find_entities_by_names(
        names=active_projects,
        entity_type=MemoryEntityType.project,
    )
    if linked_project_entities:
        linked_memories = await memory_repository.list_memories_for_entities(
            project_entity_ids=tuple(entity.id for entity in linked_project_entities.values()),
            kinds=(
                MemoryKind.commitment,
                MemoryKind.project,
                MemoryKind.collaborator,
                MemoryKind.obligation,
                MemoryKind.routine,
                MemoryKind.timeline,
            ),
            limit=8,
        )
        for memory in linked_memories:
            _append_structured_memory_line(
                bucketed=bucketed,
                lines=lines,
                text=(memory.summary or memory.content or "").strip(),
                bucket_name=bucket_name_for_kind(memory.kind),
            )

    return "\n".join(lines[:8]), {key: tuple(values) for key, values in bucketed.items()}


async def plan_memory_retrieval(
    *,
    query: str,
    active_projects: tuple[str, ...] = (),
) -> MemoryRetrievalPlanResult:
    structured_context, structured_buckets = await build_structured_memory_context_bundle(
        active_projects=active_projects,
    )
    normalized_query = query.strip()
    if not normalized_query:
        return MemoryRetrievalPlanResult(
            semantic_context=structured_context,
            episodic_context="",
            memory_buckets=structured_buckets,
            degraded=False,
            lane="structured_only",
        )

    hybrid = await retrieve_hybrid_memory(
        query=normalized_query,
        active_projects=active_projects,
        limit=8,
    )
    semantic_hits = [hit for hit in hybrid.hits if hit.bucket != "episode"]
    episodic_hits = [hit for hit in hybrid.hits if hit.bucket == "episode"]
    semantic_context, semantic_buckets = _render_hits(
        semantic_hits,
        limit=6 if not _prefer_episodic_lane(normalized_query) else 3,
    )
    episodic_context, _episode_buckets = _render_hits(
        episodic_hits,
        limit=4 if _prefer_episodic_lane(normalized_query) else 2,
    )

    return MemoryRetrievalPlanResult(
        semantic_context=_merge_contexts(structured_context, semantic_context),
        episodic_context=episodic_context,
        memory_buckets=_merge_buckets(structured_buckets, semantic_buckets),
        degraded=hybrid.degraded,
        lane="episodic" if _prefer_episodic_lane(normalized_query) else "hybrid",
    )
