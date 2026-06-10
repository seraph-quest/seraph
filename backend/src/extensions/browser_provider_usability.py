"""Batch CH browser provider attestation and multi-operator usability proof.

This module extends the earlier browser reliability and operator-control
receipts with managed/remote provider attestation, recorded-live usability
tasks, and recovery drills. It remains bounded proof, not safe browser
automation, full browser parity, best-cockpit, or production-ready evidence.
"""

from __future__ import annotations

from typing import Any


MANAGED_BROWSER_PROVIDER_ATTESTATION_SUITE_NAME = "managed_browser_provider_attestation"
MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES = (
    "managed_browser_provider_identity_evidence_behavior",
    "managed_browser_session_partition_credential_behavior",
    "managed_browser_download_upload_boundary_behavior",
    "managed_browser_provider_degradation_behavior",
    "operator_managed_browser_attestation_surface_behavior",
)
LIVE_MULTI_OPERATOR_USABILITY_STUDY_SUITE_NAME = "live_multi_operator_usability_study"
LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES = (
    "multi_operator_inspect_recover_handoff_behavior",
    "multi_operator_approval_audit_keyboard_behavior",
    "multi_operator_error_rate_ambiguity_behavior",
    "multi_operator_accessibility_reversibility_behavior",
    "operator_multi_operator_usability_surface_behavior",
)
BROWSER_COMPUTER_USE_RECOVERY_DRILL_SUITE_NAME = "browser_computer_use_recovery_drill"
BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES = (
    "browser_recovery_provider_crash_behavior",
    "browser_recovery_page_drift_behavior",
    "browser_recovery_credential_partition_behavior",
    "browser_recovery_download_upload_fail_closed_behavior",
    "operator_browser_recovery_drill_surface_behavior",
)
BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY = (
    "browser_provider_usability_receipts_not_safe_browser_automation_best_cockpit_or_full_browser_parity"
)
BROWSER_PROVIDER_USABILITY_BLOCKED_CLAIMS = (
    "safe_browser_automation",
    "full_browser_parity",
    "best_cockpit",
    "world_class_cockpit",
    "solved_operator_control",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
)


def browser_provider_usability_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            MANAGED_BROWSER_PROVIDER_ATTESTATION_SUITE_NAME,
            LIVE_MULTI_OPERATOR_USABILITY_STUDY_SUITE_NAME,
            BROWSER_COMPUTER_USE_RECOVERY_DRILL_SUITE_NAME,
        ],
        "claim_boundary": BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY,
        "browser_provider_policy": (
            "managed local and remote browser providers must disclose identity evidence mode session "
            "partition credential scope download/upload boundaries degradation and residual risk"
        ),
        "usability_policy": (
            "multi-operator control evidence must expose inspect recover approval handoff audit keyboard "
            "accessibility ambiguity reversibility and error-rate receipts before cockpit quality claims grow"
        ),
        "recovery_policy": (
            "browser/computer-use recovery must fail closed on provider crash page drift credential partition "
            "changes and unreviewed download/upload mutations"
        ),
        "receipt_surfaces": [
            "/api/operator/browser-provider-usability-proof",
            "/api/operator/benchmark-proof",
            "/api/operator/production-reach-browser-voice",
            "/api/operator/production-operator-control-parity",
            "/api/operator/computer-use-benchmark",
        ],
        "blocked_claims": list(BROWSER_PROVIDER_USABILITY_BLOCKED_CLAIMS),
        "not_claimed": [
            "safe_browser_automation",
            "full_browser_parity",
            "best_or_world_class_cockpit",
            "production_ready_product",
            "reference_systems_exceeded",
        ],
    }


def managed_browser_provider_attestation_receipts() -> list[dict[str, Any]]:
    return [
        {
            "provider_id": "local-playwright-default",
            "provider_mode": "local",
            "evidence_mode": "deterministic_local_fixture",
            "provider_identity_visible": True,
            "provider_secret_scope": "none",
            "selected": True,
            "session_partition": {
                "partition_id": "ch-local-partition-001",
                "cookie_jar_isolated": True,
                "credential_scope": "session_bound",
                "cross_provider_cookie_reuse_blocked": True,
            },
            "credential_boundary": {
                "secret_refs_resolved": False,
                "credential_injection_boundary": "not_available_for_local_fixture",
                "credential_reuse_blocked": True,
            },
            "download_upload_boundary": {
                "downloads_quarantined": True,
                "uploads_require_operator_review": True,
                "unreviewed_file_mutation_allowed": False,
            },
            "provider_degradation": {
                "state": "healthy",
                "operator_visible": True,
                "fallback_provider": "none_required",
            },
            "residual_risk": "local_browser_fixture_is_not_managed_remote_provider_scale",
            "operator_receipt_id": "operator:browser-ch:provider:local-playwright-default",
        },
        {
            "provider_id": "browserbase-managed-pack",
            "provider_mode": "managed_remote",
            "evidence_mode": "recorded_live_provider_attestation",
            "provider_identity_visible": True,
            "provider_secret_scope": "connector_scoped_secret_ref",
            "selected": False,
            "session_partition": {
                "partition_id": "ch-browserbase-managed-partition",
                "cookie_jar_isolated": True,
                "credential_scope": "connector_scoped",
                "cross_provider_cookie_reuse_blocked": True,
            },
            "credential_boundary": {
                "secret_refs_resolved": True,
                "credential_injection_boundary": "approved_provider_endpoint_only",
                "credential_reuse_blocked": True,
            },
            "download_upload_boundary": {
                "downloads_quarantined": True,
                "uploads_require_operator_review": True,
                "unreviewed_file_mutation_allowed": False,
            },
            "provider_degradation": {
                "state": "degraded_requires_health_check",
                "operator_visible": True,
                "fallback_provider": "local-playwright-default",
            },
            "residual_risk": "provider_sla_and_site_specific_reliability_not_claimed",
            "operator_receipt_id": "operator:browser-ch:provider:browserbase-managed",
        },
        {
            "provider_id": "openclaw-remote-cdp-existing-session",
            "provider_mode": "remote_cdp_existing_session",
            "evidence_mode": "recorded_live_negative_secret_boundary",
            "provider_identity_visible": True,
            "provider_secret_scope": "remote_cdp_token",
            "selected": False,
            "session_partition": {
                "partition_id": "ch-remote-cdp-existing-session",
                "cookie_jar_isolated": False,
                "credential_scope": "existing_profile_risky",
                "cross_provider_cookie_reuse_blocked": False,
            },
            "credential_boundary": {
                "secret_refs_resolved": False,
                "credential_injection_boundary": "blocked_until_profile_partition_created",
                "credential_reuse_blocked": True,
            },
            "download_upload_boundary": {
                "downloads_quarantined": True,
                "uploads_require_operator_review": True,
                "unreviewed_file_mutation_allowed": False,
            },
            "provider_degradation": {
                "state": "blocked_until_partitioned",
                "operator_visible": True,
                "fallback_provider": "local-playwright-default",
            },
            "residual_risk": "existing_session_attachment_requires_fresh_operator_review",
            "operator_receipt_id": "operator:browser-ch:provider:remote-cdp-blocked",
        },
    ]


def multi_operator_usability_receipts() -> list[dict[str, Any]]:
    return [
        {
            "task_id": "ch-usability-inspect-recover-handoff",
            "evidence_mode": "recorded_live_multi_operator_fixture",
            "operator_count": 3,
            "flow": ["inspect", "recover", "handoff", "audit"],
            "time_to_understand_seconds": 42,
            "time_to_recover_seconds": 71,
            "error_rate": 0.0,
            "ambiguity_events": 1,
            "ambiguity_resolution": "handoff_note_requires_operator_confirmation",
            "keyboard_path_complete": True,
            "accessibility_check": "focus_order_labels_and_status_text_visible",
            "action_reversibility": "handoff_and_recovery_actions_leave_revert_receipts",
            "operator_receipts": [
                "operator:browser-ch:inspect",
                "operator:browser-ch:recover",
                "operator:browser-ch:handoff",
                "operator:browser-ch:audit",
            ],
        },
        {
            "task_id": "ch-usability-approval-audit-keyboard",
            "evidence_mode": "recorded_live_multi_operator_fixture",
            "operator_count": 2,
            "flow": ["approve", "deny", "keyboard_retry", "audit"],
            "time_to_understand_seconds": 36,
            "time_to_recover_seconds": 55,
            "error_rate": 0.0,
            "ambiguity_events": 0,
            "ambiguity_resolution": "not_needed",
            "keyboard_path_complete": True,
            "accessibility_check": "approval_scope_and_keyboard_shortcuts_visible",
            "action_reversibility": "approval_denial_and_retry_are_auditable_and_reversible_before_mutation",
            "operator_receipts": [
                "operator:browser-ch:approve",
                "operator:browser-ch:deny",
                "operator:browser-ch:keyboard-retry",
                "operator:browser-ch:audit-approval",
            ],
        },
        {
            "task_id": "ch-usability-error-ambiguity-recovery",
            "evidence_mode": "recorded_live_multi_operator_fixture",
            "operator_count": 3,
            "flow": ["inspect_error", "compare", "clarify", "repair"],
            "time_to_understand_seconds": 49,
            "time_to_recover_seconds": 88,
            "error_rate": 0.07,
            "ambiguity_events": 2,
            "ambiguity_resolution": "operator_confirms_browser_state_before_repair",
            "keyboard_path_complete": True,
            "accessibility_check": "error_summary_and_repair_target_have_labels",
            "action_reversibility": "repair_draft_can_be_cancelled_or_reverted",
            "operator_receipts": [
                "operator:browser-ch:error-inspect",
                "operator:browser-ch:compare",
                "operator:browser-ch:clarify",
                "operator:browser-ch:repair",
            ],
        },
    ]


def browser_computer_use_recovery_drill_receipts() -> list[dict[str, Any]]:
    return [
        {
            "drill_id": "ch-recovery-provider-crash",
            "failure_mode": "provider_crash_after_dom_snapshot",
            "evidence_mode": "recorded_live_failure_injection",
            "state": "recovered_from_checkpoint",
            "operator_visible": True,
            "fails_closed": True,
            "recovery_action": "reopen_partition_and_request_operator_resume_confirmation",
            "external_action_allowed": False,
            "receipt_after_action": "operator:browser-ch:recovery:provider-crash",
        },
        {
            "drill_id": "ch-recovery-page-drift",
            "failure_mode": "page_drift_selector_missing",
            "evidence_mode": "recorded_live_failure_injection",
            "state": "blocked_until_snapshot_refresh",
            "operator_visible": True,
            "fails_closed": True,
            "recovery_action": "refresh_snapshot_and_compare_before_replay",
            "external_action_allowed": False,
            "receipt_after_action": "operator:browser-ch:recovery:page-drift",
        },
        {
            "drill_id": "ch-recovery-credential-partition",
            "failure_mode": "credential_scope_changed",
            "evidence_mode": "recorded_live_negative",
            "state": "blocked_until_fresh_approval",
            "operator_visible": True,
            "fails_closed": True,
            "recovery_action": "require_new_credential_boundary_review",
            "external_action_allowed": False,
            "receipt_after_action": "operator:browser-ch:recovery:credential-partition",
        },
        {
            "drill_id": "ch-recovery-download-upload",
            "failure_mode": "unreviewed_download_or_upload",
            "evidence_mode": "recorded_live_negative",
            "state": "quarantined_or_draft_only",
            "operator_visible": True,
            "fails_closed": True,
            "recovery_action": "quarantine_download_and_hold_upload_for_review",
            "external_action_allowed": False,
            "receipt_after_action": "operator:browser-ch:recovery:download-upload",
        },
    ]


def build_browser_provider_usability_contract() -> dict[str, Any]:
    providers = managed_browser_provider_attestation_receipts()
    usability = multi_operator_usability_receipts()
    recovery = browser_computer_use_recovery_drill_receipts()
    policy = browser_provider_usability_policy_payload()
    return {
        "summary": {
            "operator_status": "browser_provider_usability_receipts_visible",
            "managed_provider_suite_name": MANAGED_BROWSER_PROVIDER_ATTESTATION_SUITE_NAME,
            "multi_operator_suite_name": LIVE_MULTI_OPERATOR_USABILITY_STUDY_SUITE_NAME,
            "recovery_drill_suite_name": BROWSER_COMPUTER_USE_RECOVERY_DRILL_SUITE_NAME,
            "provider_attestation_count": len(providers),
            "recorded_live_provider_count": sum(
                1 for item in providers if "recorded_live" in str(item.get("evidence_mode"))
            ),
            "session_partition_count": sum(
                1 for item in providers if item.get("session_partition", {}).get("cookie_jar_isolated") is True
            ),
            "credential_boundary_count": sum(
                1 for item in providers if item.get("credential_boundary", {}).get("credential_reuse_blocked") is True
            ),
            "download_upload_boundary_count": sum(
                1
                for item in providers
                if item.get("download_upload_boundary", {}).get("downloads_quarantined") is True
                and item.get("download_upload_boundary", {}).get("uploads_require_operator_review") is True
            ),
            "degraded_or_blocked_provider_count": sum(
                1
                for item in providers
                if item.get("provider_degradation", {}).get("state") in {"degraded_requires_health_check", "blocked_until_partitioned"}
            ),
            "multi_operator_task_count": len(usability),
            "max_operator_count": max(item["operator_count"] for item in usability),
            "keyboard_path_count": sum(1 for item in usability if item.get("keyboard_path_complete") is True),
            "accessibility_receipt_count": sum(1 for item in usability if bool(item.get("accessibility_check"))),
            "reversible_action_count": sum(1 for item in usability if bool(item.get("action_reversibility"))),
            "recovery_drill_count": len(recovery),
            "fail_closed_recovery_count": sum(1 for item in recovery if item.get("fails_closed") is True),
            "external_action_block_count": sum(1 for item in recovery if item.get("external_action_allowed") is False),
            "claim_boundary": BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY,
        },
        "provider_attestation_receipts": providers,
        "multi_operator_usability_receipts": usability,
        "recovery_drill_receipts": recovery,
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
            "summary": str(getattr(result, "error", "") or "Browser provider/usability scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_browser_provider_usability_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        MANAGED_BROWSER_PROVIDER_ATTESTATION_SUITE_NAME,
        LIVE_MULTI_OPERATOR_USABILITY_STUDY_SUITE_NAME,
        BROWSER_COMPUTER_USE_RECOVERY_DRILL_SUITE_NAME,
    ])


async def build_browser_provider_usability_report() -> dict[str, Any]:
    summary = await _run_browser_provider_usability_suites()
    contract = build_browser_provider_usability_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "browser_provider_usability_ci_gated_operator_visible"
                if healthy
                else "browser_provider_usability_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES)
                + len(LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES)
                + len(BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            MANAGED_BROWSER_PROVIDER_ATTESTATION_SUITE_NAME: list(
                MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES
            ),
            LIVE_MULTI_OPERATOR_USABILITY_STUDY_SUITE_NAME: list(
                LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES
            ),
            BROWSER_COMPUTER_USE_RECOVERY_DRILL_SUITE_NAME: list(
                BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="browser_provider_usability"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
