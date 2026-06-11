"""Tests for Batch DB certified secure-host evidence receipts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.security.certified_secure_host import (
    CERTIFIED_SECURE_HOST_BLOCKED_CLAIMS,
    CERTIFIED_SECURE_HOST_CLAIM_BOUNDARY,
    CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SCENARIO_NAMES,
    CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SUITE_NAME,
    EXTERNAL_SECURITY_CERTIFICATION_SCENARIO_NAMES,
    EXTERNAL_SECURITY_CERTIFICATION_SUITE_NAME,
    HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SCENARIO_NAMES,
    HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SUITE_NAME,
    RUNTIME_ISOLATION_IMPLEMENTATION_SCENARIO_NAMES,
    RUNTIME_ISOLATION_IMPLEMENTATION_SUITE_NAME,
    build_certified_secure_host_contract,
    build_certified_secure_host_report,
)


def test_certified_secure_host_contract_blocks_overclaims():
    contract = build_certified_secure_host_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["suite_name"] == "certified_secure_host"
    assert summary["operator_status"] == "certified_secure_host_covered_path_receipts_visible"
    assert summary["claim_boundary"] == CERTIFIED_SECURE_HOST_CLAIM_BOUNDARY
    assert set(CERTIFIED_SECURE_HOST_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "ironclaw_class_secure_execution" in policy["blocked_claims"]
    assert "hardware_backed_isolation" in policy["blocked_claims"]
    assert "formal_security_certification" in policy["blocked_claims"]
    assert "certified_secure_isolation" in policy["not_claimed"]
    assert "/api/operator/certified-secure-host" in policy["receipt_surfaces"]


def test_certified_secure_host_receipts_cover_batch_db_acceptance():
    contract = build_certified_secure_host_contract()
    summary = contract["summary"]
    classes = {item["capability_class"] for item in contract["runtime_isolation_profiles"]}
    broker_decisions = contract["credential_broker_egress_decisions"]
    certification_records = contract["external_security_certification_records"]
    escape_cases = contract["hostile_runtime_escape_cases"]
    findings = [
        finding
        for record in certification_records
        for finding in record.get("findings", [])
    ]
    waivers = [
        waiver
        for record in certification_records
        for waiver in record.get("waivers", [])
    ]

    assert {
        "tool_process",
        "browser_automation",
        "authenticated_connector",
        "external_mcp",
        "extension_runtime",
        "workflow_replay",
        "hardware_backed_runtime",
    } <= classes
    assert summary["implemented_runtime_profile_count"] >= 6
    assert summary["hardware_substitute_visible"] is True
    assert all(item["operator_visible"] is True for item in contract["runtime_isolation_profiles"])
    assert all(item["fail_closed"] is True for item in contract["runtime_isolation_profiles"])
    assert all(item["enforcement_hooks"] for item in contract["runtime_isolation_profiles"])
    assert summary["credential_broker_decision_count"] >= 6
    assert summary["credential_broker_block_count"] >= 5
    assert summary["credential_leak_count"] == 0
    assert all(item["field_scoped_injection"] is True for item in broker_decisions)
    assert all(item["endpoint_allowlist_checked"] is True for item in broker_decisions)
    assert all(item["private_network_checked"] is True for item in broker_decisions)
    assert summary["external_record_count"] >= 3
    assert summary["formal_certification_count"] == 0
    assert all(item["artifact_digest"].startswith("sha256:") for item in certification_records)
    assert summary["finding_count"] >= 4
    assert summary["remediated_or_waived_findings"] == summary["finding_count"]
    assert all(item["retest_status"] in {"passed", "waived_with_expiry"} for item in findings)
    assert summary["waiver_count"] >= 1
    assert all(item["expires"] for item in waivers)
    assert summary["escape_case_count"] >= 8
    assert summary["escape_fail_closed_count"] == summary["escape_case_count"]
    assert all(item["operator_visible"] is True for item in escape_cases)
    assert all(item["recovery_actions"] for item in escape_cases)


def test_certified_secure_host_report_runs_all_batch_db_suites():
    scenario_names = [
        *RUNTIME_ISOLATION_IMPLEMENTATION_SCENARIO_NAMES,
        *CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SCENARIO_NAMES,
        *EXTERNAL_SECURITY_CERTIFICATION_SCENARIO_NAMES,
        *HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SCENARIO_NAMES,
    ]
    summary = SimpleNamespace(
        total=len(scenario_names),
        passed=len(scenario_names),
        failed=0,
        duration_ms=31,
        results=[SimpleNamespace(name=name, passed=True, error="") for name in scenario_names],
    )

    with patch(
        "src.security.certified_secure_host._run_certified_secure_host_suites",
        AsyncMock(return_value=summary),
    ):
        payload = asyncio.run(build_certified_secure_host_report())

    assert payload["summary"]["benchmark_posture"] == "certified_secure_host_covered_path_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == len(scenario_names)
    assert payload["latest_run"]["failed"] == 0
    assert payload["failure_report"] == []
    assert payload["scenario_names"][RUNTIME_ISOLATION_IMPLEMENTATION_SUITE_NAME] == list(
        RUNTIME_ISOLATION_IMPLEMENTATION_SCENARIO_NAMES
    )
    assert payload["scenario_names"][CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SUITE_NAME] == list(
        CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SCENARIO_NAMES
    )
    assert payload["scenario_names"][EXTERNAL_SECURITY_CERTIFICATION_SUITE_NAME] == list(
        EXTERNAL_SECURITY_CERTIFICATION_SCENARIO_NAMES
    )
    assert payload["scenario_names"][HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SUITE_NAME] == list(
        HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SCENARIO_NAMES
    )
