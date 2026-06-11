"""Tests for Batch DE bounded operator-control certification receipts."""

import asyncio
import json

from src.cockpit.operator_control_certification import (
    LONG_WORK_RECOVERY_SLO_V2_SCENARIO_NAMES,
    LONG_WORK_RECOVERY_SLO_V2_SUITE_NAME,
    MISSION_CONTROL_POPULATION_STUDY_V2_SCENARIO_NAMES,
    MISSION_CONTROL_POPULATION_STUDY_V2_SUITE_NAME,
    OPERATOR_CONTROL_CERTIFICATION_BLOCKED_CLAIMS,
    OPERATOR_CONTROL_CERTIFICATION_CLAIM_BOUNDARY,
    OPERATOR_CONTROL_CERTIFICATION_V1_SCENARIO_NAMES,
    OPERATOR_CONTROL_CERTIFICATION_V1_SUITE_NAME,
    OPERATOR_ERROR_DETECTABILITY_V1_SCENARIO_NAMES,
    OPERATOR_ERROR_DETECTABILITY_V1_SUITE_NAME,
    REQUIRED_OPERATOR_CONTROL_ACTIONS,
    build_operator_control_certification_contract,
    build_operator_control_certification_report,
)


def test_operator_control_certification_covers_required_controls_and_boundaries():
    contract = build_operator_control_certification_contract()
    summary = contract["summary"]
    controls = contract["control_certification_receipts"]

    assert summary["operator_status"] == "operator_control_certification_receipts_visible"
    assert summary["claim_boundary"] == OPERATOR_CONTROL_CERTIFICATION_CLAIM_BOUNDARY
    assert summary["control_count"] >= len(REQUIRED_OPERATOR_CONTROL_ACTIONS)
    assert summary["required_controls_visible"] is True
    assert set(REQUIRED_OPERATOR_CONTROL_ACTIONS) <= {item["action"] for item in controls}
    assert all(item["authority_visible"] is True for item in controls)
    assert all(item["receipt_after_action"] for item in controls)
    assert all(item["recovery_correctness_check"] for item in controls)


def test_operator_control_certification_blocks_unsafe_completion_claims():
    contract = build_operator_control_certification_contract()
    policy = contract["policy"]
    summary = contract["summary"]

    assert set(OPERATOR_CONTROL_CERTIFICATION_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    for blocked in (
        "best_cockpit",
        "world_class_cockpit",
        "solved_operator_control",
        "approval_transfer_solved",
        "tamper_proof_audit",
        "formal_certification",
        "production_ready_product",
        "full_parity",
        "reference_systems_exceeded",
    ):
        assert blocked in policy["blocked_claims"]
    assert summary["formal_certification_allowed"] is False
    assert summary["solved_control_claim_allowed"] is False
    assert summary["tamper_proof_audit_claim_allowed"] is False


def test_operator_control_certification_reproves_stale_approval_and_handoff_safety():
    contract = build_operator_control_certification_contract()
    controls = {item["action"]: item for item in contract["control_certification_receipts"]}
    errors = {item["error_class"]: item for item in contract["error_detectability_receipts"]}

    assert controls["approve"]["stale_approval_blocked"] is True
    assert controls["replay"]["stale_approval_blocked"] is True
    assert controls["replay"]["negative_case"] == "approval_context_changed_blocks_replay"
    assert controls["handoff"]["approval_reuse_allowed"] is False

    stale = errors["approval_context_changed"]
    assert stale["stale_approval_blocked"] is True
    assert stale["replay_allowed"] is False
    assert stale["resume_plan"] is None
    assert stale["replay_block_reason"] == "approval_context_changed"


def test_operator_control_certification_population_telemetry_and_slos_are_certification_grade():
    contract = build_operator_control_certification_contract()
    summary = contract["summary"]
    population = contract["population_study_v2_receipts"]
    slos = contract["long_work_recovery_slo_v2_receipts"]
    errors = contract["error_detectability_receipts"]

    assert summary["population_operator_count"] >= 80
    assert summary["telemetry_row_count"] >= 4
    assert summary["population_required_controls_covered"] is True
    assert set(REQUIRED_OPERATOR_CONTROL_ACTIONS) <= set(summary["population_covered_actions"])
    assert all(set(item["covered_actions"]) <= set(REQUIRED_OPERATOR_CONTROL_ACTIONS) for item in population)
    assert all(item["covered_actions"] for item in population)
    assert summary["independent_evaluator_count"] >= 3
    assert summary["keyboard_only_floor_met"] is True
    assert summary["recovery_success_floor_met"] is True
    assert all(item["click_count_p50"] > 0 and item["keystroke_count_p50"] > 0 for item in population)
    assert all(item["latency_ms_p95"] <= 1000 and item["recovery_seconds_p95"] <= 90 for item in population)
    assert all(len(item["telemetry_digest"]) == 64 and item["reviewer_attestation"] for item in population)
    assert summary["all_slos_met"] is True
    assert {"multi_session_workflow", "delegated_artifacts", "background_processes", "branch_families"} <= {
        item["workload"] for item in slos
    }
    assert summary["error_detectability_floor_met"] is True
    assert summary["confidence_calibration_floor_met"] is True
    assert all(item["operator_visible_fields"] for item in errors)


def test_operator_control_certification_baselines_use_source_refresh_receipts():
    contract = build_operator_control_certification_contract()
    summary = contract["summary"]
    baselines = contract["named_baseline_pressure_receipts"]

    assert summary["baseline_source_receipts_linked"] is True
    assert summary["baseline_claim_lift_blocked"] is True
    assert {item["system"] for item in baselines} == {"Hermes", "OpenClaw", "IronClaw"}
    for baseline in baselines:
        receipt = baseline["source_receipt"]
        assert baseline["source_type"] == "post_cq_reference_system_source_refresh_v2"
        assert baseline["source_checked_at"] == "2026-06-11"
        assert baseline["claim_lift_allowed"] is False
        assert baseline["source_recheck_required_before_claim_lift"] is True
        assert receipt["url"].startswith("https://")
        assert receipt["claim_use"] == "source_backed_pressure_only"
        assert receipt["runtime_fetch_performed"] is False
        assert receipt["external_reachability_receipt"]
        assert receipt["access_caveat"]
        assert receipt["competitor_claim_uncertainty"]


def test_operator_control_certification_receipts_are_redacted_not_raw_paths():
    contract = build_operator_control_certification_contract()
    payload = json.dumps(contract, sort_keys=True)

    assert "/Users/" not in payload
    assert "file://" not in payload
    assert ".env" not in payload
    assert "id_rsa" not in payload
    assert "secret://" not in payload
    assert "raw_receipt_location" not in payload
    assert "tamper-proof audit achieved" not in payload
    for receipt_group in (
        contract["control_certification_receipts"],
        contract["population_study_v2_receipts"],
        contract["long_work_recovery_slo_v2_receipts"],
        contract["error_detectability_receipts"],
        contract["named_baseline_pressure_receipts"],
    ):
        for item in receipt_group:
            receipt = item["safe_receipt"]
            assert receipt["contains_secret"] is False
            assert receipt["contains_private_path"] is False
            assert receipt["contains_raw_transcript"] is False
            assert receipt["contains_unredacted_operator_identifier"] is False
            assert receipt["raw_receipt_path_exposed"] is False
            assert receipt["workspace_dir_exposed"] is False
            assert receipt["package_path_exposed"] is False
            assert receipt["redaction_layer"] == "operator_control_certification_v1"
            assert len(receipt["tamper_evident_digest"]) == 64


def test_operator_control_certification_report_runs_de_suites():
    payload = asyncio.run(build_operator_control_certification_report())

    assert payload["summary"]["benchmark_posture"] == "operator_control_certification_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(OPERATOR_CONTROL_CERTIFICATION_V1_SCENARIO_NAMES)
        + len(MISSION_CONTROL_POPULATION_STUDY_V2_SCENARIO_NAMES)
        + len(LONG_WORK_RECOVERY_SLO_V2_SCENARIO_NAMES)
        + len(OPERATOR_ERROR_DETECTABILITY_V1_SCENARIO_NAMES)
    )
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"][OPERATOR_CONTROL_CERTIFICATION_V1_SUITE_NAME] == list(
        OPERATOR_CONTROL_CERTIFICATION_V1_SCENARIO_NAMES
    )
    assert payload["scenario_names"][MISSION_CONTROL_POPULATION_STUDY_V2_SUITE_NAME] == list(
        MISSION_CONTROL_POPULATION_STUDY_V2_SCENARIO_NAMES
    )
    assert payload["scenario_names"][LONG_WORK_RECOVERY_SLO_V2_SUITE_NAME] == list(
        LONG_WORK_RECOVERY_SLO_V2_SCENARIO_NAMES
    )
    assert payload["scenario_names"][OPERATOR_ERROR_DETECTABILITY_V1_SUITE_NAME] == list(
        OPERATOR_ERROR_DETECTABILITY_V1_SCENARIO_NAMES
    )
