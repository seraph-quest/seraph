from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_operator_timeline_aggregates_threaded_workflows_notifications_and_repairs(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-1",
                        "workflow_name": "web-brief-to-file",
                        "summary": "Workflow is waiting on approval",
                        "status": "awaiting_approval",
                        "started_at": "2026-03-19T10:00:00Z",
                        "updated_at": "2026-03-19T10:02:00Z",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "thread_continue_message": "Continue from pending workflow approval.",
                        "replay_draft": None,
                        "replay_allowed": False,
                        "replay_block_reason": "pending_approval",
                        "replay_recommended_actions": [{"type": "set_tool_policy", "label": "Allow write_file", "mode": "full"}],
                        "risk_level": "medium",
                        "execution_boundaries": ["workspace_write"],
                        "pending_approval_count": 1,
                        "resume_from_step": "approval_gate",
                        "resume_checkpoint_label": "Approval gate",
                        "run_identity": "session-1:workflow_web_brief_to_file:web-brief",
                        "run_fingerprint": "web-brief",
                        "continued_error_steps": [],
                        "failed_step_tool": None,
                        "checkpoint_step_ids": ["search"],
                        "last_completed_step_id": "search",
                        "checkpoint_candidates": [
                            {
                                "step_id": "approval_gate",
                                "label": "Approval gate",
                                "kind": "approval_gate",
                                "status": "pending",
                            },
                            {
                                "step_id": "search",
                                "label": "search (web_search)",
                                "kind": "branch_from_checkpoint",
                                "status": "succeeded",
                            },
                        ],
                        "branch_kind": "approval_resume",
                        "branch_depth": 0,
                        "parent_run_identity": None,
                        "root_run_identity": "session-1:workflow_web_brief_to_file:web-brief",
                        "resume_plan": {
                            "source_run_identity": "session-1:workflow_web_brief_to_file:web-brief",
                            "parent_run_identity": "session-1:workflow_web_brief_to_file:web-brief",
                            "root_run_identity": "session-1:workflow_web_brief_to_file:web-brief",
                            "branch_kind": "approval_resume",
                            "resume_from_step": "approval_gate",
                            "resume_checkpoint_label": "Approval gate",
                            "requires_manual_execution": True,
                        },
                        "availability": "blocked",
                        "step_records": [
                            {"id": "search", "tool": "web_search", "status": "succeeded"},
                        ],
                    }
                ]
            ),
        ),
        patch(
            "src.api.operator.approval_repository.list_pending",
            AsyncMock(
                return_value=[
                    {
                        "id": "approval-1",
                        "tool_name": "workflow_web_brief_to_file",
                        "summary": "Approve workflow",
                        "created_at": "2026-03-19T10:01:00Z",
                        "session_id": "session-1",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "resume_message": "Resume after approval.",
                        "risk_level": "medium",
                    }
                ]
            ),
        ),
        patch(
            "src.api.operator.native_notification_queue.list",
            AsyncMock(
                return_value=[
                    SimpleNamespace(
                        id="note-1",
                        session_id="session-1",
                        title="Guardian note",
                        body="Please continue the earlier thread.",
                        intervention_type="advisory",
                        urgency=2,
                        resume_message="Continue from native notification.",
                        created_at="2026-03-19T10:03:00+00:00",
                    )
                ]
            ),
        ),
        patch(
            "src.api.operator.insight_queue.peek_all",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.operator.guardian_feedback_repository.list_recent",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.operator.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-1",
                        "event_type": "tool_failed",
                        "tool_name": "write_file",
                        "summary": "write_file failed",
                        "created_at": "2026-03-19T10:04:00Z",
                        "session_id": "session-1",
                        "risk_level": "medium",
                    }
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Session 1"}]),
        ),
    ):
        resp = await client.get("/api/operator/timeline", params={"session_id": "session-1", "limit": 8})

    assert resp.status_code == 200
    payload = resp.json()
    kinds = [item["kind"] for item in payload["items"]]
    assert "workflow_run" in kinds
    assert "approval" in kinds
    assert "notification" in kinds
    assert "audit" in kinds

    workflow_item = next(item for item in payload["items"] if item["kind"] == "workflow_run")
    assert workflow_item["thread_label"] == "Session 1"
    assert workflow_item["continue_message"] == "Continue from pending workflow approval."
    assert workflow_item["recommended_actions"][0]["type"] == "set_tool_policy"
    assert workflow_item["metadata"]["run_identity"] == "session-1:workflow_web_brief_to_file:web-brief"
    assert workflow_item["metadata"]["run_fingerprint"] == "web-brief"
    assert workflow_item["metadata"]["branch_kind"] == "approval_resume"
    assert workflow_item["metadata"]["checkpoint_candidates"][0]["step_id"] == "approval_gate"
    assert workflow_item["metadata"]["resume_plan"]["resume_from_step"] == "approval_gate"

    approval_item = next(item for item in payload["items"] if item["kind"] == "approval")
    assert approval_item["continue_message"] == "Resume after approval."

    notification_item = next(item for item in payload["items"] if item["kind"] == "notification")
    assert notification_item["continue_message"] == "Continue from native notification."
    assert notification_item["created_at"] == "2026-03-19T10:03:00+00:00"


@pytest.mark.asyncio
async def test_operator_timeline_uses_session_scoped_interventions_for_requested_thread(client):
    intervention_repo = AsyncMock(return_value=[])

    with (
        patch("src.api.operator._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.operator.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.operator.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.operator.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.operator.guardian_feedback_repository.list_recent", intervention_repo),
        patch("src.api.operator.audit_repository.list_events", AsyncMock(return_value=[])),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Session 1"}]),
        ),
    ):
        resp = await client.get("/api/operator/timeline", params={"session_id": "session-1", "limit": 8})

    assert resp.status_code == 200
    intervention_repo.assert_awaited_once_with(limit=12, session_id="session-1")


@pytest.mark.asyncio
async def test_operator_timeline_keeps_queued_insight_thread_mapping_for_session(client):
    intervention = SimpleNamespace(
        id="intervention-1",
        session_id="session-1",
        intervention_type="advisory",
        content_excerpt="Follow up on the earlier note.",
        latest_outcome="bundled",
        updated_at="2026-03-19T10:02:00+00:00",
        policy_action="bundle",
        policy_reason="blocked_state",
        transport="bundle_queue",
        feedback_type=None,
    )
    queued_insight = SimpleNamespace(
        id="queued-1",
        intervention_id="intervention-1",
        intervention_type="advisory",
        content="Continue this queued intervention thread.",
        urgency=2,
        reasoning="blocked_state",
        created_at="2026-03-19T10:03:00+00:00",
    )

    with (
        patch("src.api.operator._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.operator.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.operator.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.operator.insight_queue.peek_all", AsyncMock(return_value=[queued_insight])),
        patch(
            "src.api.operator.guardian_feedback_repository.list_recent",
            AsyncMock(return_value=[intervention]),
        ),
        patch("src.api.operator.audit_repository.list_events", AsyncMock(return_value=[])),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Session 1"}]),
        ),
    ):
        resp = await client.get("/api/operator/timeline", params={"session_id": "session-1", "limit": 8})

    assert resp.status_code == 200
    payload = resp.json()
    queued_item = next(item for item in payload["items"] if item["kind"] == "queued_insight")
    assert queued_item["thread_id"] == "session-1"
    assert queued_item["thread_label"] == "Session 1"


@pytest.mark.asyncio
async def test_operator_timeline_uses_persisted_queued_insight_session_id_when_recent_window_is_empty(client):
    queued_insight = SimpleNamespace(
        id="queued-1",
        intervention_id="intervention-older",
        session_id="session-1",
        intervention_type="advisory",
        content="Continue this queued intervention thread.",
        urgency=2,
        reasoning="blocked_state",
        created_at="2026-03-19T10:03:00+00:00",
    )

    with (
        patch("src.api.operator._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.operator.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.operator.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.operator.insight_queue.peek_all", AsyncMock(return_value=[queued_insight])),
        patch("src.api.operator.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch("src.api.operator.audit_repository.list_events", AsyncMock(return_value=[])),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Session 1"}]),
        ),
    ):
        resp = await client.get("/api/operator/timeline", params={"session_id": "session-1", "limit": 8})

    assert resp.status_code == 200
    payload = resp.json()
    queued_item = next(item for item in payload["items"] if item["kind"] == "queued_insight")
    assert queued_item["thread_id"] == "session-1"
    assert queued_item["thread_label"] == "Session 1"


@pytest.mark.asyncio
async def test_operator_timeline_projects_routing_metadata(client):
    with (
        patch("src.api.operator._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.operator.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.operator.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.operator.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.operator.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch(
            "src.api.operator.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-routing-1",
                        "event_type": "llm_routing_decision",
                        "tool_name": "chat_agent",
                        "summary": "Selected openrouter/gpt-4o-mini for chat_agent",
                        "created_at": "2026-03-19T10:04:00Z",
                        "session_id": "session-1",
                        "details": {
                            "runtime_path": "chat_agent",
                            "runtime_profile": "balanced",
                            "selected_model": "openrouter/gpt-4o-mini",
                            "selected_profile": "default",
                            "selected_source": "fallback",
                            "selected_reason_codes": ["policy_score", "healthy"],
                            "selected_policy_score": 8.5,
                            "required_policy_intents": ["fast", "cheap"],
                            "max_cost_tier": "medium",
                            "max_latency_tier": "medium",
                            "required_task_class": "interactive",
                            "max_budget_class": "standard",
                            "attempt_order": ["gpt-4o-mini", "gpt-4.1-mini"],
                            "reroute_cause": "primary_timeout",
                            "rerouted_from_unhealthy_primary": False,
                            "rerouted_from_policy_guardrails": True,
                            "guardrail_compliant_targets_present": True,
                            "rejected_target_count": 2,
                            "candidate_targets": ["gpt-4o-mini", "gpt-4.1-mini", "local-model"],
                            "rejected_targets": [
                                {"target": "local-model", "reason": "task_class_mismatch"},
                                {"target": "gpt-4.1-mini", "reason": "latency_tier_exceeded"},
                            ],
                        },
                    }
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Session 1"}]),
        ),
    ):
        resp = await client.get("/api/operator/timeline", params={"session_id": "session-1", "limit": 8})

    assert resp.status_code == 200
    payload = resp.json()
    routing_item = next(item for item in payload["items"] if item["kind"] == "routing")
    assert routing_item["status"] == "selected"
    assert routing_item["thread_id"] == "session-1"
    assert routing_item["metadata"]["runtime_path"] == "chat_agent"
    assert routing_item["metadata"]["runtime_profile"] == "balanced"
    assert routing_item["metadata"]["selected_model"] == "openrouter/gpt-4o-mini"
    assert routing_item["metadata"]["selected_reason_codes"] == ["policy_score", "healthy"]
    assert routing_item["metadata"]["reroute_cause"] == "primary_timeout"
    assert routing_item["metadata"]["rejected_target_count"] == 2
    assert routing_item["metadata"]["guardrail_compliant_targets_present"] is True


@pytest.mark.asyncio
async def test_operator_timeline_uses_retry_draft_when_no_thread_continue_message_exists(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-2",
                        "workflow_name": "retryable-save",
                        "summary": "retryable-save degraded",
                        "status": "failed",
                        "started_at": "2026-03-19T10:00:00Z",
                        "updated_at": "2026-03-19T10:02:00Z",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "thread_continue_message": None,
                        "approval_recovery_message": None,
                        "retry_from_step_draft": "Retry workflow \"retryable-save\" from step \"save\".",
                        "replay_draft": None,
                        "replay_allowed": False,
                        "replay_block_reason": "workflow_unavailable",
                        "replay_recommended_actions": [],
                        "risk_level": "medium",
                        "execution_boundaries": ["workspace_write"],
                        "pending_approval_count": 0,
                        "run_identity": "session-1:workflow_retryable_save:retryable",
                        "run_fingerprint": "retryable",
                        "continued_error_steps": ["save"],
                        "failed_step_tool": "write_file",
                        "checkpoint_step_ids": ["search", "save"],
                        "last_completed_step_id": "search",
                        "checkpoint_candidates": [
                            {
                                "step_id": "save",
                                "label": "save (write_file)",
                                "kind": "retry_failed_step",
                                "status": "continued_error",
                            },
                        ],
                        "branch_kind": "retry_failed_step",
                        "branch_depth": 0,
                        "parent_run_identity": None,
                        "root_run_identity": "session-1:workflow_retryable_save:retryable",
                        "resume_plan": {
                            "resume_from_step": "save",
                            "draft": "Retry workflow \"retryable-save\" from step \"save\".",
                        },
                        "availability": "blocked",
                        "step_records": [
                            {"id": "search", "tool": "web_search", "status": "succeeded"},
                            {"id": "save", "tool": "write_file", "status": "continued_error"},
                        ],
                    }
                ]
            ),
        ),
        patch("src.api.operator.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.operator.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.operator.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.operator.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch("src.api.operator.audit_repository.list_events", AsyncMock(return_value=[])),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Session 1"}]),
        ),
    ):
        resp = await client.get("/api/operator/timeline", params={"session_id": "session-1", "limit": 8})

    assert resp.status_code == 200
    payload = resp.json()
    workflow_item = next(item for item in payload["items"] if item["kind"] == "workflow_run")
    assert workflow_item["continue_message"] == "Retry workflow \"retryable-save\" from step \"save\"."
