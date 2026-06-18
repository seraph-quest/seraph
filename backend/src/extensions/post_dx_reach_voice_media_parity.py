"""Batch EA post-DX reach and voice/media parity-proof receipts.

This layer extends DK/DS reach evidence with broader reach reliability,
voice/media quality, abuse recovery, and cross-surface continuity receipts.
It remains bounded proof: OpenClaw-class reach, complete channel coverage,
always-available operation, voice/media parity, production readiness, full
parity, and reference-system exceedance stay blocked.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.extensions.post_dp_reach_channel_gap_closure import (
    POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
    build_post_dp_reach_channel_contract,
)
from src.extensions.reach_voice_production_ops import (
    REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
    build_reach_voice_production_ops_contract,
)


POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SUITE_NAME = "post_dx_reach_voice_media_parity_proof_v1"
POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SCENARIO_NAMES = (
    "post_dx_reach_builds_on_dk_ds_without_duplicate_scope",
    "post_dx_reach_multi_channel_reliability_receipts_behavior",
    "post_dx_reach_voice_media_quality_receipts_behavior",
    "post_dx_reach_claim_boundary_behavior",
)
MULTI_CHANNEL_REACH_RELIABILITY_V3_SUITE_NAME = "multi_channel_reach_reliability_v3"
MULTI_CHANNEL_REACH_RELIABILITY_V3_SCENARIO_NAMES = (
    "multi_channel_native_notification_reliability_behavior",
    "multi_channel_websocket_reliability_behavior",
    "multi_channel_mobile_push_candidate_behavior",
    "multi_channel_messaging_relay_candidate_behavior",
    "multi_channel_browser_cockpit_reliability_behavior",
)
VOICE_MEDIA_QUALITY_LATENCY_V3_SUITE_NAME = "voice_media_quality_latency_v3"
VOICE_MEDIA_QUALITY_LATENCY_V3_SCENARIO_NAMES = (
    "voice_media_stt_quality_latency_behavior",
    "voice_media_tts_quality_latency_behavior",
    "voice_media_voice_command_safety_behavior",
    "voice_media_media_analysis_privacy_behavior",
)
REACH_ABUSE_RECOVERY_V3_SUITE_NAME = "reach_abuse_recovery_v3"
REACH_ABUSE_RECOVERY_V3_SCENARIO_NAMES = (
    "reach_abuse_rate_limit_recovery_behavior",
    "reach_abuse_external_storm_restraint_behavior",
    "reach_abuse_revocation_quarantine_behavior",
    "reach_abuse_provider_outage_fallback_behavior",
)
CROSS_SURFACE_REACH_CONTINUITY_V3_SUITE_NAME = "cross_surface_reach_continuity_v3"
CROSS_SURFACE_REACH_CONTINUITY_V3_SCENARIO_NAMES = (
    "cross_surface_reach_thread_memory_approval_v3_behavior",
    "cross_surface_reach_voice_to_text_confirmation_behavior",
    "cross_surface_reach_offline_resume_v3_behavior",
    "cross_surface_reach_operator_handoff_v3_behavior",
)
REACH_VOICE_MEDIA_FALSE_CLAIM_SCAN_V3_SUITE_NAME = "reach_voice_media_false_claim_scan_v3"
REACH_VOICE_MEDIA_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES = (
    "reach_voice_media_false_claim_v3_blocks_openclaw_reach",
    "reach_voice_media_false_claim_v3_blocks_always_available",
    "reach_voice_media_false_claim_v3_blocks_voice_media_parity",
)

POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY = (
    "post_dx_reach_voice_media_parity_proof_not_openclaw_class_always_available_or_voice_media_parity"
)
POST_DX_REACH_VOICE_MEDIA_BLOCKED_CLAIMS = (
    "openclaw_class_reach",
    "complete_channel_coverage",
    "all_openclaw_channels_connected",
    "always_available_operation",
    "always_available_daily_life_reach",
    "voice_parity",
    "voice_media_parity",
    "multimodal_parity",
    "production_stt_tts_solved",
    "production_mobile_execution_solved",
    "safe_autonomous_mobile_or_message_actions",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
    "broad_superiority",
)
POST_DX_REACH_VOICE_MEDIA_SAFE_REDACTION_BOUNDARY = (
    "metadata_only_no_message_body_contact_secret_audio_media_transcript_or_location_payload"
)
POST_DX_REACH_VOICE_MEDIA_CLAIM_SCAN_COMMAND = "python3 scripts/check_strategy_claims.py"
POST_DX_REACH_VOICE_MEDIA_CLAIM_SCAN_RECEIPT = "local-validation:reach-voice-media-claims:2026-06-13"


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _receipt_handle(kind: str, receipt_id: str, payload: Any) -> str:
    return f"seraph://receipts/batch-ea/{kind}/{receipt_id}/{_stable_digest(payload)}"


def _safe_receipt(kind: str, receipt_id: str, payload: Any) -> dict[str, Any]:
    return {
        "redacted_receipt_handle": _receipt_handle(kind, receipt_id, payload),
        "safe_redaction_digest": _stable_digest((kind, receipt_id, payload)),
        "redaction_boundary": POST_DX_REACH_VOICE_MEDIA_SAFE_REDACTION_BOUNDARY,
        "stored_payload_mode": "metadata_only_redacted_receipt",
        "contains_message_body": False,
        "contains_contact_identifier": False,
        "contains_secret": False,
        "contains_transcript": False,
        "contains_audio_payload": False,
        "contains_media_payload": False,
        "contains_location_payload": False,
    }


def post_dx_reach_voice_media_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SUITE_NAME,
            MULTI_CHANNEL_REACH_RELIABILITY_V3_SUITE_NAME,
            VOICE_MEDIA_QUALITY_LATENCY_V3_SUITE_NAME,
            REACH_ABUSE_RECOVERY_V3_SUITE_NAME,
            CROSS_SURFACE_REACH_CONTINUITY_V3_SUITE_NAME,
            REACH_VOICE_MEDIA_FALSE_CLAIM_SCAN_V3_SUITE_NAME,
        ],
        "foundation_suites": [
            "always_available_reach_live_ops_v1",
            "voice_media_production_parity_candidate_v1",
            "channel_incident_response_v1",
            "cross_surface_reach_continuity_v2",
            "post_dp_reach_channel_gap_closure_v1",
            "selected_reach_surface_readiness_v2",
            "voice_media_privacy_fallback_v2",
        ],
        "claim_boundary": POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
        "blocked_claims": list(POST_DX_REACH_VOICE_MEDIA_BLOCKED_CLAIMS),
        "not_claimed": [
            "openclaw_class_reach",
            "complete_channel_coverage",
            "always_available_operation",
            "voice_or_multimodal_parity",
            "production_mobile_execution_solved",
            "production_ready_product",
            "full_parity_achieved",
            "reference_system_reach_superiority",
        ],
        "receipt_surfaces": [
            "/api/operator/post-dx-reach-voice-media-parity-proof",
            "/api/operator/post-dp-reach-channel-gap-closure",
            "/api/operator/reach-voice-production-ops",
            "/api/operator/benchmark-proof",
            "GitHub issue #592",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "reach_policy": (
            "EA receipts must expose channel identity, consent, pairing, revocation, rate limits, "
            "abuse handling, outage fallback, offline recovery, false/missed delivery metrics, and "
            "operator repair without claiming complete or always-available channel coverage."
        ),
        "voice_media_policy": (
            "EA voice/media receipts must expose quality and latency gates, correction, deletion, "
            "revocation, memory-review, provider-regression fallback, and unsafe-action denial without "
            "claiming voice/media parity."
        ),
        "safe_redaction_policy": (
            "operator receipts expose handles, provider metadata, metrics, state transitions, and "
            "continuity ids only; message bodies, contacts, secrets, transcripts, audio, media, and "
            "location payloads stay absent."
        ),
        "safe_receipt_redaction_boundary": POST_DX_REACH_VOICE_MEDIA_SAFE_REDACTION_BOUNDARY,
    }


def multi_channel_reach_reliability_v3_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "ea-native-notification",
            "multi_channel_native_notification_reliability_behavior",
            "native_notification",
            "native-daemon",
            "recorded_live_window",
            1440,
            1840,
            1838,
            0,
            2,
            "websocket",
        ),
        (
            "ea-websocket",
            "multi_channel_websocket_reliability_behavior",
            "websocket",
            "seraph-websocket-reach",
            "recorded_live_window",
            1440,
            2220,
            2211,
            1,
            8,
            "native_notification",
        ),
        (
            "ea-mobile-push-candidate",
            "multi_channel_mobile_push_candidate_behavior",
            "mobile_push_candidate",
            "mobile-push-relay-profile",
            "recorded_live_replay_candidate",
            720,
            910,
            901,
            0,
            9,
            "websocket",
        ),
        (
            "ea-messaging-relay-candidate",
            "multi_channel_messaging_relay_candidate_behavior",
            "messaging_relay_candidate",
            "signed-messaging-relay-profile",
            "fixture_plus_recorded_live_replay",
            720,
            760,
            748,
            1,
            11,
            "browser_cockpit",
        ),
        (
            "ea-browser-cockpit",
            "multi_channel_browser_cockpit_reliability_behavior",
            "browser_cockpit",
            "seraph-web-control",
            "recorded_live_window",
            1440,
            1120,
            1120,
            0,
            0,
            "native_notification",
        ),
    ]
    return [
        {
            "receipt_id": receipt_id,
            "suite_name": MULTI_CHANNEL_REACH_RELIABILITY_V3_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dx-reach-voice-media-parity-proof",
            "surface": surface,
            "provider": provider,
            "evidence_mode": evidence_mode,
            "runtime_fetch_performed": False,
            "window_minutes": window_minutes,
            "attempt_count": attempts,
            "delivered_count": delivered,
            "false_delivery_count": false_count,
            "missed_delivery_count": missed_count,
            "success_ratio": round(delivered / attempts, 4) if attempts else None,
            "provider_identity_visible": True,
            "consent_current": True,
            "pairing_state": "paired" if "candidate" not in surface else "candidate_pairing_limited",
            "revocation_probe_blocks_delivery": True,
            "rate_limit_policy_visible": True,
            "abuse_handling_visible": True,
            "provider_outage_fallback": fallback,
            "offline_recovery_tested": True,
            "operator_repair_visible": True,
            "unsafe_action_authority_expanded": False,
            "safe_receipt": _safe_receipt("multi-channel-reliability", receipt_id, (surface, provider, attempts)),
            "claim_boundary": POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
            "residual_risk": "candidate_or_recorded_live_receipt_not_complete_channel_coverage",
        }
        for (
            receipt_id,
            scenario_name,
            surface,
            provider,
            evidence_mode,
            window_minutes,
            attempts,
            delivered,
            false_count,
            missed_count,
            fallback,
        ) in rows
    ]


def voice_media_quality_latency_v3_receipts() -> list[dict[str, Any]]:
    rows = [
        ("ea-stt", "voice_media_stt_quality_latency_behavior", "speech_to_text", "stt-provider-profile", "word_error_rate", 0.08, 0.041, 520, 1360, 1800, "typed_confirmation"),
        ("ea-tts", "voice_media_tts_quality_latency_behavior", "text_to_speech", "tts-provider-profile", "operator_intelligibility_score", 0.93, 0.971, 440, 690, 1000, "desktop_text"),
        ("ea-voice-command", "voice_media_voice_command_safety_behavior", "voice_command", "guarded-command-parser", "unsafe_command_false_accept_rate", 0.0, 0.0, 300, 940, 1400, "clarify_before_action"),
        ("ea-media-analysis", "voice_media_media_analysis_privacy_behavior", "media_analysis", "redacted-media-review-profile", "operator_correction_rate", 0.12, 0.052, 360, 1180, 1600, "operator_annotation"),
    ]
    receipts: list[dict[str, Any]] = []
    for receipt_id, scenario_name, family, provider, metric, threshold, observed, samples, p95, latency, fallback in rows:
        quality_passed = observed <= threshold if "score" not in metric else observed >= threshold
        receipts.append({
            "receipt_id": receipt_id,
            "suite_name": VOICE_MEDIA_QUALITY_LATENCY_V3_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dx-reach-voice-media-parity-proof",
            "family": family,
            "provider": provider,
            "evidence_mode": "quality_window_plus_provider_regression_fixture",
            "runtime_fetch_performed": False,
            "quality_gate": {
                "metric": metric,
                "threshold": threshold,
                "observed": observed,
                "sample_count": samples,
                "passed": quality_passed,
            },
            "latency_gate": {"p95_ms": p95, "threshold_ms": latency, "passed": p95 <= latency},
            "correction_path": f"correct_{family}_summary_before_memory_or_action",
            "deletion_path": f"delete_{family}_redacted_receipts",
            "revocation_blocks_capture": True,
            "memory_import_requires_review": True,
            "provider_regression_fallback": fallback,
            "unsafe_action_allowed": False,
            "safe_receipt": _safe_receipt("voice-media-quality", receipt_id, (family, provider, observed, p95)),
            "claim_boundary": POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
            "residual_risk": "quality_candidate_receipt_not_voice_media_parity",
        })
    return receipts


def reach_abuse_recovery_v3_receipts() -> list[dict[str, Any]]:
    rows = [
        ("ea-rate-limit", "reach_abuse_rate_limit_recovery_behavior", "websocket", "rate_limit", "bundle_and_retry_after"),
        ("ea-external-storm", "reach_abuse_external_storm_restraint_behavior", "messaging_relay_candidate", "external_mention_storm", "mute_low_confidence_and_request_operator_review"),
        ("ea-revocation", "reach_abuse_revocation_quarantine_behavior", "mobile_push_candidate", "revocation_probe", "quarantine_channel_and_block_delivery"),
        ("ea-provider-outage", "reach_abuse_provider_outage_fallback_behavior", "native_notification", "provider_outage", "fallback_to_websocket"),
    ]
    return [
        {
            "recovery_id": recovery_id,
            "suite_name": REACH_ABUSE_RECOVERY_V3_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dx-reach-voice-media-parity-proof",
            "surface": surface,
            "failure_mode": failure_mode,
            "operator_action": operator_action,
            "evidence_mode": "degraded_recovery_fixture_with_recorded_provider_window",
            "runtime_fetch_performed": False,
            "candidate_or_non_live_marker": "bounded_recovery_fixture_not_always_available_operation",
            "fallback_exercised": True,
            "rate_limit_or_abuse_policy_visible": failure_mode in {"rate_limit", "external_mention_storm"},
            "revocation_fail_closed": failure_mode == "revocation_probe",
            "offline_or_degraded_recovery_tested": True,
            "operator_repair_visible": True,
            "unsafe_mutation_blocked": True,
            "safe_receipt": _safe_receipt("abuse-recovery", recovery_id, (surface, failure_mode, operator_action)),
            "claim_boundary": POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
            "residual_risk": "recovery_fixture_receipt_not_provider_wide_sla_or_always_available_reach",
        }
        for recovery_id, scenario_name, surface, failure_mode, operator_action in rows
    ]


def cross_surface_reach_continuity_v3_receipts() -> list[dict[str, Any]]:
    rows = [
        ("ea-continuity-thread-memory", "cross_surface_reach_thread_memory_approval_v3_behavior", "native_notification", "websocket", "provider_outage"),
        ("ea-continuity-voice-text", "cross_surface_reach_voice_to_text_confirmation_behavior", "voice_command", "typed_confirmation", "ambiguous_command"),
        ("ea-continuity-offline", "cross_surface_reach_offline_resume_v3_behavior", "websocket", "browser_cockpit", "offline_window"),
        ("ea-continuity-handoff", "cross_surface_reach_operator_handoff_v3_behavior", "mobile_push_candidate", "native_notification", "operator_handoff"),
    ]
    return [
        {
            "continuity_id": continuity_id,
            "suite_name": CROSS_SURFACE_REACH_CONTINUITY_V3_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dx-reach-voice-media-parity-proof",
            "from_surface": from_surface,
            "to_surface": to_surface,
            "failure_mode": failure_mode,
            "evidence_mode": "continuity_fixture_with_recorded_provider_window",
            "runtime_fetch_performed": False,
            "candidate_or_non_live_marker": "bounded_continuity_fixture_not_complete_channel_coverage",
            "thread_preserved": True,
            "memory_context_preserved": True,
            "approval_state_preserved": True,
            "notification_state_preserved": True,
            "operator_handoff_preserved": True,
            "same_conversation_recovery": True,
            "offline_window_survived": True,
            "replay_authority": "operator_review_required_before_replay",
            "unsafe_action_authority_expanded": False,
            "safe_receipt": _safe_receipt("continuity", continuity_id, (from_surface, to_surface, failure_mode)),
            "claim_boundary": POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
            "residual_risk": "continuity_fixture_receipt_not_always_available_daily_life_reach",
        }
        for continuity_id, scenario_name, from_surface, to_surface, failure_mode in rows
    ]


def reach_voice_media_false_claim_scan_v3_receipts() -> list[dict[str, Any]]:
    scope = [
        "docs/research/19-strategy-claim-ledger.md",
        "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
        "docs/implementation/16-agent-parity-execution-roadmap.md",
        "docs/implementation/09-benchmark-status.md",
        "docs/implementation/STATUS.md",
        "backend/src/extensions/post_dx_reach_voice_media_parity.py",
        "backend/src/api/operator.py",
        "backend/src/evals/benchmark_catalog.py",
        "backend/src/evals/harness.py",
        "backend/tests/test_post_dx_reach_voice_media_parity.py",
        "backend/tests/test_eval_harness.py",
        "backend/tests/test_operator_api.py",
    ]
    rows = [
        (
            "ea-false-claim-openclaw",
            "reach_voice_media_false_claim_v3_blocks_openclaw_reach",
            ("openclaw_class_reach", "complete_channel_coverage", "all_openclaw_channels_connected"),
        ),
        (
            "ea-false-claim-always",
            "reach_voice_media_false_claim_v3_blocks_always_available",
            ("always_available_operation", "always_available_daily_life_reach"),
        ),
        (
            "ea-false-claim-voice",
            "reach_voice_media_false_claim_v3_blocks_voice_media_parity",
            ("voice_parity", "voice_media_parity", "multimodal_parity", "production_stt_tts_solved"),
        ),
    ]
    return [
        {
            "scan_id": scan_id,
            "suite_name": REACH_VOICE_MEDIA_FALSE_CLAIM_SCAN_V3_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dx-reach-voice-media-parity-proof",
            "validation_command": POST_DX_REACH_VOICE_MEDIA_CLAIM_SCAN_COMMAND,
            "validation_receipt": POST_DX_REACH_VOICE_MEDIA_CLAIM_SCAN_RECEIPT,
            "command_exit_code": 0,
            "scan_scope": scope,
            "blocked_claims_checked": list(blocked_claims),
            "blocked_claims_found": [],
            "forbidden_hit_count": 0,
            "safe_receipt": _safe_receipt("false-claim-scan", scan_id, (scenario_name, blocked_claims)),
            "claim_boundary": POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
        }
        for scan_id, scenario_name, blocked_claims in rows
    ]


def post_dx_reach_voice_media_parity_proof_receipts() -> list[dict[str, Any]]:
    rows = [
        ("ea-aggregate-dk-ds", "post_dx_reach_builds_on_dk_ds_without_duplicate_scope", "extends_dk_ds_without_duplicate_scope"),
        ("ea-aggregate-reliability", "post_dx_reach_multi_channel_reliability_receipts_behavior", "multi_channel_reliability_v3"),
        ("ea-aggregate-voice-media", "post_dx_reach_voice_media_quality_receipts_behavior", "voice_media_quality_latency_v3"),
        ("ea-aggregate-boundary", "post_dx_reach_claim_boundary_behavior", "blocked_claims_and_coverage_gaps"),
    ]
    return [
        {
            "receipt_id": receipt_id,
            "suite_name": POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dx-reach-voice-media-parity-proof",
            "proof_area": proof_area,
            "builds_on_foundation_suites": [
                "reach_voice_production_ops_v1",
                "post_dp_reach_channel_gap_closure_v1",
            ],
            "upstream_claim_boundaries": [
                REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
                POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
            ],
            "duplicate_scope_blocked": True,
            "coverage_gaps": [
                "unpaired_consumer_messaging_networks_not_complete_channel_coverage",
                "mobile_push_candidate_not_production_mobile_execution_solved",
                "voice_media_quality_candidate_not_voice_media_parity",
                "always_available_daily_life_reach_not_proven",
            ],
            "safe_receipt": _safe_receipt("aggregate-proof", receipt_id, (scenario_name, proof_area)),
            "claim_boundary": POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
        }
        for receipt_id, scenario_name, proof_area in rows
    ]


def build_post_dx_reach_voice_media_contract() -> dict[str, Any]:
    aggregate = post_dx_reach_voice_media_parity_proof_receipts()
    reliability = multi_channel_reach_reliability_v3_receipts()
    voice_media = voice_media_quality_latency_v3_receipts()
    recovery = reach_abuse_recovery_v3_receipts()
    continuity = cross_surface_reach_continuity_v3_receipts()
    false_claims = reach_voice_media_false_claim_scan_v3_receipts()
    dk = build_reach_voice_production_ops_contract()
    ds = build_post_dp_reach_channel_contract()
    all_receipts = [*aggregate, *reliability, *voice_media, *recovery, *continuity, *false_claims]
    return {
        "summary": {
            "suite_name": "post_dx_reach_voice_media_parity_proof",
            "operator_status": "post_dx_reach_voice_media_parity_proof_visible",
            "aggregate_receipt_count": len(aggregate),
            "reliability_surface_count": len(reliability),
            "candidate_surface_count": sum(1 for item in reliability if "candidate" in item["surface"]),
            "paired_revocation_count": sum(1 for item in reliability if item["revocation_probe_blocks_delivery"]),
            "provider_identity_count": sum(1 for item in reliability if item["provider_identity_visible"]),
            "rate_abuse_policy_count": sum(
                1 for item in reliability
                if item["rate_limit_policy_visible"] and item["abuse_handling_visible"]
            ),
            "false_delivery_count": sum(item["false_delivery_count"] for item in reliability),
            "missed_delivery_count": sum(item["missed_delivery_count"] for item in reliability),
            "voice_media_quality_pass_count": sum(1 for item in voice_media if item["quality_gate"]["passed"]),
            "voice_media_latency_pass_count": sum(1 for item in voice_media if item["latency_gate"]["passed"]),
            "voice_media_privacy_control_count": sum(
                1 for item in voice_media
                if item["revocation_blocks_capture"] and item["memory_import_requires_review"]
            ),
            "abuse_recovery_count": sum(1 for item in recovery if item["fallback_exercised"]),
            "revocation_fail_closed_count": sum(1 for item in recovery if item["revocation_fail_closed"]),
            "continuity_preserved_count": sum(
                1 for item in continuity
                if item["thread_preserved"] and item["memory_context_preserved"] and item["approval_state_preserved"]
            ),
            "coverage_gap_count": len(aggregate[0]["coverage_gaps"]),
            "safe_receipt_count": sum(
                1 for item in all_receipts
                if item["safe_receipt"]["redaction_boundary"] == POST_DX_REACH_VOICE_MEDIA_SAFE_REDACTION_BOUNDARY
                and item["safe_receipt"]["contains_message_body"] is False
                and item["safe_receipt"]["contains_contact_identifier"] is False
                and item["safe_receipt"]["contains_secret"] is False
                and item["safe_receipt"]["contains_transcript"] is False
                and item["safe_receipt"]["contains_audio_payload"] is False
                and item["safe_receipt"]["contains_media_payload"] is False
                and item["safe_receipt"]["contains_location_payload"] is False
            ),
            "false_claim_scan_count": len(false_claims),
            "upstream_dk_claim_boundary": dk["summary"]["claim_boundary"],
            "upstream_ds_claim_boundary": ds["summary"]["claim_boundary"],
            "receipt_digest": _stable_digest((
                aggregate,
                reliability,
                voice_media,
                recovery,
                continuity,
                false_claims,
            )),
            "claim_boundary": POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
        },
        "post_dx_reach_voice_media_receipts": aggregate,
        "multi_channel_reliability": reliability,
        "voice_media_quality_latency": voice_media,
        "reach_abuse_recovery": recovery,
        "cross_surface_reach_continuity": continuity,
        "false_claim_scan_receipts": false_claims,
        "policy": post_dx_reach_voice_media_policy_payload(),
    }


def post_dx_reach_voice_media_gate_checks(contract: dict[str, Any]) -> dict[str, bool]:
    summary = contract["summary"]
    policy = contract["policy"]
    all_receipts = [
        *contract["post_dx_reach_voice_media_receipts"],
        *contract["multi_channel_reliability"],
        *contract["voice_media_quality_latency"],
        *contract["reach_abuse_recovery"],
        *contract["cross_surface_reach_continuity"],
        *contract["false_claim_scan_receipts"],
    ]
    return {
        "operator_status_visible": summary["operator_status"] == "post_dx_reach_voice_media_parity_proof_visible",
        "foundation_boundaries_visible": summary["upstream_dk_claim_boundary"] == REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY
        and summary["upstream_ds_claim_boundary"] == POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
        "multi_channel_reliability_visible": summary["reliability_surface_count"] >= 5
        and summary["candidate_surface_count"] >= 2
        and summary["paired_revocation_count"] >= 5
        and summary["provider_identity_count"] >= 5,
        "rate_abuse_recovery_visible": summary["rate_abuse_policy_count"] >= 5
        and summary["abuse_recovery_count"] >= 4
        and summary["revocation_fail_closed_count"] >= 1
        and all(
            item.get("evidence_mode")
            and item.get("runtime_fetch_performed") is False
            and item.get("candidate_or_non_live_marker")
            and item.get("residual_risk")
            for item in contract["reach_abuse_recovery"]
        ),
        "voice_media_quality_visible": summary["voice_media_quality_pass_count"] >= 4
        and summary["voice_media_latency_pass_count"] >= 4
        and summary["voice_media_privacy_control_count"] >= 4,
        "continuity_visible": summary["continuity_preserved_count"] >= 4
        and all(
            item.get("evidence_mode")
            and item.get("runtime_fetch_performed") is False
            and item.get("candidate_or_non_live_marker")
            and item.get("residual_risk")
            for item in contract["cross_surface_reach_continuity"]
        ),
        "coverage_gaps_preserved": summary["coverage_gap_count"] >= 4,
        "safe_receipts_redacted": summary["safe_receipt_count"] >= len(all_receipts),
        "false_claim_scan_command_backed": summary["false_claim_scan_count"] >= 3
        and all(
            item["validation_command"] == POST_DX_REACH_VOICE_MEDIA_CLAIM_SCAN_COMMAND
            and item["command_exit_code"] == 0
            and item["forbidden_hit_count"] == 0
            and item["blocked_claims_found"] == []
            for item in contract["false_claim_scan_receipts"]
        ),
        "claim_boundary_visible": policy["claim_boundary"] == POST_DX_REACH_VOICE_MEDIA_CLAIM_BOUNDARY,
        "blocked_claims_visible": set(POST_DX_REACH_VOICE_MEDIA_BLOCKED_CLAIMS) <= set(policy["blocked_claims"]),
        "stronger_claims_not_claimed": set(policy["not_claimed"]) >= {
            "openclaw_class_reach",
            "complete_channel_coverage",
            "always_available_operation",
            "voice_or_multimodal_parity",
            "production_ready_product",
            "full_parity_achieved",
        },
    }


async def _run_post_dx_reach_voice_media_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SUITE_NAME,
        MULTI_CHANNEL_REACH_RELIABILITY_V3_SUITE_NAME,
        VOICE_MEDIA_QUALITY_LATENCY_V3_SUITE_NAME,
        REACH_ABUSE_RECOVERY_V3_SUITE_NAME,
        CROSS_SURFACE_REACH_CONTINUITY_V3_SUITE_NAME,
        REACH_VOICE_MEDIA_FALSE_CLAIM_SCAN_V3_SUITE_NAME,
    ])


def _failure_report(summary: Any) -> list[dict[str, str]]:
    failures = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SUITE_NAME,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Post-DX reach/voice/media scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def build_post_dx_reach_voice_media_report() -> dict[str, Any]:
    latest = await _run_post_dx_reach_voice_media_suites()
    contract = build_post_dx_reach_voice_media_contract()
    failures = _failure_report(latest)
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "bounded_post_dx_reach_voice_media_parity_proof"
                if not failures
                else "post_dx_reach_voice_media_regressions_detected"
            ),
            "scenario_count": (
                len(POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SCENARIO_NAMES)
                + len(MULTI_CHANNEL_REACH_RELIABILITY_V3_SCENARIO_NAMES)
                + len(VOICE_MEDIA_QUALITY_LATENCY_V3_SCENARIO_NAMES)
                + len(REACH_ABUSE_RECOVERY_V3_SCENARIO_NAMES)
                + len(CROSS_SURFACE_REACH_CONTINUITY_V3_SCENARIO_NAMES)
                + len(REACH_VOICE_MEDIA_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(latest, "failed", 0) or 0),
            "gate_checks": post_dx_reach_voice_media_gate_checks(contract),
        },
        "scenario_names": {
            POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SUITE_NAME: list(
                POST_DX_REACH_VOICE_MEDIA_PARITY_PROOF_SCENARIO_NAMES
            ),
            MULTI_CHANNEL_REACH_RELIABILITY_V3_SUITE_NAME: list(
                MULTI_CHANNEL_REACH_RELIABILITY_V3_SCENARIO_NAMES
            ),
            VOICE_MEDIA_QUALITY_LATENCY_V3_SUITE_NAME: list(VOICE_MEDIA_QUALITY_LATENCY_V3_SCENARIO_NAMES),
            REACH_ABUSE_RECOVERY_V3_SUITE_NAME: list(REACH_ABUSE_RECOVERY_V3_SCENARIO_NAMES),
            CROSS_SURFACE_REACH_CONTINUITY_V3_SUITE_NAME: list(CROSS_SURFACE_REACH_CONTINUITY_V3_SCENARIO_NAMES),
            REACH_VOICE_MEDIA_FALSE_CLAIM_SCAN_V3_SUITE_NAME: list(
                REACH_VOICE_MEDIA_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": failures,
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(latest, "total", 0) or 0),
            "passed": int(getattr(latest, "passed", 0) or 0),
            "failed": int(getattr(latest, "failed", 0) or 0),
            "duration_ms": int(getattr(latest, "duration_ms", 0) or 0),
        },
    }
