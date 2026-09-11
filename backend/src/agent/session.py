import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from time import perf_counter

from sqlalchemy import func, or_, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import select, col

from config.settings import settings
from src.approval.runtime import get_current_trust_principal, reset_runtime_context, set_runtime_context
from src.audit.runtime import log_background_task_event
from src.conversation.identity import ConversationIdentityError, validate_attachment_refs
from src.model_fabric.caller_context import build_canonical_inference_context
from src.db.engine import get_session
from src.db.models import (
    ApprovalRequest,
    AuditEvent,
    GuardianIntervention,
    MemoryEpisode,
    MemoryEpisodeType,
    Message,
    NativeNotificationOutbox,
    TelegramTransportOutbox,
    QueuedInsight,
    ScheduledJob,
    Session,
    SessionTodo,
)
from src.db.session_refs import ensure_sessions_exist
from src.memory.episodes import build_message_episode
from src.memory.flush import flush_session_memory
from src.tools.process_tools import SessionProcessCleanupError, process_runtime_manager

logger = logging.getLogger(__name__)


class SessionOwnerMismatchError(Exception):
    """Raised when a caller attempts to claim an already-owned session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"session {session_id!r} is owned by another principal")


class SessionNotFoundError(Exception):
    """Raised when an ingress references a session that does not exist."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"session {session_id!r} was not found")


class MessageIngressConflictError(Exception):
    """Raised when a canonical message identity is reused with new metadata."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        super().__init__(f"message {message_id!r} conflicts with an existing ingress")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _matching_snippet(text: str, query: str, *, snippet_chars: int) -> str:
    flattened = text.replace("\n", " ").strip()
    if len(flattened) <= snippet_chars:
        return flattened

    normalized_text = flattened.lower()
    normalized_query = query.strip().lower()
    match_index = normalized_text.find(normalized_query)
    if match_index < 0:
        return flattened[:snippet_chars] + "..."

    match_end = match_index + len(normalized_query)
    padding = max(20, (snippet_chars - len(normalized_query)) // 2)
    start = max(0, match_index - padding)
    end = min(len(flattened), match_end + padding)
    snippet = flattened[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(flattened):
        snippet = snippet + "..."
    return snippet


def _build_fts_match_expression(query: str) -> str | None:
    terms = [part for part in query.strip().split() if part]
    if not terms:
        return None
    if any(any(char in term for char in {"%", "_"}) for term in terms):
        return None
    normalized_terms: list[str] = []
    for term in terms:
        if not re.search(r"[A-Za-z0-9]", term):
            return None
        if re.search(r"[^A-Za-z0-9'\\-]", term):
            return None
        escaped_term = term.replace('"', '""')
        normalized_terms.append(f'"{escaped_term}"')
    if not normalized_terms:
        return None
    return " AND ".join(normalized_terms)


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


class SessionManager:
    """DB-backed session manager replacing the old in-memory dict."""

    @staticmethod
    async def _claim_session_owner(db, session: Session, owner_principal_id: str | None) -> None:
        if not owner_principal_id:
            return
        existing_owner_principal_id = str(session.owner_principal_id or "").strip() or None
        if existing_owner_principal_id is None:
            claimed = await db.execute(
                update(Session)
                .where(
                    Session.id == session.id,
                    Session.owner_principal_id.is_(None),
                )
                .values(owner_principal_id=owner_principal_id)
            )
            await db.flush()
            if claimed.rowcount != 1:
                await db.refresh(session)
                existing_owner_principal_id = str(session.owner_principal_id or "").strip() or None
                if existing_owner_principal_id != owner_principal_id:
                    raise SessionOwnerMismatchError(session.id)
            else:
                session.owner_principal_id = owner_principal_id
        elif existing_owner_principal_id != owner_principal_id:
            raise SessionOwnerMismatchError(session.id)

    async def get_or_create(
        self,
        session_id: str | None = None,
        *,
        owner_principal_id: str | None = None,
    ) -> Session:
        if session_id is not None and not str(session_id).strip():
            raise ValueError("session_id must not be blank")
        if owner_principal_id is not None and not str(owner_principal_id).strip():
            raise ValueError("owner_principal_id must not be blank")
        normalized_owner_principal_id = str(owner_principal_id or "").strip() or None
        async with get_session() as db:
            if session_id:
                result = await db.execute(select(Session).where(Session.id == session_id))
                session = result.scalars().first()
                if session:
                    await self._claim_session_owner(db, session, normalized_owner_principal_id)
                    db.expunge(session)
                    return session

            new_id = session_id or uuid.uuid4().hex
            for attempt in range(2):
                session = Session(
                    id=new_id,
                    owner_principal_id=normalized_owner_principal_id,
                    title="New Conversation",
                )
                db.add(session)
                try:
                    await db.flush()
                except IntegrityError:
                    await db.rollback()
                    if session_id:
                        result = await db.execute(select(Session).where(Session.id == new_id))
                        existing = result.scalars().first()
                        if existing is None:
                            raise
                        await self._claim_session_owner(
                            db,
                            existing,
                            normalized_owner_principal_id,
                        )
                        db.expunge(existing)
                        return existing
                    if attempt == 1:
                        raise
                    new_id = uuid.uuid4().hex
                else:
                    db.expunge(session)
                    return session
            raise RuntimeError("session creation retry exhausted")

    async def get_for_ingress(
        self,
        session_id: str | None = None,
        *,
        owner_principal_id: str | None = None,
    ) -> Session:
        """Resolve an ingress session without creating an unknown explicit ID."""
        if session_id is None:
            return await self.get_or_create(None, owner_principal_id=owner_principal_id)
        normalized_session_id = str(session_id).strip()
        if not normalized_session_id:
            raise ValueError("session_id must not be blank")
        async with get_session() as db:
            result = await db.execute(
                select(Session).where(Session.id == normalized_session_id)
            )
            session = result.scalars().first()
            if session is None:
                raise SessionNotFoundError(normalized_session_id)
            await self._claim_session_owner(db, session, owner_principal_id)
            db.expunge(session)
            return session

    async def get(
        self,
        session_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> Session | None:
        async with get_session() as db:
            stmt = select(Session).where(Session.id == session_id)
            if owner_principal_id is not None:
                stmt = stmt.where(Session.owner_principal_id == owner_principal_id)
            result = await db.execute(stmt)
            session = result.scalars().first()
            if session:
                db.expunge(session)
            return session

    async def get_message(self, message_id: str) -> Message | None:
        """Read one persisted transcript message by its server-owned ID."""
        async with get_session() as db:
            result = await db.execute(select(Message).where(Message.id == message_id))
            message = result.scalars().first()
            if message:
                db.expunge(message)
            return message

    async def delete(
        self,
        session_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> bool:
        if owner_principal_id is not None:
            # Claiming an unowned legacy session is safe only through the
            # authenticated ingress path. Once claimed, deletion cannot race
            # another principal without failing this check.
            await self.get_for_ingress(
                session_id,
                owner_principal_id=owner_principal_id,
            )
        cleanup_fence_acquired = process_runtime_manager.begin_session_cleanup(session_id)
        if not cleanup_fence_acquired:
            return False
        try:
            return await self._delete_session_records(
                session_id,
                owner_principal_id=owner_principal_id,
            )
        finally:
            if cleanup_fence_acquired:
                process_runtime_manager.end_session_cleanup(session_id)

    async def _delete_session_records(
        self,
        session_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> bool:
        await flush_session_memory(session_id, trigger="session_end", manager=self)
        async with get_session() as db:
            result = await db.execute(select(Session).where(Session.id == session_id))
            session = result.scalars().first()
            if not session:
                return False
            if (
                owner_principal_id is not None
                and session.owner_principal_id != owner_principal_id
            ):
                raise SessionOwnerMismatchError(session_id)
            try:
                process_runtime_manager.stop_processes_for_session(
                    session_id,
                    cleanup_fence_held=True,
                    fail_closed=True,
                )
            except SessionProcessCleanupError as exc:
                logger.warning(
                    "Session deletion blocked because process cleanup is incomplete for %s: %s",
                    session_id,
                    exc,
                )
                return False
            msgs = await db.execute(
                select(Message).where(Message.session_id == session_id)
            )
            session_messages = msgs.scalars().all()
            message_ids = tuple(
                dict.fromkeys(message.id for message in session_messages if message.id)
            )
            episode_filters = [MemoryEpisode.session_id == session_id]
            if message_ids:
                episode_filters.append(col(MemoryEpisode.source_message_id).in_(message_ids))
            episodes = await db.execute(
                select(MemoryEpisode).where(or_(*episode_filters))
            )
            for episode in episodes.scalars().all():
                await db.delete(episode)
            # Delete associated messages first
            for msg in session_messages:
                await db.delete(msg)
            todos = await db.execute(
                select(SessionTodo).where(SessionTodo.session_id == session_id)
            )
            for todo in todos.scalars().all():
                await db.delete(todo)
            scheduled_jobs = await db.execute(
                select(ScheduledJob).where(
                    (ScheduledJob.session_id == session_id)
                    | (ScheduledJob.created_by_session_id == session_id)
                )
            )
            for scheduled_job in scheduled_jobs.scalars().all():
                await db.delete(scheduled_job)
            approval_requests = await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.session_id == session_id)
            )
            for approval_request in approval_requests.scalars().all():
                await db.delete(approval_request)
            audit_events = await db.execute(
                select(AuditEvent).where(AuditEvent.session_id == session_id)
            )
            for audit_event in audit_events.scalars().all():
                await db.delete(audit_event)
            queued_insights = await db.execute(
                select(QueuedInsight).where(QueuedInsight.session_id == session_id)
            )
            for queued_insight in queued_insights.scalars().all():
                await db.delete(queued_insight)
            interventions = await db.execute(
                select(GuardianIntervention).where(GuardianIntervention.session_id == session_id)
            )
            for intervention in interventions.scalars().all():
                await db.delete(intervention)
            # A deleted conversation can never resume an old native delivery.
            # Preserve the outbox receipt while cancelling active handoffs so
            # a restarted daemon cannot dispatch stale content.
            await db.execute(
                update(NativeNotificationOutbox)
                .where(
                    NativeNotificationOutbox.session_id == session_id,
                    NativeNotificationOutbox.status.in_(
                        {"queued", "claimed", "display_attempted"}
                    ),
                )
                .values(
                    status="cancelled",
                    last_error="conversation_deleted",
                    degraded_state="conversation_deleted",
                    lease_owner=None,
                    lease_expires_at=None,
                    cancelled_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            # A deleted canonical conversation cannot resume a Telegram
            # delivery after restart. Preserve the receipt but cancel any
            # queued/claimed handoff under the same owner fence.
            await db.execute(
                update(TelegramTransportOutbox)
                .where(
                    TelegramTransportOutbox.session_id == session_id,
                    TelegramTransportOutbox.status.in_({"queued", "sending", "unknown"}),
                )
                .values(
                    status="cancelled",
                    last_error="conversation_deleted",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await db.delete(session)
            return True

    async def list_sessions(
        self,
        *,
        owner_principal_id: str | None = None,
    ) -> list[dict]:
        try:
            async with get_session() as db:
                # Single query: fetch sessions with their latest message using window function
                rows = (await db.execute(text(
                    """
                    SELECT
                        s.id, s.title, s.created_at, s.updated_at,
                        lm.content AS last_content, lm.role AS last_role
                    FROM sessions s
                    LEFT JOIN (
                        SELECT session_id, content, role,
                               ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY created_at DESC) AS rn
                        FROM messages
                    ) lm ON lm.session_id = s.id AND lm.rn = 1
                    WHERE (:owner_principal_id IS NULL OR s.owner_principal_id = :owner_principal_id)
                    ORDER BY s.updated_at DESC
                    """
                ), {"owner_principal_id": owner_principal_id})).all()

                return [
                    {
                        "id": r.id,
                        "title": r.title,
                        "created_at": r.created_at if isinstance(r.created_at, str) else r.created_at.isoformat(),
                        "updated_at": r.updated_at if isinstance(r.updated_at, str) else r.updated_at.isoformat(),
                        "last_message": r.last_content[:100] if r.last_content else None,
                        "last_message_role": r.last_role,
                    }
                    for r in rows
                ]
        except SQLAlchemyError as exc:
            logger.warning("Session list unavailable; returning empty list: %s", exc)
            return []

    async def get_recent_sessions_summary(
        self,
        *,
        exclude_session_id: str | None = None,
        limit_sessions: int = 3,
        snippet_chars: int = 140,
    ) -> str:
        """Summarize recent sessions outside the current thread for guardian state."""
        try:
            async with get_session() as db:
                stmt = select(Session)
                if exclude_session_id:
                    stmt = stmt.where(Session.id != exclude_session_id)
                session_result = await db.execute(stmt)
                sessions = session_result.scalars().all()
                if not sessions:
                    return ""

                recency_stmt = (
                    select(Message.session_id, func.max(Message.created_at))
                    .where(Message.role.in_(["user", "assistant"]))  # type: ignore[attr-defined]
                    .group_by(Message.session_id)
                )
                if exclude_session_id:
                    recency_stmt = recency_stmt.where(Message.session_id != exclude_session_id)
                recency_rows = await db.execute(recency_stmt)
                conversation_recency = {
                    session_id: latest_at
                    for session_id, latest_at in recency_rows.all()
                }
                sessions = sorted(
                    sessions,
                    key=lambda session: conversation_recency.get(session.id) or session.created_at,
                    reverse=True,
                )[:limit_sessions]

                lines: list[str] = []
                for session in sessions:
                    msg_result = await db.execute(
                        select(Message)
                        .where(Message.session_id == session.id)
                        .where(Message.role.in_(["user", "assistant"]))  # type: ignore[attr-defined]
                        .order_by(col(Message.created_at).desc())
                        .limit(1)
                    )
                    latest = msg_result.scalars().first()
                    title = session.title or "Untitled session"
                    if latest and latest.content:
                        snippet = latest.content.replace("\n", " ").strip()
                        if len(snippet) > snippet_chars:
                            snippet = snippet[:snippet_chars] + "..."
                        lines.append(f"- {title}: {latest.role} said \"{snippet}\"")
                    else:
                        lines.append(f"- {title}: no user-facing messages yet")

                return "\n".join(lines)
        except SQLAlchemyError as exc:
            logger.warning("Recent sessions summary unavailable; returning empty summary: %s", exc)
            return ""

    async def _conversation_recency_map(
        self,
        db,
        *,
        exclude_session_id: str | None = None,
    ) -> dict[str, datetime]:
        recency_stmt = (
            select(Message.session_id, func.max(Message.created_at))
            .where(Message.role.in_(["user", "assistant"]))  # type: ignore[attr-defined]
            .group_by(Message.session_id)
        )
        if exclude_session_id:
            recency_stmt = recency_stmt.where(Message.session_id != exclude_session_id)
        recency_rows = await db.execute(recency_stmt)
        return {
            session_id: latest_at
            for session_id, latest_at in recency_rows.all()
        }

    async def _search_sessions_fallback(
        self,
        *,
        db,
        normalized_query: str,
        session_map: dict[str, Session],
        limit: int,
        exclude_session_id: str | None,
        snippet_chars: int,
    ) -> list[dict]:
        pattern = f"%{_escape_like(normalized_query)}%"
        message_recency = await self._conversation_recency_map(
            db,
            exclude_session_id=exclude_session_id,
        )
        sessions = list(session_map.values())

        title_hits = {
            session.id: {
                "session_id": session.id,
                "title": session.title or "Untitled session",
                "matched_at": message_recency.get(session.id) or session.created_at,
                "snippet": session.title or "Untitled session",
                "source": "title",
                "rank": 0.0,
            }
            for session in sessions
            if normalized_query in (session.title or "").lower()
        }

        message_stmt = (
            select(Message)
            .where(Message.role.in_(["user", "assistant"]))  # type: ignore[attr-defined]
            .where(func.lower(Message.content).like(pattern, escape="\\"))
            .order_by(col(Message.created_at).desc())
        )
        if exclude_session_id:
            message_stmt = message_stmt.where(Message.session_id != exclude_session_id)
        message_rows = await db.execute(message_stmt)

        combined = dict(title_hits)
        for message in message_rows.scalars().all():
            session = session_map.get(message.session_id)
            if session is None or message.session_id in combined:
                continue
            combined[message.session_id] = {
                "session_id": message.session_id,
                "title": session.title or "Untitled session",
                "matched_at": message.created_at,
                "snippet": _matching_snippet(
                    message.content,
                    normalized_query,
                    snippet_chars=snippet_chars,
                ),
                "source": "message",
                "rank": 1.0,
            }

        event_stmt = (
            select(MemoryEpisode)
            .where(MemoryEpisode.session_id.is_not(None))
            .where(MemoryEpisode.episode_type != MemoryEpisodeType.conversation)
            .where(
                or_(
                    func.lower(MemoryEpisode.summary).like(pattern, escape="\\"),
                    func.lower(MemoryEpisode.content).like(pattern, escape="\\"),
                )
            )
            .order_by(
                col(MemoryEpisode.observed_at).desc(),
                col(MemoryEpisode.created_at).desc(),
            )
        )
        if exclude_session_id:
            event_stmt = event_stmt.where(MemoryEpisode.session_id != exclude_session_id)
        event_rows = await db.execute(event_stmt)
        for episode in event_rows.scalars().all():
            if not isinstance(episode.session_id, str) or episode.session_id in combined:
                continue
            session = session_map.get(episode.session_id)
            if session is None:
                continue
            event_text = "\n".join(
                part.strip()
                for part in (episode.summary or "", episode.content or "")
                if part.strip()
            )
            combined[episode.session_id] = {
                "session_id": episode.session_id,
                "title": session.title or "Untitled session",
                "matched_at": episode.observed_at or episode.created_at,
                "snippet": _matching_snippet(
                    event_text,
                    normalized_query,
                    snippet_chars=snippet_chars,
                ),
                "source": "event",
                "rank": 1.0,
            }

        ordered = sorted(
            combined.values(),
            key=lambda item: (
                item["rank"],
                -(message_recency.get(item["session_id"]) or item["matched_at"]).timestamp(),
                -item["matched_at"].timestamp(),
            ),
        )

        return [
            {
                "session_id": item["session_id"],
                "title": item["title"],
                "matched_at": item["matched_at"].isoformat(),
                "snippet": item["snippet"],
                "source": item["source"],
            }
            for item in ordered[:limit]
        ]

    async def search_sessions(
        self,
        query: str,
        *,
        limit: int = 5,
        exclude_session_id: str | None = None,
        snippet_chars: int = 180,
        owner_principal_id: str | None = None,
    ) -> list[dict]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []

        async with get_session() as db:
            session_stmt = select(Session)
            if exclude_session_id:
                session_stmt = session_stmt.where(Session.id != exclude_session_id)
            if owner_principal_id is not None:
                session_stmt = session_stmt.where(
                    Session.owner_principal_id == owner_principal_id
                )
            session_rows = await db.execute(session_stmt)
            sessions = session_rows.scalars().all()
            if not sessions:
                return []

            session_map = {session.id: session for session in sessions}
            message_recency = await self._conversation_recency_map(
                db,
                exclude_session_id=exclude_session_id,
            )
            match_expression = _build_fts_match_expression(normalized_query)
            if not match_expression:
                return await self._search_sessions_fallback(
                    db=db,
                    normalized_query=normalized_query,
                    session_map=session_map,
                    limit=limit,
                    exclude_session_id=exclude_session_id,
                    snippet_chars=snippet_chars,
                )

            try:
                rows = (
                    await db.execute(
                        text(
                            """
                            SELECT
                                entry_key,
                                session_id,
                                entry_type,
                                source_label,
                                text,
                                created_at,
                                bm25(session_recall_fts) AS rank
                            FROM session_recall_fts
                            WHERE session_recall_fts MATCH :match
                              AND session_id IS NOT NULL
                              AND (:exclude_session_id IS NULL OR session_id != :exclude_session_id)
                            ORDER BY rank ASC, created_at DESC
                            LIMIT :candidate_limit
                            """
                        ),
                        {
                            "match": match_expression,
                            "exclude_session_id": exclude_session_id,
                            "candidate_limit": max(limit * 4, 12),
                        },
                    )
                ).mappings().all()
            except Exception:
                logger.debug("FTS session search failed; falling back to LIKE search", exc_info=True)
                rows = []

            if not rows:
                return await self._search_sessions_fallback(
                    db=db,
                    normalized_query=normalized_query,
                    session_map=session_map,
                    limit=limit,
                    exclude_session_id=exclude_session_id,
                    snippet_chars=snippet_chars,
                )

            combined: dict[str, dict[str, object]] = {}
            for row in rows:
                session_id = row.get("session_id")
                if not isinstance(session_id, str):
                    continue
                session = session_map.get(session_id)
                if session is None:
                    continue
                entry_type = str(row.get("entry_type") or "message")
                source = "title" if entry_type == "title" else "event" if entry_type == "event" else "message"
                raw_text = str(row.get("text") or "")
                matched_at = _coerce_datetime(row.get("created_at"))
                if matched_at is None:
                    continue
                rank = float(row.get("rank") or 0.0)
                existing = combined.get(session_id)
                if existing is not None:
                    existing_rank = float(existing.get("rank") or 0.0)
                    existing_matched_at = existing.get("matched_at")
                    if rank > existing_rank:
                        continue
                    if rank == existing_rank and isinstance(existing_matched_at, datetime) and matched_at <= existing_matched_at:
                        continue
                combined[session_id] = {
                    "session_id": session_id,
                    "title": session.title or "Untitled session",
                    "matched_at": matched_at,
                    "snippet": (
                        session.title or "Untitled session"
                        if source == "title"
                        else _matching_snippet(
                            raw_text,
                            normalized_query,
                            snippet_chars=snippet_chars,
                        )
                    ),
                    "source": source,
                    "rank": rank,
                }

            ordered = sorted(
                combined.values(),
                key=lambda item: (
                    float(item["rank"]),
                    -(message_recency.get(item["session_id"]) or item["matched_at"]).timestamp(),  # type: ignore[union-attr]
                    -item["matched_at"].timestamp(),  # type: ignore[union-attr]
                ),
            )

            return [
                {
                    "session_id": item["session_id"],
                    "title": item["title"],
                    "matched_at": item["matched_at"].isoformat(),
                    "snippet": item["snippet"],
                    "source": item["source"],
                }
                for item in ordered[:limit]
            ]

    async def update_title(
        self,
        session_id: str,
        title: str,
        *,
        owner_principal_id: str | None = None,
    ) -> bool:
        async with get_session() as db:
            stmt = select(Session).where(Session.id == session_id)
            if owner_principal_id is not None:
                stmt = stmt.where(Session.owner_principal_id == owner_principal_id)
            result = await db.execute(stmt)
            session = result.scalars().first()
            if not session:
                return False
            session.title = title
            session.updated_at = datetime.now(timezone.utc)
            db.add(session)
            return True

    @staticmethod
    def _ingress_metadata_matches(
        existing_metadata_json: str | None,
        metadata_json: str,
    ) -> bool:
        try:
            existing = json.loads(existing_metadata_json or "{}")
            requested = json.loads(metadata_json)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(existing, dict) or not isinstance(requested, dict):
            return False
        existing_ingress = existing.get("ingress")
        requested_ingress = requested.get("ingress")
        if not isinstance(existing_ingress, dict) or not isinstance(requested_ingress, dict):
            return False
        return all(
            existing_ingress.get(field) == requested_ingress.get(field)
            for field in (
                "idempotency_key_digest",
                "content_digest",
                "principal_id",
                "device_id",
                "session_id",
                "conversation_id",
                "thread_id",
                "channel",
                "transport",
                "correlation_id",
                "attachment_refs",
            )
        )

    async def reserve_ingress_message(
        self,
        session_id: str,
        content: str,
        *,
        message_id: str,
        metadata_json: str,
        attachment_refs: object = None,
    ) -> tuple[Message, bool]:
        """Persist one user ingress before dispatch and detect safe retries.

        The canonical server message ID is deterministic for a supplied retry
        key.  SQLite's existing primary-key constraint closes the small race
        between concurrent retries without adding a parallel receipt table.
        """
        existing = await self.get_message(message_id)
        if existing is not None:
            if (
                existing.session_id != session_id
                or existing.role != "user"
                or not self._ingress_metadata_matches(existing.metadata_json, metadata_json)
            ):
                raise MessageIngressConflictError(message_id)
            return existing, True
        try:
            message = await self.add_message(
                session_id,
                "user",
                content,
                metadata_json=metadata_json,
                message_id=message_id,
                attachment_refs=attachment_refs,
            )
        except IntegrityError:
            # A concurrent request won the primary-key reservation.  Read its
            # durable row and apply the same identity check.
            raced = await self.get_message(message_id)
            if raced is None:
                raise
            if (
                raced.session_id != session_id
                or raced.role != "user"
                or not self._ingress_metadata_matches(raced.metadata_json, metadata_json)
            ):
                raise MessageIngressConflictError(message_id)
            return raced, True
        return message, False

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        step_number: int | None = None,
        tool_used: str | None = None,
        metadata_json: str | None = None,
        message_id: str | None = None,
        attachment_refs: object = None,
    ) -> Message:
        # Truncate oversized content (50 KB)
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n[truncated]"
        episode_metadata: dict | None = None
        if metadata_json:
            try:
                parsed_metadata = json.loads(metadata_json)
            except json.JSONDecodeError:
                parsed_metadata = None
            if isinstance(parsed_metadata, dict):
                episode_metadata = parsed_metadata
        lineage = episode_metadata.get("lineage") if isinstance(episode_metadata, dict) else None
        if not isinstance(lineage, dict) and isinstance(episode_metadata, dict):
            # Web ingress metadata predates the explicit lineage block.  Keep
            # old rows readable while still projecting their canonical fields.
            candidate = episode_metadata.get("ingress")
            lineage = candidate if isinstance(candidate, dict) else None
        if not isinstance(lineage, dict):
            lineage = {}
        lineage_owner_principal_id = str(
            lineage.get("owner_principal_id") or lineage.get("principal_id") or ""
        ).strip() or None
        lineage_attachment_refs = lineage.get("attachment_refs")
        attachment_input = (
            attachment_refs
            if attachment_refs is not None
            else lineage_attachment_refs
        )
        safe_attachment_refs = validate_attachment_refs(
            attachment_input,
            owner_principal_id=lineage_owner_principal_id,
        )
        lineage_conversation_id = str(
            lineage.get("conversation_id") or lineage.get("session_id") or session_id
        ).strip() or session_id
        lineage_thread_id = str(
            lineage.get("thread_id") or lineage_conversation_id
        ).strip() or lineage_conversation_id
        async with get_session() as db:
            msg = Message(
                id=message_id or uuid.uuid4().hex,
                session_id=session_id,
                conversation_id=lineage_conversation_id,
                thread_id=lineage_thread_id,
                owner_principal_id=(
                    lineage_owner_principal_id
                ),
                operator_session_id=(
                    str(lineage.get("operator_session_id") or "").strip() or None
                ),
                device_id=str(lineage.get("device_id") or "").strip() or None,
                channel=str(lineage.get("channel") or "").strip() or None,
                transport=str(lineage.get("transport") or "").strip() or None,
                correlation_id=str(lineage.get("correlation_id") or "").strip() or None,
                causation_id=str(lineage.get("causation_id") or "").strip() or None,
                attachment_refs_json=json.dumps(safe_attachment_refs, sort_keys=True),
                role=role,
                content=content,
                step_number=step_number,
                tool_used=tool_used,
                metadata_json=metadata_json,
            )
            db.add(msg)
            # Update session timestamp
            result = await db.execute(select(Session).where(Session.id == session_id))
            session = result.scalars().first()
            if session:
                session.updated_at = datetime.now(timezone.utc)
                db.add(session)
            await db.flush()
            episode_draft = build_message_episode(
                role=role,
                content=content,
                tool_used=tool_used,
                metadata=episode_metadata,
            )
            if episode_draft is not None:
                try:
                    async with db.begin_nested():
                        db.add(
                            MemoryEpisode(
                                session_id=session_id,
                                episode_type=episode_draft.episode_type,
                                summary=episode_draft.summary,
                                content=episode_draft.content,
                                source_message_id=msg.id,
                                source_tool_name=episode_draft.source_tool_name,
                                source_role=episode_draft.source_role,
                                salience=episode_draft.salience,
                                confidence=episode_draft.confidence,
                                metadata_json=json.dumps(episode_draft.metadata or {}, sort_keys=True),
                                observed_at=msg.created_at,
                                created_at=msg.created_at,
                            )
                        )
                        await db.flush()
                except Exception:
                    logger.debug(
                        "Failed to persist episodic event for message %s",
                        msg.id,
                        exc_info=True,
                    )
            db.expunge(msg)
            return msg

    async def get_history_text(
        self,
        session_id: str,
        limit: int = 50,
        *,
        allow_memory_flush: bool = True,
    ) -> str:
        async with get_session() as db:
            result = await db.execute(
                select(Message)
                .where(Message.session_id == session_id)
                .where(Message.role.in_(["user", "assistant"]))  # type: ignore[attr-defined]
                .order_by(col(Message.created_at).desc())
                .limit(200)
            )
            messages = list(reversed(result.scalars().all()))
            if not messages:
                return ""

            msg_dicts = [
                {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
                for m in messages
            ]

            try:
                from src.agent.context_window import build_context_window, requires_middle_summary
                if allow_memory_flush and requires_middle_summary(msg_dicts):
                    await flush_session_memory(
                        session_id,
                        trigger="pre_compaction",
                        manager=self,
                    )
                return await asyncio.to_thread(
                    build_context_window,
                    msg_dicts,
                    session_id=session_id,
                )
            except Exception:
                logger.warning("Token-aware context failed, falling back to simple truncation")
                lines = []
                for msg in messages[-limit:]:
                    role = msg.role.capitalize()
                    lines.append(f"{role}: {msg.content}")
                return "\n".join(lines)

    async def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
        *,
        newest_first: bool = False,
    ) -> list[dict]:
        limit = min(max(limit, 1), 1000)
        async with get_session() as db:
            order = col(Message.created_at).desc() if newest_first else col(Message.created_at).asc()
            result = await db.execute(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(order)
                .offset(offset)
                .limit(limit)
            )
            messages = result.scalars().all()
            if newest_first:
                messages.reverse()
            output: list[dict] = []
            for message in messages:
                try:
                    metadata = json.loads(message.metadata_json) if message.metadata_json else None
                except (TypeError, json.JSONDecodeError):
                    metadata = None
                attachment_refs_status = "available"
                try:
                    attachment_refs = validate_attachment_refs(
                        message.attachment_refs_json and json.loads(message.attachment_refs_json),
                        owner_principal_id=message.owner_principal_id,
                    )
                except ConversationIdentityError as exc:
                    attachment_refs = []
                    attachment_refs_status = (
                        "expired" if exc.code == "attachment_receipt_expired" else "unavailable"
                    )
                except Exception:
                    attachment_refs = []
                    attachment_refs_status = "unavailable"
                output.append(
                    {
                        "id": message.id,
                        "role": message.role,
                        "content": message.content,
                        "metadata": metadata,
                        "step_number": message.step_number,
                        "tool_used": message.tool_used,
                        "created_at": message.created_at.isoformat(),
                        "conversation_id": message.conversation_id or message.session_id,
                        "thread_id": message.thread_id or message.session_id,
                        "owner_principal_id": message.owner_principal_id,
                        "operator_session_id": message.operator_session_id,
                        "device_id": message.device_id,
                        "channel": message.channel,
                        "transport": message.transport,
                        "correlation_id": message.correlation_id,
                        "causation_id": message.causation_id,
                        "attachment_refs": attachment_refs,
                        "attachment_refs_status": attachment_refs_status,
                    }
                )
            return output

    async def get_todos(self, session_id: str) -> list[dict]:
        async with get_session() as db:
            result = await db.execute(
                select(SessionTodo)
                .where(SessionTodo.session_id == session_id)
                .order_by(col(SessionTodo.sort_order).asc(), col(SessionTodo.created_at).asc())
            )
            return [
                {
                    "id": todo.id,
                    "content": todo.content,
                    "completed": bool(todo.completed),
                    "sort_order": todo.sort_order,
                    "created_at": todo.created_at.isoformat(),
                    "updated_at": todo.updated_at.isoformat(),
                }
                for todo in result.scalars().all()
            ]

    async def replace_todos(self, session_id: str, items: list[dict]) -> list[dict]:
        async with get_session() as db:
            await ensure_sessions_exist(db, [session_id])
            existing = await db.execute(
                select(SessionTodo).where(SessionTodo.session_id == session_id)
            )
            for todo in existing.scalars().all():
                await db.delete(todo)

            now = datetime.now(timezone.utc)
            for index, item in enumerate(items):
                todo = SessionTodo(
                    session_id=session_id,
                    content=str(item.get("content", "")).strip(),
                    completed=bool(item.get("completed", False)),
                    sort_order=index,
                    created_at=now,
                    updated_at=now,
                )
                db.add(todo)

            session = await db.execute(select(Session).where(Session.id == session_id))
            current = session.scalars().first()
            if current:
                current.updated_at = now
                db.add(current)

        return await self.get_todos(session_id)

    async def append_todos(self, session_id: str, items: list[dict]) -> list[dict]:
        async with get_session() as db:
            await ensure_sessions_exist(db, [session_id])
            existing = await db.execute(
                select(SessionTodo)
                .where(SessionTodo.session_id == session_id)
                .order_by(col(SessionTodo.sort_order).desc())
                .limit(1)
            )
            last = existing.scalars().first()
            next_sort_order = (last.sort_order + 1) if last else 0
            now = datetime.now(timezone.utc)
            for item in items:
                todo = SessionTodo(
                    session_id=session_id,
                    content=str(item.get("content", "")).strip(),
                    completed=bool(item.get("completed", False)),
                    sort_order=next_sort_order,
                    created_at=now,
                    updated_at=now,
                )
                db.add(todo)
                next_sort_order += 1

            session = await db.execute(select(Session).where(Session.id == session_id))
            current = session.scalars().first()
            if current:
                current.updated_at = now
                db.add(current)

        return await self.get_todos(session_id)

    async def update_todo_completion(
        self,
        session_id: str,
        item_ref: str,
        *,
        completed: bool,
    ) -> list[dict] | None:
        async with get_session() as db:
            todo = await self._resolve_todo(db, session_id, item_ref)
            if todo is None:
                return None
            todo.completed = completed
            todo.updated_at = datetime.now(timezone.utc)
            db.add(todo)
            session = await db.execute(select(Session).where(Session.id == session_id))
            current = session.scalars().first()
            if current:
                current.updated_at = datetime.now(timezone.utc)
                db.add(current)

        return await self.get_todos(session_id)

    async def remove_todo(self, session_id: str, item_ref: str) -> list[dict] | None:
        async with get_session() as db:
            todo = await self._resolve_todo(db, session_id, item_ref)
            if todo is None:
                return None
            removed_sort_order = todo.sort_order
            await db.delete(todo)
            later_items = await db.execute(
                select(SessionTodo)
                .where(SessionTodo.session_id == session_id)
                .where(SessionTodo.sort_order > removed_sort_order)
                .order_by(col(SessionTodo.sort_order).asc())
            )
            for index, item in enumerate(later_items.scalars().all(), start=removed_sort_order):
                item.sort_order = index
                item.updated_at = datetime.now(timezone.utc)
                db.add(item)
            session = await db.execute(select(Session).where(Session.id == session_id))
            current = session.scalars().first()
            if current:
                current.updated_at = datetime.now(timezone.utc)
                db.add(current)

        return await self.get_todos(session_id)

    async def clear_todos(self, session_id: str) -> None:
        async with get_session() as db:
            existing = await db.execute(
                select(SessionTodo).where(SessionTodo.session_id == session_id)
            )
            for todo in existing.scalars().all():
                await db.delete(todo)
            session = await db.execute(select(Session).where(Session.id == session_id))
            current = session.scalars().first()
            if current:
                current.updated_at = datetime.now(timezone.utc)
                db.add(current)

    async def _resolve_todo(
        self,
        db,
        session_id: str,
        item_ref: str,
    ) -> SessionTodo | None:
        normalized = item_ref.strip()
        if not normalized:
            return None

        direct = await db.execute(
            select(SessionTodo)
            .where(SessionTodo.session_id == session_id)
            .where(SessionTodo.id == normalized)
        )
        todo = direct.scalars().first()
        if todo is not None:
            return todo

        if normalized.isdigit():
            parsed_index = int(normalized)
            if parsed_index <= 0:
                return None
            index = parsed_index - 1
            ordered = await db.execute(
                select(SessionTodo)
                .where(SessionTodo.session_id == session_id)
                .order_by(col(SessionTodo.sort_order).asc(), col(SessionTodo.created_at).asc())
            )
            items = ordered.scalars().all()
            if 0 <= index < len(items):
                return items[index]
        return None

    async def generate_title(
        self,
        session_id: str,
        *,
        owner_principal_id: str | None = None,
    ) -> str | None:
        """Generate a short title for a session using LLM."""
        started_at = perf_counter()
        session = await self.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        if not session or session.title != "New Conversation":
            await log_background_task_event(
                task_name="session_title_generation",
                outcome="skipped",
                session_id=session_id,
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "session_missing" if not session else "title_already_set",
                },
            )
            return session.title if session else None

        async with get_session() as db:
            result = await db.execute(
                select(Message)
                .where(Message.session_id == session_id)
                .where(Message.role.in_(["user", "assistant"]))  # type: ignore[attr-defined]
                .order_by(col(Message.created_at).asc())
                .limit(6)
            )
            messages = result.scalars().all()

        if not messages:
            await log_background_task_event(
                task_name="session_title_generation",
                outcome="skipped",
                session_id=session_id,
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "no_messages",
                },
            )
            return None

        transcript = "\n".join(f"{m.role.capitalize()}: {m.content[:200]}" for m in messages)
        runtime_tokens = None

        try:
            from src.llm_runtime import completion_with_fallback

            runtime_tokens = set_runtime_context(
                session_id,
                "high_risk",
                trust_principal=get_current_trust_principal(),
            )
            transport_messages = [{
                    "role": "user",
                    "content": f"Generate a very short title (3-6 words, no quotes) for this conversation. Respond with ONLY the title.\n\n{transcript}",
                }]
            response = await completion_with_fallback(
                messages=transport_messages,
                temperature=0.3,
                max_tokens=20,
                runtime_path="session_title_generation",
                # Active inference is OpenRouter-only; legacy local preference
                # settings must not reintroduce a local model dependency.
                local_runtime_only=False,
                request_context=build_canonical_inference_context(
                    "session_title_generation",
                    payload=transport_messages,
                    output_tokens=20,
                    timeout_seconds=settings.agent_chat_timeout,
                ),
            )

            title = response.choices[0].message.content.strip().strip('"\'')
            await self.update_title(session_id, title)
            logger.info("Generated title for session %s: %s", session_id[:8], title)
            await log_background_task_event(
                task_name="session_title_generation",
                outcome="succeeded",
                session_id=session_id,
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "message_count": len(messages),
                    "title_length": len(title),
                },
            )
            return title
        except Exception as exc:
            await log_background_task_event(
                task_name="session_title_generation",
                outcome="failed",
                session_id=session_id,
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "message_count": len(messages),
                    "error": str(exc),
                },
            )
            logger.exception("Failed to generate title for session %s", session_id[:8])
            return None
        finally:
            if runtime_tokens is not None:
                reset_runtime_context(runtime_tokens)

    async def count_messages(self, session_id: str) -> int:
        """Count user+assistant messages in a session."""
        async with get_session() as db:
            result = await db.execute(
                select(Message)
                .where(Message.session_id == session_id)
                .where(Message.role.in_(["user", "assistant"]))  # type: ignore[attr-defined]
            )
            return len(result.scalars().all())

session_manager = SessionManager()
