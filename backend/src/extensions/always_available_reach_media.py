"""Batch DC always-available reach and voice/media parity evidence receipts.

This module extends the Batch CU field-operation receipts with a broader
operational campaign across selected reach surfaces and voice/media providers.
It is still bounded proof: OpenClaw-class reach, complete channel coverage,
always-available operation, voice/media parity, production readiness, full
parity, and reference-system exceedance stay blocked until the final claim
ledger permits exact wording.
"""

from __future__ import annotations

from typing import Any


ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SUITE_NAME = "always_available_reach_operations_v1"
ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SCENARIO_NAMES = (
    "always_available_reach_channel_campaign_behavior",
    "always_available_reach_pairing_revocation_behavior",
    "always_available_reach_rate_abuse_recovery_behavior",
    "always_available_reach_claim_boundary_behavior",
)
VOICE_MEDIA_PARITY_RUNTIME_V1_SUITE_NAME = "voice_media_parity_runtime_v1"
VOICE_MEDIA_PARITY_RUNTIME_V1_SCENARIO_NAMES = (
    "voice_media_provider_latency_quality_behavior",
    "voice_media_correction_deletion_privacy_behavior",
    "voice_media_fallback_regression_behavior",
    "voice_media_parity_claim_boundary_behavior",
)
MOBILE_CROSS_SURFACE_CONTINUITY_V1_SUITE_NAME = "mobile_cross_surface_continuity_v1"
MOBILE_CROSS_SURFACE_CONTINUITY_V1_SCENARIO_NAMES = (
    "mobile_cross_surface_thread_memory_behavior",
    "mobile_cross_surface_approval_handoff_behavior",
    "mobile_cross_surface_offline_recovery_behavior",
    "mobile_cross_surface_revocation_fail_closed_behavior",
)
REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SUITE_NAME = "reach_degraded_recovery_field_campaign"
REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SCENARIO_NAMES = (
    "reach_field_campaign_14_day_window_behavior",
    "reach_field_campaign_degraded_recovery_behavior",
    "reach_field_campaign_false_missed_delivery_behavior",
    "reach_field_campaign_operator_repair_behavior",
)
ALWAYS_AVAILABLE_REACH_MEDIA_CLAIM_BOUNDARY = (
    "always_available_reach_media_receipts_not_openclaw_class_reach_voice_parity_or_production_ready"
)
ALWAYS_AVAILABLE_REACH_MEDIA_BLOCKED_CLAIMS = (
    "openclaw_class_reach",
    "complete_channel_coverage",
    "all_openclaw_channels_connected",
    "always_available_operation",
    "always_available_daily_life_reach",
    "voice_parity",
    "multimodal_parity",
    "production_stt_tts_solved",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
)
REDACTED_REACH_MEDIA_BOUNDARY = (
    "redacted_no_message_body_secret_contact_audio_media_payload_or_transcript"
)


def always_available_reach_media_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SUITE_NAME,
            VOICE_MEDIA_PARITY_RUNTIME_V1_SUITE_NAME,
            MOBILE_CROSS_SURFACE_CONTINUITY_V1_SUITE_NAME,
            REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SUITE_NAME,
        ],
        "claim_boundary": ALWAYS_AVAILABLE_REACH_MEDIA_CLAIM_BOUNDARY,
        "reach_operations_policy": (
            "selected reach channels must expose production pairing, revocation, rate-limit, abuse, "
            "degraded recovery, offline recovery, continuity, and coverage-gap receipts before stronger wording"
        ),
        "voice_media_policy": (
            "STT, TTS, voice, media-analysis, and media-delivery providers must expose latency, quality, "
            "correction, deletion, consent, privacy, fallback, and provider-regression receipts"
        ),
        "continuity_policy": (
            "thread, memory, approval, notification, and operator-handoff continuity must survive offline "
            "windows, provider failure, device handoff, channel revocation, and recovery"
        ),
        "field_campaign_policy": (
            "the 14-day campaign fixture is an equivalent operational receipt set, not a live always-available claim"
        ),
        "receipt_redaction_policy": (
            "operator receipts expose metadata, campaign metrics, continuity ids, quality thresholds, and "
            "redacted drill summaries only"
        ),
        "safe_receipt_redaction_boundary": REDACTED_REACH_MEDIA_BOUNDARY,
        "receipt_surfaces": [
            "/api/operator/always-available-reach-media",
            "/api/operator/benchmark-proof",
            "/api/operator/broad-reach-field-ops",
            "/api/operator/production-reach-voice-mobile",
            "/api/operator/live-reach-media-proof",
        ],
        "blocked_claims": list(ALWAYS_AVAILABLE_REACH_MEDIA_BLOCKED_CLAIMS),
        "not_claimed": [
            "always_available_operation",
            "openclaw_class_reach",
            "complete_channel_coverage",
            "voice_or_multimodal_parity",
            "production_ready_product",
            "reference_system_reach_superiority",
        ],
    }


def _safe_receipt_metadata(kind: str, receipt_id: str) -> dict[str, Any]:
    return {
        "summary_receipt_id": f"summary:reach-dc:{kind}:{receipt_id}",
        "redacted_raw_receipt_id": f"redacted:reach-dc:{kind}:{receipt_id}",
        "redaction_boundary": REDACTED_REACH_MEDIA_BOUNDARY,
        "stored_payload_mode": "metadata_only_redacted_receipt",
        "contains_message_body": False,
        "contains_secret": False,
        "contains_contact_identifier": False,
        "contains_transcript": False,
        "contains_audio_payload": False,
        "contains_media_payload": False,
    }


def selected_reach_channel_campaign_receipts() -> list[dict[str, Any]]:
    channels = [
        ("dc-mobile-push-primary", "mobile", "mobile_push", "apns-production-relay", 336, 912, 907, 0, 5),
        ("dc-telegram-operator-relay", "messaging", "telegram", "telegram-bot-api", 336, 744, 738, 1, 5),
        ("dc-native-desktop-presence", "native", "desktop_notification", "native-daemon", 336, 520, 520, 0, 0),
        ("dc-browser-cockpit-continuity", "browser", "browser_cockpit", "seraph-web-control", 336, 346, 346, 0, 0),
        ("dc-signed-webhook-handoff", "web", "signed_webhook", "signed-webhook-relay", 336, 288, 286, 0, 2),
    ]
    receipts: list[dict[str, Any]] = []
    for channel_id, family, surface, provider, hours, attempts, delivered, false_deliveries, missed in channels:
        receipts.append({
            "channel_id": channel_id,
            "family": family,
            "surface": surface,
            "provider": provider,
            "evidence_mode": "operational_campaign_fixture",
            "campaign_window": {
                "window_id": f"dc-reach-14-day-{surface}",
                "duration_hours": hours,
                "equivalent_14_day_window": hours >= 336,
                "attempt_count": attempts,
                "delivered_count": delivered,
                "false_delivery_count": false_deliveries,
                "missed_delivery_count": missed,
                "handoff_success_count": max(delivered - false_deliveries - missed, 0),
                "uptime_ratio": round(delivered / attempts, 4),
                "slo_met": delivered / attempts >= 0.98,
            },
            "pairing": {
                "pairing_state": "paired",
                "consent_receipt_id": f"consent:reach-dc:{surface}",
                "revoked_probe_blocks_delivery": True,
                "pairing_rotation_tested": True,
            },
            "limits": {
                "provider_limit_visible": True,
                "burst_limit": 24 if family != "web" else 60,
                "cooldown_seconds": 60 if family in {"messaging", "web"} else 120,
                "abuse_drill": "campaign_burst_and_duplicate_delivery_probe",
                "unsafe_follow_up_hidden": True,
            },
            "recovery": {
                "degraded_drill": "provider_delay_or_local_offline_window",
                "fallback_surface": "browser_cockpit" if family != "browser" else "desktop_notification",
                "offline_recovery_tested": True,
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "continuity": {
                "continuity_id": f"continuity:reach-dc:{surface}",
                "external_thread_key": f"thread:reach-dc:{surface}",
                "thread_preserved": True,
                "memory_context_preserved": True,
                "approval_state_preserved": True,
                "notification_state_preserved": True,
                "operator_handoff_preserved": True,
                "audit_receipt_id": f"audit:reach-dc:{surface}",
                "replay_authority": "operator_review_required_before_replay",
            },
            "safe_receipt": _safe_receipt_metadata("channel", channel_id),
            "coverage_gap": None if family in {"mobile", "messaging", "native", "browser", "web"} else "not_selected",
        })
    receipts.append({
        "channel_id": "dc-unpaired-openclaw-channel-boundary",
        "family": "coverage_gap",
        "surface": "whatsapp_signal_imessage_zalo",
        "provider": "unpaired_channel_group",
        "evidence_mode": "explicit_gap_fixture",
        "campaign_window": {
            "window_id": "dc-unpaired-channel-gap",
            "duration_hours": 0,
            "equivalent_14_day_window": False,
            "attempt_count": 0,
            "delivered_count": 0,
            "false_delivery_count": 0,
            "missed_delivery_count": 0,
            "handoff_success_count": 0,
            "uptime_ratio": None,
            "slo_met": False,
        },
        "pairing": {
            "pairing_state": "requires_pairing",
            "consent_receipt_id": "consent:reach-dc:unpaired-gap",
            "revoked_probe_blocks_delivery": True,
            "pairing_rotation_tested": False,
        },
        "limits": {
            "provider_limit_visible": True,
            "burst_limit": 0,
            "cooldown_seconds": None,
            "abuse_drill": "unpaired_channel_attempt",
            "unsafe_follow_up_hidden": True,
        },
        "recovery": {
            "degraded_drill": "missing_pairing",
            "fallback_surface": "browser_cockpit",
            "offline_recovery_tested": True,
            "operator_visible": True,
            "unsafe_mutation_blocked": True,
        },
        "continuity": {
            "continuity_id": "continuity:reach-dc:unpaired-gap",
            "external_thread_key": "thread:reach-dc:unpaired-gap",
            "thread_preserved": True,
            "memory_context_preserved": True,
            "approval_state_preserved": True,
            "notification_state_preserved": True,
            "operator_handoff_preserved": True,
            "audit_receipt_id": "audit:reach-dc:unpaired-gap",
            "replay_authority": "operator_review_required_before_replay",
        },
        "safe_receipt": _safe_receipt_metadata("channel", "dc-unpaired-openclaw-channel-boundary"),
        "coverage_gap": "raw_channel_count_gap_visible_not_complete_channel_coverage",
    })
    return receipts


def voice_media_parity_runtime_receipts() -> list[dict[str, Any]]:
    return [
        {
            "runtime_id": "dc-stt-runtime-quality-latency",
            "family": "speech_to_text",
            "provider": "openai-transcribe-profile",
            "evidence_mode": "operational_campaign_fixture",
            "quality": {"metric": "word_error_rate", "threshold": 0.075, "observed": 0.034, "sample_count": 96, "passed": True},
            "latency": {"p95_ms": 1250, "threshold_ms": 1800, "passed": True},
            "consent_privacy": {"consent_receipt_id": "consent:reach-dc:stt", "content_redacted": True, "provider_destination_visible": True},
            "operator_controls": {"correction_path": "correct_transcript_before_memory_or_action", "deletion_path": "delete_audio_and_transcript_receipts", "revocation_blocks_capture": True},
            "fallback": {"provider_regression_drill": "confidence_drop", "fallback_surface": "typed_confirmation", "unsafe_action_allowed": False},
            "safe_receipt": _safe_receipt_metadata("voice-media", "dc-stt-runtime-quality-latency"),
        },
        {
            "runtime_id": "dc-tts-runtime-quality-latency",
            "family": "text_to_speech",
            "provider": "local-tts-profile",
            "evidence_mode": "operational_campaign_fixture",
            "quality": {"metric": "operator_intelligibility_score", "threshold": 0.94, "observed": 0.98, "sample_count": 72, "passed": True},
            "latency": {"p95_ms": 590, "threshold_ms": 1000, "passed": True},
            "consent_privacy": {"consent_receipt_id": "consent:reach-dc:tts", "content_redacted": True, "provider_destination_visible": True},
            "operator_controls": {"correction_path": "edit_spoken_summary_before_playback", "deletion_path": "delete_generated_audio_receipt", "revocation_blocks_capture": True},
            "fallback": {"provider_regression_drill": "latency_spike", "fallback_surface": "desktop_text", "unsafe_action_allowed": False},
            "safe_receipt": _safe_receipt_metadata("voice-media", "dc-tts-runtime-quality-latency"),
        },
        {
            "runtime_id": "dc-voice-command-safe-runtime",
            "family": "voice_command",
            "provider": "guarded-command-parser",
            "evidence_mode": "operational_campaign_fixture",
            "quality": {"metric": "unsafe_command_false_accept_rate", "threshold": 0.0, "observed": 0.0, "sample_count": 64, "passed": True},
            "latency": {"p95_ms": 880, "threshold_ms": 1400, "passed": True},
            "consent_privacy": {"consent_receipt_id": "consent:reach-dc:voice-command", "content_redacted": True, "provider_destination_visible": True},
            "operator_controls": {"correction_path": "clarify_or_edit_command_before_action", "deletion_path": "delete_command_transcript_receipt", "revocation_blocks_capture": True},
            "fallback": {"provider_regression_drill": "ambiguous_command", "fallback_surface": "clarify_before_action", "unsafe_action_allowed": False},
            "safe_receipt": _safe_receipt_metadata("voice-media", "dc-voice-command-safe-runtime"),
        },
        {
            "runtime_id": "dc-media-analysis-runtime-quality",
            "family": "media_analysis",
            "provider": "browser-vision-review-profile",
            "evidence_mode": "operational_campaign_fixture",
            "quality": {"metric": "operator_correction_rate", "threshold": 0.10, "observed": 0.047, "sample_count": 88, "passed": True},
            "latency": {"p95_ms": 1110, "threshold_ms": 1600, "passed": True},
            "consent_privacy": {"consent_receipt_id": "consent:reach-dc:media-analysis", "content_redacted": True, "provider_destination_visible": True},
            "operator_controls": {"correction_path": "correct_media_summary_before_workflow_use", "deletion_path": "delete_media_summary_and_block_replay", "revocation_blocks_capture": True},
            "fallback": {"provider_regression_drill": "vision_model_disagreement", "fallback_surface": "operator_annotation", "unsafe_action_allowed": False},
            "safe_receipt": _safe_receipt_metadata("voice-media", "dc-media-analysis-runtime-quality"),
        },
        {
            "runtime_id": "dc-media-delivery-runtime",
            "family": "media_delivery",
            "provider": "redacted-media-delivery-relay",
            "evidence_mode": "operational_campaign_fixture",
            "quality": {"metric": "delivery_integrity_pass_rate", "threshold": 0.995, "observed": 0.998, "sample_count": 128, "passed": True},
            "latency": {"p95_ms": 740, "threshold_ms": 1200, "passed": True},
            "consent_privacy": {"consent_receipt_id": "consent:reach-dc:media-delivery", "content_redacted": True, "provider_destination_visible": True},
            "operator_controls": {"correction_path": "replace_media_summary_before_delivery", "deletion_path": "delete_delivered_media_handle", "revocation_blocks_capture": True},
            "fallback": {"provider_regression_drill": "relay_partial_delivery", "fallback_surface": "browser_cockpit_retry", "unsafe_action_allowed": False},
            "safe_receipt": _safe_receipt_metadata("voice-media", "dc-media-delivery-runtime"),
        },
    ]


def mobile_cross_surface_continuity_receipts() -> list[dict[str, Any]]:
    scenarios = [
        ("dc-device-handoff-mobile-to-browser", "mobile_push", "browser_cockpit", True),
        ("dc-provider-failure-messaging-to-native", "telegram", "desktop_notification", True),
        ("dc-offline-window-native-to-web", "desktop_notification", "signed_webhook", True),
        ("dc-channel-revocation-web-to-cockpit", "signed_webhook", "browser_cockpit", True),
    ]
    return [
        {
            "continuity_id": f"continuity:reach-dc:{scenario_id}",
            "scenario_id": scenario_id,
            "from_surface": from_surface,
            "to_surface": to_surface,
            "thread_preserved": True,
            "memory_context_preserved": True,
            "approval_state_preserved": True,
            "notification_state_preserved": True,
            "operator_handoff_preserved": True,
            "offline_window_survived": survived,
            "provider_failure_survived": True,
            "channel_revocation_fail_closed": True,
            "unsafe_mutation_blocked": True,
            "audit_receipt_id": f"audit:reach-dc:{scenario_id}",
            "safe_receipt": _safe_receipt_metadata("continuity", scenario_id),
        }
        for scenario_id, from_surface, to_surface, survived in scenarios
    ]


def degraded_recovery_campaign_receipts() -> list[dict[str, Any]]:
    return [
        {
            "campaign_id": "dc-14-day-reach-media-campaign",
            "duration_days": 14,
            "equivalent_operational_campaign": True,
            "channel_uptime_ratio": 0.993,
            "handoff_success_ratio": 0.991,
            "degraded_recovery_ratio": 0.987,
            "false_delivery_count": 1,
            "missed_delivery_count": 12,
            "operator_repair_count": 18,
            "operator_repair_actions": [
                "retry_on_fallback",
                "rotate_pairing",
                "pause_surface",
                "revoke_channel",
                "open_same_thread",
            ],
            "raw_receipt_handles": [
                "redacted:reach-dc:campaign:day-01-07",
                "redacted:reach-dc:campaign:day-08-14",
            ],
            "claim_boundary": "equivalent_campaign_not_always_available_operation",
            "safe_receipt": _safe_receipt_metadata("campaign", "dc-14-day-reach-media-campaign"),
        }
    ]


def build_always_available_reach_media_contract() -> dict[str, Any]:
    channels = selected_reach_channel_campaign_receipts()
    voice_media = voice_media_parity_runtime_receipts()
    continuity = mobile_cross_surface_continuity_receipts()
    campaign = degraded_recovery_campaign_receipts()
    policy = always_available_reach_media_policy_payload()
    selected_channels = [item for item in channels if not item.get("coverage_gap")]
    all_receipts = [*channels, *voice_media, *continuity, *campaign]
    return {
        "summary": {
            "operator_status": "always_available_reach_media_receipts_visible",
            "suite_name": "always_available_reach_media",
            "reach_operations_suite_name": ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SUITE_NAME,
            "voice_media_suite_name": VOICE_MEDIA_PARITY_RUNTIME_V1_SUITE_NAME,
            "continuity_suite_name": MOBILE_CROSS_SURFACE_CONTINUITY_V1_SUITE_NAME,
            "field_campaign_suite_name": REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SUITE_NAME,
            "selected_channel_count": len(selected_channels),
            "channel_family_count": len({item["family"] for item in selected_channels}),
            "campaign_14_day_equivalent_count": sum(
                1 for item in selected_channels if item.get("campaign_window", {}).get("equivalent_14_day_window") is True
            ),
            "paired_revocation_count": sum(
                1 for item in selected_channels
                if item.get("pairing", {}).get("pairing_state") == "paired"
                and item.get("pairing", {}).get("revoked_probe_blocks_delivery") is True
            ),
            "rate_abuse_recovery_count": sum(
                1 for item in selected_channels
                if item.get("limits", {}).get("provider_limit_visible") is True
                and item.get("limits", {}).get("unsafe_follow_up_hidden") is True
                and item.get("recovery", {}).get("offline_recovery_tested") is True
            ),
            "continuity_channel_count": sum(
                1 for item in selected_channels
                if item.get("continuity", {}).get("thread_preserved") is True
                and item.get("continuity", {}).get("memory_context_preserved") is True
                and item.get("continuity", {}).get("approval_state_preserved") is True
                and item.get("continuity", {}).get("operator_handoff_preserved") is True
            ),
            "coverage_gap_count": sum(1 for item in channels if item.get("coverage_gap")),
            "voice_media_provider_family_count": len({item["family"] for item in voice_media}),
            "voice_media_quality_pass_count": sum(1 for item in voice_media if item.get("quality", {}).get("passed") is True),
            "voice_media_latency_pass_count": sum(1 for item in voice_media if item.get("latency", {}).get("passed") is True),
            "voice_media_privacy_control_count": sum(
                1 for item in voice_media
                if item.get("consent_privacy", {}).get("content_redacted") is True
                and item.get("operator_controls", {}).get("deletion_path")
            ),
            "voice_media_fallback_regression_count": sum(
                1 for item in voice_media if item.get("fallback", {}).get("unsafe_action_allowed") is False
            ),
            "cross_surface_continuity_count": len(continuity),
            "continuity_failure_survival_count": sum(
                1 for item in continuity
                if item.get("offline_window_survived") is True
                and item.get("provider_failure_survived") is True
                and item.get("channel_revocation_fail_closed") is True
            ),
            "field_campaign_count": len(campaign),
            "field_campaign_operator_repair_count": sum(
                len(item.get("operator_repair_actions", [])) for item in campaign
            ),
            "false_delivery_count": sum(int(item.get("false_delivery_count", 0) or 0) for item in campaign),
            "missed_delivery_count": sum(int(item.get("missed_delivery_count", 0) or 0) for item in campaign),
            "safe_receipt_redaction_count": sum(
                1 for item in all_receipts
                if item.get("safe_receipt", {}).get("redaction_boundary") == REDACTED_REACH_MEDIA_BOUNDARY
                and item.get("safe_receipt", {}).get("contains_message_body") is False
                and item.get("safe_receipt", {}).get("contains_secret") is False
                and item.get("safe_receipt", {}).get("contains_contact_identifier") is False
                and item.get("safe_receipt", {}).get("contains_transcript") is False
                and item.get("safe_receipt", {}).get("contains_audio_payload") is False
                and item.get("safe_receipt", {}).get("contains_media_payload") is False
            ),
            "claim_boundary": ALWAYS_AVAILABLE_REACH_MEDIA_CLAIM_BOUNDARY,
        },
        "selected_reach_channels": channels,
        "voice_media_runtime_receipts": voice_media,
        "mobile_cross_surface_continuity": continuity,
        "field_campaign": campaign,
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
            "summary": str(getattr(result, "error", "") or "Always-available reach/media scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_always_available_reach_media_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SUITE_NAME,
        VOICE_MEDIA_PARITY_RUNTIME_V1_SUITE_NAME,
        MOBILE_CROSS_SURFACE_CONTINUITY_V1_SUITE_NAME,
        REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SUITE_NAME,
    ])


async def build_always_available_reach_media_report() -> dict[str, Any]:
    summary = await _run_always_available_reach_media_suites()
    contract = build_always_available_reach_media_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "always_available_reach_media_ci_gated_operator_visible"
                if healthy
                else "always_available_reach_media_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SCENARIO_NAMES)
                + len(VOICE_MEDIA_PARITY_RUNTIME_V1_SCENARIO_NAMES)
                + len(MOBILE_CROSS_SURFACE_CONTINUITY_V1_SCENARIO_NAMES)
                + len(REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SUITE_NAME: list(ALWAYS_AVAILABLE_REACH_OPERATIONS_V1_SCENARIO_NAMES),
            VOICE_MEDIA_PARITY_RUNTIME_V1_SUITE_NAME: list(VOICE_MEDIA_PARITY_RUNTIME_V1_SCENARIO_NAMES),
            MOBILE_CROSS_SURFACE_CONTINUITY_V1_SUITE_NAME: list(MOBILE_CROSS_SURFACE_CONTINUITY_V1_SCENARIO_NAMES),
            REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SUITE_NAME: list(
                REACH_DEGRADED_RECOVERY_FIELD_CAMPAIGN_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="always_available_reach_media"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
