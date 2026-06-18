"""Batch BY production reach, browser, and voice/media hardening receipts.

This module composes the earlier one-channel canary, computer-use benchmark,
and guardian-safe voice/media governance floors into a production-oriented
receipt contract. It remains deterministic proof, not a claim of broad live
channel coverage, voice parity, or safe browser automation.
"""

from __future__ import annotations

from typing import Any


PRODUCTION_REACH_CHANNEL_HARDENING_SUITE_NAME = "production_reach_channel_hardening"
PRODUCTION_REACH_CHANNEL_HARDENING_SCENARIO_NAMES = (
    "production_reach_external_messaging_pairing_behavior",
    "production_reach_channel_failure_recovery_behavior",
    "production_reach_privacy_redaction_behavior",
    "operator_production_reach_channel_surface_behavior",
)
BROWSER_COMPUTER_USE_RELIABILITY_V2_SUITE_NAME = "browser_computer_use_reliability_v2"
BROWSER_COMPUTER_USE_RELIABILITY_V2_SCENARIO_NAMES = (
    "browser_reliability_provider_truth_behavior",
    "browser_reliability_session_partition_behavior",
    "browser_reliability_crash_recovery_behavior",
    "browser_reliability_page_drift_replay_behavior",
    "operator_browser_reliability_v2_surface_behavior",
)
GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SUITE_NAME = "guardian_safe_voice_media_runtime"
GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SCENARIO_NAMES = (
    "voice_media_runtime_guardian_value_behavior",
    "voice_media_runtime_privacy_transcript_behavior",
    "voice_media_runtime_correction_deletion_behavior",
    "voice_media_runtime_revocation_fail_closed_behavior",
    "operator_voice_media_runtime_surface_behavior",
)
PRODUCTION_REACH_BROWSER_VOICE_CLAIM_BOUNDARY = (
    "production_reach_browser_voice_receipts_not_broad_reach_voice_or_browser_parity"
)
PRODUCTION_REACH_BROWSER_VOICE_BLOCKED_CLAIMS = (
    "broad_reach",
    "complete_channel_coverage",
    "openclaw_class_reach",
    "voice_parity",
    "multimodal_parity",
    "safe_browser_automation",
    "always_available_operation",
    "full_browser_parity",
    "full_production_parity",
    "reference_systems_exceeded",
)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def production_reach_browser_voice_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            PRODUCTION_REACH_CHANNEL_HARDENING_SUITE_NAME,
            BROWSER_COMPUTER_USE_RELIABILITY_V2_SUITE_NAME,
            GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SUITE_NAME,
        ],
        "claim_boundary": PRODUCTION_REACH_BROWSER_VOICE_CLAIM_BOUNDARY,
        "channel_policy": (
            "external messaging channels require pairing revocation identity binding "
            "thread continuity approval handoff privacy redaction audit and degraded recovery"
        ),
        "browser_policy": (
            "browser and computer-use providers must disclose local managed or remote mode "
            "and preserve session partitions crash recovery timelines and page-drift replay receipts"
        ),
        "voice_media_policy": (
            "voice media and browser-vision runtimes require guardian-value reasons capture privacy "
            "transcript audit correction deletion and revocation receipts before use"
        ),
        "receipt_surfaces": [
            "/api/operator/production-reach-browser-voice",
            "/api/operator/benchmark-proof",
            "/api/operator/one-reach-channel-canary",
            "/api/operator/computer-use-benchmark",
            "/api/operator/guardian-safe-multimodal-voice",
        ],
        "blocked_claims": list(PRODUCTION_REACH_BROWSER_VOICE_BLOCKED_CLAIMS),
        "not_claimed": [
            "live_slack_discord_telegram_delivery_at_scale",
            "production_mobile_or_sms_reach",
            "safe_browser_automation_without_site_policy_and_session_limits",
            "production_stt_tts_runtime",
            "full_multimodal_runtime",
            "reference_system_reach_superiority",
        ],
    }


def production_reach_channel_receipts() -> list[dict[str, Any]]:
    return [
        {
            "channel_id": "seraph.messaging.telegram-operator-relay",
            "transport": "telegram",
            "surface_kind": "external_messaging",
            "status": "paired",
            "identity_binding": {
                "external_user_hash": "sha256:telegram-operator-001",
                "channel_thread_key": "telegram://operator-relay/thread-001",
                "seraph_session_id": "session-reach-by-001",
                "binding_state": "bound",
            },
            "pairing": {
                "pairing_id": "pair-telegram-operator-001",
                "state": "paired",
                "trust_state": "trusted",
                "scopes": ["inbound_thread", "outbound_reply_draft", "approval_handoff"],
            },
            "revocation_probe": {
                "pairing_id": "pair-telegram-operator-old",
                "state": "revoked",
                "safe_follow_up_hidden": True,
                "revocation_reason": "operator_revoked_old_external_pairing",
                "audit_receipt_id": "audit:reach-by:telegram-revoked",
            },
            "threading": {
                "inbound_message_id": "tg-inbound-001",
                "outbound_draft_id": "tg-outbound-draft-001",
                "same_thread_reply": True,
                "memory_context_id": "memctx-reach-by-001",
            },
            "approval_handoff": {
                "approval_id": "approval-reach-by-telegram-reply",
                "status": "pending_operator_approval",
                "mutation_boundary": "draft_only_until_approved",
            },
            "privacy": {
                "content_redacted": True,
                "payload_hash": "sha256:telegram-redacted-payload-001",
                "secret_payload_present": False,
            },
            "audit_receipts": [
                "audit:reach-by:telegram-paired",
                "audit:reach-by:telegram-inbound",
                "audit:reach-by:telegram-approval-handoff",
                "audit:reach-by:telegram-retry",
            ],
            "degraded_recovery": {
                "status": "rate_limited",
                "retry_after_seconds": 90,
                "fallback_transport": "native_notification",
                "operator_visible": True,
                "unsafe_follow_up_hidden": True,
            },
        },
        {
            "channel_id": "seraph.messaging.slack-review-relay",
            "transport": "slack",
            "surface_kind": "external_messaging",
            "status": "requires_config",
            "identity_binding": {
                "workspace_hash": "sha256:slack-workspace-001",
                "binding_state": "unbound_until_pairing",
            },
            "pairing": {"state": "not_paired", "trust_state": "untrusted", "scopes": []},
            "revocation_probe": {
                "state": "not_applicable",
                "safe_follow_up_hidden": True,
            },
            "threading": {"same_thread_reply": False},
            "approval_handoff": {
                "status": "blocked_until_pairing",
                "mutation_boundary": "closed",
            },
            "privacy": {
                "content_redacted": True,
                "payload_hash": "sha256:slack-config-required",
                "secret_payload_present": False,
            },
            "audit_receipts": ["audit:reach-by:slack-config-required"],
            "degraded_recovery": {
                "status": "requires_config",
                "repair_action": "pair_and_bind_slack_connector",
                "operator_visible": True,
                "unsafe_follow_up_hidden": True,
            },
        },
    ]


def browser_reliability_v2_receipts() -> list[dict[str, Any]]:
    return [
        {
            "provider_id": "local-playwright-default",
            "provider_mode": "local",
            "selected": True,
            "claim_truth": "local_browser_replay_not_remote_managed_browser",
            "session_partition": {
                "partition_id": "browser-partition-session-reach-by-001",
                "cookie_jar_isolated": True,
                "credential_scope": "session_bound",
                "cross_channel_cookie_reuse_blocked": True,
            },
            "crash_recovery": {
                "crash_detected": True,
                "recovered_from_checkpoint": True,
                "timeline_events": [
                    "navigate_started",
                    "dom_snapshot_saved",
                    "browser_process_lost",
                    "session_reopened",
                    "operator_replay_ready",
                ],
            },
            "page_drift_replay": {
                "drift_detected": True,
                "drift_reason": "selector_missing_after_reload",
                "replay_action": "refresh_snapshot_and_request_operator_confirmation",
                "external_action_allowed": False,
            },
            "action_timeline": [
                {"action": "navigate", "status": "recorded"},
                {"action": "extract", "status": "recorded"},
                {"action": "screenshot", "status": "recorded"},
            ],
            "degraded_recovery": {
                "status": "recoverable",
                "operator_visible": True,
                "next_action": "resume_from_last_dom_snapshot",
            },
        },
        {
            "provider_id": "browserbase-managed-pack",
            "provider_mode": "managed_remote",
            "selected": False,
            "claim_truth": "managed_remote_available_only_when_connector_health_and_secret_scope_pass",
            "session_partition": {
                "partition_id": "browserbase-partition-declared",
                "cookie_jar_isolated": True,
                "credential_scope": "connector_scoped",
                "cross_channel_cookie_reuse_blocked": True,
            },
            "crash_recovery": {
                "crash_detected": False,
                "recovered_from_checkpoint": False,
                "timeline_events": ["provider_health_checked", "not_selected"],
            },
            "page_drift_replay": {
                "drift_detected": False,
                "external_action_allowed": False,
            },
            "action_timeline": [],
            "degraded_recovery": {
                "status": "requires_connector_health",
                "operator_visible": True,
                "next_action": "verify_managed_browser_connector",
            },
        },
    ]


def voice_media_runtime_receipts() -> list[dict[str, Any]]:
    return [
        {
            "runtime_id": "voice-stt-operator-confirmation",
            "family": "voice_stt",
            "status": "guarded_runtime_receipt_ready",
            "guardian_value_reason": "accessibility_and_fast_correction_when_typing_is_costly",
            "capture": {
                "captured_surface": "microphone",
                "provider_model": "declared_by_speech_profile",
                "transcript_id": "transcript-reach-by-001",
                "content_redacted": True,
                "retention": "operator_review_then_minimize",
            },
            "privacy": {
                "secret_payload_present": False,
                "memory_import_requires_review": True,
                "provider_destination_visible": True,
            },
            "correction_deletion": {
                "correction_path": "operator_corrects_transcript_before_memory_write",
                "deletion_path": "delete_capture_receipt_and_block_memory_import",
            },
            "revocation": {
                "state": "active",
                "fails_closed_after_revoke": True,
            },
        },
        {
            "runtime_id": "browser-vision-task-check",
            "family": "browser_vision",
            "status": "guarded_runtime_receipt_ready",
            "guardian_value_reason": "situational_awareness_for_browser_tasks_without_cookie_expansion",
            "capture": {
                "captured_surface": "visible_browser_view",
                "provider_model": "declared_by_browser_or_multimodal_provider",
                "summary_id": "browser-vision-summary-reach-by-001",
                "content_redacted": True,
                "retention": "operator_review_then_minimize",
            },
            "privacy": {
                "secret_payload_present": False,
                "credential_scope_expanded": False,
                "provider_destination_visible": True,
            },
            "correction_deletion": {
                "correction_path": "operator_corrects_media_summary_before_memory_write",
                "deletion_path": "delete_media_summary_and_block_memory_import",
            },
            "revocation": {
                "state": "revoked_probe_blocks_capture",
                "fails_closed_after_revoke": True,
            },
        },
    ]


def build_production_reach_browser_voice_contract() -> dict[str, Any]:
    channels = production_reach_channel_receipts()
    browsers = browser_reliability_v2_receipts()
    voice_media = voice_media_runtime_receipts()
    paired_channels = [
        item for item in channels
        if item.get("surface_kind") == "external_messaging"
        and item.get("pairing", {}).get("state") == "paired"
    ]
    degraded = [
        *[item.get("degraded_recovery", {}) for item in channels],
        *[item.get("degraded_recovery", {}) for item in browsers],
    ]
    policy = production_reach_browser_voice_policy_payload()
    return {
        "summary": {
            "operator_status": "production_reach_browser_voice_receipts_visible",
            "channel_suite_name": PRODUCTION_REACH_CHANNEL_HARDENING_SUITE_NAME,
            "browser_suite_name": BROWSER_COMPUTER_USE_RELIABILITY_V2_SUITE_NAME,
            "voice_media_suite_name": GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SUITE_NAME,
            "external_messaging_channel_count": len(channels),
            "paired_external_messaging_channel_count": len(paired_channels),
            "revoked_follow_up_hidden_count": sum(
                1 for item in channels
                if item.get("revocation_probe", {}).get("safe_follow_up_hidden") is True
            ),
            "privacy_redaction_count": sum(
                1 for item in channels
                if item.get("privacy", {}).get("content_redacted") is True
                and item.get("privacy", {}).get("secret_payload_present") is False
            ),
            "browser_provider_count": len(browsers),
            "browser_session_partition_count": sum(
                1 for item in browsers
                if item.get("session_partition", {}).get("cookie_jar_isolated") is True
            ),
            "browser_crash_recovery_count": sum(
                1 for item in browsers
                if item.get("crash_recovery", {}).get("recovered_from_checkpoint") is True
            ),
            "browser_page_drift_block_count": sum(
                1 for item in browsers
                if item.get("page_drift_replay", {}).get("drift_detected") is True
                and item.get("page_drift_replay", {}).get("external_action_allowed") is False
            ),
            "voice_media_runtime_count": len(voice_media),
            "voice_media_deletion_path_count": sum(
                1 for item in voice_media
                if bool(item.get("correction_deletion", {}).get("deletion_path"))
            ),
            "voice_media_revocation_fail_closed_count": sum(
                1 for item in voice_media
                if item.get("revocation", {}).get("fails_closed_after_revoke") is True
            ),
            "degraded_recovery_count": sum(1 for item in degraded if item.get("operator_visible") is True),
            "claim_boundary": PRODUCTION_REACH_BROWSER_VOICE_CLAIM_BOUNDARY,
        },
        "channels": channels,
        "browser_reliability": browsers,
        "voice_media_runtimes": voice_media,
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
            "summary": str(getattr(result, "error", "") or "Production reach/browser/voice scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:6]


async def _run_production_reach_browser_voice_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        PRODUCTION_REACH_CHANNEL_HARDENING_SUITE_NAME,
        BROWSER_COMPUTER_USE_RELIABILITY_V2_SUITE_NAME,
        GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SUITE_NAME,
    ])


async def build_production_reach_browser_voice_report() -> dict[str, Any]:
    summary = await _run_production_reach_browser_voice_suites()
    contract = build_production_reach_browser_voice_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "production_reach_browser_voice_ci_gated_operator_visible"
                if healthy
                else "production_reach_browser_voice_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(PRODUCTION_REACH_CHANNEL_HARDENING_SCENARIO_NAMES)
                + len(BROWSER_COMPUTER_USE_RELIABILITY_V2_SCENARIO_NAMES)
                + len(GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            PRODUCTION_REACH_CHANNEL_HARDENING_SUITE_NAME: list(PRODUCTION_REACH_CHANNEL_HARDENING_SCENARIO_NAMES),
            BROWSER_COMPUTER_USE_RELIABILITY_V2_SUITE_NAME: list(BROWSER_COMPUTER_USE_RELIABILITY_V2_SCENARIO_NAMES),
            GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SUITE_NAME: list(GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="production_reach_browser_voice"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
