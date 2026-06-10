"""Tests for Batch CN dense long-work operator debugging and recovery receipts."""

import asyncio
from hashlib import sha256
from pathlib import Path

from src.cockpit.dense_operator_recovery import (
    DENSE_OPERATOR_RECOVERY_BLOCKED_CLAIMS,
    DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY,
    build_dense_operator_recovery_contract,
    build_dense_operator_recovery_report,
)


def test_dense_operator_recovery_contract_exposes_required_control_matrix():
    contract = build_dense_operator_recovery_contract()
    summary = contract["summary"]
    controls = contract["control_density_receipts"]
    task_matrix = contract["operator_task_matrix"]
    controls_by_action = {item["action"]: item for item in controls}

    assert summary["operator_status"] == "dense_operator_recovery_control_receipts_visible"
    assert summary["required_controls_visible"] is True
    assert summary["control_action_count"] >= 11
    assert summary["task_matrix_count"] >= 8
    assert summary["operator_task_matrix_complete"] is True
    assert {
        "pause",
        "resume",
        "retry",
        "repair",
        "branch",
        "compare",
        "revoke",
        "quarantine",
        "handoff",
        "rollback",
        "audit",
    } <= {item["action"] for item in controls}
    assert controls_by_action["revoke"]["revocation_boundary"] == "approval_token_capability_session_and_pending_replay"
    assert (
        controls_by_action["quarantine"]["quarantine_release_condition"]
        == "independent_review_plus_hash_match_plus_no_privacy_regression"
    )
    assert controls_by_action["rollback"]["rollback_restore_point"] == "pre_mutation_checkpoint_or_package_version_with_hash"
    assert all(item["approval_scope"] for item in controls)
    assert all(item["recovery_correctness_check"] for item in controls)
    assert {
        "diagnose_failed_long_workflow",
        "identify_unsafe_approval_drift",
        "compare_branch_outputs",
        "recover_delegated_artifact_handoff",
        "revoke_or_quarantine_unsafe_action",
        "resume_after_interruption",
        "inspect_cross_batch_residual_risk",
        "hand_off_to_another_operator",
    } <= {item["task"] for item in task_matrix}
    assert all(item["raw_receipt_location"] for item in task_matrix)


def test_dense_operator_recovery_contract_exposes_debugging_and_recovery_correctness():
    contract = build_dense_operator_recovery_contract()
    summary = contract["summary"]
    debugging = contract["debugging_receipts"]

    assert summary["debugging_receipt_count"] >= 4
    assert summary["recovery_correctness_count"] >= 2
    assert summary["cross_batch_recovery_view_visible"] is True
    assert any(item["operator_task"] == "compare_branch_outputs" for item in debugging)
    assert any(item["operator_task"] == "resume_after_interruption" for item in debugging)
    assert any(
        item["operator_task"] == "inspect_cross_batch_residual_risk"
        and "production_sla_orchestration" in item["cross_batch_receipts"]
        and "independent_learning_memory_parity" in item["cross_batch_receipts"]
        for item in debugging
    )
    assert all(item["failure_budget"] for item in debugging)
    assert all(item["residual_gap"] for item in debugging)


def test_dense_operator_recovery_contract_exposes_independent_usability_accessibility_receipts():
    contract = build_dense_operator_recovery_contract()
    summary = contract["summary"]
    usability = contract["independent_usability_accessibility_receipts"]

    assert summary["independent_usability_receipt_count"] == 3
    assert summary["keyboard_path_count"] == len(usability)
    assert summary["accessibility_blocker_count"] >= 1
    assert all(item["reviewer_independence"] for item in usability)
    assert all(item["sample_size"] >= 10 for item in usability)
    assert all(item["raw_receipt_location"] for item in usability)
    assert all(item["residual_gap"] for item in usability)


def test_dense_operator_recovery_contract_verifies_raw_receipt_integrity_manifest():
    contract = build_dense_operator_recovery_contract()
    repo_root = Path(__file__).resolve().parents[2]
    all_receipts = [
        *contract["debugging_receipts"],
        *contract["operator_task_matrix"],
        *contract["independent_usability_accessibility_receipts"],
    ]
    manifest = contract["receipt_integrity_manifest"]
    manifest_by_location = {item["raw_receipt_location"]: item for item in manifest}

    assert contract["summary"]["receipt_integrity_manifest_count"] == len(all_receipts)
    assert contract["summary"]["receipt_integrity_verified_count"] == len(all_receipts)
    assert {item["raw_receipt_location"] for item in all_receipts} == set(manifest_by_location)
    assert all(item["fixture_artifact_declared"] is True for item in manifest)
    assert all(item["artifact_exists"] is True for item in manifest)
    assert all(item["verified"] is True for item in manifest)
    assert all(item["outcome_verified"] is True for item in manifest)
    assert all(item["metadata_matches_receipt"] is True for item in manifest)
    assert all(len(item["content_sha256"]) == 64 for item in manifest)
    assert all(
        sha256((repo_root / item["raw_receipt_location"]).read_bytes()).hexdigest()
        == item["content_sha256"]
        for item in manifest
    )
    assert all(item["reviewer_attestation"] for item in manifest)


def test_dense_operator_recovery_contract_preserves_claim_boundaries():
    contract = build_dense_operator_recovery_contract()
    policy = contract["policy"]

    assert policy["claim_boundary"] == DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY
    assert set(DENSE_OPERATOR_RECOVERY_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "best_or_world_class_cockpit" in policy["not_claimed"]
    assert "solved_operator_control" in policy["not_claimed"]
    assert "production_ready_product" in policy["not_claimed"]
    assert "/api/operator/dense-operator-recovery-control" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]


def test_dense_operator_recovery_report_exposes_ci_gated_posture():
    report = asyncio.run(build_dense_operator_recovery_report())

    assert report["summary"]["benchmark_posture"] == "dense_operator_recovery_control_ci_gated_operator_visible"
    assert report["summary"]["scenario_count"] == 15
    assert report["summary"]["active_failure_count"] == 0
    assert report["latest_run"]["failed"] == 0
    assert report["policy"]["claim_boundary"] == DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY
