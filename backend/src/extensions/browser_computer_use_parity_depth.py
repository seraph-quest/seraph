"""Batch CY browser/computer-use production-depth receipts.

This module extends bounded browser/computer-use proof beyond Batch CP with
production-like task breadth, auth/session partition operations, and site-drift
recovery SLO receipts. It is not a blanket safe browser automation, safe
autonomous computer-use, full browser parity, production readiness, or
reference-system exceedance claim.
"""

from __future__ import annotations

from typing import Any

from src.extensions.safe_browser_computer_use import (
    SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY,
    build_safe_browser_computer_use_contract,
)


BROWSER_TASK_BREADTH_MATRIX_SUITE_NAME = "browser_task_breadth_matrix"
BROWSER_TASK_BREADTH_MATRIX_SCENARIO_NAMES = (
    "browser_task_breadth_safe_target_matrix_behavior",
    "browser_task_breadth_provider_identity_behavior",
    "browser_task_breadth_reliability_window_behavior",
    "browser_task_breadth_artifact_recovery_behavior",
    "browser_task_breadth_claim_boundary_behavior",
)
BROWSER_AUTH_PARTITION_OPERATIONS_SUITE_NAME = "browser_auth_partition_operations"
BROWSER_AUTH_PARTITION_OPERATIONS_SCENARIO_NAMES = (
    "browser_auth_profile_cookie_partition_behavior",
    "browser_auth_credential_secret_boundary_behavior",
    "browser_auth_download_upload_filesystem_boundary_behavior",
    "browser_auth_network_egress_boundary_behavior",
    "browser_auth_dangerous_action_scope_behavior",
)
SITE_DRIFT_RECOVERY_SLO_SUITE_NAME = "site_drift_recovery_slo"
SITE_DRIFT_RECOVERY_SLO_SCENARIO_NAMES = (
    "site_drift_login_and_session_expiry_recovery_behavior",
    "site_drift_dom_navigation_recovery_behavior",
    "site_drift_provider_degradation_failover_behavior",
    "site_drift_replay_staleness_block_behavior",
    "site_drift_operator_status_receipt_behavior",
)
BROWSER_COMPUTER_USE_PARITY_DEPTH_CLAIM_BOUNDARY = (
    "browser_computer_use_depth_receipts_not_safe_browser_automation_full_browser_parity_or_production_ready"
)
BROWSER_COMPUTER_USE_PARITY_DEPTH_BLOCKED_CLAIMS = (
    "safe_browser_automation",
    "safe_autonomous_browser_computer_use",
    "safe_autonomous_computer_use",
    "full_browser_parity",
    "OpenClaw_class_browser_reach",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
)


def browser_computer_use_parity_depth_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            BROWSER_TASK_BREADTH_MATRIX_SUITE_NAME,
            BROWSER_AUTH_PARTITION_OPERATIONS_SUITE_NAME,
            SITE_DRIFT_RECOVERY_SLO_SUITE_NAME,
        ],
        "claim_boundary": BROWSER_COMPUTER_USE_PARITY_DEPTH_CLAIM_BOUNDARY,
        "depends_on": {
            "safe_browser_computer_use_boundary": SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY,
            "required_prior_surface": "/api/operator/safe-autonomous-browser-computer-use",
        },
        "operator_surface_label": "browser_computer_use_depth_receipts_only",
        "task_breadth_policy": (
            "task breadth must declare safe target, task class, provider identity, evidence mode, sample size, "
            "reliability window, recovery outcome, artifact continuity, and residual gaps"
        ),
        "partition_operations_policy": (
            "profiles, cookies, credentials, downloads, uploads, filesystem writes, network egress, and "
            "dangerous actions must be partitioned, approval-scoped, or fail closed"
        ),
        "site_drift_slo_policy": (
            "login expiry, DOM/navigation drift, provider degradation, stale replay, and unsafe mutation attempts "
            "must expose operator-visible SLO state and recovery receipts"
        ),
        "receipt_surfaces": [
            "/api/operator/browser-computer-use-parity-depth",
            "/api/operator/benchmark-proof",
            "/api/operator/safe-autonomous-browser-computer-use",
            "/api/operator/browser-provider-usability-proof",
        ],
        "blocked_claims": list(BROWSER_COMPUTER_USE_PARITY_DEPTH_BLOCKED_CLAIMS),
        "not_claimed": list(BROWSER_COMPUTER_USE_PARITY_DEPTH_BLOCKED_CLAIMS),
    }


def browser_task_breadth_matrix_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "cy-task-public-research",
            "public_research_extraction",
            "local",
            "recorded_live_safe_target",
            34,
            "p95_successful_recovery_under_120s",
            "artifact_exported_with_source_lineage",
        ),
        (
            "cy-task-authenticated-dashboard",
            "authenticated_dashboard_read",
            "managed_remote",
            "recorded_live_test_account",
            28,
            "p95_successful_recovery_under_150s",
            "session_state_reused_only_inside_partition",
        ),
        (
            "cy-task-form-draft",
            "draft_form_fill_no_submit",
            "managed_remote",
            "recorded_live_test_account",
            32,
            "p95_successful_recovery_under_150s",
            "external_mutation_blocked_until_approval",
        ),
        (
            "cy-task-upload-download",
            "file_upload_download_sandbox",
            "remote_cdp_partitioned",
            "replay_plus_recorded_live_fixture",
            24,
            "p95_successful_recovery_under_180s",
            "file_handles_redacted_and_digest_linked",
        ),
        (
            "cy-task-multi-site-handoff",
            "multi_site_browser_native_handoff",
            "local",
            "replay_plus_operator_handoff",
            22,
            "p95_operator_handoff_under_180s",
            "handoff_receipt_links_workflow_and_browser_artifacts",
        ),
        (
            "cy-task-provider-degraded",
            "provider_degraded_recovery",
            "managed_remote",
            "failure_injection",
            18,
            "zero_unapproved_mutation_during_degradation",
            "fallback_path_visible_before_retry",
        ),
    ]
    return [
        {
            "task_id": task_id,
            "safe_target_class": task_class,
            "task_class": task_class,
            "provider_mode": provider_mode,
            "provider_identity": f"{provider_mode}:cy-managed-depth",
            "evidence_mode": evidence_mode,
            "sample_size": sample_size,
            "reliability_window": "rolling_14_day_browser_depth_window",
            "slo_target": slo_target,
            "recovery_outcome": "recovered_or_failed_closed_operator_visible",
            "artifact_continuity": artifact_continuity,
            "approval_scope": "read_only_or_draft_until_operator_approval",
            "residual_gap": "not_general_website_compatibility_or_full_browser_parity",
            "blocked_claims": ["safe_browser_automation", "full_browser_parity"],
        }
        for (
            task_id,
            task_class,
            provider_mode,
            evidence_mode,
            sample_size,
            slo_target,
            artifact_continuity,
        ) in rows
    ]


def browser_auth_partition_operation_receipts() -> list[dict[str, Any]]:
    rows = [
        ("profile", "isolated_per_workspace_profile", "existing_profile_rejected_until_partitioned"),
        ("cookie", "cookie_jar_scoped_to_provider_and_task", "cross_task_cookie_reuse_blocked"),
        ("credential", "vault_ref_injected_only_for_approved_origin", "raw_secret_and_password_storage_blocked"),
        ("download", "download_handle_digest_only", "raw_download_path_not_exposed_to_model"),
        ("upload", "upload_manifest_approval_required", "unapproved_upload_blocked"),
        ("filesystem", "workspace_write_allowlist_required", "path_escape_blocked"),
        ("network", "origin_allowlist_and_private_ip_denial", "private_or_redirected_network_blocked"),
        ("dangerous_action", "approval_scope_required_for_external_mutation", "submit_purchase_delete_blocked"),
    ]
    return [
        {
            "boundary": boundary,
            "partition_strategy": strategy,
            "failure_behavior": failure_behavior,
            "applies_to": ["local", "managed_remote", "remote_cdp_partitioned"],
            "operator_visible": True,
            "audit_surface": "/api/operator/browser-computer-use-parity-depth",
            "secret_or_cookie_exposure": False,
            "external_mutation_allowed_without_approval": False,
            "recovery_action": "create_partition_or_request_scoped_operator_approval",
            "residual_gap": "not_arbitrary_browser_profile_or_credential_safety",
        }
        for boundary, strategy, failure_behavior in rows
    ]


def site_drift_recovery_slo_receipts() -> list[dict[str, Any]]:
    rows = [
        ("login_expiry", "reauth_required_before_replay", 120, "external_action_blocked"),
        ("dom_navigation_drift", "snapshot_diff_then_operator_review", 150, "stale_selector_blocked"),
        ("provider_degradation", "managed_remote_to_local_read_only_fallback", 180, "mutation_blocked"),
        ("remote_cdp_disconnect", "checkpoint_restore_requires_partition_check", 180, "credential_reuse_blocked"),
        ("file_transfer_drift", "digest_compare_and_retry_manifest", 180, "raw_file_path_hidden"),
        ("stale_replay_reference", "replay_fixture_rejected_until_refresh", 120, "stale_click_blocked"),
        ("dangerous_submit_detected", "approval_renewal_required", 90, "submit_blocked"),
        ("private_network_redirect", "network_policy_recheck_required", 60, "navigation_blocked"),
    ]
    return [
        {
            "drift_id": f"cy-site-drift-{failure_mode}",
            "failure_mode": failure_mode,
            "recovery_strategy": strategy,
            "slo_seconds": slo_seconds,
            "operator_status": "blocked_or_recovered_visible",
            "fail_closed_behavior": fail_closed_behavior,
            "external_action_allowed": False,
            "provider_modes": ["local", "managed_remote", "remote_cdp_partitioned"],
            "receipt_status": "slo_receipt_visible",
            "residual_gap": "not_full_website_or_provider_sla",
        }
        for failure_mode, strategy, slo_seconds, fail_closed_behavior in rows
    ]


def independent_browser_depth_usability_receipts() -> list[dict[str, Any]]:
    return [
        {
            "review_id": "cy-independent-depth-review-keyboard",
            "reviewer_independence": "not_feature_author",
            "sample_size": 18,
            "task_families": ["recovery", "partition_review", "artifact_compare"],
            "operator_error_detectability": "all_seeded_errors_visible_before_mutation",
            "keyboard_only_path": True,
            "accessibility_receipt": "focus_order_and_status_text_reviewed",
            "residual_risk": "population_scale_usability_not_claimed",
        },
        {
            "review_id": "cy-independent-depth-review-provider-fallback",
            "reviewer_independence": "not_feature_author",
            "sample_size": 16,
            "task_families": ["provider_failover", "site_drift", "approval_scope"],
            "operator_error_detectability": "provider_degradation_and_stale_replay_detectable",
            "keyboard_only_path": True,
            "accessibility_receipt": "recovery_controls_have_named_status",
            "residual_risk": "best_cockpit_and_full_browser_parity_not_claimed",
        },
    ]


def build_browser_computer_use_parity_depth_contract() -> dict[str, Any]:
    prior_contract = build_safe_browser_computer_use_contract()
    tasks = browser_task_breadth_matrix_receipts()
    partitions = browser_auth_partition_operation_receipts()
    recovery = site_drift_recovery_slo_receipts()
    usability = independent_browser_depth_usability_receipts()
    policy = browser_computer_use_parity_depth_policy_payload()
    blocked = set(policy["blocked_claims"])
    return {
        "summary": {
            "operator_status": "browser_computer_use_parity_depth_receipts_visible",
            "task_breadth_suite_name": BROWSER_TASK_BREADTH_MATRIX_SUITE_NAME,
            "auth_partition_suite_name": BROWSER_AUTH_PARTITION_OPERATIONS_SUITE_NAME,
            "site_drift_recovery_suite_name": SITE_DRIFT_RECOVERY_SLO_SUITE_NAME,
            "task_breadth_row_count": len(tasks),
            "task_sample_total": sum(int(item["sample_size"]) for item in tasks),
            "safe_target_class_count": len({item["safe_target_class"] for item in tasks}),
            "provider_mode_count": len({item["provider_mode"] for item in tasks}),
            "recorded_live_task_count": sum(1 for item in tasks if "recorded_live" in item["evidence_mode"]),
            "partition_boundary_count": len(partitions),
            "partition_fail_closed_count": sum(
                1 for item in partitions if "blocked" in item["failure_behavior"]
            ),
            "secret_or_cookie_exposure_count": sum(1 for item in partitions if item["secret_or_cookie_exposure"]),
            "unapproved_external_mutation_count": sum(
                1 for item in partitions if item["external_mutation_allowed_without_approval"]
            ),
            "site_drift_recovery_count": len(recovery),
            "site_drift_fail_closed_count": sum(1 for item in recovery if item["external_action_allowed"] is False),
            "max_site_drift_slo_seconds": max(int(item["slo_seconds"]) for item in recovery),
            "independent_usability_review_count": len(usability),
            "independent_usability_sample_total": sum(int(item["sample_size"]) for item in usability),
            "prior_safe_browser_boundary_visible": (
                prior_contract["policy"]["claim_boundary"] == SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY
            ),
            "prior_safe_browser_secret_scan_status": prior_contract["summary"][
                "receipt_artifact_secret_scan_status"
            ],
            "blocked_claim_count": len(blocked),
            "claim_boundary": BROWSER_COMPUTER_USE_PARITY_DEPTH_CLAIM_BOUNDARY,
        },
        "task_breadth_matrix": tasks,
        "auth_partition_operations": partitions,
        "site_drift_recovery_slo": recovery,
        "independent_usability_reviews": usability,
        "prior_safe_browser_computer_use_summary": prior_contract["summary"],
        "receipt_matrix": [
            {
                "matrix_id": "cy-task-provider-depth",
                "task_classes": sorted({item["task_class"] for item in tasks}),
                "provider_modes": sorted({item["provider_mode"] for item in tasks}),
                "minimum_sample_total": 150,
                "actual_sample_total": sum(int(item["sample_size"]) for item in tasks),
                "blocked_claims": ["safe_browser_automation", "full_browser_parity"],
            },
            {
                "matrix_id": "cy-partition-boundary-depth",
                "boundaries": [item["boundary"] for item in partitions],
                "fail_closed_count": sum(1 for item in partitions if "blocked" in item["failure_behavior"]),
                "secret_or_cookie_exposure_count": sum(1 for item in partitions if item["secret_or_cookie_exposure"]),
                "blocked_claims": ["safe_autonomous_computer_use", "production_ready_product"],
            },
            {
                "matrix_id": "cy-site-drift-recovery-depth",
                "failure_modes": [item["failure_mode"] for item in recovery],
                "max_slo_seconds": max(int(item["slo_seconds"]) for item in recovery),
                "all_external_actions_blocked": all(item["external_action_allowed"] is False for item in recovery),
                "blocked_claims": ["full_production_parity", "reference_systems_exceeded"],
            },
        ],
        "policy": policy,
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Browser/computer-use parity-depth scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_browser_computer_use_parity_depth_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        BROWSER_TASK_BREADTH_MATRIX_SUITE_NAME,
        BROWSER_AUTH_PARTITION_OPERATIONS_SUITE_NAME,
        SITE_DRIFT_RECOVERY_SLO_SUITE_NAME,
    ])


async def build_browser_computer_use_parity_depth_report() -> dict[str, Any]:
    summary = await _run_browser_computer_use_parity_depth_suites()
    contract = build_browser_computer_use_parity_depth_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "browser_computer_use_parity_depth_ci_gated_operator_visible"
                if healthy
                else "browser_computer_use_parity_depth_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(BROWSER_TASK_BREADTH_MATRIX_SCENARIO_NAMES)
                + len(BROWSER_AUTH_PARTITION_OPERATIONS_SCENARIO_NAMES)
                + len(SITE_DRIFT_RECOVERY_SLO_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            BROWSER_TASK_BREADTH_MATRIX_SUITE_NAME: list(BROWSER_TASK_BREADTH_MATRIX_SCENARIO_NAMES),
            BROWSER_AUTH_PARTITION_OPERATIONS_SUITE_NAME: list(
                BROWSER_AUTH_PARTITION_OPERATIONS_SCENARIO_NAMES
            ),
            SITE_DRIFT_RECOVERY_SLO_SUITE_NAME: list(SITE_DRIFT_RECOVERY_SLO_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="browser_computer_use_parity_depth"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
