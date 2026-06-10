import asyncio

from src.evals.final_parity_audit import (
    FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES,
    FINAL_PARITY_AUDIT_BLOCKED_CLAIMS,
    FINAL_PARITY_AUDIT_CLAIM_BOUNDARY,
    FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES,
    OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES,
    build_final_parity_audit_contract,
    build_final_parity_readiness_report,
)


def test_final_parity_audit_contract_exposes_current_source_receipts():
    contract = build_final_parity_audit_contract()
    summary = contract["summary"]
    sources = contract["current_source_receipts"]

    assert summary["operator_status"] == "final_parity_readiness_report_visible"
    assert summary["source_receipt_count"] == 7
    assert summary["competitor_count"] == 3
    assert summary["current_source_date"] == "2026-06-10"
    assert summary["all_sources_have_urls_and_dates"] is True
    assert summary["all_sources_reachable_with_caveats"] is True
    assert {"Hermes", "OpenClaw", "IronClaw"} <= {item["system"] for item in sources}
    assert all(item["claim_use"] == "source_backed_pressure_only" for item in sources)
    assert all(item["access_status"] == "reachable" for item in sources)
    assert all(item["access_caveat"] for item in sources)
    assert all(item["competitor_claim_uncertainty"] for item in sources)


def test_final_parity_audit_contract_reconciles_batches_and_blocks_completion_claims():
    contract = build_final_parity_audit_contract()
    summary = contract["summary"]
    batches = contract["batch_reconciliation_receipts"]

    assert summary["completed_batch_count"] == 22
    assert summary["all_completed_batches_done_merged_passed"] is True
    assert summary["final_batch_status"] == "done"
    assert summary["full_parity_claim_allowed"] is False
    assert summary["reference_systems_exceeded_claim_allowed"] is False
    assert next(item for item in batches if item["batch"] == "CH")["merged_pr"] == 503
    cl_batch = next(item for item in batches if item["batch"] == "CL")
    assert cl_batch["issue"] == 509
    assert cl_batch["status"] == "done"
    assert cl_batch["merged_pr"] == 516
    assert cl_batch["project_status"] == "Done"
    assert cl_batch["project_pr"] == "Merged"
    cm_batch = next(item for item in batches if item["batch"] == "CM")
    assert cm_batch["issue"] == 507
    assert cm_batch["status"] == "done"
    assert cm_batch["merged_pr"] == 517
    assert cm_batch["project_status"] == "Done"
    assert cm_batch["project_pr"] == "Merged"
    cn_batch = next(item for item in batches if item["batch"] == "CN")
    assert cn_batch["issue"] == 508
    assert cn_batch["primary_suite"] == "long_work_debugging_recovery"
    assert cn_batch["status"] == "done"
    assert cn_batch["merged_pr"] == 518
    assert cn_batch["project_status"] == "Done"
    assert cn_batch["project_pr"] == "Merged"
    co_batch = next(item for item in batches if item["batch"] == "CO")
    assert co_batch["issue"] == 510
    assert co_batch["primary_suite"] == "independent_package_security_review"
    assert co_batch["status"] == "done"
    assert co_batch["merged_pr"] == 519
    assert co_batch["project_status"] == "Done"
    assert co_batch["project_pr"] == "Merged"
    cp_batch = next(item for item in batches if item["batch"] == "CP")
    assert cp_batch["issue"] == 511
    assert cp_batch["primary_suite"] == "live_browser_task_depth"
    assert cp_batch["status"] == "done"
    assert cp_batch["merged_pr"] == 520
    assert cp_batch["project_status"] == "Done"
    assert cp_batch["project_pr"] == "Merged"
    assert next(item for item in batches if item["batch"] == "CI")["merged_pr"] == 504
    cq_batch = next(item for item in batches if item["batch"] == "CQ")
    assert cq_batch["issue"] == 512
    assert cq_batch["status"] == "done"
    assert cq_batch["merged_pr"] == 521
    assert cq_batch["project_status"] == "Done"
    assert cq_batch["project_pr"] == "Merged"
    assert cq_batch["code_review"] == "Passed"


def test_final_parity_audit_contract_reconciles_claim_ledger_and_critic():
    contract = build_final_parity_audit_contract()
    policy = contract["policy"]
    claims = contract["claim_ledger_reconciliation"]
    claim_lift = contract["claim_lift_matrix"]
    exact_claims = contract["exact_stronger_claim_outcomes"]
    critic = contract["critic_disposition_receipts"]
    orchestration_gap = next(
        item for item in contract["residual_gap_receipts"] if item["gap_id"] == "ci-gap-orchestration-sla"
    )
    security_gap = next(
        item for item in contract["residual_gap_receipts"] if item["gap_id"] == "ci-gap-security-independent"
    )
    reach_gap = next(
        item for item in contract["residual_gap_receipts"] if item["gap_id"] == "ci-gap-reach-media-production"
    )
    learning_gap = next(
        item for item in contract["residual_gap_receipts"] if item["gap_id"] == "ci-gap-human-outcomes-independent"
    )
    marketplace_gap = next(
        item for item in contract["residual_gap_receipts"] if item["gap_id"] == "ci-gap-marketplace-security"
    )
    operator_gap = next(
        item for item in contract["residual_gap_receipts"] if item["gap_id"] == "ci-gap-dense-operator-control"
    )
    browser_gap = next(
        item for item in contract["residual_gap_receipts"] if item["gap_id"] == "ci-gap-browser-autonomy"
    )

    assert policy["claim_boundary"] == FINAL_PARITY_AUDIT_CLAIM_BOUNDARY
    assert set(FINAL_PARITY_AUDIT_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "fully_at_parity" in policy["blocked_claims"]
    assert "/api/operator/production-sla-orchestration" in policy["receipt_surfaces"]
    assert "/api/operator/production-reach-voice-mobile" in policy["receipt_surfaces"]
    assert "/api/operator/dense-operator-recovery-control" in policy["receipt_surfaces"]
    assert "/api/operator/production-marketplace-security" in policy["receipt_surfaces"]
    assert "/api/operator/safe-autonomous-browser-computer-use" in policy["receipt_surfaces"]
    assert {511, 510, 509, 508, 507, 506, 505, 497, 496, 475} <= {
        issue for item in claims for issue in item["issue_links"]
    }
    assert {475, 512} <= {issue for item in claims for issue in item["issue_links"]}
    assert {item["claim_id"] for item in claim_lift} >= {
        "SCL-028",
        "SCL-029",
        "SCL-030",
        "SCL-031",
        "SCL-032",
        "SCL-033",
    }
    assert contract["summary"]["all_claim_lift_rows_have_project_and_pr_evidence"] is True
    assert contract["summary"]["bounded_parity_proof_train_completion_wording_allowed"] is True
    assert contract["summary"]["bounded_parity_proof_train_completion_wording_allowed_after_cq_merge"] is True
    assert contract["summary"]["continued_blocked_stronger_claim_count"] == len(exact_claims)
    assert all(item["outcome"] == "continued_blocked" for item in exact_claims)
    assert "production_sla_orchestration" in orchestration_gap["current_batch_evidence"]
    assert "exactly_once_or_crash_proof_orchestration" in orchestration_gap["blocking_claims"]
    assert "independent_secure_host_review" in security_gap["current_batch_evidence"]
    assert "/api/operator/independent-secure-host-review" in security_gap["current_batch_evidence"]
    assert "ironclaw_class_secure_execution" in security_gap["blocking_claims"]
    assert "broad_channel_sla_operations" in reach_gap["current_batch_evidence"]
    assert "mobile_execution_continuity" in reach_gap["current_batch_evidence"]
    assert "/api/operator/production-reach-voice-mobile" in reach_gap["current_batch_evidence"]
    assert "independent_outcome_cohort_review" in learning_gap["current_batch_evidence"]
    assert "task_scoped_causal_learning" in learning_gap["current_batch_evidence"]
    assert "memory_provider_parity_matrix" in learning_gap["current_batch_evidence"]
    assert "/api/operator/independent-learning-memory-parity" in learning_gap["current_batch_evidence"]
    assert "independent_package_security_review" in marketplace_gap["current_batch_evidence"]
    assert "package_network_incident_operations" in marketplace_gap["current_batch_evidence"]
    assert "/api/operator/production-marketplace-security" in marketplace_gap["current_batch_evidence"]
    assert "production_secure_marketplace" in marketplace_gap["blocking_claims"]
    scl_029 = next(item for item in claims if item["claim_id"] == "SCL-029")
    assert scl_029["status"] == "backed_for_bounded_receipts_after_batch_cm_pr_merge"
    assert "full_memory_provider_parity" in scl_029["blocked_claims"]
    assert "long_work_debugging_recovery" in operator_gap["current_batch_evidence"]
    assert "operator_control_density" in operator_gap["current_batch_evidence"]
    assert "independent_operator_usability_accessibility" in operator_gap["current_batch_evidence"]
    assert "/api/operator/dense-operator-recovery-control" in operator_gap["current_batch_evidence"]
    scl_030 = next(item for item in claims if item["claim_id"] == "SCL-030")
    assert scl_030["status"] == "backed_for_bounded_receipts_after_batch_cn_pr_merge"
    assert "solved_operator_control" in scl_030["blocked_claims"]
    scl_031 = next(item for item in claims if item["claim_id"] == "SCL-031")
    assert scl_031["status"] == "backed_for_bounded_receipts_after_batch_co_pr_merge"
    assert "/api/operator/production-marketplace-security" == scl_031["operator_surface"]
    assert "third_party_package_security_solved" in scl_031["blocked_claims"]
    assert "live_browser_task_depth" in browser_gap["current_batch_evidence"]
    assert "autonomous_browser_safety_controls" in browser_gap["current_batch_evidence"]
    assert "browser_provider_reliability_matrix" in browser_gap["current_batch_evidence"]
    assert "/api/operator/safe-autonomous-browser-computer-use" in browser_gap["current_batch_evidence"]
    assert "full_browser_parity" in browser_gap["blocking_claims"]
    scl_032 = next(item for item in claims if item["claim_id"] == "SCL-032")
    assert scl_032["status"] == "backed_for_bounded_receipts_after_batch_cp_pr_merge"
    assert scl_032["operator_surface"] == "/api/operator/safe-autonomous-browser-computer-use"
    assert "safe_autonomous_computer_use" in scl_032["blocked_claims"]
    scl_033 = next(item for item in claims if item["claim_id"] == "SCL-033")
    assert scl_033["status"] == "backed_for_bounded_final_claim_lift_receipts_broad_claims_continue_blocked"
    assert "/api/operator/final-parity-readiness-report" == scl_033["operator_surface"]
    assert "fully_at_parity" in scl_033["blocked_claims"]
    scl_032_lift = next(item for item in claim_lift if item["claim_id"] == "SCL-032")
    assert scl_032_lift["merged_pr"] == 520
    assert scl_032_lift["project_status"] == "Done"
    assert "full_browser_parity" in scl_032_lift["continued_blocked_claims"]
    scl_033_lift = next(item for item in claim_lift if item["claim_id"] == "SCL-033")
    assert scl_033_lift["issue"] == 512
    assert scl_033_lift["merged_pr"] == 521
    assert scl_033_lift["project_status"] == "Done"
    assert scl_033_lift["project_pr"] == "Merged"
    assert scl_033_lift["code_review"] == "Passed"
    assert scl_033_lift["disposition"] == "bounded_completion_wording_allowed_broad_claims_continue_blocked"
    assert scl_033_lift["currently_allowed"] is True
    assert "completed a board-backed parity proof train" in scl_033_lift["currently_permitted_exact_wording"]
    assert "completed a board-backed parity proof train" in scl_033_lift["permitted_exact_wording"]
    assert "reference_systems_exceeded" in scl_033_lift["continued_blocked_claims"]
    assert all(item["disposition"] == "accepted" for item in critic)


def test_final_parity_readiness_report_runs_all_batch_ci_suites():
    payload = asyncio.run(build_final_parity_readiness_report())

    assert payload["summary"]["benchmark_posture"] == "final_parity_audit_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES)
        + len(FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES)
        + len(OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == FINAL_PARITY_AUDIT_CLAIM_BOUNDARY
