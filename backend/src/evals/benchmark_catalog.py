"""Shared benchmark-suite catalog for deterministic proof surfaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from src.browser.benchmark import COMPUTER_USE_BENCHMARK_SCENARIO_NAMES, COMPUTER_USE_BENCHMARK_SUITE_NAME
from src.cockpit.benchmark import (
    M7_OPERATOR_COCKPIT_BENCHMARK_SCENARIO_NAMES,
    M7_OPERATOR_COCKPIT_BENCHMARK_SUITE_NAME,
)
from src.cockpit.efficiency_benchmark import (
    COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES,
    COCKPIT_EFFICIENCY_BENCHMARK_SUITE_NAME,
)
from src.cockpit.production_operator_control import (
    PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES,
    PRODUCTION_OPERATOR_CONTROL_PARITY_SUITE_NAME,
    PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES,
    PRODUCTION_PARITY_TRAIN_SUITE_NAME,
)
from src.execution.benchmark import M2_EXECUTION_BENCHMARK_SCENARIO_NAMES, M2_EXECUTION_BENCHMARK_SUITE_NAME
from src.evolution.benchmark import (
    GOVERNED_IMPROVEMENT_BENCHMARK_SCENARIO_NAMES,
    GOVERNED_IMPROVEMENT_BENCHMARK_SUITE_NAME,
)
from src.evals.production_parity_readiness import (
    PRODUCTION_PARITY_READINESS_SCENARIO_NAMES,
    PRODUCTION_PARITY_READINESS_SUITE_NAME,
)
from src.extensions.benchmark import (
    GOVERNED_CAPABILITY_PACK_HARDENING_SCENARIO_NAMES,
    GOVERNED_CAPABILITY_PACK_HARDENING_SUITE_NAME,
    M9_GOVERNED_ECOSYSTEM_BENCHMARK_SCENARIO_NAMES,
    M9_GOVERNED_ECOSYSTEM_BENCHMARK_SUITE_NAME,
)
from src.extensions.marketplace_lifecycle import (
    CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES,
    CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SUITE_NAME,
    GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES,
    GOVERNED_CAPABILITY_LIFECYCLE_V2_SUITE_NAME,
    MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES,
    MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SUITE_NAME,
)
from src.extensions.reach_channel_canary import (
    ONE_REACH_CHANNEL_CANARY_SCENARIO_NAMES,
    ONE_REACH_CHANNEL_CANARY_SUITE_NAME,
)
from src.extensions.production_reach_hardening import (
    BROWSER_COMPUTER_USE_RELIABILITY_V2_SCENARIO_NAMES,
    BROWSER_COMPUTER_USE_RELIABILITY_V2_SUITE_NAME,
    GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SCENARIO_NAMES,
    GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SUITE_NAME,
    PRODUCTION_REACH_CHANNEL_HARDENING_SCENARIO_NAMES,
    PRODUCTION_REACH_CHANNEL_HARDENING_SUITE_NAME,
)
from src.extensions.live_reach_media import (
    CROSS_SURFACE_CONTINUITY_RECOVERY_SCENARIO_NAMES,
    CROSS_SURFACE_CONTINUITY_RECOVERY_SUITE_NAME,
    LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SCENARIO_NAMES,
    LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SUITE_NAME,
    PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SCENARIO_NAMES,
    PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SUITE_NAME,
)
from src.guardian.benchmark import (
    GUARDIAN_USER_MODEL_BENCHMARK_SCENARIO_NAMES,
    GUARDIAN_USER_MODEL_BENCHMARK_SUITE_NAME,
)
from src.guardian.brain import (
    M8_GUARDIAN_BRAIN_BENCHMARK_SCENARIO_NAMES,
    M8_GUARDIAN_BRAIN_BENCHMARK_SUITE_NAME,
)
from src.guardian.learning_arbitration_benchmark import (
    GUARDIAN_LEARNING_ARBITRATION_SCENARIO_NAMES,
    GUARDIAN_LEARNING_ARBITRATION_SUITE_NAME,
)
from src.guardian.live_learning_quality import (
    CANONICAL_MEMORY_RECONCILIATION_V2_SCENARIO_NAMES,
    CANONICAL_MEMORY_RECONCILIATION_V2_SUITE_NAME,
    GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SCENARIO_NAMES,
    GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SUITE_NAME,
    LIVE_GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES,
    LIVE_GUARDIAN_LEARNING_QUALITY_SUITE_NAME,
    MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SCENARIO_NAMES,
    MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SUITE_NAME,
    PROVIDER_USEFULNESS_REGRESSION_SCENARIO_NAMES,
    PROVIDER_USEFULNESS_REGRESSION_SUITE_NAME,
)
from src.guardian.live_human_outcome_learning import (
    GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SCENARIO_NAMES,
    GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SUITE_NAME,
    LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SCENARIO_NAMES,
    LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SUITE_NAME,
    MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SCENARIO_NAMES,
    MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SUITE_NAME,
)
from src.guardian.multimodal_voice import (
    GUARDIAN_SAFE_MULTIMODAL_VOICE_SCENARIO_NAMES,
    GUARDIAN_SAFE_MULTIMODAL_VOICE_SUITE_NAME,
)
from src.memory.benchmark import GUARDIAN_MEMORY_BENCHMARK_SCENARIO_NAMES, GUARDIAN_MEMORY_BENCHMARK_SUITE_NAME
from src.memory.provider_quality_gate import (
    MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES,
    MEMORY_PROVIDER_QUALITY_GATE_SUITE_NAME,
)
from src.memory.superiority_benchmark import (
    M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES,
    M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME,
)
from src.replay.benchmark import LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES, LIVE_REPLAY_BENCHMARK_SUITE_NAME
from src.security.benchmark import TRUST_BOUNDARY_BENCHMARK_SCENARIO_NAMES, TRUST_BOUNDARY_BENCHMARK_SUITE_NAME
from src.security.secure_host_benchmark import (
    SECURE_CAPABILITY_HOST_BENCHMARK_SCENARIO_NAMES,
    SECURE_CAPABILITY_HOST_BENCHMARK_SUITE_NAME,
)
from src.security.production_hardening import (
    PRODUCTION_SECURE_HOST_HARDENING_SCENARIO_NAMES,
    PRODUCTION_SECURE_HOST_HARDENING_SUITE_NAME,
    SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SCENARIO_NAMES,
    SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SUITE_NAME,
)
from src.security.production_isolation import (
    PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES,
    PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SUITE_NAME,
    PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES,
    PRODUCTION_ISOLATION_HARDENING_V2_SUITE_NAME,
    SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES,
    SECURITY_INCIDENT_RECOVERY_DRILL_SUITE_NAME,
)
from src.workflows.benchmark import (
    M5_OPERATING_LAYER_BENCHMARK_SCENARIO_NAMES,
    M5_OPERATING_LAYER_BENCHMARK_SUITE_NAME,
    WORKFLOW_ENDURANCE_BENCHMARK_SCENARIO_NAMES,
    WORKFLOW_ENDURANCE_BENCHMARK_SUITE_NAME,
)
from src.workflows.endurance_canary import (
    LIVE_WORKFLOW_ENDURANCE_CANARY_SCENARIO_NAMES,
    LIVE_WORKFLOW_ENDURANCE_CANARY_SUITE_NAME,
)
from src.workflows.durable_state import (
    DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES,
    DURABLE_WORKFLOW_ENGINE_BENCHMARK_SUITE_NAME,
    DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES,
    DURABLE_WORKFLOW_ENGINE_V2_SUITE_NAME,
    PRODUCTION_DURABLE_ORCHESTRATION_SCENARIO_NAMES,
    PRODUCTION_DURABLE_ORCHESTRATION_SUITE_NAME,
)
from src.workflows.live_orchestration import (
    LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES,
    LIVE_EXTERNAL_ORCHESTRATION_SUITE_NAME,
    ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES,
    ORCHESTRATION_CRASH_RECOVERY_STUDY_SUITE_NAME,
)

CHANNELS_PRESENCE_DEVICE_PAIRING_BENCHMARK_SUITE_NAME = "channels_presence_device_pairing"
CHANNELS_PRESENCE_DEVICE_PAIRING_BENCHMARK_SCENARIO_NAMES = (
    "channel_identity_boundary_metadata_behavior",
    "external_channel_continuity_behavior",
    "device_pairing_revocation_fail_closed",
    "channel_mutation_boundary_behavior",
    "channel_abuse_failure_review_behavior",
)


@dataclass(frozen=True)
class BenchmarkSuiteDefinition:
    name: str
    label: str
    description: str
    benchmark_axis: str
    operator_summary: str
    remaining_gap: str
    scenario_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scenario_count"] = len(self.scenario_names)
        return payload


_BENCHMARK_SUITES: tuple[BenchmarkSuiteDefinition, ...] = (
    BenchmarkSuiteDefinition(
        name=PRODUCTION_PARITY_READINESS_SUITE_NAME,
        label="Production parity readiness",
        description=(
            "Pins the production-grade parity train contract before later batches claim implementation readiness: "
            "batch proof paths, blocked claim language, Project-field receipts, duplicate-scope guardrails, "
            "operator receipt targets, and validation classes."
        ),
        benchmark_axis="production_parity_readiness",
        operator_summary=(
            "The production parity train now has an operator-visible readiness contract that blocks full parity, "
            "superiority, production-ready, secure/private, broad-reach, voice-parity, and marketplace claims "
            "until the later batch proofs land."
        ),
        remaining_gap=(
            "This readiness suite does not implement production-grade parity; secure host, orchestration, reach, "
            "learning, marketplace, and final cockpit verification batches remain open."
        ),
        scenario_names=PRODUCTION_PARITY_READINESS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GUARDIAN_MEMORY_BENCHMARK_SUITE_NAME,
        label="Guardian memory benchmark",
        description=(
            "Pins reasoning-heavy engineering-memory retrieval, contradiction-aware ranking, "
            "selective forgetting, and operator-visible failure reporting into one CI-gated suite."
        ),
        benchmark_axis="guardian_memory_quality",
        operator_summary=(
            "Guardian memory quality is benchmarked as contradiction-aware, selective, and operator-visible "
            "instead of just measuring raw recall volume."
        ),
        remaining_gap="Live long-horizon workload replay and external benchmark parity still remain for future work.",
        scenario_names=GUARDIAN_MEMORY_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GUARDIAN_USER_MODEL_BENCHMARK_SUITE_NAME,
        label="Guardian user-model and restraint benchmark",
        description=(
            "Pins persistent user-model receipts, ambiguity-aware clarification, "
            "guardian restraint, and operator-visible judgment contracts into one CI-gated suite."
        ),
        benchmark_axis="guardian_judgment_and_restraint",
        operator_summary=(
            "User modeling now tightens clarification and restraint behavior through explicit receipts instead of hidden personalization."
        ),
        remaining_gap="Longer-horizon live replay and broader external user-model benchmarks still remain for future work.",
        scenario_names=GUARDIAN_USER_MODEL_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=M8_GUARDIAN_BRAIN_BENCHMARK_SUITE_NAME,
        label="M8 guardian intervention quality",
        description=(
            "Pins guardian judgment over the capability substrate across act, defer, bundle, clarify, "
            "approval, and stay-silent cases with capability-choice receipts and operator correction hooks."
        ),
        benchmark_axis="m8_guardian_intervention_quality",
        operator_summary=(
            "M8 guardian intelligence is judged by receipted capability choice, restraint, timing, trust preservation, "
            "and recovery under ambiguous, stale, conflicting, risky, and no-action cases."
        ),
        remaining_gap=(
            "Live long-horizon human outcome studies and external guardian-intelligence superiority claims remain future proof work."
        ),
        scenario_names=M8_GUARDIAN_BRAIN_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GUARDIAN_SAFE_MULTIMODAL_VOICE_SUITE_NAME,
        label="Guardian-safe multimodal and voice",
        description=(
            "Pins voice, TTS/STT, browser vision, image/media analysis, and media delivery "
            "behind governance, audit, privacy, continuity, revocation, and guardian-value gates."
        ),
        benchmark_axis="guardian_safe_multimodal_voice",
        operator_summary=(
            "Voice and media capability families now have a dedicated proof gate that requires "
            "guardian value and operator-visible capture/provider/privacy/correction receipts."
        ),
        remaining_gap=(
            "Live broad voice runtime, production STT/TTS, mobile voice, and full multimodal "
            "runtime parity remain future implementation work."
        ),
        scenario_names=GUARDIAN_SAFE_MULTIMODAL_VOICE_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_REACH_CHANNEL_HARDENING_SUITE_NAME,
        label="Production reach channel hardening",
        description=(
            "Pins external messaging reach beyond the native notification canary behind pairing, revocation, "
            "identity binding, threading, approval handoff, privacy redaction, audit, and degraded recovery receipts."
        ),
        benchmark_axis="production_reach_channel_hardening",
        operator_summary=(
            "External messaging reach now has production-oriented receipts for one paired channel and fail-closed "
            "degraded surfaces without claiming broad OpenClaw-class reach."
        ),
        remaining_gap=(
            "Live broad mobile, SMS, Slack, Discord, and Telegram delivery at production scale remains future work."
        ),
        scenario_names=PRODUCTION_REACH_CHANNEL_HARDENING_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=BROWSER_COMPUTER_USE_RELIABILITY_V2_SUITE_NAME,
        label="Browser computer-use reliability v2",
        description=(
            "Pins browser provider truth, session partitioning, crash recovery, action timelines, and page-drift "
            "replay receipts for local, managed, and remote browser modes."
        ),
        benchmark_axis="browser_computer_use_reliability_v2",
        operator_summary=(
            "Browser/computer-use reliability now distinguishes local, managed, and remote provider truth while "
            "keeping session partitions and recovery receipts operator-visible."
        ),
        remaining_gap=(
            "Broader live browser transports, site-specific automation reliability, and full browser parity remain future work."
        ),
        scenario_names=BROWSER_COMPUTER_USE_RELIABILITY_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SUITE_NAME,
        label="Guardian-safe voice/media runtime",
        description=(
            "Pins guarded runtime receipts for voice, STT/TTS, browser vision, and media analysis: guardian value, "
            "privacy, transcript/audit, correction, deletion, and revocation fail-closed behavior."
        ),
        benchmark_axis="guardian_safe_voice_media_runtime",
        operator_summary=(
            "Voice/media runtime proof now advances from governance-only receipts to guarded runtime receipts "
            "without claiming voice or multimodal parity."
        ),
        remaining_gap=(
            "Production STT/TTS, live mobile voice, and full multimodal runtime parity remain future work."
        ),
        scenario_names=GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SUITE_NAME,
        label="Live broad reach channel attestation",
        description=(
            "Pins live or recorded-live mobile and messaging channel receipts for provider identity, consent, "
            "pairing, revocation, rate limits, abuse handling, approval handoff, and degraded recovery."
        ),
        benchmark_axis="live_broad_reach_channel_attestation",
        operator_summary=(
            "Broad reach now has evidence-mode-aware channel receipts across mobile push and external messaging "
            "without claiming OpenClaw-class reach or complete channel coverage."
        ),
        remaining_gap=(
            "Production channel SLAs, complete channel inventory, and broad daily-life availability remain future work."
        ),
        scenario_names=LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SUITE_NAME,
        label="Production voice/media provider runtime",
        description=(
            "Pins provider-named STT, TTS, and media-analysis runtime receipts for consent, capture boundaries, "
            "correction, deletion, provider failures, privacy, and fail-closed fallback."
        ),
        benchmark_axis="production_voice_media_provider_runtime",
        operator_summary=(
            "Voice/media provider runtime proof now names provider, evidence mode, consent, capture boundary, "
            "fallback, and deletion receipts without claiming voice or multimodal parity."
        ),
        remaining_gap=(
            "Production STT/TTS quality, mobile voice execution, and full multimodal runtime parity remain future work."
        ),
        scenario_names=PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CROSS_SURFACE_CONTINUITY_RECOVERY_SUITE_NAME,
        label="Cross-surface continuity recovery",
        description=(
            "Pins recovery receipts showing thread identity, memory context, approvals, audit, routing, and "
            "fail-closed degraded states across browser, desktop, mobile, messaging, and voice surfaces."
        ),
        benchmark_axis="cross_surface_continuity_recovery",
        operator_summary=(
            "Cross-surface reach can preserve thread, memory, approval, and recovery receipts across broader "
            "surfaces while blocking unsafe mutations during degraded handoff."
        ),
        remaining_gap=(
            "Live multi-surface operational scale and independent reliability evidence remain future work."
        ),
        scenario_names=CROSS_SURFACE_CONTINUITY_RECOVERY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GUARDIAN_LEARNING_ARBITRATION_SUITE_NAME,
        label="Guardian learning arbitration v2",
        description=(
            "Pins act, defer, bundle, clarify, approval, and stay-silent arbitration over stale, "
            "conflicting, ambiguous, degraded, unsafe, and repeated-negative intervention cases."
        ),
        benchmark_axis="guardian_learning_arbitration_v2",
        operator_summary=(
            "Guardian learning proof now exposes why Seraph acts, waits, asks, escalates, bundles, "
            "or stays silent under conflicting live and durable evidence."
        ),
        remaining_gap=(
            "Live long-horizon human outcome studies, external-channel intervention replay, and "
            "guardian-intelligence superiority claims remain future proof work."
        ),
        scenario_names=GUARDIAN_LEARNING_ARBITRATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LIVE_GUARDIAN_LEARNING_QUALITY_SUITE_NAME,
        label="Live guardian learning quality",
        description=(
            "Pins policy deltas, false-positive and false-negative receipts, stale-evidence decay, "
            "and operator-visible learning provenance beyond deterministic arbitration floors."
        ),
        benchmark_axis="live_guardian_learning_quality",
        operator_summary=(
            "Guardian learning now has production-oriented outcome receipts for policy change without claiming "
            "guardian intelligence superiority or live human-outcome superiority."
        ),
        remaining_gap=(
            "Live human outcome studies and reference-system guardian intelligence comparisons remain future proof work."
        ),
        scenario_names=LIVE_GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SUITE_NAME,
        label="Guardian intervention outcome cohorts",
        description=(
            "Pins accepted, ignored, corrected, deferred, harmful, helpful, channel-shifted, and follow-through "
            "outcome cohorts with typed policy-delta receipts."
        ),
        benchmark_axis="guardian_intervention_outcome_cohorts",
        operator_summary=(
            "Intervention outcomes now carry cohort-level receipts that explain how bad or good outcomes affect restraint, "
            "clarification, timing, channel choice, and follow-through attention."
        ),
        remaining_gap="Live multi-user outcome studies and real-world causal attribution remain future proof work.",
        scenario_names=GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SUITE_NAME,
        label="Memory provider ecosystem maturity v1",
        description=(
            "Pins provider usefulness, noise, contradiction, freshness, privacy, latency, outage, and behavior-changing "
            "contribution receipts while preserving canonical memory precedence."
        ),
        benchmark_axis="memory_provider_ecosystem_maturity_v1",
        operator_summary=(
            "Provider evidence can now be evaluated over time as useful, degraded, or quarantined without granting "
            "external providers canonical memory authority."
        ),
        remaining_gap="Broader live-provider workloads and provider-specific benchmark parity remain future proof work.",
        scenario_names=MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CANONICAL_MEMORY_RECONCILIATION_V2_SUITE_NAME,
        label="Canonical memory reconciliation v2",
        description=(
            "Pins canonical precedence, provider-assisted retrieval, advisory writeback, delete/export receipts, "
            "and provider quarantine behavior."
        ),
        benchmark_axis="canonical_memory_reconciliation_v2",
        operator_summary=(
            "Canonical memory remains the source of truth while provider retrieval, writeback, deletion, export, and "
            "quarantine are visible as advisory receipts."
        ),
        remaining_gap="Cross-provider deletion/export execution and external-provider attestation remain future proof work.",
        scenario_names=CANONICAL_MEMORY_RECONCILIATION_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PROVIDER_USEFULNESS_REGRESSION_SUITE_NAME,
        label="Provider usefulness regression",
        description=(
            "Pins provider behavior-change, latency/outage, privacy, and quarantine regressions before provider evidence "
            "can affect guardian behavior."
        ),
        benchmark_axis="provider_usefulness_regression",
        operator_summary=(
            "Provider usefulness is now guarded by regression receipts that keep unsafe, stale, private, or noisy evidence "
            "out of action-changing context."
        ),
        remaining_gap="Live provider-specific regression telemetry remains future proof work.",
        scenario_names=PROVIDER_USEFULNESS_REGRESSION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SUITE_NAME,
        label="Live human outcome quality study",
        description=(
            "Pins recorded-live, consent-aware, anonymized human-outcome cohorts with correction, harm, "
            "follow-through, and bias or coverage limitation receipts."
        ),
        benchmark_axis="live_human_outcome_quality_study",
        operator_summary=(
            "Guardian learning now exposes bounded recorded-live human outcome study receipts beyond deterministic "
            "outcome fixtures."
        ),
        remaining_gap=(
            "Larger independent live studies and generalized guardian-intelligence superiority evidence remain future proof work."
        ),
        scenario_names=LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SUITE_NAME,
        label="Guardian learning causal attribution",
        description=(
            "Pins counterfactual, negative-control, switchback, and harmful-intervention reversal receipts with bounded "
            "confidence, effect size, and confounder disclosure."
        ),
        benchmark_axis="guardian_learning_causal_attribution",
        operator_summary=(
            "Guardian learning changes now carry causal-attribution receipts that explain what the study can and cannot prove."
        ),
        remaining_gap="Independent causal studies and production superiority claims remain future proof work.",
        scenario_names=GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SUITE_NAME,
        label="Memory provider live regression monitor",
        description=(
            "Pins live-window usefulness deltas, stale-evidence decay, privacy and bias monitoring, quarantine, and "
            "reviewable reversal for memory-provider learning changes."
        ),
        benchmark_axis="memory_provider_live_regression_monitor",
        operator_summary=(
            "Provider-backed learning now exposes live regression monitors before provider evidence can change behavior."
        ),
        remaining_gap="Broader external-provider telemetry and provider-specific production attestation remain future proof work.",
        scenario_names=MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name="memory_continuity_workflows",
        label="Memory, continuity, and workflows",
        description=(
            "Measures whether canonical memory, long-running workflow recovery, and cross-session continuity "
            "still hold together under deterministic regression coverage."
        ),
        benchmark_axis="memory_and_workflow_endurance",
        operator_summary="Guardian memory and workflow continuity retain recoverable state instead of degrading into isolated surfaces.",
        remaining_gap="Broader live-provider and production-like workload replay is still missing.",
        scenario_names=(
            "memory_commitment_continuity_behavior",
            "memory_collaborator_lookup_behavior",
            "memory_provider_user_model_behavior",
            "memory_provider_stale_evidence_behavior",
            "memory_provider_writeback_behavior",
            "bounded_memory_snapshot_behavior",
            "memory_supersession_filter_behavior",
            "memory_decay_contradiction_cleanup_behavior",
            "memory_reconciliation_policy_behavior",
            "background_session_handoff_behavior",
            "workflow_context_condenser_behavior",
            "workflow_operating_layer_behavior",
            "engineering_memory_bundle_behavior",
            "operator_continuity_graph_behavior",
        ),
    ),
    BenchmarkSuiteDefinition(
        name=M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME,
        label="M6 memory superiority",
        description=(
            "Pins long-horizon recall, contradiction handling, stale-memory override, "
            "source trust and privacy boundaries, provider quality, and behavior-change receipts "
            "into one milestone-sized memory proof suite."
        ),
        benchmark_axis="m6_memory_superiority",
        operator_summary=(
            "M6 memory superiority is judged by recall, freshness, trust, provider quality, and receipted behavior change, "
            "not by raw memory volume or unbounded provider authority."
        ),
        remaining_gap=(
            "Broader live-provider workloads and external memory benchmark parity remain future proof work after the deterministic M6 lane."
        ),
        scenario_names=M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MEMORY_PROVIDER_QUALITY_GATE_SUITE_NAME,
        label="Memory provider quality gate",
        description=(
            "Pins external memory-provider declarations, quality-gated context entry, noisy/stale/conflict suppression, "
            "and operator correction, pin, forget, and audit visibility."
        ),
        benchmark_axis="memory_provider_quality_gate",
        operator_summary=(
            "External memory evidence must pass a canonical-first quality gate before shaping guardian context."
        ),
        remaining_gap=(
            "Broader live-provider workloads and provider-specific quality tuning remain future proof work."
        ),
        scenario_names=MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=WORKFLOW_ENDURANCE_BENCHMARK_SUITE_NAME,
        label="Workflow endurance, anticipatory repair, and backup branches",
        description=(
            "Pins anticipatory repair planning, checkpoint-backed backup branching, "
            "compaction fidelity, and multi-session workflow endurance into one deterministic proof lane."
        ),
        benchmark_axis="workflow_endurance_and_repair",
        operator_summary=(
            "Long-running workflows now surface backup branches, pre-action repair choices, and compaction-fidelity receipts instead of only exposing post-failure recovery."
        ),
        remaining_gap="Broader live workload replay and external long-context benchmark parity still remain for future work.",
        scenario_names=WORKFLOW_ENDURANCE_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LIVE_WORKFLOW_ENDURANCE_CANARY_SUITE_NAME,
        label="Live workflow endurance canary",
        description=(
            "Pins a replayable canary receipt for multi-session workflow endurance across delegated ownership, "
            "checkpoint branching, injected failure, recovery, artifact comparison, approval preservation, "
            "trust-boundary blocking, and final audit trail."
        ),
        benchmark_axis="live_workflow_endurance_canary",
        operator_summary=(
            "Long-running workflow endurance now has an operator-visible canary receipt over audit-projected state, "
            "without claiming a durable workflow engine."
        ),
        remaining_gap=(
            "Crash-safe durable workflow execution, heartbeat or reactive triggers, and production live workload studies "
            "remain future proof work."
        ),
        scenario_names=LIVE_WORKFLOW_ENDURANCE_CANARY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=DURABLE_WORKFLOW_ENGINE_BENCHMARK_SUITE_NAME,
        label="Durable workflow engine v1",
        description=(
            "Pins durable workflow state reporting, crash-safe continuation posture, heartbeat or trigger receipts, "
            "and operator-visible recovery policy into one CI-gated proof lane."
        ),
        benchmark_axis="durable_workflow_engine_v1",
        operator_summary=(
            "Durable workflow engine v1 exposes durable state, recovery, trigger, and operator receipt posture "
            "through a dedicated benchmark and operator surface."
        ),
        remaining_gap=(
            "Broader production workload replay and external long-running agent benchmark parity remain future proof work."
        ),
        scenario_names=DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_DURABLE_ORCHESTRATION_SUITE_NAME,
        label="Production durable orchestration",
        description=(
            "Pins the Batch BX production orchestration proof gate: durable leases, transition idempotency, "
            "trigger dedupe, unsafe-resume blocking, delegated-artifact adoption gates, and operator-visible "
            "recovery receipts without claiming LangGraph-class or exactly-once execution."
        ),
        benchmark_axis="production_durable_orchestration",
        operator_summary=(
            "Durable orchestration v2 now exposes production-oriented recovery receipts over leases, idempotent "
            "transitions, deduped triggers, unsafe-resume blocks, and delegated-artifact adoption gates."
        ),
        remaining_gap=(
            "This still does not claim full distributed workflow execution, exactly-once external scheduling, "
            "or competitor-superiority parity."
        ),
        scenario_names=PRODUCTION_DURABLE_ORCHESTRATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=DURABLE_WORKFLOW_ENGINE_V2_SUITE_NAME,
        label="Durable workflow engine v2",
        description=(
            "Adds v2 durable orchestration receipts on top of the v1 state kernel: lease ownership, revision "
            "guards, idempotent trigger and transition ledgers, recovery plans, and delegated artifact adoption."
        ),
        benchmark_axis="durable_workflow_engine_v2",
        operator_summary=(
            "Durable workflow engine v2 makes long-running recovery and multi-agent ownership legible through "
            "explicit receipts and negative cases."
        ),
        remaining_gap=(
            "External queue durability, exactly-once third-party effects, and broad live crash studies remain "
            "outside this bounded proof gate."
        ),
        scenario_names=DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LIVE_EXTERNAL_ORCHESTRATION_SUITE_NAME,
        label="Live external orchestration attestation",
        description=(
            "Pins recorded-live provider identity, replay windows, idempotency keys, side-effect boundaries, "
            "delivery semantics, and operator recovery receipts for external orchestration surfaces."
        ),
        benchmark_axis="live_external_orchestration_attestation",
        operator_summary=(
            "Live external orchestration is judged by provider identity, evidence mode, idempotency boundary, "
            "side-effect boundary, replay suppression, and recovery controls, not by exactly-once guarantees."
        ),
        remaining_gap=(
            "Continuous provider SLA monitoring, production exactly-once scheduling, and crash-proof orchestration "
            "remain outside this bounded recorded-live proof gate."
        ),
        scenario_names=LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=ORCHESTRATION_CRASH_RECOVERY_STUDY_SUITE_NAME,
        label="Orchestration crash recovery study",
        description=(
            "Pins crash/restart drill receipts across checkpoint recovery, side-effect boundaries, lease transfer, "
            "resume authority, duplicate replay suppression, and operator audit controls."
        ),
        benchmark_axis="orchestration_crash_recovery_study",
        operator_summary=(
            "Crash recovery proof must show what happened before and after the injected failure, what can be "
            "resumed, what must be audited, and which side effects remain uncertain."
        ),
        remaining_gap=(
            "This is recorded-live and deterministic crash-study evidence, not a production crash-proof workflow engine."
        ),
        scenario_names=ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LIVE_REPLAY_BENCHMARK_SUITE_NAME,
        label="Live-ish long-horizon eval replay",
        description=(
            "Pins fake-provider, time-stable replay receipts across memory, workflow, reach, security, "
            "and cockpit/operator surfaces before stronger live long-horizon quality claims."
        ),
        benchmark_axis="live_long_horizon_eval_replay",
        operator_summary=(
            "Long-horizon proof now has a replay substrate with failure taxonomy and operator receipts instead of "
            "depending on broad local shards or live provider behavior."
        ),
        remaining_gap=(
            "Live human-outcome studies, real provider attestation, and production external-channel replay remain future proof work."
        ),
        scenario_names=LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=M5_OPERATING_LAYER_BENCHMARK_SUITE_NAME,
        label="M5 jobs, routines, workflows, and delegation",
        description=(
            "Pins durable scheduled-job run history, pause/resume no-fire receipts, "
            "audit-projected workflow branch and repair state, background churn, and delegated trust partitions."
        ),
        benchmark_axis="m5_jobs_routines_workflows_delegation",
        operator_summary=(
            "M5 exposes cron-style jobs and routines, workflow projection receipts, background churn, "
            "and delegation trust partitions without claiming heartbeat triggers or a full durable workflow state machine."
        ),
        remaining_gap=(
            "Heartbeat/reactive triggers and a real durable workflow state machine remain future implementation work."
        ),
        scenario_names=M5_OPERATING_LAYER_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=TRUST_BOUNDARY_BENCHMARK_SUITE_NAME,
        label="Trust boundaries and safety receipts",
        description=(
            "Pins adversarial secret-egress, delegation partitioning, background-session containment, "
            "workflow boundary drift, and operator-visible safety receipts into one deterministic proof lane."
        ),
        benchmark_axis="trust_boundary_and_safety_receipts",
        operator_summary=(
            "Trust posture now has one explicit benchmark lane for secret egress, replay drift, delegation boundaries, and operator safety receipts."
        ),
        remaining_gap="Broader live hostile-environment replay and stronger privileged-path isolation still remain for future work.",
        scenario_names=TRUST_BOUNDARY_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SECURE_CAPABILITY_HOST_BENCHMARK_SUITE_NAME,
        label="M3 secure capability host",
        description=(
            "Pins concrete secure-host enforcement for secret refs, credential egress, "
            "workspace secret files, process environments, prompt-surface quarantine, delegation, and provider trust receipts."
        ),
        benchmark_axis="m3_secure_capability_host",
        operator_summary=(
            "Secure capability-host proof now binds least-privilege decisions to live choke points instead of adding receipt-only policy text."
        ),
        remaining_gap="Full host/container isolation, live hostile browser replay, and production provider trust telemetry remain future hardening work.",
        scenario_names=SECURE_CAPABILITY_HOST_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_SECURE_HOST_HARDENING_SUITE_NAME,
        label="Production secure-host hardening",
        description=(
            "Pins the Batch BW production-hardening contract above the M3 secure-host foundation: "
            "receipt schema, blocked claims, operator surfaces, current-source boundaries, and v2 live isolation gates."
        ),
        benchmark_axis="production_secure_host_hardening",
        operator_summary=(
            "Production secure-host hardening now has its own operator-visible gate while keeping secure/private, "
            "production-ready, and IronClaw-class claims blocked."
        ),
        remaining_gap=(
            "This is the privileged-path hardening gate, not full TEE/Wasm/container isolation or a production security claim."
        ),
        scenario_names=PRODUCTION_SECURE_HOST_HARDENING_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SUITE_NAME,
        label="Secure capability-host live isolation v2",
        description=(
            "Pins v2 live-isolation negative cases for secret replay/redaction, browser recovery partitions, "
            "private-network egress, revoked extension contributions, and workflow/provider trust drift."
        ),
        benchmark_axis="secure_capability_host_live_isolation_v2",
        operator_summary=(
            "Secure-host v2 live isolation makes privileged-path deny/recover receipts explicit across live-ish failure modes."
        ),
        remaining_gap=(
            "Live external attack replay and hardware-backed isolation remain future proof work before stronger security wording."
        ),
        scenario_names=SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_ISOLATION_HARDENING_V2_SUITE_NAME,
        label="Production isolation hardening v2",
        description=(
            "Pins Batch CD isolation evidence for worker roots, browser profiles, connector credentials, "
            "extension quarantine, workflow replay trust guards, operator receipts, and unsupported host-isolation claims."
        ),
        benchmark_axis="production_isolation_hardening_v2",
        operator_summary=(
            "Production isolation hardening v2 makes privileged boundary receipts visible while keeping "
            "secure/private, IronClaw-class, TEE/Wasm/container, and production-ready wording blocked."
        ),
        remaining_gap=(
            "This is deterministic and recorded-drill isolation evidence, not full host/container isolation or production security."
        ),
        scenario_names=PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SUITE_NAME,
        label="Privileged-path red-team gauntlet v2",
        description=(
            "Pins Batch CD red-team negative cases for secret replay, filesystem escape, private egress, "
            "plugin permission creep, prompt-injection delegation, and browser session bleed."
        ),
        benchmark_axis="privileged_path_red_team_gauntlet_v2",
        operator_summary=(
            "The privileged-path red-team gauntlet records what was blocked, quarantined, and recoverable "
            "without treating negative-case receipts as solved production security."
        ),
        remaining_gap="Live external adversarial testing and independent security review remain future proof work.",
        scenario_names=PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SECURITY_INCIDENT_RECOVERY_DRILL_SUITE_NAME,
        label="Security incident recovery drill",
        description=(
            "Pins Batch CD incident drills for revocation, quarantine, kill switch, evidence redaction, "
            "credential rotation, and operator notification."
        ),
        benchmark_axis="security_incident_recovery_drill",
        operator_summary=(
            "Security incidents now have replayable operator-visible drill receipts for containment, recovery, "
            "redaction, rotation, and notification."
        ),
        remaining_gap="This is replayable drill proof, not production incident response certification.",
        scenario_names=SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=COMPUTER_USE_BENCHMARK_SUITE_NAME,
        label="Computer-use, browser, and desktop execution",
        description=(
            "Pins replayable browser tasks, desktop notification actions, cross-surface continuity, and operator-visible receipts into one CI-gated proof lane."
        ),
        benchmark_axis="computer_use_execution",
        operator_summary=(
            "Browser and desktop execution now have one explicit benchmark lane with replay receipts instead of depending on isolated browser or daemon anecdotes."
        ),
        remaining_gap="Broader live website, OS, and mobile task depth still remains for future work.",
        scenario_names=COMPUTER_USE_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CHANNELS_PRESENCE_DEVICE_PAIRING_BENCHMARK_SUITE_NAME,
        label="M4 channels presence and device pairing",
        description=(
            "Pins channel identity boundaries, device pairing and revocation fail-closed posture, "
            "external-channel continuity, mutation boundaries, and abuse/failure review into one deterministic proof lane."
        ),
        benchmark_axis="presence_and_reach_across_surfaces",
        operator_summary=(
            "M4 reach now has deterministic proof that identity, pairing, revocation, continuity, mutation, "
            "and review boundaries stay visible instead of implying broad live channel reach."
        ),
        remaining_gap=(
            "Production-grade live pairing protocols, broader mobile or voice reach, and real external-channel abuse replay still remain future work."
        ),
        scenario_names=CHANNELS_PRESENCE_DEVICE_PAIRING_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=ONE_REACH_CHANNEL_CANARY_SUITE_NAME,
        label="One excellent reach channel canary",
        description=(
            "Pins the selected native-notification canary across pairing, revocation, health, retry, "
            "thread and memory continuity, approval handoff, audit receipts, degraded-state UI, "
            "and one-channel scope control."
        ),
        benchmark_axis="one_excellent_reach_channel_canary",
        operator_summary=(
            "Seraph now proves one native-notification reach path deeply before claiming broader external-channel reach."
        ),
        remaining_gap=(
            "Real Slack, Discord, Telegram, email, mobile, voice, and production pairing coverage remain future work."
        ),
        scenario_names=ONE_REACH_CHANNEL_CANARY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=M2_EXECUTION_BENCHMARK_SUITE_NAME,
        label="M2 execution supremacy completion",
        description=(
            "Pins the whole M2 execution milestone across terminal/process, browser/HTTP, sandbox, "
            "filesystem patches, artifact registry, operator receipts, and the #435 adversarial security gauntlet."
        ),
        benchmark_axis="m2_execution_completion",
        operator_summary=(
            "M2 execution readiness now has a single completion lane instead of being inferred from smaller execution slices."
        ),
        remaining_gap="Live hostile-environment replay, deeper remote computer-use providers, and external agent benchmarks still remain after M2.",
        scenario_names=M2_EXECUTION_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=M7_OPERATOR_COCKPIT_BENCHMARK_SUITE_NAME,
        label="M7 operator cockpit legibility",
        description=(
            "Pins operator-readable receipts, control-mode-labeled fast controls, control-plane handoff state, "
            "and trust-boundary clarity into one deterministic cockpit proof lane."
        ),
        benchmark_axis="m7_operator_cockpit_control_legibility",
        operator_summary=(
            "M7 cockpit legibility is judged by readable receipts and direct, routed, or drafted controls staying clearly labeled in operator surfaces, "
            "not by broad live usability or external superiority claims."
        ),
        remaining_gap=(
            "Live multi-operator usability studies and broader cross-device command latency proof remain future work."
        ),
        scenario_names=M7_OPERATOR_COCKPIT_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=COCKPIT_EFFICIENCY_BENCHMARK_SUITE_NAME,
        label="Cockpit operator efficiency benchmark",
        description=(
            "Pins scripted inspect, approve, deny, pause, resume, retry, repair, branch, compare, revoke, and audit "
            "task fixtures with action, time, error-detectability, and receipt metrics."
        ),
        benchmark_axis="cockpit_operator_efficiency",
        operator_summary=(
            "Cockpit efficiency is measured by deterministic operator task paths and receipts, not by density alone."
        ),
        remaining_gap=(
            "Live multi-operator usability studies and source-dated competitor superiority claims remain future proof work."
        ),
        scenario_names=COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name="planning_retrieval_reporting",
        label="Planning, retrieval, and reporting",
        description=(
            "Pins route planning, retrieval-adjacent source routines, and auditable publication planning into one proof layer."
        ),
        benchmark_axis="planning_and_retrieval_quality",
        operator_summary="Planning and retrieval behavior now has explicit route, source-review, and publication-proof seams instead of anecdotal claims.",
        remaining_gap="Live external-system benchmark depth is still narrower than the deterministic proof surface.",
        scenario_names=(
            "provider_policy_capabilities",
            "provider_policy_scoring",
            "provider_policy_safeguards",
            "provider_routing_decision_audit",
            "source_adapter_evidence_behavior",
            "source_review_routine_behavior",
            "source_mutation_boundary_behavior",
            "source_report_action_workflow_behavior",
            "activity_ledger_attribution_behavior",
        ),
    ),
    BenchmarkSuiteDefinition(
        name=GOVERNED_IMPROVEMENT_BENCHMARK_SUITE_NAME,
        label="Governed improvement safeguards",
        description=(
            "Pins anti-misevolution blocking, preference-diversity safeguards, canary-and-rollback policy, "
            "and operator-visible governed-improvement receipts into one deterministic proof lane."
        ),
        benchmark_axis="governed_self_improvement",
        operator_summary=(
            "Self-improvement now exposes anti-drift, canary, rollback, and receipt policy instead of treating candidate generation as sufficient proof."
        ),
        remaining_gap="Broader live adoption telemetry and longer-horizon candidate diversity replay still remain for future work.",
        scenario_names=GOVERNED_IMPROVEMENT_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=M9_GOVERNED_ECOSYSTEM_BENCHMARK_SUITE_NAME,
        label="M9 governed ecosystem",
        description=(
            "Pins manifest governance, lifecycle review gates, connector degradation truth, marketplace governance flow, "
            "diagnostics/update triage, and the operator-visible M9 benchmark surface into one deterministic proof lane."
        ),
        benchmark_axis="m9_governed_ecosystem",
        operator_summary=(
            "M9 ecosystem proof is judged by local governance foundations for extension packages, managed connectors, "
            "marketplace flows, diagnostics, and update triage, not by competitor superiority or production marketplace security claims."
        ),
        remaining_gap=(
            "Production marketplace security, external package verification networks, and broader third-party ecosystem operations remain future work."
        ),
        scenario_names=M9_GOVERNED_ECOSYSTEM_BENCHMARK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GOVERNED_CAPABILITY_PACK_HARDENING_SUITE_NAME,
        label="Governed capability-pack hardening",
        description=(
            "Pins review receipts, compatibility and downgrade truth, permission-creep blocking, "
            "supply-chain suspicion fail-closed behavior, rollback readiness, and operator-visible hardening proof."
        ),
        benchmark_axis="governed_capability_pack_hardening",
        operator_summary=(
            "Capability-pack changes now expose what changed, what risk changed, whether rollback is available, "
            "and which marketplace or trust claims remain blocked."
        ),
        remaining_gap=(
            "This is deterministic governance proof, not production marketplace security or third-party ecosystem maturity."
        ),
        scenario_names=GOVERNED_CAPABILITY_PACK_HARDENING_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SUITE_NAME,
        label="Marketplace-grade capability lifecycle",
        description=(
            "Pins install, update, downgrade, disable, rollback, review, quarantine, diagnostics, and staged rollout "
            "receipts with before/after, permission-delta, risk-delta, rollback, and recovery proof."
        ),
        benchmark_axis="marketplace_grade_capability_lifecycle",
        operator_summary=(
            "Capability lifecycle maturity is judged by operator-visible mutation receipts and recovery paths, "
            "not by package count or marketplace breadth."
        ),
        remaining_gap=(
            "Live third-party package attestation, production marketplace security, and ecosystem superiority remain future proof work."
        ),
        scenario_names=MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GOVERNED_CAPABILITY_LIFECYCLE_V2_SUITE_NAME,
        label="Governed capability lifecycle v2",
        description=(
            "Pins permission deltas, risk deltas, dependency graphs, compatibility resolution, staged rollout, "
            "cross-family coverage, and claim-boundary receipts."
        ),
        benchmark_axis="governed_capability_lifecycle_v2",
        operator_summary=(
            "Lifecycle v2 keeps skills, workflows, runbooks, packs, connectors, providers, and reach surfaces under "
            "one review/rollback/diagnostics contract."
        ),
        remaining_gap=(
            "Production-scale marketplace operations and live external verification remain future proof work."
        ),
        scenario_names=GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SUITE_NAME,
        label="Capability rollback and failure diagnostics",
        description=(
            "Pins failed-update recovery, rollback availability, permission-creep negative cases, diagnostics triage, "
            "and quarantine re-entry review receipts."
        ),
        benchmark_axis="capability_rollback_failure_diagnostics",
        operator_summary=(
            "Failed lifecycle changes must fail closed, remain diagnosable, and keep rollback or quarantine review visible."
        ),
        remaining_gap=(
            "Live marketplace incident drills and external package attestation remain future proof work."
        ),
        scenario_names=CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_OPERATOR_CONTROL_PARITY_SUITE_NAME,
        label="Production operator control parity",
        description=(
            "Pins dense long-work operator controls across durable orchestration, secure-host receipts, reach/browser/voice "
            "recovery, learning explanations, marketplace lifecycle events, approvals, and activity audit receipts."
        ),
        benchmark_axis="production_operator_control_parity",
        operator_summary=(
            "Operator control parity is judged by state, authority, risk, recovery, and receipt visibility across the "
            "production parity train, not by visual density or unsupported cockpit superiority claims."
        ),
        remaining_gap=(
            "Live multi-operator usability studies and solved operator-control claims remain outside this bounded proof gate."
        ),
        scenario_names=PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_PARITY_TRAIN_SUITE_NAME,
        label="Production parity train",
        description=(
            "Pins final train verification across Batch BV through CB: linked issues, merged prior PRs, proof suites, "
            "operator surfaces, residual risks, board receipts, and final critic/audit requirements."
        ),
        benchmark_axis="production_parity_train",
        operator_summary=(
            "The production parity train has an aggregate operator-visible verification surface while full parity, "
            "superiority, and production-ready wording remain claim-ledger gated."
        ),
        remaining_gap=(
            "Live third-party attestations, broad external reach, live human outcome studies, and production security "
            "claims still require separate proof before stronger wording is allowed."
        ),
        scenario_names=PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES,
    ),
)


def benchmark_suite_definitions() -> tuple[BenchmarkSuiteDefinition, ...]:
    return _BENCHMARK_SUITES


def benchmark_suite_names() -> tuple[str, ...]:
    return tuple(item.name for item in _BENCHMARK_SUITES)


def benchmark_suite_scenarios(selected_suite_names: Iterable[str] | None = None) -> list[str]:
    selected = set(str(name).strip() for name in (selected_suite_names or ()) if str(name).strip())
    suites = [suite for suite in _BENCHMARK_SUITES if not selected or suite.name in selected]
    missing = sorted(selected - {suite.name for suite in suites})
    if missing:
        raise ValueError(
            "Unknown benchmark suite(s): " + ", ".join(missing)
        )
    ordered: list[str] = []
    for suite in suites:
        for scenario_name in suite.scenario_names:
            if scenario_name not in ordered:
                ordered.append(scenario_name)
    return ordered


def benchmark_suite_report() -> list[dict[str, Any]]:
    return [suite.to_dict() for suite in _BENCHMARK_SUITES]
