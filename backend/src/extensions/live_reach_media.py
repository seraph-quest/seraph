"""Batch CE live broad reach and production voice/media proof receipts.

This module extends the Batch BY deterministic hardening floor with
live/recorded-live attestation receipts for broader channels, production
voice/media providers, and cross-surface recovery. It does not claim broad
OpenClaw-class reach, voice parity, multimodal parity, or production readiness.
"""

from __future__ import annotations

from typing import Any


LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SUITE_NAME = "live_broad_reach_channel_attestation"
LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SCENARIO_NAMES = (
    "live_reach_mobile_push_identity_consent_behavior",
    "live_reach_messaging_provider_pairing_revocation_behavior",
    "live_reach_rate_limit_abuse_recovery_behavior",
    "live_reach_operator_routing_evidence_behavior",
)
PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SUITE_NAME = "production_voice_media_provider_runtime"
PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SCENARIO_NAMES = (
    "voice_media_stt_provider_consent_capture_behavior",
    "voice_media_tts_provider_fallback_behavior",
    "voice_media_media_analysis_correction_deletion_behavior",
    "voice_media_provider_failure_privacy_behavior",
)
CROSS_SURFACE_CONTINUITY_RECOVERY_SUITE_NAME = "cross_surface_continuity_recovery"
CROSS_SURFACE_CONTINUITY_RECOVERY_SCENARIO_NAMES = (
    "cross_surface_browser_desktop_mobile_handoff_behavior",
    "cross_surface_messaging_voice_thread_continuity_behavior",
    "cross_surface_approval_memory_recovery_behavior",
    "cross_surface_degraded_route_fail_closed_behavior",
)
LIVE_REACH_MEDIA_CLAIM_BOUNDARY = (
    "live_reach_media_receipts_not_openclaw_class_reach_voice_or_multimodal_parity"
)
LIVE_REACH_MEDIA_BLOCKED_CLAIMS = (
    "broad_reach",
    "complete_channel_coverage",
    "openclaw_class_reach",
    "voice_parity",
    "multimodal_parity",
    "production_stt_tts_solved",
    "production_mobile_execution_solved",
    "always_available_operation",
    "safe_browser_automation",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
)


def live_reach_media_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SUITE_NAME,
            PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SUITE_NAME,
            CROSS_SURFACE_CONTINUITY_RECOVERY_SUITE_NAME,
        ],
        "claim_boundary": LIVE_REACH_MEDIA_CLAIM_BOUNDARY,
        "channel_policy": (
            "live and recorded-live channels must name provider identity evidence mode consent "
            "pairing revocation rate limits abuse handling continuity approval handoff and degraded recovery"
        ),
        "voice_media_policy": (
            "production voice media providers must name capture boundary consent provider destination "
            "fallback correction deletion privacy and provider-failure receipts before runtime claims"
        ),
        "continuity_policy": (
            "cross-surface work must preserve thread identity memory context approvals audit receipts "
            "and fail-closed degraded route explanations across browser desktop mobile messaging and voice"
        ),
        "receipt_surfaces": [
            "/api/operator/live-reach-media-proof",
            "/api/operator/benchmark-proof",
            "/api/operator/production-reach-browser-voice",
            "/api/operator/one-reach-channel-canary",
            "/api/operator/guardian-safe-multimodal-voice",
        ],
        "blocked_claims": list(LIVE_REACH_MEDIA_BLOCKED_CLAIMS),
        "not_claimed": [
            "all_openclaw_channels_connected",
            "production_slack_discord_telegram_mobile_sla",
            "production_stt_tts_quality_solved",
            "voice_or_multimodal_parity",
            "safe_autonomous_browser_or_mobile_use",
            "reference_system_reach_superiority",
        ],
    }


def live_broad_reach_channel_receipts() -> list[dict[str, Any]]:
    return [
        {
            "channel_id": "seraph.mobile.push.operator-primary",
            "provider": "apns-dev-relay",
            "transport": "mobile_push",
            "evidence_mode": "recorded_live",
            "recorded_at": "2026-06-10T02:00:00Z",
            "operator_identity": {
                "external_identity_hash": "sha256:mobile-operator-001",
                "device_binding_id": "mobile-bind-ce-001",
                "consent_receipt_id": "consent:reach-ce:mobile-push",
                "identity_state": "bound",
            },
            "pairing": {
                "state": "paired",
                "scopes": ["inbound_reply", "action_card_resume", "approval_handoff"],
                "revocation_probe_blocks_delivery": True,
            },
            "continuity": {
                "seraph_session_id": "session-reach-ce-001",
                "thread_key": "mobile://operator-primary/thread-001",
                "memory_context_id": "memctx-reach-ce-001",
                "same_thread_resume": True,
            },
            "rate_limits": {
                "provider_limit_visible": True,
                "cooldown_seconds": 120,
                "retry_after_seconds": 180,
                "abuse_guard": "bundle_low_urgency_and_require_operator_resume",
            },
            "approval_handoff": {
                "approval_id": "approval-reach-ce-mobile-action",
                "status": "pending_operator_approval",
                "mutation_boundary": "action_card_draft_until_approved",
            },
            "degraded_recovery": {
                "status": "provider_rate_limited",
                "fallback_surface": "desktop_notification",
                "unsafe_follow_up_hidden": True,
                "operator_visible": True,
            },
            "audit_receipts": [
                "audit:reach-ce:mobile-bound",
                "audit:reach-ce:mobile-consent",
                "audit:reach-ce:mobile-rate-limit",
            ],
        },
        {
            "channel_id": "seraph.messaging.telegram-operator-relay",
            "provider": "telegram-bot-api",
            "transport": "telegram",
            "evidence_mode": "recorded_live",
            "recorded_at": "2026-06-10T02:05:00Z",
            "operator_identity": {
                "external_identity_hash": "sha256:telegram-operator-001",
                "device_binding_id": "telegram-bind-ce-001",
                "consent_receipt_id": "consent:reach-ce:telegram",
                "identity_state": "bound",
            },
            "pairing": {
                "state": "paired",
                "scopes": ["inbound_thread", "outbound_reply_draft", "approval_handoff"],
                "revocation_probe_blocks_delivery": True,
            },
            "continuity": {
                "seraph_session_id": "session-reach-ce-001",
                "thread_key": "telegram://operator-relay/thread-001",
                "memory_context_id": "memctx-reach-ce-001",
                "same_thread_resume": True,
            },
            "rate_limits": {
                "provider_limit_visible": True,
                "cooldown_seconds": 60,
                "retry_after_seconds": 90,
                "abuse_guard": "mute_repeated_external_mentions_until_operator_review",
            },
            "approval_handoff": {
                "approval_id": "approval-reach-ce-telegram-reply",
                "status": "pending_operator_approval",
                "mutation_boundary": "reply_draft_until_approved",
            },
            "degraded_recovery": {
                "status": "revoked_probe_blocked",
                "fallback_surface": "browser_cockpit",
                "unsafe_follow_up_hidden": True,
                "operator_visible": True,
            },
            "audit_receipts": [
                "audit:reach-ce:telegram-bound",
                "audit:reach-ce:telegram-revoked-probe",
                "audit:reach-ce:telegram-approval",
            ],
        },
        {
            "channel_id": "seraph.messaging.slack-review-relay",
            "provider": "slack-web-api",
            "transport": "slack",
            "evidence_mode": "configured_degraded",
            "recorded_at": "2026-06-10T02:10:00Z",
            "operator_identity": {
                "workspace_hash": "sha256:slack-workspace-001",
                "consent_receipt_id": "consent:reach-ce:slack-config",
                "identity_state": "unbound_until_pairing",
            },
            "pairing": {
                "state": "requires_pairing",
                "scopes": [],
                "revocation_probe_blocks_delivery": True,
            },
            "continuity": {
                "seraph_session_id": "session-reach-ce-001",
                "thread_key": None,
                "memory_context_id": "memctx-reach-ce-001",
                "same_thread_resume": False,
            },
            "rate_limits": {
                "provider_limit_visible": True,
                "cooldown_seconds": 0,
                "retry_after_seconds": None,
                "abuse_guard": "closed_until_identity_pairing",
            },
            "approval_handoff": {
                "status": "blocked_until_pairing",
                "mutation_boundary": "closed",
            },
            "degraded_recovery": {
                "status": "requires_config",
                "fallback_surface": "browser_cockpit",
                "unsafe_follow_up_hidden": True,
                "operator_visible": True,
            },
            "audit_receipts": ["audit:reach-ce:slack-config-required"],
        },
    ]


def production_voice_media_provider_receipts() -> list[dict[str, Any]]:
    return [
        {
            "runtime_id": "stt-live-operator-correction",
            "family": "speech_to_text",
            "provider": "openai-transcribe-profile",
            "evidence_mode": "recorded_live",
            "consent": {
                "consent_receipt_id": "consent:voice-ce:stt",
                "microphone_allowed": True,
                "capture_scope": "operator_initiated_clip",
            },
            "capture_boundary": {
                "surface": "microphone",
                "max_clip_seconds": 45,
                "content_redacted": True,
                "provider_destination_visible": True,
                "memory_import_requires_review": True,
            },
            "operator_controls": {
                "correction_path": "correct_transcript_before_memory_or_action_use",
                "deletion_path": "delete_transcript_and_block_memory_import",
                "revocation_blocks_capture": True,
            },
            "provider_failure": {
                "failure_mode": "provider_timeout",
                "fallback": "typed_confirmation",
                "unsafe_action_allowed": False,
                "operator_visible": True,
            },
            "audit_receipts": ["audit:voice-ce:stt-consent", "audit:voice-ce:stt-fallback"],
        },
        {
            "runtime_id": "tts-live-confirmation-playback",
            "family": "text_to_speech",
            "provider": "local-tts-profile",
            "evidence_mode": "recorded_live",
            "consent": {
                "consent_receipt_id": "consent:voice-ce:tts",
                "speaker_allowed": True,
                "capture_scope": "operator_selected_playback",
            },
            "capture_boundary": {
                "surface": "speaker",
                "max_clip_seconds": 30,
                "content_redacted": True,
                "provider_destination_visible": True,
                "memory_import_requires_review": False,
            },
            "operator_controls": {
                "correction_path": "edit_spoken_summary_before_playback",
                "deletion_path": "delete_generated_audio_receipt",
                "revocation_blocks_capture": True,
            },
            "provider_failure": {
                "failure_mode": "audio_device_unavailable",
                "fallback": "desktop_text_notification",
                "unsafe_action_allowed": False,
                "operator_visible": True,
            },
            "audit_receipts": ["audit:voice-ce:tts-consent", "audit:voice-ce:tts-fallback"],
        },
        {
            "runtime_id": "media-analysis-browser-vision-review",
            "family": "media_analysis",
            "provider": "browser-vision-review-profile",
            "evidence_mode": "recorded_live",
            "consent": {
                "consent_receipt_id": "consent:voice-ce:media",
                "screen_region_allowed": True,
                "capture_scope": "visible_browser_region_only",
            },
            "capture_boundary": {
                "surface": "browser_visible_region",
                "max_clip_seconds": None,
                "content_redacted": True,
                "provider_destination_visible": True,
                "memory_import_requires_review": True,
                "credential_scope_expanded": False,
            },
            "operator_controls": {
                "correction_path": "correct_media_summary_before_workflow_use",
                "deletion_path": "delete_media_summary_and_block_workflow_replay",
                "revocation_blocks_capture": True,
            },
            "provider_failure": {
                "failure_mode": "vision_confidence_low",
                "fallback": "request_operator_screenshot_annotation",
                "unsafe_action_allowed": False,
                "operator_visible": True,
            },
            "audit_receipts": ["audit:voice-ce:media-consent", "audit:voice-ce:media-fallback"],
        },
    ]


def cross_surface_continuity_recovery_receipts() -> list[dict[str, Any]]:
    return [
        {
            "recovery_id": "continuity-ce-browser-mobile-message",
            "surfaces": ["browser_cockpit", "desktop_notification", "mobile_push", "telegram"],
            "evidence_mode": "recorded_live",
            "thread_identity": {
                "seraph_session_id": "session-reach-ce-001",
                "thread_key": "thread://reach-ce/operator-primary",
                "same_thread_preserved": True,
                "memory_context_preserved": True,
            },
            "approval_boundary": {
                "approval_id": "approval-reach-ce-mobile-action",
                "approval_state": "pending_operator_approval",
                "approval_survived_surface_shift": True,
                "mutation_allowed_without_approval": False,
            },
            "routing": {
                "primary_surface": "mobile_push",
                "fallback_surface": "desktop_notification",
                "fallback_reason": "provider_rate_limited",
                "operator_visible": True,
            },
            "degraded_state": {
                "unsafe_follow_up_hidden": True,
                "recovery_action": "resume_in_browser_cockpit",
                "audit_receipt_id": "audit:continuity-ce:mobile-fallback",
            },
        },
        {
            "recovery_id": "continuity-ce-voice-message",
            "surfaces": ["voice_stt", "browser_cockpit", "telegram"],
            "evidence_mode": "recorded_live",
            "thread_identity": {
                "seraph_session_id": "session-reach-ce-voice-001",
                "thread_key": "thread://reach-ce/voice-operator",
                "same_thread_preserved": True,
                "memory_context_preserved": True,
            },
            "approval_boundary": {
                "approval_id": "approval-reach-ce-telegram-reply",
                "approval_state": "pending_operator_approval",
                "approval_survived_surface_shift": True,
                "mutation_allowed_without_approval": False,
            },
            "routing": {
                "primary_surface": "voice_stt",
                "fallback_surface": "browser_cockpit",
                "fallback_reason": "stt_provider_timeout",
                "operator_visible": True,
            },
            "degraded_state": {
                "unsafe_follow_up_hidden": True,
                "recovery_action": "operator_corrects_transcript_before_reply",
                "audit_receipt_id": "audit:continuity-ce:voice-fallback",
            },
        },
    ]


def build_live_reach_media_contract() -> dict[str, Any]:
    channels = live_broad_reach_channel_receipts()
    voice_media = production_voice_media_provider_receipts()
    continuity = cross_surface_continuity_recovery_receipts()
    policy = live_reach_media_policy_payload()
    return {
        "summary": {
            "operator_status": "live_reach_media_receipts_visible",
            "suite_name": "live_reach_media_proof",
            "live_reach_suite_name": LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SUITE_NAME,
            "voice_media_suite_name": PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SUITE_NAME,
            "continuity_suite_name": CROSS_SURFACE_CONTINUITY_RECOVERY_SUITE_NAME,
            "channel_receipt_count": len(channels),
            "recorded_live_channel_count": sum(
                1 for item in channels if item.get("evidence_mode") == "recorded_live"
            ),
            "paired_channel_count": sum(1 for item in channels if item.get("pairing", {}).get("state") == "paired"),
            "revocation_fail_closed_count": sum(
                1 for item in channels
                if item.get("pairing", {}).get("revocation_probe_blocks_delivery") is True
            ),
            "rate_limit_visible_count": sum(
                1 for item in channels if item.get("rate_limits", {}).get("provider_limit_visible") is True
            ),
            "degraded_recovery_visible_count": sum(
                1 for item in channels
                if item.get("degraded_recovery", {}).get("operator_visible") is True
                and item.get("degraded_recovery", {}).get("unsafe_follow_up_hidden") is True
            ),
            "voice_media_provider_count": len(voice_media),
            "voice_media_recorded_live_count": sum(
                1 for item in voice_media if item.get("evidence_mode") == "recorded_live"
            ),
            "voice_media_consent_count": sum(1 for item in voice_media if item.get("consent", {}).get("consent_receipt_id")),
            "voice_media_deletion_count": sum(
                1 for item in voice_media if item.get("operator_controls", {}).get("deletion_path")
            ),
            "voice_media_failure_fallback_count": sum(
                1 for item in voice_media
                if item.get("provider_failure", {}).get("operator_visible") is True
                and item.get("provider_failure", {}).get("unsafe_action_allowed") is False
            ),
            "cross_surface_recovery_count": len(continuity),
            "continuity_thread_preserved_count": sum(
                1 for item in continuity
                if item.get("thread_identity", {}).get("same_thread_preserved") is True
                and item.get("thread_identity", {}).get("memory_context_preserved") is True
            ),
            "approval_survived_surface_shift_count": sum(
                1 for item in continuity
                if item.get("approval_boundary", {}).get("approval_survived_surface_shift") is True
                and item.get("approval_boundary", {}).get("mutation_allowed_without_approval") is False
            ),
            "claim_boundary": LIVE_REACH_MEDIA_CLAIM_BOUNDARY,
        },
        "channels": channels,
        "voice_media_providers": voice_media,
        "cross_surface_recovery": continuity,
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
            "summary": str(getattr(result, "error", "") or "Live reach/media scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:6]


async def _run_live_reach_media_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SUITE_NAME,
        PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SUITE_NAME,
        CROSS_SURFACE_CONTINUITY_RECOVERY_SUITE_NAME,
    ])


async def build_live_reach_media_report() -> dict[str, Any]:
    summary = await _run_live_reach_media_suites()
    contract = build_live_reach_media_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "live_reach_media_ci_gated_operator_visible"
                if healthy
                else "live_reach_media_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SCENARIO_NAMES)
                + len(PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SCENARIO_NAMES)
                + len(CROSS_SURFACE_CONTINUITY_RECOVERY_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SUITE_NAME: list(
                LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SCENARIO_NAMES
            ),
            PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SUITE_NAME: list(
                PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SCENARIO_NAMES
            ),
            CROSS_SURFACE_CONTINUITY_RECOVERY_SUITE_NAME: list(
                CROSS_SURFACE_CONTINUITY_RECOVERY_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="live_reach_media_proof"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
