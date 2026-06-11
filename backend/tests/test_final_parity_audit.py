import asyncio

from src.evals.final_parity_audit import (
    BOARD_PR_ISSUE_RECONCILIATION_V3_SCENARIO_NAMES,
    FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES,
    FINAL_FULL_PARITY_CLAIM_LIFT_V1_SCENARIO_NAMES,
    FINAL_PRODUCTION_PARITY_BLOCKED_CLAIMS,
    FINAL_PRODUCTION_PARITY_CLAIM_BOUNDARY,
    FINAL_PARITY_AUDIT_BLOCKED_CLAIMS,
    FINAL_PARITY_AUDIT_CLAIM_BOUNDARY,
    FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES,
    FALSE_COMPLETION_SCAN_V2_SCENARIO_NAMES,
    FALSE_COMPLETION_SCAN_V3_SCENARIO_NAMES,
    OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES,
    POST_CQ_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES,
    POST_CQ_CLAIM_READINESS_BLOCKED_CLAIMS,
    POST_CQ_CLAIM_READINESS_CLAIM_BOUNDARY,
    PRODUCTION_READINESS_SOAK_V1_SCENARIO_NAMES,
    REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SCENARIO_NAMES,
    REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SCENARIO_NAMES,
    build_final_parity_audit_contract,
    build_final_parity_readiness_report,
    build_final_production_parity_contract,
    build_final_production_parity_report,
    build_post_cq_claim_readiness_contract,
    build_post_cq_claim_readiness_report,
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


def test_post_cq_claim_readiness_contract_reconciles_sources_batches_and_claims():
    contract = build_post_cq_claim_readiness_contract()
    summary = contract["summary"]
    sources = contract["reference_system_source_refresh_v2"]
    batches = contract["post_cq_batch_reconciliation_receipts"]
    claims = contract["post_cq_claim_ledger_reconciliation"]

    assert summary["operator_status"] == "post_cq_claim_readiness_visible"
    assert summary["source_receipt_count"] == 7
    assert summary["competitor_count"] == 3
    assert summary["current_source_date"] == "2026-06-11"
    assert summary["completed_post_cq_batch_count"] == 8
    assert summary["post_cq_batch_count"] == 9
    assert summary["cz_batch_status"] == "cz_gate_receipts_visible"
    assert summary["all_sources_have_urls_and_dates"] is True
    assert summary["all_sources_static_snapshot_no_runtime_fetch"] is True
    assert summary["all_sources_have_external_critic_reachability_receipts"] is True
    assert summary["all_sources_reachable_with_caveats"] is True
    assert all(item["source_refresh_version"] == "v2_post_cq" for item in sources)
    assert all(item["checked_on"] == "2026-06-11" for item in sources)
    assert all(item["runtime_fetch_performed"] is False for item in sources)
    assert all(item["claim_lift_allowed"] is False for item in sources)
    assert {"Hermes", "OpenClaw", "IronClaw"} <= {item["system"] for item in sources}
    assert next(item for item in batches if item["batch"] == "CR")["merged_pr"] == 531
    assert next(item for item in batches if item["batch"] == "CY")["merged_pr"] == 538
    assert next(item for item in batches if item["batch"] == "CZ")["issue"] == 530
    assert summary["all_completed_post_cq_batches_done_merged_passed"] is True
    assert summary["live_project_verification_required"] is True
    assert {item["claim_id"] for item in claims} >= {
        "SCL-034",
        "SCL-035",
        "SCL-036",
        "SCL-037",
        "SCL-038",
        "SCL-039",
        "SCL-040",
        "SCL-041",
    }
    scl_041 = next(item for item in claims if item["claim_id"] == "SCL-041")
    assert scl_041["operator_surface"] == "/api/operator/post-cq-claim-readiness"
    assert "post-CQ production-evidence claim-readiness audit" in scl_041["allowed_wording"]


def test_post_cq_claim_readiness_contract_blocks_false_completion_claims():
    contract = build_post_cq_claim_readiness_contract()
    summary = contract["summary"]
    policy = contract["policy"]
    scans = contract["false_completion_scan_v2"]
    critic = contract["critic_disposition_receipts"]

    assert policy["claim_boundary"] == POST_CQ_CLAIM_READINESS_CLAIM_BOUNDARY
    assert set(POST_CQ_CLAIM_READINESS_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/post-cq-claim-readiness" in policy["receipt_surfaces"]
    assert summary["post_cq_bounded_claim_readiness_wording_allowed"] is True
    assert summary["full_parity_claim_allowed"] is False
    assert summary["reference_systems_exceeded_claim_allowed"] is False
    assert summary["production_ready_claim_allowed"] is False
    assert summary["secure_private_by_default_claim_allowed"] is False
    assert summary["false_completion_scan_count"] == 3
    assert summary["local_false_completion_violation_count"] == 0
    assert summary["all_local_false_completion_scans_clean"] is True
    assert summary["false_completion_public_claim_gate_clear"] is False
    assert all(
        item["violations_found"] == 0
        for item in scans
        if item.get("scan_mode") == "local_repository_file_scan"
    )
    assert any(
        item.get("scan_mode") == "external_github_pr_issue_review_required"
        and item.get("runtime_static_scan") is False
        and item.get("external_scan_status") == "required_before_pr_creation_or_merge"
        for item in scans
    )
    assert all(item["disposition"].startswith("accepted") for item in critic)
    assert {item["reviewer"] for item in critic} == {"Rawls"}


def test_post_cq_claim_readiness_report_runs_all_batch_cz_suites():
    payload = asyncio.run(build_post_cq_claim_readiness_report())

    assert payload["summary"]["benchmark_posture"] == "post_cq_claim_readiness_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(POST_CQ_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES)
        + len(REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SCENARIO_NAMES)
        + len(FALSE_COMPLETION_SCAN_V2_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == POST_CQ_CLAIM_READINESS_CLAIM_BOUNDARY


def test_final_production_parity_contract_reconciles_da_dg_and_keeps_claims_blocked():
    contract = build_final_production_parity_contract()
    summary = contract["summary"]
    batches = contract["da_dg_batch_reconciliation_receipts"]
    claim_lift = contract["final_full_parity_claim_lift_v1"]

    assert summary["operator_status"] == "final_production_parity_gate_visible"
    assert summary["completed_da_dg_batch_count"] == 7
    assert summary["all_completed_da_dg_batches_done_merged_passed"] is True
    assert summary["dg_merged_pr"] == 555
    assert summary["dh_batch_status"] == "in_progress_on_feature_branch"
    assert summary["bounded_final_production_parity_wording_allowed"] is True
    assert summary["full_parity_claim_allowed"] is False
    assert summary["production_ready_claim_allowed"] is False
    assert summary["reference_systems_exceeded_claim_allowed"] is False

    assert {item["batch"] for item in batches if item["status"] == "done"} == {
        "DA",
        "DB",
        "DC",
        "DD",
        "DE",
        "DF",
        "DG",
    }
    assert next(item for item in batches if item["batch"] == "DG")["issue"] == 546
    assert next(item for item in batches if item["batch"] == "DG")["project_pr"] == "Merged"
    assert next(item for item in batches if item["batch"] == "DH")["active_branch"] == (
        "feat/batch-dh-final-production-parity"
    )
    assert {item["claim_id"] for item in claim_lift} >= {
        "SCL-043",
        "SCL-044",
        "SCL-045",
        "SCL-046",
        "SCL-047",
        "SCL-048",
        "SCL-049",
        "SCL-050",
    }
    assert all(item["broad_claim_lift_allowed"] is False for item in claim_lift)
    assert set(FINAL_PRODUCTION_PARITY_BLOCKED_CLAIMS) <= set(contract["policy"]["blocked_claims"])
    assert contract["policy"]["claim_boundary"] == FINAL_PRODUCTION_PARITY_CLAIM_BOUNDARY


def test_final_production_parity_contract_records_sources_soak_scans_and_critic():
    contract = build_final_production_parity_contract()
    sources = contract["reference_system_source_refresh_v3"]
    soak = contract["production_readiness_soak_v1"]
    scans = contract["false_completion_scan_v3"]
    critic = contract["critic_disposition_receipts"]

    assert {item["system"] for item in sources} >= {"Hermes", "OpenClaw", "IronClaw"}
    assert all(item["checked_on"] == "2026-06-11" for item in sources)
    assert all(item["source_refresh_version"] == "v3_final_production_parity_gate" for item in sources)
    assert all(item["runtime_fetch_performed"] is False for item in sources)
    assert all(item["source_refresh_kind"] == "manual_current_source_review_receipt" for item in sources)
    assert all(item["claim_lift_allowed"] is False for item in sources)
    assert {item["area"] for item in soak} == {
        "runtime_reliability",
        "trust_boundaries",
        "presence_and_reach",
        "guardian_intelligence",
        "operator_control",
        "ecosystem_and_marketplace",
        "browser_computer_use",
    }
    assert all(item["raw_receipt_digest"].startswith("sha256:") for item in soak)
    assert all(item["evidence_mode"] == "representative_cross_surface_reconciliation" for item in soak)
    assert all(item["actual_runtime_soak_performed"] is False for item in soak)
    assert all(item["operational_window"] == "not_a_live_soak_window" for item in soak)
    assert all(item["claim_lift_allowed"] is False for item in soak)
    assert any(item.get("stale_pr_number") == 548 and item["stale_pr_state"] == "CLOSED" for item in scans)
    assert any(item.get("issue_475_body_refreshed") is True for item in scans)
    assert sum(int(item.get("violations_found") or 0) for item in scans) == 0
    assert all(item["disposition"].startswith("accepted") for item in critic)


def test_final_production_parity_report_runs_all_batch_dh_suites():
    payload = asyncio.run(build_final_production_parity_report())

    assert payload["summary"]["benchmark_posture"] == "final_production_parity_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(PRODUCTION_READINESS_SOAK_V1_SCENARIO_NAMES)
        + len(FINAL_FULL_PARITY_CLAIM_LIFT_V1_SCENARIO_NAMES)
        + len(REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SCENARIO_NAMES)
        + len(FALSE_COMPLETION_SCAN_V3_SCENARIO_NAMES)
        + len(BOARD_PR_ISSUE_RECONCILIATION_V3_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == FINAL_PRODUCTION_PARITY_CLAIM_BOUNDARY
