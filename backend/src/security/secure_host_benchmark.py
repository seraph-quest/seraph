from __future__ import annotations

from typing import Any


SECURE_CAPABILITY_HOST_BENCHMARK_SUITE_NAME = "secure_capability_host"
SECURE_CAPABILITY_HOST_BENCHMARK_SCENARIO_NAMES = (
    "secure_host_secret_ref_fail_closed_behavior",
    "secure_host_workspace_secret_path_boundary_behavior",
    "secure_host_process_env_isolation_behavior",
    "secure_host_prompt_injection_quarantine_behavior",
    "secure_host_delegation_partition_behavior",
    "secure_host_provider_fallback_boundary_behavior",
    "operator_secure_capability_host_benchmark_surface_behavior",
)


def secure_capability_host_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "credential_egress",
            "label": "Credential egress",
            "summary": "Secret references must be session-bound, unexpired, field-scoped, and destination-host allowlisted before resolution.",
        },
        {
            "name": "workspace_secret_files",
            "label": "Workspace secret-file boundary",
            "summary": "Secret-like workspace files are not generic read or patch artifacts; they require vault or secret-management paths.",
        },
        {
            "name": "process_isolation",
            "label": "Process environment isolation",
            "summary": "Foreground and background processes receive an allowlisted environment instead of ambient host credentials.",
        },
        {
            "name": "prompt_surface_quarantine",
            "label": "Prompt-surface quarantine",
            "summary": "Prompt-bearing capability content, tool output, and mutation instructions produce blocked receipts when suspicious content appears.",
        },
        {
            "name": "delegation_and_provider_trust",
            "label": "Delegation and provider trust",
            "summary": "Delegated specialists and provider fallback decisions preserve trust partitions and block explicit trust-class violations.",
        },
    ]


def secure_capability_host_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "secret_ref_replay",
            "severity": "high",
            "summary": "An expired, cross-session, or destination-mismatched secret reference resolves instead of failing closed.",
        },
        {
            "name": "ambient_process_secret_leak",
            "severity": "high",
            "summary": "Host API keys, tokens, or cloud credentials enter foreground or background process environments.",
        },
        {
            "name": "workspace_secret_file_read",
            "severity": "high",
            "summary": "Generic workspace tools can read or patch .env, key, credential, token, or private-key files.",
        },
        {
            "name": "prompt_injection_unquarantined",
            "severity": "medium",
            "summary": "Prompt-bearing content with instruction override, exfiltration, or policy bypass language is treated as safe context.",
        },
        {
            "name": "provider_or_delegation_trust_flattening",
            "severity": "medium",
            "summary": "Delegated work or provider fallback changes trust class without an operator-visible blocked receipt.",
        },
        {
            "name": "hidden_secure_host_receipt",
            "severity": "medium",
            "summary": "Secure-host posture is not visible in benchmark-proof, dedicated operator surfaces, and cockpit proof cards.",
        },
    ]


def secure_capability_host_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": SECURE_CAPABILITY_HOST_BENCHMARK_SUITE_NAME,
        "credential_egress_policy": "session_bound_field_scoped_destination_host_allowlisted_secret_refs",
        "workspace_secret_file_policy": "generic_read_and_patch_paths_block_secret_like_files",
        "process_environment_policy": "allowlisted_environment_only_for_foreground_and_background_processes",
        "prompt_surface_policy": "suspicious_prompt_bearing_content_quarantined_before_capability_execution",
        "delegation_provider_policy": "delegation_partitions_and_provider_fallback_trust_changes_must_be_explicit_receipts",
        "operator_visibility": "benchmark_proof_plus_secure_host_surface_plus_cockpit_receipts_visible",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/secure-capability-host-benchmark",
            "/api/operator/trust-boundary-benchmark",
            "/api/activity/ledger",
        ],
        "claim_boundary": "deterministic_secure_host_choke_points_not_full_host_container_isolation",
        "ci_gate_mode": "required_benchmark_suite",
    }


def _secure_capability_host_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Secure capability-host benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:10]


async def _run_secure_capability_host_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([SECURE_CAPABILITY_HOST_BENCHMARK_SUITE_NAME])


async def build_secure_capability_host_benchmark_report() -> dict[str, Any]:
    summary = await _run_secure_capability_host_benchmark_suite()
    failure_report = _secure_capability_host_failure_report(summary)
    healthy = summary.failed == 0
    degraded_state = "regressions_detected"
    return {
        "summary": {
            "suite_name": SECURE_CAPABILITY_HOST_BENCHMARK_SUITE_NAME,
            "benchmark_posture": (
                "secure_host_ci_gated_operator_visible"
                if healthy
                else "secure_host_regressions_detected_operator_visible"
            ),
            "operator_status": "secure_capability_host_receipts_visible",
            "scenario_count": len(SECURE_CAPABILITY_HOST_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(secure_capability_host_dimensions()),
            "failure_mode_count": len(secure_capability_host_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "credential_egress_state": "session_field_host_allowlist_enforced" if healthy else degraded_state,
            "workspace_secret_file_state": "generic_read_patch_blocked" if healthy else degraded_state,
            "process_environment_state": "ambient_secret_env_scrubbed" if healthy else degraded_state,
            "prompt_surface_state": "suspicious_context_quarantined" if healthy else degraded_state,
            "delegation_provider_state": "trust_partition_receipts_visible" if healthy else degraded_state,
        },
        "scenario_names": list(SECURE_CAPABILITY_HOST_BENCHMARK_SCENARIO_NAMES),
        "dimensions": secure_capability_host_dimensions(),
        "failure_taxonomy": secure_capability_host_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": secure_capability_host_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
