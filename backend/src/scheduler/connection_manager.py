import logging
from dataclasses import dataclass

from fastapi import WebSocket

from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BroadcastResult:
    attempted_connections: int
    delivered_connections: int
    failed_connections: int


class ConnectionManager:
    """Registry of active WebSocket connections for broadcasting proactive messages."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    @property
    def active_count(self) -> int:
        return len(self._connections)

    def connect(self, ws: WebSocket) -> None:
        self._connections.add(ws)
        logger.debug("WS registered (%d active)", self.active_count)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        logger.debug("WS unregistered (%d active)", self.active_count)

    async def broadcast(self, message: WSResponse) -> BroadcastResult:
        """Send a message to all connected clients, dropping any that error."""
        payload = message.model_dump_json()
        dead: list[WebSocket] = []
        attempted_connections = len(self._connections)
        failed_connections = 0
        for ws in self._connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
                failed_connections += 1
        for ws in dead:
            self._connections.discard(ws)
            logger.debug("Removed dead WS connection (%d active)", self.active_count)
        delivered_connections = attempted_connections - failed_connections
        return BroadcastResult(
            attempted_connections=attempted_connections,
            delivered_connections=delivered_connections,
            failed_connections=failed_connections,
        )


ws_manager = ConnectionManager()
