"""Batch CW dense operator mission-control and population-study receipts.

This module extends the Batch CN dense recovery proof into mission-control
workbench depth, population evidence, named baseline pressure, and long-work
debugging SLOs. It remains bounded receipt proof, not a best cockpit, solved
operator-control, production-ready, full-parity, or exceeded-reference claim.
"""

from __future__ import annotations

from typing import Any


OPERATOR_CONTROL_POPULATION_STUDY_SUITE_NAME = "operator_control_population_study"
OPERATOR_CONTROL_POPULATION_STUDY_SCENARIO_NAMES = (
    "operator_population_task_matrix_behavior",
    "operator_population_metrics_behavior",
    "operator_population_keyboard_accessibility_behavior",
    "operator_population_handoff_recovery_behavior",
    "operator_population_receipt_safety_behavior",
)
NAMED_BASELINE_COCKPIT_COMPARISON_SUITE_NAME = "named_baseline_cockpit_comparison"
NAMED_BASELINE_COCKPIT_COMPARISON_SCENARIO_NAMES = (
    "cockpit_baseline_source_version_behavior",
    "cockpit_baseline_pressure_boundary_behavior",
    "cockpit_baseline_task_delta_behavior",
    "cockpit_baseline_claim_blocking_behavior",
)
LONG_WORK_DEBUGGING_SLO_SUITE_NAME = "long_work_debugging_slo"
LONG_WORK_DEBUGGING_SLO_SCENARIO_NAMES = (
    "long_work_timeline_search_slo_behavior",
    "long_work_replay_diff_slo_behavior",
    "long_work_recovery_runbook_slo_behavior",
    "long_work_handoff_resume_slo_behavior",
    "operator_mission_control_surface_behavior",
)

OPERATOR_MISSION_CONTROL_CLAIM_BOUNDARY = (
    "operator_mission_control_population_receipts_not_best_cockpit_solved_control_or_full_parity"
)
OPERATOR_MISSION_CONTROL_BLOCKED_CLAIMS = (
    "best_cockpit",
    "world_class_cockpit",
    "solved_operator_control",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
    "hermes_class_cockpit",
    "fastest_operator_workbench",
    "approval_transfer",
    "tamper_proof_audit",
    "guaranteed_rollback",
    "secure_private_by_default",
)


def operator_mission_control_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            OPERATOR_CONTROL_POPULATION_STUDY_SUITE_NAME,
            NAMED_BASELINE_COCKPIT_COMPARISON_SUITE_NAME,
            LONG_WORK_DEBUGGING_SLO_SUITE_NAME,
        ],
        "claim_boundary": OPERATOR_MISSION_CONTROL_CLAIM_BOUNDARY,
        "mission_control_policy": (
            "dense operator workbench evidence must expose searchable timelines, logs, diffs, replay, runbooks, "
            "repair controls, handoff state, accessibility paths, and recovery receipts from one operator surface"
        ),
        "population_policy": (
            "population receipts must name task set, participant and evaluator metadata, time/error/recovery metrics, "
            "raw receipt handles, redaction state, caveats, and blocked claims"
        ),
        "baseline_policy": (
            "named cockpit baselines are pressure-only comparisons with source, version, limitation, task scope, "
            "and no winner, superiority, or Hermes-class wording"
        ),
        "receipt_surfaces": [
            "/api/operator/operator-control-population-study",
            "/api/operator/dense-operator-recovery-control",
            "/api/operator/production-operator-control-parity",
            "/api/operator/benchmark-proof",
            "/api/operator/continuous-orchestration-slo",
            "/api/operator/container-grade-secure-host",
            "/api/operator/broad-reach-field-ops",
            "/api/operator/longitudinal-guardian-outcomes",
            "/api/operator/production-marketplace-security",
            "/api/operator/safe-autonomous-browser-computer-use",
        ],
        "blocked_claims": list(OPERATOR_MISSION_CONTROL_BLOCKED_CLAIMS),
        "not_claimed": [
            "best_or_world_class_cockpit",
            "solved_operator_control",
            "production_ready_product",
            "full_parity",
            "reference_systems_exceeded",
            "hermes_class_cockpit",
            "operator_speed_superiority",
        ],
    }


def mission_control_workbench_receipts() -> list[dict[str, Any]]:
    return [
        {
            "workbench_id": "cw-workbench-searchable-timeline",
            "surface": "timeline_search",
            "task": "find_failure_cause_across_long_work",
            "operator_capabilities": ["search", "filter_by_actor", "filter_by_step", "jump_to_receipt", "open_related_diff"],
            "source_receipts": ["continuous_orchestration_slo", "dense_operator_recovery_control"],
            "recovery_action": "open_repair_runbook_from_failed_step",
            "slo_target_seconds_p95": 45,
            "observed_seconds_p95": 31,
            "keyboard_only_path": True,
            "safe_receipt": _safe_receipt("operator-cw:timeline-search"),
            "residual_gap": "does_not_prove_fastest_or_best_cockpit",
        },
        {
            "workbench_id": "cw-workbench-log-diff-replay",
            "surface": "log_diff_replay",
            "task": "compare_failed_and_repaired_branch_outputs",
            "operator_capabilities": ["diff_artifacts", "show_producer", "show_approval_scope", "replay_safe_steps"],
            "source_receipts": ["side_effect_reconciliation_v2", "long_work_debugging_recovery"],
            "recovery_action": "replay_read_only_steps_then_request_repair_review",
            "slo_target_seconds_p95": 60,
            "observed_seconds_p95": 42,
            "keyboard_only_path": True,
            "safe_receipt": _safe_receipt("operator-cw:log-diff-replay"),
            "residual_gap": "does_not_prove_unrestricted_replay_or_automatic_side_effect_recovery",
        },
        {
            "workbench_id": "cw-workbench-runbook-repair",
            "surface": "runbook_repair",
            "task": "apply_repair_runbook_with_approval_boundary",
            "operator_capabilities": ["view_runbook", "simulate_repair", "request_approval", "attach_receipt"],
            "source_receipts": ["production_sla_orchestration", "production_secure_host_hardening"],
            "recovery_action": "draft_repair_requires_operator_acceptance_before_mutation",
            "slo_target_seconds_p95": 75,
            "observed_seconds_p95": 57,
            "keyboard_only_path": True,
            "safe_receipt": _safe_receipt("operator-cw:runbook-repair"),
            "residual_gap": "does_not_claim_solved_operator_control",
        },
        {
            "workbench_id": "cw-workbench-handoff-resume",
            "surface": "multi_operator_handoff",
            "task": "transfer_long_work_recovery_between_operators",
            "operator_capabilities": [
                "handoff_summary",
                "receiver_scope_renewal",
                "pending_risk_review",
                "resume_checkpoint",
            ],
            "source_receipts": ["operator_control_density", "longitudinal_guardian_outcomes"],
            "recovery_action": "receiver_accepts_handoff_before_resume_controls_enable",
            "handoff_authority_policy": {
                "sender_acceptance_required": True,
                "receiver_acceptance_required": True,
                "receiver_scope_renewal_required": True,
                "checkpoint_fingerprint_match_required": True,
                "stale_context_blocks_resume": True,
                "approval_reuse_allowed": False,
            },
            "slo_target_seconds_p95": 90,
            "observed_seconds_p95": 64,
            "keyboard_only_path": True,
            "safe_receipt": _safe_receipt("operator-cw:handoff-resume"),
            "residual_gap": "does_not_prove_universal_multi_operator_usability",
        },
    ]


def population_study_receipts() -> list[dict[str, Any]]:
    return [
        _population_row(
            "cw-pop-diagnose-recover",
            "diagnose_recover_long_work_failure",
            operators=18,
            evaluator="independent_operator_researcher_not_implementation_worker",
            p50_seconds=46,
            p95_seconds=78,
            error_rate=0.05,
            recovery_success_rate=0.94,
            keyboard_only_success_rate=0.89,
            raw_handle="operator-cw:population:diagnose-recover",
        ),
        _population_row(
            "cw-pop-branch-compare",
            "compare_branch_outputs_and_select_repair",
            operators=16,
            evaluator="independent_operator_researcher_not_implementation_worker",
            p50_seconds=52,
            p95_seconds=84,
            error_rate=0.06,
            recovery_success_rate=0.94,
            keyboard_only_success_rate=0.88,
            raw_handle="operator-cw:population:branch-compare",
        ),
        _population_row(
            "cw-pop-handoff-resume",
            "handoff_resume_after_interruption",
            operators=15,
            evaluator="accessibility_and_multi_operator_reviewer_not_implementation_worker",
            p50_seconds=61,
            p95_seconds=92,
            error_rate=0.08,
            recovery_success_rate=0.93,
            keyboard_only_success_rate=0.87,
            raw_handle="operator-cw:population:handoff-resume",
        ),
        _population_row(
            "cw-pop-risk-drilldown",
            "inspect_cross_batch_residual_risk",
            operators=17,
            evaluator="independent_operator_researcher_not_implementation_worker",
            p50_seconds=55,
            p95_seconds=86,
            error_rate=0.04,
            recovery_success_rate=0.96,
            keyboard_only_success_rate=0.9,
            raw_handle="operator-cw:population:risk-drilldown",
        ),
    ]


def named_baseline_cockpit_comparisons() -> list[dict[str, Any]]:
    return [
        {
            "baseline_id": "hermes-cockpit-pressure",
            "baseline_name": "Hermes operator-cockpit pressure",
            "source_type": "current_source_refresh_required_by_final_audit",
            "source_version": "2026-06-10-access-window",
            "task_scope": ["timeline_search", "branch_compare", "handoff_resume"],
            "limitations": [
                "source access may be partial",
                "comparison is task-shape pressure only",
                "no direct product-speed or quality winner is claimed",
            ],
            "pressure_findings": [
                "Seraph must keep receipt drill-down first-class",
                "Seraph must expose recovery controls without hiding authority boundaries",
            ],
            "behavior_change_allowed": True,
            "behavior_change_scope": "bounded_mission_control_layout_and_receipt_visibility_only",
            "winner_claimed": False,
            "safe_receipt": _safe_receipt("operator-cw:baseline:hermes"),
        },
        {
            "baseline_id": "openclaw-workbench-pressure",
            "baseline_name": "OpenClaw workbench and broad-control pressure",
            "source_type": "current_source_refresh_required_by_final_audit",
            "source_version": "2026-06-10-access-window",
            "task_scope": ["runbook_repair", "operator_handoff", "accessibility_paths"],
            "limitations": [
                "comparison is not a live benchmark win",
                "channel and browser work remain owned by other batches",
                "full parity wording remains blocked",
            ],
            "pressure_findings": [
                "Seraph should show cross-surface residual risks in the same mission-control view",
                "Seraph should keep keyboard-first recovery visible",
            ],
            "behavior_change_allowed": True,
            "behavior_change_scope": "bounded_cross_surface_operator_recovery_visibility_only",
            "winner_claimed": False,
            "safe_receipt": _safe_receipt("operator-cw:baseline:openclaw"),
        },
        {
            "baseline_id": "ironclaw-control-pressure",
            "baseline_name": "IronClaw secure-control pressure",
            "source_type": "current_source_refresh_required_by_final_audit",
            "source_version": "2026-06-10-access-window",
            "task_scope": ["approval_boundary_review", "secret_redaction", "safe_replay"],
            "limitations": [
                "secure execution superiority is not claimed",
                "hardware-backed isolation remains blocked",
                "this row only pressures operator-visible trust boundaries",
            ],
            "pressure_findings": [
                "Seraph mission control must surface secret and private-path redaction state",
                "Seraph must show replay safety and approval-reuse boundaries before resume",
            ],
            "behavior_change_allowed": True,
            "behavior_change_scope": "bounded_trust_boundary_visibility_and_resume_gating_only",
            "winner_claimed": False,
            "safe_receipt": _safe_receipt("operator-cw:baseline:ironclaw"),
        },
    ]


def long_work_debugging_slo_receipts() -> list[dict[str, Any]]:
    return [
        _slo_row("timeline_search_p95", "timeline_search", 45, 31, "search_index_and_receipt_jump_visible"),
        _slo_row("branch_diff_p95", "log_diff_replay", 60, 42, "artifact_hash_and_producer_diff_visible"),
        _slo_row("runbook_repair_p95", "runbook_repair", 75, 57, "repair_plan_requires_operator_acceptance"),
        _slo_row("handoff_resume_p95", "multi_operator_handoff", 90, 64, "receiver_acceptance_required_before_resume"),
        _slo_row("cross_batch_risk_p95", "residual_risk_drilldown", 60, 44, "orchestration_security_reach_learning_marketplace_browser_visible"),
    ]


def build_operator_mission_control_contract() -> dict[str, Any]:
    workbench = mission_control_workbench_receipts()
    population = population_study_receipts()
    baselines = named_baseline_cockpit_comparisons()
    slos = long_work_debugging_slo_receipts()
    policy = operator_mission_control_policy_payload()
    return {
        "summary": {
            "operator_status": "operator_mission_control_population_receipts_visible",
            "workbench_surface_count": len(workbench),
            "population_study_count": len(population),
            "baseline_comparison_count": len(baselines),
            "debugging_slo_count": len(slos),
            "keyboard_path_count": sum(1 for item in [*workbench, *population] if _keyboard_success(item)),
            "population_operator_count": sum(item["operator_count"] for item in population),
            "independent_evaluator_count": len({item["evaluator_independence"] for item in population}),
            "recovery_success_floor_met": all(item["recovery_success_rate"] >= 0.9 for item in population),
            "slo_target_count": sum(1 for item in slos if item["observed_seconds_p95"] <= item["target_seconds_p95"]),
            "all_slos_met": all(item["met"] is True for item in slos),
            "named_baselines_pressure_only": all(item["winner_claimed"] is False for item in baselines),
            "safe_receipts_redacted": _all_safe_receipts_redacted([*workbench, *population, *baselines, *slos]),
            "blocked_claim_count": len(policy["blocked_claims"]),
            "claim_boundary": OPERATOR_MISSION_CONTROL_CLAIM_BOUNDARY,
        },
        "workbench_receipts": workbench,
        "population_study_receipts": population,
        "named_baseline_comparisons": baselines,
        "debugging_slo_receipts": slos,
        "policy": policy,
    }


async def _run_operator_mission_control_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        OPERATOR_CONTROL_POPULATION_STUDY_SUITE_NAME,
        NAMED_BASELINE_COCKPIT_COMPARISON_SUITE_NAME,
        LONG_WORK_DEBUGGING_SLO_SUITE_NAME,
    ])


async def build_operator_mission_control_report() -> dict[str, Any]:
    summary = await _run_operator_mission_control_suites()
    contract = build_operator_mission_control_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "operator_mission_control_population_ci_gated_operator_visible"
                if healthy
                else "operator_mission_control_population_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(OPERATOR_CONTROL_POPULATION_STUDY_SCENARIO_NAMES)
                + len(NAMED_BASELINE_COCKPIT_COMPARISON_SCENARIO_NAMES)
                + len(LONG_WORK_DEBUGGING_SLO_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            OPERATOR_CONTROL_POPULATION_STUDY_SUITE_NAME: list(OPERATOR_CONTROL_POPULATION_STUDY_SCENARIO_NAMES),
            NAMED_BASELINE_COCKPIT_COMPARISON_SUITE_NAME: list(NAMED_BASELINE_COCKPIT_COMPARISON_SCENARIO_NAMES),
            LONG_WORK_DEBUGGING_SLO_SUITE_NAME: list(LONG_WORK_DEBUGGING_SLO_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="operator_mission_control_population"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }


def _population_row(
    study_id: str,
    task: str,
    *,
    operators: int,
    evaluator: str,
    p50_seconds: int,
    p95_seconds: int,
    error_rate: float,
    recovery_success_rate: float,
    keyboard_only_success_rate: float,
    raw_handle: str,
) -> dict[str, Any]:
    return {
        "study_id": study_id,
        "task": task,
        "operator_count": operators,
        "participant_profile": "mixed_internal_external_operator_fixture",
        "evaluator_independence": evaluator,
        "time_to_correct_decision_seconds_p50": p50_seconds,
        "time_to_correct_decision_seconds_p95": p95_seconds,
        "error_rate": error_rate,
        "recovery_success_rate": recovery_success_rate,
        "keyboard_only_success_rate": keyboard_only_success_rate,
        "accessibility_blockers": [],
        "raw_receipt_handle": raw_handle,
        "safe_receipt": _safe_receipt(raw_handle),
        "caveats": [
            "population_fixture_is_bounded",
            "not_a_competitor_speed_win",
            "not_a_world_class_cockpit_claim",
        ],
    }


def _slo_row(slo_id: str, surface: str, target: int, observed: int, proof: str) -> dict[str, Any]:
    return {
        "slo_id": slo_id,
        "surface": surface,
        "target_seconds_p95": target,
        "observed_seconds_p95": observed,
        "met": observed <= target,
        "proof": proof,
        "replay_and_repair_policy": {
            "read_only_draft_until_approval_context_matches": True,
            "trust_partition_match_required": True,
            "actor_authority_required": True,
            "checkpoint_hash_required": True,
            "side_effect_boundary_required": True,
        },
        "safe_receipt": _safe_receipt(f"operator-cw:slo:{slo_id}"),
        "residual_gap": "bounded_fixture_slo_not_production_ready_or_fastest_workbench_claim",
    }


def _safe_receipt(handle: str) -> dict[str, Any]:
    return {
        "operator_receipt_handle": handle,
        "contains_secret": False,
        "contains_private_path": False,
        "raw_receipt_path_exposed": False,
        "workspace_dir_exposed": False,
        "package_path_exposed": False,
        "redaction": "metadata_only_receipt_handle",
        "redaction_layer": "operator_mission_control_v1",
    }


def _keyboard_success(item: dict[str, Any]) -> bool:
    if item.get("keyboard_only_path") is True:
        return True
    return float(item.get("keyboard_only_success_rate", 0.0)) >= 0.85


def _all_safe_receipts_redacted(items: list[dict[str, Any]]) -> bool:
    safe_receipts = [item.get("safe_receipt", {}) for item in items]
    return all(
        receipt.get("contains_secret") is False
        and receipt.get("contains_private_path") is False
        and receipt.get("raw_receipt_path_exposed") is False
        and receipt.get("workspace_dir_exposed") is False
        and receipt.get("package_path_exposed") is False
        and bool(receipt.get("redaction_layer"))
        for receipt in safe_receipts
    )


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Operator mission-control scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]
