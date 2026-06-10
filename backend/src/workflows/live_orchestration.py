"""Batch CC live/external orchestration and crash-study receipts.

This module extends the durable-orchestration proof train with recorded-live
provider and crash-study evidence. It does not claim solved durable workflows,
production exactly-once scheduling, crash-proof orchestration, full parity, or
production readiness.
"""

from __future__ import annotations

from typing import Any


LIVE_EXTERNAL_ORCHESTRATION_SUITE_NAME = "live_external_orchestration_attestation"
LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES = (
    "live_external_scheduler_provider_identity_behavior",
    "live_external_idempotency_boundary_behavior",
    "live_external_replay_suppression_behavior",
    "live_external_operator_recovery_control_behavior",
    "live_external_evidence_mode_boundary_behavior",
    "live_external_orchestration_claim_boundary_behavior",
)
ORCHESTRATION_CRASH_RECOVERY_STUDY_SUITE_NAME = "orchestration_crash_recovery_study"
ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES = (
    "orchestration_crash_checkpoint_recovery_behavior",
    "orchestration_crash_side_effect_boundary_behavior",
    "orchestration_crash_lease_transfer_behavior",
    "orchestration_crash_resume_authority_behavior",
    "orchestration_crash_study_claim_boundary_behavior",
)
LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY = (
    "recorded_live_orchestration_receipts_not_exactly_once_or_crash_proof_engine"
)
LIVE_EXTERNAL_ORCHESTRATION_BLOCKED_CLAIMS = (
    "solved_durable_workflows",
    "exactly_once_production_scheduling",
    "crash_proof_orchestration",
    "full_distributed_workflow_engine",
    "langgraph_class_durability",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
)


def live_external_orchestration_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            LIVE_EXTERNAL_ORCHESTRATION_SUITE_NAME,
            ORCHESTRATION_CRASH_RECOVERY_STUDY_SUITE_NAME,
        ],
        "claim_boundary": LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY,
        "evidence_policy": (
            "live/external orchestration receipts must name provider identity evidence mode replay window "
            "idempotency boundary side-effect boundary and residual uncertainty"
        ),
        "receipt_surfaces": [
            "/api/operator/live-external-orchestration",
            "/api/operator/benchmark-proof",
            "/api/operator/durable-workflow-engine-v2",
            "GitHub issue #491",
            "GitHub Project fields",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "blocked_claims": list(LIVE_EXTERNAL_ORCHESTRATION_BLOCKED_CLAIMS),
        "not_claimed": [
            "exactly_once_production_scheduler",
            "crash_proof_workflow_engine",
            "full_distributed_workflow_engine",
            "full_parity_achieved",
            "production_ready_agent",
        ],
    }


def external_scheduler_attestation_receipts() -> list[dict[str, Any]]:
    return [
        {
            "receipt_id": "cc-provider-temporal-recorded-live",
            "provider": "temporal_cloud_recorded_live_fixture",
            "evidence_mode": "recorded_live_fixture",
            "provider_identity_visible": True,
            "lease_or_run_id": "temporal:namespace:seraph-prod-parity:workflow:release-brief:run-20260610",
            "idempotency_key": "workflow:release-brief:collect:20260610T0118Z",
            "replay_window": "24h_provider_history_window",
            "side_effect_boundary": "external_email_send_blocked_until_operator_approval",
            "delivery_semantics": "at_least_once_with_idempotency_receipt",
            "operator_visible": True,
            "residual_uncertainty": "provider_history_is_recorded_live_fixture_not_continuously_monitored_sla",
        },
        {
            "receipt_id": "cc-provider-github-actions-recorded-live",
            "provider": "github_actions_schedule_recorded_live_fixture",
            "evidence_mode": "recorded_live_fixture",
            "provider_identity_visible": True,
            "lease_or_run_id": "github-actions:workflow:nightly-operator-check:run-27246559902",
            "idempotency_key": "workflow:nightly-operator-check:batch-cc:20260610",
            "replay_window": "90d_actions_run_history_window",
            "side_effect_boundary": "repo_mutation_requires_branch_pr_and_review",
            "delivery_semantics": "at_least_once_with_branch_and_pr_dedupe",
            "operator_visible": True,
            "residual_uncertainty": "actions_scheduling_jitter_and_provider_outage_are_not_eliminated",
        },
        {
            "receipt_id": "cc-provider-local-scheduler-contract",
            "provider": "seraph_scheduler_deterministic_contract",
            "evidence_mode": "deterministic_contract",
            "provider_identity_visible": True,
            "lease_or_run_id": "seraph:scheduler:daily-briefing:lease-local-contract",
            "idempotency_key": "daily-briefing:operator-thread:next-fire",
            "replay_window": "local_run_history_window",
            "side_effect_boundary": "notification_and_workspace_write_require_existing_policy",
            "delivery_semantics": "single_process_best_effort_with_receipts",
            "operator_visible": True,
            "residual_uncertainty": "not_a_distributed_external_scheduler",
        },
    ]


def crash_recovery_study_receipts() -> list[dict[str, Any]]:
    return [
        {
            "study_id": "cc-crash-before-side-effect",
            "evidence_mode": "recorded_live_fixture",
            "injected_failure": "worker_restart_before_external_side_effect",
            "checkpoint": "collect_step_completed_write_step_not_started",
            "replay_suppression": "idempotency_key_reused_and_duplicate_collect_result_suppressed",
            "side_effect_state": "not_started",
            "resume_authority": "safe_checkpoint_resume_draft_requires_operator_review",
            "operator_controls": ["resume", "retry", "branch", "cancel"],
            "operator_visible": True,
            "result": "resumed_without_duplicate_external_side_effect",
        },
        {
            "study_id": "cc-crash-after-side-effect",
            "evidence_mode": "recorded_live_fixture",
            "injected_failure": "worker_restart_after_side_effect_before_ack",
            "checkpoint": "write_step_completed_ack_missing",
            "replay_suppression": "side_effect_receipt_blocks_second_write_until_operator_confirms",
            "side_effect_state": "completed_unacknowledged",
            "resume_authority": "manual_audit_required_before_retry",
            "operator_controls": ["inspect", "audit", "retry", "cancel"],
            "operator_visible": True,
            "result": "duplicate_side_effect_blocked_pending_operator_audit",
        },
        {
            "study_id": "cc-crash-during-lease-transfer",
            "evidence_mode": "deterministic_contract",
            "injected_failure": "lease_owner_lost_heartbeat_during_recovery",
            "checkpoint": "lease_revision_7_checkpoint_recovery_candidate",
            "replay_suppression": "stale_owner_transition_rejected_by_revision_guard",
            "side_effect_state": "unknown_until_new_owner_audit",
            "resume_authority": "new_owner_must_acquire_lease_and_record_revision",
            "operator_controls": ["inspect", "resume", "repair", "branch", "cancel"],
            "operator_visible": True,
            "result": "lease_transfer_visible_stale_owner_blocked",
        },
    ]


def operator_recovery_receipts() -> list[dict[str, Any]]:
    return [
        _operator_control(
            "inspect",
            "provider_history_and_side_effect_receipts",
            "direct",
            "operator-control:inspect:live-external-orchestration",
        ),
        _operator_control(
            "resume",
            "safe_checkpoint_with_matching_idempotency_key",
            "drafted",
            "operator-control:resume:live-external-orchestration",
        ),
        _operator_control(
            "retry",
            "failed_idempotent_step_only",
            "drafted",
            "operator-control:retry:live-external-orchestration",
        ),
        _operator_control(
            "branch",
            "uncertain_side_effect_or_provider_window",
            "drafted",
            "operator-control:branch:live-external-orchestration",
        ),
        _operator_control(
            "cancel",
            "unsafe_or_duplicative_recovery_path",
            "direct",
            "operator-control:cancel:live-external-orchestration",
        ),
        _operator_control(
            "audit",
            "completed_unacknowledged_side_effect",
            "direct",
            "operator-control:audit:live-external-orchestration",
        ),
    ]


def build_live_external_orchestration_contract() -> dict[str, Any]:
    providers = external_scheduler_attestation_receipts()
    crash_studies = crash_recovery_study_receipts()
    controls = operator_recovery_receipts()
    policy = live_external_orchestration_policy_payload()
    evidence_modes = sorted({
        str(item["evidence_mode"])
        for item in [*providers, *crash_studies]
        if item.get("evidence_mode")
    })
    return {
        "summary": {
            "operator_status": "live_external_orchestration_receipts_visible",
            "provider_receipt_count": len(providers),
            "crash_study_count": len(crash_studies),
            "operator_control_count": len(controls),
            "recorded_live_receipt_count": sum(
                1
                for item in [*providers, *crash_studies]
                if item.get("evidence_mode") == "recorded_live_fixture"
            ),
            "deterministic_contract_count": sum(
                1
                for item in [*providers, *crash_studies]
                if item.get("evidence_mode") == "deterministic_contract"
            ),
            "side_effect_boundary_count": sum(1 for item in providers if item.get("side_effect_boundary")),
            "replay_suppression_count": sum(1 for item in crash_studies if item.get("replay_suppression")),
            "required_controls_visible": {"inspect", "resume", "retry", "branch", "cancel", "audit"}
            <= {item["action"] for item in controls},
            "evidence_modes": evidence_modes,
            "claim_boundary": LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY,
        },
        "provider_attestation_receipts": providers,
        "crash_recovery_study_receipts": crash_studies,
        "operator_recovery_receipts": controls,
        "policy": policy,
    }


def _operator_control(action: str, target: str, mode: str, receipt_after_action: str) -> dict[str, Any]:
    return {
        "action": action,
        "target": target,
        "mode": mode,
        "enabled": True,
        "requires_approval_or_review": action in {"resume", "retry", "branch", "audit"},
        "receipt_after_action": receipt_after_action,
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Live orchestration scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_live_external_orchestration_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        LIVE_EXTERNAL_ORCHESTRATION_SUITE_NAME,
        ORCHESTRATION_CRASH_RECOVERY_STUDY_SUITE_NAME,
    ])


async def build_live_external_orchestration_report() -> dict[str, Any]:
    summary = await _run_live_external_orchestration_suites()
    contract = build_live_external_orchestration_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "live_external_orchestration_ci_gated_operator_visible"
                if healthy
                else "live_external_orchestration_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES)
                + len(ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            LIVE_EXTERNAL_ORCHESTRATION_SUITE_NAME: list(LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES),
            ORCHESTRATION_CRASH_RECOVERY_STUDY_SUITE_NAME: list(
                ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name=LIVE_EXTERNAL_ORCHESTRATION_SUITE_NAME),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
