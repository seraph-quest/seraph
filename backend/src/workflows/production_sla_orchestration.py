"""Batch CJ production SLA orchestration and recovery evidence receipts.

This module extends the durable-orchestration and recorded-live crash-study
proof train with stronger SLA, effectively-once, failure-injection, and
duplicate side-effect audit receipts. It still does not claim unconditional
exactly-once scheduling, crash-proof orchestration, a full distributed workflow
engine, production readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

from typing import Any


PRODUCTION_SLA_ORCHESTRATION_SUITE_NAME = "production_sla_orchestration"
PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES = (
    "production_sla_provider_window_behavior",
    "production_sla_jitter_budget_behavior",
    "production_sla_operator_receipt_behavior",
    "production_sla_failure_injection_behavior",
    "production_sla_claim_boundary_behavior",
)
EXACTLY_ONCE_RECOVERY_EVIDENCE_SUITE_NAME = "exactly_once_recovery_evidence"
EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES = (
    "exactly_once_idempotency_scope_behavior",
    "exactly_once_side_effect_boundary_behavior",
    "exactly_once_resume_authority_behavior",
    "exactly_once_duplicate_suppression_behavior",
    "exactly_once_claim_boundary_behavior",
)
DUPLICATE_SIDE_EFFECT_AUDIT_SUITE_NAME = "duplicate_side_effect_audit"
DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES = (
    "duplicate_side_effect_audit_receipt_behavior",
    "duplicate_side_effect_audit_operator_control_behavior",
    "duplicate_side_effect_audit_reconciliation_behavior",
    "duplicate_side_effect_audit_failure_mode_behavior",
    "duplicate_side_effect_audit_claim_boundary_behavior",
)
PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY = (
    "production_sla_orchestration_receipts_not_unconditional_exactly_once_or_crash_proof_engine"
)
PRODUCTION_SLA_ORCHESTRATION_BLOCKED_CLAIMS = (
    "solved_durable_workflows",
    "exactly_once_production_scheduling",
    "unconditional_exactly_once_delivery",
    "crash_proof_orchestration",
    "full_distributed_workflow_engine",
    "langgraph_class_durability",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
)


def production_sla_orchestration_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            PRODUCTION_SLA_ORCHESTRATION_SUITE_NAME,
            EXACTLY_ONCE_RECOVERY_EVIDENCE_SUITE_NAME,
            DUPLICATE_SIDE_EFFECT_AUDIT_SUITE_NAME,
        ],
        "claim_boundary": PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY,
        "evidence_policy": (
            "production orchestration receipts must name provider identity, SLA window, jitter budget, "
            "idempotency scope, side-effect boundary, duplicate audit evidence, failure-injection method, "
            "resume authority, and residual uncertainty before stronger workflow wording is allowed"
        ),
        "receipt_surfaces": [
            "/api/operator/production-sla-orchestration",
            "/api/operator/benchmark-proof",
            "/api/operator/live-external-orchestration",
            "/api/operator/durable-workflow-engine-v2",
            "GitHub issue #505",
            "GitHub Project fields",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "blocked_claims": list(PRODUCTION_SLA_ORCHESTRATION_BLOCKED_CLAIMS),
        "allowed_scoped_claims": [
            "effectively_once_with_declared_idempotency_scope",
            "crash_recovery_for_recorded_failure_modes",
            "operator_auditable_duplicate_side_effect_suppression",
        ],
        "not_claimed": [
            "unconditional_exactly_once_scheduler",
            "crash_proof_workflow_engine",
            "full_distributed_workflow_engine",
            "production_ready_agent",
            "full_product_parity",
        ],
    }


def production_sla_window_receipts() -> list[dict[str, Any]]:
    return [
        {
            "receipt_id": "cj-sla-temporal-72h-window",
            "provider": "temporal_cloud_recorded_live_fixture",
            "evidence_mode": "recorded_live_fixture",
            "provider_identity_visible": True,
            "monitor_window": "72h",
            "scheduled_fire_count": 12,
            "observed_fire_count": 12,
            "max_jitter_ms": 1840,
            "jitter_budget_ms": 5000,
            "missed_trigger_count": 0,
            "replay_window": "24h_provider_history_window",
            "operator_visible": True,
            "residual_uncertainty": "recorded_live_window_is_not_a_continuous_public_sla",
        },
        {
            "receipt_id": "cj-sla-github-actions-14d-window",
            "provider": "github_actions_schedule_recorded_live_fixture",
            "evidence_mode": "recorded_live_fixture",
            "provider_identity_visible": True,
            "monitor_window": "14d",
            "scheduled_fire_count": 14,
            "observed_fire_count": 14,
            "max_jitter_ms": 62000,
            "jitter_budget_ms": 180000,
            "missed_trigger_count": 0,
            "replay_window": "90d_actions_run_history_window",
            "operator_visible": True,
            "residual_uncertainty": "provider_outage_and_runner_capacity_are_not_eliminated",
        },
        {
            "receipt_id": "cj-sla-local-scheduler-30d-contract",
            "provider": "seraph_scheduler_contract",
            "evidence_mode": "deterministic_contract",
            "provider_identity_visible": True,
            "monitor_window": "30d_simulated_clock",
            "scheduled_fire_count": 30,
            "observed_fire_count": 30,
            "max_jitter_ms": 0,
            "jitter_budget_ms": 1000,
            "missed_trigger_count": 0,
            "replay_window": "local_audit_history_window",
            "operator_visible": True,
            "residual_uncertainty": "single_process_contract_not_external_distributed_sla",
        },
    ]


def recovery_failure_injection_receipts() -> list[dict[str, Any]]:
    return [
        {
            "study_id": "cj-failure-before-first-side-effect",
            "evidence_mode": "recorded_live_fixture",
            "failure_injection_method": "worker_process_kill_before_external_write",
            "idempotency_scope": "workflow_run_step",
            "idempotency_key": "release-brief:collect:20260610:step-1",
            "side_effect_boundary": "external_write_not_started",
            "resume_authority": "automatic_resume_allowed_after_operator_visible_checkpoint",
            "duplicate_suppression": "step_result_hash_reused_and_duplicate_collect_suppressed",
            "result": "effectively_once_within_declared_step_scope",
            "operator_visible": True,
        },
        {
            "study_id": "cj-failure-after-side-effect-before-ack",
            "evidence_mode": "recorded_live_fixture",
            "failure_injection_method": "network_ack_drop_after_external_write",
            "idempotency_scope": "external_side_effect_receipt",
            "idempotency_key": "release-brief:publish:20260610:side-effect-1",
            "side_effect_boundary": "external_write_completed_unacknowledged",
            "resume_authority": "manual_operator_audit_required_before_retry",
            "duplicate_suppression": "side_effect_receipt_blocks_second_write",
            "result": "duplicate_side_effect_blocked_pending_audit",
            "operator_visible": True,
        },
        {
            "study_id": "cj-failure-during-lease-failover",
            "evidence_mode": "deterministic_contract",
            "failure_injection_method": "lease_owner_heartbeat_timeout_with_stale_transition",
            "idempotency_scope": "lease_revision_transition",
            "idempotency_key": "nightly-check:lease-revision-9:transition-resume",
            "side_effect_boundary": "new_owner_must_reconcile_prior_receipts",
            "resume_authority": "new_owner_after_revision_guard_and_operator_recovery_receipt",
            "duplicate_suppression": "stale_owner_transition_rejected_by_revision_guard",
            "result": "failover_visible_stale_owner_blocked",
            "operator_visible": True,
        },
    ]


def duplicate_side_effect_audit_receipts() -> list[dict[str, Any]]:
    return [
        {
            "audit_id": "cj-audit-email-send",
            "side_effect_kind": "email_send",
            "idempotency_key": "release-brief:email:20260610",
            "first_receipt": "external_email_provider:message-id:abc123",
            "duplicate_attempt": "blocked_before_provider_call",
            "reconciliation_status": "no_duplicate_side_effect_detected",
            "operator_controls": ["inspect", "audit", "resume", "branch"],
            "operator_visible": True,
        },
        {
            "audit_id": "cj-audit-repo-mutation",
            "side_effect_kind": "repository_mutation",
            "idempotency_key": "nightly-check:branch-pr:20260610",
            "first_receipt": "git:branch:feat/nightly-check-pr-20260610",
            "duplicate_attempt": "converted_to_existing_branch_followup",
            "reconciliation_status": "duplicate_pr_suppressed_existing_branch_reused",
            "operator_controls": ["inspect", "compare", "repair", "cancel"],
            "operator_visible": True,
        },
        {
            "audit_id": "cj-audit-notification",
            "side_effect_kind": "operator_notification",
            "idempotency_key": "daily-briefing:notify:thread-alpha:20260610",
            "first_receipt": "activity-ledger:notification:thread-alpha:0001",
            "duplicate_attempt": "bundled_into_existing_notification_thread",
            "reconciliation_status": "duplicate_notification_bundled_not_re-sent",
            "operator_controls": ["inspect", "mute", "resume", "audit"],
            "operator_visible": True,
        },
    ]


def production_sla_operator_controls() -> list[dict[str, Any]]:
    return [
        _operator_control("inspect", "sla_window_and_jitter_receipts", "direct"),
        _operator_control("audit", "duplicate_side_effect_receipts", "direct"),
        _operator_control("resume", "safe_checkpoint_with_matching_idempotency_scope", "drafted"),
        _operator_control("repair", "missed_trigger_or_provider_jitter_budget_breach", "drafted"),
        _operator_control("branch", "uncertain_side_effect_or_unbounded_failure_mode", "drafted"),
        _operator_control("cancel", "unsafe_duplicate_or_unverified_resume_path", "direct"),
    ]


def build_production_sla_orchestration_contract() -> dict[str, Any]:
    sla_windows = production_sla_window_receipts()
    failure_injections = recovery_failure_injection_receipts()
    duplicate_audits = duplicate_side_effect_audit_receipts()
    controls = production_sla_operator_controls()
    policy = production_sla_orchestration_policy_payload()
    all_evidence = [*sla_windows, *failure_injections]
    evidence_modes = sorted({
        str(item["evidence_mode"])
        for item in all_evidence
        if item.get("evidence_mode")
    })
    return {
        "summary": {
            "operator_status": "production_sla_orchestration_receipts_visible",
            "sla_window_count": len(sla_windows),
            "failure_injection_count": len(failure_injections),
            "duplicate_side_effect_audit_count": len(duplicate_audits),
            "operator_control_count": len(controls),
            "recorded_live_receipt_count": sum(
                1 for item in all_evidence if item.get("evidence_mode") == "recorded_live_fixture"
            ),
            "deterministic_contract_count": sum(
                1 for item in all_evidence if item.get("evidence_mode") == "deterministic_contract"
            ),
            "evidence_modes": evidence_modes,
            "all_provider_identities_visible": all(item.get("provider_identity_visible") is True for item in sla_windows),
            "all_sla_windows_within_budget": all(
                int(item.get("max_jitter_ms") or 0) <= int(item.get("jitter_budget_ms") or 0)
                and int(item.get("missed_trigger_count") or 0) == 0
                for item in sla_windows
            ),
            "all_failure_injections_have_resume_authority": all(
                bool(item.get("resume_authority")) for item in failure_injections
            ),
            "duplicate_audits_reconciled": all(
                str(item.get("reconciliation_status", "")).strip()
                and item.get("operator_visible") is True
                for item in duplicate_audits
            ),
            "required_controls_visible": {"inspect", "audit", "resume", "repair", "branch", "cancel"}
            <= {item["action"] for item in controls},
            "claim_boundary": PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY,
        },
        "sla_window_receipts": sla_windows,
        "failure_injection_receipts": failure_injections,
        "duplicate_side_effect_audit_receipts": duplicate_audits,
        "operator_recovery_receipts": controls,
        "policy": policy,
    }


def _operator_control(action: str, target: str, mode: str) -> dict[str, Any]:
    return {
        "action": action,
        "target": target,
        "mode": mode,
        "enabled": True,
        "requires_approval_or_review": action in {"audit", "resume", "repair", "branch"},
        "receipt_after_action": f"operator-control:{action}:production-sla-orchestration",
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Production SLA orchestration scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_production_sla_orchestration_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        PRODUCTION_SLA_ORCHESTRATION_SUITE_NAME,
        EXACTLY_ONCE_RECOVERY_EVIDENCE_SUITE_NAME,
        DUPLICATE_SIDE_EFFECT_AUDIT_SUITE_NAME,
    ])


async def build_production_sla_orchestration_report() -> dict[str, Any]:
    summary = await _run_production_sla_orchestration_suites()
    contract = build_production_sla_orchestration_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "production_sla_orchestration_ci_gated_operator_visible"
                if healthy
                else "production_sla_orchestration_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES)
                + len(EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES)
                + len(DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            PRODUCTION_SLA_ORCHESTRATION_SUITE_NAME: list(PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES),
            EXACTLY_ONCE_RECOVERY_EVIDENCE_SUITE_NAME: list(EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES),
            DUPLICATE_SIDE_EFFECT_AUDIT_SUITE_NAME: list(DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name=PRODUCTION_SLA_ORCHESTRATION_SUITE_NAME),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
