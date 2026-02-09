import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlmodel import select, col

from config.settings import settings
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
            result = await db.execute(
                select(Session).order_by(col(Session.updated_at).desc())
            )
            sessions = result.scalars().all()
            out = []
            for s in sessions:
                # Get last message preview
                msg_result = await db.execute(
                    select(Message)
                    .where(Message.session_id == s.id)
                    .order_by(col(Message.created_at).desc())
                    .limit(1)
                )
                last_msg = msg_result.scalars().first()
                out.append({
                    "id": s.id,
                    "title": s.title,
                    "created_at": s.created_at.isoformat(),
                    "updated_at": s.updated_at.isoformat(),
                    "last_message": last_msg.content[:100] if last_msg else None,
                    "last_message_role": last_msg.role if last_msg else None,
                })
            return out

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
                .limit(limit)
            )
            messages = list(reversed(result.scalars().all()))
            if not messages:
                return ""
            lines = []
            for msg in messages:
                role = msg.role.capitalize()
                lines.append(f"{role}: {msg.content}")
            return "\n".join(lines)

    async def get_messages(
        self, session_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
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
                    "step_number": m.step_number,
                    "tool_used": m.tool_used,
                    "created_at": m.created_at.isoformat(),
                }
                for m in result.scalars().all()
            ]

    async def generate_title(self, session_id: str) -> str | None:
        """Generate a short title for a session using LLM."""
        session = await self.get(session_id)
        if not session or session.title != "New Conversation":
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
            return None

        transcript = "\n".join(f"{m.role.capitalize()}: {m.content[:200]}" for m in messages)

        try:
            import litellm

            response = await asyncio.to_thread(
                litellm.completion,
                model=settings.default_model,
                messages=[{
                    "role": "user",
                    "content": f"Generate a very short title (3-6 words, no quotes) for this conversation. Respond with ONLY the title.\n\n{transcript}",
                }],
                api_key=settings.openrouter_api_key,
                api_base="https://openrouter.ai/api/v1",
                temperature=0.3,
                max_tokens=20,
            )

            title = response.choices[0].message.content.strip().strip('"\'')
            await self.update_title(session_id, title)
            logger.info("Generated title for session %s: %s", session_id[:8], title)
            return title
        except Exception:
            logger.exception("Failed to generate title for session %s", session_id[:8])
            return None

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
