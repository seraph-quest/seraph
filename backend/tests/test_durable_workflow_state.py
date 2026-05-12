"""Tests for the minimal durable workflow state kernel."""

from __future__ import annotations

import pytest

from src.workflows.durable_state import (
    DURABLE_STATE_CLAIM_BOUNDARY,
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
