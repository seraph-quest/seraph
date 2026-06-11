import asyncio

from src.extensions.full_browser_parity import (
    BROWSER_PARITY_EVIDENCE_BLOCKED_CLAIMS,
    BROWSER_PARITY_EVIDENCE_CLAIM_BOUNDARY,
    BROWSER_SESSION_PARTITION_CERTIFICATION_V1_SCENARIO_NAMES,
    FULL_BROWSER_PARITY_MATRIX_V1_SCENARIO_NAMES,
    REAL_SITE_DRIFT_RECOVERY_V2_SCENARIO_NAMES,
    REQUIRED_BROWSER_BOUNDARIES,
    REQUIRED_BROWSER_PROVIDER_MODES,
    REQUIRED_HOSTILE_BROWSER_CASES,
    REQUIRED_REAL_SITE_DRIFT_CLASSES,
    SAFE_AUTONOMOUS_BROWSER_RUNTIME_V1_SCENARIO_NAMES,
    build_full_browser_parity_contract,
    build_full_browser_parity_report,
)


def test_full_browser_parity_contract_blocks_overclaims():
    contract = build_full_browser_parity_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "browser_parity_evidence_receipts_visible"
    assert summary["claim_boundary"] == BROWSER_PARITY_EVIDENCE_CLAIM_BOUNDARY
    assert summary["safe_browser_automation_claim_allowed"] is False
    assert summary["safe_autonomous_computer_use_claim_allowed"] is False
    assert summary["full_browser_parity_claim_allowed"] is False
    assert set(BROWSER_PARITY_EVIDENCE_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert set(BROWSER_PARITY_EVIDENCE_BLOCKED_CLAIMS) <= set(policy["not_claimed"])
    assert "/api/operator/full-browser-parity" in policy["receipt_surfaces"]


def test_full_browser_parity_runtime_provider_boundaries_are_safe():
    contract = build_full_browser_parity_contract()
    summary = contract["summary"]
    runtime = contract["safe_autonomous_browser_runtime"]

    assert summary["runtime_task_count"] >= 12
    assert summary["runtime_sample_total"] >= 350
    assert summary["required_provider_modes_covered"] is True
    assert summary["managed_remote_live_provider_claimed"] is False
    assert summary["existing_session_unpartitioned_blocked"] is True
    assert summary["dangerous_actions_default_blocked"] is True
    assert {item["provider_mode"] for item in runtime} == set(REQUIRED_BROWSER_PROVIDER_MODES)
    assert all(
        item["execution_mode"] != "live_provider_execution"
        for item in runtime
        if item["provider_mode"] in {"managed_remote", "remote_cdp_partitioned"}
    )


def test_full_browser_parity_negative_cases_and_redactions_are_visible():
    contract = build_full_browser_parity_contract()
    summary = contract["summary"]
    matrix = contract["full_browser_parity_matrix"]
    drift = contract["real_site_drift_recovery_v2"]
    hostile = contract["hostile_browser_negative_cases"]
    partition = contract["browser_session_partition_certification"]
    redaction = contract["redaction_scan_receipts"]
    receipts = [
        item["safe_receipt"]
        for item in [*contract["safe_autonomous_browser_runtime"], *matrix, *drift, *hostile, *partition, *redaction]
    ]

    assert summary["required_boundaries_covered"] is True
    assert summary["all_boundaries_enforced"] is True
    assert summary["boundary_leak_count"] == 0
    assert summary["boundary_negative_case_count"] >= (
        len(REQUIRED_BROWSER_PROVIDER_MODES) * len(REQUIRED_BROWSER_BOUNDARIES)
    )
    assert set(REQUIRED_BROWSER_BOUNDARIES) <= {
        boundary["boundary"]
        for row in matrix
        for boundary in row["boundaries"]
    }
    assert all(
        boundary["negative_case_verified"] is True
        and boundary["negative_case_receipt"]["decision"] == "blocked"
        and boundary["negative_case_receipt"]["seeded_sensitive_value_present_in_raw_fixture"] is True
        and boundary["negative_case_receipt"]["seeded_sensitive_value_present_in_safe_receipt"] is False
        and len(boundary["negative_case_receipt"]["seeded_sensitive_value_digest"]) == 64
        and len(boundary["negative_case_receipt"]["safe_receipt_digest"]) == 64
        for row in matrix
        for boundary in row["boundaries"]
    )
    assert summary["required_real_site_drift_classes_covered"] is True
    assert set(REQUIRED_REAL_SITE_DRIFT_CLASSES) <= {item["drift_class"] for item in drift}
    assert all(
        item["real_site_fixture_mode"] == "deterministic_safe_target_fixture_with_redacted_artifact_digests"
        and item["fixture_artifact_id"].startswith("artifact:browser-dg:site-drift:")
        and len(item["selector_diff_digest"]) == 64
        and len(item["dom_snapshot_digest"]) == 64
        and len(item["screenshot_digest"]) == 64
        and len(item["auth_or_network_trace_digest"]) == 64
        for item in drift
    )
    assert summary["required_hostile_browser_cases_covered"] is True
    assert set(REQUIRED_HOSTILE_BROWSER_CASES) <= {item["hostile_case"] for item in hostile}
    assert summary["credential_leak_count"] == 0
    assert summary["cookie_leak_count"] == 0
    assert summary["private_data_leak_count"] == 0
    assert summary["clipboard_leak_count"] == 0
    assert summary["unapproved_mutation_count"] == 0
    assert summary["partition_session_leak_count"] == 0
    assert summary["partition_claim_lift_blocked"] is True
    assert summary["partition_verified_provider_mode_count"] == len(REQUIRED_BROWSER_PROVIDER_MODES) - 1
    assert summary["existing_session_partition_certificate_blocked"] is True
    existing_session = next(
        item
        for item in partition
        if item["provider_mode"] == "existing_session_unpartitioned_blocked"
    )
    assert existing_session["negative_certification_receipt"] is True
    assert existing_session["profile_partition_verified"] is False
    assert existing_session["cookie_jar_isolated"] is False
    assert existing_session["credential_scope_verified"] is False
    assert existing_session["network_private_egress_blocked"] is False
    assert summary["redaction_scan_count"] == len(redaction)
    assert summary["redaction_scan_passed"] is True
    assert all(item["seed_marker_present_in_raw_fixture"] is True for item in redaction)
    assert all(item["seed_marker_present_in_safe_receipt"] is False for item in redaction)
    assert all(item["scan_passed"] is True for item in redaction)
    assert all(len(item["raw_seed_payload_digest"]) == 64 for item in redaction)
    assert all(len(item["seed_marker_digest"]) == 64 for item in redaction)
    assert all(receipt["redaction_layer"] == "browser_parity_evidence_v1" for receipt in receipts)
    assert all(receipt["contains_raw_dom"] is False for receipt in receipts)
    assert all(receipt["contains_screenshot"] is False for receipt in receipts)
    assert all(receipt["contains_clipboard_content"] is False for receipt in receipts)
    assert all(receipt["contains_private_page_content"] is False for receipt in receipts)
    assert all(receipt["tamper_evident_digest"] != receipt["evidence_body_digest"] for receipt in receipts)


def test_full_browser_parity_report_runs_all_gates():
    report = asyncio.run(build_full_browser_parity_report())
    summary = report["summary"]

    assert summary["benchmark_posture"] == "browser_parity_evidence_ci_gated_operator_visible"
    assert summary["active_failure_count"] == 0
    assert summary["scenario_count"] == (
        len(SAFE_AUTONOMOUS_BROWSER_RUNTIME_V1_SCENARIO_NAMES)
        + len(FULL_BROWSER_PARITY_MATRIX_V1_SCENARIO_NAMES)
        + len(REAL_SITE_DRIFT_RECOVERY_V2_SCENARIO_NAMES)
        + len(BROWSER_SESSION_PARTITION_CERTIFICATION_V1_SCENARIO_NAMES)
    )
    assert report["latest_run"]["failed"] == 0
    assert report["failure_report"] == []
