"""Bounded ``goal-snapshot-to-file`` execution through the governed runtime.

The goal-conditioned loop remains the decision and receipt boundary.  This
module only adapts its one capability to the existing workflow tool surface and
the canonical durable job repository.  It deliberately has no model, queue,
authorization database, or memory-learning implementation of its own.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config.settings import settings
from src.approval.runtime import (
    get_current_approval_mode,
    get_current_trust_principal,
    reset_runtime_context,
    set_runtime_context,
)
from src.db.models import Goal
from src.goals.contracts import (
    GoalCandidateAction,
    GoalCandidateDecision,
    GoalCandidateRequest,
    GoalExecutionResult,
    GoalOutcomeReceipt,
    StrategyDeltaProvenance,
    normalized_evidence_refs,
    stable_candidate_key,
)
from src.goals.repository import goal_repository
from src.guardian.goal_conditioned_loop import (
    build_goal_candidate_decision,
    dispatch_goal_candidate,
)
from src.native_tools.registry import canonical_tool_name
from src.security.authority_envelope import (
    CAPABILITY_POLICY_SCHEMA_VERSION,
    CapabilityDecision,
    CapabilityEnvelope,
    CapabilityPolicy,
    CapabilityScope,
    GlobalCapabilityPolicy,
    ResourceLimits,
    authorize_capability,
    approval_binding_digest,
    issue_capability_authority,
)
from src.security.trust_contract import (
    ApprovalBinding,
    AuthorityGrant,
    EgressClass,
    PrincipalType,
    TrustPrincipal,
)
from src.workflows.job_runtime import (
    DurableJobIdempotencyConflict,
    DurableJobIdentity,
    DurableJobSpec,
    durable_job_repository,
)
from src.workspace import canonical_workspace_root


CAPABILITY_ID = "workflow.goal-snapshot-to-file"
CAPABILITY_VERSION = "1"
WORKFLOW_NAME = "goal-snapshot-to-file"
WORKFLOW_TOOL_NAME = "workflow_goal_snapshot_to_file"
_ALLOWED_WORKFLOW_STEP_SEQUENCE = ("get_goals", "write_file")
_CANONICAL_WORKFLOW_STEP_DEFINITIONS = (
    {
        "id": "goals",
        "tool": "get_goals",
        "arguments": {},
    },
    {
        "id": "save",
        "tool": "write_file",
        "arguments": {
            "file_path": "{{ file_path }}",
            "content": "Goal snapshot\n\n{{ steps.goals.result }}\n",
        },
    },
)
DEFAULT_PRIORITY = 60
MAX_PRIORITY = 100
DEFAULT_DEADLINE_SECONDS = 300
MAX_DEADLINE_SECONDS = 900
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_RECONCILIATION_ACTION = "reconcile the durable job and any workspace effect before retry or cancel"
_AUTHORITY_SOURCE_ID = "source:goal-snapshot-to-file"
_AUTHORITY_MEMORY_BYTES = 256 * 1024 * 1024
_AUTHORITY_PID_COUNT = 4
_AUTHORITY_CPU_SECONDS = 60.0
_AUTHORITY_POLICY_DEADLINE_SECONDS = DEFAULT_DEADLINE_SECONDS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _safe_digest(value: Any) -> str:
    import json

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def normalize_workspace_relative_path(value: Any) -> str:
    """Accept only a normalized, relative POSIX path.

    Rejecting ``.``/``..`` and Windows drive forms before joining the canonical
    workspace root prevents traversal through either POSIX or Windows-shaped
    input.  Existing symlink components are rejected separately at readback.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("file_path must be a non-empty workspace-relative path")
    raw = value.strip()
    normalized_input = raw.replace("\\", "/")
    windows_path = PureWindowsPath(raw)
    if (
        "\x00" in raw
        or "\\" in raw
        or raw.startswith("~")
        or raw.startswith("/")
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ":" in raw
    ):
        raise ValueError("file_path must be workspace-relative")
    parts = PurePosixPath(normalized_input).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("file_path must not contain traversal or empty path parts")
    normalized = "/".join(parts)
    if normalized != normalized_input:
        raise ValueError("file_path must use normalized POSIX separators")
    return normalized


class GoalSnapshotToFileRequest(BaseModel):
    """Typed admission input for this one capability.

    A request is owned by a service principal and must carry the goal revision
    observed by the caller.  The durable repository binds those fields into its
    idempotency record; the adapter re-reads the goal immediately before the
    governed workflow call.
    """

    model_config = ConfigDict(extra="forbid")

    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    file_path: str = Field(min_length=1, max_length=512)
    owner_principal_id: str = Field(min_length=1, max_length=160)
    service_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    parent_job_id: str | None = Field(default=None, min_length=1, max_length=160)
    parent_fencing_token: int | None = Field(default=None, ge=1)
    capability_version: Literal[CAPABILITY_VERSION] = CAPABILITY_VERSION
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(default="goal_snapshot_requested", max_length=1_000)
    expected_outcome: str = Field(default="", max_length=1_000)
    priority: int = Field(default=DEFAULT_PRIORITY, ge=0, le=MAX_PRIORITY)
    deadline_at: datetime = Field(
        default_factory=lambda: _now() + timedelta(seconds=DEFAULT_DEADLINE_SECONDS)
    )
    cancel_requested: bool = False

    @field_validator("goal_id", "owner_principal_id", "service_id", "session_id", "capability_version", "reason", "expected_outcome", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return _text(value)

    @field_validator("file_path", mode="before")
    @classmethod
    def _normalize_file_path(cls, value: Any) -> str:
        return normalize_workspace_relative_path(value)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _normalize_evidence_refs(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set)):
            raise TypeError("evidence_refs must be a list of strings")
        return list(normalized_evidence_refs(*[str(item) for item in value]))

    @field_validator("deadline_at", mode="after")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deadline_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_service_and_deadline(self) -> "GoalSnapshotToFileRequest":
        if not self.owner_principal_id.startswith("service:") or not self.owner_principal_id[8:]:
            raise ValueError("owner_principal_id must identify a service principal")
        if not self.service_id.startswith("service:") or not self.service_id[8:]:
            raise ValueError("service_id must identify a service")
        if self.deadline_at > _now() + timedelta(seconds=MAX_DEADLINE_SECONDS):
            raise ValueError("deadline_at exceeds the bounded execution horizon")
        return self


class GoalSnapshotToFileResult(BaseModel):
    """Public result with execution and verification kept on separate axes."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str = CAPABILITY_ID
    capability_version: str = CAPABILITY_VERSION
    job_id: str | None = None
    durable_status: str | None = None
    goal_id: str
    goal_revision: int
    file_path: str
    execution_status: Literal["succeeded", "failed", "blocked"]
    verification: Literal["passed", "failed", "unknown"]
    learning: Literal["no_learning"] = "no_learning"
    artifact_ref: str | None = None
    content_sha256: str | None = None
    output_exists: bool = False
    workspace_contained: bool = False
    goal_id_read_back: bool = False
    reconciliation_required: bool = False
    recovery_action: str | None = None
    durable_failure: dict[str, Any] | None = None
    authority_receipt: dict[str, Any] | None = None
    decision_input_digest: str | None = None
    strategy_delta_id: str | None = None
    strategy_delta_provenance: StrategyDeltaProvenance = "not_present"
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str = ""


class WorkflowToolProvider(Protocol):
    """Provider for the already wrapped WorkflowManager tool surface."""

    def __call__(self, workflow_name: str) -> Any | None:
        """Return the governed workflow tool or ``None`` when unavailable."""


@dataclass(frozen=True, slots=True)
class _Readback:
    output_exists: bool
    workspace_contained: bool
    goal_id_read_back: bool
    content_sha256: str | None
    content: bytes | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _AuthorityRequest:
    """Typed, model-independent authority material for one file effect."""

    envelope: CapabilityEnvelope
    policy: CapabilityPolicy
    global_policy: GlobalCapabilityPolicy
    principal: TrustPrincipal
    resource_limits: ResourceLimits
    workspace_path: Path
    approval_required: bool
    approval_state: str


def _workflow_definition_digest(
    *,
    workflow_name: str,
    workflow_version: str,
    step_sequence: list[str],
    workflow_tool_name: str = WORKFLOW_TOOL_NAME,
    step_definitions: Any = _CANONICAL_WORKFLOW_STEP_DEFINITIONS,
) -> str:
    return _safe_digest(
        {
            "workflow_name": workflow_name,
            "workflow_version": workflow_version,
            "workflow_tool_name": workflow_tool_name,
            "step_sequence": step_sequence,
            "step_definitions": step_definitions,
        }
    )


def _job_id(candidate: GoalCandidateDecision, request: GoalSnapshotToFileRequest) -> str:
    return "job_goal_snapshot_" + _safe_digest(
        {
            "candidate": candidate.dedupe_key,
            "owner": request.owner_principal_id,
            "service": request.service_id,
            # Keep scheduler retries idempotent across parent occurrences while
            # keeping an operator-triggered run in a distinct job namespace.
            "origin": "scheduler" if request.parent_job_id else "operator",
        }
    )[:24]


def _candidate_for_missing_goal(request: GoalSnapshotToFileRequest) -> GoalCandidateDecision:
    expected = request.expected_outcome.strip() or f"Write a goal snapshot to {request.file_path}"
    evidence_refs = list(normalized_evidence_refs(*request.evidence_refs))
    dedupe = stable_candidate_key(
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        criterion_id=None,
        capability_id=CAPABILITY_ID,
        capability_version=request.capability_version,
        evidence_refs=evidence_refs,
        expected_outcome=expected,
        inputs={"file_path": request.file_path},
    )
    return GoalCandidateDecision(
        candidate_id="cand_" + hashlib.sha256(dedupe.encode("utf-8")).hexdigest()[:24],
        dedupe_key=dedupe,
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        criterion_id=None,
        action=GoalCandidateAction.act,
        reason=request.reason,
        evidence_refs=evidence_refs,
        capability_id=CAPABILITY_ID,
        capability_version=request.capability_version,
        inputs={"file_path": request.file_path},
        expected_outcome=expected,
        expires_at=request.deadline_at,
    )


def _candidate_with_requested_revision(
    goal: Goal,
    request: GoalSnapshotToFileRequest,
) -> GoalCandidateDecision:
    candidate_request = GoalCandidateRequest(
        capability_id=CAPABILITY_ID,
        capability_version=request.capability_version,
        inputs={"file_path": request.file_path},
        evidence_refs=request.evidence_refs,
        reason=request.reason,
        expected_outcome=request.expected_outcome.strip() or f"Write a goal snapshot to {request.file_path}",
        expires_at=request.deadline_at,
    )
    decision = build_goal_candidate_decision(goal, candidate_request)
    if decision.goal_revision == request.goal_revision:
        return decision
    evidence_refs = list(decision.evidence_refs)
    dedupe = stable_candidate_key(
        goal_id=goal.id,
        goal_revision=request.goal_revision,
        criterion_id=decision.criterion_id,
        capability_id=decision.capability_id,
        capability_version=decision.capability_version,
        evidence_refs=evidence_refs,
        expected_outcome=decision.expected_outcome,
        inputs=decision.inputs,
    )
    return decision.model_copy(
        update={
            "candidate_id": "cand_" + hashlib.sha256(dedupe.encode("utf-8")).hexdigest()[:24],
            "dedupe_key": dedupe,
            "goal_revision": request.goal_revision,
        }
    )


def _status(projection: dict[str, Any] | None) -> str:
    if not isinstance(projection, dict):
        return ""
    return _text(projection.get("status") or projection.get("receipt", {}).get("status"))


def _lease(projection: dict[str, Any] | None) -> tuple[str | None, int | None]:
    lease = projection.get("lease") if isinstance(projection, dict) else None
    if not isinstance(lease, dict):
        return None, None
    owner = _text(lease.get("owner")) or None
    fencing = lease.get("fencing_token")
    try:
        fencing_token = int(fencing) if fencing is not None else None
    except (TypeError, ValueError):
        fencing_token = None
    return owner, fencing_token


def _artifact_from_projection(projection: dict[str, Any] | None) -> dict[str, Any] | None:
    artifacts = projection.get("artifacts") if isinstance(projection, dict) else None
    if not isinstance(artifacts, list):
        return None
    for item in reversed(artifacts):
        if isinstance(item, dict):
            return item
    return None


class GoalSnapshotToFileAdapter:
    """Execute exactly one goal snapshot through the governed workflow tool."""

    def __init__(
        self,
        request: GoalSnapshotToFileRequest,
        *,
        jobs: Any | None = None,
        goals: Any | None = None,
        workflow_tool_provider: WorkflowToolProvider | None = None,
        authority_policy: CapabilityPolicy | None = None,
        global_authority_policy: GlobalCapabilityPolicy | None = None,
        authority_principal: TrustPrincipal | None = None,
        authority_approval: ApprovalBinding | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.request = request
        self.jobs = jobs or durable_job_repository
        self.goals = goals or goal_repository
        self.workflow_tool_provider = workflow_tool_provider
        self.authority_policy = authority_policy
        self.global_authority_policy = global_authority_policy
        self.authority_principal = authority_principal
        self.authority_approval = authority_approval
        self.clock = clock
        self.last_receipt: dict[str, Any] | None = None
        self._resolved_workflow_binding: dict[str, Any] | None = None

    # The snapshot adapter owns the durable execution contract.  Narrow
    # workflow variants may override these hooks while retaining the same
    # admission, authority, lease, artifact, readback, and recovery path.
    def _capability_identifier(self) -> str:
        return CAPABILITY_ID

    def _workflow_identifier(self) -> str:
        return WORKFLOW_NAME

    def _workflow_tool_identifier(self) -> str:
        return WORKFLOW_TOOL_NAME

    def _allowed_workflow_steps(self) -> tuple[str, ...]:
        return _ALLOWED_WORKFLOW_STEP_SEQUENCE

    def _canonical_workflow_steps(self) -> tuple[dict[str, Any], ...]:
        return _CANONICAL_WORKFLOW_STEP_DEFINITIONS

    def _authority_source_identifier(self) -> str:
        return _AUTHORITY_SOURCE_ID

    def _authority_egress_class(self) -> EgressClass:
        return EgressClass.LOCAL_ONLY

    def _authority_operations(self) -> tuple[str, ...]:
        return ("write_file",)

    def _authority_network_hosts(self) -> tuple[str, ...]:
        return ()

    def _resource_claims(self) -> tuple[str, ...]:
        return ("workspace_read", "workspace_write")

    def _artifact_type(self) -> str:
        return "goal_snapshot"

    def _workflow_inputs(self, path: str) -> dict[str, Any]:
        return {"file_path": path}

    def _job_identifier(self, candidate: GoalCandidateDecision) -> str:
        return "job_goal_snapshot_" + _safe_digest(
            {
                "candidate": candidate.dedupe_key,
                "owner": self.request.owner_principal_id,
                "service": self.request.service_id,
                "origin": "scheduler" if self.request.parent_job_id else "operator",
            }
        )[:24]

    def _idempotency_scope(self) -> str:
        return (
            "goal-snapshot-to-file-scheduler"
            if self.request.parent_job_id
            else "goal-snapshot-to-file"
        )

    def _success_reason(self) -> str:
        return "goal_snapshot_executed_and_verified"

    def _extra_evidence_refs(self, readback: _Readback) -> tuple[str, ...]:
        return ()

    @staticmethod
    def _approval_requirement(
        context: dict[str, Any] | None,
        *,
        tool: Any | None = None,
    ) -> tuple[bool, str]:
        """Extract only the approval state needed by the authority request.

        The workflow's free-form approval context is data.  A caller-provided
        ``ApprovalBinding`` remains the only value that can satisfy an
        approval-required policy; a string such as ``"approved"`` is never
        promoted into authority by this adapter.
        """

        context = context if isinstance(context, dict) else {}
        raw_state = context.get("approval_state", context.get("approval_status"))
        state = _text(raw_state).lower()
        explicit_required = context.get("requires_approval", context.get("approval_required"))
        required = explicit_required is True or state in {
            "required",
            "pending",
            "missing",
            "denied",
            "rejected",
        }
        if _text(context.get("risk_level")).lower() == "high":
            required = True
        # ApprovalTool checks pending approval while the high-risk runtime mode
        # is active.  Detect that existing wrapper boundary without requiring
        # every injectable test tool to impersonate the wrapper.
        current = tool
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if hasattr(current, "force_approval") and hasattr(current, "wrapped_tool"):
                if bool(getattr(current, "force_approval", False)) or get_current_approval_mode() == "high_risk":
                    required = True
                break
            current = getattr(current, "wrapped_tool", None)
        if not required:
            return False, "not_required"
        if state in {"approved", "allow", "allowed"}:
            return True, "approved"
        return True, state or "missing"

    @staticmethod
    def _safe_approval_context(context: dict[str, Any] | None) -> dict[str, Any]:
        """Persist a digest and bounded metadata, never workflow arguments."""

        context = context if isinstance(context, dict) else {}
        safe: dict[str, Any] = {
            "context_digest": _safe_digest(context),
            "workflow_name": _text(context.get("workflow_name")) or None,
            "risk_level": _text(context.get("risk_level")).lower() or None,
            "requires_approval": bool(
                context.get("requires_approval") is True
                or context.get("approval_required") is True
            ),
            "approval_state": _text(
                context.get("approval_state", context.get("approval_status"))
            ).lower() or None,
            "step_tools": sorted(
                {
                    _text(item)
                    for item in context.get("step_tools", [])
                    if _text(item)
                }
            ),
            "execution_boundaries": sorted(
                {
                    _text(item)
                    for item in context.get("execution_boundaries", [])
                    if _text(item)
                }
            ),
        }
        return safe

    def _workflow_binding(
        self,
        *,
        status: str = "bound",
        source: str | None = None,
        observed: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Return the immutable workflow contract carried by this capability."""

        step_sequence = list(self._allowed_workflow_steps())
        binding = {
            "workflow_name": self._workflow_identifier(),
            "workflow_version": self.request.capability_version,
            "step_sequence": step_sequence,
            "step_sequence_digest": _safe_digest(step_sequence),
            "workflow_definition_digest": _workflow_definition_digest(
                workflow_name=self._workflow_identifier(),
                workflow_version=self.request.capability_version,
                step_sequence=step_sequence,
                workflow_tool_name=self._workflow_tool_identifier(),
                step_definitions=self._canonical_workflow_steps(),
            ),
            "binding_mode": "exact_step_sequence",
            "binding_status": status,
        }
        if source:
            binding["binding_source"] = source
        if reason:
            binding["rejection_reason"] = reason
        if observed is not None:
            binding["observed"] = observed
        return binding

    @staticmethod
    def _step_sequence(value: Any) -> list[str] | None:
        if not isinstance(value, (list, tuple)):
            return None
        sequence: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                return None
            sequence.append(canonical_tool_name(item.strip()))
        return sequence

    @staticmethod
    def _workflow_value(workflow: Any, key: str) -> Any:
        if isinstance(workflow, dict):
            return workflow.get(key)
        return getattr(workflow, key, None)

    @classmethod
    def _step_definitions(cls, value: Any) -> list[dict[str, Any]] | None:
        if not isinstance(value, (list, tuple)):
            return None
        definitions: list[dict[str, Any]] = []
        for index, step in enumerate(value):
            tool = cls._workflow_value(step, "tool")
            if not isinstance(tool, str) or not tool.strip():
                return None
            raw_arguments = cls._workflow_value(step, "arguments")
            if raw_arguments is None:
                raw_arguments = {}
            if not isinstance(raw_arguments, dict):
                return None
            step_id = _text(cls._workflow_value(step, "id")) or f"step_{index}"
            definitions.append(
                {
                    "id": step_id,
                    "tool": canonical_tool_name(tool.strip()),
                    "arguments": raw_arguments,
                }
            )
        return definitions

    def _sequence_mismatch_reason(self, sequence: list[str] | None) -> str | None:
        if sequence is None or not sequence:
            return "workflow_step_sequence_missing"
        expected_sequence = list(self._allowed_workflow_steps())
        if sequence == expected_sequence:
            return None
        expected = set(expected_sequence)
        if any(tool_name not in expected for tool_name in sequence):
            return "workflow_step_sequence_extra"
        if len(sequence) < len(expected_sequence) or not expected.issubset(sequence):
            return "workflow_step_sequence_missing"
        if len(sequence) == len(expected_sequence) and set(sequence) == expected:
            return "workflow_step_sequence_reordered"
        return "workflow_step_sequence_mismatch"

    def _rejected_workflow_binding(
        self,
        *,
        reason: str,
        observed_name: str | None,
        observed_version: str | None,
        observed_sequence: list[str] | None,
    ) -> tuple[dict[str, Any], str]:
        observed = {
            "workflow_name": observed_name,
            "workflow_version": observed_version,
            "step_sequence": observed_sequence,
        }
        return (
            self._workflow_binding(
                status="rejected",
                observed=observed,
                reason=reason,
            ),
            reason,
        )

    def _validate_workflow_definition(
        self,
        tool: Any,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """Require the exact registered workflow shape before admission.

        The manager's approval context is intentionally normalized for policy
        evaluation, so inspect an underlying Workflow definition when a
        wrapper exposes one and always validate the raw context sequence too.
        """

        observed_name = _text(context.get("workflow_name")) or None
        observed_version_raw = context.get("workflow_version", context.get("version"))
        observed_version = _text(observed_version_raw) or None
        observed_sequence = self._step_sequence(context.get("step_tools"))
        if not observed_name:
            return self._rejected_workflow_binding(
                reason="workflow_name_missing",
                observed_name=None,
                observed_version=observed_version,
                observed_sequence=observed_sequence,
            )
        if observed_name != self._workflow_identifier():
            return self._rejected_workflow_binding(
                reason="workflow_name_mismatch",
                observed_name=observed_name,
                observed_version=observed_version,
                observed_sequence=observed_sequence,
            )
        if observed_version_raw is not None and observed_version != self.request.capability_version:
            return self._rejected_workflow_binding(
                reason="workflow_version_mismatch",
                observed_name=observed_name,
                observed_version=observed_version,
                observed_sequence=observed_sequence,
            )
        sequence_reason = self._sequence_mismatch_reason(observed_sequence)
        if sequence_reason:
            return self._rejected_workflow_binding(
                reason=sequence_reason,
                observed_name=observed_name,
                observed_version=observed_version,
                observed_sequence=observed_sequence,
            )

        definition_source = "approval_context"
        current = tool
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            tool_name = _text(getattr(current, "name", None))
            if tool_name and tool_name != self._workflow_tool_identifier():
                return self._rejected_workflow_binding(
                    reason="workflow_tool_name_mismatch",
                    observed_name=observed_name,
                    observed_version=observed_version,
                    observed_sequence=observed_sequence,
                )
            workflow = getattr(current, "workflow", None)
            if workflow is not None:
                definition_source = "workflow_definition"
                definition_name = _text(self._workflow_value(workflow, "name")) or None
                definition_version_raw = self._workflow_value(workflow, "workflow_version")
                if definition_version_raw is None:
                    definition_version_raw = self._workflow_value(workflow, "version")
                definition_version = _text(definition_version_raw) or None
                definition_tool_name = _text(self._workflow_value(workflow, "tool_name")) or None
                definition_steps = self._step_sequence(
                    [
                        self._workflow_value(step, "tool")
                        for step in (self._workflow_value(workflow, "steps") or [])
                    ]
                    if isinstance(self._workflow_value(workflow, "steps"), (list, tuple))
                    else None
                )
                if not definition_name:
                    return self._rejected_workflow_binding(
                        reason="workflow_name_missing",
                        observed_name=None,
                        observed_version=definition_version,
                        observed_sequence=definition_steps,
                    )
                if definition_name != self._workflow_identifier():
                    return self._rejected_workflow_binding(
                        reason="workflow_name_mismatch",
                        observed_name=definition_name,
                        observed_version=definition_version,
                        observed_sequence=definition_steps,
                    )
                if definition_tool_name and definition_tool_name != self._workflow_tool_identifier():
                    return self._rejected_workflow_binding(
                        reason="workflow_tool_name_mismatch",
                        observed_name=definition_name,
                        observed_version=definition_version,
                        observed_sequence=definition_steps,
                    )
                if not definition_tool_name:
                    return self._rejected_workflow_binding(
                        reason="workflow_tool_name_missing",
                        observed_name=definition_name,
                        observed_version=definition_version,
                        observed_sequence=definition_steps,
                    )
                if definition_version_raw is not None and definition_version != self.request.capability_version:
                    return self._rejected_workflow_binding(
                        reason="workflow_version_mismatch",
                        observed_name=definition_name,
                        observed_version=definition_version,
                        observed_sequence=definition_steps,
                    )
                definition_reason = self._sequence_mismatch_reason(definition_steps)
                if definition_reason:
                    return self._rejected_workflow_binding(
                        reason=definition_reason,
                        observed_name=definition_name,
                        observed_version=definition_version,
                        observed_sequence=definition_steps,
                    )
                step_definitions = self._step_definitions(self._workflow_value(workflow, "steps"))
                definition_digest = _workflow_definition_digest(
                    workflow_name=definition_name,
                    workflow_version=self.request.capability_version,
                    workflow_tool_name=definition_tool_name,
                    step_sequence=definition_steps,
                    step_definitions=step_definitions,
                )
                expected_digest = self._workflow_binding()["workflow_definition_digest"]
                if definition_digest != expected_digest:
                    return self._rejected_workflow_binding(
                        reason="workflow_definition_digest_mismatch",
                        observed_name=definition_name,
                        observed_version=definition_version,
                        observed_sequence=definition_steps,
                    )
                break
            current = getattr(current, "wrapped_tool", None)

        return self._workflow_binding(source=definition_source), None

    def _authority_limits(self) -> ResourceLimits:
        remaining = (self.request.deadline_at - self.clock()).total_seconds()
        return ResourceLimits(
            cpu_seconds=min(_AUTHORITY_CPU_SECONDS, max(remaining, 0.001)),
            memory_bytes=_AUTHORITY_MEMORY_BYTES,
            pid_count=_AUTHORITY_PID_COUNT,
            output_bytes=MAX_OUTPUT_BYTES,
            deadline_seconds=min(_AUTHORITY_POLICY_DEADLINE_SECONDS, max(remaining, 0.001)),
        )

    def _authenticated_principal(self, *, job_id: str) -> TrustPrincipal:
        principal = self.authority_principal or get_current_trust_principal()
        if principal is None:
            raise ValueError("authenticated_owner_missing")
        if principal.principal_id != self.request.owner_principal_id:
            raise ValueError("authenticated_owner_mismatch")
        if principal.session_id and principal.session_id != self.request.session_id:
            raise ValueError("authenticated_owner_session_mismatch")
        if principal.job_id and principal.job_id != job_id:
            raise ValueError("authenticated_owner_job_mismatch")
        # The durable child job is the execution identity for this effect.  A
        # trusted service principal may delegate its authenticated grant to
        # that exact child identity; the authority gate still checks auth,
        # principal type, and capability grant below.
        return replace(
            principal,
            session_id=self.request.session_id,
            job_id=job_id,
        )

    def _build_authority_request(
        self,
        *,
        path: str,
        goal_id: str,
        job_id: str,
        approval_context: dict[str, Any] | None,
        workflow_tool: Any | None,
    ) -> _AuthorityRequest:
        root = canonical_workspace_root(settings.workspace_dir)
        workspace_path = root.joinpath(*PurePosixPath(path).parts)
        resource_limits = self._authority_limits()
        approval_required, approval_state = self._approval_requirement(
            approval_context,
            tool=workflow_tool,
        )
        if self.authority_policy is None:
            policy_limits = ResourceLimits(
                cpu_seconds=_AUTHORITY_CPU_SECONDS,
                memory_bytes=_AUTHORITY_MEMORY_BYTES,
                pid_count=_AUTHORITY_PID_COUNT,
                output_bytes=MAX_OUTPUT_BYTES,
                deadline_seconds=_AUTHORITY_POLICY_DEADLINE_SECONDS,
            )
            scope = CapabilityScope(
                operations=self._authority_operations(),
                paths=(str(root),),
                sources=(self._authority_source_identifier(),),
                egress_class=self._authority_egress_class(),
                network_hosts=self._authority_network_hosts(),
            )
            policy = CapabilityPolicy(
                capability_id=self._capability_identifier(),
                capability_version=self.request.capability_version,
                owner_id=self.request.owner_principal_id,
                principal_type=PrincipalType.SERVICE,
                scope=scope,
                resource_limits=policy_limits,
                goal_id=goal_id,
                requires_approval=approval_required,
                requires_audit=False,
                expires_at=self.request.deadline_at.timestamp(),
            )
        else:
            policy = self.authority_policy
            if approval_required and not policy.requires_approval:
                policy = replace(policy, requires_approval=True)
        if self.global_authority_policy is None:
            global_policy = GlobalCapabilityPolicy(
                scope=policy.scope,
                resource_limits=policy.resource_limits,
                allowed_capabilities=(self._capability_identifier(),),
                allowed_versions=(self.request.capability_version,),
                expires_at=self.request.deadline_at.timestamp(),
            )
        else:
            global_policy = self.global_authority_policy
        approval_required = approval_required or bool(policy.requires_approval)
        if approval_required and approval_state == "not_required":
            approval_state = "approved" if self.authority_approval is not None else "missing"
        principal = self._authenticated_principal(job_id=job_id)
        now = self.clock().timestamp()
        authority = issue_capability_authority(
            policy,
            principal,
            session_id=self.request.session_id,
            job_id=job_id,
            goal_id=goal_id,
            now=now,
            expires_at=self.request.deadline_at.timestamp(),
        )
        if self.authority_approval is not None:
            authority = replace(
                authority,
                approval=self.authority_approval,
                approval_digest=approval_binding_digest(self.authority_approval),
            )
        envelope = CapabilityEnvelope.create(
            authority=authority,
            operation="write_file",
            resource_limits=resource_limits,
            deadline_at=self.request.deadline_at.timestamp(),
            now=now,
            path=str(workspace_path),
            source_id=self._authority_source_identifier(),
            egress_class=self._authority_egress_class(),
            goal_id=goal_id,
            resource_type="workspace_file",
            resource_id="goal-snapshot:" + _safe_digest(
                {"goal_id": goal_id, "job_id": job_id, "path": path}
            )[:24],
        )
        return _AuthorityRequest(
            envelope=envelope,
            policy=policy,
            global_policy=global_policy,
            principal=principal,
            resource_limits=resource_limits,
            workspace_path=workspace_path,
            approval_required=approval_required,
            approval_state=approval_state,
        )

    def _authority_failure_receipt(self, *, reason: str, job_id: str, path: str) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_POLICY_SCHEMA_VERSION,
            "allowed": False,
            "effect": "deny",
            "reason_code": reason,
            "request_digest": _safe_digest({"job_id": job_id, "path": path}),
            "decision_id": "authority_failure_" + _safe_digest({"job_id": job_id, "reason": reason})[:24],
            "degradation": "blocked_before_effect",
            "capability_id": self._capability_identifier(),
            "capability_version": self.request.capability_version,
            "identity": {
                "owner_digest": _safe_digest({"owner_id": self.request.owner_principal_id}),
                "goal_digest": _safe_digest({"goal_id": self.request.goal_id}),
                "session_digest": _safe_digest({"session_id": self.request.session_id}),
                "job_digest": _safe_digest({"job_id": job_id}),
            },
            "scope": {
                "operation": "write_file",
                "path_digest": _safe_digest({"path": path}),
                "source_digest": _safe_digest({"source_id": self._authority_source_identifier()}),
                "egress_class": self._authority_egress_class().value,
            },
            "content": {
                "raw_content_stored": False,
                "secret_values_stored": False,
            },
            "recovery": "operator_review_or_fresh_authority",
        }

    def _evaluate_authority(
        self,
        *,
        path: str,
        goal_id: str,
        job_id: str,
        approval_context: dict[str, Any] | None,
        workflow_tool: Any | None,
        workflow_binding: dict[str, Any] | None = None,
        workflow_binding_reason: str | None = None,
    ) -> tuple[_AuthorityRequest | None, CapabilityDecision | None, dict[str, Any], str]:
        if workflow_binding_reason:
            reason = workflow_binding_reason
            receipt = self._authority_failure_receipt(
                reason=reason,
                job_id=job_id,
                path=path,
            )
            if workflow_binding is not None:
                receipt["workflow_binding"] = workflow_binding
            return None, None, receipt, reason
        try:
            material = self._build_authority_request(
                path=path,
                goal_id=goal_id,
                job_id=job_id,
                approval_context=approval_context,
                workflow_tool=workflow_tool,
            )
        except Exception as exc:
            reason = f"authority_request_invalid:{type(exc).__name__}"
            return None, None, self._authority_failure_receipt(reason=reason, job_id=job_id, path=path), reason
        try:
            decision = authorize_capability(
                material.envelope,
                material.policy,
                material.global_policy,
                now=self.clock().timestamp(),
            )
            if not isinstance(decision, CapabilityDecision):
                raise TypeError("authority_decision_invalid")
        except Exception as exc:
            reason = f"authority_evaluation_failed:{type(exc).__name__}"
            return material, None, self._authority_failure_receipt(reason=reason, job_id=job_id, path=path), reason
        return material, decision, dict(decision.receipt), decision.reason_code

    @staticmethod
    def _stable_authority_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        """Drop per-attempt references before binding authority to idempotency."""

        stable: dict[str, Any] = {}
        for key in (
            "schema_version",
            "allowed",
            "effect",
            "reason_code",
            "degradation",
            "capability_id",
            "capability_version",
            "destination_digest",
            "resource_digest",
            "principal",
            "scope",
            "audit",
            "resource_limits",
            "content",
            "recovery",
            "workflow_binding",
        ):
            if key in receipt:
                stable[key] = receipt[key]
        identity = receipt.get("identity")
        if isinstance(identity, dict):
            stable["identity"] = {
                key: identity[key]
                for key in ("owner_digest", "goal_digest", "session_digest", "job_digest")
                if key in identity
            }
        authority = receipt.get("authority")
        if isinstance(authority, dict):
            stable["authority"] = {
                "grant_present": bool(authority.get("grant_digest")),
                "approval_present": bool(authority.get("approval_digest")),
            }
        return stable

    def _recheck_authority(
        self,
        material: _AuthorityRequest,
    ) -> tuple[CapabilityDecision | None, dict[str, Any], str]:
        """Re-evaluate the same envelope after the durable lease is held."""

        policy = self.authority_policy or material.policy
        if material.approval_required and not policy.requires_approval:
            policy = replace(policy, requires_approval=True)
        global_policy = self.global_authority_policy or material.global_policy
        try:
            decision = authorize_capability(
                material.envelope,
                policy,
                global_policy,
                now=self.clock().timestamp(),
            )
            if not isinstance(decision, CapabilityDecision):
                raise TypeError("authority_decision_invalid")
        except Exception as exc:
            reason = f"authority_recheck_failed:{type(exc).__name__}"
            return None, self._authority_failure_receipt(
                reason=reason,
                job_id=material.envelope.job_id,
                path=self.request.file_path,
            ), reason
        return decision, dict(decision.receipt), decision.reason_code

    def _authority_declaration(
        self,
        *,
        material: _AuthorityRequest | None,
        decision: CapabilityDecision | None,
        receipt: dict[str, Any],
        approval_context: dict[str, Any] | None,
        path: str,
        workflow_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        effect = _text(getattr(decision, "effect", None)) or _text(receipt.get("effect")) or "deny"
        stable_receipt = self._stable_authority_receipt(receipt)
        stable_request_digest = _safe_digest(
            {
                "capability_id": self._capability_identifier(),
                "capability_version": self.request.capability_version,
                "receipt": stable_receipt,
            }
        )
        stable_reason = _text(getattr(decision, "reason_code", None)) or _text(receipt.get("reason_code"))
        stable_decision_id = "cap_" + _safe_digest(
            {"request": stable_request_digest, "reason": stable_reason}
        )[:24]
        return {
            "capability_id": self._capability_identifier(),
            "capability_version": self.request.capability_version,
            "required_grant": AuthorityGrant.CAPABILITY_EXECUTE.value,
            "principal": material.principal.principal_id if material else self.request.owner_principal_id,
            "authenticated": bool(material and material.principal.authenticated),
            "owner_kind": "service",
            "owner_principal_id": material.principal.principal_id if material else self.request.owner_principal_id,
            "service_id": self.request.service_id,
            "session_id": self.request.session_id,
            "goal_id": material.envelope.goal_id if material else self.request.goal_id,
            "authority_envelope": {
                "schema_version": _text(receipt.get("schema_version")) or CAPABILITY_POLICY_SCHEMA_VERSION,
                "allowed": bool(decision and decision.allowed),
                "effect": effect,
                "reason_code": stable_reason,
                "decision_id": stable_decision_id,
                "request_digest": stable_request_digest,
                "receipt": stable_receipt,
            },
            "approval": {
                "required": bool(material and material.approval_required),
                "state": material.approval_state if material else "missing",
                "binding_present": bool(material and material.envelope.authority and material.envelope.authority.approval),
                "binding_digest": (
                    material.envelope.authority.approval_digest
                    if material and material.envelope.authority
                    else ""
                ),
            },
            "scope": {
                "operation": "write_file",
                "path_digest": _safe_digest({"path": path}),
                "resource_limits_digest": material.resource_limits.digest() if material else None,
            },
            "approval_context": self._safe_approval_context(approval_context),
            "permissions": {
                "workspace_relative_output_only": True,
                "allowed_step_tools": list(self._allowed_workflow_steps()),
            },
            "workflow_binding": workflow_binding,
        }

    async def execute(
        self,
        *,
        goal: Goal,
        candidate: GoalCandidateDecision,
    ) -> GoalExecutionResult:
        self.last_receipt = None
        candidate_path = _text(candidate.inputs.get("file_path")) if isinstance(candidate.inputs, dict) else ""
        try:
            request_path = normalize_workspace_relative_path(self.request.file_path)
        except ValueError as exc:
            return self._blocked(f"unsafe_output_path:{_text(exc)}")
        try:
            path = normalize_workspace_relative_path(candidate_path)
        except ValueError:
            return self._blocked("candidate_output_path_mismatch")
        if path != request_path:
            return self._blocked("candidate_output_path_mismatch")
        if candidate.capability_id != self._capability_identifier() or candidate.capability_version != self.request.capability_version:
            return self._blocked("capability_contract_mismatch")
        if candidate.goal_id != self.request.goal_id or candidate.goal_revision != self.request.goal_revision:
            return self._blocked("request_goal_binding_mismatch")

        current_goal = await self.goals.get(candidate.goal_id)
        if current_goal is None:
            return self._blocked("goal_not_found")
        current_revision = max(int(current_goal.revision or 1), 1)
        if _text(current_goal.status) != "active":
            return self._blocked("goal_not_active")
        if current_revision != candidate.goal_revision:
            return self._blocked("stale_goal_revision")

        self._resolved_workflow_binding = None
        workflow_tool, approval_context, workflow_reason = self._resolve_workflow_tool(path)
        workflow_binding = self._resolved_workflow_binding
        workflow_binding_reason = (
            workflow_reason
            if isinstance(workflow_binding, dict)
            and workflow_binding.get("binding_status") == "rejected"
            else None
        )
        job_id = self._job_identifier(candidate)
        runner_owner = f"{self.request.service_id}:{job_id}"
        if self.request.cancel_requested:
            authority_material = None
            authority_decision = None
            authority_receipt = self._authority_failure_receipt(
                reason="authority_not_evaluated_cancel_requested",
                job_id=job_id,
                path=path,
            )
            authority_reason = "cancel_requested"
        else:
            (
                authority_material,
                authority_decision,
                authority_receipt,
                authority_reason,
            ) = self._evaluate_authority(
                path=path,
                goal_id=candidate.goal_id,
                job_id=job_id,
                approval_context=approval_context,
                workflow_tool=workflow_tool,
                workflow_binding=workflow_binding,
                workflow_binding_reason=workflow_binding_reason,
            )
        if workflow_binding is not None:
            authority_receipt = {
                **authority_receipt,
                "workflow_binding": workflow_binding,
            }
        declared_authority = self._authority_declaration(
            material=authority_material,
            decision=authority_decision,
            receipt=authority_receipt,
            approval_context=approval_context,
            path=path,
            workflow_binding=workflow_binding,
        )
        declared_authority.update(
            {
                "goal_revision": candidate.goal_revision,
                "workflow_name": self._workflow_identifier(),
                "workflow_tool_name": self._workflow_tool_identifier(),
                "workflow_available": workflow_tool is not None,
                "workflow_blocked_reason": workflow_reason,
            }
        )
        self.last_receipt = {
            "authority_receipt": authority_receipt,
            "authority_status": "allowed" if authority_decision and authority_decision.allowed else "denied",
            "authority_reason": authority_reason,
            "workflow_binding": workflow_binding,
        }
        spec = DurableJobSpec(
            identity=DurableJobIdentity(
                job_id=job_id,
                owner_kind="service",
                owner_principal_id=self.request.owner_principal_id,
                job_kind=self._capability_identifier(),
                capability_version=self.request.capability_version,
                idempotency_scope=self._idempotency_scope(),
                idempotency_key=candidate.dedupe_key,
            ),
            inputs={
                "goal_id": candidate.goal_id,
                "goal_revision": candidate.goal_revision,
                **self._workflow_inputs(path),
                "workflow_binding": workflow_binding,
            },
            session_id=self.request.session_id,
            parent_job_id=self.request.parent_job_id,
            parent_fencing_token=self.request.parent_fencing_token,
            goal_id=candidate.goal_id,
            goal_revision=candidate.goal_revision,
            candidate_id=candidate.candidate_id,
            priority=self.request.priority,
            resource_claims=self._resource_claims(),
            declared_authority=declared_authority,
            deadline_at=self.request.deadline_at,
            max_attempts=1,
            service_id=self.request.service_id,
        )
        try:
            admission = await self.jobs.admit_job(spec)
        except DurableJobIdempotencyConflict:
            if authority_decision is None or not authority_decision.allowed:
                self._mark_durable_failure(
                    job_id,
                    operation="authority_denial_admission",
                    error=ValueError("idempotency_conflict"),
                )
                return self._durable_blocked(
                    job_id,
                    fallback_reason=f"authority_denied:{authority_reason}",
                )
            return self._blocked("idempotency_conflict", job_id=job_id)
        except Exception as exc:
            if authority_decision is None or not authority_decision.allowed:
                self._mark_durable_failure(
                    job_id,
                    operation="authority_denial_admission",
                    error=exc,
                )
                return self._durable_blocked(
                    job_id,
                    fallback_reason=f"authority_denied:{authority_reason}",
                )
            return self._blocked(f"job_admission_failed:{type(exc).__name__}", job_id=job_id)
        self._remember_projection(admission, job_id=job_id)
        admission_receipt_status = _text(admission.get("receipt", {}).get("status"))
        if admission_receipt_status == "deduped":
            if not self.request.cancel_requested and (authority_decision is None or not authority_decision.allowed):
                return await self._record_authority_denial(
                    admission,
                    job_id=job_id,
                    reason=authority_reason,
                    receipt=authority_receipt,
                )
            return await self._replay_admission(admission, candidate, path)
        if _status(admission) == "failed":
            return self._failed("deadline_expired", job_id=job_id, durable_status="failed")
        if not self.request.cancel_requested and (authority_decision is None or not authority_decision.allowed):
            return await self._record_authority_denial(
                admission,
                job_id=job_id,
                reason=authority_reason,
                receipt=authority_receipt,
            )

        try:
            queued = await self.jobs.queue_job(job_id)
            self._remember_projection(queued, job_id=job_id)
        except Exception as exc:
            self._mark_durable_failure(job_id, operation="queue", error=exc)
            return self._durable_blocked(
                job_id,
                fallback_reason=f"queue_failed:{type(exc).__name__}",
            )
        if self.request.cancel_requested:
            return await self._cancel(job_id, reason="cancel_requested")

        try:
            claimed = await self.jobs.claim_job(job_id, owner=runner_owner, lease_seconds=MAX_DEADLINE_SECONDS)
            self._remember_projection(claimed, job_id=job_id)
        except Exception as exc:
            self._mark_durable_failure(job_id, operation="claim", error=exc)
            return self._durable_blocked(
                job_id,
                fallback_reason=f"claim_failed:{type(exc).__name__}",
            )
        if _status(claimed) in {"failed", "blocked", "cancelled"}:
            return self._result_for_durable_status(_status(claimed), _text(claimed.get("failure_reason")) or "job_not_runnable", job_id=job_id)
        lease_owner, fencing_token = _lease(claimed)
        if lease_owner != runner_owner or fencing_token is None:
            self._mark_durable_failure(
                job_id,
                operation="claim",
                error=ValueError("claim_missing_current_fence"),
                durable_status=_status(claimed) or "unknown",
            )
            return self._durable_blocked(
                job_id,
                fallback_reason="claim_missing_current_fence",
                durable_status=_status(claimed) or "unknown",
            )
        if self.request.cancel_requested:
            return await self._cancel(job_id, owner=runner_owner, fencing_token=fencing_token, reason="cancel_requested")

        current_goal = await self.goals.get(candidate.goal_id)
        current_revision = max(int(current_goal.revision or 1), 1) if current_goal else None
        if current_goal is None or _text(current_goal.status) != "active" or current_revision != candidate.goal_revision:
            reason = "goal_not_found" if current_goal is None else (
                "goal_not_active" if _text(current_goal.status) != "active" else "stale_goal_revision"
            )
            guard_effect = await self._record_effect(
                job_id,
                effect_type="goal_revision_guard",
                status="blocked",
                details={"reason": reason, "expected_revision": candidate.goal_revision, "observed_revision": current_revision},
                owner=runner_owner,
                fencing_token=fencing_token,
            )
            return await self._block_job(
                job_id,
                reason=reason,
                owner=runner_owner,
                fencing_token=fencing_token,
            )
        if self.request.deadline_at <= self.clock():
            return await self._fail_job(
                job_id,
                reason="deadline_expired",
                owner=runner_owner,
                fencing_token=fencing_token,
            )
        if workflow_tool is None:
            workflow_effect = await self._record_effect(
                job_id,
                effect_type="workflow_invocation",
                status="blocked",
                details={
                    "workflow_name": self._workflow_identifier(),
                    "reason": workflow_reason or "workflow_unavailable",
                    "workflow_binding": workflow_binding,
                },
                owner=runner_owner,
                fencing_token=fencing_token,
            )
            return await self._block_job(
                job_id,
                reason=workflow_reason or "workflow_unavailable",
                owner=runner_owner,
                fencing_token=fencing_token,
            )

        if authority_material is None:
            return await self._record_authority_denial(
                claimed,
                job_id=job_id,
                reason="authority_material_missing",
                receipt=self._authority_failure_receipt(
                    reason="authority_material_missing",
                    job_id=job_id,
                    path=path,
                ),
                owner=runner_owner,
                fencing_token=fencing_token,
            )
        (
            authority_decision,
            authority_receipt,
            authority_reason,
        ) = self._recheck_authority(authority_material)
        if workflow_binding is not None:
            authority_receipt = {
                **authority_receipt,
                "workflow_binding": workflow_binding,
            }
        self.last_receipt = {
            **(self.last_receipt or {}),
            "authority_receipt": authority_receipt,
            "authority_status": "allowed" if authority_decision and authority_decision.allowed else "denied",
            "authority_reason": authority_reason,
        }
        if authority_decision is None or not authority_decision.allowed:
            return await self._record_authority_denial(
                claimed,
                job_id=job_id,
                reason=authority_reason,
                receipt=authority_receipt,
                owner=runner_owner,
                fencing_token=fencing_token,
            )

        try:
            raw_result, workflow_audit = await self._invoke_workflow(
                workflow_tool,
                path,
                session_id=self.request.session_id,
                job_id=job_id,
                principal=authority_material.principal if authority_material is not None else None,
            )
        except Exception as exc:
            if type(exc).__name__ == "ApprovalRequired":
                approval_effect = await self._record_effect(
                    job_id,
                    effect_type="workflow_invocation",
                    status="blocked",
                    details={
                        "workflow_name": self._workflow_identifier(),
                        "reason": "approval_required",
                        "workflow_binding": workflow_binding,
                    },
                    owner=runner_owner,
                    fencing_token=fencing_token,
                )
                if approval_effect is None:
                    return await self._block_job(
                        job_id,
                        reason="approval_required",
                        owner=runner_owner,
                        fencing_token=fencing_token,
                    )
                approval_transition = await self._transition(
                    job_id,
                    "awaiting_approval",
                    owner=runner_owner,
                    fencing_token=fencing_token,
                    reason="approval_required",
                )
                if approval_transition is None or _status(approval_transition) != "awaiting_approval":
                    return self._durable_blocked(
                        job_id,
                        fallback_reason="approval_transition_not_confirmed",
                        durable_status=_status(approval_transition) or "running",
                    )
                return self._blocked("approval_required", job_id=job_id, durable_status="awaiting_approval")
            workflow_failure_reason = f"workflow_failed:{type(exc).__name__}"
            failure_effect = await self._record_effect(
                job_id,
                effect_type="workflow_invocation",
                status="failed",
                details={
                    "workflow_name": self._workflow_identifier(),
                    "error_type": type(exc).__name__,
                    "workflow_binding": workflow_binding,
                },
                owner=runner_owner,
                fencing_token=fencing_token,
            )
            if failure_effect is None:
                return await self._block_job(
                    job_id,
                    reason=workflow_failure_reason,
                    owner=runner_owner,
                    fencing_token=fencing_token,
                )
            return await self._fail_job(
                job_id,
                reason=workflow_failure_reason,
                owner=runner_owner,
                fencing_token=fencing_token,
            )

        invocation_effect = await self._record_effect(
            job_id,
            effect_type="workflow_invocation",
            status="failed" if _text(raw_result).startswith("Error:") else "succeeded",
            details={
                "workflow_name": self._workflow_identifier(),
                "workflow_binding": workflow_binding,
                "result_error": _text(raw_result).startswith("Error:"),
                "workflow_audit_digest": _safe_digest(workflow_audit or {}),
                "durable_workflow_run_identity": _text((workflow_audit or {}).get("durable_run_identity")) or None,
            },
            owner=runner_owner,
            fencing_token=fencing_token,
        )
        if invocation_effect is None:
            return await self._block_job(
                job_id,
                reason="execution_receipt_failed",
                owner=runner_owner,
                fencing_token=fencing_token,
            )
        if _text(raw_result).startswith("Error:"):
            return await self._fail_job(
                job_id,
                reason="workflow_returned_error",
                owner=runner_owner,
                fencing_token=fencing_token,
            )
        readback = self._readback(path, candidate.goal_id)
        if not readback.output_exists or not readback.workspace_contained or not readback.goal_id_read_back or readback.content is None:
            readback_receipt = await self._record_readback(
                job_id,
                path,
                readback,
                owner=runner_owner,
                fencing_token=fencing_token,
            )
            reason = readback.reason or "output_readback_failed"
            if readback_receipt is None:
                return await self._block_job(
                    job_id,
                    reason=reason,
                    owner=runner_owner,
                    fencing_token=fencing_token,
                )
            return await self._fail_job(
                job_id,
                reason=reason,
                owner=runner_owner,
                fencing_token=fencing_token,
                readback=readback,
            )

        try:
            artifact = await self.jobs.record_artifact(
                job_id,
                file_path=path,
                artifact_type=self._artifact_type(),
                content=readback.content,
                owner=runner_owner,
                fencing_token=fencing_token,
            )
            if not isinstance(artifact, dict):
                raise TypeError("artifact_receipt_missing")
            artifact_record = artifact.get("receipt", artifact)
            if not isinstance(artifact_record, dict):
                raise TypeError("artifact_receipt_missing")
            artifact_id = _text(artifact_record.get("artifact_id")) or None
        except Exception as exc:
            self._mark_durable_failure(job_id, operation="artifact", error=exc)
            return await self._block_job(
                job_id,
                reason="artifact_receipt_failed",
                owner=runner_owner,
                fencing_token=fencing_token,
            )

        if not artifact_id:
            self._mark_durable_failure(
                job_id,
                operation="artifact",
                error=ValueError("artifact_id_missing"),
            )
            return await self._block_job(
                job_id,
                reason="artifact_receipt_invalid",
                owner=runner_owner,
                fencing_token=fencing_token,
            )
        readback_receipt = await self._record_readback(
            job_id,
            path,
            readback,
            owner=runner_owner,
            fencing_token=fencing_token,
        )
        if readback_receipt is None:
            return await self._block_job(
                job_id,
                reason="verification_receipt_failed",
                owner=runner_owner,
                fencing_token=fencing_token,
            )
        write_effect = await self._record_effect(
            job_id,
            effect_type="workspace_write",
            status="succeeded",
            content_sha256=readback.content_sha256,
            details={"artifact_id": artifact_id, "goal_id": candidate.goal_id, "file_path": path},
            owner=runner_owner,
            fencing_token=fencing_token,
        )
        if write_effect is None:
            return await self._block_job(
                job_id,
                reason="workspace_write_receipt_failed",
                owner=runner_owner,
                fencing_token=fencing_token,
            )
        evidence = list(normalized_evidence_refs(
            *candidate.evidence_refs,
            f"job:{job_id}",
            f"readback:{readback.content_sha256}",
            *self._extra_evidence_refs(readback),
        ))
        transitioned = await self._transition(
            job_id,
            "succeeded",
            owner=runner_owner,
            fencing_token=fencing_token,
            result={
                "goal_id": candidate.goal_id,
                "goal_revision": candidate.goal_revision,
                "file_path": path,
                "content_sha256": readback.content_sha256,
                "artifact_id": artifact_id,
                "verification": "passed",
            },
            result_summary="goal snapshot workflow executed and output read back",
        )
        if transitioned is None or _status(transitioned) != "succeeded":
            return self._durable_blocked(
                job_id,
                fallback_reason="succeeded_transition_not_confirmed",
                durable_status=_status(transitioned) or "running",
            )

        self.last_receipt = {
            **(self.last_receipt or {}),
            "job_id": job_id,
            "durable_status": "succeeded",
            "artifact_id": artifact_id,
            "content_sha256": readback.content_sha256,
            "output_exists": readback.output_exists,
            "workspace_contained": readback.workspace_contained,
            "goal_id_read_back": readback.goal_id_read_back,
            "workflow_audit_digest": _safe_digest(workflow_audit or {}),
        }
        return GoalExecutionResult(
            execution_status="succeeded",
            verification="passed",
            usefulness="unknown",
            learning="no_learning",
            artifact_ref=artifact_id,
            evidence_refs=evidence,
            reason=self._success_reason(),
        )

    def _resolve_workflow_tool(self, path: str) -> tuple[Any | None, dict[str, Any] | None, str | None]:
        if self.workflow_tool_provider is not None:
            try:
                tool = self.workflow_tool_provider(self._workflow_identifier())
            except Exception as exc:
                return None, None, f"workflow_resolution_failed:{type(exc).__name__}"
            if tool is None:
                return None, None, "governed_workflow_unavailable"
            context = self._approval_context(tool, path)
            if context is None:
                return None, None, "workflow_approval_context_unavailable"
            try:
                binding, binding_reason = self._validate_workflow_definition(tool, context)
            except Exception as exc:
                self._resolved_workflow_binding = self._workflow_binding(
                    status="rejected",
                    reason=type(exc).__name__,
                )
                return None, context, f"workflow_definition_validation_failed:{type(exc).__name__}"
            self._resolved_workflow_binding = binding
            if binding_reason:
                return None, context, binding_reason
            return tool, context, None
        try:
            from src.agent.factory import get_tools
            from src.workflows.manager import workflow_manager

            workflow = workflow_manager.get_workflow(self._workflow_identifier())
            if workflow is None or not workflow.enabled:
                return None, None, "workflow_not_loaded_or_disabled"
            tools = get_tools()
            tool = next((item for item in tools if getattr(item, "name", "") == workflow.tool_name), None)
            if tool is None:
                return None, None, "governed_workflow_tool_unavailable"
            context = self._approval_context(tool, path)
            if context is None:
                return None, None, "workflow_approval_context_unavailable"
            binding, binding_reason = self._validate_workflow_definition(tool, context)
            self._resolved_workflow_binding = binding
            if binding_reason:
                return None, context, binding_reason
            return tool, context, None
        except Exception as exc:
            return None, None, f"workflow_resolution_failed:{type(exc).__name__}"

    @staticmethod
    def _approval_context(tool: Any, path: str) -> dict[str, Any] | None:
        hook = getattr(tool, "get_approval_context", None)
        if not callable(hook):
            current = getattr(tool, "wrapped_tool", None)
            while current is not None and current is not tool:
                hook = getattr(current, "get_approval_context", None)
                if callable(hook):
                    break
                current = getattr(current, "wrapped_tool", None)
        if not callable(hook):
            return None
        context = hook({"file_path": path})
        return context if isinstance(context, dict) else None

    async def _invoke_workflow(
        self,
        tool: Any,
        path: str,
        *,
        session_id: str,
        job_id: str,
        principal: TrustPrincipal | None = None,
    ) -> tuple[Any, dict[str, Any] | None]:
        if principal is None:
            raise PermissionError("authenticated_owner_missing")
        effective_principal = principal
        call = partial(tool, **self._workflow_inputs(path), sanitize_inputs_outputs=True)
        if self.workflow_tool_provider is not None:
            # The injectable boundary is deliberately synchronous for tests;
            # the production provider below runs wrappers off the event loop.
            tokens = set_runtime_context(
                session_id,
                get_current_approval_mode(),
                trust_principal=effective_principal,
            )
            try:
                result = call()
            finally:
                reset_runtime_context(tokens)
        else:
            tokens = set_runtime_context(
                session_id,
                get_current_approval_mode(),
                trust_principal=effective_principal,
            )
            try:
                run_context = contextvars.copy_context()
            finally:
                reset_runtime_context(tokens)
            result = await asyncio.to_thread(run_context.run, call)
        audit_payload = self._audit_result_payload(tool, self._workflow_inputs(path), result)
        return result, audit_payload

    @staticmethod
    def _audit_result_payload(tool: Any, arguments: dict[str, Any], result: Any) -> dict[str, Any] | None:
        current = tool
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            hook = getattr(current, "get_audit_result_payload", None)
            if callable(hook):
                payload = hook(arguments, result)
                if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[1], dict):
                    return payload[1]
            current = getattr(current, "wrapped_tool", None)
        return None

    def _readback(self, path: str, goal_id: str) -> _Readback:
        try:
            root = canonical_workspace_root(settings.workspace_dir)
            target = root.joinpath(*PurePosixPath(path).parts)
            current = root
            for index, part in enumerate(PurePosixPath(path).parts):
                current = current / part
                try:
                    stat_result = current.lstat()
                except FileNotFoundError:
                    return _Readback(False, False, False, None, None, "output_missing")
                if stat.S_ISLNK(stat_result.st_mode):
                    return _Readback(False, False, False, None, None, "output_symlink_blocked")
                if index < len(PurePosixPath(path).parts) - 1 and not current.is_dir():
                    return _Readback(False, False, False, None, None, "output_parent_not_directory")
            resolved = target.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError:
                return _Readback(True, False, False, None, None, "output_outside_workspace")
            if not resolved.is_file():
                return _Readback(False, True, False, None, None, "output_not_regular_file")
            if resolved.stat().st_size > MAX_OUTPUT_BYTES:
                return _Readback(True, True, False, None, None, "output_exceeds_size_limit")
            content = resolved.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            goal_id_read_back = goal_id.encode("utf-8") in content
            if not goal_id_read_back:
                return _Readback(True, True, False, digest, content, "goal_id_missing_from_output")
            return _Readback(True, True, True, digest, content)
        except (OSError, ValueError) as exc:
            return _Readback(False, False, False, None, None, f"output_readback_failed:{type(exc).__name__}")

    async def _record_authority_denial(
        self,
        admission: dict[str, Any],
        *,
        job_id: str,
        reason: str,
        receipt: dict[str, Any],
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> GoalExecutionResult:
        """Persist a blocked authority decision before any workflow effect."""

        denial_reason = f"authority_denied:{reason}"
        status = _status(admission) or "accepted"
        self.last_receipt = {
            **(self.last_receipt or {}),
            "authority_receipt": receipt,
            "authority_status": "denied",
            "authority_reason": reason,
        }
        # A fresh accepted record can be durably converted to blocked without
        # taking a lease.  This records the denial and leaves the capability
        # unqueued, so the governed workflow cannot be invoked accidentally.
        lease_kwargs = {
            "owner": owner,
            "fencing_token": fencing_token,
        }
        can_block = status in {"accepted", "queued"} or (
            status == "running" and owner is not None and fencing_token is not None
        )
        if can_block:
            effect = await self._record_effect(
                job_id,
                effect_type="authority_gate",
                receipt_kind="effect",
                status="blocked",
                details={
                    "decision": "deny",
                    "reason_code": reason,
                    "redacted_receipt": receipt,
                },
                **lease_kwargs,
            )
            if effect is None:
                return self._durable_blocked(
                    job_id,
                    fallback_reason=f"authority_denial_effect_failed:{reason}",
                    durable_status=status,
                )
            transitioned = await self._transition(
                job_id,
                "blocked",
                reason=denial_reason,
                **lease_kwargs,
            )
            if transitioned is None:
                return self._durable_blocked(
                    job_id,
                    fallback_reason=f"authority_denial_transition_failed:{reason}",
                    durable_status=status,
                )
            transitioned_status = _status(transitioned) or "blocked"
            if transitioned_status != "blocked":
                return self._durable_blocked(
                    job_id,
                    fallback_reason=f"authority_denial_not_blocked:{reason}",
                    durable_status=transitioned_status,
                )
            return self._blocked(denial_reason, job_id=job_id, durable_status="blocked")

        # An already-running or queued idempotent projection must not be
        # claimed by a denied retry.  Leave an operator-visible reconciliation
        # marker rather than pretending the decision was applied to it.
        if status not in {"blocked", "failed", "cancelled"}:
            self.last_receipt = {
                **(self.last_receipt or {}),
                "reconciliation_required": True,
                "recovery_action": _RECONCILIATION_ACTION,
                "authority_denial_unapplied": True,
            }
        return self._result_for_durable_status(
            status,
            denial_reason,
            job_id=job_id,
        )

    async def _record_effect(self, job_id: str, **kwargs: Any) -> dict[str, Any] | None:
        try:
            result = await self.jobs.record_effect(job_id, **kwargs)
            self._remember_projection(result, job_id=job_id)
            return result
        except Exception as exc:
            self._mark_durable_failure(
                job_id,
                operation=f"effect:{_text(kwargs.get('effect_type')) or 'unknown'}",
                error=exc,
            )
            return None

    async def _record_readback(self, job_id: str, path: str, readback: _Readback, **kwargs: Any) -> dict[str, Any] | None:
        details = {
            "output_exists": readback.output_exists,
            "workspace_contained": readback.workspace_contained,
            "goal_id_read_back": readback.goal_id_read_back,
            "reason": readback.reason,
            "goal_id_digest": _safe_digest({"goal_id": self.request.goal_id}),
            "verified": bool(
                readback.output_exists
                and readback.workspace_contained
                and readback.goal_id_read_back
                and readback.content is not None
            ),
        }
        try:
            if hasattr(self.jobs, "record_readback"):
                result = await self.jobs.record_readback(
                    job_id,
                    target_path=path,
                    status="succeeded" if readback.goal_id_read_back else "failed",
                    content_sha256=readback.content_sha256,
                    details=details,
                    **kwargs,
                )
            else:
                result = await self.jobs.record_effect(
                    job_id,
                    effect_type="readback",
                    receipt_kind="readback",
                    target_path=path,
                    status="succeeded" if readback.goal_id_read_back else "failed",
                    content_sha256=readback.content_sha256,
                    details=details,
                    **kwargs,
                )
            self._remember_projection(result, job_id=job_id)
            return result
        except Exception as exc:
            self._mark_durable_failure(job_id, operation="readback", error=exc)
            return None

    async def _transition(self, job_id: str, status: str, **kwargs: Any) -> dict[str, Any] | None:
        try:
            result = await self.jobs.transition_job(job_id, status, **kwargs)
            self._remember_projection(result, job_id=job_id)
            return result
        except Exception as exc:
            self._mark_durable_failure(
                job_id,
                operation=f"transition:{status}",
                error=exc,
            )
            return None

    async def _cancel(self, job_id: str, *, reason: str, owner: str | None = None, fencing_token: int | None = None) -> GoalExecutionResult:
        result = await self._transition(job_id, "cancelled", owner=owner, fencing_token=fencing_token, reason=reason)
        if result is None:
            return self._durable_blocked(job_id, fallback_reason=f"cancel_transition_failed:{reason}")
        durable_status = _status(result) or "cancelled"
        self.last_receipt = {"job_id": job_id, "durable_status": durable_status}
        return self._blocked(reason, job_id=job_id, durable_status=durable_status)

    def _mark_durable_failure(
        self,
        job_id: str,
        *,
        operation: str,
        error: Exception,
        durable_status: str | None = None,
    ) -> str:
        """Keep a safe recovery marker when durable persistence fails."""
        reason = f"durable_reconciliation_required:{operation}:{type(error).__name__}"
        previous = dict(self.last_receipt or {})
        previous_failure = previous.get("durable_failure")
        failures = list(previous.get("durable_failures") or [])
        if isinstance(previous_failure, dict) and previous_failure not in failures:
            failures.append(previous_failure)
        failure = {
            "reason": reason,
            "operation": operation,
            "error_type": type(error).__name__,
            "reconciliation_required": True,
        }
        failures.append(failure)
        self.last_receipt = {
            **previous,
            "job_id": job_id,
            "durable_status": durable_status or previous.get("durable_status") or "unknown",
            "durable_failure": failure,
            "durable_failures": failures[-8:],
            "reconciliation_required": True,
            "recovery_action": _RECONCILIATION_ACTION,
        }
        return reason

    def _durable_blocked(
        self,
        job_id: str,
        *,
        fallback_reason: str,
        durable_status: str | None = None,
    ) -> GoalExecutionResult:
        failure = self.last_receipt.get("durable_failure") if isinstance(self.last_receipt, dict) else None
        reason = _text(failure.get("reason")) if isinstance(failure, dict) else ""
        if not reason:
            reason = f"durable_reconciliation_required:transition:{fallback_reason}"
            self.last_receipt = {
                **(self.last_receipt or {}),
                "job_id": job_id,
                "durable_status": durable_status or (self.last_receipt or {}).get("durable_status") or "unknown",
                "reconciliation_required": True,
                "recovery_action": _RECONCILIATION_ACTION,
            }
        return self._blocked(
            reason,
            job_id=job_id,
            durable_status=durable_status or (self.last_receipt or {}).get("durable_status"),
        )

    async def _block_job(
        self,
        job_id: str,
        *,
        reason: str,
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> GoalExecutionResult:
        failure = self.last_receipt.get("durable_failure") if isinstance(self.last_receipt, dict) else None
        failure_reason = _text(failure.get("reason")) if isinstance(failure, dict) else ""
        transitioned = await self._transition(
            job_id,
            "blocked",
            owner=owner,
            fencing_token=fencing_token,
            reason=reason,
        )
        if transitioned is None:
            return self._durable_blocked(job_id, fallback_reason=f"blocked_transition_failed:{reason}")
        return self._blocked(
            failure_reason or reason,
            job_id=job_id,
            durable_status=_status(transitioned) or "blocked",
        )

    async def _fail_job(
        self,
        job_id: str,
        *,
        reason: str,
        owner: str | None = None,
        fencing_token: int | None = None,
        readback: _Readback | None = None,
    ) -> GoalExecutionResult:
        transitioned = await self._transition(
            job_id,
            "failed",
            owner=owner,
            fencing_token=fencing_token,
            reason=reason,
        )
        if transitioned is None:
            return self._durable_blocked(job_id, fallback_reason=f"failed_transition_failed:{reason}")
        return self._failed(
            reason,
            job_id=job_id,
            durable_status=_status(transitioned) or "failed",
            readback=readback,
        )

    async def _replay_admission(self, projection: dict[str, Any], candidate: GoalCandidateDecision, path: str) -> GoalExecutionResult:
        job_id = _text(projection.get("job_id")) or self._job_identifier(candidate)
        status = _status(projection)
        if status == "succeeded":
            artifact = _artifact_from_projection(projection)
            readback = self._readback(path, candidate.goal_id)
            if artifact and readback.output_exists and readback.workspace_contained and readback.goal_id_read_back:
                artifact_id = _text(artifact.get("artifact_id")) or None
                digest = readback.content_sha256 or _text(artifact.get("content_sha256")) or None
                self.last_receipt = {
                    "job_id": job_id,
                    "durable_status": "succeeded",
                    "artifact_id": artifact_id,
                    "content_sha256": digest,
                    "output_exists": True,
                    "workspace_contained": True,
                    "goal_id_read_back": True,
                    "idempotent_replay": True,
                }
                return GoalExecutionResult(
                    execution_status="succeeded",
                    verification="passed",
                    learning="no_learning",
                    artifact_ref=artifact_id,
                    evidence_refs=[
                        f"job:{job_id}",
                        f"readback:{digest}",
                        *self._extra_evidence_refs(readback),
                    ],
                    reason="idempotent_replay_verified",
                )
            return self._blocked("idempotent_terminal_artifact_readback_failed", job_id=job_id, durable_status=status)
        if status == "failed":
            return self._failed("idempotent_existing_failed_job", job_id=job_id, durable_status=status)
        if status == "cancelled":
            return self._blocked("idempotent_existing_cancelled_job", job_id=job_id, durable_status=status)
        return self._blocked("idempotent_job_already_admitted", job_id=job_id, durable_status=status or "accepted")

    def _remember_projection(self, projection: dict[str, Any] | None, *, job_id: str) -> None:
        if not isinstance(projection, dict):
            return
        receipt = projection.get("receipt") if isinstance(projection.get("receipt"), dict) else {}
        lease = projection.get("lease") if isinstance(projection.get("lease"), dict) else {}
        self.last_receipt = {
            **(self.last_receipt or {}),
            "job_id": job_id,
            "durable_status": _status(projection),
            "receipt_kind": receipt.get("kind"),
            "receipt_status": receipt.get("status"),
            "fencing_token": lease.get("fencing_token"),
        }

    def _blocked(
        self,
        reason: str,
        *,
        job_id: str | None = None,
        durable_status: str | None = None,
        readback: _Readback | None = None,
    ) -> GoalExecutionResult:
        if job_id:
            self.last_receipt = {
                **(self.last_receipt or {}),
                "job_id": job_id,
                "durable_status": durable_status or (self.last_receipt or {}).get("durable_status") or "blocked",
            }
        return GoalExecutionResult(
            execution_status="blocked",
            verification="unknown",
            learning="no_learning",
            evidence_refs=[],
            reason=reason,
        )

    def _failed(
        self,
        reason: str,
        *,
        job_id: str | None = None,
        durable_status: str | None = None,
        readback: _Readback | None = None,
    ) -> GoalExecutionResult:
        if job_id:
            self.last_receipt = {
                **(self.last_receipt or {}),
                "job_id": job_id,
                "durable_status": durable_status or (self.last_receipt or {}).get("durable_status") or "failed",
                **({
                    "output_exists": readback.output_exists,
                    "workspace_contained": readback.workspace_contained,
                    "goal_id_read_back": readback.goal_id_read_back,
                    "content_sha256": readback.content_sha256,
                } if readback else {}),
            }
        return GoalExecutionResult(
            execution_status="failed",
            verification="failed" if readback is not None else "unknown",
            learning="no_learning",
            reason=reason,
        )

    def _result_for_durable_status(self, status: str, reason: str, *, job_id: str) -> GoalExecutionResult:
        if status == "failed":
            return self._failed(reason, job_id=job_id, durable_status=status)
        return self._blocked(reason, job_id=job_id, durable_status=status)


class GoalSnapshotToFileService:
    """Orchestrate proposal, guarded dispatch, and the public result contract."""

    def __init__(
        self,
        *,
        goals: Any | None = None,
        dispatcher: Callable[..., Any] | None = None,
        jobs: Any | None = None,
        workflow_tool_provider: WorkflowToolProvider | None = None,
        authority_policy: CapabilityPolicy | None = None,
        global_authority_policy: GlobalCapabilityPolicy | None = None,
        authority_principal: TrustPrincipal | None = None,
        authority_approval: ApprovalBinding | None = None,
    ) -> None:
        self.goals = goals or goal_repository
        self.dispatcher = dispatcher or dispatch_goal_candidate
        self.jobs = jobs
        self.workflow_tool_provider = workflow_tool_provider
        self.authority_policy = authority_policy
        self.global_authority_policy = global_authority_policy
        self.authority_principal = authority_principal
        self.authority_approval = authority_approval

    request_model = GoalSnapshotToFileRequest
    adapter_type = GoalSnapshotToFileAdapter
    result_type = GoalSnapshotToFileResult

    def _candidate_for_request(
        self,
        goal: Goal | None,
        request: GoalSnapshotToFileRequest,
    ) -> GoalCandidateDecision:
        return (
            _candidate_for_missing_goal(request)
            if goal is None
            else _candidate_with_requested_revision(goal, request)
        )

    def _result_for_outcome(
        self,
        *,
        request: GoalSnapshotToFileRequest,
        candidate: GoalCandidateDecision,
        outcome: GoalOutcomeReceipt,
        receipt: dict[str, Any],
    ) -> GoalSnapshotToFileResult:
        return GoalSnapshotToFileResult(
            job_id=_text(receipt.get("job_id")) or None,
            durable_status=_text(receipt.get("durable_status")) or None,
            goal_id=candidate.goal_id,
            goal_revision=candidate.goal_revision,
            file_path=request.file_path,
            execution_status=outcome.execution_status,
            verification=outcome.verification,
            learning=outcome.learning,
            artifact_ref=outcome.artifact_ref,
            content_sha256=_text(receipt.get("content_sha256")) or None,
            output_exists=bool(receipt.get("output_exists", False)),
            workspace_contained=bool(receipt.get("workspace_contained", False)),
            goal_id_read_back=bool(receipt.get("goal_id_read_back", False)),
            reconciliation_required=bool(receipt.get("reconciliation_required", False)),
            recovery_action=_text(receipt.get("recovery_action")) or None,
            durable_failure=(
                dict(receipt["durable_failure"])
                if isinstance(receipt.get("durable_failure"), dict)
                else None
            ),
            authority_receipt=(
                dict(receipt["authority_receipt"])
                if isinstance(receipt.get("authority_receipt"), dict)
                else None
            ),
            decision_input_digest=outcome.decision_input_digest,
            strategy_delta_id=outcome.strategy_delta_id,
            strategy_delta_provenance=outcome.strategy_delta_provenance,
            evidence_refs=list(outcome.evidence_refs),
            reason=outcome.reason,
        )

    async def run(self, request: GoalSnapshotToFileRequest | dict[str, Any]) -> GoalSnapshotToFileResult:
        request = request if isinstance(request, self.request_model) else self.request_model.model_validate(request)
        goal = await self.goals.get(request.goal_id)
        candidate = self._candidate_for_request(goal, request)
        adapter = self.adapter_type(
            request,
            jobs=self.jobs,
            goals=self.goals,
            workflow_tool_provider=self.workflow_tool_provider,
            authority_policy=self.authority_policy,
            global_authority_policy=self.global_authority_policy,
            authority_principal=self.authority_principal,
            authority_approval=self.authority_approval,
        )
        outcome = await self.dispatcher(candidate, adapter=adapter)
        if not isinstance(outcome, GoalOutcomeReceipt):
            outcome = GoalOutcomeReceipt.model_validate(outcome)
        receipt = adapter.last_receipt or {}
        return self._result_for_outcome(
            request=request,
            candidate=candidate,
            outcome=outcome,
            receipt=receipt,
        )


async def run_goal_snapshot_to_file(
    request: GoalSnapshotToFileRequest | dict[str, Any],
    **kwargs: Any,
) -> GoalSnapshotToFileResult:
    """Convenience entry point for the bounded capability service."""

    return await GoalSnapshotToFileService(**kwargs).run(request)


__all__ = [
    "CAPABILITY_ID",
    "CAPABILITY_VERSION",
    "WORKFLOW_NAME",
    "WORKFLOW_TOOL_NAME",
    "GoalSnapshotToFileRequest",
    "GoalSnapshotToFileResult",
    "GoalSnapshotToFileAdapter",
    "GoalSnapshotToFileService",
    "normalize_workspace_relative_path",
    "run_goal_snapshot_to_file",
]
