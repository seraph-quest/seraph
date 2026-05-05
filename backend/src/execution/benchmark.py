from __future__ import annotations

from typing import Any


M2_EXECUTION_BENCHMARK_SUITE_NAME = "m2_execution_supremacy"
M2_EXECUTION_BENCHMARK_SCENARIO_NAMES = (
    "execution_artifact_registry_behavior",
    "execution_security_gauntlet_behavior",
    "filesystem_patch_receipt_behavior",
    "process_recovery_boundary_behavior",
    "shell_tool_timeout_contract",
    "shell_tool_runtime_audit",
    "browser_execution_task_replay_behavior",
    "browser_runtime_audit",
    "workflow_boundary_blocked_surface_behavior",
    "operator_trust_boundary_benchmark_surface_behavior",
    "operator_computer_use_benchmark_surface_behavior",
)


def m2_execution_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "terminal_process_sandbox",
            "label": "Terminal, process, and sandbox execution",
            "summary": "Foreground commands, background processes, and sandbox failures must expose bounded execution, recovery state, and operator receipts.",
        },
        {
            "name": "browser_http_computer_use",
            "label": "Browser, HTTP, and computer-use receipts",
            "summary": "Browser and HTTP-like external reads must preserve DNS/private-address controls, action receipts, and blocked-navigation evidence.",
        },
        {
            "name": "filesystem_patch_artifacts",
            "label": "Filesystem patches and artifacts",
            "summary": "Workspace writes and patch applications must produce stable artifact IDs, hashes, lineage, trust boundaries, and rollback hints.",
        },
        {
            "name": "adversarial_security_gauntlet",
            "label": "Adversarial execution gauntlet",
            "summary": "Secret egress, shell injection, private-network access, permission creep, replay drift, and delegated/background privilege drift are pinned together.",
        },
        {
            "name": "operator_readiness",
            "label": "Operator readiness proof",
            "summary": "M2 completion stays degraded unless every required execution and security family is CI-gated and operator-visible.",
        },
    ]


def m2_execution_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "artifact_lineage_gap",
            "severity": "high",
            "summary": "Execution output is visible only as a raw path or string, without a stable artifact ID, hash, producer, boundary, and recovery hint.",
        },
        {
            "name": "command_injection_gap",
            "severity": "high",
            "summary": "Terminal or process execution accepts shell metacharacters, newlines, subshell payloads, or inline interpreter escapes.",
        },
        {
            "name": "private_network_gap",
            "severity": "high",
            "summary": "Browser or HTTP execution can reach loopback, private IPs, DNS-resolved private hosts, or redirect/subrequest targets without a blocked receipt.",
        },
        {
            "name": "secret_or_permission_creep",
            "severity": "high",
            "summary": "Secret-bearing fields or extension capabilities can expand across external connectors without explicit manifests, allowlists, and lifecycle boundaries.",
        },
        {
            "name": "replay_or_delegation_drift",
            "severity": "medium",
            "summary": "Workflow replay, delegated work, or background sessions continue after a changed trust boundary instead of failing closed.",
        },
        {
            "name": "hidden_operator_receipt",
            "severity": "medium",
            "summary": "Execution or security readiness is not visible through benchmark-proof, dedicated benchmark, workflow, activity, and cockpit surfaces.",
        },
    ]


def m2_execution_benchmark_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": M2_EXECUTION_BENCHMARK_SUITE_NAME,
        "milestone_contract": "one_milestone_one_ready_pr",
        "completion_policy": "all_execution_families_and_435_security_gauntlet_must_pass",
        "artifact_policy": "stable_artifact_id_hash_producer_boundary_and_recovery_hint_required",
        "terminal_process_policy": "single_executable_token_no_shell_meta_with_session_scoped_recovery",
        "browser_http_policy": "initial_final_dns_redirect_and_subrequest_private_targets_blocked",
        "security_gauntlet_policy": "secret_egress_shell_injection_private_network_permission_creep_replay_and_delegation_drift_pinned",
        "operator_visibility": "benchmark_proof_plus_m2_execution_surface_plus_runtime_receipts_visible",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/m2-execution-benchmark",
            "/api/operator/trust-boundary-benchmark",
            "/api/operator/computer-use-benchmark",
            "/api/operator/workflow-orchestration",
            "/api/activity/ledger",
        ],
        "ci_gate_mode": "required_benchmark_suite",
    }


def _m2_execution_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "M2 execution benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:10]


async def _run_m2_execution_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([M2_EXECUTION_BENCHMARK_SUITE_NAME])


async def build_m2_execution_benchmark_report() -> dict[str, Any]:
    summary = await _run_m2_execution_benchmark_suite()
    failure_report = _m2_execution_failure_report(summary)
    healthy = summary.failed == 0
    degraded_state = "regressions_detected"
    return {
        "summary": {
            "suite_name": M2_EXECUTION_BENCHMARK_SUITE_NAME,
            "benchmark_posture": (
                "m2_completion_ci_gated_operator_visible"
                if healthy
                else "m2_completion_regressions_detected_operator_visible"
            ),
            "operator_status": "m2_execution_readiness_visible",
            "scenario_count": len(M2_EXECUTION_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(m2_execution_benchmark_dimensions()),
            "failure_mode_count": len(m2_execution_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "terminal_process_state": "bounded_with_recovery_receipts" if healthy else degraded_state,
            "browser_http_state": "dns_redirect_and_subrequest_guarded" if healthy else degraded_state,
            "artifact_registry_state": "stable_ids_hashes_boundaries_and_recovery_hints_visible" if healthy else degraded_state,
            "security_gauntlet_state": "m2_435_threats_pinned" if healthy else degraded_state,
            "milestone_completion_state": "ready_to_close_m2" if healthy else "blocked_until_regressions_fixed",
        },
        "scenario_names": list(M2_EXECUTION_BENCHMARK_SCENARIO_NAMES),
        "dimensions": m2_execution_benchmark_dimensions(),
        "failure_taxonomy": m2_execution_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": m2_execution_benchmark_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }

