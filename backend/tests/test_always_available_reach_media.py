import pytest

from src.extensions.always_available_reach_media import (
    ALWAYS_AVAILABLE_REACH_MEDIA_BLOCKED_CLAIMS,
    ALWAYS_AVAILABLE_REACH_MEDIA_CLAIM_BOUNDARY,
    ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SUITE_NAME,
    MOBILE_CROSS_SURFACE_CONTINUITY_V1_SUITE_NAME,
    REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SUITE_NAME,
    VOICE_MEDIA_PARITY_RUNTIME_V1_SUITE_NAME,
    build_always_available_reach_media_contract,
    build_always_available_reach_media_report,
)


def test_always_available_reach_media_contract_exposes_batch_dc_receipts():
    contract = build_always_available_reach_media_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["reach_operations_suite_name"] == ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SUITE_NAME
    assert summary["voice_media_suite_name"] == VOICE_MEDIA_PARITY_RUNTIME_V1_SUITE_NAME
    assert summary["continuity_suite_name"] == MOBILE_CROSS_SURFACE_CONTINUITY_V1_SUITE_NAME
    assert summary["field_campaign_suite_name"] == REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SUITE_NAME
    assert summary["selected_channel_count"] >= 5
    assert summary["channel_family_count"] >= 5
    assert summary["campaign_14_day_equivalent_count"] >= 5
    assert summary["paired_revocation_count"] >= 5
    assert summary["rate_abuse_recovery_count"] >= 5
    assert summary["continuity_channel_count"] >= 5
    assert summary["coverage_gap_count"] >= 1
    assert summary["voice_media_provider_family_count"] >= 5
    assert summary["voice_media_quality_pass_count"] >= 5
    assert summary["voice_media_latency_pass_count"] >= 5
    assert summary["voice_media_privacy_control_count"] >= 5
    assert summary["voice_media_fallback_regression_count"] >= 5
    assert summary["cross_surface_continuity_count"] >= 4
    assert summary["continuity_failure_survival_count"] >= 4
    assert summary["field_campaign_count"] == 1
    assert summary["field_campaign_operator_repair_count"] >= 5
    assert summary["claim_boundary"] == ALWAYS_AVAILABLE_REACH_MEDIA_CLAIM_BOUNDARY
    assert set(ALWAYS_AVAILABLE_REACH_MEDIA_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])


def test_always_available_reach_media_blocks_overclaims_and_raw_payloads():
    contract = build_always_available_reach_media_contract()

    gap = next(item for item in contract["selected_reach_channels"] if item["coverage_gap"])
    assert gap["pairing"]["pairing_state"] == "requires_pairing"
    assert gap["campaign_window"]["slo_met"] is False
    assert gap["recovery"]["unsafe_mutation_blocked"] is True

    for channel in contract["selected_reach_channels"]:
        assert channel["pairing"]["revoked_probe_blocks_delivery"] is True
        assert channel["limits"]["unsafe_follow_up_hidden"] is True
        assert channel["recovery"]["unsafe_mutation_blocked"] is True
        assert channel["continuity"]["replay_authority"] == "operator_review_required_before_replay"
        assert channel["safe_receipt"]["stored_payload_mode"] == "metadata_only_redacted_receipt"
        assert channel["safe_receipt"]["contains_message_body"] is False
        assert channel["safe_receipt"]["contains_secret"] is False
        assert channel["safe_receipt"]["contains_contact_identifier"] is False

    for runtime in contract["voice_media_runtime_receipts"]:
        assert runtime["quality"]["passed"] is True
        assert runtime["latency"]["passed"] is True
        assert runtime["consent_privacy"]["content_redacted"] is True
        assert runtime["operator_controls"]["correction_path"]
        assert runtime["operator_controls"]["deletion_path"]
        assert runtime["operator_controls"]["revocation_blocks_capture"] is True
        assert runtime["fallback"]["unsafe_action_allowed"] is False

    for receipt_group in (
        contract["voice_media_runtime_receipts"],
        contract["mobile_cross_surface_continuity"],
        contract["field_campaign"],
    ):
        for receipt in receipt_group:
            safe_receipt = receipt["safe_receipt"]
            assert safe_receipt["contains_message_body"] is False
            assert safe_receipt["contains_secret"] is False
            assert safe_receipt["contains_transcript"] is False
            assert safe_receipt["contains_audio_payload"] is False
            assert safe_receipt["contains_media_payload"] is False

    assert "always_available_operation" in contract["policy"]["blocked_claims"]
    assert "openclaw_class_reach" in contract["policy"]["blocked_claims"]
    assert "voice_parity" in contract["policy"]["blocked_claims"]
    assert "full_parity" in contract["policy"]["blocked_claims"]


@pytest.mark.asyncio
async def test_always_available_reach_media_report_exposes_ci_gated_posture():
    report = await build_always_available_reach_media_report()

    assert report["summary"]["benchmark_posture"] == "always_available_reach_media_ci_gated_operator_visible"
    assert report["summary"]["operator_status"] == "always_available_reach_media_receipts_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["scenario_count"] == 16
    assert report["policy"]["claim_boundary"] == ALWAYS_AVAILABLE_REACH_MEDIA_CLAIM_BOUNDARY
    assert "/api/operator/always-available-reach-media" in report["policy"]["receipt_surfaces"]
