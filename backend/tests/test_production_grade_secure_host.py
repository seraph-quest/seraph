"""Tests for Batch DJ production-grade secure-host evidence receipts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.security.production_grade_secure_host import (
    CREDENTIAL_BROKER_EGRESS_SOAK_SCENARIO_NAMES,
    CREDENTIAL_BROKER_EGRESS_SOAK_SUITE_NAME,
    PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SCENARIO_NAMES,
    PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SUITE_NAME,
    PRODUCTION_GRADE_SECURE_HOST_BLOCKED_CLAIMS,
    PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
    RUNTIME_ISOLATION_ATTESTATION_MATRIX_SCENARIO_NAMES,
    RUNTIME_ISOLATION_ATTESTATION_MATRIX_SUITE_NAME,
    SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SCENARIO_NAMES,
    SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SUITE_NAME,
    SECURE_HOST_FALSE_CLAIM_SCAN_SCENARIO_NAMES,
    SECURE_HOST_FALSE_CLAIM_SCAN_SUITE_NAME,
    SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SCENARIO_NAMES,
    SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SUITE_NAME,
    build_production_grade_secure_host_contract,
    build_production_grade_secure_host_report,
)


def test_production_grade_secure_host_contract_blocks_security_overclaims():
    contract = build_production_grade_secure_host_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["suite_name"] == "production_grade_secure_host"
    assert summary["operator_status"] == "production_grade_secure_host_receipts_visible"
    assert summary["claim_boundary"] == PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY
    assert set(PRODUCTION_GRADE_SECURE_HOST_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "ironclaw_class_secure_execution" in policy["blocked_claims"]
    assert "hardware_backed_isolation" in policy["blocked_claims"]
    assert "formal_security_certification" in policy["blocked_claims"]
    assert "secure_private_by_default" in policy["not_claimed"]
    assert "/api/operator/production-grade-secure-capability-host" in policy["receipt_surfaces"]


def test_production_grade_secure_host_receipts_cover_dj_acceptance_fields():
    contract = build_production_grade_secure_host_contract()
    summary = contract["summary"]
    surface_matrix = contract["surface_matrix"]
    surfaces = {item["surface_id"] for item in surface_matrix}
    surface_by_id = {item["surface_id"]: item for item in surface_matrix}
    attack_chains = contract["cross_surface_attack_chains"]
    egress = contract["credential_broker_egress_soak"]
    attestation = contract["runtime_isolation_attestation_matrix"]
    recovery = contract["operator_recovery_authority"]
    false_claims = contract["false_claim_scan_receipts"]

    assert {
        "tool_process",
        "browser_automation",
        "authenticated_connector",
        "external_mcp",
        "extension_runtime",
        "workflow_replay",
        "delegation",
        "background_execution",
        "provider_fallback",
        "filesystem",
        "network",
        "credential",
    } <= surfaces
    assert all(item["redacted_receipt_handle"].startswith("seraph://receipts/batch-dj/") for item in surface_matrix)
    for surface_id in (
        "authenticated_connector",
        "external_mcp",
        "extension_runtime",
        "workflow_replay",
        "provider_fallback",
        "credential",
    ):
        assert surface_by_id[surface_id]["credential_scope"] == "field_and_destination_scoped_secret_ref"
    assert summary["attack_chain_count"] >= 7
    assert summary["attack_chain_fail_closed_count"] == summary["attack_chain_count"]
    assert all(item["fail_closed"] is True for item in attack_chains)
    assert all(item["source_surface"] and item["destination_surface"] for item in attack_chains)
    assert all(item["private_network_decision"] == "blocked" for item in attack_chains)
    assert all(item["redaction_digest"] for item in attack_chains)
    assert all(item["redacted_receipt_handle"].startswith("seraph://receipts/batch-dj/") for item in attack_chains)
    assert all(item["fixture_vs_live"] == "attack_chain_fixture_not_live_external_target" for item in attack_chains)
    assert summary["credential_egress_block_count"] >= 4
    assert summary["credential_leak_count"] == 0
    assert all(item["field_scoped_injection"] is True for item in egress)
    assert all(item["dns_redirect_rechecked"] is True for item in egress)
    assert all(item["redacted_receipt_handle"].startswith("seraph://receipts/batch-dj/") for item in egress)
    assert any(item["surface"] == "hardware_backed_runtime" and item["implemented"] is False for item in attestation)
    assert any(
        item["surface"] == "formal_security_certification" and item["implemented"] is False
        for item in attestation
    )
    assert all(item["redacted_receipt_handle"].startswith("seraph://receipts/batch-dj/") for item in attestation)
    assert {item["action"] for item in recovery} >= {
        "deny",
        "quarantine",
        "rotate",
        "rollback",
        "revoke",
        "replay_block",
        "repair",
        "audit",
    }
    assert all(item["safe_redaction_digest"] for item in recovery)
    assert all(item["redacted_receipt_handle"].startswith("seraph://receipts/batch-dj/") for item in recovery)
    assert false_claims[0]["redacted_receipt_handle"].startswith("seraph://receipts/batch-dj/")
    assert false_claims[0]["validation_command"] == "python3 scripts/check_strategy_claims.py"
    assert false_claims[0]["forbidden_hit_count"] == 0
    assert false_claims[0]["blocked_claims_found"] == []
    assert false_claims[0]["fixture_vs_live"] == "repository_scan_not_external_certification"


def test_production_grade_secure_host_report_runs_all_dj_suites():
    scenario_names = [
        *PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SCENARIO_NAMES,
        *SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SCENARIO_NAMES,
        *CREDENTIAL_BROKER_EGRESS_SOAK_SCENARIO_NAMES,
        *RUNTIME_ISOLATION_ATTESTATION_MATRIX_SCENARIO_NAMES,
        *SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SCENARIO_NAMES,
        *SECURE_HOST_FALSE_CLAIM_SCAN_SCENARIO_NAMES,
    ]
    summary = SimpleNamespace(
        total=len(scenario_names),
        passed=len(scenario_names),
        failed=0,
        duration_ms=27,
        results=[SimpleNamespace(name=name, passed=True, error="") for name in scenario_names],
    )

    with patch(
        "src.security.production_grade_secure_host._run_production_grade_secure_host_suites",
        AsyncMock(return_value=summary),
    ):
        payload = asyncio.run(build_production_grade_secure_host_report())

    assert payload["summary"]["benchmark_posture"] == "production_grade_secure_host_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == len(scenario_names)
    assert payload["latest_run"]["failed"] == 0
    assert payload["failure_report"] == []
    assert payload["scenario_names"][PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SUITE_NAME] == list(
        PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SCENARIO_NAMES
    )
    assert payload["scenario_names"][SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SUITE_NAME] == list(
        SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SCENARIO_NAMES
    )
    assert payload["scenario_names"][CREDENTIAL_BROKER_EGRESS_SOAK_SUITE_NAME] == list(
        CREDENTIAL_BROKER_EGRESS_SOAK_SCENARIO_NAMES
    )
    assert payload["scenario_names"][RUNTIME_ISOLATION_ATTESTATION_MATRIX_SUITE_NAME] == list(
        RUNTIME_ISOLATION_ATTESTATION_MATRIX_SCENARIO_NAMES
    )
    assert payload["scenario_names"][SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SUITE_NAME] == list(
        SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SCENARIO_NAMES
    )
    assert payload["scenario_names"][SECURE_HOST_FALSE_CLAIM_SCAN_SUITE_NAME] == list(
        SECURE_HOST_FALSE_CLAIM_SCAN_SCENARIO_NAMES
    )
