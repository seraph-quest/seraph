import logging
from dataclasses import dataclass, replace

from fastapi import WebSocket

from src.conversation.identity import ConversationIdentityError, validate_attachment_refs
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BroadcastResult:
    attempted_connections: int
    delivered_connections: int
    failed_connections: int


@dataclass(frozen=True)
class _ConnectionBinding:
    """Authenticated scope attached to one WebSocket connection."""

    owner_principal_id: str | None = None
    operator_session_id: str | None = None
    conversation_id: str | None = None


class ConnectionManager:
    """Registry of active WebSocket connections for broadcasting proactive messages."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._bindings: dict[WebSocket, _ConnectionBinding] = {}

    @property
    def active_count(self) -> int:
        return len(self._connections)

    def connect(
        self,
        ws: WebSocket,
        *,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        self._connections.add(ws)
        self._bindings[ws] = _ConnectionBinding(
            owner_principal_id=str(owner_principal_id or "").strip() or None,
            operator_session_id=str(operator_session_id or "").strip() or None,
            conversation_id=str(conversation_id or "").strip() or None,
        )
        logger.debug("WS registered (%d active)", self.active_count)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        self._bindings.pop(ws, None)
        logger.debug("WS unregistered (%d active)", self.active_count)

    def bind_conversation(self, ws: WebSocket, conversation_id: str | None) -> None:
        """Update the canonical conversation currently selected by a client."""
        if ws not in self._connections:
            return
        binding = self._bindings.get(ws, _ConnectionBinding())
        self._bindings[ws] = replace(
            binding,
            conversation_id=str(conversation_id or "").strip() or None,
        )

    def bind_operator(
        self,
        ws: WebSocket,
        *,
        owner_principal_id: str | None,
        operator_session_id: str | None,
    ) -> None:
        """Refresh authentication scope without dropping conversation state."""
        if ws not in self._connections:
            return
        binding = self._bindings.get(ws, _ConnectionBinding())
        self._bindings[ws] = replace(
            binding,
            owner_principal_id=str(owner_principal_id or "").strip() or None,
            operator_session_id=str(operator_session_id or "").strip() or None,
        )

    async def broadcast(self, message: WSResponse) -> BroadcastResult:
        """Send ambient messages broadly and bound messages only in scope.

        A bound response must match the authenticated owner/operator and the
        selected conversation. Connections without an authenticated binding
        are skipped for bound messages, which keeps a stale or partially
        initialized socket from becoming a cross-operator broadcast sink.
        """
        if message.attachment_refs:
            try:
                message = message.model_copy(
                    update={
                        "attachment_refs": validate_attachment_refs(
                            message.attachment_refs,
                            owner_principal_id=message.owner_principal_id,
                        )
                    }
                )
            except ConversationIdentityError:
                # A malformed or expired attachment must never become a WS
                # bearer-token frame. The delivery boundary records the
                # degraded state; this lower-level fan-out guard fails closed
                # for direct server callers as well.
                logger.warning("Skipped WS broadcast with invalid attachment refs")
                return BroadcastResult(
                    attempted_connections=0,
                    delivered_connections=0,
                    failed_connections=0,
                )
        payload = message.model_dump_json()
        dead: list[WebSocket] = []
        message_owner = str(message.owner_principal_id or "").strip() or None
        message_operator = str(message.operator_session_id or "").strip() or None
        message_conversation = str(
            message.conversation_id or message.session_id or ""
        ).strip() or None
        bound_message = bool(message_owner or message_operator or message_conversation)
        eligible: list[WebSocket] = []
        for ws in self._connections:
            binding = self._bindings.get(ws)
            if not bound_message:
                eligible.append(ws)
                continue
            if binding is None:
                continue
            if message_owner is None or binding.owner_principal_id != message_owner:
                continue
            if message_operator is not None and binding.operator_session_id != message_operator:
                continue
            if message_conversation is not None and binding.conversation_id != message_conversation:
                continue
            eligible.append(ws)
        attempted_connections = len(eligible)
        failed_connections = 0
        for ws in eligible:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
                failed_connections += 1
        for ws in dead:
            self._connections.discard(ws)
            self._bindings.pop(ws, None)
            logger.debug("Removed dead WS connection (%d active)", self.active_count)
        delivered_connections = attempted_connections - failed_connections
        return BroadcastResult(
            attempted_connections=attempted_connections,
            delivered_connections=delivered_connections,
            failed_connections=failed_connections,
        )


ws_manager = ConnectionManager()
