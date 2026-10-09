"""Closed metadata-only Home wire. Inspector targets never grant authority."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", check_fields=False)
    @classmethod
    def bounded_source_scalars(cls, value, info):
        name = info.field_name
        if value is None:
            return value
        if name.endswith("_id"):
            from src.operator.home_cursor import source_id
            source_id(value, task=name in {"task_id", "attempt_id"})
            if name == "programme_id" and (len(value) != 32 or any(c not in "0123456789abcdef" for c in value)):
                raise ValueError("unsupported programme identity")
        if name.endswith("_revision") and not 1 <= value < 1 << 63:
            raise ValueError("unsupported revision")
        if name == "priority" and not 0 <= value <= 100:
            raise ValueError("unsupported priority")
        if name == "sort_order" and not -(1 << 63) <= value < 1 << 63:
            raise ValueError("unsupported ordering")
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("UTC timestamp required")
        return value


Access = Literal["current", "recovered_read_only"]
Reason = Literal["unknown_effect", "cost_liability", "needs_input", "approval_expired",
    "general_task_operator_paused", "attempt_limit", "owner_mismatch", "goal_not_active",
    "goal_revision_changed", "policy_blocked", "source_changed", "recovery_unknown",
    "programme_blocked", "programme_paused", "programme_revoked", "programme_review_due",
    "unsupported_source"]


class GoalTarget(Closed):
    kind: Literal["goal"] = "goal"
    goal_id: str
    goal_revision: int


class TaskTarget(Closed):
    kind: Literal["task"] = "task"
    task_id: str
    task_revision: int


class ProgrammeTarget(Closed):
    kind: Literal["programme"] = "programme"
    goal_id: str
    programme_id: str
    goal_revision: int


class ApprovalTarget(Closed):
    kind: Literal["approval"] = "approval"
    approval_id: str


class OutputTarget(Closed):
    kind: Literal["output"] = "output"
    task_id: str
    task_revision: int
    attempt_id: str


class MethodTarget(Closed):
    kind: Literal["method"] = "method"
    proposal_id: str
    version: str
    digest: str


class HistoricalMethod(Closed):
    status: Literal["admitted", "baseline", "unknown"]
    method_id: str | None
    version: str | None
    digest: str | None
    admitted_at: datetime | None
    lifecycle: Literal["active_metadata", "rolled_back_metadata", "suppressed_metadata", "unavailable", "unknown"]
    reason_code: Literal["method_projection_missing", "method_projection_invalid", "method_key_unavailable",
        "method_canonical_unavailable", "method_suppressed", "method_rolled_back"] | None
    target: MethodTarget | None


class Row(Closed):
    ownership_access: Access
    source_at: datetime


class ActiveGoal(Row):
    kind: Literal["active_goal"] = "active_goal"
    goal_id: str
    goal_revision: int
    status: Literal["active"] = "active"
    sort_order: int
    due_at: datetime | None
    target: GoalTarget


class Programme(Row):
    kind: Literal["programme"] = "programme"
    goal_id: str
    goal_revision: int
    programme_id: str
    grant_revision: int
    state: Literal["active", "blocked", "paused", "revoked", "review_due"]
    reason_code: Reason | None
    expires_at: datetime
    next_digest_at: datetime | None
    target: ProgrammeTarget


class TaskNextAction(Row):
    kind: Literal["task_next_action"] = "task_next_action"
    task_id: str
    task_revision: int
    goal_id: str
    goal_revision: int
    status: Literal["triage", "todo", "ready", "running"]
    priority: int
    scheduled_at: datetime | None
    action: Literal["inspect_task", "review_plan", "review_result", "recover_task"]
    method: HistoricalMethod | None
    target: TaskTarget


class PreparedOutput(Row):
    kind: Literal["prepared_output"] = "prepared_output"
    task_id: str
    task_revision: int
    attempt_id: str
    output_state: Literal["prepared", "blocked", "unknown"]
    method: HistoricalMethod | None
    target: OutputTarget


class Approval(Row):
    kind: Literal["approval"] = "approval"
    approval_id: str
    status: Literal["pending"] = "pending"
    expires_at: datetime | None
    target: ApprovalTarget


class BlockedTask(Row):
    kind: Literal["blocked_task"] = "blocked_task"
    task_id: str
    task_revision: int
    goal_id: str
    goal_revision: int
    reason_code: Reason
    target: TaskTarget


class BlockedApproval(Row):
    kind: Literal["blocked_approval"] = "blocked_approval"
    approval_id: str
    reason_code: Literal["approval_expired"] = "approval_expired"
    target: ApprovalTarget


class BlockedProgramme(Row):
    kind: Literal["blocked_programme"] = "blocked_programme"
    goal_id: str
    goal_revision: int
    programme_id: str
    grant_revision: int
    reason_code: Reason
    target: ProgrammeTarget


Item = Annotated[ActiveGoal | Programme | TaskNextAction | PreparedOutput | Approval |
    BlockedTask | BlockedApproval | BlockedProgramme, Field(discriminator="kind")]


class Section(Closed):
    items: list[Item] = Field(max_length=20)
    state: Literal["ready", "empty", "degraded", "blocked"]
    source_as_of: datetime | None


class HomeContinuation(Closed):
    active_goals: Section
    programme_status: Section
    task_next_actions: Section
    prepared_outputs: Section
    approvals: Section
    blocked_items: Section
    as_of: datetime

    @model_validator(mode="after")
    def bounded_sections(self):
        kinds = {"active_goals":{"active_goal"}, "programme_status":{"programme"},
            "task_next_actions":{"task_next_action"}, "prepared_outputs":{"prepared_output"},
            "approvals":{"approval"}, "blocked_items":{"blocked_task", "blocked_approval", "blocked_programme"}}
        if sum(len(getattr(self, name).items) for name in kinds) > 20:
            raise ValueError("aggregate item bound exceeded")
        for name, allowed in kinds.items():
            section = getattr(self, name)
            if any(item.kind not in allowed for item in section.items):
                raise ValueError("section source mismatch")
            if section.state == "empty" and section.items:
                raise ValueError("empty section contains rows")
        return self
