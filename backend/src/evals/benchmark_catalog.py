"""Shared benchmark-suite catalog for deterministic proof surfaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from src.guardian.benchmark import (
    GUARDIAN_USER_MODEL_BENCHMARK_SCENARIO_NAMES,
    GUARDIAN_USER_MODEL_BENCHMARK_SUITE_NAME,
)
from src.memory.benchmark import GUARDIAN_MEMORY_BENCHMARK_SCENARIO_NAMES, GUARDIAN_MEMORY_BENCHMARK_SUITE_NAME
from src.security.benchmark import TRUST_BOUNDARY_BENCHMARK_SCENARIO_NAMES, TRUST_BOUNDARY_BENCHMARK_SUITE_NAME
from src.workflows.benchmark import (
    WORKFLOW_ENDURANCE_BENCHMARK_SCENARIO_NAMES,
    WORKFLOW_ENDURANCE_BENCHMARK_SUITE_NAME,
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
        name="computer_use_browser_desktop",
        label="Computer-use, browser, and desktop execution",
        description=(
            "Groups the current browser, native desktop, and cross-surface execution seams into one benchmark-facing proof lane."
        ),
        benchmark_axis="computer_use_execution",
        operator_summary="Browser and desktop continuity paths stay visible, recoverable, and auditable instead of collapsing into one opaque transport lane.",
        remaining_gap="A fuller real-world browser or desktop task harness still remains for future work.",
        scenario_names=(
            "browser_runtime_audit",
            "native_presence_notification_behavior",
            "native_desktop_shell_behavior",
            "cross_surface_notification_controls_behavior",
            "cross_surface_continuity_behavior",
            "workflow_boundary_blocked_surface_behavior",
        ),
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
        name="governed_improvement",
        label="Governed improvement gates",
        description=(
            "Verifies that self-evolution stays bounded, review-oriented, and tied to explicit proof receipts."
        ),
        benchmark_axis="governed_self_improvement",
        operator_summary="Self-evolution remains eval-gated and review-first instead of mutating capability assets without receipts.",
        remaining_gap="The rollout is still human-reviewed and should stay that way until stronger live regression evidence exists.",
        scenario_names=(
            "governed_self_evolution_behavior",
            "capability_repair_behavior",
            "capability_preflight_behavior",
        ),
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
