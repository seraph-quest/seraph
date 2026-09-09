from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.exc import SQLAlchemyError

from src.db.models import MemoryEntityType, MemoryKind
from src.memory.hybrid_retrieval import (
    HybridMemoryHit,
    _apply_contradiction_aware_ranking,
    _contradictory_hits,
    retrieve_hybrid_memory,
)
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


def _provider_context_hit(line: str) -> HybridMemoryHit | None:
    """Parse one provider context line without retaining provider payloads."""

    if not line.startswith("- [") or "] " not in line:
        return None
    bucket, _, payload = line.removeprefix("- [").partition("] ")
    provider_name, separator, text = payload.partition(": ")
    normalized_text = _normalize_provider_claim_value(text)
    if not bucket.strip() or not separator or not provider_name.strip() or not normalized_text:
        return None
    return HybridMemoryHit(
        text=normalized_text,
        bucket=bucket.strip(),
        source="provider",
        score=0.0,
    )


def _canonical_context_hits(context: str) -> tuple[HybridMemoryHit, ...]:
    hits: list[HybridMemoryHit] = []
    for line in context.splitlines():
        if not line.startswith("- [") or "] " not in line:
            continue
        bucket, _, text = line.removeprefix("- [").partition("] ")
        if not bucket.strip() or not text.strip():
            continue
        hits.append(
            HybridMemoryHit(
                text=text.strip(),
                bucket=bucket.strip(),
                source="canonical",
                score=1.0,
            )
        )
    return tuple(hits)


def _provider_conflicts_with_canonical(
    provider_hit: HybridMemoryHit,
    canonical_hits: tuple[HybridMemoryHit, ...],
) -> bool:
    """Return whether a provider claim contradicts an active canonical claim.

    Providers may use ``external_memory`` for a cross-bucket claim. Such a
    claim is compared with every canonical bucket; typed provider buckets are
    compared only with the matching canonical bucket to avoid suppressing
    unrelated project, collaborator, or preference evidence.
    """

    return any(
        (
            provider_hit.bucket == "external_memory"
            or provider_hit.bucket == canonical_hit.bucket
        )
        and _contradictory_hits(
            canonical_hit,
            HybridMemoryHit(
                text=provider_hit.text,
                bucket=canonical_hit.bucket,
                source=provider_hit.source,
                score=provider_hit.score,
            ),
        )
        for canonical_hit in canonical_hits
    )


def _normalize_provider_claim_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _ambiguous_provider_buckets(
    buckets: dict[str, tuple[str, ...]],
) -> frozenset[str]:
    ambiguous: set[str] = set()
    for raw_bucket, values in buckets.items():
        bucket = _normalize_provider_claim_value(raw_bucket)
        if not bucket:
            continue
        if not isinstance(values, (tuple, list)):
            ambiguous.add(bucket)
            continue
        if any(
            not isinstance(value, str) or "\r" in value or "\n" in value
            for value in values
        ):
            ambiguous.add(bucket)
    return frozenset(ambiguous)


def _normalize_provider_context_records(provider_context: str) -> tuple[str, ...]:
    """Keep multiline provider claims attached to their record boundary."""

    records: list[str] = []
    current: list[str] = []
    for raw_line in provider_context.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- ["):
            if current:
                records.append(" ".join(current))
            current = [line]
            continue
        if current:
            current.append(line)
    if current:
        records.append(" ".join(current))
    return tuple(record for record in records if _provider_context_hit(record) is not None)


def _drop_ambiguous_provider_context_buckets(
    provider_context: str,
    ambiguous_buckets: frozenset[str],
) -> str:
    if not ambiguous_buckets:
        return provider_context
    safe_records: list[str] = []
    for record in _normalize_provider_context_records(provider_context):
        provider_hit = _provider_context_hit(record)
        if provider_hit is None:
            continue
        bucket = _normalize_provider_claim_value(provider_hit.bucket)
        if bucket in ambiguous_buckets:
            continue
        safe_records.append(record)
    return "\n".join(safe_records)


def _suppress_provider_context_conflicts(
    *,
    canonical_context: str,
    provider_context: str,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Keep provider context advisory when it does not contradict local canon."""

    canonical_hits = _canonical_context_hits(canonical_context)
    provider_records = _normalize_provider_context_records(provider_context)
    normalized_context = "\n".join(provider_records)
    if not canonical_hits or not normalized_context:
        return normalized_context, ()

    retained_lines: list[str] = []
    suppressed: list[tuple[str, str]] = []
    for line in provider_records:
        provider_hit = _provider_context_hit(line)
        if provider_hit is None:
            continue
        if not _provider_conflicts_with_canonical(provider_hit, canonical_hits):
            retained_lines.append(line)
            continue
        suppressed.append((provider_hit.bucket, provider_hit.text))
    return "\n".join(retained_lines), tuple(suppressed)


def _filter_provider_buckets(
    buckets: dict[str, tuple[str, ...]],
    suppressed: tuple[tuple[str, str], ...],
    *,
    excluded_buckets: frozenset[str] = frozenset(),
) -> dict[str, tuple[str, ...]]:
    suppressed_keys = {
        (normalized_bucket, normalized_text)
        for raw_bucket, raw_text in suppressed
        if (normalized_bucket := _normalize_provider_claim_value(raw_bucket))
        and (normalized_text := _normalize_provider_claim_value(raw_text))
    }
    normalized_buckets: dict[str, list[str]] = {}
    for raw_bucket, values in buckets.items():
        bucket = _normalize_provider_claim_value(raw_bucket)
        if not bucket or bucket in excluded_buckets or not isinstance(values, (tuple, list)):
            continue
        normalized_values = normalized_buckets.setdefault(bucket, [])
        for raw_text in values:
            text = _normalize_provider_claim_value(raw_text)
            if not text or (bucket, text) in suppressed_keys or text in normalized_values:
                continue
            normalized_values.append(text)
    return {
        bucket: tuple(values)
        for bucket, values in normalized_buckets.items()
        if values
    }


def _canonical_provider_conflict_diagnostic(
    suppressed_count: int,
) -> tuple[dict[str, object], ...]:
    if suppressed_count <= 0:
        return ()
    return (
        {
            "ranking_policy": "canonical_first_provider_conflict_suppression",
            "canonical_provider_conflict_suppressed_count": suppressed_count,
            "suppression_reasons": ["canonical_memory_conflict"],
            "authority_boundary": "provider_evidence_remains_advisory",
        },
    )


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


def _canonical_provider_suppression_count(
    diagnostics: tuple[dict[str, object], ...],
) -> int:
    total = 0
    for item in diagnostics:
        value = item.get("canonical_provider_conflict_suppressed_count")
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
    quality_gate_suppressed_count = _provider_suppression_count(provider_diagnostics, "quality_gate_suppressed_count")
    irrelevant_provider_hit_count = _provider_suppression_count(
        provider_diagnostics,
        "suppressed_irrelevant_hit_count",
    )
    contradiction_suppression_count = _hybrid_suppression_count(retrieval_diagnostics)
    canonical_provider_suppression_count = _canonical_provider_suppression_count(retrieval_diagnostics)
    suppression_count = (
        stale_provider_hit_count
        + quality_gate_suppressed_count
        + irrelevant_provider_hit_count
        + contradiction_suppression_count
        + canonical_provider_suppression_count
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
            "quality_gate_suppressed_count": quality_gate_suppressed_count,
            "stale_provider_hit_count": stale_provider_hit_count,
            "irrelevant_provider_hit_count": irrelevant_provider_hit_count,
            "canonical_memory_conflict_count": canonical_provider_suppression_count,
            "reasons": [
                reason
                for reason, count in (
                    ("lower_ranked_contradiction", contradiction_suppression_count),
                    ("provider_quality_gate", quality_gate_suppressed_count),
                    ("stale_provider_evidence", stale_provider_hit_count),
                    ("irrelevant_provider_evidence", irrelevant_provider_hit_count),
                    ("canonical_memory_conflict", canonical_provider_suppression_count),
                )
                if count
            ],
        },
        "provenance": {
            "guardian_canonical": bool(structured_context.strip() or semantic_context.strip() or episodic_context.strip()),
            "external_advisory": bool(provider_context.strip() or provider_capabilities_used),
            "policy": "canonical_first",
            "provider_declaration_complete": all(
                bool(item.get("provider_declaration_complete"))
                for item in provider_diagnostics
            ) if provider_diagnostics else False,
            "provider_evidence_ids": [
                str(evidence_id)
                for item in provider_diagnostics
                for evidence_id in item.get("accepted_evidence_ids", [])
                if isinstance(evidence_id, str) and evidence_id.strip()
            ],
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
    _skip_tombstone_reconciliation: bool = False,
) -> tuple[str, dict[str, tuple[str, ...]]]:
    if not _skip_tombstone_reconciliation:
        try:
            reconciliation = await memory_repository.reconcile_memory_tombstones()
        except SQLAlchemyError:
            raise
        if reconciliation.get("status") != "ready":
            return "", {}

    try:
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

        linked_project_entities = await memory_repository.find_entities_by_names(
            names=active_projects,
            entity_type=MemoryEntityType.project,
        )
        linked_memories = (
            await memory_repository.list_memories_for_entities(
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
            if linked_project_entities
            else []
        )
    except SQLAlchemyError:
        raise

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

    for memory in linked_memories:
        _append_structured_memory_line(
            bucketed=bucketed,
            lines=lines,
            text=_memory_context_text(memory.kind.value, memory),
            bucket_name=bucket_name_for_kind(memory.kind),
        )

    filtered_lines, filtered_buckets = _suppress_structured_context_contradictions(lines)
    return "\n".join(filtered_lines[:8]), {
        key: tuple(values) for key, values in filtered_buckets.items()
    }


def _blocked_memory_retrieval_result(
    *,
    reason: str,
    receipt: dict[str, object],
) -> MemoryRetrievalPlanResult:
    diagnostic = {
        "reason": reason,
        "status": "degraded_no_learning",
        "tombstone_reconciliation": receipt,
    }
    decision_receipt = {
        "receipt_type": "memory_decision",
        "changed_decision": False,
        "changed_intervention_timing": False,
        "lane": "canonical_memory_unavailable",
        "intervention_timing": "no_memory_context",
        "capability_choice": {
            "lane": "canonical_memory_unavailable",
            "canonical_guardian_memory": False,
            "provider_capabilities_used": [],
            "provider_failed_capabilities": [],
            "degraded": True,
        },
        "suppression": {"suppressed_count": 0, "reasons": [reason]},
        "provenance": {
            "guardian_canonical": False,
            "external_advisory": False,
            "policy": "canonical_first_fail_closed",
        },
        "confidence": {"degraded": True, "bucket_count": 0},
        "privacy_boundary": "operator_visible",
        "auditability": {"retrieval_diagnostics_visible": True},
    }
    return MemoryRetrievalPlanResult(
        semantic_context="",
        episodic_context="",
        memory_buckets={},
        degraded=True,
        lane="canonical_memory_unavailable",
        retrieval_diagnostics=(diagnostic,),
        decision_receipt=decision_receipt,
    )


def _hybrid_canonical_read_is_unavailable(result) -> bool:
    """Identify a hybrid result that must not be replaced by provider context."""

    return any(
        str(diagnostic.get("status") or "") == "degraded_no_learning"
        and str(diagnostic.get("reason") or "").startswith("canonical_")
        for diagnostic in result.diagnostics
    )


async def plan_memory_retrieval(
    *,
    query: str,
    active_projects: tuple[str, ...] = (),
) -> MemoryRetrievalPlanResult:
    try:
        tombstone_reconciliation = await memory_repository.reconcile_memory_tombstones()
    except SQLAlchemyError:
        return _blocked_memory_retrieval_result(
            reason="canonical_tombstone_reconciliation_unavailable",
            receipt={"status": "degraded_no_learning"},
        )
    if tombstone_reconciliation.get("status") != "ready":
        return _blocked_memory_retrieval_result(
            reason="canonical_tombstone_reconciliation_degraded",
            receipt=tombstone_reconciliation,
        )

    try:
        structured_context, structured_buckets = await build_structured_memory_context_bundle(
            active_projects=active_projects,
            _skip_tombstone_reconciliation=True,
        )
    except SQLAlchemyError:
        return _blocked_memory_retrieval_result(
            reason="canonical_memory_read_unavailable",
            receipt={"status": "degraded_no_learning"},
        )
    normalized_query = query.strip()
    provider_project_hints = _project_hint_candidates(
        query=normalized_query,
        active_projects=active_projects,
        structured_buckets=structured_buckets,
    )

    hybrid = None
    if normalized_query:
        try:
            hybrid = await retrieve_hybrid_memory(
                query=normalized_query,
                active_projects=active_projects,
                limit=8,
            )
        except SQLAlchemyError:
            return _blocked_memory_retrieval_result(
                reason="canonical_memory_read_unavailable",
                receipt={"status": "degraded_no_learning"},
            )
        if _hybrid_canonical_read_is_unavailable(hybrid):
            return _blocked_memory_retrieval_result(
                reason="canonical_memory_read_unavailable",
                receipt={"status": "degraded_no_learning"},
            )

    provider_retrieval = await retrieve_additive_memory_provider_context(
        query=normalized_query,
        active_projects=provider_project_hints,
        limit=3,
        include_user_model=bool(provider_project_hints),
    )
    ambiguous_provider_buckets = _ambiguous_provider_buckets(provider_retrieval.buckets)
    provider_context_input = _drop_ambiguous_provider_context_buckets(
        provider_retrieval.context,
        ambiguous_provider_buckets,
    )
    if not normalized_query:
        provider_context, suppressed_provider = _suppress_provider_context_conflicts(
            canonical_context=structured_context,
            provider_context=provider_context_input,
        )
        provider_buckets = _filter_provider_buckets(
            provider_retrieval.buckets,
            suppressed_provider,
            excluded_buckets=ambiguous_provider_buckets,
        )
        retrieval_diagnostics = _canonical_provider_conflict_diagnostic(
            len(suppressed_provider),
        )
        semantic_context = _merge_contexts(structured_context, provider_context)
        buckets = _merge_buckets(structured_buckets, provider_buckets)
        lane = (
            "structured_plus_provider_model"
            if provider_context and _provider_uses_user_model(provider_retrieval.diagnostics)
            else "structured_only"
        )
        return MemoryRetrievalPlanResult(
            semantic_context=semantic_context,
            episodic_context="",
            memory_buckets=buckets,
            degraded=provider_retrieval.degraded,
            lane=lane,
            provider_diagnostics=provider_retrieval.diagnostics,
            retrieval_diagnostics=retrieval_diagnostics,
            decision_receipt=_memory_decision_receipt(
                lane=lane,
                semantic_context=semantic_context,
                episodic_context="",
                structured_context=structured_context,
                provider_context=provider_context,
                degraded=provider_retrieval.degraded,
                provider_diagnostics=provider_retrieval.diagnostics,
                retrieval_diagnostics=retrieval_diagnostics,
                memory_buckets=buckets,
            ),
        )

    assert hybrid is not None
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
    provider_context, suppressed_provider = _suppress_provider_context_conflicts(
        canonical_context=_merge_contexts(structured_context, semantic_context),
        provider_context=provider_context_input,
    )
    provider_buckets = _filter_provider_buckets(
        provider_retrieval.buckets,
        suppressed_provider,
        excluded_buckets=ambiguous_provider_buckets,
    )
    retrieval_diagnostics = (
        *hybrid.diagnostics,
        *_canonical_provider_conflict_diagnostic(len(suppressed_provider)),
    )
    if provider_context:
        lane = (
            f"{lane}_plus_provider_model"
            if _provider_uses_user_model(provider_retrieval.diagnostics)
            else f"{lane}_plus_provider"
        )
    merged_semantic_context = _merge_contexts(structured_context, semantic_context, provider_context)
    merged_buckets = _merge_buckets(structured_buckets, semantic_buckets, provider_buckets)
    degraded = hybrid.degraded or provider_retrieval.degraded

    return MemoryRetrievalPlanResult(
        semantic_context=merged_semantic_context,
        episodic_context=episodic_context,
        memory_buckets=merged_buckets,
        degraded=degraded,
        lane=lane,
        provider_diagnostics=provider_retrieval.diagnostics,
        retrieval_diagnostics=retrieval_diagnostics,
        decision_receipt=_memory_decision_receipt(
            lane=lane,
            semantic_context=merged_semantic_context,
            episodic_context=episodic_context,
            structured_context=structured_context,
            provider_context=provider_context,
            degraded=degraded,
            provider_diagnostics=provider_retrieval.diagnostics,
            retrieval_diagnostics=retrieval_diagnostics,
            memory_buckets=merged_buckets,
        ),
    )
