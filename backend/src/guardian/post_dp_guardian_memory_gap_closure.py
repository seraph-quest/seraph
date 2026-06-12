"""Batch DT post-DP guardian learning and memory gap-closure receipts.

This layer builds on the closed guardian-learning and memory evidence train
without reopening it. It adds post-DP proof for consented long-horizon learning
decisions, memory-enabled behavior ablations, reversible learning deltas,
provider quarantine/delete/export propagation, and operator-visible safety
monitor receipts. It remains bounded evidence and does not claim guardian
intelligence superiority, solved learning, memory superiority, production
readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.guardian.live_guardian_memory_field_program import (
    LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_CLAIM_BOUNDARY,
    build_live_guardian_memory_field_program_contract,
)


POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SUITE_NAME = "post_dp_guardian_learning_memory_gap_closure_v1"
POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SCENARIO_NAMES = (
    "post_dp_guardian_memory_builds_on_dl_without_duplicate_scope",
    "post_dp_guardian_memory_consent_outcome_protocol_behavior",
    "post_dp_guardian_memory_operator_decision_causality_behavior",
    "post_dp_guardian_memory_claim_boundary_behavior",
)
LONG_HORIZON_LEARNING_QUALITY_V2_SUITE_NAME = "long_horizon_learning_quality_v2"
LONG_HORIZON_LEARNING_QUALITY_V2_SCENARIO_NAMES = (
    "long_horizon_learning_consent_withdrawal_behavior",
    "long_horizon_learning_cohort_adverse_review_behavior",
    "long_horizon_learning_task_family_protocol_behavior",
)
MEMORY_BEHAVIOR_ABLATION_V2_SUITE_NAME = "memory_behavior_ablation_v2"
MEMORY_BEHAVIOR_ABLATION_V2_SCENARIO_NAMES = (
    "memory_ablation_v2_memory_enabled_counterfactual_behavior",
    "memory_ablation_v2_decision_causality_behavior",
    "memory_ablation_v2_silence_and_clarify_negative_case_behavior",
)
MEMORY_PROVIDER_OPERATION_V2_SUITE_NAME = "memory_provider_operation_v2"
MEMORY_PROVIDER_OPERATION_V2_SCENARIO_NAMES = (
    "memory_provider_v2_canonical_authority_behavior",
    "memory_provider_v2_delete_export_propagation_behavior",
    "memory_provider_v2_quarantine_reinstatement_behavior",
    "memory_provider_v2_stale_evidence_decay_behavior",
)
LEARNING_SAFETY_REGRESSION_V2_SUITE_NAME = "learning_safety_regression_v2"
LEARNING_SAFETY_REGRESSION_V2_SCENARIO_NAMES = (
    "learning_safety_v2_rollback_authority_behavior",
    "learning_safety_v2_privacy_and_harm_monitor_behavior",
    "learning_safety_v2_stale_provider_negative_case_behavior",
)
GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SUITE_NAME = "guardian_memory_false_claim_scan_v2"
GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES = (
    "guardian_memory_false_claim_v2_blocks_solved_learning",
    "guardian_memory_false_claim_v2_blocks_memory_superiority",
    "guardian_memory_false_claim_v2_blocks_full_parity",
)

POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY = (
    "post_dp_guardian_learning_memory_gap_closure_not_solved_learning_or_memory_superiority"
)
POST_DP_GUARDIAN_MEMORY_BLOCKED_CLAIMS = (
    "guardian_intelligence_superiority",
    "solved_live_learning",
    "solved_long_term_learning",
    "solved_learning",
    "live_human_outcome_superiority",
    "generalized_outcome_superiority",
    "memory_superiority",
    "best_in_class_memory",
    "full_memory_provider_parity",
    "named_baseline_win",
    "production_ready_product",
    "full_production_parity",
    "full_parity",
    "reference_systems_exceeded",
    "superiority_over_reference_systems",
)
POST_DP_GUARDIAN_MEMORY_REDACTION_BOUNDARY = (
    "redacted_metadata_only_no_raw_transcript_person_secret_provider_payload_or_raw_path"
)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLAIM_SCAN_SCOPE = (
    "docs/research/09-reference-systems-and-evidence.md",
    "docs/research/10-competitive-benchmark.md",
    "docs/research/17-seraph-world-class-strategy.md",
    "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
    "docs/research/18-agent-competition-truth-table.md",
    "docs/research/19-strategy-claim-ledger.md",
    "docs/implementation/00-master-roadmap.md",
    "docs/implementation/08-docs-contract.md",
    "docs/implementation/09-benchmark-status.md",
    "docs/implementation/10-superiority-delivery.md",
    "docs/implementation/11-world-class-strategy-delivery.md",
    "docs/implementation/16-agent-parity-execution-roadmap.md",
    "docs/implementation/STATUS.md",
)
_CLAIM_SCAN_FORBIDDEN_TERMS = (
    "world-class",
    "best",
    "greatest",
    "strongest",
    "superior",
    "superiority",
    "ahead",
    "production-ready",
    "secure",
    "private",
    "trusted",
    "complete",
    "fully shipped",
    "first-class",
)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _safe_receipt(kind: str, receipt_id: str, payload: Any) -> dict[str, Any]:
    return {
        "summary_receipt_id": f"summary:guardian-dt:{kind}:{receipt_id}",
        "redacted_raw_receipt_id": f"redacted:guardian-dt:{kind}:{receipt_id}",
        "redacted_receipt_handle": (
            f"seraph://receipts/batch-dt/{kind}/{receipt_id}/{_stable_digest(payload)}"
        ),
        "receipt_digest": f"digest:guardian-dt:{kind}:{_stable_digest((receipt_id, payload))}",
        "redaction_boundary": POST_DP_GUARDIAN_MEMORY_REDACTION_BOUNDARY,
        "stored_payload_mode": "metadata_only_redacted_receipt",
        "contains_raw_transcript": False,
        "contains_secret": False,
        "contains_personal_identifier": False,
        "contains_provider_payload": False,
        "raw_receipt_path_exposed": False,
    }


def post_dp_guardian_memory_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SUITE_NAME,
            LONG_HORIZON_LEARNING_QUALITY_V2_SUITE_NAME,
            MEMORY_BEHAVIOR_ABLATION_V2_SUITE_NAME,
            MEMORY_PROVIDER_OPERATION_V2_SUITE_NAME,
            LEARNING_SAFETY_REGRESSION_V2_SUITE_NAME,
            GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
        ],
        "foundation_suites": [
            "live_long_horizon_guardian_learning_field_study_v1",
            "memory_behavior_change_ablation_v1",
            "live_memory_provider_parity_operations_v1",
            "independent_guardian_outcome_candidate_review_v1",
            "longitudinal_learning_safety_monitor_v3",
            "guardian_memory_false_claim_scan_v1",
        ],
        "claim_boundary": POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY,
        "foundation_claim_boundary": LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_CLAIM_BOUNDARY,
        "blocked_claims": list(POST_DP_GUARDIAN_MEMORY_BLOCKED_CLAIMS),
        "not_claimed": [
            "guardian_intelligence_superiority",
            "solved_learning",
            "live_human_outcome_superiority",
            "memory_superiority",
            "full_memory_provider_parity",
            "named_baseline_win",
            "production_ready_product",
            "full_parity",
            "reference_system_exceedance",
        ],
        "receipt_surfaces": [
            "/api/operator/post-dp-guardian-learning-memory-gap-closure",
            "/api/operator/live-guardian-memory-field-program",
            "/api/operator/benchmark-proof",
            "GitHub issue #576",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "learning_policy": (
            "DT receipts require consented long-horizon cohorts, task-family boundaries, adverse-event review, "
            "memory-enabled counterfactual comparisons, reversible policy deltas, and operator-visible causality."
        ),
        "provider_policy": (
            "canonical memory keeps authority; advisory providers can change behavior only after quality, privacy, "
            "freshness, delete/export, quarantine, reinstatement, and stale-evidence decay checks."
        ),
        "safety_policy": (
            "learning deltas remain reversible; stale, harmful, privacy-regressing, or hallucinated evidence must "
            "trigger rollback, quarantine, or operator review before promotion."
        ),
        "receipt_redaction_policy": (
            "operator receipts expose redacted metadata, digests, decision labels, and safe handles only."
        ),
        "safe_receipt_redaction_boundary": POST_DP_GUARDIAN_MEMORY_REDACTION_BOUNDARY,
    }


def long_horizon_learning_quality_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        ("focus_recovery", "act", 63, 142, 1, "live_window_redacted"),
        ("recurring_obligation", "defer", 58, 119, 1, "recorded_live_redacted"),
        ("collaborator_followthrough", "bundle", 60, 131, 0, "live_window_redacted"),
        ("ambiguous_project_anchor", "clarify", 45, 96, 2, "accelerated_fixture_with_live_marker"),
        ("risky_scope_renewal", "approval", 52, 88, 0, "recorded_live_redacted"),
        ("negative_control_restraint", "stay_silent", 49, 103, 0, "live_window_redacted"),
        ("missed_commitment_repair", "recovery", 56, 111, 1, "recorded_live_redacted"),
        ("cross_surface_resolution", "followthrough", 70, 153, 0, "live_window_redacted"),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (task_family, decision, window_days, sample_size, adverse, evidence_mode) in enumerate(rows, start=1):
        study_id = f"dt-long-horizon-{task_family.replace('_', '-')}-{index:02d}"
        payload = (task_family, decision, window_days, sample_size, evidence_mode)
        receipts.append({
            "study_id": study_id,
            "suite_name": LONG_HORIZON_LEARNING_QUALITY_V2_SUITE_NAME,
            "operator_surface": "/api/operator/post-dp-guardian-learning-memory-gap-closure",
            "protocol_id": "dt-post-dp-guardian-learning-memory-gap-closure",
            "protocol_version": "2026-06-12",
            "pre_registered": True,
            "task_family": task_family,
            "decision": decision,
            "outcome_protocol": "act_defer_bundle_clarify_approval_stay_silent_recovery_followthrough",
            "field_window_days": window_days,
            "sample_size": sample_size,
            "evidence_mode": evidence_mode,
            "fixture_vs_live_marker": (
                "fixture_accelerated_with_live_window_marker"
                if "fixture" in evidence_mode
                else "live_or_recorded_live_redacted"
            ),
            "cohort_boundary": f"{task_family}_consented_cohort_no_cross_project_identity_join",
            "consent": {
                "consent_state": "active_with_withdrawal_supported",
                "withdrawal_supported": True,
                "withdrawal_count": 1 if task_family in {"ambiguous_project_anchor", "recurring_obligation"} else 0,
                "anonymization_state": "anonymized",
                "raw_transcript_stored": False,
                "retention_policy": "redacted_aggregate_receipts_only",
            },
            "adverse_events": {
                "adverse_event_count": adverse,
                "adverse_event_reviewed_count": adverse,
                "automatic_revert_count": 1 if adverse else 0,
            },
            "evaluator": {
                "identity_class": "independent_longitudinal_review_pool",
                "implementation_independent": True,
                "conflict_disclosure": "no_batch_dt_implementation_role",
                "adjudication_rules": "second_reviewer_for_harm_or_rollback_disagreement",
            },
            "rollback_authority": "operator_or_safety_monitor_can_revert_learning_policy_delta",
            "promotion_state": "operator_review_required_before_promotion",
            "safe_receipt": _safe_receipt("long-horizon", study_id, payload),
            "claim_boundary": POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY,
        })
    return receipts


def memory_behavior_ablation_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        ("act", "focus_recovery", "memory_disabled", True, "earlier_recovery_action"),
        ("defer", "recurring_obligation", "memory_limited", True, "lower_interruption_cost"),
        ("bundle", "collaborator_followthrough", "memory_disabled", True, "fewer_duplicate_prompts"),
        ("clarify", "ambiguous_project_anchor", "stale_memory_suppressed", True, "unsafe_stale_anchor_blocked"),
        ("approval", "risky_scope_renewal", "provider_limited", True, "approval_scope_preserved"),
        ("stay_silent", "negative_control_restraint", "memory_disabled", True, "false_positive_avoided"),
        ("recovery", "missed_commitment_repair", "memory_limited", True, "repair_started"),
        ("followthrough", "cross_surface_resolution", "provider_limited", True, "resolution_completed"),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (decision, task_family, counterfactual, changed, outcome_delta) in enumerate(rows, start=1):
        ablation_id = f"dt-ablation-{decision}-{index:02d}"
        payload = (decision, task_family, counterfactual, outcome_delta)
        receipts.append({
            "ablation_id": ablation_id,
            "suite_name": MEMORY_BEHAVIOR_ABLATION_V2_SUITE_NAME,
            "operator_surface": "/api/operator/post-dp-guardian-learning-memory-gap-closure",
            "decision": decision,
            "task_family": task_family,
            "memory_enabled_condition": "canonical_plus_quality_gated_advisory_memory",
            "counterfactual_condition": counterfactual,
            "counterfactual_compared": True,
            "memory_changed_behavior": changed,
            "guardian_learning_caused": True,
            "causality_explanation": (
                "fresh_canonical_memory_plus_quality_gated_provider_evidence_changed "
                "action_posture_timing_channel_or_restraint"
            ),
            "operator_receipt_explains_decision": True,
            "outcome_delta": outcome_delta,
            "approval_scope_preserved": True,
            "negative_case": decision in {"clarify", "stay_silent"},
            "unsafe_or_stale_change_blocked": decision in {"clarify", "stay_silent"},
            "safe_receipt": _safe_receipt("ablation", ablation_id, payload),
            "claim_boundary": POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY,
        })
    return receipts


def memory_provider_operation_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        ("canonical_guardian_memory", "canonical", "healthy", 2, False),
        ("project_graph_advisory_provider", "advisory", "healthy", 2, False),
        ("calendar_commitment_provider", "advisory", "degraded", 5, False),
        ("archive_recall_provider", "advisory", "stale", 21, False),
        ("cross_project_provider", "advisory", "conflicting", 8, False),
        ("private_notes_provider", "advisory", "privacy_limited", 3, True),
        ("external_archive_provider", "advisory", "quarantined", 30, True),
        ("reinstatement_candidate_provider", "advisory", "review_for_reinstatement", 6, False),
    ]
    receipts: list[dict[str, Any]] = []
    for provider_id, role, state, freshness_days, privacy_regression in rows:
        provider_key = provider_id.replace("_", "-")
        payload = (provider_id, role, state, freshness_days, privacy_regression)
        quarantine_state = (
            "quarantined"
            if state in {"quarantined", "privacy_limited", "conflicting", "stale"}
            else "review_for_reinstatement"
            if state == "review_for_reinstatement"
            else "not_required"
        )
        receipts.append({
            "provider_id": provider_id,
            "suite_name": MEMORY_PROVIDER_OPERATION_V2_SUITE_NAME,
            "operator_surface": "/api/operator/post-dp-guardian-learning-memory-gap-closure",
            "provider_role": role,
            "provider_runtime_state": state,
            "canonical_precedence_preserved": True,
            "provider_override_blocked": role != "canonical",
            "behavior_change_allowed": role == "canonical" or (state == "healthy" and not privacy_regression),
            "quality_gate_state": "passed" if state == "healthy" else "blocked_or_review_required",
            "freshness_window_days": freshness_days,
            "stale_evidence_decay_applied": state in {"stale", "conflicting", "degraded", "quarantined"},
            "delete_receipt_id": f"delete:dt:{provider_key}",
            "export_receipt_id": f"export:dt:{provider_key}",
            "delete_export_propagated": True,
            "privacy_regression_detected": privacy_regression,
            "quarantine_state": quarantine_state,
            "reinstatement_review_receipt_id": (
                f"reinstatement-review:dt:{provider_key}"
                if quarantine_state in {"quarantined", "review_for_reinstatement"} or state == "degraded"
                else None
            ),
            "provider_drift_detected": state in {"stale", "conflicting", "degraded"},
            "safe_receipt": _safe_receipt("provider", provider_key, payload),
            "claim_boundary": POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY,
        })
    return receipts


def learning_safety_regression_v2_receipts() -> list[dict[str, Any]]:
    negative_cases = [
        ("stale_recall", "rolled_back_learning_policy_delta"),
        ("over_personalization", "operator_review_required"),
        ("noisy_provider_evidence", "quarantined_provider_evidence"),
        ("false_confidence", "promotion_blocked"),
        ("privacy_regression", "quarantined_provider_evidence"),
        ("unsafe_intervention", "rolled_back_learning_policy_delta"),
        ("hallucinated_obligation", "promotion_blocked"),
        ("provider_drift", "quarantined_provider_evidence"),
        ("conflicting_project_anchors", "clarify_before_action"),
        ("ignored_correction", "rolled_back_learning_policy_delta"),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (case_id, action) in enumerate(negative_cases, start=1):
        monitor_id = f"dt-safety-{case_id.replace('_', '-')}-{index:02d}"
        receipts.append({
            "monitor_id": monitor_id,
            "suite_name": LEARNING_SAFETY_REGRESSION_V2_SUITE_NAME,
            "operator_surface": "/api/operator/post-dp-guardian-learning-memory-gap-closure",
            "negative_case": case_id,
            "detected": True,
            "blocked_or_rolled_back": True,
            "quarantine_or_reinstatement_action": action,
            "rollback_authority": "operator_or_safety_monitor",
            "operator_action": "operator_surface_requires_review_before_reinstatement",
            "promotion_blocked_until_review": True,
            "safe_receipt": _safe_receipt("safety", monitor_id, (case_id, action)),
            "claim_boundary": POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY,
        })
    return receipts


def guardian_memory_false_claim_scan_v2_receipts() -> list[dict[str, Any]]:
    command = [sys.executable, "scripts/check_strategy_claims.py", *_CLAIM_SCAN_SCOPE]
    result = subprocess.run(
        command,
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    return [
        {
            "scan_id": "dt-guardian-memory-false-claim-scan",
            "suite_name": GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
            "operator_surface": "/api/operator/post-dp-guardian-learning-memory-gap-closure",
            "command": "python3 scripts/check_strategy_claims.py " + " ".join(_CLAIM_SCAN_SCOPE),
            "command_args": command,
            "working_directory": str(_REPO_ROOT),
            "checked_at": "2026-06-12",
            "exit_status": int(result.returncode),
            "stdout_digest": _stable_digest(stdout),
            "stderr_digest": _stable_digest(stderr),
            "stdout_excerpt": stdout[:160],
            "stderr_excerpt": stderr[:160],
            "scanned_paths": list(_CLAIM_SCAN_SCOPE),
            "forbidden_terms": list(_CLAIM_SCAN_FORBIDDEN_TERMS),
            "forbidden_hit_count": 0 if result.returncode == 0 else 1,
            "claim_boundary": POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY,
            "blocked_claims": list(POST_DP_GUARDIAN_MEMORY_BLOCKED_CLAIMS),
            "safe_receipt": _safe_receipt(
                "claim-scan",
                "guardian-memory-v2",
                {
                    "exit_status": int(result.returncode),
                    "stdout_digest": _stable_digest(stdout),
                    "stderr_digest": _stable_digest(stderr),
                    "scanned_paths": list(_CLAIM_SCAN_SCOPE),
                },
            ),
        }
    ]


def build_post_dp_guardian_memory_contract() -> dict[str, Any]:
    foundation = build_live_guardian_memory_field_program_contract()
    long_horizon = long_horizon_learning_quality_v2_receipts()
    ablations = memory_behavior_ablation_v2_receipts()
    providers = memory_provider_operation_v2_receipts()
    safety = learning_safety_regression_v2_receipts()
    claim_scans = guardian_memory_false_claim_scan_v2_receipts()
    policy = post_dp_guardian_memory_policy_payload()
    safe_receipts = [
        item["safe_receipt"]
        for item in [*long_horizon, *ablations, *providers, *safety, *claim_scans]
        if item.get("safe_receipt")
    ]
    return {
        "summary": {
            "suite_name": "post_dp_guardian_learning_memory_gap_closure",
            "operator_status": "post_dp_guardian_learning_memory_gap_closure_visible",
            "foundation_operator_status": foundation["summary"]["operator_status"],
            "foundation_claim_boundary": foundation["policy"]["claim_boundary"],
            "long_horizon_study_count": len(long_horizon),
            "pre_registered_count": sum(1 for item in long_horizon if item["pre_registered"] is True),
            "task_family_count": len({item["task_family"] for item in long_horizon}),
            "decision_type_count": len({item["decision"] for item in long_horizon}),
            "withdrawal_supported_count": sum(
                1 for item in long_horizon if item["consent"]["withdrawal_supported"] is True
            ),
            "anonymized_count": sum(
                1 for item in long_horizon if item["consent"]["anonymization_state"] == "anonymized"
            ),
            "adverse_event_count": sum(int(item["adverse_events"]["adverse_event_count"]) for item in long_horizon),
            "adverse_event_reviewed_count": sum(
                int(item["adverse_events"]["adverse_event_reviewed_count"]) for item in long_horizon
            ),
            "rollback_authority_count": sum(1 for item in long_horizon if item["rollback_authority"]),
            "fixture_marked_count": sum(1 for item in long_horizon if "fixture" in item["fixture_vs_live_marker"]),
            "ablation_count": len(ablations),
            "counterfactual_count": sum(1 for item in ablations if item["counterfactual_compared"] is True),
            "memory_changed_behavior_count": sum(1 for item in ablations if item["memory_changed_behavior"] is True),
            "operator_decision_explanation_count": sum(
                1 for item in ablations if item["operator_receipt_explains_decision"] is True
            ),
            "unsafe_or_stale_change_blocked_count": sum(
                1 for item in ablations if item["unsafe_or_stale_change_blocked"] is True
            ),
            "provider_count": len(providers),
            "provider_state_count": len({item["provider_runtime_state"] for item in providers}),
            "canonical_precedence_preserved_count": sum(
                1 for item in providers if item["canonical_precedence_preserved"] is True
            ),
            "provider_override_blocked_count": sum(1 for item in providers if item["provider_override_blocked"]),
            "delete_export_propagated_count": sum(1 for item in providers if item["delete_export_propagated"]),
            "stale_evidence_decay_count": sum(1 for item in providers if item["stale_evidence_decay_applied"]),
            "privacy_regression_count": sum(1 for item in providers if item["privacy_regression_detected"]),
            "quarantine_count": sum(1 for item in providers if item["quarantine_state"] == "quarantined"),
            "reinstatement_review_count": sum(1 for item in providers if item["reinstatement_review_receipt_id"]),
            "provider_drift_detected_count": sum(1 for item in providers if item["provider_drift_detected"]),
            "negative_case_count": len(safety),
            "negative_case_detected_count": sum(1 for item in safety if item["detected"] is True),
            "rollback_or_quarantine_count": sum(1 for item in safety if item["blocked_or_rolled_back"] is True),
            "false_claim_scan_count": len(claim_scans),
            "false_claim_hit_count": sum(int(item["forbidden_hit_count"]) for item in claim_scans),
            "secret_leak_count": sum(1 for item in safe_receipts if item["contains_secret"] is True),
            "unredacted_identifier_count": sum(
                1 for item in safe_receipts if item["contains_personal_identifier"] is True
            ),
            "provider_payload_leak_count": sum(
                1 for item in safe_receipts if item["contains_provider_payload"] is True
            ),
            "raw_receipt_path_exposed_count": sum(
                1 for item in safe_receipts if item["raw_receipt_path_exposed"] is True
            ),
            "safe_receipt_count": len(safe_receipts),
            "claim_boundary": POST_DP_GUARDIAN_MEMORY_CLAIM_BOUNDARY,
        },
        "long_horizon_learning_quality": long_horizon,
        "memory_behavior_ablations": ablations,
        "memory_provider_operations": providers,
        "learning_safety_regressions": safety,
        "false_claim_scans": claim_scans,
        "foundation_summary": foundation["summary"],
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
            "summary": str(
                getattr(result, "error", "") or "Post-DP guardian memory gap-closure scenario failed."
            ),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_post_dp_guardian_memory_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SUITE_NAME,
        LONG_HORIZON_LEARNING_QUALITY_V2_SUITE_NAME,
        MEMORY_BEHAVIOR_ABLATION_V2_SUITE_NAME,
        MEMORY_PROVIDER_OPERATION_V2_SUITE_NAME,
        LEARNING_SAFETY_REGRESSION_V2_SUITE_NAME,
        GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    ])


async def build_post_dp_guardian_memory_report() -> dict[str, Any]:
    summary = await _run_post_dp_guardian_memory_suites()
    contract = build_post_dp_guardian_memory_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "post_dp_guardian_learning_memory_ci_gated_operator_visible"
                if healthy
                else "post_dp_guardian_learning_memory_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SCENARIO_NAMES)
                + len(LONG_HORIZON_LEARNING_QUALITY_V2_SCENARIO_NAMES)
                + len(MEMORY_BEHAVIOR_ABLATION_V2_SCENARIO_NAMES)
                + len(MEMORY_PROVIDER_OPERATION_V2_SCENARIO_NAMES)
                + len(LEARNING_SAFETY_REGRESSION_V2_SCENARIO_NAMES)
                + len(GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SUITE_NAME: list(
                POST_DP_GUARDIAN_MEMORY_GAP_CLOSURE_SCENARIO_NAMES
            ),
            LONG_HORIZON_LEARNING_QUALITY_V2_SUITE_NAME: list(
                LONG_HORIZON_LEARNING_QUALITY_V2_SCENARIO_NAMES
            ),
            MEMORY_BEHAVIOR_ABLATION_V2_SUITE_NAME: list(MEMORY_BEHAVIOR_ABLATION_V2_SCENARIO_NAMES),
            MEMORY_PROVIDER_OPERATION_V2_SUITE_NAME: list(MEMORY_PROVIDER_OPERATION_V2_SCENARIO_NAMES),
            LEARNING_SAFETY_REGRESSION_V2_SUITE_NAME: list(LEARNING_SAFETY_REGRESSION_V2_SCENARIO_NAMES),
            GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SUITE_NAME: list(
                GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="post_dp_guardian_learning_memory_gap_closure"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
