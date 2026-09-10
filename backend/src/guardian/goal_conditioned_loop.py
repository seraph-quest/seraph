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
import re
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
    StrategyDeltaProvenance,
    normalized_evidence_refs,
    stable_candidate_key,
)
from src.goals.repository import deserialize_success_criterion, goal_repository
from src.memory.control import get_strategy_delta
from src.memory.gate_b_provider_decision import (
    GateBCanonicalDecisionRecord,
    build_gate_b_canonical_decision_record,
)

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


_SAFE_EVIDENCE_KIND = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_SAFE_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAFE_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _safe_evidence_refs(*refs: object) -> tuple[str, ...]:
    """Keep receipt evidence bounded and opaque while preserving delta lookup.

    Evidence is caller-controlled and may contain URLs, queries, or correction
    prose.  Only the generated strategy-delta identifier remains readable so
    the durable correction resolver can look it up; every other reference is
    represented by a typed SHA-256 digest.  The transformation is idempotent
    so legacy and newly written receipts have one stable readback shape.
    """

    normalized = normalized_evidence_refs(*refs)[:32]
    safe: set[str] = set()
    for raw in normalized:
        if raw.startswith("strategy-delta:"):
            suffix = raw.removeprefix("strategy-delta:").strip()
            if suffix.startswith("invalid:") and _SAFE_DIGEST.fullmatch(suffix.removeprefix("invalid:")):
                safe.add(raw)
            elif _SAFE_OPAQUE_ID.fullmatch(suffix):
                safe.add(f"strategy-delta:{suffix}")
            else:
                safe.add(f"strategy-delta:invalid:{_safe_digest(raw)}")
            continue

        if raw.startswith("evidence:"):
            parts = raw.split(":")
            if len(parts) == 3 and _SAFE_EVIDENCE_KIND.fullmatch(parts[1]) and _SAFE_DIGEST.fullmatch(parts[2]):
                safe.add(raw)
                continue

        kind, separator, _value = raw.partition(":")
        if not separator or not _SAFE_EVIDENCE_KIND.fullmatch(kind):
            kind = "opaque"
        safe.add(f"evidence:{kind}:{_safe_digest(raw)}")
    return tuple(sorted(safe))


def _strategy_delta_ids_from_evidence(
    evidence_refs: list[str] | tuple[str, ...],
) -> tuple[tuple[str, ...], bool]:
    """Extract bounded correction IDs and flag malformed correction evidence."""

    delta_ids: set[str] = set()
    malformed = False
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref.startswith("strategy-delta:"):
            continue
        value = ref.removeprefix("strategy-delta:").strip()
        if not value or len(value) > 128 or ":" in value or not _SAFE_OPAQUE_ID.fullmatch(value):
            malformed = True
            continue
        delta_ids.add(value)
    return tuple(sorted(delta_ids)), malformed


async def _resolve_strategy_delta_provenance(
    *,
    candidate: GoalCandidateDecision,
    goal: Goal | None,
    evidence_refs: list[str] | tuple[str, ...] | None = None,
) -> tuple[str | None, StrategyDeltaProvenance]:
    """Verify a correction receipt before exposing its ID as provenance.

    Evidence references are caller data.  Only an applied, goal-owned delta
    whose recorded target still matches the current goal can establish the
    later choice's correction provenance.  Any ambiguity or storage failure
    stays explicitly unresolved and never becomes a positive receipt claim.
    """

    refs = (
        candidate.evidence_refs
        if evidence_refs is None
        else _safe_evidence_refs(*evidence_refs)
    )
    delta_ids, malformed = _strategy_delta_ids_from_evidence(refs)
    if not delta_ids and not malformed:
        return None, "not_present"
    if malformed or len(delta_ids) != 1 or goal is None:
        return None, "unresolved"

    delta_id = delta_ids[0]
    try:
        delta = await get_strategy_delta(delta_id)
    except Exception:
        logger.debug("Could not verify strategy delta provenance", exc_info=True)
        return None, "unresolved"
    if delta is None:
        return None, "unresolved"

    criterion = deserialize_success_criterion(goal)
    target = criterion.target if criterion is not None else None
    try:
        current_revision = max(int(goal.revision or 1), 1)
        revision_before = int(delta.goal_revision_before)
        revision_after = int(delta.goal_revision_after) if delta.goal_revision_after is not None else None
    except (TypeError, ValueError):
        return None, "unresolved"

    if not isinstance(target, dict) or not isinstance(delta.after, dict):
        return None, "unresolved"
    if (
        delta.delta_id != delta_id
        or delta.goal_id != candidate.goal_id
        or candidate.capability_id != "workflow.web-brief-to-file"
        or delta.scope != "goal"
        or delta.field_name != "web_brief_target"
        or not str(delta.author_id or "").strip()
        or delta.status != "applied"
        or revision_after is None
        or revision_before >= revision_after
        or revision_after > candidate.goal_revision
        or current_revision != candidate.goal_revision
        or target.get("strategy_delta_id") != delta_id
        or target != delta.after
        or any(
            key in target and candidate.inputs.get(key) != target[key]
            for key in ("query", "file_path", "priority")
        )
    ):
        return None, "unresolved"
    return delta_id, "verified"


def _required_strategy_delta_id(
    *,
    candidate: GoalCandidateDecision,
    goal: Goal,
) -> tuple[str | None, bool]:
    """Return the correction identity required by a corrected web-brief goal.

    A goal target carrying a correction identity is a durable strategy
    reference, not optional evidence.  The later capability run must either
    verify that exact applied delta or stop before any workflow side effect.
    """

    if candidate.capability_id != "workflow.web-brief-to-file":
        return None, False
    criterion = deserialize_success_criterion(goal)
    target = criterion.target if criterion is not None else None
    if not isinstance(target, dict) or "strategy_delta_id" not in target:
        return None, False
    value = target.get("strategy_delta_id")
    if not isinstance(value, str):
        return None, True
    normalized = value.strip()
    if not normalized or not _SAFE_OPAQUE_ID.fullmatch(normalized):
        return None, True
    return normalized, True


def _candidate_receipt_details(
    decision: GoalCandidateDecision,
    *,
    strategy_delta_id: str | None,
    strategy_delta_provenance: StrategyDeltaProvenance,
) -> dict[str, Any]:
    """Return an inspectable receipt without persisting input values."""

    canonical_decision = build_gate_b_canonical_decision_record(
        goal_id=decision.goal_id,
        goal_revision=decision.goal_revision,
        # This seam does not own a durable plan revision or canonical-memory
        # reconciliation state.  Keep the Gate B record contract-only until a
        # governed caller can supply those bindings.
        plan_revision=None,
        decision_input_digest=_safe_digest(decision.inputs),
        memory_delta_id=strategy_delta_id,
        memory_delta_provenance=strategy_delta_provenance,
        memory_control_owner=None,
        memory_state="unknown",
        tombstone_ledger_revision=None,
        recovery_state="restart_unverified",
        decision=decision.action.value,
    )
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
        "evidence_refs": list(_safe_evidence_refs(*decision.evidence_refs)),
        "capability_id": decision.capability_id,
        "capability_version": decision.capability_version,
        "input_keys": sorted(str(key) for key in decision.inputs),
        "input_digest": _safe_digest(decision.inputs),
        "strategy_delta_id": strategy_delta_id,
        "strategy_delta_provenance": strategy_delta_provenance,
        "canonical_decision_record": canonical_decision.as_payload(),
        "expected_outcome": _safe_text(decision.expected_outcome),
        "expires_at": decision.expires_at.isoformat() if decision.expires_at else None,
        "content_redacted": True,
    }


def _canonical_decision_for_outcome(
    receipt: GoalOutcomeReceipt,
) -> GateBCanonicalDecisionRecord:
    return build_gate_b_canonical_decision_record(
        goal_id=receipt.goal_id,
        goal_revision=receipt.goal_revision,
        # The goal-loop receipt has no authoritative plan revision or
        # canonical-memory restart binding at this extension point.
        plan_revision=None,
        decision_input_digest=receipt.decision_input_digest,
        memory_delta_id=receipt.strategy_delta_id,
        memory_delta_provenance=receipt.strategy_delta_provenance,
        memory_control_owner=None,
        memory_state="unknown",
        tombstone_ledger_revision=None,
        recovery_state="restart_unverified",
        decision="act",
        verification=receipt.verification,
        usefulness=receipt.usefulness,
        learning_writeback_id=receipt.learning_record_id,
        requested_learning=receipt.learning,
    )


def _outcome_receipt_details(
    receipt: GoalOutcomeReceipt,
    *,
    capability_id: str | None = None,
) -> dict[str, Any]:
    canonical_decision = _canonical_decision_for_outcome(receipt)
    details = {
        "receipt_version": GOAL_LOOP_RECEIPT_VERSION,
        "receipt_type": receipt.receipt_type,
        "outcome_id": receipt.outcome_id,
        "candidate_id": receipt.candidate_id,
        "dedupe_key": receipt.dedupe_key,
        "decision_input_digest": receipt.decision_input_digest,
        "strategy_delta_id": receipt.strategy_delta_id,
        "strategy_delta_provenance": receipt.strategy_delta_provenance,
        "canonical_decision_record": canonical_decision.as_payload(),
        "goal_id": receipt.goal_id,
        "goal_revision": receipt.goal_revision,
        "execution_status": receipt.execution_status,
        "verification": receipt.verification,
        "usefulness": receipt.usefulness,
        "learning": receipt.learning,
        "learning_record_id": receipt.learning_record_id,
        "artifact_ref": _safe_text(receipt.artifact_ref, limit=240) if receipt.artifact_ref else None,
        "evidence_refs": list(_safe_evidence_refs(*receipt.evidence_refs)),
        "reason": _safe_text(receipt.reason),
        "content_redacted": True,
    }
    if capability_id is not None:
        details["capability_id"] = capability_id
    return details


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
        "decision_input_digest",
        "strategy_delta_id",
        "strategy_delta_provenance",
        "canonical_decision_record",
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

    safe_details = {
        key: value if key in _SAFE_RECEIPT_FIELDS else redact_for_audit(value, key)
        for key, value in details.items()
    }
    if "evidence_refs" in safe_details:
        evidence_refs = safe_details.get("evidence_refs")
        if isinstance(evidence_refs, (list, tuple, set)):
            safe_details["evidence_refs"] = list(_safe_evidence_refs(*evidence_refs))
        else:
            safe_details["evidence_refs"] = []
    if "canonical_decision_record" in safe_details:
        safe_details["canonical_decision_record"] = _sanitize_canonical_decision_record(
            safe_details.get("canonical_decision_record")
        )
    return _sanitize_strategy_delta_receipt(safe_details)


def _sanitize_canonical_decision_record(value: object) -> dict[str, Any] | None:
    """Rebuild the Gate B record from bounded fields before audit persistence."""

    if not isinstance(value, dict):
        return None
    try:
        record = build_gate_b_canonical_decision_record(
            goal_id=value.get("goal_id"),
            goal_revision=value.get("goal_revision"),
            plan_revision=value.get("plan_revision"),
            decision_input_digest=value.get("decision_input_digest"),
            memory_delta_id=value.get("memory_delta_id"),
            memory_delta_provenance=value.get("memory_delta_provenance"),
            memory_control_owner=value.get("memory_control_owner"),
            memory_state=value.get("memory_state"),
            tombstone_ledger_revision=value.get("tombstone_ledger_revision"),
            recovery_state=value.get("recovery_state"),
            decision=value.get("decision"),
            verification=value.get("verification"),
            usefulness=value.get("usefulness"),
            learning_writeback_id=value.get("learning_writeback_id"),
            requested_learning=value.get("learning"),
        )
    except Exception:
        return None
    return record.as_payload()


def _downgrade_canonical_decision_record(value: object) -> dict[str, Any] | None:
    """Remove positive canonical-memory claims from an unresolved outer row.

    Stored audit details are untrusted.  A nested Gate B record cannot retain a
    verified/applied claim when the surrounding StrategyDelta provenance was
    downgraded (for example, on the list/readback surface where the candidate
    inputs are unavailable).  Rebuild the record through the same bounded
    contract so no caller-supplied content is copied into the replacement.
    """

    safe_record = _sanitize_canonical_decision_record(value)
    if safe_record is None:
        return None
    try:
        record = build_gate_b_canonical_decision_record(
            goal_id=safe_record.get("goal_id"),
            goal_revision=safe_record.get("goal_revision"),
            plan_revision=safe_record.get("plan_revision"),
            decision_input_digest=safe_record.get("decision_input_digest"),
            memory_delta_id=None,
            memory_delta_provenance="unresolved",
            memory_control_owner=None,
            memory_state="unknown",
            tombstone_ledger_revision=None,
            recovery_state="restart_unverified",
            decision=safe_record.get("decision"),
            verification=safe_record.get("verification"),
            usefulness="unknown",
            learning_writeback_id=None,
            requested_learning="no_learning",
        )
    except Exception:
        return None
    return record.as_payload()


def _sanitize_strategy_delta_receipt(details: dict[str, Any]) -> dict[str, Any]:
    """Keep legacy audit rows from exposing unverified correction/learning claims."""

    safe_details = dict(details)
    has_strategy_binding = (
        "strategy_delta_id" in safe_details
        or "strategy_delta_provenance" in safe_details
    )
    if "canonical_decision_record" in safe_details:
        safe_details["canonical_decision_record"] = _sanitize_canonical_decision_record(
            safe_details.get("canonical_decision_record")
        )
    if "evidence_refs" in safe_details:
        evidence_refs = safe_details.get("evidence_refs")
        if isinstance(evidence_refs, (list, tuple, set)):
            safe_details["evidence_refs"] = list(_safe_evidence_refs(*evidence_refs))
        else:
            safe_details["evidence_refs"] = []
    if "learning" in safe_details:
        learning = safe_details.get("learning")
        safe_details["learning"] = (
            learning
            if isinstance(learning, str) and learning in {"applied", "no_learning"}
            else "no_learning"
        )
    if "learning_record_id" in safe_details:
        learning_record_id = safe_details.get("learning_record_id")
        if not (
            isinstance(learning_record_id, str)
            and _SAFE_OPAQUE_ID.fullmatch(learning_record_id.strip()) is not None
        ):
            safe_details["learning_record_id"] = None
    delta_id = safe_details.get("strategy_delta_id")
    provenance = safe_details.get("strategy_delta_provenance")
    outer_verified = (
        provenance == "verified"
        and isinstance(delta_id, str)
        and _SAFE_OPAQUE_ID.fullmatch(delta_id.strip()) is not None
    )
    if has_strategy_binding and not outer_verified:
        safe_details["strategy_delta_id"] = None
        safe_details["strategy_delta_provenance"] = (
            "not_present" if provenance is None and delta_id is None else "unresolved"
        )
    if not outer_verified:
        if "learning" in safe_details:
            safe_details["learning"] = "no_learning"
        if "learning_record_id" in safe_details:
            safe_details["learning_record_id"] = None
        if "canonical_decision_record" in safe_details:
            safe_details["canonical_decision_record"] = _downgrade_canonical_decision_record(
                safe_details.get("canonical_decision_record")
            )
        return safe_details

    nested = safe_details.get("canonical_decision_record")
    if nested is not None:
        nested_mismatch = not isinstance(nested, dict)
        if not nested_mismatch:
            nested_mismatch = (
                nested.get("memory_delta_id") != delta_id
                or nested.get("memory_delta_provenance") != "verified"
            )
            outer_goal_id = safe_details.get("goal_id")
            outer_goal_revision = safe_details.get("goal_revision")
            if outer_goal_id is None or nested.get("goal_id") != outer_goal_id:
                nested_mismatch = True
            if outer_goal_revision is None or nested.get("goal_revision") != outer_goal_revision:
                nested_mismatch = True
            outer_digest = safe_details.get("decision_input_digest")
            if outer_digest is None:
                outer_digest = safe_details.get("input_digest")
            if outer_digest is None or nested.get("decision_input_digest") != outer_digest:
                nested_mismatch = True
            top_learning = safe_details.get("learning", "no_learning")
            nested_learning = nested.get("learning")
            if top_learning == "applied":
                nested_mismatch = nested_mismatch or not (
                    nested.get("status") == "verified"
                    and nested_learning == "applied"
                    and nested.get("learning_writeback_id")
                    == safe_details.get("learning_record_id")
                )
            elif nested_learning == "applied":
                nested_mismatch = True
        if nested_mismatch:
            safe_details["strategy_delta_id"] = None
            safe_details["strategy_delta_provenance"] = "unresolved"
            if "learning" in safe_details:
                safe_details["learning"] = "no_learning"
            if "learning_record_id" in safe_details:
                safe_details["learning_record_id"] = None
            safe_details["canonical_decision_record"] = _downgrade_canonical_decision_record(
                nested
            )
    elif safe_details.get("learning") == "applied":
        safe_details["learning"] = "no_learning"
        if "learning_record_id" in safe_details:
            safe_details["learning_record_id"] = None
    return safe_details


async def _validate_stored_receipt_provenance(
    details: dict[str, Any],
    *,
    candidate: GoalCandidateDecision | None,
    goal: Goal | None,
) -> dict[str, Any]:
    """Downgrade legacy provenance unless the current choice re-verifies it."""

    safe_details = _sanitize_strategy_delta_receipt(details)
    if safe_details.get("strategy_delta_provenance") != "verified":
        return safe_details
    if candidate is None or goal is None:
        return _sanitize_strategy_delta_receipt(
            {
                **safe_details,
                "strategy_delta_id": None,
                "strategy_delta_provenance": "unresolved",
            }
        )

    expected_digest = _safe_digest(candidate.inputs)
    stored_digest = safe_details.get("decision_input_digest") or safe_details.get("input_digest")
    stored_capability_id = safe_details.get("capability_id")
    if stored_digest != expected_digest:
        return _sanitize_strategy_delta_receipt(
            {
                **safe_details,
                "strategy_delta_id": None,
                "strategy_delta_provenance": "unresolved",
            }
        )
    resolved_id, resolved_provenance = await _resolve_strategy_delta_provenance(
        candidate=candidate,
        goal=goal,
        evidence_refs=safe_details.get("evidence_refs", []),
    )
    if (
        resolved_provenance != "verified"
        or resolved_id != safe_details.get("strategy_delta_id")
        or safe_details.get("goal_id") != candidate.goal_id
        or safe_details.get("goal_revision") != candidate.goal_revision
        or (
            stored_capability_id is not None
            and stored_capability_id != candidate.capability_id
        )
    ):
        return _sanitize_strategy_delta_receipt(
            {
                **safe_details,
                "strategy_delta_id": None,
                "strategy_delta_provenance": "unresolved",
            }
        )
    return safe_details


async def _existing_receipt(
    *,
    event_type: str,
    dedupe_key: str,
    candidate: GoalCandidateDecision | None = None,
    goal: Goal | None = None,
) -> dict[str, Any] | None:
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
            return await _validate_stored_receipt_provenance(
                details,
                candidate=candidate,
                goal=goal,
            )
    return None


async def _persist_receipt(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
    candidate: GoalCandidateDecision | None = None,
    goal: Goal | None = None,
) -> dict[str, Any]:
    safe_details = _redact_receipt_details(details)
    existing = await _existing_receipt(
        event_type=event_type,
        dedupe_key=str(safe_details.get("dedupe_key") or ""),
        candidate=candidate,
        goal=goal,
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
    evidence_refs = _safe_evidence_refs(
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
        inputs=request.inputs,
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
    strategy_delta_id, strategy_delta_provenance = await _resolve_strategy_delta_provenance(
        candidate=decision,
        goal=goal,
    )
    await _persist_receipt(
        event_type=_CANDIDATE_EVENT,
        summary=f"Goal candidate {decision.action.value} for {goal.id}",
        details=_candidate_receipt_details(
            decision,
            strategy_delta_id=strategy_delta_id,
            strategy_delta_provenance=strategy_delta_provenance,
        ),
        candidate=decision,
        goal=goal,
    )
    if decision.action is not GoalCandidateAction.act:
        await _persist_no_learning(
            decision,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason=f"candidate_not_dispatched:{decision.reason}",
            strategy_delta_id=strategy_delta_id,
            strategy_delta_provenance=strategy_delta_provenance,
            goal=goal,
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
    strategy_delta_id: str | None = None,
    strategy_delta_provenance: StrategyDeltaProvenance = "not_present",
    goal: Goal | None = None,
) -> GoalOutcomeReceipt:
    if strategy_delta_provenance != "verified":
        strategy_delta_id = None
    receipt = GoalOutcomeReceipt(
        receipt_type="no_learning",
        outcome_id="nl_" + hashlib.sha256(
            f"{candidate.dedupe_key}:no_learning".encode("utf-8")
        ).hexdigest()[:24],
        candidate_id=candidate.candidate_id,
        dedupe_key=candidate.dedupe_key,
        decision_input_digest=_safe_digest(candidate.inputs),
        strategy_delta_id=strategy_delta_id,
        strategy_delta_provenance=strategy_delta_provenance,
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
        details=_outcome_receipt_details(receipt, capability_id=candidate.capability_id),
        candidate=candidate,
        goal=goal,
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
    safe_evidence_refs = _safe_evidence_refs(*candidate.evidence_refs)
    if tuple(candidate.evidence_refs) != safe_evidence_refs:
        candidate = candidate.model_copy(update={"evidence_refs": list(safe_evidence_refs)})
    goal = await goal_repository.get(candidate.goal_id)
    strategy_delta_id, strategy_delta_provenance = await _resolve_strategy_delta_provenance(
        candidate=candidate,
        goal=goal,
    )
    if goal is None:
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason="goal_not_found",
            strategy_delta_id=strategy_delta_id,
            strategy_delta_provenance=strategy_delta_provenance,
            goal=goal,
        )
    current_revision = max(int(goal.revision or 1), 1)
    if _enum_value(goal.status) != "active":
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason="goal_not_active",
            strategy_delta_id=strategy_delta_id,
            strategy_delta_provenance=strategy_delta_provenance,
            goal=goal,
        )
    if current_revision != candidate.goal_revision:
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason="stale_goal_revision",
            strategy_delta_id=strategy_delta_id,
            strategy_delta_provenance=strategy_delta_provenance,
            goal=goal,
        )
    required_delta_id, correction_required = _required_strategy_delta_id(
        candidate=candidate,
        goal=goal,
    )
    if correction_required and (
        strategy_delta_provenance != "verified" or strategy_delta_id != required_delta_id
    ):
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason="strategy_delta_unresolved",
            evidence_refs=tuple(candidate.evidence_refs),
            strategy_delta_provenance="unresolved",
            goal=goal,
        )
    # Revalidate the live goal and any required correction before replaying a
    # cached outcome. A stale positive receipt must not bypass the correction
    # gate merely because its dedupe key still matches.
    existing = await _existing_receipt(
        event_type=_OUTCOME_EVENT,
        dedupe_key=candidate.dedupe_key,
        candidate=candidate,
        goal=goal,
    )
    if existing is not None:
        return GoalOutcomeReceipt.model_validate(existing)
    if not candidate.dispatchable:
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason=f"candidate_action_{candidate.action.value}",
            strategy_delta_id=strategy_delta_id,
            strategy_delta_provenance=strategy_delta_provenance,
            goal=goal,
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
                strategy_delta_id=strategy_delta_id,
                strategy_delta_provenance=strategy_delta_provenance,
                goal=goal,
            )
    if adapter is None:
        return await _persist_no_learning(
            candidate,
            execution_status="blocked",
            verification="unknown",
            usefulness="unknown",
            reason="execution_adapter_unavailable",
            strategy_delta_id=strategy_delta_id,
            strategy_delta_provenance=strategy_delta_provenance,
            goal=goal,
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
    evidence_refs = _safe_evidence_refs(
        *candidate.evidence_refs,
        *result.evidence_refs,
    )
    strategy_delta_id, strategy_delta_provenance = await _resolve_strategy_delta_provenance(
        candidate=candidate,
        goal=goal,
        evidence_refs=evidence_refs,
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
        decision_input_digest=_safe_digest(candidate.inputs),
        strategy_delta_id=strategy_delta_id,
        strategy_delta_provenance=strategy_delta_provenance,
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
    canonical_decision = _canonical_decision_for_outcome(outcome)
    if outcome.learning == "applied" and not (
        canonical_decision.status == "verified" and canonical_decision.learning == "applied"
    ):
        outcome = outcome.model_copy(update={"learning": "no_learning", "learning_record_id": None})
    await _persist_receipt(
        event_type=_OUTCOME_EVENT,
        summary=f"Goal candidate {candidate.candidate_id} outcome recorded",
        details=_outcome_receipt_details(outcome, capability_id=candidate.capability_id),
        candidate=candidate,
        goal=goal,
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
            strategy_delta_id=outcome.strategy_delta_id,
            strategy_delta_provenance=outcome.strategy_delta_provenance,
            goal=goal,
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
    try:
        goal = await goal_repository.get(goal_id)
    except Exception:
        goal = None
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
        # The audit row is legacy/untrusted input.  Without the original
        # candidate inputs we cannot prove a stored positive claim, so the
        # validator deliberately downgrades verified provenance on this list
        # surface instead of replaying it as authority.
        details = await _validate_stored_receipt_provenance(
            details,
            candidate=None,
            goal=goal,
        )
        receipts.append(
            {
                "audit_event_id": event.get("id"),
                "event_type": event.get("event_type"),
                "created_at": event.get("created_at"),
                **details,
            }
        )
    return receipts
