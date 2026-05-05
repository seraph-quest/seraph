"""Shared benchmark-suite catalog for deterministic proof surfaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from src.browser.benchmark import COMPUTER_USE_BENCHMARK_SCENARIO_NAMES, COMPUTER_USE_BENCHMARK_SUITE_NAME
from src.execution.benchmark import M2_EXECUTION_BENCHMARK_SCENARIO_NAMES, M2_EXECUTION_BENCHMARK_SUITE_NAME
from src.evolution.benchmark import (
    GOVERNED_IMPROVEMENT_BENCHMARK_SCENARIO_NAMES,
    GOVERNED_IMPROVEMENT_BENCHMARK_SUITE_NAME,
)
from src.guardian.benchmark import (
    GUARDIAN_USER_MODEL_BENCHMARK_SCENARIO_NAMES,
    GUARDIAN_USER_MODEL_BENCHMARK_SUITE_NAME,
)
from src.memory.benchmark import GUARDIAN_MEMORY_BENCHMARK_SCENARIO_NAMES, GUARDIAN_MEMORY_BENCHMARK_SUITE_NAME
from src.security.benchmark import TRUST_BOUNDARY_BENCHMARK_SCENARIO_NAMES, TRUST_BOUNDARY_BENCHMARK_SUITE_NAME
from src.security.secure_host_benchmark import (
    SECURE_CAPABILITY_HOST_BENCHMARK_SCENARIO_NAMES,
    SECURE_CAPABILITY_HOST_BENCHMARK_SUITE_NAME,
)
from src.workflows.benchmark import (
    M5_OPERATING_LAYER_BENCHMARK_SCENARIO_NAMES,
    M5_OPERATING_LAYER_BENCHMARK_SUITE_NAME,
    WORKFLOW_ENDURANCE_BENCHMARK_SCENARIO_NAMES,
    WORKFLOW_ENDURANCE_BENCHMARK_SUITE_NAME,
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
