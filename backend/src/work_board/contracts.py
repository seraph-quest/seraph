"""Typed contracts for the authenticated operator work board.

The contracts deliberately describe operator intent and coordination only.
Execution authority stays in ``WorkflowRunState`` and the durable job runtime.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.db.models import WorkBoardStatus


class WorkBoardContractError(ValueError):
    """Raised when an input cannot be represented by the board contract."""


class WorkBoardAction(str, Enum):
    promote = "promote"
    block = "block"
    unblock = "unblock"
    archive = "archive"


class WorkBoardBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9_.:/-]{1,512}$")


def _safe_reference(value: str | None, *, field_name: str) -> str | None:
    """Keep identifiers and workspace references bounded and opaque."""
    if value is None:
        return None
    normalized = str(value).strip()
    if (
        not _SAFE_REFERENCE.fullmatch(normalized)
        or normalized.startswith(("/", "~"))
        or "\\" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError(f"{field_name} must be a bounded safe reference")
    return normalized


def _safe_opaque_identifier(value: str | None, *, field_name: str) -> str | None:
    """Validate an identifier that must never be interpreted as a path."""
    normalized = _safe_reference(value, field_name=field_name)
    if normalized is not None and "/" in normalized:
        raise ValueError(f"{field_name} must be an opaque identifier")
    return normalized


class WorkBoardTaskCreate(WorkBoardBaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=4_000)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    status: WorkBoardStatus = WorkBoardStatus.triage
    capability_id: str | None = Field(default=None, min_length=1, max_length=128)
    typed_input_ref: str | None = Field(default=None, min_length=1, max_length=512)
    typed_input_digest: str | None = Field(default=None, min_length=64, max_length=64)
    executor_id: str | None = Field(default=None, min_length=1, max_length=128)
    assignee_id: str | None = Field(default=None, min_length=1, max_length=128)
    priority: int = Field(default=50, ge=0, le=100)
    idempotency_scope: str = Field(default="task", min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    scheduled_at: datetime | None = None
    requires_review: bool = False
    reviewer_id: str | None = Field(default=None, min_length=1, max_length=128)
    origin_thread_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator(
        "capability_id",
        "executor_id",
        "assignee_id",
        "reviewer_id",
        "origin_thread_id",
    )
    @classmethod
    def validate_opaque_identifiers(cls, value: str | None, info) -> str | None:
        return _safe_opaque_identifier(value, field_name=str(info.field_name))

    @field_validator("typed_input_ref")
    @classmethod
    def validate_safe_references(cls, value: str | None, info) -> str | None:
        return _safe_reference(value, field_name=str(info.field_name))

    @field_validator("typed_input_digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("typed_input_digest must be a SHA-256 hexadecimal digest")
        return normalized

    @model_validator(mode="after")
    def validate_initial_status(self) -> "WorkBoardTaskCreate":
        if self.status not in {WorkBoardStatus.triage, WorkBoardStatus.todo}:
            raise ValueError("new tasks may start only in triage or todo")
        if self.status is WorkBoardStatus.todo:
            if not self.typed_input_ref or not self.typed_input_digest:
                raise ValueError("todo tasks require a typed input reference and digest")
        if self.typed_input_ref and not self.typed_input_digest:
            raise ValueError("typed_input_ref requires typed_input_digest")
        if self.typed_input_digest and not self.typed_input_ref:
            raise ValueError("typed_input_digest requires typed_input_ref")
        if self.requires_review and not self.reviewer_id:
            raise ValueError("requires_review tasks require a reviewer_id")
        return self


class WorkBoardTaskPatch(WorkBoardBaseModel):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=4_000)
    priority: int | None = Field(default=None, ge=0, le=100)
    capability_id: str | None = Field(default=None, min_length=1, max_length=128)
    typed_input_ref: str | None = Field(default=None, min_length=1, max_length=512)
    typed_input_digest: str | None = Field(default=None, min_length=64, max_length=64)
    executor_id: str | None = Field(default=None, min_length=1, max_length=128)
    assignee_id: str | None = Field(default=None, min_length=1, max_length=128)
    scheduled_at: datetime | None = None

    @field_validator("capability_id", "executor_id", "assignee_id")
    @classmethod
    def validate_opaque_identifiers(cls, value: str | None, info) -> str | None:
        return _safe_opaque_identifier(value, field_name=str(info.field_name))

    @field_validator("typed_input_ref")
    @classmethod
    def validate_safe_references(cls, value: str | None, info) -> str | None:
        return _safe_reference(value, field_name=str(info.field_name))

    @field_validator("typed_input_digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("typed_input_digest must be a SHA-256 hexadecimal digest")
        return normalized

    @model_validator(mode="after")
    def validate_typed_pair(self) -> "WorkBoardTaskPatch":
        fields = self.model_fields_set
        ref_set = "typed_input_ref" in fields
        digest_set = "typed_input_digest" in fields
        if ref_set != digest_set or (
            ref_set
            and digest_set
            and (self.typed_input_ref is None) != (self.typed_input_digest is None)
        ):
            raise ValueError("typed input reference and digest must be supplied together")
        return self


class WorkBoardActionRequest(WorkBoardBaseModel):
    action: WorkBoardAction
    expected_revision: int = Field(ge=1)
    block_kind: Literal["operator"] | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_action_fields(self) -> "WorkBoardActionRequest":
        if self.action is WorkBoardAction.block and not self.reason:
            raise ValueError("block action requires a reason")
        if self.action is not WorkBoardAction.block and (self.block_kind or self.reason):
            raise ValueError("block fields are valid only for block action")
        return self


class WorkBoardCommentCreate(WorkBoardBaseModel):
    expected_revision: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=2_000)


class WorkBoardLinkCreate(WorkBoardBaseModel):
    parent_task_id: str = Field(min_length=1, max_length=128)
    child_task_id: str = Field(min_length=1, max_length=128)
    expected_child_revision: int = Field(ge=1)


class WorkBoardLinkDelete(WorkBoardBaseModel):
    parent_task_id: str = Field(min_length=1, max_length=128)
    child_task_id: str = Field(min_length=1, max_length=128)
    expected_child_revision: int = Field(ge=1)


class WorkBoardOwner(BaseModel):
    """Server-derived identity; never accept this from a browser body."""

    model_config = ConfigDict(frozen=True)

    principal_id: str
    session_id: str


class WorkBoardEventPage(BaseModel):
    events: list[dict[str, Any]]
    last_event_id: int
    gap: bool


__all__ = [
    "WorkBoardAction",
    "WorkBoardActionRequest",
    "WorkBoardCommentCreate",
    "WorkBoardContractError",
    "WorkBoardEventPage",
    "WorkBoardLinkCreate",
    "WorkBoardLinkDelete",
    "WorkBoardOwner",
    "WorkBoardStatus",
    "WorkBoardTaskCreate",
    "WorkBoardTaskPatch",
]
