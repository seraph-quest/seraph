"""Tests for Batch CK independent secure-host review receipts."""

import asyncio
from unittest.mock import AsyncMock, patch

from src.security.independent_review import (
    INDEPENDENT_SECURE_HOST_REVIEW_BLOCKED_CLAIMS,
    INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY,
    INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES,
    LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES,
    SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES,
    build_independent_secure_host_review_contract,
    build_independent_secure_host_review_report,
)


def test_independent_secure_host_review_contract_blocks_overclaims():
    contract = build_independent_secure_host_review_contract()

    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["suite_name"] == "independent_secure_host_review"
    assert summary["operator_status"] == "independent_secure_host_review_receipts_visible"
    assert summary["claim_boundary"] == INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY
    assert set(INDEPENDENT_SECURE_HOST_REVIEW_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "secure_private_by_default" in policy["blocked_claims"]
    assert "ironclaw_class_secure_execution" in policy["blocked_claims"]
    assert "tee_cvm_wasm_or_container_runtime_isolation" in policy["not_claimed"]
    assert "/api/operator/independent-secure-host-review" in policy["receipt_surfaces"]


def test_independent_secure_host_review_receipts_cover_ck_acceptance():
    contract = build_independent_secure_host_review_contract()
    review_scope = set(contract["review_receipts"][0]["target_surfaces"])
    hostile_families = {item["attack_family"] for item in contract["hostile_drill_receipts"]}
    recovery_actions = {item["action"] for item in contract["recovery_authority_receipts"]}

    assert {
        "tool_runtime",
        "browser_computer_use",
        "connector_credentials",
        "workflow_replay",
        "extension_runtime_contributions",
        "background_execution",
    } <= review_scope
    assert contract["summary"]["finding_count"] >= 4
    assert contract["summary"]["remediated_or_documented_finding_count"] == contract["summary"]["finding_count"]
    assert contract["summary"]["unsupported_isolation_claim_visible"] is True
    assert all(item["policy_decision"] in {"blocked", "quarantined"} for item in contract["hostile_drill_receipts"])
    assert {
        "prompt_injection",
        "ssrf_private_egress",
        "filesystem_escape",
        "credential_exfiltration",
        "extension_permission_creep",
        "workflow_replay_approval_drift",
        "browser_session_bleed",
    } <= hostile_families
    assert {"allow", "deny", "quarantine", "rotate", "recover", "post_incident_audit"} <= recovery_actions


def test_independent_secure_host_review_report_runs_all_batch_ck_suites():
    scenario_names = [
        *INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES,
        *LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES,
        *SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES,
    ]

    class Summary:
        total = len(scenario_names)
        passed = len(scenario_names)
        failed = 0
        duration_ms = 17
        results = []

    with patch(
        "src.security.independent_review._run_independent_secure_host_review_suites",
        AsyncMock(return_value=Summary()),
    ):
        payload = asyncio.run(build_independent_secure_host_review_report())

    assert payload["summary"]["benchmark_posture"] == "independent_secure_host_review_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == len(scenario_names)
    assert payload["latest_run"]["failed"] == 0
    assert payload["failure_report"] == []
