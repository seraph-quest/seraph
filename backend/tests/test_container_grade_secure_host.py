"""Tests for Batch CT container-grade secure-host validation receipts."""

import asyncio
from unittest.mock import AsyncMock, patch

from src.security.container_grade_host import (
    CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES,
    CONTAINER_GRADE_SECURE_HOST_BLOCKED_CLAIMS,
    CONTAINER_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
    EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES,
    SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES,
    build_container_grade_secure_host_contract,
    build_container_grade_secure_host_report,
)


def test_container_grade_secure_host_contract_blocks_overclaims():
    contract = build_container_grade_secure_host_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["suite_name"] == "container_grade_secure_host"
    assert summary["operator_status"] == "container_grade_secure_host_validation_visible"
    assert summary["claim_boundary"] == CONTAINER_GRADE_SECURE_HOST_CLAIM_BOUNDARY
    assert summary["missing_hardware_boundary_visible"] is True
    assert set(CONTAINER_GRADE_SECURE_HOST_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "secure_private_by_default" in policy["blocked_claims"]
    assert "ironclaw_class_secure_execution" in policy["blocked_claims"]
    assert "hardware_backed_isolation" in policy["blocked_claims"]
    assert "tee_cvm_wasm_or_container_runtime_isolation" in policy["not_claimed"]
    assert "/api/operator/container-grade-secure-host" in policy["receipt_surfaces"]


def test_container_grade_secure_host_receipts_cover_batch_ct_acceptance():
    contract = build_container_grade_secure_host_contract()
    summary = contract["summary"]
    classes = {item["capability_class"] for item in contract["isolation_model_decisions"]}
    findings = [
        finding
        for receipt in contract["external_security_validation_receipts"]
        for finding in receipt.get("findings", [])
    ]
    external_validation = contract["external_security_validation_receipts"]
    egress_decisions = {item["decision"] for item in contract["secret_egress_certification_drills"]}
    recovery_actions = {item["action"] for item in contract["incident_recovery_validation_receipts"]}

    assert {
        "tool_process",
        "browser_automation",
        "authenticated_connector",
        "extension_runtime",
        "workflow_replay",
        "hardware_backed_runtime",
    } <= classes
    assert summary["implemented_boundary_count"] >= 5
    assert summary["unsupported_boundary_count"] >= 1
    assert summary["external_review_count"] >= 2
    assert all("raw_report_path" not in receipt for receipt in external_validation)
    assert all(receipt["declared_fixture_path"].endswith(".fixture.json") for receipt in external_validation)
    assert all(receipt["real_external_certification"] is False for receipt in external_validation)
    assert all(
        receipt["evidence_boundary"] == "fixture_validation_record_not_external_security_certification"
        for receipt in external_validation
    )
    assert "external_security_certification" in contract["policy"]["blocked_claims"]
    assert summary["finding_count"] >= 4
    assert summary["remediated_or_waived_findings"] == summary["finding_count"]
    assert all(item["status"] in {"remediated", "residual_risk_waiver"} for item in findings)
    assert {"allowed", "blocked"} <= egress_decisions
    assert summary["secret_egress_drill_count"] >= 4
    assert summary["secret_leak_count"] == 0
    assert summary["all_secret_drills_safe"] is True
    assert {"rotate", "quarantine", "kill_switch", "waive", "post_incident_audit"} <= recovery_actions
    assert summary["recovery_authority_count"] >= 5


def test_container_grade_secure_host_report_runs_all_batch_ct_suites():
    scenario_names = [
        *CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES,
        *EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES,
        *SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES,
    ]

    class Summary:
        total = len(scenario_names)
        passed = len(scenario_names)
        failed = 0
        duration_ms = 23
        results = []

    with patch(
        "src.security.container_grade_host._run_container_grade_secure_host_suites",
        AsyncMock(return_value=Summary()),
    ):
        payload = asyncio.run(build_container_grade_secure_host_report())

    assert payload["summary"]["benchmark_posture"] == "container_grade_secure_host_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == len(scenario_names)
    assert payload["latest_run"]["failed"] == 0
    assert payload["failure_report"] == []
