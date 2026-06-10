import asyncio

from src.workflows.production_sla_orchestration import (
    DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES,
    DUPLICATE_SIDE_EFFECT_AUDIT_SUITE_NAME,
    EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES,
    EXACTLY_ONCE_RECOVERY_EVIDENCE_SUITE_NAME,
    PRODUCTION_SLA_ORCHESTRATION_BLOCKED_CLAIMS,
    PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY,
    PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES,
    PRODUCTION_SLA_ORCHESTRATION_SUITE_NAME,
    build_production_sla_orchestration_contract,
    build_production_sla_orchestration_report,
)


def test_production_sla_orchestration_contract_exposes_sla_and_recovery_receipts():
    contract = build_production_sla_orchestration_contract()
    summary = contract["summary"]

    assert summary["operator_status"] == "production_sla_orchestration_receipts_visible"
    assert summary["sla_window_count"] == 3
    assert summary["failure_injection_count"] == 3
    assert summary["duplicate_side_effect_audit_count"] == 3
    assert summary["recorded_live_receipt_count"] >= 4
    assert summary["deterministic_contract_count"] >= 2
    assert summary["all_sla_windows_within_budget"] is True
    assert summary["all_failure_injections_have_resume_authority"] is True
    assert summary["duplicate_audits_reconciled"] is True
    assert summary["required_controls_visible"] is True
    assert summary["claim_boundary"] == PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY
    assert set(PRODUCTION_SLA_ORCHESTRATION_BLOCKED_CLAIMS) <= set(contract["policy"]["blocked_claims"])


def test_production_sla_orchestration_receipts_name_boundaries_and_uncertainty():
    contract = build_production_sla_orchestration_contract()

    for receipt in contract["sla_window_receipts"]:
        assert receipt["provider_identity_visible"] is True
        assert receipt["monitor_window"]
        assert int(receipt["max_jitter_ms"]) <= int(receipt["jitter_budget_ms"])
        assert int(receipt["missed_trigger_count"]) == 0
        assert receipt["residual_uncertainty"]

    for receipt in contract["failure_injection_receipts"]:
        assert receipt["failure_injection_method"]
        assert receipt["idempotency_scope"]
        assert receipt["side_effect_boundary"]
        assert receipt["resume_authority"]
        assert receipt["duplicate_suppression"]
        assert receipt["operator_visible"] is True

    for receipt in contract["duplicate_side_effect_audit_receipts"]:
        assert receipt["first_receipt"]
        assert receipt["duplicate_attempt"]
        assert receipt["reconciliation_status"]
        assert receipt["operator_visible"] is True


def test_production_sla_orchestration_operator_controls_leave_receipts():
    contract = build_production_sla_orchestration_contract()
    controls = {item["action"]: item for item in contract["operator_recovery_receipts"]}

    assert {"inspect", "audit", "resume", "repair", "branch", "cancel"} <= set(controls)
    assert all(item["enabled"] for item in controls.values())
    assert all(item["receipt_after_action"] for item in controls.values())
    assert controls["resume"]["requires_approval_or_review"] is True
    assert controls["cancel"]["mode"] == "direct"


def test_production_sla_orchestration_report_runs_batch_cj_suites():
    payload = asyncio.run(build_production_sla_orchestration_report())

    assert payload["summary"]["benchmark_posture"] == "production_sla_orchestration_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES)
        + len(EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES)
        + len(DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES)
    )
    assert payload["scenario_names"][PRODUCTION_SLA_ORCHESTRATION_SUITE_NAME] == list(
        PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES
    )
    assert payload["scenario_names"][EXACTLY_ONCE_RECOVERY_EVIDENCE_SUITE_NAME] == list(
        EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES
    )
    assert payload["scenario_names"][DUPLICATE_SIDE_EFFECT_AUDIT_SUITE_NAME] == list(
        DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES
    )
    assert payload["latest_run"]["failed"] == 0
