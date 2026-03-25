from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select, col

from src.db.engine import get_session
from src.db.models import (
    Memory,
    MemoryCategory,
    MemoryEdge,
    MemoryEdgeType,
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


def _coerce_enum(
    value: Any,
    enum_cls: type[
        MemoryCategory
        | MemoryKind
        | MemoryStatus
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


class MemoryRepository:
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
                        source_type="session",
                        source_session_id=source_session_id,
                        source_message_id=source_message_id,
                        snippet=(summary or normalized_content)[:240],
                    )
                )
                await db.flush()

            db.expunge(memory)
            return MemoryWriteResult(
                memory_id=memory.id,
                subject_entity_id=subject_entity_id,
                project_entity_id=project_entity_id,
            )

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
        subject_entity_id: str | None = None,
        project_entity_id: str | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        async with get_session() as db:
            stmt = (
                select(Memory)
                .where(Memory.status == MemoryStatus.active)
                .order_by(col(Memory.importance).desc(), col(Memory.created_at).desc())
                .limit(limit)
            )
            if subject_entity_id:
                stmt = stmt.where(Memory.subject_entity_id == subject_entity_id)
            if project_entity_id:
                stmt = stmt.where(Memory.project_entity_id == project_entity_id)
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
