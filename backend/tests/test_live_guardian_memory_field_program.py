import asyncio

from src.guardian.live_guardian_memory_field_program import (
    LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_BLOCKED_CLAIMS,
    LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_CLAIM_BOUNDARY,
    build_live_guardian_memory_field_program_contract,
    build_live_guardian_memory_field_program_report,
)


def test_live_field_program_pre_registers_long_horizon_cohorts():
    contract = build_live_guardian_memory_field_program_contract()
    summary = contract["summary"]
    field_studies = contract["field_studies"]

    assert summary["field_study_count"] >= 6
    assert summary["pre_registered_count"] == summary["field_study_count"]
    assert summary["withdrawal_supported_count"] == summary["field_study_count"]
    assert summary["anonymized_count"] == summary["field_study_count"]
    assert summary["independent_evaluator_count"] == summary["field_study_count"]
    assert summary["adverse_event_reviewed_count"] == summary["adverse_event_count"]
    assert summary["rollback_authority_count"] == summary["field_study_count"]
    assert summary["live_or_recorded_window_count"] >= 5
    assert summary["fixture_marked_count"] >= 1
    assert {
        "engineering_long_work",
        "recurring_obligation",
        "collaborator_project_drift",
        "routine_interruptions",
        "source_review_report_followthrough",
        "cross_surface_continuity",
    } <= {item["task_family"] for item in field_studies}


def test_memory_behavior_ablations_show_memory_changed_behavior_against_counterfactuals():
    contract = build_live_guardian_memory_field_program_contract()
    summary = contract["summary"]
    ablations = contract["memory_behavior_ablations"]

    assert summary["ablation_count"] == 8
    assert summary["decision_type_count"] == 8
    assert summary["counterfactual_count"] == summary["ablation_count"]
    assert summary["memory_changed_behavior_count"] == summary["ablation_count"]
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
    assert all(item["timing"] and item["channel"] for item in ablations)


def test_live_memory_provider_operations_preserve_canonical_authority():
    contract = build_live_guardian_memory_field_program_contract()
    summary = contract["summary"]
    providers = contract["memory_provider_operations"]

    assert summary["provider_count"] >= 8
    assert summary["canonical_precedence_preserved_count"] == summary["provider_count"]
    assert summary["provider_override_blocked_count"] >= 6
    assert summary["privacy_regression_count"] >= 2
    assert summary["delete_export_propagated_count"] == summary["provider_count"]
    assert summary["quarantine_count"] >= 4
    assert summary["reinstatement_review_count"] >= 3
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
    assert any(item["provider_role"] == "canonical" for item in providers)


def test_safety_monitor_and_receipts_cover_negative_cases_without_leaks():
    contract = build_live_guardian_memory_field_program_contract()
    summary = contract["summary"]
    safety = contract["safety_monitor"]
    receipts = [
        item["safe_receipt"]
        for group in (
            contract["field_studies"],
            contract["memory_behavior_ablations"],
            contract["memory_provider_operations"],
            contract["independent_candidate_reviews"],
            contract["safety_monitor"],
            contract["false_claim_scans"],
        )
        for item in group
    ]

    assert summary["negative_case_count"] >= 10
    assert summary["negative_case_detected_count"] == summary["negative_case_count"]
    assert summary["rollback_or_quarantine_count"] == summary["negative_case_count"]
    assert {
        "stale_recall",
        "over_personalization",
        "noisy_provider_evidence",
        "false_confidence",
        "privacy_regression",
        "unsafe_intervention",
        "hallucinated_obligation",
        "provider_drift",
        "conflicting_project_anchors",
        "ignored_correction",
    } <= {item["negative_case"] for item in safety}
    assert summary["raw_transcript_stored_count"] == 0
    assert summary["secret_leak_count"] == 0
    assert summary["unredacted_identifier_count"] == 0
    assert summary["provider_payload_leak_count"] == 0
    assert summary["raw_receipt_path_exposed_count"] == 0
    assert all(receipt["contains_raw_transcript"] is False for receipt in receipts)
    assert all(receipt["contains_secret"] is False for receipt in receipts)
    assert all(receipt["contains_provider_payload"] is False for receipt in receipts)
    assert all(receipt["raw_receipt_path_exposed"] is False for receipt in receipts)


def test_live_guardian_memory_policy_keeps_completion_claims_blocked():
    contract = build_live_guardian_memory_field_program_contract()
    policy = contract["policy"]

    assert policy["claim_boundary"] == LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_CLAIM_BOUNDARY
    assert set(LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "full_memory_provider_parity" in policy["blocked_claims"]
    assert "solved_long_term_learning" in policy["blocked_claims"]
    assert "full_production_parity" in policy["blocked_claims"]
    assert "/api/operator/live-guardian-memory-field-program" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]


def test_live_guardian_memory_report_exposes_ci_gated_posture():
    report = asyncio.run(build_live_guardian_memory_field_program_report())

    assert report["summary"]["operator_status"] == "live_guardian_memory_field_program_receipts_visible"
    assert report["summary"]["benchmark_posture"] == "live_guardian_memory_field_program_ci_gated_operator_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["scenario_count"] == 20
    assert report["latest_run"]["failed"] == 0
