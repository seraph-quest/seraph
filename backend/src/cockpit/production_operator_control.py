"""Batch CB production operator control and parity-train receipts.

This module composes the production parity train into one operator-visible
control and verification contract. It is deterministic proof for dense control
and train integration, not a full parity, superiority, production-ready, or
solved operator-control claim.
"""

from __future__ import annotations

from typing import Any


PRODUCTION_OPERATOR_CONTROL_PARITY_SUITE_NAME = "production_operator_control_parity"
PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES = (
    "operator_control_train_receipt_behavior",
    "operator_control_long_work_debugger_behavior",
    "operator_control_recovery_action_behavior",
    "operator_control_authority_boundary_behavior",
    "operator_control_claim_boundary_behavior",
    "operator_production_control_surface_behavior",
)
PRODUCTION_PARITY_TRAIN_SUITE_NAME = "production_parity_train"
PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES = (
    "production_parity_train_batch_merge_receipt_behavior",
    "production_parity_train_suite_coverage_behavior",
    "production_parity_train_operator_surface_behavior",
    "production_parity_train_residual_risk_behavior",
    "production_parity_train_board_receipt_behavior",
    "production_parity_train_final_audit_behavior",
)
PRODUCTION_OPERATOR_CONTROL_CLAIM_BOUNDARY = (
    "operator_control_parity_receipts_not_full_parity_superiority_or_production_ready_product"
)
PRODUCTION_OPERATOR_CONTROL_BLOCKED_CLAIMS = (
    "best_cockpit",
    "world_class_cockpit",
    "full_parity",
    "reference_systems_exceeded",
    "production_ready_product",
    "solved_operator_control",
    "production_security_solved",
    "broad_reach_parity",
    "marketplace_superiority",
    "live_human_outcome_superiority",
)


def production_operator_control_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            PRODUCTION_OPERATOR_CONTROL_PARITY_SUITE_NAME,
            PRODUCTION_PARITY_TRAIN_SUITE_NAME,
        ],
        "claim_boundary": PRODUCTION_OPERATOR_CONTROL_CLAIM_BOUNDARY,
        "control_policy": (
            "long-work operator controls must explain state authority risk recovery and the receipt left by each action"
        ),
        "train_policy": (
            "production parity train summaries must list every batch gate, linked PR, operator surface, "
            "residual risk, and blocked claim before completion wording is allowed"
        ),
        "receipt_surfaces": [
            "/api/operator/production-operator-control-parity",
            "/api/operator/benchmark-proof",
            "/api/operator/production-parity-readiness",
            "/api/operator/secure-capability-host-hardening",
            "/api/operator/durable-workflow-engine-v2",
            "/api/operator/production-reach-browser-voice",
            "/api/operator/live-guardian-learning-quality",
            "/api/operator/marketplace-lifecycle-maturity",
            "/api/operator/cockpit-efficiency-benchmark",
            "GitHub issue #482",
            "GitHub Project fields",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "blocked_claims": list(PRODUCTION_OPERATOR_CONTROL_BLOCKED_CLAIMS),
        "not_claimed": [
            "full_parity_achieved",
            "reference_systems_exceeded",
            "production_ready_agent",
            "solved_operator_control",
            "live_human_outcome_superiority",
        ],
    }


def operator_control_receipts() -> list[dict[str, Any]]:
    return [
        {
            "receipt_id": "cb-control-durable-orchestration",
            "surface": "durable_orchestration",
            "operator_question": "what workflow state is durable and what can be resumed safely",
            "authority_source": "/api/operator/durable-workflow-engine-v2",
            "state_visible": "lease_owner_revision_guard_checkpoint_and_trigger_dedupe_visible",
            "risk_visible": "unsafe_resume_and_delegated_artifact_adoption_gates_visible",
            "controls": [
                _control("pause", "direct", "lease_owned_run", True, True),
                _control("resume", "drafted", "safe_checkpoint", True, True),
                _control("cancel", "direct", "unsafe_or_operator_stopped_run", True, True),
                _control("retry", "drafted", "failed_idempotent_step", True, True),
                _control("branch", "drafted", "checkpoint_candidate", True, True),
            ],
            "residual_risk": "external_queue_exactly_once_effects_remain_outside_receipt_scope",
        },
        {
            "receipt_id": "cb-control-secure-host",
            "surface": "secure_host",
            "operator_question": "what authority was used and what boundary blocked or allowed recovery",
            "authority_source": "/api/operator/secure-capability-host-hardening",
            "state_visible": "secret_redaction_partition_egress_revocation_and_trust_drift_receipts_visible",
            "risk_visible": "privileged_path_and_credential_or_evidence_exposure_visible",
            "controls": [
                _control("inspect", "direct", "trust_boundary_receipt", True, False),
                _control("revoke", "direct", "extension_or_connector_runtime_access", True, True),
                _control("repair", "drafted", "failed_closed_privileged_path", True, True),
            ],
            "residual_risk": "hardware_backed_or_container_isolation_is_not_claimed",
        },
        {
            "receipt_id": "cb-control-reach-browser-voice",
            "surface": "reach_browser_voice",
            "operator_question": "which channel or browser session failed and what recovery is safe",
            "authority_source": "/api/operator/production-reach-browser-voice",
            "state_visible": "pairing_session_partition_crash_recovery_page_drift_and_voice_media_receipts_visible",
            "risk_visible": "revocation_privacy_redaction_and_capture_provider_boundary_visible",
            "controls": [
                _control("resume", "drafted", "same_thread_channel_recovery", True, True),
                _control("repair", "drafted", "degraded_channel_or_browser_provider", True, True),
                _control("revoke", "direct", "paired_channel_or_voice_media_profile", True, True),
            ],
            "residual_risk": "broad_live_channel_and_production_stt_tts_coverage_remain_future_work",
        },
        {
            "receipt_id": "cb-control-learning-memory",
            "surface": "guardian_learning_memory",
            "operator_question": "why did the guardian adapt or restrain itself",
            "authority_source": "/api/operator/live-guardian-learning-quality",
            "state_visible": "intervention_outcome_cohorts_policy_deltas_provider_degradation_and_memory_reconciliation_visible",
            "risk_visible": "false_positive_false_negative_stale_evidence_and_provider_regression_visible",
            "controls": [
                _control("inspect", "direct", "learning_explanation", True, False),
                _control("repair", "drafted", "provider_degradation_or_stale_evidence", True, True),
                _control("audit", "direct", "policy_delta_receipt", True, False),
            ],
            "residual_risk": "live_human_outcome_superiority_and_causal_attribution_are_not_claimed",
        },
        {
            "receipt_id": "cb-control-marketplace",
            "surface": "marketplace_lifecycle",
            "operator_question": "what changed in a capability package and how can it roll back",
            "authority_source": "/api/operator/marketplace-lifecycle-maturity",
            "state_visible": "install_update_downgrade_disable_rollback_review_quarantine_diagnostics_visible",
            "risk_visible": "permission_delta_risk_delta_dependency_compatibility_and_failed_update_recovery_visible",
            "controls": [
                _control("compare", "direct", "before_after_permission_and_risk_delta", True, False),
                _control("revoke", "direct", "quarantined_or_suspicious_package", True, True),
                _control("repair", "drafted", "failed_update_or_incompatible_package", True, True),
            ],
            "residual_risk": "live_third_party_attestation_and_production_marketplace_security_are_not_claimed",
        },
        {
            "receipt_id": "cb-control-approval-audit",
            "surface": "approvals_and_activity_ledger",
            "operator_question": "who approved what and what changed afterward",
            "authority_source": "/api/operator/benchmark-proof",
            "state_visible": "approval_scope_activity_receipts_actor_target_time_and_followup_visible",
            "risk_visible": "approval_context_boundary_and_mutation_scope_visible_before_action",
            "controls": [
                _control("approve", "direct", "pending_approval_with_scope", True, True),
                _control("deny", "direct", "pending_approval_with_reason", True, True),
                _control("audit", "direct", "recent_high_risk_action", True, False),
            ],
            "residual_risk": "live_multi_operator_usability_studies_are_not_claimed",
        },
    ]


def production_parity_train_receipts() -> list[dict[str, Any]]:
    return [
        _train_receipt("BV", 476, 484, "production_parity_readiness", "/api/operator/production-parity-readiness"),
        _train_receipt(
            "BW",
            477,
            485,
            "production_secure_host_hardening",
            "/api/operator/secure-capability-host-hardening",
        ),
        _train_receipt(
            "BX",
            478,
            486,
            "production_durable_orchestration",
            "/api/operator/durable-workflow-engine-v2",
        ),
        _train_receipt(
            "BY",
            479,
            487,
            "production_reach_channel_hardening",
            "/api/operator/production-reach-browser-voice",
        ),
        _train_receipt(
            "BZ",
            480,
            488,
            "live_guardian_learning_quality",
            "/api/operator/live-guardian-learning-quality",
        ),
        _train_receipt(
            "CA",
            481,
            489,
            "marketplace_grade_capability_lifecycle",
            "/api/operator/marketplace-lifecycle-maturity",
        ),
        {
            "batch": "CB",
            "issue": 482,
            "merged_pr": None,
            "proof_suite": PRODUCTION_OPERATOR_CONTROL_PARITY_SUITE_NAME,
            "operator_surface": "/api/operator/production-operator-control-parity",
            "evidence_state": "active_branch_receipts_visible_until_pr_merge",
            "project_state": "queue_now_status_in_progress_pr_not_ready_code_review_not_ready",
            "blocked_claims_visible": True,
        },
    ]


def final_audit_receipts() -> list[dict[str, Any]]:
    return [
        {
            "audit_id": "cb-audit-board-state",
            "question": "does the execution layer name the active batch and avoid duplicate M0-M9 work",
            "evidence": "issue_482_project_fields_and_duplicate_scope_boundary",
            "required_before_goal_completion": True,
            "current_disposition": "active_batch_in_progress_until_cb_pr_merges",
        },
        {
            "audit_id": "cb-audit-claim-ledger",
            "question": "does wording distinguish proof receipts from full parity or superiority",
            "evidence": "docs/research/19-strategy-claim-ledger.md",
            "required_before_goal_completion": True,
            "current_disposition": "blocked_claims_remain_explicit",
        },
        {
            "audit_id": "cb-audit-critic",
            "question": "did an independent critic check false completion, stale board, and trust-boundary gaps",
            "evidence": "pr_body_and_final_review_receipt",
            "required_before_goal_completion": True,
            "current_disposition": "required_before_pr_creation_or_merge",
        },
    ]


def build_production_operator_control_contract() -> dict[str, Any]:
    controls = operator_control_receipts()
    train = production_parity_train_receipts()
    audits = final_audit_receipts()
    policy = production_operator_control_policy_payload()
    control_actions = {
        control["action"]
        for receipt in controls
        for control in receipt["controls"]
    }
    return {
        "summary": {
            "operator_status": "production_operator_control_parity_receipts_visible",
            "control_surface_count": len(controls),
            "train_batch_count": len(train),
            "merged_prior_batch_count": sum(1 for item in train if item.get("merged_pr")),
            "recovery_control_count": sum(
                1
                for receipt in controls
                for control in receipt["controls"]
                if control["action"] in {"pause", "resume", "cancel", "retry", "repair", "branch", "revoke"}
            ),
            "authority_receipt_count": sum(1 for item in controls if item.get("authority_source")),
            "residual_risk_count": sum(1 for item in controls if item.get("residual_risk")),
            "final_audit_count": len(audits),
            "blocked_claim_count": len(policy["blocked_claims"]),
            "required_actions_visible": {
                "inspect",
                "approve",
                "deny",
                "pause",
                "resume",
                "cancel",
                "retry",
                "repair",
                "branch",
                "compare",
                "revoke",
                "audit",
            }
            <= control_actions,
            "claim_boundary": PRODUCTION_OPERATOR_CONTROL_CLAIM_BOUNDARY,
        },
        "control_receipts": controls,
        "train_receipts": train,
        "final_audit_receipts": audits,
        "policy": policy,
    }


def _control(
    action: str,
    mode: str,
    target: str,
    enabled: bool,
    requires_approval_or_review: bool,
) -> dict[str, Any]:
    return {
        "action": action,
        "mode": mode,
        "target": target,
        "enabled": enabled,
        "requires_approval_or_review": requires_approval_or_review,
        "receipt_after_action": f"operator-control:{action}:{target}",
    }


def _train_receipt(batch: str, issue: int, pr: int, proof_suite: str, operator_surface: str) -> dict[str, Any]:
    return {
        "batch": batch,
        "issue": issue,
        "merged_pr": pr,
        "proof_suite": proof_suite,
        "operator_surface": operator_surface,
        "evidence_state": "merged_to_develop",
        "project_state": "done_pr_merged_code_review_passed",
        "blocked_claims_visible": True,
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Production operator control scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_production_operator_control_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        PRODUCTION_OPERATOR_CONTROL_PARITY_SUITE_NAME,
        PRODUCTION_PARITY_TRAIN_SUITE_NAME,
    ])


async def build_production_operator_control_report() -> dict[str, Any]:
    summary = await _run_production_operator_control_suites()
    contract = build_production_operator_control_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "production_operator_control_parity_ci_gated_operator_visible"
                if healthy
                else "production_operator_control_parity_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES)
                + len(PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            PRODUCTION_OPERATOR_CONTROL_PARITY_SUITE_NAME: list(
                PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES
            ),
            PRODUCTION_PARITY_TRAIN_SUITE_NAME: list(PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="production_operator_control_parity"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
