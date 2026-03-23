import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from time import perf_counter

from sqlalchemy import func
from sqlmodel import select, col

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.runtime import log_background_task_event
from src.db.engine import get_session
from src.db.models import Session, Message, SessionTodo

logger = logging.getLogger(__name__)


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


class SessionManager:
    """DB-backed session manager replacing the old in-memory dict."""

    async def get_or_create(self, session_id: str | None = None) -> Session:
        async with get_session() as db:
            if session_id:
                result = await db.execute(select(Session).where(Session.id == session_id))
                session = result.scalars().first()
                if session:
                    db.expunge(session)
                    return session

            new_id = session_id or uuid.uuid4().hex
            session = Session(id=new_id, title="New Conversation")
            db.add(session)
            await db.flush()
            db.expunge(session)
            return session

    async def get(self, session_id: str) -> Session | None:
        async with get_session() as db:
            result = await db.execute(select(Session).where(Session.id == session_id))
            session = result.scalars().first()
            if session:
                db.expunge(session)
            return session

    async def delete(self, session_id: str) -> bool:
        async with get_session() as db:
            result = await db.execute(select(Session).where(Session.id == session_id))
            session = result.scalars().first()
            if not session:
                return False
            # Delete associated messages first
            msgs = await db.execute(
                select(Message).where(Message.session_id == session_id)
            )
            for msg in msgs.scalars().all():
                await db.delete(msg)
            todos = await db.execute(
                select(SessionTodo).where(SessionTodo.session_id == session_id)
            )
            for todo in todos.scalars().all():
                await db.delete(todo)
            await db.delete(session)
            return True

    async def list_sessions(self) -> list[dict]:
        async with get_session() as db:
            # Single query: fetch sessions with their latest message using window function
            from sqlalchemy import text
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
                ORDER BY s.updated_at DESC
                """
            ))).all()

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

    async def get_recent_sessions_summary(
        self,
        *,
        exclude_session_id: str | None = None,
        limit_sessions: int = 3,
        snippet_chars: int = 140,
    ) -> str:
        """Summarize recent sessions outside the current thread for guardian state."""
        async with get_session() as db:
            stmt = select(Session)
            if exclude_session_id:
                stmt = stmt.where(Session.id != exclude_session_id)
            result = await db.execute(
                stmt.order_by(col(Session.updated_at).desc()).limit(limit_sessions)
            )
            sessions = result.scalars().all()
            if not sessions:
                return ""

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

    async def search_sessions(
        self,
        query: str,
        *,
        limit: int = 5,
        exclude_session_id: str | None = None,
        snippet_chars: int = 180,
    ) -> list[dict]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []

        async with get_session() as db:
            session_stmt = select(Session)
            if exclude_session_id:
                session_stmt = session_stmt.where(Session.id != exclude_session_id)
            session_rows = await db.execute(session_stmt)
            sessions = session_rows.scalars().all()
            if not sessions:
                return []

            session_map = {session.id: session for session in sessions}
            pattern = f"%{_escape_like(normalized_query)}%"
            recency_rows = await db.execute(
                select(Message.session_id, func.max(Message.created_at))
                .where(Message.role.in_(["user", "assistant"]))  # type: ignore[attr-defined]
                .group_by(Message.session_id)
            )
            message_recency = {
                session_id: latest_at
                for session_id, latest_at in recency_rows.all()
            }

            title_hits = {
                session.id: {
                    "session_id": session.id,
                    "title": session.title or "Untitled session",
                    "matched_at": message_recency.get(session.id) or session.created_at,
                    "snippet": session.title or "Untitled session",
                    "source": "title",
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
                if session is None:
                    continue
                if message.session_id in combined:
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
                }

            ordered = sorted(
                combined.values(),
                key=lambda item: item["matched_at"],
                reverse=True,
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

    async def update_title(self, session_id: str, title: str) -> bool:
        async with get_session() as db:
            result = await db.execute(select(Session).where(Session.id == session_id))
            session = result.scalars().first()
            if not session:
                return False
            session.title = title
            session.updated_at = datetime.now(timezone.utc)
            db.add(session)
            return True

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        step_number: int | None = None,
        tool_used: str | None = None,
        metadata_json: str | None = None,
    ) -> Message:
        # Truncate oversized content (50 KB)
        if len(content) > 50_000:
            content = content[:50_000] + "\n\n[truncated]"
        async with get_session() as db:
            msg = Message(
                session_id=session_id,
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
            db.expunge(msg)
            return msg

    async def get_history_text(
        self, session_id: str, limit: int = 50
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
                from src.agent.context_window import build_context_window
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
        self, session_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        limit = min(max(limit, 1), 1000)
        async with get_session() as db:
            result = await db.execute(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(col(Message.created_at).asc())
                .offset(offset)
                .limit(limit)
            )
            return [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "metadata": json.loads(m.metadata_json) if m.metadata_json else None,
                    "step_number": m.step_number,
                    "tool_used": m.tool_used,
                    "created_at": m.created_at.isoformat(),
                }
                for m in result.scalars().all()
            ]

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

    async def generate_title(self, session_id: str) -> str | None:
        """Generate a short title for a session using LLM."""
        started_at = perf_counter()
        session = await self.get(session_id)
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

            runtime_tokens = set_runtime_context(session_id, "high_risk")
            response = await completion_with_fallback(
                messages=[{
                    "role": "user",
                    "content": f"Generate a very short title (3-6 words, no quotes) for this conversation. Respond with ONLY the title.\n\n{transcript}",
                }],
                temperature=0.3,
                max_tokens=20,
                runtime_path="session_title_generation",
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
