from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from sqlmodel import col, select

from src.db.engine import get_session
from src.db.models import (
    Memory,
    MemoryCategory,
    MemoryEdge,
    MemoryEdgeType,
    MemoryEpisode,
    MemoryEpisodeType,
    MemoryEntity,
    MemoryEntityType,
    MemoryKind,
    MemorySnapshot,
    MemorySnapshotKind,
    MemorySource,
    MemoryStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_entity_key(name: str, entity_type: MemoryEntityType | str) -> str:
    normalized_type = _coerce_enum(entity_type, MemoryEntityType).value
    return f"{normalized_type}:{' '.join(name.strip().lower().split())}"


def _normalize_entity_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _name_contains_requested_tokens(candidate_name: str, requested_name: str) -> bool:
    candidate_tokens = _normalize_entity_name(candidate_name).split()
    requested_tokens = _normalize_entity_name(requested_name).split()
    if not candidate_tokens or not requested_tokens or len(requested_tokens) > len(candidate_tokens):
        return False
    for index in range(len(candidate_tokens) - len(requested_tokens) + 1):
        if candidate_tokens[index : index + len(requested_tokens)] == requested_tokens:
            return True
    return False


def _coerce_enum(
    value: Any,
    enum_cls: type[
        MemoryCategory
        | MemoryKind
        | MemoryStatus
        | MemoryEpisodeType
        | MemoryEntityType
        | MemorySnapshotKind
        | MemoryEdgeType
    ],
):
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {enum_cls.__name__}: {value!r}") from exc


@dataclass(frozen=True)
class MemoryWriteResult:
    memory_id: str
    subject_entity_id: str | None = None
    project_entity_id: str | None = None
    message_source_count: int = 0
    session_source_created: bool = False


@dataclass(frozen=True)
class MemorySourceWriteResult:
    source_id: str
    created: bool


class MemoryRepository:
    @staticmethod
    def _normalize_memory_text(value: str | None) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.strip().lower().split())

    @staticmethod
    def _normalize_source_snippet(value: str | None) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return " ".join(value.strip().split())[:240]

    @staticmethod
    def _normalize_episode_kwargs(
        *,
        episode_type: MemoryEpisodeType | str,
        summary: str,
        content: str,
        session_id: str | None,
        source_message_id: str | None,
        source_tool_name: str | None,
        source_role: str | None,
        subject_entity_id: str | None,
        project_entity_id: str | None,
        salience: float,
        confidence: float,
        metadata: dict[str, Any] | None,
        observed_at: datetime | None,
    ) -> dict[str, Any]:
        normalized_summary = summary.strip()
        normalized_content = content.strip()
        if not normalized_summary:
            raise ValueError("summary must be non-empty")
        if not normalized_content:
            raise ValueError("content must be non-empty")
        normalized_episode_type = _coerce_enum(episode_type, MemoryEpisodeType)
        return {
            "session_id": session_id,
            "episode_type": normalized_episode_type,
            "summary": normalized_summary,
            "content": normalized_content,
            "source_message_id": source_message_id,
            "source_tool_name": source_tool_name,
            "source_role": source_role,
            "subject_entity_id": subject_entity_id,
            "project_entity_id": project_entity_id,
            "salience": salience,
            "confidence": confidence,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
            "observed_at": observed_at or _now(),
        }

    async def get_or_create_entity(
        self,
        *,
        canonical_name: str,
        entity_type: MemoryEntityType | str = MemoryEntityType.person,
        aliases: list[str] | None = None,
    ) -> MemoryEntity:
        normalized_name = canonical_name.strip()
        if not normalized_name:
            raise ValueError("canonical_name must be non-empty")
        normalized_entity_type = _coerce_enum(entity_type, MemoryEntityType)
        canonical_key = _normalize_entity_key(normalized_name, normalized_entity_type)
        requested_aliases = sorted({alias.strip() for alias in aliases or [] if alias.strip()})

        async with get_session() as db:
            result = await db.execute(
                select(MemoryEntity)
                .where(MemoryEntity.canonical_key == canonical_key)
            )
            entity = result.scalars().first()
            if entity is None and requested_aliases:
                alias_candidates = (
                    await db.execute(
                        select(MemoryEntity).where(MemoryEntity.entity_type == normalized_entity_type)
                    )
                ).scalars().all()
                requested_alias_keys = {_normalize_entity_key(alias, normalized_entity_type) for alias in requested_aliases}
                for candidate in alias_candidates:
                    candidate_keys = {_normalize_entity_key(candidate.canonical_name, normalized_entity_type)}
                    candidate_keys.update(
                        _normalize_entity_key(alias, normalized_entity_type)
                        for alias in json.loads(candidate.aliases_json or "[]")
                    )
                    if requested_alias_keys & candidate_keys or canonical_key in candidate_keys:
                        entity = candidate
                        break
            if entity is not None:
                updated_aliases = sorted(
                    {
                        alias.strip()
                        for alias in requested_aliases + json.loads(entity.aliases_json or "[]")
                        if alias.strip()
                    }
                )
                if updated_aliases != json.loads(entity.aliases_json or "[]"):
                    entity.aliases_json = json.dumps(updated_aliases)
                    entity.updated_at = _now()
                    db.add(entity)
                await db.flush()
                db.expunge(entity)
                return entity

            entity = MemoryEntity(
                canonical_key=canonical_key,
                canonical_name=normalized_name,
                entity_type=normalized_entity_type,
                aliases_json=json.dumps(requested_aliases),
            )
            db.add(entity)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                result = await db.execute(
                    select(MemoryEntity).where(MemoryEntity.canonical_key == canonical_key)
                )
                entity = result.scalars().one()
            db.expunge(entity)
            return entity

    async def create_memory(
        self,
        *,
        content: str,
        category: MemoryCategory | str = MemoryCategory.fact,
        kind: MemoryKind | str = MemoryKind.fact,
        source_session_id: str | None = None,
        source_message_id: str | None = None,
        source_type: str = "session",
        source_snippet: str | None = None,
        summary: str | None = None,
        confidence: float = 0.5,
        importance: float = 0.5,
        reinforcement: float = 1.0,
        subject_entity_id: str | None = None,
        project_entity_id: str | None = None,
        embedding_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: MemoryStatus | str = MemoryStatus.active,
        last_confirmed_at: datetime | None = None,
    ) -> MemoryWriteResult:
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("content must be non-empty")
        normalized_category = _coerce_enum(category, MemoryCategory)
        normalized_kind = _coerce_enum(kind, MemoryKind)
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_embedding_id = (
            embedding_id.strip()
            if isinstance(embedding_id, str) and embedding_id.strip()
            else None
        )
        normalized_source_type = source_type.strip() or "session"
        normalized_source_snippet = (
            " ".join(source_snippet.strip().split())[:240]
            if isinstance(source_snippet, str) and source_snippet.strip()
            else None
        )

        async with get_session() as db:
            memory = Memory(
                content=normalized_content,
                category=normalized_category,
                kind=normalized_kind,
                summary=summary,
                confidence=confidence,
                importance=importance,
                reinforcement=reinforcement,
                status=normalized_status,
                subject_entity_id=subject_entity_id,
                project_entity_id=project_entity_id,
                source_session_id=source_session_id,
                embedding_id=normalized_embedding_id,
                metadata_json=json.dumps(metadata or {}, sort_keys=True),
                last_confirmed_at=last_confirmed_at,
                updated_at=_now(),
            )
            db.add(memory)
            await db.flush()

            if source_session_id or source_message_id:
                db.add(
                    MemorySource(
                        memory_id=memory.id,
                        source_type=normalized_source_type,
                        source_session_id=source_session_id,
                        source_message_id=source_message_id,
                        snippet=(
                            normalized_source_snippet
                            or (summary or normalized_content)[:240]
                        ),
                    )
                )
                await db.flush()

            db.expunge(memory)
            return MemoryWriteResult(
                memory_id=memory.id,
                subject_entity_id=subject_entity_id,
                project_entity_id=project_entity_id,
            )

    async def add_memory_source(
        self,
        *,
        memory_id: str,
        source_type: str = "session",
        source_session_id: str | None = None,
        source_message_id: str | None = None,
        snippet: str | None = None,
    ) -> MemorySourceWriteResult:
        normalized_source_type = source_type.strip() or "session"
        normalized_snippet = (
            " ".join(snippet.strip().split())[:240]
            if isinstance(snippet, str) and snippet.strip()
            else None
        )
        async with get_session() as db:
            stmt = select(MemorySource).where(MemorySource.memory_id == memory_id)
            if source_message_id:
                stmt = stmt.where(MemorySource.source_message_id == source_message_id)
            else:
                stmt = stmt.where(MemorySource.source_message_id.is_(None))
                if source_session_id is not None:
                    stmt = stmt.where(MemorySource.source_session_id == source_session_id)
                stmt = stmt.where(MemorySource.source_type == normalized_source_type)
            existing = (await db.execute(stmt)).scalars().first()
            if existing is not None:
                db.expunge(existing)
                return MemorySourceWriteResult(source_id=existing.id, created=False)

            source = MemorySource(
                memory_id=memory_id,
                source_type=normalized_source_type,
                source_session_id=source_session_id,
                source_message_id=source_message_id,
                snippet=normalized_snippet,
            )
            db.add(source)
            await db.flush()
            db.expunge(source)
            return MemorySourceWriteResult(source_id=source.id, created=True)

    async def list_sources(
        self,
        *,
        memory_id: str,
    ) -> list[MemorySource]:
        async with get_session() as db:
            result = await db.execute(
                select(MemorySource)
                .where(MemorySource.memory_id == memory_id)
                .order_by(col(MemorySource.created_at).asc())
            )
            sources = result.scalars().all()
            for source in sources:
                db.expunge(source)
            return list(sources)

    async def find_merge_candidate(
        self,
        *,
        kind: MemoryKind | str,
        summary: str | None,
        content: str,
        subject_entity_id: str | None = None,
        project_entity_id: str | None = None,
        status: MemoryStatus | str = MemoryStatus.active,
    ) -> Memory | None:
        normalized_kind = _coerce_enum(kind, MemoryKind)
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_summary = self._normalize_memory_text(summary)
        normalized_content = self._normalize_memory_text(content)
        if not normalized_summary and not normalized_content:
            return None

        async with get_session() as db:
            stmt = (
                select(Memory)
                .where(Memory.kind == normalized_kind)
                .where(Memory.status == normalized_status)
                .order_by(
                    col(Memory.reinforcement).desc(),
                    col(Memory.importance).desc(),
                    col(Memory.updated_at).desc(),
                    col(Memory.created_at).desc(),
                )
            )
            if subject_entity_id is not None:
                stmt = stmt.where(
                    or_(
                        Memory.subject_entity_id == subject_entity_id,
                        Memory.subject_entity_id.is_(None),
                    )
                )
            else:
                stmt = stmt.where(Memory.subject_entity_id.is_(None))
            if project_entity_id is not None:
                stmt = stmt.where(
                    or_(
                        Memory.project_entity_id == project_entity_id,
                        Memory.project_entity_id.is_(None),
                    )
                )
            else:
                stmt = stmt.where(Memory.project_entity_id.is_(None))

            candidates = (await db.execute(stmt)).scalars().all()
            for memory in candidates:
                candidate_summary = self._normalize_memory_text(memory.summary)
                candidate_content = self._normalize_memory_text(memory.content)
                if normalized_summary and candidate_summary == normalized_summary:
                    db.expunge(memory)
                    return memory
                if normalized_content and candidate_content == normalized_content:
                    db.expunge(memory)
                    return memory
            return None

    async def merge_memory(
        self,
        memory_id: str,
        *,
        summary: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        reinforcement_delta: float = 0.25,
        subject_entity_id: str | None = None,
        project_entity_id: str | None = None,
        embedding_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        last_confirmed_at: datetime | None = None,
        message_sources: list[dict[str, str | None]] | None = None,
        session_source: dict[str, str | None] | None = None,
    ) -> MemoryWriteResult:
        def _normalize_timestamp(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        async with get_session() as db:
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()
            if memory is None:
                raise ValueError(f"Unknown memory id: {memory_id}")

            created_message_source_count = 0
            session_source_created = False

            if summary and not memory.summary:
                memory.summary = summary
            if confidence is not None:
                memory.confidence = max(memory.confidence, confidence)
            if importance is not None:
                memory.importance = max(memory.importance, importance)
            memory.reinforcement = max(0.0, memory.reinforcement + reinforcement_delta)
            if memory.subject_entity_id is None and subject_entity_id is not None:
                memory.subject_entity_id = subject_entity_id
            if memory.project_entity_id is None and project_entity_id is not None:
                memory.project_entity_id = project_entity_id
            if memory.embedding_id is None and embedding_id:
                memory.embedding_id = embedding_id
            if last_confirmed_at is not None:
                normalized_existing = _normalize_timestamp(memory.last_confirmed_at)
                normalized_candidate = _normalize_timestamp(last_confirmed_at)
                if (
                    normalized_candidate is not None
                    and (
                        normalized_existing is None
                        or normalized_candidate > normalized_existing
                    )
                ):
                    memory.last_confirmed_at = normalized_candidate
            if metadata:
                existing_metadata = json.loads(memory.metadata_json or "{}")
                existing_metadata.update(metadata)
                memory.metadata_json = json.dumps(existing_metadata, sort_keys=True)
            memory.updated_at = _now()
            db.add(memory)

            for source in message_sources or []:
                source_message_id = source.get("source_message_id")
                if not source_message_id:
                    continue
                existing = (
                    await db.execute(
                        select(MemorySource)
                        .where(MemorySource.memory_id == memory_id)
                        .where(MemorySource.source_message_id == source_message_id)
                    )
                ).scalars().first()
                if existing is not None:
                    continue
                db.add(
                    MemorySource(
                        memory_id=memory_id,
                        source_type="message",
                        source_session_id=source.get("source_session_id"),
                        source_message_id=source_message_id,
                        snippet=self._normalize_source_snippet(source.get("snippet")),
                    )
                )
                created_message_source_count += 1

            if session_source:
                source_session_id = session_source.get("source_session_id")
                if source_session_id:
                    existing = (
                        await db.execute(
                            select(MemorySource)
                            .where(MemorySource.memory_id == memory_id)
                            .where(MemorySource.source_type == "session")
                            .where(MemorySource.source_session_id == source_session_id)
                            .where(MemorySource.source_message_id.is_(None))
                        )
                    ).scalars().first()
                    if existing is None:
                        db.add(
                            MemorySource(
                                memory_id=memory_id,
                                source_type="session",
                                source_session_id=source_session_id,
                                snippet=self._normalize_source_snippet(session_source.get("snippet")),
                            )
                        )
                        session_source_created = True

            await db.flush()
            db.expunge(memory)
            return MemoryWriteResult(
                memory_id=memory.id,
                subject_entity_id=memory.subject_entity_id,
                project_entity_id=memory.project_entity_id,
                message_source_count=created_message_source_count,
                session_source_created=session_source_created,
            )

    async def create_episode(
        self,
        *,
        episode_type: MemoryEpisodeType | str = MemoryEpisodeType.conversation,
        summary: str,
        content: str,
        session_id: str | None = None,
        source_message_id: str | None = None,
        source_tool_name: str | None = None,
        source_role: str | None = None,
        subject_entity_id: str | None = None,
        project_entity_id: str | None = None,
        salience: float = 0.5,
        confidence: float = 0.5,
        metadata: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> MemoryEpisode:
        episodes = await self.create_episode_batch(
            items=[
                {
                    "episode_type": episode_type,
                    "summary": summary,
                    "content": content,
                    "session_id": session_id,
                    "source_message_id": source_message_id,
                    "source_tool_name": source_tool_name,
                    "source_role": source_role,
                    "subject_entity_id": subject_entity_id,
                    "project_entity_id": project_entity_id,
                    "salience": salience,
                    "confidence": confidence,
                    "metadata": metadata,
                    "observed_at": observed_at,
                }
            ]
        )
        return episodes[0]

    async def create_episode_batch(
        self,
        *,
        items: list[dict[str, Any]],
    ) -> list[MemoryEpisode]:
        if not items:
            return []
        normalized_items = [
            self._normalize_episode_kwargs(
                episode_type=item.get("episode_type", MemoryEpisodeType.conversation),
                summary=str(item.get("summary", "")),
                content=str(item.get("content", "")),
                session_id=item.get("session_id"),
                source_message_id=item.get("source_message_id"),
                source_tool_name=item.get("source_tool_name"),
                source_role=item.get("source_role"),
                subject_entity_id=item.get("subject_entity_id"),
                project_entity_id=item.get("project_entity_id"),
                salience=float(item.get("salience", 0.5)),
                confidence=float(item.get("confidence", 0.5)),
                metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else None,
                observed_at=item.get("observed_at"),
            )
            for item in items
        ]

        async with get_session() as db:
            episodes: list[MemoryEpisode] = []
            for episode_kwargs in normalized_items:
                episode = MemoryEpisode(**episode_kwargs)
                db.add(episode)
                episodes.append(episode)
            await db.flush()
            for episode in episodes:
                db.expunge(episode)
            return episodes

    async def find_entities_by_names(
        self,
        *,
        names: tuple[str, ...],
        entity_type: MemoryEntityType | str,
    ) -> dict[str, MemoryEntity]:
        normalized_entity_type = _coerce_enum(entity_type, MemoryEntityType)
        requested = {
            name: _normalize_entity_key(name, normalized_entity_type)
            for name in dict.fromkeys(name.strip() for name in names if name.strip())
        }
        if not requested:
            return {}

        async with get_session() as db:
            result = await db.execute(
                select(MemoryEntity).where(MemoryEntity.entity_type == normalized_entity_type)
            )
            entities = result.scalars().all()
            resolved: dict[str, MemoryEntity] = {}
            for entity in entities:
                candidate_keys = {
                    _normalize_entity_key(entity.canonical_name, normalized_entity_type)
                }
                candidate_keys.update(
                    _normalize_entity_key(alias, normalized_entity_type)
                    for alias in json.loads(entity.aliases_json or "[]")
                    if alias
                )
                for requested_name, requested_key in requested.items():
                    if requested_key not in candidate_keys or requested_name in resolved:
                        continue
                    resolved[requested_name] = entity
            unresolved_names = [name for name in requested if name not in resolved]
            if unresolved_names and normalized_entity_type == MemoryEntityType.project:
                for requested_name in unresolved_names:
                    matches: list[MemoryEntity] = []
                    for entity in entities:
                        candidate_names = [entity.canonical_name]
                        candidate_names.extend(
                            alias for alias in json.loads(entity.aliases_json or "[]") if alias
                        )
                        if any(
                            _name_contains_requested_tokens(candidate_name, requested_name)
                            for candidate_name in candidate_names
                        ):
                            matches.append(entity)
                    unique_matches = {entity.id: entity for entity in matches}
                    if len(unique_matches) == 1:
                        resolved[requested_name] = next(iter(unique_matches.values()))
            for entity in {id(entity): entity for entity in resolved.values()}.values():
                db.expunge(entity)
            return resolved

    async def list_memories(
        self,
        *,
        kind: MemoryKind | str | None = None,
        limit: int = 20,
        status: MemoryStatus | str = MemoryStatus.active,
    ) -> list[Memory]:
        normalized_status = _coerce_enum(status, MemoryStatus)
        async with get_session() as db:
            stmt = (
                select(Memory)
                .where(Memory.status == normalized_status)
                .order_by(col(Memory.importance).desc(), col(Memory.created_at).desc())
                .limit(limit)
            )
            if kind:
                stmt = stmt.where(Memory.kind == _coerce_enum(kind, MemoryKind))
            result = await db.execute(stmt)
            memories = result.scalars().all()
            for memory in memories:
                db.expunge(memory)
            return list(memories)

    async def list_memories_for_entities(
        self,
        *,
        subject_entity_ids: tuple[str, ...] = (),
        project_entity_ids: tuple[str, ...] = (),
        kinds: tuple[MemoryKind | str, ...] = (),
        limit: int = 20,
        status: MemoryStatus | str = MemoryStatus.active,
    ) -> list[Memory]:
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_subject_ids = tuple(
            dict.fromkeys(entity_id.strip() for entity_id in subject_entity_ids if entity_id.strip())
        )
        normalized_project_ids = tuple(
            dict.fromkeys(entity_id.strip() for entity_id in project_entity_ids if entity_id.strip())
        )
        normalized_kinds = tuple(
            dict.fromkeys(_coerce_enum(kind, MemoryKind) for kind in kinds)
        )
        if not normalized_subject_ids and not normalized_project_ids:
            return []
        async with get_session() as db:
            stmt = (
                select(Memory)
                .where(Memory.status == normalized_status)
                .order_by(
                    col(Memory.importance).desc(),
                    col(Memory.last_confirmed_at).desc(),
                    col(Memory.created_at).desc(),
                )
                .limit(limit)
            )
            filters = []
            if normalized_subject_ids:
                filters.append(col(Memory.subject_entity_id).in_(normalized_subject_ids))
            if normalized_project_ids:
                filters.append(col(Memory.project_entity_id).in_(normalized_project_ids))
            stmt = stmt.where(or_(*filters))
            if normalized_kinds:
                stmt = stmt.where(col(Memory.kind).in_(normalized_kinds))
            result = await db.execute(stmt)
            memories = result.scalars().all()
            for memory in memories:
                db.expunge(memory)
            return list(memories)

    async def list_memories_by_kinds(
        self,
        *,
        kinds: tuple[MemoryKind | str, ...],
        limit_per_kind: int = 3,
        status: MemoryStatus | str = MemoryStatus.active,
    ) -> dict[str, list[Memory]]:
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_kinds = tuple(dict.fromkeys(_coerce_enum(kind, MemoryKind) for kind in kinds))
        if not normalized_kinds:
            return {}

        async with get_session() as db:
            stmt = (
                select(Memory)
                .where(Memory.status == normalized_status)
                .where(col(Memory.kind).in_(normalized_kinds))
                .order_by(
                    col(Memory.importance).desc(),
                    col(Memory.last_confirmed_at).desc(),
                    col(Memory.created_at).desc(),
                )
            )
            result = await db.execute(stmt)
            grouped: dict[str, list[Memory]] = {kind.value: [] for kind in normalized_kinds}
            for memory in result.scalars().all():
                bucket = grouped.setdefault(memory.kind.value, [])
                if len(bucket) >= limit_per_kind:
                    continue
                db.expunge(memory)
                bucket.append(memory)
            return {key: value for key, value in grouped.items() if value}

    async def list_episodes(
        self,
        *,
        session_id: str | None = None,
        episode_types: tuple[MemoryEpisodeType | str, ...] = (),
        subject_entity_ids: tuple[str, ...] = (),
        project_entity_ids: tuple[str, ...] = (),
        limit: int = 20,
    ) -> list[MemoryEpisode]:
        normalized_episode_types = tuple(
            dict.fromkeys(_coerce_enum(item, MemoryEpisodeType) for item in episode_types)
        )
        normalized_subject_ids = tuple(
            dict.fromkeys(entity_id.strip() for entity_id in subject_entity_ids if entity_id.strip())
        )
        normalized_project_ids = tuple(
            dict.fromkeys(entity_id.strip() for entity_id in project_entity_ids if entity_id.strip())
        )
        async with get_session() as db:
            stmt = (
                select(MemoryEpisode)
                .order_by(
                    col(MemoryEpisode.observed_at).desc(),
                    col(MemoryEpisode.salience).desc(),
                    col(MemoryEpisode.created_at).desc(),
                )
                .limit(limit)
            )
            if session_id is not None:
                stmt = stmt.where(MemoryEpisode.session_id == session_id)
            if normalized_episode_types:
                stmt = stmt.where(col(MemoryEpisode.episode_type).in_(normalized_episode_types))
            entity_filters = []
            if normalized_subject_ids:
                entity_filters.append(col(MemoryEpisode.subject_entity_id).in_(normalized_subject_ids))
            if normalized_project_ids:
                entity_filters.append(col(MemoryEpisode.project_entity_id).in_(normalized_project_ids))
            if entity_filters:
                stmt = stmt.where(or_(*entity_filters))
            result = await db.execute(stmt)
            episodes = result.scalars().all()
            for episode in episodes:
                db.expunge(episode)
            return list(episodes)

    async def create_edge(
        self,
        *,
        from_memory_id: str,
        to_memory_id: str,
        edge_type: MemoryEdgeType | str = MemoryEdgeType.related,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEdge:
        if not from_memory_id or not to_memory_id:
            raise ValueError("from_memory_id and to_memory_id must be non-empty")
        normalized_edge_type = _coerce_enum(edge_type, MemoryEdgeType)
        async with get_session() as db:
            edge = MemoryEdge(
                from_memory_id=from_memory_id,
                to_memory_id=to_memory_id,
                edge_type=normalized_edge_type,
                weight=weight,
                metadata_json=json.dumps(metadata or {}, sort_keys=True),
            )
            db.add(edge)
            await db.flush()
            db.expunge(edge)
            return edge

    async def save_snapshot(
        self,
        *,
        kind: MemorySnapshotKind | str = MemorySnapshotKind.bounded_guardian_context,
        content: str,
        source_hash: str | None = None,
    ) -> MemorySnapshot:
        normalized_kind = _coerce_enum(kind, MemorySnapshotKind)
        async with get_session() as db:
            result = await db.execute(
                select(MemorySnapshot).where(MemorySnapshot.kind == normalized_kind)
            )
            snapshot = result.scalars().first()
            if snapshot is None:
                snapshot = MemorySnapshot(kind=normalized_kind, content=content, source_hash=source_hash)
            else:
                snapshot.content = content
                snapshot.source_hash = source_hash
                snapshot.updated_at = _now()
            db.add(snapshot)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                snapshot = (
                    await db.execute(
                        select(MemorySnapshot).where(MemorySnapshot.kind == normalized_kind)
                    )
                ).scalars().one()
                snapshot.content = content
                snapshot.source_hash = source_hash
                snapshot.updated_at = _now()
                db.add(snapshot)
                await db.flush()
            db.expunge(snapshot)
            return snapshot

    async def get_snapshot(self, kind: MemorySnapshotKind | str = MemorySnapshotKind.bounded_guardian_context) -> MemorySnapshot | None:
        normalized_kind = _coerce_enum(kind, MemorySnapshotKind)
        async with get_session() as db:
            result = await db.execute(
                select(MemorySnapshot).where(MemorySnapshot.kind == normalized_kind)
            )
            snapshot = result.scalars().first()
            if snapshot is not None:
                db.expunge(snapshot)
            return snapshot


memory_repository = MemoryRepository()
