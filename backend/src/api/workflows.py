"""Workflows API — list, toggle, reload reusable multi-step workflows."""

from collections import defaultdict
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
import stat
import tempfile
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import col, select

from config.settings import settings
from src.api.capabilities import (
    _recommended_tool_policy_mode,
    _require_authenticated_capability_operator,
)
from src.api.chat import (
    _begin_rest_revocation_watch,
    _end_rest_revocation_watch,
    _ensure_rest_authorized,
)
from src.agent.session import session_manager
from src.agent.factory import get_base_tools_and_active_skills
from src.artifacts.registry import artifact_id_for, artifact_records_from_paths
from src.approval.repository import fingerprint_tool_call
from src.approval.repository import approval_repository
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.repository import audit_repository
from src.audit.runtime import log_integration_event
from src.auth.cancellation import RuntimeRevokedError, assert_runtime_not_revoked
from src.auth.service import bind_operator_principal
from src.db.engine import get_session
from src.db.models import AuditEvent, Goal, GuardianDecisionPacket, GuardianSourceWatch
from src.extensions.registry import ExtensionRegistry
from src.extensions.registry import default_manifest_roots_for_workspace
from src.extensions.workflow_runtimes import list_workflow_runtime_inventory
from src.observer.manager import context_manager
from src.extensions.workspace_package import save_workspace_contribution
from src.execution.repo_sandbox import (
    RepoSandboxError,
    RepoSandboxLimits,
    RepoSandboxJob,
    RootlessDockerRepoSandbox,
    limits_digest,
)
from src.goals.contracts import GoalCandidateAction, GoalCandidateDecision, GoalOutcomeReceipt
from src.guardian.goal_conditioned_loop import (
    _OUTCOME_EVENT,
    _outcome_receipt_details,
    _persist_receipt_compat,
    _safe_evidence_refs,
)
from src.goals.repository import deserialize_success_criterion, goal_repository
from src.tools.policy import get_current_tool_policy_mode
from src.workflows.loader import parse_workflow_content
from src.workflows.manager import (
    _workflow_canonical_lease_owner,
    approval_context_requires_tracked_lineage,
    workflow_manager,
)
from src.workflows.durable_state import _safe_operator_recovery_target, workflow_state_repository
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobSpec,
    DurableJobError,
    DurableJobTransitionError,
    _canonical_reconciliation_receipt,
    durable_job_repository,
    durable_lease_id,
)
from src.workflows.run_identity import build_workflow_run_identity, parse_workflow_run_identity
from src.workspace import WorkspaceStateClass, canonical_workspace_registry

router = APIRouter()
logger = logging.getLogger(__name__)

_WORKFLOW_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


class UpdateWorkflowRequest(BaseModel):
    enabled: bool


class WorkflowResumePlanRequest(BaseModel):
    step_id: str | None = None
    operator_context: dict[str, Any] | None = None


class WorkflowRunControlRequest(BaseModel):
    action: str
    target: str | None = None
    step_id: str | None = None
    owner: str | None = None
    operator_context: dict[str, Any] | None = None
    action_handle: dict[str, Any] | None = None


class WorkflowDraftRequest(BaseModel):
    content: str
    file_name: str | None = None


class RepoChangePreviewRequest(BaseModel):
    """Operator request for the fixed repository change capability.

    The request contains references and bounded intent only.  The backend
    resolves the candidate, patch bytes, execution owner, image, and runtime
    limits from canonical state before admitting a durable job.
    """

    model_config = {"extra": "forbid"}

    goal_id: str = Field(min_length=1, max_length=160)
    goal_revision: int = Field(ge=1)
    candidate_id: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=160)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    repository_path: str = Field(min_length=1, max_length=512)
    patch_artifact_id: str = Field(min_length=1, max_length=160)
    patch_sha256: str = Field(min_length=64, max_length=64)
    allowed_paths: list[str] = Field(min_length=1, max_length=64)
    test_args: list[str] = Field(min_length=1, max_length=16)
    priority: int = Field(default=50, ge=0, le=100)
    deadline_seconds: int = Field(default=180, ge=30, le=180)


class RepoChangeResumeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    approval_id: str = Field(min_length=1, max_length=160)


class RepoChangeCancelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str = Field(default="operator_requested_stop", min_length=1, max_length=160)


class RepoChangeRetryRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reconciliation_receipt: dict[str, Any]


_WORKFLOW_CONTROL_ACTIONS = frozenset(
    {
        "pause",
        "resume",
        "retry",
        "repair",
        "branch",
        "compare",
        "revoke",
        "quarantine",
        "handoff",
        "rollback",
        "audit",
        "replay",
        "runbook",
    }
)
_WORKFLOW_REPLAY_ACTIONS = frozenset({"resume", "retry", "repair", "branch", "replay"})
# An unbound run may still be inspected through the existing audit action.  All
# actions that can affect recovery state require a durable user ownership
# binding; service-owned runs are never operator-controllable.
_WORKFLOW_INSPECTION_ACTIONS = frozenset({"audit"})
_WORKFLOW_IDENTITY_BOUND_ACTIONS = frozenset(
    _WORKFLOW_CONTROL_ACTIONS - _WORKFLOW_INSPECTION_ACTIONS
)
_WORKFLOW_REPLAY_BLOCK_REASONS = frozenset(
    {
        "approval_context_changed",
        "approval_context_missing",
        "workflow_disabled",
        "workflow_unavailable",
        "pending_approval",
        "secret_ref_surface",
        "secret_bearing_boundary",
        "high_risk_requires_manual_reentry",
        "durable_projection_missing",
        "workflow_approval_ambiguous",
        "workflow_candidate_stale",
        "workflow_candidate_unavailable",
    }
)
_WORKFLOW_SAFE_REFUSAL_CODES = _WORKFLOW_REPLAY_BLOCK_REASONS | {
    "workflow_control_refused",
    "workflow_control_action_unsupported",
    "workflow_control_failed",
    "workflow_control_lease_blocked",
    "workflow_recovery_blocked",
    "workflow_transition_blocked",
    "workflow_transition_unavailable",
    "workflow_control_state_unavailable",
    "workflow_replay_blocked",
    "workflow_approval_gate_blocked",
    "active_lease_owned_by_another_worker",
    "active_owner_lease_required",
    "revision_mismatch",
    "receiver_authority_not_accepted",
    "missing_delegated_artifact_review_approval",
    "workflow_run_not_found",
    "workflow_run_not_persisted",
    "workflow_checkpoint_not_found",
    "workflow_checkpoint_not_reusable",
    "workflow_resume_plan_refused",
    "workflow_owner_mismatch",
    "lease_mismatch",
    "transition_binding_missing",
    "transition_owner_mismatch",
    "workflow_control_fence_blocked",
    "workflow_action_handle_invalid",
    "workflow_action_handle_mismatch",
    "workflow_identity_binding_missing",
    "workflow_identity_binding_mismatch",
    "workflow_goal_unavailable",
    "stale_goal_revision",
    "workflow_criterion_stale",
    "workflow_plan_revision_stale",
    "workflow_approval_ambiguous",
    "workflow_candidate_stale",
    "workflow_candidate_unavailable",
}
_WORKFLOW_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_WORKFLOW_SAFE_ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{24}$")
_WORKFLOW_SAFE_ARTIFACT_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$", re.IGNORECASE)
_WORKFLOW_ARTIFACT_SECRET_PARTS = frozenset(
    {
        ".aws",
        ".azure",
        ".config",
        ".docker",
        ".gnupg",
        ".ssh",
        "credential",
        "credentials",
        "private",
        "secret",
        "secrets",
        "token",
        "tokens",
        "vault",
    }
)
_WORKFLOW_ARTIFACT_SECRET_NAMES = frozenset(
    {
        ".env",
        ".env.dev",
        ".env.local",
        ".env.production",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "google_credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "private_key",
    }
)
_WORKFLOW_ARTIFACT_SECRET_NAME_TOKENS = (
    "api-key",
    "api_key",
    "apikey",
    "credential",
    "password",
    "private",
    "secret",
    "token",
)
_WORKFLOW_ARTIFACT_SECRET_SUFFIXES = (".key", ".p12", ".pem", ".pfx")


def _workflow_identity_digest(run_identity: str) -> str:
    return hashlib.sha256(str(run_identity).encode("utf-8", errors="replace")).hexdigest()[:16]


def _safe_workflow_token(value: Any, *, fallback: str) -> str:
    candidate = str(value or "").strip()
    if _WORKFLOW_SAFE_TOKEN_RE.fullmatch(candidate):
        return candidate
    return fallback


def _safe_workflow_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_workflow_step_id(value: Any) -> str:
    """Preserve distinct checkpoint identities without exposing raw input."""
    candidate = str(value or "").strip()
    if not candidate:
        return "redacted_workflow_step"
    digest = hashlib.sha256(candidate.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"redacted_workflow_step_{digest}"


def _workflow_requested_step_id(run: dict[str, Any], value: Any) -> str | None:
    """Resolve a cockpit checkpoint handle back to its trusted step id.

    The API projection only exposes a digest, while the durable transition and
    WorkflowTool restore path retain the validated raw id.  Resolution is
    scoped to this run and must be unambiguous before a control can proceed.
    """
    requested = str(value or "").strip()
    if not requested:
        return None
    candidates: list[str] = []
    for candidate in run.get("checkpoint_candidates") or []:
        if isinstance(candidate, dict):
            step_id = str(candidate.get("step_id") or "").strip()
            if step_id and step_id not in candidates:
                candidates.append(step_id)
    for step in run.get("step_records") or []:
        if isinstance(step, dict):
            step_id = str(step.get("id") or "").strip()
            if step_id and step_id not in candidates:
                candidates.append(step_id)
    for field_name in ("resume_from_step", "last_completed_step_id"):
        step_id = str(run.get(field_name) or "").strip()
        if step_id and step_id not in candidates:
            candidates.append(step_id)
    for step_id_value in run.get("continued_error_steps") or []:
        step_id = str(step_id_value or "").strip()
        if step_id and step_id not in candidates:
            candidates.append(step_id)
    if requested in candidates:
        return requested
    if requested.startswith("redacted_workflow_step_"):
        matches = [step_id for step_id in candidates if _safe_workflow_step_id(step_id) == requested]
        if len(matches) == 1:
            return matches[0]
    return requested


def _validate_workflow_action_handle(
    handle: Any,
    *,
    run: dict[str, Any],
    run_identity: str,
    action: str,
) -> str | None:
    """Validate a safe cockpit handle before resolving it to trusted state."""
    if not isinstance(handle, dict):
        raise HTTPException(status_code=422, detail="workflow_action_handle_invalid")
    if handle.get("kind") != "workflow_control" or handle.get("requires_live_control") is not True:
        raise HTTPException(status_code=422, detail="workflow_action_handle_invalid")
    handle_action = _safe_workflow_action(handle.get("action"))
    if handle_action != action:
        raise HTTPException(status_code=409, detail="workflow_action_handle_mismatch")
    handle_run_identity = str(handle.get("run_identity") or "").strip()
    if handle_run_identity not in {run_identity, _safe_workflow_identity(run_identity)}:
        raise HTTPException(status_code=409, detail="workflow_action_handle_mismatch")
    expected_thread = _safe_workflow_token(run.get("thread_id") or run.get("session_id"), fallback="")
    handle_thread = str(handle.get("thread_id") or "").strip()
    if handle_thread and expected_thread and handle_thread != expected_thread:
        raise HTTPException(status_code=409, detail="workflow_action_handle_mismatch")
    handle_step = str(handle.get("step_id") or "").strip()
    if handle_step and not handle_step.startswith("redacted_workflow_step_"):
        raise HTTPException(status_code=422, detail="workflow_action_handle_invalid")
    return _workflow_requested_step_id(run, handle_step) if handle_step else None


def _workflow_operator_owner(principal_id: str, session_id: str) -> str:
    """Use a stable per-operator/session lease owner without leaking identity."""
    principal_digest = hashlib.sha256(principal_id.encode("utf-8", errors="replace")).hexdigest()[:16]
    session_digest = hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"operator:{principal_digest}:{session_digest}"


def _workflow_owner_is_bound(run: dict[str, Any], principal_id: str) -> bool:
    """Require a durable user owner before exposing or changing recovery state."""
    owner_kind = str(run.get("owner_kind") or "").strip().lower()
    owner_principal_id = str(run.get("owner_principal_id") or "").strip()
    return (
        owner_kind == "user"
        and bool(owner_principal_id)
        and owner_principal_id == principal_id
    )


def _workflow_conversation_id(run: dict[str, Any]) -> str:
    """Return the durable conversation scope for a workflow run."""
    return str(
        _workflow_run_identity_value(
            run,
            "conversation_id",
            "approval_conversation_id",
            "session_id",
            "thread_id",
        )
        or ""
    ).strip()


def _workflow_operator_session_id(run: dict[str, Any]) -> str:
    """Return the durable browser authentication owner for a run."""
    return str(
        _workflow_run_identity_value(
            run,
            "operator_session_id",
            "approval_owner_operator_session_id",
            "approval_owner_auth_session_id",
        )
        or ""
    ).strip()


def _workflow_recovery_identity_detail(
    *,
    run: dict[str, Any],
    run_identity: str,
    operator_session_id: str,
) -> str | None:
    """Require both conversation and auth-session bindings to be exact.

    ``run_identity`` encodes the execution conversation.  The authenticated
    browser session is persisted separately on the run and must match the
    current operator session; equating the two would reject valid recovery
    after a browser refresh or session rotation.
    """
    try:
        identity_conversation, _tool_name, _fingerprint, _discriminator = parse_workflow_run_identity(
            run_identity
        )
    except (TypeError, ValueError):
        return "workflow_identity_binding_missing"
    conversation_id = _workflow_conversation_id(run)
    if not conversation_id or not identity_conversation or conversation_id != identity_conversation:
        return "workflow_identity_binding_mismatch"
    bound_operator_session = _workflow_operator_session_id(run)
    if not bound_operator_session or not operator_session_id or bound_operator_session != operator_session_id:
        return "workflow_owner_mismatch"
    return None


def _workflow_identity_binding_detail(
    *,
    run: dict[str, Any],
    run_identity: str,
    operator_context: dict[str, Any] | None,
    require_plan_revision: bool = False,
) -> str | None:
    """Require the caller to echo the server-owned goal/run identity exactly."""
    context = operator_context if isinstance(operator_context, dict) else {}
    expected_run_identity = str(run.get("run_identity") or run_identity).strip()
    supplied_run_identity = str(context.get("workflow_run_identity") or "").strip()
    if not expected_run_identity or not supplied_run_identity:
        return "workflow_identity_binding_missing"
    if supplied_run_identity != expected_run_identity:
        return "workflow_identity_binding_mismatch"

    required_fields = ("goal_id", "criterion_id", "goal_revision")
    for field_name in required_fields:
        expected = run.get(field_name)
        supplied = context.get(field_name)
        if expected is None or supplied is None:
            return "workflow_identity_binding_missing"
        if field_name == "goal_revision":
            expected_revision = _positive_json_integer(expected)
            supplied_revision = _positive_json_integer(supplied)
            if expected_revision is None or supplied_revision is None:
                return "workflow_identity_binding_missing"
            if expected_revision != supplied_revision:
                return "workflow_identity_binding_mismatch"
        elif str(expected).strip() != str(supplied).strip():
            return "workflow_identity_binding_mismatch"

    expected_candidate_id = run.get("candidate_id")
    expected_candidate = str(expected_candidate_id or "").strip()
    supplied_candidate = str(context.get("candidate_id") or "").strip()
    if expected_candidate != supplied_candidate:
        if expected_candidate and supplied_candidate:
            return "workflow_identity_binding_mismatch"
        return "workflow_identity_binding_missing"

    expected_plan_revision = run.get("plan_revision")
    if require_plan_revision and expected_plan_revision is None:
        return "workflow_identity_binding_missing"
    if expected_plan_revision is not None:
        supplied_plan_revision = context.get("plan_revision")
        if supplied_plan_revision is None:
            return "workflow_identity_binding_missing"
        expected_revision = _positive_json_integer(expected_plan_revision)
        supplied_revision = _positive_json_integer(supplied_plan_revision)
        if expected_revision is None or supplied_revision is None:
            return "workflow_identity_binding_missing"
        if expected_revision != supplied_revision:
            return "workflow_identity_binding_mismatch"
    return None


def _positive_json_integer(value: Any) -> int | None:
    """Accept only positive JSON integer values for authority revisions."""
    if type(value) is not int or value <= 0:
        return None
    return value


def _workflow_authority_budget_microusd(run: dict[str, Any]) -> int | None:
    authority = run.get("declared_authority")
    if not isinstance(authority, dict):
        authority = run.get("approval_context")
    if not isinstance(authority, dict):
        return None
    value: Any = None
    found = False
    for field_name in ("budget_microusd", "max_budget_microusd", "owner_cost_budget_microusd"):
        if field_name in authority:
            value = authority[field_name]
            found = True
            break
    if not found and isinstance(authority.get("budget"), dict):
        budget = authority["budget"]
        for field_name in ("microusd", "max_microusd", "amount_microusd"):
            if field_name in budget:
                value = budget[field_name]
                found = True
                break
    if not found or value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _workflow_canonical_plan_revision(goal: Any, *, current_goal_revision: int) -> int | None:
    """Resolve the plan revision from canonical goal/plan state.

    Goal currently stores its criterion and plan under the same compare-and-set
    revision.  A future dedicated plan record may expose ``plan_revision``;
    until then, the goal revision is the authoritative plan revision.  Caller
    context is never used as a source of truth here.
    """
    explicit_revision = getattr(goal, "plan_revision", None)
    if explicit_revision is not None:
        return _positive_json_integer(explicit_revision)
    return _positive_json_integer(current_goal_revision)


async def _workflow_current_goal_binding_detail(run: dict[str, Any]) -> str | None:
    """Check a typed workflow identity against current canonical goal state."""
    goal_id = str(run.get("goal_id") or "").strip()
    criterion_id = str(run.get("criterion_id") or "").strip()
    expected_revision = _positive_json_integer(run.get("goal_revision"))
    expected_plan_revision = _positive_json_integer(run.get("plan_revision"))
    if expected_revision is None or expected_plan_revision is None:
        return "workflow_identity_binding_missing"
    if not goal_id or not criterion_id:
        return "workflow_identity_binding_missing"
    try:
        goal = await goal_repository.get(goal_id)
    except Exception:
        logger.exception("Canonical goal lookup failed before workflow recovery")
        return "workflow_goal_unavailable"
    if goal is None or str(getattr(goal, "id", goal_id) or "").strip() != goal_id:
        return "workflow_goal_unavailable"
    current_revision = _positive_json_integer(getattr(goal, "revision", None))
    if current_revision is None:
        return "workflow_goal_unavailable"
    if current_revision != expected_revision:
        return "stale_goal_revision"

    criterion = deserialize_success_criterion(goal)
    if criterion is None:
        raw_criterion = getattr(goal, "success_criterion", None)
        if isinstance(raw_criterion, dict):
            criterion_id_value = raw_criterion.get("criterion_id") or raw_criterion.get("id")
        else:
            criterion_id_value = getattr(raw_criterion, "criterion_id", None)
    else:
        criterion_id_value = criterion.criterion_id
    if str(criterion_id_value or "").strip() != criterion_id:
        return "workflow_criterion_stale"

    current_plan_revision = _workflow_canonical_plan_revision(
        goal,
        current_goal_revision=current_revision,
    )
    if current_plan_revision is None or current_plan_revision != expected_plan_revision:
        return "workflow_plan_revision_stale"

    # A run carrying a candidate identity must still point at the durable
    # goal-loop candidate receipt for this exact canonical goal revision and
    # criterion.  Candidate ids are part of the mutation authority boundary;
    # accepting a stale id would let a run mutate the current goal after its
    # decision inputs have changed.
    if "candidate_id" in run and run.get("candidate_id") is not None:
        candidate_id = str(run.get("candidate_id") or "").strip()
        if not candidate_id:
            return "workflow_candidate_stale"
        try:
            candidate_events = await audit_repository.list_events(limit=500)
        except Exception:
            logger.exception("Canonical goal-loop candidate lookup failed before workflow recovery")
            return "workflow_candidate_unavailable"
        matching_candidates: list[dict[str, Any]] = []
        for event in candidate_events:
            if event.get("event_type") != "goal_loop_candidate":
                continue
            details = event.get("details")
            if not isinstance(details, dict):
                continue
            if (
                str(details.get("candidate_id") or "").strip() == candidate_id
                and str(details.get("goal_id") or "").strip() == goal_id
                and type(details.get("goal_revision")) is int
                and details.get("goal_revision") == expected_revision
                and str(details.get("criterion_id") or "").strip() == criterion_id
            ):
                matching_candidates.append(details)
        if len(matching_candidates) != 1:
            return "workflow_candidate_stale"
    return None


def _safe_workflow_action(value: Any) -> str:
    candidate = str(value or "").strip().lower().replace("-", "_")
    return candidate if candidate in _WORKFLOW_CONTROL_ACTIONS else "unsupported"


def _safe_workflow_refusal_detail(value: Any, *, fallback: str = "workflow_control_refused") -> str:
    candidate = str(value or "").strip()
    if candidate in _WORKFLOW_SAFE_REFUSAL_CODES:
        return candidate
    if candidate.startswith("workflow_replay_blocked:"):
        reason = candidate.partition(":")[2]
        if reason in _WORKFLOW_REPLAY_BLOCK_REASONS:
            return candidate
    return fallback


def _workflow_replay_block_detail(reason: Any) -> str:
    safe_reason = str(reason or "").strip()
    if safe_reason not in _WORKFLOW_REPLAY_BLOCK_REASONS:
        safe_reason = "workflow_replay_blocked"
        return safe_reason
    return f"workflow_replay_blocked:{safe_reason}"


def _safe_workflow_http_detail(status_code: int, *, fallback: str = "workflow_control_refused") -> str:
    if status_code == 404:
        return "workflow_checkpoint_not_found"
    if status_code == 409:
        return "workflow_recovery_blocked"
    if status_code == 423:
        return "workflow_control_lease_blocked"
    return fallback


def _safe_workflow_receipt(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    status = str(value.get("status") or "unavailable")
    if status not in {"acquired", "blocked", "ready", "recorded", "deduped", "accepted"}:
        status = "unavailable"
    receipt: dict[str, Any] = {"status": status}
    action = _safe_workflow_action(value.get("action"))
    if action != "unsupported":
        receipt["action"] = action
    target = value.get("target")
    if target is not None:
        safe_target = _safe_operator_recovery_target(str(target))
        receipt["target"] = safe_target["target"]
        receipt["target_digest"] = safe_target["target_digest"]
    for field_name in ("external_action_allowed", "enabled", "requires_fresh_run", "operator_visible"):
        if field_name in value:
            receipt[field_name] = bool(value[field_name])
    for field_name in ("expected_revision", "actual_revision", "revision"):
        if value.get(field_name) is not None:
            try:
                receipt[field_name] = int(value[field_name])
            except (TypeError, ValueError):
                pass
    owner = value.get("owner") or value.get("lease_owner")
    if owner:
        receipt["owner_digest"] = _workflow_identity_digest(str(owner))
    lease_id = value.get("lease_id")
    if lease_id:
        receipt["lease_id_digest"] = _workflow_identity_digest(str(lease_id))
    elif value.get("lease_id_digest"):
        receipt["lease_id_digest"] = str(value["lease_id_digest"])
    transition_key = value.get("transition_key")
    if transition_key:
        receipt["transition_key_digest"] = _workflow_identity_digest(str(transition_key))
    if value.get("transition_revision") is not None:
        try:
            receipt["transition_revision"] = int(value["transition_revision"])
        except (TypeError, ValueError):
            pass
    if value.get("fence_binding"):
        receipt["fence_binding"] = _safe_workflow_token(
            value.get("fence_binding"), fallback="fence_bound"
        )
    revision_value = value.get("actual_revision", value.get("revision"))
    if revision_value is not None:
        receipt["revision_digest"] = _workflow_identity_digest(str(revision_value))
    blocked_reason = value.get("blocked_reason")
    if blocked_reason:
        receipt["blocked_reason"] = _safe_workflow_refusal_detail(
            blocked_reason,
            fallback="workflow_control_refused",
        )
    return receipt


def _safe_workflow_identity(value: Any, *, fallback: str = "workflow") -> str:
    candidate = str(value or "").strip()
    return _safe_workflow_token(candidate, fallback=f"{fallback}:{_workflow_identity_digest(candidate)}")


def _safe_workflow_artifact_path(value: Any) -> str | None:
    """Return a canonical workspace artifact path without exposing host paths."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().replace("\\", "/")
    if not candidate or len(candidate) > 512 or "\x00" in candidate:
        return None
    parts = candidate.split("/")
    if (
        candidate.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or any(not (char.isalnum() or char in " ./_-") for char in candidate)
    ):
        return None
    lower_parts = {part.lower() for part in parts}
    file_name = parts[-1].lower()
    if (
        lower_parts & _WORKFLOW_ARTIFACT_SECRET_PARTS
        or file_name in _WORKFLOW_ARTIFACT_SECRET_NAMES
        or file_name.endswith(_WORKFLOW_ARTIFACT_SECRET_SUFFIXES)
        or any(token in file_name for token in _WORKFLOW_ARTIFACT_SECRET_NAME_TOKENS)
    ):
        return None
    try:
        state_class = canonical_workspace_registry(settings.workspace_dir).classify_path(candidate)
    except Exception:
        return None
    if state_class is not WorkspaceStateClass.CANONICAL:
        return None
    return candidate


def _safe_workflow_artifact_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _WORKFLOW_SAFE_ARTIFACT_DIGEST_RE.fullmatch(candidate):
        return None
    if candidate.lower().startswith("sha256:"):
        candidate = candidate[7:]
    return candidate.lower()


def _safe_workflow_artifact_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _WORKFLOW_SAFE_ARTIFACT_ID_RE.fullmatch(candidate) else None


def _safe_workflow_artifact_projection(value: Any) -> dict[str, Any]:
    """Project only managed artifact identity, digest, and logical path."""
    if not isinstance(value, dict):
        return {"artifact_paths": [], "artifact_registry": []}

    raw_registry = value.get("artifact_registry")
    raw_paths = value.get("artifact_paths")
    candidates: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw_registry, list):
        for item in raw_registry:
            if not isinstance(item, dict):
                continue
            path = _safe_workflow_artifact_path(item.get("file_path") or item.get("path"))
            if path is not None:
                candidates.append((path, item))
    if isinstance(raw_paths, list):
        for raw_path in raw_paths:
            path = _safe_workflow_artifact_path(raw_path)
            if path is not None:
                candidates.append((path, {}))

    workflow_name = _safe_workflow_token(value.get("workflow_name"), fallback="workflow")
    run_identity = _safe_workflow_identity(value.get("run_identity") or value.get("id"))
    safe_registry: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for path, source in candidates:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        digest = None
        for field_name in ("content_sha256", "artifact_digest", "digest"):
            digest = _safe_workflow_artifact_digest(source.get(field_name))
            if digest is not None:
                break
        artifact_id = _safe_workflow_artifact_id(source.get("artifact_id"))
        if artifact_id is None:
            artifact_id = artifact_id_for(
                file_path=path,
                artifact_type="workspace_file",
                producer=f"workflow:{workflow_name}",
                run_id=run_identity,
                content_sha256=digest,
            )
        safe_registry.append(
            {
                "artifact_id": artifact_id,
                "file_path": path,
                "content_sha256": digest,
            }
        )
    return {
        "artifact_paths": [record["file_path"] for record in safe_registry],
        "artifact_registry": safe_registry,
    }


def _is_typed_workflow_run(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        return int(value.get("record_schema_version") or 0) >= 2
    except (TypeError, ValueError, OverflowError):
        return False


def _canonical_lease_projection(value: dict[str, Any]) -> dict[str, Any]:
    raw_lease = value.get("lease")
    if isinstance(raw_lease, dict):
        lease = dict(raw_lease)
    else:
        lease = {
            "owner": value.get("lease_owner"),
            "expires_at": value.get("lease_expires_at"),
            "fencing_token": value.get("fencing_token"),
        }
    try:
        if lease.get("fencing_token") is not None:
            lease["fencing_token"] = int(lease["fencing_token"])
    except (TypeError, ValueError, OverflowError):
        lease["fencing_token"] = 0
    return lease


def _safe_canonical_receipt_projection(
    receipts: Any,
    *,
    kind: str,
) -> list[dict[str, Any]]:
    """Expose bounded typed receipt structure without payload or secret text."""
    if not isinstance(receipts, list):
        return []
    safe: list[dict[str, Any]] = []
    for item in receipts[-100:]:
        if not isinstance(item, dict):
            continue
        receipt: dict[str, Any] = {"kind": kind}
        for field_name in (
            "status",
            "receipt_kind",
            "effect_type",
            "artifact_type",
            "safe",
            "reconciled",
            "reconciliation_status",
            "exists",
            "operator_visible",
        ):
            if field_name in item and isinstance(item[field_name], (str, bool)):
                receipt[field_name] = item[field_name]
        for field_name in ("recorded_at", "observed_at", "recorded_at"):
            if isinstance(item.get(field_name), str):
                receipt[field_name] = item[field_name]
        for field_name in ("fencing_token", "size_bytes"):
            if item.get(field_name) is not None:
                try:
                    receipt[field_name] = max(0, int(item[field_name]))
                except (TypeError, ValueError, OverflowError):
                    continue
        for field_name in ("checkpoint_id", "artifact_id", "effect_id"):
            if item.get(field_name):
                receipt[f"{field_name}_digest"] = _workflow_identity_digest(str(item[field_name]))
        for field_name in ("state_digest", "content_sha256", "target_digest", "readback_digest"):
            digest = _safe_workflow_artifact_digest(item.get(field_name))
            if digest is not None:
                receipt[field_name] = digest
        if kind == "artifact" and isinstance(item.get("file_path"), str):
            path = _safe_workflow_artifact_path(item["file_path"])
            if path is not None:
                receipt["file_path"] = path
        safe.append(receipt)
    return safe


def _canonical_workflow_projection_input(value: Any) -> dict[str, Any] | None:
    """Adapt a typed job readback to the existing cockpit projection shape."""
    if not _is_typed_workflow_run(value):
        return value if isinstance(value, dict) else None
    raw = dict(value)
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {
        "kind": raw.get("owner_kind"),
        "principal_id": raw.get("owner_principal_id"),
        "service_id": raw.get("service_id"),
    }
    lease = _canonical_lease_projection(raw)
    try:
        current_fence = int(lease.get("fencing_token") or 0)
        lease = {
            **lease,
            "lease_id": durable_lease_id(
                raw.get("job_id") or raw.get("run_identity"),
                current_fence,
            ),
            "revision": int(raw.get("revision") or 0),
        }
    except (TypeError, ValueError, OverflowError):
        # The typed repository validates job identity/fence before exposing a
        # row. Preserve malformed data so recovery rejects it explicitly
        # rather than inventing an alternate lease identity.
        lease = {**lease, "lease_id": None, "revision": None}
    checkpoints = raw.get("checkpoints")
    if not isinstance(checkpoints, list):
        checkpoints = raw.get("checkpoint_receipts")
    checkpoints = checkpoints if isinstance(checkpoints, list) else []
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = raw.get("artifact_receipts")
    artifacts = artifacts if isinstance(artifacts, list) else []
    effects = raw.get("effects")
    if not isinstance(effects, list):
        effects = raw.get("effect_receipts")
    effects = effects if isinstance(effects, list) else []
    checkpoint_context: dict[str, Any] = {}
    step_records: list[dict[str, Any]] = []
    artifact_paths: list[str] = []
    continued_error_steps: list[str] = []
    for receipt in checkpoints[-100:]:
        if not isinstance(receipt, dict):
            continue
        payload = receipt.get("payload") if isinstance(receipt.get("payload"), dict) else {}
        step_id = str(payload.get("step_id") or receipt.get("checkpoint_id") or "").strip()
        if step_id.startswith("step:"):
            step_id = step_id[5:]
        state = payload.get("state")
        if isinstance(state, dict):
            checkpoint_context[step_id] = state
        if not step_id:
            continue
        raw_status = str(payload.get("status") or receipt.get("status") or "unknown")
        paths = payload.get("artifact_paths") if isinstance(payload.get("artifact_paths"), list) else []
        paths = [path for path in paths if isinstance(path, str) and path.strip()]
        for path in paths:
            if path not in artifact_paths:
                artifact_paths.append(path)
        step = {
            "id": step_id,
            "index": _safe_workflow_count(payload.get("step_index")) or len(step_records) + 1,
            "tool": str(payload.get("tool") or "workflow_step"),
            "status": raw_status,
            "arguments": state.get("arguments", {}) if isinstance(state, dict) else {},
            "result": state.get("result") if isinstance(state, dict) else None,
            "artifact_paths": paths,
            "result_summary": payload.get("result_summary"),
            "error_kind": payload.get("error_kind"),
            "error_summary": payload.get("error_summary"),
        }
        step_records.append(step)
        if raw_status in {"failed", "continued_error"}:
            continued_error_steps.append(step_id)
    for receipt in artifacts:
        if isinstance(receipt, dict):
            path = receipt.get("file_path")
            if isinstance(path, str) and path.strip() and path not in artifact_paths:
                artifact_paths.append(path)
    updated_at = raw.get("updated_at") or raw.get("started_at")
    started_at = raw.get("started_at") or updated_at
    status = str(raw.get("status") or "unknown")
    authority = raw.get("declared_authority")
    if not isinstance(authority, dict):
        authority = raw.get("approval_context") if isinstance(raw.get("approval_context"), dict) else {}
    conversation_id = (
        raw.get("conversation_id")
        if raw.get("conversation_id") is not None
        else authority.get("conversation_id")
        or authority.get("approval_conversation_id")
        or raw.get("session_id")
    )
    operator_session_id = (
        raw.get("operator_session_id")
        if raw.get("operator_session_id") is not None
        else authority.get("operator_session_id")
        or authority.get("approval_owner_operator_session_id")
        or authority.get("approval_owner_auth_session_id")
    )
    typed_receipts = {
        "checkpoints": _safe_canonical_receipt_projection(checkpoints, kind="checkpoint"),
        "artifacts": _safe_canonical_receipt_projection(artifacts, kind="artifact"),
        "effects": _safe_canonical_receipt_projection(effects, kind="effect"),
    }
    raw_pending_approvals = raw.get("pending_approvals")
    pending_approvals = (
        [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "workflow_run_identity",
                    "run_identity",
                    "job_id",
                    "tool_name",
                    "workflow_name",
                    "session_id",
                    "conversation_id",
                    "approval_conversation_id",
                    "operator_session_id",
                    "approval_owner_operator_session_id",
                    "owner_kind",
                    "approval_owner_kind",
                    "owner_principal_id",
                    "approval_owner_principal_id",
                    "goal_id",
                    "criterion_id",
                    "goal_revision",
                    "plan_revision",
                    "candidate_id",
                    "status",
                    "risk_level",
                    "summary",
                    "action",
                    "expires_at",
                    "approval_expires_at",
                )
                if item.get(key) is not None
            }
            for item in raw_pending_approvals
            if isinstance(item, dict)
        ]
        if isinstance(raw_pending_approvals, list)
        else []
    )
    pending_ids = [
        str(item.get("id") or item.get("approval_id") or "").strip()
        for item in pending_approvals
        if str(item.get("id") or item.get("approval_id") or "").strip()
    ]
    raw_arguments = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
    return {
        **raw,
        "id": raw.get("id") or raw.get("job_id") or raw.get("run_identity"),
        "run_identity": raw.get("run_identity") or raw.get("job_id"),
        "root_run_identity": raw.get("root_run_identity") or raw.get("run_identity") or raw.get("job_id"),
        "parent_run_identity": raw.get("parent_run_identity") or raw.get("parent_job_id"),
        "workflow_name": raw.get("workflow_name") or raw.get("job_kind") or "workflow",
        "tool_name": raw.get("tool_name") or raw.get("job_kind") or "workflow",
        "owner_kind": owner.get("kind"),
        "owner_principal_id": owner.get("principal_id"),
        "service_id": owner.get("service_id"),
        "conversation_id": conversation_id,
        "operator_session_id": operator_session_id,
        "goal_id": raw.get("goal_id") if raw.get("goal_id") is not None else authority.get("goal_id"),
        "criterion_id": raw.get("criterion_id") if raw.get("criterion_id") is not None else authority.get("criterion_id"),
        "goal_revision": raw.get("goal_revision") if raw.get("goal_revision") is not None else authority.get("goal_revision"),
        "plan_revision": raw.get("plan_revision") if raw.get("plan_revision") is not None else authority.get("plan_revision"),
        "candidate_id": raw.get("candidate_id") if raw.get("candidate_id") is not None else authority.get("candidate_id"),
        "status": status,
        "summary": f"Workflow {raw.get('workflow_name') or raw.get('job_kind') or 'workflow'} {status}",
        "started_at": started_at,
        "updated_at": updated_at,
        "finished_at": raw.get("finished_at"),
        "lease": lease,
        "lease_owner": lease.get("owner"),
        "lease_expires_at": lease.get("expires_at"),
        "fencing_token": lease.get("fencing_token"),
        "checkpoint_context": checkpoint_context,
        "checkpoint_context_available": bool(checkpoint_context),
        "step_records": step_records,
        "checkpoint_step_ids": [step["id"] for step in step_records],
        "last_completed_step_id": step_records[-1]["id"] if step_records else None,
        "artifact_paths": artifact_paths,
        "continued_error_steps": continued_error_steps,
        "arguments": raw_arguments,
        "approval_context": authority,
        "pending_approvals": pending_approvals,
        "pending_approval_count": len(pending_approvals) if pending_approvals else (1 if status == "awaiting_approval" else 0),
        "pending_approval_ids": pending_ids,
        "availability": "durable",
        "state_source": "durable_workflow_state",
        "typed_receipts": typed_receipts,
        "replay_inputs": raw_arguments,
        "replay_allowed": bool(checkpoint_context) and status not in {"succeeded", "degraded", "cancelled"},
        "replay_block_reason": None,
        "metadata": {
            "durable_job": {
                "goal_id": raw.get("goal_id"),
                "criterion_id": raw.get("criterion_id") if raw.get("criterion_id") is not None else authority.get("criterion_id"),
                "goal_revision": raw.get("goal_revision"),
                "plan_revision": raw.get("plan_revision"),
                "candidate_id": raw.get("candidate_id"),
                "dependencies": raw.get("dependencies", []),
                "deadline_at": raw.get("deadline_at"),
                "revision": raw.get("revision"),
                "lease": lease,
                "parent_job_id": raw.get("parent_job_id"),
                "parent_fencing_token": raw.get("parent_fencing_token"),
                "conversation_id": conversation_id,
                "operator_session_id": operator_session_id,
            }
        },
    }


def _safe_workflow_action_handle(
    value: Any,
    *,
    action: str = "resume",
    step_id: Any = None,
) -> dict[str, Any]:
    source = _as_record(value)
    raw_step_id = step_id if step_id is not None else source.get("resume_from_step")
    parent_revision = source.get("parent_revision")
    orchestration = _as_record(source.get("orchestration_v2"))
    if not orchestration:
        orchestration = _as_record(_as_record(source.get("metadata")).get("orchestration_v2"))
    if parent_revision is None:
        parent_revision = orchestration.get("revision")
    try:
        parent_revision = int(parent_revision) if parent_revision is not None else None
    except (TypeError, ValueError):
        parent_revision = None
    lease_id = source.get("parent_lease_id")
    if not lease_id:
        lease_id = _as_record(orchestration.get("lease")).get("lease_id")
    handle: dict[str, Any] = {
        "kind": "workflow_control",
        "action": _safe_workflow_action(action),
        # The handle is posted to the route for the run that emitted it.  A
        # parent identity is lineage metadata, never the route authority; a
        # branch child must therefore carry its own identity here.
        "run_identity": _safe_workflow_identity(
            source.get("run_identity")
            or source.get("source_run_identity")
            or source.get("id")
        ),
        "step_id": _safe_workflow_step_id(raw_step_id) if raw_step_id else None,
        "thread_id": _safe_workflow_token(source.get("thread_id") or source.get("session_id"), fallback=""),
        "requires_live_control": True,
        "draft_available": bool(source.get("draft") or source.get("draft_available")),
    }
    if parent_revision is not None:
        handle["parent_revision"] = parent_revision
        handle["parent_revision_digest"] = _workflow_identity_digest(str(parent_revision))
    if lease_id:
        handle["parent_lease_id_digest"] = _workflow_identity_digest(str(lease_id))
    return handle


def _safe_workflow_step_record(value: Any, run: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_step_id = str(value.get("id") or "").strip()
    if not raw_step_id:
        return None
    safe_actions: list[dict[str, Any]] = []
    for action in value.get("recovery_actions") or []:
        if not isinstance(action, dict):
            continue
        action_type = _safe_workflow_token(action.get("type"), fallback="review")
        safe_action = {
            "type": action_type,
            "label": _safe_workflow_token(action.get("label"), fallback="Review step"),
            "step_id": _safe_workflow_step_id(raw_step_id),
            "requires_live_control": action_type in {"resume", "retry", "repair", "branch", "replay"},
        }
        if action.get("mode"):
            safe_action["mode"] = _safe_workflow_token(action.get("mode"), fallback="balanced")
        safe_actions.append(safe_action)
    return {
        "id": _safe_workflow_step_id(raw_step_id),
        "index": _safe_workflow_count(value.get("index")),
        "tool": _safe_workflow_token(value.get("tool"), fallback="workflow_step"),
        "status": _safe_workflow_token(value.get("status"), fallback="unknown"),
        "argument_key_count": len(value.get("arguments") or {}) if isinstance(value.get("arguments"), dict) else 0,
        "artifact_count": len(value.get("artifact_paths") or []) if isinstance(value.get("artifact_paths"), list) else 0,
        "recovery_actions": safe_actions,
        "recovery_action_count": len(safe_actions),
        "is_recoverable": bool(value.get("is_recoverable") or safe_actions),
        "started_at": value.get("started_at") if isinstance(value.get("started_at"), str) else None,
        "completed_at": value.get("completed_at") if isinstance(value.get("completed_at"), str) else None,
    }


def _safe_workflow_resume_plan(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_action = "retry" if str(value.get("branch_kind") or "") == "retry_failed_step" else "resume"
    raw_inputs = value.get("replay_inputs")
    if not isinstance(raw_inputs, dict):
        raw_inputs = {}
    plan: dict[str, Any] = {
        "source_run_identity": _safe_workflow_identity(value.get("source_run_identity")),
        "parent_run_identity": _safe_workflow_identity(value.get("parent_run_identity")),
        "root_run_identity": _safe_workflow_identity(value.get("root_run_identity")),
        "parent_fencing_token": _safe_workflow_count(value.get("parent_fencing_token"))
        if value.get("parent_fencing_token") is not None
        else None,
        "thread_id": _safe_workflow_token(value.get("thread_id"), fallback="") or None,
        "branch_kind": _safe_workflow_token(value.get("branch_kind"), fallback="recovery"),
        "resume_from_step": _safe_workflow_step_id(value.get("resume_from_step"))
        if value.get("resume_from_step")
        else None,
        "resume_checkpoint_label": _safe_workflow_token(
            value.get("resume_checkpoint_label"), fallback="checkpoint"
        ) if value.get("resume_checkpoint_label") else None,
        "replay_allowed": bool(value.get("replay_allowed")),
        "replay_block_reason": _safe_workflow_refusal_detail(
            value.get("replay_block_reason"), fallback="workflow_replay_blocked"
        ) if value.get("replay_block_reason") else None,
        "requires_manual_execution": bool(value.get("requires_manual_execution", True)),
        "draft_available": bool(value.get("draft")),
        # Drafts contain workflow arguments and control identities.  Keep the
        # cockpit contract stable with explicit redaction and an opaque action
        # handle that routes back through the authenticated control endpoint.
        "draft": None,
        "replay_draft": None,
        "retry_from_step_draft": None,
        "replay_inputs": {
            "redacted": True,
            "argument_keys": sorted(str(key) for key in raw_inputs.keys()),
            "requires_live_control": True,
        },
        "continue_message": (
            "Use the live workflow recovery controls to continue this run."
            if value.get("draft") or value.get("continue_message")
            else None
        ),
        "action_handle": _safe_workflow_action_handle(
            value,
            action=raw_action,
            step_id=value.get("resume_from_step"),
        ),
    }
    candidates = value.get("checkpoint_candidates")
    if isinstance(candidates, list):
        safe_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            safe_candidates.append({
                "step_id": _safe_workflow_step_id(candidate.get("step_id")),
                "label": _safe_workflow_token(candidate.get("label"), fallback="checkpoint"),
                "kind": _safe_workflow_token(candidate.get("kind"), fallback="checkpoint"),
                "status": _safe_workflow_token(candidate.get("status"), fallback="unknown"),
                "resume_supported": bool(candidate.get("resume_supported")),
                "resume_draft": None,
                "continue_message": None,
                "action_handle": _safe_workflow_action_handle(
                    value,
                    action=(
                        "retry"
                        if str(candidate.get("kind") or "") == "retry_failed_step"
                        else "branch"
                        if str(candidate.get("kind") or "") in {"branch", "branch_from_checkpoint"}
                        else "resume"
                    ),
                    step_id=candidate.get("step_id"),
                ),
            })
        plan["checkpoint_candidates"] = safe_candidates
    return plan


def _safe_workflow_run_projection(value: Any) -> dict[str, Any] | None:
    value = _canonical_workflow_projection_input(value)
    if not isinstance(value, dict):
        return None
    workflow_name = _safe_workflow_token(value.get("workflow_name"), fallback="workflow")
    status = _safe_workflow_token(value.get("status"), fallback="unknown")
    raw_steps = value.get("step_records") if isinstance(value.get("step_records"), list) else []
    safe_steps = [
        safe_step
        for step in raw_steps
        if (safe_step := _safe_workflow_step_record(step, value)) is not None
    ]
    raw_candidates = value.get("checkpoint_candidates") if isinstance(value.get("checkpoint_candidates"), list) else []
    raw_inputs = value.get("replay_inputs")
    if not isinstance(raw_inputs, dict):
        raw_inputs = value.get("arguments") if isinstance(value.get("arguments"), dict) else {}
    artifact_projection = _safe_workflow_artifact_projection(value)
    replay_allowed = bool(value.get("replay_allowed", True))
    replay_block_reason = (
        _safe_workflow_refusal_detail(value.get("replay_block_reason"), fallback="workflow_replay_blocked")
        if value.get("replay_block_reason")
        else None
    )
    raw_resume_step = value.get("resume_from_step") or value.get("last_completed_step_id")
    action_kind = "retry" if value.get("continued_error_steps") else "resume"
    projection: dict[str, Any] = {
        "id": _safe_workflow_identity(value.get("id") or value.get("run_identity")),
        "run_identity": _safe_workflow_identity(value.get("run_identity") or value.get("id")),
        "workflow_name": workflow_name,
        "tool_name": _safe_workflow_token(value.get("tool_name"), fallback="workflow_tool"),
        "status": status,
        "availability": _safe_workflow_token(value.get("availability"), fallback="unknown"),
        "summary": f"Workflow {workflow_name} {status}",
        "session_id": _safe_workflow_token(value.get("session_id"), fallback="") or None,
        "goal_id": _safe_workflow_token(value.get("goal_id"), fallback="") or None,
        "goal_revision": _safe_workflow_count(value.get("goal_revision")) if value.get("goal_revision") is not None else None,
        "criterion_id": _safe_workflow_token(value.get("criterion_id"), fallback="") or None,
        "plan_revision": _safe_workflow_count(value.get("plan_revision")) if value.get("plan_revision") is not None else None,
        "candidate_id": _safe_workflow_token(value.get("candidate_id"), fallback="") or None,
        "record_schema_version": _safe_workflow_count(value.get("record_schema_version")),
        "owner_kind": _safe_workflow_token(value.get("owner_kind"), fallback="legacy"),
        "owner_principal_id_digest": (
            _workflow_identity_digest(str(value.get("owner_principal_id")))
            if value.get("owner_principal_id")
            else None
        ),
        "revision": _safe_workflow_count(value.get("revision")),
        "lease": {
            "owner_digest": _workflow_identity_digest(str(value.get("lease", {}).get("owner")))
            if isinstance(value.get("lease"), dict) and value.get("lease", {}).get("owner")
            else None,
            "lease_id_digest": _workflow_identity_digest(str(value.get("lease", {}).get("lease_id")))
            if isinstance(value.get("lease"), dict) and value.get("lease", {}).get("lease_id")
            else None,
            "expires_at": value.get("lease", {}).get("expires_at")
            if isinstance(value.get("lease"), dict) and isinstance(value.get("lease", {}).get("expires_at"), str)
            else None,
            "fencing_token": _safe_workflow_count(value.get("lease", {}).get("fencing_token"))
            if isinstance(value.get("lease"), dict)
            else 0,
            "revision": _safe_workflow_count(value.get("lease", {}).get("revision"))
            if isinstance(value.get("lease"), dict)
            else 0,
        },
        "parent_job_id_digest": (
            _workflow_identity_digest(str(value.get("parent_job_id")))
            if value.get("parent_job_id")
            else None
        ),
        "parent_fencing_token": _safe_workflow_count(value.get("parent_fencing_token"))
        if value.get("parent_fencing_token") is not None
        else None,
        "thread_id": _safe_workflow_token(value.get("thread_id") or value.get("session_id"), fallback="") or None,
        "thread_label": _safe_workflow_token(value.get("thread_label"), fallback="workflow thread")
        if value.get("thread_label")
        else None,
        "thread_source": _safe_workflow_token(value.get("thread_source"), fallback="session"),
        # Preserve branch grouping for Cockpit while keeping lineage values in
        # the same opaque identity form as the control handle.
        "root_run_identity": (
            _safe_workflow_identity(value.get("root_run_identity"))
            if value.get("root_run_identity")
            else None
        ),
        "parent_run_identity": (
            _safe_workflow_identity(value.get("parent_run_identity"))
            if value.get("parent_run_identity")
            else None
        ),
        "branch_kind": (
            _safe_workflow_token(value.get("branch_kind"), fallback="branch")
            if value.get("branch_kind")
            else None
        ),
        "branch_depth": _safe_workflow_count(value.get("branch_depth")),
        "is_branch_run": bool(value.get("parent_run_identity")),
        "continue_message": (
            "Use the live workflow recovery controls to continue this run."
            if value.get("thread_continue_message")
            or value.get("approval_recovery_message")
            or value.get("replay_draft")
            or value.get("retry_from_step_draft")
            else None
        ),
        # Operator orchestration derives this surface through
        # workflow_surface_continue_message(), so retain only the generic
        # safe message rather than the raw continuation text.
        "thread_continue_message": (
            "Use the live workflow recovery controls to continue this run."
            if value.get("thread_continue_message")
            or value.get("approval_recovery_message")
            or value.get("replay_draft")
            or value.get("retry_from_step_draft")
            else None
        ),
        "approval_recovery_message": None,
        "started_at": value.get("started_at") if isinstance(value.get("started_at"), str) else None,
        "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else None,
        "finished_at": value.get("finished_at") if isinstance(value.get("finished_at"), str) else None,
        "pending_approval_count": _safe_workflow_count(value.get("pending_approval_count")),
        "checkpoint_context_available": bool(value.get("checkpoint_context_available")),
        "artifact_count": len(artifact_projection["artifact_paths"]),
        "artifact_paths": artifact_projection["artifact_paths"],
        "artifact_registry": artifact_projection["artifact_registry"],
        "step_count": len(raw_steps),
        "step_tools": [
            _safe_workflow_token(tool, fallback="workflow_step")
            for tool in value.get("step_tools") or []
            if isinstance(tool, str)
        ],
        "continued_error_steps": [
            _safe_workflow_step_id(step_id)
            for step_id in value.get("continued_error_steps") or []
            if isinstance(step_id, str) and step_id.strip()
        ],
        "failed_step_count": len(value.get("continued_error_steps") or [])
        if isinstance(value.get("continued_error_steps"), list)
        else 0,
        "step_records": safe_steps,
        "checkpoint_candidates": [
            safe_candidate
            for candidate in raw_candidates
            if isinstance(candidate, dict)
            for safe_candidate in [{
                "step_id": _safe_workflow_step_id(candidate.get("step_id")),
                "label": _safe_workflow_token(candidate.get("label"), fallback="checkpoint"),
                "kind": _safe_workflow_token(candidate.get("kind"), fallback="checkpoint"),
                "status": _safe_workflow_token(candidate.get("status"), fallback="unknown"),
                "resume_supported": bool(candidate.get("resume_supported")),
                "resume_draft": None,
                "action_handle": _safe_workflow_action_handle(
                    value,
                    action=(
                        "retry"
                        if str(candidate.get("kind") or "") == "retry_failed_step"
                        else "branch"
                        if str(candidate.get("kind") or "") in {"branch", "branch_from_checkpoint"}
                        else "resume"
                    ),
                    step_id=candidate.get("step_id"),
                ),
            }]
        ],
        "checkpoint_candidate_count": len(raw_candidates),
        "replay_allowed": replay_allowed,
        "replay_block_reason": replay_block_reason,
        "replay_draft": None,
        "retry_from_step_draft": None,
        "replay_inputs": {
            "redacted": True,
            "argument_keys": sorted(str(key) for key in raw_inputs.keys()),
            "requires_live_control": True,
        },
        "retry_from_step_available": bool(value.get("retry_from_step_draft")),
        "replay_recommended_actions": [
            {
                "type": _safe_workflow_token(action.get("type"), fallback="review"),
                "label": _safe_workflow_token(action.get("label"), fallback="Review workflow"),
                "requires_live_control": True,
            }
            for action in value.get("replay_recommended_actions") or []
            if isinstance(action, dict)
        ],
        "resume_from_step": _safe_workflow_step_id(raw_resume_step) if raw_resume_step else None,
        "resume_checkpoint_label": _safe_workflow_token(value.get("resume_checkpoint_label"), fallback="checkpoint")
        if value.get("resume_checkpoint_label")
        else None,
        "recovery_action": _safe_workflow_action_handle(
            value,
            action=action_kind,
            step_id=raw_resume_step,
        ),
        "trust_boundary": {
            "status": _safe_workflow_token(
                _as_record(value.get("trust_boundary")).get("status"), fallback="unknown"
            ),
            "blocked": bool(_as_record(value.get("trust_boundary")).get("blocked")),
            "reason": _safe_workflow_refusal_detail(
                _as_record(value.get("trust_boundary")).get("reason"), fallback="workflow_replay_blocked"
            ) if _as_record(value.get("trust_boundary")).get("reason") else None,
        },
    }
    if isinstance(value.get("typed_receipts"), dict):
        projection["durable_receipts"] = value["typed_receipts"]
        projection["checkpoint_receipts"] = value["typed_receipts"].get("checkpoints", [])
        projection["artifact_receipts"] = value["typed_receipts"].get("artifacts", [])
        projection["effect_receipts"] = value["typed_receipts"].get("effects", [])
    projection["action_handle"] = projection["recovery_action"]
    plan = _safe_workflow_resume_plan(value.get("resume_plan"))
    if plan is not None:
        projection["resume_plan"] = plan
    return projection


async def _record_workflow_route_receipt(
    *,
    event_type: str,
    session_id: str | None,
    run_identity: str,
    action: str,
    status_code: int,
    detail: str,
    workflow_name: Any = None,
    step_id: Any = None,
) -> None:
    safe_workflow_name = _safe_workflow_token(workflow_name, fallback="workflow")
    await audit_repository.log_event(
        session_id=session_id if isinstance(session_id, str) else None,
        actor="operator",
        event_type=event_type,
        tool_name=safe_workflow_name,
        risk_level="medium",
        policy_mode=get_current_tool_policy_mode(),
        summary="Workflow operator route refused",
        details={
            "run_identity_digest": _workflow_identity_digest(run_identity),
            "workflow_name": safe_workflow_name,
            "action": _safe_workflow_action(action),
            "step_id": _safe_workflow_step_id(step_id)
            if step_id
            else None,
            "status_code": int(status_code),
            "detail": _safe_workflow_refusal_detail(detail),
            "external_action_allowed": False,
        },
    )


async def _workflow_session_fence(request: Request, revocation_scope) -> None:
    """Revalidate the current operator session around every awaited boundary."""
    await _ensure_rest_authorized(request, revocation_scope)
    assert_runtime_not_revoked()


def _safe_markdown_filename(name: str) -> str:
    value = _WORKFLOW_FILENAME_RE.sub("-", name.strip()).strip("-_").lower()
    return f"{value or 'workflow'}.md"


def _resolve_workflow_file_name(file_name: str | None, *, default_name: str) -> str:
    if not file_name:
        return default_name
    candidate = file_name.strip()
    normalized = os.path.normpath(candidate)
    if (
        not candidate
        or os.path.isabs(candidate)
        or normalized.startswith("..")
        or os.path.basename(normalized) != normalized
    ):
        raise HTTPException(status_code=400, detail="Workflow file name must stay within the managed workspace package")
    stem, _ = os.path.splitext(normalized)
    return _safe_markdown_filename(stem or normalized)


def _ensure_workflow_manager_workspace_extensions_loaded() -> None:
    workflows_dir = workflow_manager._workflows_dir or os.path.join(settings.workspace_dir, "workflows")
    manifest_roots = list(workflow_manager._manifest_roots or [])
    changed = not bool(workflow_manager._workflows_dir)
    for root in default_manifest_roots_for_workspace(settings.workspace_dir):
        if root not in manifest_roots:
            manifest_roots.append(root)
            changed = True
    if changed:
        workflow_manager.init(workflows_dir, manifest_roots=manifest_roots)


def _workflow_extension_snapshot():
    return ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()


def _workflow_surface_maps(snapshot) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    runtime_defaults_by_name: dict[str, str] = {}
    canvas_metadata_by_name: dict[str, dict[str, Any]] = {}
    for contribution in snapshot.list_contributions("workflow_runtimes"):
        if isinstance(contribution.metadata.get("registry_conflict"), dict):
            continue
        name = contribution.metadata.get("name")
        default_output_surface = contribution.metadata.get("default_output_surface")
        if (
            isinstance(name, str)
            and name.strip()
            and isinstance(default_output_surface, str)
            and default_output_surface.strip()
        ):
            runtime_defaults_by_name[name.strip()] = default_output_surface.strip()
    for contribution in snapshot.list_contributions("canvas_outputs"):
        if isinstance(contribution.metadata.get("registry_conflict"), dict):
            continue
        name = contribution.metadata.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        raw_sections = contribution.metadata.get("sections")
        raw_artifact_types = contribution.metadata.get("artifact_types")
        canvas_metadata_by_name[name.strip()] = {
            "title": str(contribution.metadata.get("title") or ""),
            "sections": [
                str(item).strip()
                for item in raw_sections
                if isinstance(item, str) and item.strip()
            ] if isinstance(raw_sections, list) else [],
            "artifact_types": [
                str(item).strip()
                for item in raw_artifact_types
                if isinstance(item, str) and item.strip()
            ] if isinstance(raw_artifact_types, list) else [],
        }
    return runtime_defaults_by_name, canvas_metadata_by_name


def _validate_workflow_content(content: str, *, path: str = "<draft>") -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    workflow = parse_workflow_content(content, path=path, errors=errors)
    if workflow is None:
        return {
            "valid": False,
            "errors": errors,
            "workflow": None,
            "runtime_ready": False,
            "missing_tools": [],
            "missing_skills": [],
        }

    base_tools, active_skill_names, mcp_mode = get_base_tools_and_active_skills()
    available_tool_names = [tool.name for tool in base_tools]
    missing_tools = [
        tool_name for tool_name in workflow.step_tools
        if tool_name not in set(available_tool_names)
    ]
    missing_skills = [
        skill_name for skill_name in workflow.requires_skills
        if skill_name not in set(active_skill_names)
    ]
    snapshot = _workflow_extension_snapshot()
    runtime_defaults_by_name, canvas_metadata_by_name = _workflow_surface_maps(snapshot)
    available_runtime_profiles = {
        str(item.metadata.get("name"))
        for item in snapshot.list_contributions("workflow_runtimes")
        if not isinstance(item.metadata.get("registry_conflict"), dict)
        if isinstance(item.metadata.get("name"), str) and str(item.metadata.get("name")).strip()
    }
    available_output_surfaces = {
        str(item.metadata.get("name"))
        for item in snapshot.list_contributions("canvas_outputs")
        if not isinstance(item.metadata.get("registry_conflict"), dict)
        if isinstance(item.metadata.get("name"), str) and str(item.metadata.get("name")).strip()
    }
    missing_runtime_profiles = (
        [workflow.runtime_profile]
        if workflow.runtime_profile and workflow.runtime_profile not in available_runtime_profiles
        else []
    )
    declared_output_surface = workflow.output_surface
    effective_output_surface = workflow.output_surface or (
        runtime_defaults_by_name.get(workflow.runtime_profile, "")
        if workflow.runtime_profile
        else ""
    )
    canvas_metadata = canvas_metadata_by_name.get(effective_output_surface, {})
    missing_output_surfaces = (
        [effective_output_surface]
        if effective_output_surface and effective_output_surface not in available_output_surfaces
        else []
    )
    execution_boundaries = workflow_manager._infer_execution_boundaries(workflow)
    risk_level = workflow_manager._infer_risk_level(workflow)
    policy_modes = workflow_manager._infer_policy_modes(workflow)
    requires_approval = (
        risk_level == "high"
        or ("external_mcp" in execution_boundaries and mcp_mode == "approval")
    )
    return {
        "valid": True,
        "errors": [],
        "workflow": {
            "name": workflow.name,
            "tool_name": workflow.tool_name,
            "description": workflow.description,
            "inputs": workflow.inputs,
            "requires_tools": workflow.requires_tools,
            "requires_skills": workflow.requires_skills,
            "runtime_profile": workflow.runtime_profile,
            "output_surface": effective_output_surface,
            "declared_output_surface": declared_output_surface,
            "effective_output_surface": effective_output_surface,
            "output_surface_title": str(canvas_metadata.get("title") or ""),
            "output_surface_sections": list(canvas_metadata.get("sections") or []),
            "output_surface_artifact_types": list(canvas_metadata.get("artifact_types") or []),
            "user_invocable": workflow.user_invocable,
            "enabled": workflow.enabled,
            "file_path": workflow.file_path,
            "step_count": len(workflow.steps),
            "policy_modes": policy_modes,
            "execution_boundaries": execution_boundaries,
            "risk_level": risk_level,
            "accepts_secret_refs": workflow_manager._accepts_secret_refs(workflow),
        },
        "runtime_ready": (
            not missing_tools
            and not missing_skills
            and not missing_runtime_profiles
            and not missing_output_surfaces
            and workflow.enabled
        ),
        "missing_tools": missing_tools,
        "missing_skills": missing_skills,
        "missing_runtime_profiles": missing_runtime_profiles,
        "missing_output_surfaces": missing_output_surfaces,
        "requires_approval": requires_approval,
    }


def _as_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: dict[str, None] = {}
    for item in value:
        text = str(item).strip()
        if text:
            normalized[text] = None
    return sorted(normalized)


def _normalize_source_systems(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, bool, tuple[str, ...]]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        server_name = str(item.get("server_name") or "").strip()
        hostname = str(item.get("hostname") or "").strip()
        source = str(item.get("source") or "").strip()
        authenticated_source = bool(item.get("authenticated_source", False))
        credential_sources = tuple(_normalize_string_list(item.get("credential_sources")))
        key = (
            server_name,
            hostname,
            source,
            authenticated_source,
            credential_sources,
        )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "server_name": server_name,
                "hostname": hostname,
                "source": source,
                "authenticated_source": authenticated_source,
                "credential_sources": list(credential_sources),
            }
        )
    normalized.sort(
        key=lambda item: (
            str(item.get("server_name") or ""),
            str(item.get("hostname") or ""),
            str(item.get("source") or ""),
            bool(item.get("authenticated_source", False)),
            tuple(item.get("credential_sources") or []),
        )
    )
    return normalized


def _normalize_credential_egress_policies(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        mode = str(item.get("mode") or "").strip()
        transport = str(item.get("transport") or "").strip()
        allowed_hosts = tuple(_normalize_string_list(item.get("allowed_hosts")))
        key = (mode, transport, allowed_hosts)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "mode": mode or "unknown",
                "transport": transport or "unknown",
                "allowed_hosts": list(allowed_hosts),
            }
        )
    normalized.sort(
        key=lambda item: (
            str(item.get("mode") or ""),
            str(item.get("transport") or ""),
            tuple(item.get("allowed_hosts") or []),
        )
    )
    return normalized


def _normalize_trust_partition(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    mode = str(value.get("mode") or "").strip()
    if not mode:
        return None
    return {
        "mode": mode,
        "background_capable": bool(value.get("background_capable", False)),
        "authenticated_source": bool(value.get("authenticated_source", False)),
        "credential_egress_policy_count": int(value.get("credential_egress_policy_count", 0) or 0),
        "blocked": bool(value.get("blocked", False)),
    }


def _normalize_approval_context(value: Any, *, workflow_name: str | None = None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    risk_level = str(value.get("risk_level") or "").strip()
    execution_boundaries = _normalize_string_list(value.get("execution_boundaries"))
    step_tools = _normalize_string_list(value.get("step_tools"))
    delegated_specialists = _normalize_string_list(value.get("delegated_specialists"))
    delegated_tool_names = _normalize_string_list(value.get("delegated_tool_names"))
    authenticated_source = bool(value.get("authenticated_source", False))
    delegation_target_unresolved = bool(value.get("delegation_target_unresolved", False))
    source_systems = _normalize_source_systems(value.get("source_systems"))
    credential_egress_policies = _normalize_credential_egress_policies(value.get("credential_egress_policies"))
    trust_partition = _normalize_trust_partition(value.get("trust_partition"))
    if not any(
        [
            risk_level,
            execution_boundaries,
            step_tools,
            delegated_specialists,
            delegated_tool_names,
            "accepts_secret_refs" in value,
            authenticated_source,
            delegation_target_unresolved,
            source_systems,
            credential_egress_policies,
            trust_partition,
        ]
    ):
        return None
    normalized = {
        "workflow_name": str(value.get("workflow_name") or workflow_name or "").strip() or None,
        "risk_level": risk_level or "unknown",
        "execution_boundaries": execution_boundaries,
        "accepts_secret_refs": bool(value.get("accepts_secret_refs", False)),
        "step_tools": step_tools,
    }
    if delegated_specialists:
        normalized["delegated_specialists"] = delegated_specialists
    if delegated_tool_names:
        normalized["delegated_tool_names"] = delegated_tool_names
    if authenticated_source:
        normalized["authenticated_source"] = True
    if delegation_target_unresolved:
        normalized["delegation_target_unresolved"] = True
    if source_systems:
        normalized["source_systems"] = source_systems
    if credential_egress_policies:
        normalized["credential_egress_policies"] = credential_egress_policies
    if trust_partition:
        normalized["trust_partition"] = trust_partition
    return normalized


def _workflow_current_approval_context(
    *,
    workflow_name: str,
    workflow_meta: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_approval_context(
        workflow_meta.get("approval_context"),
        workflow_name=workflow_name,
    )
    if normalized is not None:
        return normalized
    normalized = {
        "workflow_name": workflow_name,
        "risk_level": str(workflow_meta.get("risk_level") or "high"),
        "execution_boundaries": _normalize_string_list(
            workflow_meta.get("execution_boundaries") or ["unknown"]
        ),
        "accepts_secret_refs": bool(workflow_meta.get("accepts_secret_refs", False)),
        "step_tools": _normalize_string_list(workflow_meta.get("step_tools")),
    }
    if bool(workflow_meta.get("authenticated_source", False)):
        normalized["authenticated_source"] = True
    source_systems = _normalize_source_systems(workflow_meta.get("source_systems"))
    if source_systems:
        normalized["source_systems"] = source_systems
    credential_egress_policies = _normalize_credential_egress_policies(workflow_meta.get("credential_egress_policies"))
    if credential_egress_policies:
        normalized["credential_egress_policies"] = credential_egress_policies
    trust_partition = _normalize_trust_partition(workflow_meta.get("trust_partition"))
    if trust_partition:
        normalized["trust_partition"] = trust_partition
    return normalized


def _approval_context_surface_summary(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    summary = {
        "workflow_name": str(value.get("workflow_name") or ""),
        "risk_level": str(value.get("risk_level") or "unknown"),
        "execution_boundaries": _normalize_string_list(value.get("execution_boundaries")),
        "accepts_secret_refs": bool(value.get("accepts_secret_refs", False)),
        "step_tools": _normalize_string_list(value.get("step_tools")),
        "authenticated_source": bool(value.get("authenticated_source", False)),
        "source_systems": _normalize_source_systems(value.get("source_systems")),
        "credential_egress_policies": _normalize_credential_egress_policies(value.get("credential_egress_policies")),
        "delegated_specialists": _normalize_string_list(value.get("delegated_specialists")),
        "delegated_tool_names": _normalize_string_list(value.get("delegated_tool_names")),
        "delegation_target_unresolved": bool(value.get("delegation_target_unresolved", False)),
        "trust_partition": _normalize_trust_partition(value.get("trust_partition")),
    }
    return summary


def _approval_context_changed_fields(
    *,
    recorded: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return []
    changed_fields: list[str] = []
    for field_name in (
        "risk_level",
        "execution_boundaries",
        "accepts_secret_refs",
        "step_tools",
        "authenticated_source",
        "source_systems",
        "credential_egress_policies",
        "delegated_specialists",
        "delegated_tool_names",
        "delegation_target_unresolved",
        "trust_partition",
    ):
        if recorded.get(field_name) != current.get(field_name):
            changed_fields.append(field_name)
    return changed_fields


def _workflow_trust_boundary_payload(
    *,
    workflow_name: str,
    recorded_approval_context: dict[str, Any] | None,
    current_approval_context: dict[str, Any] | None,
    approval_context_mismatch: bool,
    approval_context_missing_for_protected_surface: bool,
) -> dict[str, Any]:
    if approval_context_mismatch:
        return {
            "status": "changed",
            "blocked": True,
            "reason": "approval_context_changed",
            "message": (
                f"Workflow '{workflow_name}' changed its trust boundary after this run. "
                "Start a fresh run instead of replaying or resuming."
            ),
            "requires_fresh_run": True,
            "changed_fields": _approval_context_changed_fields(
                recorded=recorded_approval_context,
                current=current_approval_context,
            ),
            "recorded": _approval_context_surface_summary(recorded_approval_context),
            "current": _approval_context_surface_summary(current_approval_context),
        }
    if approval_context_missing_for_protected_surface:
        return {
            "status": "missing",
            "blocked": True,
            "reason": "approval_context_missing",
            "message": (
                f"Workflow '{workflow_name}' predates trust-boundary tracking for its current privileged surface. "
                "Start a fresh run instead of replaying or resuming."
            ),
            "requires_fresh_run": True,
            "changed_fields": [],
            "recorded": None,
            "current": _approval_context_surface_summary(current_approval_context),
        }
    return {
        "status": "stable",
        "blocked": False,
        "reason": None,
        "message": None,
        "requires_fresh_run": False,
        "changed_fields": [],
        "recorded": _approval_context_surface_summary(recorded_approval_context),
        "current": _approval_context_surface_summary(current_approval_context),
    }


def _workflow_recovery_message(
    *,
    workflow_name: str,
    trust_boundary: dict[str, Any],
    pending_approval_count: int,
    availability: str,
) -> str | None:
    if bool(trust_boundary.get("blocked")):
        message = trust_boundary.get("message")
        return str(message) if isinstance(message, str) and message.strip() else None
    if pending_approval_count > 0:
        return f"Review pending approval(s) for workflow '{workflow_name}' before replaying."
    if availability != "ready":
        return f"Repair workflow '{workflow_name}' before replaying."
    return None


def _workflow_runtime_approval_contexts() -> dict[str, dict[str, Any]]:
    base_tools, active_skill_names, _mcp_mode = get_base_tools_and_active_skills()
    runtime_contexts: dict[str, dict[str, Any]] = {}
    for tool in workflow_manager.build_workflow_tools(base_tools, active_skill_names):
        tool_name = getattr(tool, "name", None)
        if not isinstance(tool_name, str) or not tool_name.startswith("workflow_"):
            continue
        hook = getattr(tool, "get_approval_context", None)
        if not callable(hook):
            continue
        normalized = _normalize_approval_context(
            hook({}),
            workflow_name=_workflow_name_from_tool(tool_name),
        )
        if normalized is not None:
            runtime_contexts[tool_name] = normalized
    return runtime_contexts


def _workflow_name_from_tool(tool_name: str) -> str:
    if tool_name.startswith("workflow_"):
        return tool_name.removeprefix("workflow_").replace("_", "-")
    return tool_name


def _extract_artifact_paths(value: Any) -> list[str]:
    paths: list[str] = []

    def visit(current: Any, key_hint: str | None = None) -> None:
        if isinstance(current, list):
            for item in current:
                visit(item, key_hint)
            return
        if isinstance(current, dict):
            for key, inner in current.items():
                visit(inner, key)
            return
        if (
            key_hint == "file_path"
            and isinstance(current, str)
            and current.strip()
            and current not in paths
        ):
            paths.append(current)

    visit(value)
    return paths


def _workflow_event_fingerprint(tool_name: str, details: dict[str, Any]) -> str:
    run_fingerprint = details.get("run_fingerprint")
    if isinstance(run_fingerprint, str) and run_fingerprint.strip():
        return run_fingerprint
    arguments = _as_record(details.get("arguments"))
    if arguments:
        return fingerprint_tool_call(
            tool_name,
            arguments,
            approval_context=_normalize_approval_context(details.get("approval_context")),
        )
    return "none"


def _workflow_projection_key(event: dict[str, Any], details: dict[str, Any]) -> str:
    durable_run_identity = details.get("durable_run_identity")
    if isinstance(durable_run_identity, str) and durable_run_identity.strip():
        return durable_run_identity.strip()
    tool_name = str(event.get("tool_name") or "workflow")
    fingerprint = _workflow_event_fingerprint(tool_name, details)
    return build_workflow_run_identity(
        event.get("session_id") if isinstance(event.get("session_id"), str) else None,
        tool_name,
        fingerprint,
        run_discriminator=_workflow_run_discriminator(details),
    )


def _workflow_projection_prefix(session_id: str | None, tool_name: str) -> str:
    return f"{session_id or 'global'}:{tool_name}:"


def _workflow_run_discriminator(details: dict[str, Any]) -> str | None:
    call_event_id = details.get("call_event_id")
    if isinstance(call_event_id, str) and call_event_id.strip():
        return call_event_id.strip()
    return None


def _workflow_identity_discriminator(event: dict[str, Any], details: dict[str, Any]) -> str | None:
    run_discriminator = _workflow_run_discriminator(details)
    if run_discriminator is not None:
        return run_discriminator
    if str(event.get("event_type") or "") == "tool_call":
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id.strip():
            return event_id.strip()
    return None


def _approval_projection_key(
    *,
    session_id: str | None,
    tool_name: str,
    fingerprint: str | None,
) -> str:
    return f"{session_id or 'global'}:{tool_name}:{fingerprint or 'none'}"


def _workflow_run_approval_key(run: dict[str, Any]) -> str:
    tool_name = str(run.get("tool_name") or "workflow")
    run_fingerprint = run.get("run_fingerprint")
    fingerprint = (
        run_fingerprint
        if isinstance(run_fingerprint, str) and run_fingerprint.strip()
        else (
            fingerprint_tool_call(
                tool_name,
                run.get("arguments") or {},
                approval_context=_normalize_approval_context(run.get("approval_context")),
            )
            if run.get("arguments")
            else None
        )
    )
    return _approval_projection_key(
        session_id=run.get("session_id") if isinstance(run.get("session_id"), str) else None,
        tool_name=tool_name,
        fingerprint=fingerprint,
    )


def _workflow_approval_value(approval: dict[str, Any], *names: str) -> Any:
    """Read an approval identity field from its flattened or nested receipt."""
    details = approval.get("details")
    nested = details if isinstance(details, dict) else {}
    for name in names:
        if approval.get(name) is not None:
            return approval.get(name)
        if nested.get(name) is not None:
            return nested.get(name)
    return None


def _workflow_run_identity_value(run: dict[str, Any], *names: str) -> Any:
    """Read a workflow identity field from its canonical projection sources."""
    metadata = run.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    durable = metadata.get("durable_job")
    durable = durable if isinstance(durable, dict) else {}
    declared_authority = run.get("declared_authority")
    declared_authority = declared_authority if isinstance(declared_authority, dict) else {}
    approval_context = run.get("approval_context")
    approval_context = approval_context if isinstance(approval_context, dict) else {}
    for source in (run, metadata, durable, declared_authority, approval_context):
        for name in names:
            if source.get(name) is not None:
                return source.get(name)
    return None


def _workflow_approval_expiry(approval: dict[str, Any]) -> datetime | None:
    """Parse the finite decision deadline carried by a pending projection."""
    value = _workflow_approval_value(
        approval,
        "approval_owner_expires_at",
        "approval_expires_at",
        "decision_expires_at",
        "expires_at",
    )
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _workflow_approval_matches_identity(
    run: dict[str, Any],
    approval: dict[str, Any],
) -> bool:
    """Require a pending approval to carry the complete run authority identity."""
    if not isinstance(approval, dict):
        return False
    expected_run_identity = str(
        _workflow_run_identity_value(run, "run_identity", "workflow_run_identity", "job_id")
        or ""
    ).strip()
    approval_run_identity = str(
        _workflow_approval_value(
            approval,
            "workflow_run_identity",
            "run_identity",
            "durable_job_id",
            "job_id",
        )
        or ""
    ).strip()
    if not expected_run_identity or approval_run_identity != expected_run_identity:
        return False

    expected_workflow = str(
        _workflow_run_identity_value(run, "workflow_name", "tool_name") or ""
    ).strip()
    approval_workflow = str(
        _workflow_approval_value(approval, "workflow_name")
        or _workflow_approval_value(approval, "tool_name")
        or ""
    ).strip()
    if not expected_workflow or approval_workflow not in {
        expected_workflow,
        str(_workflow_run_identity_value(run, "tool_name") or "").strip(),
    }:
        return False

    expected_session = str(
        _workflow_run_identity_value(run, "session_id", "thread_id") or ""
    ).strip()
    approval_session = str(_workflow_approval_value(approval, "session_id") or "").strip()
    if not expected_session or approval_session != expected_session:
        return False

    expected_conversation = str(
        _workflow_run_identity_value(
            run,
            "conversation_id",
            "approval_conversation_id",
        )
        or expected_session
        or ""
    ).strip()
    approval_conversation = str(
        _workflow_approval_value(
            approval,
            "conversation_id",
            "approval_conversation_id",
        )
        or ""
    ).strip()
    if not expected_conversation or not approval_conversation or approval_conversation != expected_conversation:
        return False

    expected_operator_session = str(
        _workflow_run_identity_value(
            run,
            "operator_session_id",
            "approval_owner_operator_session_id",
            "approval_owner_auth_session_id",
        )
        or ""
    ).strip()
    approval_operator_session = str(
        _workflow_approval_value(
            approval,
            "operator_session_id",
            "approval_owner_operator_session_id",
            "approval_owner_auth_session_id",
        )
        or ""
    ).strip()
    if (
        not expected_operator_session
        or not approval_operator_session
        or approval_operator_session != expected_operator_session
    ):
        return False

    expected_owner_kind = str(_workflow_run_identity_value(run, "owner_kind") or "").strip()
    approval_owner_kind = str(
        _workflow_approval_value(approval, "owner_kind", "approval_owner_kind") or ""
    ).strip()
    if not expected_owner_kind or approval_owner_kind != expected_owner_kind:
        return False

    expected_owner = str(
        _workflow_run_identity_value(run, "owner_principal_id") or ""
    ).strip()
    approval_owner = str(
        _workflow_approval_value(
            approval,
            "owner_principal_id",
            "approval_owner_principal_id",
        )
        or ""
    ).strip()
    if not expected_owner or approval_owner != expected_owner:
        return False

    for field_name in ("goal_id", "criterion_id", "candidate_id"):
        expected = str(_workflow_run_identity_value(run, field_name) or "").strip()
        actual = str(_workflow_approval_value(approval, field_name) or "").strip()
        if actual != expected:
            return False
    for field_name in ("goal_revision", "plan_revision"):
        expected = _positive_json_integer(_workflow_run_identity_value(run, field_name))
        actual = _positive_json_integer(_workflow_approval_value(approval, field_name))
        if expected is None or actual is None or actual != expected:
            return False

    status = str(_workflow_approval_value(approval, "status") or "").strip().lower()
    if status not in {"pending", "awaiting_approval", "approval_required"}:
        return False
    expiry = _workflow_approval_expiry(approval)
    if expiry is None or expiry <= datetime.now(timezone.utc):
        return False

    pending_ids = run.get("pending_approval_ids")
    approval_id = str(_workflow_approval_value(approval, "id", "approval_id") or "").strip()
    if isinstance(pending_ids, list) and pending_ids:
        if not approval_id or approval_id not in {
            str(item).strip() for item in pending_ids if str(item).strip()
        }:
            return False
    return bool(approval_id)


def _workflow_unique_approval(
    run: dict[str, Any],
    approvals: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the sole authority-bound approval; ambiguity fails closed."""
    if not approvals:
        return None
    matches = [
        approval
        for approval in approvals
        if _workflow_approval_matches_identity(run, approval)
    ]
    if len(matches) != 1:
        raise HTTPException(status_code=409, detail="workflow_approval_ambiguous")
    return matches[0]


def _workflow_identity_fields(value: Any) -> dict[str, Any]:
    record = _as_record(value)
    if record is None:
        return {}
    metadata = _as_record(record.get("metadata"))
    durable = _as_record(metadata.get("durable_job")) if metadata else None
    arguments = _as_record(record.get("arguments"))
    authority = _as_record(record.get("declared_authority"))
    sources = (record, metadata, durable, authority, arguments)
    fields: dict[str, Any] = {}
    for name in (
        "owner_kind",
        "owner_principal_id",
        "service_id",
        "conversation_id",
        "approval_conversation_id",
        "operator_session_id",
        "approval_owner_operator_session_id",
        "approval_owner_auth_session_id",
    ):
        for source in sources[:-1]:
            candidate = source.get(name) if source else None
            if candidate is not None and str(candidate).strip():
                fields[name] = str(candidate).strip()
                break
    for name in ("goal_id", "criterion_id", "candidate_id"):
        for source in sources:
            candidate = source.get(name) if source else None
            if candidate is not None and str(candidate).strip():
                fields[name] = str(candidate).strip()
                break
    for name in ("goal_revision", "plan_revision"):
        for source in sources:
            candidate = source.get(name) if source else None
            if isinstance(candidate, bool):
                continue
            normalized = _positive_json_integer(candidate)
            if normalized is not None:
                fields[name] = normalized
                break
    return fields


def _workflow_replay_draft(
    workflow_name: str,
    arguments: dict[str, Any] | None,
    *,
    control_inputs: dict[str, Any] | None = None,
) -> str:
    serialized_items: list[str] = []
    for key, value in (arguments or {}).items():
        serialized_items.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    for key, value in (control_inputs or {}).items():
        if value is None:
            continue
        serialized_items.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    if not serialized_items:
        return f'Run workflow "{workflow_name}".'
    return f'Run workflow "{workflow_name}" with {", ".join(serialized_items)}.'


def _workflow_retry_from_step_draft(
    workflow_name: str,
    *,
    step_id: str,
    arguments: dict[str, Any] | None,
    parent_run_identity: str | None = None,
    root_run_identity: str | None = None,
    branch_kind: str = "retry_failed_step",
    branch_depth: int | None = None,
    parent_revision: int | None = None,
    parent_lease_id: str | None = None,
    parent_fencing_token: int | None = None,
) -> str:
    control_inputs: dict[str, Any] = {
        "_seraph_resume_from_step": step_id,
        "_seraph_branch_kind": branch_kind,
    }
    if parent_run_identity:
        control_inputs["_seraph_parent_run_identity"] = parent_run_identity
    if root_run_identity:
        control_inputs["_seraph_root_run_identity"] = root_run_identity
    if isinstance(branch_depth, int):
        control_inputs["_seraph_branch_depth"] = branch_depth
    if isinstance(parent_revision, int):
        control_inputs["_seraph_parent_revision"] = parent_revision
    if isinstance(parent_lease_id, str) and parent_lease_id.strip():
        control_inputs["_seraph_parent_lease_id"] = parent_lease_id.strip()
    if isinstance(parent_fencing_token, int) and parent_fencing_token > 0:
        control_inputs["_seraph_parent_fencing_token"] = parent_fencing_token
    return _workflow_replay_draft(
        workflow_name,
        arguments,
        control_inputs=control_inputs,
    )


def _workflow_parent_recovery_metadata(
    run: dict[str, Any],
) -> tuple[int | None, str | None, int | None]:
    """Read only the lease metadata needed to bind a manual recovery draft."""
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    orchestration = run.get("orchestration_v2")
    if not isinstance(orchestration, dict):
        orchestration = metadata.get("orchestration_v2")
    if not isinstance(orchestration, dict) and _is_typed_workflow_run(run):
        orchestration = {
            "revision": run.get("revision"),
            "lease": run.get("lease"),
        }
    if not isinstance(orchestration, dict):
        return None, None, None
    lease = orchestration.get("lease")
    lease_id = lease.get("lease_id") if isinstance(lease, dict) else None
    try:
        revision = int(orchestration["revision"]) if orchestration.get("revision") is not None else None
    except (TypeError, ValueError):
        revision = None
    try:
        fencing_token = int(lease.get("fencing_token")) if isinstance(lease, dict) and lease.get("fencing_token") is not None else None
    except (TypeError, ValueError):
        fencing_token = None
    if _is_typed_workflow_run(run):
        # Schema-v2 has no mutable lease-id column.  Always derive the
        # recovery handle from the canonical identity and fence so an old or
        # caller-supplied lease label cannot become recovery authority.
        try:
            typed_run_identity = str(run.get("run_identity") or run.get("job_id") or "").strip()
            if typed_run_identity and fencing_token is not None:
                lease_id = durable_lease_id(typed_run_identity, fencing_token)
        except (TypeError, ValueError, OverflowError):
            lease_id = None
        if revision is None:
            try:
                revision = int(lease.get("revision")) if isinstance(lease, dict) and lease.get("revision") is not None else None
            except (TypeError, ValueError, OverflowError):
                revision = None
    return revision, str(lease_id).strip() if lease_id else None, fencing_token


def _workflow_branch_lineage(
    *,
    run_identity: str,
    details: dict[str, Any],
    approvals: list[dict[str, Any]],
    continued_error_steps: list[str],
) -> dict[str, Any]:
    parent_run_identity = details.get("parent_run_identity")
    if not isinstance(parent_run_identity, str) or not parent_run_identity.strip():
        parent_run_identity = None
    root_run_identity = details.get("root_run_identity")
    if not isinstance(root_run_identity, str) or not root_run_identity.strip():
        root_run_identity = parent_run_identity or run_identity
    branch_kind = details.get("branch_kind")
    if not isinstance(branch_kind, str) or not branch_kind.strip():
        if approvals:
            branch_kind = "approval_resume"
        elif continued_error_steps:
            branch_kind = "retry_failed_step"
        else:
            branch_kind = "replay_from_start"
    branch_depth = details.get("branch_depth")
    if not isinstance(branch_depth, int) or branch_depth < 0:
        branch_depth = 1 if parent_run_identity else 0
    return {
        "parent_run_identity": parent_run_identity,
        "root_run_identity": root_run_identity,
        "branch_kind": branch_kind,
        "branch_depth": branch_depth,
        "is_branch_run": parent_run_identity is not None,
    }


def _workflow_checkpoint_candidates(
    run: dict[str, Any],
    *,
    approvals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    workflow_name = str(run["workflow_name"])
    approval = _workflow_unique_approval(run, approvals)
    arguments = run.get("arguments")
    continued_error_steps = set(str(step_id) for step_id in run.get("continued_error_steps", []))
    root_run_identity = (
        str(run.get("root_run_identity"))
        if isinstance(run.get("root_run_identity"), str) and str(run.get("root_run_identity")).strip()
        else str(run.get("run_identity") or "")
    )
    next_branch_depth = (
        int(run.get("branch_depth")) + 1
        if isinstance(run.get("branch_depth"), int) and int(run.get("branch_depth")) >= 0
        else (1 if run.get("run_identity") else 0)
    )
    parent_revision, parent_lease_id, parent_fencing_token = _workflow_parent_recovery_metadata(run)
    candidates: list[dict[str, Any]] = []
    if approval is not None:
        candidates.append({
            "step_id": "approval_gate",
            "label": "Approval gate",
            "kind": "approval_gate",
            "status": "pending",
            "step_tool": None,
            "resume_draft": None,
            "continue_message": (
                approval.get("resume_message")
                if isinstance(approval, dict)
                else None
            ),
        })
    for index, step in enumerate(run.get("step_records", []) or []):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        step_tool = str(step.get("tool") or "tool")
        step_status = str(step.get("status") or "unknown")
        is_failed = step_id in continued_error_steps or step_status in {"failed", "continued_error"}
        resume_supported = bool(run.get("checkpoint_context_available")) or index == 0
        candidates.append({
            "step_id": step_id,
            "label": f"{step_id} ({step_tool})",
            "kind": "retry_failed_step" if is_failed else "branch_from_checkpoint",
            "status": step_status,
            "step_tool": step_tool,
            "resume_draft": (
                _workflow_retry_from_step_draft(
                    workflow_name,
                    step_id=step_id,
                    arguments=arguments,
                    parent_run_identity=str(run.get("run_identity") or "") or None,
                    root_run_identity=root_run_identity or None,
                    branch_kind="retry_failed_step" if is_failed else "branch_from_checkpoint",
                    branch_depth=next_branch_depth,
                    parent_revision=parent_revision,
                    parent_lease_id=parent_lease_id,
                    parent_fencing_token=parent_fencing_token,
                )
                if resume_supported
                else None
            ),
            "continue_message": None,
            "resume_supported": resume_supported,
        })
    return candidates


def _resolve_resume_step_id(
    run: dict[str, Any],
    *,
    checkpoint_candidates: list[dict[str, Any]],
    requested_step_id: str | None = None,
) -> str | None:
    candidate_ids = {
        str(checkpoint.get("step_id") or "")
        for checkpoint in checkpoint_candidates
        if isinstance(checkpoint, dict)
    }
    has_pending_approval = "approval_gate" in candidate_ids
    if isinstance(requested_step_id, str) and requested_step_id.strip():
        normalized = requested_step_id.strip()
        if normalized not in candidate_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow run '{run['run_identity']}' has no checkpoint '{normalized}'",
            )
        if has_pending_approval and normalized != "approval_gate":
            raise HTTPException(
                status_code=409,
                detail="Workflow run must clear the approval gate before branching from a later checkpoint",
            )
        if normalized == "approval_gate":
            return normalized
        return normalized
    if run.get("resume_from_step"):
        normalized = str(run["resume_from_step"])
        if normalized in candidate_ids:
            selected_checkpoint = next(
                (
                    checkpoint for checkpoint in checkpoint_candidates
                    if str(checkpoint.get("step_id") or "") == normalized
                ),
                None,
            )
            if (
                normalized != "approval_gate"
                and isinstance(selected_checkpoint, dict)
                and selected_checkpoint.get("resume_supported") is False
            ):
                return None
            return normalized
    return None


def _workflow_resume_plan(
    run: dict[str, Any],
    *,
    approvals: list[dict[str, Any]],
    requested_step_id: str | None = None,
) -> dict[str, Any]:
    replay_block_reason = str(run.get("replay_block_reason") or "").strip() or None
    if replay_block_reason:
        raise HTTPException(status_code=409, detail=_workflow_replay_block_detail(replay_block_reason))
    if run.get("replay_allowed") is False:
        raise HTTPException(status_code=409, detail="workflow_replay_blocked")
    checkpoint_candidates = _workflow_checkpoint_candidates(run, approvals=approvals)
    resume_step_id = _resolve_resume_step_id(
        run,
        checkpoint_candidates=checkpoint_candidates,
        requested_step_id=requested_step_id,
    )
    selected_checkpoint = next(
        (
            checkpoint for checkpoint in checkpoint_candidates
            if str(checkpoint.get("step_id") or "") == str(resume_step_id or "")
        ),
        None,
    )
    if (
        isinstance(selected_checkpoint, dict)
        and resume_step_id is not None
        and resume_step_id != "approval_gate"
        and selected_checkpoint.get("resume_supported") is False
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Workflow run '{run['run_identity']}' cannot branch from checkpoint "
                f"'{resume_step_id}' because the parent run did not persist reusable checkpoint state"
            ),
        )
    branch_kind = str(run.get("branch_kind") or "replay_from_start")
    if resume_step_id == "approval_gate":
        branch_kind = "approval_resume"
    elif isinstance(selected_checkpoint, dict):
        checkpoint_kind = str(selected_checkpoint.get("kind") or "")
        if checkpoint_kind == "retry_failed_step":
            branch_kind = "retry_failed_step"
        elif checkpoint_kind == "branch_from_checkpoint":
            branch_kind = "branch_from_checkpoint"
    replay_draft = (
        str(selected_checkpoint.get("resume_draft"))
        if isinstance(selected_checkpoint, dict) and selected_checkpoint.get("resume_draft")
        else run.get("retry_from_step_draft")
    )
    if not replay_draft and run.get("replay_draft"):
        replay_draft = str(run["replay_draft"])
    parent_revision, parent_lease_id, parent_fencing_token = _workflow_parent_recovery_metadata(run)
    return {
        "source_run_identity": run["run_identity"],
        "parent_run_identity": run["run_identity"],
        "root_run_identity": run.get("root_run_identity") or run["run_identity"],
        "thread_id": run.get("thread_id") or run.get("session_id"),
        "branch_kind": branch_kind,
        "resume_from_step": resume_step_id,
        "resume_checkpoint_label": (
            selected_checkpoint.get("label")
            if isinstance(selected_checkpoint, dict)
            else run.get("resume_checkpoint_label")
        ),
        "replay_allowed": bool(run.get("replay_allowed")),
        "replay_block_reason": run.get("replay_block_reason"),
        "draft": replay_draft,
        "continue_message": (
            selected_checkpoint.get("continue_message")
            if isinstance(selected_checkpoint, dict)
            else None
        ) or run.get("thread_continue_message") or run.get("approval_recovery_message"),
        "requires_manual_execution": True,
        "checkpoint_candidates": checkpoint_candidates,
        "parent_revision": parent_revision,
        "parent_lease_id": parent_lease_id,
        "parent_fencing_token": parent_fencing_token,
        "replay_inputs": run.get("arguments") or {},
    }


async def _find_workflow_run_for_control(run_identity: str) -> dict[str, Any] | None:
    # Scope the projection and pending-approval lookup to the run's
    # server-generated conversation session before inspecting any durable state.
    session_id, _tool_name, _run_fingerprint, _run_discriminator = _parse_run_identity(run_identity)
    runs = await _list_workflow_runs(limit=100, session_id=session_id)
    run = next((item for item in runs if item.get("run_identity") == run_identity), None)
    if run is not None:
        return run
    scoped_events, scoped_session_id = await _load_workflow_events_for_identity(run_identity)
    if scoped_events:
        runs = await _list_workflow_runs(
            limit=max(len(scoped_events), 1),
            session_id=scoped_session_id,
            events=scoped_events,
        )
        run = next((item for item in runs if item.get("run_identity") == run_identity), None)
    return run


async def _load_typed_workflow_run_for_control(
    run_identity: str,
    *,
    principal_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Read the current canonical row before exposing or mutating recovery."""
    durable_run = await durable_job_repository.get_job(run_identity)
    if durable_run is None:
        return None
    run = _canonical_workflow_projection_input(durable_run)
    if not isinstance(run, dict):
        return None
    if not _workflow_owner_is_bound(run, principal_id):
        raise HTTPException(status_code=403, detail="workflow_owner_mismatch")
    identity_detail = _workflow_recovery_identity_detail(
        run=run,
        run_identity=run_identity,
        operator_session_id=session_id,
    )
    if identity_detail is not None:
        raise HTTPException(status_code=403, detail=identity_detail)
    return run


def _typed_workflow_control_detail(exc: Exception) -> str:
    message = str(exc).lower()
    if "approval" in message:
        return "workflow_approval_gate_blocked"
    if "lease" in message or "fenc" in message or "revision" in message:
        return "workflow_control_lease_blocked"
    if "retry" in message or "reconcil" in message or "effect" in message:
        return "workflow_recovery_blocked"
    if "terminal" in message or "transition" in message:
        return "workflow_transition_blocked"
    return "workflow_control_failed"


async def _control_typed_workflow_run(
    *,
    run_identity: str,
    action: str,
    run: dict[str, Any],
    principal_id: str,
    session_id: str,
    operator_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply operator controls through the canonical typed repository.

    Legacy V2 orchestration receipts and leases cannot safely interpret a
    schema-v2 job row.  This adapter keeps the existing API response shape
    while every state-changing action uses the job repository's CAS/fence
    methods.
    """
    current = await _load_typed_workflow_run_for_control(
        run_identity,
        principal_id=principal_id,
        session_id=session_id,
    )
    if current is None:
        raise HTTPException(status_code=404, detail="workflow_run_not_found")
    run = current
    identity_detail = _workflow_identity_binding_detail(
        run=run,
        run_identity=run_identity,
        operator_context=operator_context,
        require_plan_revision=True,
    )
    if identity_detail is not None:
        raise HTTPException(status_code=409, detail=identity_detail)
    goal_detail = await _workflow_current_goal_binding_detail(run)
    if goal_detail is not None:
        raise HTTPException(status_code=409, detail=goal_detail)
    lease = run.get("lease") if isinstance(run.get("lease"), dict) else {}
    lease_owner = str(lease.get("owner") or "").strip() or None
    try:
        fencing_token = int(lease.get("fencing_token")) if lease.get("fencing_token") is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=409, detail="workflow_control_lease_blocked") from exc
    try:
        revision = int(run.get("revision"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=409, detail="workflow_control_lease_blocked") from exc
    if lease_owner and lease_owner != _workflow_canonical_lease_owner(run_identity):
        raise HTTPException(status_code=409, detail="workflow_control_lease_blocked")

    transition: dict[str, Any] | None = None
    if action == "audit":
        receipt = {
            "kind": "operator_control",
            "status": "recorded",
            "action": action,
            "revision": revision,
            "operator_visible": True,
        }
    else:
        try:
            if action == "pause":
                if lease_owner is None or fencing_token is None:
                    raise DurableJobError("active owner lease and fencing token are required")
                transition = await durable_job_repository.pause_job(
                    run_identity,
                    owner=lease_owner,
                    fencing_token=fencing_token,
                    expected_revision=revision,
                )
            elif action == "resume":
                if str(run.get("status") or "") == "awaiting_approval":
                    context = operator_context if isinstance(operator_context, dict) else {}
                    supplied_receipt = context.get("approval_receipt")
                    if not isinstance(supplied_receipt, dict):
                        raise DurableJobError(
                            "approval resume requires a typed approval receipt"
                        )
                    approval_id = str(
                        context.get("approval_id")
                        or supplied_receipt.get("approval_id")
                        or ""
                    ).strip()
                    expires_at = supplied_receipt.get("expires_at")
                    if not approval_id or expires_at is None:
                        raise DurableJobError(
                            "approval resume requires a current authenticated approval"
                        )
                    transition = await durable_job_repository.resume_approved_job(
                        run_identity,
                        approval_receipt=supplied_receipt,
                        approval_id=approval_id,
                        authority_digest=str(run.get("authority_digest") or ""),
                        goal_id=run.get("goal_id"),
                        goal_revision=run.get("goal_revision"),
                        plan_revision=run.get("plan_revision"),
                        capability_version=str(run.get("capability_version") or ""),
                        owner_kind=str(run.get("owner_kind") or ""),
                        owner_principal_id=str(run.get("owner_principal_id") or ""),
                        service_id=run.get("service_id"),
                        budget_microusd=_workflow_authority_budget_microusd(run),
                        budget_digest=str(run.get("budget_digest") or ""),
                        operator_principal_id=principal_id,
                        operator_session_id=session_id,
                        expires_at=expires_at,
                        expected_revision=revision,
                    )
                else:
                    transition = await durable_job_repository.resume_job(
                        run_identity,
                        expected_revision=revision,
                    )
            elif action == "revoke":
                transition = await durable_job_repository.revoke_job(
                    run_identity,
                    owner=lease_owner,
                    fencing_token=fencing_token,
                    expected_revision=revision,
                )
            elif action == "retry":
                context = operator_context if isinstance(operator_context, dict) else {}
                reconciliation_receipt = context.get("reconciliation_receipt")
                if not isinstance(reconciliation_receipt, dict):
                    raise DurableJobError("retry requires external-effect reconciliation")
                transition = await durable_job_repository.retry_job(
                    run_identity,
                    owner_kind=str(run.get("owner_kind") or "user"),
                    owner_principal_id=str(run.get("owner_principal_id") or principal_id),
                    service_id=run.get("service_id"),
                    reconciliation_receipt=reconciliation_receipt,
                    expected_revision=revision,
                )
            else:
                raise DurableJobError("typed workflow action requires a canonical transition")
        except HTTPException:
            raise
        except Exception as exc:
            detail = _typed_workflow_control_detail(exc)
            status_code = 423 if detail == "workflow_control_lease_blocked" else 409
            raise HTTPException(status_code=status_code, detail=detail) from exc
        receipt = transition.get("receipt") if isinstance(transition, dict) else None
        if not isinstance(receipt, dict):
            raise HTTPException(status_code=409, detail="workflow_transition_unavailable")
    refreshed = transition or run
    refreshed = _canonical_workflow_projection_input(refreshed) or refreshed
    return {
        "run_identity": _safe_workflow_identity(run_identity),
        "workflow_name": _safe_workflow_token(run.get("workflow_name"), fallback="workflow"),
        "action": action,
        "status": "recorded",
        "external_action_allowed": False,
        "control_receipt": _safe_workflow_receipt(receipt),
        "lease_receipt": None,
        "recovery_receipt": None,
        "transition_receipt": _safe_workflow_receipt(receipt),
        "resume_plan": None,
        "run": _safe_workflow_run_projection(refreshed),
    }


def _parse_run_identity(run_identity: str) -> tuple[str | None, str, str, str | None]:
    try:
        return parse_workflow_run_identity(run_identity)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Workflow run '{run_identity}' not found") from exc


def _serialize_audit_event(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "session_id": event.session_id,
        "actor": event.actor,
        "event_type": event.event_type,
        "tool_name": event.tool_name,
        "risk_level": event.risk_level,
        "policy_mode": event.policy_mode,
        "summary": event.summary,
        "details": json.loads(event.details_json) if event.details_json else None,
        "created_at": event.created_at.isoformat(),
    }


async def _load_workflow_events_for_identity(run_identity: str) -> tuple[list[dict[str, Any]], str | None]:
    session_id, tool_name, _run_fingerprint, run_discriminator = _parse_run_identity(run_identity)
    async with get_session() as db:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.tool_name == tool_name)
            .order_by(col(AuditEvent.created_at).desc())
        )
        if session_id is None:
            stmt = stmt.where(col(AuditEvent.session_id).is_(None))
        else:
            stmt = stmt.where(AuditEvent.session_id == session_id)
        result = await db.execute(stmt)
        events = result.scalars().all()
    serialized = [_serialize_audit_event(event) for event in events]
    if run_discriminator is None:
        return serialized, session_id
    return [
        event
        for event in serialized
        if _workflow_identity_discriminator(event, _as_record(event.get("details"))) == run_discriminator
    ], session_id


def _workflow_replay_policy(
    *,
    availability: str,
    risk_level: str,
    execution_boundaries: list[str],
    accepts_secret_refs: bool,
    pending_approval_count: int,
    approval_context_mismatch: bool,
    approval_context_missing_for_protected_surface: bool,
) -> tuple[bool, str | None]:
    if approval_context_mismatch:
        return False, "approval_context_changed"
    if approval_context_missing_for_protected_surface:
        return False, "approval_context_missing"
    if availability == "disabled":
        return False, "workflow_disabled"
    if availability != "ready":
        return False, "workflow_unavailable"
    if pending_approval_count > 0:
        return False, "pending_approval"
    if accepts_secret_refs:
        return False, "secret_ref_surface"
    if any(
        boundary in {"secret_management", "secret_read", "secret_injection"}
        for boundary in execution_boundaries
    ):
        return False, "secret_bearing_boundary"
    if risk_level == "high":
        return False, "high_risk_requires_manual_reentry"
    return True, None


def _workflow_resume_surface_allowed(*, replay_block_reason: str | None) -> bool:
    return not bool(replay_block_reason)


def _workflow_repair_surface_allowed(*, replay_block_reason: str | None) -> bool:
    return not bool(replay_block_reason)


def _workflow_boundary_blocked(*, replay_block_reason: str | None) -> bool:
    return bool(replay_block_reason)


def _workflow_continue_message_allowed(
    *,
    replay_block_reason: str | None,
    approval_context: dict[str, Any] | None,
) -> bool:
    return not replay_block_reason


def workflow_surface_continue_message(run: dict[str, Any]) -> str | None:
    replay_block_reason = str(run.get("replay_block_reason") or "") or None
    if _workflow_boundary_blocked(replay_block_reason=replay_block_reason):
        if _workflow_continue_message_allowed(
            replay_block_reason=replay_block_reason,
            approval_context=_as_record(run.get("current_approval_context"))
            or _as_record(run.get("approval_context")),
        ):
            message = run.get("thread_continue_message")
            if isinstance(message, str) and message.strip():
                return message
        message = run.get("approval_recovery_message")
        return str(message) if isinstance(message, str) and message.strip() else None
    for value in (
        run.get("thread_continue_message"),
        run.get("approval_recovery_message"),
        run.get("retry_from_step_draft"),
        run.get("replay_draft"),
    ):
        if isinstance(value, str) and value.strip():
            return value
    return None


def workflow_surface_replay_draft(run: dict[str, Any]) -> str | None:
    replay_block_reason = str(run.get("replay_block_reason") or "") or None
    if _workflow_boundary_blocked(replay_block_reason=replay_block_reason):
        return None
    value = run.get("replay_draft")
    return str(value) if isinstance(value, str) and value.strip() else None


def workflow_surface_recommended_actions(run: dict[str, Any]) -> list[dict[str, Any]]:
    replay_block_reason = str(run.get("replay_block_reason") or "") or None
    if _workflow_boundary_blocked(replay_block_reason=replay_block_reason):
        return []
    value = run.get("replay_recommended_actions")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def workflow_surface_resume_metadata(run: dict[str, Any]) -> dict[str, Any]:
    replay_block_reason = str(run.get("replay_block_reason") or "") or None
    blocked = _workflow_boundary_blocked(replay_block_reason=replay_block_reason)
    checkpoint_candidates = run.get("checkpoint_candidates")
    if not isinstance(checkpoint_candidates, list):
        checkpoint_candidates = []
    return {
        "replay_allowed": False if blocked else bool(run.get("replay_allowed")),
        "resume_from_step": None if blocked else run.get("resume_from_step"),
        "resume_checkpoint_label": None if blocked else run.get("resume_checkpoint_label"),
        "checkpoint_candidates": [] if blocked else checkpoint_candidates,
        "resume_plan": None if blocked else run.get("resume_plan"),
        "trust_boundary": run.get("trust_boundary"),
    }


def _workflow_runtime_statuses() -> dict[str, dict[str, Any]]:
    base_tools, active_skill_names, _ = get_base_tools_and_active_skills()
    available_tool_names = [tool.name for tool in base_tools]
    workflows = workflow_manager.list_workflows(
        available_tool_names=available_tool_names,
        active_skill_names=active_skill_names,
    )
    statuses: dict[str, dict[str, Any]] = {}
    for workflow in workflows:
        enabled = bool(workflow.get("enabled", False))
        is_available = bool(workflow.get("is_available", False))
        if not enabled:
            availability = "disabled"
        elif is_available:
            availability = "ready"
        else:
            availability = "blocked"
        statuses[str(workflow["name"])] = {
            **workflow,
            "availability": availability,
            "missing_tools": list(workflow.get("missing_tools", [])),
            "missing_skills": list(workflow.get("missing_skills", [])),
        }
    return statuses


def _workflow_replay_recommended_actions(workflow_status: dict[str, Any] | None) -> list[dict[str, Any]]:
    if workflow_status is None:
        return []
    actions: list[dict[str, Any]] = []
    if not bool(workflow_status.get("enabled", False)):
        actions.append({
            "type": "toggle_workflow",
            "label": "Enable workflow",
            "name": workflow_status["name"],
            "enabled": True,
        })
    for skill_name in workflow_status.get("missing_skills", []) or []:
        actions.append({
            "type": "toggle_skill",
            "label": f"Enable {skill_name}",
            "name": skill_name,
            "enabled": True,
        })
    current_tool_mode = get_current_tool_policy_mode()
    for tool_name in workflow_status.get("missing_tools", []) or []:
        suggested_mode = _recommended_tool_policy_mode(
            current_mode=current_tool_mode,
            blocked_reason=None,
        )
        if suggested_mode is None:
            continue
        actions.append({
            "type": "set_tool_policy",
            "label": f"Allow {tool_name}",
            "mode": suggested_mode,
        })
    if not actions:
        actions.append({
            "type": "open_settings",
            "label": "Open settings",
            "target": "workflows",
        })
    return actions


def _step_recovery_recommended_actions(
    *,
    step: dict[str, Any],
    workflow_status: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    actions = _workflow_replay_recommended_actions(workflow_status)
    step_tool = str(step.get("tool") or "")
    missing_tools = workflow_status.get("missing_tools") if isinstance(workflow_status, dict) else []
    if not isinstance(missing_tools, list):
        missing_tools = []
    step_requires_policy_repair = (
        step_tool
        and step_tool != "unknown"
        and str(workflow_status.get("availability") or "") == "blocked"
        and step_tool in {str(tool) for tool in missing_tools}
    )
    if step_requires_policy_repair:
        current_tool_mode = get_current_tool_policy_mode()
        suggested_mode = _recommended_tool_policy_mode(
            current_mode=current_tool_mode,
            blocked_reason=None,
        )
        if suggested_mode is not None:
            actions.append({
                "type": "set_tool_policy",
                "label": f"Allow {step_tool}",
                "mode": suggested_mode,
            })
    seen: set[tuple[str, str | None, str | None]] = set()
    deduped: list[dict[str, Any]] = []
    for action in actions:
        key = (
            str(action.get("type") or ""),
            str(action.get("name")) if action.get("name") is not None else None,
            str(action.get("mode")) if action.get("mode") is not None else None,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _step_recovery_hint(step: dict[str, Any]) -> str | None:
    step_tool = str(step.get("tool") or "step")
    error_kind = str(step.get("error_kind") or "").strip()
    error_summary = str(step.get("error_summary") or "").strip()
    if error_kind or error_summary:
        base = error_summary or error_kind.replace("_", " ")
        return f"{step_tool} failed and needs repair before replay"
    return f"Review {step_tool} inputs and retry this step"


def _resume_checkpoint_label(*, approvals: list[dict[str, Any]], continued_error_steps: list[str]) -> str | None:
    if approvals:
        return "Approval gate"
    if continued_error_steps:
        return "Retry failed step"
    return None


def _timeline_entries_for_run(
    run: dict[str, Any],
    *,
    approvals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries = [
        {
            "kind": "workflow_started",
            "at": run["started_at"],
            "summary": "Workflow started",
        }
    ]
    for step in run.get("step_records", []) or []:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "step")
        step_tool = str(step.get("tool") or "tool")
        step_status = str(step.get("status") or "succeeded")
        result_summary = str(step.get("result_summary") or "").strip()
        entries.append({
            "kind": f"workflow_step_{step_status}",
            "at": step.get("completed_at") or step.get("started_at") or run["updated_at"],
            "summary": (
                f"{step_id} ({step_tool}) {step_status.replace('_', ' ')}"
                + (f" · {result_summary}" if result_summary else "")
            ),
            "step_id": step_id,
            "step_tool": step_tool,
            "result_summary": result_summary,
            "error_kind": step.get("error_kind"),
            "error_summary": step.get("error_summary"),
            "duration_ms": step.get("duration_ms"),
        })
    for approval in approvals:
        entries.append({
            "kind": "approval_pending",
            "at": approval.get("created_at") or run["updated_at"],
            "summary": approval.get("summary")
            or f"Approval pending for {run['workflow_name']}",
            "approval_id": approval.get("id"),
            "risk_level": approval.get("risk_level"),
        })
    status = str(run["status"])
    entries.append({
        "kind": f"workflow_{status}",
        "at": run["updated_at"],
        "summary": run["summary"],
    })
    return entries


async def _list_workflow_runs(
    *,
    limit: int,
    session_id: str | None,
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if events is None:
        events = await audit_repository.list_events(limit=max(limit * 6, 30), session_id=session_id)
    workflow_events = [
        event for event in events
        if isinstance(event.get("tool_name"), str) and str(event["tool_name"]).startswith("workflow_")
    ]
    workflow_events.sort(key=lambda item: item.get("created_at", ""))
    pending_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    completed: list[dict[str, Any]] = []
    pending_approvals = await approval_repository.list_pending(session_id=session_id, limit=100)
    workflow_statuses = _workflow_runtime_statuses()
    workflow_runtime_contexts = _workflow_runtime_approval_contexts()
    pending_by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for approval in pending_approvals:
        tool_name = str(approval.get("tool_name") or "")
        approval_session_id = approval.get("session_id")
        pending_by_signature[
            _approval_projection_key(
                session_id=approval_session_id if isinstance(approval_session_id, str) else None,
                tool_name=tool_name,
                fingerprint=str(approval.get("fingerprint") or ""),
            )
        ].append(approval)
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions()
        if isinstance(session, dict) and session.get("id")
    }

    for event in workflow_events:
        details = _as_record(event.get("details"))
        tool_name = str(event.get("tool_name") or "workflow")
        key = _workflow_projection_key(event, details)
        event_projection_key = key
        run_fingerprint = _workflow_event_fingerprint(tool_name, details)
        if event.get("event_type") == "tool_call":
            arguments = _as_record(details.get("arguments")) or None
            pending_by_key[key].append({
                "id": event["id"],
                "tool_name": tool_name,
                "workflow_name": str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
                "session_id": event.get("session_id"),
                "run_fingerprint": run_fingerprint,
                "status": "running",
                "started_at": event["created_at"],
                "updated_at": event["created_at"],
                "summary": event.get("summary") or "",
                "step_tools": [],
                "step_records": [],
                "checkpoint_step_ids": [],
                "last_completed_step_id": None,
                "artifact_paths": _extract_artifact_paths(arguments),
                "continued_error_steps": [],
                "arguments": arguments,
                **_workflow_identity_fields({**details, "arguments": arguments}),
                "approval_context": _normalize_approval_context(
                    details.get("approval_context"),
                    workflow_name=str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
                ),
            })
            continue

        run_queue = pending_by_key.get(key, [])
        run_discriminator = _workflow_run_discriminator(details)
        if not run_queue and run_discriminator is not None:
            legacy_key = build_workflow_run_identity(
                event.get("session_id") if isinstance(event.get("session_id"), str) else None,
                tool_name,
                run_fingerprint,
            )
            legacy_queue = pending_by_key.get(legacy_key, [])
            legacy_index = next(
                (
                    index
                    for index, pending_run in enumerate(legacy_queue)
                    if str(pending_run.get("id") or "") == run_discriminator
                ),
                None,
            )
            if legacy_index is not None:
                key = legacy_key
                run_queue = pending_by_key.get(legacy_key, [])
                run = run_queue.pop(legacy_index)
            else:
                run = None
        else:
            run = None
        if not run_queue and run_fingerprint == "none":
            prefix = _workflow_projection_prefix(
                event.get("session_id") if isinstance(event.get("session_id"), str) else None,
                tool_name,
            )
            fallback_key = next(
                (
                    pending_key for pending_key, queue in pending_by_key.items()
                    if pending_key.startswith(prefix) and queue
                ),
                None,
            )
            if fallback_key is not None:
                key = fallback_key
                run_queue = pending_by_key.get(fallback_key, [])
        if run is None:
            run = run_queue.pop(0) if run_queue else {
            "id": event["id"],
            "tool_name": tool_name,
            "workflow_name": str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
            "session_id": event.get("session_id"),
            "run_fingerprint": run_fingerprint,
            "status": "running",
            "started_at": event["created_at"],
            "updated_at": event["created_at"],
            "summary": event.get("summary") or "",
            "step_tools": [],
            "step_records": [],
            "checkpoint_step_ids": [],
            "last_completed_step_id": None,
            "artifact_paths": [],
            "continued_error_steps": [],
            "arguments": _as_record(details.get("arguments")) or None,
            **_workflow_identity_fields({**details, "arguments": _as_record(details.get("arguments")) or None}),
            "approval_context": _normalize_approval_context(
                details.get("approval_context"),
                workflow_name=str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
            ),
            }
        if not run_queue and key in pending_by_key:
            pending_by_key.pop(key, None)

        artifact_paths = list(run.get("artifact_paths", []))
        for path in details.get("artifact_paths") or []:
            if isinstance(path, str) and path.strip() and path not in artifact_paths:
                artifact_paths.append(path)
        for path in _extract_artifact_paths(details.get("arguments")):
            if path not in artifact_paths:
                artifact_paths.append(path)

        for field_name, field_value in _workflow_identity_fields({**details, "arguments": details.get("arguments")}).items():
            if run.get(field_name) is None:
                run[field_name] = field_value

        workflow_meta = workflow_manager.get_tool_metadata(tool_name) or {}
        workflow_status = workflow_statuses.get(str(run["workflow_name"]))
        # Pending approvals remain display data until one receipt carries the
        # complete canonical identity.  List ordering cannot select recovery
        # authority.
        run["run_identity"] = event_projection_key
        approval_key = _workflow_run_approval_key(run)
        approvals = pending_by_signature.get(approval_key, [])
        try:
            recovery_approvals = [_workflow_unique_approval(run, approvals)] if approvals else []
        except HTTPException:
            recovery_approvals = []
        recorded_approval_context = (
            _normalize_approval_context(
                details.get("approval_context"),
                workflow_name=str(run["workflow_name"]),
            )
            or _normalize_approval_context(
                run.get("approval_context"),
                workflow_name=str(run["workflow_name"]),
            )
        )
        current_approval_context = _workflow_current_approval_context(
            workflow_name=str(run["workflow_name"]),
            workflow_meta={
                **workflow_meta,
                "approval_context": workflow_runtime_contexts.get(tool_name)
                or workflow_meta.get("approval_context"),
            },
        )
        effective_approval_context = recorded_approval_context or current_approval_context
        approval_context_mismatch = bool(
            recorded_approval_context is not None
            and recorded_approval_context != current_approval_context
        )
        approval_context_missing_for_protected_surface = bool(
            recorded_approval_context is None
            and approval_context_requires_tracked_lineage(current_approval_context)
        )
        trust_boundary = _workflow_trust_boundary_payload(
            workflow_name=str(run["workflow_name"]),
            recorded_approval_context=recorded_approval_context,
            current_approval_context=current_approval_context,
            approval_context_mismatch=approval_context_mismatch,
            approval_context_missing_for_protected_surface=approval_context_missing_for_protected_surface,
        )

        run.update({
            "status": "failed" if event.get("event_type") == "tool_failed" else "succeeded",
            "updated_at": event["created_at"],
            "summary": event.get("summary") or run.get("summary") or "",
            "step_tools": [
                value for value in details.get("step_tools", [])
                if isinstance(value, str)
            ] or run.get("step_tools", []),
            "step_records": [
                value for value in details.get("step_records", [])
                if isinstance(value, dict)
            ] or run.get("step_records", []),
            "checkpoint_step_ids": [
                value for value in details.get("checkpoint_step_ids", [])
                if isinstance(value, str)
            ] or run.get("checkpoint_step_ids", []),
            "last_completed_step_id": (
                str(details.get("last_completed_step_id"))
                if details.get("last_completed_step_id") is not None
                else run.get("last_completed_step_id")
            ),
            "artifact_paths": artifact_paths,
            "continued_error_steps": [
                value for value in details.get("continued_error_steps", [])
                if isinstance(value, str)
            ] or run.get("continued_error_steps", []),
            "runtime_profile": (
                str(details.get("runtime_profile"))
                if details.get("runtime_profile") is not None
                else workflow_meta.get("runtime_profile", "")
            ),
            "output_surface": (
                str(details.get("output_surface"))
                if details.get("output_surface") is not None
                else workflow_meta.get("output_surface", "")
            ),
            "canvas_output": (
                details.get("canvas_output")
                if isinstance(details.get("canvas_output"), dict)
                else run.get("canvas_output")
            ),
            "checkpoint_context_available": bool(
                details.get("checkpoint_context_available")
                or isinstance(details.get("checkpoint_context"), dict)
            ),
            "approval_context": effective_approval_context,
            "recorded_approval_context": recorded_approval_context,
            "current_approval_context": current_approval_context,
            "approval_context_mismatch": approval_context_mismatch,
            "trust_boundary": trust_boundary,
            "risk_level": (
                str(effective_approval_context.get("risk_level"))
                if effective_approval_context is not None
                else workflow_meta.get("risk_level", "high")
            ),
            "execution_boundaries": (
                list(effective_approval_context.get("execution_boundaries", []))
                if effective_approval_context is not None
                else workflow_meta.get("execution_boundaries", ["unknown"])
            ),
            "accepts_secret_refs": (
                bool(effective_approval_context.get("accepts_secret_refs", False))
                if effective_approval_context is not None
                else bool(workflow_meta.get("accepts_secret_refs", False))
            ),
            "pending_approval_count": len(approvals),
            "pending_approval_ids": [approval["id"] for approval in approvals],
            "pending_approvals": approvals,
            "availability": (
                workflow_status.get("availability", "unknown")
                if workflow_status is not None
                else "unknown"
            ),
            "replay_inputs": run.get("arguments") or {},
            "parameter_schema": (
                workflow_status.get("inputs", {})
                if workflow_status is not None and isinstance(workflow_status.get("inputs"), dict)
                else {}
            ),
            "replay_recommended_actions": _workflow_replay_recommended_actions(workflow_status),
        })
        step_records = run.get("step_records") or []
        if isinstance(step_records, list):
            for step in step_records:
                if not isinstance(step, dict):
                    continue
                step["recovery_actions"] = _step_recovery_recommended_actions(
                    step=step,
                    workflow_status=workflow_status,
                )
                step["recovery_hint"] = _step_recovery_hint(step)
                step["is_recoverable"] = bool(step["recovery_actions"])
        replay_allowed, replay_block_reason = _workflow_replay_policy(
            availability=str(run["availability"]),
            risk_level=str(run["risk_level"]),
            execution_boundaries=list(run["execution_boundaries"]),
            accepts_secret_refs=bool(run["accepts_secret_refs"]),
            pending_approval_count=len(approvals),
            approval_context_mismatch=bool(run.get("approval_context_mismatch")),
            approval_context_missing_for_protected_surface=approval_context_missing_for_protected_surface,
        )
        repair_surface_allowed = _workflow_repair_surface_allowed(
            replay_block_reason=replay_block_reason,
        )
        if not repair_surface_allowed:
            run["replay_recommended_actions"] = []
            if isinstance(step_records, list):
                for step in step_records:
                    if not isinstance(step, dict):
                        continue
                    step["recovery_actions"] = []
                    step["recovery_hint"] = None
                    step["is_recoverable"] = False
        run_identity = event_projection_key
        lineage = _workflow_branch_lineage(
            run_identity=run_identity,
            details=details,
            approvals=recovery_approvals,
            continued_error_steps=list(run.get("continued_error_steps", [])),
        )
        resume_surface_allowed = _workflow_resume_surface_allowed(
            replay_block_reason=replay_block_reason,
        )
        resume_from_step = (
            (
                "approval_gate"
                if recovery_approvals
                else (run["continued_error_steps"][0] if run.get("continued_error_steps") else None)
            )
            if resume_surface_allowed
            else None
        )
        retry_from_step_draft = (
            _workflow_retry_from_step_draft(
                str(run["workflow_name"]),
                step_id=str(run["continued_error_steps"][0]),
                arguments=run.get("arguments"),
                parent_run_identity=run_identity,
                root_run_identity=str(lineage.get("root_run_identity") or run_identity),
                branch_kind="retry_failed_step",
                branch_depth=int(lineage.get("branch_depth") or 0) + 1,
            )
            if resume_surface_allowed
            and replay_allowed
            and run.get("continued_error_steps")
            and (
                bool(run.get("checkpoint_context_available"))
                or (
                    isinstance(run.get("step_records"), list)
                    and run.get("step_records")
                    and str(run["continued_error_steps"][0]) == str(run["step_records"][0].get("id") or "")
                )
            )
            else None
        )
        run.update({
            "thread_id": run.get("session_id"),
            "thread_label": (
                session_titles.get(str(run["session_id"]))
                if run.get("session_id")
                else None
            ),
            "thread_source": "session" if run.get("session_id") else "ambient",
            "replay_allowed": replay_allowed,
            "replay_block_reason": replay_block_reason,
            "replay_draft": (
                _workflow_replay_draft(str(run["workflow_name"]), run.get("arguments"))
                if replay_allowed
                else None
            ),
            "resume_from_step": resume_from_step,
            "retry_from_step_draft": retry_from_step_draft,
            "resume_checkpoint_label": _resume_checkpoint_label(
                approvals=recovery_approvals,
                continued_error_steps=list(run.get("continued_error_steps", [])),
            ) if resume_surface_allowed else None,
            "approval_recovery_message": _workflow_recovery_message(
                workflow_name=str(run["workflow_name"]),
                trust_boundary=trust_boundary,
                pending_approval_count=len(approvals),
                availability=str(run["availability"]),
            ),
            "thread_continue_message": (
                recovery_approvals[0].get("resume_message")
                if recovery_approvals
                and isinstance(recovery_approvals[0], dict)
                and _workflow_continue_message_allowed(
                    replay_block_reason=replay_block_reason,
                    approval_context=current_approval_context,
                )
                else None
            ),
            "run_identity": run_identity,
            "artifact_registry": artifact_records_from_paths(
                list(run.get("artifact_paths", [])),
                producer=f"workflow:{run['workflow_name']}",
                run_id=run_identity,
                session_id=run.get("session_id") if isinstance(run.get("session_id"), str) else None,
                trust_boundary=trust_boundary,
                recovery_hint="Replay or resume the workflow from operator recovery controls before replacing this artifact.",
            ),
            "timeline": _timeline_entries_for_run(run, approvals=approvals),
            "failed_step_tool": (
                next(
                    (
                        str(step.get("tool") or "")
                        for step in run.get("step_records", []) or []
                        if isinstance(step, dict)
                        and str(step.get("id") or "") in set(run.get("continued_error_steps", []))
                    ),
                    None,
                )
            ),
            **lineage,
        })
        if resume_surface_allowed:
            run["checkpoint_candidates"] = _workflow_checkpoint_candidates(run, approvals=recovery_approvals)
            run["resume_plan"] = _workflow_resume_plan(run, approvals=recovery_approvals)
        else:
            run["checkpoint_candidates"] = []
            run["resume_plan"] = None
        completed.append(run)

    for run_queue in pending_by_key.values():
        for run in run_queue:
            workflow_meta = workflow_manager.get_tool_metadata(str(run["tool_name"])) or {}
            workflow_status = workflow_statuses.get(str(run["workflow_name"]))
            run_identity = build_workflow_run_identity(
                run.get("session_id") if isinstance(run.get("session_id"), str) else None,
                str(run["tool_name"]),
                str(run.get("run_fingerprint") or "none"),
                run_discriminator=(
                    str(run.get("id"))
                    if isinstance(run.get("id"), str) and str(run.get("id")).strip()
                    else None
                ),
            )
            run["run_identity"] = run_identity
            approval_key = _workflow_run_approval_key(run)
            approvals = pending_by_signature.get(approval_key, [])
            try:
                recovery_approvals = [_workflow_unique_approval(run, approvals)] if approvals else []
            except HTTPException:
                recovery_approvals = []
            recorded_approval_context = _normalize_approval_context(
                run.get("approval_context"),
                workflow_name=str(run["workflow_name"]),
            )
            current_approval_context = _workflow_current_approval_context(
                workflow_name=str(run["workflow_name"]),
                workflow_meta={
                    **workflow_meta,
                    "approval_context": workflow_runtime_contexts.get(str(run["tool_name"]))
                    or workflow_meta.get("approval_context"),
                },
            )
            effective_approval_context = recorded_approval_context or current_approval_context
            approval_context_mismatch = bool(
                recorded_approval_context is not None
                and recorded_approval_context != current_approval_context
            )
            approval_context_missing_for_protected_surface = bool(
                recorded_approval_context is None
                and approval_context_requires_tracked_lineage(current_approval_context)
            )
            trust_boundary = _workflow_trust_boundary_payload(
                workflow_name=str(run["workflow_name"]),
                recorded_approval_context=recorded_approval_context,
                current_approval_context=current_approval_context,
                approval_context_mismatch=approval_context_mismatch,
                approval_context_missing_for_protected_surface=approval_context_missing_for_protected_surface,
            )
            run.update({
                "approval_context": effective_approval_context,
                "recorded_approval_context": recorded_approval_context,
                "current_approval_context": current_approval_context,
                "approval_context_mismatch": approval_context_mismatch,
                "trust_boundary": trust_boundary,
                "checkpoint_context_available": bool(run.get("checkpoint_context_available")),
                "risk_level": (
                    str(effective_approval_context.get("risk_level"))
                    if effective_approval_context is not None
                    else workflow_meta.get("risk_level", "high")
                ),
                "execution_boundaries": (
                    list(effective_approval_context.get("execution_boundaries", []))
                    if effective_approval_context is not None
                    else workflow_meta.get("execution_boundaries", ["unknown"])
                ),
                "accepts_secret_refs": (
                    bool(effective_approval_context.get("accepts_secret_refs", False))
                    if effective_approval_context is not None
                    else bool(workflow_meta.get("accepts_secret_refs", False))
                ),
                "status": "awaiting_approval" if len(approvals) > 0 else "running",
                "pending_approval_count": len(approvals),
                "pending_approval_ids": [approval["id"] for approval in approvals],
                "pending_approvals": approvals,
                "availability": (
                    workflow_status.get("availability", "unknown")
                    if workflow_status is not None
                    else "unknown"
                ),
                "replay_inputs": run.get("arguments") or {},
                "checkpoint_step_ids": list(run.get("checkpoint_step_ids", [])),
                "last_completed_step_id": run.get("last_completed_step_id"),
                "parameter_schema": (
                    workflow_status.get("inputs", {})
                    if workflow_status is not None and isinstance(workflow_status.get("inputs"), dict)
                    else {}
                ),
                "replay_recommended_actions": _workflow_replay_recommended_actions(workflow_status),
            })
            step_records = run.get("step_records") or []
            if isinstance(step_records, list):
                for step in step_records:
                    if not isinstance(step, dict):
                        continue
                    step["recovery_actions"] = _step_recovery_recommended_actions(
                        step=step,
                        workflow_status=workflow_status,
                    )
                    step["recovery_hint"] = _step_recovery_hint(step)
                    step["is_recoverable"] = bool(step["recovery_actions"])
            replay_allowed, replay_block_reason = _workflow_replay_policy(
                availability=str(run["availability"]),
                risk_level=str(run["risk_level"]),
                execution_boundaries=list(run["execution_boundaries"]),
                accepts_secret_refs=bool(run["accepts_secret_refs"]),
                pending_approval_count=len(approvals),
                approval_context_mismatch=bool(run.get("approval_context_mismatch")),
                approval_context_missing_for_protected_surface=approval_context_missing_for_protected_surface,
            )
            repair_surface_allowed = _workflow_repair_surface_allowed(
                replay_block_reason=replay_block_reason,
            )
            if not repair_surface_allowed:
                run["replay_recommended_actions"] = []
                if isinstance(step_records, list):
                    for step in step_records:
                        if not isinstance(step, dict):
                            continue
                        step["recovery_actions"] = []
                        step["recovery_hint"] = None
                        step["is_recoverable"] = False
            lineage = _workflow_branch_lineage(
                run_identity=run_identity,
                details={},
                approvals=recovery_approvals,
                continued_error_steps=list(run.get("continued_error_steps", [])),
            )
            resume_surface_allowed = _workflow_resume_surface_allowed(
                replay_block_reason=replay_block_reason,
            )
            resume_from_step = (
                (
                    "approval_gate"
                    if recovery_approvals
                    else (run["continued_error_steps"][0] if run.get("continued_error_steps") else None)
                )
                if resume_surface_allowed
                else None
            )
            retry_from_step_draft = (
                _workflow_retry_from_step_draft(
                    str(run["workflow_name"]),
                    step_id=str(run["continued_error_steps"][0]),
                    arguments=run.get("arguments"),
                    parent_run_identity=run_identity,
                    root_run_identity=str(lineage.get("root_run_identity") or run_identity),
                    branch_kind="retry_failed_step",
                    branch_depth=int(lineage.get("branch_depth") or 0) + 1,
                )
                if resume_surface_allowed
                and replay_allowed
                and run.get("continued_error_steps")
                and (
                    bool(run.get("checkpoint_context_available"))
                    or (
                        isinstance(run.get("step_records"), list)
                        and run.get("step_records")
                        and str(run["continued_error_steps"][0]) == str(run["step_records"][0].get("id") or "")
                    )
                )
                else None
            )
            run.update({
                "thread_id": run.get("session_id"),
                "thread_label": (
                    session_titles.get(str(run["session_id"]))
                    if run.get("session_id")
                    else None
                ),
                "thread_source": "session" if run.get("session_id") else "ambient",
                "replay_allowed": replay_allowed,
                "replay_block_reason": replay_block_reason,
                "replay_draft": (
                    _workflow_replay_draft(str(run["workflow_name"]), run.get("arguments"))
                    if replay_allowed
                    else None
                ),
                "resume_from_step": resume_from_step,
                "retry_from_step_draft": retry_from_step_draft,
                "resume_checkpoint_label": _resume_checkpoint_label(
                    approvals=recovery_approvals,
                    continued_error_steps=list(run.get("continued_error_steps", [])),
                ) if resume_surface_allowed else None,
                "approval_recovery_message": _workflow_recovery_message(
                    workflow_name=str(run["workflow_name"]),
                    trust_boundary=trust_boundary,
                    pending_approval_count=len(approvals),
                    availability=str(run["availability"]),
                ),
                "thread_continue_message": (
                    recovery_approvals[0].get("resume_message")
                    if recovery_approvals
                    and isinstance(recovery_approvals[0], dict)
                    and _workflow_continue_message_allowed(
                        replay_block_reason=replay_block_reason,
                        approval_context=current_approval_context,
                    )
                    else None
                ),
                "run_identity": run_identity,
                "artifact_registry": artifact_records_from_paths(
                    list(run.get("artifact_paths", [])),
                    producer=f"workflow:{run['workflow_name']}",
                    run_id=run_identity,
                    session_id=run.get("session_id") if isinstance(run.get("session_id"), str) else None,
                    trust_boundary=trust_boundary,
                    recovery_hint="Replay or resume the workflow from operator recovery controls before replacing this artifact.",
                ),
                "timeline": _timeline_entries_for_run(run, approvals=approvals),
                "failed_step_tool": (
                    next(
                        (
                            str(step.get("tool") or "")
                            for step in run.get("step_records", []) or []
                            if isinstance(step, dict)
                            and str(step.get("id") or "") in set(run.get("continued_error_steps", []))
                        ),
                        None,
                    )
                ),
                **lineage,
            })
            if resume_surface_allowed:
                run["checkpoint_candidates"] = _workflow_checkpoint_candidates(run, approvals=recovery_approvals)
                run["resume_plan"] = _workflow_resume_plan(run, approvals=recovery_approvals)
            else:
                run["checkpoint_candidates"] = []
                run["resume_plan"] = None
            completed.append(run)

    try:
        durable_runs = await workflow_state_repository.list_runs(limit=limit, session_id=session_id)
    except Exception:
        durable_runs = []
    try:
        # Schema-v2 rows and their checkpoint/effect ledgers are owned by the
        # typed repository.  Keep the legacy query for compatibility, then
        # let the canonical projection replace any same-identity legacy view.
        typed_runs = await durable_job_repository.list_jobs(limit=limit, session_id=session_id)
    except Exception:
        typed_runs = []
    durable_runs.extend(typed_runs)
    completed_by_identity = {
        str(run.get("run_identity") or run.get("id")): run
        for run in completed
        if run.get("run_identity") or run.get("id")
    }
    for durable_run in durable_runs:
        is_typed = _is_typed_workflow_run(durable_run)
        if is_typed:
            durable_run = _canonical_workflow_projection_input(durable_run) or durable_run
            # Typed rows do not own approval rows, but their operator-facing
            # projection must carry the exact pending approval receipts that
            # can authorize recovery. Join by the durable session/tool/
            # fingerprint key; identity matching below remains authoritative.
            typed_approval_key = _workflow_run_approval_key(durable_run)
            typed_approvals = pending_by_signature.get(typed_approval_key, [])
            durable_run["pending_approvals"] = typed_approvals
            durable_run["pending_approval_ids"] = [
                str(approval.get("id") or "").strip()
                for approval in typed_approvals
                if str(approval.get("id") or "").strip()
            ]
            durable_run["pending_approval_count"] = len(typed_approvals)
        run_identity = str(durable_run.get("run_identity") or durable_run.get("id"))
        if not run_identity:
            continue
        existing = completed_by_identity.get(run_identity, {})
        audit_projection_available = bool(existing)
        durable_only_replay_allowed = False
        durable_only_replay_block_reason = "durable_projection_missing"
        merged = {
            **existing,
            **durable_run,
            "status": durable_run.get("status") or existing.get("status"),
            "availability": existing.get("availability") or (
                durable_run.get("availability") if is_typed else "audit_projection_missing"
            ),
            "checkpoint_candidates": existing.get("checkpoint_candidates") or (
                _workflow_checkpoint_candidates(durable_run, approvals=[])
                if is_typed and durable_run.get("checkpoint_context_available")
                else []
            ),
            "resume_plan": existing.get("resume_plan") if not is_typed else None,
            "replay_allowed": (
                durable_run.get("replay_allowed", durable_only_replay_allowed)
                if is_typed
                else (
                    existing.get("replay_allowed", durable_only_replay_allowed)
                    if audit_projection_available
                    else durable_only_replay_allowed
                )
            ),
            "replay_block_reason": (
                durable_run.get("replay_block_reason")
                if is_typed
                else (
                    existing.get("replay_block_reason")
                    if audit_projection_available
                    else durable_only_replay_block_reason
                )
            ),
            "state_source": "durable_workflow_state",
            "audit_projection_available": audit_projection_available or is_typed,
        }
        completed_by_identity[run_identity] = merged
    completed = list(completed_by_identity.values())

    completed.sort(
        key=lambda item: datetime.fromisoformat(str(item["updated_at"]).replace("Z", "+00:00")),
        reverse=True,
    )
    return completed[:limit]


_REPO_CHANGE_JOB_KIND = "engineering.repo-change.v1"
_REPO_CHANGE_CAPABILITY_VERSION = "engineering.repo-change.v1"
_REPO_CHANGE_SERVICE_LEASE = "service:repo-change"
_REPO_CHANGE_RECOVERY_LEASE = "service:repo-change:recovery"
_REPO_CHANGE_APPROVAL_TTL_SECONDS = 5 * 60
_REPO_CHANGE_JOB_TTL_SECONDS = 10 * 60


def _repo_change_workspace_root() -> Path:
    return Path(settings.workspace_dir).expanduser().resolve()


def _repo_change_repository_ref_for_compare(value: str) -> str:
    """Canonicalize a repository reference without probing Docker or contents."""

    workspace = _repo_change_workspace_root()
    candidate = Path(str(value or "")).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.absolute()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "repo_change_idempotency_conflict", "fields": ["repository_path"]}) from exc
    try:
        resolved = candidate.resolve(strict=False)
        relative = resolved.relative_to(workspace)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"code": "repo_change_idempotency_conflict", "fields": ["repository_path"]}) from exc
    return relative.as_posix()


def _repo_change_safe_relative(value: str, *, field_name: str) -> str:
    text = str(value or "").strip()
    path = PurePosixPath(text)
    if not text or "\x00" in text or path.is_absolute() or ".." in path.parts:
        raise HTTPException(status_code=422, detail={"code": f"{field_name}_invalid"})
    normalized = path.as_posix()
    if normalized in {"", "."} or normalized.startswith("/"):
        raise HTTPException(status_code=422, detail={"code": f"{field_name}_invalid"})
    return normalized


def _repo_change_patch_candidates(artifact_id: str) -> tuple[str, ...]:
    if not _WORKFLOW_SAFE_ARTIFACT_ID_RE.fullmatch(str(artifact_id or "")):
        raise HTTPException(status_code=422, detail={"code": "patch_artifact_id_invalid"})
    return (
        f"artifacts/repo-change/{artifact_id}.patch",
        f".seraph/repo-change/artifacts/{artifact_id}.patch",
    )


def _repo_change_open_workspace_file(
    relative_path: str,
    *,
    flags: int,
    mode: int = 0o600,
    create_parents: bool = False,
) -> int:
    """Open one workspace file through no-follow directory descriptors.

    Every ancestor is opened with ``O_NOFOLLOW`` and the final file is opened
    with the same flag.  Holding the directory descriptors across the final
    open prevents a workspace symlink swap from redirecting a patch read or
    result write after a lexical containment check.
    """

    relative = PurePosixPath(str(relative_path))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RepoSandboxError("repository artifact path is invalid")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    root = _repo_change_workspace_root()
    try:
        parent_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise RepoSandboxError("repository workspace root is unavailable") from exc
    try:
        for component in relative.parts[:-1]:
            if create_parents:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(
            relative.parts[-1],
            flags | nofollow | cloexec,
            mode,
            dir_fd=parent_fd,
        )
        try:
            file_stat = os.fstat(descriptor)
        except OSError:
            os.close(descriptor)
            raise
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            raise RepoSandboxError("repository artifact is not a regular file")
        if file_stat.st_nlink != 1:
            os.close(descriptor)
            raise RepoSandboxError("repository artifact hardlink is not allowed")
        return descriptor
    except OSError as exc:
        if exc.errno in {getattr(os, "ELOOP", 40), getattr(os, "ENOTDIR", 20)}:
            raise RepoSandboxError("repository artifact path contains a symlink") from exc
        raise
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _repo_change_read_patch(artifact_id: str) -> bytes:
    """Read an approved patch through a descriptor-safe workspace path."""

    candidates = _repo_change_patch_candidates(artifact_id)
    for relative in candidates:
        try:
            descriptor = _repo_change_open_workspace_file(relative, flags=os.O_RDONLY)
        except FileNotFoundError:
            continue
        except RepoSandboxError as exc:
            if "symlink" in str(exc):
                raise HTTPException(status_code=422, detail={"code": "patch_artifact_symlink"}) from exc
            if "hardlink" in str(exc):
                raise HTTPException(status_code=422, detail={"code": "patch_artifact_hardlink"}) from exc
            continue
        try:
            with os.fdopen(descriptor, "rb") as handle:
                # Read one byte beyond the fixed profile bound so an
                # oversized artifact is rejected before an unbounded
                # allocation can occur.  The same profile limit is bound into
                # the durable authority and worker input contract.
                max_patch_bytes = RepoSandboxLimits.from_settings(settings.repo_sandbox).max_patch_bytes
                payload = handle.read(max_patch_bytes + 1)
                if len(payload) > max_patch_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail={"code": "patch_artifact_too_large", "max_bytes": max_patch_bytes},
                    )
                return payload
        except OSError as exc:
            raise HTTPException(status_code=422, detail={"code": "patch_artifact_unavailable"}) from exc
    raise HTTPException(status_code=422, detail={"code": "patch_artifact_unavailable"})


def _repo_change_write_artifact(job_id: str, name: str, payload: bytes) -> str:
    """Write one fixed result artifact without following workspace symlinks."""

    if not re.fullmatch(r"repo-change-[0-9a-f]{32}", str(job_id or "")):
        raise RepoSandboxError("repository job artifact identity is invalid")
    if name not in {"manifest.json", "readback.json", "diff.patch", "pytest.stdout", "pytest.stderr"}:
        raise RepoSandboxError("repository result artifact name is not allowed")
    if not isinstance(payload, bytes):
        raise RepoSandboxError("repository result artifact must be bytes")
    relative = f"artifacts/repo-change/{job_id}/{name}"
    descriptor = _repo_change_open_workspace_file(
        relative,
        flags=os.O_WRONLY | os.O_CREAT,
        create_parents=True,
    )
    try:
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RepoSandboxError("repository result artifact write failed") from exc
    return relative


def _repo_change_dispatch_checkpoint(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the latest durable Docker dispatch fence payload."""

    checkpoints = job.get("checkpoints") if isinstance(job, dict) else None
    if not isinstance(checkpoints, list):
        return None
    for item in reversed(checkpoints):
        payload = item.get("payload") if isinstance(item, dict) else None
        if isinstance(payload, dict) and payload.get("phase") == "docker_dispatch_reserved":
            return payload
    return None


def _repo_change_dispatch_reserved(job: dict[str, Any] | None) -> bool:
    """Return whether the durable row crossed the Docker dispatch fence."""

    return _repo_change_dispatch_checkpoint(job) is not None


def _repo_change_dispatch_contract(
    job: dict[str, Any] | None,
    authority: dict[str, Any] | None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Validate the persisted dispatch fence before adopting external work."""

    payload = _repo_change_dispatch_checkpoint(job)
    if payload is None:
        return False, "recovery_dispatch_fence_missing", None
    job = job if isinstance(job, dict) else {}
    authority = authority if isinstance(authority, dict) else {}
    token = RootlessDockerRepoSandbox._server_token(str(job.get("job_id") or ""))
    expected = {
        "job_id": str(job.get("job_id") or ""),
        "attempt": int(job.get("attempt_count") or 0),
        "authority_digest": str(job.get("authority_digest") or ""),
        "base_digest": str(authority.get("base_digest") or ""),
        "patch_sha256": str(authority.get("patch_sha256") or ""),
        "image_digest": str(authority.get("image_digest") or ""),
        "limits_digest": str(authority.get("limits_digest") or ""),
        "container_name": f"{token}-worker",
        "input_volume": f"{token}-input",
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return False, "recovery_dispatch_contract_mismatch", payload
    return True, "", payload


def _repo_change_error_code(exc: BaseException) -> str:
    """Project execution exceptions to bounded operator reason codes."""

    if isinstance(exc, RepoSandboxError) and (
        str(exc) == "output_lost"
        or (exc.phase == "output_exported" and exc.terminal_status == "failed")
    ):
        return "output_lost"
    if isinstance(exc, RepoSandboxError):
        return "repo_sandbox_execution_failed"
    return "repo_change_execution_error"


def _repo_change_patch_error_code(exc: BaseException) -> str:
    """Return a bounded reason code for an approved patch read failure."""

    if isinstance(exc, RepoSandboxError) and str(exc) == "patch_digest_changed":
        return "patch_digest_changed"
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = detail.get("code")
            if isinstance(code, str) and code.startswith("patch_artifact_"):
                return code[:80]
    return "patch_artifact_unavailable"


def _repo_change_terminal_action(status: str) -> str:
    if status in {"unknown_external_effect", "cost_liability"}:
        return "reconcile_or_cancel"
    if status in {"failed", "blocked"}:
        return "retry_or_cancel"
    return "none"


def _repo_change_cleanup_proven(cleanup: Any) -> bool:
    """Require an explicit cleanup proof before projecting cancellation."""

    return bool(
        isinstance(cleanup, dict)
        and cleanup.get("cleanup_proven") is True
        and str(cleanup.get("status") or "") in {"cancelled", "cleanup_verified"}
    )


async def _repo_change_patch_read_blocked(
    *,
    job_id: str,
    owner: str,
    fencing_token: int,
    revision: int,
    exc: BaseException,
    recovered: bool = False,
) -> dict[str, Any]:
    """Persist an operator-visible block when an approved patch cannot be read.

    Patch resolution happens after admission and approval in both the normal
    and restart paths.  Letting that read exception escape leaves the durable
    row running even though no worker can be dispatched, so this helper owns
    the terminal-to-recovery transition and its bounded operator receipt.
    """

    error_code = _repo_change_patch_error_code(exc)
    latest = await durable_job_repository.get_job(job_id)
    latest = latest if isinstance(latest, dict) else {}
    latest_status = str(latest.get("status") or "")
    if latest_status in {
        "cancelled",
        "unknown_external_effect",
        "succeeded",
        "failed",
        "blocked",
        "cost_liability",
    }:
        return {
            "status": latest_status,
            "job_id": job_id,
            "job": latest,
            "reason_code": error_code,
            "operator_action": _repo_change_terminal_action(latest_status),
            "learning": "no_learning",
            "operator_visible": True,
        }
    latest_lease = latest.get("lease") if isinstance(latest.get("lease"), dict) else {}
    transition_owner = str(latest_lease.get("owner") or owner)
    transition_fence = int(latest_lease.get("fencing_token") or fencing_token)
    transition_revision = int(latest.get("revision") or revision)
    cleanup: dict[str, Any] | None = None
    if _repo_change_dispatch_reserved(latest):
        token = RootlessDockerRepoSandbox._server_token(job_id)
        try:
            cleanup = RootlessDockerRepoSandbox().cancel(
                container_name=f"{token}-worker",
                additional_container_names=(f"{token}-loader",),
                input_volume=f"{token}-input",
            )
        except (OSError, RepoSandboxError, ValueError) as cleanup_exc:
            cleanup = {
                "status": "unknown_external_effect",
                "reason": "cleanup_unproven",
                "error": type(cleanup_exc).__name__,
            }
    cleanup_proven = _repo_change_cleanup_proven(cleanup)
    transition_status = "blocked" if cleanup is None or cleanup_proven else "unknown_external_effect"
    result = {
        "learning": "no_learning",
        "memory_status": "no_learning",
        "reason_code": error_code,
        "phase": "approved_patch_read",
        "recovery_action": "create_fresh_preview_and_approval",
        "recovered": recovered,
        "cleanup": cleanup,
        "cleanup_proven": cleanup_proven,
        "operator_action": "retry_or_cancel" if transition_status == "blocked" else "reconcile_or_cancel",
    }
    blocked = await durable_job_repository.transition_job(
        job_id,
        transition_status,
        owner=transition_owner,
        fencing_token=transition_fence,
        expected_revision=transition_revision,
        reason=error_code,
        result=result,
        result_summary=(
            "Approved patch artifact could not be read after verified worker cleanup; create a fresh preview or cancel the job."
            if transition_status == "blocked"
            else "Approved patch artifact could not be read and worker cleanup is unproven; reconcile before retry."
        ),
    )
    return {
        "status": transition_status,
        "job_id": job_id,
        "job": blocked,
        "reason_code": error_code,
        "operator_action": "retry_or_cancel" if transition_status == "blocked" else "reconcile_or_cancel",
        "learning": "no_learning",
        "operator_visible": True,
    }


async def _record_repo_change_goal_outcome(
    *,
    job: dict[str, Any],
    authority: dict[str, Any],
    artifact_ref: str,
    recovered: bool = False,
) -> GoalOutcomeReceipt:
    """Write the canonical no-learning goal outcome after verified readback."""

    job_id = str(job.get("job_id") or "")
    candidate_id = str(authority.get("candidate_id") or job.get("candidate_id") or "")
    goal_id = str(job.get("goal_id") or "")
    goal_revision = int(job.get("goal_revision") or 0)
    if not candidate_id or not goal_id or goal_revision < 1:
        raise ValueError("repo-change goal outcome binding is incomplete")
    inputs = {
        "job_id": job_id,
        "base_digest": str(authority.get("base_digest") or ""),
        "patch_sha256": str(authority.get("patch_sha256") or ""),
    }
    dedupe_key = f"repo-change-outcome:{job_id}"
    evidence_refs = _safe_evidence_refs(
        *(authority.get("evidence_refs") or []),
        f"repo-change-job:{job_id}",
        f"artifact:{artifact_ref}",
    )
    candidate = GoalCandidateDecision(
        candidate_id=candidate_id,
        dedupe_key=dedupe_key,
        goal_id=goal_id,
        goal_revision=goal_revision,
        action=GoalCandidateAction.act,
        reason="repo_change_readback_verified",
        evidence_refs=list(evidence_refs),
        capability_id=_REPO_CHANGE_JOB_KIND,
        capability_version=_REPO_CHANGE_CAPABILITY_VERSION,
        inputs=inputs,
        expected_outcome="bounded repository change completed with verified readback",
    )
    receipt = GoalOutcomeReceipt(
        receipt_type="outcome",
        outcome_id="out_" + hashlib.sha256(
            f"{dedupe_key}:succeeded:{artifact_ref}".encode("utf-8")
        ).hexdigest()[:24],
        candidate_id=candidate_id,
        dedupe_key=dedupe_key,
        decision_input_digest=hashlib.sha256(
            json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        goal_id=goal_id,
        goal_revision=goal_revision,
        execution_status="succeeded",
        verification="passed",
        usefulness="unknown",
        learning="no_learning",
        artifact_ref=artifact_ref,
        evidence_refs=list(evidence_refs),
        reason="repo_change_readback_verified_recovered" if recovered else "repo_change_readback_verified",
    )
    await _persist_receipt_compat(
        event_type=_OUTCOME_EVENT,
        summary=f"Goal candidate {candidate_id} repository outcome recorded",
        details=_outcome_receipt_details(receipt, capability_id=_REPO_CHANGE_JOB_KIND),
        candidate=candidate,
        goal=None,
    )
    return receipt


async def _repo_change_local_finalize_pending(
    *,
    job_id: str,
    owner: str,
    fencing_token: int,
    revision: int,
    retry: bool = False,
    recovered: bool = False,
    error_type: str = "Exception",
) -> dict[str, Any]:
    """Leave a durable blocked receipt when local result finalization fails."""

    latest = await durable_job_repository.get_job(job_id)
    if latest is not None and str(latest.get("status") or "") in {
        "cancelled", "unknown_external_effect", "succeeded", "failed",
    }:
        status = str(latest.get("status"))
        return {
            "status": status,
            "job_id": job_id,
            "job": latest,
            "operator_action": "reconcile_or_cancel" if status == "unknown_external_effect" else "none",
            "learning": "no_learning",
        }
    current_revision = int((latest or {}).get("revision") or revision)
    try:
        blocked = await durable_job_repository.transition_job(
            job_id,
            "blocked",
            # Keep the caller's original fence.  Never adopt a newer lease
            # merely because local finalization failed after a race.
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=current_revision,
            reason="local_finalize_pending",
            result={
                "reason_code": "local_finalize_pending",
                "recovery_action": "local_finalize",
                "error_type": str(error_type or "Exception")[:80],
                "learning": "no_learning",
                "memory_status": "no_learning",
                "retry": retry,
                "recovered": recovered,
            },
            result_summary="Execution completed but local artifact/readback finalization needs operator recovery.",
        )
        return {
            "status": "blocked",
            "job_id": job_id,
            "job": blocked,
            "reason_code": "local_finalize_pending",
            "operator_action": "local_finalize",
            "learning": "no_learning",
            "operator_visible": True,
        }
    except Exception:
        latest_after_error = await durable_job_repository.get_job(job_id)
        return {
            "status": "blocked",
            "job_id": job_id,
            "job": latest_after_error or latest,
            "reason_code": "local_finalize_pending",
            "operator_action": "local_finalize",
            "learning": "no_learning",
            "operator_visible": True,
        }


async def _repo_change_reconcile_verified_local_result(
    *,
    job: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, Any] | None:
    """Finalize a verified local result before attempting worker adoption."""

    job_id = str(job.get("job_id") or "")
    if not job_id:
        return None
    expected_path = f"artifacts/repo-change/{job_id}/readback.json"
    effects = job.get("effects") if isinstance(job.get("effects"), list) else []
    readback = next(
        (
            item
            for item in reversed(effects)
            if isinstance(item, dict)
            and str(item.get("receipt_kind") or "") == "readback"
            and str(item.get("status") or "") == "succeeded"
            and str(item.get("target_path") or "") == expected_path
            and isinstance(item.get("details"), dict)
            and item["details"].get("verified") is True
        ),
        None,
    )
    if readback is None:
        return None
    expected_digest = str(readback.get("content_sha256") or readback.get("target_digest") or "")
    if not expected_digest:
        return None
    try:
        descriptor = _repo_change_open_workspace_file(expected_path, flags=os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as handle:
            payload = handle.read(RepoSandboxLimits.from_settings(settings.repo_sandbox).max_output_bytes + 1)
        if len(payload) > RepoSandboxLimits.from_settings(settings.repo_sandbox).max_output_bytes:
            return None
    except (OSError, RepoSandboxError, ValueError):
        return None
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        return None
    current = await durable_job_repository.get_job(job_id) or job
    if str(current.get("status") or "") == "succeeded":
        return {"status": "succeeded", "job": current, "recovery": "local_finalize_already_complete"}
    if str(current.get("status") or "") == "running":
        try:
            current = await durable_job_repository.recover_stale_job(job_id)
        except (DurableJobError, ValueError):
            return None
    if str(current.get("status") or "") not in {"blocked", "failed"}:
        return None
    try:
        await _record_repo_change_goal_outcome(
            job=current,
            authority=authority,
            artifact_ref=expected_path,
            recovered=True,
        )
        finalized = await durable_job_repository.finalize_reconciled_job(
            job_id,
            owner_kind=str(current.get("owner", {}).get("kind") or "user"),
            owner_principal_id=str(current.get("owner", {}).get("principal_id") or ""),
            expected_revision=current.get("revision"),
            result={
                "readback_path": expected_path,
                "learning": "no_learning",
                "recovery": "local_finalize_reconciled",
            },
            result_summary="Verified local readback finalized after worker cleanup.",
        )
    except (DurableJobError, DurableJobTransitionError, ValueError):
        return None
    return {
        "status": "succeeded",
        "job": finalized,
        "recovery": "local_finalize_reconciled",
        "readback_path": expected_path,
        "learning": "no_learning",
    }


def _repo_change_patch_path(artifact_id: str) -> Path:
    candidates = _repo_change_patch_candidates(artifact_id)
    root = _repo_change_workspace_root()
    for relative in candidates:
        candidate = root / relative
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "patch_artifact_outside_workspace"}) from exc
        current = candidate
        while current != root:
            if current.is_symlink():
                raise HTTPException(status_code=422, detail={"code": "patch_artifact_symlink"})
            current = current.parent
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "patch_artifact_outside_workspace"}) from exc
        if not resolved.is_file() or resolved.is_symlink():
            raise HTTPException(status_code=422, detail={"code": "patch_artifact_unavailable"})
        try:
            if resolved.stat().st_nlink != 1:
                raise HTTPException(status_code=422, detail={"code": "patch_artifact_hardlink"})
        except OSError as exc:
            raise HTTPException(status_code=422, detail={"code": "patch_artifact_unavailable"}) from exc
        return resolved
    raise HTTPException(status_code=422, detail={"code": "patch_artifact_unavailable"})


def _repo_change_job_id(principal_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"{_REPO_CHANGE_JOB_KIND}\0{principal_id}\0{idempotency_key}".encode("utf-8")
    ).hexdigest()[:32]
    return f"repo-change-{digest}"


def _repo_change_candidate_plan_matches(watch: Any, packet: Any) -> bool:
    """Require the packet proof and active watch to share one plan revision."""

    return int(getattr(watch, "plan_revision", 0) or 0) == int(getattr(packet, "plan_revision", 0) or 0)


def _validate_repo_test_args(test_args: tuple[str, ...], allowed_paths: tuple[str, ...]) -> tuple[str, ...]:
    accepted_flags = {"pytest", "-q", "-x", "--maxfail=1", "--disable-warnings"}
    allowed = set(allowed_paths)
    if not test_args or len(test_args) > 16:
        raise HTTPException(status_code=422, detail={"code": "test_args_invalid"})
    normalized: list[str] = []
    named_path = False
    for value in test_args:
        item = str(value).strip()
        if not item or len(item.encode("utf-8")) > 4096:
            raise HTTPException(status_code=422, detail={"code": "test_args_invalid"})
        if item in accepted_flags:
            if item != "pytest":
                normalized.append(item)
            continue
        path = _repo_change_safe_relative(item, field_name="test_arg")
        if path not in allowed:
            raise HTTPException(status_code=422, detail={"code": "test_arg_outside_allowed_paths"})
        named_path = True
        normalized.append(path)
    if not named_path:
        raise HTTPException(status_code=422, detail={"code": "test_path_required"})
    if sum(len(item.encode("utf-8")) for item in normalized) > 64 * 1024:
        raise HTTPException(status_code=422, detail={"code": "test_args_invalid"})
    return tuple(normalized)


async def _resolve_repo_change_candidate(
    *,
    candidate_id: str,
    goal_id: str,
    goal_revision: int,
    owner_principal_id: str,
    owner_session_id: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    """Resolve the M1 proof used by repository execution.

    The request may name a candidate, but it cannot manufacture the authority
    chain.  A successful, owner-bound M1 packet is the canonical candidate
    proof for this capability; all execution bindings are derived from it.
    """
    async with get_session() as db:
        goal = (await db.execute(select(Goal).where(Goal.id == goal_id))).scalars().first()
        if goal is None or str(goal.owner_principal_id or "") != owner_principal_id or str(goal.owner_session_id or "") != owner_session_id:
            raise HTTPException(status_code=404, detail={"code": "goal_not_owned"})
        if int(goal.revision or 0) != int(goal_revision) or str(getattr(goal.status, "value", goal.status) or "") != "active":
            raise HTTPException(status_code=409, detail={"code": "goal_revision_stale"})
        packet = (
            await db.execute(
                select(GuardianDecisionPacket).where(
                    GuardianDecisionPacket.id == candidate_id,
                    GuardianDecisionPacket.goal_id == goal_id,
                    GuardianDecisionPacket.goal_revision == goal_revision,
                    GuardianDecisionPacket.status == "succeeded",
                    GuardianDecisionPacket.verification_status == "passed",
                )
            )
        ).scalars().first()
        if packet is None:
            raise HTTPException(status_code=409, detail={"code": "m1_candidate_proof_required"})
        watch = (
            await db.execute(
                select(GuardianSourceWatch).where(
                    GuardianSourceWatch.id == packet.source_watch_id,
                    GuardianSourceWatch.owner_principal_id == owner_principal_id,
                    GuardianSourceWatch.goal_id == goal_id,
                    GuardianSourceWatch.state == "active",
                )
            )
        ).scalars().first()
        if (
            watch is None
            or int(watch.goal_revision or 0) != int(goal_revision)
            or not _repo_change_candidate_plan_matches(watch, packet)
            or not packet.dossier_artifact_id
            or not packet.dossier_sha256
        ):
            raise HTTPException(status_code=409, detail={"code": "m1_candidate_proof_stale"})
        canonical_refs = {
            f"guardian-packet:{packet.id}",
            str(packet.id),
            str(packet.dossier_artifact_id),
        }
        supplied = {str(item).strip() for item in evidence_refs if str(item).strip()}
        if supplied and not supplied.intersection(canonical_refs):
            raise HTTPException(status_code=409, detail={"code": "m1_evidence_binding_mismatch"})
        return {
            "source_watch_id": packet.source_watch_id,
            "source_watch_job_id": packet.run_identity,
            "source_packet_id": packet.id,
            "source_plan_revision": int(packet.plan_revision),
            "dossier_artifact_id": str(packet.dossier_artifact_id),
            "dossier_sha256": str(packet.dossier_sha256),
            "evidence_refs": sorted(canonical_refs),
        }


def _repo_change_dispatch_payload(
    *,
    current: dict[str, Any],
    claimed: dict[str, Any],
    authority: dict[str, Any],
    retry: bool,
) -> dict[str, Any]:
    """Build the dispatch fence from the post-claim durable job identity."""

    job_id = str(current["job_id"])
    dispatch_token = RootlessDockerRepoSandbox._server_token(job_id)
    return {
        "phase": "docker_dispatch_reserved",
        "job_id": job_id,
        "attempt": int(claimed.get("attempt_count") or 0),
        "authority_digest": str(current.get("authority_digest") or ""),
        "base_digest": str(authority.get("base_digest") or ""),
        "patch_sha256": str(authority.get("patch_sha256") or ""),
        "image_digest": str(authority.get("image_digest") or ""),
        "limits_digest": str(authority.get("limits_digest") or ""),
        "container_name": f"{dispatch_token}-worker",
        "input_volume": f"{dispatch_token}-input",
        "retry": retry,
    }


async def _execute_repo_change_claimed(
    *,
    current: dict[str, Any],
    authority: dict[str, Any],
    claimed: dict[str, Any],
    approval_id: str | None = None,
    retry: bool = False,
) -> dict[str, Any]:
    """Run one already-approved, fenced repository job exactly once."""
    lease = claimed.get("lease") or {}
    owner = str(lease.get("owner") or _REPO_CHANGE_SERVICE_LEASE)
    fencing_token = int(lease.get("fencing_token") or 0)
    revision = int(claimed.get("revision") or 0)

    async def record_repo_phase(
        phase: str,
        *,
        checkpoint_payload: dict[str, Any] | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        nonlocal owner, fencing_token, revision
        latest = await durable_job_repository.get_job(str(current["job_id"])) or claimed
        latest_lease = latest.get("lease") or lease
        updated = await durable_job_repository.record_checkpoint(
            str(current["job_id"]),
            checkpoint_id=phase,
            state={"phase": phase, **details},
            checkpoint_payload=checkpoint_payload,
            owner=str(latest_lease.get("owner") or owner),
            fencing_token=int(latest_lease.get("fencing_token") or fencing_token),
            expected_revision=int(latest.get("revision") or revision),
        )
        owner = str((updated.get("lease") or {}).get("owner") or owner)
        fencing_token = int((updated.get("lease") or {}).get("fencing_token") or fencing_token)
        revision = int(updated.get("revision") or revision)
        return updated

    await record_repo_phase(
        "admitted",
        job_status="accepted",
        retry=retry,
    )
    await record_repo_phase(
        "approved",
        approval_id=approval_id or str(authority.get("approval_id") or ""),
        retry=retry,
    )
    try:
        try:
            patch = _repo_change_read_patch(str(authority.get("patch_artifact_id") or ""))
        except (HTTPException, OSError) as exc:
            return await _repo_change_patch_read_blocked(
                job_id=str(current["job_id"]),
                owner=owner,
                fencing_token=fencing_token,
                revision=revision,
                exc=exc,
            )
        if hashlib.sha256(patch).hexdigest() != str(authority.get("patch_sha256") or ""):
            return await _repo_change_patch_read_blocked(
                job_id=str(current["job_id"]),
                owner=owner,
                fencing_token=fencing_token,
                revision=revision,
                exc=RepoSandboxError("patch_digest_changed"),
            )
        await record_repo_phase("worker_started", retry=retry)

        dispatch_loop = asyncio.get_running_loop()
        dispatch_payload = _repo_change_dispatch_payload(
            current=current,
            claimed=claimed,
            authority=authority,
            retry=retry,
        )

        async def reserve_dispatch() -> dict[str, Any]:
            # This CAS is the final durable boundary before the sandbox's
            # first Docker volume/container creation.  A cancellation that
            # wins first changes the row to terminal and makes this write
            # fail; a cancellation that arrives after this write must retain
            # an unknown-effect recovery state.
            return await record_repo_phase(
                "docker_dispatch_reserved",
                checkpoint_payload=dispatch_payload,
                container_name=dispatch_payload["container_name"],
                input_volume=dispatch_payload["input_volume"],
                retry=retry,
            )

        def dispatch_guard() -> None:
            future = asyncio.run_coroutine_threadsafe(reserve_dispatch(), dispatch_loop)
            try:
                future.result(timeout=10)
            except Exception as exc:
                future.cancel()
                raise RepoSandboxError("repo_change_dispatch_fence_lost", phase="worker_started") from exc

        result = await asyncio.to_thread(
            RootlessDockerRepoSandbox().execute_job,
            RepoSandboxJob(
                job_id=str(current["job_id"]), repository_root=str(authority.get("repository_ref") or ""),
                patch_bytes=patch, allowed_paths=tuple(authority.get("allowed_paths") or ()),
                test_args=tuple(authority.get("test_args") or ()), authority_digest=str(current.get("authority_digest") or ""),
                base_digest=str(authority.get("base_digest") or ""), deadline_seconds=int(authority.get("deadline_seconds", 180)),
                worker_image_digest=str(authority.get("image_digest") or ""),
                limits_digest=str(authority.get("limits_digest") or ""),
            ),
            before_dispatch=dispatch_guard,
        )
        result_phases = result.get("checkpoint_phases") if isinstance(result, dict) else []
        for phase in ("snapshot_verified", "input_loaded", "tests_finished", "output_exported"):
            if isinstance(result_phases, list) and phase in result_phases:
                await record_repo_phase(
                    phase,
                    profile=authority.get("profile"),
                    image_digest=authority.get("image_digest"),
                    retry=retry,
                )
    except (OSError, RepoSandboxError) as exc:
        latest = await durable_job_repository.get_job(str(current["job_id"]))
        if latest and str(latest.get("status") or "") in {"cancelled", "unknown_external_effect"}:
            status = str(latest.get("status"))
            return {
                "status": status,
                "job_id": current["job_id"],
                "job": latest,
                "operator_action": "reconcile_or_cancel" if status == "unknown_external_effect" else "none",
                "learning": "no_learning",
            }
        latest_lease = (latest or {}).get("lease") or lease
        if isinstance(exc, RepoSandboxError):
            for phase in exc.checkpoint_phases:
                if phase in {"snapshot_verified", "input_loaded", "tests_finished", "output_exported"}:
                    try:
                        await record_repo_phase(phase, error_phase=True, retry=retry)
                    except Exception:
                        break
        failure_status = "failed" if isinstance(exc, RepoSandboxError) and exc.terminal_status == "failed" else "blocked"
        error_code = _repo_change_error_code(exc)
        try:
            await durable_job_repository.transition_job(
                str(current["job_id"]),
                failure_status,
                owner=str(latest_lease.get("owner") or owner),
                fencing_token=int(latest_lease.get("fencing_token") or fencing_token),
                expected_revision=(await durable_job_repository.get_job(str(current["job_id"])) or latest or {}).get("revision"),
                reason=error_code,
                result={
                    "learning": "no_learning",
                    "memory_status": "no_learning",
                    "reason_code": error_code,
                    "phase": getattr(exc, "phase", "admitted"),
                    "operator_action": "inspect_output_and_create_fresh_preview" if error_code == "output_lost" else "reconcile_or_cancel",
                    "retry": retry,
                },
            )
        except Exception:
            pass
        return {
            "status": failure_status,
            "reason_code": error_code,
            "job_id": current["job_id"],
            "operator_action": "inspect_output_and_create_fresh_preview" if error_code == "output_lost" else "reconcile_or_cancel",
            "learning": "no_learning",
        }

    latest = await durable_job_repository.get_job(str(current["job_id"]))
    latest_lease = (latest or {}).get("lease") or lease
    owner = str(latest_lease.get("owner") or owner)
    fencing_token = int(latest_lease.get("fencing_token") or fencing_token)
    revision = int((latest or {}).get("revision") or 0)
    if latest and str(latest.get("status") or "") in {"cancelled", "unknown_external_effect"}:
        status = str(latest.get("status"))
        return {
            "status": status,
            "job_id": current["job_id"],
            "job": latest,
            "operator_action": "reconcile_or_cancel" if status == "unknown_external_effect" else "none",
            "learning": "no_learning",
        }
    if result.get("status") == "unknown_external_effect":
        unknown_result = {
            "learning": "no_learning",
            "memory_status": "no_learning",
            "retry": retry,
            "operator_action": "reconcile_or_cancel",
            "operator_visible": True,
        }
        await durable_job_repository.transition_job(
            str(current["job_id"]), "unknown_external_effect", owner=owner, fencing_token=fencing_token,
            expected_revision=revision, reason="cleanup_unproven",
            result=unknown_result,
        )
        return {"status": "unknown_external_effect", "job_id": current["job_id"], **unknown_result}
    outputs = result.get("outputs") if isinstance(result, dict) else {}
    manifest = result.get("manifest") if isinstance(result, dict) else {}
    outputs = outputs if isinstance(outputs, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    result_phases = result.get("checkpoint_phases") if isinstance(result, dict) else []
    if result.get("status") == "blocked":
        if isinstance(result_phases, list):
            for phase in result_phases:
                if phase in {"snapshot_verified", "input_loaded", "tests_finished", "output_exported"}:
                    await record_repo_phase(phase, profile=authority.get("profile"), image_digest=authority.get("image_digest"), retry=retry)
        latest = await durable_job_repository.get_job(str(current["job_id"])) or latest or current
        latest_lease = latest.get("lease") or latest_lease
        blocked = await durable_job_repository.transition_job(
            str(current["job_id"]), "blocked",
            owner=str(latest_lease.get("owner") or owner),
            fencing_token=int(latest_lease.get("fencing_token") or fencing_token),
            expected_revision=int(latest.get("revision") or revision),
            reason=str(result.get("reason") or "sandbox_preflight_blocked"),
            result={"learning": "no_learning", "memory_status": "no_learning", "preflight": result.get("preflight"), "retry": retry},
        )
        return {
            "status": "blocked",
            "job_id": current["job_id"],
            "learning": "no_learning",
            "operator_action": "retry_or_cancel",
            "job": blocked,
        }
    try:
        if isinstance(result.get("cleanup"), dict) and result["cleanup"].get("status") == "cleanup_verified":
            cleanup_checkpoint = await record_repo_phase("cleanup_verified", retry=retry)
            revision = int(cleanup_checkpoint.get("revision") or revision)
        written: dict[str, str] = {}
        for name in ("manifest.json", "readback.json", "diff.patch", "pytest.stdout", "pytest.stderr"):
            payload = outputs.get(name)
            if not isinstance(payload, bytes):
                continue
            written[name] = _repo_change_write_artifact(str(current["job_id"]), name, payload)
            latest = await durable_job_repository.record_artifact(
                str(current["job_id"]), file_path=written[name], artifact_type="repo_change_" + name.replace(".", "_"),
                owner=owner, fencing_token=fencing_token, expected_revision=revision,
            )
            revision = int(latest.get("revision") or revision)
        readback_path = written.get("readback.json")
        readback_bytes = outputs.get("readback.json")
        if result.get("status") == "succeeded" and readback_path and isinstance(readback_bytes, bytes):
            latest = await durable_job_repository.record_readback(
                str(current["job_id"]), target_path=readback_path, status="succeeded",
                target_digest=hashlib.sha256(readback_bytes).hexdigest(), content_sha256=hashlib.sha256(readback_bytes).hexdigest(),
                details={"verified": True, "output_exists": True, "workspace_contained": True, "goal_id_read_back": bool(current.get("goal_id")), "learning": "no_learning", "retry": retry},
                owner=owner, fencing_token=fencing_token, expected_revision=revision,
            )
            revision = int(latest.get("revision") or revision)
            latest = await durable_job_repository.get_job(str(current["job_id"])) or latest
            latest_lease = latest.get("lease") or latest_lease
            checkpoint = await durable_job_repository.record_checkpoint(
                str(current["job_id"]), checkpoint_id="readback_verified",
                state={"phase": "readback_verified", "readback_path": readback_path, "retry": retry},
                owner=owner, fencing_token=fencing_token, expected_revision=int(latest.get("revision") or revision),
            )
            revision = int(checkpoint.get("revision") or revision)
            await _record_repo_change_goal_outcome(
                job=latest,
                authority=authority,
                artifact_ref=readback_path,
            )
            await durable_job_repository.transition_job(
                str(current["job_id"]), "succeeded", owner=owner, fencing_token=fencing_token, expected_revision=revision,
                reason="repo_change_readback_verified", result={"readback_path": readback_path, "learning": "no_learning", "manifest": manifest, "retry": retry},
                result_summary="Bounded repository change completed with verified readback.",
            )
        else:
            await durable_job_repository.transition_job(
                str(current["job_id"]), "failed", owner=owner, fencing_token=fencing_token, expected_revision=revision,
                reason="worker_failed",
                result={"learning": "no_learning", "memory_status": "no_learning", "manifest": manifest, "retry": retry},
            )
    except Exception as exc:
        return await _repo_change_local_finalize_pending(
            job_id=str(current["job_id"]),
            owner=owner,
            fencing_token=fencing_token,
            revision=revision,
            retry=retry,
            error_type=type(exc).__name__,
        )
    return (await durable_job_repository.get_job(str(current["job_id"]))) or {"status": "failed", "job_id": current["job_id"]}


async def _run_repo_change_after_approval(*, job: dict[str, Any], operator: Any, approval_id: str) -> dict[str, Any]:
    current = await durable_job_repository.get_job(str(job["job_id"]))
    if current is None:
        raise HTTPException(status_code=404, detail={"code": "repo_change_job_not_found"})
    authority = current.get("declared_authority") or {}
    if not isinstance(authority, dict):
        raise HTTPException(status_code=409, detail={"code": "repo_change_authority_corrupt"})
    approval = await approval_repository.get(approval_id)
    if approval is None or str(getattr(approval, "status", "")) != "approved":
        raise HTTPException(status_code=409, detail={"code": "approval_not_current"})
    try:
        details = json.loads(getattr(approval, "details_json", None) or "{}")
    except (TypeError, ValueError):
        details = {}
    if not isinstance(details, dict):
        raise HTTPException(status_code=409, detail={"code": "approval_binding_corrupt"})
    expires_at = details.get("approval_expires_at", details.get("expires_at"))
    if expires_at is None and getattr(approval, "expires_at", None) is not None:
        expires_at = approval.expires_at.timestamp()
    try:
        expires_at = float(expires_at)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=409, detail={"code": "approval_expiry_invalid"}) from exc
    operator_principal_id = str(operator.principal.principal_id)
    operator_session_id = str(operator.session_id)
    receipt = {
        "status": "approved", "authenticated": True,
        "operator_principal_id": operator_principal_id, "operator_session_id": operator_session_id,
        "owner_kind": current["owner"]["kind"], "owner_principal_id": current["owner"]["principal_id"],
        "service_id": current["owner"].get("service_id"), "approval_id": approval_id,
        "authority_digest": current.get("authority_digest"), "goal_id": current.get("goal_id"),
        "goal_revision": current.get("goal_revision"), "plan_revision": current.get("plan_revision"),
        "capability_version": current.get("capability_version"), "budget_microusd": 0,
        "budget_digest": current.get("budget_digest"), "expires_at": expires_at,
    }
    try:
        resumed = await durable_job_repository.resume_approved_job(
            str(current["job_id"]), approval_receipt=receipt, approval_id=approval_id,
            authority_digest=str(current.get("authority_digest") or ""), goal_id=current.get("goal_id"),
            goal_revision=current.get("goal_revision"), plan_revision=current.get("plan_revision"),
            capability_version=str(current.get("capability_version") or ""), owner_kind=str(current["owner"]["kind"]),
            owner_principal_id=str(current["owner"]["principal_id"]), service_id=current["owner"].get("service_id"),
            budget_microusd=0, budget_digest=str(current.get("budget_digest") or ""),
            operator_principal_id=operator_principal_id, operator_session_id=operator_session_id,
            expires_at=expires_at, expected_revision=current.get("revision"),
        )
        queued_lease = resumed.get("lease") or {}
        claimed = await durable_job_repository.claim_job(
            str(current["job_id"]), owner=_REPO_CHANGE_SERVICE_LEASE,
            expected_revision=resumed.get("revision"), expected_fencing_token=queued_lease.get("fencing_token"),
            lease_seconds=int(authority.get("deadline_seconds", 180)) + 60,
        )
    except (DurableJobError, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"code": "approval_resume_blocked", "reason": str(exc)}) from exc

    return await _execute_repo_change_claimed(
        current=current,
        authority=authority,
        claimed=claimed,
        approval_id=approval_id,
    )


async def _recover_repo_change_preview_job(*, job: dict[str, Any], operator: Any) -> dict[str, Any]:
    """Finish an interrupted admission without re-probing Docker or the repo."""

    current = await durable_job_repository.get_job(str(job.get("job_id") or "")) or job
    status = str(current.get("status") or "")
    if status == "accepted":
        current = await durable_job_repository.queue_job(
            str(current["job_id"]),
            expected_revision=current.get("revision"),
            expected_fencing_token=int(current.get("fencing_token") or 0),
        )
        status = str(current.get("status") or "")
    if status == "queued":
        current = await durable_job_repository.claim_job(
            str(current["job_id"]),
            owner=_REPO_CHANGE_SERVICE_LEASE,
            expected_revision=current.get("revision"),
            expected_fencing_token=(current.get("lease") or {}).get("fencing_token"),
            lease_seconds=360,
        )
        status = str(current.get("status") or "")
    if status != "running":
        return {"status": status, "job": current, "deduped": True, "operator_visible": True}
    authority = current.get("declared_authority") if isinstance(current.get("declared_authority"), dict) else {}
    principal_id = str(operator.principal.principal_id)
    session_id = str(operator.session_id)
    lease = current.get("lease") if isinstance(current.get("lease"), dict) else {}
    fingerprint = fingerprint_tool_call(
        _REPO_CHANGE_JOB_KIND,
        {
            "job_id": current.get("job_id"),
            "patch_sha256": authority.get("patch_sha256"),
            "candidate_id": authority.get("candidate_id"),
            "source_packet_id": authority.get("source_packet_id"),
        },
    )
    approval = await approval_repository.get_or_create_pending(
        session_id=session_id,
        tool_name=_REPO_CHANGE_JOB_KIND,
        risk_level="high",
        summary=f"Approve bounded repository change for goal {current.get('goal_id')}",
        fingerprint=fingerprint,
        details={
            "approval_owner_principal_id": principal_id,
            "approval_owner_operator_session_id": session_id,
            "approval_operator_principal_id": principal_id,
            "approval_execution_owner_principal_id": principal_id,
            "approval_execution_session_id": session_id,
            "approval_conversation_id": session_id,
            "durable_job_id": current.get("job_id"),
            "durable_owner_kind": "user",
            "durable_owner_principal_id": principal_id,
            "durable_authority_digest": current.get("authority_digest"),
            "durable_goal_id": current.get("goal_id"),
            "durable_goal_revision": current.get("goal_revision"),
            "durable_capability_version": _REPO_CHANGE_CAPABILITY_VERSION,
            "durable_budget_digest": current.get("budget_digest"),
            "patch_sha256": authority.get("patch_sha256"),
            "base_digest": authority.get("base_digest"),
            "candidate_id": authority.get("candidate_id"),
            "evidence_refs": authority.get("evidence_refs") or [],
            "source_packet_id": authority.get("source_packet_id"),
            "dossier_artifact_id": authority.get("dossier_artifact_id"),
            "dossier_sha256": authority.get("dossier_sha256"),
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=_REPO_CHANGE_APPROVAL_TTL_SECONDS)).timestamp(),
        },
    )
    bound = await durable_job_repository.bind_approval_id(
        str(current["job_id"]),
        approval.id,
        owner=str(lease.get("owner") or _REPO_CHANGE_SERVICE_LEASE),
        fencing_token=int(lease.get("fencing_token") or 0),
        expected_revision=current.get("revision"),
    )
    await approval_repository.update_pending_details(
        approval.id,
        owner_principal_id=principal_id,
        operator_session_id=session_id,
        updates={
            "approval_id": approval.id,
            "durable_approval_id": approval.id,
            "authority_digest": bound.get("authority_digest"),
            "durable_authority_digest": bound.get("authority_digest"),
            "approval_expires_at": approval.expires_at.timestamp() if approval.expires_at else None,
        },
    )
    held = await durable_job_repository.transition_job(
        str(current["job_id"]),
        "awaiting_approval",
        owner=str(lease.get("owner") or _REPO_CHANGE_SERVICE_LEASE),
        fencing_token=int(lease.get("fencing_token") or 0),
        expected_revision=bound.get("revision"),
        reason="repo_change_approval_required_recovered",
    )
    return {
        "status": "awaiting_approval",
        "approval_id": approval.id,
        "base_digest": authority.get("base_digest"),
        "patch_sha256": authority.get("patch_sha256"),
        "profile": authority.get("profile"),
        "image_digest": authority.get("image_digest"),
        "allowed_paths": authority.get("allowed_paths") or [],
        "test_args": authority.get("test_args") or [],
        **held,
    }



@router.post("/workflows/repo-change/preview")
async def preview_repo_change(req: RepoChangePreviewRequest, request: Request):
    operator = _require_authenticated_capability_operator(request)
    principal_id = str(operator.principal.principal_id)
    job_id = _repo_change_job_id(principal_id, req.idempotency_key)
    existing = await durable_job_repository.get_job(job_id)
    if existing is not None:
        owner = existing.get("owner") if isinstance(existing.get("owner"), dict) else {}
        authority = existing.get("declared_authority") if isinstance(existing.get("declared_authority"), dict) else {}
        if str(owner.get("principal_id") or "") != principal_id:
            raise HTTPException(status_code=403, detail={"code": "repo_change_owner_mismatch"})
        if str(authority.get("session_id") or "") != str(operator.session_id):
            raise HTTPException(status_code=403, detail={"code": "repo_change_session_mismatch"})
        immutable_conflicts = []
        for field, requested, stored in (
            ("patch_sha256", req.patch_sha256, authority.get("patch_sha256")),
            ("candidate_id", req.candidate_id, authority.get("candidate_id")),
            ("goal_id", req.goal_id, existing.get("goal_id")),
            ("goal_revision", req.goal_revision, existing.get("goal_revision")),
            ("patch_artifact_id", req.patch_artifact_id, authority.get("patch_artifact_id")),
        ):
            if stored is not None and str(requested) != str(stored):
                immutable_conflicts.append(field)
        if immutable_conflicts:
            raise HTTPException(
                status_code=409,
                detail={"code": "repo_change_idempotency_conflict", "fields": immutable_conflicts},
            )
        # The idempotency row is authoritative.  Compare every execution
        # input before the recovery/preflight branch so a repeated key cannot
        # silently adopt a different repository, sandbox policy, or priority.
        requested_repository_ref = _repo_change_repository_ref_for_compare(req.repository_path)
        requested_allowed_paths = tuple(
            _repo_change_safe_relative(value, field_name="allowed_path")
            for value in req.allowed_paths
        )
        if len(set(requested_allowed_paths)) != len(requested_allowed_paths):
            raise HTTPException(status_code=422, detail={"code": "allowed_paths_duplicate"})
        requested_test_args = _validate_repo_test_args(
            tuple(str(value).strip() for value in req.test_args),
            requested_allowed_paths,
        )
        for field, requested, stored in (
            ("repository_path", requested_repository_ref, authority.get("repository_ref")),
            ("allowed_paths", list(requested_allowed_paths), authority.get("allowed_paths")),
            ("test_args", list(requested_test_args), authority.get("test_args")),
            ("priority", req.priority, existing.get("priority")),
            ("deadline_seconds", req.deadline_seconds, authority.get("deadline_seconds")),
        ):
            if stored is not None and requested != stored:
                immutable_conflicts.append(field)
        if immutable_conflicts:
            raise HTTPException(
                status_code=409,
                detail={"code": "repo_change_idempotency_conflict", "fields": immutable_conflicts},
            )
        status = str(existing.get("status") or "")
        if status in {"accepted", "queued"}:
            try:
                return await _recover_repo_change_preview_job(job=existing, operator=operator)
            except (DurableJobError, RepoSandboxError, ValueError) as exc:
                raise HTTPException(status_code=409, detail={"code": "repo_change_recovery_blocked", "reason": str(exc)}) from exc
        if status in {
            "running",
            "awaiting_approval",
            "blocked",
            "failed",
            "succeeded",
            "degraded",
            "cancelled",
            "unknown_external_effect",
            "cost_liability",
        }:
            return {"status": status, "job": existing, "deduped": True, "operator_visible": True}
    sandbox = RootlessDockerRepoSandbox()
    preflight = sandbox.preflight()
    if not preflight.ok:
        return {"status": "blocked", "reason_code": preflight.reason, "preflight": preflight.as_receipt(), "operator_visible": True}
    if not _WORKFLOW_SAFE_ARTIFACT_DIGEST_RE.fullmatch(req.patch_sha256):
        raise HTTPException(status_code=422, detail={"code": "patch_sha256_invalid"})
    allowed_paths = tuple(_repo_change_safe_relative(value, field_name="allowed_path") for value in req.allowed_paths)
    if len(set(allowed_paths)) != len(allowed_paths):
        raise HTTPException(status_code=422, detail={"code": "allowed_paths_duplicate"})
    test_args = _validate_repo_test_args(
        tuple(str(value).strip() for value in req.test_args),
        allowed_paths,
    )
    candidate_proof = await _resolve_repo_change_candidate(
        candidate_id=req.candidate_id,
        goal_id=req.goal_id,
        goal_revision=req.goal_revision,
        owner_principal_id=principal_id,
        owner_session_id=str(operator.session_id),
        evidence_refs=req.evidence_refs,
    )
    patch = _repo_change_read_patch(req.patch_artifact_id)
    if hashlib.sha256(patch).hexdigest() != req.patch_sha256:
        raise HTTPException(status_code=409, detail={"code": "patch_digest_changed"})
    repository = sandbox.validate_snapshot_root(req.repository_path)
    workspace = _repo_change_workspace_root()
    repository_ref = repository.relative_to(workspace).as_posix()
    with tempfile.TemporaryDirectory(prefix="seraph-repo-preview-") as temp_dir:
        snapshot = sandbox.snapshot_repository(repository, Path(temp_dir) / "snapshot")
    image = sandbox.validate_image_digest(sandbox.config.worker_image_digest)
    authority = {
        "principal": principal_id, "owner_kind": "user", "session_id": str(operator.session_id),
        "capability_version": _REPO_CHANGE_CAPABILITY_VERSION, "profile": str(sandbox.config.profile),
        "image_digest": image, "base_digest": snapshot.digest, "patch_artifact_id": req.patch_artifact_id,
        "patch_sha256": req.patch_sha256, "repository_ref": repository_ref, "allowed_paths": list(allowed_paths),
        "test_args": list(test_args), "deadline_seconds": int(req.deadline_seconds), "budget_microusd": 0,
        "limits_digest": limits_digest(sandbox.limits),
        "candidate_id": req.candidate_id, "evidence_refs": candidate_proof["evidence_refs"],
        "source_watch_id": candidate_proof["source_watch_id"],
        "source_watch_job_id": candidate_proof["source_watch_job_id"],
        "source_packet_id": candidate_proof["source_packet_id"],
        "source_plan_revision": candidate_proof["source_plan_revision"],
        "dossier_artifact_id": candidate_proof["dossier_artifact_id"],
        "dossier_sha256": candidate_proof["dossier_sha256"],
    }
    spec = DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=job_id, owner_kind="user", owner_principal_id=principal_id, job_kind=_REPO_CHANGE_JOB_KIND,
            capability_version=_REPO_CHANGE_CAPABILITY_VERSION, idempotency_scope="repo-change", idempotency_key=req.idempotency_key,
        ),
        inputs={"base_digest": snapshot.digest, "patch_sha256": req.patch_sha256, "candidate_id": req.candidate_id, "repository_ref": repository_ref, "allowed_paths": list(allowed_paths), "test_args": list(test_args), **candidate_proof},
        session_id=str(operator.session_id), operator_session_id=str(operator.session_id), goal_id=req.goal_id,
        goal_revision=req.goal_revision, candidate_id=req.candidate_id, priority=req.priority, declared_authority=authority,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=_REPO_CHANGE_JOB_TTL_SECONDS), max_attempts=1,
        run_fingerprint=hashlib.sha256(json.dumps(authority, sort_keys=True).encode("utf-8")).hexdigest(), budget_microusd=0,
    )
    try:
        admitted = await durable_job_repository.admit_job(spec)
        admission_receipt = admitted.get("receipt") if isinstance(admitted, dict) else None
        admission_is_deduped = (
            isinstance(admission_receipt, dict)
            and admission_receipt.get("status") == "deduped"
        )
        existing_status = str(admitted.get("status") or "")
        if admission_is_deduped and existing_status not in {"accepted", "queued"}:
            # The durable admission row is authoritative for repeated or
            # terminal invocations.  Do not create a fresh approval for a
            # running, awaiting, failed, blocked, or terminal job.
            return admitted
        if existing_status == "accepted":
            admitted = await durable_job_repository.queue_job(
                job_id,
                expected_revision=admitted.get("revision"),
                expected_fencing_token=int(admitted.get("fencing_token") or 0),
            )
            existing_status = str(admitted.get("status") or "")
        if existing_status == "queued":
            queued = admitted
            claimed = await durable_job_repository.claim_job(
                job_id,
                owner=_REPO_CHANGE_SERVICE_LEASE,
                expected_revision=queued.get("revision"),
                expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"),
                lease_seconds=360,
            )
        elif existing_status == "accepted":
            raise DurableJobTransitionError("repo-change admission could not be queued")
        else:
            queued = await durable_job_repository.queue_job(job_id, expected_revision=admitted.get("revision"))
            claimed = await durable_job_repository.claim_job(
                job_id, owner=_REPO_CHANGE_SERVICE_LEASE, expected_revision=queued.get("revision"),
                expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"), lease_seconds=360,
            )
        if claimed.get("status") == "awaiting_approval":
            return {"status": "awaiting_approval", **claimed}
        lease = claimed.get("lease") or {}
        fingerprint = fingerprint_tool_call(
            _REPO_CHANGE_JOB_KIND,
            {"job_id": job_id, "base_digest": snapshot.digest, "patch_sha256": req.patch_sha256, "candidate_id": req.candidate_id, "source_packet_id": candidate_proof["source_packet_id"]},
        )
        approval = await approval_repository.get_or_create_pending(
            session_id=str(operator.session_id), tool_name=_REPO_CHANGE_JOB_KIND, risk_level="high",
            summary=f"Approve bounded repository change for goal {req.goal_id}", fingerprint=fingerprint,
            details={
                "approval_owner_principal_id": principal_id, "approval_owner_operator_session_id": str(operator.session_id),
                "approval_operator_principal_id": principal_id, "approval_execution_owner_principal_id": principal_id,
                "approval_execution_session_id": str(operator.session_id), "approval_conversation_id": str(operator.session_id),
                "durable_job_id": job_id, "durable_owner_kind": "user", "durable_owner_principal_id": principal_id,
                "durable_authority_digest": claimed.get("authority_digest"), "durable_goal_id": req.goal_id,
                "durable_goal_revision": req.goal_revision, "durable_capability_version": _REPO_CHANGE_CAPABILITY_VERSION,
                "durable_budget_digest": claimed.get("budget_digest"), "patch_sha256": req.patch_sha256,
                "base_digest": snapshot.digest, "candidate_id": req.candidate_id,
                "evidence_refs": candidate_proof["evidence_refs"],
                "source_packet_id": candidate_proof["source_packet_id"],
                "dossier_artifact_id": candidate_proof["dossier_artifact_id"],
                "dossier_sha256": candidate_proof["dossier_sha256"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=_REPO_CHANGE_APPROVAL_TTL_SECONDS)).timestamp(),
            },
        )
        bound = await durable_job_repository.bind_approval_id(
            job_id, approval.id, owner=str(lease.get("owner") or _REPO_CHANGE_SERVICE_LEASE),
            fencing_token=int(lease.get("fencing_token") or 0), expected_revision=claimed.get("revision"),
        )
        await approval_repository.update_pending_details(
            approval.id, owner_principal_id=principal_id, operator_session_id=str(operator.session_id),
            updates={"approval_id": approval.id, "durable_approval_id": approval.id, "authority_digest": bound.get("authority_digest"), "durable_authority_digest": bound.get("authority_digest"), "approval_expires_at": approval.expires_at.timestamp() if approval.expires_at else None},
        )
        held = await durable_job_repository.transition_job(
            job_id, "awaiting_approval", owner=str(lease.get("owner") or _REPO_CHANGE_SERVICE_LEASE),
            fencing_token=int(lease.get("fencing_token") or 0), expected_revision=bound.get("revision"), reason="repo_change_approval_required",
        )
        return {"status": "awaiting_approval", "approval_id": approval.id, "base_digest": snapshot.digest, "patch_sha256": req.patch_sha256, "profile": str(sandbox.config.profile), "image_digest": image, "allowed_paths": list(allowed_paths), "test_args": list(test_args), "limits": {field: getattr(sandbox.limits, field) for field in sandbox.limits.__dataclass_fields__}, **held}
    except (DurableJobError, RepoSandboxError, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"code": "repo_change_admission_blocked", "reason": str(exc)}) from exc


@router.get("/workflows/repo-change/{job_id}")
async def get_repo_change(job_id: str, request: Request):
    operator = _require_authenticated_capability_operator(request)
    job = await durable_job_repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "repo_change_job_not_found"})
    if str(job.get("owner", {}).get("principal_id") or "") != str(operator.principal.principal_id):
        raise HTTPException(status_code=403, detail={"code": "repo_change_owner_mismatch"})
    if str((job.get("declared_authority") or {}).get("session_id") or "") != str(operator.session_id):
        raise HTTPException(status_code=403, detail={"code": "repo_change_session_mismatch"})
    status = str(job.get("status") or "")
    operator_action = (
        "recover_or_cancel" if status in {"running", "unknown_external_effect"}
        else "resume_or_preview" if status in {"accepted", "queued"}
        else "retry_or_cancel" if status in {"failed", "blocked"}
        else "none"
    )
    return {"status": status, "job": job, "operator_action": operator_action}


@router.post("/workflows/repo-change/{job_id}/resume")
async def resume_repo_change(job_id: str, req: RepoChangeResumeRequest, request: Request):
    operator = _require_authenticated_capability_operator(request)
    job = await durable_job_repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "repo_change_job_not_found"})
    if str(job.get("owner", {}).get("principal_id") or "") != str(operator.principal.principal_id):
        raise HTTPException(status_code=403, detail={"code": "repo_change_owner_mismatch"})
    if str((job.get("declared_authority") or {}).get("session_id") or "") != str(operator.session_id):
        raise HTTPException(status_code=403, detail={"code": "repo_change_session_mismatch"})
    return await _run_repo_change_after_approval(job=job, operator=operator, approval_id=req.approval_id)


@router.post("/workflows/repo-change/{job_id}/retry")
async def retry_repo_change(job_id: str, req: RepoChangeRetryRequest, request: Request):
    """Require a fresh preview and approval instead of replaying this job."""

    operator = _require_authenticated_capability_operator(request)
    job = await durable_job_repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "repo_change_job_not_found"})
    principal_id = str(operator.principal.principal_id)
    owner = job.get("owner") if isinstance(job.get("owner"), dict) else {}
    if str(owner.get("principal_id") or "") != principal_id:
        raise HTTPException(status_code=403, detail={"code": "repo_change_owner_mismatch"})
    if str((job.get("declared_authority") or {}).get("session_id") or "") != str(operator.session_id):
        raise HTTPException(status_code=403, detail={"code": "repo_change_session_mismatch"})
    try:
        canonical_receipt, receipt_digest = _canonical_reconciliation_receipt(req.reconciliation_receipt)
        reconciliation_receipt = json.loads(canonical_receipt)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "repo_change_reconciliation_receipt_invalid", "operator_visible": True},
        ) from exc
    status = str(job.get("status") or "")
    if status in {"unknown_external_effect", "cost_liability", "running"}:
        return {
            "status": status,
            "job": job,
            "operator_action": "reconcile_or_cancel" if status != "running" else "recover_or_cancel",
            "operator_visible": True,
        }
    if status not in {"failed", "blocked"}:
        return {"status": status, "job": job, "operator_action": "none", "operator_visible": True}
    checkpoints = job.get("checkpoints") if isinstance(job.get("checkpoints"), list) else []
    retry_checkpoint = next(
        (
            item
            for item in checkpoints
            if isinstance(item, dict) and item.get("checkpoint_id") == "retry_requires_fresh_approval"
        ),
        None,
    )
    retry_payload = retry_checkpoint.get("payload") if isinstance(retry_checkpoint, dict) else None
    retry_payload = retry_payload if isinstance(retry_payload, dict) else {}
    existing_receipt_digest = retry_payload.get("reconciliation_receipt_digest")
    if existing_receipt_digest is not None and str(existing_receipt_digest) != receipt_digest:
        raise HTTPException(
            status_code=409,
            detail={"code": "repo_change_reconciliation_receipt_conflict", "operator_visible": True},
        )
    already_recorded = existing_receipt_digest == receipt_digest
    if not already_recorded:
        try:
            job = await durable_job_repository.record_recovery_checkpoint(
                job_id,
                owner_kind=str(owner.get("kind") or "user"),
                owner_principal_id=principal_id,
                checkpoint_id="retry_requires_fresh_approval",
                state={
                    "phase": "retry_requires_fresh_approval",
                    "fresh_preview_required": True,
                    "fresh_approval_required": True,
                    "prior_attempt_count": int(job.get("attempt_count") or 0),
                    "reconciliation_receipt_digest": receipt_digest,
                },
                checkpoint_payload={
                    "route": "repo_change_retry",
                    "reason_code": "retry_requires_fresh_approval",
                    "fresh_preview_required": True,
                    "fresh_approval_required": True,
                    "reconciliation_receipt": reconciliation_receipt,
                    "reconciliation_receipt_digest": receipt_digest,
                },
                expected_revision=job.get("revision"),
            )
        except (DurableJobError, ValueError) as exc:
            latest = await durable_job_repository.get_job(job_id)
            latest_checkpoint = next(
                (
                    item
                    for item in (latest.get("checkpoints") or [])
                    if isinstance(item, dict) and item.get("checkpoint_id") == "retry_requires_fresh_approval"
                ),
                None,
            ) if latest is not None else None
            latest_payload = latest_checkpoint.get("payload") if isinstance(latest_checkpoint, dict) else None
            latest_payload = latest_payload if isinstance(latest_payload, dict) else {}
            if latest is not None and latest_payload.get("reconciliation_receipt_digest") == receipt_digest:
                job = latest
            else:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "repo_change_reconciliation_receipt_conflict", "operator_visible": True},
                ) from exc
    return {
        "status": "blocked",
        "durable_status": str(job.get("status") or status),
        "job": job,
        "reason_code": "retry_requires_fresh_approval",
        "operator_action": "create_fresh_preview_and_approval",
        "operator_visible": True,
        "reconciliation_receipt_digest": receipt_digest,
        "learning": "no_learning",
    }


@router.post("/workflows/repo-change/{job_id}/cancel")
async def cancel_repo_change(job_id: str, req: RepoChangeCancelRequest, request: Request):
    operator = _require_authenticated_capability_operator(request)
    job = await durable_job_repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "repo_change_job_not_found"})
    if str(job.get("owner", {}).get("principal_id") or "") != str(operator.principal.principal_id):
        raise HTTPException(status_code=403, detail={"code": "repo_change_owner_mismatch"})
    if str((job.get("declared_authority") or {}).get("session_id") or "") != str(operator.session_id):
        raise HTTPException(status_code=403, detail={"code": "repo_change_session_mismatch"})
    status = str(job.get("status") or "")
    if status in {"queued", "awaiting_approval", "accepted", "paused", "blocked"}:
        if status == "blocked" and _repo_change_dispatch_reserved(job):
            authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), dict) else {}
            dispatch_ok, dispatch_reason, dispatch_payload = _repo_change_dispatch_contract(job, authority)
            if not dispatch_ok or dispatch_payload is None:
                uncertain = await durable_job_repository.transition_job(
                    job_id,
                    "unknown_external_effect",
                    expected_revision=job.get("revision"),
                    reason=dispatch_reason or "recovery_dispatch_contract_mismatch",
                    result={
                        "learning": "no_learning",
                        "memory_status": "no_learning",
                        "dispatch_contract": "mismatch",
                        "operator_action": "reconcile_or_cancel",
                    },
                )
                return {
                    "status": uncertain.get("status"),
                    "job": uncertain,
                    "operator_action": "reconcile_or_cancel",
                }
            token = RootlessDockerRepoSandbox._server_token(job_id)
            try:
                cleanup = RootlessDockerRepoSandbox().cancel(
                    container_name=str(dispatch_payload["container_name"]),
                    additional_container_names=(f"{token}-loader",),
                    input_volume=str(dispatch_payload["input_volume"]),
                )
            except (OSError, RepoSandboxError, ValueError) as exc:
                cleanup = {"status": "unknown_external_effect", "reason": "cleanup_unproven", "error": type(exc).__name__}
            cleanup_proven = _repo_change_cleanup_proven(cleanup)
            settled_status = "cancelled" if cleanup_proven else "unknown_external_effect"
            settled = await durable_job_repository.transition_job(
                job_id,
                settled_status,
                expected_revision=job.get("revision"),
                reason=req.reason if cleanup_proven else "cleanup_unproven",
                result={
                    "learning": "no_learning",
                    "memory_status": "no_learning",
                    "cleanup": cleanup,
                    "cleanup_proven": cleanup_proven,
                    "operator_action": "none" if cleanup_proven else "reconcile_or_cancel",
                },
            )
            return {
                "status": settled.get("status"),
                "job": settled,
                "cleanup": cleanup,
                "operator_action": "none" if cleanup_proven else "reconcile_or_cancel",
            }
        cancelled = await durable_job_repository.transition_job(
            job_id,
            "cancelled",
            reason=req.reason,
            expected_revision=job.get("revision"),
            result={"learning": "no_learning", "memory_status": "no_learning"},
        )
        return {"status": cancelled.get("status"), "job": cancelled}
    if status == "running":
        token = RootlessDockerRepoSandbox._server_token(job_id)
        lease = job.get("lease") or {}
        try:
            checkpoint = await durable_job_repository.record_checkpoint(
                job_id,
                checkpoint_id="cancel_requested",
                state={"phase": "cancel_requested", "reason": req.reason},
                owner=str(lease.get("owner") or ""),
                fencing_token=int(lease.get("fencing_token") or 0),
                expected_revision=job.get("revision"),
            )
        except (DurableJobError, ValueError) as exc:
            latest = await durable_job_repository.get_job(job_id)
            if latest is not None and str(latest.get("status") or "") in {
                "cancelled",
                "unknown_external_effect",
                "succeeded",
                "failed",
                "blocked",
            }:
                latest_status = str(latest.get("status"))
                return {
                    "status": latest_status,
                    "job": latest,
                    "operator_action": (
                        "reconcile_or_cancel"
                        if latest_status == "unknown_external_effect"
                        else "retry_or_cancel"
                        if latest_status in {"failed", "blocked"}
                        else "none"
                    ),
                }
            raise HTTPException(
                status_code=409,
                detail={"code": "repo_change_cancel_race", "reason": str(exc), "operator_visible": True},
            ) from exc

        latest = await durable_job_repository.get_job(job_id) or checkpoint
        latest_status = str(latest.get("status") or "")
        if latest_status in {"cancelled", "unknown_external_effect", "succeeded", "failed", "blocked"}:
            return {
                "status": latest_status,
                "job": latest,
                "operator_action": (
                    "reconcile_or_cancel"
                    if latest_status == "unknown_external_effect"
                    else "retry_or_cancel"
                    if latest_status in {"failed", "blocked"}
                    else "none"
                ),
            }
        checkpoint_lease = latest.get("lease") or checkpoint.get("lease") or lease
        dispatch_reserved = _repo_change_dispatch_reserved(latest) or _repo_change_dispatch_reserved(checkpoint)
        dispatch_payload = None
        if dispatch_reserved:
            authority = latest.get("declared_authority") if isinstance(latest.get("declared_authority"), dict) else {}
            dispatch_ok, dispatch_reason, dispatch_payload = _repo_change_dispatch_contract(latest, authority)
            if not dispatch_ok or dispatch_payload is None:
                uncertain = await durable_job_repository.transition_job(
                    job_id,
                    "unknown_external_effect",
                    owner=str(checkpoint_lease.get("owner") or "") or None,
                    fencing_token=int(checkpoint_lease.get("fencing_token") or 0) or None,
                    expected_revision=latest.get("revision"),
                    reason=dispatch_reason or "recovery_dispatch_contract_mismatch",
                    result={
                        "learning": "no_learning",
                        "memory_status": "no_learning",
                        "dispatch_contract": "mismatch",
                        "operator_action": "reconcile_or_cancel",
                    },
                )
                return {
                    "status": uncertain.get("status"),
                    "reason_code": dispatch_reason or "recovery_dispatch_contract_mismatch",
                    "operator_action": "reconcile_or_cancel",
                    "job": uncertain,
                }
        worker_name = str((dispatch_payload or {}).get("container_name") or f"{token}-worker")
        input_volume = str((dispatch_payload or {}).get("input_volume") or f"{token}-input")
        try:
            cleanup = RootlessDockerRepoSandbox().cancel(
                container_name=worker_name,
                additional_container_names=(f"{token}-loader",),
                input_volume=input_volume,
            )
        except (OSError, RepoSandboxError, ValueError) as exc:
            latest_after_error = await durable_job_repository.get_job(job_id) or latest
            if str(latest_after_error.get("status") or "") in {
                "cancelled",
                "unknown_external_effect",
                "succeeded",
                "failed",
                "blocked",
            }:
                latest_status = str(latest_after_error.get("status"))
                return {
                    "status": latest_status,
                    "reason_code": "cancel_race",
                    "job": latest_after_error,
                    "operator_action": "reconcile_or_cancel" if latest_status == "unknown_external_effect" else "none",
                }
            uncertain = await durable_job_repository.transition_job(
                job_id,
                "unknown_external_effect",
                owner=str(checkpoint_lease.get("owner") or ""),
                fencing_token=int(checkpoint_lease.get("fencing_token") or 0),
                expected_revision=latest_after_error.get("revision"),
                reason="cancel_unavailable",
                result={
                    "learning": "no_learning",
                    "memory_status": "no_learning",
                    "cleanup": {"status": "unknown_external_effect", "reason": "cancel_unavailable", "error": str(exc)},
                },
            )
            return {
                "status": "unknown_external_effect",
                "reason_code": "cancel_unavailable",
                "operator_action": "reconcile_or_cancel",
                "job": uncertain,
                "learning": "no_learning",
            }
        latest_after_cleanup = await durable_job_repository.get_job(job_id) or latest
        latest_status = str(latest_after_cleanup.get("status") or "")
        if latest_status in {"cancelled", "unknown_external_effect", "succeeded", "failed", "blocked"}:
            return {
                "status": latest_status,
                "cleanup": cleanup,
                "job": latest_after_cleanup,
                "operator_action": (
                    "reconcile_or_cancel"
                    if latest_status == "unknown_external_effect"
                    else "retry_or_cancel"
                    if latest_status in {"failed", "blocked"}
                    else "none"
                ),
            }
        dispatch_reserved = dispatch_reserved or _repo_change_dispatch_reserved(latest_after_cleanup)
        cleanup_proven = _repo_change_cleanup_proven(cleanup)
        # A missing dispatch fence does not prove that cleanup happened.  Keep
        # the durable row uncertain until the cancel receipt explicitly proves
        # the worker and volume are gone.
        cleanup_unproven = not cleanup_proven
        # Dispatch alone is not an unresolved effect once the exact worker and
        # input volume removal are proven.  Reserve the uncertain state for a
        # cleanup receipt that cannot establish what remains externally.
        transition_status = "unknown_external_effect" if cleanup_unproven else "cancelled"
        transition_lease = latest_after_cleanup.get("lease") or checkpoint_lease
        transition_revision = latest_after_cleanup.get("revision")
        transition_reason = "cleanup_unproven" if cleanup_unproven else req.reason
        transition_result = (
            {
                "learning": "no_learning",
                "memory_status": "no_learning",
                "cleanup": cleanup,
                "dispatch_reserved": dispatch_reserved,
                "cleanup_proven": cleanup_proven,
                "operator_action": "reconcile_or_cancel" if transition_status == "unknown_external_effect" else "none",
            }
        )
        try:
            updated = await durable_job_repository.transition_job(
                job_id,
                transition_status,
                owner=str(transition_lease.get("owner") or ""),
                fencing_token=int(transition_lease.get("fencing_token") or 0),
                expected_revision=transition_revision,
                reason=transition_reason,
                result=transition_result,
            )
        except (DurableJobError, ValueError) as exc:
            latest_after_race = await durable_job_repository.get_job(job_id)
            if latest_after_race is not None and str(latest_after_race.get("status") or "") in {
                "cancelled",
                "unknown_external_effect",
                "succeeded",
                "failed",
                "blocked",
            }:
                latest_status = str(latest_after_race.get("status"))
                return {
                    "status": latest_status,
                    "cleanup": cleanup,
                    "job": latest_after_race,
                    "operator_action": (
                        "reconcile_or_cancel"
                        if latest_status == "unknown_external_effect"
                        else "retry_or_cancel"
                        if latest_status in {"failed", "blocked"}
                        else "none"
                    ),
                }
            raise HTTPException(
                status_code=409,
                detail={"code": "repo_change_cancel_race", "reason": str(exc), "operator_visible": True},
            ) from exc
        return {
            "status": updated.get("status"),
            "cleanup": cleanup,
            "job": updated,
            "operator_action": "reconcile_or_cancel" if transition_status == "unknown_external_effect" else "none",
            "learning": "no_learning",
        }
    return {"status": status, "job": job, "operator_action": "reconcile" if status == "unknown_external_effect" else "none"}


async def _recover_repo_change_after_restart(*, job: dict[str, Any], operator: Any) -> dict[str, Any]:
    """Adopt a matching worker without replaying the approved operation."""

    job_id = str(job["job_id"])
    authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), dict) else {}
    status = str(job.get("status") or "")
    local_finalized = await _repo_change_reconcile_verified_local_result(
        job=job,
        authority=authority,
    )
    if local_finalized is not None:
        return local_finalized
    if status == "unknown_external_effect":
        return {
            "status": "blocked",
            "durable_status": status,
            "reason_code": "reconciliation_required",
            "operator_action": "reconcile_or_cancel_before_retry",
            "side_effects": "none",
            "learning": "no_learning",
            "job": job,
        }
    if status != "running":
        return {"status": status, "job": job, "recovery": "terminal_or_not_running", "operator_visible": True}
    lease = job.get("lease") if isinstance(job.get("lease"), dict) else {}
    expires_at = lease.get("expires_at")
    try:
        lease_active = expires_at is not None and datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        lease_active = False
    if lease_active:
        return {
            "status": "blocked",
            "durable_status": status,
            "reason_code": "recovery_lease_active",
            "operator_action": "wait_for_worker_or_cancel",
            "side_effects": "none",
            "learning": "no_learning",
            "job": job,
        }
    dispatch_ok, dispatch_reason, dispatch_payload = _repo_change_dispatch_contract(job, authority)
    if not dispatch_ok:
        try:
            stale = await durable_job_repository.recover_stale_job(job_id)
        except (DurableJobError, ValueError) as exc:
            return {
                "status": "blocked",
                "job_id": job_id,
                "reason_code": dispatch_reason,
                "operator_action": "reconcile_or_cancel" if dispatch_reason.endswith("mismatch") else "fresh_preview_required",
                "recovery_error": type(exc).__name__,
                "operator_visible": True,
                "learning": "no_learning",
            }
        return {
            "status": str(stale.get("status") or "blocked"),
            "job_id": job_id,
            "job": stale,
            "reason_code": dispatch_reason,
            "operator_action": "reconcile_or_cancel" if dispatch_reason.endswith("mismatch") else "fresh_preview_required",
            "operator_visible": True,
            "learning": "no_learning",
        }
    try:
        claimed = await durable_job_repository.transfer_lease(
            job_id,
            # A normal worker already uses ``service:repo-change``.  Recovery
            # must transfer to a distinct owner so the durable CAS can fence
            # the expired worker instead of rejecting an identical-owner
            # transfer.
            to_owner=_REPO_CHANGE_RECOVERY_LEASE,
            from_owner=str(lease.get("owner") or ""),
            expected_revision=job.get("revision"),
            expected_fencing_token=lease.get("fencing_token"),
            lease_seconds=int(authority.get("deadline_seconds", 180)) + 60,
            expected_state="running",
        )
    except (DurableJobError, ValueError) as exc:
        return {
            "status": "blocked",
            "job_id": job_id,
            "reason_code": "restart_lease_recovery_blocked",
            "reason": "lease_transfer_failed",
            "error_type": type(exc).__name__,
            "operator_visible": True,
            "learning": "no_learning",
        }

    lease = claimed.get("lease") if isinstance(claimed.get("lease"), dict) else {}
    owner = str(lease.get("owner") or _REPO_CHANGE_SERVICE_LEASE)
    fencing_token = int(lease.get("fencing_token") or 0)
    revision = int(claimed.get("revision") or 0)

    async def record_repo_phase(phase: str, **details: Any) -> dict[str, Any]:
        nonlocal owner, fencing_token, revision
        latest = await durable_job_repository.get_job(job_id) or claimed
        latest_lease = latest.get("lease") if isinstance(latest.get("lease"), dict) else lease
        updated = await durable_job_repository.record_checkpoint(
            job_id,
            checkpoint_id=phase,
            state={"phase": phase, **details},
            owner=str(latest_lease.get("owner") or owner),
            fencing_token=int(latest_lease.get("fencing_token") or fencing_token),
            expected_revision=int(latest.get("revision") or revision),
        )
        owner = str((updated.get("lease") or {}).get("owner") or owner)
        fencing_token = int((updated.get("lease") or {}).get("fencing_token") or fencing_token)
        revision = int(updated.get("revision") or revision)
        return updated

    await record_repo_phase("admitted", job_status="accepted")
    await record_repo_phase("approved", recovery=True)
    try:
        try:
            patch = _repo_change_read_patch(str(authority.get("patch_artifact_id") or ""))
        except (HTTPException, OSError) as exc:
            return await _repo_change_patch_read_blocked(
                job_id=job_id,
                owner=owner,
                fencing_token=fencing_token,
                revision=revision,
                exc=exc,
                recovered=True,
            )
        if hashlib.sha256(patch).hexdigest() != str(authority.get("patch_sha256") or ""):
            return await _repo_change_patch_read_blocked(
                job_id=job_id,
                owner=owner,
                fencing_token=fencing_token,
                revision=revision,
                exc=RepoSandboxError("patch_digest_changed", phase="admitted"),
                recovered=True,
            )
        result = await asyncio.to_thread(
            RootlessDockerRepoSandbox().recover_job,
            RepoSandboxJob(
                job_id=job_id,
                repository_root=str(authority.get("repository_ref") or ""),
                patch_bytes=patch,
                allowed_paths=tuple(authority.get("allowed_paths") or ()),
                test_args=tuple(authority.get("test_args") or ()),
                authority_digest=str(job.get("authority_digest") or ""),
                base_digest=str(authority.get("base_digest") or ""),
                deadline_seconds=int(authority.get("deadline_seconds", 180)),
                worker_image_digest=str(authority.get("image_digest") or ""),
                limits_digest=str(authority.get("limits_digest") or ""),
            ),
            expected_container_name=str((dispatch_payload or {}).get("container_name") or ""),
            expected_input_volume=str((dispatch_payload or {}).get("input_volume") or ""),
        )
    except (OSError, RepoSandboxError) as exc:
        latest = await durable_job_repository.get_job(job_id) or claimed
        latest_lease = latest.get("lease") if isinstance(latest.get("lease"), dict) else lease
        failure_status = "failed" if isinstance(exc, RepoSandboxError) and exc.terminal_status == "failed" else "blocked"
        error_code = _repo_change_error_code(exc)
        try:
            await durable_job_repository.transition_job(
                job_id,
                failure_status,
                owner=str(latest_lease.get("owner") or owner),
                fencing_token=int(latest_lease.get("fencing_token") or fencing_token),
                expected_revision=latest.get("revision"),
                reason=error_code,
                result={
                    "learning": "no_learning",
                    "memory_status": "no_learning",
                    "reason_code": error_code,
                    "phase": getattr(exc, "phase", "admitted"),
                    "operator_action": "inspect_output_and_create_fresh_preview" if error_code == "output_lost" else "reconcile_or_cancel",
                },
            )
        except Exception:
            pass
        return {
            "status": failure_status,
            "job_id": job_id,
            "reason_code": error_code,
            "operator_action": "inspect_output_and_create_fresh_preview" if error_code == "output_lost" else "reconcile_or_cancel",
            "learning": "no_learning",
            "operator_visible": True,
        }

    result_phases = result.get("checkpoint_phases") if isinstance(result, dict) else []
    try:
        if isinstance(result_phases, list):
            for phase in ("snapshot_verified", "input_loaded", "tests_finished", "output_exported"):
                if phase in result_phases:
                    await record_repo_phase(phase, recovery=True, profile=authority.get("profile"), image_digest=authority.get("image_digest"))
    except Exception as exc:
        return await _repo_change_local_finalize_pending(
            job_id=job_id,
            owner=owner,
            fencing_token=fencing_token,
            revision=revision,
            recovered=True,
            error_type=type(exc).__name__,
        )
    latest = await durable_job_repository.get_job(job_id) or claimed
    latest_lease = latest.get("lease") if isinstance(latest.get("lease"), dict) else lease
    owner = str(latest_lease.get("owner") or owner)
    fencing_token = int(latest_lease.get("fencing_token") or fencing_token)
    revision = int(latest.get("revision") or revision)
    if result.get("status") == "unknown_external_effect":
        unknown_result = {
            "learning": "no_learning",
            "memory_status": "no_learning",
            "cleanup": result.get("cleanup"),
            "operator_action": "reconcile_or_cancel",
            "operator_visible": True,
        }
        unknown = await durable_job_repository.transition_job(
            job_id,
            "unknown_external_effect",
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=revision,
            reason="cleanup_unproven",
            result=unknown_result,
        )
        return {"status": "unknown_external_effect", "job_id": job_id, "job": unknown, **unknown_result}
    if result.get("reason_code") == "output_lost" or result.get("reason") in {"matching_worker_not_found", "output_lost"}:
        output_lost_result = {
            "learning": "no_learning",
            "memory_status": "no_learning",
            "reason_code": "output_lost",
            "phase": "worker_started",
            "operator_action": "inspect_output_and_create_fresh_preview",
            "recovery_action": "create_fresh_preview_and_approval",
            "recovered": True,
            "side_effects": "none",
            "recovery_receipt": {
                "status": "output_lost",
                "dispatch_fence": True,
                "worker_identity": (dispatch_payload or {}).get("container_name"),
                "operator_action": "inspect_output_and_create_fresh_preview",
            },
        }
        failed = await durable_job_repository.transition_job(
            job_id,
            "failed",
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=revision,
            reason="output_lost",
            result=output_lost_result,
        )
        return {
            "status": "failed",
            "job_id": job_id,
            "job": failed,
            "reason_code": "output_lost",
            "operator_action": "inspect_output_and_create_fresh_preview",
            "recovery_action": "create_fresh_preview_and_approval",
            "recovery_receipt": output_lost_result["recovery_receipt"],
            "learning": "no_learning",
            "operator_visible": True,
        }
    if result.get("status") == "blocked":
        blocked = await durable_job_repository.transition_job(
            job_id,
            "blocked",
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=revision,
            reason=str(result.get("reason") or "sandbox_recovery_blocked"),
            result={"learning": "no_learning", "memory_status": "no_learning", "preflight": result.get("preflight")},
        )
        return {
            "status": "blocked",
            "job_id": job_id,
            "job": blocked,
            "learning": "no_learning",
            "operator_action": "retry_or_cancel",
            "operator_visible": True,
        }
    outputs = result.get("outputs") if isinstance(result, dict) else {}
    manifest = result.get("manifest") if isinstance(result, dict) else {}
    outputs = outputs if isinstance(outputs, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    try:
        if isinstance(result.get("cleanup"), dict) and result["cleanup"].get("status") == "cleanup_verified":
            cleanup_checkpoint = await record_repo_phase("cleanup_verified", recovery=True)
            revision = int(cleanup_checkpoint.get("revision") or revision)
        written: dict[str, str] = {}
        for name in ("manifest.json", "readback.json", "diff.patch", "pytest.stdout", "pytest.stderr"):
            payload = outputs.get(name)
            if not isinstance(payload, bytes):
                continue
            written[name] = _repo_change_write_artifact(job_id, name, payload)
            receipt = await durable_job_repository.record_artifact(
                job_id,
                file_path=written[name],
                artifact_type="repo_change_" + name.replace(".", "_"),
                owner=owner,
                fencing_token=fencing_token,
                expected_revision=revision,
            )
            revision = int(receipt.get("revision") or revision)
        readback_path = written.get("readback.json")
        readback_bytes = outputs.get("readback.json")
        if result.get("status") == "succeeded" and readback_path and isinstance(readback_bytes, bytes):
            receipt = await durable_job_repository.record_readback(
                job_id,
                target_path=readback_path,
                status="succeeded",
                target_digest=hashlib.sha256(readback_bytes).hexdigest(),
                content_sha256=hashlib.sha256(readback_bytes).hexdigest(),
                details={"verified": True, "output_exists": True, "workspace_contained": True, "goal_id_read_back": bool(latest.get("goal_id")), "learning": "no_learning", "recovered": True},
                owner=owner,
                fencing_token=fencing_token,
                expected_revision=revision,
            )
            revision = int(receipt.get("revision") or revision)
            checkpoint = await durable_job_repository.record_checkpoint(
                job_id,
                checkpoint_id="readback_verified",
                state={"phase": "readback_verified", "readback_path": readback_path, "recovered": True},
                owner=owner,
                fencing_token=fencing_token,
                expected_revision=revision,
            )
            revision = int(checkpoint.get("revision") or revision)
            await _record_repo_change_goal_outcome(
                job=latest,
                authority=authority,
                artifact_ref=readback_path,
                recovered=True,
            )
            done = await durable_job_repository.transition_job(
                job_id,
                "succeeded",
                owner=owner,
                fencing_token=fencing_token,
                expected_revision=revision,
                reason="repo_change_restart_readback_verified",
                result={"readback_path": readback_path, "learning": "no_learning", "manifest": manifest, "recovered": True},
                result_summary="Bounded repository change was recovered from the matching worker and read back.",
            )
            return {"status": "succeeded", "job": done, "recovery": "worker_adopted", "learning": "no_learning"}
        failed = await durable_job_repository.transition_job(
            job_id,
            "failed",
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=revision,
            reason="worker_failed",
            result={"learning": "no_learning", "memory_status": "no_learning", "manifest": manifest, "recovered": True},
        )
        return {"status": "failed", "job": failed, "recovery": "worker_adopted", "learning": "no_learning"}
    except Exception as exc:
        return await _repo_change_local_finalize_pending(
            job_id=job_id,
            owner=owner,
            fencing_token=fencing_token,
            revision=revision,
            recovered=True,
            error_type=type(exc).__name__,
        )


@router.post("/workflows/repo-change/{job_id}/recover")
async def recover_repo_change(job_id: str, request: Request):
    """Recover one expired repository worker through its durable identity."""

    operator = _require_authenticated_capability_operator(request)
    job = await durable_job_repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "repo_change_job_not_found"})
    if str(job.get("owner", {}).get("principal_id") or "") != str(operator.principal.principal_id):
        raise HTTPException(status_code=403, detail={"code": "repo_change_owner_mismatch"})
    if str((job.get("declared_authority") or {}).get("session_id") or "") != str(operator.session_id):
        raise HTTPException(status_code=403, detail={"code": "repo_change_session_mismatch"})
    return await _recover_repo_change_after_restart(job=job, operator=operator)


@router.get("/workflows")
async def list_workflows():
    base_tools, active_skill_names, mcp_mode = get_base_tools_and_active_skills()
    workflows = []
    for workflow in workflow_manager.list_workflows(
        available_tool_names=[tool.name for tool in base_tools],
        active_skill_names=active_skill_names,
    ):
        boundaries = workflow.get("execution_boundaries", [])
        risk_level = workflow.get("risk_level", "low")
        requires_approval = (
            risk_level == "high"
            or ("external_mcp" in boundaries and mcp_mode == "approval")
        )
        if "external_mcp" in boundaries and mcp_mode == "approval":
            approval_behavior = "always"
        elif risk_level == "high":
            approval_behavior = "high_risk_mode"
        else:
            approval_behavior = "never"
        workflows.append({
            **workflow,
            "requires_approval": requires_approval,
            "approval_behavior": approval_behavior,
        })
    return {"workflows": workflows}


@router.get("/workflows/diagnostics")
async def workflow_diagnostics():
    diagnostics = workflow_manager.get_diagnostics()
    return diagnostics


@router.get("/workflows/runtimes")
async def list_workflow_runtimes():
    snapshot = _workflow_extension_snapshot()
    inventory = list_workflow_runtime_inventory(snapshot.list_contributions("workflow_runtimes"))
    return {
        "runtimes": [
            {
                "extension_id": item.extension_id,
                "name": item.name,
                "engine_kind": item.engine_kind,
                "description": item.description,
                "delegation_mode": item.delegation_mode,
                "checkpoint_policy": item.checkpoint_policy,
                "structured_output": item.structured_output,
                "default_output_surface": item.default_output_surface,
                "reference": item.reference,
            }
            for item in inventory
        ]
    }


@router.get("/workflows/{name}/source")
async def get_workflow_source(name: str):
    workflow = workflow_manager.get_workflow(name)
    if workflow is None or not workflow.file_path:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    try:
        with open(workflow.file_path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read workflow source: {exc}") from exc
    validation = _validate_workflow_content(content, path=workflow.file_path)
    return {
        "name": name,
        "file_path": workflow.file_path,
        "content": content,
        **validation,
    }


@router.post("/workflows/validate")
async def validate_workflow_draft(req: WorkflowDraftRequest):
    return _validate_workflow_content(req.content, path=req.file_name or "<draft>")


@router.post("/workflows/save")
async def save_workflow_draft(req: WorkflowDraftRequest, request: Request):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        await _workflow_session_fence(request, revocation_scope)
        validation = _validate_workflow_content(req.content, path=req.file_name or "<draft>")
        if not bool(validation["valid"]) or not isinstance(validation["workflow"], dict):
            raise HTTPException(status_code=400, detail={"message": "Workflow draft is invalid", **validation})
        file_name = _resolve_workflow_file_name(
            req.file_name,
            default_name=_safe_markdown_filename(str(validation["workflow"]["name"])),
        )
        _ensure_workflow_manager_workspace_extensions_loaded()
        await _workflow_session_fence(request, revocation_scope)
        target_path = str(save_workspace_contribution("workflows", file_name=file_name, content=req.content))
        workflows = workflow_manager.reload()
        await _workflow_session_fence(request, revocation_scope)
        await log_integration_event(
            integration_type="workflow",
            name=str(validation["workflow"]["name"]),
            outcome="succeeded",
            details={
                "saved_path": target_path,
                "validation": validation,
            },
        )
        await _workflow_session_fence(request, revocation_scope)
        return {
            "status": "saved",
            "file_path": target_path,
            "workflows": workflows,
            **_validate_workflow_content(req.content, path=target_path),
        }
    except RuntimeRevokedError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked during workflow save."},
        ) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.put("/workflows/{name}")
async def update_workflow(name: str, req: UpdateWorkflowRequest, request: Request):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        await _workflow_session_fence(request, revocation_scope)
        ok = workflow_manager.enable(name) if req.enabled else workflow_manager.disable(name)
        if not ok:
            await _workflow_session_fence(request, revocation_scope)
            await log_integration_event(
                integration_type="workflow",
                name=_safe_workflow_token(name, fallback="workflow"),
                outcome="failed",
                details={
                    "status": "not_found",
                    "enabled": req.enabled,
                },
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=404, detail="workflow_not_found")
        await _workflow_session_fence(request, revocation_scope)
        await log_integration_event(
            integration_type="workflow",
            name=_safe_workflow_token(name, fallback="workflow"),
            outcome="succeeded",
            details={"enabled": req.enabled},
        )
        await _workflow_session_fence(request, revocation_scope)
        return {"status": "updated", "name": name, "enabled": req.enabled}
    except RuntimeRevokedError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked during workflow update."},
        ) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/workflows/reload")
async def reload_workflows(request: Request):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        await _workflow_session_fence(request, revocation_scope)
        workflows = workflow_manager.reload()
        await _workflow_session_fence(request, revocation_scope)
        await log_integration_event(
            integration_type="workflows",
            name="reload",
            outcome="succeeded",
            details={
                "count": len(workflows),
                "enabled_count": sum(1 for workflow in workflows if workflow.get("enabled", False)),
                "workflow_names": [workflow["name"] for workflow in workflows],
            },
        )
        await _workflow_session_fence(request, revocation_scope)
        return {"status": "reloaded", "count": len(workflows), "workflows": workflows}
    except RuntimeRevokedError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked during workflow reload."},
        ) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.get("/workflows/runs")
async def list_workflow_runs(
    request: Request,
    limit: int = Query(default=12, ge=1, le=50),
    session_id: str | None = Query(default=None),
):
    operator = _require_authenticated_capability_operator(request)
    runs = await _list_workflow_runs(limit=limit, session_id=session_id)
    # Audit projections can predate durable ownership.  They are deliberately
    # omitted from this authenticated surface rather than being exposed under
    # a caller-selected conversation session.
    visible_runs = [
        run
        for run in runs
        if _workflow_owner_is_bound(run, operator.principal.principal_id)
    ]
    return {
        "runs": [
            projection
            for run in visible_runs
            if (projection := _safe_workflow_run_projection(run)) is not None
        ]
    }


@router.post("/workflows/runs/{run_identity:path}/resume-plan")
async def build_workflow_resume_plan(
    run_identity: str,
    request: Request,
    req: WorkflowResumePlanRequest | None = None,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        await _workflow_session_fence(request, revocation_scope)
        try:
            run = await _find_workflow_run_for_control(run_identity)
        except HTTPException as exc:
            safe_detail = "workflow_run_not_found" if exc.status_code == 404 else _safe_workflow_http_detail(exc.status_code)
            await _record_workflow_route_receipt(
                event_type="workflow_resume_plan_refused",
                session_id=active_session_id,
                run_identity=run_identity,
                action="resume",
                status_code=exc.status_code,
                detail=safe_detail,
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=exc.status_code, detail=safe_detail) from exc
        await _workflow_session_fence(request, revocation_scope)
        if run is None:
            detail = "workflow_run_not_found"
            await _record_workflow_route_receipt(
                event_type="workflow_resume_plan_refused",
                session_id=active_session_id,
                run_identity=run_identity,
                action="resume",
                status_code=404,
                detail=detail,
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=404, detail=detail)
        if not _workflow_owner_is_bound(run, operator.principal.principal_id):
            detail = "workflow_owner_mismatch"
            await _record_workflow_route_receipt(
                event_type="workflow_resume_plan_refused",
                session_id=active_session_id,
                run_identity=run_identity,
                action="resume",
                status_code=403,
                detail=detail,
                workflow_name=run.get("workflow_name"),
                step_id=req.step_id if req is not None else None,
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=403, detail=detail)
        identity_detail = _workflow_recovery_identity_detail(
            run=run,
            run_identity=run_identity,
            operator_session_id=active_session_id,
        )
        if identity_detail is not None:
            detail = identity_detail
            await _record_workflow_route_receipt(
                event_type="workflow_resume_plan_refused",
                session_id=active_session_id,
                run_identity=run_identity,
                action="resume",
                status_code=403,
                detail=detail,
                workflow_name=run.get("workflow_name"),
                step_id=req.step_id if req is not None else None,
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=403, detail=detail)
        if _is_typed_workflow_run(run):
            # Refresh the typed row so a stale list projection cannot produce
            # a resume draft with an old revision, lease, or parent fence.
            try:
                run = await _load_typed_workflow_run_for_control(
                    run_identity,
                    principal_id=operator.principal.principal_id,
                    session_id=active_session_id,
                )
            except HTTPException as exc:
                detail = str(exc.detail or "workflow_owner_mismatch")
                await _record_workflow_route_receipt(
                    event_type="workflow_resume_plan_refused",
                    session_id=active_session_id,
                    run_identity=run_identity,
                    action="resume",
                    status_code=exc.status_code,
                    detail=detail,
                    workflow_name=run.get("workflow_name") if isinstance(run, dict) else None,
                )
                await _workflow_session_fence(request, revocation_scope)
                raise
            if run is None:
                detail = "workflow_run_not_found"
                await _workflow_session_fence(request, revocation_scope)
                raise HTTPException(status_code=404, detail=detail)
            identity_detail = _workflow_identity_binding_detail(
                run=run,
                run_identity=run_identity,
                operator_context=req.operator_context if req is not None else None,
                require_plan_revision=True,
            )
            if identity_detail is not None:
                await _record_workflow_route_receipt(
                    event_type="workflow_resume_plan_refused",
                    session_id=active_session_id,
                    run_identity=run_identity,
                    action="resume",
                    status_code=409,
                    detail=identity_detail,
                    workflow_name=run.get("workflow_name"),
                    step_id=req.step_id if req is not None else None,
                )
                await _workflow_session_fence(request, revocation_scope)
                raise HTTPException(status_code=409, detail=identity_detail)
            goal_detail = await _workflow_current_goal_binding_detail(run)
            if goal_detail is not None:
                await _record_workflow_route_receipt(
                    event_type="workflow_resume_plan_refused",
                    session_id=active_session_id,
                    run_identity=run_identity,
                    action="resume",
                    status_code=409,
                    detail=goal_detail,
                    workflow_name=run.get("workflow_name"),
                    step_id=req.step_id if req is not None else None,
                )
                await _workflow_session_fence(request, revocation_scope)
                raise HTTPException(status_code=409, detail=goal_detail)
            typed_lease = run.get("lease") if isinstance(run.get("lease"), dict) else {}
            typed_lease_owner = str(typed_lease.get("owner") or "").strip()
            if typed_lease_owner and typed_lease_owner != _workflow_canonical_lease_owner(run_identity):
                detail = "workflow_control_lease_blocked"
                await _record_workflow_route_receipt(
                    event_type="workflow_resume_plan_refused",
                    session_id=active_session_id,
                    run_identity=run_identity,
                    action="resume",
                    status_code=423,
                    detail=detail,
                    workflow_name=run.get("workflow_name"),
                )
                await _workflow_session_fence(request, revocation_scope)
                raise HTTPException(status_code=423, detail=detail)
        else:
            identity_detail = _workflow_identity_binding_detail(
                run=run,
                run_identity=run_identity,
                operator_context=req.operator_context if req is not None else None,
                require_plan_revision=True,
            )
            if identity_detail is not None:
                await _record_workflow_route_receipt(
                    event_type="workflow_resume_plan_refused",
                    session_id=active_session_id,
                    run_identity=run_identity,
                    action="resume",
                    status_code=409,
                    detail=identity_detail,
                    workflow_name=run.get("workflow_name"),
                    step_id=req.step_id if req is not None else None,
                )
                await _workflow_session_fence(request, revocation_scope)
                raise HTTPException(status_code=409, detail=identity_detail)
            goal_detail = await _workflow_current_goal_binding_detail(run)
            if goal_detail is not None:
                await _record_workflow_route_receipt(
                    event_type="workflow_resume_plan_refused",
                    session_id=active_session_id,
                    run_identity=run_identity,
                    action="resume",
                    status_code=409,
                    detail=goal_detail,
                    workflow_name=run.get("workflow_name"),
                    step_id=req.step_id if req is not None else None,
                )
                await _workflow_session_fence(request, revocation_scope)
                raise HTTPException(status_code=409, detail=goal_detail)
        replay_block_reason = str(run.get("replay_block_reason") or "").strip() or None
        if replay_block_reason or run.get("replay_allowed") is False:
            detail = _workflow_replay_block_detail(replay_block_reason)
            await _record_workflow_route_receipt(
                event_type="workflow_resume_plan_refused",
                session_id=active_session_id,
                run_identity=run_identity,
                action="resume",
                status_code=409,
                detail=detail,
                workflow_name=run.get("workflow_name"),
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=409, detail=detail)
        requested_step_id = _workflow_requested_step_id(
            run,
            req.step_id if req is not None else None,
        )
        try:
            resume_plan = _workflow_resume_plan(
                run,
                approvals=list(run.get("pending_approvals", [])),
                requested_step_id=requested_step_id,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                detail = "workflow_checkpoint_not_found"
            elif exc.status_code == 409:
                raw_detail = str(exc.detail or "")
                detail = (
                    "Workflow run must clear the approval gate before branching from a later checkpoint"
                    if "approval gate" in raw_detail.lower()
                    else "Workflow checkpoint cannot be reused because the parent run did not persist reusable checkpoint state"
                )
            else:
                detail = _safe_workflow_http_detail(exc.status_code, fallback="workflow_resume_plan_refused")
            audit_detail = _safe_workflow_http_detail(
                exc.status_code,
                fallback="workflow_resume_plan_refused",
            )
            await _record_workflow_route_receipt(
                event_type="workflow_resume_plan_refused",
                session_id=active_session_id,
                run_identity=run_identity,
                action="resume",
                status_code=exc.status_code,
                detail=audit_detail,
                workflow_name=run.get("workflow_name"),
                step_id=req.step_id if req is not None else None,
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=exc.status_code, detail=detail) from exc
        except Exception as exc:
            try:
                await _record_workflow_route_receipt(
                    event_type="workflow_resume_plan_failed",
                    session_id=active_session_id,
                    run_identity=run_identity,
                    action="resume",
                    status_code=500,
                    detail="workflow_resume_plan_refused",
                    workflow_name=run.get("workflow_name"),
                    step_id=req.step_id if req is not None else None,
                )
            except Exception:
                # The durable receipt is best effort at this boundary; never
                # replace the safe refusal with an audit/storage exception.
                logger.exception("Workflow resume-plan failure receipt could not be persisted")
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=500, detail="workflow_resume_plan_refused") from exc
        await _workflow_session_fence(request, revocation_scope)
        safe_plan = _safe_workflow_resume_plan(resume_plan)
        return {
            "run_identity": _safe_workflow_identity(run_identity),
            "workflow_name": _safe_workflow_token(run.get("workflow_name"), fallback="workflow"),
            "resume_plan": safe_plan,
        }
    except RuntimeRevokedError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked during workflow resume planning."},
        ) from exc
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)


@router.post("/workflows/runs/{run_identity:path}/control")
async def control_workflow_run(
    run_identity: str,
    req: WorkflowRunControlRequest,
    request: Request,
):
    operator = _require_authenticated_capability_operator(request)
    active_session_id = operator.session_id
    tokens = set_runtime_context(
        active_session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, active_session_id),
    )
    revocation_scope = None
    action = ""
    run: dict[str, Any] | None = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        await _workflow_session_fence(request, revocation_scope)
        action = req.action.strip().lower().replace("-", "_")
        if action not in _WORKFLOW_CONTROL_ACTIONS:
            detail = "workflow_control_action_unsupported"
            await _record_workflow_route_receipt(
                event_type="workflow_control_refused",
                session_id=active_session_id,
                run_identity=run_identity,
                action=action,
                status_code=422,
                detail=detail,
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=422, detail=detail)

        try:
            run = await _find_workflow_run_for_control(run_identity)
        except HTTPException as exc:
            safe_detail = "workflow_run_not_found" if exc.status_code == 404 else _safe_workflow_http_detail(exc.status_code)
            await _record_workflow_route_receipt(
                event_type="workflow_control_refused",
                session_id=active_session_id,
                run_identity=run_identity,
                action=action,
                status_code=exc.status_code,
                detail=safe_detail,
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=exc.status_code, detail=safe_detail) from exc
        await _workflow_session_fence(request, revocation_scope)
        if run is None:
            detail = "workflow_run_not_found"
            await _record_workflow_route_receipt(
                event_type="workflow_control_refused",
                session_id=active_session_id,
                run_identity=run_identity,
                action=action,
                status_code=404,
                detail=detail,
            )
            await _workflow_session_fence(request, revocation_scope)
            raise HTTPException(status_code=404, detail=detail)

        # A repository double or a direct typed read may provide the nested
        # schema-v2 owner/lease shape.  Normalize it before the ingress owner
        # check so every route decision uses the same canonical projection.
        if _is_typed_workflow_run(run):
            run = _canonical_workflow_projection_input(run) or run

        recovery_session_id = str(run.get("session_id") or "").strip() or None
        # A route auth session is the ingress fence.  Parent checkpoint
        # authority is fenced to the run's conversation session so the draft
        # can be consumed by WorkflowTool under that same session.
        owner = _workflow_operator_owner(
            operator.principal.principal_id,
            recovery_session_id or active_session_id,
        )
        target = str(req.target or req.step_id or run.get("workflow_name") or run_identity).strip()
        requested_step_id = _workflow_requested_step_id(run, req.step_id)
        safe_step_id = _safe_workflow_step_id(requested_step_id) if requested_step_id else None
        operator_context = {
            **(req.operator_context or {}),
            "workflow_name": run.get("workflow_name"),
            "thread_id": run.get("thread_id"),
            "session_id": run.get("session_id"),
            "operator_session_id": active_session_id,
            "recovery_session_id": recovery_session_id,
            "operator_principal_id": operator.principal.principal_id,
            "requested_owner": req.owner,
            "requested_step_id": safe_step_id,
        }

        lease_result = None
        recovery_result = None
        transition_result = None
        resume_plan = None

        async def log_refusal(
            *,
            status_code: int,
            detail: str,
            lease: dict[str, Any] | None = None,
            recovery: dict[str, Any] | None = None,
            transition: dict[str, Any] | None = None,
        ) -> None:
            assert_runtime_not_revoked()
            safe_target = _safe_operator_recovery_target(target)
            await audit_repository.log_event(
                session_id=run.get("session_id") if isinstance(run.get("session_id"), str) else None,
                actor="operator",
                event_type="workflow_control_refused",
                tool_name=_safe_workflow_token(run.get("tool_name"), fallback="workflow"),
                risk_level=str(run.get("risk_level") or "medium"),
                policy_mode=get_current_tool_policy_mode(),
                summary="Workflow operator control refused",
                details={
                    "run_identity_digest": _workflow_identity_digest(run_identity),
                    "workflow_name": _safe_workflow_token(run.get("workflow_name"), fallback="workflow"),
                    "action": _safe_workflow_action(action),
                    "target": safe_target["target"],
                    "target_digest": safe_target["target_digest"],
                    "step_id": safe_step_id,
                    "action_handle_digest": (
                        _workflow_identity_digest(
                            json.dumps(req.action_handle, sort_keys=True, separators=(",", ":"))
                        )
                        if req.action_handle is not None
                        else None
                    ),
                    "status_code": status_code,
                    "detail": _safe_workflow_refusal_detail(detail),
                    "external_action_allowed": False,
                    "lease_receipt": _safe_workflow_receipt(lease.get("receipt"))
                    if isinstance(lease, dict)
                    else None,
                    "recovery_receipt": _safe_workflow_receipt(recovery.get("receipt"))
                    if isinstance(recovery, dict)
                    else None,
                    "transition_receipt": _safe_workflow_receipt(transition.get("receipt"))
                    if isinstance(transition, dict)
                    else None,
                },
            )
            await _workflow_session_fence(request, revocation_scope)

        if (
            action not in _WORKFLOW_INSPECTION_ACTIONS
            and not _workflow_owner_is_bound(run, operator.principal.principal_id)
        ):
            await log_refusal(status_code=403, detail="workflow_owner_mismatch")
            raise HTTPException(status_code=403, detail="workflow_owner_mismatch")

        if action in _WORKFLOW_IDENTITY_BOUND_ACTIONS:
            identity_detail = _workflow_recovery_identity_detail(
                run=run,
                run_identity=run_identity,
                operator_session_id=active_session_id,
            )
            if identity_detail is not None:
                await log_refusal(status_code=403, detail=identity_detail)
                raise HTTPException(status_code=403, detail=identity_detail)
            identity_detail = _workflow_identity_binding_detail(
                run=run,
                run_identity=run_identity,
                operator_context=req.operator_context,
                require_plan_revision=True,
            )
            if identity_detail is not None:
                await log_refusal(status_code=409, detail=identity_detail)
                raise HTTPException(status_code=409, detail=identity_detail)
            if not _is_typed_workflow_run(run):
                goal_detail = await _workflow_current_goal_binding_detail(run)
                if goal_detail is not None:
                    await log_refusal(status_code=409, detail=goal_detail)
                    raise HTTPException(status_code=409, detail=goal_detail)

        # The cockpit only receives opaque action handles. Resolve the handle
        # against this authenticated run before changing its durable fence;
        # client supplied parent metadata is never authority.
        if req.action_handle is not None:
            try:
                handle_step_id = _validate_workflow_action_handle(
                    req.action_handle,
                    run=run,
                    run_identity=run_identity,
                    action=action,
                )
            except HTTPException as exc:
                detail = str(exc.detail or "workflow_action_handle_invalid")
                await log_refusal(status_code=exc.status_code, detail=detail)
                raise
            explicit_step_id = _workflow_requested_step_id(run, req.step_id)
            if explicit_step_id and handle_step_id and explicit_step_id != handle_step_id:
                detail = "workflow_action_handle_mismatch"
                await log_refusal(status_code=409, detail=detail)
                raise HTTPException(status_code=409, detail=detail)
            if handle_step_id:
                requested_step_id = handle_step_id
                safe_step_id = _safe_workflow_step_id(handle_step_id)
                operator_context["requested_step_id"] = safe_step_id

        if _is_typed_workflow_run(run):
            # Schema-v2 rows are owned by DurableJobRepository.  Route them
            # before the legacy orchestration lease/control calls below; the
            # latter intentionally reject typed rows rather than silently
            # changing a canonical job through a second state machine.
            try:
                typed_result = await _control_typed_workflow_run(
                    run_identity=run_identity,
                    action=action,
                    run=run,
                    principal_id=operator.principal.principal_id,
                    session_id=active_session_id,
                    operator_context=operator_context,
                )
            except HTTPException as exc:
                await log_refusal(
                    status_code=exc.status_code,
                    detail=str(exc.detail or "workflow_control_failed"),
                )
                raise
            await _workflow_session_fence(request, revocation_scope)
            return typed_result

        replay_block_reason = str(run.get("replay_block_reason") or "") or None
        if replay_block_reason in {"approval_context_changed", "approval_context_missing"}:
            await log_refusal(status_code=409, detail=replay_block_reason)
            raise HTTPException(
                status_code=409,
                detail=(
                    "Workflow trust boundary changed; start a fresh run instead of applying a live control."
                    if replay_block_reason == "approval_context_changed"
                    else (
                        "Workflow predates trust-boundary tracking; start a fresh run "
                        "instead of applying a live control."
                    )
                ),
            )

        if action in _WORKFLOW_REPLAY_ACTIONS and not recovery_session_id:
            detail = "workflow_owner_mismatch"
            await log_refusal(status_code=403, detail=detail)
            raise HTTPException(status_code=403, detail=detail)

        if action in _WORKFLOW_REPLAY_ACTIONS and (
            replay_block_reason is not None or run.get("replay_allowed") is False
        ):
            detail = _workflow_replay_block_detail(replay_block_reason)
            await log_refusal(status_code=409, detail=detail)
            raise HTTPException(status_code=409, detail=detail)

        if action in _WORKFLOW_REPLAY_ACTIONS:
            try:
                resume_plan = _workflow_resume_plan(
                    run,
                    approvals=list(run.get("pending_approvals", [])),
                    requested_step_id=requested_step_id,
                )
            except HTTPException as exc:
                if exc.status_code == 404:
                    detail = "workflow_checkpoint_not_found"
                elif exc.status_code == 409:
                    raw_detail = str(exc.detail or "")
                    detail = (
                        "workflow_approval_gate_blocked"
                        if "approval gate" in raw_detail.lower()
                        else "workflow_checkpoint_not_reusable"
                    )
                else:
                    detail = _safe_workflow_http_detail(exc.status_code)
                await log_refusal(status_code=exc.status_code, detail=detail)
                raise HTTPException(status_code=exc.status_code, detail=detail) from exc

            await _workflow_session_fence(request, revocation_scope)
            lease_result = await workflow_state_repository.acquire_or_renew_v2_lease(
                run_identity=run_identity,
                owner=owner,
            )
            await _workflow_session_fence(request, revocation_scope)
            if lease_result is None:
                detail = "workflow_run_not_persisted"
                await log_refusal(status_code=404, detail=detail)
                raise HTTPException(status_code=404, detail=detail)
            lease_receipt = lease_result.get("receipt") if isinstance(lease_result, dict) else None
            if isinstance(lease_receipt, dict) and lease_receipt.get("status") == "blocked":
                detail = _safe_workflow_refusal_detail(
                    lease_receipt.get("blocked_reason"),
                    fallback="workflow_control_lease_blocked",
                )
                await log_refusal(status_code=423, detail=detail, lease=lease_result)
                raise HTTPException(status_code=423, detail=detail)

            # Lease acquisition supplies the parent's current fencing revision
            # for any manual checkpoint draft returned to the operator.
            orchestration = lease_result.get("orchestration_v2") if isinstance(lease_result, dict) else None
            if isinstance(orchestration, dict):
                try:
                    resume_plan = _workflow_resume_plan(
                        {**run, "orchestration_v2": orchestration},
                        approvals=list(run.get("pending_approvals", [])),
                        requested_step_id=requested_step_id,
                    )
                except HTTPException as exc:
                    detail = _safe_workflow_http_detail(exc.status_code)
                    if exc.status_code == 404:
                        detail = "workflow_checkpoint_not_found"
                    elif exc.status_code == 409:
                        detail = "workflow_checkpoint_not_reusable"
                    await log_refusal(
                        status_code=exc.status_code,
                        detail=detail,
                        lease=lease_result,
                    )
                    raise HTTPException(status_code=exc.status_code, detail=detail) from exc
                except Exception as exc:
                    await log_refusal(
                        status_code=500,
                        detail="workflow_control_failed",
                        lease=lease_result,
                    )
                    raise HTTPException(status_code=500, detail="workflow_control_failed") from exc

            await _workflow_session_fence(request, revocation_scope)
            recovery_result = await workflow_state_repository.build_v2_recovery_plan(
                run_identity=run_identity,
                owner=owner,
                approval_context=run.get("current_approval_context") or run.get("approval_context"),
            )
            await _workflow_session_fence(request, revocation_scope)
            if recovery_result is None:
                detail = "workflow_run_not_persisted"
                await log_refusal(
                    status_code=404,
                    detail=detail,
                    lease=lease_result,
                )
                raise HTTPException(status_code=404, detail=detail)
            recovery_receipt = recovery_result.get("receipt") if isinstance(recovery_result, dict) else None
            if isinstance(recovery_receipt, dict) and recovery_receipt.get("status") == "blocked":
                detail = _safe_workflow_refusal_detail(
                    recovery_receipt.get("blocked_reason"),
                    fallback="workflow_recovery_blocked",
                )
                await log_refusal(status_code=409, detail=detail, lease=lease_result, recovery=recovery_result)
                raise HTTPException(status_code=409, detail=detail)

        control_result = None
        if action in _WORKFLOW_REPLAY_ACTIONS:
            await _workflow_session_fence(request, revocation_scope)
            expected_revision = None
            if isinstance(lease_result, dict):
                orchestration = lease_result.get("orchestration_v2")
                if isinstance(orchestration, dict) and orchestration.get("revision") is not None:
                    expected_revision = int(orchestration["revision"])
            transition_result = await workflow_state_repository.record_v2_transition(
                run_identity=run_identity,
                transition_key=f"operator:{action}:{safe_step_id or 'run'}",
                transition_type=action,
                owner=owner,
                # Keep the validated id in the trusted transition ledger;
                # safe projections and audit carry only its digest.
                step_id=requested_step_id,
                expected_revision=expected_revision,
            )
            await _workflow_session_fence(request, revocation_scope)
            if not isinstance(transition_result, dict) or not isinstance(transition_result.get("receipt"), dict):
                detail = "workflow_transition_unavailable"
                await log_refusal(
                    status_code=404,
                    detail=detail,
                    lease=lease_result,
                    recovery=recovery_result,
                    transition=transition_result,
                )
                raise HTTPException(status_code=404, detail=detail)
            transition_receipt = transition_result.get("receipt") if isinstance(transition_result, dict) else None
            if isinstance(transition_receipt, dict) and transition_receipt.get("status") == "blocked":
                detail = _safe_workflow_refusal_detail(
                    transition_receipt.get("blocked_reason"),
                    fallback="workflow_transition_blocked",
                )
                await log_refusal(
                    status_code=409,
                    detail=detail,
                    lease=lease_result,
                    recovery=recovery_result,
                    transition=transition_result,
                )
                raise HTTPException(status_code=409, detail=detail)

        # Persist the enabled control only after a replay transition has been
        # accepted.  A refused/missing transition must not leave an enabled
        # durable control receipt behind.
        await _workflow_session_fence(request, revocation_scope)
        control_result = await workflow_state_repository.record_v2_operator_recovery_control(
            run_identity=run_identity,
            action=action,
            target=target,
            operator_context=operator_context,
            enabled=True,
            owner=owner if action in _WORKFLOW_REPLAY_ACTIONS else None,
            expected_revision=(
                int(transition_result["orchestration_v2"].get("revision"))
                if isinstance(transition_result, dict)
                and isinstance(transition_result.get("orchestration_v2"), dict)
                and transition_result["orchestration_v2"].get("revision") is not None
                else expected_revision if action in _WORKFLOW_REPLAY_ACTIONS else None
            ),
            lease_id=(
                str(
                    _as_record(
                        _as_record(lease_result.get("orchestration_v2") if isinstance(lease_result, dict) else {}).get("lease")
                    ).get("lease_id")
                    or ""
                ).strip()
                or None
            ),
            transition_key=(
                str(transition_result["receipt"].get("transition_key") or "").strip()
                if isinstance(transition_result, dict)
                and isinstance(transition_result.get("receipt"), dict)
                else None
            ),
        )
        await _workflow_session_fence(request, revocation_scope)
        if not isinstance(control_result, dict) or not isinstance(control_result.get("receipt"), dict):
            detail = "workflow_control_state_unavailable"
            await log_refusal(
                status_code=404,
                detail=detail,
                lease=lease_result,
                recovery=recovery_result,
                transition=transition_result,
            )
            raise HTTPException(status_code=404, detail=detail)
        control_receipt = control_result.get("receipt")
        if (
            action in _WORKFLOW_REPLAY_ACTIONS
            and isinstance(control_receipt, dict)
            and control_receipt.get("status") == "blocked"
        ):
            detail = _safe_workflow_refusal_detail(
                control_receipt.get("blocked_reason"),
                fallback="workflow_control_fence_blocked",
            )
            await log_refusal(
                status_code=409,
                detail=detail,
                lease=lease_result,
                recovery=recovery_result,
                transition=transition_result,
            )
            raise HTTPException(status_code=409, detail=detail)

        # The control receipt advances the same fence as the transition.  A
        # draft built before it would carry a stale parent revision, so rebuild
        # from the authoritative post-control orchestration state.
        if action in _WORKFLOW_REPLAY_ACTIONS and isinstance(control_result.get("orchestration_v2"), dict):
            try:
                resume_plan = _workflow_resume_plan(
                    {**run, "orchestration_v2": control_result["orchestration_v2"]},
                    approvals=list(run.get("pending_approvals", [])),
                    requested_step_id=requested_step_id,
                )
            except Exception as exc:
                await log_refusal(
                    status_code=500,
                    detail="workflow_control_failed",
                    lease=lease_result,
                    recovery=recovery_result,
                    transition=transition_result,
                )
                raise HTTPException(status_code=500, detail="workflow_control_failed") from exc

        safe_target = _safe_operator_recovery_target(target)
        await _workflow_session_fence(request, revocation_scope)
        await audit_repository.log_event(
            session_id=run.get("session_id") if isinstance(run.get("session_id"), str) else None,
            actor="operator",
            event_type="workflow_control",
            tool_name=_safe_workflow_token(run.get("tool_name"), fallback="workflow"),
            risk_level=str(run.get("risk_level") or "medium"),
            policy_mode=get_current_tool_policy_mode(),
            summary="Workflow operator control recorded",
            details={
                "run_identity_digest": _workflow_identity_digest(run_identity),
                "workflow_name": _safe_workflow_token(run.get("workflow_name"), fallback="workflow"),
                "action": _safe_workflow_action(action),
                "target": safe_target["target"],
                "target_digest": safe_target["target_digest"],
                "step_id": safe_step_id,
                "action_handle_digest": (
                    _workflow_identity_digest(
                        json.dumps(req.action_handle, sort_keys=True, separators=(",", ":"))
                    )
                    if req.action_handle is not None
                    else None
                ),
                "external_action_allowed": False,
                "control_receipt": _safe_workflow_receipt(control_result.get("receipt")),
                "lease_receipt": _safe_workflow_receipt(lease_result.get("receipt"))
                if isinstance(lease_result, dict)
                else None,
                "recovery_receipt": _safe_workflow_receipt(recovery_result.get("receipt"))
                if isinstance(recovery_result, dict)
                else None,
                "transition_receipt": _safe_workflow_receipt(transition_result.get("receipt"))
                if isinstance(transition_result, dict)
                else None,
            },
        )

        await _workflow_session_fence(request, revocation_scope)
        refreshed_run = await _find_workflow_run_for_control(run_identity)
        await _workflow_session_fence(request, revocation_scope)
        return {
            "run_identity": _safe_workflow_identity(run_identity),
            "workflow_name": _safe_workflow_token(run.get("workflow_name"), fallback="workflow"),
            "action": action,
            "status": "recorded",
            "external_action_allowed": False,
            "control_receipt": _safe_workflow_receipt(control_result.get("receipt")),
            "lease_receipt": _safe_workflow_receipt(lease_result.get("receipt"))
            if isinstance(lease_result, dict)
            else None,
            "recovery_receipt": _safe_workflow_receipt(recovery_result.get("receipt"))
            if isinstance(recovery_result, dict)
            else None,
            "transition_receipt": _safe_workflow_receipt(transition_result.get("receipt"))
            if isinstance(transition_result, dict)
            else None,
            "resume_plan": _safe_workflow_resume_plan(resume_plan),
            "run": _safe_workflow_run_projection(refreshed_run or run),
        }
    except RuntimeRevokedError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked during workflow control."},
        ) from exc
    except HTTPException:
        raise
    except Exception:
        try:
            assert_runtime_not_revoked()
            await _record_workflow_route_receipt(
                event_type="workflow_control_failed",
                session_id=active_session_id,
                run_identity=run_identity,
                action=action,
                status_code=500,
                detail="workflow_control_failed",
                workflow_name=run.get("workflow_name") if isinstance(run, dict) else None,
                step_id=req.step_id,
            )
        except Exception:
            pass
        raise
    finally:
        await _end_rest_revocation_watch(revocation_scope)
        reset_runtime_context(tokens)
