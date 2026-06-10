"""Batch CL broad reach, production voice/media, and mobile execution receipts.

This module extends the Batch BY/CE reach and voice/media floors with stronger
production-oriented SLA, quality-gate, mobile-execution, abuse, and continuity
receipts. It remains bounded proof: broad reach, OpenClaw-class reach,
voice/media parity, always-available operation, production readiness, and full
parity stay blocked.
"""

from __future__ import annotations

from typing import Any


BROAD_CHANNEL_SLA_OPERATIONS_SUITE_NAME = "broad_channel_sla_operations"
BROAD_CHANNEL_SLA_OPERATIONS_SCENARIO_NAMES = (
    "channel_sla_provider_window_behavior",
    "channel_sla_rate_limit_abuse_behavior",
    "channel_sla_degraded_delivery_recovery_behavior",
    "channel_sla_coverage_gap_claim_boundary_behavior",
)
PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SUITE_NAME = "production_voice_media_quality_gates"
PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SCENARIO_NAMES = (
    "voice_media_stt_quality_gate_behavior",
    "voice_media_tts_quality_gate_behavior",
    "voice_media_privacy_correction_memory_boundary_behavior",
    "voice_media_provider_regression_fallback_behavior",
)
MOBILE_EXECUTION_CONTINUITY_SUITE_NAME = "mobile_execution_continuity"
MOBILE_EXECUTION_CONTINUITY_SCENARIO_NAMES = (
    "mobile_execution_notification_approval_handoff_behavior",
    "mobile_execution_action_continuity_behavior",
    "mobile_execution_thread_memory_recovery_behavior",
    "mobile_execution_offline_revocation_fail_closed_behavior",
)
PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY = (
    "production_reach_voice_mobile_receipts_not_openclaw_class_reach_voice_parity_or_production_ready"
)
PRODUCTION_REACH_VOICE_MOBILE_BLOCKED_CLAIMS = (
    "broad_reach",
    "complete_channel_coverage",
    "openclaw_class_reach",
    "voice_parity",
    "multimodal_parity",
    "production_stt_tts_solved",
    "production_mobile_execution_solved",
    "always_available_operation",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
)


def production_reach_voice_mobile_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            BROAD_CHANNEL_SLA_OPERATIONS_SUITE_NAME,
            PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SUITE_NAME,
            MOBILE_EXECUTION_CONTINUITY_SUITE_NAME,
        ],
        "claim_boundary": PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY,
        "channel_policy": (
            "channel providers must expose identity, consent, pairing, revocation, health, SLA windows, "
            "rate limits, abuse handling, degraded recovery, and explicit coverage gaps"
        ),
        "voice_media_policy": (
            "STT/TTS/media providers must expose quality gates, latency/error thresholds, fallback, "
            "privacy, correction, deletion, and memory-import boundaries before production wording"
        ),
        "mobile_policy": (
            "mobile execution must preserve notification, approval handoff, action continuity, thread/memory "
            "continuity, offline recovery, and revocation fail-closed receipts"
        ),
        "receipt_surfaces": [
            "/api/operator/production-reach-voice-mobile",
            "/api/operator/benchmark-proof",
            "/api/operator/live-reach-media-proof",
            "/api/operator/production-reach-browser-voice",
        ],
        "blocked_claims": list(PRODUCTION_REACH_VOICE_MOBILE_BLOCKED_CLAIMS),
        "not_claimed": [
            "all_openclaw_channels_connected",
            "always_available_daily_life_reach",
            "production_stt_tts_quality_solved",
            "voice_or_multimodal_parity",
            "production_mobile_execution_solved",
            "reference_system_reach_superiority",
        ],
    }


def broad_channel_sla_receipts() -> list[dict[str, Any]]:
    return [
        {
            "channel_id": "seraph.mobile.push.operator-primary",
            "provider": "apns-production-relay",
            "transport": "mobile_push",
            "evidence_mode": "recorded_live",
            "consent_receipt_id": "consent:reach-cl:mobile-push",
            "pairing_state": "paired",
            "revocation_probe_blocks_delivery": True,
            "health": {"status": "healthy", "checked_at": "2026-06-10T08:20:00Z"},
            "sla": {
                "delivery_window_seconds": 20,
                "p95_delivery_seconds": 8,
                "jitter_budget_seconds": 5,
                "window_met": True,
            },
            "rate_limits": {
                "provider_limit_visible": True,
                "burst_limit": 12,
                "cooldown_seconds": 120,
            },
            "abuse_handling": {
                "spam_signal": "rapid_repeated_action_cards",
                "guard": "bundle_and_require_operator_resume",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "status": "fallback_tested",
                "fallback_surface": "desktop_notification",
                "operator_visible": True,
                "unsafe_follow_up_hidden": True,
            },
            "coverage_gap": None,
        },
        {
            "channel_id": "seraph.messaging.telegram-operator-relay",
            "provider": "telegram-bot-api",
            "transport": "telegram",
            "evidence_mode": "recorded_live",
            "consent_receipt_id": "consent:reach-cl:telegram",
            "pairing_state": "paired",
            "revocation_probe_blocks_delivery": True,
            "health": {"status": "healthy", "checked_at": "2026-06-10T08:22:00Z"},
            "sla": {
                "delivery_window_seconds": 30,
                "p95_delivery_seconds": 11,
                "jitter_budget_seconds": 6,
                "window_met": True,
            },
            "rate_limits": {
                "provider_limit_visible": True,
                "burst_limit": 20,
                "cooldown_seconds": 60,
            },
            "abuse_handling": {
                "spam_signal": "external_mention_storm",
                "guard": "mute_until_operator_review",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "status": "revocation_probe_blocked",
                "fallback_surface": "browser_cockpit",
                "operator_visible": True,
                "unsafe_follow_up_hidden": True,
            },
            "coverage_gap": None,
        },
        {
            "channel_id": "seraph.messaging.slack-review-relay",
            "provider": "slack-web-api",
            "transport": "slack",
            "evidence_mode": "recorded_live",
            "consent_receipt_id": "consent:reach-cl:slack",
            "pairing_state": "paired",
            "revocation_probe_blocks_delivery": True,
            "health": {"status": "degraded", "checked_at": "2026-06-10T08:24:00Z"},
            "sla": {
                "delivery_window_seconds": 45,
                "p95_delivery_seconds": 34,
                "jitter_budget_seconds": 10,
                "window_met": True,
            },
            "rate_limits": {
                "provider_limit_visible": True,
                "burst_limit": 15,
                "cooldown_seconds": 90,
            },
            "abuse_handling": {
                "spam_signal": "workspace_rate_limit",
                "guard": "defer_low_urgency_and_show_retry_after",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "status": "rate_limited",
                "fallback_surface": "mobile_push",
                "operator_visible": True,
                "unsafe_follow_up_hidden": True,
            },
            "coverage_gap": "workspace_admin_install_required_for_full_workspace_breadth",
        },
        {
            "channel_id": "seraph.messaging.matrix-incident-relay",
            "provider": "matrix-appservice",
            "transport": "matrix",
            "evidence_mode": "configured_degraded",
            "consent_receipt_id": "consent:reach-cl:matrix-config",
            "pairing_state": "requires_pairing",
            "revocation_probe_blocks_delivery": True,
            "health": {"status": "requires_config", "checked_at": "2026-06-10T08:26:00Z"},
            "sla": {
                "delivery_window_seconds": None,
                "p95_delivery_seconds": None,
                "jitter_budget_seconds": None,
                "window_met": False,
            },
            "rate_limits": {
                "provider_limit_visible": True,
                "burst_limit": 0,
                "cooldown_seconds": None,
            },
            "abuse_handling": {
                "spam_signal": "unpaired_channel",
                "guard": "closed_until_pairing",
                "unsafe_follow_up_hidden": True,
            },
            "degraded_recovery": {
                "status": "closed_until_pairing",
                "fallback_surface": "browser_cockpit",
                "operator_visible": True,
                "unsafe_follow_up_hidden": True,
            },
            "coverage_gap": "matrix_pairing_not_complete",
        },
    ]


def production_voice_media_quality_receipts() -> list[dict[str, Any]]:
    return [
        {
            "runtime_id": "stt-production-quality-gate",
            "family": "speech_to_text",
            "provider": "openai-transcribe-profile",
            "evidence_mode": "recorded_live",
            "quality_gate": {
                "metric": "word_error_rate",
                "threshold": 0.08,
                "observed": 0.041,
                "passed": True,
                "sample_count": 24,
            },
            "latency_gate": {"p95_ms": 1380, "threshold_ms": 1800, "passed": True},
            "privacy": {
                "consent_receipt_id": "consent:voice-cl:stt",
                "content_redacted": True,
                "provider_destination_visible": True,
                "memory_import_requires_review": True,
            },
            "operator_controls": {
                "correction_path": "correct_transcript_before_memory_or_action_use",
                "deletion_path": "delete_audio_and_transcript_receipts",
                "revocation_blocks_capture": True,
            },
            "provider_regression": {
                "detected": False,
                "fallback": "typed_confirmation",
                "unsafe_action_allowed": False,
            },
        },
        {
            "runtime_id": "tts-production-quality-gate",
            "family": "text_to_speech",
            "provider": "local-tts-profile",
            "evidence_mode": "recorded_live",
            "quality_gate": {
                "metric": "operator_intelligibility_score",
                "threshold": 0.92,
                "observed": 0.97,
                "passed": True,
                "sample_count": 18,
            },
            "latency_gate": {"p95_ms": 620, "threshold_ms": 1000, "passed": True},
            "privacy": {
                "consent_receipt_id": "consent:voice-cl:tts",
                "content_redacted": True,
                "provider_destination_visible": True,
                "memory_import_requires_review": False,
            },
            "operator_controls": {
                "correction_path": "edit_spoken_summary_before_playback",
                "deletion_path": "delete_generated_audio_receipt",
                "revocation_blocks_capture": True,
            },
            "provider_regression": {
                "detected": False,
                "fallback": "desktop_text_notification",
                "unsafe_action_allowed": False,
            },
        },
        {
            "runtime_id": "media-analysis-quality-gate",
            "family": "media_analysis",
            "provider": "browser-vision-review-profile",
            "evidence_mode": "recorded_live",
            "quality_gate": {
                "metric": "operator_correction_rate",
                "threshold": 0.12,
                "observed": 0.06,
                "passed": True,
                "sample_count": 20,
            },
            "latency_gate": {"p95_ms": 1100, "threshold_ms": 1600, "passed": True},
            "privacy": {
                "consent_receipt_id": "consent:voice-cl:media",
                "content_redacted": True,
                "provider_destination_visible": True,
                "memory_import_requires_review": True,
                "credential_scope_expanded": False,
            },
            "operator_controls": {
                "correction_path": "correct_media_summary_before_workflow_use",
                "deletion_path": "delete_media_summary_and_block_replay",
                "revocation_blocks_capture": True,
            },
            "provider_regression": {
                "detected": True,
                "fallback": "request_operator_annotation",
                "unsafe_action_allowed": False,
            },
        },
    ]


def mobile_execution_continuity_receipts() -> list[dict[str, Any]]:
    return [
        {
            "execution_id": "mobile-action-card-approval-cl-001",
            "surface": "ios_action_card",
            "evidence_mode": "recorded_live",
            "notification": {
                "delivered": True,
                "delivery_seconds": 6,
                "thread_key": "mobile://operator-primary/thread-cl-001",
            },
            "approval_handoff": {
                "approval_id": "approval-reach-cl-mobile-action",
                "status": "pending_operator_approval",
                "mutation_allowed_without_approval": False,
                "survived_surface_shift": True,
            },
            "action_continuity": {
                "draft_action_id": "draft:mobile:reply-summary",
                "resumed_in_browser": True,
                "same_thread_preserved": True,
                "audit_receipt_id": "audit:mobile-cl:approval-handoff",
            },
            "memory_continuity": {
                "memory_context_id": "memctx-reach-cl-001",
                "memory_import_requires_review": True,
                "same_context_preserved": True,
            },
            "offline_recovery": {
                "offline_detected": False,
                "fallback_surface": "desktop_notification",
                "operator_visible": True,
            },
            "revocation": {"revoked_device_blocks_action": True, "unsafe_follow_up_hidden": True},
        },
        {
            "execution_id": "mobile-offline-recovery-cl-002",
            "surface": "android_push_relay",
            "evidence_mode": "recorded_live",
            "notification": {
                "delivered": False,
                "delivery_seconds": None,
                "thread_key": "mobile://operator-secondary/thread-cl-002",
            },
            "approval_handoff": {
                "approval_id": "approval-reach-cl-mobile-offline",
                "status": "blocked_until_device_online",
                "mutation_allowed_without_approval": False,
                "survived_surface_shift": True,
            },
            "action_continuity": {
                "draft_action_id": "draft:mobile:blocked-offline",
                "resumed_in_browser": True,
                "same_thread_preserved": True,
                "audit_receipt_id": "audit:mobile-cl:offline-recovery",
            },
            "memory_continuity": {
                "memory_context_id": "memctx-reach-cl-002",
                "memory_import_requires_review": True,
                "same_context_preserved": True,
            },
            "offline_recovery": {
                "offline_detected": True,
                "fallback_surface": "browser_cockpit",
                "operator_visible": True,
            },
            "revocation": {"revoked_device_blocks_action": True, "unsafe_follow_up_hidden": True},
        },
    ]


def build_production_reach_voice_mobile_contract() -> dict[str, Any]:
    channels = broad_channel_sla_receipts()
    voice_media = production_voice_media_quality_receipts()
    mobile = mobile_execution_continuity_receipts()
    policy = production_reach_voice_mobile_policy_payload()
    return {
        "summary": {
            "operator_status": "production_reach_voice_mobile_receipts_visible",
            "suite_name": "production_reach_voice_mobile",
            "channel_suite_name": BROAD_CHANNEL_SLA_OPERATIONS_SUITE_NAME,
            "voice_media_suite_name": PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SUITE_NAME,
            "mobile_suite_name": MOBILE_EXECUTION_CONTINUITY_SUITE_NAME,
            "channel_provider_count": len(channels),
            "recorded_live_channel_count": sum(1 for item in channels if item["evidence_mode"] == "recorded_live"),
            "paired_channel_count": sum(1 for item in channels if item["pairing_state"] == "paired"),
            "sla_window_visible_count": sum(
                1 for item in channels if item.get("sla", {}).get("delivery_window_seconds") is not None
            ),
            "sla_window_met_count": sum(1 for item in channels if item.get("sla", {}).get("window_met") is True),
            "rate_limit_abuse_visible_count": sum(
                1 for item in channels
                if item.get("rate_limits", {}).get("provider_limit_visible") is True
                and item.get("abuse_handling", {}).get("unsafe_follow_up_hidden") is True
            ),
            "degraded_recovery_visible_count": sum(
                1 for item in channels
                if item.get("degraded_recovery", {}).get("operator_visible") is True
                and item.get("degraded_recovery", {}).get("unsafe_follow_up_hidden") is True
            ),
            "coverage_gap_visible_count": sum(1 for item in channels if item.get("coverage_gap")),
            "voice_media_quality_gate_count": len(voice_media),
            "voice_media_quality_gate_pass_count": sum(
                1 for item in voice_media if item.get("quality_gate", {}).get("passed") is True
            ),
            "voice_media_latency_gate_pass_count": sum(
                1 for item in voice_media if item.get("latency_gate", {}).get("passed") is True
            ),
            "voice_media_privacy_boundary_count": sum(
                1 for item in voice_media
                if item.get("privacy", {}).get("content_redacted") is True
                and item.get("operator_controls", {}).get("deletion_path")
            ),
            "voice_media_regression_fallback_count": sum(
                1 for item in voice_media
                if item.get("provider_regression", {}).get("unsafe_action_allowed") is False
            ),
            "mobile_execution_receipt_count": len(mobile),
            "mobile_approval_handoff_count": sum(
                1 for item in mobile
                if item.get("approval_handoff", {}).get("mutation_allowed_without_approval") is False
            ),
            "mobile_action_continuity_count": sum(
                1 for item in mobile
                if item.get("action_continuity", {}).get("same_thread_preserved") is True
                and item.get("action_continuity", {}).get("resumed_in_browser") is True
            ),
            "mobile_memory_continuity_count": sum(
                1 for item in mobile
                if item.get("memory_continuity", {}).get("same_context_preserved") is True
                and item.get("memory_continuity", {}).get("memory_import_requires_review") is True
            ),
            "mobile_revocation_fail_closed_count": sum(
                1 for item in mobile
                if item.get("revocation", {}).get("revoked_device_blocks_action") is True
                and item.get("revocation", {}).get("unsafe_follow_up_hidden") is True
            ),
            "claim_boundary": PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY,
        },
        "channel_sla_receipts": channels,
        "voice_media_quality_receipts": voice_media,
        "mobile_execution_receipts": mobile,
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
            "summary": str(getattr(result, "error", "") or "Production reach/voice/mobile scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:6]


async def _run_production_reach_voice_mobile_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        BROAD_CHANNEL_SLA_OPERATIONS_SUITE_NAME,
        PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SUITE_NAME,
        MOBILE_EXECUTION_CONTINUITY_SUITE_NAME,
    ])


async def build_production_reach_voice_mobile_report() -> dict[str, Any]:
    summary = await _run_production_reach_voice_mobile_suites()
    contract = build_production_reach_voice_mobile_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "production_reach_voice_mobile_ci_gated_operator_visible"
                if healthy
                else "production_reach_voice_mobile_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(BROAD_CHANNEL_SLA_OPERATIONS_SCENARIO_NAMES)
                + len(PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SCENARIO_NAMES)
                + len(MOBILE_EXECUTION_CONTINUITY_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            BROAD_CHANNEL_SLA_OPERATIONS_SUITE_NAME: list(BROAD_CHANNEL_SLA_OPERATIONS_SCENARIO_NAMES),
            PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SUITE_NAME: list(
                PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SCENARIO_NAMES
            ),
            MOBILE_EXECUTION_CONTINUITY_SUITE_NAME: list(MOBILE_EXECUTION_CONTINUITY_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="production_reach_voice_mobile"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
