"""Tests for Batch CV longitudinal guardian-learning outcome operations."""

import asyncio

from src.guardian.longitudinal_guardian_outcomes import (
    LEARNING_SAFETY_MONITOR_V2_SCENARIO_NAMES,
    LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SCENARIO_NAMES,
    LONGITUDINAL_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS,
    LONGITUDINAL_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY,
    NAMED_BASELINE_MEMORY_COMPARISON_SCENARIO_NAMES,
    build_longitudinal_guardian_outcomes_contract,
    build_longitudinal_guardian_outcomes_report,
)


def _string_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value


def test_longitudinal_guardian_outcome_study_requires_windows_baselines_power_and_evaluator_independence():
    contract = build_longitudinal_guardian_outcomes_contract()
    summary = contract["summary"]
    studies = contract["longitudinal_outcome_studies"]

    assert summary["operator_status"] == "longitudinal_guardian_outcomes_receipts_visible"
    assert summary["study_count"] == 3
    assert summary["longitudinal_window_count"] == summary["study_count"]
    assert summary["sample_size_total"] >= 250
    assert summary["independent_evaluator_count"] == summary["study_count"]
    assert all(item["window_days"] >= 60 for item in studies)
    assert all(item["baseline"]["baseline_name"] for item in studies)
    assert all(item["baseline"]["baseline_version"] for item in studies)
    assert all(item["power_rationale"] for item in studies)
    assert all(item["evaluator"]["implementation_independent"] is True for item in studies)
    assert all(item["evaluator"]["protocol_id"] for item in studies)


def test_longitudinal_guardian_outcome_study_withdrawal_rolls_back_or_reweights_learning():
    contract = build_longitudinal_guardian_outcomes_contract()
    summary = contract["summary"]
    studies = contract["longitudinal_outcome_studies"]

    assert summary["withdrawal_supported_count"] == summary["study_count"]
    assert summary["withdrawal_reweighted_count"] >= 1
    withdrawn = next(item for item in studies if item["consent"]["withdrawal_count"] > 0)
    assert withdrawn["consent"]["withdrawal_supported"] is True
    assert "reweighted" in withdrawn["consent"]["withdrawal_disposition"]
    assert withdrawn["learning_change"]["rollback_receipt_id"].startswith("rollback:")


def test_learning_safety_monitor_blocks_policy_promotion_on_harm_or_privacy_regression():
    contract = build_longitudinal_guardian_outcomes_contract()
    summary = contract["summary"]
    monitors = contract["learning_safety_monitors"]

    assert summary["adverse_event_reviewed_count"] == summary["adverse_event_count"]
    assert summary["privacy_regression_count"] >= 1
    assert any(item["promotion_state"] == "blocked_by_harm_review" for item in monitors)
    assert any(item["promotion_state"] == "blocked_by_privacy_regression" for item in monitors)
    assert any(
        item.get("privacy_regression_detected") is True
        and item.get("behavior_change_allowed") is False
        for item in monitors
    )


def test_learning_safety_monitor_records_policy_version_and_rollback_receipts():
    contract = build_longitudinal_guardian_outcomes_contract()
    summary = contract["summary"]
    monitors = contract["learning_safety_monitors"]

    assert summary["policy_version_count"] >= 3
    assert summary["rollback_receipt_count"] >= summary["study_count"]
    assert all(
        item.get("rollback_receipt_id")
        for item in monitors
        if item.get("learning_policy_version_after")
    )


def test_named_baseline_memory_comparison_is_pressure_evidence_not_superiority():
    contract = build_longitudinal_guardian_outcomes_contract()
    policy = contract["policy"]
    baselines = contract["named_baseline_memory_comparisons"]

    assert policy["claim_boundary"] == LONGITUDINAL_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY
    assert set(LONGITUDINAL_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "named_baseline_win" in policy["not_claimed"]
    assert any("pressure_evidence_only" in item["comparison_disposition"] for item in baselines)
    assert "memory_superiority" in policy["blocked_claims"]
    assert "full_memory_provider_parity" in policy["blocked_claims"]
    assert all(item["behavior_change_scope"] for item in baselines)
    assert all("win" not in item["behavior_change_scope"] for item in baselines)
    assert all("parity" not in item["behavior_change_scope"] for item in baselines)


def test_named_baseline_memory_comparison_requires_baseline_source_version_and_limitations():
    contract = build_longitudinal_guardian_outcomes_contract()
    baselines = contract["named_baseline_memory_comparisons"]

    assert len(baselines) >= 3
    assert all(item["baseline_name"] for item in baselines)
    assert all(item["baseline_version"] for item in baselines)
    assert all(item["baseline_source"] for item in baselines)
    assert all(item["baseline_window"] for item in baselines)
    assert all(item["baseline_limitations"] for item in baselines)


def test_memory_provider_longitudinal_monitor_quarantines_stale_or_privacy_regressed_provider():
    contract = build_longitudinal_guardian_outcomes_contract()
    summary = contract["summary"]
    monitors = contract["learning_safety_monitors"]

    assert summary["quarantine_count"] >= 2
    assert summary["stale_behavior_change_blocked_count"] >= 1
    assert any(
        item.get("quarantine_state") == "quarantined"
        and item.get("privacy_regression_detected") is True
        and item.get("behavior_change_allowed") is False
        for item in monitors
    )


def test_memory_provider_reinstatement_requires_operator_review_and_no_behavior_change_before_review():
    contract = build_longitudinal_guardian_outcomes_contract()
    monitors = contract["learning_safety_monitors"]

    reinstatement = next(item for item in monitors if item.get("quarantine_state") == "review_for_reinstatement")
    assert reinstatement["reinstatement_review_receipt_id"].startswith("reinstatement-review:")
    assert reinstatement["behavior_change_allowed"] is False
    assert reinstatement["lost_capability_visible"] is True


def test_delete_export_mismatch_blocks_provider_promotion():
    contract = build_longitudinal_guardian_outcomes_contract()
    summary = contract["summary"]
    baselines = contract["named_baseline_memory_comparisons"]

    assert summary["delete_export_mismatch_count"] >= 1
    assert any(
        item["delete_export_mismatch_count"] > 0
        and item["behavior_change_allowed"] is False
        for item in baselines
    )


def test_provider_receipts_redact_config_secret_identifier_and_provider_echo():
    contract = build_longitudinal_guardian_outcomes_contract()
    receipts = [
        item["safe_receipt"]
        for item in [
            *contract["longitudinal_outcome_studies"],
            *contract["named_baseline_memory_comparisons"],
            *contract["learning_safety_monitors"],
        ]
    ]

    assert receipts
    assert all(
        "raw_receipt_location" not in item
        for item in contract["longitudinal_outcome_studies"]
    )
    assert all(item["contains_raw_transcript"] is False for item in receipts)
    assert all(item["contains_secret"] is False for item in receipts)
    assert all(item["contains_personal_identifier"] is False for item in receipts)
    assert all(item["contains_provider_payload"] is False for item in receipts)
    assert all(item["raw_receipt_path_exposed"] is False for item in receipts)
    exposed_strings = list(_string_values(contract))
    assert all("/Users/" not in item and "file://" not in item for item in exposed_strings)
    assert all("raw_receipt_location" not in item for item in exposed_strings)
    assert all("provider_payload:" not in item for item in exposed_strings)


def test_longitudinal_guardian_outcomes_report_exposes_ci_gated_posture():
    report = asyncio.run(build_longitudinal_guardian_outcomes_report())

    assert report["summary"]["benchmark_posture"] == "longitudinal_guardian_outcomes_ci_gated_operator_visible"
    assert report["summary"]["scenario_count"] == (
        len(LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SCENARIO_NAMES)
        + len(NAMED_BASELINE_MEMORY_COMPARISON_SCENARIO_NAMES)
        + len(LEARNING_SAFETY_MONITOR_V2_SCENARIO_NAMES)
    )
    assert report["latest_run"]["failed"] == 0
    assert report["policy"]["claim_boundary"] == LONGITUDINAL_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY
