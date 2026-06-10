import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.security.production_isolation import (
    PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES,
    PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SUITE_NAME,
    PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES,
    PRODUCTION_ISOLATION_HARDENING_V2_SUITE_NAME,
    PRODUCTION_ISOLATION_SECURITY_BLOCKED_CLAIMS,
    PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
    SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES,
    SECURITY_INCIDENT_RECOVERY_DRILL_SUITE_NAME,
    build_production_isolation_security_contract,
    build_production_isolation_security_report,
)


def test_production_isolation_security_contract_blocks_overclaims():
    contract = build_production_isolation_security_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["suite_name"] == "production_isolation_security"
    assert summary["operator_status"] == "production_isolation_security_receipts_visible"
    assert summary["isolation_receipt_count"] == len(contract["isolation_receipts"]) >= 5
    assert summary["red_team_case_count"] == len(contract["red_team_cases"]) >= 6
    assert summary["incident_drill_count"] == len(contract["incident_drill_receipts"]) >= 6
    assert summary["claim_boundary"] == PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY
    assert set(PRODUCTION_ISOLATION_SECURITY_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "ironclaw_class_secure_execution" in policy["not_claimed"]
    assert "full_host_container_isolation" in policy["not_claimed"]
    assert "/api/operator/production-isolation-hardening" in policy["receipt_surfaces"]


def test_production_isolation_receipts_are_fail_closed_and_bounded():
    contract = build_production_isolation_security_contract()

    assert all(item["fail_closed"] is True for item in contract["isolation_receipts"])
    assert all(item["operator_visible"] is True for item in contract["isolation_receipts"])
    assert {
        "recorded_live_drill",
        "deterministic_contract",
    } <= {item["evidence_mode"] for item in contract["isolation_receipts"] + contract["incident_drill_receipts"]}
    assert all("host_isolation_posture" in item for item in contract["isolation_receipts"])
    assert all(
        "not_container" in item["host_isolation_posture"]
        or "not_full" in item["host_isolation_posture"]
        or "not_external" in item["host_isolation_posture"]
        or "not_supply_chain" in item["host_isolation_posture"]
        or "not_distributed" in item["host_isolation_posture"]
        for item in contract["isolation_receipts"]
    )
    assert all(item["residual_risk"] for item in contract["isolation_receipts"])


def test_privileged_red_team_and_incident_drills_have_recovery_receipts():
    contract = build_production_isolation_security_contract()

    assert all(item["fail_closed"] is True for item in contract["red_team_cases"])
    assert all(item["recovery_actions"] for item in contract["red_team_cases"])
    assert all(item["operator_visible"] is True for item in contract["red_team_cases"])
    assert all(item["replayable"] is True for item in contract["incident_drill_receipts"])
    assert all(item["audit_receipt"] for item in contract["incident_drill_receipts"])
    assert any(item["incident_type"] == "credential_boundary_drift" for item in contract["incident_drill_receipts"])
    assert any(
        item["operator_notification"] == "notify_operator"
        for item in contract["incident_drill_receipts"]
    )


def test_production_isolation_security_report_runs_all_batch_cd_suites():
    scenario_names = [
        *PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES,
        *PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES,
        *SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES,
    ]
    summary = SimpleNamespace(
        total=len(scenario_names),
        passed=len(scenario_names),
        failed=0,
        duration_ms=27,
        results=[SimpleNamespace(name=name, passed=True, error="") for name in scenario_names],
    )

    with patch(
        "src.security.production_isolation._run_production_isolation_security_suites",
        AsyncMock(return_value=summary),
    ):
        payload = asyncio.run(build_production_isolation_security_report())

    assert payload["summary"]["benchmark_posture"] == "production_isolation_security_ci_gated_operator_visible"
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"][PRODUCTION_ISOLATION_HARDENING_V2_SUITE_NAME] == list(
        PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES
    )
    assert payload["scenario_names"][PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SUITE_NAME] == list(
        PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES
    )
    assert payload["scenario_names"][SECURITY_INCIDENT_RECOVERY_DRILL_SUITE_NAME] == list(
        SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES
    )
