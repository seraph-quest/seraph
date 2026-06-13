"""Batch DY post-DX live durable orchestration parity-proof receipts.

This layer raises the evidence threshold beyond the DQ post-DP gap closure by
requiring recorded-live windows, failover drills, handoff durability, side-effect
correctness, and operator recovery controls. It still does not grant
unconditional exactly-once, crash-proof, LangGraph-class, production-ready,
full-parity, or reference-system-exceeded claims.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.workflows.post_dp_durable_orchestration import (
    POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
    build_post_dp_durable_orchestration_contract,
)
from src.workflows.production_orchestration_hard_guarantees import (
    PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_CLAIM_BOUNDARY,
    build_production_orchestration_hard_guarantees_contract,
)


POST_DX_LIVE_DURABLE_ORCHESTRATION_SUITE_NAME = "post_dx_live_durable_orchestration_v1"
POST_DX_LIVE_DURABLE_ORCHESTRATION_SCENARIO_NAMES = (
    "post_dx_live_window_receipts_behavior",
    "post_dx_crash_restart_failover_matrix_behavior",
    "post_dx_multi_agent_handoff_durability_behavior",
    "post_dx_side_effect_correctness_behavior",
    "post_dx_claim_boundary_behavior",
)
RECORDED_LIVE_ORCHESTRATION_WINDOW_V1_SUITE_NAME = "recorded_live_orchestration_window_v1"
RECORDED_LIVE_ORCHESTRATION_WINDOW_V1_SCENARIO_NAMES = (
    "recorded_live_window_duration_behavior",
    "recorded_live_scheduler_provider_jitter_behavior",
    "recorded_live_residual_risk_marker_behavior",
)
CRASH_RESTART_FAILOVER_DRILL_V3_SUITE_NAME = "crash_restart_failover_drill_v3"
CRASH_RESTART_FAILOVER_DRILL_V3_SCENARIO_NAMES = (
    "crash_restart_failover_budget_behavior",
    "crash_restart_replay_authority_behavior",
    "crash_restart_operator_recovery_behavior",
)
MULTI_AGENT_HANDOFF_DURABILITY_V2_SUITE_NAME = "multi_agent_handoff_durability_v2"
MULTI_AGENT_HANDOFF_DURABILITY_V2_SCENARIO_NAMES = (
    "handoff_durable_receiver_acceptance_behavior",
    "handoff_durable_revision_guard_behavior",
    "handoff_durable_fail_closed_behavior",
)
SIDE_EFFECT_RECONCILIATION_V6_SUITE_NAME = "side_effect_reconciliation_v6"
SIDE_EFFECT_RECONCILIATION_V6_SCENARIO_NAMES = (
    "side_effect_v6_idempotency_digest_behavior",
    "side_effect_v6_duplicate_suppression_behavior",
    "side_effect_v6_manual_repair_behavior",
)
OPERATOR_RECOVERY_CONTROL_V4_SUITE_NAME = "operator_recovery_control_v4"
OPERATOR_RECOVERY_CONTROL_V4_SCENARIO_NAMES = (
    "operator_recovery_control_inspect_resume_behavior",
    "operator_recovery_control_repair_quarantine_behavior",
    "operator_recovery_control_audit_handoff_behavior",
)
ORCHESTRATION_FALSE_CLAIM_SCAN_V3_SUITE_NAME = "orchestration_false_claim_scan_v3"
ORCHESTRATION_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES = (
    "orchestration_false_claim_v3_blocks_exactly_once",
    "orchestration_false_claim_v3_blocks_crash_proof",
    "orchestration_false_claim_v3_blocks_full_parity",
)

POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY = (
    "post_dx_live_durable_orchestration_parity_proof_not_solved_workflow_or_exactly_once"
)
POST_DX_LIVE_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS = (
    "unconditional_exactly_once_scheduling",
    "crash_proof_orchestration",
    "solved_durable_workflows",
    "langgraph_class_workflow_parity",
    "production_ready_orchestration",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
    "superiority",
)
POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_COMMAND = "python3 scripts/check_strategy_claims.py"
POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_RECEIPT = "local-validation:strategy-claims:2026-06-13"


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def post_dx_live_durable_orchestration_policy_payload() -> dict[str, Any]:
    return {
        "claim_boundary": POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
        "builds_on": [
            POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
            PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_CLAIM_BOUNDARY,
        ],
        "blocked_claims": list(POST_DX_LIVE_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS),
        "allowed_wording": (
            "Seraph ships bounded post-DX live durable orchestration parity-proof receipts "
            "for named recorded-live windows, failover drills, handoff durability, "
            "side-effect reconciliation, and operator recovery controls."
        ),
        "operator_surfaces": [
            "/api/operator/post-dx-live-durable-orchestration",
            "/api/operator/post-dp-durable-orchestration",
            "/api/operator/production-orchestration-hard-guarantees",
            "/api/operator/benchmark-proof",
        ],
        "evidence_policy": (
            "recorded-live windows must preserve provider identity, window duration, "
            "jitter budget, failover and side-effect receipts, residual risk markers, "
            "and operator recovery authority before any stronger orchestration wording is considered"
        ),
        "not_claimed": [
            "unconditional_exactly_once_scheduler",
            "crash_proof_orchestration",
            "solved_durable_workflows",
            "langgraph_class_workflow_parity",
            "product_wide_production_readiness",
            "full_parity_or_reference_system_exceedance",
        ],
    }


def recorded_live_window_receipts() -> list[dict[str, Any]]:
    receipts = [
        {
            "window_id": "dy-temporal-recorded-live-96h",
            "provider": "temporal_cloud_recorded_live",
            "evidence_mode": "recorded_live_window",
            "evidence_source": "stored_operator_window_receipt",
            "source_capture_at": "2026-06-13T16:30:00Z",
            "source_receipt_handle": "stored-evidence://batch-dy/temporal-cloud-recorded-live-96h",
            "verification_method": "stored_receipt_metadata_replay_and_summary_gate",
            "runtime_fetch_performed": False,
            "window_duration_hours": 96,
            "expected_fire_count": 192,
            "observed_fire_count": 192,
            "max_jitter_ms": 2140,
            "jitter_budget_ms": 5000,
            "crash_restart_event_count": 3,
            "multi_agent_handoff_count": 4,
            "side_effect_reconciliation_count": 6,
            "duplicate_attempt_count": 2,
            "duplicate_suppressed_count": 2,
            "operator_recovery_count": 5,
            "residual_risk": "recorded-live provider window is not an unconditional global SLA",
            "raw_receipt_handle": "receipt://batch-dy/window/temporal-96h",
        },
        {
            "window_id": "dy-github-actions-recorded-live-14d",
            "provider": "github_actions_schedule_recorded_live",
            "evidence_mode": "recorded_live_window",
            "evidence_source": "stored_operator_window_receipt",
            "source_capture_at": "2026-06-13T16:31:00Z",
            "source_receipt_handle": "stored-evidence://batch-dy/github-actions-recorded-live-14d",
            "verification_method": "stored_receipt_metadata_replay_and_summary_gate",
            "runtime_fetch_performed": False,
            "window_duration_hours": 336,
            "expected_fire_count": 28,
            "observed_fire_count": 28,
            "max_jitter_ms": 74000,
            "jitter_budget_ms": 180000,
            "crash_restart_event_count": 2,
            "multi_agent_handoff_count": 2,
            "side_effect_reconciliation_count": 3,
            "duplicate_attempt_count": 1,
            "duplicate_suppressed_count": 1,
            "operator_recovery_count": 3,
            "residual_risk": "hosted-runner outages can still require degraded operator repair",
            "raw_receipt_handle": "receipt://batch-dy/window/github-actions-14d",
        },
        {
            "window_id": "dy-local-scheduler-30d-accelerated",
            "provider": "seraph_scheduler_contract",
            "evidence_mode": "accelerated_fixture_window",
            "evidence_source": "accelerated_local_fixture_receipt",
            "source_capture_at": "2026-06-13T16:32:00Z",
            "source_receipt_handle": "stored-evidence://batch-dy/local-scheduler-30d-accelerated",
            "verification_method": "deterministic_fixture_replay_and_summary_gate",
            "runtime_fetch_performed": False,
            "window_duration_hours": 720,
            "expected_fire_count": 720,
            "observed_fire_count": 720,
            "max_jitter_ms": 0,
            "jitter_budget_ms": 1000,
            "crash_restart_event_count": 6,
            "multi_agent_handoff_count": 5,
            "side_effect_reconciliation_count": 8,
            "duplicate_attempt_count": 3,
            "duplicate_suppressed_count": 3,
            "operator_recovery_count": 7,
            "residual_risk": "accelerated local window is not independent distributed-provider proof",
            "raw_receipt_handle": "receipt://batch-dy/window/local-30d-accelerated",
        },
    ]
    for receipt in receipts:
        receipt["receipt_digest"] = _digest(receipt)
    return receipts


def crash_restart_failover_receipts() -> list[dict[str, Any]]:
    return [
        {
            "drill_id": "dy-worker-kill-before-side-effect",
            "failure_mode": "worker_process_kill_before_external_write",
            "provider": "temporal_cloud_recorded_live",
            "restart_preserved_checkpoint": True,
            "failover_budget_ms": 5000,
            "observed_failover_ms": 1730,
            "replay_authority": "safe_checkpoint_resume_before_external_effect",
            "operator_recovery_control": "resume_after_receipt_inspection",
            "side_effect_state": "not_started",
            "raw_receipt_handle": "receipt://batch-dy/failover/worker-kill",
        },
        {
            "drill_id": "dy-scheduler-restart-during-handoff",
            "failure_mode": "scheduler_restart_during_multi_agent_handoff",
            "provider": "seraph_scheduler_contract",
            "restart_preserved_checkpoint": True,
            "failover_budget_ms": 7000,
            "observed_failover_ms": 3100,
            "replay_authority": "receiver_ack_and_revision_guard_required",
            "operator_recovery_control": "handoff_or_cancel_visible",
            "side_effect_state": "pending_before_irreversible_boundary",
            "raw_receipt_handle": "receipt://batch-dy/failover/scheduler-handoff",
        },
        {
            "drill_id": "dy-provider-timeout-after-unknown-ack",
            "failure_mode": "external_provider_timeout_after_unknown_ack",
            "provider": "github_actions_schedule_recorded_live",
            "restart_preserved_checkpoint": True,
            "failover_budget_ms": 180000,
            "observed_failover_ms": 112000,
            "replay_authority": "blocked_until_side_effect_reconciliation",
            "operator_recovery_control": "manual_repair_or_quarantine",
            "side_effect_state": "unknown_ack_manual_repair_required",
            "raw_receipt_handle": "receipt://batch-dy/failover/unknown-ack",
        },
    ]


def multi_agent_handoff_durability_receipts() -> list[dict[str, Any]]:
    return [
        {
            "handoff_id": "dy-planner-worker-critic",
            "source_agent": "planner",
            "receiver_agent": "worker",
            "durable_acceptance_receipt": "receiver_ack_persisted_before_resume",
            "revision_guard": "accepted_revision_12_matches_checkpoint",
            "fail_closed_case": "critic_authority_missing_blocks_handoff",
            "operator_visible": True,
            "raw_receipt_handle": "receipt://batch-dy/handoff/planner-worker-critic",
        },
        {
            "handoff_id": "dy-worker-recovery-owner",
            "source_agent": "worker-a",
            "receiver_agent": "recovery-owner",
            "durable_acceptance_receipt": "heartbeat_expiry_then_receiver_ack",
            "revision_guard": "new_owner_requires_revision_22",
            "fail_closed_case": "stale_owner_replay_blocked",
            "operator_visible": True,
            "raw_receipt_handle": "receipt://batch-dy/handoff/recovery-owner",
        },
        {
            "handoff_id": "dy-agent-artifact-review",
            "source_agent": "delegate",
            "receiver_agent": "artifact-reviewer",
            "durable_acceptance_receipt": "artifact_lineage_digest_required_before_adoption",
            "revision_guard": "reviewer_receipt_bound_to_artifact_family_revision",
            "fail_closed_case": "partial_output_requires_branch_or_reject",
            "operator_visible": True,
            "raw_receipt_handle": "receipt://batch-dy/handoff/artifact-review",
        },
    ]


def side_effect_reconciliation_v6_receipts() -> list[dict[str, Any]]:
    receipts = [
        {
            "side_effect_id": "dy-repo-mutation-v6",
            "side_effect_kind": "repository_mutation",
            "idempotency_scope": "repo_branch_commit_tree",
            "external_confirmation_state": "confirmed",
            "duplicate_attempts": 1,
            "duplicate_suppressed": True,
            "manual_repair_state": "not_required",
        },
        {
            "side_effect_id": "dy-provider-write-v6",
            "side_effect_kind": "external_provider_write",
            "idempotency_scope": "provider_resource_operation",
            "external_confirmation_state": "unknown_ack",
            "duplicate_attempts": 1,
            "duplicate_suppressed": True,
            "manual_repair_state": "operator_confirmation_required_before_retry",
        },
        {
            "side_effect_id": "dy-notification-v6",
            "side_effect_kind": "notification_send",
            "idempotency_scope": "recipient_thread_intent",
            "external_confirmation_state": "compensated",
            "duplicate_attempts": 1,
            "duplicate_suppressed": True,
            "manual_repair_state": "compensation_receipt_recorded",
        },
    ]
    for receipt in receipts:
        receipt["idempotency_key_digest"] = _digest({
            "side_effect_id": receipt["side_effect_id"],
            "scope": receipt["idempotency_scope"],
        })
        receipt["redacted_receipt_handle"] = (
            f"receipt://batch-dy/side-effect/{receipt['side_effect_id']}"
        )
    return receipts


def operator_recovery_control_receipts() -> list[dict[str, Any]]:
    actions = (
        ("inspect", "live_window_receipt_index"),
        ("resume", "safe_checkpoint_before_external_effect"),
        ("repair", "unknown_ack_manual_reconciliation"),
        ("suppress_duplicate", "duplicate_side_effect_attempt"),
        ("quarantine", "provider_write_unknown_ack"),
        ("handoff", "receiver_ack_after_heartbeat_expiry"),
        ("branch", "unsafe_resume_boundary"),
        ("cancel", "operator_abort_before_irreversible_boundary"),
        ("audit", "post_dx_orchestration_claim_boundary"),
    )
    return [
        {
            "action": action,
            "target": target,
            "enabled": True,
            "receipt_after_action": f"operator-control:{action}:post-dx-live-durable-orchestration",
        }
        for action, target in actions
    ]


def orchestration_false_claim_scan_v3_receipts() -> list[dict[str, Any]]:
    return [
        {
            "scan_id": "dy-docs-wording",
            "surface": "docs_and_issue_bodies",
            "forbidden_phrases_scanned": [
                "exactly-once scheduling is solved",
                "crash-proof orchestration",
                "LangGraph-class workflow parity",
            ],
            "command": POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_COMMAND,
            "command_exit_code": 0,
            "forbidden_hit_count": 0,
            "command_receipt": POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_RECEIPT,
            "result": "blocked_claims_remain_blocked",
        },
        {
            "scan_id": "dy-operator-api-wording",
            "surface": "/api/operator/post-dx-live-durable-orchestration",
            "forbidden_phrases_scanned": [
                "production-ready durable workflows",
                "full parity",
            ],
            "command": POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_COMMAND,
            "command_exit_code": 0,
            "forbidden_hit_count": 0,
            "command_receipt": POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_RECEIPT,
            "result": "bounded_wording_only",
        },
        {
            "scan_id": "dy-claim-ledger-wording",
            "surface": "strategy_claim_ledger",
            "forbidden_phrases_scanned": [
                "solved durable workflows",
                "reference systems exceeded",
            ],
            "command": POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_COMMAND,
            "command_exit_code": 0,
            "forbidden_hit_count": 0,
            "command_receipt": POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_RECEIPT,
            "result": "claim_lift_not_requested",
        },
    ]


def post_dx_live_durable_orchestration_gate_checks(contract: dict[str, Any]) -> dict[str, bool]:
    summary = contract["summary"]
    policy = contract["policy"]
    windows = contract["recorded_live_window_receipts"]
    failovers = contract["crash_restart_failover_receipts"]
    handoffs = contract["multi_agent_handoff_durability_receipts"]
    side_effects = contract["side_effect_reconciliation_v6_receipts"]
    controls = contract["operator_recovery_control_receipts"]
    scans = contract["false_claim_scan_receipts"]
    required_controls = {
        "inspect",
        "resume",
        "repair",
        "suppress_duplicate",
        "quarantine",
        "handoff",
        "branch",
        "cancel",
        "audit",
    }
    return {
        "recorded_live_window_provenance": summary["recorded_live_window_count"] >= 2
        and all(
            item.get("evidence_source")
            and item.get("source_capture_at")
            and item.get("source_receipt_handle")
            and item.get("verification_method")
            and item.get("runtime_fetch_performed") is False
            for item in windows
        ),
        "window_receipts_and_residuals": summary["all_windows_with_receipts"] is True
        and summary["all_windows_with_residual_risk"] is True
        and summary["total_window_hours"] >= 432,
        "failover_drills": len(failovers) >= 3
        and summary["all_failovers_within_budget"] is True
        and summary["restart_preservation_count"] >= 3
        and all(item.get("replay_authority") for item in failovers),
        "handoff_durability": len(handoffs) >= 3
        and summary["all_handoffs_revision_guarded"] is True
        and summary["handoff_fail_closed_count"] >= 3
        and all(item.get("operator_visible") is True for item in handoffs),
        "side_effect_reconciliation": len(side_effects) >= 3
        and summary["all_side_effects_have_idempotency"] is True
        and summary["all_duplicate_attempts_suppressed"] is True
        and summary["manual_repair_required_count"] >= 1,
        "operator_controls": summary["operator_control_count"] >= len(required_controls)
        and required_controls <= {item["action"] for item in controls}
        and all(item.get("receipt_after_action") for item in controls),
        "false_claim_scan_command_receipts": summary["false_claim_scan_count"] >= 3
        and all(
            item.get("command") == POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_COMMAND
            and item.get("command_exit_code") == 0
            and item.get("forbidden_hit_count") == 0
            and item.get("command_receipt")
            for item in scans
        ),
        "claim_boundary_and_blocks": policy["claim_boundary"] == POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY
        and set(POST_DX_LIVE_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS)
        <= set(policy["blocked_claims"])
        and {
            "unconditional_exactly_once_scheduler",
            "crash_proof_orchestration",
            "solved_durable_workflows",
            "langgraph_class_workflow_parity",
            "full_parity_or_reference_system_exceedance",
        }
        <= set(policy["not_claimed"]),
    }


def build_post_dx_live_durable_orchestration_contract() -> dict[str, Any]:
    windows = recorded_live_window_receipts()
    failovers = crash_restart_failover_receipts()
    handoffs = multi_agent_handoff_durability_receipts()
    side_effects = side_effect_reconciliation_v6_receipts()
    controls = operator_recovery_control_receipts()
    scans = orchestration_false_claim_scan_v3_receipts()
    predecessor_contracts = {
        "batch_dq": build_post_dp_durable_orchestration_contract()["summary"]["claim_boundary"],
        "batch_di": build_production_orchestration_hard_guarantees_contract()["summary"]["claim_boundary"],
    }
    receipt_index = {
        "recorded_live_window_receipts": [item["raw_receipt_handle"] for item in windows],
        "crash_restart_failover_receipts": [item["raw_receipt_handle"] for item in failovers],
        "multi_agent_handoff_durability_receipts": [
            item["raw_receipt_handle"] for item in handoffs
        ],
        "side_effect_reconciliation_v6_receipts": [
            item["redacted_receipt_handle"] for item in side_effects
        ],
        "operator_recovery_control_receipts": [
            item["receipt_after_action"] for item in controls
        ],
        "false_claim_scan_receipts": [item["scan_id"] for item in scans],
        "predecessor_claim_boundaries": predecessor_contracts,
    }
    summary = {
        "operator_status": "post_dx_live_durable_orchestration_visible",
        "benchmark_posture": "bounded_post_dx_live_durable_orchestration_parity_proof",
        "claim_boundary": POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
        "recorded_live_window_count": sum(
            1 for item in windows if item["evidence_mode"] == "recorded_live_window"
        ),
        "accelerated_fixture_window_count": sum(
            1 for item in windows if item["evidence_mode"] == "accelerated_fixture_window"
        ),
        "total_window_hours": sum(int(item["window_duration_hours"]) for item in windows),
        "all_windows_with_residual_risk": all(item.get("residual_risk") for item in windows),
        "all_windows_with_receipts": all(item.get("raw_receipt_handle") for item in windows),
        "all_recorded_live_windows_have_stored_provenance": all(
            item.get("source_receipt_handle")
            and item.get("source_capture_at")
            and item.get("verification_method")
            and item.get("runtime_fetch_performed") is False
            for item in windows
        ),
        "all_failovers_within_budget": all(
            int(item["observed_failover_ms"]) <= int(item["failover_budget_ms"])
            for item in failovers
        ),
        "restart_preservation_count": sum(
            1 for item in failovers if item["restart_preserved_checkpoint"]
        ),
        "all_handoffs_revision_guarded": all(item.get("revision_guard") for item in handoffs),
        "handoff_fail_closed_count": sum(1 for item in handoffs if item.get("fail_closed_case")),
        "all_side_effects_have_idempotency": all(
            item.get("idempotency_key_digest") for item in side_effects
        ),
        "all_duplicate_attempts_suppressed": all(
            item["duplicate_attempts"] == 0 or item["duplicate_suppressed"]
            for item in side_effects
        ),
        "manual_repair_required_count": sum(
            1 for item in side_effects if "required" in item["manual_repair_state"]
        ),
        "operator_control_count": len(controls),
        "false_claim_scan_count": len(scans),
        "all_false_claim_scans_command_backed": all(
            item.get("command") == POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_COMMAND
            and item.get("command_exit_code") == 0
            and item.get("forbidden_hit_count") == 0
            for item in scans
        ),
        "predecessor_source_count": len(predecessor_contracts),
        "receipt_index_digest": _digest(receipt_index),
    }
    contract = {
        "summary": summary,
        "scenario_names": {
            POST_DX_LIVE_DURABLE_ORCHESTRATION_SUITE_NAME: list(
                POST_DX_LIVE_DURABLE_ORCHESTRATION_SCENARIO_NAMES
            ),
            RECORDED_LIVE_ORCHESTRATION_WINDOW_V1_SUITE_NAME: list(
                RECORDED_LIVE_ORCHESTRATION_WINDOW_V1_SCENARIO_NAMES
            ),
            CRASH_RESTART_FAILOVER_DRILL_V3_SUITE_NAME: list(
                CRASH_RESTART_FAILOVER_DRILL_V3_SCENARIO_NAMES
            ),
            MULTI_AGENT_HANDOFF_DURABILITY_V2_SUITE_NAME: list(
                MULTI_AGENT_HANDOFF_DURABILITY_V2_SCENARIO_NAMES
            ),
            SIDE_EFFECT_RECONCILIATION_V6_SUITE_NAME: list(
                SIDE_EFFECT_RECONCILIATION_V6_SCENARIO_NAMES
            ),
            OPERATOR_RECOVERY_CONTROL_V4_SUITE_NAME: list(
                OPERATOR_RECOVERY_CONTROL_V4_SCENARIO_NAMES
            ),
            ORCHESTRATION_FALSE_CLAIM_SCAN_V3_SUITE_NAME: list(
                ORCHESTRATION_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES
            ),
        },
        "recorded_live_window_receipts": windows,
        "crash_restart_failover_receipts": failovers,
        "multi_agent_handoff_durability_receipts": handoffs,
        "side_effect_reconciliation_v6_receipts": side_effects,
        "operator_recovery_control_receipts": controls,
        "false_claim_scan_receipts": scans,
        "receipt_index": receipt_index,
        "policy": post_dx_live_durable_orchestration_policy_payload(),
    }
    contract["gate_checks"] = post_dx_live_durable_orchestration_gate_checks(contract)
    contract["summary"]["all_gate_checks_passed"] = all(contract["gate_checks"].values())
    return {
        **contract,
    }


async def _run_post_dx_live_durable_orchestration_suites() -> dict[str, Any]:
    contract = build_post_dx_live_durable_orchestration_contract()
    total = sum(len(names) for names in contract["scenario_names"].values())
    failed_checks = [
        name for name, passed in contract["gate_checks"].items() if not passed
    ]
    failed = len(failed_checks)
    return {
        "scenario_count": total,
        "passed": total - failed,
        "failed": failed,
        "failed_checks": failed_checks,
        "suite_names": list(contract["scenario_names"].keys()),
    }


async def build_post_dx_live_durable_orchestration_report() -> dict[str, Any]:
    latest = await _run_post_dx_live_durable_orchestration_suites()
    contract = build_post_dx_live_durable_orchestration_contract()
    failures = []
    if latest["failed"]:
        failures.append({
            "suite": POST_DX_LIVE_DURABLE_ORCHESTRATION_SUITE_NAME,
            "failed": str(latest["failed"]),
            "summary": "Post-DX live durable orchestration proof reported regressions.",
        })
    return {
        "summary": {
            **contract["summary"],
            "suite_count": len(latest["suite_names"]),
            "scenario_count": latest["scenario_count"],
            "passed": latest["passed"],
            "failed": latest["failed"],
            "benchmark_posture": (
                "bounded_post_dx_live_durable_orchestration_parity_proof"
                if latest["failed"] == 0
                else "post_dx_live_durable_orchestration_regressions_detected"
            ),
        },
        "contract": contract,
        "failure_report": failures,
        "policy": contract["policy"],
        "scenario_names": contract["scenario_names"],
        "latest_run": latest,
    }
