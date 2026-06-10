import asyncio

from src.workflows.live_orchestration import (
    LIVE_EXTERNAL_ORCHESTRATION_BLOCKED_CLAIMS,
    LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY,
    LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES,
    LIVE_EXTERNAL_ORCHESTRATION_SUITE_NAME,
    ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES,
    ORCHESTRATION_CRASH_RECOVERY_STUDY_SUITE_NAME,
    build_live_external_orchestration_contract,
    build_live_external_orchestration_report,
)


def test_live_external_orchestration_contract_exposes_provider_and_crash_receipts():
    contract = build_live_external_orchestration_contract()
    summary = contract["summary"]

    assert summary["operator_status"] == "live_external_orchestration_receipts_visible"
    assert summary["provider_receipt_count"] == 3
    assert summary["crash_study_count"] == 3
    assert summary["recorded_live_receipt_count"] >= 3
    assert summary["deterministic_contract_count"] >= 1
    assert summary["required_controls_visible"] is True
    assert summary["claim_boundary"] == LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY
    assert set(LIVE_EXTERNAL_ORCHESTRATION_BLOCKED_CLAIMS) <= set(contract["policy"]["blocked_claims"])


def test_live_external_orchestration_receipts_name_identity_idempotency_and_boundaries():
    contract = build_live_external_orchestration_contract()

    for receipt in contract["provider_attestation_receipts"]:
        assert receipt["provider_identity_visible"] is True
        assert receipt["evidence_mode"] in {"recorded_live_fixture", "deterministic_contract"}
        assert receipt["idempotency_key"]
        assert receipt["side_effect_boundary"]
        assert receipt["delivery_semantics"]
        assert receipt["residual_uncertainty"]

    for study in contract["crash_recovery_study_receipts"]:
        assert study["checkpoint"]
        assert study["replay_suppression"]
        assert study["resume_authority"]
        assert study["operator_visible"] is True


def test_live_external_orchestration_operator_controls_leave_receipts():
    contract = build_live_external_orchestration_contract()
    controls = {item["action"]: item for item in contract["operator_recovery_receipts"]}

    assert {"inspect", "resume", "retry", "branch", "cancel", "audit"} <= set(controls)
    assert all(item["enabled"] for item in controls.values())
    assert all(item["receipt_after_action"] for item in controls.values())
    assert controls["resume"]["requires_approval_or_review"] is True
    assert controls["cancel"]["mode"] == "direct"


def test_live_external_orchestration_report_runs_batch_cc_suites():
    payload = asyncio.run(build_live_external_orchestration_report())

    assert payload["summary"]["benchmark_posture"] == "live_external_orchestration_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES)
        + len(ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES)
    )
    assert payload["scenario_names"][LIVE_EXTERNAL_ORCHESTRATION_SUITE_NAME] == list(
        LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES
    )
    assert payload["scenario_names"][ORCHESTRATION_CRASH_RECOVERY_STUDY_SUITE_NAME] == list(
        ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES
    )
    assert payload["latest_run"]["failed"] == 0
