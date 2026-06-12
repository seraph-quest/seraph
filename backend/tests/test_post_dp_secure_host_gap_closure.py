"""Tests for Batch DR post-DP secure-host gap-closure receipts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.security.post_dp_secure_host_gap_closure import (
    DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SCENARIO_NAMES,
    DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SUITE_NAME,
    HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SCENARIO_NAMES,
    HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SUITE_NAME,
    POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SCENARIO_NAMES,
    POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SUITE_NAME,
    POST_DP_SECURE_HOST_BLOCKED_CLAIMS,
    POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
    RUNTIME_PROFILE_SELECTION_V2_SCENARIO_NAMES,
    RUNTIME_PROFILE_SELECTION_V2_SUITE_NAME,
    SECURE_HOST_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES,
    SECURE_HOST_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    SECURE_HOST_RECOVERY_AUTHORITY_V2_SCENARIO_NAMES,
    SECURE_HOST_RECOVERY_AUTHORITY_V2_SUITE_NAME,
    build_post_dp_secure_host_contract,
    build_post_dp_secure_host_report,
)


def test_post_dp_secure_host_contract_covers_dr_acceptance_fields():
    contract = build_post_dp_secure_host_contract()
    summary = contract["summary"]
    policy = contract["policy"]
    runtime_profiles = contract["runtime_profiles"]
    egress = contract["credential_egress"]
    hostile_chains = contract["hostile_chains"]
    recovery = contract["recovery_authority"]

    assert summary["operator_status"] == "post_dp_secure_capability_host_gap_closure_visible"
    assert summary["claim_boundary"] == POST_DP_SECURE_HOST_CLAIM_BOUNDARY
    assert set(POST_DP_SECURE_HOST_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/post-dp-secure-capability-host" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]
    assert summary["runtime_profile_count"] >= 7
    assert summary["runtime_profile_deny_default_count"] == summary["runtime_profile_count"]
    assert {item["capability_surface"] for item in runtime_profiles} >= {
        "tool_process",
        "browser_automation",
        "authenticated_connector",
        "external_mcp",
        "extension_package",
        "workflow_replay",
        "background_execution",
    }
    assert all(item["profile_selected_before_execution"] for item in runtime_profiles)
    assert all(item["deny_by_default"] for item in runtime_profiles)
    assert all(item["redacted_receipt_handle"].startswith("seraph://receipts/batch-dr/") for item in runtime_profiles)
    assert summary["credential_egress_block_count"] >= 5
    assert summary["credential_leak_count"] == 0
    assert all(item["default_posture"] == "deny" for item in egress)
    assert all(item["field_destination_scope_enforced"] for item in egress)
    assert all(item["raw_secret_leaked"] is False for item in egress)
    assert summary["hostile_chain_fail_closed_count"] == summary["hostile_chain_count"]
    assert summary["quarantine_before_runtime_count"] == summary["hostile_chain_count"]
    assert all(item["quarantine_before_runtime_contribution"] for item in hostile_chains)
    assert all(item["fail_closed"] for item in hostile_chains)
    assert summary["operator_owned_recovery_count"] == summary["recovery_action_count"]
    assert summary["automatic_authority_expansion_count"] == 0
    assert all(item["operator_owned"] for item in recovery)
    assert all(item["automatic_authority_expansion"] is False for item in recovery)


def test_post_dp_secure_host_contract_redacts_operator_payloads():
    contract = build_post_dp_secure_host_contract()
    serialized = json.dumps(contract, sort_keys=True)

    assert "prod-secure-token" not in serialized
    assert "Authorization: Bearer" not in serialized
    assert "sk-live" not in serialized.lower()
    assert "ironclaw_class_secure_execution" in serialized
    assert "claim_boundary" in serialized
    assert all(
        item["raw_secret_present"] is False
        for collection_name in ("runtime_profiles", "hostile_chains", "recovery_authority")
        for item in contract[collection_name]
    )


def test_post_dp_secure_host_report_runs_all_dr_suites():
    scenario_names = [
        *POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SCENARIO_NAMES,
        *RUNTIME_PROFILE_SELECTION_V2_SCENARIO_NAMES,
        *DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SCENARIO_NAMES,
        *HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SCENARIO_NAMES,
        *SECURE_HOST_RECOVERY_AUTHORITY_V2_SCENARIO_NAMES,
        *SECURE_HOST_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES,
    ]
    summary = SimpleNamespace(
        total=len(scenario_names),
        passed=len(scenario_names),
        failed=0,
        duration_ms=32,
        results=[SimpleNamespace(name=name, passed=True, error="") for name in scenario_names],
    )

    with patch(
        "src.security.post_dp_secure_host_gap_closure._run_post_dp_secure_host_suites",
        AsyncMock(return_value=summary),
    ):
        report = asyncio.run(build_post_dp_secure_host_report())

    assert report["summary"]["benchmark_posture"] == "post_dp_secure_capability_host_ci_gated_operator_visible"
    assert report["summary"]["scenario_count"] == len(scenario_names)
    assert report["latest_run"]["failed"] == 0
    assert report["failure_report"] == []
    assert report["scenario_names"][POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SUITE_NAME] == list(
        POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SCENARIO_NAMES
    )
    assert report["scenario_names"][RUNTIME_PROFILE_SELECTION_V2_SUITE_NAME] == list(
        RUNTIME_PROFILE_SELECTION_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SUITE_NAME] == list(
        DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SUITE_NAME] == list(
        HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][SECURE_HOST_RECOVERY_AUTHORITY_V2_SUITE_NAME] == list(
        SECURE_HOST_RECOVERY_AUTHORITY_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][SECURE_HOST_FALSE_CLAIM_SCAN_V2_SUITE_NAME] == list(
        SECURE_HOST_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES
    )
