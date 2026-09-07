"""Small, governed goal-conditioned planning and evidence receipt seam.

This module intentionally stops at the existing capability/workflow boundary.
It can propose a candidate from a goal and dispatch only through an explicitly
injected adapter.  No adapter is installed by the production path while the
durable admission/execution contracts from #743/#744/#747 are unavailable.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from src.audit.formatting import redact_for_audit
from src.audit.repository import audit_repository
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
from src.goals.repository import deserialize_success_criterion, goal_repository

logger = logging.getLogger(__name__)

GOAL_LOOP_RECEIPT_VERSION = "goal_conditioned_loop_v1"
_CANDIDATE_EVENT = "goal_loop_candidate"
_OUTCOME_EVENT = "goal_loop_outcome"
_NO_LEARNING_EVENT = "goal_loop_no_learning"


class GoalExecutionAdapter(Protocol):
    """Dependency-injected seam for an existing governed runtime."""

    async def execute(
        self,
        *,
        goal: Goal,
        candidate: GoalCandidateDecision,
    ) -> GoalExecutionResult | dict[str, Any]:
        """Execute and independently read back evidence for one candidate."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _safe_text(value: object, *, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _safe_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candidate_receipt_details(decision: GoalCandidateDecision) -> dict[str, Any]:
    """Return an inspectable receipt without persisting input values."""

    return {
        "receipt_version": GOAL_LOOP_RECEIPT_VERSION,
        "receipt_type": "candidate",
        "proposal_only": True,
        "candidate_id": decision.candidate_id,
        "dedupe_key": decision.dedupe_key,
        "goal_id": decision.goal_id,
        "goal_revision": decision.goal_revision,
        "criterion_id": decision.criterion_id,
        "action": decision.action.value,
        "reason": _safe_text(decision.reason),
        "evidence_refs": list(decision.evidence_refs),
        "capability_id": decision.capability_id,
        "capability_version": decision.capability_version,
        "input_keys": sorted(str(key) for key in decision.inputs),
        "input_digest": _safe_digest(decision.inputs),
        "expected_outcome": _safe_text(decision.expected_outcome),
        "expires_at": decision.expires_at.isoformat() if decision.expires_at else None,
        "content_redacted": True,
    }


def _outcome_receipt_details(receipt: GoalOutcomeReceipt) -> dict[str, Any]:
    return {
        "receipt_version": GOAL_LOOP_RECEIPT_VERSION,
        "receipt_type": receipt.receipt_type,
        "outcome_id": receipt.outcome_id,
        "candidate_id": receipt.candidate_id,
        "dedupe_key": receipt.dedupe_key,
        "goal_id": receipt.goal_id,
        "goal_revision": receipt.goal_revision,
        "execution_status": receipt.execution_status,
        "verification": receipt.verification,
        "usefulness": receipt.usefulness,
        "learning": receipt.learning,
        "learning_record_id": receipt.learning_record_id,
        "artifact_ref": _safe_text(receipt.artifact_ref, limit=240) if receipt.artifact_ref else None,
        "evidence_refs": list(receipt.evidence_refs),
        "reason": _safe_text(receipt.reason),
        "content_redacted": True,
    }


_SAFE_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "receipt_type",
        "proposal_only",
        "candidate_id",
        "outcome_id",
        "dedupe_key",
        "goal_id",
        "goal_revision",
        "criterion_id",
        "action",
        "evidence_refs",
        "capability_id",
        "capability_version",
        "input_keys",
        "input_digest",
        "expected_outcome",
        "expires_at",
        "execution_status",
        "verification",
        "usefulness",
        "learning",
        "learning_record_id",
        "artifact_ref",
        "reason",
        "content_redacted",
    }
)


def _redact_receipt_details(details: dict[str, Any]) -> dict[str, Any]:
    """Redact free-form fields while retaining safe receipt identity fields."""

    return {
        key: value if key in _SAFE_RECEIPT_FIELDS else redact_for_audit(value, key)
        for key, value in details.items()
    }


async def _existing_receipt(*, event_type: str, dedupe_key: str) -> dict[str, Any] | None:
    try:
        events = await audit_repository.list_events(limit=500)
    except Exception:
        logger.debug("Could not inspect goal-loop receipts", exc_info=True)
        return None
    for event in events:
        if event.get("event_type") != event_type:
            continue
        details = event.get("details")
        if isinstance(details, dict) and details.get("dedupe_key") == dedupe_key:
            return details
    return None


async def _persist_receipt(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    safe_details = _redact_receipt_details(details)
    existing = await _existing_receipt(
        event_type=event_type,
        dedupe_key=str(safe_details.get("dedupe_key") or ""),
    )
    if existing is not None:
        return existing
    await audit_repository.log_event(
        actor="guardian",
        event_type=event_type,
        tool_name="goal_conditioned_loop",
        risk_level="low",
        policy_mode="full",
        summary=summary,
        details=safe_details,
    )
    return safe_details


def build_goal_candidate_decision(
    goal: Goal,
    request: GoalCandidateRequest,
) -> GoalCandidateDecision:
    """Build a deterministic decision while preserving the goal revision.

    Missing or malformed success evidence is deliberately conservative: it
    produces clarify/defer and can never become an ``act`` decision.
    """

    if not isinstance(request, GoalCandidateRequest):
        request = GoalCandidateRequest.model_validate(request)
    criterion = deserialize_success_criterion(goal)
    criterion_id = criterion.criterion_id if criterion else None
    evidence_refs = normalized_evidence_refs(
        *(criterion.evidence_refs if criterion else []),
        *request.evidence_refs,
    )
    expected_outcome = request.expected_outcome.strip()
    if not expected_outcome and criterion is not None:
        expected_outcome = criterion.description
    dedupe_key = stable_candidate_key(
        goal_id=goal.id,
        goal_revision=max(int(goal.revision or 1), 1),
        criterion_id=criterion_id,
        capability_id=request.capability_id,
        capability_version=request.capability_version,
        evidence_refs=evidence_refs,
        expected_outcome=expected_outcome,
    )
    candidate_id = "cand_" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:24]
    action = GoalCandidateAction.act
    reason = request.reason.strip() or "goal_conditioned_candidate"

    if _enum_value(goal.status) != "active":
        action = GoalCandidateAction.silent
        reason = "goal_not_active"
    elif criterion is None:
        action = GoalCandidateAction.clarify
        reason = "missing_success_criterion"
    elif not criterion.verifier_configured:
        action = GoalCandidateAction.clarify
        reason = "missing_success_verifier"
    elif not evidence_refs:
        action = GoalCandidateAction.defer
        reason = "missing_success_evidence"
    elif request.expires_at is not None:
        expiry = request.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= _now():
            action = GoalCandidateAction.defer
            reason = "candidate_expired"

    return GoalCandidateDecision(
        candidate_id=candidate_id,
        dedupe_key=dedupe_key,
        goal_id=goal.id,
        goal_revision=max(int(goal.revision or 1), 1),
        criterion_id=criterion_id,
        action=action,
        reason=reason,
        evidence_refs=list(evidence_refs),
        capability_id=request.capability_id,
        capability_version=request.capability_version,
        inputs=dict(request.inputs),
        expected_outcome=expected_outcome,
        expires_at=request.expires_at,
    )


async def propose_goal_candidate(
    goal_id: str,
    request: GoalCandidateRequest,
) -> GoalCandidateDecision:
    """Build and persist one inspectable candidate decision."""

    goal = await goal_repository.get(goal_id)
    if goal is None:
        raise LookupError(f"Goal '{goal_id}' not found")
    decision = build_goal_candidate_decision(goal, request)
    await _persist_receipt(
        event_type=_CANDIDATE_EVENT,
        summary=f"Goal candidate {decision.action.value} for {goal.id}",
        details=_candidate_receipt_details(decision),
    )
    if decision.action is not GoalCandidateAction.act:
        await _persist_no_learning(
            decision,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason=f"candidate_not_dispatched:{decision.reason}",
        )
    return decision


def _coerce_execution_result(value: GoalExecutionResult | dict[str, Any]) -> GoalExecutionResult:
    if isinstance(value, GoalExecutionResult):
        return value
    return GoalExecutionResult.model_validate(value)


async def _call_adapter(
    adapter: GoalExecutionAdapter | Any,
    *,
    goal: Goal,
    candidate: GoalCandidateDecision,
) -> GoalExecutionResult:
    execute = getattr(adapter, "execute", None)
    if execute is None and callable(adapter):
        execute = adapter
    if execute is None:
        raise TypeError("goal execution adapter must expose execute()")
    result = execute(goal=goal, candidate=candidate)
    if inspect.isawaitable(result):
        result = await result
    return _coerce_execution_result(result)


def _outcome_id(candidate: GoalCandidateDecision, result: GoalExecutionResult) -> str:
    seed = f"{candidate.dedupe_key}:{result.execution_status}:{result.verification}:{result.artifact_ref or ''}"
    return "out_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


async def _persist_no_learning(
    candidate: GoalCandidateDecision,
    *,
    execution_status: str,
    verification: str,
    usefulness: str,
    reason: str,
    evidence_refs: tuple[str, ...] = (),
    artifact_ref: str | None = None,
) -> GoalOutcomeReceipt:
    receipt = GoalOutcomeReceipt(
        receipt_type="no_learning",
        outcome_id="nl_" + hashlib.sha256(
            f"{candidate.dedupe_key}:no_learning".encode("utf-8")
        ).hexdigest()[:24],
        candidate_id=candidate.candidate_id,
        dedupe_key=candidate.dedupe_key,
        goal_id=candidate.goal_id,
        goal_revision=candidate.goal_revision,
        execution_status=execution_status,  # type: ignore[arg-type]
        verification=verification,  # type: ignore[arg-type]
        usefulness=usefulness,  # type: ignore[arg-type]
        learning="no_learning",
        artifact_ref=artifact_ref,
        evidence_refs=list(evidence_refs),
        reason=reason,
    )
    await _persist_receipt(
        event_type=_NO_LEARNING_EVENT,
        summary=f"Goal candidate {candidate.candidate_id} recorded no learning",
        details=_outcome_receipt_details(receipt),
    )
    return receipt


async def dispatch_goal_candidate(
    candidate: GoalCandidateDecision,
    *,
    adapter: GoalExecutionAdapter | Any | None = None,
) -> GoalOutcomeReceipt:
    """Dispatch only an active, current-revision candidate through an adapter.

    ``adapter=None`` is the production-safe state while prerequisite admission
    and execution contracts are absent: the candidate is recorded as blocked
    with explicit ``no_learning`` rather than being treated as successful.
    """

    if not isinstance(candidate, GoalCandidateDecision):
        candidate = GoalCandidateDecision.model_validate(candidate)
    existing = await _existing_receipt(
        event_type=_OUTCOME_EVENT,
        dedupe_key=candidate.dedupe_key,
    )
    if existing is not None:
        return GoalOutcomeReceipt.model_validate(existing)

    goal = await goal_repository.get(candidate.goal_id)
    if goal is None:
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason="goal_not_found",
        )
    current_revision = max(int(goal.revision or 1), 1)
    if _enum_value(goal.status) != "active":
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason="goal_not_active",
        )
    if current_revision != candidate.goal_revision:
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason="stale_goal_revision",
        )
    if not candidate.dispatchable:
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason=f"candidate_action_{candidate.action.value}",
        )
    if candidate.expires_at is not None:
        expiry = candidate.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= _now():
            return await _persist_no_learning(
                candidate,
                execution_status="blocked",
                verification="unknown",
                usefulness="unknown",
                reason="candidate_expired",
            )
    if adapter is None:
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason="execution_adapter_unavailable",
        )

    try:
        result = await _call_adapter(adapter, goal=goal, candidate=candidate)
    except Exception as exc:
        logger.info("Goal candidate execution failed: %s", type(exc).__name__)
        result = GoalExecutionResult(
            execution_status="failed",
            verification="unknown",
            usefulness="unknown",
            learning="no_learning",
            reason=f"adapter_failed:{type(exc).__name__}",
        )

    criterion = deserialize_success_criterion(goal)
    evidence_refs = normalized_evidence_refs(
        *candidate.evidence_refs,
        *result.evidence_refs,
    )
    verification = result.verification
    if verification == "passed":
        if criterion is None or not criterion.verifier_configured or not evidence_refs:
            verification = "unknown"
        elif criterion.verifier_kind.value == "artifact_readback" and not result.artifact_ref:
            verification = "unknown"
    learning = result.learning
    learning_record_id = result.learning_record_id
    if learning == "applied" and not learning_record_id:
        learning = "no_learning"
    outcome = GoalOutcomeReceipt(
        receipt_type="outcome",
        outcome_id=_outcome_id(candidate, result),
        candidate_id=candidate.candidate_id,
        dedupe_key=candidate.dedupe_key,
        goal_id=candidate.goal_id,
        goal_revision=candidate.goal_revision,
        execution_status=result.execution_status,
        verification=verification,
        usefulness=result.usefulness,
        learning=learning,
        learning_record_id=learning_record_id,
        artifact_ref=result.artifact_ref,
        evidence_refs=list(evidence_refs),
        reason=_safe_text(result.reason or "goal_candidate_outcome"),
    )
    await _persist_receipt(
        event_type=_OUTCOME_EVENT,
        summary=f"Goal candidate {candidate.candidate_id} outcome recorded",
        details=_outcome_receipt_details(outcome),
    )
    if outcome.learning == "no_learning":
        await _persist_no_learning(
            candidate,
            execution_status=outcome.execution_status,
            verification=outcome.verification,
            usefulness=outcome.usefulness,
            reason=outcome.reason or "no_reliable_learning_evidence",
            evidence_refs=tuple(outcome.evidence_refs),
            artifact_ref=outcome.artifact_ref,
        )
    return outcome


async def list_goal_loop_receipts(
    goal_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return candidate/outcome/no-learning receipts for operator inspection."""

    try:
        events = await audit_repository.list_events(limit=min(max(limit, 1), 500))
    except Exception:
        return []
    receipts: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") not in {
            _CANDIDATE_EVENT,
            _OUTCOME_EVENT,
            _NO_LEARNING_EVENT,
        }:
            continue
        details = event.get("details")
        if not isinstance(details, dict) or details.get("goal_id") != goal_id:
            continue
        receipts.append(
            {
                "audit_event_id": event.get("id"),
                "event_type": event.get("event_type"),
                "created_at": event.get("created_at"),
                **details,
            }
        )
    return receipts
