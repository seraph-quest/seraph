"""Tests for Batch CW operator mission-control population receipts."""

import asyncio
import json

from src.cockpit.operator_mission_control import (
    LONG_WORK_DEBUGGING_SLO_SCENARIO_NAMES,
    LONG_WORK_DEBUGGING_SLO_SUITE_NAME,
    NAMED_BASELINE_COCKPIT_COMPARISON_SCENARIO_NAMES,
    NAMED_BASELINE_COCKPIT_COMPARISON_SUITE_NAME,
    OPERATOR_CONTROL_POPULATION_STUDY_SCENARIO_NAMES,
    OPERATOR_CONTROL_POPULATION_STUDY_SUITE_NAME,
    OPERATOR_MISSION_CONTROL_BLOCKED_CLAIMS,
    OPERATOR_MISSION_CONTROL_CLAIM_BOUNDARY,
    build_operator_mission_control_contract,
    build_operator_mission_control_report,
)


def test_operator_mission_control_contract_exposes_population_baseline_and_slo_receipts():
    contract = build_operator_mission_control_contract()
    summary = contract["summary"]

    assert summary["operator_status"] == "operator_mission_control_population_receipts_visible"
    assert summary["workbench_surface_count"] >= 4
    assert summary["population_study_count"] >= 4
    assert summary["baseline_comparison_count"] >= 3
    assert summary["debugging_slo_count"] >= 5
    assert summary["population_operator_count"] >= 60
    assert summary["recovery_success_floor_met"] is True
    assert summary["all_slos_met"] is True
    assert summary["named_baselines_pressure_only"] is True
    assert summary["safe_receipts_redacted"] is True


def test_operator_mission_control_contract_blocks_unsafe_claims_and_approval_reuse():
    contract = build_operator_mission_control_contract()
    policy = contract["policy"]

    assert policy["claim_boundary"] == OPERATOR_MISSION_CONTROL_CLAIM_BOUNDARY
    assert set(OPERATOR_MISSION_CONTROL_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "approval_transfer" in policy["blocked_claims"]
    assert "solved_operator_control" in policy["not_claimed"]
    handoff = next(
        item for item in contract["workbench_receipts"] if item["surface"] == "multi_operator_handoff"
    )
    assert "approval_transfer" not in handoff["operator_capabilities"]
    assert handoff["handoff_authority_policy"]["approval_reuse_allowed"] is False
    assert handoff["handoff_authority_policy"]["receiver_scope_renewal_required"] is True
    assert handoff["handoff_authority_policy"]["checkpoint_fingerprint_match_required"] is True


def test_operator_mission_control_receipts_are_redacted_handles_not_private_paths():
    contract = build_operator_mission_control_contract()
    payload = json.dumps(contract, sort_keys=True)

    assert "/Users/" not in payload
    assert "file://" not in payload
    assert ".env" not in payload
    assert "id_rsa" not in payload
    assert "secret://" not in payload
    assert "raw_receipt_location" not in payload
    for receipt_group in (
        contract["workbench_receipts"],
        contract["population_study_receipts"],
        contract["named_baseline_comparisons"],
        contract["debugging_slo_receipts"],
    ):
        assert all(item["safe_receipt"]["contains_secret"] is False for item in receipt_group)
        assert all(item["safe_receipt"]["contains_private_path"] is False for item in receipt_group)
        assert all(item["safe_receipt"]["raw_receipt_path_exposed"] is False for item in receipt_group)
        assert all(item["safe_receipt"]["workspace_dir_exposed"] is False for item in receipt_group)
        assert all(item["safe_receipt"]["package_path_exposed"] is False for item in receipt_group)


def test_operator_mission_control_report_runs_cw_suites():
    payload = asyncio.run(build_operator_mission_control_report())

    assert payload["summary"]["benchmark_posture"] == "operator_mission_control_population_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(OPERATOR_CONTROL_POPULATION_STUDY_SCENARIO_NAMES)
        + len(NAMED_BASELINE_COCKPIT_COMPARISON_SCENARIO_NAMES)
        + len(LONG_WORK_DEBUGGING_SLO_SCENARIO_NAMES)
    )
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"][OPERATOR_CONTROL_POPULATION_STUDY_SUITE_NAME] == list(
        OPERATOR_CONTROL_POPULATION_STUDY_SCENARIO_NAMES
    )
    assert payload["scenario_names"][NAMED_BASELINE_COCKPIT_COMPARISON_SUITE_NAME] == list(
        NAMED_BASELINE_COCKPIT_COMPARISON_SCENARIO_NAMES
    )
    assert payload["scenario_names"][LONG_WORK_DEBUGGING_SLO_SUITE_NAME] == list(
        LONG_WORK_DEBUGGING_SLO_SCENARIO_NAMES
    )
