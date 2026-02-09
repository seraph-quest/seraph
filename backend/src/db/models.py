import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel, Relationship


# ─── Enums ────────────────────────────────────────────────

class GoalLevel(str, enum.Enum):
    vision = "vision"
    annual = "annual"
    quarterly = "quarterly"
    monthly = "monthly"
    weekly = "weekly"
    daily = "daily"


class GoalDomain(str, enum.Enum):
    productivity = "productivity"
    performance = "performance"
    health = "health"
    influence = "influence"
    growth = "growth"


class GoalStatus(str, enum.Enum):
    active = "active"
    completed = "completed"
    paused = "paused"
    abandoned = "abandoned"


class MemoryCategory(str, enum.Enum):
    fact = "fact"
    preference = "preference"
    pattern = "pattern"
    goal = "goal"
    reflection = "reflection"


# ─── Helper ──────────────────────────────────────────────

def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Session ─────────────────────────────────────────────

class Session(SQLModel, table=True):
    __tablename__ = "sessions"

    id: str = Field(default_factory=_uuid, primary_key=True)
    title: str = Field(default="New Conversation")
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    messages: list["Message"] = Relationship(back_populates="session")


# ─── Message ─────────────────────────────────────────────

class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: str = Field(default_factory=_uuid, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    role: str = Field(index=True)  # user | assistant | step | error
    content: str = Field(default="")
    metadata_json: Optional[str] = Field(default=None)
    step_number: Optional[int] = Field(default=None)
    tool_used: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)

    session: Optional[Session] = Relationship(back_populates="messages")


# ─── Memory ──────────────────────────────────────────────

class Memory(SQLModel, table=True):
    __tablename__ = "memories"

    id: str = Field(default_factory=_uuid, primary_key=True)
    content: str
    category: str = Field(default=MemoryCategory.fact)
    source_session_id: Optional[str] = Field(default=None)
    embedding_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


# ─── Goal ────────────────────────────────────────────────

class Goal(SQLModel, table=True):
    __tablename__ = "goals"

    id: str = Field(default_factory=_uuid, primary_key=True)
    parent_id: Optional[str] = Field(default=None, foreign_key="goals.id", index=True)
    path: str = Field(default="/")  # Materialized path e.g. /v1/a3/q7/
    level: str = Field(default=GoalLevel.daily)
    title: str
    description: Optional[str] = Field(default=None)
    status: str = Field(default=GoalStatus.active, index=True)
    domain: str = Field(default=GoalDomain.productivity, index=True)
    start_date: Optional[datetime] = Field(default=None)
    due_date: Optional[datetime] = Field(default=None)
    sort_order: int = Field(default=0)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ─── UserProfile ─────────────────────────────────────────

class UserProfile(SQLModel, table=True):
    __tablename__ = "user_profiles"

    id: str = Field(default="singleton", primary_key=True)
    name: str = Field(default="Traveler")
    soul_text: Optional[str] = Field(default=None)
    preferences_json: Optional[str] = Field(default=None)
    onboarding_completed: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
