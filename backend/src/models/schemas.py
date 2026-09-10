from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message to the agent")
    session_id: str | None = Field(None, description="Session ID for conversation continuity")
    message_id: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="Optional caller message identity used for safe retry correlation",
    )
    idempotency_key: str | None = Field(
        None,
        min_length=1,
        max_length=256,
        description="Opaque retry key for this message; the server stores only its digest",
    )
    attachments: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=32,
        description="Optional public attachment metadata; private file references are discarded",
    )


class ChatIngressEnvelope(BaseModel):
    """Server-owned metadata persisted for one interactive message ingress.

    ``idempotency_key_digest`` and ``content_digest`` keep retry and identity
    checks durable without putting message content or a caller-supplied key in
    audit details.  The envelope is created only after the authenticated
    operator and canonical session have been bound.
    """

    schema_version: Literal["seraph.chat.message.v1"] = "seraph.chat.message.v1"
    conversation_schema_version: Literal["seraph.conversation.v1"] = "seraph.conversation.v1"
    message_id: str = Field(..., min_length=1, max_length=64)
    client_message_id: str | None = Field(None, max_length=128)
    idempotency_key: str = Field(..., min_length=7, max_length=80)
    idempotency_key_digest: str = Field(..., min_length=64, max_length=64)
    principal_id: str = Field(..., min_length=1, max_length=256)
    operator_session_id: str = Field(..., min_length=1, max_length=256)
    device_id: str = Field(..., min_length=1, max_length=256)
    channel: Literal["web"] = "web"
    transport: Literal["rest", "websocket"]
    session_id: str = Field(..., min_length=1, max_length=256)
    conversation_id: str = Field(..., min_length=1, max_length=256)
    thread_id: str = Field(..., min_length=1, max_length=256)
    correlation_id: str = Field(..., min_length=1, max_length=256)
    causation_id: str | None = Field(None, max_length=256)
    content_digest: str = Field(..., min_length=64, max_length=64)
    attachment_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    received_at: datetime


class AgentStep(BaseModel):
    step: int
    type: str
    content: str


class ChatResponse(BaseModel):
    response: str
    session_id: str
    conversation_id: str = ""
    thread_id: str = ""
    message_id: str | None = None
    owner_principal_id: str | None = None
    operator_session_id: str | None = None
    device_id: str | None = None
    channel: str = "web"
    transport: str = "rest"
    correlation_id: str | None = None
    causation_id: str | None = None
    attachment_refs: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)


class WSMessage(BaseModel):
    type: str = Field("message", description="Message type: message | resume_message | ping | skip_onboarding")
    message: str = Field("", min_length=1, description="User message")
    session_id: str | None = None
    message_id: str | None = Field(None, min_length=1, max_length=128)
    idempotency_key: str | None = Field(None, min_length=1, max_length=256)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=32)


class WSResponse(BaseModel):
    type: str = Field(..., description="Response type: status | step | delta | final | error | pong | proactive | proactive_bundle | ambient | approval_required | clarification_required")
    content: str = ""
    session_id: str = ""
    conversation_id: str = ""
    thread_id: str = ""
    message_id: str | None = None
    owner_principal_id: str | None = None
    operator_session_id: str | None = None
    device_id: str | None = None
    channel: str = "web"
    transport: str = "websocket"
    correlation_id: str | None = None
    causation_id: str | None = None
    attachment_refs: list[dict[str, Any]] = Field(default_factory=list)
    degraded_state: str | None = None
    intervention_id: str | None = None
    step: int | None = None
    seq: int | None = None
    approval_id: str | None = None
    tool_name: str | None = None
    risk_level: str | None = None
    question: str | None = None
    reason: str | None = None
    options: list[str] | None = None
    # Phase 3 — Proactive messages
    urgency: int | None = None
    intervention_type: str | None = None
    reasoning: str | None = None
    requires_approval: bool | None = None
    # Phase 3 — Ambient state
    state: str | None = None
    tooltip: str | None = None
