import asyncio

from src.extensions.browser_computer_use_parity_depth import (
    BROWSER_AUTH_PARTITION_OPERATIONS_SCENARIO_NAMES,
    BROWSER_COMPUTER_USE_PARITY_DEPTH_BLOCKED_CLAIMS,
    BROWSER_COMPUTER_USE_PARITY_DEPTH_CLAIM_BOUNDARY,
    BROWSER_TASK_BREADTH_MATRIX_SCENARIO_NAMES,
    SITE_DRIFT_RECOVERY_SLO_SCENARIO_NAMES,
    build_browser_computer_use_parity_depth_contract,
    build_browser_computer_use_parity_depth_report,
)


def test_browser_computer_use_parity_depth_contract_exposes_cy_depth():
    contract = build_browser_computer_use_parity_depth_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "browser_computer_use_parity_depth_receipts_visible"
    assert summary["task_breadth_row_count"] == 6
    assert summary["task_sample_total"] >= 150
    assert summary["safe_target_class_count"] == 6
    assert summary["provider_mode_count"] == 3
    assert summary["recorded_live_task_count"] >= 4
    assert summary["partition_boundary_count"] == 8
    assert summary["secret_or_cookie_exposure_count"] == 0
    assert summary["unapproved_external_mutation_count"] == 0
    assert summary["site_drift_recovery_count"] == 8
    assert summary["site_drift_fail_closed_count"] == 8
    assert summary["max_site_drift_slo_seconds"] <= 180
    assert summary["prior_safe_browser_boundary_visible"] is True
    assert summary["prior_safe_browser_secret_scan_status"] == "passed"
    assert summary["claim_boundary"] == BROWSER_COMPUTER_USE_PARITY_DEPTH_CLAIM_BOUNDARY
    assert set(BROWSER_COMPUTER_USE_PARITY_DEPTH_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/browser-computer-use-parity-depth" in policy["receipt_surfaces"]


def test_browser_computer_use_parity_depth_task_matrix_is_provider_and_artifact_shaped():
    contract = build_browser_computer_use_parity_depth_contract()
    tasks = contract["task_breadth_matrix"]

    assert {
        "public_research_extraction",
        "authenticated_dashboard_read",
        "draft_form_fill_no_submit",
        "file_upload_download_sandbox",
        "multi_site_browser_native_handoff",
        "provider_degraded_recovery",
    } == {item["task_class"] for item in tasks}
    assert {"local", "managed_remote", "remote_cdp_partitioned"} == {item["provider_mode"] for item in tasks}
    assert all(item["provider_identity"] for item in tasks)
    assert all(item["reliability_window"] == "rolling_14_day_browser_depth_window" for item in tasks)
    assert all(item["artifact_continuity"] for item in tasks)
    assert all(item["recovery_outcome"] == "recovered_or_failed_closed_operator_visible" for item in tasks)


def test_browser_computer_use_parity_depth_partitions_and_drift_fail_closed():
    contract = build_browser_computer_use_parity_depth_contract()
    partitions = contract["auth_partition_operations"]
    recovery = contract["site_drift_recovery_slo"]

    assert {
        "profile",
        "cookie",
        "credential",
        "download",
        "upload",
        "filesystem",
        "network",
        "dangerous_action",
    } == {item["boundary"] for item in partitions}
    assert all(item["operator_visible"] for item in partitions)
    assert all(item["secret_or_cookie_exposure"] is False for item in partitions)
    assert all(item["external_mutation_allowed_without_approval"] is False for item in partitions)
    assert {
        "login_expiry",
        "dom_navigation_drift",
        "provider_degradation",
        "remote_cdp_disconnect",
        "file_transfer_drift",
        "stale_replay_reference",
        "dangerous_submit_detected",
        "private_network_redirect",
    } == {item["failure_mode"] for item in recovery}
    assert all(item["external_action_allowed"] is False for item in recovery)
    assert all(item["operator_status"] == "blocked_or_recovered_visible" for item in recovery)


def test_browser_computer_use_parity_depth_report_runs_all_cy_suites():
    payload = asyncio.run(build_browser_computer_use_parity_depth_report())

    assert payload["summary"]["benchmark_posture"] == (
        "browser_computer_use_parity_depth_ci_gated_operator_visible"
    )
    assert payload["summary"]["scenario_count"] == (
        len(BROWSER_TASK_BREADTH_MATRIX_SCENARIO_NAMES)
        + len(BROWSER_AUTH_PARTITION_OPERATIONS_SCENARIO_NAMES)
        + len(SITE_DRIFT_RECOVERY_SLO_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == BROWSER_COMPUTER_USE_PARITY_DEPTH_CLAIM_BOUNDARY
