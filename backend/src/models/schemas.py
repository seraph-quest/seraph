from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message to the agent")
    session_id: str | None = Field(None, description="Session ID for conversation continuity")


class AgentStep(BaseModel):
    step: int
    type: str
    content: str


class ChatResponse(BaseModel):
    response: str
    session_id: str
    steps: list[AgentStep] = Field(default_factory=list)


class WSMessage(BaseModel):
    type: str = Field("message", description="Message type: message | ping | skip_onboarding")
    message: str = Field("", description="User message")
    session_id: str | None = None


class WSResponse(BaseModel):
    type: str = Field(..., description="Response type: step | final | error | pong | proactive | ambient")
    content: str = ""
    session_id: str = ""
    step: int | None = None
    # Phase 3 — Proactive messages
    urgency: int | None = None
    intervention_type: str | None = None
    reasoning: str | None = None
    # Phase 3 — Ambient state
    state: str | None = None
    tooltip: str | None = None
