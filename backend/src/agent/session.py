import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from time import perf_counter

from sqlmodel import select, col

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.runtime import log_background_task_event
from src.db.engine import get_session
from src.db.models import Session, Message

logger = logging.getLogger(__name__)


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
