import pytest

from src.extensions.reach_voice_production_ops import (
    ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SCENARIO_NAMES,
    ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SUITE_NAME,
    CHANNEL_INCIDENT_RESPONSE_V1_SCENARIO_NAMES,
    CHANNEL_INCIDENT_RESPONSE_V1_SUITE_NAME,
    CROSS_SURFACE_REACH_CONTINUITY_V2_SCENARIO_NAMES,
    CROSS_SURFACE_REACH_CONTINUITY_V2_SUITE_NAME,
    REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES,
    REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
    REACH_VOICE_PRODUCTION_OPS_BLOCKED_CLAIMS,
    REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
    REACH_VOICE_SAFE_REDACTION_BOUNDARY,
    VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SCENARIO_NAMES,
    VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SUITE_NAME,
    build_reach_voice_production_ops_contract,
    build_reach_voice_production_ops_report,
)


def _receipt_groups(contract):
    return (
        contract["live_reach_operations"],
        contract["voice_media_production_candidates"],
        contract["channel_incident_response"],
        contract["cross_surface_reach_continuity_v2"],
        contract["false_claim_scan_receipts"],
    )


def test_reach_voice_production_ops_contract_exposes_batch_dk_receipts():
    contract = build_reach_voice_production_ops_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["live_ops_suite_name"] == ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SUITE_NAME
    assert summary["voice_media_suite_name"] == VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SUITE_NAME
    assert summary["incident_suite_name"] == CHANNEL_INCIDENT_RESPONSE_V1_SUITE_NAME
    assert summary["continuity_suite_name"] == CROSS_SURFACE_REACH_CONTINUITY_V2_SUITE_NAME
    assert summary["false_claim_suite_name"] == REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SUITE_NAME
    assert summary["selected_channel_count"] >= 6
    assert summary["channel_family_count"] >= 5
    assert summary["recorded_live_or_degraded_window_count"] >= 6
    assert summary["paired_revocation_count"] >= 6
    assert summary["rate_abuse_degraded_recovery_count"] >= 6
    assert summary["coverage_gap_count"] >= 1
    assert summary["false_delivery_count"] >= 1
    assert summary["missed_delivery_count"] >= 1
    assert summary["voice_media_candidate_count"] >= 5
    assert summary["voice_media_quality_pass_count"] >= 5
    assert summary["voice_media_latency_pass_count"] >= 5
    assert summary["voice_media_privacy_control_count"] >= 5
    assert summary["voice_media_regression_fallback_count"] >= 5
    assert summary["incident_count"] >= 5
    assert summary["incident_fallback_count"] >= 5
    assert summary["operator_repair_action_count"] >= 5
    assert summary["revocation_fail_closed_count"] >= 1
    assert summary["continuity_path_count"] >= 5
    assert summary["continuity_preserved_count"] >= 5
    assert summary["safe_receipt_count"] >= 23
    assert summary["claim_boundary"] == REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY
    assert set(REACH_VOICE_PRODUCTION_OPS_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])


def test_reach_voice_production_ops_receipts_are_redacted_and_boundary_scoped():
    contract = build_reach_voice_production_ops_contract()

    gap = next(item for item in contract["live_reach_operations"] if item["coverage_gap"])
    assert gap["consent_pairing"]["pairing_state"] == "requires_pairing"
    assert gap["recovery"]["degraded_state"] == "closed_until_pairing"
    assert gap["provider_identity"]["credential_scope"] == "no_credential_configured"

    for receipt in contract["live_reach_operations"]:
        assert receipt["operator_surface"] == "/api/operator/reach-voice-production-ops"
        assert receipt["provider_identity"]["session_or_profile_owner"] == "originating_operator_session"
        assert "credential_scope" in receipt["provider_identity"]
        assert receipt["consent_pairing"]["revoked_probe_blocks_delivery"] is True
        assert receipt["limits"]["unsafe_follow_up_hidden"] is True
        assert receipt["recovery"]["offline_recovery_tested"] is True
        assert receipt["network_boundary"]["destination_policy"] == "provider_allowlist_with_private_network_denial"
        assert receipt["network_boundary"]["filesystem_root"] == "no_filesystem_payload_export"

    for candidate in contract["voice_media_production_candidates"]:
        assert candidate["quality_gate"]["passed"] is True
        assert candidate["latency_gate"]["passed"] is True
        assert candidate["correction_deletion_privacy"]["content_redacted"] is True
        assert candidate["correction_deletion_privacy"]["deletion_path"]
        assert candidate["correction_deletion_privacy"]["revocation_blocks_capture"] is True
        assert candidate["provider_regression"]["unsafe_action_allowed"] is False
        assert candidate["private_data_boundary"] == REACH_VOICE_SAFE_REDACTION_BOUNDARY

    for incident in contract["channel_incident_response"]:
        assert incident["fallback_exercised"] is True
        assert incident["offline_recovery_tested"] is True
        assert incident["unsafe_mutation_blocked"] is True
        assert incident["operator_visible"] is True

    for continuity in contract["cross_surface_reach_continuity_v2"]:
        assert continuity["thread_preserved"] is True
        assert continuity["memory_context_preserved"] is True
        assert continuity["approval_state_preserved"] is True
        assert continuity["notification_state_preserved"] is True
        assert continuity["operator_handoff_preserved"] is True
        assert continuity["replay_authority"] == "operator_review_required_before_replay"

    for receipt_group in _receipt_groups(contract):
        for receipt in receipt_group:
            safe_receipt = receipt["safe_receipt"]
            assert safe_receipt["redacted_receipt_handle"].startswith("seraph://receipts/batch-dk/")
            assert safe_receipt["redaction_boundary"] == REACH_VOICE_SAFE_REDACTION_BOUNDARY
            assert safe_receipt["stored_payload_mode"] == "metadata_only_redacted_receipt"
            assert safe_receipt["contains_message_body"] is False
            assert safe_receipt["contains_contact_identifier"] is False
            assert safe_receipt["contains_secret"] is False
            assert safe_receipt["contains_transcript"] is False
            assert safe_receipt["contains_audio_payload"] is False
            assert safe_receipt["contains_media_payload"] is False


def test_reach_voice_production_ops_false_claim_scan_blocks_overclaims():
    contract = build_reach_voice_production_ops_contract()
    scan = contract["false_claim_scan_receipts"][0]

    assert scan["validation_command"] == "python3 scripts/check_strategy_claims.py"
    assert scan["blocked_claims_found"] == []
    assert scan["forbidden_hit_count"] == 0
    assert "openclaw_class_reach" in scan["blocked_claims_checked"]
    assert "voice_media_parity" in scan["blocked_claims_checked"]
    assert "production_ready_product" in scan["blocked_claims_checked"]
    assert "full_parity" in scan["blocked_claims_checked"]
    assert "reference_systems_exceeded" in scan["blocked_claims_checked"]


@pytest.mark.asyncio
async def test_reach_voice_production_ops_report_exposes_ci_gated_posture():
    report = await build_reach_voice_production_ops_report()

    assert report["summary"]["benchmark_posture"] == "reach_voice_production_ops_ci_gated_operator_visible"
    assert report["summary"]["operator_status"] == "reach_voice_production_ops_receipts_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["scenario_count"] == 18
    assert report["summary"]["claim_boundary"] == REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY
    assert report["scenario_names"][ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SUITE_NAME] == list(
        ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SCENARIO_NAMES
    )
    assert report["scenario_names"][VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SUITE_NAME] == list(
        VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SCENARIO_NAMES
    )
    assert report["scenario_names"][CHANNEL_INCIDENT_RESPONSE_V1_SUITE_NAME] == list(
        CHANNEL_INCIDENT_RESPONSE_V1_SCENARIO_NAMES
    )
    assert report["scenario_names"][CROSS_SURFACE_REACH_CONTINUITY_V2_SUITE_NAME] == list(
        CROSS_SURFACE_REACH_CONTINUITY_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SUITE_NAME] == list(
        REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES
    )
    assert "/api/operator/reach-voice-production-ops" in report["policy"]["receipt_surfaces"]
