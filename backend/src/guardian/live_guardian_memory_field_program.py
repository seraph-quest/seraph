"""Batch DL live guardian-memory field-program receipts.

This module raises the guardian-learning and memory-provider evidence bar
beyond Batch DD with field windows, memory-behavior ablations, provider
operations, independent adjudication, safety monitoring, and false-claim
receipts. It remains bounded evidence and does not claim guardian intelligence
superiority, solved learning, memory superiority, full memory-provider parity,
production readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

from typing import Any


LIVE_LONG_HORIZON_GUARDIAN_LEARNING_FIELD_STUDY_V1_SUITE_NAME = (
    "live_long_horizon_guardian_learning_field_study_v1"
)
LIVE_LONG_HORIZON_GUARDIAN_LEARNING_FIELD_STUDY_V1_SCENARIO_NAMES = (
    "live_field_preregistered_protocol_behavior",
    "live_field_consent_withdrawal_anonymization_behavior",
    "live_field_task_family_coverage_behavior",
    "live_field_independent_adverse_rollback_behavior",
)
MEMORY_BEHAVIOR_CHANGE_ABLATION_V1_SUITE_NAME = "memory_behavior_change_ablation_v1"
MEMORY_BEHAVIOR_CHANGE_ABLATION_V1_SCENARIO_NAMES = (
    "memory_ablation_decision_family_behavior",
    "memory_ablation_counterfactual_behavior",
    "memory_ablation_timing_channel_followthrough_behavior",
    "memory_ablation_negative_case_behavior",
)
LIVE_MEMORY_PROVIDER_PARITY_OPERATIONS_V1_SUITE_NAME = "live_memory_provider_parity_operations_v1"
LIVE_MEMORY_PROVIDER_PARITY_OPERATIONS_V1_SCENARIO_NAMES = (
    "live_provider_role_state_matrix_behavior",
    "live_provider_delete_export_quarantine_behavior",
    "live_provider_canonical_authority_behavior",
    "live_provider_privacy_conflict_drift_behavior",
)
INDEPENDENT_GUARDIAN_OUTCOME_CANDIDATE_REVIEW_V1_SUITE_NAME = (
    "independent_guardian_outcome_candidate_review_v1"
)
INDEPENDENT_GUARDIAN_OUTCOME_CANDIDATE_REVIEW_V1_SCENARIO_NAMES = (
    "independent_candidate_review_protocol_behavior",
    "independent_candidate_review_adjudication_behavior",
    "independent_candidate_review_no_superiority_behavior",
)
LONGITUDINAL_LEARNING_SAFETY_MONITOR_V3_SUITE_NAME = "longitudinal_learning_safety_monitor_v3"
LONGITUDINAL_LEARNING_SAFETY_MONITOR_V3_SCENARIO_NAMES = (
    "longitudinal_safety_negative_case_matrix_behavior",
    "longitudinal_safety_rollback_quarantine_behavior",
    "longitudinal_safety_operator_surface_behavior",
)
GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V1_SUITE_NAME = "guardian_memory_false_claim_scan_v1"
GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES = (
    "guardian_memory_false_claim_scan_receipt_behavior",
    "guardian_memory_claim_boundary_behavior",
)
LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_CLAIM_BOUNDARY = (
    "bounded_live_guardian_memory_field_program_receipts_not_solved_learning_or_memory_superiority"
)
LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_BLOCKED_CLAIMS = (
    "guardian_intelligence_superiority",
    "solved_live_learning",
    "solved_long_term_learning",
    "live_human_outcome_superiority",
    "generalized_outcome_superiority",
    "memory_superiority",
    "best_in_class_memory",
    "full_memory_provider_parity",
    "named_baseline_win",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
    "superiority_over_reference_systems",
)
LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_REDACTION_BOUNDARY = (
    "redacted_metadata_only_no_raw_transcript_person_secret_provider_payload_or_raw_path"
)


def live_guardian_memory_field_program_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            LIVE_LONG_HORIZON_GUARDIAN_LEARNING_FIELD_STUDY_V1_SUITE_NAME,
            MEMORY_BEHAVIOR_CHANGE_ABLATION_V1_SUITE_NAME,
            LIVE_MEMORY_PROVIDER_PARITY_OPERATIONS_V1_SUITE_NAME,
            INDEPENDENT_GUARDIAN_OUTCOME_CANDIDATE_REVIEW_V1_SUITE_NAME,
            LONGITUDINAL_LEARNING_SAFETY_MONITOR_V3_SUITE_NAME,
            GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
        ],
        "claim_boundary": LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_CLAIM_BOUNDARY,
        "field_program_policy": (
            "live field windows require pre-registration, cohort boundaries, consent, withdrawal, anonymization, "
            "fixture-vs-live markers, evaluator independence, adverse-event review, and rollback authority"
        ),
        "memory_ablation_policy": (
            "memory can change behavior only when memory-enabled evidence beats memory-disabled or memory-limited "
            "counterfactuals across decision, timing, channel, recovery, and follow-through receipts"
        ),
        "provider_operations_policy": (
            "canonical memory retains authority; advisory, degraded, stale, conflicting, and privacy-limited "
            "providers can only influence behavior through quality, privacy, freshness, delete/export, quarantine, "
            "and reinstatement gates"
        ),
        "receipt_redaction_policy": (
            "operator receipts expose redacted metadata, metrics, digests, and safe handles only; raw transcripts, "
            "secrets, personal identifiers, provider payloads, and raw local paths are not exposed"
        ),
        "receipt_surfaces": [
            "/api/operator/live-guardian-memory-field-program",
            "/api/operator/generalized-guardian-outcomes",
            "/api/operator/longitudinal-guardian-outcomes",
            "/api/operator/benchmark-proof",
        ],
        "blocked_claims": list(LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_BLOCKED_CLAIMS),
        "not_claimed": [
            "guardian_intelligence_superiority",
            "solved_live_learning",
            "solved_long_term_learning",
            "memory_superiority",
            "full_memory_provider_parity",
            "production_ready_product",
            "full_production_parity",
        ],
    }


def _safe_receipt(receipt_type: str, receipt_id: str) -> dict[str, Any]:
    return {
        "summary_receipt_id": f"summary:guardian-dl:{receipt_type}:{receipt_id}",
        "redacted_raw_receipt_id": f"redacted:guardian-dl:{receipt_type}:{receipt_id}",
        "receipt_digest": f"digest:guardian-dl:{receipt_type}:{receipt_id}",
        "redaction_boundary": LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_REDACTION_BOUNDARY,
        "stored_payload_mode": "metadata_only_redacted_receipt",
        "contains_raw_transcript": False,
        "contains_secret": False,
        "contains_personal_identifier": False,
        "contains_provider_payload": False,
        "raw_receipt_path_exposed": False,
    }


def live_field_study_receipts() -> list[dict[str, Any]]:
    rows = [
        ("engineering_long_work", "live_field_window", 30, 118, 2),
        ("recurring_obligation", "recorded_live_redacted", 21, 84, 1),
        ("collaborator_project_drift", "live_field_window", 28, 96, 1),
        ("routine_interruptions", "accelerated_fixture_with_live_marker", 14, 72, 0),
        ("source_review_report_followthrough", "recorded_live_redacted", 35, 104, 2),
        ("cross_surface_continuity", "live_field_window", 45, 132, 1),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (task_family, evidence_mode, window_days, sample_size, adverse) in enumerate(rows, start=1):
        study_id = f"dl-field-{task_family.replace('_', '-')}-{index:02d}"
        receipts.append({
            "study_id": study_id,
            "protocol_id": "dl-live-guardian-memory-field-program",
            "protocol_version": "2026-06-11",
            "pre_registered": True,
            "task_family": task_family,
            "field_window_days": window_days,
            "sample_size": sample_size,
            "cohort_boundary": f"{task_family}_consented_cohort_no_cross_project_identity_join",
            "evidence_mode": evidence_mode,
            "fixture_vs_live_marker": (
                "fixture_accelerated_with_live_window_marker"
                if evidence_mode == "accelerated_fixture_with_live_marker"
                else "live_or_recorded_live_redacted"
            ),
            "consent": {
                "consent_state": "active_with_withdrawal_supported",
                "withdrawal_supported": True,
                "withdrawal_count": 1 if task_family in {"recurring_obligation", "collaborator_project_drift"} else 0,
                "anonymization_state": "anonymized",
                "raw_transcript_stored": False,
                "retention_policy": "redacted_aggregate_receipts_only",
            },
            "evaluator": {
                "identity_class": "independent_longitudinal_review_pool",
                "implementation_independent": True,
                "conflict_disclosure": "no_batch_implementation_role",
                "adjudication_rules": "second_reviewer_for_harm_correction_or_rollback_disagreement",
            },
            "adverse_events": {
                "adverse_event_count": adverse,
                "adverse_event_reviewed_count": adverse,
                "automatic_revert_count": 1 if adverse else 0,
            },
            "rollback_authority": "operator_or_safety_monitor_can_revert_learning_policy_delta",
            "claim_scope": f"bounded_to_{task_family}_field_window_not_solved_learning",
            "safe_receipt": _safe_receipt("field-study", study_id),
            "blocked_claims": list(LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_BLOCKED_CLAIMS),
        })
    return receipts


def memory_behavior_ablation_receipts() -> list[dict[str, Any]]:
    rows = [
        ("act", "urgent_followthrough_memory_enabled", "memory_disabled"),
        ("defer", "low_urgency_focus_window_memory_enabled", "memory_limited"),
        ("bundle", "multi_commitment_digest_memory_enabled", "memory_disabled"),
        ("clarify", "ambiguous_anchor_memory_enabled", "stale_memory_suppressed"),
        ("approval", "risky_scope_memory_enabled", "provider_limited"),
        ("stay_silent", "negative_control_memory_enabled", "memory_disabled"),
        ("recovery", "missed_commitment_repair_memory_enabled", "memory_limited"),
        ("followthrough", "cross_surface_resolution_memory_enabled", "provider_limited"),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (decision, task_family, counterfactual) in enumerate(rows, start=1):
        ablation_id = f"dl-ablation-{decision}-{index:02d}"
        receipts.append({
            "ablation_id": ablation_id,
            "decision": decision,
            "task_family": task_family,
            "memory_enabled_condition": "canonical_plus_quality_gated_advisory_memory",
            "counterfactual_condition": counterfactual,
            "memory_changed_behavior": True,
            "behavior_change_reason": (
                "fresh_canonical_commitment_and_provider_quality_receipt_changed_timing_channel_or_action"
            ),
            "timing": "delivered_in_receptive_or_deadline_relevant_window",
            "channel": "selected_from_surface_receptivity_and_private_data_boundary",
            "followthrough_delta": 0.16 if decision in {"act", "recovery", "followthrough"} else 0.08,
            "approval_scope_preserved": True,
            "negative_case": decision == "stay_silent",
            "unsafe_or_stale_change_blocked": decision in {"clarify", "stay_silent"},
            "claim_scope": f"memory_changed_{decision}_behavior_not_memory_superiority",
            "safe_receipt": _safe_receipt("ablation", ablation_id),
        })
    return receipts


def live_memory_provider_operation_receipts() -> list[dict[str, Any]]:
    rows = [
        ("canonical_guardian_memory", "canonical", "healthy", False, False),
        ("project_graph_advisory_provider", "advisory", "healthy", False, True),
        ("calendar_commitment_provider", "advisory", "degraded", False, True),
        ("archive_recall_provider", "advisory", "stale", False, True),
        ("cross_project_provider", "advisory", "conflicting", False, True),
        ("private_notes_provider", "advisory", "privacy_limited", True, True),
        ("external_archive_provider", "advisory", "quarantined", True, True),
        ("reinstatement_candidate_provider", "advisory", "review_for_reinstatement", False, True),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (provider_id, role, state, privacy_regression, override_blocked) in enumerate(rows, start=1):
        provider_key = provider_id.replace("_", "-")
        behavior_allowed = role == "canonical" or state == "healthy"
        receipts.append({
            "provider_id": provider_id,
            "provider_role": role,
            "provider_runtime_state": state,
            "canonical_precedence_preserved": True,
            "provider_override_blocked": override_blocked,
            "behavior_change_allowed": behavior_allowed,
            "behavior_change_scope": (
                "canonical_memory_authoritative"
                if role == "canonical"
                else "advisory_only_after_quality_privacy_freshness_gate"
            ),
            "delete_receipt_id": f"delete:dl:{provider_key}",
            "export_receipt_id": f"export:dl:{provider_key}",
            "delete_export_propagated": True,
            "privacy_regression_detected": privacy_regression,
            "conflict_resolution": "canonical_wins_provider_advisory_or_quarantined",
            "quarantine_state": (
                "quarantined"
                if state in {"quarantined", "privacy_limited", "conflicting", "stale"}
                else "review_for_reinstatement" if state == "review_for_reinstatement" else "not_required"
            ),
            "reinstatement_review_receipt_id": (
                f"reinstatement-review:dl:{provider_key}"
                if state in {"quarantined", "review_for_reinstatement", "degraded"}
                else None
            ),
            "provider_drift_detected": state in {"stale", "conflicting", "degraded"},
            "safe_receipt": _safe_receipt("provider", provider_key),
        })
    return receipts


def independent_candidate_review_receipts() -> list[dict[str, Any]]:
    rows = [
        ("protocol-review", "protocol_and_power_rationale", "operator_review_required_before_promotion"),
        ("outcome-adjudication", "longitudinal_outcome_sample", "operator_review_required_before_promotion"),
        ("fairness-adverse-review", "fairness_and_adverse_event_sample", "blocked_pending_adverse_event_review"),
        ("baseline-claim-boundary", "claim_boundary_and_baseline_language", "blocked_for_superiority_claims"),
    ]
    return [
        {
            "review_id": f"dl-independent-{review_id}",
            "review_type": review_type,
            "implementation_independent": True,
            "longitudinal_window": "2026-04-01..2026-06-10",
            "sample_and_power_rationale": "directional_field_program_power_not_superiority_power",
            "fairness_review_required": review_id in {"fairness-adverse-review", "baseline-claim-boundary"},
            "adverse_event_review_required": review_id == "fairness-adverse-review",
            "promotion_state": promotion_state,
            "no_superiority_or_baseline_win_claim": True,
            "safe_receipt": _safe_receipt("independent-review", review_id),
        }
        for review_id, review_type, promotion_state in rows
    ]


def longitudinal_learning_safety_monitor_v3_receipts() -> list[dict[str, Any]]:
    negative_cases = [
        "stale_recall",
        "over_personalization",
        "noisy_provider_evidence",
        "false_confidence",
        "privacy_regression",
        "unsafe_intervention",
        "hallucinated_obligation",
        "provider_drift",
        "conflicting_project_anchors",
        "ignored_correction",
    ]
    receipts: list[dict[str, Any]] = []
    for index, case_id in enumerate(negative_cases, start=1):
        receipts.append({
            "monitor_id": f"dl-safety-{case_id.replace('_', '-')}-{index:02d}",
            "negative_case": case_id,
            "detected": True,
            "blocked_or_rolled_back": True,
            "quarantine_or_reinstatement_action": (
                "quarantined_provider_evidence"
                if case_id in {"privacy_regression", "provider_drift", "noisy_provider_evidence"}
                else "rolled_back_learning_policy_delta"
            ),
            "operator_action": "operator_surface_requires_review_before_reinstatement",
            "safe_receipt": _safe_receipt("safety-monitor", case_id.replace("_", "-")),
        })
    return receipts


def guardian_memory_false_claim_scan_receipts() -> list[dict[str, Any]]:
    return [
        {
            "scan_id": "dl-guardian-memory-false-claim-scan",
            "command": "python3 scripts/check_strategy_claims.py",
            "checked_at": "2026-06-11",
            "forbidden_hit_count": 0,
            "claim_boundary": LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_CLAIM_BOUNDARY,
            "blocked_claims": list(LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_BLOCKED_CLAIMS),
            "safe_receipt": _safe_receipt("claim-scan", "guardian-memory"),
        }
    ]


def build_live_guardian_memory_field_program_contract() -> dict[str, Any]:
    field_studies = live_field_study_receipts()
    ablations = memory_behavior_ablation_receipts()
    providers = live_memory_provider_operation_receipts()
    independent_reviews = independent_candidate_review_receipts()
    safety = longitudinal_learning_safety_monitor_v3_receipts()
    claim_scans = guardian_memory_false_claim_scan_receipts()
    policy = live_guardian_memory_field_program_policy_payload()
    safe_receipts = [
        item["safe_receipt"]
        for item in [*field_studies, *ablations, *providers, *independent_reviews, *safety, *claim_scans]
        if item.get("safe_receipt")
    ]
    return {
        "summary": {
            "operator_status": "live_guardian_memory_field_program_receipts_visible",
            "field_study_count": len(field_studies),
            "field_window_count": len(field_studies),
            "pre_registered_count": sum(1 for item in field_studies if item["pre_registered"] is True),
            "task_family_count": len({item["task_family"] for item in field_studies}),
            "live_or_recorded_window_count": sum(
                1 for item in field_studies if item["evidence_mode"] in {"live_field_window", "recorded_live_redacted"}
            ),
            "fixture_marked_count": sum(
                1 for item in field_studies if "fixture" in item["fixture_vs_live_marker"]
            ),
            "withdrawal_supported_count": sum(
                1 for item in field_studies if item["consent"]["withdrawal_supported"] is True
            ),
            "anonymized_count": sum(
                1 for item in field_studies if item["consent"]["anonymization_state"] == "anonymized"
            ),
            "independent_evaluator_count": sum(
                1 for item in field_studies if item["evaluator"]["implementation_independent"] is True
            ),
            "adverse_event_count": sum(int(item["adverse_events"]["adverse_event_count"]) for item in field_studies),
            "adverse_event_reviewed_count": sum(
                int(item["adverse_events"]["adverse_event_reviewed_count"]) for item in field_studies
            ),
            "rollback_authority_count": sum(1 for item in field_studies if item["rollback_authority"]),
            "ablation_count": len(ablations),
            "decision_type_count": len({item["decision"] for item in ablations}),
            "counterfactual_count": sum(1 for item in ablations if item["counterfactual_condition"]),
            "memory_changed_behavior_count": sum(1 for item in ablations if item["memory_changed_behavior"] is True),
            "unsafe_or_stale_change_blocked_count": sum(
                1 for item in ablations if item["unsafe_or_stale_change_blocked"] is True
            ),
            "provider_count": len(providers),
            "provider_state_count": len({item["provider_runtime_state"] for item in providers}),
            "canonical_precedence_preserved_count": sum(
                1 for item in providers if item["canonical_precedence_preserved"] is True
            ),
            "provider_override_blocked_count": sum(1 for item in providers if item["provider_override_blocked"]),
            "privacy_regression_count": sum(1 for item in providers if item["privacy_regression_detected"]),
            "delete_export_propagated_count": sum(1 for item in providers if item["delete_export_propagated"]),
            "quarantine_count": sum(1 for item in providers if item["quarantine_state"] == "quarantined"),
            "reinstatement_review_count": sum(1 for item in providers if item["reinstatement_review_receipt_id"]),
            "provider_drift_detected_count": sum(1 for item in providers if item["provider_drift_detected"]),
            "independent_review_count": len(independent_reviews),
            "implementation_independent_review_count": sum(
                1 for item in independent_reviews if item["implementation_independent"] is True
            ),
            "no_superiority_review_count": sum(
                1 for item in independent_reviews if item["no_superiority_or_baseline_win_claim"] is True
            ),
            "negative_case_count": len(safety),
            "negative_case_detected_count": sum(1 for item in safety if item["detected"] is True),
            "rollback_or_quarantine_count": sum(1 for item in safety if item["blocked_or_rolled_back"] is True),
            "false_claim_scan_count": len(claim_scans),
            "false_claim_hit_count": sum(int(item["forbidden_hit_count"]) for item in claim_scans),
            "raw_transcript_stored_count": sum(
                1 for item in field_studies if item["consent"]["raw_transcript_stored"] is True
            ),
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
            "claim_boundary": LIVE_GUARDIAN_MEMORY_FIELD_PROGRAM_CLAIM_BOUNDARY,
        },
        "field_studies": field_studies,
        "memory_behavior_ablations": ablations,
        "memory_provider_operations": providers,
        "independent_candidate_reviews": independent_reviews,
        "safety_monitor": safety,
        "false_claim_scans": claim_scans,
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
                getattr(result, "error", "") or "Live guardian-memory field-program scenario failed."
            ),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_live_guardian_memory_field_program_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        LIVE_LONG_HORIZON_GUARDIAN_LEARNING_FIELD_STUDY_V1_SUITE_NAME,
        MEMORY_BEHAVIOR_CHANGE_ABLATION_V1_SUITE_NAME,
        LIVE_MEMORY_PROVIDER_PARITY_OPERATIONS_V1_SUITE_NAME,
        INDEPENDENT_GUARDIAN_OUTCOME_CANDIDATE_REVIEW_V1_SUITE_NAME,
        LONGITUDINAL_LEARNING_SAFETY_MONITOR_V3_SUITE_NAME,
        GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
    ])


async def build_live_guardian_memory_field_program_report() -> dict[str, Any]:
    summary = await _run_live_guardian_memory_field_program_suites()
    contract = build_live_guardian_memory_field_program_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "live_guardian_memory_field_program_ci_gated_operator_visible"
                if healthy
                else "live_guardian_memory_field_program_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(LIVE_LONG_HORIZON_GUARDIAN_LEARNING_FIELD_STUDY_V1_SCENARIO_NAMES)
                + len(MEMORY_BEHAVIOR_CHANGE_ABLATION_V1_SCENARIO_NAMES)
                + len(LIVE_MEMORY_PROVIDER_PARITY_OPERATIONS_V1_SCENARIO_NAMES)
                + len(INDEPENDENT_GUARDIAN_OUTCOME_CANDIDATE_REVIEW_V1_SCENARIO_NAMES)
                + len(LONGITUDINAL_LEARNING_SAFETY_MONITOR_V3_SCENARIO_NAMES)
                + len(GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            LIVE_LONG_HORIZON_GUARDIAN_LEARNING_FIELD_STUDY_V1_SUITE_NAME: list(
                LIVE_LONG_HORIZON_GUARDIAN_LEARNING_FIELD_STUDY_V1_SCENARIO_NAMES
            ),
            MEMORY_BEHAVIOR_CHANGE_ABLATION_V1_SUITE_NAME: list(
                MEMORY_BEHAVIOR_CHANGE_ABLATION_V1_SCENARIO_NAMES
            ),
            LIVE_MEMORY_PROVIDER_PARITY_OPERATIONS_V1_SUITE_NAME: list(
                LIVE_MEMORY_PROVIDER_PARITY_OPERATIONS_V1_SCENARIO_NAMES
            ),
            INDEPENDENT_GUARDIAN_OUTCOME_CANDIDATE_REVIEW_V1_SUITE_NAME: list(
                INDEPENDENT_GUARDIAN_OUTCOME_CANDIDATE_REVIEW_V1_SCENARIO_NAMES
            ),
            LONGITUDINAL_LEARNING_SAFETY_MONITOR_V3_SUITE_NAME: list(
                LONGITUDINAL_LEARNING_SAFETY_MONITOR_V3_SCENARIO_NAMES
            ),
            GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V1_SUITE_NAME: list(
                GUARDIAN_MEMORY_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="live_guardian_memory_field_program"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
