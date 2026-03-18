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
    ) -> NativeNotification:
        notification = NativeNotification(
            id=str(uuid4()),
            intervention_id=intervention_id,
            title=title,
            body=body,
            intervention_type=intervention_type,
            urgency=urgency,
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

    async def ack(self, notification_id: str) -> bool:
        async with self._lock:
            for idx, item in enumerate(self._items):
                if item.id == notification_id:
                    self._items.pop(idx)
                    return True
        return False

    async def count(self) -> int:
        async with self._lock:
            return len(self._items)

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()


native_notification_queue = NativeNotificationQueue()
