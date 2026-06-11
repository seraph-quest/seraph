import pytest

from src.workflows.production_orchestration_hard_guarantees import (
    DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SCENARIO_NAMES,
    DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SUITE_NAME,
    EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SCENARIO_NAMES,
    EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SUITE_NAME,
    ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES,
    ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
    PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_BLOCKED_CLAIMS,
    PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_CLAIM_BOUNDARY,
    PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SCENARIO_NAMES,
    PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SUITE_NAME,
    SCHEDULER_FAILOVER_SOAK_V1_SCENARIO_NAMES,
    SCHEDULER_FAILOVER_SOAK_V1_SUITE_NAME,
    build_production_orchestration_hard_guarantees_contract,
    build_production_orchestration_hard_guarantees_report,
)


def test_production_orchestration_hard_guarantees_contract_exposes_di_receipts():
    contract = build_production_orchestration_hard_guarantees_contract()
    summary = contract["summary"]

    assert summary["operator_status"] == "production_orchestration_hard_guarantees_visible"
    assert summary["hard_guarantee_receipt_count"] == 3
    assert summary["distributed_recovery_receipt_count"] == 3
    assert summary["external_side_effect_correctness_v4_count"] == 3
    assert summary["scheduler_failover_soak_count"] == 3
    assert summary["false_claim_scan_count"] == 3
    assert summary["all_failovers_within_budget"] is True
    assert summary["all_side_effects_have_idempotency_keys"] is True
    assert summary["all_side_effects_have_redacted_receipts"] is True
    assert summary["unsafe_replay_blocks_visible"] is True
    assert summary["continuous_live_soak_not_claimed"] is True
    assert summary["claim_boundary"] == PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_CLAIM_BOUNDARY
    assert set(PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_BLOCKED_CLAIMS) <= set(
        contract["policy"]["blocked_claims"]
    )
    assert "/api/operator/production-orchestration-hard-guarantees" in contract["policy"]["receipt_surfaces"]
    assert contract["receipt_index"]["predecessor_sources"]["batch_da"]


def test_production_orchestration_hard_guarantees_receipts_cover_side_effects_and_soak():
    contract = build_production_orchestration_hard_guarantees_contract()

    assert {"confirmed", "compensated", "quarantined"} <= {
        item["external_confirmation_state"]
        for item in contract["external_side_effect_correctness_v4_receipts"]
    }
    assert all(
        item["idempotency_key"] and item["redacted_receipt_handle"] and item["provider_receipt_digest"]
        for item in contract["external_side_effect_correctness_v4_receipts"]
    )
    assert all(
        int(item["observed_failover_ms"]) <= int(item["failover_budget_ms"])
        for item in contract["scheduler_failover_soak_receipts"]
    )
    assert all(
        item["live_window_marker"] == "missing_independent_continuous_live_window"
        and item["evidence_mode"].endswith("not_continuous_live_soak")
        for item in contract["scheduler_failover_soak_receipts"]
    )
    assert "continuous_live_soak_completed" in contract["policy"]["not_claimed"]
    assert any(
        item["result"] == "blocked_claims_remain_blocked"
        for item in contract["false_claim_scan_receipts"]
    )


@pytest.mark.asyncio
async def test_production_orchestration_hard_guarantees_report_runs_all_di_suites():
    report = await build_production_orchestration_hard_guarantees_report()
    expected_count = (
        len(PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SCENARIO_NAMES)
        + len(DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SCENARIO_NAMES)
        + len(EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SCENARIO_NAMES)
        + len(SCHEDULER_FAILOVER_SOAK_V1_SCENARIO_NAMES)
        + len(ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES)
    )

    assert report["summary"]["suite_count"] == 5
    assert report["summary"]["scenario_count"] == expected_count
    assert report["summary"]["failed"] == 0
    assert report["scenario_names"][PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SUITE_NAME] == list(
        PRODUCTION_ORCHESTRATION_HARD_GUARANTEES_SCENARIO_NAMES
    )
    assert report["scenario_names"][DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SUITE_NAME] == list(
        DISTRIBUTED_WORKFLOW_RECOVERY_OPERATIONS_SCENARIO_NAMES
    )
    assert report["scenario_names"][EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SUITE_NAME] == list(
        EXTERNAL_SIDE_EFFECT_CORRECTNESS_V4_SCENARIO_NAMES
    )
    assert report["scenario_names"][SCHEDULER_FAILOVER_SOAK_V1_SUITE_NAME] == list(
        SCHEDULER_FAILOVER_SOAK_V1_SCENARIO_NAMES
    )
    assert report["scenario_names"][ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SUITE_NAME] == list(
        ORCHESTRATION_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES
    )
