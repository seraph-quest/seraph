import asyncio

from src.extensions.browser_provider_usability import (
    BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES,
    BROWSER_PROVIDER_USABILITY_BLOCKED_CLAIMS,
    BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY,
    LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES,
    MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES,
    build_browser_provider_usability_contract,
    build_browser_provider_usability_report,
)


def test_browser_provider_usability_contract_exposes_provider_boundaries():
    contract = build_browser_provider_usability_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "browser_provider_usability_receipts_visible"
    assert summary["provider_attestation_count"] == 3
    assert summary["recorded_live_provider_count"] == 2
    assert summary["session_partition_count"] == 2
    assert summary["credential_boundary_count"] == 3
    assert summary["download_upload_boundary_count"] == 3
    assert summary["claim_boundary"] == BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY
    assert set(BROWSER_PROVIDER_USABILITY_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/browser-provider-usability-proof" in policy["receipt_surfaces"]


def test_browser_provider_usability_receipts_block_remote_cdp_existing_session():
    contract = build_browser_provider_usability_contract()
    providers = {item["provider_id"]: item for item in contract["provider_attestation_receipts"]}

    remote = providers["openclaw-remote-cdp-existing-session"]
    assert remote["provider_mode"] == "remote_cdp_existing_session"
    assert remote["session_partition"]["cookie_jar_isolated"] is False
    assert remote["provider_degradation"]["state"] == "blocked_until_partitioned"
    assert remote["credential_boundary"]["credential_reuse_blocked"] is True
    assert remote["download_upload_boundary"]["unreviewed_file_mutation_allowed"] is False


def test_browser_provider_usability_receipts_include_multi_operator_metrics():
    contract = build_browser_provider_usability_contract()

    assert contract["summary"]["multi_operator_task_count"] == 3
    assert contract["summary"]["max_operator_count"] == 3
    assert contract["summary"]["keyboard_path_count"] == 3
    assert contract["summary"]["accessibility_receipt_count"] == 3
    assert contract["summary"]["reversible_action_count"] == 3
    assert all("error_rate" in item for item in contract["multi_operator_usability_receipts"])
    assert all("ambiguity_events" in item for item in contract["multi_operator_usability_receipts"])


def test_browser_provider_usability_recovery_drills_fail_closed():
    contract = build_browser_provider_usability_contract()

    assert contract["summary"]["recovery_drill_count"] == 4
    assert contract["summary"]["fail_closed_recovery_count"] == 4
    assert contract["summary"]["external_action_block_count"] == 4
    assert all(item["operator_visible"] for item in contract["recovery_drill_receipts"])


def test_browser_provider_usability_report_runs_all_batch_ch_suites():
    payload = asyncio.run(build_browser_provider_usability_report())

    assert payload["summary"]["benchmark_posture"] == "browser_provider_usability_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES)
        + len(LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES)
        + len(BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY
