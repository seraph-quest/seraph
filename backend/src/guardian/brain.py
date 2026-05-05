"""M8 guardian brain decisions over the capability substrate."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any


M8_GUARDIAN_BRAIN_BENCHMARK_SUITE_NAME = "m8_guardian_intervention_quality"
M8_GUARDIAN_BRAIN_BENCHMARK_SCENARIO_NAMES = (
    "m8_capability_choice_act_behavior",
    "m8_ambiguous_evidence_clarify_behavior",
    "m8_stale_memory_defer_behavior",
    "m8_conflicting_commitment_bundle_behavior",
    "m8_risky_capability_approval_behavior",
    "m8_no_action_restraint_behavior",
    "operator_m8_guardian_brain_surface_behavior",
)


class GuardianBrainAction(str, enum.Enum):
    act = "act"
    defer = "defer"
    bundle = "bundle"
    clarify = "clarify"
    request_approval = "request_approval"
    stay_silent = "stay_silent"


@dataclass(frozen=True)
class CapabilityCandidate:
    id: str
    label: str
    lane: str
    risk_level: str = "low"
    trust_level: str = "core"
    channel: str = "operator_cockpit"
    requires_approval: bool = False
    operator_visible: bool = True
    continuity_state: str = "current_thread"
    summary: str = ""

    def to_receipt(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GuardianBrainContext:
    scenario_id: str
    signal: str
    content: str
    urgency: int = 3
    salience_level: str = "medium"
    evidence_quality: str = "grounded"
    memory_freshness: str = "fresh"
    memory_confidence: str = "grounded"
    project_state: str = "aligned"
    commitment_state: str = "clear"
    channel_context: str = "existing_thread"
    capability_risk: str = "low"
    interruption_cost: str = "medium"
    user_state: str = "available"
    operator_preference: str = "balanced"
    recent_feedback_bias: str = "neutral"
    requires_approval: bool = False
    no_action_preferred: bool = False
    trust_boundary: str = "core_workspace"
    capability_candidates: tuple[CapabilityCandidate, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GuardianBrainDecision:
    action: GuardianBrainAction
    reason: str
    selected_capability: CapabilityCandidate | None
    rejected_capabilities: tuple[CapabilityCandidate, ...]
    receipt: dict[str, Any]


def default_m8_capabilities() -> tuple[CapabilityCandidate, ...]:
    return (
        CapabilityCandidate(
            id="guardian.thread_continue",
            label="Continue existing thread",
            lane="continuity",
            risk_level="low",
            trust_level="core",
            channel="operator_cockpit",
            summary="Preserve continuity by continuing the current grounded work thread.",
        ),
        CapabilityCandidate(
            id="guardian.native_notification",
            label="Native notification",
            lane="reach",
            risk_level="medium",
            trust_level="paired_device",
            channel="native_notification",
            summary="Use a paired native surface when timing matters but the user is not in the cockpit.",
        ),
        CapabilityCandidate(
            id="guardian.workflow_repair",
            label="Workflow repair",
            lane="workflow",
            risk_level="medium",
            trust_level="core",
            channel="workflow",
            summary="Prepare a workflow repair or continuation when there is enough state to act.",
        ),
        CapabilityCandidate(
            id="guardian.external_mutation",
            label="External mutation",
            lane="external_action",
            risk_level="high",
            trust_level="approval_gated",
            channel="connector",
            requires_approval=True,
            summary="Crosses a privileged external boundary and must remain approval-gated.",
        ),
    )


def _candidate_score(candidate: CapabilityCandidate, context: GuardianBrainContext) -> int:
    score = 0
    if candidate.operator_visible:
        score += 2
    if candidate.continuity_state == "current_thread":
        score += 2
    if context.channel_context == candidate.channel:
        score += 3
    if context.project_state == "aligned" and candidate.lane in {"continuity", "workflow"}:
        score += 2
    if context.commitment_state in {"due_soon", "blocked"} and candidate.lane in {"continuity", "workflow"}:
        score += 2
    if context.operator_preference == "prefer_existing_thread" and candidate.lane == "continuity":
        score += 3
    if context.salience_level == "high" and candidate.channel in {"native_notification", "operator_cockpit"}:
        score += 1
    if candidate.risk_level == "high":
        score -= 3
    if candidate.trust_level in {"blocked", "unknown"}:
        score -= 8
    return score


def _sorted_candidates(context: GuardianBrainContext) -> tuple[CapabilityCandidate, ...]:
    candidates = context.capability_candidates or default_m8_capabilities()
    return tuple(sorted(candidates, key=lambda item: (_candidate_score(item, context), item.id), reverse=True))


def _choose_capability(context: GuardianBrainContext) -> tuple[CapabilityCandidate | None, tuple[CapabilityCandidate, ...]]:
    candidates = _sorted_candidates(context)
    viable = [
        candidate
        for candidate in candidates
        if candidate.trust_level not in {"blocked", "unknown"} and candidate.operator_visible
    ]
    selected = viable[0] if viable else None
    rejected = tuple(candidate for candidate in candidates if candidate is not selected)
    return selected, rejected


def _metric_score(action: GuardianBrainAction, context: GuardianBrainContext) -> dict[str, Any]:
    action_matches_risk = (
        (context.requires_approval or context.capability_risk == "high")
        and action == GuardianBrainAction.request_approval
    ) or (
        context.no_action_preferred
        and action == GuardianBrainAction.stay_silent
    ) or (
        context.evidence_quality in {"ambiguous", "split"} and action == GuardianBrainAction.clarify
    ) or (
        context.memory_freshness == "stale" and action == GuardianBrainAction.defer
    ) or (
        context.interruption_cost == "high" and action in {GuardianBrainAction.bundle, GuardianBrainAction.defer}
    ) or action in {GuardianBrainAction.act, GuardianBrainAction.bundle}
    return {
        "timing": "now" if action in {GuardianBrainAction.act, GuardianBrainAction.request_approval} else "guarded",
        "usefulness": "high" if action_matches_risk else "review",
        "false_positive_risk": "low" if action in {GuardianBrainAction.clarify, GuardianBrainAction.defer, GuardianBrainAction.stay_silent} else "managed",
        "false_negative_risk": "managed" if context.urgency >= 4 and action != GuardianBrainAction.act else "low",
        "trust_preservation": "high" if action != GuardianBrainAction.act or context.capability_risk != "high" else "approval_required",
        "recovery": "operator_correctable",
    }


def build_guardian_brain_decision(context: GuardianBrainContext) -> GuardianBrainDecision:
    selected, rejected = _choose_capability(context)
    action = GuardianBrainAction.act
    reason = "grounded_capability_choice"

    if not content_has_meaning(context.content):
        action = GuardianBrainAction.stay_silent
        reason = "empty_or_non_actionable_signal"
        selected = None
    elif context.no_action_preferred and context.urgency <= 2 and context.salience_level != "high":
        action = GuardianBrainAction.stay_silent
        reason = "restraint_preferred_no_action_case"
        selected = None
    elif context.evidence_quality in {"ambiguous", "split"} or context.project_state in {"ambiguous", "unknown"}:
        action = GuardianBrainAction.clarify
        reason = "ambiguous_evidence_or_project_anchor"
        selected = None
    elif context.commitment_state == "conflicting":
        action = GuardianBrainAction.bundle
        reason = "conflicting_commitments_bundle_for_operator_resolution"
    elif context.memory_freshness == "stale" and context.urgency < 4:
        action = GuardianBrainAction.defer
        reason = "stale_memory_requires_fresh_evidence"
        selected = None
    elif context.requires_approval or context.capability_risk == "high" or (selected and selected.requires_approval):
        action = GuardianBrainAction.request_approval
        reason = "risky_capability_requires_approval"
    elif context.channel_context in {"revoked", "degraded", "unpaired"}:
        action = GuardianBrainAction.defer
        reason = "channel_context_not_safe_to_use"
        selected = None
    elif context.interruption_cost == "high" and context.urgency < 4:
        action = GuardianBrainAction.bundle
        reason = "high_interruption_cost_bundle"
    elif context.recent_feedback_bias == "reduce_interruptions" and context.urgency < 4:
        action = GuardianBrainAction.bundle
        reason = "feedback_conditioned_restraint"
    elif context.urgency >= 4 and context.salience_level == "high":
        action = GuardianBrainAction.act
        reason = "high_salience_grounded_follow_through"

    receipt = {
        "scenario_id": context.scenario_id,
        "action": action.value,
        "reason": reason,
        "signal": context.signal,
        "selected_capability": selected.to_receipt() if selected else None,
        "rejected_capabilities": [candidate.to_receipt() for candidate in rejected[:4]],
        "inputs": {
            "urgency": context.urgency,
            "salience_level": context.salience_level,
            "evidence_quality": context.evidence_quality,
            "memory_freshness": context.memory_freshness,
            "memory_confidence": context.memory_confidence,
            "project_state": context.project_state,
            "commitment_state": context.commitment_state,
            "channel_context": context.channel_context,
            "capability_risk": context.capability_risk,
            "interruption_cost": context.interruption_cost,
            "user_state": context.user_state,
            "operator_preference": context.operator_preference,
            "trust_boundary": context.trust_boundary,
        },
        "scores": _metric_score(action, context),
        "operator_correction": {
            "can_correct_action": True,
            "can_correct_capability": selected is not None,
            "receipt_surface": "/api/operator/m8-guardian-brain",
        },
        "claim_boundary": "deterministic_guardian_judgment_receipt_not_live_superiority_claim",
    }
    return GuardianBrainDecision(
        action=action,
        reason=reason,
        selected_capability=selected,
        rejected_capabilities=rejected,
        receipt=receipt,
    )


def content_has_meaning(content: str) -> bool:
    return bool(str(content or "").strip())


def build_m8_guardian_brain_scenarios() -> tuple[GuardianBrainContext, ...]:
    return (
        GuardianBrainContext(
            scenario_id="m8_capability_choice_act_behavior",
            signal="blocked workflow has fresh state and aligned project context",
            content="Continue the release repair while the operator is available.",
            urgency=4,
            salience_level="high",
            project_state="aligned",
            commitment_state="due_soon",
            operator_preference="prefer_existing_thread",
        ),
        GuardianBrainContext(
            scenario_id="m8_ambiguous_evidence_clarify_behavior",
            signal="unclear request mentions it without a project anchor",
            content="Follow up on it",
            evidence_quality="ambiguous",
            project_state="ambiguous",
            memory_confidence="partial",
        ),
        GuardianBrainContext(
            scenario_id="m8_stale_memory_defer_behavior",
            signal="provider memory claims a stale workflow is still active",
            content="Resume old workflow",
            memory_freshness="stale",
            memory_confidence="partial",
            urgency=2,
        ),
        GuardianBrainContext(
            scenario_id="m8_conflicting_commitment_bundle_behavior",
            signal="two commitments compete for the same quiet window",
            content="Prepare both choices for review.",
            commitment_state="conflicting",
            interruption_cost="high",
            urgency=3,
        ),
        GuardianBrainContext(
            scenario_id="m8_risky_capability_approval_behavior",
            signal="external connector mutation may resolve a commitment",
            content="Update the external tracker.",
            capability_risk="high",
            requires_approval=True,
            urgency=4,
            capability_candidates=(
                CapabilityCandidate(
                    id="guardian.external_mutation",
                    label="External mutation",
                    lane="external_action",
                    risk_level="high",
                    trust_level="approval_gated",
                    channel="connector",
                    requires_approval=True,
                    summary="Approval-gated connector mutation.",
                ),
            ),
        ),
        GuardianBrainContext(
            scenario_id="m8_no_action_restraint_behavior",
            signal="low-value background nudge during quiet recovery",
            content="Maybe mention a minor optimization.",
            urgency=1,
            salience_level="low",
            no_action_preferred=True,
            interruption_cost="high",
            user_state="winding_down",
        ),
    )


def build_m8_guardian_brain_receipts() -> list[dict[str, Any]]:
    return [
        build_guardian_brain_decision(context).receipt
        for context in build_m8_guardian_brain_scenarios()
    ]
