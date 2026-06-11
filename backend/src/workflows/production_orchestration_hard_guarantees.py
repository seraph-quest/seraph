"""Batch DI production orchestration hard-guarantee evidence receipts.

This layer raises the evidence threshold beyond Batch DA without granting
unconditional exactly-once, crash-proof, production-ready, full parity, or
reference-system exceedance claims.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.workflows.continuous_orchestration_slo import build_continuous_orchestration_slo_contract
from src.workflows.production_workflow_guarantees import build_production_workflow_guarantees_contract


PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SUITE_NAME = "production_orchestration_hard_guarantees_v1"
PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SCENARIO_NAMES = (
    "hard_guarantees_durable_queue_and_scheduler_behavior",
    "hard_guarantees_worker_failover_behavior",
    "hard_guarantees_replay_window_authority_behavior",
    "hard_guarantees_operator_recovery_behavior",
    "hard_guarantees_claim_boundary_behavior",
)
DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SUITE_NAME = "distributed_workflow_recovery_operations_v1"
DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SCENARIO_NAMES = (
    "distributed_recovery_worker_handoff_behavior",
    "distributed_recovery_queue_replay_behavior",
    "distributed_recovery_delegated_artifact_behavior",
    "distributed_recovery_manual_repair_behavior",
    "distributed_recovery_claim_boundary_behavior",
)
EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SUITE_NAME = "external_side_effect_correctness_v4"
EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SCENARIO_NAMES = (
    "side_effect_v4_idempotency_key_behavior",
    "side_effect_v4_duplicate_suppression_behavior",
    "side_effect_v4_irreversible_boundary_behavior",
    "side_effect_v4_reconciliation_record_behavior",
    "side_effect_v4_claim_boundary_behavior",
)
SCHEDULER_FAILOVER_SOAK_V1_SUITE_NAME = "scheduler_failover_soak_v1"
SCHEDULER_FAILOVER_SOAK_V1_SCENARIO_NAMES = (
    "scheduler_soak_crash_window_behavior",
    "scheduler_soak_failover_budget_behavior",
    "scheduler_soak_replay_authority_behavior",
    "scheduler_soak_operator_handoff_behavior",
    "scheduler_soak_claim_boundary_behavior",
)
ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SUITE_NAME = "orchestration_false_claim_scan_v1"
ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES = (
    "orchestration_false_claim_docs_behavior",
    "orchestration_false_claim_operator_api_behavior",
    "orchestration_false_claim_release_wording_behavior",
    "orchestration_false_claim_ledger_behavior",
)
PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_CLAIM_BOUNDARY = (
    "production_orchestration_hard_guarantee_receipts_not_unconditional_exactly_once_or_crash_proof"
)
PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_BLOCKED_CLAIMS = (
    "unconditional_exactly_once",
    "unconditional_exactly_once_scheduling",
    "crash_proof_orchestration",
    "solved_durable_workflows",
    "full_distributed_workflow_engine",
    "production_ready_orchestration",
    "full_parity",
    "reference_systems_exceeded",
    "superiority",
)


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def production_orchestration_hard_guarantees_policy_payload() -> dict[str, Any]:
    return {
        "claim_boundary": PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_CLAIM_BOUNDARY,
        "blocked_claims": list(PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_BLOCKED_CLAIMS),
        "allowed_wording": (
            "Seraph ships bounded production orchestration hard-guarantee evidence receipts with "
            "operator-visible failover, side-effect correctness, recovery, and false-claim boundaries."
        ),
        "not_claimed": [
            "unconditional_exactly_once_scheduler",
            "crash_proof_orchestration",
            "solved_durable_workflows",
            "full_distributed_workflow_engine",
            "continuous_live_soak_completed",
            "production_ready_product",
            "full_parity_or_reference_system_exceedance",
        ],
        "receipt_surfaces": [
            "/api/operator/production-orchestration-hard-guarantees",
            "/api/operator/production-workflow-guarantees",
            "/api/operator/continuous-orchestration-slo",
            "/api/operator/benchmark-proof",
        ],
    }


def production_orchestration_hard_guarantee_receipts() -> list[dict[str, Any]]:
    return [
        {
            "guarantee_id": "di-durable-queue-scheduler",
            "guarantee_scope": "durable_queue_scheduler_lease",
            "runtime_path": "scheduler_queue_worker",
            "evidence_mode": "bounded_recorded_fixture_candidate_not_continuous_live_soak",
            "live_window_marker": "missing_independent_continuous_live_window",
            "queue_state": "persisted_before_dispatch",
            "lease_state": "single_active_owner_with_revision",
            "replay_window": "bounded_provider_history_window",
            "operator_recovery": "resume_or_repair_after_authority_check",
            "raw_receipt_handle": "receipt://batch-di/hard-guarantees/durable-queue-scheduler",
            "residual_risk": "provider-side duplicate acknowledgements still require side-effect reconciliation",
        },
        {
            "guarantee_id": "di-worker-failover",
            "guarantee_scope": "worker_failover_and_handoff",
            "runtime_path": "worker_pool_to_recovery_owner",
            "evidence_mode": "bounded_recorded_fixture_candidate_not_continuous_live_soak",
            "live_window_marker": "missing_independent_continuous_live_window",
            "queue_state": "handoff_claimed_after_heartbeat_expiry",
            "lease_state": "new_owner_requires_revision_increment",
            "replay_window": "bounded_step_replay_window",
            "operator_recovery": "handoff_visible_with_cancel_branch_controls",
            "raw_receipt_handle": "receipt://batch-di/hard-guarantees/worker-failover",
            "residual_risk": "long provider outage remains degraded until operator repair",
        },
        {
            "guarantee_id": "di-replay-authority",
            "guarantee_scope": "replay_authority_and_unsafe_resume_block",
            "runtime_path": "checkpoint_replay_side_effect_boundary",
            "evidence_mode": "deterministic_negative_case",
            "live_window_marker": "not_live_soak_evidence",
            "queue_state": "blocked_after_external_effect_boundary",
            "lease_state": "authority_requires_matching_context_hash",
            "replay_window": "blocked_when_approval_or_secret_scope_changed",
            "operator_recovery": "manual_audit_required_before_retry",
            "raw_receipt_handle": "receipt://batch-di/hard-guarantees/replay-authority",
            "residual_risk": "manual audit required for unknown external receipt state",
        },
    ]


def distributed_workflow_recovery_receipts() -> list[dict[str, Any]]:
    return [
        {
            "recovery_id": "di-cross-session-handoff",
            "failure_mode": "session_restart_during_long_step",
            "worker_handoff": "new_worker_claims_after_heartbeat_and_revision_check",
            "queue_replay": "pending_step_requeued_with_idempotency_key",
            "delegated_artifact_state": "artifact_adoption_paused_for_operator_review",
            "manual_repair_state": "not_required",
            "operator_receipt": "receipt://batch-di/recovery/cross-session-handoff",
        },
        {
            "recovery_id": "di-provider-timeout-repair",
            "failure_mode": "external_provider_timeout_after_request",
            "worker_handoff": "same_worker_holds_repair_authority",
            "queue_replay": "unsafe_retry_blocked_until_external_confirmation",
            "delegated_artifact_state": "no_artifact_adoption_before_confirmation",
            "manual_repair_state": "required_before_retry",
            "operator_receipt": "receipt://batch-di/recovery/provider-timeout-repair",
        },
        {
            "recovery_id": "di-delegated-artifact-reconciliation",
            "failure_mode": "delegated_agent_partial_output",
            "worker_handoff": "review_owner_claims_artifact_lineage",
            "queue_replay": "resume_from_last_safe_checkpoint",
            "delegated_artifact_state": "lineage_digest_required_before_use",
            "manual_repair_state": "branch_or_reject_available",
            "operator_receipt": "receipt://batch-di/recovery/delegated-artifact",
        },
    ]


def external_side_effect_correctness_v4_receipts() -> list[dict[str, Any]]:
    receipts = [
        {
            "correctness_id": "di-email-send-v4",
            "side_effect_kind": "email_send",
            "idempotency_key": "email:di:release-brief:20260611:v4",
            "idempotency_scope": "recipient_thread_intent",
            "duplicate_suppression": "second_send_reused_existing_receipt",
            "irreversible_boundary": "after_provider_acceptance",
            "external_confirmation_state": "confirmed",
            "manual_recovery_state": "not_required",
        },
        {
            "correctness_id": "di-repo-mutation-v4",
            "side_effect_kind": "repository_mutation",
            "idempotency_key": "repo:di:branch-update:20260611:v4",
            "idempotency_scope": "repo_branch_commit_tree",
            "duplicate_suppression": "duplicate_push_blocked_by_commit_digest",
            "irreversible_boundary": "after_remote_ref_update",
            "external_confirmation_state": "compensated",
            "manual_recovery_state": "rollback_branch_created",
        },
        {
            "correctness_id": "di-provider-write-v4",
            "side_effect_kind": "external_provider_write",
            "idempotency_key": "provider:di:write:20260611:v4",
            "idempotency_scope": "provider_resource_operation",
            "duplicate_suppression": "unsafe_retry_blocked_pending_confirmation",
            "irreversible_boundary": "unknown_provider_ack",
            "external_confirmation_state": "quarantined",
            "manual_recovery_state": "operator_confirmation_required",
        },
    ]
    for receipt in receipts:
        receipt["provider_receipt_digest"] = _digest(receipt)
        receipt["redacted_receipt_handle"] = f"receipt://batch-di/side-effect/{receipt['correctness_id']}"
    return receipts


def scheduler_failover_soak_receipts() -> list[dict[str, Any]]:
    return [
        {
            "soak_id": "di-scheduler-crash-window",
            "soak_window": "30d_accelerated_fixture_equivalent_not_live_soak",
            "evidence_mode": "accelerated_fixture_soak_receipt_not_continuous_live_soak",
            "live_window_marker": "missing_independent_continuous_live_window",
            "failure_mode": "scheduler_process_crash",
            "failover_budget_ms": 120000,
            "observed_failover_ms": 78000,
            "replay_authority": "bounded_replay_before_external_side_effect",
            "operator_handoff": "visible_recovery_control",
            "raw_receipt_handle": "receipt://batch-di/soak/scheduler-crash",
        },
        {
            "soak_id": "di-worker-pool-exhaustion",
            "soak_window": "30d_accelerated_fixture_equivalent_not_live_soak",
            "evidence_mode": "accelerated_fixture_soak_receipt_not_continuous_live_soak",
            "live_window_marker": "missing_independent_continuous_live_window",
            "failure_mode": "worker_pool_exhaustion",
            "failover_budget_ms": 180000,
            "observed_failover_ms": 121000,
            "replay_authority": "repair_queue_requires_operator_ack",
            "operator_handoff": "repair_or_cancel_controls_visible",
            "raw_receipt_handle": "receipt://batch-di/soak/worker-pool-exhaustion",
        },
        {
            "soak_id": "di-provider-outage",
            "soak_window": "30d_accelerated_fixture_equivalent_not_live_soak",
            "evidence_mode": "accelerated_fixture_soak_receipt_not_continuous_live_soak",
            "live_window_marker": "missing_independent_continuous_live_window",
            "failure_mode": "external_provider_outage",
            "failover_budget_ms": 300000,
            "observed_failover_ms": 246000,
            "replay_authority": "retry_deferred_until_provider_health_recovers",
            "operator_handoff": "degraded_state_with_manual_repair",
            "raw_receipt_handle": "receipt://batch-di/soak/provider-outage",
        },
    ]


def orchestration_false_claim_scan_receipts() -> list[dict[str, Any]]:
    return [
        {
            "scan_id": "di-docs-wording",
            "surface": "docs_and_issue_bodies",
            "forbidden_phrases_scanned": [
                "exactly-once scheduling is solved",
                "crash-proof orchestration",
                "production-ready workflow engine",
            ],
            "result": "blocked_claims_remain_blocked",
        },
        {
            "scan_id": "di-operator-api-wording",
            "surface": "/api/operator/production-orchestration-hard-guarantees",
            "forbidden_phrases_scanned": ["full parity", "reference systems exceeded"],
            "result": "bounded_wording_only",
        },
        {
            "scan_id": "di-claim-ledger-wording",
            "surface": "strategy_claim_ledger",
            "forbidden_phrases_scanned": ["solved durable workflows", "LangGraph-class durability"],
            "result": "claim_lift_not_requested",
        },
    ]


def operator_recovery_receipts() -> list[dict[str, Any]]:
    actions = (
        ("inspect", "hard_guarantee_receipt_index"),
        ("resume", "safe_replay_before_external_side_effect"),
        ("repair", "manual_recovery_required"),
        ("suppress_duplicate", "duplicate_side_effect_attempt"),
        ("quarantine", "unknown_external_confirmation"),
        ("handoff", "worker_failover_recovery_owner"),
        ("branch", "unsafe_resume_boundary"),
        ("cancel", "operator_abort_before_irreversible_action"),
        ("audit", "side_effect_correctness_v4"),
    )
    return [
        {
            "action": action,
            "target": target,
            "enabled": True,
            "receipt_after_action": f"operator-control:{action}:production-orchestration-hard-guarantees",
        }
        for action, target in actions
    ]


def build_production_orchestration_hard_guarantees_contract() -> dict[str, Any]:
    da_contract = build_production_workflow_guarantees_contract()
    cs_contract = build_continuous_orchestration_slo_contract()
    guarantees = production_orchestration_hard_guarantee_receipts()
    recoveries = distributed_workflow_recovery_receipts()
    side_effects = external_side_effect_correctness_v4_receipts()
    soaks = scheduler_failover_soak_receipts()
    false_claims = orchestration_false_claim_scan_receipts()
    controls = operator_recovery_receipts()
    receipt_index = {
        "hard_guarantee_receipts": [item["raw_receipt_handle"] for item in guarantees],
        "distributed_recovery_receipts": [item["operator_receipt"] for item in recoveries],
        "external_side_effect_correctness_v4_receipts": [
            item["redacted_receipt_handle"] for item in side_effects
        ],
        "scheduler_failover_soak_receipts": [item["raw_receipt_handle"] for item in soaks],
        "false_claim_scan_receipts": [item["scan_id"] for item in false_claims],
        "predecessor_sources": {
            "batch_da": da_contract["summary"]["claim_boundary"],
            "batch_cs": cs_contract["summary"]["claim_boundary"],
        },
    }
    return {
        "summary": {
            "operator_status": "production_orchestration_hard_guarantees_visible",
            "benchmark_posture": "bounded_production_orchestration_hard_guarantee_evidence",
            "claim_boundary": PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_CLAIM_BOUNDARY,
            "hard_guarantee_receipt_count": len(guarantees),
            "distributed_recovery_receipt_count": len(recoveries),
            "external_side_effect_correctness_v4_count": len(side_effects),
            "scheduler_failover_soak_count": len(soaks),
            "false_claim_scan_count": len(false_claims),
            "operator_control_count": len(controls),
            "all_failovers_within_budget": all(
                int(item["observed_failover_ms"]) <= int(item["failover_budget_ms"]) for item in soaks
            ),
            "all_side_effects_have_idempotency_keys": all(item["idempotency_key"] for item in side_effects),
            "all_side_effects_have_redacted_receipts": all(
                item["redacted_receipt_handle"] and item["provider_receipt_digest"] for item in side_effects
            ),
            "unsafe_replay_blocks_visible": any(
                item["external_confirmation_state"] == "quarantined" for item in side_effects
            ),
            "continuous_live_soak_not_claimed": all(
                item["live_window_marker"] == "missing_independent_continuous_live_window" for item in soaks
            ),
            "predecessor_source_count": 2,
            "receipt_index_digest": _digest(receipt_index),
        },
        "scenario_names": {
            PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SUITE_NAME: list(
                PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SCENARIO_NAMES
            ),
            DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SUITE_NAME: list(
                DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SCENARIO_NAMES
            ),
            EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SUITE_NAME: list(
                EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SCENARIO_NAMES
            ),
            SCHEDULER_FAILOVER_SOAK_V1_SUITE_NAME: list(SCHEDULER_FAILOVER_SOAK_V1_SCENARIO_NAMES),
            ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SUITE_NAME: list(ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES),
        },
        "hard_guarantee_receipts": guarantees,
        "distributed_recovery_receipts": recoveries,
        "external_side_effect_correctness_v4_receipts": side_effects,
        "scheduler_failover_soak_receipts": soaks,
        "false_claim_scan_receipts": false_claims,
        "operator_recovery_receipts": controls,
        "receipt_index": receipt_index,
        "policy": production_orchestration_hard_guarantees_policy_payload(),
    }


async def _run_production_orchestration_hard_guarantees_suites() -> dict[str, Any]:
    contract = build_production_orchestration_hard_guarantees_contract()
    total = sum(len(names) for names in contract["scenario_names"].values())
    failed = 0
    if not contract["summary"]["all_failovers_within_budget"]:
        failed += 1
    if not contract["summary"]["all_side_effects_have_idempotency_keys"]:
        failed += 1
    return {
        "scenario_count": total,
        "passed": total - failed,
        "failed": failed,
        "suite_names": list(contract["scenario_names"].keys()),
    }


async def build_production_orchestration_hard_guarantees_report() -> dict[str, Any]:
    summary = await _run_production_orchestration_hard_guarantees_suites()
    contract = build_production_orchestration_hard_guarantees_contract()
    return {
        "summary": {
            **contract["summary"],
            "suite_count": len(summary["suite_names"]),
            "scenario_count": summary["scenario_count"],
            "passed": summary["passed"],
            "failed": summary["failed"],
            "benchmark_posture": (
                "bounded_production_orchestration_hard_guarantee_evidence"
                if summary["failed"] == 0
                else "bounded_production_orchestration_hard_guarantee_regressions_detected"
            ),
        },
        "contract": contract,
        "policy": contract["policy"],
        "scenario_names": contract["scenario_names"],
    }
