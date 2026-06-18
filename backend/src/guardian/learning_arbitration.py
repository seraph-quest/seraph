from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace

from src.guardian.feedback import GuardianLearningSignal
from src.guardian.learning_evidence import (
    GuardianLearningAxisEvidence,
    clamp_unit_interval,
    learning_field_for_axis,
    learning_evidence_weight,
    neutral_axis_evidence,
    ordered_learning_axes,
)
from src.memory.procedural_guidance import ProceduralMemoryGuidance

_WEIGHT_TIE_TOLERANCE = 0.05


@dataclass(frozen=True)
class LearningAxisArbitration:
    axis: str
    field_name: str
    selected_bias: str
    selected_source: str
    selected_weight: float
    live_bias: str
    live_weight: float
    procedural_bias: str
    procedural_weight: float
    reason: str


@dataclass(frozen=True)
class GuardianLearningArbitration:
    live_signal: GuardianLearningSignal
    procedural_guidance: ProceduralMemoryGuidance
    effective_signal: GuardianLearningSignal
    decisions: tuple[LearningAxisArbitration, ...]

    @property
    def source_label(self) -> str:
        if any(decision.selected_source == "procedural_memory" for decision in self.decisions):
            return "heuristic_plus_procedural_memory"
        return "heuristic_only"

    def selected_sources(self) -> dict[str, str]:
        return {decision.axis: decision.selected_source for decision in self.decisions}

    def selected_reasons(self) -> dict[str, str]:
        return {decision.axis: decision.reason for decision in self.decisions}

    def selected_weights(self) -> dict[str, float]:
        return {decision.axis: decision.selected_weight for decision in self.decisions}

    def conflicting_decisions(self) -> tuple[LearningAxisArbitration, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if decision.live_bias != "neutral"
            and decision.procedural_bias != "neutral"
            and decision.live_bias != decision.procedural_bias
            and decision.live_weight > 0.0
            and decision.procedural_weight > 0.0
        )

    def procedural_override_conflicts(self) -> tuple[LearningAxisArbitration, ...]:
        return tuple(
            decision
            for decision in self.conflicting_decisions()
            if decision.selected_source == "procedural_memory"
        )

    def live_override_conflicts(self) -> tuple[LearningAxisArbitration, ...]:
        return tuple(
            decision
            for decision in self.conflicting_decisions()
            if decision.selected_source == "live_signal"
        )


def _normalize_evidence(
    *,
    axis: str,
    bias: str,
    evidence: GuardianLearningAxisEvidence,
    source: str,
) -> GuardianLearningAxisEvidence:
    normalized = evidence
    if normalized.axis != axis or normalized.field_name != learning_field_for_axis(axis):
        normalized = replace(
            normalized,
            axis=axis,
            field_name=learning_field_for_axis(axis),
        )
    if normalized.source != source or normalized.bias != bias:
        normalized = replace(
            normalized,
            source=source,
            bias=bias,
        )
    return normalized


def _live_axis_evidence(signal: GuardianLearningSignal, axis: str) -> GuardianLearningAxisEvidence:
    field_name = learning_field_for_axis(axis)
    return _normalize_evidence(
        axis=axis,
        bias=getattr(signal, field_name),
        evidence=signal.evidence_for_axis(axis),
        source="live_signal",
    )


def _procedural_axis_evidence(
    guidance: ProceduralMemoryGuidance,
    axis: str,
) -> GuardianLearningAxisEvidence:
    field_name = learning_field_for_axis(axis)
    return _normalize_evidence(
        axis=axis,
        bias=getattr(guidance, field_name),
        evidence=guidance.evidence_for_axis(axis),
        source="procedural_memory",
    )


def _axis_weight(evidence: GuardianLearningAxisEvidence) -> float:
    return learning_evidence_weight(evidence)


def _select_axis_bias(
    *,
    axis: str,
    live_evidence: GuardianLearningAxisEvidence,
    procedural_evidence: GuardianLearningAxisEvidence,
) -> tuple[GuardianLearningAxisEvidence, LearningAxisArbitration]:
    live_weight = _axis_weight(live_evidence)
    procedural_weight = _axis_weight(procedural_evidence)

    if live_weight == 0.0 and procedural_weight == 0.0:
        selected_evidence = neutral_axis_evidence(axis, source="live_signal")
        reason = "no_supported_bias"
    elif live_evidence.bias == procedural_evidence.bias:
        if live_evidence.bias == "neutral":
            selected_evidence = neutral_axis_evidence(axis, source="live_signal")
            reason = "both_neutral"
        elif procedural_weight > live_weight:
            selected_evidence = procedural_evidence
            reason = "aligned_bias_procedural_evidence_stronger"
        else:
            selected_evidence = live_evidence
            reason = "aligned_bias_live_evidence_stronger"
    elif live_evidence.bias == "neutral":
        if procedural_weight > 0.0:
            selected_evidence = procedural_evidence
            reason = "procedural_memory_fills_live_gap"
        else:
            selected_evidence = neutral_axis_evidence(axis, source="live_signal")
            reason = "procedural_memory_missing_evidence"
    elif procedural_evidence.bias == "neutral":
        if live_weight > 0.0:
            selected_evidence = live_evidence
            reason = "live_signal_fills_procedural_gap"
        else:
            selected_evidence = neutral_axis_evidence(axis, source="live_signal")
            reason = "live_signal_missing_evidence"
    elif live_weight >= procedural_weight + _WEIGHT_TIE_TOLERANCE:
        selected_evidence = live_evidence
        reason = "live_signal_stronger"
    elif procedural_weight >= live_weight + _WEIGHT_TIE_TOLERANCE:
        selected_evidence = procedural_evidence
        reason = "procedural_memory_stronger"
    elif live_evidence.recency_score >= procedural_evidence.recency_score:
        selected_evidence = live_evidence
        reason = "tie_prefers_fresher_live_signal"
    else:
        selected_evidence = procedural_evidence
        reason = "tie_prefers_durable_memory"

    decision = LearningAxisArbitration(
        axis=axis,
        field_name=learning_field_for_axis(axis),
        selected_bias=selected_evidence.bias,
        selected_source=selected_evidence.source,
        selected_weight=max(live_weight, procedural_weight)
        if selected_evidence.bias == "neutral"
        else (live_weight if selected_evidence.source == "live_signal" else procedural_weight),
        live_bias=live_evidence.bias,
        live_weight=live_weight,
        procedural_bias=procedural_evidence.bias,
        procedural_weight=procedural_weight,
        reason=reason,
    )
    return selected_evidence, decision


def arbitrate_learning_signal(
    *,
    live_signal: GuardianLearningSignal,
    procedural_guidance: ProceduralMemoryGuidance | None = None,
) -> GuardianLearningArbitration:
    guidance = procedural_guidance or ProceduralMemoryGuidance(
        intervention_type=live_signal.intervention_type
    )
    selected_biases: dict[str, str] = {}
    selected_axis_evidence: list[GuardianLearningAxisEvidence] = []
    decisions: list[LearningAxisArbitration] = []

    for axis in ordered_learning_axes():
        live_evidence = _live_axis_evidence(live_signal, axis)
        procedural_evidence = _procedural_axis_evidence(guidance, axis)
        selected_evidence, decision = _select_axis_bias(
            axis=axis,
            live_evidence=live_evidence,
            procedural_evidence=procedural_evidence,
        )
        selected_axis_evidence.append(selected_evidence)
        selected_biases[learning_field_for_axis(axis)] = selected_evidence.bias
        decisions.append(decision)

    effective_signal = replace(
        live_signal,
        axis_evidence=tuple(selected_axis_evidence),
        **selected_biases,
    )
    return GuardianLearningArbitration(
        live_signal=live_signal,
        procedural_guidance=guidance,
        effective_signal=effective_signal,
        decisions=tuple(decisions),
    )
