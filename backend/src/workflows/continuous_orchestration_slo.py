"""Batch CS continuous orchestration SLO and recovery operations receipts.

This layer turns the earlier durable-kernel, recorded-live crash-study, and
production-SLA receipts into an operator-visible continuous operations contract.
It still blocks unconditional exactly-once, crash-proof, production-ready, full
parity, and reference-system exceedance claims.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


CONTINUOUS_ORCHESTRATION_SLO_SUITE_NAME = "continuous_orchestration_slo_monitor"
CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES = (
    "continuous_orchestration_monitor_window_behavior",
    "continuous_orchestration_scheduler_health_behavior",
    "continuous_orchestration_retry_jitter_budget_behavior",
    "continuous_orchestration_operator_recovery_behavior",
    "continuous_orchestration_claim_boundary_behavior",
)
CRASH_FAILOVER_SOAK_SUITE_NAME = "crash_failover_soak_v1"
CRASH_FAILOVER_SOAK_SCENARIO_NAMES = (
    "crash_failover_soak_window_behavior",
    "crash_failover_replay_authority_behavior",
    "crash_failover_operator_handoff_behavior",
    "crash_failover_residual_uncertainty_behavior",
)
SIDE_EFFECT_RECONCILIATION_V2_SUITE_NAME = "side_effect_reconciliation_v2"
SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES = (
    "side_effect_reconciliation_idempotency_behavior",
    "side_effect_reconciliation_duplicate_suppression_behavior",
    "side_effect_reconciliation_manual_recovery_behavior",
    "side_effect_reconciliation_irreversible_boundary_behavior",
)
CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY = (
    "continuous_orchestration_slo_receipts_not_unconditional_exactly_once_or_crash_proof_engine"
)
CONTINUOUS_ORCHESTRATION_SLO_BLOCKED_CLAIMS = (
    "unconditional_exactly_once",
    "crash_proof_orchestration",
    "full_distributed_workflow_engine",
    "production_ready_orchestration",
    "full_parity",
    "reference_systems_exceeded",
)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class ContinuousOrchestrationSloRuntime:
    """Small deterministic runtime ledger for monitor, failover, and reconciliation events."""

    def __init__(self) -> None:
        self._monitor_samples: list[dict[str, Any]] = []
        self._crash_failover_receipts: list[dict[str, Any]] = []
        self._side_effect_receipts: list[dict[str, Any]] = []

    def record_monitor_sample(self, sample: dict[str, Any]) -> dict[str, Any]:
        receipt = {
            **sample,
            "runtime_receipt_id": sample.get("monitor_id") or f"monitor:{len(self._monitor_samples) + 1}",
            "within_budget": int(sample.get("observed_fire_count") or 0) >= int(sample.get("expected_fire_count") or 0)
            and int(sample.get("max_jitter_ms") or 0) <= int(sample.get("jitter_budget_ms") or 0),
            "needs_operator_recovery": sample.get("operator_recovery_state") not in {
                "no_manual_recovery_required",
                "automatic_reconciliation_clean",
                "provider_delay_visible_no_duplicate_side_effect",
            },
        }
        self._monitor_samples.append(receipt)
        return receipt

    def record_crash_failover(self, receipt: dict[str, Any]) -> dict[str, Any]:
        runtime_receipt = {
            **receipt,
            "runtime_receipt_id": receipt.get("soak_id") or f"failover:{len(self._crash_failover_receipts) + 1}",
            "within_budget": int(receipt.get("failover_latency_ms") or 0)
            <= int(receipt.get("failover_budget_ms") or 0),
            "requires_manual_recovery": "manual" in str(receipt.get("operator_recovery_state") or "")
            or "manual" in str(receipt.get("replay_authority") or ""),
        }
        self._crash_failover_receipts.append(runtime_receipt)
        return runtime_receipt

    def record_side_effect_reconciliation(self, receipt: dict[str, Any]) -> dict[str, Any]:
        runtime_receipt = {
            **receipt,
            "runtime_receipt_id": receipt.get("reconciliation_id")
            or f"reconciliation:{len(self._side_effect_receipts) + 1}",
            "duplicate_safe": receipt.get("duplicate_suppression_state")
            in {"suppressed", "suppressed_with_existing_artifact_reuse", "blocked"},
            "manual_recovery_required": "required" in str(receipt.get("manual_recovery_state") or ""),
        }
        self._side_effect_receipts.append(runtime_receipt)
        return runtime_receipt

    def snapshot(self) -> dict[str, Any]:
        budget_breaches = [
            item["runtime_receipt_id"]
            for item in [*self._monitor_samples, *self._crash_failover_receipts]
            if not item.get("within_budget")
        ]
        recovery_queue = [
            item["runtime_receipt_id"]
            for item in [*self._monitor_samples, *self._crash_failover_receipts, *self._side_effect_receipts]
            if item.get("needs_operator_recovery")
            or item.get("requires_manual_recovery")
            or item.get("manual_recovery_required")
        ]
        duplicate_risks = [
            item["runtime_receipt_id"]
            for item in self._side_effect_receipts
            if not item.get("duplicate_safe")
        ]
        receipt_index = {
            "monitor_samples": self._monitor_samples,
            "crash_failover_receipts": self._crash_failover_receipts,
            "side_effect_reconciliation_receipts": self._side_effect_receipts,
        }
        return {
            "runtime_status": "continuous_orchestration_runtime_ledger_visible",
            "runtime_observation_count": (
                len(self._monitor_samples) + len(self._crash_failover_receipts) + len(self._side_effect_receipts)
            ),
            "active_budget_breach_count": len(budget_breaches),
            "active_recovery_queue_count": len(recovery_queue),
            "active_duplicate_risk_count": len(duplicate_risks),
            "budget_breaches": budget_breaches,
            "operator_recovery_queue": recovery_queue,
            "duplicate_risks": duplicate_risks,
            "receipt_index": receipt_index,
            "runtime_receipt_digest": _stable_digest(receipt_index),
            "claim_boundary": CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY,
        }


def continuous_orchestration_slo_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            CONTINUOUS_ORCHESTRATION_SLO_SUITE_NAME,
            CRASH_FAILOVER_SOAK_SUITE_NAME,
            SIDE_EFFECT_RECONCILIATION_V2_SUITE_NAME,
        ],
        "claim_boundary": CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY,
        "evidence_policy": (
            "continuous orchestration evidence must name monitor window, scheduler/provider health, "
            "retry and jitter budgets, crash/failover events, replay authority, idempotency scope, "
            "duplicate suppression state, irreversible side-effect boundaries, operator recovery state, "
            "and residual uncertainty before stronger workflow wording is allowed"
        ),
        "receipt_surfaces": [
            "/api/operator/continuous-orchestration-slo",
            "/api/operator/benchmark-proof",
            "/api/operator/production-sla-orchestration",
            "/api/operator/live-external-orchestration",
            "/api/operator/durable-workflow-engine-v2",
            "GitHub issue #523",
            "GitHub Project fields",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "blocked_claims": list(CONTINUOUS_ORCHESTRATION_SLO_BLOCKED_CLAIMS),
        "allowed_scoped_claims": [
            "continuous_slo_monitoring_for_recorded_windows",
            "operator_visible_crash_failover_recovery_for_drilled_modes",
            "side_effect_reconciliation_with_declared_idempotency_scope",
        ],
        "not_claimed": [
            "unconditional_exactly_once_scheduler",
            "crash_proof_workflow_engine",
            "full_distributed_workflow_engine",
            "production_ready_agent",
            "full_product_parity",
        ],
    }


def continuous_monitor_samples() -> list[dict[str, Any]]:
    return [
        {
            "monitor_id": "cs-monitor-temporal-rolling-72h",
            "provider": "temporal_cloud_recorded_live_fixture",
            "evidence_mode": "recorded_live_fixture",
            "monitor_window": "72h_rolling",
            "scheduler_health": "healthy",
            "expected_fire_count": 12,
            "observed_fire_count": 12,
            "retry_count": 1,
            "max_jitter_ms": 1840,
            "jitter_budget_ms": 5000,
            "replay_window": "24h_provider_history_window",
            "operator_recovery_state": "no_manual_recovery_required",
            "residual_uncertainty": "recorded_live_window_is_not_a_continuous_public_sla",
        },
        {
            "monitor_id": "cs-monitor-github-actions-14d",
            "provider": "github_actions_schedule_recorded_live_fixture",
            "evidence_mode": "recorded_live_fixture",
            "monitor_window": "14d_rolling",
            "scheduler_health": "degraded_but_within_budget",
            "expected_fire_count": 14,
            "observed_fire_count": 14,
            "retry_count": 2,
            "max_jitter_ms": 62000,
            "jitter_budget_ms": 180000,
            "replay_window": "90d_actions_run_history_window",
            "operator_recovery_state": "provider_delay_visible_no_duplicate_side_effect",
            "residual_uncertainty": "runner_capacity_and_provider_outages_are_not_eliminated",
        },
        {
            "monitor_id": "cs-monitor-local-scheduler-soak",
            "provider": "seraph_scheduler_contract",
            "evidence_mode": "deterministic_soak_fixture",
            "monitor_window": "30d_simulated_clock",
            "scheduler_health": "healthy",
            "expected_fire_count": 30,
            "observed_fire_count": 30,
            "retry_count": 0,
            "max_jitter_ms": 0,
            "jitter_budget_ms": 1000,
            "replay_window": "local_audit_history_window",
            "operator_recovery_state": "automatic_reconciliation_clean",
            "residual_uncertainty": "single_process_contract_not_external_distributed_sla",
        },
    ]


def crash_failover_soak_receipts() -> list[dict[str, Any]]:
    return [
        {
            "soak_id": "cs-soak-worker-kill-before-side-effect",
            "evidence_mode": "recorded_live_fixture",
            "monitor_window": "6h_failure_injection_window",
            "failure_event": "worker_process_kill_before_external_write",
            "failover_latency_ms": 1430,
            "failover_budget_ms": 5000,
            "replay_authority": "safe_checkpoint_resume_draft_requires_operator_review",
            "side_effect_state": "not_started",
            "operator_recovery_action": "resume",
            "operator_recovery_state": "drafted_and_receipted",
            "residual_uncertainty": "drill_covers_named_failure_mode_not_all_crashes",
        },
        {
            "soak_id": "cs-soak-lease-owner-timeout",
            "evidence_mode": "deterministic_soak_fixture",
            "monitor_window": "24h_simulated_lease_window",
            "failure_event": "lease_owner_heartbeat_timeout",
            "failover_latency_ms": 2400,
            "failover_budget_ms": 7000,
            "replay_authority": "new_owner_after_revision_guard_and_operator_receipt",
            "side_effect_state": "requires_receipt_reconciliation",
            "operator_recovery_action": "audit_then_resume",
            "operator_recovery_state": "manual_audit_required",
            "residual_uncertainty": "not_a_multi_region_consensus_protocol",
        },
        {
            "soak_id": "cs-soak-provider-ack-loss",
            "evidence_mode": "recorded_live_fixture",
            "monitor_window": "6h_failure_injection_window",
            "failure_event": "external_provider_ack_loss_after_write",
            "failover_latency_ms": 0,
            "failover_budget_ms": 0,
            "replay_authority": "manual_operator_audit_required_before_retry",
            "side_effect_state": "completed_unacknowledged",
            "operator_recovery_action": "audit_or_cancel",
            "operator_recovery_state": "unsafe_retry_blocked",
            "residual_uncertainty": "provider_ack_loss_requires_external_receipt_confirmation",
        },
    ]


def side_effect_reconciliation_receipts() -> list[dict[str, Any]]:
    return [
        {
            "reconciliation_id": "cs-reconcile-email-send",
            "side_effect_kind": "email_send",
            "idempotency_scope": "external_provider_message",
            "idempotency_key": "release-brief:email:20260610",
            "duplicate_attempt": "blocked_before_provider_call",
            "duplicate_suppression_state": "suppressed",
            "irreversible_boundary": "provider_message_id_recorded",
            "manual_recovery_state": "not_required",
            "operator_visible": True,
        },
        {
            "reconciliation_id": "cs-reconcile-repo-mutation",
            "side_effect_kind": "repository_mutation",
            "idempotency_scope": "branch_and_pull_request",
            "idempotency_key": "nightly-check:branch-pr:20260610",
            "duplicate_attempt": "converted_to_existing_branch_followup",
            "duplicate_suppression_state": "suppressed_with_existing_artifact_reuse",
            "irreversible_boundary": "branch_created_pr_requires_review",
            "manual_recovery_state": "compare_or_cancel_available",
            "operator_visible": True,
        },
        {
            "reconciliation_id": "cs-reconcile-provider-ack-loss",
            "side_effect_kind": "external_provider_write",
            "idempotency_scope": "side_effect_receipt",
            "idempotency_key": "provider-write:ack-loss:20260610",
            "duplicate_attempt": "blocked_pending_manual_audit",
            "duplicate_suppression_state": "blocked",
            "irreversible_boundary": "external_write_may_have_completed",
            "manual_recovery_state": "audit_required_before_retry",
            "operator_visible": True,
        },
    ]


def continuous_operator_controls() -> list[dict[str, Any]]:
    return [
        _operator_control("inspect", "continuous_monitor_samples", "direct"),
        _operator_control("audit", "side_effect_reconciliation_receipts", "direct"),
        _operator_control("resume", "safe_checkpoint_with_matching_replay_authority", "drafted"),
        _operator_control("repair", "missed_trigger_or_failover_budget_breach", "drafted"),
        _operator_control("suppress_duplicate", "duplicate_side_effect_attempt", "direct"),
        _operator_control("branch", "uncertain_irreversible_side_effect_boundary", "drafted"),
        _operator_control("cancel", "unsafe_replay_or_unbounded_failure_mode", "direct"),
    ]


def build_seeded_continuous_orchestration_runtime() -> ContinuousOrchestrationSloRuntime:
    runtime = ContinuousOrchestrationSloRuntime()
    for sample in continuous_monitor_samples():
        runtime.record_monitor_sample(sample)
    for receipt in crash_failover_soak_receipts():
        runtime.record_crash_failover(receipt)
    for receipt in side_effect_reconciliation_receipts():
        runtime.record_side_effect_reconciliation(receipt)
    return runtime


def build_continuous_orchestration_slo_contract() -> dict[str, Any]:
    monitors = continuous_monitor_samples()
    soaks = crash_failover_soak_receipts()
    reconciliations = side_effect_reconciliation_receipts()
    controls = continuous_operator_controls()
    runtime_snapshot = build_seeded_continuous_orchestration_runtime().snapshot()
    evidence_modes = sorted({
        str(item["evidence_mode"])
        for item in [*monitors, *soaks]
        if item.get("evidence_mode")
    })
    return {
        "summary": {
            "operator_status": "continuous_orchestration_slo_visible",
            "monitor_sample_count": len(monitors),
            "crash_failover_soak_count": len(soaks),
            "side_effect_reconciliation_count": len(reconciliations),
            "operator_control_count": len(controls),
            "recorded_live_receipt_count": sum(
                1 for item in [*monitors, *soaks] if item.get("evidence_mode") == "recorded_live_fixture"
            ),
            "deterministic_soak_count": sum(
                1 for item in [*monitors, *soaks] if item.get("evidence_mode") == "deterministic_soak_fixture"
            ),
            "evidence_modes": evidence_modes,
            "all_monitors_within_budget": all(
                int(item.get("observed_fire_count") or 0) >= int(item.get("expected_fire_count") or 0)
                and int(item.get("max_jitter_ms") or 0) <= int(item.get("jitter_budget_ms") or 0)
                for item in monitors
            ),
            "all_failovers_within_budget": all(
                int(item.get("failover_latency_ms") or 0) <= int(item.get("failover_budget_ms") or 0)
                for item in soaks
            ),
            "reconciliation_complete": all(
                item.get("duplicate_suppression_state") in {"suppressed", "suppressed_with_existing_artifact_reuse", "blocked"}
                and item.get("operator_visible") is True
                for item in reconciliations
            ),
            "required_controls_visible": {"inspect", "audit", "resume", "repair", "suppress_duplicate", "branch", "cancel"}
            <= {item["action"] for item in controls},
            "runtime_status": runtime_snapshot["runtime_status"],
            "runtime_observation_count": runtime_snapshot["runtime_observation_count"],
            "active_budget_breach_count": runtime_snapshot["active_budget_breach_count"],
            "active_recovery_queue_count": runtime_snapshot["active_recovery_queue_count"],
            "active_duplicate_risk_count": runtime_snapshot["active_duplicate_risk_count"],
            "runtime_receipt_digest": runtime_snapshot["runtime_receipt_digest"],
            "claim_boundary": CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY,
        },
        "monitor_samples": monitors,
        "crash_failover_soak_receipts": soaks,
        "side_effect_reconciliation_receipts": reconciliations,
        "operator_recovery_receipts": controls,
        "runtime_operations": runtime_snapshot,
        "policy": continuous_orchestration_slo_policy_payload(),
    }


def _operator_control(action: str, target: str, mode: str) -> dict[str, Any]:
    return {
        "action": action,
        "target": target,
        "mode": mode,
        "enabled": True,
        "requires_approval_or_review": action in {"audit", "resume", "repair", "branch"},
        "receipt_after_action": f"operator-control:{action}:continuous-orchestration-slo",
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Continuous orchestration SLO scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_continuous_orchestration_slo_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        CONTINUOUS_ORCHESTRATION_SLO_SUITE_NAME,
        CRASH_FAILOVER_SOAK_SUITE_NAME,
        SIDE_EFFECT_RECONCILIATION_V2_SUITE_NAME,
    ])


async def build_continuous_orchestration_slo_report() -> dict[str, Any]:
    summary = await _run_continuous_orchestration_slo_suites()
    contract = build_continuous_orchestration_slo_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "continuous_orchestration_slo_ci_gated_operator_visible"
                if healthy
                else "continuous_orchestration_slo_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES)
                + len(CRASH_FAILOVER_SOAK_SCENARIO_NAMES)
                + len(SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            CONTINUOUS_ORCHESTRATION_SLO_SUITE_NAME: list(CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES),
            CRASH_FAILOVER_SOAK_SUITE_NAME: list(CRASH_FAILOVER_SOAK_SCENARIO_NAMES),
            SIDE_EFFECT_RECONCILIATION_V2_SUITE_NAME: list(SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name=CONTINUOUS_ORCHESTRATION_SLO_SUITE_NAME),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
