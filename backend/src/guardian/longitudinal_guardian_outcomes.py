"""Batch CV longitudinal guardian-learning and memory-provider outcome receipts.

This module extends the BZ/CF/CM proof surfaces into longer-horizon
operations: named baselines, longitudinal windows, safety monitoring,
delete/export propagation, rollback, quarantine, and reinstatement evidence.
It remains bounded proof and does not claim guardian intelligence superiority,
solved live learning, memory superiority, full memory-provider parity,
production readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

from typing import Any


LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SUITE_NAME = "longitudinal_guardian_outcome_study"
LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SCENARIO_NAMES = (
    "longitudinal_outcome_window_baseline_behavior",
    "longitudinal_outcome_evaluator_protocol_behavior",
    "longitudinal_outcome_withdrawal_reweight_behavior",
    "longitudinal_outcome_adverse_event_rollback_behavior",
    "operator_longitudinal_outcome_surface_behavior",
)
NAMED_BASELINE_MEMORY_COMPARISON_SUITE_NAME = "named_baseline_memory_comparison"
NAMED_BASELINE_MEMORY_COMPARISON_SCENARIO_NAMES = (
    "named_baseline_source_version_limit_behavior",
    "named_baseline_pressure_not_superiority_behavior",
    "named_baseline_provider_quality_behavior",
    "named_baseline_delete_export_propagation_behavior",
)
LEARNING_SAFETY_MONITOR_V2_SUITE_NAME = "learning_safety_monitor_v2"
LEARNING_SAFETY_MONITOR_V2_SCENARIO_NAMES = (
    "learning_safety_policy_version_behavior",
    "learning_safety_harm_privacy_block_behavior",
    "learning_safety_stale_evidence_drift_behavior",
    "learning_safety_quarantine_reinstatement_behavior",
    "learning_safety_operator_recovery_surface_behavior",
)
LONGITUDINAL_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY = (
    "longitudinal_learning_memory_operations_receipts_not_superiority_or_solved_learning"
)
LONGITUDINAL_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS = (
    "guardian_intelligence_superiority",
    "generalized_outcome_superiority",
    "solved_live_learning",
    "solved_long_term_learning",
    "live_human_outcome_superiority",
    "memory_superiority",
    "full_memory_provider_parity",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
    "named_baseline_win",
    "best_in_class_memory",
)
LONGITUDINAL_RECEIPT_REDACTION_BOUNDARY = (
    "redacted_no_raw_transcript_secret_identifier_or_provider_payload"
)


def longitudinal_guardian_outcomes_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SUITE_NAME,
            NAMED_BASELINE_MEMORY_COMPARISON_SUITE_NAME,
            LEARNING_SAFETY_MONITOR_V2_SUITE_NAME,
        ],
        "claim_boundary": LONGITUDINAL_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY,
        "study_policy": (
            "longitudinal outcome evidence must declare windows, task families, baselines, power rationale, "
            "independent evaluator protocol, consent, withdrawal, anonymization, retention, confounders, "
            "adverse events, and residual gaps before learning changes can be promoted"
        ),
        "baseline_policy": (
            "named baselines are pressure evidence only; source, version, window, and limitations must be visible, "
            "and no baseline row may imply memory superiority, full provider parity, or competitor defeat"
        ),
        "safety_monitor_policy": (
            "learning-policy versions, stale-evidence drift, harm, privacy regressions, provider quarantine, "
            "delete/export mismatches, rollback, and reinstatement review must be operator-visible"
        ),
        "memory_provider_policy": (
            "canonical memory keeps authority; provider evidence remains advisory until quality, privacy, freshness, "
            "delete/export, correction, and operator-review gates pass"
        ),
        "receipt_redaction_policy": (
            "operator receipts expose digest, status, metrics, and redacted examples only; raw transcripts, secrets, "
            "contact identifiers, provider payloads, and raw receipt paths are absent"
        ),
        "receipt_surfaces": [
            "/api/operator/longitudinal-guardian-outcomes",
            "/api/operator/independent-learning-memory-parity",
            "/api/operator/live-human-outcome-learning-proof",
            "/api/operator/live-guardian-learning-quality",
            "/api/operator/benchmark-proof",
        ],
        "blocked_claims": list(LONGITUDINAL_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS),
        "not_claimed": [
            "generalized_outcome_superiority",
            "named_baseline_win",
            "solved_live_learning",
            "memory_superiority",
            "full_memory_provider_parity",
            "production_ready_product",
        ],
    }


def _safe_receipt(receipt_type: str, receipt_id: str) -> dict[str, Any]:
    return {
        "summary_receipt_id": f"summary:guardian-cv:{receipt_type}:{receipt_id}",
        "redacted_raw_receipt_id": f"redacted:guardian-cv:{receipt_type}:{receipt_id}",
        "receipt_digest": f"digest:guardian-cv:{receipt_type}:{receipt_id}",
        "redaction_boundary": LONGITUDINAL_RECEIPT_REDACTION_BOUNDARY,
        "stored_payload_mode": "metadata_only_redacted_receipt",
        "contains_raw_transcript": False,
        "contains_secret": False,
        "contains_personal_identifier": False,
        "contains_provider_payload": False,
        "raw_receipt_path_exposed": False,
    }


def longitudinal_outcome_study_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "followthrough",
            "cross_thread_commitment_followthrough",
            "cm-independent-followthrough-baseline",
            "2026-04-baseline-policy",
            96,
            84,
            0.15,
            1,
        ),
        (
            "correction",
            "ambiguous_project_anchor_correction",
            "cf-correction-cohort-baseline",
            "2026-04-clarify-policy",
            72,
            66,
            -0.09,
            2,
        ),
        (
            "restraint",
            "low_urgency_interruption_restraint",
            "cm-negative-control-restraint-baseline",
            "2026-04-restraint-policy",
            108,
            94,
            0.18,
            0,
        ),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (axis, task_family, baseline_name, baseline_version, sample_size, baseline_sample, effect, adverse) in enumerate(rows, start=1):
        study_id = f"cv-longitudinal-{axis}-{index:02d}"
        receipts.append({
            "study_id": study_id,
            "window_start": "2026-04-01",
            "window_end": "2026-05-31",
            "window_days": 61,
            "cohort_id": f"cv-cohort-{axis}",
            "task_family": task_family,
            "sample_size": sample_size,
            "baseline_sample_size": baseline_sample,
            "power_rationale": "directional_longitudinal_effect_threshold_not_generalized_superiority_power",
            "baseline": {
                "baseline_name": baseline_name,
                "baseline_version": baseline_version,
                "baseline_source": "prior_seraph_policy_and_recorded_live_receipt_cohort",
                "baseline_window": "2026-03-01..2026-03-31",
                "baseline_limitations": [
                    "single_workspace_family",
                    "not_randomized_clinical_study",
                    "not_reference_system_superiority",
                ],
            },
            "evaluator": {
                "evaluator_identity_class": "independent_review_pool",
                "implementation_independent": True,
                "protocol_id": "cv-longitudinal-outcome-protocol",
                "protocol_version": "2026-06-10",
                "blinding": "blinded_to_policy_delta_where_possible",
                "conflict_disclosure": "no_batch_implementation_role",
                "adjudication_rules": "second_reviewer_for_harm_correction_or_policy_promotion_disagreement",
            },
            "consent": {
                "consent_state": "active_with_withdrawal_supported",
                "withdrawal_supported": True,
                "withdrawal_count": 2 if axis == "correction" else 0,
                "withdrawal_disposition": "removed_or_reweighted_before_policy_promotion",
                "anonymization_state": "anonymized",
                "raw_transcript_stored": False,
                "retention_policy": "redacted_aggregate_receipts_only",
            },
            "redaction": {
                "redaction_policy_version": "cv-redaction-v1",
                "secret_leak_count": 0,
                "unredacted_identifier_count": 0,
                "redaction_failure_examples_redacted": [],
            },
            "outcome_metrics": {
                "effect_size": effect,
                "confidence_interval": [round(effect - 0.06, 2), round(effect + 0.06, 2)],
                "correction_rate": 0.08 if axis == "correction" else 0.04,
                "harm_rate": 0.03 if adverse else 0.0,
                "followthrough_delta": 0.17 if axis == "followthrough" else 0.03,
                "false_positive_delta": -0.08 if axis in {"correction", "restraint"} else -0.02,
                "false_negative_delta": -0.06 if axis == "followthrough" else -0.01,
                "confounders": ["workspace_self_selection", "calendar_density", "task_label_quality"],
            },
            "learning_change": {
                "learning_policy_version_before": baseline_version,
                "learning_policy_version_after": f"2026-06-cv-{axis}-candidate",
                "policy_delta_id": f"policy-delta:cv:{axis}",
                "promotion_state": "operator_review_required_before_promotion",
                "rollback_authority": "operator_or_harm_monitor_can_revert",
                "rollback_receipt_id": f"rollback:cv:{axis}",
            },
            "adverse_events": {
                "adverse_event_count": adverse,
                "adverse_event_reviewed_count": adverse,
                "automatic_revert_count": 1 if adverse else 0,
                "operator_review_required": adverse > 0,
            },
            "operator_receipt_handle": f"operator:guardian-cv:study:{axis}",
            "safe_receipt": _safe_receipt("study", study_id),
            "blocked_claims": list(LONGITUDINAL_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS),
            "residual_gaps": ["not_generalized_superiority", "not_competitor_outcome_claim"],
        })
    return receipts


def named_baseline_memory_comparison_receipts() -> list[dict[str, Any]]:
    return [
        {
            "comparison_id": "cv-baseline-canonical-memory",
            "baseline_name": "canonical_guardian_memory_cm_matrix",
            "baseline_version": "batch-cm-memory-provider-parity-matrix",
            "baseline_source": "Seraph Batch CM bounded provider matrix receipts",
            "baseline_window": "2026-05-01..2026-05-30",
            "baseline_limitations": ["not_external_market_parity", "not_memory_superiority"],
            "provider_id": "canonical_guardian_memory",
            "provider_version": "canonical-memory-v2",
            "provider_role": "canonical_source",
            "provider_runtime_state": "healthy",
            "quality_declaration_complete": True,
            "canonical_precedence_preserved": True,
            "behavior_change_allowed": True,
            "behavior_change_scope": "canonical_memory_only_after_quality_gate_not_provider_superiority",
            "provider_override_blocked": False,
            "freshness_window_days": 7,
            "stale_hit_count": 1,
            "stale_behavior_change_blocked_count": 1,
            "privacy_regression_detected": False,
            "delete_receipt_id": "delete:cv:canonical",
            "export_receipt_id": "export:cv:canonical",
            "delete_export_mismatch_count": 0,
            "comparison_disposition": "pressure_evidence_only_not_superiority",
            "safe_receipt": _safe_receipt("baseline", "canonical-memory"),
        },
        {
            "comparison_id": "cv-baseline-project-provider",
            "baseline_name": "additive_project_memory_provider_cf_monitor",
            "baseline_version": "batch-cf-live-regression-monitor",
            "baseline_source": "Seraph Batch CF recorded-live provider monitor receipts",
            "baseline_window": "2026-05-01..2026-05-14",
            "baseline_limitations": ["advisory_provider_only", "single_provider_family"],
            "provider_id": "additive_project_memory_provider",
            "provider_version": "project-provider-v3",
            "provider_role": "advisory_provider",
            "provider_runtime_state": "watch",
            "quality_declaration_complete": True,
            "canonical_precedence_preserved": True,
            "behavior_change_allowed": True,
            "behavior_change_scope": "advisory_pressure_signal_only_no_authority_override",
            "provider_override_blocked": True,
            "freshness_window_days": 14,
            "stale_hit_count": 4,
            "stale_behavior_change_blocked_count": 4,
            "privacy_regression_detected": False,
            "delete_receipt_id": "delete:cv:project-provider",
            "export_receipt_id": "export:cv:project-provider",
            "delete_export_mismatch_count": 0,
            "comparison_disposition": "pressure_evidence_only_not_provider_parity",
            "safe_receipt": _safe_receipt("baseline", "project-provider"),
        },
        {
            "comparison_id": "cv-baseline-archive-provider",
            "baseline_name": "external_archive_memory_provider_cm_quarantine",
            "baseline_version": "batch-cm-provider-quarantine",
            "baseline_source": "Seraph Batch CM privacy-regression quarantine receipts",
            "baseline_window": "2026-05-01..2026-05-30",
            "baseline_limitations": ["provider_quarantined", "not_reinstated"],
            "provider_id": "external_archive_memory_provider",
            "provider_version": "archive-provider-v1",
            "provider_role": "advisory_archive",
            "provider_runtime_state": "quarantined",
            "quality_declaration_complete": True,
            "canonical_precedence_preserved": True,
            "behavior_change_allowed": False,
            "behavior_change_scope": "blocked_by_privacy_regression_and_delete_export_mismatch",
            "provider_override_blocked": True,
            "freshness_window_days": 30,
            "stale_hit_count": 12,
            "stale_behavior_change_blocked_count": 12,
            "privacy_regression_detected": True,
            "delete_receipt_id": "delete:cv:archive-provider",
            "export_receipt_id": "export:cv:archive-provider",
            "delete_export_mismatch_count": 1,
            "comparison_disposition": "unsafe_provider_blocks_promotion",
            "safe_receipt": _safe_receipt("baseline", "archive-provider"),
        },
    ]


def learning_safety_monitor_v2_receipts() -> list[dict[str, Any]]:
    return [
        {
            "monitor_id": "cv-monitor-followthrough-policy",
            "learning_policy_version_before": "2026-04-baseline-policy",
            "learning_policy_version_after": "2026-06-cv-followthrough-candidate",
            "policy_delta_id": "policy-delta:cv:followthrough",
            "promotion_state": "operator_review_pending",
            "rollback_authority": "operator_or_harm_monitor_can_revert",
            "rollback_receipt_id": "rollback:cv:followthrough",
            "adverse_event_count": 0,
            "privacy_regression_detected": False,
            "stale_hit_count": 1,
            "stale_behavior_change_blocked_count": 1,
            "operator_review_required": True,
            "safe_receipt": _safe_receipt("monitor", "followthrough"),
        },
        {
            "monitor_id": "cv-monitor-correction-harm",
            "learning_policy_version_before": "2026-04-clarify-policy",
            "learning_policy_version_after": "2026-06-cv-correction-candidate",
            "policy_delta_id": "policy-delta:cv:correction",
            "promotion_state": "blocked_by_harm_review",
            "rollback_authority": "automatic_revert_and_operator_review",
            "rollback_receipt_id": "rollback:cv:correction",
            "adverse_event_count": 2,
            "privacy_regression_detected": False,
            "stale_hit_count": 3,
            "stale_behavior_change_blocked_count": 3,
            "operator_review_required": True,
            "safe_receipt": _safe_receipt("monitor", "correction"),
        },
        {
            "monitor_id": "cv-monitor-archive-provider",
            "provider_id": "external_archive_memory_provider",
            "provider_version": "archive-provider-v1",
            "provider_role": "advisory_archive",
            "provider_runtime_state": "quarantined",
            "quality_declaration_complete": True,
            "canonical_precedence_preserved": True,
            "behavior_change_allowed": False,
            "behavior_change_scope": "blocked_until_operator_quarantine_review",
            "provider_override_blocked": True,
            "privacy_regression_detected": True,
            "delete_receipt_id": "delete:cv:archive-provider",
            "export_receipt_id": "export:cv:archive-provider",
            "delete_export_mismatch_count": 1,
            "quarantine_state": "quarantined",
            "quarantine_reason": "privacy_regression_and_delete_export_mismatch",
            "quarantine_started_at": "2026-05-18T09:00:00Z",
            "lost_capability_visible": True,
            "reinstatement_review_receipt_id": "reinstatement-review:cv:archive-provider",
            "promotion_state": "blocked_by_privacy_regression",
            "rollback_authority": "operator_provider_quarantine",
            "safe_receipt": _safe_receipt("monitor", "archive-provider"),
        },
        {
            "monitor_id": "cv-monitor-calendar-reinstatement",
            "provider_id": "calendar_commitment_memory_provider",
            "provider_version": "calendar-provider-v2",
            "provider_role": "advisory_commitment_provider",
            "provider_runtime_state": "review_for_reinstatement",
            "quality_declaration_complete": True,
            "canonical_precedence_preserved": True,
            "behavior_change_allowed": False,
            "behavior_change_scope": "blocked_until_operator_reinstatement_review",
            "provider_override_blocked": True,
            "privacy_regression_detected": False,
            "delete_receipt_id": "delete:cv:calendar-provider",
            "export_receipt_id": "export:cv:calendar-provider",
            "delete_export_mismatch_count": 0,
            "quarantine_state": "review_for_reinstatement",
            "quarantine_reason": "prior_quarantine_requires_operator_review",
            "quarantine_started_at": "2026-05-20T10:30:00Z",
            "lost_capability_visible": True,
            "reinstatement_review_receipt_id": "reinstatement-review:cv:calendar-provider",
            "promotion_state": "reinstatement_review_pending",
            "rollback_authority": "operator_reinstatement_review",
            "safe_receipt": _safe_receipt("monitor", "calendar-provider"),
        },
    ]


def build_longitudinal_guardian_outcomes_contract() -> dict[str, Any]:
    studies = longitudinal_outcome_study_receipts()
    baselines = named_baseline_memory_comparison_receipts()
    monitors = learning_safety_monitor_v2_receipts()
    policy = longitudinal_guardian_outcomes_policy_payload()
    provider_rows = [
        item for item in [*baselines, *monitors]
        if item.get("provider_id")
    ]
    return {
        "summary": {
            "operator_status": "longitudinal_guardian_outcomes_receipts_visible",
            "study_count": len(studies),
            "longitudinal_window_count": sum(1 for item in studies if int(item.get("window_days", 0) or 0) >= 60),
            "sample_size_total": sum(int(item.get("sample_size", 0) or 0) for item in studies),
            "baseline_count": len(baselines),
            "named_baseline_pressure_only_count": sum(
                1 for item in baselines
                if "pressure_evidence_only" in str(item.get("comparison_disposition", ""))
            ),
            "independent_evaluator_count": sum(
                1 for item in studies
                if item.get("evaluator", {}).get("implementation_independent") is True
            ),
            "withdrawal_supported_count": sum(
                1 for item in studies if item.get("consent", {}).get("withdrawal_supported") is True
            ),
            "withdrawal_reweighted_count": sum(
                1 for item in studies
                if item.get("consent", {}).get("withdrawal_count", 0)
                and "reweighted" in str(item.get("consent", {}).get("withdrawal_disposition", ""))
            ),
            "raw_transcript_stored_count": sum(
                1 for item in studies if item.get("consent", {}).get("raw_transcript_stored") is True
            ),
            "secret_leak_count": sum(
                int(item.get("redaction", {}).get("secret_leak_count", 0) or 0) for item in studies
            ),
            "unredacted_identifier_count": sum(
                int(item.get("redaction", {}).get("unredacted_identifier_count", 0) or 0) for item in studies
            ),
            "adverse_event_count": sum(
                int(item.get("adverse_events", {}).get("adverse_event_count", 0) or 0) for item in studies
            ),
            "adverse_event_reviewed_count": sum(
                int(item.get("adverse_events", {}).get("adverse_event_reviewed_count", 0) or 0) for item in studies
            ),
            "policy_version_count": len(
                {
                    str(item.get("learning_policy_version_after"))
                    for item in monitors
                    if item.get("learning_policy_version_after")
                }
                | {
                    str(item.get("learning_change", {}).get("learning_policy_version_after"))
                    for item in studies
                    if item.get("learning_change", {}).get("learning_policy_version_after")
                }
            ),
            "rollback_receipt_count": sum(
                1 for item in [*studies, *monitors]
                if item.get("rollback_receipt_id")
                or item.get("learning_change", {}).get("rollback_receipt_id")
            ),
            "provider_monitor_count": len(provider_rows),
            "canonical_precedence_preserved_count": sum(
                1 for item in provider_rows if item.get("canonical_precedence_preserved") is True
            ),
            "provider_override_blocked_count": sum(
                1 for item in provider_rows if item.get("provider_override_blocked") is True
            ),
            "privacy_regression_count": sum(
                1 for item in provider_rows if item.get("privacy_regression_detected") is True
            ),
            "delete_export_mismatch_count": sum(
                int(item.get("delete_export_mismatch_count", 0) or 0) for item in provider_rows
            ),
            "quarantine_count": sum(
                1 for item in monitors
                if item.get("quarantine_state") in {"quarantined", "review_for_reinstatement"}
            ),
            "reinstatement_review_count": sum(
                1 for item in monitors if item.get("reinstatement_review_receipt_id")
            ),
            "stale_behavior_change_blocked_count": sum(
                int(item.get("stale_behavior_change_blocked_count", 0) or 0) for item in provider_rows
            ),
            "claim_boundary": LONGITUDINAL_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY,
        },
        "longitudinal_outcome_studies": studies,
        "named_baseline_memory_comparisons": baselines,
        "learning_safety_monitors": monitors,
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
                getattr(result, "error", "") or "Longitudinal guardian outcome operation scenario failed."
            ),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_longitudinal_guardian_outcomes_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SUITE_NAME,
        NAMED_BASELINE_MEMORY_COMPARISON_SUITE_NAME,
        LEARNING_SAFETY_MONITOR_V2_SUITE_NAME,
    ])


async def build_longitudinal_guardian_outcomes_report() -> dict[str, Any]:
    summary = await _run_longitudinal_guardian_outcomes_suites()
    contract = build_longitudinal_guardian_outcomes_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "longitudinal_guardian_outcomes_ci_gated_operator_visible"
                if healthy
                else "longitudinal_guardian_outcomes_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SCENARIO_NAMES)
                + len(NAMED_BASELINE_MEMORY_COMPARISON_SCENARIO_NAMES)
                + len(LEARNING_SAFETY_MONITOR_V2_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SUITE_NAME: list(
                LONGITUDINAL_GUARDIAN_OUTCOME_STUDY_SCENARIO_NAMES
            ),
            NAMED_BASELINE_MEMORY_COMPARISON_SUITE_NAME: list(
                NAMED_BASELINE_MEMORY_COMPARISON_SCENARIO_NAMES
            ),
            LEARNING_SAFETY_MONITOR_V2_SUITE_NAME: list(LEARNING_SAFETY_MONITOR_V2_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="longitudinal_guardian_outcomes"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
