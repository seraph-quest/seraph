import asyncio

from src.extensions.safe_browser_computer_use import (
    AUTONOMOUS_BROWSER_SAFETY_SCENARIO_NAMES,
    BROWSER_PROVIDER_RELIABILITY_MATRIX_SCENARIO_NAMES,
    BROWSER_SESSION_PARTITIONING_SCENARIO_NAMES,
    INDEPENDENT_BROWSER_USABILITY_REVIEW_SCENARIO_NAMES,
    LIVE_BROWSER_TASK_DEPTH_SCENARIO_NAMES,
    SAFE_BROWSER_COMPUTER_USE_BLOCKED_CLAIMS,
    SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY,
    SITE_SPECIFIC_BROWSER_RECOVERY_SCENARIO_NAMES,
    build_safe_browser_computer_use_contract,
    build_safe_browser_computer_use_report,
    receipt_artifact_security_scan,
)


def test_safe_browser_computer_use_contract_exposes_task_depth_and_boundaries():
    contract = build_safe_browser_computer_use_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "safe_browser_computer_use_receipts_visible"
    assert summary["provider_mode_count"] == 3
    assert summary["recorded_live_provider_count"] == 2
    assert summary["task_class_count"] == 4
    assert summary["task_sample_total"] >= 30
    assert summary["raw_receipt_artifact_count"] >= 36
    assert summary["raw_receipt_evidence_body_count"] == summary["raw_receipt_artifact_count"]
    assert summary["raw_receipt_missing_count"] == 0
    assert summary["raw_receipt_required_field_missing_count"] == 0
    assert summary["secret_or_credential_leak_count"] == 0
    assert summary["receipt_artifact_secret_scan_status"] == "passed"
    assert {
        "receipt_json",
        "redacted_payload_excerpt",
        "evidence_summary",
    } <= set(summary["receipt_artifact_secret_scan_scope"])
    assert summary["claim_boundary"] == SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY
    assert set(SAFE_BROWSER_COMPUTER_USE_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/safe-autonomous-browser-computer-use" in policy["receipt_surfaces"]


def test_safe_browser_computer_use_receipts_are_artifact_backed_and_evidence_shaped():
    contract = build_safe_browser_computer_use_contract()
    required_fields = {
        "workload",
        "sample_size",
        "environment",
        "provider_configuration",
        "baseline_or_rationale",
        "raw_receipt_location",
        "raw_receipt_artifact_present",
        "raw_receipt_artifact_kind",
        "raw_receipt_digest",
        "raw_receipt_outcome",
        "raw_receipt_redaction_status",
        "raw_receipt_secret_scan_status",
        "failure_budget",
        "residual_gap",
    }
    receipt_families = [
        contract["provider_mode_receipts"],
        contract["live_task_depth_receipts"],
        contract["dangerous_action_taxonomy"],
        contract["autonomous_task_receipts"],
        contract["session_isolation_invariants"],
        contract["site_specific_recovery_drills"],
        contract["browser_provider_reliability_matrix"],
        contract["independent_usability_reviews"],
    ]

    assert receipt_artifact_security_scan()["secret_scan_status"] == "passed"
    assert receipt_artifact_security_scan()["receipt_required_field_missing_count"] == 0
    assert contract["receipt_artifact_security_scan"]["raw_receipt_count"] >= 36
    assert contract["receipt_artifact_security_scan"]["receipts_with_evidence_body_count"] == contract[
        "receipt_artifact_security_scan"
    ]["raw_receipt_count"]
    for receipt in [item for family in receipt_families for item in family]:
        assert required_fields <= set(receipt)
        assert receipt["raw_receipt_location"].startswith(
            "backend/src/defaults/operator_receipts/safe_browser_computer_use/cp_receipts.json#"
        )
        assert receipt["raw_receipt_artifact_present"] is True
        assert receipt["raw_receipt_artifact_kind"]
        assert receipt["raw_receipt_digest"].startswith("sha256:")
        assert receipt["raw_receipt_outcome"]
        assert receipt["raw_receipt_redaction_status"] == "passed_no_raw_credentials_cookies_or_private_page_content"
        assert receipt["raw_receipt_secret_scan_status"] == "passed"
    for matrix_row in contract["receipt_matrix"]:
        assert matrix_row["raw_receipt_locations"]
        assert all(
            item.startswith("backend/src/defaults/operator_receipts/safe_browser_computer_use/cp_receipts.json#")
            for item in matrix_row["raw_receipt_locations"]
        )


def test_safe_browser_computer_use_blocks_dangerous_actions_by_default():
    contract = build_safe_browser_computer_use_contract()

    assert contract["summary"]["dangerous_action_category_count"] == 7
    assert contract["summary"]["dangerous_action_default_block_count"] == 7
    assert {
        "financial",
        "legal",
        "medical",
        "account",
        "security",
        "destructive",
        "personal_data",
    } == {item["category"] for item in contract["dangerous_action_taxonomy"]}
    assert all(item["external_mutation_allowed_without_approval"] is False for item in contract["dangerous_action_taxonomy"])


def test_safe_browser_computer_use_preserves_session_and_secret_boundaries():
    contract = build_safe_browser_computer_use_contract()
    providers = {item["provider_id"]: item for item in contract["provider_mode_receipts"]}

    assert providers["cp-remote-cdp-existing-profile"]["degradation_state"] == "blocked_until_partitioned"
    assert providers["cp-remote-cdp-existing-profile"]["network_boundary"] == "blocked_until_connection_and_scope_review"
    assert contract["summary"]["session_isolation_invariant_count"] == 6
    assert contract["summary"]["session_isolation_satisfied_count"] == 6
    assert any(
        item["invariant"] == "replay_fixture_scrubbing"
        and item["failure_behavior"] == "fixture_rejected_if_secret_or_cookie_pattern_detected"
        for item in contract["session_isolation_invariants"]
    )


def test_safe_browser_computer_use_recovery_drills_fail_closed():
    contract = build_safe_browser_computer_use_contract()

    assert contract["summary"]["site_recovery_drill_count"] == 8
    assert contract["summary"]["fail_closed_recovery_count"] == 8
    assert all(item["operator_visible"] for item in contract["site_specific_recovery_drills"])
    assert all(item["external_action_allowed"] is False for item in contract["site_specific_recovery_drills"])


def test_safe_browser_computer_use_provider_reliability_matrix_is_visible():
    contract = build_safe_browser_computer_use_contract()

    assert contract["summary"]["provider_reliability_receipt_count"] == 3
    assert contract["summary"]["provider_reliability_sample_total"] >= 20
    assert {
        "local",
        "managed_remote",
        "remote_cdp_existing_session",
    } == {item["provider_mode"] for item in contract["browser_provider_reliability_matrix"]}
    assert all(item["raw_receipt_location"] for item in contract["browser_provider_reliability_matrix"])
    assert any(
        item["fallback_path"] == "block_until_profile_partition_created"
        for item in contract["browser_provider_reliability_matrix"]
    )


def test_safe_browser_computer_use_independent_usability_has_metadata():
    contract = build_safe_browser_computer_use_contract()

    assert contract["summary"]["independent_usability_review_count"] == 2
    assert contract["summary"]["independent_usability_sample_total"] >= 20
    assert all(item["reviewer_independence"] for item in contract["independent_usability_reviews"])
    assert all(item["raw_receipt_location"] for item in contract["independent_usability_reviews"])
    assert all(item["residual_risk"] for item in contract["independent_usability_reviews"])


def test_safe_browser_computer_use_report_runs_all_batch_cp_suites():
    payload = asyncio.run(build_safe_browser_computer_use_report())

    assert payload["summary"]["benchmark_posture"] == "safe_browser_computer_use_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(LIVE_BROWSER_TASK_DEPTH_SCENARIO_NAMES)
        + len(AUTONOMOUS_BROWSER_SAFETY_SCENARIO_NAMES)
        + len(BROWSER_SESSION_PARTITIONING_SCENARIO_NAMES)
        + len(SITE_SPECIFIC_BROWSER_RECOVERY_SCENARIO_NAMES)
        + len(BROWSER_PROVIDER_RELIABILITY_MATRIX_SCENARIO_NAMES)
        + len(INDEPENDENT_BROWSER_USABILITY_REVIEW_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY
