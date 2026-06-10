import asyncio

from src.workflows.continuous_orchestration_slo import (
    CONTINUOUS_ORCHESTRATION_SLO_BLOCKED_CLAIMS,
    CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY,
    CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES,
    CONTINUOUS_ORCHESTRATION_SLO_SUITE_NAME,
    CRASH_FAILOVER_SOAK_SCENARIO_NAMES,
    CRASH_FAILOVER_SOAK_SUITE_NAME,
    SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES,
    SIDE_EFFECT_RECONCILIATION_V2_SUITE_NAME,
    build_continuous_orchestration_slo_contract,
    build_continuous_orchestration_slo_report,
)


def test_continuous_orchestration_slo_contract_exposes_monitor_and_recovery_receipts():
    contract = build_continuous_orchestration_slo_contract()
    summary = contract["summary"]

    assert summary["operator_status"] == "continuous_orchestration_slo_visible"
    assert summary["monitor_sample_count"] == 3
    assert summary["crash_failover_soak_count"] == 3
    assert summary["side_effect_reconciliation_count"] == 3
    assert summary["recorded_live_receipt_count"] >= 4
    assert summary["deterministic_soak_count"] >= 2
    assert summary["all_monitors_within_budget"] is True
    assert summary["all_failovers_within_budget"] is True
    assert summary["reconciliation_complete"] is True
    assert summary["required_controls_visible"] is True
    assert summary["runtime_status"] == "continuous_orchestration_runtime_ledger_visible"
    assert summary["runtime_observation_count"] == 9
    assert summary["active_budget_breach_count"] == 0
    assert summary["active_duplicate_risk_count"] == 0
    assert summary["active_recovery_queue_count"] >= 2
    assert summary["runtime_receipt_digest"]
    assert summary["claim_boundary"] == CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY
    assert set(CONTINUOUS_ORCHESTRATION_SLO_BLOCKED_CLAIMS) <= set(contract["policy"]["blocked_claims"])


def test_continuous_orchestration_slo_receipts_name_boundaries_and_uncertainty():
    contract = build_continuous_orchestration_slo_contract()

    for receipt in contract["monitor_samples"]:
        assert receipt["provider"]
        assert receipt["monitor_window"]
        assert receipt["scheduler_health"]
        assert int(receipt["observed_fire_count"]) >= int(receipt["expected_fire_count"])
        assert int(receipt["max_jitter_ms"]) <= int(receipt["jitter_budget_ms"])
        assert receipt["replay_window"]
        assert receipt["operator_recovery_state"]
        assert receipt["residual_uncertainty"]

    for receipt in contract["crash_failover_soak_receipts"]:
        assert receipt["failure_event"]
        assert int(receipt["failover_latency_ms"]) <= int(receipt["failover_budget_ms"])
        assert receipt["replay_authority"]
        assert receipt["operator_recovery_action"]
        assert receipt["operator_recovery_state"]
        assert receipt["residual_uncertainty"]

    for receipt in contract["side_effect_reconciliation_receipts"]:
        assert receipt["idempotency_scope"]
        assert receipt["idempotency_key"]
        assert receipt["duplicate_suppression_state"] in {
            "suppressed",
            "suppressed_with_existing_artifact_reuse",
            "blocked",
        }
        assert receipt["irreversible_boundary"]
        assert receipt["manual_recovery_state"]
        assert receipt["operator_visible"] is True


def test_continuous_orchestration_slo_operator_controls_leave_receipts():
    contract = build_continuous_orchestration_slo_contract()
    controls = {item["action"]: item for item in contract["operator_recovery_receipts"]}

    assert {"inspect", "audit", "resume", "repair", "suppress_duplicate", "branch", "cancel"} <= set(controls)
    assert all(item["enabled"] for item in controls.values())
    assert all(item["receipt_after_action"] for item in controls.values())
    assert controls["resume"]["requires_approval_or_review"] is True
    assert controls["suppress_duplicate"]["mode"] == "direct"


def test_continuous_orchestration_slo_runtime_tracks_recovery_and_reconciliation_state():
    contract = build_continuous_orchestration_slo_contract()
    runtime = contract["runtime_operations"]

    assert runtime["runtime_status"] == "continuous_orchestration_runtime_ledger_visible"
    assert runtime["active_budget_breach_count"] == 0
    assert runtime["active_duplicate_risk_count"] == 0
    assert "cs-soak-provider-ack-loss" in runtime["operator_recovery_queue"]
    assert runtime["receipt_index"]["monitor_samples"][0]["within_budget"] is True
    assert runtime["receipt_index"]["side_effect_reconciliation_receipts"][2]["duplicate_safe"] is True
    assert runtime["claim_boundary"] == CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY


def test_continuous_orchestration_slo_report_runs_batch_cs_suites():
    payload = asyncio.run(build_continuous_orchestration_slo_report())

    assert payload["summary"]["benchmark_posture"] == "continuous_orchestration_slo_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES)
        + len(CRASH_FAILOVER_SOAK_SCENARIO_NAMES)
        + len(SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES)
    )
    assert payload["scenario_names"][CONTINUOUS_ORCHESTRATION_SLO_SUITE_NAME] == list(
        CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES
    )
    assert payload["scenario_names"][CRASH_FAILOVER_SOAK_SUITE_NAME] == list(CRASH_FAILOVER_SOAK_SCENARIO_NAMES)
    assert payload["scenario_names"][SIDE_EFFECT_RECONCILIATION_V2_SUITE_NAME] == list(
        SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES
    )
    assert payload["latest_run"]["failed"] == 0
