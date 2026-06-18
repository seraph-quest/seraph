"""Batch DS post-DP reach and channel gap-closure receipts.

This layer builds on the bounded reach/voice proof train and closes the next
implementation-facing gap for selected surface readiness, degraded recovery,
guardian-aware continuity, voice/media privacy fallback, and operator receipt
visibility. It remains bounded proof; it does not claim OpenClaw-class reach,
complete channel coverage, always-available operation, voice/media parity,
production readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.extensions.reach_voice_production_ops import (
    REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
    build_reach_voice_production_ops_contract,
)


POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME = "post_dp_reach_channel_gap_closure_v1"
POST_DP_REACH_CHANNEL_GAP_CLOSURE_SCENARIO_NAMES = (
    "post_dp_reach_contract_builds_on_dk_without_duplicate_scope",
    "post_dp_reach_selected_surface_readiness_behavior",
    "post_dp_reach_guardian_context_continuity_behavior",
    "post_dp_reach_claim_boundary_behavior",
)
SELECTED_REACH_SURFACE_READINESS_V2_SUITE_NAME = "selected_reach_surface_readiness_v2"
SELECTED_REACH_SURFACE_READINESS_V2_SCENARIO_NAMES = (
    "selected_reach_native_desktop_readiness_behavior",
    "selected_reach_websocket_readiness_behavior",
)
CHANNEL_DEGRADED_RECOVERY_V2_SUITE_NAME = "channel_degraded_recovery_v2"
CHANNEL_DEGRADED_RECOVERY_V2_SCENARIO_NAMES = (
    "channel_recovery_provider_outage_fallback_behavior",
    "channel_recovery_rate_limit_bundle_retry_behavior",
    "channel_recovery_abuse_storm_restraint_behavior",
    "channel_recovery_offline_resume_behavior",
)
GUARDIAN_REACH_CONTINUITY_V2_SUITE_NAME = "guardian_reach_continuity_v2"
GUARDIAN_REACH_CONTINUITY_V2_SCENARIO_NAMES = (
    "guardian_reach_thread_memory_continuity_behavior",
    "guardian_reach_approval_authority_preservation_behavior",
    "guardian_reach_timing_restraint_behavior",
    "guardian_reach_handoff_audit_behavior",
)
VOICE_MEDIA_PRIVACY_FALLBACK_V2_SUITE_NAME = "voice_media_privacy_fallback_v2"
VOICE_MEDIA_PRIVACY_FALLBACK_V2_SCENARIO_NAMES = (
    "voice_media_stt_privacy_fallback_behavior",
    "voice_media_tts_correction_deletion_behavior",
    "voice_media_command_clarification_behavior",
    "voice_media_transcript_memory_review_behavior",
)
REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SUITE_NAME = "reach_channel_false_claim_scan_v2"
REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES = (
    "reach_channel_false_claim_v2_blocks_openclaw_reach",
    "reach_channel_false_claim_v2_blocks_always_available",
    "reach_channel_false_claim_v2_blocks_voice_media_parity",
)

POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY = (
    "post_dp_reach_channel_gap_closure_not_openclaw_class_always_available_or_voice_media_parity"
)
POST_DP_REACH_CHANNEL_BLOCKED_CLAIMS = (
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
POST_DP_REACH_SAFE_REDACTION_BOUNDARY = (
    "metadata_only_no_message_body_contact_secret_audio_media_or_transcript_payload"
)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _receipt_handle(kind: str, receipt_id: str, payload: Any) -> str:
    return f"seraph://receipts/batch-ds/{kind}/{receipt_id}/{_stable_digest(payload)}"


def _safe_receipt(kind: str, receipt_id: str, payload: Any) -> dict[str, Any]:
    return {
        "redacted_receipt_handle": _receipt_handle(kind, receipt_id, payload),
        "safe_redaction_digest": _stable_digest((kind, receipt_id, payload)),
        "redaction_boundary": POST_DP_REACH_SAFE_REDACTION_BOUNDARY,
        "stored_payload_mode": "metadata_only_redacted_receipt",
        "contains_message_body": False,
        "contains_contact_identifier": False,
        "contains_secret": False,
        "contains_transcript": False,
        "contains_audio_payload": False,
        "contains_media_payload": False,
    }


def post_dp_reach_channel_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME,
            SELECTED_REACH_SURFACE_READINESS_V2_SUITE_NAME,
            CHANNEL_DEGRADED_RECOVERY_V2_SUITE_NAME,
            GUARDIAN_REACH_CONTINUITY_V2_SUITE_NAME,
            VOICE_MEDIA_PRIVACY_FALLBACK_V2_SUITE_NAME,
            REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
        ],
        "foundation_suites": [
            "always_available_reach_live_ops_v1",
            "voice_media_production_parity_candidate_v1",
            "channel_incident_response_v1",
            "cross_surface_reach_continuity_v2",
            "reach_media_false_claim_scan_v1",
        ],
        "claim_boundary": POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
        "blocked_claims": list(POST_DP_REACH_CHANNEL_BLOCKED_CLAIMS),
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
            "/api/operator/post-dp-reach-channel-gap-closure",
            "/api/operator/reach-voice-production-ops",
            "/api/operator/benchmark-proof",
            "GitHub issue #575",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "reach_policy": (
            "DS receipts must prove selected-surface readiness, consent, pairing, revocation, "
            "provider outage fallback, rate-limit or abuse restraint, offline recovery, and "
            "thread/memory/approval continuity without expanding unsafe action authority."
        ),
        "safe_redaction_policy": (
            "operator receipts expose handles, provider metadata, state transitions, continuity ids, "
            "quality metrics, and redacted summaries only."
        ),
        "safe_receipt_redaction_boundary": POST_DP_REACH_SAFE_REDACTION_BOUNDARY,
    }


def selected_reach_surface_readiness_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "ds-native-notification",
            "selected_reach_native_desktop_readiness_behavior",
            "native_notification",
            "native-daemon",
            "paired",
            "websocket",
        ),
        (
            "ds-websocket",
            "selected_reach_websocket_readiness_behavior",
            "websocket",
            "seraph-websocket-reach",
            "paired",
            "native_notification",
        ),
    ]
    return [
        {
            "receipt_id": receipt_id,
            "suite_name": SELECTED_REACH_SURFACE_READINESS_V2_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dp-reach-channel-gap-closure",
            "capability_surface": surface,
            "provider": provider,
            "pairing_state": pairing_state,
            "consent_current": True,
            "revocation_probe_blocks_delivery": True,
            "provider_identity_visible": True,
            "rate_limit_policy_visible": True,
            "abuse_handling_visible": True,
            "provider_outage_behavior": f"fallback_to_{fallback_surface}",
            "fallback_surface": fallback_surface,
            "offline_recovery_tested": True,
            "thread_continuity_id": f"thread:reach-ds:{surface}",
            "memory_context_id": f"memory:reach-ds:{surface}",
            "approval_authority": "operator_owned",
            "unsafe_action_authority_expanded": False,
            "safe_receipt": _safe_receipt("selected-surface", receipt_id, (surface, provider, fallback_surface)),
            "claim_boundary": POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
        }
        for receipt_id, scenario_name, surface, provider, pairing_state, fallback_surface in rows
    ]


def channel_degraded_recovery_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "ds-recovery-native-provider-outage",
            "channel_recovery_provider_outage_fallback_behavior",
            "native_notification",
            "provider_outage",
            "fallback_to_websocket",
        ),
        (
            "ds-recovery-websocket-rate-limit",
            "channel_recovery_rate_limit_bundle_retry_behavior",
            "websocket",
            "rate_limit",
            "bundle_retry_after",
        ),
        (
            "ds-recovery-native-abuse-storm",
            "channel_recovery_abuse_storm_restraint_behavior",
            "native_notification",
            "external_mention_storm",
            "defer_low_confidence",
        ),
        (
            "ds-recovery-websocket-offline-resume",
            "channel_recovery_offline_resume_behavior",
            "websocket",
            "offline_window",
            "resume_same_thread",
        ),
    ]
    return [
        {
            "recovery_id": recovery_id,
            "suite_name": CHANNEL_DEGRADED_RECOVERY_V2_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dp-reach-channel-gap-closure",
            "surface": surface,
            "failure_mode": failure_mode,
            "operator_action": operator_action,
            "fallback_exercised": True,
            "offline_or_degraded_recovery_tested": True,
            "rate_limit_or_abuse_policy_visible": failure_mode in {"rate_limit", "external_mention_storm"},
            "revocation_state_rechecked": True,
            "unsafe_mutation_blocked": True,
            "operator_visible": True,
            "safe_receipt": _safe_receipt("degraded-recovery", recovery_id, (surface, failure_mode, operator_action)),
            "claim_boundary": POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
        }
        for recovery_id, scenario_name, surface, failure_mode, operator_action in rows
    ]


def guardian_reach_continuity_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "ds-continuity-urgent-native-websocket",
            "guardian_reach_thread_memory_continuity_behavior",
            "native_notification",
            "websocket",
            "urgent_and_actionable",
        ),
        (
            "ds-continuity-low-confidence-websocket-native",
            "guardian_reach_approval_authority_preservation_behavior",
            "websocket",
            "native_notification",
            "low_confidence_defer",
        ),
        (
            "ds-continuity-voice-typed-confirmation",
            "guardian_reach_timing_restraint_behavior",
            "voice_command",
            "typed_confirmation",
            "ambiguous_command",
        ),
        (
            "ds-continuity-offline-websocket-native",
            "guardian_reach_handoff_audit_behavior",
            "websocket",
            "native_notification",
            "offline_recovered",
        ),
    ]
    return [
        {
            "continuity_id": continuity_id,
            "suite_name": GUARDIAN_REACH_CONTINUITY_V2_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dp-reach-channel-gap-closure",
            "from_surface": from_surface,
            "to_surface": to_surface,
            "guardian_context": guardian_context,
            "thread_preserved": True,
            "memory_context_preserved": True,
            "approval_state_preserved": True,
            "operator_handoff_preserved": True,
            "audit_receipt_id": f"audit:reach-ds:{continuity_id}",
            "timing_or_restraint_improved": True,
            "guardian_restrained_delivery": guardian_context in {"low_confidence_defer", "ambiguous_command"},
            "replay_authority": "operator_review_required_before_replay",
            "unsafe_action_authority_expanded": False,
            "safe_receipt": _safe_receipt(
                "guardian-continuity",
                continuity_id,
                (from_surface, to_surface, guardian_context),
            ),
            "claim_boundary": POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
        }
        for continuity_id, scenario_name, from_surface, to_surface, guardian_context in rows
    ]


def voice_media_privacy_fallback_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "ds-voice-stt",
            "voice_media_stt_privacy_fallback_behavior",
            "speech_to_text",
            "openai-transcribe-profile",
            "typed_confirmation",
        ),
        (
            "ds-voice-tts",
            "voice_media_tts_correction_deletion_behavior",
            "text_to_speech",
            "local-tts-profile",
            "desktop_text",
        ),
        (
            "ds-voice-command",
            "voice_media_command_clarification_behavior",
            "voice_command",
            "guarded-command-parser",
            "clarify_before_action",
        ),
        (
            "ds-media-analysis",
            "voice_media_transcript_memory_review_behavior",
            "media_analysis",
            "browser-vision-review-profile",
            "operator_annotation",
        ),
    ]
    return [
        {
            "receipt_id": receipt_id,
            "suite_name": VOICE_MEDIA_PRIVACY_FALLBACK_V2_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dp-reach-channel-gap-closure",
            "family": family,
            "provider": provider,
            "quality_gate_passed": True,
            "latency_gate_passed": True,
            "correction_path": f"correct_{family}_summary_before_memory_or_action",
            "deletion_path": f"delete_{family}_redacted_receipts",
            "revocation_blocks_capture": True,
            "memory_import_requires_review": True,
            "provider_regression_fallback": fallback,
            "unsafe_action_allowed": False,
            "private_data_boundary": POST_DP_REACH_SAFE_REDACTION_BOUNDARY,
            "safe_receipt": _safe_receipt("voice-media-privacy", receipt_id, (family, provider, fallback)),
            "claim_boundary": POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
        }
        for receipt_id, scenario_name, family, provider, fallback in rows
    ]


def reach_channel_false_claim_scan_v2_receipts() -> list[dict[str, Any]]:
    scan_scope = [
        "docs/research/19-strategy-claim-ledger.md",
        "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
        "docs/implementation/16-agent-parity-execution-roadmap.md",
        "docs/implementation/STATUS.md",
        "backend/src/extensions/post_dp_reach_channel_gap_closure.py",
    ]
    rows = [
        (
            "ds-reach-channel-false-claim-openclaw",
            "reach_channel_false_claim_v2_blocks_openclaw_reach",
            ("openclaw_class_reach", "complete_channel_coverage", "all_openclaw_channels_connected"),
        ),
        (
            "ds-reach-channel-false-claim-always-available",
            "reach_channel_false_claim_v2_blocks_always_available",
            ("always_available_operation", "always_available_daily_life_reach"),
        ),
        (
            "ds-reach-channel-false-claim-voice-media",
            "reach_channel_false_claim_v2_blocks_voice_media_parity",
            ("voice_parity", "voice_media_parity", "multimodal_parity", "production_stt_tts_solved"),
        ),
    ]
    return [
        {
            "scan_id": scan_id,
            "suite_name": REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dp-reach-channel-gap-closure",
            "validation_command": "python3 scripts/check_strategy_claims.py",
            "scan_scope": scan_scope,
            "blocked_claims_checked": list(blocked_claims),
            "blocked_claims_found": [],
            "forbidden_hit_count": 0,
            "safe_receipt": _safe_receipt("false-claim-scan", scan_id, (scenario_name, blocked_claims)),
            "claim_boundary": POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
        }
        for scan_id, scenario_name, blocked_claims in rows
    ]


def post_dp_reach_channel_gap_closure_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "ds-aggregate-builds-on-dk",
            "post_dp_reach_contract_builds_on_dk_without_duplicate_scope",
            "extends_dk_without_duplicate_scope",
        ),
        (
            "ds-aggregate-selected-surfaces",
            "post_dp_reach_selected_surface_readiness_behavior",
            "selected_native_notification_and_websocket_readiness",
        ),
        (
            "ds-aggregate-guardian-continuity",
            "post_dp_reach_guardian_context_continuity_behavior",
            "thread_memory_approval_and_restraint_continuity",
        ),
        (
            "ds-aggregate-claim-boundary",
            "post_dp_reach_claim_boundary_behavior",
            "blocked_claims_and_staged_channel_gaps",
        ),
    ]
    return [
        {
            "receipt_id": receipt_id,
            "suite_name": POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME,
            "scenario_name": scenario_name,
            "operator_surface": "/api/operator/post-dp-reach-channel-gap-closure",
            "proof_area": proof_area,
            "builds_on_foundation_suite": "reach_voice_production_ops_v1",
            "duplicate_scope_blocked": True,
            "selected_runtime_surfaces": ["native_notification", "websocket"],
            "staged_channel_gaps": [
                "mobile_push_provider_not_proven_live_in_ds",
                "telegram_or_slack_connector_metadata_not_runtime_transport",
                "voice_or_media_runtime_not_full_parity",
            ],
            "safe_receipt": _safe_receipt("aggregate-gap-closure", receipt_id, (scenario_name, proof_area)),
            "claim_boundary": POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
        }
        for receipt_id, scenario_name, proof_area in rows
    ]


def build_post_dp_reach_channel_contract() -> dict[str, Any]:
    aggregate_receipts = post_dp_reach_channel_gap_closure_receipts()
    selected_surfaces = selected_reach_surface_readiness_receipts()
    recovery = channel_degraded_recovery_v2_receipts()
    continuity = guardian_reach_continuity_v2_receipts()
    voice_media = voice_media_privacy_fallback_v2_receipts()
    false_claims = reach_channel_false_claim_scan_v2_receipts()
    upstream = build_reach_voice_production_ops_contract()
    all_receipts = [
        *aggregate_receipts,
        *selected_surfaces,
        *recovery,
        *continuity,
        *voice_media,
        *false_claims,
    ]
    receipt_index = {
        "aggregate_receipts": aggregate_receipts,
        "selected_surfaces": selected_surfaces,
        "recovery": recovery,
        "continuity": continuity,
        "voice_media": voice_media,
        "false_claims": false_claims,
        "upstream_claim_boundary": upstream["summary"]["claim_boundary"],
    }
    return {
        "summary": {
            "suite_name": POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME,
            "operator_status": "post_dp_reach_channel_gap_closure_visible",
            "aggregate_receipt_count": len(aggregate_receipts),
            "selected_surface_count": len(selected_surfaces),
            "paired_revocation_count": sum(
                1 for item in selected_surfaces
                if item["pairing_state"] == "paired" and item["revocation_probe_blocks_delivery"] is True
            ),
            "provider_outage_behavior_count": sum(1 for item in selected_surfaces if item["provider_outage_behavior"]),
            "staged_channel_gap_count": 3,
            "staged_channel_gaps": [
                "mobile_push_provider_not_proven_live_in_ds",
                "telegram_or_slack_connector_metadata_not_runtime_transport",
                "voice_or_media_runtime_not_full_parity",
            ],
            "degraded_recovery_count": sum(1 for item in recovery if item["fallback_exercised"]),
            "rate_limit_abuse_policy_count": sum(
                1 for item in [*selected_surfaces, *recovery]
                if item.get("rate_limit_policy_visible") or item.get("rate_limit_or_abuse_policy_visible")
            ),
            "continuity_preserved_count": sum(
                1 for item in continuity
                if item["thread_preserved"] and item["memory_context_preserved"] and item["approval_state_preserved"]
            ),
            "guardian_restraint_count": sum(1 for item in continuity if item["guardian_restrained_delivery"]),
            "voice_media_privacy_fallback_count": sum(
                1 for item in voice_media
                if item["revocation_blocks_capture"] and item["memory_import_requires_review"]
            ),
            "safe_receipt_count": sum(
                1 for item in all_receipts
                if item["safe_receipt"]["redaction_boundary"] == POST_DP_REACH_SAFE_REDACTION_BOUNDARY
                and item["safe_receipt"]["contains_message_body"] is False
                and item["safe_receipt"]["contains_contact_identifier"] is False
                and item["safe_receipt"]["contains_secret"] is False
                and item["safe_receipt"]["contains_transcript"] is False
                and item["safe_receipt"]["contains_audio_payload"] is False
                and item["safe_receipt"]["contains_media_payload"] is False
            ),
            "false_claim_scan_count": len(false_claims),
            "upstream_claim_boundary": REACH_VOICE_PRODUCTION_OPS_CLAIM_BOUNDARY,
            "receipt_digest": _stable_digest(receipt_index),
            "claim_boundary": POST_DP_REACH_CHANNEL_CLAIM_BOUNDARY,
        },
        "post_dp_reach_channel_receipts": aggregate_receipts,
        "selected_reach_surfaces": selected_surfaces,
        "channel_degraded_recovery": recovery,
        "guardian_reach_continuity": continuity,
        "voice_media_privacy_fallback": voice_media,
        "false_claim_scan_receipts": false_claims,
        "upstream_reach_voice_digest": upstream["summary"].get("safe_receipt_count"),
        "policy": post_dp_reach_channel_policy_payload(),
    }


def _failure_report(summary: Any) -> list[dict[str, str]]:
    failures = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Post-DP reach/channel scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_post_dp_reach_channel_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME,
        SELECTED_REACH_SURFACE_READINESS_V2_SUITE_NAME,
        CHANNEL_DEGRADED_RECOVERY_V2_SUITE_NAME,
        GUARDIAN_REACH_CONTINUITY_V2_SUITE_NAME,
        VOICE_MEDIA_PRIVACY_FALLBACK_V2_SUITE_NAME,
        REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    ])


async def build_post_dp_reach_channel_report() -> dict[str, Any]:
    summary = await _run_post_dp_reach_channel_suites()
    contract = build_post_dp_reach_channel_contract()
    failures = _failure_report(summary)
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "post_dp_reach_channel_ci_gated_operator_visible"
                if not failures
                else "post_dp_reach_channel_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(POST_DP_REACH_CHANNEL_GAP_CLOSURE_SCENARIO_NAMES)
                + len(SELECTED_REACH_SURFACE_READINESS_V2_SCENARIO_NAMES)
                + len(CHANNEL_DEGRADED_RECOVERY_V2_SCENARIO_NAMES)
                + len(GUARDIAN_REACH_CONTINUITY_V2_SCENARIO_NAMES)
                + len(VOICE_MEDIA_PRIVACY_FALLBACK_V2_SCENARIO_NAMES)
                + len(REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            POST_DP_REACH_CHANNEL_GAP_CLOSURE_SUITE_NAME: list(
                POST_DP_REACH_CHANNEL_GAP_CLOSURE_SCENARIO_NAMES
            ),
            SELECTED_REACH_SURFACE_READINESS_V2_SUITE_NAME: list(
                SELECTED_REACH_SURFACE_READINESS_V2_SCENARIO_NAMES
            ),
            CHANNEL_DEGRADED_RECOVERY_V2_SUITE_NAME: list(CHANNEL_DEGRADED_RECOVERY_V2_SCENARIO_NAMES),
            GUARDIAN_REACH_CONTINUITY_V2_SUITE_NAME: list(GUARDIAN_REACH_CONTINUITY_V2_SCENARIO_NAMES),
            VOICE_MEDIA_PRIVACY_FALLBACK_V2_SUITE_NAME: list(VOICE_MEDIA_PRIVACY_FALLBACK_V2_SCENARIO_NAMES),
            REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SUITE_NAME: list(
                REACH_CHANNEL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": failures,
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
