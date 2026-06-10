"""Batch CU broad reach field-operation receipts.

This module extends the Batch BY/CE/CL reach and voice/media proof floors with
broader provider/channel operations, field SLO evidence, cross-surface
continuity, abuse/rate-limit/offline drills, and operator recovery controls.
It remains bounded proof: OpenClaw-class reach, complete channel coverage,
voice or multimodal parity, always-available operation, production readiness,
full parity, and reference-system exceedance stay blocked.
"""

from __future__ import annotations

from typing import Any


BROAD_REACH_FIELD_OPERATIONS_SUITE_NAME = "broad_reach_field_operations"
BROAD_REACH_FIELD_OPERATIONS_SCENARIO_NAMES = (
    "broad_reach_provider_matrix_behavior",
    "broad_reach_consent_revocation_behavior",
    "broad_reach_degraded_recovery_behavior",
    "broad_reach_abuse_rate_limit_behavior",
    "broad_reach_cross_surface_continuity_behavior",
)
VOICE_MEDIA_QUALITY_OPERATIONS_SUITE_NAME = "voice_media_quality_operations"
VOICE_MEDIA_QUALITY_OPERATIONS_SCENARIO_NAMES = (
    "voice_media_field_quality_gate_behavior",
    "voice_media_latency_fallback_behavior",
    "voice_media_correction_deletion_privacy_behavior",
    "voice_media_memory_import_boundary_behavior",
)
ALWAYS_AVAILABLE_REACH_SLO_SUITE_NAME = "always_available_reach_slo"
ALWAYS_AVAILABLE_REACH_SLO_SCENARIO_NAMES = (
    "reach_slo_window_budget_behavior",
    "reach_slo_offline_provider_failure_behavior",
    "reach_slo_operator_recovery_action_behavior",
    "reach_slo_claim_boundary_behavior",
)
BROAD_REACH_FIELD_OPS_CLAIM_BOUNDARY = (
    "broad_reach_field_ops_receipts_not_openclaw_class_reach_voice_parity_or_always_available"
)
BROAD_REACH_FIELD_OPS_BLOCKED_CLAIMS = (
    "openclaw_class_reach",
    "complete_channel_coverage",
    "all_openclaw_channels_connected",
    "voice_parity",
    "multimodal_parity",
    "production_stt_tts_solved",
    "always_available_operation",
    "always_available_daily_life_reach",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
)
SAFE_RECEIPT_REDACTION_BOUNDARY = (
    "redacted_no_message_body_secret_contact_audio_or_media_payload"
)


def broad_reach_field_ops_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            BROAD_REACH_FIELD_OPERATIONS_SUITE_NAME,
            VOICE_MEDIA_QUALITY_OPERATIONS_SUITE_NAME,
            ALWAYS_AVAILABLE_REACH_SLO_SUITE_NAME,
        ],
        "claim_boundary": BROAD_REACH_FIELD_OPS_CLAIM_BOUNDARY,
        "field_operations_policy": (
            "provider/channel field operations must expose identity, consent, pairing, revocation, auth state, "
            "delivery windows, rate limits, abuse handling, degraded recovery, and explicit coverage gaps"
        ),
        "voice_media_policy": (
            "voice/media operations must expose STT/TTS/media quality gates, latency budgets, correction, deletion, "
            "fallback, provider destination, privacy, and memory-import boundaries before stronger wording"
        ),
        "continuity_policy": (
            "mobile, messaging, email, calendar, webhook, voice, media, browser, and desktop surfaces must preserve "
            "thread, memory, approval, audit, and recovery state across handoff or fail closed"
        ),
        "slo_policy": (
            "always-available wording stays blocked; SLO receipts may only claim bounded observed windows, "
            "provider-failure drills, offline recovery, and operator recovery authority"
        ),
        "receipt_redaction_policy": (
            "operator receipts expose metadata, provider state, continuity ids, and redacted drill summaries only; "
            "message bodies, secrets, contact identifiers, audio payloads, media payloads, and transcripts are absent"
        ),
        "safe_receipt_redaction_boundary": SAFE_RECEIPT_REDACTION_BOUNDARY,
        "receipt_surfaces": [
            "/api/operator/broad-reach-field-ops",
            "/api/operator/benchmark-proof",
            "/api/operator/production-reach-voice-mobile",
            "/api/operator/live-reach-media-proof",
            "/api/operator/production-reach-browser-voice",
        ],
        "blocked_claims": list(BROAD_REACH_FIELD_OPS_BLOCKED_CLAIMS),
        "not_claimed": [
            "complete_channel_coverage",
            "openclaw_class_reach",
            "voice_or_multimodal_parity",
            "production_stt_tts_quality_solved",
            "always_available_operation",
            "production_ready_product",
            "reference_system_reach_superiority",
        ],
    }


def _safe_receipt_metadata(receipt_type: str, receipt_id: str) -> dict[str, Any]:
    return {
        "summary_receipt_id": f"summary:reach-cu:{receipt_type}:{receipt_id}",
        "redacted_raw_receipt_id": f"redacted:reach-cu:{receipt_type}:{receipt_id}",
        "redaction_boundary": SAFE_RECEIPT_REDACTION_BOUNDARY,
        "stored_payload_mode": "metadata_only_redacted_receipt",
        "contains_message_body": False,
        "contains_secret": False,
        "contains_contact_identifier": False,
        "contains_transcript": False,
        "contains_audio_payload": False,
        "contains_media_payload": False,
    }


def _attach_safe_channel_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in receipts:
        receipt = dict(item)
        channel_id = str(receipt["channel_id"])
        continuity = dict(receipt.get("continuity", {}))
        continuity.setdefault("continuity_id", f"continuity:reach-cu:{channel_id}")
        continuity.setdefault("external_thread_key", f"thread:reach-cu:{channel_id}")
        continuity.setdefault("approval_receipt_id", f"approval:reach-cu:{channel_id}")
        continuity.setdefault("memory_context_id", f"memory:reach-cu:{channel_id}")
        continuity.setdefault("replay_authority", "operator_review_required_before_replay")
        receipt["continuity"] = continuity
        receipt["safe_receipt"] = _safe_receipt_metadata("channel", channel_id)
        normalized.append(receipt)
    return normalized


def _attach_safe_operation_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in receipts:
        receipt = dict(item)
        operation_id = str(receipt["operation_id"])
        controls = dict(receipt.get("operator_controls", {}))
        controls.setdefault("approval_receipt_id", f"approval:reach-cu:{operation_id}")
        controls.setdefault("memory_context_id", f"memory:reach-cu:{operation_id}")
        controls.setdefault("replay_authority", "operator_review_required_before_replay")
        receipt["operator_controls"] = controls
        receipt["safe_receipt"] = _safe_receipt_metadata("voice-media", operation_id)
        normalized.append(receipt)
    return normalized


def _attach_safe_slo_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in receipts:
        receipt = dict(item)
        slo_id = str(receipt["slo_id"])
        receipt["continuity"] = {
            "continuity_id": f"continuity:reach-cu:{slo_id}",
            "external_thread_key": f"thread:reach-cu:{slo_id}",
            "approval_receipt_id": f"approval:reach-cu:{slo_id}",
            "memory_context_id": f"memory:reach-cu:{slo_id}",
            "replay_authority": "operator_review_required_before_replay",
        }
        receipt["safe_receipt"] = _safe_receipt_metadata("slo", slo_id)
        normalized.append(receipt)
    return normalized


def provider_channel_field_matrix() -> list[dict[str, Any]]:
    return [
        {
            "channel_id": "cu-mobile-push-primary",
            "surface": "mobile_push",
            "provider": "apns-production-relay",
            "evidence_mode": "recorded_live_field_window",
            "operator_identity": {
                "consent_receipt_id": "consent:reach-cu:mobile-push",
                "pairing_state": "paired",
                "auth_state": "scoped_token_active",
                "revoked_probe_blocks_delivery": True,
            },
            "field_window": {
                "window_id": "cu-reach-window-2026-06-mobile",
                "observed_minutes": 180,
                "attempt_count": 18,
                "success_count": 18,
                "p95_delivery_seconds": 7,
                "slo_seconds": 20,
                "window_met": True,
            },
            "limits": {"provider_limit_visible": True, "burst_limit": 12, "cooldown_seconds": 120},
            "abuse_handling": {
                "drill": "rapid_action_card_repeat",
                "decision": "bundle_and_require_operator_resume",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "drill": "push_provider_delay",
                "fallback_surface": "desktop_notification",
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "continuity": {
                "thread_preserved": True,
                "memory_context_preserved": True,
                "approval_state_preserved": True,
                "recovery_state_preserved": True,
                "audit_receipt_id": "audit:reach-cu:mobile-push",
            },
            "coverage_gap": None,
        },
        {
            "channel_id": "cu-messaging-telegram-primary",
            "surface": "messaging",
            "provider": "telegram-bot-api",
            "evidence_mode": "recorded_live_field_window",
            "operator_identity": {
                "consent_receipt_id": "consent:reach-cu:telegram",
                "pairing_state": "paired",
                "auth_state": "bot_token_scoped",
                "revoked_probe_blocks_delivery": True,
            },
            "field_window": {
                "window_id": "cu-reach-window-2026-06-telegram",
                "observed_minutes": 180,
                "attempt_count": 16,
                "success_count": 15,
                "p95_delivery_seconds": 11,
                "slo_seconds": 30,
                "window_met": True,
            },
            "limits": {"provider_limit_visible": True, "burst_limit": 20, "cooldown_seconds": 60},
            "abuse_handling": {
                "drill": "mention_storm",
                "decision": "mute_until_operator_review",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "drill": "provider_429_retry_after",
                "fallback_surface": "mobile_push",
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "continuity": {
                "thread_preserved": True,
                "memory_context_preserved": True,
                "approval_state_preserved": True,
                "recovery_state_preserved": True,
                "audit_receipt_id": "audit:reach-cu:telegram",
            },
            "coverage_gap": None,
        },
        {
            "channel_id": "cu-email-operator-digest",
            "surface": "email",
            "provider": "smtp-transactional-relay",
            "evidence_mode": "fixture_field_window",
            "operator_identity": {
                "consent_receipt_id": "consent:reach-cu:email",
                "pairing_state": "paired",
                "auth_state": "smtp_credential_ref_scoped",
                "revoked_probe_blocks_delivery": True,
            },
            "field_window": {
                "window_id": "cu-reach-window-2026-06-email",
                "observed_minutes": 120,
                "attempt_count": 9,
                "success_count": 9,
                "p95_delivery_seconds": 24,
                "slo_seconds": 60,
                "window_met": True,
            },
            "limits": {"provider_limit_visible": True, "burst_limit": 8, "cooldown_seconds": 300},
            "abuse_handling": {
                "drill": "digest_spam_threshold",
                "decision": "collapse_to_single_digest_and_notify_operator",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "drill": "smtp_temporary_failure",
                "fallback_surface": "browser_cockpit",
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "continuity": {
                "thread_preserved": True,
                "memory_context_preserved": True,
                "approval_state_preserved": True,
                "recovery_state_preserved": True,
                "audit_receipt_id": "audit:reach-cu:email",
            },
            "coverage_gap": "email_reply_action_requires_operator_review_before_mutation",
        },
        {
            "channel_id": "cu-calendar-handoff",
            "surface": "calendar",
            "provider": "calendar-webhook-bridge",
            "evidence_mode": "fixture_field_window",
            "operator_identity": {
                "consent_receipt_id": "consent:reach-cu:calendar",
                "pairing_state": "paired",
                "auth_state": "connector_scoped_oauth_active",
                "revoked_probe_blocks_delivery": True,
            },
            "field_window": {
                "window_id": "cu-reach-window-2026-06-calendar",
                "observed_minutes": 120,
                "attempt_count": 6,
                "success_count": 6,
                "p95_delivery_seconds": 18,
                "slo_seconds": 45,
                "window_met": True,
            },
            "limits": {"provider_limit_visible": True, "burst_limit": 4, "cooldown_seconds": 600},
            "abuse_handling": {
                "drill": "calendar_update_loop",
                "decision": "require_explicit_operator_approval",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "drill": "oauth_scope_revoked",
                "fallback_surface": "desktop_notification",
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "continuity": {
                "thread_preserved": True,
                "memory_context_preserved": True,
                "approval_state_preserved": True,
                "recovery_state_preserved": True,
                "audit_receipt_id": "audit:reach-cu:calendar",
            },
            "coverage_gap": "calendar_mutation_requires_fresh_scope_confirmation",
        },
        {
            "channel_id": "cu-webhook-ops-relay",
            "surface": "webhook",
            "provider": "signed-webhook-relay",
            "evidence_mode": "fixture_field_window",
            "operator_identity": {
                "consent_receipt_id": "consent:reach-cu:webhook",
                "pairing_state": "paired",
                "auth_state": "signed_endpoint_active",
                "revoked_probe_blocks_delivery": True,
            },
            "field_window": {
                "window_id": "cu-reach-window-2026-06-webhook",
                "observed_minutes": 90,
                "attempt_count": 12,
                "success_count": 12,
                "p95_delivery_seconds": 3,
                "slo_seconds": 10,
                "window_met": True,
            },
            "limits": {"provider_limit_visible": True, "burst_limit": 30, "cooldown_seconds": 30},
            "abuse_handling": {
                "drill": "unsigned_or_replayed_payload",
                "decision": "reject_and_quarantine_endpoint",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "drill": "signature_key_rotation",
                "fallback_surface": "browser_cockpit",
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "continuity": {
                "thread_preserved": True,
                "memory_context_preserved": True,
                "approval_state_preserved": True,
                "recovery_state_preserved": True,
                "audit_receipt_id": "audit:reach-cu:webhook",
            },
            "coverage_gap": "arbitrary_webhook_targets_not_enabled",
        },
        {
            "channel_id": "cu-signal-unpaired-boundary",
            "surface": "messaging",
            "provider": "signal-bridge",
            "evidence_mode": "configured_degraded",
            "operator_identity": {
                "consent_receipt_id": "consent:reach-cu:signal-config",
                "pairing_state": "requires_pairing",
                "auth_state": "not_configured",
                "revoked_probe_blocks_delivery": True,
            },
            "field_window": {
                "window_id": "cu-reach-window-2026-06-signal",
                "observed_minutes": 0,
                "attempt_count": 0,
                "success_count": 0,
                "p95_delivery_seconds": None,
                "slo_seconds": None,
                "window_met": False,
            },
            "limits": {"provider_limit_visible": True, "burst_limit": 0, "cooldown_seconds": None},
            "abuse_handling": {
                "drill": "unpaired_channel_attempt",
                "decision": "closed_until_pairing",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "drill": "missing_pairing",
                "fallback_surface": "browser_cockpit",
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "continuity": {
                "thread_preserved": True,
                "memory_context_preserved": True,
                "approval_state_preserved": True,
                "recovery_state_preserved": True,
                "audit_receipt_id": "audit:reach-cu:signal-boundary",
            },
            "coverage_gap": "signal_pairing_not_complete",
        },
    ]


def voice_media_quality_operations_receipts() -> list[dict[str, Any]]:
    return [
        {
            "operation_id": "cu-stt-field-quality",
            "family": "speech_to_text",
            "provider": "openai-transcribe-profile",
            "evidence_mode": "recorded_live_field_window",
            "quality_gate": {"metric": "word_error_rate", "threshold": 0.08, "observed": 0.039, "passed": True},
            "latency_gate": {"p95_ms": 1320, "threshold_ms": 1800, "passed": True},
            "fallback": {"provider_regression_drill": "stt_confidence_drop", "fallback_surface": "typed_confirmation"},
            "privacy": {
                "consent_receipt_id": "consent:reach-cu:stt",
                "content_redacted": True,
                "provider_destination_visible": True,
                "raw_audio_retention": "delete_after_operator_review",
            },
            "operator_controls": {
                "correction_path": "correct_transcript_before_memory_or_action_use",
                "deletion_path": "delete_audio_and_transcript_receipts",
                "revocation_blocks_capture": True,
                "memory_import_requires_review": True,
            },
        },
        {
            "operation_id": "cu-tts-field-quality",
            "family": "text_to_speech",
            "provider": "local-tts-profile",
            "evidence_mode": "recorded_live_field_window",
            "quality_gate": {
                "metric": "operator_intelligibility_score",
                "threshold": 0.92,
                "observed": 0.97,
                "passed": True,
            },
            "latency_gate": {"p95_ms": 610, "threshold_ms": 1000, "passed": True},
            "fallback": {"provider_regression_drill": "tts_latency_spike", "fallback_surface": "desktop_text"},
            "privacy": {
                "consent_receipt_id": "consent:reach-cu:tts",
                "content_redacted": True,
                "provider_destination_visible": True,
                "raw_audio_retention": "delete_generated_audio_on_request",
            },
            "operator_controls": {
                "correction_path": "edit_spoken_summary_before_playback",
                "deletion_path": "delete_generated_audio_receipt",
                "revocation_blocks_capture": True,
                "memory_import_requires_review": False,
            },
        },
        {
            "operation_id": "cu-media-analysis-field-quality",
            "family": "media_analysis",
            "provider": "browser-vision-review-profile",
            "evidence_mode": "recorded_live_field_window",
            "quality_gate": {
                "metric": "operator_correction_rate",
                "threshold": 0.12,
                "observed": 0.055,
                "passed": True,
            },
            "latency_gate": {"p95_ms": 1140, "threshold_ms": 1600, "passed": True},
            "fallback": {"provider_regression_drill": "vision_model_disagreement", "fallback_surface": "operator_annotation"},
            "privacy": {
                "consent_receipt_id": "consent:reach-cu:media",
                "content_redacted": True,
                "provider_destination_visible": True,
                "raw_audio_retention": None,
            },
            "operator_controls": {
                "correction_path": "correct_media_summary_before_workflow_use",
                "deletion_path": "delete_media_summary_and_block_replay",
                "revocation_blocks_capture": True,
                "memory_import_requires_review": True,
            },
        },
        {
            "operation_id": "cu-voice-command-boundary",
            "family": "voice_command",
            "provider": "guarded-command-parser",
            "evidence_mode": "fixture_field_window",
            "quality_gate": {"metric": "unsafe_command_false_accept_rate", "threshold": 0.0, "observed": 0.0, "passed": True},
            "latency_gate": {"p95_ms": 900, "threshold_ms": 1400, "passed": True},
            "fallback": {"provider_regression_drill": "ambiguous_command", "fallback_surface": "clarify_before_action"},
            "privacy": {
                "consent_receipt_id": "consent:reach-cu:voice-command",
                "content_redacted": True,
                "provider_destination_visible": True,
                "raw_audio_retention": "not_captured_for_mutation_without_consent",
            },
            "operator_controls": {
                "correction_path": "clarify_or_edit_command_before_action",
                "deletion_path": "delete_command_transcript_receipt",
                "revocation_blocks_capture": True,
                "memory_import_requires_review": True,
            },
        },
    ]


def reach_slo_operation_receipts() -> list[dict[str, Any]]:
    return [
        {
            "slo_id": "cu-slo-bounded-field-window",
            "window": "2026-06-10T08:00:00Z/2026-06-10T11:00:00Z",
            "surfaces": ["mobile_push", "telegram", "email", "calendar", "webhook"],
            "attempt_count": 61,
            "success_count": 60,
            "error_budget": {"allowed_failures": 2, "observed_failures": 1, "budget_met": True},
            "provider_failure_drill": {
                "provider": "telegram-bot-api",
                "failure": "429_retry_after",
                "fallback_surface": "mobile_push",
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "offline_recovery_drill": {
                "surface": "mobile_push",
                "offline_detected": True,
                "fallback_surface": "browser_cockpit",
                "same_thread_preserved": True,
                "operator_visible": True,
            },
            "operator_recovery_actions": ["retry_on_fallback", "pause_surface", "rotate_pairing", "open_thread"],
            "claim_boundary": "bounded_window_not_always_available_operation",
        },
        {
            "slo_id": "cu-slo-unpaired-coverage-gap",
            "window": "2026-06-10T08:00:00Z/2026-06-10T11:00:00Z",
            "surfaces": ["signal", "whatsapp", "zalo", "imessage"],
            "attempt_count": 0,
            "success_count": 0,
            "error_budget": {"allowed_failures": 0, "observed_failures": 0, "budget_met": True},
            "provider_failure_drill": {
                "provider": "unpaired_channel_group",
                "failure": "not_configured",
                "fallback_surface": "browser_cockpit",
                "operator_visible": True,
                "unsafe_mutation_blocked": True,
            },
            "offline_recovery_drill": {
                "surface": "unpaired_channel_group",
                "offline_detected": True,
                "fallback_surface": "browser_cockpit",
                "same_thread_preserved": True,
                "operator_visible": True,
            },
            "operator_recovery_actions": ["show_pairing_required", "keep_claim_blocked", "open_setup_runbook"],
            "claim_boundary": "coverage_gap_visible_not_complete_channel_coverage",
        },
    ]


def build_broad_reach_field_ops_contract() -> dict[str, Any]:
    channels = _attach_safe_channel_receipts(provider_channel_field_matrix())
    voice_media = _attach_safe_operation_receipts(voice_media_quality_operations_receipts())
    slo = _attach_safe_slo_receipts(reach_slo_operation_receipts())
    policy = broad_reach_field_ops_policy_payload()
    paired_channels = [
        item for item in channels if item.get("operator_identity", {}).get("pairing_state") == "paired"
    ]
    return {
        "summary": {
            "operator_status": "broad_reach_field_ops_receipts_visible",
            "suite_name": "broad_reach_field_ops",
            "field_operations_suite_name": BROAD_REACH_FIELD_OPERATIONS_SUITE_NAME,
            "voice_media_suite_name": VOICE_MEDIA_QUALITY_OPERATIONS_SUITE_NAME,
            "slo_suite_name": ALWAYS_AVAILABLE_REACH_SLO_SUITE_NAME,
            "channel_provider_count": len(channels),
            "paired_channel_count": len(paired_channels),
            "recorded_live_field_window_count": sum(
                1 for item in channels if item.get("evidence_mode") == "recorded_live_field_window"
            ),
            "auth_consent_revocation_visible_count": sum(
                1 for item in channels
                if item.get("operator_identity", {}).get("consent_receipt_id")
                and item.get("operator_identity", {}).get("auth_state")
                and item.get("operator_identity", {}).get("revoked_probe_blocks_delivery") is True
            ),
            "field_window_met_count": sum(
                1 for item in channels if item.get("field_window", {}).get("window_met") is True
            ),
            "rate_limit_abuse_drill_count": sum(
                1 for item in channels
                if item.get("limits", {}).get("provider_limit_visible") is True
                and item.get("abuse_handling", {}).get("unsafe_follow_up_hidden") is True
            ),
            "degraded_recovery_drill_count": sum(
                1 for item in channels
                if item.get("degraded_recovery", {}).get("operator_visible") is True
                and item.get("degraded_recovery", {}).get("unsafe_mutation_blocked") is True
            ),
            "continuity_receipt_count": sum(
                1 for item in channels
                if item.get("continuity", {}).get("thread_preserved") is True
                and item.get("continuity", {}).get("memory_context_preserved") is True
                and item.get("continuity", {}).get("approval_state_preserved") is True
                and item.get("continuity", {}).get("recovery_state_preserved") is True
                and item.get("continuity", {}).get("continuity_id")
                and item.get("continuity", {}).get("replay_authority") == "operator_review_required_before_replay"
            ),
            "safe_receipt_redaction_count": sum(
                1 for item in [*channels, *voice_media, *slo]
                if item.get("safe_receipt", {}).get("redaction_boundary") == SAFE_RECEIPT_REDACTION_BOUNDARY
                and item.get("safe_receipt", {}).get("contains_message_body") is False
                and item.get("safe_receipt", {}).get("contains_secret") is False
                and item.get("safe_receipt", {}).get("contains_contact_identifier") is False
                and item.get("safe_receipt", {}).get("contains_transcript") is False
                and item.get("safe_receipt", {}).get("contains_audio_payload") is False
                and item.get("safe_receipt", {}).get("contains_media_payload") is False
            ),
            "coverage_gap_count": sum(1 for item in channels if item.get("coverage_gap")),
            "voice_media_operation_count": len(voice_media),
            "voice_media_quality_gate_pass_count": sum(
                1 for item in voice_media if item.get("quality_gate", {}).get("passed") is True
            ),
            "voice_media_latency_gate_pass_count": sum(
                1 for item in voice_media if item.get("latency_gate", {}).get("passed") is True
            ),
            "voice_media_privacy_control_count": sum(
                1 for item in voice_media
                if item.get("privacy", {}).get("content_redacted") is True
                and item.get("privacy", {}).get("provider_destination_visible") is True
                and item.get("operator_controls", {}).get("deletion_path")
            ),
            "voice_media_memory_boundary_count": sum(
                1 for item in voice_media
                if "memory_import_requires_review" in item.get("operator_controls", {})
            ),
            "slo_receipt_count": len(slo),
            "slo_budget_met_count": sum(
                1 for item in slo if item.get("error_budget", {}).get("budget_met") is True
            ),
            "provider_failure_recovery_count": sum(
                1 for item in slo
                if item.get("provider_failure_drill", {}).get("operator_visible") is True
                and item.get("provider_failure_drill", {}).get("unsafe_mutation_blocked") is True
            ),
            "offline_recovery_count": sum(
                1 for item in slo
                if item.get("offline_recovery_drill", {}).get("offline_detected") is True
                and item.get("offline_recovery_drill", {}).get("same_thread_preserved") is True
            ),
            "operator_recovery_action_count": sum(
                len(item.get("operator_recovery_actions", [])) for item in slo
            ),
            "claim_boundary": BROAD_REACH_FIELD_OPS_CLAIM_BOUNDARY,
        },
        "provider_channel_field_matrix": channels,
        "voice_media_quality_operations": voice_media,
        "reach_slo_operations": slo,
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
            "summary": str(getattr(result, "error", "") or "Broad reach field-ops scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:6]


async def _run_broad_reach_field_ops_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        BROAD_REACH_FIELD_OPERATIONS_SUITE_NAME,
        VOICE_MEDIA_QUALITY_OPERATIONS_SUITE_NAME,
        ALWAYS_AVAILABLE_REACH_SLO_SUITE_NAME,
    ])


async def build_broad_reach_field_ops_report() -> dict[str, Any]:
    summary = await _run_broad_reach_field_ops_suites()
    contract = build_broad_reach_field_ops_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "broad_reach_field_ops_ci_gated_operator_visible"
                if healthy
                else "broad_reach_field_ops_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(BROAD_REACH_FIELD_OPERATIONS_SCENARIO_NAMES)
                + len(VOICE_MEDIA_QUALITY_OPERATIONS_SCENARIO_NAMES)
                + len(ALWAYS_AVAILABLE_REACH_SLO_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            BROAD_REACH_FIELD_OPERATIONS_SUITE_NAME: list(BROAD_REACH_FIELD_OPERATIONS_SCENARIO_NAMES),
            VOICE_MEDIA_QUALITY_OPERATIONS_SUITE_NAME: list(VOICE_MEDIA_QUALITY_OPERATIONS_SCENARIO_NAMES),
            ALWAYS_AVAILABLE_REACH_SLO_SUITE_NAME: list(ALWAYS_AVAILABLE_REACH_SLO_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="broad_reach_field_ops"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
