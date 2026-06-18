import pytest

from src.extensions.post_dx_reach_voice_media_parity import (
    POST_DX_REACH_VOICE_MEDIA_BLOCKED_CLAIMS,
    POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
    POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SUITE_NAME,
    POST_DX_REACH_VOICE_MEDIA_SAFE_REDACTION_BOUNDARY,
    build_post_dx_reach_voice_media_contract,
    build_post_dx_reach_voice_media_report,
)


def test_post_dx_reach_voice_media_contract_preserves_boundaries():
    contract = build_post_dx_reach_voice_media_contract()

    assert contract["summary"]["suite_name"] == "post_dx_reach_voice_media_parity_proof"
    assert contract["summary"]["operator_status"] == "post_dx_reach_voice_media_parity_proof_visible"
    assert contract["summary"]["reliability_surface_count"] >= 5
    assert contract["summary"]["candidate_surface_count"] >= 2
    assert contract["summary"]["voice_media_quality_pass_count"] >= 4
    assert contract["summary"]["abuse_recovery_count"] >= 4
    assert contract["summary"]["continuity_preserved_count"] >= 4
    assert contract["summary"]["coverage_gap_count"] >= 4
    assert contract["policy"]["claim_boundary"] == POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY
    assert set(POST_DX_REACH_VOICE_MEDIA_BLOCKED_CLAIMS) <= set(contract["policy"]["blocked_claims"])
    assert all(
        item["runtime_fetch_performed"] is False
        and item["candidate_or_non_live_marker"]
        and item["residual_risk"]
        for item in contract["reach_abuse_recovery"]
    )
    assert all(
        item["runtime_fetch_performed"] is False
        and item["candidate_or_non_live_marker"]
        and item["residual_risk"]
        for item in contract["cross_surface_reach_continuity"]
    )
    assert all(
        "backend/src/api/operator.py" in item["scan_scope"]
        and "backend/src/evals/benchmark_catalog.py" in item["scan_scope"]
        and "backend/src/evals/harness.py" in item["scan_scope"]
        for item in contract["false_claim_scan_receipts"]
    )


def test_post_dx_reach_voice_media_receipts_are_redacted_and_bounded():
    contract = build_post_dx_reach_voice_media_contract()
    receipt_groups = (
        "post_dx_reach_voice_media_receipts",
        "multi_channel_reliability",
        "voice_media_quality_latency",
        "reach_abuse_recovery",
        "cross_surface_reach_continuity",
        "false_claim_scan_receipts",
    )

    for group_name in receipt_groups:
        for receipt in contract[group_name]:
            safe_receipt = receipt["safe_receipt"]
            assert receipt["claim_boundary"] == POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY
            assert safe_receipt["redaction_boundary"] == POST_DX_REACH_VOICE_MEDIA_SAFE_REDACTION_BOUNDARY
            assert safe_receipt["contains_message_body"] is False
            assert safe_receipt["contains_contact_identifier"] is False
            assert safe_receipt["contains_secret"] is False
            assert safe_receipt["contains_transcript"] is False
            assert safe_receipt["contains_audio_payload"] is False
            assert safe_receipt["contains_media_payload"] is False
            assert safe_receipt["contains_location_payload"] is False


@pytest.mark.asyncio
async def test_post_dx_reach_voice_media_report_runs_all_suites(monkeypatch):
    class Summary:
        total = 24
        passed = 24
        failed = 0
        duration_ms = 12
        results = []

    async def fake_run(suites):
        assert suites == [
            "post_dx_reach_voice_media_parity_proof_v1",
            "multi_channel_reach_reliability_v3",
            "voice_media_quality_latency_v3",
            "reach_abuse_recovery_v3",
            "cross_surface_reach_continuity_v3",
            "reach_voice_media_false_claim_scan_v3",
        ]
        return Summary()

    monkeypatch.setattr("src.evals.harness.run_benchmark_suites", fake_run)

    payload = await build_post_dx_reach_voice_media_report()

    assert payload["summary"]["benchmark_posture"] == "bounded_post_dx_reach_voice_media_parity_proof"
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["scenario_names"][POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SUITE_NAME]
