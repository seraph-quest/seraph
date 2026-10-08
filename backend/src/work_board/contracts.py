"""Typed contracts for the authenticated operator work board.

The contracts deliberately describe operator intent and coordination only.
Execution authority stays in ``WorkflowRunState`` and the durable job runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import re
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.db.models import WorkBoardStatus


class WorkBoardContractError(ValueError):
    """Raised when an input cannot be represented by the board contract."""


class ClosedTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TaskLimits(ClosedTaskModel):
    max_steps: int = Field(default=16, ge=1, le=16)
    max_inference_calls: int = Field(default=12, ge=0, le=12)
    wall_seconds: int = Field(default=900, ge=1, le=900)
    depth: Literal[0] = 0
    max_outstanding_children: int = Field(default=2, ge=0, le=2)
    max_cost_microusd: int = Field(default=0, ge=0)


class GeneralTaskInput(ClosedTaskModel):
    schema_version: Literal[1] = 1
    goal_ref: str = Field(min_length=1, max_length=128)
    intent: str = Field(min_length=1, max_length=8192)
    evidence_refs: list[str] = Field(default_factory=list, max_length=12)
    requested_output: dict[str, Any]
    limits: TaskLimits = Field(default_factory=TaskLimits)
    tool_set_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    inference_egress_acknowledged: bool = False

    @field_validator("intent")
    @classmethod
    def bounded_intent(cls, value):
        if len(value.encode("utf-8")) > 8192:
            raise ValueError("intent exceeds 8192 UTF-8 bytes")
        return value

    @field_validator("goal_ref")
    @classmethod
    def safe_goal(cls, value):
        return _safe_reference(value, field_name="goal_ref")

    @field_validator("evidence_refs")
    @classmethod
    def safe_evidence(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("duplicate evidence references")
        return [_safe_reference(item, field_name="evidence_refs") for item in value]


class DependencyPointer(ClosedTaskModel):
    """A data reference into a verified predecessor, never an expression."""
    step_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    pointer: str = Field(max_length=512, pattern=r"^(?:/(?:[^~]|~[01])*)*$")


class PlanStep(ClosedTaskModel):
    step_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    tool_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    input: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list, max_length=15)
    output_contract: dict[str, Any]


class PlanSpec(ClosedTaskModel):
    schema_version: Literal[1] = 1
    revision: int = Field(ge=1, le=16)
    steps: list[PlanStep] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def acyclic(self):
        by_id = {step.step_id: step for step in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError("duplicate step identity")
        visiting, done = set(), set()
        def visit(identity):
            if identity in visiting:
                raise ValueError("plan dependency cycle")
            if identity in done:
                return
            if identity not in by_id:
                raise ValueError("unknown dependency")
            visiting.add(identity)
            dependencies = by_id[identity].depends_on
            if len(set(dependencies)) != len(dependencies):
                raise ValueError("duplicate dependency")
            for dependency in dependencies:
                visit(dependency)
            visiting.remove(identity)
            done.add(identity)
        for identity in by_id:
            visit(identity)
        return self


class ToolDescriptor(ClosedTaskModel):
    tool_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    version: str = Field(min_length=1, max_length=128)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effects: list[str] = Field(min_length=1, max_length=16)
    permissions: list[str] = Field(min_length=1, max_length=16)
    credential_refs: list[str] = Field(default_factory=list, max_length=16)
    deadline: int = Field(ge=1, le=900)
    verifier: str = Field(min_length=1, max_length=128)
    server_id: str | None = Field(default=None, max_length=128)
    connection_revision: int | None = Field(default=None, ge=1)
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def complete(self):
        if any(effect in {"unknown", "", "shell"} for effect in self.effects):
            raise ValueError("unknown or arbitrary shell effects are excluded")
        if self.server_id and self.connection_revision is None:
            raise ValueError("MCP descriptors require a connection revision")
        return self


class TaskStrategyBinding(ClosedTaskModel):
    schema_version: Literal[1] = 1
    status: Literal["none", "active", "blocked"]
    method_id: str | None = None
    version: str | None = None
    digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    typed_data: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def binding_complete(self):
        if self.status == "active" and not all((self.method_id, self.version, self.digest, self.typed_data is not None)):
            raise ValueError("active strategy requires an exact typed binding")
        if self.status == "blocked" and not self.reason:
            raise ValueError("blocked strategy requires a reason")
        if self.status == "none" and any(item is not None for item in (self.method_id, self.version, self.digest, self.typed_data)):
            raise ValueError("baseline strategy carries no method authority")
        return self


class StrategyResolver(Protocol):
    def resolve(self, owner: "WorkBoardOwner", goal_ref: str, task_family: str,
                programme_grant: Any | None = None) -> TaskStrategyBinding: ...


class GeneralTaskCreate(ClosedTaskModel):
    goal_revision: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    input: GeneralTaskInput
    plan: PlanSpec | None = None
    accept: bool = False
    expected_plan_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def exact_revision(self):
        if self.plan is None:
            if self.accept or self.expected_plan_revision is not None:
                raise ValueError("generated plans require review before acceptance")
            return self
        if self.expected_plan_revision != self.plan.revision:
            raise ValueError("plan revision changed")
        if len(self.plan.steps) > self.input.limits.max_steps:
            raise ValueError("plan exceeds task step limit")
        return self


class TaskProposalGroupV1(ClosedTaskModel):
    """Server-owned original allowance; no browser request accepts this type."""
    schema_version: Literal["general_task.proposal_group.v1"] = "general_task.proposal_group.v1"
    group_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    owner_principal_id: str = Field(min_length=1, max_length=128)
    owner_session_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    creation_request_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    initial_input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    planning_snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    intent_egress_ack_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    limits_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_inference_calls: int = Field(ge=0, le=12)
    max_cost_microusd: int = Field(ge=0)
    max_steps: int = Field(ge=1, le=16)
    issued_at: datetime
    original_deadline_at: datetime

    @field_validator("issued_at", "original_deadline_at", mode="before")
    @classmethod
    def utc_timestamp(cls, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("original proposal timestamps require UTC")
        return value

    @model_validator(mode="after")
    def original_clock(self):
        if self.original_deadline_at <= self.issued_at:
            raise ValueError("original task deadline must follow issuance")
        import json
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > 4096:
            raise ValueError("proposal group exceeds its bounded envelope")
        return self


class TaskProposalProvenanceV1(ClosedTaskModel):
    schema_version: Literal["general_task.proposal_provenance.v1"] = "general_task.proposal_provenance.v1"
    group_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    group_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_operation_id: str = Field(min_length=1, max_length=256)
    initial_inference_job_id: str = Field(min_length=1, max_length=256)
    initial_payload_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    deployment_id: str = Field(min_length=1, max_length=128)
    settings_revision: int = Field(ge=1)
    reservation_sequence: int = Field(ge=1)
    reservation_binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_deadline_at: datetime

    _utc_timestamp = field_validator("original_deadline_at", mode="before")(TaskProposalGroupV1.utc_timestamp.__func__)


class PlanRevisionRequest(ClosedTaskModel):
    expected_revision: int = Field(ge=1)
    replacements: list[PlanStep] = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class StepReceipt(ClosedTaskModel):
    step_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    plan_revision: int = Field(ge=1, le=16)
    invocation_id: str = Field(min_length=1, max_length=256)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    contact_state: Literal["not_contacted", "contact_started", "contact_denied", "unknown", "settled"]
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    status: Literal["admitted", "running", "awaiting_approval", "verified", "failed", "blocked", "cancelled", "unknown"]


TaskIdentity = Annotated[str, Field(min_length=1, max_length=128)]
TaskDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
NativeInvocationIdentity = Annotated[str, Field(min_length=1, max_length=256)]
TaskManifestPhase = Literal["native_ready", "native_wait", "assembly", "operator_paused", "approval_wait", "cancelled", "unknown_recovery", "complete"]
GENERAL_TASK_NATIVE_CHILD_KIND = "general_task_native_tool_v1"
GENERAL_TASK_NATIVE_CHILD_CAPABILITY = "agent.native-tool-step.v1"
GENERAL_TASK_MANIFEST_KEY = "general-task:current-manifest:v1"


class GeneralTaskArtifactRef(ClosedTaskModel):
    artifact_id: TaskIdentity
    digest: TaskDigest
    schema_version: Literal["GeneralTaskEnvelope.v1", "GeneralTaskPlanRevision.v1", "StepReceipt.v1", "GeneralTaskOutput.v1", "GeneralTaskToolInput.v1"]


class GeneralTaskStepReceiptV1(StepReceipt):
    schema_version: Literal["StepReceipt.v1"] = "StepReceipt.v1"
    descriptor_digest: TaskDigest
    selected_grant_digest: TaskDigest
    task_id: TaskIdentity
    attempt_id: TaskIdentity
    child_job_id: NativeInvocationIdentity
    child_attempt_count: int = Field(ge=1)
    child_fence: int = Field(ge=1)
    parent_creation_digest: TaskDigest
    phase_digest: TaskDigest
    artifact_refs: list[GeneralTaskArtifactRef] = Field(default_factory=list, max_length=16)
    approval_id: TaskIdentity | None = None
    approval_binding_digest: TaskDigest | None = None
    effect_receipt_digest: TaskDigest | None = None
    cleanup_receipt_digest: TaskDigest | None = None
    no_learning: Literal[True] = True


class GeneralTaskCurrentManifestV1(ClosedTaskModel):
    schema_version: Literal["general_task.current_manifest.v1"] = "general_task.current_manifest.v1"
    task_id: TaskIdentity
    original_root_id: TaskIdentity
    owner_principal_id: TaskIdentity
    attempt_id: TaskIdentity
    run_id: NativeInvocationIdentity
    task_revision: int = Field(ge=1)
    manifest_revision: int = Field(ge=1)
    board_fence: int = Field(ge=1)
    job_fence: int = Field(ge=1)
    original_envelope_artifact_id: TaskIdentity
    original_envelope_digest: TaskDigest
    original_input_digest: TaskDigest
    selected_grant_digest: TaskDigest
    group_id: TaskDigest
    group_digest: TaskDigest
    original_limits_digest: TaskDigest
    creation_digest: TaskDigest
    original_deadline_at: datetime
    phase: TaskManifestPhase
    native_deadline_at: datetime
    phase_revision: int = Field(ge=1)
    phase_digest: TaskDigest
    plan_revision: int = Field(ge=1, le=16)
    current_plan_artifact_id: TaskIdentity
    current_plan_digest: TaskDigest
    revision_numbers: list[int] = Field(min_length=1, max_length=16)
    revision_artifact_ids: list[TaskIdentity] = Field(min_length=1, max_length=16)
    revision_artifact_digests: list[TaskDigest] = Field(min_length=1, max_length=16)
    revision_artifact_schemas: list[Literal["GeneralTaskEnvelope.v1", "GeneralTaskPlanRevision.v1"]] = Field(min_length=1, max_length=16)
    step_ids: list[TaskIdentity] = Field(default_factory=list, max_length=16)
    step_receipt_artifact_ids: list[TaskIdentity] = Field(default_factory=list, max_length=16)
    step_receipt_digests: list[TaskDigest] = Field(default_factory=list, max_length=16)
    step_receipt_schemas: list[Literal["StepReceipt.v1"]] = Field(default_factory=list, max_length=16)
    admitted_invocation_ids: list[NativeInvocationIdentity] = Field(default_factory=list, max_length=16)
    required_checkpoint_ids: list[TaskIdentity] = Field(default_factory=list, max_length=50)
    no_learning: Literal[True] = True

    _utc_timestamp = field_validator("original_deadline_at", "native_deadline_at", mode="before")(TaskProposalGroupV1.utc_timestamp.__func__)

    @model_validator(mode="after")
    def finite_aligned_references(self):
        if self.native_deadline_at > self.original_deadline_at:
            raise ValueError("native deadline cannot extend the original proposal cutoff")
        if self.revision_numbers != list(range(1, self.plan_revision + 1)):
            raise ValueError("immutable revisions must be contiguous and end at current plan")
        if len({len(self.revision_numbers), len(self.revision_artifact_ids), len(self.revision_artifact_digests), len(self.revision_artifact_schemas)}) != 1:
            raise ValueError("revision references must be aligned")
        if len({len(self.step_ids), len(self.step_receipt_artifact_ids), len(self.step_receipt_digests), len(self.step_receipt_schemas)}) != 1:
            raise ValueError("step receipt references must be aligned")
        for values in (self.step_ids, self.admitted_invocation_ids, self.required_checkpoint_ids):
            if len(set(values)) != len(values):
                raise ValueError("manifest references must be unique")
        import json
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > 65536:
            raise ValueError("current manifest exceeds 64 KiB")
        return self


class GeneralTaskNativeChildBindingV1(ClosedTaskModel):
    schema_version: Literal["general_task.native_child.v1"] = "general_task.native_child.v1"
    parent_job_id: NativeInvocationIdentity
    task_id: TaskIdentity
    attempt_id: TaskIdentity
    original_root_id: TaskIdentity
    owner_principal_id: TaskIdentity
    goal_id: TaskIdentity
    goal_revision: int = Field(ge=1)
    original_deadline_at: datetime
    native_deadline_at: datetime
    original_envelope_digest: TaskDigest
    parent_authority_digest: TaskDigest
    creation_digest: TaskDigest
    creation_job_fence: int = Field(ge=1)
    creation_board_fence: int = Field(ge=1)
    plan_revision: int = Field(ge=1, le=16)
    plan_digest: TaskDigest
    step_id: TaskIdentity
    invocation_id: NativeInvocationIdentity
    input_digest: TaskDigest
    descriptor_digest: TaskDigest
    selected_grant_digest: TaskDigest
    phase_revision: int = Field(ge=1)
    phase_digest: TaskDigest
    live_root_digest: TaskDigest

    _utc_timestamp = field_validator("original_deadline_at", "native_deadline_at", mode="before")(TaskProposalGroupV1.utc_timestamp.__func__)

    @model_validator(mode="after")
    def native_cutoff(self):
        if self.native_deadline_at > self.original_deadline_at:
            raise ValueError("native deadline cannot extend the original proposal cutoff")
        return self


class GeneralTaskPlanRevisionV1(ClosedTaskModel):
    schema_version: Literal["GeneralTaskPlanRevision.v1"] = "GeneralTaskPlanRevision.v1"
    parent_job_id: NativeInvocationIdentity
    creation_digest: TaskDigest
    original_envelope_digest: TaskDigest
    selected_grant_digest: TaskDigest
    original_limits_digest: TaskDigest
    original_deadline_at: datetime
    plan: PlanSpec
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    no_learning: Literal[True] = True

    _utc_timestamp = field_validator("original_deadline_at", mode="before")(TaskProposalGroupV1.utc_timestamp.__func__)


class GeneralTaskToolInputV1(ClosedTaskModel):
    schema_version: Literal["GeneralTaskToolInput.v1"] = "GeneralTaskToolInput.v1"
    parent_job_id: NativeInvocationIdentity
    creation_digest: TaskDigest
    invocation_id: NativeInvocationIdentity
    tool_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    descriptor_digest: TaskDigest
    input_digest: TaskDigest
    inputs: dict[str, Any]
    no_learning: Literal[True] = True

    @model_validator(mode="after")
    def exact_local_data(self):
        from src.work_board.general_task import canonical, digest, validate_data
        validate_data(self.inputs, dependencies=set())
        if digest(self.inputs) != self.input_digest:
            raise ValueError("native input digest does not match exact resolved literals")
        canonical(self.model_dump(mode="json"))
        return self


class GeneralTaskEnvelope(ClosedTaskModel):
    """Single immutable artifact holding intent and the accepted inert plan."""
    schema_version: Literal[1] = 1
    task_input: GeneralTaskInput
    plan: PlanSpec | None = None
    proposal_error: str | None = Field(default=None, max_length=128)
    descriptors: list[ToolDescriptor] = Field(default_factory=list, max_length=16)
    strategy: TaskStrategyBinding
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    proposal_group: "TaskProposalGroupV1 | None" = None
    proposal_provenance: "TaskProposalProvenanceV1 | None" = None

    @model_validator(mode="after")
    def immutable_snapshot(self):
        if not self.task_input.tool_set_digest:
            raise ValueError("persisted plans require an exact tool snapshot")
        if self.plan is None and not self.proposal_error:
            raise ValueError("an incomplete proposal requires a visible reason")
        if self.plan is not None and (not self.descriptors or self.proposal_error):
            raise ValueError("valid plans require registered descriptors without proposal errors")
        if self.proposal_provenance is not None:
            if (self.proposal_group is None
                or self.proposal_provenance.group_id != self.proposal_group.group_id
                or self.proposal_provenance.original_deadline_at != self.proposal_group.original_deadline_at):
                raise ValueError("proposal provenance requires its exact original group")
        if self.proposal_group is not None:
            limits = self.task_input.limits
            if (self.proposal_group.goal_id != self.task_input.goal_ref
                or self.proposal_group.max_inference_calls != limits.max_inference_calls
                or self.proposal_group.max_cost_microusd != limits.max_cost_microusd
                or self.proposal_group.max_steps != limits.max_steps):
                raise ValueError("proposal allowance cannot change")
        return self


class GeneralTaskPlanUpdate(ClosedTaskModel):
    expected_revision: int = Field(ge=1)
    expected_plan_revision: int = Field(ge=0)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    plan: PlanSpec

    @model_validator(mode="after")
    def next_plan(self):
        if self.plan.revision != self.expected_plan_revision + 1:
            raise ValueError("edited plan must advance exactly one revision")
        return self


class GeneralTaskResume(ClosedTaskModel):
    expected_revision: int = Field(ge=1)
    expected_plan_revision: int = Field(ge=1)
    workflow_run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=128)
    fencing_token: int = Field(ge=1)
    workflow_revision: int = Field(ge=1)
    approval_id: str = Field(min_length=1, max_length=128)


class WorkBoardAction(str, Enum):
    promote = "promote"
    block = "block"
    unblock = "unblock"
    retry = "retry"
    cancel = "cancel"
    archive = "archive"
    request_review = "request_review"
    request_changes = "request_changes"
    complete_review = "complete_review"
    renew_review = "renew_review"


# ``operator`` is retained for the M1 operator-correction path.  The other
# public kinds are the bounded M4 recovery categories.  Workflow/effect
# reconciliation kinds (for example ``unknown_effect``) remain representable
# in projections, but are never accepted by the generic authenticated action
# endpoint; only the authoritative reconciliation paths may create them.
WORK_BOARD_BLOCK_KINDS = frozenset(
    {
        "operator",
        "dependency",
        "needs_input",
        "capability",
        "transient",
        "cancelled",
        "review_expired",
        "unknown_effect",
    }
)
WORK_BOARD_AUTHENTICATED_BLOCK_KINDS = frozenset(
    {
        "operator",
        "dependency",
        "needs_input",
        "capability",
        "transient",
        "cancelled",
    }
)


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


def _normalize_schedule_utc(value: datetime | None) -> datetime | None:
    """Normalize schedules before SQLite drops timezone offsets on bind."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class WorkBoardTaskCreate(WorkBoardBaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=4_000)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    status: WorkBoardStatus = WorkBoardStatus.triage
    capability_id: str | None = Field(default=None, min_length=1, max_length=128)
    input_artifact_id: str | None = Field(default=None, min_length=1, max_length=512)
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

    @model_validator(mode="before")
    @classmethod
    def validate_near_canonical_uuid(cls,value):
        if isinstance(value,dict) and str(value.get('capability_id','')).strip()=='inference.near-text.v1':
            from uuid import UUID
            key=value.get('idempotency_key')
            try:
                if not isinstance(key,str) or str(UUID(key))!=key:
                    raise ValueError('near_idempotency_invalid: canonical UUID required')
            except (TypeError,ValueError,AttributeError):
                raise ValueError('near_idempotency_invalid: canonical UUID required') from None
        return value

    @field_validator("scheduled_at")
    @classmethod
    def normalize_scheduled_at(cls, value: datetime | None) -> datetime | None:
        return _normalize_schedule_utc(value)

    @field_validator(
        "capability_id",
        "input_artifact_id",
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
            if not self.capability_id:
                raise ValueError("todo tasks require a registered capability")
            if self.input_artifact_id:
                if self.typed_input_ref or self.typed_input_digest:
                    raise ValueError("input_artifact_id cannot be combined with a typed input reference or digest")
            elif not self.typed_input_ref or not self.typed_input_digest:
                raise ValueError("todo tasks require a typed input reference and digest")
        elif self.input_artifact_id and self.capability_id != "agent.task.v1":
            raise ValueError("input_artifact_id is accepted only for executable todo tasks")
        if self.input_artifact_id and (self.typed_input_ref or self.typed_input_digest):
            raise ValueError("input_artifact_id cannot be combined with a typed input reference or digest")
        if self.typed_input_ref and not self.typed_input_digest:
            raise ValueError("typed_input_ref requires typed_input_digest")
        if self.typed_input_digest and not self.typed_input_ref:
            raise ValueError("typed_input_digest requires typed_input_ref")
        return self


class WorkBoardInputArtifactCreate(BaseModel):
    """Strict owner-bound typed-input reservation request."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    schema_version: Literal[1]
    capability_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(gt=0)
    input: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="before")
    @classmethod
    def validate_near_canonical_uuid(cls,value):
        if isinstance(value,dict) and str(value.get('capability_id','')).strip()=='inference.near-text.v1':
            from uuid import UUID
            key=value.get('idempotency_key')
            try:
                if not isinstance(key,str) or str(UUID(key))!=key:
                    raise ValueError('near_idempotency_invalid: canonical UUID required')
            except (TypeError,ValueError,AttributeError):
                raise ValueError('near_idempotency_invalid: canonical UUID required') from None
        return value

    @field_validator("capability_id", "idempotency_key")
    @classmethod
    def validate_opaque_fields(cls, value: str, info) -> str:
        return _safe_opaque_identifier(value, field_name=str(info.field_name)) or ""

    @field_validator("goal_id")
    @classmethod
    def validate_goal_id(cls, value: str) -> str:
        return _safe_reference(value, field_name="goal_id") or ""


class WorkBoardInputArtifactMetadata(WorkBoardBaseModel):
    """Safe metadata returned for an owner-bound typed input artifact."""

    artifact_id: str
    typed_input_ref: str
    typed_input_digest: str
    capability_id: str
    goal_id: str
    goal_revision: int
    expires_at: datetime
    state: str | None = None
    size_bytes: int | None = None
    bound_task_id: str | None = None
    bound_task_revision: int | None = None
    revision: int | None = None


class WorkBoardInputArtifactDelete(WorkBoardBaseModel):
    expected_revision: int = Field(ge=1)


# Backward-compatible descriptive aliases for API/test callers that use the
# shorter ``Response``/``DeleteRequest`` vocabulary.
WorkBoardInputArtifactResponse = WorkBoardInputArtifactMetadata
WorkBoardInputArtifactDeleteRequest = WorkBoardInputArtifactDelete


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

    @field_validator("scheduled_at")
    @classmethod
    def normalize_scheduled_at(cls, value: datetime | None) -> datetime | None:
        return _normalize_schedule_utc(value)

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
    block_kind: Literal[
        "operator",
        "dependency",
        "needs_input",
        "capability",
        "transient",
        "cancelled",
        "review_expired",
        "unknown_effect",
    ] | None = None
    source_status: WorkBoardStatus | None = None
    attempt_id: str | None = Field(default=None, min_length=1, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    resolution: str | None = Field(default=None, min_length=1, max_length=1_000)

    @field_validator("attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str | None) -> str | None:
        return _safe_opaque_identifier(value, field_name="attempt_id")

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, values: list[str]) -> list[str]:
        return [
            _safe_reference(value, field_name="evidence_refs") or ""
            for value in values
        ]

    @model_validator(mode="after")
    def validate_action_fields(self) -> "WorkBoardActionRequest":
        if self.action is WorkBoardAction.block:
            if not self.block_kind:
                raise ValueError("block action requires a typed block_kind")
            if not self.reason:
                raise ValueError("block action requires a reason")
            if self.source_status is None:
                raise ValueError("block action requires the current source_status")
        if self.action is not WorkBoardAction.block and self.source_status is not None:
            raise ValueError("source_status is valid only for block action")
        if self.action is WorkBoardAction.unblock and not self.resolution:
            raise ValueError("unblock action requires an explicit resolution")
        if self.action is WorkBoardAction.request_review:
            if not self.attempt_id:
                raise ValueError("request_review requires attempt_id")
        if self.action is WorkBoardAction.request_changes and not self.reason:
            raise ValueError("request_changes requires a reason")
        if self.action is WorkBoardAction.complete_review and not self.attempt_id:
            raise ValueError("complete_review requires attempt_id")
        if self.action is WorkBoardAction.block and self.attempt_id:
            raise ValueError("attempt_id is valid only for review actions")
        if self.action not in {
            WorkBoardAction.block,
            WorkBoardAction.request_changes,
        } and self.block_kind:
            raise ValueError("block fields are valid only for block action")
        if self.action not in {
            WorkBoardAction.block,
            WorkBoardAction.request_changes,
        } and self.reason:
            raise ValueError("reason is valid only for block or request_changes action")
        if self.action is not WorkBoardAction.unblock and self.resolution:
            raise ValueError("resolution is valid only for unblock action")
        if self.action not in {
            WorkBoardAction.request_review,
            WorkBoardAction.complete_review,
        } and self.attempt_id:
            raise ValueError("attempt_id is valid only for review actions")
        if self.action is not WorkBoardAction.request_review and self.evidence_refs:
            raise ValueError("evidence_refs are valid only for request_review")
        return self


class WorkBoardProposalRequest(WorkBoardBaseModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        return _safe_opaque_identifier(value, field_name="idempotency_key") or ""


class WorkBoardSpecificationEvidenceReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_packet_revision: int = Field(ge=1)
    expected_packet_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_execution_use: bool

    @field_validator("acknowledge_execution_use")
    @classmethod
    def require_explicit_execution_use(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Explicit execution-use acknowledgment is required")
        return value


class WorkBoardProposalAccept(WorkBoardBaseModel):
    expected_proposal_revision: int = Field(ge=1)
    expected_parent_revision: int = Field(ge=1)
    execution_replacement: WorkBoardSpecificationEvidenceReplacement | None = None


class WorkBoardProposalReject(WorkBoardBaseModel):
    expected_proposal_revision: int = Field(ge=1)


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


class WorkBoardRoutinePublicationPrepareRequest(WorkBoardBaseModel):
    """Operator supplied publication text for one canonical routine task.

    The route derives the routine, parent run, destination, approval, and
    current authority from the task and attempt.  Callers may provide only the
    bounded text that will appear in the exact M3 preview.
    """

    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=160)
    body: str = Field(min_length=1, max_length=32_000)


class WorkBoardRoutinePublicationRecoverRequest(WorkBoardBaseModel):
    """Resume the exact prepared publication after separate approval."""

    expected_revision: int = Field(ge=1)


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
    "WorkBoardInputArtifactCreate",
    "WorkBoardInputArtifactDelete",
    "WorkBoardInputArtifactDeleteRequest",
    "WorkBoardInputArtifactMetadata",
    "WorkBoardInputArtifactResponse",
    "WorkBoardLinkCreate",
    "WorkBoardLinkDelete",
    "WorkBoardOwner",
    "WorkBoardProposalAccept",
    "WorkBoardProposalReject",
    "WorkBoardProposalRequest",
    "WorkBoardRoutinePublicationPrepareRequest",
    "WorkBoardRoutinePublicationRecoverRequest",
    "WorkBoardStatus",
    "WorkBoardTaskCreate",
    "WorkBoardTaskPatch",
]
