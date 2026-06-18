from __future__ import annotations

from typing import Any


TRUST_BOUNDARY_BENCHMARK_SUITE_NAME = "trust_boundary_and_safety_receipts"
TRUST_BOUNDARY_BENCHMARK_SCENARIO_NAMES = (
    "secret_ref_egress_boundary_behavior",
    "tool_policy_guardrails_behavior",
    "delegation_secret_boundary_behavior",
    "process_recovery_boundary_behavior",
    "background_session_handoff_behavior",
    "workflow_boundary_blocked_surface_behavior",
    "source_mutation_boundary_behavior",
)


def trust_boundary_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "secret_egress_controls",
            "label": "Secret egress controls",
            "summary": "Secret-bearing tool calls should stay field-scoped, host-allowlisted, and fail closed before credentials cross a connector boundary.",
        },
        {
            "name": "delegation_and_background_partitioning",
            "label": "Delegation and background partitioning",
            "summary": "Delegated specialists, managed processes, and background handoff should preserve explicit trust partitions instead of flattening privileged state into generic runtime continuity.",
        },
        {
            "name": "workflow_boundary_drift",
            "label": "Workflow boundary drift",
            "summary": "Replay and resume should fail closed when the workflow trust boundary changes after the original run.",
        },
        {
            "name": "operator_safety_receipts",
            "label": "Operator safety receipts",
            "summary": "Operators should be able to inspect trust posture, receipt surfaces, and safety failure taxonomy directly instead of relying on descriptive docs alone.",
        },
        {
            "name": "ci_regression_gating",
            "label": "CI regression gating",
            "summary": "Trust-boundary regressions should live in a named deterministic benchmark suite that can gate future changes.",
        },
    ]


def trust_boundary_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "secret_egress_leak",
            "severity": "high",
            "summary": "A secret-bearing call can cross an MCP or connector boundary without an explicit field allowlist and credential-egress allowlist.",
        },
        {
            "name": "delegation_partition_collapse",
            "severity": "high",
            "summary": "Delegated or background work collapses a narrower trust partition back into a generic runtime surface.",
        },
        {
            "name": "background_scope_leak",
            "severity": "high",
            "summary": "Managed process recovery or background-session continuity exposes state outside the owning session partition.",
        },
        {
            "name": "workflow_boundary_drift_regression",
            "severity": "medium",
            "summary": "Workflow replay or resume continues after a trust-boundary change instead of failing closed with an operator-visible receipt.",
        },
        {
            "name": "hidden_safety_receipt",
            "severity": "medium",
            "summary": "Safety posture, trust receipts, or trust-failure taxonomy are not visible through operator benchmark surfaces.",
        },
        {
            "name": "ungated_trust_regression",
            "severity": "medium",
            "summary": "Trust-boundary regressions are no longer pinned by a named deterministic suite.",
        },
    ]


def trust_boundary_benchmark_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": TRUST_BOUNDARY_BENCHMARK_SUITE_NAME,
        "secret_egress_policy": "field_scoped_secret_refs_plus_required_credential_egress_allowlist",
        "delegation_partition_policy": "vault_operations_route_to_vault_keeper",
        "background_execution_policy": "session_partitioned_managed_process_recovery",
        "workflow_replay_policy": "trust_boundary_drift_blocks_replay_and_resume",
        "operator_visibility": "benchmark_proof_plus_runtime_receipts_visible",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/trust-boundary-benchmark",
            "/api/operator/workflow-orchestration",
            "/api/operator/background-sessions",
            "/api/activity/ledger",
        ],
        "ci_gate_mode": "required_benchmark_suite",
    }


def _trust_boundary_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Trust-boundary benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:8]


async def _run_trust_boundary_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([TRUST_BOUNDARY_BENCHMARK_SUITE_NAME])


async def build_trust_boundary_benchmark_report() -> dict[str, Any]:
    summary = await _run_trust_boundary_benchmark_suite()
    failure_report = _trust_boundary_failure_report(summary)
    healthy = summary.failed == 0
    benchmark_posture = (
        "ci_gated_operator_visible"
        if healthy
        else "ci_regressions_detected_operator_visible"
    )
    return {
        "summary": {
            "suite_name": TRUST_BOUNDARY_BENCHMARK_SUITE_NAME,
            "benchmark_posture": benchmark_posture,
            "operator_status": "safety_receipts_visible",
            "scenario_count": len(TRUST_BOUNDARY_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(trust_boundary_benchmark_dimensions()),
            "failure_mode_count": len(trust_boundary_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "secret_egress_state": (
                "field_scoped_egress_allowlist_required"
                if healthy
                else "regressions_detected"
            ),
            "delegation_partition_state": (
                "vault_and_background_partitioned"
                if healthy
                else "regressions_detected"
            ),
            "workflow_replay_state": (
                "boundary_drift_blocks_replay"
                if healthy
                else "regressions_detected"
            ),
            "operator_receipt_state": "benchmark_and_runtime_visible",
        },
        "scenario_names": list(TRUST_BOUNDARY_BENCHMARK_SCENARIO_NAMES),
        "dimensions": trust_boundary_benchmark_dimensions(),
        "failure_taxonomy": trust_boundary_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": trust_boundary_benchmark_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
