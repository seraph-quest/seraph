import logging

from fastapi import WebSocket

from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)


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

    async def broadcast(self, message: WSResponse) -> None:
        """Send a message to all connected clients, dropping any that error."""
        payload = message.model_dump_json()
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)
            logger.debug("Removed dead WS connection (%d active)", self.active_count)


ws_manager = ConnectionManager()
