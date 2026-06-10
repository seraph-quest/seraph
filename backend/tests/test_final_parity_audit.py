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
    assert {"Hermes", "OpenClaw", "IronClaw"} <= {item["system"] for item in sources}
    assert all(item["claim_use"] == "source_backed_pressure_only" for item in sources)


def test_final_parity_audit_contract_reconciles_batches_and_blocks_completion_claims():
    contract = build_final_parity_audit_contract()
    summary = contract["summary"]
    batches = contract["batch_reconciliation_receipts"]

    assert summary["completed_batch_count"] == 16
    assert summary["all_completed_batches_done_merged_passed"] is True
    assert summary["final_batch_status"] == "self_referential_final_audit_batch"
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
    assert cm_batch["status"] == "active_branch_receipts_visible_until_pr_merge"
    assert cm_batch["project_status"] == "owned_by_github_project_until_pr_merge"
    assert cm_batch["project_pr"] == "owned_by_linked_pull_request_until_pr_merge"
    cn_batch = next(item for item in batches if item["batch"] == "CN")
    assert cn_batch["issue"] == 508
    assert cn_batch["primary_suite"] == "long_work_debugging_recovery"
    assert cn_batch["status"] == "active_branch_receipts_visible_until_pr_merge"
    assert cn_batch["project_status"] == "owned_by_github_project_until_pr_merge"
    assert next(item for item in batches if item["batch"] == "CI")["issue"] == 497


def test_final_parity_audit_contract_reconciles_claim_ledger_and_critic():
    contract = build_final_parity_audit_contract()
    policy = contract["policy"]
    claims = contract["claim_ledger_reconciliation"]
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
    operator_gap = next(
        item for item in contract["residual_gap_receipts"] if item["gap_id"] == "ci-gap-dense-operator-control"
    )

    assert policy["claim_boundary"] == FINAL_PARITY_AUDIT_CLAIM_BOUNDARY
    assert set(FINAL_PARITY_AUDIT_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "fully_at_parity" in policy["blocked_claims"]
    assert "/api/operator/production-sla-orchestration" in policy["receipt_surfaces"]
    assert "/api/operator/production-reach-voice-mobile" in policy["receipt_surfaces"]
    assert "/api/operator/dense-operator-recovery-control" in policy["receipt_surfaces"]
    assert {509, 508, 507, 506, 505, 497, 496, 475} <= {issue for item in claims for issue in item["issue_links"]}
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
    scl_029 = next(item for item in claims if item["claim_id"] == "SCL-029")
    assert scl_029["status"] == "active_branch_receipts_visible_until_batch_cm_pr_merge"
    assert "full_memory_provider_parity" in scl_029["blocked_claims"]
    assert "long_work_debugging_recovery" in operator_gap["current_batch_evidence"]
    assert "operator_control_density" in operator_gap["current_batch_evidence"]
    assert "independent_operator_usability_accessibility" in operator_gap["current_batch_evidence"]
    assert "/api/operator/dense-operator-recovery-control" in operator_gap["current_batch_evidence"]
    scl_030 = next(item for item in claims if item["claim_id"] == "SCL-030")
    assert scl_030["status"] == "active_branch_receipts_visible_until_batch_cn_pr_merge"
    assert "solved_operator_control" in scl_030["blocked_claims"]
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
