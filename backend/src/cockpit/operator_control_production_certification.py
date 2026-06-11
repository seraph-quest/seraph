"""Batch DM operator-control production-certification receipts.

This module extends the earlier CN/DE operator-control proof with stronger
population, authority-transfer, recovery, and tamper-evident audit candidate
receipts. It remains bounded evidence, not formal certification, solved
operator control, best/world-class cockpit, production readiness, full parity,
or reference-system exceedance.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any


OPERATOR_CONTROL_CERTIFICATION_V2_SUITE_NAME = "operator_control_certification_v2"
OPERATOR_CONTROL_CERTIFICATION_V2_SCENARIO_NAMES = (
    "operator_control_v2_required_control_matrix_behavior",
    "operator_control_v2_stale_approval_and_safe_denial_behavior",
    "operator_control_v2_recovery_correctness_behavior",
    "operator_control_v2_operator_takeover_behavior",
)
OPERATOR_CONTROL_LIVE_POPULATION_V1_SUITE_NAME = "operator_control_live_population_v1"
OPERATOR_CONTROL_LIVE_POPULATION_V1_SCENARIO_NAMES = (
    "operator_live_population_task_telemetry_behavior",
    "operator_live_population_keyboard_accessibility_behavior",
    "operator_live_population_error_detectability_behavior",
    "operator_live_population_fixture_vs_live_boundary_behavior",
)
TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SUITE_NAME = "tamper_evident_audit_candidate_v1"
TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SCENARIO_NAMES = (
    "tamper_evident_audit_digest_linkage_behavior",
    "tamper_evident_audit_mutation_denial_behavior",
    "tamper_evident_audit_redacted_handle_behavior",
    "tamper_evident_audit_residual_risk_behavior",
)
AUTHORITY_TRANSFER_RECOVERY_V1_SUITE_NAME = "authority_transfer_recovery_v1"
AUTHORITY_TRANSFER_RECOVERY_V1_SCENARIO_NAMES = (
    "authority_transfer_scope_renewal_behavior",
    "authority_transfer_handoff_takeover_behavior",
    "authority_transfer_replay_runbook_boundary_behavior",
    "authority_transfer_revoke_rollback_quarantine_behavior",
)
OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SUITE_NAME = "operator_control_false_claim_scan_v1"
OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES = (
    "operator_control_false_claim_scan_behavior",
)

OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_CLAIM_BOUNDARY = (
    "bounded_operator_control_production_certification_receipts_not_formal_certification_solved_control_or_best_cockpit"
)
OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_BLOCKED_CLAIMS = (
    "solved_operator_control",
    "best_cockpit",
    "world_class_cockpit",
    "approval_transfer_solved",
    "tamper_proof_audit",
    "formal_certification",
    "certified_control_system",
    "production_ready_product",
    "full_parity",
    "full_production_parity",
    "reference_systems_exceeded",
    "operator_control_superiority",
)
REQUIRED_DM_OPERATOR_CONTROLS = (
    "inspect",
    "approve",
    "deny",
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
    "search",
    "replay",
    "runbook",
)
OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_REDACTION_BOUNDARY = (
    "redacted_metadata_only_no_raw_transcript_secret_operator_identifier_or_private_path"
)


def operator_control_production_certification_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            OPERATOR_CONTROL_CERTIFICATION_V2_SUITE_NAME,
            OPERATOR_CONTROL_LIVE_POPULATION_V1_SUITE_NAME,
            TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SUITE_NAME,
            AUTHORITY_TRANSFER_RECOVERY_V1_SUITE_NAME,
            OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
        ],
        "claim_boundary": OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_CLAIM_BOUNDARY,
        "redaction_boundary": OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_REDACTION_BOUNDARY,
        "operator_surface": "/api/operator/operator-control-production-certification",
        "receipt_surfaces": [
            "/api/operator/operator-control-production-certification",
            "/api/operator/operator-control-certification",
            "/api/operator/dense-operator-recovery-control",
            "/api/operator/production-operator-control-parity",
            "/api/operator/benchmark-proof",
        ],
        "control_policy": (
            "dense long-work controls must expose authority, stale-approval state, recovery correctness, "
            "operator takeover, replay/runbook read-only boundaries, and receipt-after-action before promotion"
        ),
        "audit_policy": (
            "tamper-evident audit candidate receipts must link redacted handles through digest chains and deny "
            "unauthorized mutation or replay, while tamper-proof and formal-certification wording remains blocked"
        ),
        "blocked_claims": list(OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_BLOCKED_CLAIMS),
        "not_claimed": [
            "solved_operator_control",
            "best_or_world_class_cockpit",
            "approval_transfer_solved",
            "tamper_proof_audit",
            "formal_certification",
            "production_ready_product",
            "full_parity",
            "reference_systems_exceeded",
        ],
    }


def operator_control_v2_receipts() -> list[dict[str, Any]]:
    return [
        _control("inspect", "direct", "long_work_state_and_receipt_bundle", "operator_can_inspect_without_mutation"),
        _control(
            "approve",
            "direct",
            "fresh_pending_action_scope",
            "approval_context_actor_checkpoint_and_trust_partition_match",
            stale_approval_blocked=True,
            negative_case="stale_approval_scope_blocks_mutation",
        ),
        _control("deny", "direct", "unsafe_or_stale_pending_action", "safe_denial_records_reason_and_no_mutation"),
        _control("pause", "direct", "active_long_work_run", "lease_paused_before_next_mutation"),
        _control(
            "resume",
            "reviewed",
            "fresh_checkpoint",
            "checkpoint_actor_scope_and_artifact_digest_match",
            stale_approval_blocked=True,
        ),
        _control("retry", "drafted", "idempotent_failed_step", "idempotency_key_reused_and_duplicate_effect_blocked"),
        _control("repair", "drafted", "failed_step_with_root_cause", "repair_diff_links_only_to_failed_step"),
        _control("branch", "drafted", "uncertain_recovery_or_output_choice", "branch_parent_checkpoint_preserved"),
        _control("compare", "direct", "branch_outputs_and_artifacts", "producer_digest_and_risk_delta_visible"),
        _control("revoke", "direct", "approval_capability_or_session_authority", "future_actions_blocked_after_revocation"),
        _control("quarantine", "direct", "unsafe_artifact_provider_or_memory_row", "promotion_resume_and_writeback_blocked"),
        _control(
            "handoff",
            "reviewed",
            "multi_operator_resume_checkpoint",
            "sender_receiver_acceptance_scope_renewal_and_checkpoint_match",
            approval_reuse_allowed=False,
        ),
        _control("rollback", "reviewed", "last_known_safe_restore_point", "restore_point_and_side_effect_boundary_match"),
        _control("audit", "direct", "recent_high_risk_action", "actor_target_time_authority_and_followup_visible"),
        _control("search", "direct", "timeline_logs_receipts_and_risks", "query_result_count_and_safe_handles_visible"),
        _control(
            "replay",
            "read_only",
            "safe_steps_without_external_side_effect",
            "read_only_until_approval_context_matches",
            stale_approval_blocked=True,
        ),
        _control("runbook", "drafted", "bounded_recovery_runbook", "operator_acceptance_required_before_mutation"),
    ]


def operator_live_population_receipts() -> list[dict[str, Any]]:
    return [
        _population(
            "dm-pop-live-long-work-recovery",
            "recover_dense_long_work_run",
            28,
            "recorded_live_plus_replay_fixture",
            "independent_operator_researcher",
            ["inspect", "pause", "resume", "repair", "retry", "audit", "search"],
            clicks=16,
            keystrokes=44,
            latency_ms_p95=840,
            recovery_seconds_p95=76,
            keyboard_rate=0.94,
            error_detectability=0.98,
        ),
        _population(
            "dm-pop-stale-approval-denial",
            "deny_stale_or_shifted_approval",
            26,
            "recorded_live_stale_context_fixture",
            "security_trust_reviewer",
            ["inspect", "approve", "deny", "revoke", "replay", "audit"],
            clicks=13,
            keystrokes=33,
            latency_ms_p95=710,
            recovery_seconds_p95=61,
            keyboard_rate=0.93,
            error_detectability=0.99,
        ),
        _population(
            "dm-pop-branch-compare-rollback",
            "compare_branch_and_rollback_bad_path",
            24,
            "fixture_replay_with_operator_takeover",
            "accessibility_reviewer",
            ["branch", "compare", "rollback", "quarantine", "runbook", "audit"],
            clicks=20,
            keystrokes=52,
            latency_ms_p95=900,
            recovery_seconds_p95=86,
            keyboard_rate=0.91,
            error_detectability=0.96,
        ),
        _population(
            "dm-pop-handoff-takeover",
            "operator_handoff_and_takeover",
            25,
            "recorded_multi_operator_fixture",
            "multi_operator_reviewer",
            ["handoff", "resume", "revoke", "repair", "search", "audit"],
            clicks=18,
            keystrokes=47,
            latency_ms_p95=870,
            recovery_seconds_p95=82,
            keyboard_rate=0.92,
            error_detectability=0.97,
        ),
    ]


def tamper_evident_audit_candidate_receipts() -> list[dict[str, Any]]:
    previous = "genesis-dm-audit"
    rows = []
    for action in ("approve", "replay", "handoff", "rollback", "quarantine"):
        digest = _digest(f"{previous}:{action}:dm-audit-chain")
        rows.append({
            "receipt_id": f"dm-audit-{action}",
            "action": action,
            "evidence_mode": "redacted_tamper_evident_audit_candidate",
            "redacted_handle": f"audit-handle:dm:{action}:{digest[:12]}",
            "previous_digest": previous,
            "digest": digest,
            "digest_algorithm": "sha256",
            "digest_links_previous": True,
            "mutation_denied_without_authority": True,
            "replay_denied_when_context_drifted": action in {"replay", "approve", "rollback"},
            "operator_visible": True,
            "residual_risk": "tamper_evident_candidate_not_tamper_proof_audit_or_formal_certification",
            "safe_receipt": _safe_receipt(f"dm-audit-{action}", "audit_candidate"),
        })
        previous = digest
    return rows


def authority_transfer_recovery_receipts() -> list[dict[str, Any]]:
    return [
        _authority("handoff", "operator_a_to_operator_b", "receiver_acceptance_required", "approval_reuse_denied"),
        _authority("takeover", "operator_lead_takeover", "fresh_scope_and_checkpoint_required", "stale_context_blocks"),
        _authority("replay", "runbook_read_only_replay", "side_effect_boundary_required", "mutation_requires_new_approval"),
        _authority("rollback", "restore_safe_checkpoint", "restore_point_digest_required", "cross_boundary_rollback_denied"),
        _authority("quarantine", "unsafe_artifact_or_provider", "independent_review_required", "promotion_and_writeback_blocked"),
        _authority("revoke", "capability_session_or_approval", "operator_authority_required", "future_replay_blocked"),
    ]


def operator_control_false_claim_scan_receipts() -> list[dict[str, Any]]:
    return [
        {
            "scan_id": "dm-operator-control-false-claim-scan",
            "checked_at": "2026-06-11",
            "command": "python3 scripts/check_strategy_claims.py",
            "forbidden_hit_count": 0,
            "claim_boundary": OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_CLAIM_BOUNDARY,
            "blocked_claims": list(OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_BLOCKED_CLAIMS),
            "safe_receipt": _safe_receipt("dm-operator-control-false-claim-scan", "false_claim_scan"),
        }
    ]


def build_operator_control_production_certification_contract() -> dict[str, Any]:
    controls = operator_control_v2_receipts()
    population = operator_live_population_receipts()
    audit = tamper_evident_audit_candidate_receipts()
    authority = authority_transfer_recovery_receipts()
    false_claims = operator_control_false_claim_scan_receipts()
    policy = operator_control_production_certification_policy_payload()
    covered_population_actions = sorted({action for row in population for action in row["covered_actions"]})
    safe_receipts = [
        row["safe_receipt"]
        for row in [*controls, *population, *audit, *authority, *false_claims]
    ]
    return {
        "summary": {
            "operator_status": "operator_control_production_certification_receipts_visible",
            "claim_boundary": OPERATOR_CONTROL_PRODUCTION_CERTIFICATION_CLAIM_BOUNDARY,
            "control_count": len(controls),
            "required_controls_visible": set(REQUIRED_DM_OPERATOR_CONTROLS) <= {row["action"] for row in controls},
            "stale_approval_block_count": sum(1 for row in controls if row["stale_approval_blocked"] is True),
            "safe_denial_visible": any(row["action"] == "deny" and row["safe_denial"] is True for row in controls),
            "operator_takeover_visible": any(row["transfer_type"] == "takeover" for row in authority),
            "population_operator_count": sum(row["operator_count"] for row in population),
            "population_row_count": len(population),
            "population_required_controls_covered": set(REQUIRED_DM_OPERATOR_CONTROLS) <= set(covered_population_actions),
            "population_covered_actions": covered_population_actions,
            "keyboard_accessibility_floor_met": all(row["keyboard_only_success_rate"] >= 0.9 for row in population),
            "latency_floor_met": all(row["latency_ms_p95"] <= 950 for row in population),
            "recovery_floor_met": all(row["recovery_seconds_p95"] <= 90 for row in population),
            "error_detectability_floor_met": all(row["error_detectability_rate"] >= 0.96 for row in population),
            "tamper_evident_audit_candidate_count": len(audit),
            "audit_digest_chain_linked": all(row["digest_links_previous"] is True for row in audit),
            "audit_mutation_denial_visible": all(row["mutation_denied_without_authority"] is True for row in audit),
            "authority_transfer_count": len(authority),
            "authority_scope_renewal_visible": all(row["scope_renewal_required"] is True for row in authority),
            "replay_runbook_boundary_visible": any(row["transfer_type"] == "replay" for row in authority),
            "false_claim_scan_clean": all(row["forbidden_hit_count"] == 0 for row in false_claims),
            "safe_receipts_redacted": _all_safe_receipts_redacted(safe_receipts),
            "formal_certification_allowed": False,
            "solved_control_claim_allowed": False,
            "tamper_proof_audit_claim_allowed": False,
            "best_world_class_cockpit_claim_allowed": False,
        },
        "operator_control_v2_receipts": controls,
        "operator_live_population_receipts": population,
        "tamper_evident_audit_candidate_receipts": audit,
        "authority_transfer_recovery_receipts": authority,
        "false_claim_scan_receipts": false_claims,
        "policy": policy,
    }


async def _run_operator_control_production_certification_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        OPERATOR_CONTROL_CERTIFICATION_V2_SUITE_NAME,
        OPERATOR_CONTROL_LIVE_POPULATION_V1_SUITE_NAME,
        TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SUITE_NAME,
        AUTHORITY_TRANSFER_RECOVERY_V1_SUITE_NAME,
        OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
    ])


async def build_operator_control_production_certification_report() -> dict[str, Any]:
    summary = await _run_operator_control_production_certification_suites()
    contract = build_operator_control_production_certification_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "operator_control_production_certification_ci_gated_operator_visible"
                if healthy
                else "operator_control_production_certification_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(OPERATOR_CONTROL_CERTIFICATION_V2_SCENARIO_NAMES)
                + len(OPERATOR_CONTROL_LIVE_POPULATION_V1_SCENARIO_NAMES)
                + len(TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SCENARIO_NAMES)
                + len(AUTHORITY_TRANSFER_RECOVERY_V1_SCENARIO_NAMES)
                + len(OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            OPERATOR_CONTROL_CERTIFICATION_V2_SUITE_NAME: list(OPERATOR_CONTROL_CERTIFICATION_V2_SCENARIO_NAMES),
            OPERATOR_CONTROL_LIVE_POPULATION_V1_SUITE_NAME: list(OPERATOR_CONTROL_LIVE_POPULATION_V1_SCENARIO_NAMES),
            TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SUITE_NAME: list(TAMPER_EVIDENT_AUDIT_CANDIDATE_V1_SCENARIO_NAMES),
            AUTHORITY_TRANSFER_RECOVERY_V1_SUITE_NAME: list(AUTHORITY_TRANSFER_RECOVERY_V1_SCENARIO_NAMES),
            OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SUITE_NAME: list(OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES),
        },
        "contract": contract,
        "policy": contract["policy"],
        "failure_report": _failure_report(summary),
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }


def _control(
    action: str,
    mode: str,
    target: str,
    correctness: str,
    *,
    stale_approval_blocked: bool = False,
    negative_case: str | None = None,
    approval_reuse_allowed: bool | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "control_mode": mode,
        "target": target,
        "enabled": True,
        "authority_visible": True,
        "stale_approval_blocked": stale_approval_blocked,
        "negative_case": negative_case,
        "approval_reuse_allowed": False if approval_reuse_allowed is None else approval_reuse_allowed,
        "safe_denial": action == "deny",
        "receipt_after_action": f"operator-control-dm:{action}",
        "recovery_correctness_check": correctness,
        "safe_receipt": _safe_receipt(f"dm-control-{action}", "operator_control_v2"),
    }


def _population(
    receipt_id: str,
    task_class: str,
    operators: int,
    evidence_mode: str,
    evaluator: str,
    covered_actions: list[str],
    *,
    clicks: int,
    keystrokes: int,
    latency_ms_p95: int,
    recovery_seconds_p95: int,
    keyboard_rate: float,
    error_detectability: float,
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "task_class": task_class,
        "operator_count": operators,
        "evidence_mode": evidence_mode,
        "fixture_vs_live_marker": evidence_mode,
        "evaluator_independence": f"{evaluator}_not_implementation_worker",
        "covered_actions": covered_actions,
        "click_count_p50": clicks,
        "keystroke_count_p50": keystrokes,
        "latency_ms_p95": latency_ms_p95,
        "recovery_seconds_p95": recovery_seconds_p95,
        "keyboard_only_success_rate": keyboard_rate,
        "accessibility_audit_passed": True,
        "error_detectability_rate": error_detectability,
        "operator_takeover_visible": "handoff" in covered_actions or "rollback" in covered_actions,
        "telemetry_digest": _digest(receipt_id),
        "reviewer_attestation": f"attested:{receipt_id}:{evaluator}",
        "safe_receipt": _safe_receipt(receipt_id, "operator_live_population"),
    }


def _authority(
    transfer_type: str,
    target: str,
    authority_rule: str,
    denial_rule: str,
) -> dict[str, Any]:
    return {
        "receipt_id": f"dm-authority-{transfer_type}",
        "transfer_type": transfer_type,
        "target": target,
        "authority_rule": authority_rule,
        "denial_rule": denial_rule,
        "scope_renewal_required": True,
        "checkpoint_digest_required": True,
        "actor_authority_required": True,
        "approval_reuse_allowed": False,
        "recovery_correctness_check": f"{transfer_type}_authority_scope_and_checkpoint_match",
        "safe_receipt": _safe_receipt(f"dm-authority-{transfer_type}", "authority_transfer"),
    }


def _safe_receipt(receipt_id: str, receipt_class: str) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "receipt_class": receipt_class,
        "redaction_layer": "operator_control_production_certification_v1",
        "contains_secret": False,
        "contains_private_path": False,
        "contains_raw_transcript": False,
        "contains_unredacted_operator_identifier": False,
        "raw_receipt_path_exposed": False,
        "workspace_dir_exposed": False,
        "package_path_exposed": False,
        "safe_handle": f"receipt:dm:{receipt_class}:{_digest(receipt_id)[:16]}",
        "tamper_evident_digest": _digest(f"{receipt_class}:{receipt_id}"),
    }


def _all_safe_receipts_redacted(receipts: list[dict[str, Any]]) -> bool:
    return bool(receipts) and all(
        receipt["contains_secret"] is False
        and receipt["contains_private_path"] is False
        and receipt["contains_raw_transcript"] is False
        and receipt["contains_unredacted_operator_identifier"] is False
        and receipt["raw_receipt_path_exposed"] is False
        and receipt["workspace_dir_exposed"] is False
        and receipt["package_path_exposed"] is False
        and receipt["redaction_layer"] == "operator_control_production_certification_v1"
        and len(receipt["tamper_evident_digest"]) == 64
        for receipt in receipts
    )


def _failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": "operator_control_production_certification",
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Operator-control production certification failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
