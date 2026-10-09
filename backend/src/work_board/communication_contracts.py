"""Closed communications values; references grant no execution authority."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from src.work_board.contracts import ClosedTaskModel, GeneralTaskNativeChildBindingV1, TaskProposalGroupV1, CommunicationSelection

TOOL_ID = "communication_prepare"
PLAN_MAX_BYTES = 65_536
CIPHERTEXT_MAX_BYTES = 98_304
METADATA_MAX_BYTES = 16_384
Digest = str


class CommunicationCreate(ClosedTaskModel):
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    selection: CommunicationSelection
    max_cost_microusd: int = Field(ge=0)
    wall_seconds: int = Field(default=900, ge=1, le=900)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class CommunicationPreparationMarker(ClosedTaskModel):
    schema_version: Literal["communication.source_preparation.v1"] = "communication.source_preparation.v1"
    binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    native_invocation_id: str = Field(min_length=1, max_length=256)
    parent_job_id: str = Field(min_length=1, max_length=256)
    group_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    ordinal: int = Field(ge=0, le=9)
    source_task_id: str = Field(min_length=1, max_length=128)
    source_attempt_id: str = Field(min_length=1, max_length=128)
    source_job_id: str = Field(min_length=1, max_length=256)


class CommunicationSourceRef(ClosedTaskModel):
    source_id: str = Field(min_length=1, max_length=256)
    capability_id: Literal["work.mail-reply-draft.v1", "calendar.meeting-prep.v1"]
    source_revision: str = Field(min_length=1, max_length=128)
    source_input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=256)
    artifact_path: str = Field(min_length=1, max_length=512)
    artifact_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    readback_id: str = Field(min_length=1, max_length=256)


class CommunicationReply(ClosedTaskModel):
    source_ref: CommunicationSourceRef
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    caveats: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("caveats")
    @classmethod
    def bounded_caveats(cls, values):
        if any(len(value) > 300 for value in values):
            raise ValueError("reply caveat exceeds source owner bound")
        return values


class CommunicationMeeting(ClosedTaskModel):
    source_ref: CommunicationSourceRef
    brief: dict[str, Any]

    @field_validator("brief")
    @classmethod
    def bounded_brief(cls, value):
        from src.work_board.general_task import canonical
        from src.integrations.google_calendar import MeetingPrepService
        value = MeetingPrepService.validate_model_output(value,
            event_key=value.get("event_key"), event_revision=value.get("event_revision"))
        if len(canonical(value)) > PLAN_MAX_BYTES:
            raise ValueError("meeting brief exceeds private plan bound")
        return value


class CommunicationReschedule(ClosedTaskModel):
    source_ref: CommunicationSourceRef
    input: dict[str, Any]

    @field_validator("input")
    @classmethod
    def original_grammar(cls, value):
        from src.work_board.dispatcher import CalendarRescheduleInput
        return CalendarRescheduleInput.model_validate(value).model_dump(mode="json")


class CommunicationQuestion(ClosedTaskModel):
    source_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    job_id: str | None = Field(default=None, max_length=256)
    recovery: Literal["review_source", "inspect_original", "review_capacity", "review_calendar_occupancy"]


class CommunicationPlan(ClosedTaskModel):
    source_refs: list[CommunicationSourceRef] = Field(default_factory=list, max_length=10)
    reply_drafts: list[CommunicationReply] = Field(default_factory=list, max_length=5)
    meeting_preparations: list[CommunicationMeeting] = Field(default_factory=list, max_length=5)
    reschedule_proposals: list[CommunicationReschedule] = Field(default_factory=list, max_length=3)
    unresolved_questions: list[CommunicationQuestion] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def exact_refs_and_size(self):
        from src.work_board.general_task import canonical
        refs = {ref.source_input_digest: ref for ref in self.source_refs}
        if len(refs) != len(self.source_refs):
            raise ValueError("duplicate preparation reference")
        for value in [*self.reply_drafts, *self.meeting_preparations, *self.reschedule_proposals]:
            if refs.get(value.source_ref.source_input_digest) != value.source_ref:
                raise ValueError("entry lacks its exact source reference")
        if len(canonical(self.model_dump(mode="json"))) > PLAN_MAX_BYTES:
            raise ValueError("complete private communication plan exceeds 65536 bytes")
        return self


class CommunicationAction(ClosedTaskModel):
    kind: Literal["reply", "reschedule"]
    source_input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_id: str = Field(min_length=1, max_length=256)


class ActionBundle(ClosedTaskModel):
    selected_actions: list[CommunicationAction] = Field(default_factory=list, max_length=8)
    exact_preview_digests: list[str] = Field(default_factory=list, max_length=8)
    approval_ids: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def independent_exact_actions(self):
        if len({len(self.selected_actions), len(self.exact_preview_digests), len(self.approval_ids)}) != 1:
            raise ValueError("each selected action requires its own exact preview and approval")
        if (len({action.operation_id for action in self.selected_actions}) != len(self.selected_actions)
            or len(set(self.approval_ids)) != len(self.approval_ids)):
            raise ValueError("selected operations and approvals must be independent")
        import re
        if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in self.exact_preview_digests):
            raise ValueError("exact preview digest required")
        if any(not value or len(value) > 256 for value in self.approval_ids):
            raise ValueError("bounded original approval identity required")
        return self


@dataclass(frozen=True)
class CommunicationPreparationBinding:
    """Private issuer result; canonical rows are rechecked in every writer."""
    native: GeneralTaskNativeChildBindingV1
    child_owner: str
    child_fence: int
    group: TaskProposalGroupV1
    ordinal: int
    capability_id: str
    source_choice_digest: str
    source_task_id: str
    source_attempt_id: str
    input_artifact_id: str
    input_artifact_digest: str
    source_job_id: str
    source_deadline_at: datetime
    budget_microusd: int
    _seal: object
    _physical_witness: object | None = None
