"""Tests for Batch DM operator-control production-certification receipts."""

import asyncio
import json

from src.cockpit.operator_control_production_certification import (
    AUTHORITY_TRANSFER_RECOVERY_V1_SCENARIO_NAMES,
    AUTHORITY_TRANSFER_RECOVERY_V1_SUITE_NAME,
    OPERATOR_CONTROL_CERTIFICATION_V2_SCENARIO_NAMES,
    OPERATOR_CONTROL_CERTIFICATION_V2_SUITE_NAME,
    OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES,
    OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
    OPERATOR_CONTROL_LIVE_POPULATION_V1_SCENARIO_NAMES,
    OPERATOR_CONTROL_LIVE_POPULATION_V1_SUITE_NAME,
    OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_BLOCKED_CLAIMS,
    OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_CLAIM_BOUNDARY,
    REQUIRED_DM_OPERATOR_CONTROLS,
    TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SCENARIO_NAMES,
    TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SUITE_NAME,
    build_operator_control_production_certification_contract,
    build_operator_control_production_certification_report,
)


def test_operator_control_production_certification_covers_required_control_matrix():
    contract = build_operator_control_production_certification_contract()
    summary = contract["summary"]
    controls = contract["operator_control_v2_receipts"]

    assert summary["operator_status"] == "operator_control_production_certification_receipts_visible"
    assert summary["claim_boundary"] == OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_CLAIM_BOUNDARY
    assert summary["required_controls_visible"] is True
    assert set(REQUIRED_DM_OPERATOR_CONTROLS) <= {item["action"] for item in controls}
    assert summary["stale_approval_block_count"] >= 3
    assert summary["safe_denial_visible"] is True
    assert all(item["authority_visible"] is True for item in controls)
    assert all(item["receipt_after_action"] for item in controls)
    assert all(item["recovery_correctness_check"] for item in controls)


def test_operator_control_production_certification_population_and_authority_receipts():
    contract = build_operator_control_production_certification_contract()
    summary = contract["summary"]
    population = contract["operator_live_population_receipts"]
    authority = contract["authority_transfer_recovery_receipts"]

    assert summary["population_operator_count"] >= 100
    assert summary["population_required_controls_covered"] is True
    assert summary["keyboard_accessibility_floor_met"] is True
    assert summary["latency_floor_met"] is True
    assert summary["recovery_floor_met"] is True
    assert summary["error_detectability_floor_met"] is True
    assert all(item["fixture_vs_live_marker"] for item in population)
    assert all(item["evaluator_independence"].endswith("_not_implementation_worker") for item in population)
    assert all(len(item["telemetry_digest"]) == 64 for item in population)
    assert {item["transfer_type"] for item in authority} >= {
        "handoff",
        "takeover",
        "replay",
        "rollback",
        "quarantine",
        "revoke",
    }
    assert all(item["scope_renewal_required"] is True for item in authority)
    assert all(item["approval_reuse_allowed"] is False for item in authority)


def test_operator_control_production_certification_audit_candidate_is_redacted_and_bounded():
    contract = build_operator_control_production_certification_contract()
    summary = contract["summary"]
    audit = contract["tamper_evident_audit_candidate_receipts"]
    payload = json.dumps(contract, sort_keys=True)

    assert summary["tamper_evident_audit_candidate_count"] >= 5
    assert summary["audit_digest_chain_linked"] is True
    assert summary["audit_mutation_denial_visible"] is True
    assert summary["tamper_proof_audit_claim_allowed"] is False
    assert "/Users/" not in payload
    assert "file://" not in payload
    assert ".env" not in payload
    assert "secret://" not in payload
    assert all(item["redacted_handle"].startswith("audit-handle:dm:") for item in audit)
    assert all(len(item["digest"]) == 64 for item in audit)
    assert all("not_tamper_proof" in item["residual_risk"] for item in audit)


def test_operator_control_production_certification_blocks_unsupported_claims_and_scans():
    contract = build_operator_control_production_certification_contract()
    summary = contract["summary"]
    policy = contract["policy"]
    false_claims = contract["false_claim_scan_receipts"]

    assert set(OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    for blocked in (
        "solved_operator_control",
        "best_cockpit",
        "world_class_cockpit",
        "tamper_proof_audit",
        "formal_certification",
        "production_ready_product",
        "full_parity",
        "reference_systems_exceeded",
    ):
        assert blocked in policy["blocked_claims"]
    assert summary["formal_certification_allowed"] is False
    assert summary["solved_control_claim_allowed"] is False
    assert summary["best_world_class_cockpit_claim_allowed"] is False
    assert summary["false_claim_scan_clean"] is True
    assert false_claims[0]["command"] == "python3 scripts/check_strategy_claims.py"
    assert false_claims[0]["forbidden_hit_count"] == 0


def test_operator_control_production_certification_safe_receipts_are_redacted():
    contract = build_operator_control_production_certification_contract()
    all_groups = (
        contract["operator_control_v2_receipts"],
        contract["operator_live_population_receipts"],
        contract["tamper_evident_audit_candidate_receipts"],
        contract["authority_transfer_recovery_receipts"],
        contract["false_claim_scan_receipts"],
    )

    assert contract["summary"]["safe_receipts_redacted"] is True
    for group in all_groups:
        for item in group:
            receipt = item["safe_receipt"]
            assert receipt["contains_secret"] is False
            assert receipt["contains_private_path"] is False
            assert receipt["contains_raw_transcript"] is False
            assert receipt["contains_unredacted_operator_identifier"] is False
            assert receipt["raw_receipt_path_exposed"] is False
            assert receipt["redaction_layer"] == "operator_control_production_certification_v1"
            assert len(receipt["tamper_evident_digest"]) == 64


def test_operator_control_production_certification_report_runs_dm_suites():
    report = asyncio.run(build_operator_control_production_certification_report())

    assert (
        report["summary"]["benchmark_posture"]
        == "operator_control_production_certification_ci_gated_operator_visible"
    )
    assert report["summary"]["scenario_count"] == (
        len(OPERATOR_CONTROL_CERTIFICATION_V2_SCENARIO_NAMES)
        + len(OPERATOR_CONTROL_LIVE_POPULATION_V1_SCENARIO_NAMES)
        + len(TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SCENARIO_NAMES)
        + len(AUTHORITY_TRANSFER_RECOVERY_V1_SCENARIO_NAMES)
        + len(OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES)
    )
    assert report["latest_run"]["failed"] == 0
    assert report["scenario_names"][OPERATOR_CONTROL_CERTIFICATION_V2_SUITE_NAME] == list(
        OPERATOR_CONTROL_CERTIFICATION_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][OPERATOR_CONTROL_LIVE_POPULATION_V1_SUITE_NAME] == list(
        OPERATOR_CONTROL_LIVE_POPULATION_V1_SCENARIO_NAMES
    )
    assert report["scenario_names"][TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SUITE_NAME] == list(
        TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SCENARIO_NAMES
    )
    assert report["scenario_names"][AUTHORITY_TRANSFER_RECOVERY_V1_SUITE_NAME] == list(
        AUTHORITY_TRANSFER_RECOVERY_V1_SCENARIO_NAMES
    )
    assert report["scenario_names"][OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SUITE_NAME] == list(
        OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES
    )
