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

    assert summary["completed_batch_count"] == 13
    assert summary["all_completed_batches_done_merged_passed"] is True
    assert summary["final_batch_status"] == "self_referential_final_audit_batch"
    assert summary["full_parity_claim_allowed"] is False
    assert summary["reference_systems_exceeded_claim_allowed"] is False
    assert next(item for item in batches if item["batch"] == "CH")["merged_pr"] == 503
    assert next(item for item in batches if item["batch"] == "CI")["issue"] == 497


def test_final_parity_audit_contract_reconciles_claim_ledger_and_critic():
    contract = build_final_parity_audit_contract()
    policy = contract["policy"]
    claims = contract["claim_ledger_reconciliation"]
    critic = contract["critic_disposition_receipts"]

    assert policy["claim_boundary"] == FINAL_PARITY_AUDIT_CLAIM_BOUNDARY
    assert set(FINAL_PARITY_AUDIT_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "fully_at_parity" in policy["blocked_claims"]
    assert {497, 496, 475} <= {issue for item in claims for issue in item["issue_links"]}
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
