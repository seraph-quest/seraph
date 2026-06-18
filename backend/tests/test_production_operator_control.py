import asyncio

from src.cockpit.production_operator_control import (
    PRODUCTION_OPERATOR_CONTROL_BLOCKED_CLAIMS,
    PRODUCTION_OPERATOR_CONTROL_CLAIM_BOUNDARY,
    PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES,
    PRODUCTION_OPERATOR_CONTROL_PARITY_SUITE_NAME,
    PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES,
    PRODUCTION_PARITY_TRAIN_SUITE_NAME,
    build_production_operator_control_contract,
    build_production_operator_control_report,
)


def test_production_operator_control_contract_exposes_control_and_train_receipts():
    contract = build_production_operator_control_contract()
    summary = contract["summary"]

    assert summary["operator_status"] == "production_operator_control_parity_receipts_visible"
    assert summary["control_surface_count"] == 6
    assert summary["train_batch_count"] == 7
    assert summary["merged_prior_batch_count"] == 6
    assert summary["required_actions_visible"] is True
    assert summary["claim_boundary"] == PRODUCTION_OPERATOR_CONTROL_CLAIM_BOUNDARY
    assert set(PRODUCTION_OPERATOR_CONTROL_BLOCKED_CLAIMS) <= set(contract["policy"]["blocked_claims"])


def test_operator_control_receipts_cover_authority_recovery_and_residual_risks():
    contract = build_production_operator_control_contract()

    for receipt in contract["control_receipts"]:
        assert receipt["authority_source"].startswith("/api/operator/")
        assert receipt["state_visible"]
        assert receipt["risk_visible"]
        assert receipt["residual_risk"]
        assert all(control["receipt_after_action"] for control in receipt["controls"])

    control_actions = {
        control["action"]
        for receipt in contract["control_receipts"]
        for control in receipt["controls"]
    }
    assert {"pause", "resume", "retry", "repair", "branch", "compare", "revoke", "audit"} <= control_actions


def test_production_parity_train_receipts_keep_cb_truthful_until_merge():
    contract = build_production_operator_control_contract()
    train_by_batch = {item["batch"]: item for item in contract["train_receipts"]}

    assert set(train_by_batch) == {"BV", "BW", "BX", "BY", "BZ", "CA", "CB"}
    assert all(
        item["evidence_state"] == "merged_to_develop"
        for batch, item in train_by_batch.items()
        if batch != "CB"
    )
    assert train_by_batch["CA"]["merged_pr"] == 489
    assert train_by_batch["CB"]["merged_pr"] is None
    assert train_by_batch["CB"]["evidence_state"] == "active_branch_receipts_visible_until_pr_merge"


def test_final_audit_receipts_require_critic_and_claim_ledger():
    contract = build_production_operator_control_contract()
    audits = {item["audit_id"]: item for item in contract["final_audit_receipts"]}

    assert audits["cb-audit-board-state"]["required_before_goal_completion"] is True
    assert audits["cb-audit-claim-ledger"]["evidence"] == "docs/research/19-strategy-claim-ledger.md"
    assert audits["cb-audit-critic"]["required_before_goal_completion"] is True
    assert "full_parity_achieved" in contract["policy"]["not_claimed"]


def test_production_operator_control_report_runs_cb_suites():
    payload = asyncio.run(build_production_operator_control_report())

    assert payload["summary"]["benchmark_posture"] == "production_operator_control_parity_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES)
        + len(PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES)
    )
    assert payload["scenario_names"][PRODUCTION_OPERATOR_CONTROL_PARITY_SUITE_NAME] == list(
        PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES
    )
    assert payload["scenario_names"][PRODUCTION_PARITY_TRAIN_SUITE_NAME] == list(
        PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES
    )
    assert payload["latest_run"]["failed"] == 0
