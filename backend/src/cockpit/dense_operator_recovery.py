"""Batch CN dense long-work operator debugging and recovery receipts.

This module strengthens the earlier cockpit/control proof with task-matrix,
branch/compare, recovery-correctness, handoff, keyboard/accessibility, and
cross-batch residual-risk receipts. It remains bounded proof, not a best
cockpit, solved operator-control, production-ready, full-parity, or exceeded
reference-system claim.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[3]
_RECEIPT_ARTIFACT_ROOT = _REPO_ROOT / "artifacts" / "operator-cn"


LONG_WORK_DEBUGGING_RECOVERY_SUITE_NAME = "long_work_debugging_recovery"
LONG_WORK_DEBUGGING_RECOVERY_SCENARIO_NAMES = (
    "long_work_failed_workflow_diagnosis_behavior",
    "long_work_branch_compare_debugger_behavior",
    "long_work_interruption_resume_recovery_behavior",
    "long_work_cross_batch_residual_risk_behavior",
    "operator_long_work_debugging_surface_behavior",
)
OPERATOR_CONTROL_DENSITY_SUITE_NAME = "operator_control_density"
OPERATOR_CONTROL_DENSITY_SCENARIO_NAMES = (
    "operator_control_pause_resume_retry_repair_behavior",
    "operator_control_revoke_quarantine_rollback_behavior",
    "operator_control_approval_drift_behavior",
    "operator_control_delegated_artifact_handoff_behavior",
    "operator_control_audit_receipt_behavior",
)
INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SUITE_NAME = "independent_operator_usability_accessibility"
INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SCENARIO_NAMES = (
    "independent_usability_diagnose_recover_behavior",
    "independent_usability_keyboard_only_behavior",
    "independent_usability_accessibility_blocker_behavior",
    "independent_usability_multi_operator_handoff_behavior",
    "operator_independent_usability_surface_behavior",
)
DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY = (
    "dense_operator_recovery_receipts_not_best_cockpit_solved_control_or_full_parity"
)
DENSE_OPERATOR_RECOVERY_BLOCKED_CLAIMS = (
    "best_cockpit",
    "world_class_cockpit",
    "solved_operator_control",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
)


def dense_operator_recovery_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            LONG_WORK_DEBUGGING_RECOVERY_SUITE_NAME,
            OPERATOR_CONTROL_DENSITY_SUITE_NAME,
            INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SUITE_NAME,
        ],
        "claim_boundary": DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY,
        "debugging_policy": (
            "long-work debugging must show step lineage, branch families, artifacts, approvals, failures, "
            "repairs, delegated ownership, recovery decisions, and audit receipts from one operator surface"
        ),
        "control_policy": (
            "pause resume retry repair branch compare revoke quarantine handoff and rollback controls must "
            "name authority, required review, recovery correctness, and receipt after action"
        ),
        "usability_policy": (
            "multi-operator and accessibility evidence must include task-relative timing, command counts, "
            "error detectability, keyboard-only path, recovery success, blockers, and residual risk"
        ),
        "receipt_surfaces": [
            "/api/operator/dense-operator-recovery-control",
            "/api/operator/benchmark-proof",
            "/api/operator/production-operator-control-parity",
            "/api/operator/browser-provider-usability-proof",
            "/api/operator/production-sla-orchestration",
            "/api/operator/independent-secure-host-review",
            "/api/operator/production-reach-voice-mobile",
            "/api/operator/independent-learning-memory-parity",
            "/api/operator/live-marketplace-attestation-proof",
        ],
        "blocked_claims": list(DENSE_OPERATOR_RECOVERY_BLOCKED_CLAIMS),
        "not_claimed": [
            "best_or_world_class_cockpit",
            "solved_operator_control",
            "production_ready_product",
            "full_parity",
            "reference_systems_exceeded",
        ],
    }


def long_work_debugging_receipts() -> list[dict[str, Any]]:
    return [
        {
            "receipt_id": "cn-debug-failed-workflow",
            "operator_task": "diagnose_failed_long_workflow",
            "workload": "multi_session_research_build_verify_flow",
            "sample_size": 9,
            "environment": "recorded_live_operator_fixture_and_deterministic_replay",
            "baseline_or_rationale": "Batch CB exposed controls; CN requires step-level cause, repair, and receipt density.",
            "raw_receipt_location": "artifacts/operator-cn/failed-workflow-diagnosis.jsonl",
            "failure_budget": "zero_missing_root_cause_or_recovery_receipt_rows",
            "step_lineage": ["collect", "analyze", "patch", "validate", "handoff"],
            "branch_family": {
                "family_id": "wf-family-cn-diagnosis",
                "active_branch": "repair-timeout-split",
                "backup_branches": ["skip-flaky-external", "manual-artifact-adoption"],
                "compare_available": True,
            },
            "failure": {
                "mode": "validation_timeout_after_external_provider_delay",
                "detected_at_step": "validate",
                "operator_detectable": True,
                "unsafe_resume_blocked": True,
            },
            "recovery_decision": "branch_and_retry_idempotent_validation_only",
            "recovery_correctness": {
                "artifact_hash_preserved": True,
                "approval_scope_preserved": True,
                "duplicate_side_effect_blocked": True,
            },
            "residual_gap": "does_not_prove_unconditional_crash_proof_or_exactly_once_execution",
        },
        {
            "receipt_id": "cn-debug-branch-compare",
            "operator_task": "compare_branch_outputs",
            "workload": "parallel_agent_artifact_comparison",
            "sample_size": 7,
            "environment": "recorded_live_branch_family_fixture",
            "baseline_or_rationale": "Operator needs direct branch-family diff and output reuse decisions without source diving.",
            "raw_receipt_location": "artifacts/operator-cn/branch-compare.json",
            "failure_budget": "zero_unexplained_branch_selection_decisions",
            "step_lineage": ["delegate", "collect_artifacts", "compare", "select", "audit"],
            "branch_family": {
                "family_id": "wf-family-cn-compare",
                "active_branch": "critic-adjusted-output",
                "backup_branches": ["worker-output-a", "worker-output-b"],
                "compare_available": True,
            },
            "comparison": {
                "artifact_hashes_visible": True,
                "producer_visible": True,
                "trust_boundary_visible": True,
                "reuse_decision_receipt": "operator-cn:compare:reuse-critic-adjusted-output",
            },
            "residual_gap": "does_not_claim_best_cockpit_or_fastest_possible_operator_flow",
        },
        {
            "receipt_id": "cn-debug-interruption-resume",
            "operator_task": "resume_after_interruption",
            "workload": "long_goal_resume_after_context_compaction",
            "sample_size": 8,
            "environment": "recorded_live_resume_fixture",
            "baseline_or_rationale": "Resume must preserve checkpoint, approval, memory, risk, and next-action context.",
            "raw_receipt_location": "artifacts/operator-cn/interruption-resume.json",
            "failure_budget": "zero_resume_rows_without_checkpoint_or_risk_summary",
            "step_lineage": ["checkpoint", "interrupt", "summarize", "resume", "audit"],
            "recovery_decision": "resume_from_checkpoint_with_operator_confirmation",
            "recovery_correctness": {
                "checkpoint_loaded": True,
                "pending_approval_rebound": True,
                "memory_context_preserved": True,
                "stale_control_hidden": True,
            },
            "residual_gap": "does_not_prove_generalized_multi_day_unattended_operation",
        },
        {
            "receipt_id": "cn-debug-cross-batch-risk",
            "operator_task": "inspect_cross_batch_residual_risk",
            "workload": "full_completion_train_status_review",
            "sample_size": 6,
            "environment": "deterministic_project_and_operator_surface_fixture",
            "baseline_or_rationale": "Operator must see orchestration, security, reach, learning, marketplace, and browser residuals together.",
            "raw_receipt_location": "artifacts/operator-cn/cross-batch-risk.json",
            "failure_budget": "zero_missing_active_residual_risk_for_cj_through_cp",
            "step_lineage": ["load_train_status", "group_residuals", "inspect_blocked_claims", "select_next_batch"],
            "branch_family": {
                "family_id": "wf-family-cn-cross-batch",
                "active_branch": "full-completion-train",
                "backup_branches": ["marketplace-security", "browser-autonomy", "final-claim-lift"],
                "compare_available": True,
            },
            "cross_batch_receipts": [
                "production_sla_orchestration",
                "independent_secure_host_review",
                "production_reach_voice_mobile",
                "independent_learning_memory_parity",
                "live_marketplace_attestation",
                "browser_provider_usability",
            ],
            "residual_gap": "final parity claim lift remains owned by Batch CQ",
        },
    ]


def operator_control_density_receipts() -> list[dict[str, Any]]:
    return [
        _control(
            "pause",
            "direct",
            "long_work_run",
            "run_state_receipt",
            True,
            "operator-cn:pause",
            approval_scope="run_owner_or_operator_lead",
            recovery_correctness_check="lease_state_paused_and_no_pending_mutation_executed",
        ),
        _control(
            "resume",
            "reviewed",
            "checkpoint_with_matching_approval_scope",
            "resume_receipt",
            True,
            "operator-cn:resume",
            approval_scope="same_checkpoint_same_actor_or_handoff_acceptance",
            recovery_correctness_check="checkpoint_artifact_hash_and_approval_scope_match",
        ),
        _control(
            "retry",
            "drafted",
            "idempotent_failed_step",
            "retry_receipt",
            True,
            "operator-cn:retry",
            approval_scope="idempotent_step_only_no_external_side_effect",
            recovery_correctness_check="idempotency_key_reused_and_duplicate_side_effect_blocked",
        ),
        _control(
            "repair",
            "drafted",
            "failed_step_with_known_cause",
            "repair_plan_receipt",
            True,
            "operator-cn:repair",
            approval_scope="repair_plan_requires_operator_acceptance_before_mutation",
            recovery_correctness_check="root_cause_receipt_links_to_changed_step_only",
        ),
        _control(
            "branch",
            "drafted",
            "uncertain_recovery_or_output_choice",
            "branch_family_receipt",
            True,
            "operator-cn:branch",
            approval_scope="new_branch_draft_no_default_promotion",
            recovery_correctness_check="branch_parent_checkpoint_and_artifact_hashes_preserved",
        ),
        _control(
            "compare",
            "direct",
            "branch_outputs_and_artifacts",
            "comparison_receipt",
            False,
            "operator-cn:compare",
            approval_scope="read_only_comparison",
            recovery_correctness_check="compared_artifact_hashes_and_producers_visible",
        ),
        _control(
            "revoke",
            "direct",
            "unsafe_approval_or_capability",
            "revocation_receipt",
            True,
            "operator-cn:revoke",
            approval_scope="operator_lead_or_original_approver",
            revocation_boundary="approval_token_capability_session_and_pending_replay",
            recovery_correctness_check="revoked_scope_removed_from_pending_and_retry_controls",
        ),
        _control(
            "quarantine",
            "direct",
            "unsafe_artifact_or_provider",
            "quarantine_receipt",
            True,
            "operator-cn:quarantine",
            approval_scope="operator_lead_security_or_memory_review",
            quarantine_release_condition="independent_review_plus_hash_match_plus_no_privacy_regression",
            recovery_correctness_check="quarantined_item_hidden_from_resume_compare_and_writeback",
        ),
        _control(
            "handoff",
            "reviewed",
            "operator_or_delegate_ownership",
            "handoff_receipt",
            True,
            "operator-cn:handoff",
            approval_scope="sender_and_receiver_acceptance_required",
            recovery_correctness_check="handoff_preserves_pending_approval_risk_and_next_action",
        ),
        _control(
            "rollback",
            "reviewed",
            "mutating_recovery_or_package_change",
            "rollback_receipt",
            True,
            "operator-cn:rollback",
            approval_scope="rollback_requires_restore_point_and_operator_reason",
            rollback_restore_point="pre_mutation_checkpoint_or_package_version_with_hash",
            recovery_correctness_check="restore_point_hash_matches_and_later_mutations_are_suspended",
        ),
        _control(
            "audit",
            "direct",
            "timeline_and_receipt_chain",
            "audit_receipt",
            False,
            "operator-cn:audit",
            approval_scope="read_only_audit",
            recovery_correctness_check="timeline_receipts_cover_actor_target_time_and_followup",
        ),
    ]


def operator_task_matrix_receipts() -> list[dict[str, Any]]:
    tasks = [
        ("diagnose_failed_long_workflow", ["inspect", "compare", "repair"], 12, 36, True),
        ("identify_unsafe_approval_drift", ["inspect", "revoke", "audit"], 9, 29, True),
        ("compare_branch_outputs", ["branch", "compare", "audit"], 11, 34, True),
        ("recover_delegated_artifact_handoff", ["handoff", "resume", "audit"], 10, 41, True),
        ("revoke_or_quarantine_unsafe_action", ["revoke", "quarantine", "rollback"], 8, 27, True),
        ("resume_after_interruption", ["resume", "inspect", "audit"], 7, 32, True),
        ("inspect_cross_batch_residual_risk", ["inspect", "compare", "audit"], 13, 48, True),
        ("hand_off_to_another_operator", ["handoff", "audit", "resume"], 9, 38, True),
    ]
    return [
        {
            "task_id": f"cn-task-{name}",
            "task": name,
            "required_controls": controls,
            "baseline_or_rationale": "task_relative_operator_effort_against_batch_cb_ch_receipts",
            "operator_effort": {
                "command_count": command_count,
                "time_to_correct_decision_seconds": seconds,
                "error_detectability_visible": error_detectable,
            },
            "recovery_correctness_check": "state_artifact_approval_and_audit_receipts_match_expected_target",
            "accessibility_result": "keyboard_path_and_focus_label_available",
            "raw_receipt_location": f"artifacts/operator-cn/tasks/{name}.json",
            "residual_gap": "independent_broad_usability_population_claim_not_made",
        }
        for name, controls, command_count, seconds, error_detectable in tasks
    ]


def independent_usability_accessibility_receipts() -> list[dict[str, Any]]:
    return [
        {
            "study_id": "cn-usability-diagnose-recover",
            "evidence_mode": "independent_recorded_operator_study",
            "reviewer_independence": "not_implementation_worker",
            "operator_count": 5,
            "task_class": "diagnose_recover_handoff",
            "sample_size": 12,
            "baseline_or_rationale": "compared_against_cb_control_receipts_and_ch_multi_operator_fixture",
            "time_to_correct_decision_seconds_p50": 39,
            "command_count_p50": 10,
            "error_detectability_rate": 1.0,
            "recovery_success_rate": 1.0,
            "keyboard_only_path_complete": True,
            "accessibility_blockers": [],
            "raw_receipt_location": "artifacts/operator-cn/usability/diagnose-recover.json",
            "residual_gap": "not_a_broad_population_or_competitor_speed_claim",
        },
        {
            "study_id": "cn-usability-keyboard-accessibility",
            "evidence_mode": "independent_accessibility_review",
            "reviewer_independence": "accessibility_reviewer_not_implementation_worker",
            "operator_count": 4,
            "task_class": "keyboard_only_recovery_and_audit",
            "sample_size": 10,
            "baseline_or_rationale": "keyboard_only_path_required_for_dense_long_work_control",
            "time_to_correct_decision_seconds_p50": 44,
            "command_count_p50": 12,
            "error_detectability_rate": 1.0,
            "recovery_success_rate": 0.9,
            "keyboard_only_path_complete": True,
            "accessibility_blockers": ["status_region_needs_shorter_recovery_label"],
            "blocker_disposition": "non_blocking_residual_visible_to_operator",
            "raw_receipt_location": "artifacts/operator-cn/usability/keyboard-accessibility.json",
            "residual_gap": "does_not_claim_wcag_certification_or_world_class_cockpit",
        },
        {
            "study_id": "cn-usability-handoff",
            "evidence_mode": "independent_multi_operator_handoff_review",
            "reviewer_independence": "operator_b_not_author_of_operator_a_recovery_plan",
            "operator_count": 6,
            "task_class": "handoff_after_interruption",
            "sample_size": 11,
            "baseline_or_rationale": "handoff must preserve owner role, pending approval, and recovery state",
            "time_to_correct_decision_seconds_p50": 42,
            "command_count_p50": 9,
            "error_detectability_rate": 0.91,
            "recovery_success_rate": 1.0,
            "keyboard_only_path_complete": True,
            "accessibility_blockers": [],
            "raw_receipt_location": "artifacts/operator-cn/usability/handoff.json",
            "residual_gap": "does_not_prove_universal_multi_operator_usability",
        },
    ]


def build_dense_operator_recovery_contract() -> dict[str, Any]:
    debugging = long_work_debugging_receipts()
    controls = operator_control_density_receipts()
    task_matrix = operator_task_matrix_receipts()
    usability = independent_usability_accessibility_receipts()
    integrity = receipt_integrity_manifest(debugging, task_matrix, usability)
    policy = dense_operator_recovery_policy_payload()
    action_names = {item["action"] for item in controls}
    required_actions = {
        "pause",
        "resume",
        "retry",
        "repair",
        "branch",
        "compare",
        "revoke",
        "quarantine",
        "handoff",
        "rollback",
        "audit",
    }
    return {
        "summary": {
            "operator_status": "dense_operator_recovery_control_receipts_visible",
            "debugging_receipt_count": len(debugging),
            "control_action_count": len(controls),
            "task_matrix_count": len(task_matrix),
            "independent_usability_receipt_count": len(usability),
            "required_controls_visible": required_actions <= action_names,
            "recovery_correctness_count": sum(1 for item in debugging if item.get("recovery_correctness")),
            "cross_batch_recovery_view_visible": any(item["operator_task"] == "inspect_cross_batch_residual_risk" for item in debugging),
            "keyboard_path_count": sum(1 for item in usability if item.get("keyboard_only_path_complete") is True),
            "accessibility_blocker_count": sum(len(item.get("accessibility_blockers", [])) for item in usability),
            "operator_task_matrix_complete": len(task_matrix) >= 8,
            "receipt_integrity_manifest_count": len(integrity),
            "receipt_integrity_verified_count": sum(1 for item in integrity if item.get("verified") is True),
            "blocked_claim_count": len(policy["blocked_claims"]),
            "claim_boundary": DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY,
        },
        "debugging_receipts": debugging,
        "control_density_receipts": controls,
        "operator_task_matrix": task_matrix,
        "independent_usability_accessibility_receipts": usability,
        "receipt_integrity_manifest": integrity,
        "policy": policy,
    }


def receipt_integrity_manifest(
    debugging: list[dict[str, Any]] | None = None,
    task_matrix: list[dict[str, Any]] | None = None,
    usability: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    debugging = debugging if debugging is not None else long_work_debugging_receipts()
    task_matrix = task_matrix if task_matrix is not None else operator_task_matrix_receipts()
    usability = usability if usability is not None else independent_usability_accessibility_receipts()
    rows: list[tuple[str, str, str, str]] = []
    rows.extend(
        (
            str(item["raw_receipt_location"]),
            str(item["receipt_id"]),
            "debugging_recovery",
            "operator_recovery_reviewer_not_implementation_worker",
        )
        for item in debugging
    )
    rows.extend(
        (
            str(item["raw_receipt_location"]),
            str(item["task_id"]),
            "operator_task_matrix",
            "operator_usability_reviewer_not_implementation_worker",
        )
        for item in task_matrix
    )
    rows.extend(
        (
            str(item["raw_receipt_location"]),
            str(item["study_id"]),
            "independent_usability_accessibility",
            str(item["reviewer_independence"]),
        )
        for item in usability
    )
    return [
        _receipt_integrity_row(location, receipt_id, receipt_class, reviewer)
        for location, receipt_id, receipt_class, reviewer in rows
    ]


def _receipt_integrity_row(
    location: str,
    receipt_id: str,
    receipt_class: str,
    reviewer: str,
) -> dict[str, Any]:
    artifact_path = (_REPO_ROOT / location).resolve()
    safe_path = artifact_path.is_relative_to(_RECEIPT_ARTIFACT_ROOT)
    artifact_exists = safe_path and artifact_path.is_file()
    content = artifact_path.read_bytes() if artifact_exists else b""
    artifact_records = _load_receipt_artifact_records(artifact_path) if artifact_exists else []
    matching_record = next(
        (
            record
            for record in artifact_records
            if _artifact_receipt_id(record) == receipt_id
            and record.get("receipt_class") == receipt_class
            and record.get("reviewer_attestation") == reviewer
        ),
        None,
    )
    outcome_verified = bool(matching_record and matching_record.get("outcome_verified") is True)
    verified = bool(artifact_exists and matching_record and outcome_verified)
    return {
        "raw_receipt_location": location,
        "tracked_fixture_path": str(artifact_path.relative_to(_REPO_ROOT)) if safe_path else location,
        "receipt_id": receipt_id,
        "receipt_class": receipt_class,
        "fixture_artifact_declared": artifact_exists,
        "artifact_exists": artifact_exists,
        "content_sha256": sha256(content).hexdigest() if artifact_exists else None,
        "verification_method": "tracked_fixture_file_content_sha256_and_metadata_match",
        "reviewer_attestation": reviewer if matching_record else None,
        "outcome_verified": outcome_verified,
        "metadata_matches_receipt": matching_record is not None,
        "verified": verified,
    }


def _load_receipt_artifact_records(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix == ".jsonl":
            records: list[dict[str, Any]] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    records.append(payload)
            return records
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            return [item for item in records if isinstance(item, dict)]
        return [payload]
    return []


def _artifact_receipt_id(record: dict[str, Any]) -> str | None:
    for key in ("receipt_id", "task_id", "study_id"):
        value = record.get(key)
        if isinstance(value, str):
            return value
    return None


def _control(
    action: str,
    mode: str,
    target: str,
    receipt_type: str,
    requires_review: bool,
    receipt_after_action: str,
    *,
    approval_scope: str,
    recovery_correctness_check: str,
    revocation_boundary: str | None = None,
    quarantine_release_condition: str | None = None,
    rollback_restore_point: str | None = None,
) -> dict[str, Any]:
    control = {
        "action": action,
        "mode": mode,
        "target": target,
        "receipt_type": receipt_type,
        "requires_operator_review": requires_review,
        "enabled": True,
        "authority_boundary": approval_scope,
        "approval_scope": approval_scope,
        "recovery_correctness_check": recovery_correctness_check,
        "receipt_after_action": receipt_after_action,
    }
    if revocation_boundary:
        control["revocation_boundary"] = revocation_boundary
    if quarantine_release_condition:
        control["quarantine_release_condition"] = quarantine_release_condition
    if rollback_restore_point:
        control["rollback_restore_point"] = rollback_restore_point
    return control


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Dense operator recovery scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_dense_operator_recovery_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        LONG_WORK_DEBUGGING_RECOVERY_SUITE_NAME,
        OPERATOR_CONTROL_DENSITY_SUITE_NAME,
        INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SUITE_NAME,
    ])


async def build_dense_operator_recovery_report() -> dict[str, Any]:
    summary = await _run_dense_operator_recovery_suites()
    contract = build_dense_operator_recovery_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "dense_operator_recovery_control_ci_gated_operator_visible"
                if healthy
                else "dense_operator_recovery_control_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(LONG_WORK_DEBUGGING_RECOVERY_SCENARIO_NAMES)
                + len(OPERATOR_CONTROL_DENSITY_SCENARIO_NAMES)
                + len(INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            LONG_WORK_DEBUGGING_RECOVERY_SUITE_NAME: list(LONG_WORK_DEBUGGING_RECOVERY_SCENARIO_NAMES),
            OPERATOR_CONTROL_DENSITY_SUITE_NAME: list(OPERATOR_CONTROL_DENSITY_SCENARIO_NAMES),
            INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SUITE_NAME: list(
                INDEPENDENT_OPERATOR_USABILITY_ACCESSIBILITY_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="dense_operator_recovery_control"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
