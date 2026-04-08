from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _default_empty_continuity_snapshot():
    with patch(
        "src.api.operator.build_observer_continuity_snapshot",
        AsyncMock(
            return_value={
                "daemon": {},
                "summary": {
                    "continuity_health": "ready",
                    "primary_surface": "browser",
                    "recommended_focus": None,
                },
                "recovery_actions": [],
            }
        ),
    ):
        yield


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
                        "extension_id": "seraph.test-installable",
                        "extension_display_name": "Test Installable",
                        "action": "source_save",
                        "package_path": "/tmp/extensions/test-installable",
                        "permissions": {"tool_names": ["write_file"]},
                        "approval_profile": {
                            "requires_lifecycle_approval": True,
                            "lifecycle_boundaries": ["workspace_write"],
                        },
                        "approval_scope": {
                            "action": "source_save",
                            "target": {
                                "type": "workflow_source",
                                "name": "write-note",
                                "reference": "workflows/write-note.md",
                            },
                            "source_scope": {
                                "reference": "workflows/write-note.md",
                                "requested_content_hash": "requested-hash",
                                "current_content_hash": "current-hash",
                            },
                        },
                        "approval_context": {
                            "risk_level": "medium",
                            "execution_boundaries": ["workspace_write"],
                        },
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
    assert approval_item["metadata"]["approval_id"] == "approval-1"
    assert approval_item["metadata"]["extension_id"] == "seraph.test-installable"
    assert approval_item["metadata"]["extension_action"] == "source_save"
    assert approval_item["metadata"]["lifecycle_boundaries"] == ["workspace_write"]
    assert approval_item["metadata"]["approval_scope"]["target"]["reference"] == "workflows/write-note.md"
    assert (
        approval_item["metadata"]["approval_context"]["execution_boundaries"] == ["workspace_write"]
    )

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
async def test_operator_timeline_surfaces_observer_recovery_actions(client):
    with (
        patch("src.api.operator._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.operator.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.operator.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.operator.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.operator.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch("src.api.operator.audit_repository.list_events", AsyncMock(return_value=[])),
        patch(
            "src.api.operator.build_observer_continuity_snapshot",
            AsyncMock(
                return_value={
                    "daemon": {"last_post": 1_775_521_600.0},
                    "summary": {
                        "continuity_health": "attention",
                        "primary_surface": "source_adapter",
                        "recommended_focus": "github-managed",
                        "presence_surface_count": 2,
                        "attention_presence_surface_count": 1,
                    },
                    "recovery_actions": [
                        {
                            "id": "adapter:github-managed",
                            "kind": "source_adapter_repair",
                            "label": "Restore source adapter github-managed",
                            "detail": "Reconnect the authenticated source adapter runtime.",
                            "status": "degraded",
                            "surface": "source_adapter",
                            "route": None,
                            "repair_hint": "Inspect the typed source adapter inventory and runtime bridge.",
                            "thread_id": None,
                            "continue_message": "Draft a repair plan for github-managed.",
                            "open_thread_available": False,
                        },
                        {
                            "id": "presence:messaging_connectors:seraph.relay:connectors/messaging/telegram.yaml",
                            "kind": "presence_repair",
                            "label": "Review presence surface Telegram relay",
                            "detail": "Seraph Relay Pack exposes Telegram relay on telegram (requires config).",
                            "status": "requires_config",
                            "surface": "presence",
                            "route": "messaging_connector",
                            "repair_hint": "Finish connector configuration in the operator surface before routing follow-through here.",
                            "thread_id": None,
                            "continue_message": None,
                            "open_thread_available": False,
                        },
                        {
                            "id": "presence:channel_adapters:seraph.native:channels/native.yaml",
                            "kind": "presence_follow_up",
                            "label": "Plan follow-up via native notification channel",
                            "detail": "Seraph Native Pack exposes native notification channel for native notification delivery (ready).",
                            "status": "ready",
                            "surface": "presence",
                            "route": "channel_adapter",
                            "repair_hint": None,
                            "thread_id": None,
                            "continue_message": "Plan guarded follow-through for native notification channel. Confirm the audience, target reference, channel scope, and approval boundaries before acting.",
                            "open_thread_available": False,
                        },
                        {
                            "id": "imported:messaging",
                            "kind": "imported_reach_attention",
                            "label": "Inspect imported reach family messaging",
                            "detail": "Messaging capability packages need operator attention.",
                            "status": "attention",
                            "surface": "imported_reach",
                            "route": None,
                            "repair_hint": "Inspect imported reach coverage before planning outreach.",
                            "thread_id": None,
                            "continue_message": None,
                            "open_thread_available": True,
                        },
                    ],
                }
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Session 1"}]),
        ),
    ):
        resp = await client.get("/api/operator/timeline", params={"limit": 20})

    assert resp.status_code == 200
    payload = resp.json()
    adapter_item = next(item for item in payload["items"] if item["id"] == "continuity:adapter:github-managed")
    assert adapter_item["kind"] == "reach_recovery"
    assert adapter_item["source"] == "continuity"
    assert adapter_item["continue_message"] == "Draft a repair plan for github-managed."
    assert adapter_item["metadata"]["kind"] == "source_adapter_repair"
    assert adapter_item["metadata"]["surface"] == "source_adapter"
    assert adapter_item["metadata"]["recommended_focus"] == "github-managed"
    assert adapter_item["metadata"]["presence_surface_count"] == 2
    assert adapter_item["metadata"]["attention_presence_surface_count"] == 1

    presence_item = next(
        item
        for item in payload["items"]
        if item["id"] == "continuity:presence:messaging_connectors:seraph.relay:connectors/messaging/telegram.yaml"
    )
    assert presence_item["metadata"]["kind"] == "presence_repair"
    assert presence_item["metadata"]["surface"] == "presence"

    follow_up_item = next(
        item
        for item in payload["items"]
        if item["id"] == "continuity:presence:channel_adapters:seraph.native:channels/native.yaml"
    )
    assert follow_up_item["metadata"]["kind"] == "presence_follow_up"
    assert follow_up_item["metadata"]["surface"] == "presence"
    assert follow_up_item["continue_message"] == (
        "Plan guarded follow-through for native notification channel. Confirm the audience, "
        "target reference, channel scope, and approval boundaries before acting."
    )

    imported_item = next(item for item in payload["items"] if item["id"] == "continuity:imported:messaging")
    assert imported_item["metadata"]["kind"] == "imported_reach_attention"
    assert imported_item["metadata"]["surface"] == "imported_reach"
    assert imported_item["metadata"]["primary_surface"] == "source_adapter"


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
async def test_operator_control_plane_synthesizes_governance_usage_runtime_and_handoff(client):
    with (
        patch("src.api.operator.settings.use_delegation", True),
        patch(
            "src.api.operator.context_manager.get_context",
            return_value=SimpleNamespace(
                approval_mode="high_risk",
                tool_policy_mode="balanced",
                mcp_policy_mode="approval",
            ),
        ),
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-1",
                        "workflow_name": "repo-review",
                        "summary": "Workflow is blocked by approval context drift",
                        "status": "blocked",
                        "availability": "blocked",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "started_at": "2026-04-08T10:00:00Z",
                        "updated_at": "2026-04-08T10:05:00Z",
                        "replay_block_reason": "approval_context_changed",
                        "thread_continue_message": "Start a fresh guarded repo review.",
                    },
                    {
                        "id": "run-2",
                        "workflow_name": "daily-brief",
                        "summary": "Workflow still running",
                        "status": "running",
                        "availability": "ready",
                        "thread_id": "session-2",
                        "thread_label": "Session 2",
                        "started_at": "2026-04-08T09:00:00Z",
                        "updated_at": "2026-04-08T09:03:00Z",
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.approval_repository.list_pending",
            AsyncMock(
                return_value=[
                    {
                        "id": "approval-1",
                        "tool_name": "write_file",
                        "summary": "Approve guarded write",
                        "risk_level": "high",
                        "session_id": "session-1",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "resume_message": "Resume after approval.",
                    }
                ]
            ),
        ),
        patch(
            "src.api.operator.build_observer_continuity_snapshot",
            AsyncMock(
                return_value={
                    "summary": {
                        "continuity_health": "attention",
                        "primary_surface": "presence",
                        "recommended_focus": "telegram relay",
                        "actionable_thread_count": 2,
                        "degraded_route_count": 1,
                        "degraded_source_adapter_count": 1,
                        "attention_presence_surface_count": 1,
                    },
                    "recovery_actions": [
                        {
                            "id": "presence:telegram",
                            "kind": "presence_repair",
                            "label": "Review Telegram relay",
                            "detail": "Connector requires config.",
                            "status": "requires_config",
                            "thread_id": "session-1",
                            "continue_message": "Plan the Telegram repair.",
                        },
                        {
                            "id": "presence:other",
                            "kind": "presence_repair",
                            "label": "Ignore unrelated follow-up",
                            "detail": "Different session.",
                            "status": "requires_config",
                            "thread_id": "session-2",
                            "continue_message": "Do not surface this.",
                        }
                    ],
                }
            ),
        ),
        patch(
            "src.api.operator.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-1",
                        "event_type": "approval_requested",
                        "tool_name": "write_file",
                        "summary": "Approval requested for write_file",
                        "created_at": "2026-04-08T10:01:00Z",
                        "session_id": "session-1",
                    },
                    {
                        "id": "audit-2",
                        "event_type": "tool_failed",
                        "tool_name": "write_file",
                        "summary": "write_file failed",
                        "created_at": "2026-04-08T10:02:00Z",
                        "session_id": "session-1",
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.list_recent_llm_calls",
            return_value=[
                {
                    "timestamp": "2026-04-08T10:00:00Z",
                    "source": "rest_chat",
                    "cost_usd": 0.012,
                    "tokens": {"input": 120, "output": 60},
                },
                {
                    "timestamp": "2026-04-08T10:01:00Z",
                    "source": "background",
                    "cost_usd": 0.004,
                    "tokens": {"input": 40, "output": 25},
                },
            ],
        ),
        patch(
            "src.api.operator.list_extensions",
            return_value={
                "summary": {
                    "total": 3,
                    "ready": 2,
                    "degraded": 1,
                    "issue_count": 4,
                    "degraded_connector_count": 2,
                },
                "extensions": [
                    {"id": "ext-1", "approval_profile": {"requires_lifecycle_approval": True}},
                    {"id": "ext-2", "approval_profile": {"requires_lifecycle_approval": False}},
                ],
            },
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Session 1"}]),
        ),
    ):
        resp = await client.get("/api/operator/control-plane", params={"window_hours": 24})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["governance"]["workspace_mode"] == "single_operator_guarded_workspace"
    assert payload["governance"]["approval_mode"] == "high_risk"
    assert payload["governance"]["delegation_enabled"] is True
    assert payload["governance"]["roles"][0]["id"] == "human_operator"

    usage = payload["usage"]
    assert usage["llm_call_count"] == 2
    assert usage["llm_cost_usd"] == 0.016
    assert usage["pending_approvals"] == 1
    assert usage["active_workflows"] == 2
    assert usage["blocked_workflows"] == 1
    assert usage["failure_count"] == 1

    runtime_posture = payload["runtime_posture"]
    assert runtime_posture["extensions"]["total"] == 3
    assert runtime_posture["extensions"]["governed"] == 1
    assert runtime_posture["continuity"]["continuity_health"] == "attention"
    assert runtime_posture["continuity"]["degraded_route_count"] == 1

    handoff = payload["handoff"]
    assert handoff["pending_approvals"][0]["label"] == "write_file"
    assert handoff["blocked_workflows"][0]["label"] == "repo-review"
    assert handoff["blocked_workflows"][0]["trust_boundary"]["status"] == "changed"
    assert handoff["blocked_workflows"][0]["trust_boundary"]["reason"] == "approval_context_changed"
    assert handoff["follow_ups"][0]["label"] == "Review Telegram relay"
    assert len(handoff["follow_ups"]) == 2
    assert handoff["follow_ups"][1]["label"] == "Ignore unrelated follow-up"
    assert handoff["review_receipts"][0]["status"] == "approval_requested"


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
                            "budget_steering_mode": "prefer_lower_budget",
                            "selected_budget_headroom": 1,
                            "selected_budget_preference_score": 2.0,
                            "selected_route_score": 10.5,
                            "selected_failure_risk_score": 3.5,
                            "selected_production_readiness": "guarded",
                            "selected_live_feedback": {
                                "feedback_state": "recovering",
                                "recent_failure_count": 1,
                            },
                            "attempt_order": ["gpt-4o-mini", "gpt-4.1-mini"],
                            "reroute_cause": "primary_timeout",
                            "rerouted_from_unhealthy_primary": False,
                            "rerouted_from_policy_guardrails": True,
                            "guardrail_compliant_targets_present": True,
                            "route_explanation": "selected openrouter/gpt-4o-mini; readiness=guarded; failure_risk=3.5; rejected=2",
                            "rejected_target_count": 2,
                            "rejected_target_summaries": [
                                {
                                    "model_id": "local-model",
                                    "source": "local",
                                    "decision": "skipped",
                                    "production_readiness": "degraded",
                                    "failure_risk_score": 4.0,
                                    "reason_codes": ["task_class_mismatch"],
                                }
                            ],
                            "candidate_targets": ["gpt-4o-mini", "gpt-4.1-mini", "local-model"],
                            "simulated_routes": [
                                {
                                    "rank": 1,
                                    "entry_model": "gpt-4o-mini",
                                    "selected": True,
                                    "route_score": 10.5,
                                }
                            ],
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
    assert routing_item["metadata"]["budget_steering_mode"] == "prefer_lower_budget"
    assert routing_item["metadata"]["selected_budget_preference_score"] == 2.0
    assert routing_item["metadata"]["selected_route_score"] == 10.5
    assert routing_item["metadata"]["selected_failure_risk_score"] == 3.5
    assert routing_item["metadata"]["selected_production_readiness"] == "guarded"
    assert routing_item["metadata"]["selected_live_feedback"]["feedback_state"] == "recovering"
    assert routing_item["metadata"]["route_explanation"].startswith("selected openrouter/gpt-4o-mini")
    assert routing_item["metadata"]["rejected_target_count"] == 2
    assert routing_item["metadata"]["rejected_target_summaries"][0]["model_id"] == "local-model"
    assert routing_item["metadata"]["guardrail_compliant_targets_present"] is True
    assert routing_item["metadata"]["simulated_routes"][0]["entry_model"] == "gpt-4o-mini"


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


@pytest.mark.asyncio
async def test_operator_timeline_hides_stale_resume_surface_when_workflow_boundary_is_blocked(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-3",
                        "workflow_name": "authenticated-brief",
                        "summary": "Run is blocked by trust-boundary drift.",
                        "status": "failed",
                        "started_at": "2026-03-19T10:00:00Z",
                        "updated_at": "2026-03-19T10:02:00Z",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "thread_continue_message": "Continue from stale approval.",
                        "approval_recovery_message": (
                            "Workflow 'authenticated-brief' changed its trust boundary after this run. "
                            "Start a fresh run instead of replaying or resuming."
                        ),
                        "retry_from_step_draft": "Retry workflow \"authenticated-brief\" from step \"save\".",
                        "replay_draft": "Replay authenticated workflow",
                        "replay_allowed": True,
                        "replay_block_reason": "approval_context_changed",
                        "replay_recommended_actions": [
                            {"type": "set_tool_policy", "label": "Allow write_file", "mode": "full"}
                        ],
                        "trust_boundary": {
                            "status": "changed",
                            "blocked": True,
                            "reason": "approval_context_changed",
                            "message": (
                                "Workflow 'authenticated-brief' changed its trust boundary after this run. "
                                "Start a fresh run instead of replaying or resuming."
                            ),
                            "changed_fields": ["authenticated_source", "source_systems"],
                        },
                        "risk_level": "medium",
                        "execution_boundaries": ["authenticated_external_source", "workspace_write"],
                        "pending_approval_count": 0,
                        "resume_from_step": "save",
                        "resume_checkpoint_label": "Save step",
                        "run_identity": "session-1:workflow_authenticated_brief:auth-brief",
                        "run_fingerprint": "auth-brief",
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
                        "root_run_identity": "session-1:workflow_authenticated_brief:auth-brief",
                        "resume_plan": {
                            "resume_from_step": "save",
                            "draft": "Retry workflow \"authenticated-brief\" from step \"save\".",
                        },
                        "availability": "ready",
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
    assert workflow_item["continue_message"].startswith("Workflow 'authenticated-brief' changed its trust boundary")
    assert workflow_item["replay_draft"] is None
    assert workflow_item["replay_allowed"] is False
    assert workflow_item["recommended_actions"] == []
    assert workflow_item["metadata"]["resume_from_step"] is None
    assert workflow_item["metadata"]["resume_checkpoint_label"] is None
    assert workflow_item["metadata"]["checkpoint_candidates"] == []
    assert workflow_item["metadata"]["resume_plan"] is None
    assert workflow_item["metadata"]["trust_boundary"]["status"] == "changed"
    assert workflow_item["metadata"]["trust_boundary"]["reason"] == "approval_context_changed"
