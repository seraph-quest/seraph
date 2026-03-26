from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Callable

from src.memory.linking import resolve_memory_links
from src.memory.pipeline.capture import CapturedSessionMessage
from src.memory.pipeline.strengthen import (
    latest_confirmation,
    strengthened_confidence,
    strengthened_importance,
)
from src.memory.repository import memory_repository
from src.memory.types import ConsolidatedMemoryItem

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "about",
    "after",
    "before",
    "from",
    "have",
    "into",
    "that",
    "their",
    "them",
    "they",
    "this",
    "with",
}


@dataclass(frozen=True)
class PersistedMemoryStats:
    stored_count: int = 0
    created_count: int = 0
    merged_count: int = 0
    vector_stored: int = 0
    source_link_count: int = 0
    partial_write_count: int = 0
    write_failure_count: int = 0


@dataclass(frozen=True)
class EmbeddingWriteResult:
    embedding_id: str | None = None
    stored: bool = False
    failed: bool = False


@dataclass(frozen=True)
class MergeProvenancePlan:
    message_sources: tuple[CapturedSessionMessage, ...] = ()
    session_source_needed: bool = False

    @property
    def has_new_provenance(self) -> bool:
        return bool(self.message_sources) or self.session_source_needed


def _summary_for_memory(text: str, *, max_chars: int = 140) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def _tokenize(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) >= 4 and token not in _STOPWORDS
    }


def _select_source_messages(
    *,
    memory_text: str,
    source_messages: tuple[CapturedSessionMessage, ...],
    limit: int = 3,
) -> tuple[CapturedSessionMessage, ...]:
    if not source_messages:
        return ()
    memory_tokens = _tokenize(memory_text)
    scored: list[tuple[float, int, CapturedSessionMessage]] = []
    for index, message in enumerate(source_messages):
        overlap = len(memory_tokens & _tokenize(message.content))
        if overlap <= 0:
            continue
        role_bonus = 0.2 if message.role == "user" else 0.0
        scored.append((float(overlap) + role_bonus, index, message))
    if not scored:
        return ()
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return tuple(item[2] for item in scored[:limit])


async def _link_sources(
    *,
    memory_id: str,
    session_id: str,
    sources: tuple[CapturedSessionMessage, ...],
) -> int:
    created = 0
    for source in sources:
        try:
            result = await memory_repository.add_memory_source(
                memory_id=memory_id,
                source_type="message",
                source_session_id=session_id,
                source_message_id=source.id or None,
                snippet=source.content,
            )
            if result.created:
                created += 1
        except Exception:
            logger.exception("Failed to link memory source for memory %s", memory_id[:8])
            raise
    return created


async def _write_embedding(
    *,
    item: ConsolidatedMemoryItem,
    session_id: str,
    vector_writer: Callable[..., str],
) -> EmbeddingWriteResult:
    try:
        embedding_id = await asyncio.wait_for(
            asyncio.to_thread(
                vector_writer,
                text=item.text,
                category=item.category.value,
                source_session_id=session_id,
            ),
            timeout=10,
        )
        normalized_embedding_id = (
            embedding_id.strip()
            if isinstance(embedding_id, str) and embedding_id.strip()
            else None
        )
        return EmbeddingWriteResult(
            embedding_id=normalized_embedding_id,
            stored=normalized_embedding_id is not None,
            failed=normalized_embedding_id is None,
        )
    except asyncio.TimeoutError:
        logger.warning("add_memory timed out for session %s", session_id[:8])
    except Exception:
        logger.exception("add_memory failed for session %s", session_id[:8])
    return EmbeddingWriteResult(failed=True)


async def _plan_merge_provenance(
    *,
    memory_id: str,
    session_id: str,
    selected_sources: tuple[CapturedSessionMessage, ...],
) -> MergeProvenancePlan:
    existing_sources = await memory_repository.list_sources(memory_id=memory_id)
    existing_message_ids = {
        source.source_message_id
        for source in existing_sources
        if source.source_message_id
    }
    existing_session_source = any(
        source.source_type == "session"
        and source.source_session_id == session_id
        and source.source_message_id is None
        for source in existing_sources
    )
    new_message_sources = tuple(
        source
        for source in selected_sources
        if source.id and source.id not in existing_message_ids
    )
    session_source_needed = (
        not selected_sources and not existing_session_source
    )
    return MergeProvenancePlan(
        message_sources=new_message_sources,
        session_source_needed=session_source_needed,
    )


async def persist_extracted_memories(
    *,
    extracted_memories: tuple[ConsolidatedMemoryItem, ...],
    session_id: str,
    source_messages: tuple[CapturedSessionMessage, ...],
    vector_writer: Callable[..., str],
    link_resolver=resolve_memory_links,
    writer_name: str = "session_consolidation",
) -> PersistedMemoryStats:
    stored = 0
    created = 0
    merged = 0
    vector_stored = 0
    source_link_count = 0
    partial_write_count = 0
    write_failure_count = 0

    for item in extracted_memories:
        metadata = dict(item.metadata or {})
        metadata.update(
            {
                "writer": writer_name,
                "source": "llm_extract",
            }
        )
        if item.subject_name:
            metadata["subject_name"] = item.subject_name
        if item.project_name:
            metadata["project_name"] = item.project_name

        try:
            link_resolution = await link_resolver(item)
        except Exception:
            logger.exception("memory linking failed for session %s", session_id[:8])
            partial_write_count += 1
            continue

        candidate = await memory_repository.find_merge_candidate(
            kind=item.kind,
            summary=item.summary,
            content=item.text,
            subject_entity_id=link_resolution.subject_entity_id,
            project_entity_id=link_resolution.project_entity_id,
        )
        selected_sources = _select_source_messages(
            memory_text=item.summary or item.text,
            source_messages=source_messages,
        )

        if candidate is not None:
            embedding_result = EmbeddingWriteResult()
            if candidate.embedding_id is None:
                embedding_result = await _write_embedding(
                    item=item,
                    session_id=session_id,
                    vector_writer=vector_writer,
                )
                if embedding_result.stored:
                    vector_stored += 1
            provenance_plan = await _plan_merge_provenance(
                memory_id=candidate.id,
                session_id=session_id,
                selected_sources=selected_sources,
            )
            try:
                merge_result = await memory_repository.merge_memory(
                    candidate.id,
                    summary=item.summary or _summary_for_memory(item.text),
                    confidence=strengthened_confidence(
                        existing=candidate.confidence,
                        candidate=item.confidence,
                    ),
                    importance=strengthened_importance(
                        existing=candidate.importance,
                        candidate=item.importance,
                    ),
                    reinforcement_delta=0.25 if provenance_plan.has_new_provenance else 0.0,
                    subject_entity_id=link_resolution.subject_entity_id,
                    project_entity_id=link_resolution.project_entity_id,
                    embedding_id=embedding_result.embedding_id,
                    metadata=metadata,
                    last_confirmed_at=latest_confirmation(
                        existing=candidate.last_confirmed_at,
                        candidate=item.last_confirmed_at,
                    ),
                    message_sources=[
                        {
                            "source_session_id": session_id,
                            "source_message_id": source.id or None,
                            "snippet": source.content,
                        }
                        for source in provenance_plan.message_sources
                    ],
                    session_source=(
                        {
                            "source_session_id": session_id,
                            "snippet": item.summary or item.text,
                        }
                        if provenance_plan.session_source_needed
                        else None
                    ),
                )
                source_link_count += merge_result.message_source_count
                if merge_result.session_source_created:
                    source_link_count += 1
                if candidate.embedding_id is None and embedding_result.failed:
                    partial_write_count += 1
                stored += 1
                merged += 1
            except Exception:
                logger.exception("memory merge failed for session %s", session_id[:8])
                partial_write_count += 1
                write_failure_count += 1
            continue

        embedding_result = await _write_embedding(
            item=item,
            session_id=session_id,
            vector_writer=vector_writer,
        )
        structured_succeeded = False
        primary_source = selected_sources[0] if selected_sources else None
        if embedding_result.stored:
            vector_stored += 1

        try:
            created_memory = await memory_repository.create_memory(
                content=item.text,
                category=item.category,
                kind=item.kind,
                source_session_id=session_id,
                source_message_id=primary_source.id if primary_source is not None else None,
                source_type="message" if primary_source is not None else "session",
                source_snippet=primary_source.content if primary_source is not None else None,
                embedding_id=embedding_result.embedding_id,
                summary=item.summary or _summary_for_memory(item.text),
                confidence=item.confidence,
                importance=item.importance,
                subject_entity_id=link_resolution.subject_entity_id,
                project_entity_id=link_resolution.project_entity_id,
                metadata=metadata,
                last_confirmed_at=item.last_confirmed_at,
            )
            structured_succeeded = True
            source_link_count += created_memory.message_source_count
            if created_memory.session_source_created:
                source_link_count += 1
            for extra_source in selected_sources[1:]:
                source_result = await memory_repository.add_memory_source(
                    memory_id=created_memory.memory_id,
                    source_type="message",
                    source_session_id=session_id,
                    source_message_id=extra_source.id or None,
                    snippet=extra_source.content,
                )
                if source_result.created:
                    source_link_count += 1
            stored += 1
            created += 1
        except Exception:
            logger.exception("structured memory write failed for session %s", session_id[:8])

        if embedding_result.stored != structured_succeeded:
            partial_write_count += 1
        elif embedding_result.failed and not structured_succeeded:
            write_failure_count += 1

    return PersistedMemoryStats(
        stored_count=stored,
        created_count=created,
        merged_count=merged,
        vector_stored=vector_stored,
        source_link_count=source_link_count,
        partial_write_count=partial_write_count,
        write_failure_count=write_failure_count,
    )
