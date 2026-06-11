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
from src.cockpit.dense_operator_recovery import (
    INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SCENARIO_NAMES,
    INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SUITE_NAME,
    LONG_WORK_DEBUGGING_RECOVERY_SCENARIO_NAMES,
    LONG_WORK_DEBUGGING_RECOVERY_SUITE_NAME,
    OPERATOR_CONTROL_DENSITY_SCENARIO_NAMES,
    OPERATOR_CONTROL_DENSITY_SUITE_NAME,
)
from src.cockpit.operator_mission_control import (
    LONG_WORK_DEBUGGING_SLO_SCENARIO_NAMES,
    LONG_WORK_DEBUGGING_SLO_SUITE_NAME,
    NAMED_BASELINE_COCKPIT_COMPARISON_SCENARIO_NAMES,
    NAMED_BASELINE_COCKPIT_COMPARISON_SUITE_NAME,
    OPERATOR_CONTROL_POPULATION_STUDY_SCENARIO_NAMES,
    OPERATOR_CONTROL_POPULATION_STUDY_SUITE_NAME,
)
from src.cockpit.operator_control_certification import (
    LONG_WORK_RECOVERY_SLO_V2_SCENARIO_NAMES,
    LONG_WORK_RECOVERY_SLO_V2_SUITE_NAME,
    MISSION_CONTROL_POPULATION_STUDY_V2_SCENARIO_NAMES,
    MISSION_CONTROL_POPULATION_STUDY_V2_SUITE_NAME,
    OPERATOR_CONTROL_CERTIFICATION_V1_SCENARIO_NAMES,
    OPERATOR_CONTROL_CERTIFICATION_V1_SUITE_NAME,
    OPERATOR_ERROR_DETECTABILITY_V1_SCENARIO_NAMES,
    OPERATOR_ERROR_DETECTABILITY_V1_SUITE_NAME,
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
from src.evals.final_parity_audit import (
    BOARD_PR_ISSUE_RECONCILIATION_V3_SCENARIO_NAMES,
    BOARD_PR_ISSUE_RECONCILIATION_V3_SUITE_NAME,
    FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES,
    FINAL_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME,
    FINAL_FULL_PARITY_CLAIM_LIFT_V1_SCENARIO_NAMES,
    FINAL_FULL_PARITY_CLAIM_LIFT_V1_SUITE_NAME,
    FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES,
    FINAL_SOURCE_BACKED_PARITY_AUDIT_SUITE_NAME,
    FALSE_COMPLETION_SCAN_V2_SCENARIO_NAMES,
    FALSE_COMPLETION_SCAN_V2_SUITE_NAME,
    FALSE_COMPLETION_SCAN_V3_SCENARIO_NAMES,
    FALSE_COMPLETION_SCAN_V3_SUITE_NAME,
    OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES,
    OPERATOR_FINAL_PARITY_READINESS_REPORT_SUITE_NAME,
    POST_CQ_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES,
    POST_CQ_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME,
    PRODUCTION_READINESS_SOAK_V1_SCENARIO_NAMES,
    PRODUCTION_READINESS_SOAK_V1_SUITE_NAME,
    REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SCENARIO_NAMES,
    REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SUITE_NAME,
    REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SCENARIO_NAMES,
    REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SUITE_NAME,
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
from src.extensions.live_marketplace_attestation import (
    MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SCENARIO_NAMES,
    MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SUITE_NAME,
    PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SCENARIO_NAMES,
    PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SUITE_NAME,
    THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES,
    THIRD_PARTY_MARKETPLACE_ATTESTATION_SUITE_NAME,
)
from src.extensions.production_marketplace_security import (
    HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES,
    HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SUITE_NAME,
    INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES,
    INDEPENDENT_PACKAGE_SECURITY_REVIEW_SUITE_NAME,
    MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES,
    MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SUITE_NAME,
    PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES,
    PACKAGE_NETWORK_INCIDENT_OPERATIONS_SUITE_NAME,
    PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES,
    PUBLISHER_TRUST_VULNERABILITY_HANDLING_SUITE_NAME,
)
from src.extensions.marketplace_security_corpus import (
    CONTINUOUS_VULNERABILITY_MONITORING_SCENARIO_NAMES,
    CONTINUOUS_VULNERABILITY_MONITORING_SUITE_NAME,
    MARKETPLACE_SECURITY_CORPUS_SCENARIO_NAMES,
    MARKETPLACE_SECURITY_CORPUS_SUITE_NAME,
    PUBLISHER_TRUST_OPERATIONS_SCENARIO_NAMES,
    PUBLISHER_TRUST_OPERATIONS_SUITE_NAME,
)
from src.extensions.production_secure_marketplace import (
    HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SCENARIO_NAMES,
    HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SUITE_NAME,
    MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SCENARIO_NAMES,
    MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SUITE_NAME,
    PRODUCTION_SECURE_MARKETPLACE_V1_SCENARIO_NAMES,
    PRODUCTION_SECURE_MARKETPLACE_V1_SUITE_NAME,
    THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SCENARIO_NAMES,
    THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SUITE_NAME,
)
from src.extensions.browser_provider_usability import (
    BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES,
    BROWSER_COMPUTER_USE_RECOVERY_DRILL_SUITE_NAME,
    LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES,
    LIVE_MULTI_OPERATOR_USABILITY_STUDY_SUITE_NAME,
    MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES,
    MANAGED_BROWSER_PROVIDER_ATTESTATION_SUITE_NAME,
)
from src.extensions.safe_browser_computer_use import (
    AUTONOMOUS_BROWSER_SAFETY_SCENARIO_NAMES,
    AUTONOMOUS_BROWSER_SAFETY_SUITE_NAME,
    BROWSER_PROVIDER_RELIABILITY_MATRIX_SCENARIO_NAMES,
    BROWSER_PROVIDER_RELIABILITY_MATRIX_SUITE_NAME,
    BROWSER_SESSION_PARTITIONING_SCENARIO_NAMES,
    BROWSER_SESSION_PARTITIONING_SUITE_NAME,
    INDEPENDENT_BROWSER_USABILITY_REVIEW_SCENARIO_NAMES,
    INDEPENDENT_BROWSER_USABILITY_REVIEW_SUITE_NAME,
    LIVE_BROWSER_TASK_DEPTH_SCENARIO_NAMES,
    LIVE_BROWSER_TASK_DEPTH_SUITE_NAME,
    SITE_SPECIFIC_BROWSER_RECOVERY_SCENARIO_NAMES,
    SITE_SPECIFIC_BROWSER_RECOVERY_SUITE_NAME,
)
from src.extensions.browser_computer_use_parity_depth import (
    BROWSER_AUTH_PARTITION_OPERATIONS_SCENARIO_NAMES,
    BROWSER_AUTH_PARTITION_OPERATIONS_SUITE_NAME,
    BROWSER_TASK_BREADTH_MATRIX_SCENARIO_NAMES,
    BROWSER_TASK_BREADTH_MATRIX_SUITE_NAME,
    SITE_DRIFT_RECOVERY_SLO_SCENARIO_NAMES,
    SITE_DRIFT_RECOVERY_SLO_SUITE_NAME,
)
from src.extensions.full_browser_parity import (
    BROWSER_SESSION_PARTITION_CERTIFICATION_V1_SCENARIO_NAMES,
    BROWSER_SESSION_PARTITION_CERTIFICATION_V1_SUITE_NAME,
    FULL_BROWSER_PARITY_MATRIX_V1_SCENARIO_NAMES,
    FULL_BROWSER_PARITY_MATRIX_V1_SUITE_NAME,
    REAL_SITE_DRIFT_RECOVERY_V2_SCENARIO_NAMES,
    REAL_SITE_DRIFT_RECOVERY_V2_SUITE_NAME,
    SAFE_AUTONOMOUS_BROWSER_RUNTIME_V1_SCENARIO_NAMES,
    SAFE_AUTONOMOUS_BROWSER_RUNTIME_V1_SUITE_NAME,
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
from src.extensions.production_reach_voice_mobile import (
    BROAD_CHANNEL_SLA_OPERATIONS_SCENARIO_NAMES,
    BROAD_CHANNEL_SLA_OPERATIONS_SUITE_NAME,
    MOBILE_EXECUTION_CONTINUITY_SCENARIO_NAMES,
    MOBILE_EXECUTION_CONTINUITY_SUITE_NAME,
    PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SCENARIO_NAMES,
    PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SUITE_NAME,
)
from src.extensions.field_reach_operations import (
    ALWAYS_AVAILABLE_REACH_SLO_SCENARIO_NAMES,
    ALWAYS_AVAILABLE_REACH_SLO_SUITE_NAME,
    BROAD_REACH_FIELD_OPERATIONS_SCENARIO_NAMES,
    BROAD_REACH_FIELD_OPERATIONS_SUITE_NAME,
    VOICE_MEDIA_QUALITY_OPERATIONS_SCENARIO_NAMES,
    VOICE_MEDIA_QUALITY_OPERATIONS_SUITE_NAME,
)
from src.extensions.always_available_reach_media import (
    ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SCENARIO_NAMES,
    ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SUITE_NAME,
    MOBILE_CROSS_SURFACE_CONTINUITY_V1_SCENARIO_NAMES,
    MOBILE_CROSS_SURFACE_CONTINUITY_V1_SUITE_NAME,
    REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SCENARIO_NAMES,
    REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SUITE_NAME,
    VOICE_MEDIA_PARITY_RUNTIME_V1_SCENARIO_NAMES,
    VOICE_MEDIA_PARITY_RUNTIME_V1_SUITE_NAME,
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
from src.guardian.generalized_guardian_outcomes import (
    CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SCENARIO_NAMES,
    CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SUITE_NAME,
    FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SCENARIO_NAMES,
    FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SUITE_NAME,
    GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SCENARIO_NAMES,
    GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SUITE_NAME,
    MEMORY_BASELINE_COMPARISON_V1_SCENARIO_NAMES,
    MEMORY_BASELINE_COMPARISON_V1_SUITE_NAME,
)
from src.guardian.independent_learning_memory_parity import (
    INDEPENDENT_OUTCOME_COHORT_REVIEW_SCENARIO_NAMES,
    INDEPENDENT_OUTCOME_COHORT_REVIEW_SUITE_NAME,
    MEMORY_PROVIDER_PARITY_MATRIX_SCENARIO_NAMES,
    MEMORY_PROVIDER_PARITY_MATRIX_SUITE_NAME,
    TASK_SCOPED_CAUSAL_LEARNING_SCENARIO_NAMES,
    TASK_SCOPED_CAUSAL_LEARNING_SUITE_NAME,
)
from src.guardian.longitudinal_guardian_outcomes import (
    LEARNING_SAFETY_MONITOR_V2_SCENARIO_NAMES,
    LEARNING_SAFETY_MONITOR_V2_SUITE_NAME,
    LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SCENARIO_NAMES,
    LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SUITE_NAME,
    NAMED_BASELINE_MEMORY_COMPARISON_SCENARIO_NAMES,
    NAMED_BASELINE_MEMORY_COMPARISON_SUITE_NAME,
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
from src.security.independent_review import (
    INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES,
    INDEPENDENT_SECURE_HOST_REVIEW_SUITE_NAME,
    LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES,
    LIVE_HOSTILE_ISOLATION_DRILLS_SUITE_NAME,
    SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES,
    SECURE_HOST_RECOVERY_AUTHORITY_SUITE_NAME,
)
from src.security.container_grade_host import (
    CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES,
    CONTAINER_GRADE_CAPABILITY_ISOLATION_SUITE_NAME,
    EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES,
    EXTERNAL_SECURITY_VALIDATION_V1_SUITE_NAME,
    SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES,
    SECRET_EGRESS_CERTIFICATION_DRILL_SUITE_NAME,
)
from src.security.certified_secure_host import (
    CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SCENARIO_NAMES,
    CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SUITE_NAME,
    EXTERNAL_SECURITY_CERTIFICATION_SCENARIO_NAMES,
    EXTERNAL_SECURITY_CERTIFICATION_SUITE_NAME,
    HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SCENARIO_NAMES,
    HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SUITE_NAME,
    RUNTIME_ISOLATION_IMPLEMENTATION_SCENARIO_NAMES,
    RUNTIME_ISOLATION_IMPLEMENTATION_SUITE_NAME,
)
from src.security.production_grade_secure_host import (
    CREDENTIAL_BROKER_EGRESS_SOAK_SCENARIO_NAMES,
    CREDENTIAL_BROKER_EGRESS_SOAK_SUITE_NAME,
    PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SCENARIO_NAMES,
    PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SUITE_NAME,
    RUNTIME_ISOLATION_ATTESTATION_MATRIX_SCENARIO_NAMES,
    RUNTIME_ISOLATION_ATTESTATION_MATRIX_SUITE_NAME,
    SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SCENARIO_NAMES,
    SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SUITE_NAME,
    SECURE_HOST_FALSE_CLAIM_SCAN_SCENARIO_NAMES,
    SECURE_HOST_FALSE_CLAIM_SCAN_SUITE_NAME,
    SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SCENARIO_NAMES,
    SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SUITE_NAME,
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
from src.workflows.production_sla_orchestration import (
    DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES,
    DUPLICATE_SIDE_EFFECT_AUDIT_SUITE_NAME,
    EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES,
    EXACTLY_ONCE_RECOVERY_EVIDENCE_SUITE_NAME,
    PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES,
    PRODUCTION_SLA_ORCHESTRATION_SUITE_NAME,
)
from src.workflows.continuous_orchestration_slo import (
    CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES,
    CONTINUOUS_ORCHESTRATION_SLO_SUITE_NAME,
    CRASH_FAILOVER_SOAK_SCENARIO_NAMES,
    CRASH_FAILOVER_SOAK_SUITE_NAME,
    SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES,
    SIDE_EFFECT_RECONCILIATION_V2_SUITE_NAME,
)
from src.workflows.production_workflow_guarantees import (
    CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SCENARIO_NAMES,
    CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SUITE_NAME,
    EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SCENARIO_NAMES,
    EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SUITE_NAME,
    PRODUCTION_WORKFLOW_STATE_MACHINE_SCENARIO_NAMES,
    PRODUCTION_WORKFLOW_STATE_MACHINE_SUITE_NAME,
)
from src.workflows.production_orchestration_hard_guarantees import (
    DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SCENARIO_NAMES,
    DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SUITE_NAME,
    EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SCENARIO_NAMES,
    EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SUITE_NAME,
    ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES,
    ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
    PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SCENARIO_NAMES,
    PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SUITE_NAME,
    SCHEDULER_FAILOVER_SOAK_V1_SCENARIO_NAMES,
    SCHEDULER_FAILOVER_SOAK_V1_SUITE_NAME,
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
        name=BROAD_CHANNEL_SLA_OPERATIONS_SUITE_NAME,
        label="Broad channel SLA operations",
        description=(
            "Pins Batch CL channel SLA receipts for provider identity, consent, pairing, revocation, health, "
            "delivery windows, rate limits, abuse handling, degraded recovery, and coverage gaps."
        ),
        benchmark_axis="broad_channel_sla_operations",
        operator_summary=(
            "Reach proof now exposes production-oriented channel SLA and abuse-operation receipts while "
            "still blocking OpenClaw-class reach and always-available wording."
        ),
        remaining_gap=(
            "Complete channel coverage, broad always-available operation, and OpenClaw-class reach remain blocked."
        ),
        scenario_names=BROAD_CHANNEL_SLA_OPERATIONS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SUITE_NAME,
        label="Production voice/media quality gates",
        description=(
            "Pins Batch CL STT/TTS/media quality, latency, privacy, correction, deletion, memory-import, "
            "provider regression, and fallback receipts."
        ),
        benchmark_axis="production_voice_media_quality_gates",
        operator_summary=(
            "Voice/media proof now exposes quality gates and regression fallback receipts while still blocking "
            "voice parity, multimodal parity, and production STT/TTS solved claims."
        ),
        remaining_gap=(
            "Independent large-scale quality studies and full voice/media parity remain future proof work."
        ),
        scenario_names=PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MOBILE_EXECUTION_CONTINUITY_SUITE_NAME,
        label="Mobile execution continuity",
        description=(
            "Pins Batch CL mobile notification, approval handoff, action continuity, memory/thread continuity, "
            "offline recovery, and revocation fail-closed receipts."
        ),
        benchmark_axis="mobile_execution_continuity",
        operator_summary=(
            "Mobile execution proof now exposes continuity and recovery receipts while blocking production "
            "mobile execution solved or always-available operation wording."
        ),
        remaining_gap=(
            "Production mobile execution at scale and always-available reach remain future proof work."
        ),
        scenario_names=MOBILE_EXECUTION_CONTINUITY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=BROAD_REACH_FIELD_OPERATIONS_SUITE_NAME,
        label="Broad reach field operations",
        description=(
            "Pins Batch CU provider/channel field-operation receipts for mobile, messaging, email, calendar, "
            "webhook, configured degraded channels, consent, auth, revocation, provider windows, rate limits, "
            "abuse handling, degraded recovery, coverage gaps, and cross-surface continuity."
        ),
        benchmark_axis="broad_reach_field_operations",
        operator_summary=(
            "Reach proof now exposes broader field-operation receipts while still blocking OpenClaw-class reach, "
            "complete channel coverage, and always-available wording."
        ),
        remaining_gap=(
            "OpenClaw-class reach, complete channel coverage, and always-available operation remain blocked."
        ),
        scenario_names=BROAD_REACH_FIELD_OPERATIONS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=VOICE_MEDIA_QUALITY_OPERATIONS_SUITE_NAME,
        label="Voice/media quality operations",
        description=(
            "Pins Batch CU STT/TTS/media/voice-command field-operation receipts for quality gates, latency "
            "budgets, provider regression fallback, correction, deletion, privacy, and memory-import boundaries."
        ),
        benchmark_axis="voice_media_quality_operations",
        operator_summary=(
            "Voice/media proof now exposes field quality operations while still blocking voice parity, "
            "multimodal parity, and production STT/TTS solved claims."
        ),
        remaining_gap=(
            "Full voice/media parity and production STT/TTS solved claims remain blocked."
        ),
        scenario_names=VOICE_MEDIA_QUALITY_OPERATIONS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=ALWAYS_AVAILABLE_REACH_SLO_SUITE_NAME,
        label="Always-available reach SLO boundary",
        description=(
            "Pins Batch CU bounded reach-SLO receipts for observed provider windows, error budgets, offline "
            "recovery, provider-failure drills, operator recovery actions, and explicit always-available claim "
            "boundaries."
        ),
        benchmark_axis="always_available_reach_slo",
        operator_summary=(
            "Reach proof now exposes bounded SLO and recovery receipts while keeping always-available operation "
            "and production readiness wording blocked."
        ),
        remaining_gap=(
            "Always-available reach, production readiness, and full parity remain blocked."
        ),
        scenario_names=ALWAYS_AVAILABLE_REACH_SLO_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SUITE_NAME,
        label="Always-available reach operations v1",
        description=(
            "Pins Batch DC selected mobile, messaging, native, browser, and web reach campaign receipts for "
            "production pairing, revocation, rate limits, abuse handling, degraded recovery, offline recovery, "
            "continuity, coverage gaps, and safe receipt redaction."
        ),
        benchmark_axis="always_available_reach_operations_v1",
        operator_summary=(
            "Reach proof now exposes a broader selected-channel operational campaign while keeping "
            "OpenClaw-class reach, complete channel coverage, and always-available wording blocked."
        ),
        remaining_gap=(
            "Always-available operation, complete channel coverage, and OpenClaw-class reach remain blocked."
        ),
        scenario_names=ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=VOICE_MEDIA_PARITY_RUNTIME_V1_SUITE_NAME,
        label="Voice/media parity runtime v1",
        description=(
            "Pins Batch DC STT, TTS, voice-command, media-analysis, and media-delivery receipts for latency, "
            "quality, correction, deletion, consent, privacy, fallback, and provider-regression behavior."
        ),
        benchmark_axis="voice_media_parity_runtime_v1",
        operator_summary=(
            "Voice/media proof now exposes broader runtime-provider receipts while keeping voice parity, "
            "multimodal parity, and production STT/TTS solved claims blocked."
        ),
        remaining_gap=(
            "Full voice/media parity and production STT/TTS solved claims remain blocked."
        ),
        scenario_names=VOICE_MEDIA_PARITY_RUNTIME_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MOBILE_CROSS_SURFACE_CONTINUITY_V1_SUITE_NAME,
        label="Mobile cross-surface continuity v1",
        description=(
            "Pins Batch DC continuity receipts proving thread, memory, approval, notification, and operator "
            "handoff survival across offline windows, provider failure, device handoff, channel revocation, "
            "and recovery."
        ),
        benchmark_axis="mobile_cross_surface_continuity_v1",
        operator_summary=(
            "Cross-surface reach proof now exposes mobile/device handoff and recovery continuity receipts "
            "without claiming always-available operation."
        ),
        remaining_gap=(
            "Always-available daily-life reach remains blocked pending stronger live operational evidence."
        ),
        scenario_names=MOBILE_CROSS_SURFACE_CONTINUITY_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SUITE_NAME,
        label="Reach degraded recovery field campaign",
        description=(
            "Pins Batch DC 14-day equivalent field-campaign receipts for channel uptime, handoff success, "
            "degraded recovery, false delivery, missed delivery, operator repair actions, and redacted raw "
            "receipt handles."
        ),
        benchmark_axis="reach_degraded_recovery_field_campaign",
        operator_summary=(
            "Reach proof now exposes field-campaign recovery metrics while keeping always-available and "
            "production-ready claims blocked."
        ),
        remaining_gap=(
            "Always-available operation, production readiness, full parity, and reach superiority remain blocked."
        ),
        scenario_names=REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MANAGED_BROWSER_PROVIDER_ATTESTATION_SUITE_NAME,
        label="Managed browser provider attestation",
        description=(
            "Pins local, managed-remote, and remote-CDP browser provider receipts for provider identity, "
            "evidence mode, session partitioning, credential scope, download/upload boundaries, degradation, "
            "and residual risk."
        ),
        benchmark_axis="managed_browser_provider_attestation",
        operator_summary=(
            "Browser provider proof now names provider identity and trust boundaries for local, managed, "
            "and remote browser modes without claiming safe browser automation."
        ),
        remaining_gap=(
            "Site-specific live automation reliability, provider SLAs, and full browser parity remain future proof work."
        ),
        scenario_names=MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LIVE_MULTI_OPERATOR_USABILITY_STUDY_SUITE_NAME,
        label="Live multi-operator usability study",
        description=(
            "Pins recorded-live multi-operator cockpit tasks for inspect, recover, approval, handoff, audit, "
            "keyboard flow, accessibility, ambiguity handling, action reversibility, and error-rate receipts."
        ),
        benchmark_axis="live_multi_operator_usability_study",
        operator_summary=(
            "Dense operator-control proof now includes multi-operator usability receipts while blocking "
            "best-cockpit and solved-operator-control claims."
        ),
        remaining_gap=(
            "Independent broad usability studies and best-in-category cockpit claims remain future proof work."
        ),
        scenario_names=LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=BROWSER_COMPUTER_USE_RECOVERY_DRILL_SUITE_NAME,
        label="Browser computer-use recovery drill",
        description=(
            "Pins recovery drills for provider crash, page drift, credential partition changes, and "
            "download/upload fail-closed behavior."
        ),
        benchmark_axis="browser_computer_use_recovery_drill",
        operator_summary=(
            "Browser/computer-use recovery now has failure-injection receipts that block external action "
            "until operator-visible recovery gates pass."
        ),
        remaining_gap=(
            "Safe autonomous browser/computer-use and full browser parity remain blocked by claim-ledger policy."
        ),
        scenario_names=BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LIVE_BROWSER_TASK_DEPTH_SUITE_NAME,
        label="Live browser task depth",
        description=(
            "Pins Batch CP safe/test-account browser task depth across navigation, forms, authenticated "
            "session continuity, upload/download, extraction, recovery, handoff, and artifact continuity."
        ),
        benchmark_axis="live_browser_task_depth",
        operator_summary=(
            "Browser/computer-use task proof now declares workload, sample size, provider mode, raw receipt "
            "location, failure budget, residual gap, and artifact continuity."
        ),
        remaining_gap="Blanket safe browser automation and full browser parity remain claim-ledger gated.",
        scenario_names=LIVE_BROWSER_TASK_DEPTH_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=AUTONOMOUS_BROWSER_SAFETY_SUITE_NAME,
        label="Autonomous browser safety controls",
        description=(
            "Pins Batch CP approval scopes, dangerous-action default blocks, stale-reference/page-drift "
            "recovery, artifact continuity, and operator recovery controls for autonomous browser tasks."
        ),
        benchmark_axis="autonomous_browser_safety_controls",
        operator_summary=(
            "Autonomous browser work is bounded by action-level approvals, draft/read-only defaults, dangerous "
            "action blocks, and operator-visible recovery controls."
        ),
        remaining_gap="Safe autonomous computer-use wording remains blocked until the final claim audit permits it.",
        scenario_names=AUTONOMOUS_BROWSER_SAFETY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=BROWSER_SESSION_PARTITIONING_SUITE_NAME,
        label="Browser session partitioning security",
        description=(
            "Pins Batch CP profile, cookie, credential, secret-redaction, replay-scrub, download/upload, "
            "network, and provider partition invariants."
        ),
        benchmark_axis="browser_session_partitioning_security",
        operator_summary=(
            "Browser session proof now exposes partition invariants across local, managed, and remote-CDP paths, "
            "including fail-closed behavior for existing profiles and stale credentials."
        ),
        remaining_gap="This does not prove hardware/container isolation or arbitrary browser credential safety.",
        scenario_names=BROWSER_SESSION_PARTITIONING_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SITE_SPECIFIC_BROWSER_RECOVERY_SUITE_NAME,
        label="Site-specific recovery drills",
        description=(
            "Pins Batch CP site recovery for login expiry, navigation drift, DOM/page drift, file-transfer "
            "failure, provider crash, remote loss, unsafe replay, and stale credentials."
        ),
        benchmark_axis="site_specific_recovery_drills",
        operator_summary=(
            "Browser recovery proof now fails closed across common site-specific failure modes before replay, "
            "resume, upload, download, or credentialed action can continue."
        ),
        remaining_gap="Generalized website compatibility and full browser parity remain blocked.",
        scenario_names=SITE_SPECIFIC_BROWSER_RECOVERY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=BROWSER_PROVIDER_RELIABILITY_MATRIX_SUITE_NAME,
        label="Browser provider reliability matrix",
        description=(
            "Pins Batch CP provider reliability receipts for local, managed-remote, and remote-CDP paths, "
            "including health window, fallback path, degradation behavior, and residual gaps."
        ),
        benchmark_axis="browser_provider_reliability_matrix",
        operator_summary=(
            "Browser provider reliability is now expressed as a bounded matrix with raw receipt locations and "
            "honest degraded-state behavior instead of a broad managed-browser SLA claim."
        ),
        remaining_gap="Provider-wide SLA, production remote browser reliability, and Browserbase-class parity remain blocked.",
        scenario_names=BROWSER_PROVIDER_RELIABILITY_MATRIX_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=INDEPENDENT_BROWSER_USABILITY_REVIEW_SUITE_NAME,
        label="Independent browser usability review",
        description=(
            "Pins Batch CP independent review metrics for task success, operator intervention, error "
            "detectability, accessibility, recovery confidence, and residual risk."
        ),
        benchmark_axis="independent_browser_usability_review",
        operator_summary=(
            "Browser/computer-use usability now carries independent sample metadata, reviewer independence, "
            "raw receipt locations, accessibility checks, and residual-risk receipts."
        ),
        remaining_gap="Broad population usability and best/world-class cockpit wording remain blocked.",
        scenario_names=INDEPENDENT_BROWSER_USABILITY_REVIEW_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=BROWSER_TASK_BREADTH_MATRIX_SUITE_NAME,
        label="Browser task breadth matrix",
        description=(
            "Pins Batch CY production-like safe-target browser task breadth across provider identity, "
            "evidence mode, reliability windows, recovery outcomes, and artifact continuity."
        ),
        benchmark_axis="browser_task_breadth_matrix",
        operator_summary=(
            "Browser/computer-use depth now has a task breadth matrix for local, managed, and partitioned "
            "remote providers while keeping safe automation and full browser parity blocked."
        ),
        remaining_gap=(
            "General website compatibility, safe autonomous computer-use, and full browser parity remain "
            "claim-ledger gated."
        ),
        scenario_names=BROWSER_TASK_BREADTH_MATRIX_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=BROWSER_AUTH_PARTITION_OPERATIONS_SUITE_NAME,
        label="Browser auth partition operations",
        description=(
            "Pins Batch CY profile, cookie, credential, download, upload, filesystem, network, and "
            "dangerous-action partition operations across browser providers."
        ),
        benchmark_axis="browser_auth_partition_operations",
        operator_summary=(
            "Browser auth/session proof now exposes operational partition decisions and fail-closed behavior "
            "instead of only bounded invariant receipts."
        ),
        remaining_gap="Arbitrary credentialed browsing and production-safe autonomous computer-use remain blocked.",
        scenario_names=BROWSER_AUTH_PARTITION_OPERATIONS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SITE_DRIFT_RECOVERY_SLO_SUITE_NAME,
        label="Site drift recovery SLO",
        description=(
            "Pins Batch CY site-drift recovery SLO receipts for login expiry, DOM/navigation drift, provider "
            "degradation, stale replay, file transfer, dangerous submits, and private-network redirects."
        ),
        benchmark_axis="site_drift_recovery_slo",
        operator_summary=(
            "Browser recovery proof now carries SLO/state receipts for drift and provider degradation while "
            "blocking replay or external action until recovery is visible."
        ),
        remaining_gap="Provider-wide SLA and full browser/computer-use parity remain blocked.",
        scenario_names=SITE_DRIFT_RECOVERY_SLO_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SAFE_AUTONOMOUS_BROWSER_RUNTIME_V1_SUITE_NAME,
        label="Safe autonomous browser runtime v1",
        description=(
            "Pins Batch DG safe-target runtime evidence across local, staged managed, partitioned remote-CDP, "
            "and blocked existing-session paths with task success, safe-block, intervention, and leak counters."
        ),
        benchmark_axis="safe_autonomous_browser_runtime_v1",
        operator_summary=(
            "Browser runtime evidence now reports provider execution caveats, dangerous-action blocks, "
            "operator interventions, replay-safe audit ids, and residual risk."
        ),
        remaining_gap=(
            "Safe autonomous browser/computer-use and full browser parity wording remain blocked."
        ),
        scenario_names=SAFE_AUTONOMOUS_BROWSER_RUNTIME_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=FULL_BROWSER_PARITY_MATRIX_V1_SUITE_NAME,
        label="Full browser parity matrix v1",
        description=(
            "Pins Batch DG provider and boundary matrices for auth, cookies, profiles, credentials, downloads, "
            "uploads, filesystem, clipboard, network, and private-data handling."
        ),
        benchmark_axis="full_browser_parity_matrix_v1",
        operator_summary=(
            "Full-browser-parity pressure is represented as a boundary matrix while parity and safe-automation "
            "claims stay blocked by policy."
        ),
        remaining_gap="This matrix is evidence, not a full browser parity or safe automation claim.",
        scenario_names=FULL_BROWSER_PARITY_MATRIX_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=REAL_SITE_DRIFT_RECOVERY_V2_SUITE_NAME,
        label="Safe-target drift recovery v2",
        description=(
            "Pins Batch DG selector, navigation, auth-expiry, provider-failure, rate-limit, anti-bot, and "
            "partial-completion recovery boundaries for deterministic safe-target drift fixtures."
        ),
        benchmark_axis="real_site_drift_recovery_v2",
        operator_summary=(
            "Safe-target drift fixture evidence now fails closed or recovers with operator-visible receipts without "
            "auto-solving anti-bot gates or broadening network scope."
        ),
        remaining_gap="General website compatibility and anti-bot bypass remain blocked.",
        scenario_names=REAL_SITE_DRIFT_RECOVERY_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=BROWSER_SESSION_PARTITION_CERTIFICATION_V1_SUITE_NAME,
        label="Browser session partition certification v1",
        description=(
            "Pins Batch DG fixture certification-scope receipts for profile, cookie, credential, download, "
            "upload, clipboard, private-data, network, and existing-session partition boundaries."
        ),
        benchmark_axis="browser_session_partition_certification_v1",
        operator_summary=(
            "Browser partition certification-scope evidence is visible as fixture review receipts, not formal "
            "certification or proof of arbitrary credentialed browsing safety."
        ),
        remaining_gap="Formal certification and full browser parity remain blocked.",
        scenario_names=BROWSER_SESSION_PARTITION_CERTIFICATION_V1_SCENARIO_NAMES,
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
        name=INDEPENDENT_OUTCOME_COHORT_REVIEW_SUITE_NAME,
        label="Independent outcome cohort review",
        description=(
            "Pins independent evaluator protocol, sample-size rationale, consent, anonymization, harm, correction, "
            "follow-through, and claim-scope receipts for Batch CM outcome evidence."
        ),
        benchmark_axis="independent_outcome_cohort_review",
        operator_summary=(
            "Guardian-learning outcome evidence now declares cohort, evaluator, protocol, workload, sample, "
            "raw receipt, failure budget, and residual gaps before policy claims can move."
        ),
        remaining_gap="Powered generalized superiority and named competitor outcome comparisons remain future proof work.",
        scenario_names=INDEPENDENT_OUTCOME_COHORT_REVIEW_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=TASK_SCOPED_CAUSAL_LEARNING_SUITE_NAME,
        label="Task-scoped causal learning",
        description=(
            "Pins counterfactual, negative-control, task-class, evaluator, time-window, confounder, rollback, and "
            "no-generalized-superiority receipts for Batch CM causal learning."
        ),
        benchmark_axis="task_scoped_causal_learning",
        operator_summary=(
            "Causal-learning receipts remain bounded to measured task classes and carry rollback authority instead of "
            "claiming generalized guardian intelligence improvement."
        ),
        remaining_gap="Generalized causal superiority and production-ready autonomous learning remain blocked.",
        scenario_names=TASK_SCOPED_CAUSAL_LEARNING_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MEMORY_PROVIDER_PARITY_MATRIX_SUITE_NAME,
        label="Memory provider parity matrix",
        description=(
            "Pins dimension-scoped provider comparison across canonical precedence, advisory retrieval/writeback, "
            "delete/export, privacy, freshness, conflict handling, usefulness, quarantine, and reinstatement."
        ),
        benchmark_axis="memory_provider_parity_matrix",
        operator_summary=(
            "Memory-provider parity is now treated as a dimension matrix with canonical authority preserved and "
            "unsafe providers quarantined."
        ),
        remaining_gap="Full memory-provider parity, memory superiority, and provider-market breadth remain blocked.",
        scenario_names=MEMORY_PROVIDER_PARITY_MATRIX_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SUITE_NAME,
        label="Longitudinal guardian outcome study",
        description=(
            "Pins longer-horizon outcome windows, named baselines, power rationale, evaluator protocol, consent, "
            "withdrawal, anonymization, confounders, adverse events, rollback, and residual gaps for Batch CV."
        ),
        benchmark_axis="longitudinal_guardian_outcome_study",
        operator_summary=(
            "Guardian-learning outcome operations now expose longitudinal window and baseline receipts before "
            "candidate learning changes can be promoted."
        ),
        remaining_gap="Generalized live-human-outcome superiority and solved learning remain blocked.",
        scenario_names=LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=NAMED_BASELINE_MEMORY_COMPARISON_SUITE_NAME,
        label="Named baseline memory comparison",
        description=(
            "Pins named baseline source, version, window, limitations, provider quality, canonical precedence, "
            "delete/export propagation, and pressure-evidence-only claim boundaries."
        ),
        benchmark_axis="named_baseline_memory_comparison",
        operator_summary=(
            "Memory-provider baseline comparisons now identify source and limitations while preserving canonical "
            "authority and blocking superiority wording."
        ),
        remaining_gap="Full memory-provider parity, memory superiority, and reference-system comparisons remain blocked.",
        scenario_names=NAMED_BASELINE_MEMORY_COMPARISON_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LEARNING_SAFETY_MONITOR_V2_SUITE_NAME,
        label="Learning safety monitor v2",
        description=(
            "Pins learning-policy version lifecycle, harm and privacy promotion blocks, stale-evidence drift, "
            "rollback, quarantine, reinstatement review, and operator recovery receipts."
        ),
        benchmark_axis="learning_safety_monitor_v2",
        operator_summary=(
            "Learning safety monitors now expose rollback and quarantine state before longitudinal learning changes "
            "can affect behavior."
        ),
        remaining_gap="Autonomous production learning and solved long-term learning remain blocked.",
        scenario_names=LEARNING_SAFETY_MONITOR_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SUITE_NAME,
        label="Generalized guardian outcome study v1",
        description=(
            "Pins predeclared multi-task outcome protocols, independent evaluators, consent, fairness, adverse "
            "events, and bounded claim receipts for Batch DD."
        ),
        benchmark_axis="generalized_guardian_outcome_study_v1",
        operator_summary=(
            "Generalized guardian outcome evidence now exposes broader decision families and adverse-event review "
            "before any stronger learning claim can move."
        ),
        remaining_gap="Guardian outcome superiority, solved learning, and live-human-outcome superiority remain blocked.",
        scenario_names=GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SUITE_NAME,
        label="Full memory provider parity matrix v1",
        description=(
            "Pins expanded provider rows across canonical authority, advisory roles, usefulness, freshness, privacy, "
            "delete/export, stale recall, quarantine, reinstatement, and baseline limitations."
        ),
        benchmark_axis="full_memory_provider_parity_matrix_v1",
        operator_summary=(
            "Memory-provider parity pressure is represented as a richer matrix while full provider parity remains "
            "blocked by failed dimensions and review gates."
        ),
        remaining_gap="Full memory-provider parity, memory superiority, and best-in-class memory claims remain blocked.",
        scenario_names=FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SUITE_NAME,
        label="Causal learning outcome thresholds v1",
        description=(
            "Pins counterfactual or controlled designs, confounders, negative controls, threshold rationale, "
            "promotion review, rollback authority, and no-superiority boundaries."
        ),
        benchmark_axis="causal_learning_outcome_thresholds_v1",
        operator_summary=(
            "Learning threshold evidence now requires causal design notes and rollback authority before promotion."
        ),
        remaining_gap="Generalized causal superiority and autonomous production learning remain blocked.",
        scenario_names=CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MEMORY_BASELINE_COMPARISON_V1_SUITE_NAME,
        label="Memory baseline comparison v1",
        description=(
            "Pins named baseline source, version, source freshness caveat, fairness constraints, limitations, and "
            "pressure-only wording for Batch DD."
        ),
        benchmark_axis="memory_baseline_comparison_v1",
        operator_summary=(
            "Named memory baselines remain current-source-limited pressure evidence rather than baseline wins."
        ),
        remaining_gap="Named baseline wins, full memory-provider parity, and reference-system exceedance remain blocked.",
        scenario_names=MEMORY_BASELINE_COMPARISON_V1_SCENARIO_NAMES,
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
        name=PRODUCTION_SLA_ORCHESTRATION_SUITE_NAME,
        label="Production SLA orchestration",
        description=(
            "Pins stronger Batch CJ orchestration evidence: provider SLA windows, jitter budgets, missed-trigger "
            "counts, failure-injection methods, and operator-visible residual uncertainty."
        ),
        benchmark_axis="production_sla_orchestration",
        operator_summary=(
            "Production SLA orchestration proof is judged by named providers, bounded monitoring windows, jitter "
            "budgets, failure-injection receipts, and recovery controls rather than blanket production readiness."
        ),
        remaining_gap=(
            "This still blocks unconditional exactly-once scheduling, crash-proof orchestration, and full "
            "distributed workflow parity unless later evidence permits exact wording."
        ),
        scenario_names=PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=EXACTLY_ONCE_RECOVERY_EVIDENCE_SUITE_NAME,
        label="Exactly-once recovery evidence",
        description=(
            "Pins scoped effectively-once recovery evidence across idempotency scopes, side-effect boundaries, "
            "resume authority, and duplicate suppression."
        ),
        benchmark_axis="exactly_once_recovery_evidence",
        operator_summary=(
            "Exactly-once recovery evidence must name the scope where duplicate suppression is proven and the "
            "cases that still require operator audit."
        ),
        remaining_gap=(
            "Unconditional exactly-once delivery and crash-proof workflow-engine claims remain blocked."
        ),
        scenario_names=EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=DUPLICATE_SIDE_EFFECT_AUDIT_SUITE_NAME,
        label="Duplicate side-effect audit",
        description=(
            "Pins duplicate side-effect audit receipts for external writes, repository mutations, notifications, "
            "operator controls, and reconciliation outcomes."
        ),
        benchmark_axis="duplicate_side_effect_audit",
        operator_summary=(
            "Duplicate side-effect audit proof must show the first receipt, duplicate attempt, suppression or "
            "reconciliation action, and the operator controls that preserve safety."
        ),
        remaining_gap=(
            "Side-effect audit receipts narrow duplicate risk but do not prove every external system is "
            "crash-proof or globally exactly-once."
        ),
        scenario_names=DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CONTINUOUS_ORCHESTRATION_SLO_SUITE_NAME,
        label="Continuous orchestration SLO monitor",
        description=(
            "Pins Batch CS continuous orchestration evidence: run windows, scheduler/provider health, jitter "
            "budgets, retries, replay windows, recovery actions, and residual uncertainty."
        ),
        benchmark_axis="continuous_orchestration_slo_monitor",
        operator_summary=(
            "Continuous orchestration SLO proof is judged by rolling monitor samples and operator-visible "
            "recovery state rather than one-off crash or SLA receipts."
        ),
        remaining_gap=(
            "Continuous monitor receipts still block unconditional exactly-once, crash-proof workflow-engine, "
            "production-ready, and full parity wording."
        ),
        scenario_names=CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CRASH_FAILOVER_SOAK_SUITE_NAME,
        label="Crash/failover soak v1",
        description=(
            "Pins Batch CS crash/failover soak receipts for named failure events, failover budgets, replay "
            "authority, operator handoff, and residual uncertainty."
        ),
        benchmark_axis="crash_failover_soak_v1",
        operator_summary=(
            "Crash/failover soak proof must show which failure mode was drilled, how replay authority was "
            "bounded, and which recovery action remains operator-controlled."
        ),
        remaining_gap="Named failure drills are not a proof of crash-proof orchestration or distributed consensus.",
        scenario_names=CRASH_FAILOVER_SOAK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SIDE_EFFECT_RECONCILIATION_V2_SUITE_NAME,
        label="Side-effect reconciliation v2",
        description=(
            "Pins Batch CS reconciliation receipts for idempotency keys, duplicate suppression, irreversible "
            "side-effect boundaries, manual recovery state, and operator visibility."
        ),
        benchmark_axis="side_effect_reconciliation_v2",
        operator_summary=(
            "Side-effect reconciliation proof must expose idempotency scope, duplicate handling, irreversible "
            "boundaries, and recovery state before retry or resume is safe."
        ),
        remaining_gap="Reconciliation receipts are scoped to declared boundaries, not global exactly-once delivery.",
        scenario_names=SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_WORKFLOW_STATE_MACHINE_SUITE_NAME,
        label="Production workflow state machine v1",
        description=(
            "Pins Batch DA persisted production workflow authority receipts: scheduler state owner, workflow "
            "lease, worker owner, resumable step state, replay window, recovery authority, and blocked replay."
        ),
        benchmark_axis="production_workflow_state_machine_v1",
        operator_summary=(
            "Production workflow state-machine proof must expose persisted ownership and replay authority "
            "rather than reuse bounded projection receipts alone."
        ),
        remaining_gap=(
            "This narrows production orchestration residuals but still blocks unconditional exactly-once, "
            "crash-proof, and full distributed workflow claims."
        ),
        scenario_names=PRODUCTION_WORKFLOW_STATE_MACHINE_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SUITE_NAME,
        label="Crash-proof orchestration fault campaign",
        description=(
            "Pins Batch DA fault-campaign receipts for scheduler crash, worker crash, duplicate delivery, "
            "provider timeout, stale lease, partial and irreversible side effects, approval-wait restart, "
            "and trust-boundary drift replay blocks."
        ),
        benchmark_axis="crash_proof_orchestration_fault_campaign",
        operator_summary=(
            "The suite name tracks the target claim under test; passing receipts are bounded fault-campaign "
            "evidence, not a granted crash-proof claim."
        ),
        remaining_gap="Fault-campaign coverage is not a proof of universal crash-proof orchestration.",
        scenario_names=CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SUITE_NAME,
        label="External side-effect reconciliation v3",
        description=(
            "Pins Batch DA side-effect reconciliation v3 receipts for idempotency scope, duplicate suppression, "
            "external confirmation, manual repair, safe/unsafe replay decisions, and redacted receipt handles."
        ),
        benchmark_axis="external_side_effect_reconciliation_v3",
        operator_summary=(
            "Side-effect v3 proof must show pre-effect idempotency, post-effect reconciliation, manual repair "
            "authorization, and operator-visible replay decisions."
        ),
        remaining_gap="V3 reconciliation remains scoped to declared side-effect boundaries, not global exactly-once.",
        scenario_names=EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SUITE_NAME,
        label="Production orchestration hard-guarantee evidence v1",
        description=(
            "Pins Batch DI hard-guarantee evidence for durable queues, scheduler leases, worker failover, "
            "replay authority, operator recovery, and bounded claim posture."
        ),
        benchmark_axis="production_orchestration_hard_guarantees_v1",
        operator_summary=(
            "DI hard-guarantee proof must expose which guarantee scopes are backed by receipts and where "
            "operator recovery remains required."
        ),
        remaining_gap=(
            "Hard-guarantee evidence remains bounded and does not prove unconditional exactly-once or "
            "crash-proof orchestration."
        ),
        scenario_names=PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SUITE_NAME,
        label="Distributed workflow recovery operations v1",
        description=(
            "Pins Batch DI recovery receipts for worker handoff, queue replay, delegated artifact review, "
            "manual repair, and operator-visible recovery decisions."
        ),
        benchmark_axis="distributed_workflow_recovery_operations_v1",
        operator_summary=(
            "Recovery proof must show handoff authority, queue replay safety, delegated artifact lineage, "
            "and manual repair state."
        ),
        remaining_gap="Recovery operations do not grant solved durable workflow or production-ready claims.",
        scenario_names=DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SUITE_NAME,
        label="External side-effect correctness v4",
        description=(
            "Pins Batch DI side-effect correctness receipts for idempotency keys, duplicate suppression, "
            "irreversible boundaries, reconciliation records, and manual recovery state."
        ),
        benchmark_axis="external_side_effect_correctness_v4",
        operator_summary=(
            "Side-effect v4 proof must bind each external mutation to a declared idempotency scope, "
            "confirmation state, and redacted receipt."
        ),
        remaining_gap="Side-effect correctness remains scoped to declared boundaries, not global exactly-once delivery.",
        scenario_names=EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SCHEDULER_FAILOVER_SOAK_V1_SUITE_NAME,
        label="Scheduler failover soak v1",
        description=(
            "Pins Batch DI scheduler failover soak receipts for crash windows, failover budgets, replay "
            "authority, provider outage handling, and operator handoff."
        ),
        benchmark_axis="scheduler_failover_soak_v1",
        operator_summary=(
            "Scheduler soak proof must show observed failover within budget and name the operator recovery "
            "path when automation remains unsafe."
        ),
        remaining_gap="Accelerated soak windows are not a proof of universal crash-proof orchestration.",
        scenario_names=SCHEDULER_FAILOVER_SOAK_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
        label="Orchestration false-claim scan v1",
        description=(
            "Pins Batch DI false-claim scan receipts for docs, operator API, release wording, and claim-ledger "
            "surfaces."
        ),
        benchmark_axis="orchestration_false_claim_scan_v1",
        operator_summary=(
            "False-claim proof must show that exactly-once, crash-proof, solved workflow, production-ready, "
            "full parity, and exceedance wording remains blocked."
        ),
        remaining_gap="Clean false-claim scans do not themselves prove stronger orchestration behavior.",
        scenario_names=ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES,
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
        name=INDEPENDENT_SECURE_HOST_REVIEW_SUITE_NAME,
        label="Independent secure-host review",
        description=(
            "Pins Batch CK independent security-review scope, finding remediation, isolation evidence, "
            "operator receipt surfaces, and unsupported isolation-claim boundaries."
        ),
        benchmark_axis="independent_secure_host_review",
        operator_summary=(
            "Independent secure-host review now has an operator-visible proof lane above BW/CD while "
            "secure/private, IronClaw-class, production-ready, and full-parity claims remain blocked."
        ),
        remaining_gap=(
            "This is independent review and attestation proof, not full TEE/CVM/Wasm/container isolation "
            "or production security certification."
        ),
        scenario_names=INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LIVE_HOSTILE_ISOLATION_DRILLS_SUITE_NAME,
        label="Live hostile isolation drills",
        description=(
            "Pins CK hostile replay drills for prompt injection, SSRF/private egress, filesystem escape, "
            "credential exfiltration, extension permission creep, replay approval drift, and browser session bleed."
        ),
        benchmark_axis="live_hostile_isolation_drills",
        operator_summary=(
            "Hostile security drills expose what was blocked, quarantined, and recoverable without "
            "turning negative-case receipts into blanket security claims."
        ),
        remaining_gap="Broader external penetration testing and hardware-backed isolation remain future proof work.",
        scenario_names=LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SECURE_HOST_RECOVERY_AUTHORITY_SUITE_NAME,
        label="Secure-host recovery authority",
        description=(
            "Pins CK operator recovery authority for allow, deny, quarantine, rotate, recover, and "
            "post-incident audit controls after independent review or hostile drill findings."
        ),
        benchmark_axis="secure_host_recovery_authority",
        operator_summary=(
            "Operator recovery authority is visible for independent security review findings and "
            "hostile-drill incidents."
        ),
        remaining_gap="This is recovery authority proof, not solved production incident response.",
        scenario_names=SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CONTAINER_GRADE_CAPABILITY_ISOLATION_SUITE_NAME,
        label="Container-grade capability isolation validation",
        description=(
            "Pins Batch CT capability-class isolation decision records for tool processes, browser automation, "
            "authenticated connectors, extension runtime, workflow replay, signed tool roots, and unsupported "
            "hardware-backed/runtime-isolation boundaries."
        ),
        benchmark_axis="container_grade_capability_isolation",
        operator_summary=(
            "Container-grade secure-host validation makes enforced and unsupported capability isolation "
            "boundaries operator-visible without claiming TEE/CVM/Wasm/container isolation."
        ),
        remaining_gap=(
            "This is decision-record and receipt proof, not certified hardware-backed, TEE/CVM/Wasm, "
            "or generalized container isolation."
        ),
        scenario_names=CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=EXTERNAL_SECURITY_VALIDATION_V1_SUITE_NAME,
        label="External security validation v1",
        description=(
            "Pins Batch CT external security-review scope, finding remediation, residual-risk waiver, "
            "and incident-recovery evidence for secure-host trust boundaries."
        ),
        benchmark_axis="external_security_validation_v1",
        operator_summary=(
            "External security validation receipts show review scope, findings, remediations, waivers, "
            "and recovery authority before stronger security language is allowed."
        ),
        remaining_gap=(
            "This is bounded validation-record proof, not production security certification or solved security."
        ),
        scenario_names=EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SECRET_EGRESS_CERTIFICATION_DRILL_SUITE_NAME,
        label="Secret-egress certification drill",
        description=(
            "Pins Batch CT secret-egress drills for allowlisted hosts, private-network denial, raw-secret "
            "redaction, destination drift, rotation, and operator receipts."
        ),
        benchmark_axis="secret_egress_certification_drill",
        operator_summary=(
            "Secret-egress certification drills prove no raw secret leaks in the covered cases while keeping "
            "secure/private-by-default and production-security claims blocked."
        ),
        remaining_gap=(
            "This is replayable drill proof for covered cases, not blanket secret-safety certification."
        ),
        scenario_names=SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=RUNTIME_ISOLATION_IMPLEMENTATION_SUITE_NAME,
        label="Runtime isolation implementation v1",
        description=(
            "Pins Batch DB covered-path capability boundary receipts across tool processes, "
            "browser automation, authenticated connectors, external MCP, extension runtime, workflow replay, "
            "and the explicit hardware-backed runtime substitute boundary."
        ),
        benchmark_axis="runtime_isolation_implementation_v1",
        operator_summary=(
            "Runtime isolation receipts show covered-path policy hooks and residual hardware "
            "substitute boundaries before any stronger secure-host wording is allowed."
        ),
        remaining_gap=(
            "This is covered-path policy receipt proof, not formal hardware-backed TEE/CVM/Wasm/container "
            "certification."
        ),
        scenario_names=RUNTIME_ISOLATION_IMPLEMENTATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SUITE_NAME,
        label="Credential-broker egress enforcement v1",
        description=(
            "Pins Batch DB field-scoped credential injection, endpoint allowlists, private-network and "
            "redirect denial, rotation/revocation handling, and auditable denial receipts."
        ),
        benchmark_axis="credential_broker_egress_enforcement_v1",
        operator_summary=(
            "Credential-broker enforcement receipts prove covered secret-bearing paths fail closed on "
            "endpoint drift, private-network resolution, raw output, and revoked refs."
        ),
        remaining_gap="This is covered-path enforcement proof, not blanket secret-safety certification.",
        scenario_names=CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=EXTERNAL_SECURITY_CERTIFICATION_SUITE_NAME,
        label="External security certification v1",
        description=(
            "Pins Batch DB external review/certification-scope records, findings, retest evidence, waiver "
            "expiry, artifact digests, and remaining blocked security claims."
        ),
        benchmark_axis="external_security_certification_v1",
        operator_summary=(
            "External security certification receipts expose reviewer identity, tested surfaces, findings, "
            "remediation, waivers, retest evidence, and formal-certification claim boundaries."
        ),
        remaining_gap="This is declared-scope review evidence, not formal security certification.",
        scenario_names=EXTERNAL_SECURITY_CERTIFICATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SUITE_NAME,
        label="Hostile runtime escape gauntlet v1",
        description=(
            "Pins Batch DB hostile runtime escape drills for prompt injection, SSRF/private network, DNS "
            "redirects, workspace escape, cookie theft, package abuse, credential exfiltration, and replay drift."
        ),
        benchmark_axis="hostile_runtime_escape_gauntlet_v1",
        operator_summary=(
            "Hostile runtime escape gauntlet receipts show covered attacks are blocked or quarantined with "
            "operator-visible recovery."
        ),
        remaining_gap="This is covered hostile-case proof, not solved production security.",
        scenario_names=HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SUITE_NAME,
        label="Production-grade secure capability-host evidence v1",
        description=(
            "Pins Batch DJ surface-matrix and receipt-field evidence across shell/process, browser, connector, "
            "MCP, extension/package, workflow replay, delegation, background execution, provider fallback, "
            "filesystem, network, and credential paths."
        ),
        benchmark_axis="production_grade_secure_capability_host_evidence_v1",
        operator_summary=(
            "Production-grade secure-host evidence makes cross-surface boundary fields and blocked security "
            "claims visible without claiming secure/private-by-default execution."
        ),
        remaining_gap=(
            "This is bounded certification-track receipt proof, not IronClaw-class secure execution or formal "
            "security certification."
        ),
        scenario_names=PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SUITE_NAME,
        label="Secure-host cross-surface attack chain v1",
        description=(
            "Pins Batch DJ hostile chains from prompt injection, connector secrets, browser sessions, packages, "
            "MCP endpoints, background processes, and provider fallback into fail-closed receipts."
        ),
        benchmark_axis="secure_host_cross_surface_attack_chain_v1",
        operator_summary=(
            "Cross-surface attack-chain receipts show source surface, destination surface, credential scope, "
            "session owner, network decision, recovery action, and redaction digest."
        ),
        remaining_gap="This is covered-chain proof, not solved production security or safe autonomous computer use.",
        scenario_names=SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CREDENTIAL_BROKER_EGRESS_SOAK_SUITE_NAME,
        label="Credential-broker egress soak v1",
        description=(
            "Pins Batch DJ field/destination-scoped secret injection, endpoint allowlists, DNS/redirect "
            "rechecks, revocation epochs, rotation outcomes, private-network denial, and raw-secret leak blocks."
        ),
        benchmark_axis="credential_broker_egress_soak_v1",
        operator_summary=(
            "Credential-broker egress soak receipts make secret-bearing endpoint decisions and leak-denial "
            "posture operator-visible."
        ),
        remaining_gap="This is bounded fixture-soak proof, not continuous live security certification.",
        scenario_names=CREDENTIAL_BROKER_EGRESS_SOAK_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=RUNTIME_ISOLATION_ATTESTATION_MATRIX_SUITE_NAME,
        label="Runtime isolation attestation matrix v1",
        description=(
            "Pins Batch DJ implemented runtime boundaries separately from unsupported hardware, TEE, CVM, "
            "Wasm, container, and formal-certification claims."
        ),
        benchmark_axis="runtime_isolation_attestation_matrix_v1",
        operator_summary=(
            "Runtime attestation matrix receipts distinguish implemented policy boundaries from substitutes "
            "and unsupported security claims."
        ),
        remaining_gap="This is attestation-matrix proof, not formal certification or hardware-backed isolation.",
        scenario_names=RUNTIME_ISOLATION_ATTESTATION_MATRIX_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SUITE_NAME,
        label="Secure-host operator recovery authority v1",
        description=(
            "Pins Batch DJ deny, quarantine, rotate, rollback, revoke, replay-block, repair, and audit "
            "authority receipts for secure-host incidents."
        ),
        benchmark_axis="secure_host_operator_recovery_authority_v1",
        operator_summary=(
            "Secure-host recovery authority makes operator remediation choices and redacted evidence digests "
            "visible after hostile or degraded paths."
        ),
        remaining_gap="This is operator-authority proof, not solved operator control or tamper-proof audit.",
        scenario_names=SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=SECURE_HOST_FALSE_CLAIM_SCAN_SUITE_NAME,
        label="Secure-host false claim scan v1",
        description=(
            "Pins Batch DJ false-claim scans for secure/private-by-default, IronClaw-class, hardware-backed, "
            "formal-certification, production-ready, full-parity, and superiority wording."
        ),
        benchmark_axis="secure_host_false_claim_scan_v1",
        operator_summary="Secure-host false-claim scans keep stronger security claims blocked.",
        remaining_gap="Static claim scans do not replace independent security review or external certification.",
        scenario_names=SECURE_HOST_FALSE_CLAIM_SCAN_SCENARIO_NAMES,
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
            "Production marketplace security, independent package-security audit, and ecosystem superiority remain future proof work."
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
        name=THIRD_PARTY_MARKETPLACE_ATTESTATION_SUITE_NAME,
        label="Third-party marketplace attestation",
        description=(
            "Pins recorded-live third-party package provenance, signature, publisher verification, "
            "compatibility, dependency, vulnerability, evidence-mode, and operator attestation receipts."
        ),
        benchmark_axis="third_party_marketplace_attestation",
        operator_summary=(
            "Third-party marketplace proof is judged by evidence-backed attestation and fail-closed package trust, "
            "not by package count or unsupported ecosystem claims."
        ),
        remaining_gap=(
            "Production-secure marketplace, solved package security, and ecosystem superiority remain blocked."
        ),
        scenario_names=THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SUITE_NAME,
        label="Marketplace operations and incident drill",
        description=(
            "Pins recorded-live install, update, downgrade, rollback, quarantine, failed-update recovery, "
            "permission-creep, and re-entry incident diagnostics."
        ),
        benchmark_axis="marketplace_operations_incident_drill",
        operator_summary=(
            "Marketplace operations must show review state, diagnostics, rollback, quarantine, and incident "
            "recovery before package changes are treated as trusted."
        ),
        remaining_gap=(
            "External security audit, production marketplace security, and live hostile ecosystem tests remain future proof."
        ),
        scenario_names=MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SUITE_NAME,
        label="Publisher review and package trust",
        description=(
            "Pins publisher identity, key-rotation, review freshness, trust-score explanation, incident history, "
            "and package-count claim blocking."
        ),
        benchmark_axis="publisher_review_and_package_trust",
        operator_summary=(
            "Publisher trust must be explainable, current, and incident-aware before marketplace operations promote packages."
        ),
        remaining_gap=(
            "This is recorded-live trust evidence, not a solved third-party package-security network."
        ),
        scenario_names=PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=INDEPENDENT_PACKAGE_SECURITY_REVIEW_SUITE_NAME,
        label="Independent package security review",
        description=(
            "Pins independent reviewer metadata, package digest, signed digest, key state, publisher identity, "
            "SBOM/dependency graph digest, vulnerability-source freshness, raw receipt locations, and residual exposure."
        ),
        benchmark_axis="independent_package_security_review",
        operator_summary=(
            "Package promotion is judged by independent review receipts and raw evidence paths, not by recorded-live "
            "attestation labels or package count."
        ),
        remaining_gap=(
            "This is bounded package-review proof, not production-secure marketplace or solved third-party package security."
        ),
        scenario_names=INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SUITE_NAME,
        label="Hostile ecosystem package drills",
        description=(
            "Pins unsigned artifact, digest mismatch, dependency confusion, permission creep, compromised key, "
            "unsafe lifecycle hook, suspicious transitive dependency, and compatibility migration fail-closed receipts."
        ),
        benchmark_axis="hostile_ecosystem_package_drills",
        operator_summary=(
            "Hostile package fixtures must block, quarantine, or roll back before runtime contribution, with operator "
            "notification and raw drill receipts."
        ),
        remaining_gap=(
            "This is drill evidence, not proof that all third-party package security risks are solved."
        ),
        scenario_names=HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PACKAGE_NETWORK_INCIDENT_OPERATIONS_SUITE_NAME,
        label="Package-network incident operations",
        description=(
            "Pins package-controlled URL drift, private-network/SSRF denial, redirect and DNS private-resolution denial, "
            "secret-ref injection denial, workspace escape denial, rollback, quarantine, and incident notification."
        ),
        benchmark_axis="package_network_incident_operations",
        operator_summary=(
            "Package-network incidents are judged by endpoint decisions, redirect/address evidence, secret and workspace "
            "boundary decisions, rollback, quarantine, and operator receipts."
        ),
        remaining_gap=(
            "This is package-network drill evidence, not broad production network security certification."
        ),
        scenario_names=PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PUBLISHER_TRUST_VULNERABILITY_HANDLING_SUITE_NAME,
        label="Publisher trust and vulnerability handling",
        description=(
            "Pins publisher identity, key rotation/freshness, revocation and stale review handling, vulnerability source, "
            "database freshness, severity policy, remediation, waiver, notification, and residual exposure receipts."
        ),
        benchmark_axis="publisher_trust_vulnerability_handling",
        operator_summary=(
            "Publisher trust depends on current identity/key/review state and vulnerability-source freshness before "
            "marketplace actions can promote package runtime contribution."
        ),
        remaining_gap=(
            "This does not prove ecosystem superiority, full marketplace parity, or production readiness."
        ),
        scenario_names=PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SUITE_NAME,
        label="Marketplace rollback and quarantine diagnostics",
        description=(
            "Pins install, update, downgrade, rollback, quarantine, re-entry, incident notification, and durable restore "
            "point diagnostics with snapshot IDs and raw receipt locations."
        ),
        benchmark_axis="marketplace_rollback_quarantine_diagnostics",
        operator_summary=(
            "Marketplace lifecycle incidents must remain diagnosable, restorable, quarantinable, and operator-visible."
        ),
        remaining_gap=(
            "This is rollback/quarantine proof, not a production-secure marketplace claim."
        ),
        scenario_names=MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MARKETPLACE_SECURITY_CORPUS_SUITE_NAME,
        label="Marketplace security corpus v1",
        description=(
            "Pins registry corpus receipts for package inventory, provenance, signatures, publisher keys, SBOM and "
            "dependency graph digests, compatibility, review state, lifecycle diagnostics, and redacted safe handles."
        ),
        benchmark_axis="marketplace_security_corpus_v1",
        operator_summary=(
            "CX extends bounded marketplace-security proof into corpus operations without treating package count as "
            "ecosystem superiority or full marketplace parity."
        ),
        remaining_gap=(
            "This is bounded corpus evidence, not production-secure marketplace or solved third-party package security."
        ),
        scenario_names=MARKETPLACE_SECURITY_CORPUS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=CONTINUOUS_VULNERABILITY_MONITORING_SUITE_NAME,
        label="Continuous vulnerability monitoring",
        description=(
            "Pins scanner-source freshness, database freshness, waiver expiry, remediation SLA, critical/high finding "
            "blocks, stale database negatives, and operator-visible monitoring receipts."
        ),
        benchmark_axis="continuous_vulnerability_monitoring",
        operator_summary=(
            "Continuous monitoring keeps source freshness, waiver state, remediation decisions, and deny/quarantine "
            "actions visible before packages can promote."
        ),
        remaining_gap=(
            "This does not prove solved third-party package security or production readiness."
        ),
        scenario_names=CONTINUOUS_VULNERABILITY_MONITORING_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PUBLISHER_TRUST_OPERATIONS_SUITE_NAME,
        label="Publisher trust operations",
        description=(
            "Pins publisher identity, key rotation, review freshness, incident state, quarantine/re-entry decisions, "
            "package-network denial, secret/workspace boundaries, and operator diagnostics."
        ),
        benchmark_axis="publisher_trust_operations",
        operator_summary=(
            "Publisher trust is operated as a corpus-wide safety control with visible holds, denials, and recovery "
            "diagnostics instead of broad marketplace-security claims."
        ),
        remaining_gap=(
            "Publisher trust operations remain bounded receipts, not ecosystem superiority or full marketplace parity."
        ),
        scenario_names=PUBLISHER_TRUST_OPERATIONS_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_SECURE_MARKETPLACE_V1_SUITE_NAME,
        label="Production secure marketplace v1",
        description=(
            "Pins Batch DF bounded production-secure marketplace gate receipts for promotion policy, signed "
            "provenance, SBOM/dependency evidence, scanner freshness, waiver/remediation policy, fail-closed "
            "lifecycle operations, and blocked-claim visibility."
        ),
        benchmark_axis="production_secure_marketplace_v1",
        operator_summary=(
            "Marketplace promotion requires operator-visible evidence gates and fail-closed lifecycle receipts before "
            "runtime contribution."
        ),
        remaining_gap=(
            "This is bounded DF evidence, not a blanket production-secure marketplace or solved package-security claim."
        ),
        scenario_names=PRODUCTION_SECURE_MARKETPLACE_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SUITE_NAME,
        label="Third-party package security certification v1",
        description=(
            "Pins bounded reviewer-scope, finding, remediation, waiver, retest, residual-risk, and claim-boundary "
            "receipts for third-party package-security review."
        ),
        benchmark_axis="third_party_package_security_certification_v1",
        operator_summary=(
            "Package-security certification receipts record review scope and retest evidence while keeping formal "
            "certification and solved-security wording blocked."
        ),
        remaining_gap=(
            "This is reviewer/certification-scope evidence, not formal product certification or solved security."
        ),
        scenario_names=THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SUITE_NAME,
        label="Marketplace live corpus operations v2",
        description=(
            "Pins larger live-corpus v2 receipts for package families, signatures, publisher verification, "
            "SBOM/dependency graph evidence, compatibility, scanner freshness, waiver expiry, remediation SLA, "
            "quarantine, and re-entry state."
        ),
        benchmark_axis="marketplace_live_corpus_operations_v2",
        operator_summary=(
            "The marketplace corpus is judged by quality and security receipts rather than package-count claims."
        ),
        remaining_gap=(
            "Corpus quality evidence does not prove package-count superiority, full marketplace parity, or production readiness."
        ),
        scenario_names=MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SUITE_NAME,
        label="Hostile package lifecycle gauntlet v1",
        description=(
            "Pins private-network, SSRF, redirect/DNS, secret exfiltration, workspace access, dependency-confusion, "
            "lifecycle rollback, quarantine-bypass, and malicious-update drills with fail-closed outcomes."
        ),
        benchmark_axis="hostile_package_lifecycle_gauntlet_v1",
        operator_summary=(
            "Hostile package behavior must deny, quarantine, or roll back before runtime contribution and expose "
            "operator recovery receipts."
        ),
        remaining_gap=(
            "This is hostile lifecycle drill evidence; solved third-party package-security claims remain blocked."
        ),
        scenario_names=HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LONG_WORK_DEBUGGING_RECOVERY_SUITE_NAME,
        label="Long-work debugging recovery",
        description=(
            "Pins step lineage, branch families, artifact comparison, interruption resume, delegated ownership, "
            "cross-batch residual-risk inspection, recovery decisions, and raw operator receipts for dense long work."
        ),
        benchmark_axis="long_work_debugging_recovery",
        operator_summary=(
            "Dense long-work debugging is judged by how quickly an operator can answer what failed, which branch "
            "or artifact is trustworthy, and which recovery path preserves approval and audit context."
        ),
        remaining_gap=(
            "This is bounded debugging/recovery evidence, not a best-cockpit, solved-control, or production-ready claim."
        ),
        scenario_names=LONG_WORK_DEBUGGING_RECOVERY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=OPERATOR_CONTROL_DENSITY_SUITE_NAME,
        label="Operator control density",
        description=(
            "Pins pause, resume, retry, repair, branch, compare, revoke, quarantine, handoff, rollback, and audit "
            "controls with authority boundaries and receipts after action."
        ),
        benchmark_axis="operator_control_density",
        operator_summary=(
            "Control density means every high-risk recovery action names its target, review boundary, correctness "
            "check, and audit receipt before an operator acts."
        ),
        remaining_gap=(
            "This does not prove solved operator control or unrestricted automatic recovery."
        ),
        scenario_names=OPERATOR_CONTROL_DENSITY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SUITE_NAME,
        label="Independent operator usability accessibility",
        description=(
            "Pins independent operator-study, keyboard-only, accessibility-blocker, error-detectability, recovery "
            "success, and multi-operator handoff receipts for long-work recovery."
        ),
        benchmark_axis="independent_operator_usability_accessibility",
        operator_summary=(
            "Usability evidence is task-relative and receipt-backed; broad population, certification, fastest-cockpit, "
            "or world-class claims remain blocked."
        ),
        remaining_gap=(
            "Broad independent usability evidence and best/world-class cockpit claims remain outside this proof gate."
        ),
        scenario_names=INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=OPERATOR_CONTROL_POPULATION_STUDY_SUITE_NAME,
        label="Operator control population study",
        description=(
            "Pins broader operator population receipts for long-work diagnosis, branch comparison, handoff resume, "
            "keyboard accessibility, recovery success, evaluator independence, and redacted raw receipt handles."
        ),
        benchmark_axis="operator_control_population_study",
        operator_summary=(
            "CW extends dense recovery proof into population-level mission-control evidence without claiming best "
            "cockpit, solved operator control, production readiness, or reference-system exceedance."
        ),
        remaining_gap=(
            "Population fixtures remain bounded and do not prove world-class cockpit quality or universal usability."
        ),
        scenario_names=OPERATOR_CONTROL_POPULATION_STUDY_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=NAMED_BASELINE_COCKPIT_COMPARISON_SUITE_NAME,
        label="Named baseline cockpit comparison",
        description=(
            "Pins named Hermes, OpenClaw, and IronClaw cockpit-pressure rows with source-refresh requirements, "
            "task scope, limitations, behavior-change boundaries, and no winner or superiority wording."
        ),
        benchmark_axis="named_baseline_cockpit_comparison",
        operator_summary=(
            "Named baselines are used as pressure to keep Seraph's mission-control surface honest while stronger "
            "competitor and superiority claims stay final-audit gated."
        ),
        remaining_gap=(
            "Current-source competitor refresh and any claim lift remain owned by the final parity audit."
        ),
        scenario_names=NAMED_BASELINE_COCKPIT_COMPARISON_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LONG_WORK_DEBUGGING_SLO_SUITE_NAME,
        label="Long-work debugging SLO",
        description=(
            "Pins p95 mission-control SLOs for searchable timelines, log/diff replay, runbook repair, handoff resume, "
            "and residual-risk drill-down with read-only replay and authority-boundary policies."
        ),
        benchmark_axis="long_work_debugging_slo",
        operator_summary=(
            "Long-work debugging SLOs are judged by bounded task receipts, redacted handles, receiver acceptance, "
            "and approval-context preservation."
        ),
        remaining_gap=(
            "These are fixture SLOs, not fastest-cockpit, production-ready, or solved-control evidence."
        ),
        scenario_names=LONG_WORK_DEBUGGING_SLO_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=OPERATOR_CONTROL_CERTIFICATION_V1_SUITE_NAME,
        label="Operator control certification v1",
        description=(
            "Pins bounded certification-style coverage for inspect, approve, deny, pause, resume, retry, repair, "
            "branch, compare, revoke, quarantine, handoff, rollback, audit, search, replay, and runbook controls "
            "with authority, stale-approval, integrity, and claim-boundary receipts."
        ),
        benchmark_axis="operator_control_certification_v1",
        operator_summary=(
            "Batch DE treats certification as bounded operator-control evidence: coverage, authority, receipt, "
            "negative-case, and redaction proof without formal-certification or solved-control wording."
        ),
        remaining_gap=(
            "This is not formal certification, best/world-class cockpit proof, solved operator control, "
            "tamper-proof audit, production readiness, full parity, or reference-system exceedance."
        ),
        scenario_names=OPERATOR_CONTROL_CERTIFICATION_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=MISSION_CONTROL_POPULATION_STUDY_V2_SUITE_NAME,
        label="Mission control population study v2",
        description=(
            "Pins broader task-relative telemetry for operator clicks, keystrokes, latency, recovery, effort, "
            "accessibility, keyboard-only success, reviewer independence, digest visibility, and pressure-only baselines."
        ),
        benchmark_axis="mission_control_population_study_v2",
        operator_summary=(
            "Population v2 receipts make operator-control evidence measurable while keeping named baselines pressure-only."
        ),
        remaining_gap=(
            "Population fixtures remain bounded and do not prove fastest cockpit, named baseline wins, or universal usability."
        ),
        scenario_names=MISSION_CONTROL_POPULATION_STUDY_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=LONG_WORK_RECOVERY_SLO_V2_SUITE_NAME,
        label="Long-work recovery SLO v2",
        description=(
            "Pins multi-session, delegated-artifact, background-process, branch-family, source-evidence, runbook replay, "
            "and residual-risk drill-down SLO receipts with approval-context preservation."
        ),
        benchmark_axis="long_work_recovery_slo_v2",
        operator_summary=(
            "Long-work recovery v2 checks that recovery stays operator-controlled, source-backed, and read-only until "
            "approval, trust, checkpoint, and side-effect boundaries match."
        ),
        remaining_gap=(
            "These are bounded recovery SLO receipts, not guaranteed rollback, crash-proof operation, or solved control."
        ),
        scenario_names=LONG_WORK_RECOVERY_SLO_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=OPERATOR_ERROR_DETECTABILITY_V1_SUITE_NAME,
        label="Operator error detectability v1",
        description=(
            "Pins error detectability, confidence calibration, safe denial, stale approval blocking, replay denial, "
            "and recovery-correctness receipts for representative long-work operator failures."
        ),
        benchmark_axis="operator_error_detectability_v1",
        operator_summary=(
            "Operator error detectability means unsafe recovery is visible and deniable before mutation, with calibrated "
            "confidence and explicit stale-approval negative cases."
        ),
        remaining_gap=(
            "This does not prove solved operator control, tamper-proof audit, production readiness, or full parity."
        ),
        scenario_names=OPERATOR_ERROR_DETECTABILITY_V1_SCENARIO_NAMES,
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
    BenchmarkSuiteDefinition(
        name=FINAL_SOURCE_BACKED_PARITY_AUDIT_SUITE_NAME,
        label="Final source-backed parity audit",
        description=(
            "Pins current Hermes, OpenClaw, and IronClaw source receipts, pressure-axis mapping, "
            "batch completion evidence, residual gaps, and source-date freshness."
        ),
        benchmark_axis="final_source_backed_parity_audit",
        operator_summary=(
            "Final parity audit claims must be anchored to current official/source-backed URLs and explicit "
            "residual-risk boundaries before any wording strengthens."
        ),
        remaining_gap=(
            "The audit remains a claim gate and does not itself prove full parity, production readiness, or superiority."
        ),
        scenario_names=FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=FINAL_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME,
        label="Final claim-ledger reconciliation",
        description=(
            "Pins forbidden-claim blocking, allowed wording scope, issue links, operator surfaces, "
            "and independent Critic/Contrarian disposition requirements."
        ),
        benchmark_axis="final_claim_ledger_reconciliation",
        operator_summary=(
            "Final wording is governed by exact claim-ledger rows and blocked-claim receipts, not by roadmap completion."
        ),
        remaining_gap=(
            "Full parity, superiority, production-ready, secure/private, and reference-system-exceeded wording remain blocked."
        ),
        scenario_names=FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=OPERATOR_FINAL_PARITY_READINESS_REPORT_SUITE_NAME,
        label="Operator final parity readiness report",
        description=(
            "Pins the final operator-visible report for board reconciliation, aggregate benchmark visibility, "
            "residual risks, and no-false-completion receipts."
        ),
        benchmark_axis="operator_final_parity_readiness_report",
        operator_summary=(
            "Operators can inspect source receipts, board state, claim boundaries, residual gaps, and critic disposition "
            "from one final parity-readiness report."
        ),
        remaining_gap=(
            "Operators still need stronger external operational evidence before public full-parity or superiority claims."
        ),
        scenario_names=OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=POST_CQ_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME,
        label="Post-CQ claim-ledger reconciliation",
        description=(
            "Pins post-CQ issue, PR, Project, operator, and claim-ledger reconciliation for Batches CR-CZ "
            "without mutating the historical CQ final-audit contract."
        ),
        benchmark_axis="post_cq_claim_ledger_reconciliation",
        operator_summary=(
            "Operators can inspect the final post-CQ claim-readiness gate while broad parity, production-ready, "
            "security, browser, marketplace, and superiority claims remain blocked."
        ),
        remaining_gap=(
            "The gate permits only exact bounded receipt wording unless a future ledger row allows stronger language."
        ),
        scenario_names=POST_CQ_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SUITE_NAME,
        label="Reference-system source refresh v2",
        description=(
            "Pins 2026-06-11 Hermes, OpenClaw, and IronClaw source receipts, pressure-axis mapping, "
            "access caveats, and stale-source uncertainty for the post-CQ audit."
        ),
        benchmark_axis="reference_system_source_refresh_v2",
        operator_summary=(
            "Competitor sources remain pressure evidence only and cannot imply Seraph parity or superiority."
        ),
        remaining_gap=(
            "Current source refresh does not replace implementation, live operational evidence, or claim-ledger permission."
        ),
        scenario_names=REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=FALSE_COMPLETION_SCAN_V2_SUITE_NAME,
        label="False completion scan v2",
        description=(
            "Pins repo, issue, PR, operator, and claim-ledger false-completion scan receipts for post-CQ wording."
        ),
        benchmark_axis="false_completion_scan_v2",
        operator_summary=(
            "False-completion scans keep exact bounded receipt wording separate from full parity, production-ready, "
            "security, and superiority claims."
        ),
        remaining_gap=(
            "Any stronger public wording still requires exact claim-ledger permission and fresh proof."
        ),
        scenario_names=FALSE_COMPLETION_SCAN_V2_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=PRODUCTION_READINESS_SOAK_V1_SUITE_NAME,
        label="Production readiness soak-readiness reconciliation v1",
        description=(
            "Pins the final DA-DG production-readiness soak-readiness reconciliation receipts across runtime, "
            "security, reach, learning, operator control, marketplace, and browser/computer-use evidence."
        ),
        benchmark_axis="production_readiness_soak_v1",
        operator_summary=(
            "The final reconciliation gives operators a cross-area evidence map while product-wide "
            "production-ready wording remains blocked."
        ),
        remaining_gap=(
            "Bounded soak-readiness receipts do not prove a live soak, product-wide production readiness, "
            "or reference-system exceedance."
        ),
        scenario_names=PRODUCTION_READINESS_SOAK_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=FINAL_FULL_PARITY_CLAIM_LIFT_V1_SUITE_NAME,
        label="Final full parity claim-lift v1",
        description=(
            "Reconciles SCL-043 through SCL-050, DA-DG merged evidence, and DH bounded wording without "
            "lifting broad full-parity claims."
        ),
        benchmark_axis="final_full_parity_claim_lift_v1",
        operator_summary=(
            "Claim-lift receipts permit only exact bounded DH wording and keep broad parity, superiority, "
            "security, reach, browser, marketplace, and memory claims blocked."
        ),
        remaining_gap="Any broader public wording still requires explicit claim-ledger permission.",
        scenario_names=FINAL_FULL_PARITY_CLAIM_LIFT_V1_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SUITE_NAME,
        label="Reference-system source refresh v3",
        description=(
            "Pins June 11, 2026 manual Hermes, OpenClaw, and IronClaw source-review receipts for the final "
            "DH gate with pressure-only claim use and access caveats."
        ),
        benchmark_axis="reference_system_source_refresh_v3",
        operator_summary=(
            "Current competitor sources remain pressure evidence only and cannot imply Seraph parity or superiority."
        ),
        remaining_gap="Source refresh is not a substitute for implementation, tests, board state, or claim-ledger permission.",
        scenario_names=REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=FALSE_COMPLETION_SCAN_V3_SUITE_NAME,
        label="False completion scan v3",
        description=(
            "Extends the false-completion scan through Batch DH, including docs/code/operator scans, GitHub "
            "tracking receipts, and stale PR #548 closure."
        ),
        benchmark_axis="false_completion_scan_v3",
        operator_summary=(
            "False-completion v3 keeps DH bounded wording separate from full parity, production-ready, "
            "security, and superiority claims."
        ),
        remaining_gap="External PR and issue wording must still be reviewed before merge.",
        scenario_names=FALSE_COMPLETION_SCAN_V3_SCENARIO_NAMES,
    ),
    BenchmarkSuiteDefinition(
        name=BOARD_PR_ISSUE_RECONCILIATION_V3_SUITE_NAME,
        label="Board PR issue reconciliation v3",
        description=(
            "Pins #475, #540-#547, #548, and PR #555 board/PR/issue state for the final production-parity gate."
        ),
        benchmark_axis="board_pr_issue_reconciliation_v3",
        operator_summary=(
            "Operators can inspect DA-DG Done/Merged/Passed receipts, DH active branch state, and stale PR closure."
        ),
        remaining_gap="Final DH PR fields must be updated when the aggregate PR opens and merges.",
        scenario_names=BOARD_PR_ISSUE_RECONCILIATION_V3_SCENARIO_NAMES,
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
