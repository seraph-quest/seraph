import asyncio

from src.guardian.post_dp_guardian_memory_gap_closure import (
    GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES,
    GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    LEARNING_SAFETY_REGRESSION_V2_SCENARIO_NAMES,
    LEARNING_SAFETY_REGRESSION_V2_SUITE_NAME,
    LONG_HORIZON_LEARNING_QUALITY_V2_SCENARIO_NAMES,
    LONG_HORIZON_LEARNING_QUALITY_V2_SUITE_NAME,
    MEMORY_BEHAVIOR_ABLATION_V2_SCENARIO_NAMES,
    MEMORY_BEHAVIOR_ABLATION_V2_SUITE_NAME,
    MEMORY_PROVIDER_OPERATION_V2_SCENARIO_NAMES,
    MEMORY_PROVIDER_OPERATION_V2_SUITE_NAME,
    POST_DP_GUARDIAN_MEMORY_BLOCKED_CLAIMS,
    POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY,
    POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SCENARIO_NAMES,
    POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SUITE_NAME,
    POST_DP_GUARDIAN_MEMORY_REDACTION_BOUNDARY,
    build_post_dp_guardian_memory_contract,
    build_post_dp_guardian_memory_report,
)


def test_post_dp_guardian_memory_contract_covers_dt_acceptance_fields():
    contract = build_post_dp_guardian_memory_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "post_dp_guardian_learning_memory_gap_closure_visible"
    assert summary["foundation_operator_status"] == "live_guardian_memory_field_program_receipts_visible"
    assert summary["long_horizon_study_count"] >= 8
    assert summary["pre_registered_count"] == summary["long_horizon_study_count"]
    assert summary["withdrawal_supported_count"] == summary["long_horizon_study_count"]
    assert summary["anonymized_count"] == summary["long_horizon_study_count"]
    assert summary["adverse_event_reviewed_count"] == summary["adverse_event_count"]
    assert summary["rollback_authority_count"] == summary["long_horizon_study_count"]
    assert summary["task_family_count"] >= 8
    assert summary["decision_type_count"] >= 8
    assert policy["claim_boundary"] == POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY
    assert set(POST_DP_GUARDIAN_MEMORY_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/post-dp-guardian-learning-memory-gap-closure" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]


def test_post_dp_memory_ablations_explain_learning_caused_decisions():
    contract = build_post_dp_guardian_memory_contract()
    summary = contract["summary"]
    ablations = contract["memory_behavior_ablations"]

    assert summary["ablation_count"] == 8
    assert summary["counterfactual_count"] == summary["ablation_count"]
    assert summary["memory_changed_behavior_count"] == summary["ablation_count"]
    assert summary["operator_decision_explanation_count"] == summary["ablation_count"]
    assert summary["unsafe_or_stale_change_blocked_count"] >= 2
    assert {
        "act",
        "defer",
        "bundle",
        "clarify",
        "approval",
        "stay_silent",
        "recovery",
        "followthrough",
    } <= {item["decision"] for item in ablations}
    assert all(item["guardian_learning_caused"] is True for item in ablations)
    assert all(item["operator_receipt_explains_decision"] is True for item in ablations)
    assert all(item["approval_scope_preserved"] is True for item in ablations)


def test_post_dp_provider_operations_expose_delete_export_stale_decay_and_quarantine():
    contract = build_post_dp_guardian_memory_contract()
    summary = contract["summary"]
    providers = contract["memory_provider_operations"]

    assert summary["provider_count"] >= 8
    assert summary["provider_state_count"] >= 7
    assert summary["canonical_precedence_preserved_count"] == summary["provider_count"]
    assert summary["provider_override_blocked_count"] >= 7
    assert summary["delete_export_propagated_count"] == summary["provider_count"]
    assert summary["stale_evidence_decay_count"] >= 4
    assert summary["quarantine_count"] >= 4
    assert summary["reinstatement_review_count"] >= 4
    assert summary["provider_drift_detected_count"] >= 3
    assert {
        "healthy",
        "degraded",
        "stale",
        "conflicting",
        "privacy_limited",
        "quarantined",
        "review_for_reinstatement",
    } <= {item["provider_runtime_state"] for item in providers}


def test_post_dp_safety_and_redaction_keep_learning_reversible_and_private():
    contract = build_post_dp_guardian_memory_contract()
    summary = contract["summary"]
    receipts = [
        item["safe_receipt"]
        for group in (
            contract["long_horizon_learning_quality"],
            contract["memory_behavior_ablations"],
            contract["memory_provider_operations"],
            contract["learning_safety_regressions"],
            contract["false_claim_scans"],
        )
        for item in group
    ]

    assert summary["negative_case_count"] >= 10
    assert summary["negative_case_detected_count"] == summary["negative_case_count"]
    assert summary["rollback_or_quarantine_count"] == summary["negative_case_count"]
    assert summary["false_claim_scan_count"] >= 1
    assert summary["false_claim_hit_count"] == 0
    assert contract["false_claim_scans"][0]["exit_status"] == 0
    assert contract["false_claim_scans"][0]["scanned_paths"]
    assert contract["false_claim_scans"][0]["forbidden_terms"]
    assert contract["false_claim_scans"][0]["stdout_digest"]
    assert contract["false_claim_scans"][0]["stderr_digest"]
    assert summary["secret_leak_count"] == 0
    assert summary["unredacted_identifier_count"] == 0
    assert summary["provider_payload_leak_count"] == 0
    assert summary["raw_receipt_path_exposed_count"] == 0
    assert all(receipt["redaction_boundary"] == POST_DP_GUARDIAN_MEMORY_REDACTION_BOUNDARY for receipt in receipts)
    assert all(receipt["contains_raw_transcript"] is False for receipt in receipts)
    assert all(receipt["contains_secret"] is False for receipt in receipts)
    assert all(receipt["contains_personal_identifier"] is False for receipt in receipts)
    assert all(receipt["contains_provider_payload"] is False for receipt in receipts)
    assert all(receipt["raw_receipt_path_exposed"] is False for receipt in receipts)


def test_post_dp_guardian_memory_report_runs_all_dt_suites():
    report = asyncio.run(build_post_dp_guardian_memory_report())

    assert report["summary"]["benchmark_posture"] == "post_dp_guardian_learning_memory_ci_gated_operator_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["latest_run"]["failed"] == 0
    assert report["scenario_names"][POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SUITE_NAME] == list(
        POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SCENARIO_NAMES
    )
    assert report["scenario_names"][LONG_HORIZON_LEARNING_QUALITY_V2_SUITE_NAME] == list(
        LONG_HORIZON_LEARNING_QUALITY_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][MEMORY_BEHAVIOR_ABLATION_V2_SUITE_NAME] == list(
        MEMORY_BEHAVIOR_ABLATION_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][MEMORY_PROVIDER_OPERATION_V2_SUITE_NAME] == list(
        MEMORY_PROVIDER_OPERATION_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][LEARNING_SAFETY_REGRESSION_V2_SUITE_NAME] == list(
        LEARNING_SAFETY_REGRESSION_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SUITE_NAME] == list(
        GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES
    )
