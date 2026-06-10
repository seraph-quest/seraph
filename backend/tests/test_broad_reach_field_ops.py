import pytest

from src.extensions.field_reach_operations import (
    ALWAYS_AVAILABLE_REACH_SLO_SUITE_NAME,
    BROAD_REACH_FIELD_OPERATIONS_SUITE_NAME,
    BROAD_REACH_FIELD_OPS_BLOCKED_CLAIMS,
    BROAD_REACH_FIELD_OPS_CLAIM_BOUNDARY,
    VOICE_MEDIA_QUALITY_OPERATIONS_SUITE_NAME,
    build_broad_reach_field_ops_contract,
    build_broad_reach_field_ops_report,
)


def test_broad_reach_field_ops_contract_exposes_batch_cu_receipts():
    contract = build_broad_reach_field_ops_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["field_operations_suite_name"] == BROAD_REACH_FIELD_OPERATIONS_SUITE_NAME
    assert summary["voice_media_suite_name"] == VOICE_MEDIA_QUALITY_OPERATIONS_SUITE_NAME
    assert summary["slo_suite_name"] == ALWAYS_AVAILABLE_REACH_SLO_SUITE_NAME
    assert summary["channel_provider_count"] >= 6
    assert summary["paired_channel_count"] >= 5
    assert summary["recorded_live_field_window_count"] >= 2
    assert summary["auth_consent_revocation_visible_count"] >= 6
    assert summary["field_window_met_count"] >= 5
    assert summary["rate_limit_abuse_drill_count"] >= 6
    assert summary["degraded_recovery_drill_count"] >= 6
    assert summary["continuity_receipt_count"] >= 6
    assert summary["safe_receipt_redaction_count"] >= 12
    assert summary["coverage_gap_count"] >= 4
    assert summary["voice_media_quality_gate_pass_count"] >= 4
    assert summary["voice_media_latency_gate_pass_count"] >= 4
    assert summary["voice_media_privacy_control_count"] >= 4
    assert summary["voice_media_memory_boundary_count"] >= 4
    assert summary["slo_budget_met_count"] >= 2
    assert summary["provider_failure_recovery_count"] >= 2
    assert summary["offline_recovery_count"] >= 2
    assert summary["operator_recovery_action_count"] >= 7
    assert policy["claim_boundary"] == BROAD_REACH_FIELD_OPS_CLAIM_BOUNDARY
    assert set(BROAD_REACH_FIELD_OPS_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])


def test_broad_reach_field_ops_contract_blocks_overclaims_and_unsafe_paths():
    contract = build_broad_reach_field_ops_contract()

    signal = next(
        item
        for item in contract["provider_channel_field_matrix"]
        if item["provider"] == "signal-bridge"
    )
    assert signal["operator_identity"]["pairing_state"] == "requires_pairing"
    assert signal["field_window"]["window_met"] is False
    assert signal["degraded_recovery"]["unsafe_mutation_blocked"] is True

    for receipt in contract["provider_channel_field_matrix"]:
        assert receipt["operator_identity"]["consent_receipt_id"]
        assert receipt["operator_identity"]["revoked_probe_blocks_delivery"] is True
        assert receipt["abuse_handling"]["unsafe_follow_up_hidden"] is True
        assert receipt["continuity"]["thread_preserved"] is True
        assert receipt["continuity"]["approval_state_preserved"] is True
        assert receipt["continuity"]["continuity_id"].startswith("continuity:reach-cu:")
        assert receipt["continuity"]["replay_authority"] == "operator_review_required_before_replay"
        assert receipt["safe_receipt"]["stored_payload_mode"] == "metadata_only_redacted_receipt"
        assert receipt["safe_receipt"]["contains_message_body"] is False
        assert receipt["safe_receipt"]["contains_secret"] is False
        assert receipt["safe_receipt"]["contains_contact_identifier"] is False

    for operation in contract["voice_media_quality_operations"]:
        assert operation["quality_gate"]["passed"] is True
        assert operation["latency_gate"]["passed"] is True
        assert operation["privacy"]["content_redacted"] is True
        assert operation["operator_controls"]["deletion_path"]
        assert operation["operator_controls"]["correction_path"]
        assert operation["operator_controls"]["revocation_blocks_capture"] is True
        assert operation["operator_controls"]["replay_authority"] == "operator_review_required_before_replay"
        assert operation["safe_receipt"]["contains_transcript"] is False
        assert operation["safe_receipt"]["contains_audio_payload"] is False
        assert operation["safe_receipt"]["contains_media_payload"] is False

    for slo_receipt in contract["reach_slo_operations"]:
        assert slo_receipt["continuity"]["continuity_id"].startswith("continuity:reach-cu:")
        assert slo_receipt["continuity"]["replay_authority"] == "operator_review_required_before_replay"
        assert slo_receipt["safe_receipt"]["stored_payload_mode"] == "metadata_only_redacted_receipt"
        assert slo_receipt["safe_receipt"]["contains_message_body"] is False

    assert "always_available_operation" in contract["policy"]["blocked_claims"]
    assert "openclaw_class_reach" in contract["policy"]["blocked_claims"]
    assert "voice_parity" in contract["policy"]["blocked_claims"]


@pytest.mark.asyncio
async def test_broad_reach_field_ops_report_exposes_ci_gated_posture():
    report = await build_broad_reach_field_ops_report()

    assert report["summary"]["benchmark_posture"] == "broad_reach_field_ops_ci_gated_operator_visible"
    assert report["summary"]["operator_status"] == "broad_reach_field_ops_receipts_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["scenario_count"] == 13
    assert report["policy"]["claim_boundary"] == BROAD_REACH_FIELD_OPS_CLAIM_BOUNDARY
    assert "/api/operator/broad-reach-field-ops" in report["policy"]["receipt_surfaces"]
