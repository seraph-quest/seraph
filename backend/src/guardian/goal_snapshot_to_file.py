"""Bounded ``goal-snapshot-to-file`` execution through the governed runtime.

The goal-conditioned loop remains the decision and receipt boundary.  This
module only adapts its one capability to the existing workflow tool surface and
the canonical durable job repository.  It deliberately has no model, queue,
authorization database, or memory-learning implementation of its own.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass
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
    normalized_evidence_refs,
    stable_candidate_key,
)
from src.goals.repository import goal_repository
from src.guardian.goal_conditioned_loop import (
    build_goal_candidate_decision,
    dispatch_goal_candidate,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
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
DEFAULT_PRIORITY = 60
MAX_PRIORITY = 100
DEFAULT_DEADLINE_SECONDS = 300
MAX_DEADLINE_SECONDS = 900
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_RECONCILIATION_ACTION = "reconcile the durable job and any workspace effect before retry or cancel"


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


def _job_id(candidate: GoalCandidateDecision, request: GoalSnapshotToFileRequest) -> str:
    return "job_goal_snapshot_" + _safe_digest(
        {
            "candidate": candidate.dedupe_key,
            "owner": request.owner_principal_id,
            "service": request.service_id,
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
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.request = request
        self.jobs = jobs or durable_job_repository
        self.goals = goals or goal_repository
        self.workflow_tool_provider = workflow_tool_provider
        self.clock = clock
        self.last_receipt: dict[str, Any] | None = None

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
        if candidate.capability_id != CAPABILITY_ID or candidate.capability_version != self.request.capability_version:
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

        workflow_tool, approval_context, workflow_reason = self._resolve_workflow_tool(path)
        job_id = _job_id(candidate, self.request)
        runner_owner = f"{self.request.service_id}:{job_id}"
        declared_authority = {
            "capability_id": CAPABILITY_ID,
            "capability_version": self.request.capability_version,
            "required_grant": AuthorityGrant.CAPABILITY_EXECUTE.value,
            "principal": self.request.owner_principal_id,
            "owner_kind": "service",
            "owner_principal_id": self.request.owner_principal_id,
            "service_id": self.request.service_id,
            "session_id": self.request.session_id,
            "goal_id": candidate.goal_id,
            "goal_revision": candidate.goal_revision,
            "workflow_name": WORKFLOW_NAME,
            "workflow_tool_name": "workflow_goal_snapshot_to_file",
            "workflow_available": workflow_tool is not None,
            "workflow_blocked_reason": workflow_reason,
            "approval_context": approval_context or {},
            "permissions": {
                "workspace_relative_output_only": True,
                "allowed_step_tools": ["get_goals", "write_file"],
            },
        }
        spec = DurableJobSpec(
            identity=DurableJobIdentity(
                job_id=job_id,
                owner_kind="service",
                owner_principal_id=self.request.owner_principal_id,
                job_kind=CAPABILITY_ID,
                capability_version=self.request.capability_version,
                idempotency_scope="goal-snapshot-to-file",
                idempotency_key=candidate.dedupe_key,
            ),
            inputs={
                "goal_id": candidate.goal_id,
                "goal_revision": candidate.goal_revision,
                "file_path": path,
            },
            session_id=self.request.session_id,
            goal_id=candidate.goal_id,
            goal_revision=candidate.goal_revision,
            candidate_id=candidate.candidate_id,
            priority=self.request.priority,
            resource_claims=("workspace_read", "workspace_write"),
            declared_authority=declared_authority,
            deadline_at=self.request.deadline_at,
            max_attempts=1,
            service_id=self.request.service_id,
        )
        try:
            admission = await self.jobs.admit_job(spec)
        except DurableJobIdempotencyConflict:
            return self._blocked("idempotency_conflict", job_id=job_id)
        except Exception as exc:
            return self._blocked(f"job_admission_failed:{type(exc).__name__}", job_id=job_id)
        self._remember_projection(admission, job_id=job_id)
        admission_receipt_status = _text(admission.get("receipt", {}).get("status"))
        if admission_receipt_status == "deduped":
            return await self._replay_admission(admission, candidate, path)
        if _status(admission) == "failed":
            return self._failed("deadline_expired", job_id=job_id, durable_status="failed")

        try:
            queued = await self.jobs.queue_job(job_id)
            self._remember_projection(queued, job_id=job_id)
        except Exception as exc:
            return self._blocked(f"job_queue_failed:{type(exc).__name__}", job_id=job_id)
        if self.request.cancel_requested:
            return await self._cancel(job_id, reason="cancel_requested")

        try:
            claimed = await self.jobs.claim_job(job_id, owner=runner_owner, lease_seconds=MAX_DEADLINE_SECONDS)
            self._remember_projection(claimed, job_id=job_id)
        except Exception as exc:
            return self._blocked(f"job_claim_failed:{type(exc).__name__}", job_id=job_id)
        if _status(claimed) in {"failed", "blocked", "cancelled"}:
            return self._result_for_durable_status(_status(claimed), _text(claimed.get("failure_reason")) or "job_not_runnable", job_id=job_id)
        lease_owner, fencing_token = _lease(claimed)
        if lease_owner != runner_owner or fencing_token is None:
            return self._blocked("claim_missing_current_fence", job_id=job_id, durable_status=_status(claimed))
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
                details={"workflow_name": WORKFLOW_NAME, "reason": workflow_reason or "workflow_unavailable"},
                owner=runner_owner,
                fencing_token=fencing_token,
            )
            return await self._block_job(
                job_id,
                reason=workflow_reason or "workflow_unavailable",
                owner=runner_owner,
                fencing_token=fencing_token,
            )

        try:
            raw_result, workflow_audit = await self._invoke_workflow(
                workflow_tool,
                path,
                session_id=self.request.session_id,
                job_id=job_id,
            )
        except Exception as exc:
            if type(exc).__name__ == "ApprovalRequired":
                approval_effect = await self._record_effect(
                    job_id,
                    effect_type="workflow_invocation",
                    status="blocked",
                    details={"workflow_name": WORKFLOW_NAME, "reason": "approval_required"},
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
                details={"workflow_name": WORKFLOW_NAME, "error_type": type(exc).__name__},
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
                "workflow_name": WORKFLOW_NAME,
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
                artifact_type="goal_snapshot",
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
            reason="goal_snapshot_executed_and_verified",
        )

    def _resolve_workflow_tool(self, path: str) -> tuple[Any | None, dict[str, Any] | None, str | None]:
        if self.workflow_tool_provider is not None:
            tool = self.workflow_tool_provider(WORKFLOW_NAME)
            if tool is None:
                return None, None, "governed_workflow_unavailable"
            context = self._approval_context(tool, path)
            if context is None:
                return None, None, "workflow_approval_context_unavailable"
            if context.get("workflow_name") not in {None, WORKFLOW_NAME}:
                return None, None, "workflow_name_mismatch"
            if not {"get_goals", "write_file"}.issubset(set(context.get("step_tools", []))):
                return None, None, "workflow_step_tools_not_allowed"
            return tool, context, None
        try:
            from src.agent.factory import get_tools
            from src.workflows.manager import workflow_manager

            workflow = workflow_manager.get_workflow(WORKFLOW_NAME)
            if workflow is None or not workflow.enabled:
                return None, None, "workflow_not_loaded_or_disabled"
            tools = get_tools()
            tool = next((item for item in tools if getattr(item, "name", "") == workflow.tool_name), None)
            if tool is None:
                return None, None, "governed_workflow_tool_unavailable"
            context = self._approval_context(tool, path)
            if context is None:
                return None, None, "workflow_approval_context_unavailable"
            expected = {"get_goals", "write_file"}
            actual = {str(item) for item in context.get("step_tools", [])} if isinstance(context, dict) else set()
            if not expected.issubset(actual):
                return None, None, "workflow_step_tools_not_allowed"
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
    ) -> tuple[Any, dict[str, Any] | None]:
        principal = TrustPrincipal(
            principal_id=self.request.owner_principal_id,
            principal_type=PrincipalType.SERVICE,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
            session_id=session_id,
            job_id=job_id,
        )
        call = partial(tool, file_path=path, sanitize_inputs_outputs=True)
        if self.workflow_tool_provider is not None:
            # The injectable boundary is deliberately synchronous for tests;
            # the production provider below runs wrappers off the event loop.
            tokens = set_runtime_context(
                session_id,
                get_current_approval_mode(),
                trust_principal=principal,
            )
            try:
                result = call()
            finally:
                reset_runtime_context(tokens)
        else:
            tokens = set_runtime_context(
                session_id,
                get_current_approval_mode(),
                trust_principal=principal,
            )
            try:
                run_context = contextvars.copy_context()
            finally:
                reset_runtime_context(tokens)
            result = await asyncio.to_thread(run_context.run, call)
        audit_payload = self._audit_result_payload(tool, path, result)
        return result, audit_payload

    @staticmethod
    def _audit_result_payload(tool: Any, path: str, result: Any) -> dict[str, Any] | None:
        current = tool
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            hook = getattr(current, "get_audit_result_payload", None)
            if callable(hook):
                payload = hook({"file_path": path}, result)
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
        job_id = _text(projection.get("job_id")) or _job_id(candidate, self.request)
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
                    evidence_refs=[f"job:{job_id}", f"readback:{digest}"],
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
    ) -> None:
        self.goals = goals or goal_repository
        self.dispatcher = dispatcher or dispatch_goal_candidate
        self.jobs = jobs
        self.workflow_tool_provider = workflow_tool_provider

    async def run(self, request: GoalSnapshotToFileRequest | dict[str, Any]) -> GoalSnapshotToFileResult:
        request = request if isinstance(request, GoalSnapshotToFileRequest) else GoalSnapshotToFileRequest.model_validate(request)
        goal = await self.goals.get(request.goal_id)
        candidate = (
            _candidate_for_missing_goal(request)
            if goal is None
            else _candidate_with_requested_revision(goal, request)
        )
        adapter = GoalSnapshotToFileAdapter(
            request,
            jobs=self.jobs,
            goals=self.goals,
            workflow_tool_provider=self.workflow_tool_provider,
        )
        outcome = await self.dispatcher(candidate, adapter=adapter)
        if not isinstance(outcome, GoalOutcomeReceipt):
            outcome = GoalOutcomeReceipt.model_validate(outcome)
        receipt = adapter.last_receipt or {}
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
            evidence_refs=list(outcome.evidence_refs),
            reason=outcome.reason,
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
    "GoalSnapshotToFileRequest",
    "GoalSnapshotToFileResult",
    "GoalSnapshotToFileAdapter",
    "GoalSnapshotToFileService",
    "normalize_workspace_relative_path",
    "run_goal_snapshot_to_file",
]
