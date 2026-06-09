import pytest

from src.extensions.production_reach_hardening import (
    BROWSER_COMPUTER_USE_RELIABILITY_V2_SUITE_NAME,
    GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SUITE_NAME,
    PRODUCTION_REACH_BROWSER_VOICE_BLOCKED_CLAIMS,
    PRODUCTION_REACH_BROWSER_VOICE_CLAIM_BOUNDARY,
    PRODUCTION_REACH_CHANNEL_HARDENING_SUITE_NAME,
    build_production_reach_browser_voice_contract,
    build_production_reach_browser_voice_report,
)


def test_production_reach_browser_voice_contract_exposes_batch_by_receipts():
    contract = build_production_reach_browser_voice_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["channel_suite_name"] == PRODUCTION_REACH_CHANNEL_HARDENING_SUITE_NAME
    assert summary["browser_suite_name"] == BROWSER_COMPUTER_USE_RELIABILITY_V2_SUITE_NAME
    assert summary["voice_media_suite_name"] == GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SUITE_NAME
    assert summary["paired_external_messaging_channel_count"] >= 1
    assert summary["revoked_follow_up_hidden_count"] >= 1
    assert summary["privacy_redaction_count"] >= 1
    assert summary["browser_session_partition_count"] >= 2
    assert summary["browser_crash_recovery_count"] >= 1
    assert summary["browser_page_drift_block_count"] >= 1
    assert summary["voice_media_deletion_path_count"] >= 2
    assert summary["voice_media_revocation_fail_closed_count"] >= 2
    assert summary["degraded_recovery_count"] >= 2
    assert policy["claim_boundary"] == PRODUCTION_REACH_BROWSER_VOICE_CLAIM_BOUNDARY
    assert set(PRODUCTION_REACH_BROWSER_VOICE_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])


def test_production_reach_browser_voice_contract_blocks_unsafe_channel_and_browser_actions():
    contract = build_production_reach_browser_voice_contract()
    channels = contract["channels"]
    browsers = contract["browser_reliability"]

    requires_config = next(item for item in channels if item["status"] == "requires_config")
    assert requires_config["approval_handoff"]["status"] == "blocked_until_pairing"
    assert requires_config["degraded_recovery"]["unsafe_follow_up_hidden"] is True

    drifted_browser = next(
        item for item in browsers
        if item["page_drift_replay"]["drift_detected"] is True
    )
    assert drifted_browser["page_drift_replay"]["external_action_allowed"] is False
    assert drifted_browser["session_partition"]["cross_channel_cookie_reuse_blocked"] is True


def test_production_reach_browser_voice_contract_requires_voice_media_privacy_and_deletion():
    contract = build_production_reach_browser_voice_contract()

    for runtime in contract["voice_media_runtimes"]:
        assert runtime["guardian_value_reason"]
        assert runtime["privacy"]["secret_payload_present"] is False
        assert runtime["correction_deletion"]["deletion_path"]
        assert runtime["revocation"]["fails_closed_after_revoke"] is True


@pytest.mark.asyncio
async def test_production_reach_browser_voice_report_exposes_ci_gated_posture():
    report = await build_production_reach_browser_voice_report()

    assert report["summary"]["benchmark_posture"] == "production_reach_browser_voice_ci_gated_operator_visible"
    assert report["summary"]["operator_status"] == "production_reach_browser_voice_receipts_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["scenario_count"] == 14
    assert report["policy"]["claim_boundary"] == PRODUCTION_REACH_BROWSER_VOICE_CLAIM_BOUNDARY
    assert "/api/operator/production-reach-browser-voice" in report["policy"]["receipt_surfaces"]
