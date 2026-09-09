from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import exists, func, or_, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
    MemoryTombstone,
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


_CANONICAL_MEMORY_DELETE_EXPORT_REASON = "operator_delete_export"
_CANONICAL_MEMORY_REDACTED_STATE = "canonical_memory_redacted"
_CANONICAL_MEMORY_DELETE_ACTIONS = {
    "propagate_delete_export",
    "operator_delete_export",
}
_CANONICAL_MEMORY_DELETE_CONTENT = "[delete/export propagated by operator]"


def _canonical_memory_without_tombstone_clause():
    """Return the SQL predicate for a memory with no durable delete authority."""

    return ~exists().where(MemoryTombstone.memory_id == Memory.id)


def _canonical_memory_is_active(memory: Memory) -> bool:
    try:
        return _coerce_enum(memory.status, MemoryStatus) is MemoryStatus.active
    except (TypeError, ValueError):
        return False


def _canonical_memory_deletion_marker(memory: Memory) -> str | None:
    """Return a durable canonical delete marker, if one is present.

    Canonical delete/export is terminal for the local memory record. Read both
    the current nested operator-control fields and older top-level markers so
    provider or learning ingress cannot revive a tombstone after a metadata
    shape migration. Malformed metadata on an archived or superseded row fails
    closed because it cannot disprove that a canonical deletion marker exists;
    an active malformed row remains an ordinary writable record.
    """

    raw_metadata = getattr(memory, "metadata_json", None)
    metadata_malformed = False
    try:
        parsed_metadata = json.loads(raw_metadata or "{}")
    except (TypeError, json.JSONDecodeError):
        parsed_metadata = {}
        metadata_malformed = bool(raw_metadata)
    metadata = parsed_metadata if isinstance(parsed_metadata, dict) else {}
    if not isinstance(parsed_metadata, dict):
        metadata_malformed = True

    operator_control_value = metadata.get("operator_control")
    if operator_control_value is None:
        operator_control: dict[str, Any] = {}
    elif isinstance(operator_control_value, dict):
        operator_control = operator_control_value
    else:
        operator_control = {}
        metadata_malformed = True

    def _marker_value(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip().lower()

    archived_reason = _marker_value(metadata.get("archived_reason"))
    if "archived_reason" in metadata and not isinstance(metadata.get("archived_reason"), str):
        metadata_malformed = True
    if archived_reason == _CANONICAL_MEMORY_DELETE_EXPORT_REASON:
        return f"archived_reason={archived_reason}"

    delete_export_state_value = operator_control.get("delete_export_state")
    top_level_delete_export_state = metadata.get("delete_export_state")
    if "delete_export_state" in operator_control and not isinstance(delete_export_state_value, str):
        metadata_malformed = True
    if "delete_export_state" in metadata and not isinstance(top_level_delete_export_state, str):
        metadata_malformed = True
    for delete_export_state in (
        _marker_value(delete_export_state_value),
        _marker_value(top_level_delete_export_state),
    ):
        if delete_export_state == _CANONICAL_MEMORY_REDACTED_STATE:
            return f"delete_export_state={delete_export_state}"

    last_action_value = operator_control.get("last_action")
    if "last_action" in operator_control and not isinstance(last_action_value, str):
        metadata_malformed = True
    top_level_last_action = metadata.get("last_action")
    if "last_action" in metadata and not isinstance(top_level_last_action, str):
        metadata_malformed = True
    for last_action in (
        _marker_value(last_action_value),
        _marker_value(top_level_last_action),
    ):
        if last_action in _CANONICAL_MEMORY_DELETE_ACTIONS:
            return f"last_action={last_action}"

    # The propagated replacement is itself a canonical redaction marker. Keep
    # this fallback for records written before the explicit state fields.
    content = str(getattr(memory, "content", "") or "").strip()
    summary = str(getattr(memory, "summary", "") or "").strip()
    if content == _CANONICAL_MEMORY_DELETE_CONTENT:
        return "content=canonical_memory_redacted"
    if summary == _CANONICAL_MEMORY_DELETE_CONTENT:
        return "summary=canonical_memory_redacted"

    if metadata_malformed:
        status = getattr(memory, "status", None)
        try:
            normalized_status = _coerce_enum(status, MemoryStatus).value
        except (TypeError, ValueError):
            normalized_status = str(status or "").strip().lower()
        if normalized_status in {MemoryStatus.archived.value, MemoryStatus.superseded.value}:
            return "metadata=malformed_suppressed_memory"

    return None


def _sqlite_json_object_path(key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"$.{key}"
    escaped_key = key.replace('"', '\\"')
    return f'$."{escaped_key}"'


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


@dataclass(frozen=True)
class MemoryTombstoneWriteResult:
    memory: Memory
    tombstone: MemoryTombstone
    created: bool


class MemoryRepository:
    def __init__(self) -> None:
        self._scoped_memory_locks: dict[str, asyncio.Lock] = {}
        self._canonical_memory_lock = asyncio.Lock()

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

    def _get_scoped_memory_lock(
        self,
        *,
        kind: MemoryKind,
        scope: dict[str, Any],
    ) -> asyncio.Lock:
        key = json.dumps(
            {"kind": kind.value, "scope": scope},
            sort_keys=True,
            default=str,
        )
        lock = self._scoped_memory_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._scoped_memory_locks[key] = lock
        return lock

    @staticmethod
    def _scoped_memory_key(
        *,
        kind: MemoryKind,
        scope: dict[str, Any],
    ) -> str:
        return json.dumps(
            {"kind": kind.value, "scope": scope},
            sort_keys=True,
            default=str,
        )

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
        additional_sources: list[dict[str, str | None]] | None = None,
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
        source_rows: list[dict[str, str | None]] = []
        if source_session_id or source_message_id:
            source_rows.append(
                {
                    "source_type": normalized_source_type,
                    "source_session_id": source_session_id,
                    "source_message_id": source_message_id,
                    "snippet": normalized_source_snippet,
                }
            )
        source_rows.extend(additional_sources or [])

        async with get_session() as db:
            metadata_map = dict(metadata or {})
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
                metadata_json=json.dumps(metadata_map, sort_keys=True),
                last_confirmed_at=last_confirmed_at,
                updated_at=_now(),
            )
            db.add(memory)
            await db.flush()

            message_source_count = 0
            session_source_created = False
            seen_source_keys: set[tuple[str, str | None, str | None]] = set()
            for source_row in source_rows:
                row_source_type = str(source_row.get("source_type") or "session").strip() or "session"
                row_session_id = source_row.get("source_session_id")
                row_message_id = source_row.get("source_message_id")
                if not row_session_id and not row_message_id:
                    continue
                source_key = (
                    "message",
                    row_message_id,
                    None,
                ) if row_message_id is not None else (
                    "session",
                    row_session_id,
                    row_source_type,
                )
                if source_key in seen_source_keys:
                    continue
                seen_source_keys.add(source_key)
                db.add(
                    MemorySource(
                        memory_id=memory.id,
                        source_type=row_source_type,
                        source_session_id=row_session_id,
                        source_message_id=row_message_id,
                        snippet=(
                            self._normalize_source_snippet(source_row.get("snippet"))
                            or (summary or normalized_content)[:240]
                        ),
                    )
                )
                if row_message_id is not None:
                    message_source_count += 1
                elif row_session_id is not None:
                    session_source_created = True
            if source_rows:
                await db.flush()

            db.expunge(memory)
            return MemoryWriteResult(
                memory_id=memory.id,
                subject_entity_id=subject_entity_id,
                project_entity_id=project_entity_id,
                message_source_count=message_source_count,
                session_source_created=session_source_created,
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
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()
            if memory is None:
                raise ValueError(f"Unknown memory id: {memory_id}")
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == memory_id
                    )
                )
            ).scalars().first()
            if tombstone is not None or _canonical_memory_deletion_marker(memory) is not None:
                raise ValueError(
                    "cannot add provenance to canonical memory after operator delete/export redaction"
                )
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
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == memory_id
                    )
                )
            ).scalars().first()
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()
            redact_sources = tombstone is not None or (
                memory is not None and _canonical_memory_deletion_marker(memory) is not None
            )
            result = await db.execute(
                select(MemorySource)
                .where(MemorySource.memory_id == memory_id)
                .order_by(col(MemorySource.created_at).asc())
            )
            sources = result.scalars().all()
            for source in sources:
                if redact_sources:
                    source.snippet = None
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
                .where(_canonical_memory_without_tombstone_clause())
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
                if _canonical_memory_deletion_marker(memory) is not None:
                    continue
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
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == memory_id
                    )
                )
            ).scalars().first()
            if tombstone is not None or _canonical_memory_deletion_marker(memory) is not None:
                raise ValueError(
                    "cannot merge canonical memory after operator delete/export redaction"
                )

            created_message_source_count = 0
            session_source_created = False
            existing_metadata = json.loads(memory.metadata_json or "{}")
            metadata_changed = False

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
                    for key in (
                        "decay_step",
                        "decay_age_days",
                        "archived_reason",
                        "archived_at",
                    ):
                        if key in existing_metadata:
                            existing_metadata.pop(key, None)
                            metadata_changed = True
            if metadata:
                existing_metadata.update(metadata)
                metadata_changed = True
            if metadata_changed:
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

    async def _cas_update_scoped_memory(
        self,
        db,
        memory: Memory,
        *,
        values_builder,
    ) -> Memory | None:
        """Update an existing scoped row only when its control snapshot holds."""

        def _normalize_timestamp(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        async def _select_memory_by_id(memory_id: str) -> Memory | None:
            return (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()

        for _attempt in range(2):
            if _canonical_memory_deletion_marker(memory) is not None:
                return None
            expected_metadata = memory.metadata_json
            expected_status = _coerce_enum(memory.status, MemoryStatus)
            expected_updated_at = _normalize_timestamp(memory.updated_at)
            expected_metadata_guard = (
                Memory.metadata_json.is_(None)
                if expected_metadata is None
                else Memory.metadata_json == expected_metadata
            )
            result = await db.execute(
                update(Memory)
                .where(
                    Memory.id == memory.id,
                    Memory.updated_at == expected_updated_at,
                    Memory.status == expected_status,
                    expected_metadata_guard,
                    _canonical_memory_without_tombstone_clause(),
                )
                .values(**values_builder(memory))
            )
            if result.rowcount == 1:
                return memory

            # A competing control may have committed after the read. Reset
            # this transaction before reconciling the current row so a winning
            # canonical delete can never be overwritten.
            await db.rollback()
            memory = await _select_memory_by_id(memory.id)
            if memory is None:
                return None
        return None

    async def sync_scoped_memory(
        self,
        *,
        kind: MemoryKind | str,
        scope: dict[str, Any],
        content: str | None,
        summary: str | None = None,
        category: MemoryCategory | str | None = None,
        confidence: float = 0.7,
        importance: float = 0.7,
        reinforcement: float = 1.0,
        metadata: dict[str, Any] | None = None,
        source_session_id: str | None = None,
        last_confirmed_at: datetime | None = None,
    ) -> MemoryWriteResult | None:
        """Create or refresh one memory for a stable kind/scope pair.

        ``None`` means either the existing empty-content archive path ran, a
        non-empty ingress was suppressed because the existing row carries a
        canonical delete/export tombstone, or a concurrent row change could
        not be reconciled safely. These outcomes are no-ops and are not
        successful write receipts.
        """

        normalized_kind = _coerce_enum(kind, MemoryKind)
        normalized_category = _coerce_enum(
            category if category is not None else MemoryCategory.preference,
            MemoryCategory,
        )
        normalized_scope = {
            str(key): value
            for key, value in (scope or {}).items()
            if str(key).strip() and value is not None
        }
        if not normalized_scope:
            raise ValueError("scope must contain at least one key")

        normalized_content = content.strip() if isinstance(content, str) else ""
        normalized_summary = (
            summary.strip() if isinstance(summary, str) and summary.strip() else None
        )
        merged_metadata = dict(normalized_scope)
        if isinstance(metadata, dict):
            merged_metadata.update(metadata)
        normalized_scope_key = self._scoped_memory_key(
            kind=normalized_kind,
            scope=normalized_scope,
        )

        def _normalize_timestamp(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        def _matches_scope(memory: Memory) -> bool:
            try:
                payload = json.loads(memory.metadata_json or "{}")
            except json.JSONDecodeError:
                return False
            if not isinstance(payload, dict):
                return False
            return all(payload.get(key) == value for key, value in normalized_scope.items())

        lock = self._get_scoped_memory_lock(
            kind=normalized_kind,
            scope=normalized_scope,
        )
        async with lock:
            async with get_session() as db:
                memory = (
                    await db.execute(
                        select(Memory)
                        .where(Memory.kind == normalized_kind)
                        .where(Memory.scope_key == normalized_scope_key)
                        .order_by(col(Memory.updated_at).desc(), col(Memory.created_at).desc())
                    )
                ).scalars().first()
                if memory is None:
                    candidates = (
                        await db.execute(
                            select(Memory)
                            .where(Memory.kind == normalized_kind)
                            .order_by(col(Memory.updated_at).desc(), col(Memory.created_at).desc())
                        )
                    ).scalars().all()
                    memory = next((item for item in candidates if _matches_scope(item)), None)

                if memory is not None:
                    if not normalized_content and _canonical_memory_deletion_marker(memory) is not None:
                        return None
                    tombstone = (
                        await db.execute(select(MemoryTombstone).where(MemoryTombstone.memory_id == memory.id))
                    ).scalars().first()
                    if tombstone is not None:
                        return None
                    if _canonical_memory_deletion_marker(memory) is not None:
                        return None

                if not normalized_content:
                    if memory is None:
                        return None
                    await self._cas_update_scoped_memory(
                        db,
                        memory,
                        values_builder=lambda _current: {
                            "status": MemoryStatus.archived,
                            "updated_at": _now(),
                            "scope_key": normalized_scope_key,
                            "metadata_json": json.dumps(merged_metadata, sort_keys=True),
                        },
                    )
                    return None

                if memory is None:
                    try:
                        unresolved_tombstone = (
                            await db.execute(
                                select(MemoryTombstone.id)
                                .outerjoin(Memory, Memory.id == MemoryTombstone.memory_id)
                                .where(Memory.id.is_(None))
                                .limit(1)
                            )
                        ).scalars().first()
                    except SQLAlchemyError:
                        # A missing or unavailable deletion authority must not
                        # permit a new learning/provider row to be created.
                        return None
                    if unresolved_tombstone is not None:
                        return None
                    memory = Memory(
                        content=normalized_content,
                        category=normalized_category,
                        kind=normalized_kind,
                        source_session_id=source_session_id,
                        summary=normalized_summary,
                        confidence=confidence,
                        importance=importance,
                        reinforcement=reinforcement,
                        scope_key=normalized_scope_key,
                        metadata_json=json.dumps(merged_metadata, sort_keys=True),
                        status=MemoryStatus.active,
                        last_confirmed_at=_normalize_timestamp(last_confirmed_at),
                    )
                    db.add(memory)
                    try:
                        await db.flush()
                    except IntegrityError:
                        await db.rollback()
                        memory = (
                            await db.execute(
                                select(Memory)
                                .where(Memory.kind == normalized_kind)
                                .where(Memory.scope_key == normalized_scope_key)
                            )
                        ).scalars().first()
                        if memory is None:
                            raise
                        memory = await self._cas_update_scoped_memory(
                            db,
                            memory,
                            values_builder=lambda current: {
                                "content": normalized_content,
                                "category": normalized_category,
                                "summary": normalized_summary,
                                "confidence": confidence,
                                "importance": importance,
                                "reinforcement": max(current.reinforcement, reinforcement),
                                "status": MemoryStatus.active,
                                "scope_key": normalized_scope_key,
                                "metadata_json": json.dumps(merged_metadata, sort_keys=True),
                                "updated_at": _now(),
                                **(
                                    {"source_session_id": source_session_id}
                                    if source_session_id
                                    else {}
                                ),
                                **(
                                    {
                                        "last_confirmed_at": _normalize_timestamp(last_confirmed_at)
                                    }
                                    if last_confirmed_at is not None
                                    else {}
                                ),
                            },
                        )
                        if memory is None:
                            return None
                    db.expunge(memory)
                    return MemoryWriteResult(memory_id=memory.id)

                memory = await self._cas_update_scoped_memory(
                    db,
                    memory,
                    values_builder=lambda current: {
                        "content": normalized_content,
                        "category": normalized_category,
                        "summary": normalized_summary,
                        "confidence": confidence,
                        "importance": importance,
                        "reinforcement": max(current.reinforcement, reinforcement),
                        "status": MemoryStatus.active,
                        "scope_key": normalized_scope_key,
                        "metadata_json": json.dumps(merged_metadata, sort_keys=True),
                        "updated_at": _now(),
                        **(
                            {"source_session_id": source_session_id}
                            if source_session_id
                            else {}
                        ),
                        **(
                            {"last_confirmed_at": _normalize_timestamp(last_confirmed_at)}
                            if last_confirmed_at is not None
                            else {}
                        ),
                    },
                )
                if memory is None:
                    return None
                db.expunge(memory)
                return MemoryWriteResult(
                    memory_id=memory.id,
                    subject_entity_id=memory.subject_entity_id,
                    project_entity_id=memory.project_entity_id,
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
            if normalized_status is MemoryStatus.active:
                stmt = stmt.where(_canonical_memory_without_tombstone_clause())
            if kind:
                stmt = stmt.where(Memory.kind == _coerce_enum(kind, MemoryKind))
            result = await db.execute(stmt)
            memories = [
                memory
                for memory in result.scalars().all()
                if not (
                    _canonical_memory_is_active(memory)
                    and _canonical_memory_deletion_marker(memory) is not None
                )
            ]
            for memory in memories:
                db.expunge(memory)
            return list(memories)

    async def get_memory(self, memory_id: str) -> Memory | None:
        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id:
            return None
        async with get_session() as db:
            stmt = select(Memory).where(Memory.id == normalized_memory_id).where(
                or_(
                    Memory.status != MemoryStatus.active,
                    _canonical_memory_without_tombstone_clause(),
                )
            )
            memory = (
                await db.execute(stmt)
            ).scalars().first()
            if memory is not None and _canonical_memory_deletion_marker(memory) is not None:
                if _canonical_memory_is_active(memory):
                    return None
            if memory is not None:
                db.expunge(memory)
            return memory

    async def get_memory_tombstone(self, memory_id: str) -> MemoryTombstone | None:
        """Read the local canonical deletion authority for one memory."""

        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id:
            return None
        async with get_session() as db:
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == normalized_memory_id
                    )
                )
            ).scalars().first()
            if tombstone is not None:
                db.expunge(tombstone)
            return tombstone

    async def mark_memory_tombstoned(
        self,
        memory_id: str,
        *,
        actor: str,
        reason: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
        deleted_at: datetime | None = None,
    ) -> MemoryTombstoneWriteResult:
        """Serialize repeated local delete requests for one canonical row."""

        normalized_memory_id = str(memory_id or "").strip()
        lock = self._get_scoped_memory_lock(
            kind=MemoryKind.fact,
            scope={"purpose": "canonical_tombstone", "memory_id": normalized_memory_id},
        )
        async with lock:
            async with self._canonical_memory_lock:
                return await self._mark_memory_tombstoned(
                    memory_id,
                    actor=actor,
                    reason=reason,
                    metadata_updates=metadata_updates,
                    deleted_at=deleted_at,
                )

    async def _mark_memory_tombstoned(
        self,
        memory_id: str,
        *,
        actor: str,
        reason: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
        deleted_at: datetime | None = None,
    ) -> MemoryTombstoneWriteResult:
        """Atomically redact a canonical row and record its delete authority.

        Repeating the operation is idempotent: the first actor/reason/time in
        the ledger remain authoritative while the canonical redaction is
        re-applied.  The ledger has no content field so a restore cannot use
        it to recover deleted material.
        """

        normalized_memory_id = str(memory_id or "").strip()
        normalized_actor = str(actor or "").strip()
        normalized_reason = str(reason or _CANONICAL_MEMORY_DELETE_EXPORT_REASON).strip()
        if not normalized_memory_id:
            raise ValueError("memory_id must be non-empty")
        if not normalized_actor:
            raise ValueError("actor must be non-empty")
        if not normalized_reason:
            normalized_reason = _CANONICAL_MEMORY_DELETE_EXPORT_REASON
        if len(normalized_actor) > 255 or len(normalized_reason) > 255:
            raise ValueError("actor and reason must be at most 255 characters")
        deletion_time = deleted_at or _now()
        if deletion_time.tzinfo is None:
            deletion_time = deletion_time.replace(tzinfo=timezone.utc)
        else:
            deletion_time = deletion_time.astimezone(timezone.utc)

        async with get_session() as db:
            memory = (
                await db.execute(select(Memory).where(Memory.id == normalized_memory_id))
            ).scalars().first()
            if memory is None:
                raise ValueError(f"Unknown memory id: {normalized_memory_id}")

            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == normalized_memory_id
                    )
                )
            ).scalars().first()
            created = tombstone is None
            if tombstone is None:
                tombstone = MemoryTombstone(
                    memory_id=normalized_memory_id,
                    actor=normalized_actor,
                    reason=normalized_reason,
                    created_at=deletion_time,
                )
                db.add(tombstone)
                try:
                    # The process-local lock covers the normal path.  Keep a
                    # database-level retry as well for two workers with
                    # separate repository instances racing on the unique key.
                    await db.flush()
                except IntegrityError:
                    await db.rollback()
                    tombstone = (
                        await db.execute(
                            select(MemoryTombstone).where(
                                MemoryTombstone.memory_id == normalized_memory_id
                            )
                        )
                    ).scalars().first()
                    memory = (
                        await db.execute(
                            select(Memory).where(Memory.id == normalized_memory_id)
                        )
                    ).scalars().first()
                    if tombstone is None or memory is None:
                        raise
                    created = False
            updates = dict(metadata_updates or {})
            try:
                metadata = json.loads(memory.metadata_json or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.update(updates)
            metadata["canonical_tombstone_id"] = tombstone.id
            metadata["archived_reason"] = _CANONICAL_MEMORY_DELETE_EXPORT_REASON
            memory.status = MemoryStatus.archived
            memory.content = _CANONICAL_MEMORY_DELETE_CONTENT
            memory.summary = _CANONICAL_MEMORY_DELETE_CONTENT
            memory.confidence = 0.0
            memory.importance = 0.0
            memory.reinforcement = 0.0
            memory.metadata_json = json.dumps(metadata, sort_keys=True)
            memory.updated_at = deletion_time
            db.add(memory)
            await db.execute(
                update(MemorySource)
                .where(MemorySource.memory_id == normalized_memory_id)
                .where(MemorySource.snippet.is_not(None))
                .values(snippet=None)
            )
            await db.flush()
            db.expunge(memory)
            db.expunge(tombstone)
            return MemoryTombstoneWriteResult(
                memory=memory,
                tombstone=tombstone,
                created=created,
            )

    async def reconcile_memory_tombstones(
        self,
        *,
        _acquire_lock: bool = True,
    ) -> dict[str, int | str]:
        """Re-apply durable canonical deletion authority after a stale restore.

        The result is a deterministic operator receipt.  ``degraded`` means a
        ledger row refers to a missing memory row and therefore requires
        operator restore repair; no deleted content is returned.
        """

        if _acquire_lock:
            async with self._canonical_memory_lock:
                return await self.reconcile_memory_tombstones(_acquire_lock=False)

        checked_count = 0
        reapplied_count = 0
        missing_memory_count = 0
        async with get_session() as db:
            tombstones = (
                await db.execute(
                    select(MemoryTombstone).order_by(
                        col(MemoryTombstone.created_at).asc(),
                        col(MemoryTombstone.id).asc(),
                    )
                )
            ).scalars().all()
            for tombstone in tombstones:
                checked_count += 1
                memory = (
                    await db.execute(
                        select(Memory).where(Memory.id == tombstone.memory_id)
                    )
                ).scalars().first()
                if memory is None:
                    missing_memory_count += 1
                    continue
                try:
                    metadata = json.loads(memory.metadata_json or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                operator_control = metadata.get("operator_control")
                if not isinstance(operator_control, dict):
                    operator_control = {}
                operator_control.update(
                    {
                        "last_action": "propagate_delete_export",
                        "last_actor": tombstone.actor,
                        "last_reason": tombstone.reason,
                        "last_action_at": tombstone.created_at.isoformat(),
                        "delete_export_state": _CANONICAL_MEMORY_REDACTED_STATE,
                        "provider_propagation_state": "runtime_receipt_only_no_full_provider_parity_claim",
                    }
                )
                metadata["operator_control"] = operator_control
                metadata["archived_reason"] = _CANONICAL_MEMORY_DELETE_EXPORT_REASON
                metadata["canonical_tombstone_id"] = tombstone.id
                metadata["provenance"] = {
                    "kind": "operator_propagate_delete_export",
                    "actor": tombstone.actor,
                    "source": "canonical_tombstone_ledger",
                    "recorded_at": tombstone.created_at.isoformat(),
                }
                source_redaction = await db.execute(
                    update(MemorySource)
                    .where(MemorySource.memory_id == tombstone.memory_id)
                    .where(MemorySource.snippet.is_not(None))
                    .values(snippet=None)
                )
                needs_reapply = any(
                    (
                        memory.status != MemoryStatus.archived,
                        memory.content != _CANONICAL_MEMORY_DELETE_CONTENT,
                        memory.summary != _CANONICAL_MEMORY_DELETE_CONTENT,
                        float(memory.confidence or 0.0) != 0.0,
                        float(memory.importance or 0.0) != 0.0,
                        float(memory.reinforcement or 0.0) != 0.0,
                        memory.metadata_json != json.dumps(metadata, sort_keys=True),
                    )
                )
                if needs_reapply or source_redaction.rowcount > 0:
                    memory.status = MemoryStatus.archived
                    memory.content = _CANONICAL_MEMORY_DELETE_CONTENT
                    memory.summary = _CANONICAL_MEMORY_DELETE_CONTENT
                    memory.confidence = 0.0
                    memory.importance = 0.0
                    memory.reinforcement = 0.0
                    memory.metadata_json = json.dumps(metadata, sort_keys=True)
                    memory.updated_at = _now()
                    db.add(memory)
                    reapplied_count += 1
            await db.flush()
        return {
            "schema_version": "guardian.memory_tombstone.v1",
            "status": "degraded" if missing_memory_count else "ready",
            "checked_count": checked_count,
            "reapplied_count": reapplied_count,
            "missing_memory_count": missing_memory_count,
        }

    async def list_memories_for_reindex(self, *, limit: int = 10_000) -> list[Memory]:
        """Return active canonical rows safe for a local deterministic reindex.

        Reconciliation runs first, so a stale active row from restore is
        redacted before it can be admitted to a derived index.
        """

        bounded_limit = min(max(int(limit), 1), 10_000)
        async with self._canonical_memory_lock:
            reconciliation = await self.reconcile_memory_tombstones(_acquire_lock=False)
            if reconciliation.get("status") != "ready":
                return []
            async with get_session() as db:
                result = await db.execute(
                    select(Memory)
                    .outerjoin(MemoryTombstone, MemoryTombstone.memory_id == Memory.id)
                    .where(Memory.status == MemoryStatus.active)
                    .where(MemoryTombstone.id.is_(None))
                    .order_by(col(Memory.updated_at).asc(), col(Memory.id).asc())
                    .limit(bounded_limit)
                )
                memories = result.scalars().all()
                memories = [
                    memory
                    for memory in memories
                    if _canonical_memory_deletion_marker(memory) is None
                ]
                for memory in memories:
                    db.expunge(memory)
                return list(memories)

    async def update_memory_control_metadata(
        self,
        memory_id: str,
        *,
        status: MemoryStatus | str | None = None,
        content: str | None = None,
        summary: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        reinforcement: float | None = None,
        metadata_updates: dict[str, Any] | None = None,
        last_confirmed_at: datetime | None = None,
    ) -> Memory:
        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id:
            raise ValueError("memory_id must be non-empty")

        def _normalize_timestamp(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        async with get_session() as db:
            memory = (
                await db.execute(select(Memory).where(Memory.id == normalized_memory_id))
            ).scalars().first()
            if memory is None:
                raise ValueError(f"Unknown memory id: {normalized_memory_id}")

            requested_status = (
                _coerce_enum(status, MemoryStatus)
                if status is not None
                else None
            )
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == normalized_memory_id
                    )
                )
            ).scalars().first()
            if tombstone is not None or _canonical_memory_deletion_marker(memory) is not None:
                if requested_status is MemoryStatus.active:
                    raise ValueError(
                        "cannot reactivate canonical memory after operator delete/export redaction"
                    )
                raise ValueError(
                    "cannot mutate canonical memory after operator delete/export redaction"
                )

            if status is not None:
                memory.status = requested_status
            if isinstance(content, str):
                normalized_content = content.strip()
                if not normalized_content:
                    raise ValueError("content must be non-empty")
                memory.content = normalized_content
            if isinstance(summary, str):
                memory.summary = summary.strip() or None
            if confidence is not None:
                memory.confidence = max(0.0, min(1.0, float(confidence)))
            if importance is not None:
                memory.importance = max(0.0, min(1.0, float(importance)))
            if reinforcement is not None:
                memory.reinforcement = max(0.0, float(reinforcement))
            if last_confirmed_at is not None:
                memory.last_confirmed_at = _normalize_timestamp(last_confirmed_at)

            metadata: dict[str, Any]
            try:
                parsed_metadata = json.loads(memory.metadata_json or "{}")
            except json.JSONDecodeError:
                parsed_metadata = {}
            metadata = parsed_metadata if isinstance(parsed_metadata, dict) else {}
            if metadata_updates:
                metadata.update(metadata_updates)
                memory.metadata_json = json.dumps(metadata, sort_keys=True)

            memory.updated_at = _now()
            db.add(memory)
            await db.flush()
            db.expunge(memory)
            return memory

    async def rollback_memory_if_unchanged(
        self,
        memory_id: str,
        *,
        expected_updated_at: datetime,
        expected_metadata_json: str | None,
        expected_status: MemoryStatus | str,
        confidence: float,
        importance: float,
        reinforcement: float,
        last_confirmed_at: datetime,
        metadata_updates: dict[str, Any],
    ) -> Memory:
        """Restore one memory only when its control snapshot is unchanged.

        The conditional update closes the read-then-write race with
        delete/export: if another control commits after the caller reads the
        memory, the expected timestamp or metadata no longer matches and this
        method performs no activation.
        """

        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id:
            raise ValueError("memory_id must be non-empty")
        normalized_status = _coerce_enum(expected_status, MemoryStatus)
        expected_metadata = expected_metadata_json
        try:
            parsed_metadata = json.loads(expected_metadata or "{}")
        except json.JSONDecodeError:
            parsed_metadata = {}
        metadata = parsed_metadata if isinstance(parsed_metadata, dict) else {}
        metadata.update(metadata_updates or {})

        def _normalize_timestamp(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        expected_metadata_guard = (
            Memory.metadata_json.is_(None)
            if expected_metadata is None
            else Memory.metadata_json == expected_metadata
        )
        async with get_session() as db:
            existing = (
                await db.execute(select(Memory).where(Memory.id == normalized_memory_id))
            ).scalars().first()
            if existing is not None and _canonical_memory_deletion_marker(existing) is not None:
                raise ValueError(
                    "cannot rollback canonical memory after operator delete/export redaction"
                )
            result = await db.execute(
                update(Memory)
                .where(
                    Memory.id == normalized_memory_id,
                    Memory.updated_at == _normalize_timestamp(expected_updated_at),
                    Memory.status == normalized_status,
                    expected_metadata_guard,
                    _canonical_memory_without_tombstone_clause(),
                )
                .values(
                    status=MemoryStatus.active,
                    confidence=max(0.0, min(1.0, float(confidence))),
                    importance=max(0.0, min(1.0, float(importance))),
                    reinforcement=max(0.0, float(reinforcement)),
                    last_confirmed_at=_normalize_timestamp(last_confirmed_at),
                    metadata_json=json.dumps(metadata, sort_keys=True),
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                raise ValueError(
                    "memory changed before rollback; canonical deletion or another "
                    "memory control won"
                )
            memory = (
                await db.execute(select(Memory).where(Memory.id == normalized_memory_id))
            ).scalars().first()
            if memory is None:  # pragma: no cover - guarded update matched a row
                raise ValueError(f"Unknown memory id: {normalized_memory_id}")
            db.expunge(memory)
            return memory

    async def update_memory_control(
        self,
        memory_id: str,
        *,
        status: MemoryStatus | str | None = None,
        content: str | None = None,
        summary: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        reinforcement: float | None = None,
        metadata: dict[str, Any] | None = None,
        last_confirmed_at: datetime | None = None,
    ) -> Memory:
        return await self.update_memory_control_metadata(
            memory_id,
            status=status,
            content=content,
            summary=summary,
            confidence=confidence,
            importance=importance,
            reinforcement=reinforcement,
            metadata_updates=metadata,
            last_confirmed_at=last_confirmed_at,
        )

    async def list_memories_for_scope(
        self,
        *,
        kind: MemoryKind | str,
        scope: dict[str, Any],
        limit: int = 20,
        status: MemoryStatus | str = MemoryStatus.active,
        exact_scope_keys: tuple[str, ...] = (),
    ) -> list[Memory]:
        normalized_kind = _coerce_enum(kind, MemoryKind)
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_scope = {
            str(key): value
            for key, value in (scope or {}).items()
            if str(key).strip() and value is not None
        }
        if not normalized_scope:
            raise ValueError("scope must contain at least one key")
        normalized_scope_key = self._scoped_memory_key(
            kind=normalized_kind,
            scope=normalized_scope,
        )
        normalized_exact_scope_keys = tuple(
            dict.fromkeys(str(key).strip() for key in exact_scope_keys if str(key).strip())
        )

        def _matches_exact_scope_keys(metadata: dict[str, Any]) -> bool:
            if not normalized_exact_scope_keys:
                return True
            return all(
                (
                    metadata.get(key) == normalized_scope[key]
                    if key in normalized_scope
                    else metadata.get(key) is None
                )
                for key in normalized_exact_scope_keys
            )

        matches: list[Memory] = []
        async with get_session() as db:
            exact_stmt = (
                select(Memory)
                .where(Memory.kind == normalized_kind)
                .where(Memory.status == normalized_status)
                .where(Memory.scope_key == normalized_scope_key)
                .order_by(
                    col(Memory.importance).desc(),
                    col(Memory.last_confirmed_at).desc(),
                    col(Memory.created_at).desc(),
                )
                .limit(limit)
            )
            if normalized_status is MemoryStatus.active:
                exact_stmt = exact_stmt.where(_canonical_memory_without_tombstone_clause())
            exact_result = await db.execute(exact_stmt)
            for memory in exact_result.scalars().all():
                if (
                    _canonical_memory_is_active(memory)
                    and _canonical_memory_deletion_marker(memory) is not None
                ):
                    continue
                try:
                    metadata = json.loads(memory.metadata_json or "{}")
                except json.JSONDecodeError:
                    continue
                if not isinstance(metadata, dict):
                    continue
                if not _matches_exact_scope_keys(metadata):
                    continue
                db.expunge(memory)
                matches.append(memory)
            if matches:
                return matches

            legacy_stmt = (
                select(Memory)
                .where(Memory.kind == normalized_kind)
                .where(Memory.status == normalized_status)
                .where(func.json_valid(Memory.metadata_json) == 1)
                .where(func.json_type(Memory.metadata_json, "$") == "object")
                .order_by(
                    col(Memory.importance).desc(),
                    col(Memory.last_confirmed_at).desc(),
                    col(Memory.created_at).desc(),
                )
                .limit(limit)
            )
            if normalized_status is MemoryStatus.active:
                legacy_stmt = legacy_stmt.where(_canonical_memory_without_tombstone_clause())
            for key, value in normalized_scope.items():
                legacy_stmt = legacy_stmt.where(
                    func.json_extract(Memory.metadata_json, _sqlite_json_object_path(key)) == value
                )
            result = await db.execute(legacy_stmt)
            for memory in result.scalars().all():
                if (
                    _canonical_memory_is_active(memory)
                    and _canonical_memory_deletion_marker(memory) is not None
                ):
                    continue
                try:
                    metadata = json.loads(memory.metadata_json or "{}")
                except json.JSONDecodeError:
                    continue
                if not isinstance(metadata, dict):
                    continue
                if not all(metadata.get(key) == value for key, value in normalized_scope.items()):
                    continue
                if not _matches_exact_scope_keys(metadata):
                    continue
                db.expunge(memory)
                matches.append(memory)
        return matches

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
            if normalized_status is MemoryStatus.active:
                stmt = stmt.where(_canonical_memory_without_tombstone_clause())
            filters = []
            if normalized_subject_ids:
                filters.append(col(Memory.subject_entity_id).in_(normalized_subject_ids))
            if normalized_project_ids:
                filters.append(col(Memory.project_entity_id).in_(normalized_project_ids))
            stmt = stmt.where(or_(*filters))
            if normalized_kinds:
                stmt = stmt.where(col(Memory.kind).in_(normalized_kinds))
            result = await db.execute(stmt)
            memories = [
                memory
                for memory in result.scalars().all()
                if not (
                    _canonical_memory_is_active(memory)
                    and _canonical_memory_deletion_marker(memory) is not None
                )
            ]
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
            if normalized_status is MemoryStatus.active:
                stmt = stmt.where(_canonical_memory_without_tombstone_clause())
            result = await db.execute(stmt)
            grouped: dict[str, list[Memory]] = {kind.value: [] for kind in normalized_kinds}
            for memory in result.scalars().all():
                if (
                    _canonical_memory_is_active(memory)
                    and _canonical_memory_deletion_marker(memory) is not None
                ):
                    continue
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
            existing = (
                await db.execute(
                    select(MemoryEdge)
                    .where(MemoryEdge.from_memory_id == from_memory_id)
                    .where(MemoryEdge.to_memory_id == to_memory_id)
                    .where(MemoryEdge.edge_type == normalized_edge_type)
                )
            ).scalars().first()
            if existing is not None:
                db.expunge(existing)
                return existing
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

    async def list_edges(
        self,
        *,
        from_memory_id: str | None = None,
        to_memory_id: str | None = None,
        edge_type: MemoryEdgeType | str | None = None,
    ) -> list[MemoryEdge]:
        async with get_session() as db:
            stmt = select(MemoryEdge).order_by(col(MemoryEdge.created_at).asc())
            if from_memory_id is not None:
                stmt = stmt.where(MemoryEdge.from_memory_id == from_memory_id)
            if to_memory_id is not None:
                stmt = stmt.where(MemoryEdge.to_memory_id == to_memory_id)
            if edge_type is not None:
                stmt = stmt.where(MemoryEdge.edge_type == _coerce_enum(edge_type, MemoryEdgeType))
            result = await db.execute(stmt)
            edges = result.scalars().all()
            for edge in edges:
                db.expunge(edge)
            return list(edges)

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
