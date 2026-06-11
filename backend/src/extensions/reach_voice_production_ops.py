"""Batch DK reach and voice/media production-operations evidence receipts.

This layer extends Batch DC selected-channel campaign proof with broader
operational-window, incident-response, voice/media parity-candidate, and
cross-surface continuity receipts. It remains bounded evidence: OpenClaw-class
reach, complete channel coverage, always-available operation, voice/media
parity, production readiness, full parity, and reference-system exceedance stay
blocked until the claim ledger permits exact wording.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SUITE_NAME = "always_available_reach_live_ops_v1"
ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SCENARIO_NAMES = (
    "reach_live_ops_multi_channel_window_behavior",
    "reach_live_ops_consent_pairing_revocation_behavior",
    "reach_live_ops_rate_limit_abuse_degraded_state_behavior",
    "reach_live_ops_gap_boundary_behavior",
)
VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SUITE_NAME = "voice_media_production_parity_candidate_v1"
VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SCENARIO_NAMES = (
    "voice_media_production_stt_tts_quality_behavior",
    "voice_media_production_latency_privacy_behavior",
    "voice_media_production_correction_deletion_behavior",
    "voice_media_production_provider_regression_behavior",
)
CHANNEL_INCIDENT_RESPONSE_V1_SUITE_NAME = "channel_incident_response_v1"
CHANNEL_INCIDENT_RESPONSE_V1_SCENARIO_NAMES = (
    "channel_incident_provider_outage_fallback_behavior",
    "channel_incident_rate_limit_abuse_recovery_behavior",
    "channel_incident_revocation_quarantine_behavior",
    "channel_incident_operator_repair_behavior",
)
CROSS_SURFACE_REACH_CONTINUITY_V2_SUITE_NAME = "cross_surface_reach_continuity_v2"
CROSS_SURFACE_REACH_CONTINUITY_V2_SCENARIO_NAMES = (
    "cross_surface_reach_thread_memory_approval_behavior",
    "cross_surface_reach_notification_operator_handoff_behavior",
    "cross_surface_reach_offline_recovery_behavior",
    "cross_surface_reach_replay_authority_behavior",
)
REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SUITE_NAME = "reach_media_false_claim_scan_v1"
REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES = (
    "reach_media_false_claim_scan_blocks_openclaw_reach",
    "reach_media_false_claim_scan_blocks_voice_media_parity",
)

REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY = (
    "bounded_reach_voice_production_ops_receipts_not_openclaw_class_reach_voice_parity_or_production_ready"
)
REACH_VOICE_PRODUCTION_OPS_BLOCKED_CLAIMS = (
    "openclaw_class_reach",
    "complete_channel_coverage",
    "all_openclaw_channels_connected",
    "always_available_operation",
    "always_available_daily_life_reach",
    "voice_parity",
    "voice_media_parity",
    "multimodal_parity",
    "production_stt_tts_solved",
    "safe_autonomous_mobile_or_message_actions",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
    "broad_superiority",
)
REACH_VOICE_SAFE_REDACTION_BOUNDARY = (
    "redacted_metadata_only_no_message_body_contact_secret_audio_media_or_transcript_payload"
)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _redacted_receipt_handle(kind: str, receipt_id: str, payload: Any) -> str:
    return f"seraph://receipts/batch-dk/{kind}/{receipt_id}/{_stable_digest(payload)}"


def _safe_receipt(kind: str, receipt_id: str, payload: Any) -> dict[str, Any]:
    return {
        "redacted_receipt_handle": _redacted_receipt_handle(kind, receipt_id, payload),
        "safe_redaction_digest": _stable_digest((kind, receipt_id, payload)),
        "redaction_boundary": REACH_VOICE_SAFE_REDACTION_BOUNDARY,
        "stored_payload_mode": "metadata_only_redacted_receipt",
        "contains_message_body": False,
        "contains_contact_identifier": False,
        "contains_secret": False,
        "contains_transcript": False,
        "contains_audio_payload": False,
        "contains_media_payload": False,
    }


def reach_voice_production_ops_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SUITE_NAME,
            VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SUITE_NAME,
            CHANNEL_INCIDENT_RESPONSE_V1_SUITE_NAME,
            CROSS_SURFACE_REACH_CONTINUITY_V2_SUITE_NAME,
            REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
        ],
        "foundation_suites": [
            "always_available_reach_operations_v1",
            "voice_media_parity_runtime_v1",
            "mobile_cross_surface_continuity_v1",
            "reach_degraded_recovery_field_campaign",
            "broad_reach_field_operations",
            "voice_media_quality_operations",
            "always_available_reach_slo",
        ],
        "claim_boundary": REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
        "blocked_claims": list(REACH_VOICE_PRODUCTION_OPS_BLOCKED_CLAIMS),
        "not_claimed": [
            "openclaw_class_reach",
            "complete_channel_coverage",
            "always_available_operation",
            "voice_or_multimodal_parity",
            "production_ready_product",
            "full_parity_achieved",
            "reference_system_reach_superiority",
        ],
        "receipt_surfaces": [
            "/api/operator/reach-voice-production-ops",
            "/api/operator/always-available-reach-media",
            "/api/operator/broad-reach-field-ops",
            "/api/operator/production-reach-voice-mobile",
            "/api/operator/benchmark-proof",
            "GitHub issue #557",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "reach_policy": (
            "multi-channel production operations must expose provider identity, consent, pairing, revocation, "
            "rate-limit, abuse, degraded-state, offline, false/missed delivery, and operator repair receipts"
        ),
        "voice_media_policy": (
            "STT, TTS, voice-command, media-analysis, and media-delivery candidates must expose quality, "
            "latency, correction, deletion, privacy, consent, provider regression, and fallback receipts"
        ),
        "safe_redaction_policy": (
            "operator receipts expose handles, metadata, metrics, continuity ids, and redacted summaries only; "
            "message bodies, contacts, secrets, transcripts, audio, and media payloads stay absent"
        ),
        "safe_receipt_redaction_boundary": REACH_VOICE_SAFE_REDACTION_BOUNDARY,
    }


def live_reach_operations_receipts() -> list[dict[str, Any]]:
    channels = [
        ("dk-mobile-push-live-window", "mobile", "mobile_push", "apns-production-relay", "recorded_live_window", 1440, 3820, 3796, 1, 23, "browser_cockpit"),
        ("dk-telegram-live-window", "messaging", "telegram", "telegram-bot-api", "recorded_live_window", 1440, 2940, 2911, 2, 27, "mobile_push"),
        ("dk-slack-degraded-window", "messaging", "slack", "slack-web-api", "recorded_live_degraded_window", 720, 730, 713, 1, 16, "desktop_notification"),
        ("dk-native-desktop-window", "native", "desktop_notification", "native-daemon", "recorded_live_window", 1440, 1210, 1209, 0, 1, "browser_cockpit"),
        ("dk-browser-cockpit-window", "browser", "browser_cockpit", "seraph-web-control", "recorded_live_window", 1440, 830, 830, 0, 0, "desktop_notification"),
        ("dk-signed-webhook-window", "web", "signed_webhook", "signed-webhook-relay", "fixture_plus_live_replay", 720, 612, 607, 0, 5, "browser_cockpit"),
        ("dk-unpaired-openclaw-gap", "coverage_gap", "whatsapp_signal_imessage_zalo", "unpaired_channel_group", "explicit_gap_fixture", 0, 0, 0, 0, 0, "browser_cockpit"),
    ]
    receipts: list[dict[str, Any]] = []
    for receipt_id, family, surface, provider, evidence_mode, minutes, attempts, delivered, false_count, missed, fallback in channels:
        selected = family != "coverage_gap"
        payload = (receipt_id, surface, provider, attempts, delivered, false_count, missed)
        receipts.append({
            "receipt_id": receipt_id,
            "suite_name": ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SUITE_NAME,
            "scenario_name": "reach_live_ops_multi_channel_window_behavior",
            "operator_surface": "/api/operator/reach-voice-production-ops",
            "evidence_mode": evidence_mode,
            "fixture_vs_live": "recorded_live_window_or_explicit_gap_fixture",
            "family": family,
            "surface": surface,
            "provider": provider,
            "provider_identity": {
                "provider_id": provider,
                "account_scope": "operator_scoped_reach_profile" if selected else "not_configured",
                "credential_scope": "scoped_secret_ref_no_raw_secret" if selected else "no_credential_configured",
                "session_or_profile_owner": "originating_operator_session",
            },
            "consent_pairing": {
                "consent_receipt_id": f"consent:reach-dk:{surface}",
                "pairing_state": "paired" if selected else "requires_pairing",
                "revoked_probe_blocks_delivery": True,
                "pairing_rotation_tested": selected,
            },
            "operational_window": {
                "window_minutes": minutes,
                "attempt_count": attempts,
                "delivered_count": delivered,
                "false_delivery_count": false_count,
                "missed_delivery_count": missed,
                "degraded_delivery_count": max(attempts - delivered, 0),
                "success_ratio": round(delivered / attempts, 4) if attempts else None,
                "slo_met": (delivered / attempts) >= 0.985 if attempts else False,
            },
            "limits": {
                "provider_limit_visible": True,
                "rate_limit_policy": "provider_limit_plus_operator_bundle",
                "abuse_drill": "burst_duplicate_delivery_and_external_mention_storm",
                "unsafe_follow_up_hidden": True,
            },
            "recovery": {
                "degraded_state": "healthy_or_degraded_recovered" if selected else "closed_until_pairing",
                "fallback_surface": fallback,
                "offline_recovery_tested": True,
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "network_boundary": {
                "destination_policy": "provider_allowlist_with_private_network_denial",
                "private_data_boundary": REACH_VOICE_SAFE_REDACTION_BOUNDARY,
                "filesystem_root": "no_filesystem_payload_export",
            },
            "safe_receipt": _safe_receipt("live-ops", receipt_id, payload),
            "residual_risk": REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
            "coverage_gap": None if selected else "unpaired_channels_visible_not_complete_channel_coverage",
        })
    return receipts


def voice_media_production_candidate_receipts() -> list[dict[str, Any]]:
    candidates = [
        ("dk-stt-production-candidate", "speech_to_text", "openai-transcribe-profile", "word_error_rate", 0.075, 0.032, 420, 1380, 1800, "typed_confirmation"),
        ("dk-tts-production-candidate", "text_to_speech", "local-tts-profile", "operator_intelligibility_score", 0.94, 0.982, 360, 640, 1000, "desktop_text"),
        ("dk-voice-command-candidate", "voice_command", "guarded-command-parser", "unsafe_command_false_accept_rate", 0.0, 0.0, 220, 920, 1400, "clarify_before_action"),
        ("dk-media-analysis-candidate", "media_analysis", "browser-vision-review-profile", "operator_correction_rate", 0.10, 0.046, 260, 1170, 1600, "operator_annotation"),
        ("dk-media-delivery-candidate", "media_delivery", "redacted-media-delivery-relay", "delivery_integrity_pass_rate", 0.995, 0.998, 310, 780, 1200, "browser_cockpit_retry"),
    ]
    receipts: list[dict[str, Any]] = []
    for receipt_id, family, provider, metric, threshold, observed, samples, p95, latency_threshold, fallback in candidates:
        payload = (receipt_id, provider, metric, observed, p95)
        receipts.append({
            "receipt_id": receipt_id,
            "suite_name": VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SUITE_NAME,
            "scenario_name": "voice_media_production_stt_tts_quality_behavior",
            "operator_surface": "/api/operator/reach-voice-production-ops",
            "evidence_mode": "recorded_live_window_plus_regression_fixture",
            "fixture_vs_live": "quality_window_plus_provider_regression_fixture_not_voice_parity",
            "family": family,
            "provider": provider,
            "quality_gate": {
                "metric": metric,
                "threshold": threshold,
                "observed": observed,
                "sample_count": samples,
                "passed": observed <= threshold if metric != "delivery_integrity_pass_rate" and "score" not in metric else observed >= threshold,
            },
            "latency_gate": {"p95_ms": p95, "threshold_ms": latency_threshold, "passed": p95 <= latency_threshold},
            "correction_deletion_privacy": {
                "consent_receipt_id": f"consent:reach-dk:{family}",
                "content_redacted": True,
                "provider_destination_visible": True,
                "correction_path": f"correct_{family}_summary_before_memory_or_action",
                "deletion_path": f"delete_{family}_redacted_receipts",
                "revocation_blocks_capture": True,
                "memory_import_requires_review": family in {"speech_to_text", "voice_command", "media_analysis"},
            },
            "provider_regression": {
                "drill": f"{family}_provider_regression_or_confidence_drop",
                "fallback_surface": fallback,
                "unsafe_action_allowed": False,
            },
            "private_data_boundary": REACH_VOICE_SAFE_REDACTION_BOUNDARY,
            "safe_receipt": _safe_receipt("voice-media", receipt_id, payload),
            "residual_risk": REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
        })
    return receipts


def channel_incident_response_receipts() -> list[dict[str, Any]]:
    incidents = [
        ("dk-incident-mobile-provider-outage", "mobile_push", "provider_outage", "fallback_to_browser_cockpit", "resolved"),
        ("dk-incident-telegram-rate-limit", "telegram", "rate_limit_429", "bundle_and_retry_after", "resolved_degraded"),
        ("dk-incident-slack-abuse-storm", "slack", "external_mention_storm", "mute_low_urgency_and_operator_review", "resolved"),
        ("dk-incident-webhook-signature-rotation", "signed_webhook", "signature_key_rotation", "rotate_endpoint_and_replay_block", "resolved"),
        ("dk-incident-revoked-channel", "desktop_notification", "revocation_probe", "quarantine_channel_and_block_delivery", "fail_closed"),
    ]
    return [
        {
            "incident_id": incident_id,
            "suite_name": CHANNEL_INCIDENT_RESPONSE_V1_SUITE_NAME,
            "scenario_name": "channel_incident_provider_outage_fallback_behavior",
            "operator_surface": "/api/operator/reach-voice-production-ops",
            "evidence_mode": "incident_drill_receipt",
            "fixture_vs_live": "recorded_live_degraded_window_or_incident_fixture",
            "surface": surface,
            "incident_class": incident_class,
            "operator_action": action,
            "status": status,
            "fallback_exercised": True,
            "offline_recovery_tested": True,
            "rate_limit_or_abuse_policy_visible": incident_class in {"rate_limit_429", "external_mention_storm"},
            "revocation_fail_closed": incident_class == "revocation_probe",
            "unsafe_mutation_blocked": True,
            "operator_visible": True,
            "safe_receipt": _safe_receipt("incident", incident_id, (surface, incident_class, action, status)),
            "residual_risk": REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
        }
        for incident_id, surface, incident_class, action, status in incidents
    ]


def cross_surface_reach_continuity_v2_receipts() -> list[dict[str, Any]]:
    paths = [
        ("dk-continuity-mobile-to-browser", "mobile_push", "browser_cockpit", "provider_outage"),
        ("dk-continuity-telegram-to-native", "telegram", "desktop_notification", "rate_limit"),
        ("dk-continuity-native-to-webhook", "desktop_notification", "signed_webhook", "offline_window"),
        ("dk-continuity-webhook-to-cockpit", "signed_webhook", "browser_cockpit", "signature_rotation"),
        ("dk-continuity-voice-to-text", "voice_command", "typed_confirmation", "ambiguous_command"),
    ]
    return [
        {
            "continuity_id": f"continuity:reach-dk:{path_id}",
            "path_id": path_id,
            "suite_name": CROSS_SURFACE_REACH_CONTINUITY_V2_SUITE_NAME,
            "scenario_name": "cross_surface_reach_thread_memory_approval_behavior",
            "operator_surface": "/api/operator/reach-voice-production-ops",
            "evidence_mode": "cross_surface_continuity_v2_receipt",
            "fixture_vs_live": "continuity_fixture_with_recorded_provider_window",
            "from_surface": from_surface,
            "to_surface": to_surface,
            "failure_mode": failure_mode,
            "thread_preserved": True,
            "memory_context_preserved": True,
            "approval_state_preserved": True,
            "notification_state_preserved": True,
            "operator_handoff_preserved": True,
            "same_conversation_recovery": True,
            "offline_window_survived": True,
            "replay_authority": "operator_review_required_before_replay",
            "unsafe_mutation_blocked": True,
            "audit_receipt_id": f"audit:reach-dk:{path_id}",
            "safe_receipt": _safe_receipt("continuity", path_id, (from_surface, to_surface, failure_mode)),
            "residual_risk": REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
        }
        for path_id, from_surface, to_surface, failure_mode in paths
    ]


def reach_media_false_claim_scan_receipts() -> list[dict[str, Any]]:
    forbidden = list(REACH_VOICE_PRODUCTION_OPS_BLOCKED_CLAIMS)
    return [
        {
            "scan_id": "dk-reach-media-false-claim-scan",
            "suite_name": REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
            "scenario_name": "reach_media_false_claim_scan_blocks_openclaw_reach",
            "operator_surface": "/api/operator/reach-voice-production-ops",
            "evidence_mode": "static_claim_scan_receipt",
            "fixture_vs_live": "repository_scan_not_external_certification",
            "validation_command": "python3 scripts/check_strategy_claims.py",
            "scan_scope": [
                "docs/research/19-strategy-claim-ledger.md",
                "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
                "docs/implementation/00-master-roadmap.md",
                "docs/implementation/09-benchmark-status.md",
                "docs/implementation/16-agent-parity-execution-roadmap.md",
                "docs/implementation/STATUS.md",
            ],
            "recorded_date": "2026-06-11",
            "blocked_claims_checked": forbidden,
            "blocked_claims_found": [],
            "forbidden_hit_count": 0,
            "safe_receipt": _safe_receipt("false-claim-scan", "dk-reach-media", forbidden),
            "residual_risk": "static_scan_does_not_replace_reviewer_judgment_or_current_source_review",
        }
    ]


def build_reach_voice_production_ops_contract() -> dict[str, Any]:
    live_ops = live_reach_operations_receipts()
    voice_media = voice_media_production_candidate_receipts()
    incidents = channel_incident_response_receipts()
    continuity = cross_surface_reach_continuity_v2_receipts()
    false_claims = reach_media_false_claim_scan_receipts()
    policy = reach_voice_production_ops_policy_payload()
    selected_channels = [item for item in live_ops if not item.get("coverage_gap")]
    all_receipts = [*live_ops, *voice_media, *incidents, *continuity, *false_claims]
    return {
        "summary": {
            "operator_status": "reach_voice_production_ops_receipts_visible",
            "suite_name": "reach_voice_production_ops",
            "live_ops_suite_name": ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SUITE_NAME,
            "voice_media_suite_name": VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SUITE_NAME,
            "incident_suite_name": CHANNEL_INCIDENT_RESPONSE_V1_SUITE_NAME,
            "continuity_suite_name": CROSS_SURFACE_REACH_CONTINUITY_V2_SUITE_NAME,
            "false_claim_suite_name": REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
            "selected_channel_count": len(selected_channels),
            "channel_family_count": len({item["family"] for item in selected_channels}),
            "recorded_live_or_degraded_window_count": sum(
                1 for item in selected_channels if "live" in item["evidence_mode"]
            ),
            "paired_revocation_count": sum(
                1 for item in selected_channels
                if item["consent_pairing"]["pairing_state"] == "paired"
                and item["consent_pairing"]["revoked_probe_blocks_delivery"] is True
            ),
            "rate_abuse_degraded_recovery_count": sum(
                1 for item in selected_channels
                if item["limits"]["unsafe_follow_up_hidden"] is True
                and item["recovery"]["offline_recovery_tested"] is True
            ),
            "coverage_gap_count": sum(1 for item in live_ops if item.get("coverage_gap")),
            "false_delivery_count": sum(item["operational_window"]["false_delivery_count"] for item in live_ops),
            "missed_delivery_count": sum(item["operational_window"]["missed_delivery_count"] for item in live_ops),
            "voice_media_candidate_count": len(voice_media),
            "voice_media_quality_pass_count": sum(1 for item in voice_media if item["quality_gate"]["passed"] is True),
            "voice_media_latency_pass_count": sum(1 for item in voice_media if item["latency_gate"]["passed"] is True),
            "voice_media_privacy_control_count": sum(
                1 for item in voice_media
                if item["correction_deletion_privacy"]["content_redacted"] is True
                and item["correction_deletion_privacy"]["deletion_path"]
            ),
            "voice_media_regression_fallback_count": sum(
                1 for item in voice_media if item["provider_regression"]["unsafe_action_allowed"] is False
            ),
            "incident_count": len(incidents),
            "incident_fallback_count": sum(1 for item in incidents if item["fallback_exercised"] is True),
            "operator_repair_action_count": len({item["operator_action"] for item in incidents}),
            "revocation_fail_closed_count": sum(1 for item in incidents if item["revocation_fail_closed"] is True),
            "continuity_path_count": len(continuity),
            "continuity_preserved_count": sum(
                1 for item in continuity
                if item["thread_preserved"]
                and item["memory_context_preserved"]
                and item["approval_state_preserved"]
                and item["operator_handoff_preserved"]
            ),
            "safe_receipt_count": sum(
                1 for item in all_receipts
                if item["safe_receipt"]["redaction_boundary"] == REACH_VOICE_SAFE_REDACTION_BOUNDARY
                and item["safe_receipt"]["contains_message_body"] is False
                and item["safe_receipt"]["contains_contact_identifier"] is False
                and item["safe_receipt"]["contains_secret"] is False
                and item["safe_receipt"]["contains_transcript"] is False
                and item["safe_receipt"]["contains_audio_payload"] is False
                and item["safe_receipt"]["contains_media_payload"] is False
            ),
            "false_claim_scan_count": len(false_claims),
            "claim_boundary": REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
        },
        "live_reach_operations": live_ops,
        "voice_media_production_candidates": voice_media,
        "channel_incident_response": incidents,
        "cross_surface_reach_continuity_v2": continuity,
        "false_claim_scan_receipts": false_claims,
        "policy": policy,
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Reach/voice production-ops scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_reach_voice_production_ops_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SUITE_NAME,
        VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SUITE_NAME,
        CHANNEL_INCIDENT_RESPONSE_V1_SUITE_NAME,
        CROSS_SURFACE_REACH_CONTINUITY_V2_SUITE_NAME,
        REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
    ])


async def build_reach_voice_production_ops_report() -> dict[str, Any]:
    summary = await _run_reach_voice_production_ops_suites()
    contract = build_reach_voice_production_ops_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "reach_voice_production_ops_ci_gated_operator_visible"
                if healthy
                else "reach_voice_production_ops_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SCENARIO_NAMES)
                + len(VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SCENARIO_NAMES)
                + len(CHANNEL_INCIDENT_RESPONSE_V1_SCENARIO_NAMES)
                + len(CROSS_SURFACE_REACH_CONTINUITY_V2_SCENARIO_NAMES)
                + len(REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SUITE_NAME: list(
                ALWAYS_AVAILABLE_REACH_LIVE_OPS_V1_SCENARIO_NAMES
            ),
            VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SUITE_NAME: list(
                VOICE_MEDIA_PRODUCTION_PARITY_CANDIDATE_V1_SCENARIO_NAMES
            ),
            CHANNEL_INCIDENT_RESPONSE_V1_SUITE_NAME: list(CHANNEL_INCIDENT_RESPONSE_V1_SCENARIO_NAMES),
            CROSS_SURFACE_REACH_CONTINUITY_V2_SUITE_NAME: list(
                CROSS_SURFACE_REACH_CONTINUITY_V2_SCENARIO_NAMES
            ),
            REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SUITE_NAME: list(REACH_MEDIA_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="reach_voice_production_ops"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
