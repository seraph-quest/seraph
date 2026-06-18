"""Tests for Batch DS post-DP reach/channel gap-closure receipts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.extensions.post_dp_reach_channel_gap_closure import (
    CHANNEL_DEGRADED_RECOVERY_V2_SCENARIO_NAMES,
    CHANNEL_DEGRADED_RECOVERY_V2_SUITE_NAME,
    GUARDIAN_REACH_CONTINUITY_V2_SCENARIO_NAMES,
    GUARDIAN_REACH_CONTINUITY_V2_SUITE_NAME,
    POST_DP_REACH_CHANNEL_BLOCKED_CLAIMS,
    POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
    POST_DP_REACH_CHANNEL_GAP_CLOSURE_SCENARIO_NAMES,
    POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME,
    POST_DP_REACH_SAFE_REDACTION_BOUNDARY,
    REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES,
    REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    SELECTED_REACH_SURFACE_READINESS_V2_SCENARIO_NAMES,
    SELECTED_REACH_SURFACE_READINESS_V2_SUITE_NAME,
    VOICE_MEDIA_PRIVACY_FALLBACK_V2_SCENARIO_NAMES,
    VOICE_MEDIA_PRIVACY_FALLBACK_V2_SUITE_NAME,
    build_post_dp_reach_channel_contract,
    build_post_dp_reach_channel_report,
)


def test_post_dp_reach_channel_contract_covers_ds_acceptance_fields():
    contract = build_post_dp_reach_channel_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "post_dp_reach_channel_gap_closure_visible"
    assert summary["aggregate_receipt_count"] == len(POST_DP_REACH_CHANNEL_GAP_CLOSURE_SCENARIO_NAMES)
    assert summary["selected_surface_count"] >= 2
    assert summary["paired_revocation_count"] == summary["selected_surface_count"]
    assert summary["provider_outage_behavior_count"] >= 2
    assert summary["staged_channel_gap_count"] >= 3
    assert summary["degraded_recovery_count"] >= 4
    assert summary["rate_limit_abuse_policy_count"] >= 4
    assert summary["continuity_preserved_count"] >= 4
    assert summary["guardian_restraint_count"] >= 2
    assert summary["voice_media_privacy_fallback_count"] >= 4
    assert summary["claim_boundary"] == POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY
    assert set(POST_DP_REACH_CHANNEL_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/post-dp-reach-channel-gap-closure" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]

    for item in contract["selected_reach_surfaces"]:
        assert item["consent_current"] is True
        assert item["revocation_probe_blocks_delivery"] is True
        assert item["provider_identity_visible"] is True
        assert item["rate_limit_policy_visible"] is True
        assert item["abuse_handling_visible"] is True
        assert item["offline_recovery_tested"] is True
        assert item["approval_authority"] == "operator_owned"
        assert item["unsafe_action_authority_expanded"] is False

    for item in contract["guardian_reach_continuity"]:
        assert item["thread_preserved"] is True
        assert item["memory_context_preserved"] is True
        assert item["approval_state_preserved"] is True
        assert item["operator_handoff_preserved"] is True
        assert item["replay_authority"] == "operator_review_required_before_replay"


def test_post_dp_reach_channel_receipts_are_redacted_and_boundary_scoped():
    contract = build_post_dp_reach_channel_contract()
    serialized = json.dumps(contract, sort_keys=True)

    assert "Authorization: Bearer" not in serialized
    assert "sk-live" not in serialized.lower()
    assert "full_parity" in serialized
    assert "openclaw_class_reach" in serialized

    for group in (
        "post_dp_reach_channel_receipts",
        "selected_reach_surfaces",
        "channel_degraded_recovery",
        "guardian_reach_continuity",
        "voice_media_privacy_fallback",
        "false_claim_scan_receipts",
    ):
        for item in contract[group]:
            safe = item["safe_receipt"]
            assert safe["redacted_receipt_handle"].startswith("seraph://receipts/batch-ds/")
            assert safe["redaction_boundary"] == POST_DP_REACH_SAFE_REDACTION_BOUNDARY
            assert safe["contains_message_body"] is False
            assert safe["contains_contact_identifier"] is False
            assert safe["contains_secret"] is False
            assert safe["contains_transcript"] is False
            assert safe["contains_audio_payload"] is False
            assert safe["contains_media_payload"] is False


def test_post_dp_reach_channel_registered_scenarios_have_operator_receipts():
    contract = build_post_dp_reach_channel_contract()
    expected = {
        POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME: set(POST_DP_REACH_CHANNEL_GAP_CLOSURE_SCENARIO_NAMES),
        SELECTED_REACH_SURFACE_READINESS_V2_SUITE_NAME: set(SELECTED_REACH_SURFACE_READINESS_V2_SCENARIO_NAMES),
        CHANNEL_DEGRADED_RECOVERY_V2_SUITE_NAME: set(CHANNEL_DEGRADED_RECOVERY_V2_SCENARIO_NAMES),
        GUARDIAN_REACH_CONTINUITY_V2_SUITE_NAME: set(GUARDIAN_REACH_CONTINUITY_V2_SCENARIO_NAMES),
        VOICE_MEDIA_PRIVACY_FALLBACK_V2_SUITE_NAME: set(VOICE_MEDIA_PRIVACY_FALLBACK_V2_SCENARIO_NAMES),
        REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SUITE_NAME: set(REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES),
    }
    receipt_groups = (
        contract["post_dp_reach_channel_receipts"],
        contract["selected_reach_surfaces"],
        contract["channel_degraded_recovery"],
        contract["guardian_reach_continuity"],
        contract["voice_media_privacy_fallback"],
        contract["false_claim_scan_receipts"],
    )
    actual = {suite_name: set() for suite_name in expected}
    for item in [receipt for group in receipt_groups for receipt in group]:
        actual[item["suite_name"]].add(item["scenario_name"])
        assert item["operator_surface"] == "/api/operator/post-dp-reach-channel-gap-closure"

    assert actual == expected


def test_post_dp_reach_channel_report_runs_all_ds_suites():
    scenario_names = [
        *POST_DP_REACH_CHANNEL_GAP_CLOSURE_SCENARIO_NAMES,
        *SELECTED_REACH_SURFACE_READINESS_V2_SCENARIO_NAMES,
        *CHANNEL_DEGRADED_RECOVERY_V2_SCENARIO_NAMES,
        *GUARDIAN_REACH_CONTINUITY_V2_SCENARIO_NAMES,
        *VOICE_MEDIA_PRIVACY_FALLBACK_V2_SCENARIO_NAMES,
        *REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES,
    ]
    summary = SimpleNamespace(
        total=len(scenario_names),
        passed=len(scenario_names),
        failed=0,
        duration_ms=44,
        results=[SimpleNamespace(name=name, passed=True, error="") for name in scenario_names],
    )

    with patch(
        "src.extensions.post_dp_reach_channel_gap_closure._run_post_dp_reach_channel_suites",
        AsyncMock(return_value=summary),
    ):
        report = asyncio.run(build_post_dp_reach_channel_report())

    assert report["summary"]["benchmark_posture"] == "post_dp_reach_channel_ci_gated_operator_visible"
    assert report["summary"]["scenario_count"] == len(scenario_names)
    assert report["latest_run"]["failed"] == 0
    assert report["failure_report"] == []
    assert report["scenario_names"][POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME] == list(
        POST_DP_REACH_CHANNEL_GAP_CLOSURE_SCENARIO_NAMES
    )
    assert report["scenario_names"][SELECTED_REACH_SURFACE_READINESS_V2_SUITE_NAME] == list(
        SELECTED_REACH_SURFACE_READINESS_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][CHANNEL_DEGRADED_RECOVERY_V2_SUITE_NAME] == list(
        CHANNEL_DEGRADED_RECOVERY_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][GUARDIAN_REACH_CONTINUITY_V2_SUITE_NAME] == list(
        GUARDIAN_REACH_CONTINUITY_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][VOICE_MEDIA_PRIVACY_FALLBACK_V2_SUITE_NAME] == list(
        VOICE_MEDIA_PRIVACY_FALLBACK_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SUITE_NAME] == list(
        REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES
    )
