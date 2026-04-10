"""Shared benchmark-suite catalog for deterministic proof surfaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


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
