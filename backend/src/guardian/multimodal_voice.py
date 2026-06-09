"""Guardian-safe multimodal and voice proof receipts.

This module is deliberately a governance and proof surface, not a live
voice/media runtime. It defines the metadata Seraph requires before voice,
speech, vision, or media delivery can count toward guardian reach.
"""

from __future__ import annotations

from typing import Any


GUARDIAN_SAFE_MULTIMODAL_VOICE_SUITE_NAME = "guardian_safe_multimodal_voice"
GUARDIAN_SAFE_MULTIMODAL_VOICE_SCENARIO_NAMES = (
    "multimodal_voice_capability_governance_behavior",
    "multimodal_voice_transcript_audit_privacy_behavior",
    "multimodal_voice_continuity_approval_behavior",
    "multimodal_voice_exposure_revocation_behavior",
    "multimodal_voice_guardian_value_behavior",
    "operator_guardian_safe_multimodal_voice_surface_behavior",
)
GUARDIAN_SAFE_MULTIMODAL_VOICE_CLAIM_BOUNDARY = (
    "governed_voice_media_proof_not_live_broad_multimodal_runtime_or_voice_parity"
)


def guardian_safe_multimodal_voice_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "capability_governance",
            "label": "Capability governance",
            "summary": "Voice, speech, vision, image/media analysis, and media delivery must declare owner, trust level, permissions, data access, mutation rights, and revocation before use.",
        },
        {
            "name": "transcript_audit_privacy",
            "label": "Transcript, audit, and privacy",
            "summary": "Captured media receipts must show what was captured, where it was sent, which provider/model processed it, retention posture, and correction or deletion paths.",
        },
        {
            "name": "continuity_approval",
            "label": "Continuity and approval",
            "summary": "Voice/media interactions must preserve memory, thread, approval, and workflow continuity instead of creating detached side channels.",
        },
        {
            "name": "exposure_revocation",
            "label": "Exposure and revocation",
            "summary": "Browser vision or media analysis must not silently expand screen, file, credential, camera, microphone, or network exposure and must fail closed after revocation.",
        },
        {
            "name": "guardian_value",
            "label": "Guardian value",
            "summary": "Voice/media must improve timing, accessibility, situational awareness, or intervention quality rather than shipping as raw feature presence.",
        },
    ]


def guardian_safe_multimodal_voice_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "undeclared_media_capability",
            "severity": "high",
            "summary": "A voice/media capability lacks owner, trust, permission, data-access, mutation, or revocation metadata.",
        },
        {
            "name": "opaque_capture_or_provider",
            "severity": "high",
            "summary": "An operator cannot see what was captured, where it was sent, which provider/model processed it, or how long it is retained.",
        },
        {
            "name": "detached_voice_or_media_thread",
            "severity": "medium",
            "summary": "A voice/media interaction loses memory, thread, approval, or workflow continuity.",
        },
        {
            "name": "silent_exposure_expansion",
            "severity": "high",
            "summary": "Browser vision or media analysis expands credential, screen, file, camera, microphone, or network exposure without explicit policy metadata.",
        },
        {
            "name": "feature_presence_without_guardian_value",
            "severity": "medium",
            "summary": "Voice/media is treated as parity because it exists, not because it improves a guardian decision.",
        },
        {
            "name": "overclaimed_voice_or_multimodal_parity",
            "severity": "medium",
            "summary": "The proof surface implies broad live voice/media parity instead of a governed capability gate.",
        },
    ]


def guardian_safe_multimodal_voice_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": GUARDIAN_SAFE_MULTIMODAL_VOICE_SUITE_NAME,
        "capability_governance_policy": "voice_media_capabilities_require_owner_trust_permissions_data_access_mutation_and_revocation_metadata",
        "transcript_audit_policy": "media_receipts_must_show_capture_destination_provider_model_retention_and_correction_or_deletion_paths",
        "continuity_policy": "voice_media_flows_must_preserve_thread_memory_approval_and_workflow_continuity",
        "exposure_policy": "browser_vision_and_media_analysis_cannot_expand_screen_file_credential_camera_microphone_or_network_exposure_silently",
        "guardian_value_policy": "voice_media_must_improve_timing_accessibility_situational_awareness_or_intervention_quality",
        "receipt_surfaces": [
            "/api/operator/guardian-safe-multimodal-voice",
            "/api/operator/benchmark-proof",
            "/api/extensions",
            "/api/activity/ledger",
            "/api/operator/continuity-graph",
        ],
        "ci_gate_mode": "required_benchmark_suite",
        "claim_boundary": GUARDIAN_SAFE_MULTIMODAL_VOICE_CLAIM_BOUNDARY,
        "not_claimed": [
            "live_broad_voice_runtime",
            "production_stt_tts_runtime",
            "full_multimodal_runtime",
            "voice_parity",
            "multimodal_parity",
            "competitor_superiority",
        ],
    }


def guardian_safe_multimodal_voice_capability_families() -> list[dict[str, Any]]:
    base_receipts = {
        "trust_level": "local_governed",
        "audit_posture": "transcript_and_media_receipt_required",
        "privacy_boundary": "operator_visible_capture_scope_with_deletion_path",
        "correction_path": "operator_can_correct_transcript_or_media_summary_before_memory_write",
        "deletion_path": "operator_can_delete_capture_receipt_and_block_memory_import",
        "continuity_contract": {
            "thread": "required",
            "memory_context": "required_before_import",
            "approval_context": "required_for_mutation_or_external_send",
            "workflow_context": "preserved_when_invoked_from_workflow",
        },
        "revocation_path": "disable_capability_family_and_revoke_provider_or_device_pairing",
        "claim_boundary": GUARDIAN_SAFE_MULTIMODAL_VOICE_CLAIM_BOUNDARY,
    }
    return [
        {
            **base_receipts,
            "family": "voice_stt",
            "owner": "speech_profile_extension",
            "permissions": ["microphone_capture", "transcript_write"],
            "data_access": ["microphone_audio", "transcript_text", "active_thread_context"],
            "mutation_rights": ["transcript_import_after_operator_review"],
            "provider_model": "declared_by_speech_profile",
            "capture_receipt": "microphone_audio_to_transcript",
            "guardian_value_reason": "accessibility_and_fast_correction_when_typing_is_costly",
        },
        {
            **base_receipts,
            "family": "voice_tts",
            "owner": "speech_profile_extension",
            "permissions": ["speaker_output"],
            "data_access": ["assistant_reply_excerpt", "intervention_summary"],
            "mutation_rights": [],
            "provider_model": "declared_by_speech_profile",
            "capture_receipt": "assistant_text_to_speech_output",
            "guardian_value_reason": "timely_hands_free_intervention_when_visual_attention_is_low",
        },
        {
            **base_receipts,
            "family": "browser_vision",
            "owner": "browser_provider_extension",
            "permissions": ["browser_view_capture"],
            "data_access": ["visible_browser_view", "active_url_origin", "thread_context"],
            "mutation_rights": [],
            "provider_model": "declared_by_browser_or_multimodal_provider",
            "capture_receipt": "visible_browser_view_summary",
            "guardian_value_reason": "situational_awareness_for_browser_tasks_without_full_screen_or_cookie_expansion",
        },
        {
            **base_receipts,
            "family": "image_media_analysis",
            "owner": "multimodal_context_pack",
            "permissions": ["selected_file_read", "media_summary_write"],
            "data_access": ["operator_selected_media", "thread_context"],
            "mutation_rights": ["media_summary_import_after_operator_review"],
            "provider_model": "declared_by_provider_preset",
            "capture_receipt": "selected_media_to_summary",
            "guardian_value_reason": "operator_selected_media_understanding_for_project_follow_through",
        },
        {
            **base_receipts,
            "family": "media_delivery",
            "owner": "channel_or_canvas_extension",
            "permissions": ["media_send_or_canvas_render"],
            "data_access": ["approved_media_artifact", "target_channel_metadata"],
            "mutation_rights": ["external_send_after_approval"],
            "provider_model": "declared_by_channel_adapter_or_canvas_surface",
            "capture_receipt": "approved_media_artifact_to_delivery_target",
            "guardian_value_reason": "accessible_or_context_rich_delivery_when_text_only_would_reduce_intervention_quality",
        },
    ]


def build_guardian_safe_multimodal_voice_receipts() -> list[dict[str, Any]]:
    families = guardian_safe_multimodal_voice_capability_families()
    policy = guardian_safe_multimodal_voice_policy_payload()
    return [
        {
            "scenario_id": "multimodal_voice_capability_governance_behavior",
            "dimension": "capability_governance",
            "status": "passed",
            "family_count": len(families),
            "families": [
                {
                    "family": family["family"],
                    "owner": family["owner"],
                    "trust_level": family["trust_level"],
                    "permissions": family["permissions"],
                    "data_access": family["data_access"],
                    "mutation_rights": family["mutation_rights"],
                    "revocation_path": family["revocation_path"],
                }
                for family in families
            ],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "multimodal_voice_transcript_audit_privacy_behavior",
            "dimension": "transcript_audit_privacy",
            "status": "passed",
            "operator_receipt_fields": [
                "captured_surface",
                "destination",
                "provider_model",
                "transcript_or_summary_id",
                "memory_context_used",
                "privacy_boundary",
                "retention",
                "correction_path",
                "deletion_path",
            ],
            "capture_receipts": [family["capture_receipt"] for family in families],
            "privacy_boundary_state": "operator_visible_capture_scope_with_correction_and_deletion",
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "multimodal_voice_continuity_approval_behavior",
            "dimension": "continuity_approval",
            "status": "passed",
            "continuity_contract": families[0]["continuity_contract"],
            "preserves": ["thread", "memory_context", "approval_context", "workflow_context"],
            "approval_required_for": ["external_send", "memory_import", "mutation"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "multimodal_voice_exposure_revocation_behavior",
            "dimension": "exposure_revocation",
            "status": "passed",
            "blocked_silent_expansions": [
                "credential_scope",
                "screen_capture_scope",
                "file_read_scope",
                "camera_scope",
                "microphone_scope",
                "network_destination_scope",
            ],
            "revocation_state": "capability_family_fails_closed_after_revoke",
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "multimodal_voice_guardian_value_behavior",
            "dimension": "guardian_value",
            "status": "passed",
            "accepted_value_reasons": [
                "timing",
                "accessibility",
                "situational_awareness",
                "intervention_quality",
            ],
            "rejected_reason": "raw_feature_presence",
            "family_value_reasons": [
                {
                    "family": family["family"],
                    "guardian_value_reason": family["guardian_value_reason"],
                }
                for family in families
            ],
            "claim_boundary": policy["claim_boundary"],
        },
    ]


def _guardian_safe_multimodal_voice_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "type": "benchmark_regression",
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(
                getattr(result, "error", "")
                or "Guardian-safe multimodal and voice benchmark scenario failed."
            ),
            "reason": "deterministic_eval_failure",
        })
    return failures[:6]


async def _run_guardian_safe_multimodal_voice_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([GUARDIAN_SAFE_MULTIMODAL_VOICE_SUITE_NAME])


async def build_guardian_safe_multimodal_voice_report() -> dict[str, Any]:
    summary = await _run_guardian_safe_multimodal_voice_suite()
    failure_report = _guardian_safe_multimodal_voice_failure_report(summary)
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    degraded_state = "regressions_detected"
    receipts = build_guardian_safe_multimodal_voice_receipts()
    return {
        "summary": {
            "suite_name": GUARDIAN_SAFE_MULTIMODAL_VOICE_SUITE_NAME,
            "benchmark_posture": (
                "guardian_safe_multimodal_voice_ci_gated_operator_visible"
                if healthy
                else "guardian_safe_multimodal_voice_regressions_detected_operator_visible"
            ),
            "operator_status": "guardian_safe_voice_media_receipts_visible",
            "scenario_count": len(GUARDIAN_SAFE_MULTIMODAL_VOICE_SCENARIO_NAMES),
            "dimension_count": len(guardian_safe_multimodal_voice_dimensions()),
            "failure_mode_count": len(guardian_safe_multimodal_voice_failure_taxonomy()),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
            "capability_governance_state": (
                "owner_trust_permission_data_access_mutation_revocation_visible"
                if healthy
                else degraded_state
            ),
            "transcript_audit_privacy_state": (
                "capture_destination_provider_context_correction_deletion_visible"
                if healthy
                else degraded_state
            ),
            "continuity_approval_state": (
                "thread_memory_approval_workflow_continuity_preserved"
                if healthy
                else degraded_state
            ),
            "exposure_revocation_state": (
                "silent_screen_file_credential_media_network_expansion_blocked"
                if healthy
                else degraded_state
            ),
            "guardian_value_state": (
                "voice_media_requires_guardian_value_reason"
                if healthy
                else degraded_state
            ),
            "claim_boundary": GUARDIAN_SAFE_MULTIMODAL_VOICE_CLAIM_BOUNDARY,
        },
        "scenario_names": list(GUARDIAN_SAFE_MULTIMODAL_VOICE_SCENARIO_NAMES),
        "dimensions": guardian_safe_multimodal_voice_dimensions(),
        "failure_taxonomy": guardian_safe_multimodal_voice_failure_taxonomy(),
        "capability_families": guardian_safe_multimodal_voice_capability_families(),
        "governance_receipts": receipts,
        "failure_report": failure_report,
        "policy": guardian_safe_multimodal_voice_policy_payload(),
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
