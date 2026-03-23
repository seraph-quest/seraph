"""In-memory queue for native desktop notifications consumed by the daemon."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import uuid4


@dataclass
class NativeNotification:
    id: str
    intervention_id: str | None
    title: str
    body: str
    intervention_type: str | None
    urgency: int | None
    surface: str
    session_id: str | None
    thread_id: str | None
    thread_source: str
    continuation_mode: str
    resume_message: str | None
    created_at: str

    def to_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


class NativeNotificationQueue:
    """Small fail-open queue for daemon-delivered desktop notifications."""

    def __init__(self) -> None:
        self._items: list[NativeNotification] = []
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        *,
        intervention_id: str | None,
        title: str,
        body: str,
        intervention_type: str | None,
        urgency: int | None,
        surface: str = "notification",
        session_id: str | None = None,
        thread_id: str | None = None,
        thread_source: str = "ambient",
        continuation_mode: str = "open_thread",
        resume_message: str | None = None,
    ) -> NativeNotification:
        notification = NativeNotification(
            id=str(uuid4()),
            intervention_id=intervention_id,
            title=title,
            body=body,
            intervention_type=intervention_type,
            urgency=urgency,
            surface=surface,
            session_id=session_id,
            thread_id=thread_id or session_id,
            thread_source=thread_source,
            continuation_mode=continuation_mode,
            resume_message=resume_message,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        async with self._lock:
            self._items.append(notification)
        return notification

    async def peek(self) -> NativeNotification | None:
        async with self._lock:
            if not self._items:
                return None
            return self._items[0]

    async def get(self, notification_id: str) -> NativeNotification | None:
        async with self._lock:
            for item in self._items:
                if item.id == notification_id:
                    return item
        return None

    async def list(self) -> list[NativeNotification]:
        async with self._lock:
            return list(self._items)

    async def ack(self, notification_id: str) -> bool:
        async with self._lock:
            for idx, item in enumerate(self._items):
                if item.id == notification_id:
                    self._items.pop(idx)
                    return True
        return False

    async def dismiss(self, notification_id: str) -> NativeNotification | None:
        async with self._lock:
            for idx, item in enumerate(self._items):
                if item.id == notification_id:
                    return self._items.pop(idx)
        return None

    async def dismiss_all(self) -> list[NativeNotification]:
        async with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    async def count(self) -> int:
        async with self._lock:
            return len(self._items)

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()


native_notification_queue = NativeNotificationQueue()
