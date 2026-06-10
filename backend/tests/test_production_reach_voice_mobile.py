import pytest

from src.extensions.production_reach_voice_mobile import (
    BROAD_CHANNEL_SLA_OPERATIONS_SUITE_NAME,
    MOBILE_EXECUTION_CONTINUITY_SUITE_NAME,
    PRODUCTION_REACH_VOICE_MOBILE_BLOCKED_CLAIMS,
    PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY,
    PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SUITE_NAME,
    build_production_reach_voice_mobile_contract,
    build_production_reach_voice_mobile_report,
)


def test_production_reach_voice_mobile_contract_exposes_batch_cl_receipts():
    contract = build_production_reach_voice_mobile_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["channel_suite_name"] == BROAD_CHANNEL_SLA_OPERATIONS_SUITE_NAME
    assert summary["voice_media_suite_name"] == PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SUITE_NAME
    assert summary["mobile_suite_name"] == MOBILE_EXECUTION_CONTINUITY_SUITE_NAME
    assert summary["channel_provider_count"] >= 4
    assert summary["recorded_live_channel_count"] >= 3
    assert summary["paired_channel_count"] >= 3
    assert summary["sla_window_visible_count"] >= 3
    assert summary["rate_limit_abuse_visible_count"] >= 4
    assert summary["degraded_recovery_visible_count"] >= 4
    assert summary["coverage_gap_visible_count"] >= 2
    assert summary["voice_media_quality_gate_pass_count"] >= 3
    assert summary["voice_media_latency_gate_pass_count"] >= 3
    assert summary["voice_media_privacy_boundary_count"] >= 3
    assert summary["voice_media_regression_fallback_count"] >= 3
    assert summary["mobile_approval_handoff_count"] >= 2
    assert summary["mobile_action_continuity_count"] >= 2
    assert summary["mobile_memory_continuity_count"] >= 2
    assert summary["mobile_revocation_fail_closed_count"] >= 2
    assert policy["claim_boundary"] == PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY
    assert set(PRODUCTION_REACH_VOICE_MOBILE_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])


def test_production_reach_voice_mobile_contract_blocks_unpaired_and_unsafe_paths():
    contract = build_production_reach_voice_mobile_contract()

    matrix = next(item for item in contract["channel_sla_receipts"] if item["transport"] == "matrix")
    assert matrix["pairing_state"] == "requires_pairing"
    assert matrix["sla"]["window_met"] is False
    assert matrix["degraded_recovery"]["unsafe_follow_up_hidden"] is True

    for runtime in contract["voice_media_quality_receipts"]:
        assert runtime["quality_gate"]["passed"] is True
        assert runtime["privacy"]["content_redacted"] is True
        assert runtime["operator_controls"]["deletion_path"]
        assert runtime["operator_controls"]["correction_path"]
        assert runtime["operator_controls"]["revocation_blocks_capture"] is True
        assert runtime["provider_regression"]["unsafe_action_allowed"] is False

    for mobile in contract["mobile_execution_receipts"]:
        assert mobile["approval_handoff"]["mutation_allowed_without_approval"] is False
        assert mobile["memory_continuity"]["memory_import_requires_review"] is True
        assert mobile["revocation"]["revoked_device_blocks_action"] is True


@pytest.mark.asyncio
async def test_production_reach_voice_mobile_report_exposes_ci_gated_posture():
    report = await build_production_reach_voice_mobile_report()

    assert report["summary"]["benchmark_posture"] == "production_reach_voice_mobile_ci_gated_operator_visible"
    assert report["summary"]["operator_status"] == "production_reach_voice_mobile_receipts_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["scenario_count"] == 12
    assert report["policy"]["claim_boundary"] == PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY
    assert "/api/operator/production-reach-voice-mobile" in report["policy"]["receipt_surfaces"]
