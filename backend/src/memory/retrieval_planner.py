from __future__ import annotations

from dataclasses import dataclass, field

from src.db.models import MemoryEntityType, MemoryKind
from src.memory.hybrid_retrieval import HybridMemoryHit, _apply_contradiction_aware_ranking, retrieve_hybrid_memory
from src.memory.providers import retrieve_additive_memory_provider_context
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
    provider_diagnostics: tuple[dict[str, object], ...] = ()
    retrieval_diagnostics: tuple[dict[str, object], ...] = ()
    decision_receipt: dict[str, object] = field(default_factory=dict)


def _normalize_topic(value: str | None) -> str:
    normalized = "".join(
        character.lower() if character.isalnum() else " "
        for character in str(value or "")
    )
    return " ".join(normalized.split())


def _text_matches_topic(candidate: str, topic: str | None) -> bool:
    normalized_candidate = _normalize_topic(candidate)
    normalized_topic = _normalize_topic(topic)
    if not normalized_candidate or not normalized_topic:
        return False
    return (
        normalized_topic in normalized_candidate
        or normalized_candidate in normalized_topic
    )


def _shares_topic_token(candidate: str, topic: str | None) -> bool:
    normalized_candidate = _normalize_topic(candidate)
    normalized_topic = _normalize_topic(topic)
    if not normalized_candidate or not normalized_topic:
        return False
    candidate_tokens = {token for token in normalized_candidate.split() if len(token) >= 4}
    topic_tokens = {token for token in normalized_topic.split() if len(token) >= 4}
    if not candidate_tokens or not topic_tokens:
        return False
    return bool(candidate_tokens & topic_tokens)


def _project_hint_candidates(
    *,
    query: str,
    active_projects: tuple[str, ...],
    structured_buckets: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    if active_projects:
        return active_projects
    normalized_query = query.strip()
    if not normalized_query:
        return ()
    hinted_projects: list[str] = []
    for project in structured_buckets.get("project", ()):
        normalized_project = str(project or "").strip()
        if not normalized_project:
            continue
        if _text_matches_topic(normalized_project, normalized_query) or _shares_topic_token(
            normalized_project,
            normalized_query,
        ):
            hinted_projects.append(normalized_project)
    deduped: list[str] = []
    for project in hinted_projects:
        if project not in deduped:
            deduped.append(project)
    return tuple(deduped)


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


def _suppress_structured_context_contradictions(
    lines: list[str],
) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    hits: list[HybridMemoryHit] = []
    for index, line in enumerate(lines):
        if not line.startswith("- [") or "] " not in line:
            continue
        bucket, _, text = line.removeprefix("- [").partition("] ")
        if not bucket.strip() or not text.strip():
            continue
        hits.append(
            HybridMemoryHit(
                text=text.strip(),
                bucket=bucket.strip(),
                source="structured",
                score=float(len(lines) - index),
            )
        )
    if not hits:
        return lines, {}
    ranked, _diagnostics = _apply_contradiction_aware_ranking(tuple(hits))
    kept_lines = [f"- [{hit.bucket}] {hit.text}" for hit in ranked]
    buckets: dict[str, list[str]] = {}
    for hit in ranked:
        bucket = buckets.setdefault(hit.bucket, [])
        if hit.text not in bucket:
            bucket.append(hit.text)
    return kept_lines, {key: tuple(values) for key, values in buckets.items()}


def _prefer_episodic_lane(query: str) -> bool:
    normalized = query.strip().lower()
    return any(cue in normalized for cue in _EPISODIC_CUES)


def _provider_uses_user_model(diagnostics: tuple[dict[str, object], ...]) -> bool:
    for item in diagnostics:
        capabilities_used = item.get("capabilities_used")
        if isinstance(capabilities_used, list) and "user_model" in capabilities_used:
            return True
    return False


def _provider_capability_values(
    diagnostics: tuple[dict[str, object], ...],
    key: str,
) -> list[str]:
    values: list[str] = []
    for item in diagnostics:
        raw_values = item.get(key)
        if not isinstance(raw_values, list):
            continue
        for value in raw_values:
            normalized = str(value or "").strip()
            if normalized and normalized not in values:
                values.append(normalized)
    return values


def _provider_suppression_count(diagnostics: tuple[dict[str, object], ...], key: str) -> int:
    total = 0
    for item in diagnostics:
        value = item.get(key)
        if isinstance(value, int):
            total += value
    return total


def _hybrid_suppression_count(diagnostics: tuple[dict[str, object], ...]) -> int:
    total = 0
    for item in diagnostics:
        value = item.get("suppressed_contradiction_count")
        if isinstance(value, int):
            total += value
    return total


def _memory_decision_receipt(
    *,
    lane: str,
    semantic_context: str,
    episodic_context: str,
    structured_context: str,
    provider_context: str,
    degraded: bool,
    provider_diagnostics: tuple[dict[str, object], ...],
    retrieval_diagnostics: tuple[dict[str, object], ...],
    memory_buckets: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    provider_capabilities_used = _provider_capability_values(provider_diagnostics, "capabilities_used")
    provider_failed_capabilities = _provider_capability_values(provider_diagnostics, "failed_capabilities")
    stale_provider_hit_count = _provider_suppression_count(provider_diagnostics, "stale_hit_count")
    irrelevant_provider_hit_count = _provider_suppression_count(
        provider_diagnostics,
        "suppressed_irrelevant_hit_count",
    )
    contradiction_suppression_count = _hybrid_suppression_count(retrieval_diagnostics)
    suppression_count = (
        stale_provider_hit_count
        + irrelevant_provider_hit_count
        + contradiction_suppression_count
    )
    context_changed_decision = bool(
        semantic_context.strip()
        or episodic_context.strip()
        or provider_context.strip()
        or structured_context.strip()
    )
    return {
        "receipt_type": "memory_decision",
        "changed_decision": context_changed_decision or suppression_count > 0,
        "changed_intervention_timing": bool(episodic_context.strip() or provider_capabilities_used),
        "lane": lane,
        "intervention_timing": (
            "episodic_recall_injected"
            if episodic_context.strip()
            else "provider_model_augmented"
            if provider_capabilities_used
            else "canonical_memory_injected"
            if semantic_context.strip() or structured_context.strip()
            else "no_memory_context"
        ),
        "capability_choice": {
            "lane": lane,
            "canonical_guardian_memory": bool(structured_context.strip() or semantic_context.strip() or episodic_context.strip()),
            "provider_capabilities_used": provider_capabilities_used,
            "provider_failed_capabilities": provider_failed_capabilities,
            "degraded": degraded,
        },
        "suppression": {
            "suppressed_count": suppression_count,
            "lower_ranked_contradiction_count": contradiction_suppression_count,
            "stale_provider_hit_count": stale_provider_hit_count,
            "irrelevant_provider_hit_count": irrelevant_provider_hit_count,
            "reasons": [
                reason
                for reason, count in (
                    ("lower_ranked_contradiction", contradiction_suppression_count),
                    ("stale_provider_evidence", stale_provider_hit_count),
                    ("irrelevant_provider_evidence", irrelevant_provider_hit_count),
                )
                if count
            ],
        },
        "provenance": {
            "guardian_canonical": bool(structured_context.strip() or semantic_context.strip() or episodic_context.strip()),
            "external_advisory": bool(provider_context.strip() or provider_capabilities_used),
            "policy": "canonical_first",
        },
        "confidence": {
            "degraded": degraded,
            "bucket_count": len(memory_buckets),
            "provider_quality_states": [
                str(item.get("quality_state") or "")
                for item in provider_diagnostics
                if item.get("quality_state")
            ],
        },
        "privacy_boundary": "operator_visible",
        "auditability": {
            "retrieval_diagnostics_visible": bool(retrieval_diagnostics),
            "provider_diagnostics_visible": bool(provider_diagnostics),
        },
    }


def _memory_context_text(kind_name: str, memory) -> str:
    if kind_name == MemoryKind.procedural.value:
        return (memory.content or memory.summary or "").strip()
    return (memory.summary or memory.content or "").strip()


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
            MemoryKind.pattern,
            MemoryKind.project,
            MemoryKind.collaborator,
            MemoryKind.obligation,
            MemoryKind.routine,
            MemoryKind.timeline,
        ),
        limit_per_kind=2,
    )
    procedural_memories = await memory_repository.list_memories(
        kind=MemoryKind.procedural,
        limit=4,
    )
    if procedural_memories:
        grouped[MemoryKind.procedural.value] = procedural_memories

    bucketed: dict[str, list[str]] = {}
    lines: list[str] = []
    for kind_name in (
        MemoryKind.goal.value,
        MemoryKind.commitment.value,
        MemoryKind.preference.value,
        MemoryKind.communication_preference.value,
        MemoryKind.procedural.value,
        MemoryKind.pattern.value,
        MemoryKind.project.value,
        MemoryKind.collaborator.value,
        MemoryKind.obligation.value,
        MemoryKind.routine.value,
        MemoryKind.timeline.value,
    ):
        memories = grouped.get(kind_name, [])
        if not memories:
            continue
        bucket_name = bucket_name_for_kind(kind_name)
        if kind_name == MemoryKind.procedural.value:
            texts = [
                _memory_context_text(kind_name, memory)
                for memory in memories
                if _memory_context_text(kind_name, memory)
            ]
            if texts:
                bucket = bucketed.setdefault(bucket_name, [])
                for text in texts:
                    if text not in bucket:
                        bucket.append(text)
                combined_line = f"- [{bucket_name}] {' | '.join(texts)}"
                if combined_line not in lines:
                    lines.append(combined_line)
            continue
        for memory in memories:
            text = _memory_context_text(kind_name, memory)
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
                text=_memory_context_text(memory.kind.value, memory),
                bucket_name=bucket_name_for_kind(memory.kind),
            )

    filtered_lines, _filtered_buckets = _suppress_structured_context_contradictions(lines)
    return "\n".join(filtered_lines[:8]), {key: tuple(values) for key, values in bucketed.items()}


async def plan_memory_retrieval(
    *,
    query: str,
    active_projects: tuple[str, ...] = (),
) -> MemoryRetrievalPlanResult:
    structured_context, structured_buckets = await build_structured_memory_context_bundle(
        active_projects=active_projects,
    )
    normalized_query = query.strip()
    provider_project_hints = _project_hint_candidates(
        query=normalized_query,
        active_projects=active_projects,
        structured_buckets=structured_buckets,
    )
    provider_retrieval = await retrieve_additive_memory_provider_context(
        query=normalized_query,
        active_projects=provider_project_hints,
        limit=3,
        include_user_model=bool(provider_project_hints),
    )
    if not normalized_query:
        semantic_context = _merge_contexts(structured_context, provider_retrieval.context)
        buckets = _merge_buckets(structured_buckets, provider_retrieval.buckets)
        lane = "structured_plus_provider_model" if _provider_uses_user_model(provider_retrieval.diagnostics) else "structured_only"
        return MemoryRetrievalPlanResult(
            semantic_context=semantic_context,
            episodic_context="",
            memory_buckets=buckets,
            degraded=False,
            lane=lane,
            provider_diagnostics=provider_retrieval.diagnostics,
            retrieval_diagnostics=(),
            decision_receipt=_memory_decision_receipt(
                lane=lane,
                semantic_context=semantic_context,
                episodic_context="",
                structured_context=structured_context,
                provider_context=provider_retrieval.context,
                degraded=False,
                provider_diagnostics=provider_retrieval.diagnostics,
                retrieval_diagnostics=(),
                memory_buckets=buckets,
            ),
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
    lane = "episodic" if _prefer_episodic_lane(normalized_query) else "hybrid"
    if provider_retrieval.context:
        lane = f"{lane}_plus_provider_model" if _provider_uses_user_model(provider_retrieval.diagnostics) else f"{lane}_plus_provider"
    merged_semantic_context = _merge_contexts(structured_context, semantic_context, provider_retrieval.context)
    merged_buckets = _merge_buckets(structured_buckets, semantic_buckets, provider_retrieval.buckets)
    degraded = hybrid.degraded or provider_retrieval.degraded

    return MemoryRetrievalPlanResult(
        semantic_context=merged_semantic_context,
        episodic_context=episodic_context,
        memory_buckets=merged_buckets,
        degraded=degraded,
        lane=lane,
        provider_diagnostics=provider_retrieval.diagnostics,
        retrieval_diagnostics=hybrid.diagnostics,
        decision_receipt=_memory_decision_receipt(
            lane=lane,
            semantic_context=merged_semantic_context,
            episodic_context=episodic_context,
            structured_context=structured_context,
            provider_context=provider_retrieval.context,
            degraded=degraded,
            provider_diagnostics=provider_retrieval.diagnostics,
            retrieval_diagnostics=hybrid.diagnostics,
            memory_buckets=merged_buckets,
        ),
    )
