from __future__ import annotations

from typing import Any


SECURE_CAPABILITY_HOST_BENCHMARK_SUITE_NAME = "secure_capability_host"
SECURE_CAPABILITY_HOST_BENCHMARK_SCENARIO_NAMES = (
    "secure_host_secret_ref_fail_closed_behavior",
    "secure_host_isolation_strategy_report_behavior",
    "secure_host_browser_cookie_session_partition_behavior",
    "secure_host_workspace_secret_path_boundary_behavior",
    "secure_host_workspace_escape_boundary_behavior",
    "secure_host_process_env_isolation_behavior",
    "secure_host_prompt_injection_quarantine_behavior",
    "secure_host_delegation_partition_behavior",
    "secure_host_provider_fallback_boundary_behavior",
    "secure_host_hostile_provider_replay_behavior",
    "secure_host_capability_trust_matrix_behavior",
    "secure_host_receipt_surface_completeness_behavior",
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
            "name": "workspace_escape",
            "label": "Workspace escape boundary",
            "summary": "Command, script, patch, and file paths must remain under the configured workspace or explicit disposable worker roots.",
        },
        {
            "name": "process_isolation",
            "label": "Process environment isolation",
            "summary": "Foreground and background processes receive an allowlisted environment instead of ambient host credentials.",
        },
        {
            "name": "browser_session_partition",
            "label": "Browser cookie and session partition",
            "summary": "Browser actions use per-run contexts and must not expose cookie, storage-state, or authenticated-session material in receipts.",
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
        {
            "name": "capability_trust_regression_matrix",
            "label": "Capability and trust regression matrix",
            "summary": "Core, MCP, browser, process, filesystem, delegation, provider, and extension capability classes carry owner, boundary, credential, mutation, and receipt expectations.",
        },
        {
            "name": "receipt_surface_completeness",
            "label": "Receipt surface completeness",
            "summary": "Secure-host proof must be visible through benchmark-proof, the dedicated secure-host endpoint, trust-boundary reporting, and activity/operator receipts.",
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
            "name": "workspace_escape",
            "severity": "high",
            "summary": "A workspace path, script path, or command path escapes the configured workspace without a blocked receipt.",
        },
        {
            "name": "browser_cookie_session_bleed",
            "severity": "high",
            "summary": "Browser actions reuse authenticated cookie/session state across partitions or expose session material in operator receipts.",
        },
        {
            "name": "prompt_injection_unquarantined",
            "severity": "medium",
            "summary": "Prompt-bearing content with instruction override, exfiltration, or policy bypass language is treated as safe context.",
        },
        {
            "name": "hostile_provider_replay",
            "severity": "high",
            "summary": "A provider replay or fallback can widen trust class, reuse sensitive context, or hide the blocked replay reason.",
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


def secure_capability_host_isolation_strategy() -> dict[str, Any]:
    return {
        "strategy": "deterministic_choke_point_isolation",
        "enforced_boundaries": [
            "session_bound_secret_refs",
            "destination_host_credential_egress_allowlists",
            "workspace_relative_filesystem_paths",
            "disposable_worker_roots_outside_workspace",
            "allowlisted_process_environment",
            "per_run_browser_contexts_without_persisted_storage_state",
            "provider_and_delegation_trust_receipts",
        ],
        "not_claimed": [
            "full_host_container_isolation",
            "production_secure_by_default_execution",
            "arbitrary_workspace_script_file_read_isolation",
            "complete_browser_cookie_or_credential_isolation",
            "live_provider_security_attestation",
        ],
    }


def secure_capability_host_browser_partition_policy() -> dict[str, Any]:
    return {
        "cookie_policy": "per_run_browser_contexts_do_not_persist_cookie_or_storage_state",
        "session_policy": "browser_receipts_expose_url_action_site_policy_without_cookie_or_session_values",
        "claim_boundary": "deterministic_browser_partition_strategy_not_complete_authenticated_browser_isolation",
    }


def secure_capability_host_capability_trust_matrix() -> list[dict[str, Any]]:
    return [
        {
            "capability_class": "core_filesystem",
            "owner": "seraph_core",
            "trust_boundary": "workspace_filesystem",
            "credential_egress": "not_allowed",
            "mutation_policy": "workspace_patch_preview_apply_receipts",
            "receipt_required": True,
        },
        {
            "capability_class": "process_execution",
            "owner": "seraph_core",
            "trust_boundary": "disposable_worker_process",
            "credential_egress": "allowlisted_environment_only",
            "mutation_policy": "workspace_relative_command_paths",
            "receipt_required": True,
        },
        {
            "capability_class": "browser_computer_use",
            "owner": "seraph_core_or_configured_provider",
            "trust_boundary": "per_run_browser_context",
            "credential_egress": "no_cookie_or_session_values_in_receipts",
            "mutation_policy": "site_policy_and_action_receipts",
            "receipt_required": True,
        },
        {
            "capability_class": "authenticated_mcp_connector",
            "owner": "connector_or_extension",
            "trust_boundary": "external_mcp_credential_egress",
            "credential_egress": "field_scoped_destination_host_allowlist",
            "mutation_policy": "declared_payload_fields_and_approval_context",
            "receipt_required": True,
        },
        {
            "capability_class": "delegated_specialist",
            "owner": "seraph_specialist_registry",
            "trust_boundary": "delegation_partition",
            "credential_egress": "specialist_specific_boundaries_only",
            "mutation_policy": "unresolved_or_privileged_targets_fail_closed",
            "receipt_required": True,
        },
        {
            "capability_class": "provider_fallback",
            "owner": "runtime_policy",
            "trust_boundary": "provider_trust_class",
            "credential_egress": "no_trust_expanding_replay",
            "mutation_policy": "fallback_must_preserve_or_narrow_trust_class",
            "receipt_required": True,
        },
        {
            "capability_class": "extension_capability",
            "owner": "extension_manifest",
            "trust_boundary": "declared_permission_profile",
            "credential_egress": "manifest_and_tool_policy_gated",
            "mutation_policy": "permission_creep_blocks_execution",
            "receipt_required": True,
        },
    ]


def secure_capability_host_receipt_surface_completeness() -> dict[str, Any]:
    return {
        "required_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/secure-capability-host-benchmark",
            "/api/operator/trust-boundary-benchmark",
            "/api/activity/ledger",
        ],
        "required_receipt_fields": [
            "suite_name",
            "scenario_names",
            "dimensions",
            "failure_taxonomy",
            "failure_report",
            "policy",
            "latest_run",
            "claim_boundary",
        ],
    }


def secure_capability_host_activity_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "surface": "/api/activity/ledger",
        "blocked_reason": list(receipt.get("blocked_reasons") or []),
        "trust_boundary": list(receipt.get("execution_boundaries") or []),
        "source": str(receipt.get("source") or "unknown"),
        "destination": list((receipt.get("credential_egress") or {}).get("allowed_hosts") or []),
        "recovery_posture": "recoverable" if (receipt.get("operator_receipt") or {}).get("recoverable") else "not_required",
    }


def secure_capability_host_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": SECURE_CAPABILITY_HOST_BENCHMARK_SUITE_NAME,
        "host_isolation_policy": "deterministic_choke_points_with_explicit_non_container_claim_boundary",
        "credential_egress_policy": "session_bound_field_scoped_destination_host_allowlisted_secret_refs",
        "workspace_secret_file_policy": "generic_read_and_patch_paths_block_secret_like_files",
        "workspace_escape_policy": "workspace_relative_paths_and_disposable_worker_roots_must_not_escape",
        "process_environment_policy": "allowlisted_environment_only_for_foreground_and_background_processes",
        "browser_cookie_session_policy": "per_run_browser_contexts_without_persisted_cookie_or_storage_state",
        "prompt_surface_policy": "suspicious_prompt_bearing_content_quarantined_before_capability_execution",
        "delegation_provider_policy": "delegation_partitions_and_provider_fallback_trust_changes_must_be_explicit_receipts",
        "hostile_provider_replay_policy": "provider_replay_or_fallback_must_not_expand_trust_class_or_reuse_sensitive_context",
        "capability_trust_regression_policy": "capability_classes_require_owner_boundary_credential_mutation_audit_receipts",
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
            "host_isolation_state": "deterministic_choke_points_claim_bounded" if healthy else degraded_state,
            "credential_egress_state": "session_field_host_allowlist_enforced" if healthy else degraded_state,
            "workspace_secret_file_state": "generic_read_patch_blocked" if healthy else degraded_state,
            "workspace_escape_state": "workspace_relative_paths_enforced" if healthy else degraded_state,
            "process_environment_state": "ambient_secret_env_scrubbed" if healthy else degraded_state,
            "browser_cookie_session_state": "per_run_context_no_storage_state_receipts" if healthy else degraded_state,
            "prompt_surface_state": "suspicious_context_quarantined" if healthy else degraded_state,
            "delegation_provider_state": "trust_partition_receipts_visible" if healthy else degraded_state,
            "hostile_provider_replay_state": "trust_expanding_replay_blocked" if healthy else degraded_state,
            "capability_trust_matrix_state": "owner_boundary_credential_mutation_receipts_visible" if healthy else degraded_state,
            "receipt_surface_completeness_state": "required_secure_host_surfaces_visible" if healthy else degraded_state,
        },
        "scenario_names": list(SECURE_CAPABILITY_HOST_BENCHMARK_SCENARIO_NAMES),
        "dimensions": secure_capability_host_dimensions(),
        "failure_taxonomy": secure_capability_host_failure_taxonomy(),
        "failure_report": failure_report,
        "isolation_strategy": secure_capability_host_isolation_strategy(),
        "browser_partition_policy": secure_capability_host_browser_partition_policy(),
        "capability_trust_regression_matrix": secure_capability_host_capability_trust_matrix(),
        "receipt_surface_completeness": secure_capability_host_receipt_surface_completeness(),
        "policy": secure_capability_host_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
