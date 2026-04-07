from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso_offset(*, hours: int = 0, minutes: int = 0, seconds: int = 0) -> str:
    return (NOW + timedelta(hours=hours, minutes=minutes, seconds=seconds)).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _default_empty_continuity_snapshot():
    with patch(
        "src.api.activity.build_observer_continuity_snapshot",
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
async def test_activity_ledger_aggregates_llm_calls_budget_and_threaded_actions(client):
    with (
        patch(
            "src.api.activity._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-1",
                        "workflow_name": "web-brief-to-file",
                        "summary": "Workflow resumed after approval",
                        "status": "succeeded",
                        "started_at": _iso_offset(minutes=-8),
                        "updated_at": _iso_offset(minutes=-6),
                        "thread_id": "session-1",
                        "thread_label": "Research thread",
                        "thread_continue_message": "Continue from this workflow run.",
                        "replay_draft": "Replay workflow",
                        "replay_allowed": True,
                        "replay_block_reason": None,
                        "replay_recommended_actions": [],
                        "risk_level": "medium",
                        "execution_boundaries": ["workspace_write"],
                        "pending_approval_count": 0,
                        "resume_from_step": "save",
                        "resume_checkpoint_label": "Save step",
                        "run_identity": "session-1:workflow_web_brief_to_file:run-1",
                        "run_fingerprint": "web-brief",
                        "continued_error_steps": [],
                        "failed_step_tool": None,
                        "checkpoint_candidates": [],
                        "branch_kind": "rerun",
                        "resume_plan": None,
                        "availability": "ready",
                        "step_records": [{"id": "save", "tool": "write_file", "status": "succeeded"}],
                    }
                ]
            ),
        ),
        patch(
            "src.api.activity.approval_repository.list_pending",
            AsyncMock(
                return_value=[
                    {
                        "id": "approval-1",
                        "tool_name": "write_file",
                        "summary": "Approve workspace write",
                        "created_at": _iso_offset(minutes=-7),
                        "session_id": "session-1",
                        "thread_id": "session-1",
                        "thread_label": "Research thread",
                        "resume_message": "Resume after approval.",
                        "risk_level": "high",
                    }
                ]
            ),
        ),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-agent-1",
                        "event_type": "agent_run_succeeded",
                        "tool_name": "chat_agent",
                        "summary": "Websocket chat agent run succeeded",
                        "created_at": _iso_offset(minutes=-5),
                        "session_id": "session-1",
                        "details": {"request_id": "agent-ws:session-1:123", "duration_ms": 820},
                    },
                    {
                        "id": "audit-routing-1",
                        "event_type": "llm_routing_decision",
                        "tool_name": "llm_runtime",
                        "summary": "Selected claude-sonnet-4 for websocket chat",
                        "created_at": _iso_offset(minutes=-5, seconds=-30),
                        "session_id": "session-1",
                        "details": {
                            "request_id": "agent-ws:session-1:123",
                            "selected_model": "openrouter/anthropic/claude-sonnet-4",
                            "runtime_path": "chat_agent",
                        },
                    },
                ]
            ),
        ),
        patch(
            "src.api.activity.list_recent_llm_calls",
            return_value=[
                {
                    "timestamp": _iso_offset(minutes=-5, seconds=-40).replace("Z", "+00:00"),
                    "status": "success",
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "provider": "openrouter",
                    "tokens": {"input": 1000, "output": 250, "total": 1250},
                    "cost_usd": 0.0123,
                    "latency_ms": 812.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:123",
                    "actor": "user_request",
                    "source": "websocket_chat",
                }
            ],
        ),
        patch(
            "src.api.activity.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Research thread"}]),
        ),
    ):
        response = await client.get("/api/activity/ledger", params={"session_id": "session-1", "limit": 20})

    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]

    assert payload["summary"]["llm_call_count"] == 1
    assert payload["summary"]["llm_cost_usd"] == pytest.approx(0.0123)
    assert payload["summary"]["user_triggered_llm_calls"] == 1
    assert payload["summary"]["categories"]["workflow"] == 1
    assert payload["summary"]["categories"]["approval"] == 1
    assert payload["summary"]["llm_cost_by_runtime_path"] == [
        {
            "key": "chat_agent",
            "calls": 1,
            "cost_usd": pytest.approx(0.0123),
            "input_tokens": 1000,
            "output_tokens": 250,
        }
    ]
    assert payload["summary"]["llm_cost_by_capability_family"] == [
        {
            "key": "conversation",
            "calls": 1,
            "cost_usd": pytest.approx(0.0123),
            "input_tokens": 1000,
            "output_tokens": 250,
        }
    ]

    llm_item = next(item for item in items if item["kind"] == "llm_call")
    assert llm_item["thread_label"] == "Research thread"
    assert llm_item["summary"] == "Websocket chat agent run succeeded"
    assert llm_item["prompt_tokens"] == 1000
    assert llm_item["completion_tokens"] == 250
    assert llm_item["cost_usd"] == pytest.approx(0.0123)
    assert llm_item["metadata"]["runtime_path"] == "chat_agent"
    assert llm_item["metadata"]["capability_family"] == "conversation"
    assert llm_item["metadata"]["max_budget_class"] is None

    workflow_item = next(item for item in items if item["kind"] == "workflow_run")
    assert workflow_item["continue_message"] == "Continue from this workflow run."


@pytest.mark.asyncio
async def test_activity_ledger_hides_stale_resume_surface_when_workflow_boundary_is_blocked(client):
    with (
        patch(
            "src.api.activity._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-2",
                        "workflow_name": "authenticated-brief",
                        "summary": "Authenticated workflow boundary drifted.",
                        "status": "failed",
                        "started_at": _iso_offset(minutes=-8),
                        "updated_at": _iso_offset(minutes=-6),
                        "thread_id": "session-1",
                        "thread_label": "Research thread",
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
                        "risk_level": "medium",
                        "execution_boundaries": ["authenticated_external_source", "workspace_write"],
                        "pending_approval_count": 0,
                        "resume_from_step": "save",
                        "resume_checkpoint_label": "Save step",
                        "run_identity": "session-1:workflow_authenticated_brief:run-2",
                        "run_fingerprint": "authenticated-brief",
                        "continued_error_steps": ["save"],
                        "failed_step_tool": "write_file",
                        "checkpoint_candidates": [
                            {
                                "step_id": "save",
                                "label": "save (write_file)",
                                "kind": "retry_failed_step",
                                "status": "continued_error",
                            },
                        ],
                        "branch_kind": "retry_failed_step",
                        "resume_plan": {
                            "resume_from_step": "save",
                            "draft": "Retry workflow \"authenticated-brief\" from step \"save\".",
                        },
                        "availability": "ready",
                        "step_records": [{"id": "save", "tool": "write_file", "status": "continued_error"}],
                    }
                ]
            ),
        ),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch("src.api.activity.audit_repository.list_events", AsyncMock(return_value=[])),
        patch("src.api.activity.list_recent_llm_calls", return_value=[]),
        patch(
            "src.api.activity.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Research thread"}]),
        ),
    ):
        response = await client.get("/api/activity/ledger", params={"session_id": "session-1", "limit": 20})

    assert response.status_code == 200
    payload = response.json()
    workflow_item = next(item for item in payload["items"] if item["kind"] == "workflow_run")
    assert workflow_item["continue_message"].startswith("Workflow 'authenticated-brief' changed its trust boundary")
    assert workflow_item["replay_draft"] is None
    assert workflow_item["replay_allowed"] is False
    assert workflow_item["recommended_actions"] == []
    assert workflow_item["metadata"]["resume_from_step"] is None
    assert workflow_item["metadata"]["resume_checkpoint_label"] is None
    assert workflow_item["metadata"]["checkpoint_candidates"] == []
    assert workflow_item["metadata"]["resume_plan"] is None


@pytest.mark.asyncio
async def test_activity_ledger_respects_window_and_classifies_background_llm_calls(client):
    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-background-1",
                        "event_type": "background_task_succeeded",
                        "tool_name": "session_title_generation",
                        "summary": "Background task session_title_generation succeeded",
                        "created_at": _iso_offset(hours=-2),
                        "session_id": None,
                        "details": {"duration_ms": 120},
                    }
                ]
            ),
        ),
        patch(
            "src.api.activity.list_recent_llm_calls",
            return_value=[
                {
                    "timestamp": _iso_offset(hours=-30).replace("Z", "+00:00"),
                    "status": "success",
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "provider": "openrouter",
                    "tokens": {"input": 100, "output": 20, "total": 120},
                    "cost_usd": 0.001,
                    "latency_ms": 400.0,
                    "session_id": None,
                    "request_id": "strategist_tick:123",
                    "actor": "autonomous",
                    "source": "strategist_tick",
                }
            ],
        ),
        patch("src.api.activity.session_manager.list_sessions", AsyncMock(return_value=[])),
    ):
        response = await client.get("/api/activity/ledger", params={"limit": 20, "window_hours": 24})

    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]

    assert len(items) == 1
    assert items[0]["kind"] == "background_task"
    assert payload["summary"]["llm_call_count"] == 0
    assert payload["summary"]["autonomous_llm_calls"] == 0


@pytest.mark.asyncio
async def test_activity_ledger_summary_counts_full_window_beyond_visible_limit(client):
    llm_entries = [
        {
            "timestamp": _iso_offset(minutes=-(index + 1)).replace("Z", "+00:00"),
            "status": "success",
            "model": "openrouter/anthropic/claude-sonnet-4",
            "provider": "openrouter",
            "tokens": {"input": 10 + index, "output": 5, "total": 15 + index},
            "cost_usd": 0.001,
            "latency_ms": 100.0 + index,
            "session_id": "session-1",
            "request_id": f"agent-ws:session-1:{index}",
            "actor": "user_request",
            "source": "websocket_chat",
        }
        for index in range(6)
    ]
    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch("src.api.activity.audit_repository.list_events", AsyncMock(return_value=[])),
        patch("src.api.activity.list_recent_llm_calls", return_value=llm_entries),
        patch(
            "src.api.activity.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Research thread"}]),
        ),
    ):
        response = await client.get("/api/activity/ledger", params={"session_id": "session-1", "limit": 3})

    assert response.status_code == 200
    payload = response.json()

    assert len(payload["items"]) == 3
    assert payload["summary"]["visible_items"] == 3
    assert payload["summary"]["total_items"] == 6
    assert payload["summary"]["llm_call_count"] == 6
    assert payload["summary"]["llm_cost_usd"] == pytest.approx(0.006)
    assert payload["summary"]["user_triggered_llm_calls"] == 6


@pytest.mark.asyncio
async def test_activity_ledger_surfaces_observer_recovery_actions(client):
    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch("src.api.activity.audit_repository.list_events", AsyncMock(return_value=[])),
        patch("src.api.activity.list_recent_llm_calls", return_value=[]),
        patch(
            "src.api.activity.build_observer_continuity_snapshot",
            AsyncMock(
                return_value={
                    "daemon": {"last_post": NOW.timestamp()},
                    "summary": {
                        "continuity_health": "attention",
                        "primary_surface": "source_adapter",
                        "recommended_focus": "github-managed",
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
        patch("src.api.activity.session_manager.list_sessions", AsyncMock(return_value=[])),
    ):
        response = await client.get("/api/activity/ledger", params={"limit": 20, "window_hours": 24})

    assert response.status_code == 200
    payload = response.json()
    adapter_item = next(item for item in payload["items"] if item["id"] == "continuity:adapter:github-managed")
    assert adapter_item["kind"] == "reach_recovery"
    assert adapter_item["category"] == "system"
    assert adapter_item["source"] == "continuity"
    assert adapter_item["continue_message"] == "Draft a repair plan for github-managed."
    assert adapter_item["metadata"]["kind"] == "source_adapter_repair"
    assert adapter_item["metadata"]["recommended_focus"] == "github-managed"

    imported_item = next(item for item in payload["items"] if item["id"] == "continuity:imported:messaging")
    assert imported_item["metadata"]["kind"] == "imported_reach_attention"
    assert imported_item["metadata"]["surface"] == "imported_reach"


@pytest.mark.asyncio
async def test_activity_ledger_dedupes_live_pending_approvals_and_skips_foreign_sessionless_queue_items(client):
    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.approval_repository.list_pending",
            AsyncMock(
                return_value=[
                    {
                        "id": "approval-1",
                        "tool_name": "write_file",
                        "summary": "Approve workspace write",
                        "created_at": _iso_offset(minutes=-9),
                        "session_id": "session-1",
                        "thread_id": "session-1",
                        "thread_label": "Research thread",
                        "resume_message": "Resume after approval.",
                        "risk_level": "high",
                    }
                ]
            ),
        ),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.insight_queue.peek_all",
            AsyncMock(
                return_value=[
                    type(
                        "Insight",
                        (),
                        {
                            "id": "queued-1",
                            "created_at": _iso_offset(minutes=-5),
                            "session_id": None,
                            "intervention_id": "intervention-foreign",
                            "intervention_type": "advisory",
                            "content": "Foreign queue item",
                            "urgency": 2,
                            "reasoning": "background",
                        },
                    )(),
                ]
            ),
        ),
        patch(
            "src.api.activity.guardian_feedback_repository.list_recent",
            AsyncMock(
                return_value=[
                    type(
                        "Intervention",
                        (),
                        {
                            "id": "intervention-foreign",
                            "updated_at": _iso_offset(minutes=-6),
                            "session_id": "session-2",
                            "intervention_type": "advisory",
                            "content_excerpt": "Foreign intervention",
                            "latest_outcome": "deferred",
                            "policy_action": "defer",
                            "policy_reason": "interrupt_cost_high",
                            "transport": "browser",
                            "feedback_type": None,
                        },
                    )(),
                ]
            ),
        ),
        patch(
            "src.api.activity.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-approval-1",
                        "event_type": "approval_requested",
                        "tool_name": "write_file",
                        "summary": "Approval requested for workspace write",
                        "created_at": _iso_offset(minutes=-9),
                        "session_id": "session-1",
                        "details": {"approval_id": "approval-1"},
                    }
                ]
            ),
        ),
        patch("src.api.activity.list_recent_llm_calls", return_value=[]),
        patch(
            "src.api.activity.session_manager.list_sessions",
            AsyncMock(
                return_value=[
                    {"id": "session-1", "title": "Research thread"},
                    {"id": "session-2", "title": "Foreign thread"},
                ]
            ),
        ),
    ):
        response = await client.get("/api/activity/ledger", params={"session_id": "session-1", "limit": 20})

    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]

    assert [item["kind"] for item in items] == ["approval"]
    assert payload["summary"]["pending_approvals"] == 1
    assert payload["summary"]["total_items"] == 1


@pytest.mark.asyncio
async def test_activity_ledger_groups_request_scoped_tool_and_llm_events(client):
    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-tool-call-1",
                        "event_type": "tool_call",
                        "tool_name": "web_search",
                        "summary": "Search for hinge specs",
                        "created_at": _iso_offset(minutes=-4),
                        "session_id": "session-1",
                        "details": {"request_id": "agent-ws:session-1:123", "arguments": {"query": "hinge specs"}},
                    },
                    {
                        "id": "audit-tool-result-1",
                        "event_type": "tool_result",
                        "tool_name": "web_search",
                        "summary": "Found hinge specs",
                        "created_at": _iso_offset(minutes=-4, seconds=2),
                        "session_id": "session-1",
                        "details": {"request_id": "agent-ws:session-1:123", "result_summary": "Found hinge specs"},
                    },
                ]
            ),
        ),
        patch(
            "src.api.activity.list_recent_llm_calls",
            return_value=[
                {
                    "timestamp": _iso_offset(minutes=-3, seconds=-40).replace("Z", "+00:00"),
                    "status": "success",
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "provider": "openrouter",
                    "tokens": {"input": 1000, "output": 250, "total": 1250},
                    "cost_usd": 0.0123,
                    "latency_ms": 812.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:123",
                    "actor": "user_request",
                    "source": "websocket_chat",
                }
            ],
        ),
        patch(
            "src.api.activity.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Research thread"}]),
        ),
    ):
        response = await client.get("/api/activity/ledger", params={"session_id": "session-1", "limit": 20})

    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]
    group_keys = {item["kind"]: item["group_key"] for item in items}

    assert group_keys["llm_call"] == "request:agent-ws:session-1:123"
    assert group_keys["tool_call"] == "request:agent-ws:session-1:123"
    assert group_keys["tool_result"] == "request:agent-ws:session-1:123"


@pytest.mark.asyncio
async def test_activity_ledger_attributes_llm_cost_to_runtime_and_capability_family(client):
    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-routing-chat-1",
                        "event_type": "llm_routing_decision",
                        "tool_name": "llm_runtime",
                        "summary": "Initial routing candidate",
                        "created_at": _iso_offset(minutes=-7),
                        "session_id": "session-1",
                        "details": {
                            "request_id": "agent-ws:session-1:chat",
                            "runtime_path": "session_runtime",
                            "selected_source": "primary",
                            "max_budget_class": "low",
                        },
                    },
                    {
                        "id": "audit-routing-chat-2",
                        "event_type": "llm_routing_decision",
                        "tool_name": "llm_runtime",
                        "summary": "Selected claude-sonnet-4 for websocket chat",
                        "created_at": _iso_offset(minutes=-6),
                        "session_id": "session-1",
                        "details": {
                            "request_id": "agent-ws:session-1:chat",
                            "runtime_path": "chat_agent",
                            "selected_source": "primary",
                            "max_budget_class": "medium",
                            "budget_steering_mode": "prefer_lower_budget",
                            "selected_route_score": 9.5,
                            "selected_budget_preference_score": 1.0,
                        },
                    },
                    {
                        "id": "audit-routing-browser-1",
                        "event_type": "llm_routing_decision",
                        "tool_name": "llm_runtime",
                        "summary": "Selected grok-4.1-fast for browser tool",
                        "created_at": _iso_offset(minutes=-5),
                        "session_id": "session-1",
                        "details": {
                            "request_id": "agent-ws:session-1:browser",
                            "runtime_path": "browser_agent",
                            "selected_source": "browser_provider",
                            "max_budget_class": "high",
                        },
                    },
                ]
            ),
        ),
        patch(
            "src.api.activity.list_recent_llm_calls",
            return_value=[
                {
                    "timestamp": _iso_offset(minutes=-6, seconds=10).replace("Z", "+00:00"),
                    "status": "success",
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "provider": "openrouter",
                    "tokens": {"input": 500, "output": 150, "total": 650},
                    "cost_usd": 0.01,
                    "latency_ms": 610.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:chat",
                    "actor": "user_request",
                    "source": "websocket_chat",
                },
                {
                    "timestamp": _iso_offset(minutes=-5, seconds=10).replace("Z", "+00:00"),
                    "status": "success",
                    "model": "openrouter/x-ai/grok-4.1-fast",
                    "provider": "openrouter",
                    "tokens": {"input": 250, "output": 60, "total": 310},
                    "cost_usd": 0.025,
                    "latency_ms": 880.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:browser",
                    "actor": "user_request",
                    "source": "websocket_chat",
                },
            ],
        ),
        patch(
            "src.api.activity.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Research thread"}]),
        ),
    ):
        response = await client.get("/api/activity/ledger", params={"session_id": "session-1", "limit": 20})

    assert response.status_code == 200
    payload = response.json()
    summary = payload["summary"]
    llm_items = [item for item in payload["items"] if item["kind"] == "llm_call"]
    by_request = {
        item["metadata"]["request_id"]: item["metadata"]
        for item in llm_items
    }

    assert summary["llm_cost_by_runtime_path"] == [
        {
            "key": "browser_agent",
            "calls": 1,
            "cost_usd": pytest.approx(0.025),
            "input_tokens": 250,
            "output_tokens": 60,
        },
        {
            "key": "chat_agent",
            "calls": 1,
            "cost_usd": pytest.approx(0.01),
            "input_tokens": 500,
            "output_tokens": 150,
        },
    ]
    assert summary["llm_cost_by_capability_family"] == [
        {
            "key": "browser",
            "calls": 1,
            "cost_usd": pytest.approx(0.025),
            "input_tokens": 250,
            "output_tokens": 60,
        },
        {
            "key": "conversation",
            "calls": 1,
            "cost_usd": pytest.approx(0.01),
            "input_tokens": 500,
            "output_tokens": 150,
        },
    ]
    assert by_request["agent-ws:session-1:chat"]["capability_family"] == "conversation"
    assert by_request["agent-ws:session-1:chat"]["max_budget_class"] == "medium"
    assert by_request["agent-ws:session-1:chat"]["budget_steering_mode"] == "prefer_lower_budget"
    assert by_request["agent-ws:session-1:chat"]["selected_route_score"] == 9.5
    assert by_request["agent-ws:session-1:browser"]["capability_family"] == "browser"
    assert by_request["agent-ws:session-1:browser"]["selected_source"] == "browser_provider"


@pytest.mark.asyncio
async def test_activity_ledger_marks_missing_routing_metadata_as_unattributed(client):
    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch("src.api.activity.audit_repository.list_events", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.list_recent_llm_calls",
            return_value=[
                {
                    "timestamp": _iso_offset(minutes=-6).replace("Z", "+00:00"),
                    "status": "success",
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "provider": "openrouter",
                    "tokens": {"input": 120, "output": 30, "total": 150},
                    "cost_usd": 0.0042,
                    "latency_ms": 420.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:unknown",
                    "actor": "user_request",
                    "source": "websocket_chat",
                }
            ],
        ),
        patch(
            "src.api.activity.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Research thread"}]),
        ),
    ):
        response = await client.get("/api/activity/ledger", params={"session_id": "session-1", "limit": 20})

    assert response.status_code == 200
    payload = response.json()
    llm_item = next(item for item in payload["items"] if item["kind"] == "llm_call")

    assert llm_item["metadata"]["runtime_path"] is None
    assert llm_item["metadata"]["capability_family"] == "unattributed"
    assert payload["summary"]["llm_cost_by_capability_family"] == [
        {
            "key": "unattributed",
            "calls": 1,
            "cost_usd": pytest.approx(0.0042),
            "input_tokens": 120,
            "output_tokens": 30,
        }
    ]


@pytest.mark.asyncio
async def test_activity_ledger_limit_keeps_full_request_group_visible(client):
    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-tool-call-1",
                        "event_type": "tool_call",
                        "tool_name": "web_search",
                        "summary": "Search hinge specs",
                        "created_at": _iso_offset(minutes=-4),
                        "session_id": "session-1",
                        "details": {"request_id": "agent-ws:session-1:123"},
                    },
                    {
                        "id": "audit-tool-result-1",
                        "event_type": "tool_result",
                        "tool_name": "web_search",
                        "summary": "Found hinge specs",
                        "created_at": _iso_offset(minutes=-4, seconds=2),
                        "session_id": "session-1",
                        "details": {"request_id": "agent-ws:session-1:123"},
                    },
                ]
            ),
        ),
        patch(
            "src.api.activity.list_recent_llm_calls",
            return_value=[
                {
                    "timestamp": _iso_offset(minutes=-3, seconds=-40).replace("Z", "+00:00"),
                    "status": "success",
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "provider": "openrouter",
                    "tokens": {"input": 1000, "output": 250, "total": 1250},
                    "cost_usd": 0.0123,
                    "latency_ms": 812.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:123",
                    "actor": "user_request",
                    "source": "websocket_chat",
                },
                {
                    "timestamp": _iso_offset(minutes=-10).replace("Z", "+00:00"),
                    "status": "success",
                    "model": "openrouter/anthropic/claude-haiku-4",
                    "provider": "openrouter",
                    "tokens": {"input": 100, "output": 20, "total": 120},
                    "cost_usd": 0.001,
                    "latency_ms": 120.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:older",
                    "actor": "user_request",
                    "source": "websocket_chat",
                },
            ],
        ),
        patch(
            "src.api.activity.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Research thread"}]),
        ),
    ):
        response = await client.get("/api/activity/ledger", params={"session_id": "session-1", "limit": 1})

    assert response.status_code == 200
    payload = response.json()

    assert payload["summary"]["visible_groups"] == 1
    assert len(payload["items"]) == 3
    assert {item["group_key"] for item in payload["items"]} == {"request:agent-ws:session-1:123"}


@pytest.mark.asyncio
async def test_activity_ledger_surfaces_extension_lifecycle_events(client):
    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-extension-install-1",
                        "event_type": "integration_succeeded",
                        "tool_name": "extension:seraph.test-installable",
                        "summary": "Extension seraph.test-installable succeeded",
                        "created_at": _iso_offset(minutes=-3),
                        "session_id": None,
                        "details": {
                            "integration_type": "extension",
                            "name": "seraph.test-installable",
                            "extension_id": "seraph.test-installable",
                            "extension_display_name": "Test Installable",
                            "status": "installed",
                            "action": "install",
                            "kind": "capability-pack",
                            "location": "workspace",
                            "version": "2026.3.21",
                            "issue_count": 0,
                            "load_error_count": 0,
                        },
                    },
                    {
                        "id": "audit-extension-enable-1",
                        "event_type": "integration_failed",
                        "tool_name": "extension:seraph.test-installable",
                        "summary": "Extension seraph.test-installable failed",
                        "created_at": _iso_offset(minutes=-2),
                        "session_id": None,
                        "details": {
                            "integration_type": "extension",
                            "name": "seraph.test-installable",
                            "extension_id": "seraph.test-installable",
                            "extension_display_name": "Test Installable",
                            "status": "enable_failed",
                            "action": "enable",
                            "kind": "capability-pack",
                            "location": "workspace",
                            "version": "2026.3.21",
                            "issue_count": 2,
                            "load_error_count": 1,
                            "error": "extension 'seraph.test-installable' is degraded and cannot be enabled until validation issues are fixed",
                        },
                    },
                ]
            ),
        ),
        patch("src.api.activity.list_recent_llm_calls", return_value=[]),
        patch("src.api.activity.session_manager.list_sessions", AsyncMock(return_value=[])),
    ):
        response = await client.get("/api/activity/ledger", params={"limit": 20})

    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]

    assert [item["kind"] for item in items] == ["extension", "extension"]
    assert items[0]["title"] == "Test Installable"
    assert items[0]["summary"] == "Extension enable failed · capability-pack · workspace · 2 issues · 1 load error"
    assert items[0]["status"] == "failed"
    assert items[0]["source"] == "extension"
    assert items[0]["group_key"].startswith("extension:seraph.test-installable:enable:")
    assert items[1]["summary"] == "Installed extension package · capability-pack · workspace"
    assert payload["summary"]["categories"]["system"] == 2
