import asyncio

import pytest

from src.workflows.production_workflow_guarantees import (
    CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SCENARIO_NAMES,
    CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SUITE_NAME,
    EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SCENARIO_NAMES,
    EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SUITE_NAME,
    PRODUCTION_WORKFLOW_GUARANTEES_BLOCKED_CLAIMS,
    PRODUCTION_WORKFLOW_GUARANTEES_CLAIM_BOUNDARY,
    PRODUCTION_WORKFLOW_STATE_MACHINE_SCENARIO_NAMES,
    PRODUCTION_WORKFLOW_STATE_MACHINE_SUITE_NAME,
    build_production_workflow_guarantees_contract,
    build_production_workflow_guarantees_report,
    production_workflow_guarantee_repository,
)


def test_production_workflow_guarantees_contract_exposes_da_only_receipts():
    contract = build_production_workflow_guarantees_contract()
    summary = contract["summary"]

    assert summary["operator_status"] == "production_workflow_guarantees_visible"
    assert summary["state_machine_receipt_count"] == 3
    assert summary["fault_campaign_receipt_count"] == 9
    assert summary["external_side_effect_reconciliation_v3_count"] == 3
    assert summary["all_state_receipts_persisted"] is True
    assert summary["all_fault_modes_have_replay_decisions"] is True
    assert summary["reconciliation_v3_complete"] is True
    assert summary["required_controls_visible"] is True
    assert summary["receipt_index_digest"]
    assert summary["claim_boundary"] == PRODUCTION_WORKFLOW_GUARANTEES_CLAIM_BOUNDARY
    assert set(PRODUCTION_WORKFLOW_GUARANTEES_BLOCKED_CLAIMS) <= set(contract["policy"]["blocked_claims"])
    assert "/api/operator/production-workflow-guarantees" in contract["policy"]["receipt_surfaces"]
    assert contract["receipt_index"]["state_machine_receipts"]
    assert contract["receipt_index"]["fault_campaign_receipts"]
    assert contract["receipt_index"]["external_side_effect_reconciliation_v3_receipts"]
    assert contract["receipt_index"]["composed_sources"]["continuous_orchestration_slo"]


def test_production_workflow_guarantees_receipts_cover_faults_and_replay_boundaries():
    contract = build_production_workflow_guarantees_contract()
    fault_modes = set(contract["summary"]["fault_modes_covered"])

    assert {
        "scheduler_crash",
        "worker_crash",
        "duplicate_delivery",
        "provider_timeout",
        "stale_lease",
        "partial_external_side_effect",
        "irreversible_side_effect",
        "restart_during_approval_wait",
        "trust_boundary_drift_replay",
    } <= fault_modes
    assert any(
        item["blocked_replay_reason"] == "approval_context_changed"
        for item in contract["state_machine_receipts"]
    )
    assert any(
        item["external_confirmation_state"] == "quarantined"
        and item["operator_replay_decision"] == "unsafe_retry_blocked"
        for item in contract["external_side_effect_reconciliation_v3_receipts"]
    )
    assert all(item["raw_receipt_handle"] for item in contract["fault_campaign_receipts"])


@pytest.mark.asyncio
async def test_production_workflow_guarantee_repository_persists_authority_and_blocks_stale_owner(async_db):
    state = await production_workflow_guarantee_repository.upsert_authority_state(
        run_identity="da-run-1",
        workflow_name="release-brief",
        scheduler_state_owner="seraph_scheduler_contract",
        workflow_lease_id="lease-a",
        worker_owner="worker-a",
        workflow_phase="intake",
        resumable_step_state="collect_step_completed",
        replay_window="24h_provider_history_window",
        recovery_authority="automatic_resume_allowed_before_external_side_effect",
        safe_replay_decision="safe",
        side_effect_status="not_started",
    )

    assert state["persisted_runtime_state"] == "production_workflow_authority_states"
    assert state["lease_revision"] == 1

    recorded = await production_workflow_guarantee_repository.record_transition(
        run_identity="da-run-1",
        transition_key="da-run-1:resume:write",
        transition_type="resume",
        worker_owner="worker-a",
        workflow_lease_id="lease-a",
        next_phase="external_side_effect_confirmation",
        resumable_step_state="write_step_started",
        side_effect_status="started",
        expected_revision=1,
    )
    blocked = await production_workflow_guarantee_repository.record_transition(
        run_identity="da-run-1",
        transition_key="da-run-1:stale-owner:write",
        transition_type="resume",
        worker_owner="worker-b",
        workflow_lease_id="lease-b",
        next_phase="external_side_effect_confirmation",
        resumable_step_state="write_step_started",
        side_effect_status="started",
    )
    deduped = await production_workflow_guarantee_repository.record_transition(
        run_identity="da-run-1",
        transition_key="da-run-1:resume:write",
        transition_type="resume",
        worker_owner="worker-a",
        workflow_lease_id="lease-a",
        next_phase="external_side_effect_confirmation",
        resumable_step_state="write_step_started",
        side_effect_status="started",
    )

    assert recorded and recorded["status"] == "recorded"
    assert blocked and blocked["status"] == "blocked"
    assert blocked["blocked_reason"] == "active_owner_lease_required"
    assert deduped and deduped["status"] == "deduped"


@pytest.mark.asyncio
async def test_production_workflow_guarantee_repository_persists_faults_and_side_effects(async_db):
    fault = await production_workflow_guarantee_repository.record_fault_receipt(
        fault_key="da-fault-provider-timeout",
        run_identity="da-run-2",
        injection_method="external_provider_timeout_before_ack",
        recovery_result="audit_required",
        replay_decision="manual_audit",
        raw_receipt_handle="receipt://batch-da/fault-campaign/provider-timeout",
        operator_intervention_required=True,
        residual_risk="provider timeout needs external confirmation",
    )
    side_effect = await production_workflow_guarantee_repository.record_side_effect_receipt(
        reconciliation_id="da-v3-provider-timeout",
        run_identity="da-run-2",
        side_effect_kind="external_provider_write",
        idempotency_scope="side_effect_receipt",
        idempotency_key="provider-write:timeout:20260611:v3",
        external_confirmation_state="quarantined",
        provider_receipt="provider_ack_missing",
        duplicate_suppression_receipt="unsafe_retry_blocked_pending_manual_audit",
        reconciliation_outcome="quarantined_until_external_confirmation",
        manual_repair_state="required_before_retry",
        operator_replay_decision="unsafe_retry_blocked",
    )
    duplicate = await production_workflow_guarantee_repository.record_side_effect_receipt(
        reconciliation_id="da-v3-provider-timeout-duplicate",
        run_identity="da-run-2",
        side_effect_kind="external_provider_write",
        idempotency_scope="side_effect_receipt",
        idempotency_key="provider-write:timeout:20260611:v3",
        external_confirmation_state="confirmed",
        provider_receipt="should_not_replace_original",
        duplicate_suppression_receipt="duplicate_attempt_reused_existing_receipt",
        reconciliation_outcome="duplicate_suppressed",
        manual_repair_state="not_required",
        operator_replay_decision="safe_no_retry_needed",
    )
    snapshot = await production_workflow_guarantee_repository.snapshot()

    assert fault["raw_receipt_handle"].startswith("receipt://batch-da/fault-campaign/")
    assert side_effect["external_confirmation_state"] == "quarantined"
    assert "provider_receipt" not in side_effect
    assert side_effect["provider_receipt_digest"]
    assert side_effect["redacted_receipt_handle"].startswith("receipt://batch-da/side-effect/")
    assert duplicate["external_confirmation_state"] == "quarantined"
    assert snapshot["persisted_fault_receipt_count"] == 1
    assert snapshot["persisted_side_effect_receipt_count"] == 1
    assert "persisted_authority_state" in snapshot["missing_evidence"]


@pytest.mark.asyncio
async def test_production_workflow_guarantees_report_exposes_persisted_runtime_snapshot(async_db):
    await production_workflow_guarantee_repository.upsert_authority_state(
        run_identity="da-run-3",
        workflow_name="approval-wait",
        scheduler_state_owner="durable_workflow_engine_v2",
        workflow_lease_id="lease-c",
        worker_owner="operator-review-queue",
        workflow_phase="awaiting_operator_approval",
        resumable_step_state="write_step_prepared_external_effect_blocked",
        replay_window="operator_approval_context_digest_window",
        recovery_authority="operator_audit_required_before_resume",
        safe_replay_decision="unsafe_until_operator_confirms",
        blocked_replay_reason="approval_context_changed",
        side_effect_status="blocked",
    )
    await production_workflow_guarantee_repository.record_fault_receipt(
        fault_key="da-fault-approval-restart",
        run_identity="da-run-3",
        injection_method="process_restart_while_awaiting_approval",
        recovery_result="blocked",
        replay_decision="unsafe",
        raw_receipt_handle="receipt://batch-da/fault-campaign/approval-restart",
        operator_intervention_required=True,
    )
    await production_workflow_guarantee_repository.record_side_effect_receipt(
        reconciliation_id="da-v3-approval-restart",
        run_identity="da-run-3",
        side_effect_kind="external_provider_write",
        idempotency_scope="side_effect_receipt",
        idempotency_key="provider-write:approval-restart:20260611:v3",
        external_confirmation_state="quarantined",
        provider_receipt="provider_ack_missing",
        duplicate_suppression_receipt="unsafe_retry_blocked_pending_manual_audit",
        reconciliation_outcome="quarantined_until_external_confirmation",
        manual_repair_state="required_before_retry",
        operator_replay_decision="unsafe_retry_blocked",
    )

    payload = await build_production_workflow_guarantees_report()

    assert payload["summary"]["benchmark_posture"] == "production_workflow_guarantees_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(PRODUCTION_WORKFLOW_STATE_MACHINE_SCENARIO_NAMES)
        + len(CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SCENARIO_NAMES)
        + len(EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SCENARIO_NAMES)
    )
    assert payload["summary"]["persisted_authority_state_count"] == 1
    assert payload["summary"]["persisted_fault_receipt_count"] == 1
    assert payload["summary"]["persisted_side_effect_receipt_count"] == 1
    assert payload["summary"]["missing_live_evidence"] == []
    assert payload["summary"]["unsafe_replay_count"] == 1
    assert payload["persisted_runtime"]["state_machine_receipts"][0]["blocked_replay_reason"] == (
        "approval_context_changed"
    )
    assert payload["scenario_names"][PRODUCTION_WORKFLOW_STATE_MACHINE_SUITE_NAME] == list(
        PRODUCTION_WORKFLOW_STATE_MACHINE_SCENARIO_NAMES
    )
    assert payload["scenario_names"][CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SUITE_NAME] == list(
        CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SCENARIO_NAMES
    )
    assert payload["scenario_names"][EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SUITE_NAME] == list(
        EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SCENARIO_NAMES
    )
    assert payload["latest_run"]["failed"] == 0


def test_production_workflow_guarantees_report_runs_da_suites(async_db):
    payload = asyncio.run(build_production_workflow_guarantees_report())

    assert payload["latest_run"]["failed"] == 0
    assert payload["summary"]["benchmark_posture"] == "production_workflow_guarantees_ci_gated_missing_persisted_evidence"
    assert payload["policy"]["claim_boundary"] == PRODUCTION_WORKFLOW_GUARANTEES_CLAIM_BOUNDARY
