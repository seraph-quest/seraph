from dataclasses import replace

from src.guardian.feedback import GuardianLearningSignal
from src.guardian.learning_arbitration import arbitrate_learning_signal
from src.guardian.learning_evidence import (
    GuardianLearningAxisEvidence,
    learning_field_for_axis,
    neutral_axis_evidence,
    ordered_learning_axes,
)
from src.memory.procedural_guidance import ProceduralMemoryGuidance


def _axis_evidence(
    axis: str,
    *,
    source: str,
    bias: str,
    support_count: int,
    weighted_support: float | None = None,
    recency_score: float,
    confidence_score: float,
    quality_score: float,
    metadata_complete: bool = True,
) -> tuple[GuardianLearningAxisEvidence, ...]:
    evidence_by_axis: dict[str, GuardianLearningAxisEvidence] = {
        axis: GuardianLearningAxisEvidence(
            axis=axis,
            field_name=learning_field_for_axis(axis),
            source=source,
            bias=bias,
            support_count=support_count,
            weighted_support=(
                float(support_count)
                if weighted_support is None
                else weighted_support
            ),
            recency_score=recency_score,
            confidence_score=confidence_score,
            quality_score=quality_score,
            metadata_complete=metadata_complete,
        )
    }
    return tuple(
        evidence_by_axis.get(item_axis, neutral_axis_evidence(item_axis, source=source))
        for item_axis in ordered_learning_axes()
    )


def test_arbitrate_learning_signal_prefers_live_evidence_over_stale_conflicting_memory():
    live_signal = replace(
        GuardianLearningSignal.neutral("advisory"),
        bias="reduce_interruptions",
        axis_evidence=_axis_evidence(
            "delivery",
            source="live_signal",
            bias="reduce_interruptions",
            support_count=4,
            recency_score=0.95,
            confidence_score=1.0,
            quality_score=1.0,
        ),
    )
    procedural_guidance = ProceduralMemoryGuidance(
        intervention_type="advisory",
        bias="prefer_direct_delivery",
        axis_evidence=_axis_evidence(
            "delivery",
            source="procedural_memory",
            bias="prefer_direct_delivery",
            support_count=1,
            recency_score=0.0,
            confidence_score=0.63,
            quality_score=0.4,
        ),
    )

    arbitration = arbitrate_learning_signal(
        live_signal=live_signal,
        procedural_guidance=procedural_guidance,
    )

    assert arbitration.effective_signal.bias == "reduce_interruptions"
    assert arbitration.source_label == "heuristic_only"
    assert arbitration.selected_sources()["delivery"] == "live_signal"
    assert arbitration.selected_reasons()["delivery"] == "live_signal_stronger"


def test_arbitrate_learning_signal_uses_procedural_memory_when_live_signal_is_neutral():
    procedural_guidance = ProceduralMemoryGuidance(
        intervention_type="advisory",
        channel_bias="prefer_native_notification",
        axis_evidence=_axis_evidence(
            "channel",
            source="procedural_memory",
            bias="prefer_native_notification",
            support_count=3,
            recency_score=0.8,
            confidence_score=0.9,
            quality_score=0.7,
        ),
    )

    arbitration = arbitrate_learning_signal(
        live_signal=GuardianLearningSignal.neutral("advisory"),
        procedural_guidance=procedural_guidance,
    )

    assert arbitration.effective_signal.channel_bias == "prefer_native_notification"
    assert arbitration.source_label == "heuristic_plus_procedural_memory"
    assert arbitration.selected_sources()["channel"] == "procedural_memory"
    assert arbitration.selected_reasons()["channel"] == "procedural_memory_fills_live_gap"


def test_arbitrate_learning_signal_penalizes_missing_procedural_evidence():
    live_signal = replace(
        GuardianLearningSignal.neutral("advisory"),
        timing_bias="avoid_focus_windows",
        axis_evidence=_axis_evidence(
            "timing",
            source="live_signal",
            bias="avoid_focus_windows",
            support_count=3,
            recency_score=0.9,
            confidence_score=0.9,
            quality_score=1.0,
        ),
    )
    malformed_guidance = ProceduralMemoryGuidance(
        intervention_type="advisory",
        timing_bias="prefer_available_windows",
        axis_evidence=(),
    )

    arbitration = arbitrate_learning_signal(
        live_signal=live_signal,
        procedural_guidance=malformed_guidance,
    )

    assert arbitration.effective_signal.timing_bias == "avoid_focus_windows"
    assert arbitration.selected_sources()["timing"] == "live_signal"


def test_arbitrate_learning_signal_uses_weighted_support_for_conflicts():
    live_signal = replace(
        GuardianLearningSignal.neutral("advisory"),
        bias="reduce_interruptions",
        axis_evidence=_axis_evidence(
            "delivery",
            source="live_signal",
            bias="reduce_interruptions",
            support_count=4,
            weighted_support=1.0,
            recency_score=0.9,
            confidence_score=0.9,
            quality_score=0.9,
        ),
    )
    procedural_guidance = ProceduralMemoryGuidance(
        intervention_type="advisory",
        bias="prefer_direct_delivery",
        axis_evidence=_axis_evidence(
            "delivery",
            source="procedural_memory",
            bias="prefer_direct_delivery",
            support_count=2,
            weighted_support=2.0,
            recency_score=0.9,
            confidence_score=0.9,
            quality_score=0.9,
        ),
    )

    arbitration = arbitrate_learning_signal(
        live_signal=live_signal,
        procedural_guidance=procedural_guidance,
    )

    assert arbitration.effective_signal.bias == "prefer_direct_delivery"
    assert arbitration.selected_sources()["delivery"] == "procedural_memory"
    assert arbitration.selected_reasons()["delivery"] == "procedural_memory_stronger"


def test_arbitrate_learning_signal_keeps_neutral_when_gap_source_has_no_evidence():
    malformed_guidance = ProceduralMemoryGuidance(
        intervention_type="advisory",
        timing_bias="prefer_available_windows",
        axis_evidence=(),
    )

    arbitration = arbitrate_learning_signal(
        live_signal=GuardianLearningSignal.neutral("advisory"),
        procedural_guidance=malformed_guidance,
    )

    assert arbitration.effective_signal.timing_bias == "neutral"
    assert arbitration.selected_sources()["timing"] == "live_signal"
    assert arbitration.selected_reasons()["timing"] == "no_supported_bias"
