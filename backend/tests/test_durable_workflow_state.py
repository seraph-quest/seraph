"""Tests for the minimal durable workflow state kernel."""

from __future__ import annotations

import json

import pytest

from src.workflows.durable_state import (
    DURABLE_STATE_CLAIM_BOUNDARY,
    build_durable_workflow_state_report,
    build_durable_workflow_v2_contract,
    build_durable_workflow_v2_report,
    durable_workflow_snapshot_dict,
    workflow_state_repository,
)


def test_durable_workflow_snapshot_is_deterministic_from_projection_dicts():
    run = {
        "run_identity": "session-1:workflow_release:abc",
        "root_run_identity": "session-1:workflow_release:abc",
        "workflow_name": "release",
        "status": "running",
        "thread_id": "session-1",
        "updated_at": "2026-05-12T08:30:00+00:00",
        "replay_allowed": True,
        "step_records": [
            {"id": "save", "index": 1, "tool": "write_file", "status": "running"},
            {"id": "draft", "index": 0, "tool": "llm", "status": "succeeded"},
        ],
        "checkpoint_candidates": [
            {
                "step_id": "draft",
                "label": "draft (llm)",
                "kind": "branch_from_checkpoint",
                "status": "succeeded",
                "resume_draft": 'Run workflow "release" from draft.',
            }
        ],
    }

    first = durable_workflow_snapshot_dict(run)
    reordered = {
        "checkpoint_candidates": list(reversed(run["checkpoint_candidates"])),
        "step_records": list(reversed(run["step_records"])),
        "replay_allowed": True,
        "updated_at": "2026-05-12T08:30:00+00:00",
        "thread_id": "session-1",
        "status": "running",
        "workflow_name": "release",
        "root_run_identity": "session-1:workflow_release:abc",
        "run_identity": "session-1:workflow_release:abc",
    }
    second = durable_workflow_snapshot_dict(reordered)

    assert first["snapshot_hash"] == second["snapshot_hash"]
    record = first["record"]
    assert record["state_hash"] == second["record"]["state_hash"]
    assert [step["id"] for step in record["steps"]] == ["draft", "save"]
    assert record["receipts"]["resume"]["resume_available"] is True
    assert record["receipts"]["trigger"]["heartbeat_observed"] is True
    assert first["claim_boundary"] == DURABLE_STATE_CLAIM_BOUNDARY
    assert [transition["kind"] for transition in record["transitions"]] == [
        "run_state",
        "step_state",
        "step_state",
        "resume_ready",
        "trigger_observed",
    ]


def test_resume_receipt_blocks_when_trust_boundary_changed():
    snapshot = durable_workflow_snapshot_dict({
        "run_identity": "session-1:workflow_auth:abc",
        "workflow_name": "authenticated-brief",
        "status": "failed",
        "replay_allowed": True,
        "replay_block_reason": "approval_context_changed",
        "resume_from_step": "save",
        "retry_from_step_draft": 'Run workflow "authenticated-brief" from save.',
        "trust_boundary": {
            "status": "changed",
            "reason": "approval_context_changed",
            "requires_fresh_run": True,
        },
        "approval_context": {
            "risk_level": "high",
            "execution_boundaries": ["external_mcp_credential_egress", "workspace_write"],
            "accepts_secret_refs": True,
            "authenticated_source": True,
        },
        "checkpoint_candidates": [{"step_id": "save", "label": "save", "status": "failed"}],
    })

    receipts = snapshot["record"]["receipts"]

    assert receipts["resume"]["resume_available"] is False
    assert receipts["resume"]["resume_from_step"] is None
    assert receipts["resume"]["draft"] is None
    assert receipts["resume"]["blocked_reason"] == "approval_context_changed"
    assert receipts["trust_boundary"]["blocked"] is True
    assert receipts["trust_boundary"]["requires_fresh_run"] is True
    assert receipts["trust_boundary"]["execution_boundaries"] == [
        "external_mcp_credential_egress",
        "workspace_write",
    ]
    assert receipts["claim_boundary"]["durable_claim_boundary"] == DURABLE_STATE_CLAIM_BOUNDARY
    assert "workflow_execution" in receipts["claim_boundary"]["not_claimed"]


def test_retry_repair_receipt_preserves_failed_step_and_recovery_actions():
    snapshot = durable_workflow_snapshot_dict({
        "run_identity": "session-2:workflow_daily:repair",
        "workflow_name": "daily-brief",
        "status": "failed",
        "retry_from_step_draft": 'Run workflow "daily-brief" from save.',
        "step_records": [
            {"id": "collect", "index": 0, "tool": "web_search", "status": "succeeded"},
            {
                "id": "save",
                "index": 1,
                "tool": "write_file",
                "status": "failed",
                "is_recoverable": True,
                "recovery_actions": [
                    {"type": "retry_tool"},
                    {"type": "set_tool_policy"},
                ],
            },
        ],
    })

    retry_repair = snapshot["record"]["receipts"]["retry_repair"]

    assert retry_repair["retry_available"] is True
    assert retry_repair["retry_from_step"] == "save"
    assert retry_repair["repair_available"] is True
    assert retry_repair["failed_step_ids"] == ["save"]
    assert retry_repair["repair_step_ids"] == ["save"]
    assert retry_repair["recovery_action_types"] == ["retry_tool", "set_tool_policy"]
    assert "repair_ready" in [transition["kind"] for transition in snapshot["record"]["transitions"]]


def test_heartbeat_and_reactive_trigger_receipt_is_receipt_only():
    snapshot = durable_workflow_snapshot_dict({
        "run_identity": "session-3:workflow_watch:live",
        "workflow_name": "watch-source",
        "status": "running",
        "trigger": {
            "type": "reactive",
            "heartbeat_at": "2026-05-12T09:00:00+00:00",
            "reactive_source": "observer.git",
        },
    })

    trigger = snapshot["record"]["receipts"]["trigger"]

    assert trigger["heartbeat_observed"] is True
    assert trigger["reactive_trigger_observed"] is True
    assert trigger["reactive_source"] == "observer.git"
    assert trigger["implemented_as_receipt_only"] is True
    assert "trigger_observed" in [transition["kind"] for transition in snapshot["record"]["transitions"]]


def test_delegated_artifact_review_receipt_preserves_trust_partition_and_artifacts():
    snapshot = durable_workflow_snapshot_dict({
        "run_identity": "session-4:workflow_review:artifact",
        "workflow_name": "artifact-review",
        "status": "awaiting_review",
        "artifact_paths": ["notes/review.md"],
        "approval_context": {
            "delegated_specialists": ["critic", "critic"],
            "delegated_tool_names": ["source_review"],
            "trust_partition": {"mode": "delegated_specialist", "blocked": False},
        },
        "delegated_artifact_review": {
            "review_state": "pending_review",
            "required": True,
        },
        "step_records": [
            {
                "id": "write",
                "index": 0,
                "tool": "write_file",
                "status": "succeeded",
                "artifact_paths": ["notes/review.md", "notes/evidence.json"],
            }
        ],
    })

    review = snapshot["record"]["receipts"]["delegated_artifact_review"]

    assert review["delegation_present"] is True
    assert review["artifact_review_present"] is True
    assert review["delegated_specialists"] == ["critic"]
    assert review["delegated_tool_names"] == ["source_review"]
    assert review["artifact_paths"] == ["notes/evidence.json", "notes/review.md"]
    assert review["review_state"] == "pending_review"
    assert review["required"] is True
    assert review["trust_partitions"] == [{"mode": "delegated_specialist", "blocked": False}]
    assert "artifact_review_recorded" in [
        transition["kind"] for transition in snapshot["record"]["transitions"]
    ]


def test_snapshot_preserves_audit_and_recorded_artifact_review_receipts():
    snapshot = durable_workflow_snapshot_dict({
        "run_identity": "session-5:workflow_review:runtime",
        "root_run_identity": "session-5:workflow_review:runtime",
        "workflow_name": "artifact-review",
        "status": "succeeded",
        "artifact_paths": ["notes/runtime.md"],
        "metadata": {
            "summary": "workflow_artifact_review succeeded",
            "content_redacted": True,
            "durable_audit_receipt_id": "receipt-123",
        },
        "approval_context": {
            "delegated_specialists": ["critic"],
            "delegated_tool_names": ["review_tool"],
        },
        "artifact_reviews": [
            {
                "run_identity": "session-5:workflow_review:runtime",
                "root_run_identity": "session-5:workflow_review:runtime",
                "artifact_path": "notes/runtime.md",
                "owner": "delegated_specialist",
                "review_state": "pending_operator_review",
                "reviewer": "critic",
                "metadata": {"durable_audit_receipt_id": "receipt-123"},
            }
        ],
    })

    receipts = snapshot["record"]["receipts"]

    assert snapshot["record"]["audit_receipt_id"] == "receipt-123"
    assert receipts["audit"]["durable_audit_receipt_id"] == "receipt-123"
    assert receipts["delegated_artifact_review"]["review_count"] == 1
    assert receipts["delegated_artifact_review"]["review_receipts"][0]["owner"] == "delegated_specialist"
    assert receipts["delegated_artifact_review"]["review_receipts"][0]["review_state"] == "pending_operator_review"


@pytest.mark.asyncio
async def test_workflow_state_repository_persists_run_steps_and_checkpoint(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-1:workflow_release:abc",
        root_run_identity="session-1:workflow_release:abc",
        parent_run_identity=None,
        workflow_name="release",
        tool_name="workflow_release",
        session_id="session-1",
        run_fingerprint="abc",
        arguments={"file_path": "release.md"},
        approval_context={"risk_level": "medium", "execution_boundaries": ["workspace_filesystem"]},
    )
    await workflow_state_repository.record_step_started(
        run_identity="session-1:workflow_release:abc",
        workflow_name="release",
        step_id="draft",
        step_index=1,
        tool_name="write_file",
        arguments={"file_path": "release.md"},
    )
    await workflow_state_repository.record_step_completed(
        run_identity="session-1:workflow_release:abc",
        step_id="draft",
        status="succeeded",
        result={"file_path": "release.md"},
        result_summary="object (1 keys)",
        artifact_paths=["release.md"],
        checkpoint={"tool": "write_file", "arguments": {"file_path": "release.md"}, "result": "done"},
    )
    await workflow_state_repository.finish_run(
        run_identity="session-1:workflow_release:abc",
        status="succeeded",
        checkpoint_context={
            "draft": {"tool": "write_file", "arguments": {"file_path": "release.md"}, "result": "done"}
        },
        artifact_paths=["release.md"],
        last_completed_step_id="draft",
    )

    runs = await workflow_state_repository.list_runs()
    checkpoint = await workflow_state_repository.get_checkpoint_payload("session-1:workflow_release:abc")

    assert runs[0]["state_source"] == "durable_workflow_state"
    assert runs[0]["step_records"][0]["id"] == "draft"
    assert checkpoint is not None
    assert checkpoint["checkpoint_context"]["draft"]["result"] == "done"
    assert checkpoint["state_source"] == "durable_workflow_state"


@pytest.mark.asyncio
async def test_workflow_state_repository_marks_stale_runs_interrupted(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-2:workflow_watch:abc",
        workflow_name="watch",
        tool_name="workflow_watch",
        session_id="session-2",
        run_fingerprint="abc",
        arguments={},
        approval_context={"risk_level": "low", "execution_boundaries": ["unknown"]},
    )

    interrupted = await workflow_state_repository.mark_stale_runs_interrupted(older_than_seconds=-1)

    assert interrupted
    assert interrupted[0]["status"] == "interrupted"
    assert interrupted[0]["metadata"]["resume_receipt"] is True


@pytest.mark.asyncio
async def test_workflow_state_repository_v2_lease_transition_trigger_and_recovery_receipts(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_release:abc",
        workflow_name="release-v2",
        tool_name="workflow_release_v2",
        session_id="session-v2",
        run_fingerprint="abc",
        arguments={},
        approval_context={"risk_level": "medium", "execution_boundaries": ["workspace_filesystem"]},
    )

    lease = await workflow_state_repository.acquire_or_renew_v2_lease(
        run_identity="session-v2:workflow_release:abc",
        owner="worker-a",
        lease_id="lease-a",
    )
    blocked_lease = await workflow_state_repository.acquire_or_renew_v2_lease(
        run_identity="session-v2:workflow_release:abc",
        owner="worker-b",
        lease_id="lease-b",
    )
    same_lease_takeover = await workflow_state_repository.acquire_or_renew_v2_lease(
        run_identity="session-v2:workflow_release:abc",
        owner="worker-b",
        lease_id="lease-a",
    )
    transition = await workflow_state_repository.record_v2_transition(
        run_identity="session-v2:workflow_release:abc",
        transition_key="resume:draft",
        transition_type="resume",
        owner="worker-a",
        step_id="draft",
    )
    non_owner_transition = await workflow_state_repository.record_v2_transition(
        run_identity="session-v2:workflow_release:abc",
        transition_key="resume:publish",
        transition_type="resume",
        owner="worker-b",
        step_id="publish",
    )
    deduped_transition = await workflow_state_repository.record_v2_transition(
        run_identity="session-v2:workflow_release:abc",
        transition_key="resume:draft",
        transition_type="resume",
        owner="worker-a",
        step_id="draft",
    )
    trigger = await workflow_state_repository.record_v2_trigger(
        run_identity="session-v2:workflow_release:abc",
        trigger_key="heartbeat:release-v2",
        trigger_kind="heartbeat",
        source="scheduler",
    )
    deduped_trigger = await workflow_state_repository.record_v2_trigger(
        run_identity="session-v2:workflow_release:abc",
        trigger_key="heartbeat:release-v2",
        trigger_kind="heartbeat",
        source="scheduler",
    )
    blocked_recovery = await workflow_state_repository.build_v2_recovery_plan(
        run_identity="session-v2:workflow_release:abc",
        owner="worker-a",
        approval_context={"risk_level": "high", "execution_boundaries": ["external_mcp_credential_egress"]},
    )

    assert lease is not None
    assert lease["receipt"]["status"] == "acquired"
    assert blocked_lease is not None
    assert blocked_lease["receipt"]["status"] == "blocked"
    assert blocked_lease["receipt"]["blocked_reason"] == "active_lease_owned_by_another_worker"
    assert blocked_lease["orchestration_v2"]["lease_conflict_receipts"][-1]["owner"] == "worker-b"
    assert same_lease_takeover is not None
    assert same_lease_takeover["receipt"]["status"] == "blocked"
    assert same_lease_takeover["receipt"]["blocked_reason"] == "active_lease_owned_by_another_worker"
    assert same_lease_takeover["orchestration_v2"]["lease_conflict_receipts"][-1]["lease_id"] == "lease-a"
    assert transition is not None
    assert transition["receipt"]["status"] == "recorded"
    assert non_owner_transition is not None
    assert non_owner_transition["receipt"]["status"] == "blocked"
    assert non_owner_transition["receipt"]["blocked_reason"] == "active_owner_lease_required"
    assert (
        non_owner_transition["orchestration_v2"]["transition_block_receipts"][-1]["blocked_reason"]
        == "active_owner_lease_required"
    )
    assert deduped_transition is not None
    assert deduped_transition["receipt"]["status"] == "deduped"
    assert trigger is not None
    assert trigger["receipt"]["external_action_allowed"] is False
    assert (
        trigger["receipt"]["authority_required"]
        == "recovery_plan_or_operator_resume_required_before_external_action"
    )
    assert deduped_trigger is not None
    assert deduped_trigger["receipt"]["status"] == "deduped"
    assert deduped_trigger["receipt"]["external_action_allowed"] is False
    assert blocked_recovery is not None
    assert blocked_recovery["receipt"]["status"] == "blocked"
    assert blocked_recovery["receipt"]["blocked_reason"] == "approval_context_changed"
    assert blocked_recovery["receipt"]["requires_fresh_run"] is True
    assert blocked_recovery["orchestration_v2"]["unsafe_recovery_refusal_receipts"][-1]["requires_fresh_run"] is True


@pytest.mark.asyncio
async def test_workflow_state_repository_stale_interrupt_preserves_v2_metadata(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_stale_preserve:abc",
        workflow_name="stale-preserve-v2",
        tool_name="workflow_stale_preserve_v2",
        session_id="session-v2",
        run_fingerprint="stale-preserve",
        arguments={},
        approval_context={"risk_level": "medium", "execution_boundaries": ["workspace_filesystem"]},
    )
    await workflow_state_repository.acquire_or_renew_v2_lease(
        run_identity="session-v2:workflow_stale_preserve:abc",
        owner="worker-a",
        lease_id="lease-a",
    )
    await workflow_state_repository.record_v2_transition(
        run_identity="session-v2:workflow_stale_preserve:abc",
        transition_key="resume:draft",
        transition_type="resume",
        owner="worker-a",
        step_id="draft",
    )

    interrupted = await workflow_state_repository.mark_stale_runs_interrupted(older_than_seconds=-1)

    run = next(item for item in interrupted if item["run_identity"] == "session-v2:workflow_stale_preserve:abc")
    orchestration_v2 = run["metadata"]["orchestration_v2"]
    assert run["status"] == "interrupted"
    assert run["metadata"]["resume_receipt"] is True
    assert orchestration_v2["lease"]["owner"] == "worker-a"
    assert orchestration_v2["lease"]["lease_id"] == "lease-a"
    assert orchestration_v2["transition_ledger"][-1]["transition_key"] == "resume:draft"
    assert orchestration_v2["restart_recovery_receipts"][-1]["preserved_orchestration_v2"] is True


@pytest.mark.asyncio
async def test_workflow_state_repository_v2_rejects_stale_owner_before_transition_dedupe(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_stale_owner:abc",
        workflow_name="stale-owner-v2",
        tool_name="workflow_stale_owner_v2",
        session_id="session-v2",
        run_fingerprint="stale-owner",
        arguments={},
        approval_context={"risk_level": "medium", "execution_boundaries": ["workspace_filesystem"]},
    )
    await workflow_state_repository.acquire_or_renew_v2_lease(
        run_identity="session-v2:workflow_stale_owner:abc",
        owner="worker-a",
        lease_id="lease-a",
    )
    recorded = await workflow_state_repository.record_v2_transition(
        run_identity="session-v2:workflow_stale_owner:abc",
        transition_key="resume:draft",
        transition_type="resume",
        owner="worker-a",
        step_id="draft",
    )
    stale_owner = await workflow_state_repository.record_v2_transition(
        run_identity="session-v2:workflow_stale_owner:abc",
        transition_key="resume:draft",
        transition_type="resume",
        owner="worker-b",
        step_id="draft",
    )

    assert recorded is not None
    assert recorded["receipt"]["status"] == "recorded"
    assert stale_owner is not None
    assert stale_owner["receipt"]["status"] == "blocked"
    assert stale_owner["receipt"]["blocked_reason"] == "active_owner_lease_required"
    assert stale_owner["orchestration_v2"]["transition_block_receipts"][-1]["owner"] == "worker-b"


@pytest.mark.asyncio
async def test_workflow_state_repository_v2_persists_dq_handoff_guardian_and_side_effect_receipts(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_dq_receipts:abc",
        workflow_name="dq-receipts-v2",
        tool_name="workflow_dq_receipts_v2",
        session_id="session-v2",
        run_fingerprint="dq-receipts",
        arguments={},
        approval_context={"risk_level": "medium", "execution_boundaries": ["workspace_filesystem"]},
    )
    await workflow_state_repository.acquire_or_renew_v2_lease(
        run_identity="session-v2:workflow_dq_receipts:abc",
        owner="worker-a",
        lease_id="lease-a",
    )
    handoff = await workflow_state_repository.record_v2_handoff(
        run_identity="session-v2:workflow_dq_receipts:abc",
        from_owner="worker-a",
        to_owner="worker-b",
        receiver_authority_accepted=False,
    )
    side_effect = await workflow_state_repository.record_v2_side_effect_boundary(
        run_identity="session-v2:workflow_dq_receipts:abc",
        side_effect_kind="repository_mutation",
        idempotency_scope="repo_branch_commit_tree",
        idempotency_key="raw-idempotency-key",
        external_confirmation_state="unknown_ack",
        reconciliation_status="manual_repair_required",
        duplicate_suppressed=True,
    )
    guardian = await workflow_state_repository.record_v2_guardian_recovery_context(
        run_identity="session-v2:workflow_dq_receipts:abc",
        guardian_recovery_context={"recent_context_shift": True, "private": "redacted before digest"},
        restraint_posture="operator_audit_required",
        reason_codes=["recent_context_shift"],
    )

    assert handoff is not None
    assert handoff["receipt"]["status"] == "blocked"
    assert handoff["receipt"]["blocked_reason"] == "receiver_authority_not_accepted"
    assert handoff["orchestration_v2"]["handoff_receipts"][-1]["to_owner"] == "worker-b"
    assert side_effect is not None
    assert side_effect["receipt"]["idempotency_key_digest"] != "raw-idempotency-key"
    assert side_effect["receipt"]["duplicate_suppressed"] is True
    assert side_effect["orchestration_v2"]["side_effect_boundary_receipts"][-1]["reconciliation_status"] == (
        "manual_repair_required"
    )
    assert guardian is not None
    assert guardian["receipt"]["authority_expanded"] is False
    assert guardian["receipt"]["guardian_recovery_context_digest"] != "redacted before digest"
    assert guardian["orchestration_v2"]["guardian_recovery_receipts"][-1]["restraint_posture"] == (
        "operator_audit_required"
    )


@pytest.mark.asyncio
async def test_workflow_state_repository_v2_persists_dy_live_failover_and_control_receipts(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_dy_receipts:abc",
        workflow_name="dy-receipts-v2",
        tool_name="workflow_dy_receipts_v2",
        session_id="session-v2",
        run_fingerprint="dy-receipts",
        arguments={},
        approval_context={"risk_level": "medium", "execution_boundaries": ["workspace_filesystem"]},
    )
    live_window = await workflow_state_repository.record_v2_live_orchestration_window(
        run_identity="session-v2:workflow_dy_receipts:abc",
        provider="temporal-cloud-private-provider-id",
        evidence_mode="recorded_live_window",
        window_duration_hours=96,
        expected_fire_count=192,
        observed_fire_count=192,
        max_jitter_ms=2000,
        jitter_budget_ms=5000,
        residual_risk="recorded window is not universal production proof",
    )
    failover = await workflow_state_repository.record_v2_failover_drill(
        run_identity="session-v2:workflow_dy_receipts:abc",
        failure_mode="worker_process_kill_before_external_write",
        provider="temporal-cloud-private-provider-id",
        failover_budget_ms=5000,
        observed_failover_ms=1800,
        replay_authority="safe_checkpoint_resume_before_external_effect",
        operator_recovery_control="resume_after_receipt_inspection",
    )
    duplicate = await workflow_state_repository.record_v2_duplicate_suppression(
        run_identity="session-v2:workflow_dy_receipts:abc",
        side_effect_kind="repository_mutation",
        idempotency_key="raw-idempotency-key",
        duplicate_attempt_count=2,
        suppressed_count=2,
    )
    control = await workflow_state_repository.record_v2_operator_recovery_control(
        run_identity="session-v2:workflow_dy_receipts:abc",
        action="repair",
        target="unknown_ack_manual_reconciliation",
        operator_context={"operator": "private-operator-id"},
    )
    unsafe_control = await workflow_state_repository.record_v2_operator_recovery_control(
        run_identity="session-v2:workflow_dy_receipts:abc",
        action="quarantine",
        target="provider://private-resource/user-123",
        operator_context={"operator": "private-operator-id"},
    )

    assert live_window is not None
    assert live_window["receipt"]["within_budget"] is True
    assert live_window["receipt"]["provider_digest"] != "temporal-cloud-private-provider-id"
    assert live_window["orchestration_v2"]["live_window_receipts"][-1]["evidence_mode"] == (
        "recorded_live_window"
    )
    assert failover is not None
    assert failover["receipt"]["within_budget"] is True
    assert failover["receipt"]["external_action_allowed"] is False
    assert failover["orchestration_v2"]["failover_receipts"][-1]["restart_preserved_checkpoint"] is True
    assert duplicate is not None
    assert duplicate["receipt"]["all_duplicates_suppressed"] is True
    assert duplicate["receipt"]["idempotency_key_digest"] != "raw-idempotency-key"
    assert duplicate["orchestration_v2"]["duplicate_suppression_receipts"][-1]["suppressed_count"] == 2
    assert control is not None
    assert control["receipt"]["operator_context_digest"] != "private-operator-id"
    assert control["receipt"]["external_action_allowed"] is False
    assert control["orchestration_v2"]["operator_recovery_control_receipts"][-1]["action"] == "repair"
    assert unsafe_control is not None
    serialized_unsafe_control = json.dumps(unsafe_control["receipt"], sort_keys=True)
    assert "provider://private-resource/user-123" not in serialized_unsafe_control
    assert unsafe_control["receipt"]["target"] == "redacted_operator_recovery_target"
    assert unsafe_control["receipt"]["target_digest"] != "provider://private-resource/user-123"


@pytest.mark.asyncio
async def test_workflow_state_repository_v2_blocks_transition_without_active_owner_lease(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_missing_lease:abc",
        workflow_name="missing-lease-v2",
        tool_name="workflow_missing_lease_v2",
        session_id="session-v2",
        run_fingerprint="missing-lease",
        arguments={},
        approval_context={"risk_level": "medium", "execution_boundaries": ["workspace_filesystem"]},
    )

    missing_lease_transition = await workflow_state_repository.record_v2_transition(
        run_identity="session-v2:workflow_missing_lease:abc",
        transition_key="resume:without-lease",
        transition_type="resume",
        owner="worker-a",
        step_id="draft",
    )

    assert missing_lease_transition is not None
    assert missing_lease_transition["receipt"]["status"] == "blocked"
    assert missing_lease_transition["receipt"]["blocked_reason"] == "active_owner_lease_required"
    assert missing_lease_transition["receipt"]["lease_owner"] is None
    assert (
        missing_lease_transition["orchestration_v2"]["transition_block_receipts"][-1]["transition_key"]
        == "resume:without-lease"
    )


@pytest.mark.asyncio
async def test_workflow_state_repository_v2_blocks_transition_with_expired_lease(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_expired_lease:abc",
        workflow_name="expired-lease-v2",
        tool_name="workflow_expired_lease_v2",
        session_id="session-v2",
        run_fingerprint="expired-lease",
        arguments={},
        approval_context={"risk_level": "medium", "execution_boundaries": ["workspace_filesystem"]},
    )
    await workflow_state_repository.acquire_or_renew_v2_lease(
        run_identity="session-v2:workflow_expired_lease:abc",
        owner="worker-a",
        lease_id="lease-expired",
        ttl_seconds=-1,
    )

    expired_lease_transition = await workflow_state_repository.record_v2_transition(
        run_identity="session-v2:workflow_expired_lease:abc",
        transition_key="resume:expired-lease",
        transition_type="resume",
        owner="worker-a",
        step_id="draft",
    )

    assert expired_lease_transition is not None
    assert expired_lease_transition["receipt"]["status"] == "blocked"
    assert expired_lease_transition["receipt"]["blocked_reason"] == "active_owner_lease_required"
    assert expired_lease_transition["receipt"]["lease_owner"] == "worker-a"
    assert (
        expired_lease_transition["orchestration_v2"]["transition_block_receipts"][-1]["transition_key"]
        == "resume:expired-lease"
    )


@pytest.mark.asyncio
async def test_workflow_state_repository_v2_blocks_artifact_adoption_without_review(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_review:abc",
        workflow_name="review-v2",
        tool_name="workflow_review_v2",
        session_id="session-v2",
        run_fingerprint="review",
        arguments={},
        approval_context={"risk_level": "medium", "delegated_specialists": ["critic"]},
    )

    blocked = await workflow_state_repository.record_v2_artifact_adoption(
        run_identity="session-v2:workflow_review:abc",
        artifact_path="notes/review.md",
        adopter="operator",
    )
    await workflow_state_repository.record_artifact_review(
        run_identity="session-v2:workflow_review:abc",
        root_run_identity="session-v2:workflow_review:abc",
        workflow_name="review-v2",
        artifact_path="notes/review.md",
        owner="delegated_specialist",
        review_state="approved",
        reviewer="critic",
        approval_id="approval-v2",
    )
    approved = await workflow_state_repository.record_v2_artifact_adoption(
        run_identity="session-v2:workflow_review:abc",
        artifact_path="notes/review.md",
        adopter="operator",
    )

    assert blocked is not None
    assert blocked["receipt"]["status"] == "blocked"
    assert blocked["receipt"]["blocked_reason"] == "missing_delegated_artifact_review_approval"
    assert approved is not None
    assert approved["receipt"]["status"] == "recorded"
    assert approved["receipt"]["approval_ids"] == ["approval-v2"]


@pytest.mark.asyncio
async def test_workflow_state_repository_v2_blocks_pending_artifact_handoff_with_approval_id(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_pending_review:abc",
        workflow_name="pending-review-v2",
        tool_name="workflow_pending_review_v2",
        session_id="session-v2",
        run_fingerprint="pending-review",
        arguments={},
        approval_context={"risk_level": "medium", "delegated_specialists": ["critic"]},
    )
    await workflow_state_repository.record_artifact_review(
        run_identity="session-v2:workflow_pending_review:abc",
        root_run_identity="session-v2:workflow_pending_review:abc",
        workflow_name="pending-review-v2",
        artifact_path="notes/pending.md",
        owner="delegated_specialist",
        review_state="pending_operator_review",
        reviewer="critic",
        approval_id="approval-pending",
    )

    blocked = await workflow_state_repository.record_v2_artifact_adoption(
        run_identity="session-v2:workflow_pending_review:abc",
        artifact_path="notes/pending.md",
        adopter="operator",
    )

    assert blocked is not None
    assert blocked["receipt"]["status"] == "blocked"
    assert blocked["receipt"]["blocked_reason"] == "missing_delegated_artifact_review_approval"
    assert blocked["receipt"]["approval_ids"] == []
    assert blocked["receipt"]["approved_review_count"] == 0


@pytest.mark.asyncio
async def test_workflow_state_repository_v2_blocks_rejected_artifact_handoff_with_approval_id(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-v2:workflow_rejected_review:abc",
        workflow_name="rejected-review-v2",
        tool_name="workflow_rejected_review_v2",
        session_id="session-v2",
        run_fingerprint="rejected-review",
        arguments={},
        approval_context={"risk_level": "medium", "delegated_specialists": ["critic"]},
    )
    await workflow_state_repository.record_artifact_review(
        run_identity="session-v2:workflow_rejected_review:abc",
        root_run_identity="session-v2:workflow_rejected_review:abc",
        workflow_name="rejected-review-v2",
        artifact_path="notes/rejected.md",
        owner="delegated_specialist",
        review_state="rejected",
        reviewer="critic",
        decision="rejected",
        approval_id="approval-rejected",
    )

    blocked = await workflow_state_repository.record_v2_artifact_adoption(
        run_identity="session-v2:workflow_rejected_review:abc",
        artifact_path="notes/rejected.md",
        adopter="operator",
    )

    assert blocked is not None
    assert blocked["receipt"]["status"] == "blocked"
    assert blocked["receipt"]["blocked_reason"] == "missing_delegated_artifact_review_approval"
    assert blocked["receipt"]["approval_ids"] == []
    assert blocked["receipt"]["approved_review_count"] == 0


@pytest.mark.asyncio
async def test_workflow_state_repository_records_delegated_artifact_review(async_db):
    review = await workflow_state_repository.record_artifact_review(
        run_identity="session-3:workflow_review:abc",
        root_run_identity="session-3:workflow_review:abc",
        workflow_name="artifact-review",
        artifact_path="notes/review.md",
        owner="delegated_specialist",
        review_state="pending_review",
        reviewer="critic",
        approval_id="approval-1",
        metadata={"trust_partition": "delegated_specialist"},
    )

    assert review["artifact_path"] == "notes/review.md"
    assert review["owner"] == "delegated_specialist"
    assert review["review_state"] == "pending_review"
    assert review["metadata"]["trust_partition"] == "delegated_specialist"


@pytest.mark.asyncio
async def test_durable_workflow_state_report_includes_persisted_snapshots(async_db):
    await workflow_state_repository.create_run(
        run_identity="session-4:workflow_runtime:abc",
        workflow_name="runtime-review",
        tool_name="workflow_runtime_review",
        session_id="session-4",
        run_fingerprint="abc",
        arguments={"file_path": "notes/runtime.md"},
        approval_context={
            "risk_level": "medium",
            "execution_boundaries": ["workspace_filesystem", "delegation"],
            "delegated_specialists": ["critic"],
        },
    )
    await workflow_state_repository.record_step_started(
        run_identity="session-4:workflow_runtime:abc",
        workflow_name="runtime-review",
        step_id="write",
        step_index=1,
        tool_name="write_file",
        arguments={"file_path": "notes/runtime.md"},
    )
    await workflow_state_repository.record_step_completed(
        run_identity="session-4:workflow_runtime:abc",
        step_id="write",
        status="succeeded",
        artifact_paths=["notes/runtime.md"],
        checkpoint={"tool": "write_file", "arguments": {"file_path": "notes/runtime.md"}, "result": "ok"},
    )
    await workflow_state_repository.finish_run(
        run_identity="session-4:workflow_runtime:abc",
        status="succeeded",
        checkpoint_context={
            "write": {"tool": "write_file", "arguments": {"file_path": "notes/runtime.md"}, "result": "ok"}
        },
        artifact_paths=["notes/runtime.md"],
        last_completed_step_id="write",
        metadata={
            "summary": "workflow_runtime_review succeeded",
            "content_redacted": True,
            "durable_audit_receipt_id": "runtime-receipt-1",
        },
    )
    await workflow_state_repository.record_artifact_review(
        run_identity="session-4:workflow_runtime:abc",
        root_run_identity="session-4:workflow_runtime:abc",
        workflow_name="runtime-review",
        artifact_path="notes/runtime.md",
        owner="delegated_specialist",
        review_state="pending_operator_review",
        reviewer="critic",
        metadata={"durable_audit_receipt_id": "runtime-receipt-1"},
    )

    report = await build_durable_workflow_state_report()

    assert report["summary"]["persisted_run_count"] == 1
    assert report["summary"]["persisted_snapshot_count"] == 1
    assert report["summary"]["state_count"] == 1
    assert report["proof_state_kernel"]["summary"]["state_count"] >= 2
    persisted_state = report["persisted_state_kernel"]["states"][0]
    assert persisted_state["audit"]["durable_audit_receipt_id"] == "runtime-receipt-1"
    assert persisted_state["artifact_review"]["receipts"][0]["review_state"] == "pending_operator_review"
    snapshot = report["persisted_run_snapshots"][0]
    assert snapshot["record"]["receipts"]["delegated_artifact_review"]["review_count"] == 1


@pytest.mark.asyncio
async def test_durable_workflow_v2_contract_and_report_expose_recovery_receipts(async_db):
    contract = build_durable_workflow_v2_contract()
    report = await build_durable_workflow_v2_report()

    assert contract["summary"]["lease_receipt_count"] >= 2
    assert contract["summary"]["blocked_lease_count"] >= 1
    assert contract["summary"]["transition_receipt_count"] >= 1
    assert contract["summary"]["blocked_transition_count"] >= 1
    assert contract["summary"]["deduped_trigger_count"] >= 1
    assert contract["summary"]["blocked_recovery_count"] >= 1
    assert contract["summary"]["blocked_artifact_adoption_count"] >= 1
    assert any(
        receipt["latest_lease_conflict"].get("blocked_reason")
        == "active_lease_owned_by_another_worker"
        for receipt in contract["receipts"]
    )
    assert any(
        receipt["latest_transition_block"].get("blocked_reason") == "active_owner_lease_required"
        for receipt in contract["receipts"]
    )
    assert "langgraph_class_durable_workflows" in contract["policy"]["blocked_claims"]
    assert report["summary"]["suite_name"] == "durable_workflow_engine_v2"
    assert report["summary"]["production_suite_name"] == "production_durable_orchestration"
    assert report["summary"]["blocked_lease_count"] >= 1
    assert report["summary"]["blocked_transition_count"] >= 1
    assert report["summary"]["benchmark_posture"] == "durable_workflow_engine_v2_ci_gated_operator_visible"
    assert "/api/operator/durable-workflow-engine-v2" in report["policy"]["operator_surfaces"]
