import pytest

from src.extensions.live_reach_media import (
    CROSS_SURFACE_CONTINUITY_RECOVERY_SUITE_NAME,
    LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SUITE_NAME,
    LIVE_REACH_MEDIA_BLOCKED_CLAIMS,
    LIVE_REACH_MEDIA_CLAIM_BOUNDARY,
    PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SUITE_NAME,
    build_live_reach_media_contract,
    build_live_reach_media_report,
)


def test_live_reach_media_contract_exposes_batch_ce_receipts():
    contract = build_live_reach_media_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["live_reach_suite_name"] == LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SUITE_NAME
    assert summary["voice_media_suite_name"] == PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SUITE_NAME
    assert summary["continuity_suite_name"] == CROSS_SURFACE_CONTINUITY_RECOVERY_SUITE_NAME
    assert summary["recorded_live_channel_count"] >= 2
    assert summary["paired_channel_count"] >= 2
    assert summary["revocation_fail_closed_count"] >= 3
    assert summary["rate_limit_visible_count"] >= 3
    assert summary["degraded_recovery_visible_count"] >= 3
    assert summary["voice_media_provider_count"] >= 3
    assert summary["voice_media_consent_count"] >= 3
    assert summary["voice_media_deletion_count"] >= 3
    assert summary["voice_media_failure_fallback_count"] >= 3
    assert summary["continuity_thread_preserved_count"] >= 2
    assert summary["approval_survived_surface_shift_count"] >= 2
    assert policy["claim_boundary"] == LIVE_REACH_MEDIA_CLAIM_BOUNDARY
    assert set(LIVE_REACH_MEDIA_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])


def test_live_reach_media_contract_blocks_unpaired_and_degraded_mutations():
    contract = build_live_reach_media_contract()

    slack = next(item for item in contract["channels"] if item["transport"] == "slack")
    assert slack["pairing"]["state"] == "requires_pairing"
    assert slack["approval_handoff"]["mutation_boundary"] == "closed"
    assert slack["degraded_recovery"]["unsafe_follow_up_hidden"] is True

    for recovery in contract["cross_surface_recovery"]:
        assert recovery["approval_boundary"]["mutation_allowed_without_approval"] is False
        assert recovery["degraded_state"]["unsafe_follow_up_hidden"] is True


def test_live_reach_media_contract_requires_provider_consent_and_privacy():
    contract = build_live_reach_media_contract()

    for channel in contract["channels"]:
        assert channel["provider"]
        assert channel["evidence_mode"] in {"recorded_live", "configured_degraded"}
        assert channel["operator_identity"]["consent_receipt_id"]
        assert channel["rate_limits"]["provider_limit_visible"] is True

    for runtime in contract["voice_media_providers"]:
        assert runtime["provider"]
        assert runtime["consent"]["consent_receipt_id"]
        assert runtime["capture_boundary"]["content_redacted"] is True
        assert runtime["capture_boundary"]["provider_destination_visible"] is True
        assert runtime["operator_controls"]["deletion_path"]
        assert runtime["provider_failure"]["unsafe_action_allowed"] is False


@pytest.mark.asyncio
async def test_live_reach_media_report_exposes_ci_gated_posture():
    report = await build_live_reach_media_report()

    assert report["summary"]["benchmark_posture"] == "live_reach_media_ci_gated_operator_visible"
    assert report["summary"]["operator_status"] == "live_reach_media_receipts_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["scenario_count"] == 12
    assert report["policy"]["claim_boundary"] == LIVE_REACH_MEDIA_CLAIM_BOUNDARY
    assert "/api/operator/live-reach-media-proof" in report["policy"]["receipt_surfaces"]
