import asyncio

from src.extensions.browser_computer_use_production import (
    BROWSER_COMPUTER_USE_PRODUCTION_BLOCKED_CLAIMS,
    BROWSER_COMPUTER_USE_PRODUCTION_CLAIM_BOUNDARY,
    BROWSER_COMPUTER_USE_PRODUCTION_SAFETY_V1_SCENARIO_NAMES,
    BROWSER_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES,
    BROWSER_PROVIDER_PARITY_CANDIDATE_V1_SCENARIO_NAMES,
    BROWSER_SESSION_PARTITION_ATTESTATION_V2_SCENARIO_NAMES,
    CREDENTIALED_SITE_RECOVERY_V1_SCENARIO_NAMES,
    REQUIRED_CREDENTIALED_RECOVERY_CASES,
    REQUIRED_DO_BOUNDARIES,
    REQUIRED_LIVE_OPS,
    REQUIRED_REMOTE_DEGRADATION_CASES,
    SAFE_BROWSER_AUTOMATION_LIVE_OPS_V1_SCENARIO_NAMES,
    build_browser_computer_use_production_contract,
    build_browser_computer_use_production_report,
)
from src.extensions.full_browser_parity import REQUIRED_BROWSER_PROVIDER_MODES, REQUIRED_HOSTILE_BROWSER_CASES


def _safe_receipts(contract):
    return [
        item["safe_receipt"]
        for item in [
            *contract["production_safety_providers"],
            *contract["production_boundary_matrix"],
            *contract["hostile_page_fail_closed_receipts"],
            *contract["safe_browser_automation_live_ops"],
            *contract["credentialed_site_recovery"],
            *contract["browser_provider_parity_candidates"],
            *contract["browser_session_partition_attestation_v2"],
            contract["browser_false_claim_scan"],
        ]
    ]


def test_browser_computer_use_production_contract_blocks_overclaims():
    contract = build_browser_computer_use_production_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "browser_computer_use_production_safety_receipts_visible"
    assert summary["claim_boundary"] == BROWSER_COMPUTER_USE_PRODUCTION_CLAIM_BOUNDARY
    assert summary["safe_browser_automation_claim_allowed"] is False
    assert summary["safe_autonomous_computer_use_claim_allowed"] is False
    assert summary["full_browser_parity_claim_allowed"] is False
    assert summary["openclaw_class_browser_reach_claim_allowed"] is False
    assert summary["production_ready_claim_allowed"] is False
    assert summary["reference_systems_exceeded_claim_allowed"] is False
    assert set(BROWSER_COMPUTER_USE_PRODUCTION_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert set(BROWSER_COMPUTER_USE_PRODUCTION_BLOCKED_CLAIMS) <= set(policy["not_claimed"])
    assert "/api/operator/browser-computer-use-production" in policy["receipt_surfaces"]
    assert policy["depends_on"]["required_prior_surface"] == "/api/operator/full-browser-parity"


def test_browser_computer_use_production_receipts_cover_provider_and_boundary_safety():
    contract = build_browser_computer_use_production_contract()
    summary = contract["summary"]
    providers = contract["production_safety_providers"]
    boundaries = contract["production_boundary_matrix"]
    hostile = contract["hostile_page_fail_closed_receipts"]

    assert summary["required_provider_modes_covered"] is True
    assert {item["provider_mode"] for item in providers} == set(REQUIRED_BROWSER_PROVIDER_MODES)
    assert summary["unsupported_paths_explicit"] is True
    assert summary["required_boundaries_covered"] is True
    assert summary["all_boundaries_enforced"] is True
    assert set(REQUIRED_DO_BOUNDARIES) <= {item["boundary"] for item in boundaries}
    assert summary["boundary_leak_count"] == 0
    assert summary["private_network_denial_count"] >= len(REQUIRED_BROWSER_PROVIDER_MODES)
    assert all(item["enforced"] is True and item["claim_lift_allowed"] is False for item in boundaries)
    assert summary["required_hostile_cases_covered"] is True
    assert set(REQUIRED_HOSTILE_BROWSER_CASES) <= {item["hostile_case"] for item in hostile}
    assert summary["hostile_cases_fail_closed"] is True
    assert summary["credential_leak_count"] == 0
    assert summary["cookie_leak_count"] == 0
    assert summary["private_data_leak_count"] == 0
    assert summary["unapproved_mutation_count"] == 0


def test_browser_computer_use_production_live_ops_recovery_and_provider_candidates_are_bounded():
    contract = build_browser_computer_use_production_contract()
    summary = contract["summary"]
    live_ops = contract["safe_browser_automation_live_ops"]
    recovery = contract["credentialed_site_recovery"]
    provider_candidates = contract["browser_provider_parity_candidates"]
    partition = contract["browser_session_partition_attestation_v2"]

    assert summary["required_live_ops_covered"] is True
    assert set(REQUIRED_LIVE_OPS) <= {item["operation"] for item in live_ops}
    assert summary["operator_takeover_visible"] is True
    assert all(item["dangerous_action_default_blocked"] is True for item in live_ops)
    assert all(item["external_mutation_allowed"] is False for item in live_ops)
    assert summary["required_credentialed_recovery_cases_covered"] is True
    assert set(REQUIRED_CREDENTIALED_RECOVERY_CASES) <= {item["recovery_case"] for item in recovery}
    assert summary["credentialed_recovery_fails_closed"] is True
    assert summary["captcha_boundary_human_review"] is True
    assert summary["provider_identity_visible"] is True
    assert summary["managed_remote_live_provider_claimed"] is False
    assert summary["remote_cdp_live_transport_claimed"] is False
    assert summary["remote_degradation_cases_covered"] is True
    assert set(REQUIRED_REMOTE_DEGRADATION_CASES) <= {
        item["degradation_case"]
        for item in provider_candidates
        if item.get("degradation_case")
    }
    assert summary["existing_session_unpartitioned_blocked"] is True
    assert summary["partition_attestation_v2_passed_count"] == len(REQUIRED_BROWSER_PROVIDER_MODES) - 1
    assert all(item["total_leak_count"] == 0 for item in partition)


def test_browser_computer_use_production_safe_receipts_are_redacted_and_false_claim_scan_is_clean():
    contract = build_browser_computer_use_production_contract()
    summary = contract["summary"]
    false_claim_scan = contract["browser_false_claim_scan"]
    receipts = _safe_receipts(contract)

    assert summary["safe_receipts_redacted"] is True
    assert false_claim_scan["forbidden_hit_count"] == 0
    assert false_claim_scan["claim_lift_allowed"] is False
    assert set(BROWSER_COMPUTER_USE_PRODUCTION_BLOCKED_CLAIMS) <= set(false_claim_scan["blocked_claims_checked"])
    assert all(receipt["redaction_layer"] == "browser_computer_use_production_v1" for receipt in receipts)
    assert all(receipt["contains_secret"] is False for receipt in receipts)
    assert all(receipt["contains_cookie"] is False for receipt in receipts)
    assert all(receipt["contains_raw_dom"] is False for receipt in receipts)
    assert all(receipt["contains_screenshot"] is False for receipt in receipts)
    assert all(receipt["contains_clipboard_content"] is False for receipt in receipts)
    assert all(receipt["contains_private_page_content"] is False for receipt in receipts)
    assert all(receipt["raw_receipt_path_exposed"] is False for receipt in receipts)
    assert all(len(receipt["evidence_body_digest"]) == 64 for receipt in receipts)
    assert all(len(receipt["sanitized_payload_digest"]) == 64 for receipt in receipts)
    assert all(receipt["tamper_evident_digest"] != receipt["evidence_body_digest"] for receipt in receipts)


def test_browser_computer_use_production_report_runs_all_gates():
    report = asyncio.run(build_browser_computer_use_production_report())
    summary = report["summary"]

    assert summary["benchmark_posture"] == "browser_computer_use_production_safety_ci_gated_operator_visible"
    assert summary["active_failure_count"] == 0
    assert summary["scenario_count"] == (
        len(BROWSER_COMPUTER_USE_PRODUCTION_SAFETY_V1_SCENARIO_NAMES)
        + len(SAFE_BROWSER_AUTOMATION_LIVE_OPS_V1_SCENARIO_NAMES)
        + len(CREDENTIALED_SITE_RECOVERY_V1_SCENARIO_NAMES)
        + len(BROWSER_PROVIDER_PARITY_CANDIDATE_V1_SCENARIO_NAMES)
        + len(BROWSER_SESSION_PARTITION_ATTESTATION_V2_SCENARIO_NAMES)
        + len(BROWSER_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES)
    )
    assert report["latest_run"]["failed"] == 0
    assert report["failure_report"] == []
