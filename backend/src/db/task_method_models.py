"""Canonical reviewed-method selection; no execution or accounting state."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field


class TaskMethodActive(SQLModel, table=True):
    __tablename__ = "task_method_active"
    __table_args__ = (UniqueConstraint("owner_identity_id", "goal_id", "goal_revision", "family",
        name="uq_task_method_active_scope"),)

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    owner_identity_id: str = Field(index=True)
    goal_id: str = Field(index=True)
    goal_revision: int
    family: str
    revision: int = Field(default=1)
    binding_json: str
    previous_binding_json: str = Field(default="null")
    baseline: bool = Field(default=False)
    signature_key_id: str
    signature_mac: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
