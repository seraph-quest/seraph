from __future__ import annotations

from dataclasses import dataclass

from src.agent.session import session_manager


@dataclass(frozen=True)
class CapturedSessionMessage:
    id: str
    role: str
    content: str
    created_at: str
    tool_used: str | None = None


@dataclass(frozen=True)
class SessionMemoryCapture:
    session_id: str
    history_text: str
    source_messages: tuple[CapturedSessionMessage, ...]


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def _is_candidate_message(message: dict[str, object]) -> bool:
    role = str(message.get("role") or "").strip().lower()
    if role not in {"user", "assistant", "step", "error"}:
        return False
    return len(_normalize_text(message.get("content"))) >= 16


async def capture_session_memory(
    session_id: str,
    *,
    manager=None,
    history_limit: int = 30,
    message_window: int = 60,
) -> SessionMemoryCapture:
    session_manager_ref = manager or session_manager
    history_text = await session_manager_ref.get_history_text(
        session_id,
        limit=history_limit,
        allow_memory_flush=False,
    )
    recent_messages = await session_manager_ref.get_messages(
        session_id,
        limit=max(message_window, history_limit),
    )
    source_messages: list[CapturedSessionMessage] = []
    for message in recent_messages[-message_window:]:
        if not _is_candidate_message(message):
            continue
        source_messages.append(
            CapturedSessionMessage(
                id=str(message.get("id") or ""),
                role=str(message.get("role") or ""),
                content=_normalize_text(message.get("content")),
                created_at=str(message.get("created_at") or ""),
                tool_used=(
                    str(message.get("tool_used"))
                    if isinstance(message.get("tool_used"), str)
                    else None
                ),
            )
        )
    return SessionMemoryCapture(
        session_id=session_id,
        history_text=history_text,
        source_messages=tuple(source_messages),
    )
