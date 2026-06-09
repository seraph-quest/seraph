from datetime import datetime, timedelta, timezone
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
async def test_operator_m6_memory_superiority_surface_delegates_to_memory_payload(client):
    payload = {
        "summary": {
            "operator_status": "m6_memory_superiority_visible",
            "active_memory_count": 3,
            "superseded_memory_count": 1,
            "archived_memory_count": 1,
            "source_receipt_count": 4,
            "control_receipt_count": 2,
            "behavior_receipt_count": 1,
            "privacy_boundary_count": 1,
            "provider_writeback_blocked_count": 1,
            "memory_confidence": "grounded",
            "action_posture": "clarify_first",
            "claim_boundary": "deterministic_operator_memory_control_and_behavior_receipts_not_live_external_memory_parity",
        },
        "behavior_receipts": [
            {
                "id": "guardian-state-memory-influence",
                "changed": True,
                "changed_dimensions": ["recall_context", "action_posture"],
                "action_posture": "clarify_first",
                "intent_resolution": "clarify",
                "memory_confidence": "grounded",
                "evidence": ["relevant_memory_context_present"],
                "diagnostics": ["state=conflict_reconciled"],
                "receipt_contract": "memory_changed_or_explained_guardian_behavior",
            }
        ],
        "memory_records": [],
        "control_receipts": [],
        "privacy_boundaries": ["private"],
        "reconciliation": {},
        "benchmark": {},
        "policy": {},
    }

    with patch(
        "src.api.operator.build_m6_memory_superiority_payload",
        AsyncMock(return_value=payload),
    ) as build_payload:
        resp = await client.get(
            "/api/operator/m6-memory-superiority",
            params={"session_id": "session-1", "query": "Atlas"},
        )

    assert resp.status_code == 200
    assert resp.json()["summary"]["behavior_receipt_count"] == 1
    assert resp.json()["behavior_receipts"][0]["changed_dimensions"] == ["recall_context", "action_posture"]
    build_payload.assert_awaited_once_with(session_id="session-1", query="Atlas")


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
async def test_operator_m7_cockpit_composes_dense_control_surface(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-1",
                        "run_identity": "session-1:workflow_release:root",
                        "root_run_identity": "session-1:workflow_release:root",
                        "workflow_name": "release-check",
                        "summary": "Release check is waiting on a guarded write.",
                        "status": "awaiting_approval",
                        "availability": "blocked",
                        "thread_id": "session-1",
                        "thread_label": "Release",
                        "started_at": "2026-05-05T10:00:00Z",
                        "updated_at": "2026-05-05T10:04:00Z",
                        "artifact_paths": ["artifacts/release-check.md"],
                        "branch_kind": "branch_from_checkpoint",
                        "checkpoint_candidates": [{"step_id": "draft", "status": "succeeded"}],
                        "retry_from_step_draft": "Retry release check from draft.",
                        "replay_allowed": False,
                        "replay_block_reason": "approval_context_changed",
                        "pending_approval_count": 1,
                        "approval_context": {
                            "risk_level": "high",
                            "execution_boundaries": ["workspace_write"],
                            "delegated_specialists": ["workflow_runner"],
                            "delegated_tool_names": ["write_file"],
                            "trust_partition": {"mode": "delegated_specialist"},
                        },
                        "step_records": [
                            {
                                "id": "draft",
                                "index": 0,
                                "tool": "write_file",
                                "status": "awaiting_approval",
                                "is_recoverable": True,
                                "recovery_actions": [{"type": "set_tool_policy"}],
                            }
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
                        "tool_name": "write_file",
                        "summary": "Approve release artifact write.",
                        "risk_level": "high",
                        "thread_id": "session-1",
                        "session_id": "session-1",
                        "created_at": "2026-05-05T10:03:00Z",
                        "resume_message": "Resume the release check after approval.",
                        "approval_context": {
                            "risk_level": "high",
                            "execution_boundaries": ["workspace_write"],
                            "trust_partition": {"mode": "operator_approved_write"},
                        },
                        "approval_scope": {
                            "target": {
                                "type": "artifact",
                                "reference": "artifacts/release-check.md",
                            }
                        },
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
                        "recommended_focus": "release channel",
                        "degraded_route_count": 1,
                        "attention_presence_surface_count": 1,
                    },
                    "recovery_actions": [
                        {
                            "id": "presence:release",
                            "kind": "presence_repair",
                            "label": "Repair release channel",
                            "detail": "Channel requires operator review.",
                            "status": "requires_config",
                            "continue_message": "Plan release channel repair.",
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
                        "event_type": "tool_failed",
                        "tool_name": "write_file",
                        "summary": "write_file failed before approval.",
                        "created_at": "2026-05-05T10:02:00Z",
                        "session_id": "session-1",
                        "risk_level": "high",
                    },
                    {
                        "id": "audit-2",
                        "event_type": "llm_routing_decision",
                        "tool_name": "runtime",
                        "summary": "Selected guarded runtime.",
                        "created_at": "2026-05-05T10:01:00Z",
                        "session_id": "session-1",
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.scheduled_job_repository.list_jobs",
            AsyncMock(
                return_value=[
                    {
                        "id": "job-release",
                        "name": "Release routine",
                        "enabled": False,
                        "trigger_type": "cron",
                        "trigger_spec": {"cron": "0 9 * * *", "timezone": "UTC"},
                        "action_type": "run_workflow",
                        "action_spec": {"workflow_name": "release-check"},
                        "session_id": "session-1",
                    }
                ]
            ),
        ),
        patch(
            "src.api.operator.scheduled_job_repository.list_run_history",
            AsyncMock(
                return_value=[
                    {
                        "id": "job-run-1",
                        "scheduled_job_id": "job-release",
                        "job_name": "Release routine",
                        "trigger_type": "cron",
                        "action_type": "run_workflow",
                        "status": "skipped",
                        "outcome": "skipped_disabled",
                        "started_at": "2026-05-05T09:00:00Z",
                    }
                ]
            ),
        ),
        patch(
            "src.api.operator.build_m6_memory_superiority_payload",
            AsyncMock(
                return_value={
                    "summary": {"operator_status": "m6_memory_superiority_visible"},
                    "behavior_receipts": [{"id": "behavior-1", "changed": True}],
                    "memory_records": [{"id": "memory-1", "summary": "Release preference"}],
                    "control_receipts": [{"id": "control-1", "action": "audit"}],
                }
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Release"}]),
        ),
        patch(
            "src.api.operator.process_runtime_manager.list_all_processes",
            return_value=[
                {
                    "process_id": "proc-1",
                    "pid": 123,
                    "command": "pytest",
                    "status": "running",
                    "session_id": "session-1",
                    "started_at": "2026-05-05T10:00:00Z",
                    "session_scoped": True,
                    "worker_disposable": True,
                    "trust_partition": "session",
                }
            ],
        ),
    ):
        resp = await client.get("/api/operator/m7-cockpit", params={"session_id": "session-1"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "m7_operator_cockpit_visible"
    assert payload["summary"]["pending_approval_count"] == 1
    assert payload["summary"]["trust_boundary_count"] >= 3
    assert payload["summary"]["memory_evidence_count"] == 3
    assert payload["summary"]["tool_call_count"] == 2
    assert payload["summary"]["artifact_count"] == 1
    assert payload["summary"]["job_count"] == 1
    assert payload["summary"]["background_session_count"] == 1
    assert payload["summary"]["efficiency_task_count"] == 11
    assert payload["summary"]["efficiency_action_budget"] == 33
    assert payload["summary"]["efficiency_time_budget_seconds"] == 195
    assert payload["active_work"][0]["approval_required"] is True
    active_controls = {control["action"]: control for control in payload["active_work"][0]["controls"]}
    assert active_controls["approve"]["enabled"] is False
    assert active_controls["approve"]["label"] == "Approve from approvals"
    assert active_controls["approve"]["target_kind"] == "approval_lookup"
    assert active_controls["approve"]["control_mode"] == "operator_draft_control"
    assert active_controls["deny"]["control_mode"] == "operator_draft_control"
    assert active_controls["repair"]["control_mode"] == "routed_or_policy_gated_control"
    assert active_controls["branch"]["control_mode"] == "operator_draft_control"
    assert payload["approvals"][0]["controls"][0]["action"] == "approve"
    assert payload["approvals"][0]["controls"][0]["control_mode"] == "direct_backend_control"
    assert payload["trust_boundaries"][0]["claim_boundary"] == "workflow_trust_boundary_receipt"
    assert payload["memory_evidence"]["claim_boundary"] == "guardian_memory_evidence_receipts_no_secret_values"
    assert payload["tool_calls"][0]["event_type"] == "tool_failed"
    assert payload["artifacts"][0]["path"] == "artifacts/release-check.md"
    assert payload["jobs"][0]["status"] == "paused"
    assert payload["channels_and_recovery"]["summary"]["recovery_action_count"] == 1
    fast_controls = {control["action"]: control for control in payload["fast_controls"]}
    assert list(fast_controls) == ["approve", "deny", "pause", "resume", "retry", "repair", "branch", "compare", "revoke"]
    assert fast_controls["approve"]["enabled"] is True
    assert fast_controls["approve"]["target_kind"] == "approval"
    assert fast_controls["approve"]["control_mode"] == "direct_backend_control"
    assert fast_controls["deny"]["enabled"] is True
    assert fast_controls["deny"]["control_mode"] == "direct_backend_control"
    assert fast_controls["pause"]["target_kind"] == "scheduled_job"
    assert fast_controls["pause"]["control_mode"] == "routed_or_policy_gated_control"
    assert fast_controls["resume"]["target_kind"] == "scheduled_job"
    assert fast_controls["resume"]["control_mode"] == "routed_or_policy_gated_control"
    assert fast_controls["retry"]["control_mode"] == "routed_or_policy_gated_control"
    assert fast_controls["repair"]["enabled"] is True
    assert fast_controls["repair"]["control_mode"] == "routed_or_policy_gated_control"
    assert fast_controls["branch"]["enabled"] is True
    assert fast_controls["branch"]["target_kind"] == "workflow_run"
    assert fast_controls["branch"]["control_mode"] == "operator_draft_control"
    assert fast_controls["compare"]["enabled"] is True
    assert fast_controls["compare"]["control_mode"] == "operator_draft_control"
    assert fast_controls["revoke"]["enabled"] is False
    assert fast_controls["revoke"]["target_kind"] == "connector_or_channel"
    assert fast_controls["revoke"]["control_mode"] == "operator_draft_control"
    assert payload["operator_efficiency"]["benchmark_surface"] == "/api/operator/cockpit-efficiency-benchmark"
    assert payload["operator_efficiency"]["scorecard"]["confidence_measurement_boundary"] == (
        "confidence_affordance_proxy_not_operator_reported_confidence"
    )
    assert "/api/operator/m7-cockpit" in payload["proof_receipts"]
    assert "/api/operator/cockpit-efficiency-benchmark" in payload["proof_receipts"]
    assert "automatic_control_execution_from_cockpit_payload" in payload["claim_boundaries"]["not_claimed"]


@pytest.mark.asyncio
async def test_operator_benchmark_proof_surfaces_suite_coverage_and_evolution_gates(client):
    with patch(
        "src.api.operator.list_evolution_targets",
        return_value=[
            {"target_type": "skill", "source_path": "/tmp/skills/web-briefing.md"},
            {"target_type": "prompt_pack", "source_path": "/tmp/extensions/review-pack/prompts/review.md"},
        ],
    ), patch(
        "src.api.operator.build_workflow_endurance_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "workflow_endurance_and_repair",
                    "benchmark_posture": "ci_gated_operator_visible",
                    "operator_status": "workflow_orchestration_visible",
                    "scenario_count": 4,
                    "dimension_count": 5,
                    "failure_mode_count": 5,
                    "active_failure_count": 0,
                    "anticipatory_repair_state": "checkpoint_and_pre_repair_visible",
                    "condensation_fidelity_state": "recovery_paths_and_output_history_retained",
                    "branch_continuity_state": "backup_branch_operator_selectable",
                },
                "scenario_names": [
                    "workflow_anticipatory_repair_behavior",
                    "workflow_condensation_fidelity_behavior",
                    "workflow_backup_branch_surface_behavior",
                    "workflow_multi_session_endurance_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "backup_branch_policy": "checkpoint_backed_branch_receipts_must_remain_operator_selectable",
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 4, "passed": 4, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_live_workflow_endurance_canary_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "live_workflow_endurance_canary",
                    "benchmark_posture": "live_workflow_canary_ci_gated_operator_visible",
                    "operator_status": "live_workflow_canary_visible",
                    "scenario_count": 4,
                    "session_count": 2,
                    "run_count": 5,
                    "branch_run_count": 3,
                    "checkpoint_count": 4,
                    "failure_injection_count": 1,
                    "recovery_action_count": 1,
                    "artifact_receipt_count": 4,
                    "approval_preservation_count": 2,
                    "trust_boundary_block_count": 1,
                    "audit_receipt_count": 10,
                    "active_failure_count": 0,
                    "claim_boundary": "audit_projected_replayable_canary_not_durable_workflow_engine",
                },
                "scenario_names": [
                    "live_workflow_canary_protocol_behavior",
                    "live_workflow_canary_failure_recovery_behavior",
                    "live_workflow_canary_approval_preservation_behavior",
                    "operator_live_workflow_canary_surface_behavior",
                ],
                "protocol": {"replay_command": "uv run python -m src.evals.harness --benchmark-suite live_workflow_endurance_canary --indent 0"},
                "policy": {
                    "claim_boundary": "audit_projected_replayable_canary_not_durable_workflow_engine",
                    "receipt_surfaces": [
                        "/api/operator/live-workflow-endurance-canary",
                        "/api/operator/workflow-orchestration",
                        "/api/operator/benchmark-proof",
                    ],
                    "not_claimed": ["durable_workflow_state_machine"],
                },
                "sessions": [],
                "runs": [],
                "operator_story": {
                    "multi_session_visible": True,
                    "delegated_owner_visible": True,
                    "checkpoint_branch_visible": True,
                    "failure_recovery_visible": True,
                    "artifact_comparison_visible": True,
                    "approval_preservation_visible": True,
                    "trust_boundary_fail_closed_visible": True,
                    "audit_trail_visible": True,
                },
                "failure_report": [],
                "latest_run": {"total": 4, "passed": 4, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_live_replay_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "live_long_horizon_eval_replay_v1",
                    "benchmark_posture": "live_replay_ci_gated_operator_visible",
                    "operator_status": "live_replay_receipts_visible",
                    "scenario_count": 5,
                    "dimension_count": 5,
                    "failure_mode_count": 6,
                    "active_failure_count": 0,
                    "fixture_state": "time_stable_fake_provider_replays",
                    "coverage_state": "memory_workflow_reach_security_cockpit_covered",
                    "taxonomy_state": "surface_failure_recovery_claim_boundary_visible",
                    "operator_receipt_state": "benchmark_activity_workflow_guardian_receipts_visible",
                    "claim_boundary": "deterministic_liveish_replay_proof_not_live_human_outcome_or_provider_attestation",
                },
                "scenario_names": [
                    "live_replay_fixture_contract_behavior",
                    "live_replay_cross_surface_failure_taxonomy_behavior",
                    "live_replay_surface_coverage_behavior",
                    "live_replay_operator_receipt_behavior",
                    "operator_live_replay_benchmark_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "replay_fixtures": [],
                "failure_report": [],
                "policy": {
                    "fixture_policy": "fake_providers_and_explicit_time_anchors_required",
                    "coverage_policy": "memory_workflow_reach_security_and_cockpit_surfaces_must_all_have_replay_receipts",
                    "failure_taxonomy_policy": "surface_failure_recovery_and_claim_boundary_must_be_operator_visible",
                    "claim_boundary": "deterministic_liveish_replay_proof_not_live_human_outcome_or_provider_attestation",
                    "receipt_surfaces": [
                        "/api/operator/benchmark-proof",
                        "/api/operator/live-long-horizon-replay-benchmark",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 5, "passed": 5, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_trust_boundary_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "trust_boundary_and_safety_receipts",
                    "benchmark_posture": "ci_gated_operator_visible",
                    "operator_status": "safety_receipts_visible",
                    "scenario_count": 7,
                    "dimension_count": 5,
                    "failure_mode_count": 6,
                    "active_failure_count": 0,
                    "secret_egress_state": "field_scoped_egress_allowlist_required",
                    "delegation_partition_state": "vault_and_background_partitioned",
                    "workflow_replay_state": "boundary_drift_blocks_replay",
                    "operator_receipt_state": "benchmark_and_runtime_visible",
                },
                "scenario_names": [
                    "secret_ref_egress_boundary_behavior",
                    "tool_policy_guardrails_behavior",
                    "delegation_secret_boundary_behavior",
                    "process_recovery_boundary_behavior",
                    "background_session_handoff_behavior",
                    "workflow_boundary_blocked_surface_behavior",
                    "source_mutation_boundary_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "secret_egress_policy": "field_scoped_secret_refs_plus_required_credential_egress_allowlist",
                    "operator_visibility": "benchmark_proof_plus_runtime_receipts_visible",
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 7, "passed": 7, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_m5_operating_layer_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "m5_jobs_routines_workflows_delegation",
                    "benchmark_posture": "m5_ci_gated_operator_visible",
                    "operator_status": "m5_operating_layer_visible",
                    "scenario_count": 5,
                    "dimension_count": 5,
                    "failure_mode_count": 5,
                    "active_failure_count": 0,
                    "scheduled_job_run_history_state": "durable_per_run_receipts_visible",
                    "pause_resume_state": "disabled_jobs_skip_without_firing",
                    "workflow_projection_state": "audit_projected_claim_boundary_visible",
                    "delegation_partition_state": "trust_receipts_operator_visible",
                },
                "scenario_names": [
                    "m5_operating_layer_payload_behavior",
                    "scheduled_job_run_history_behavior",
                    "scheduled_job_pause_resume_no_fire_behavior",
                    "delegation_trust_partition_receipt_behavior",
                    "operator_m5_benchmark_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "workflow_projection_policy": "workflow_runs_are_audit_projected_until_a_durable_executor_exists",
                    "ci_gate_mode": "required_benchmark_suite",
                    "receipt_surfaces": [
                        "/api/operator/m5-operating-layer",
                        "/api/operator/m5-operating-layer-benchmark",
                        "/api/operator/benchmark-proof",
                    ],
                },
                "latest_run": {"total": 5, "passed": 5, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_secure_capability_host_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "secure_capability_host",
                    "benchmark_posture": "secure_host_ci_gated_operator_visible",
                    "operator_status": "secure_capability_host_receipts_visible",
                    "scenario_count": 13,
                    "dimension_count": 9,
                    "failure_mode_count": 9,
                    "active_failure_count": 0,
                    "host_isolation_state": "deterministic_choke_points_claim_bounded",
                    "credential_egress_state": "session_field_host_allowlist_enforced",
                    "workspace_secret_file_state": "generic_read_patch_blocked",
                    "workspace_escape_state": "workspace_relative_paths_enforced",
                    "process_environment_state": "ambient_secret_env_scrubbed",
                    "browser_cookie_session_state": "per_run_context_no_storage_state_receipts",
                    "prompt_surface_state": "suspicious_context_quarantined",
                    "delegation_provider_state": "trust_partition_receipts_visible",
                    "hostile_provider_replay_state": "trust_expanding_replay_blocked",
                    "capability_trust_matrix_state": "owner_boundary_credential_mutation_receipts_visible",
                    "receipt_surface_completeness_state": "required_secure_host_surfaces_visible",
                },
                "scenario_names": [
                    "secure_host_secret_ref_fail_closed_behavior",
                    "secure_host_isolation_strategy_report_behavior",
                    "secure_host_browser_cookie_session_partition_behavior",
                    "secure_host_workspace_secret_path_boundary_behavior",
                    "secure_host_workspace_escape_boundary_behavior",
                    "secure_host_process_env_isolation_behavior",
                    "secure_host_prompt_injection_quarantine_behavior",
                    "secure_host_delegation_partition_behavior",
                    "secure_host_provider_fallback_boundary_behavior",
                    "secure_host_hostile_provider_replay_behavior",
                    "secure_host_capability_trust_matrix_behavior",
                    "secure_host_receipt_surface_completeness_behavior",
                    "operator_secure_capability_host_benchmark_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "isolation_strategy": {},
                "browser_partition_policy": {},
                "capability_trust_regression_matrix": [],
                "receipt_surface_completeness": {},
                "policy": {
                    "host_isolation_policy": "deterministic_choke_points_with_explicit_non_container_claim_boundary",
                    "credential_egress_policy": "session_bound_field_scoped_destination_host_allowlisted_secret_refs",
                    "workspace_escape_policy": "workspace_relative_paths_and_disposable_worker_roots_must_not_escape",
                    "process_environment_policy": "allowlisted_environment_only_for_foreground_and_background_processes",
                    "browser_cookie_session_policy": "per_run_browser_contexts_without_persisted_cookie_or_storage_state",
                    "hostile_provider_replay_policy": "provider_replay_or_fallback_must_not_expand_trust_class_or_reuse_sensitive_context",
                    "capability_trust_regression_policy": "capability_classes_require_owner_boundary_credential_mutation_audit_receipts",
                    "claim_boundary": "deterministic_secure_host_choke_points_not_full_host_container_isolation",
                    "receipt_surfaces": [
                        "/api/operator/benchmark-proof",
                        "/api/operator/secure-capability-host-benchmark",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 7, "passed": 7, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_computer_use_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "computer_use_browser_desktop",
                    "benchmark_posture": "ci_gated_operator_visible",
                    "operator_status": "browser_desktop_receipts_visible",
                    "scenario_count": 7,
                    "dimension_count": 5,
                    "failure_mode_count": 5,
                    "active_failure_count": 0,
                    "browser_replay_state": "extract_html_and_screenshot_receipts_visible",
                    "desktop_action_state": "dismiss_poll_and_ack_receipts_visible",
                    "cross_surface_receipt_state": "continuity_and_operator_receipts_visible",
                },
                "scenario_names": [
                    "browser_execution_task_replay_behavior",
                    "browser_runtime_audit",
                    "native_desktop_shell_behavior",
                    "desktop_notification_action_replay_behavior",
                    "cross_surface_notification_controls_behavior",
                    "cross_surface_continuity_behavior",
                    "workflow_boundary_blocked_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "browser_task_replay_policy": "extract_html_and_screenshot_actions_require_distinct_audit_receipts",
                    "desktop_action_replay_policy": "enqueue_dismiss_poll_and_ack_must_remain_cross_surface_replayable",
                    "cross_surface_continuity_policy": "browser_and_desktop_share_one_operator_visible_continuity_snapshot",
                    "operator_visibility": "benchmark_proof_plus_computer_use_receipts_visible",
                    "receipt_surfaces": [
                        "/api/operator/benchmark-proof",
                        "/api/operator/computer-use-benchmark",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 7, "passed": 7, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_m2_execution_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "m2_execution_supremacy",
                    "benchmark_posture": "m2_completion_ci_gated_operator_visible",
                    "operator_status": "m2_execution_readiness_visible",
                    "scenario_count": 11,
                    "dimension_count": 5,
                    "failure_mode_count": 6,
                    "active_failure_count": 0,
                    "terminal_process_state": "bounded_with_recovery_receipts",
                    "browser_http_state": "dns_redirect_and_subrequest_guarded",
                    "artifact_registry_state": "stable_ids_hashes_boundaries_and_recovery_hints_visible",
                    "security_gauntlet_state": "m2_435_threats_pinned",
                    "milestone_completion_state": "ready_to_close_m2",
                },
                "scenario_names": [
                    "execution_artifact_registry_behavior",
                    "execution_security_gauntlet_behavior",
                    "filesystem_patch_receipt_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "milestone_contract": "one_milestone_one_ready_pr",
                    "completion_policy": "all_execution_families_and_435_security_gauntlet_must_pass",
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 11, "passed": 11, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_m7_operator_cockpit_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "m7_operator_cockpit_legibility",
                    "benchmark_posture": "m7_ci_gated_operator_visible",
                    "operator_status": "m7_cockpit_legibility_visible",
                    "scenario_count": 4,
                    "dimension_count": 5,
                    "failure_mode_count": 5,
                    "active_failure_count": 0,
                    "receipt_legibility_state": "summary_status_time_and_thread_visible",
                    "fast_control_state": "continue_repair_and_handoff_controls_visible",
                    "control_plane_state": "governance_usage_runtime_and_handoff_visible",
                    "trust_boundary_state": "blocked_controls_preserve_boundary_reason",
                },
                "scenario_names": [
                    "operator_cockpit_receipt_legibility_behavior",
                    "operator_fast_control_availability_behavior",
                    "operator_control_plane_handoff_legibility_behavior",
                    "operator_m7_cockpit_benchmark_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "receipt_legibility_policy": "operator_receipts_must_expose_summary_status_timestamp_and_thread_context",
                    "fast_control_policy": "active_handoff_items_must_carry_labeled_continue_or_repair_controls",
                    "claim_boundary": "deterministic_operator_surface_receipts_not_live_external_usability_study",
                    "receipt_surfaces": [
                        "/api/operator/benchmark-proof",
                        "/api/operator/m7-cockpit-legibility-benchmark",
                        "/api/operator/control-plane",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 4, "passed": 4, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_cockpit_efficiency_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "cockpit_operator_efficiency_benchmark",
                    "benchmark_posture": "cockpit_efficiency_ci_gated_operator_visible",
                    "operator_status": "cockpit_efficiency_receipts_visible",
                    "scenario_count": 5,
                    "dimension_count": 5,
                    "failure_mode_count": 6,
                    "active_failure_count": 0,
                    "scripted_task_state": "inspect_to_audit_paths_measured",
                    "threshold_state": "action_and_time_budgets_visible",
                    "error_detectability_state": "blocked_degraded_risky_and_lineage_states_visible",
                    "receipt_coverage_state": "all_scripted_tasks_have_receipts",
                    "claim_boundary": "deterministic_operator_efficiency_fixture_not_live_multi_operator_usability_study",
                },
                "scenario_names": [
                    "cockpit_efficiency_task_fixture_behavior",
                    "cockpit_efficiency_threshold_behavior",
                    "cockpit_efficiency_receipt_coverage_behavior",
                    "cockpit_efficiency_baseline_claim_boundary_behavior",
                    "operator_cockpit_efficiency_benchmark_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "scripted_tasks": [],
                "scorecard": {
                    "baseline": "current_seraph_fixture",
                    "task_count": 11,
                    "max_actions_total": 33,
                    "max_seconds_total": 195,
                    "confidence_measurement_boundary": "confidence_affordance_proxy_not_operator_reported_confidence",
                },
                "failure_report": [],
                "policy": {
                    "measurement_policy": "scripted_tasks_require_action_time_error_and_receipt_metrics",
                    "baseline_policy": "baseline_is_current_seraph_fixture_not_competitor_superiority_claim",
                    "competitor_claim_policy": "competitor_informed_expectations_require_source_dated_evidence_before_public_claims",
                    "claim_boundary": "deterministic_operator_efficiency_fixture_not_live_multi_operator_usability_study",
                    "receipt_surfaces": [
                        "/api/operator/benchmark-proof",
                        "/api/operator/cockpit-efficiency-benchmark",
                        "/api/operator/m7-cockpit",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 5, "passed": 5, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_m8_guardian_brain_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "m8_guardian_intervention_quality",
                    "benchmark_posture": "m8_ci_gated_operator_visible",
                    "operator_status": "m8_guardian_brain_receipts_visible",
                    "scenario_count": 7,
                    "dimension_count": 5,
                    "failure_mode_count": 5,
                    "active_failure_count": 0,
                    "decision_surface_state": "act_defer_bundle_clarify_approval_and_silence_receipts_visible",
                    "capability_choice_state": "selected_and_rejected_capability_lanes_visible",
                    "restraint_state": "stale_ambiguous_conflicting_and_low_value_cases_do_not_silently_act",
                    "quality_score_state": "timing_usefulness_false_positive_false_negative_trust_and_recovery_visible",
                    "action_count": 6,
                },
                "scenario_names": [
                    "m8_capability_choice_act_behavior",
                    "m8_ambiguous_evidence_clarify_behavior",
                    "m8_stale_memory_defer_behavior",
                    "m8_conflicting_commitment_bundle_behavior",
                    "m8_risky_capability_approval_behavior",
                    "m8_no_action_restraint_behavior",
                    "operator_m8_guardian_brain_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "decision_receipts": [],
                "failure_report": [],
                "policy": {
                    "milestone_contract": "m8_guardian_brain_and_intervention_quality_ship_as_one_ready_pr",
                    "approval_policy": "high_risk_capability_use_requires_operator_approval_receipt",
                    "claim_boundary": "deterministic_guardian_judgment_receipts_not_live_superiority_claim",
                    "receipt_surfaces": [
                        "/api/operator/m8-guardian-brain",
                        "/api/operator/m8-guardian-intervention-benchmark",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 7, "passed": 7, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_guardian_user_model_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "guardian_user_model_restraint",
                    "benchmark_posture": "ci_gated_operator_visible",
                    "operator_status": "guardian_state_visible",
                    "scenario_count": 4,
                    "dimension_count": 5,
                    "failure_mode_count": 5,
                    "active_failure_count": 0,
                    "clarification_policy_state": "required_on_high_ambiguity",
                    "restraint_policy_state": "clarify_or_wait_before_unverified_personalization",
                },
                "scenario_names": [
                    "guardian_user_model_continuity_behavior",
                    "guardian_clarification_restraint_behavior",
                    "guardian_judgment_behavior",
                    "operator_guardian_state_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "clarify_before_action_policy": "required_on_high_ambiguity",
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 4, "passed": 4, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_m6_memory_superiority_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "m6_memory_superiority",
                    "benchmark_posture": "m6_ci_gated_operator_visible",
                    "operator_status": "m6_memory_superiority_receipts_visible",
                    "scenario_count": 7,
                    "dimension_count": 6,
                    "failure_mode_count": 6,
                    "active_failure_count": 0,
                    "long_horizon_recall_state": "workflow_approval_artifact_audit_session_receipts_ranked",
                    "contradiction_state": "lower_ranked_contradictions_suppressed",
                    "stale_override_state": "fresh_canonical_or_focused_provider_evidence_wins",
                    "source_trust_privacy_state": "guardian_authority_external_advisory_no_secret_receipts",
                    "provider_quality_state": "usefulness_and_degradation_receipts_visible",
                    "behavior_change_receipt_state": "procedural_memory_receipts_required",
                },
                "scenario_names": [
                    "m6_long_horizon_recall_behavior",
                    "m6_contradiction_handling_behavior",
                    "m6_stale_memory_override_behavior",
                    "m6_source_trust_privacy_boundary_behavior",
                    "m6_provider_quality_behavior",
                    "m6_behavior_change_receipts_behavior",
                    "operator_m6_memory_superiority_benchmark_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "milestone_contract": "m6_memory_superiority_ships_as_one_ready_pr",
                    "privacy_policy": "provider_config_and_secret_values_never_surface_in_operator_receipts",
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 7, "passed": 7, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_memory_provider_quality_gate_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "memory_provider_quality_gate",
                    "benchmark_posture": "memory_provider_quality_gate_ci_gated_operator_visible",
                    "operator_status": "memory_provider_quality_gate_visible",
                    "scenario_count": 4,
                    "dimension_count": 4,
                    "failure_mode_count": 5,
                    "active_failure_count": 0,
                    "declaration_state": "required_provider_declarations_visible",
                    "quality_state": "provider_evidence_quality_gated",
                    "suppression_state": "noisy_stale_conflicting_or_unsafe_provider_evidence_suppressed",
                    "operator_control_state": "inspect_correct_pin_forget_and_audit_surfaces_visible",
                    "claim_boundary": "deterministic_provider_quality_gate_not_live_external_memory_provider_superiority",
                },
                "scenario_names": [
                    "memory_provider_quality_gate_contract_behavior",
                    "memory_provider_quality_gate_improvement_behavior",
                    "memory_provider_quality_gate_suppression_behavior",
                    "operator_memory_provider_quality_gate_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "required_declarations": [
                        "provenance",
                        "confidence",
                        "privacy_boundary",
                        "freshness_or_created_at",
                        "evidence_id",
                        "conflict_behavior",
                        "suppression_rules",
                    ],
                    "improvement_policy": "provider_evidence_enters_guardian_context_only_when_quality_gated_and_topic_relevant",
                    "operator_control_surfaces": ["/api/memory/providers", "/api/operator/memory-provider-quality-gate"],
                },
                "latest_run": {"total": 4, "passed": 4, "failed": 0, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_governed_improvement_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "governed_improvement",
                    "benchmark_posture": "ci_gated_operator_visible",
                    "operator_status": "saved_proposal_receipts_visible",
                    "scenario_count": 6,
                    "dimension_count": 5,
                    "failure_mode_count": 5,
                    "active_failure_count": 0,
                    "anti_misevolution_state": "preference_collapse_blocked",
                    "canary_rollout_state": "review_candidates_canary_only",
                    "rollback_state": "candidate_and_receipt_paths_required",
                    "operator_receipt_state": "saved_proposal_and_benchmark_receipts_visible",
                    "recent_receipt_count": 1,
                    "held_receipt_count": 0,
                },
                "scenario_names": [
                    "governed_self_evolution_behavior",
                    "governed_preference_diversity_behavior",
                    "governed_canary_rollout_behavior",
                    "operator_governed_improvement_benchmark_surface_behavior",
                    "capability_repair_behavior",
                    "capability_preflight_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "preference_diversity_policy": "block_preference_collapse_and_watch_single_signal_edits",
                    "canary_rollout_policy": "saved_review_candidates_remain_canary_only_until_reviewed_promotion",
                    "rollback_policy": "candidate_receipt_and_source_baseline_required_before_promotion",
                    "acceptance_policy": "benchmark_gated_canary_then_reviewed_promotion",
                    "operator_visibility": "benchmark_proof_plus_recent_saved_receipts_visible",
                    "receipt_surfaces": [
                        "/api/evolution/validate",
                        "/api/evolution/proposals",
                        "/api/operator/benchmark-proof",
                        "/api/operator/governed-improvement-benchmark",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 6, "passed": 6, "failed": 0, "duration_ms": 100},
                "recent_receipts": [
                    {
                        "id": "web-briefing-review-candidate",
                        "candidate_name": "Web Briefing Review Candidate",
                        "target_type": "skill",
                        "quality_state": "ready",
                        "score": 1.0,
                        "rollout_state": "review_ready",
                        "acceptance_state": "ready_for_canary",
                        "diversity_guard_state": "multi_signal_preserved",
                        "rollback_ready": True,
                        "blocked_constraints": [],
                        "saved_candidate_path": "/tmp/extensions/workspace-capabilities/skills/web-briefing-review-candidate.md",
                        "receipt_path": "/tmp/extensions/workspace-capabilities/evolution/receipts/web-briefing-review-candidate.json",
                        "updated_at": "2026-04-11T08:00:00+00:00",
                    }
                ],
            }
        ),
    ), patch(
        "src.api.operator.build_m9_governed_ecosystem_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "m9_governed_ecosystem",
                    "benchmark_posture": "m9_ci_gated_operator_visible",
                    "operator_status": "m9_governed_ecosystem_receipts_visible",
                    "scenario_count": 6,
                    "dimension_count": 6,
                    "failure_mode_count": 6,
                    "active_failure_count": 0,
                    "manifest_governance_state": "version_compatibility_publisher_trust_and_permissions_visible",
                    "lifecycle_review_gate_state": "privileged_lifecycle_actions_review_gated",
                    "connector_health_state": "degraded_connectors_fail_closed_with_operator_repair",
                    "marketplace_governance_state": "readiness_blockers_trust_and_actions_visible",
                    "diagnostics_update_triage_state": "repair_review_or_defer_triage_visible",
                    "claim_boundary": "deterministic_local_governance_proof_not_competitor_superiority_or_production_marketplace_security",
                },
                "scenario_names": [
                    "m9_manifest_governance_behavior",
                    "m9_lifecycle_review_gate_behavior",
                    "m9_connector_health_degradation_behavior",
                    "m9_marketplace_governance_flow_behavior",
                    "m9_diagnostics_update_triage_behavior",
                    "operator_m9_governed_ecosystem_benchmark_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "governance_receipts": [],
                "failure_report": [],
                "policy": {
                    "connector_health_policy": "degraded_managed_connectors_fail_closed_with_operator_repair_guidance",
                    "claim_boundary": "deterministic_local_governance_proof_not_competitor_superiority_or_production_marketplace_security",
                    "receipt_surfaces": [
                        "/api/operator/benchmark-proof",
                        "/api/operator/m9-governed-ecosystem-benchmark",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 6, "passed": 6, "failed": 0, "duration_ms": 100},
            }
        ),
    ):
        resp = await client.get("/api/operator/benchmark-proof")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_count"] == 20
    assert payload["summary"]["benchmark_posture"] == "deterministic_proof_backed"
    assert payload["summary"]["governed_improvement_status"] == "review_gated_canary_required"
    assert payload["summary"]["memory_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["summary"]["user_model_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["summary"]["workflow_endurance_benchmark_posture"] == "ci_gated_operator_visible"
    assert (
        payload["summary"]["live_workflow_endurance_canary_posture"]
        == "live_workflow_canary_ci_gated_operator_visible"
    )
    assert payload["summary"]["m5_operating_layer_benchmark_posture"] == "m5_ci_gated_operator_visible"
    assert payload["summary"]["trust_boundary_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["summary"]["secure_capability_host_benchmark_posture"] == "secure_host_ci_gated_operator_visible"
    assert payload["summary"]["computer_use_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["summary"]["m2_execution_benchmark_posture"] == "m2_completion_ci_gated_operator_visible"
    assert payload["summary"]["m7_operator_cockpit_benchmark_posture"] == "m7_ci_gated_operator_visible"
    assert payload["summary"]["cockpit_efficiency_benchmark_posture"] == "cockpit_efficiency_ci_gated_operator_visible"
    assert payload["summary"]["m8_guardian_brain_benchmark_posture"] == "m8_ci_gated_operator_visible"
    assert payload["summary"]["live_replay_benchmark_posture"] == "live_replay_ci_gated_operator_visible"
    assert payload["summary"]["m6_memory_superiority_benchmark_posture"] == "m6_ci_gated_operator_visible"
    assert (
        payload["summary"]["memory_provider_quality_gate_benchmark_posture"]
        == "memory_provider_quality_gate_ci_gated_operator_visible"
    )
    assert payload["summary"]["m9_governed_ecosystem_benchmark_posture"] == "m9_ci_gated_operator_visible"
    assert (
        payload["summary"]["m9_governed_ecosystem_claim_boundary"]
        == "deterministic_local_governance_proof_not_competitor_superiority_or_production_marketplace_security"
    )
    assert payload["summary"]["m2_completion_state"] == "ready_to_close_m2"
    assert payload["summary"]["governed_improvement_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["m5_operating_layer_benchmark"]["summary"]["suite_name"] == "m5_jobs_routines_workflows_delegation"
    assert payload["governed_improvement"]["target_count"] == 2
    assert payload["governed_improvement"]["target_types"] == ["prompt_pack", "skill"]
    assert payload["governed_improvement"]["gate_policy"]["requires_human_review"] is True
    assert payload["governed_improvement"]["summary"]["suite_name"] == "governed_improvement"
    assert payload["governed_improvement"]["summary"]["canary_rollout_state"] == "review_candidates_canary_only"
    assert payload["governed_improvement"]["policy"]["rollback_policy"] == "candidate_receipt_and_source_baseline_required_before_promotion"
    assert payload["governed_improvement"]["recent_receipts"][0]["acceptance_state"] == "ready_for_canary"
    assert "guardian_memory_quality" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "memory_continuity_workflows" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "m9_governed_ecosystem" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "live_workflow_endurance_canary" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]

    guardian_memory_suite = next(item for item in payload["suites"] if item["name"] == "guardian_memory_quality")
    assert "memory_contradiction_ranking_behavior" in guardian_memory_suite["scenario_names"]
    assert guardian_memory_suite["scenario_count"] >= 8
    user_model_suite = next(item for item in payload["suites"] if item["name"] == "guardian_user_model_restraint")
    assert "guardian_clarification_restraint_behavior" in user_model_suite["scenario_names"]
    assert user_model_suite["scenario_count"] >= 4

    memory_suite = next(item for item in payload["suites"] if item["name"] == "memory_continuity_workflows")
    assert "workflow_operating_layer_behavior" in memory_suite["scenario_names"]
    assert memory_suite["scenario_count"] >= 10
    workflow_suite = next(item for item in payload["suites"] if item["name"] == "workflow_endurance_and_repair")
    assert "workflow_anticipatory_repair_behavior" in workflow_suite["scenario_names"]
    assert workflow_suite["scenario_count"] >= 4
    live_workflow_canary_suite = next(item for item in payload["suites"] if item["name"] == "live_workflow_endurance_canary")
    assert "live_workflow_canary_protocol_behavior" in live_workflow_canary_suite["scenario_names"]
    assert live_workflow_canary_suite["scenario_count"] == 4
    live_replay_suite = next(item for item in payload["suites"] if item["name"] == "live_long_horizon_eval_replay_v1")
    assert "live_replay_fixture_contract_behavior" in live_replay_suite["scenario_names"]
    assert live_replay_suite["scenario_count"] == 5
    trust_suite = next(item for item in payload["suites"] if item["name"] == "trust_boundary_and_safety_receipts")
    assert "secret_ref_egress_boundary_behavior" in trust_suite["scenario_names"]
    assert trust_suite["scenario_count"] >= 7
    secure_host_suite = next(item for item in payload["suites"] if item["name"] == "secure_capability_host")
    assert "secure_host_secret_ref_fail_closed_behavior" in secure_host_suite["scenario_names"]
    assert "secure_host_capability_trust_matrix_behavior" in secure_host_suite["scenario_names"]
    assert secure_host_suite["scenario_count"] >= 13

    computer_suite = next(item for item in payload["suites"] if item["name"] == "computer_use_browser_desktop")
    assert "browser_execution_task_replay_behavior" in computer_suite["scenario_names"]
    m2_suite = next(item for item in payload["suites"] if item["name"] == "m2_execution_supremacy")
    assert "execution_security_gauntlet_behavior" in m2_suite["scenario_names"]
    m7_suite = next(item for item in payload["suites"] if item["name"] == "m7_operator_cockpit_legibility")
    assert "operator_fast_control_availability_behavior" in m7_suite["scenario_names"]
    assert m7_suite["scenario_count"] == 4
    cockpit_efficiency_suite = next(
        item for item in payload["suites"] if item["name"] == "cockpit_operator_efficiency_benchmark"
    )
    assert "cockpit_efficiency_task_fixture_behavior" in cockpit_efficiency_suite["scenario_names"]
    assert cockpit_efficiency_suite["scenario_count"] == 5
    m8_suite = next(item for item in payload["suites"] if item["name"] == "m8_guardian_intervention_quality")
    assert "m8_risky_capability_approval_behavior" in m8_suite["scenario_names"]
    assert m8_suite["scenario_count"] == 7
    m6_memory_suite = next(item for item in payload["suites"] if item["name"] == "m6_memory_superiority")
    assert "m6_long_horizon_recall_behavior" in m6_memory_suite["scenario_names"]
    assert m6_memory_suite["scenario_count"] == 7
    memory_provider_gate_suite = next(item for item in payload["suites"] if item["name"] == "memory_provider_quality_gate")
    assert "memory_provider_quality_gate_contract_behavior" in memory_provider_gate_suite["scenario_names"]
    assert memory_provider_gate_suite["scenario_count"] == 4
    m9_suite = next(item for item in payload["suites"] if item["name"] == "m9_governed_ecosystem")
    assert "m9_manifest_governance_behavior" in m9_suite["scenario_names"]
    assert m9_suite["scenario_count"] == 6
    assert payload["memory_benchmark"]["summary"]["suite_name"] == "guardian_memory_quality"
    assert payload["memory_benchmark"]["summary"]["active_failure_count"] >= 0
    assert payload["memory_benchmark"]["policy"]["ci_gate_mode"] == "required_benchmark_suite"
    assert payload["user_model_benchmark"]["summary"]["suite_name"] == "guardian_user_model_restraint"
    assert payload["user_model_benchmark"]["policy"]["clarify_before_action_policy"] == "required_on_high_ambiguity"
    assert payload["workflow_endurance_benchmark"]["summary"]["suite_name"] == "workflow_endurance_and_repair"
    assert payload["workflow_endurance_benchmark"]["policy"]["backup_branch_policy"] == "checkpoint_backed_branch_receipts_must_remain_operator_selectable"
    assert payload["live_workflow_endurance_canary"]["summary"]["suite_name"] == "live_workflow_endurance_canary"
    assert payload["live_workflow_endurance_canary"]["policy"]["claim_boundary"] == (
        "audit_projected_replayable_canary_not_durable_workflow_engine"
    )
    assert payload["trust_boundary_benchmark"]["summary"]["suite_name"] == "trust_boundary_and_safety_receipts"
    assert payload["trust_boundary_benchmark"]["policy"]["secret_egress_policy"] == "field_scoped_secret_refs_plus_required_credential_egress_allowlist"
    assert payload["secure_capability_host_benchmark"]["summary"]["suite_name"] == "secure_capability_host"
    assert payload["secure_capability_host_benchmark"]["policy"]["claim_boundary"] == "deterministic_secure_host_choke_points_not_full_host_container_isolation"
    assert payload["computer_use_benchmark"]["summary"]["suite_name"] == "computer_use_browser_desktop"
    assert payload["computer_use_benchmark"]["policy"]["browser_task_replay_policy"] == "extract_html_and_screenshot_actions_require_distinct_audit_receipts"
    assert payload["m2_execution_benchmark"]["summary"]["suite_name"] == "m2_execution_supremacy"
    assert payload["m2_execution_benchmark"]["policy"]["milestone_contract"] == "one_milestone_one_ready_pr"
    assert payload["m7_operator_cockpit_benchmark"]["summary"]["suite_name"] == "m7_operator_cockpit_legibility"
    assert payload["m7_operator_cockpit_benchmark"]["policy"]["fast_control_policy"] == "active_handoff_items_must_carry_labeled_continue_or_repair_controls"
    assert payload["cockpit_efficiency_benchmark"]["summary"]["suite_name"] == "cockpit_operator_efficiency_benchmark"
    assert payload["cockpit_efficiency_benchmark"]["scorecard"]["task_count"] == 11
    assert (
        payload["cockpit_efficiency_benchmark"]["policy"]["measurement_policy"]
        == "scripted_tasks_require_action_time_error_and_receipt_metrics"
    )
    assert (
        payload["cockpit_efficiency_benchmark"]["policy"]["baseline_policy"]
        == "baseline_is_current_seraph_fixture_not_competitor_superiority_claim"
    )
    assert payload["m8_guardian_brain_benchmark"]["summary"]["suite_name"] == "m8_guardian_intervention_quality"
    assert payload["m8_guardian_brain_benchmark"]["policy"]["approval_policy"] == "high_risk_capability_use_requires_operator_approval_receipt"
    assert payload["live_replay_benchmark"]["summary"]["suite_name"] == "live_long_horizon_eval_replay_v1"
    assert payload["live_replay_benchmark"]["policy"]["fixture_policy"] == "fake_providers_and_explicit_time_anchors_required"
    assert payload["m6_memory_superiority_benchmark"]["summary"]["suite_name"] == "m6_memory_superiority"
    assert payload["m6_memory_superiority_benchmark"]["policy"]["privacy_policy"] == "provider_config_and_secret_values_never_surface_in_operator_receipts"
    assert payload["memory_provider_quality_gate_benchmark"]["summary"]["suite_name"] == "memory_provider_quality_gate"
    assert "evidence_id" in payload["memory_provider_quality_gate_benchmark"]["policy"]["required_declarations"]
    assert payload["m9_governed_ecosystem_benchmark"]["summary"]["suite_name"] == "m9_governed_ecosystem"
    assert (
        payload["m9_governed_ecosystem_benchmark"]["policy"]["claim_boundary"]
        == "deterministic_local_governance_proof_not_competitor_superiority_or_production_marketplace_security"
    )


@pytest.mark.asyncio
async def test_operator_governed_improvement_benchmark_surface_reports_policy_and_receipts(client):
    resp = await client.get("/api/operator/governed-improvement-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "governed_improvement"
    assert payload["summary"]["operator_status"] == "saved_proposal_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["anti_misevolution_state"] == "preference_collapse_blocked"
    assert payload["policy"]["preference_diversity_policy"] == "block_preference_collapse_and_watch_single_signal_edits"
    assert payload["policy"]["canary_rollout_policy"] == "saved_review_candidates_remain_canary_only_until_reviewed_promotion"
    assert "/api/operator/governed-improvement-benchmark" in payload["policy"]["receipt_surfaces"]


@pytest.mark.asyncio
async def test_operator_m9_governed_ecosystem_benchmark_surface_reports_policy_receipts_and_claim_boundary(client):
    resp = await client.get("/api/operator/m9-governed-ecosystem-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "m9_governed_ecosystem"
    assert payload["summary"]["operator_status"] == "m9_governed_ecosystem_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["manifest_governance_state"] == "version_compatibility_publisher_trust_and_permissions_visible"
    assert payload["summary"]["connector_health_state"] == "degraded_connectors_fail_closed_with_operator_repair"
    assert payload["summary"]["claim_boundary"] == (
        "deterministic_local_governance_proof_not_competitor_superiority_or_production_marketplace_security"
    )
    assert len(payload["dimensions"]) >= 6
    assert len(payload["failure_taxonomy"]) >= 6
    assert payload["policy"]["manifest_governance_policy"] == (
        "packages_must_expose_version_compatibility_publisher_trust_and_declared_permissions"
    )
    assert payload["policy"]["connector_health_policy"] == (
        "degraded_managed_connectors_fail_closed_with_operator_repair_guidance"
    )
    assert payload["policy"]["claim_boundary"] == (
        "deterministic_local_governance_proof_not_competitor_superiority_or_production_marketplace_security"
    )
    assert "/api/operator/m9-governed-ecosystem-benchmark" in payload["policy"]["receipt_surfaces"]
    assert payload["governance_receipts"][0]["scenario_id"] == "m9_manifest_governance_behavior"


@pytest.mark.asyncio
async def test_operator_m7_cockpit_legibility_benchmark_surface_reports_receipts_controls_and_claim_boundary(client):
    resp = await client.get("/api/operator/m7-cockpit-legibility-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "m7_operator_cockpit_legibility"
    assert payload["summary"]["operator_status"] == "m7_cockpit_legibility_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["receipt_legibility_state"] == "summary_status_time_and_thread_visible"
    assert payload["summary"]["fast_control_state"] == "continue_repair_and_handoff_controls_visible"
    assert payload["policy"]["receipt_legibility_policy"] == "operator_receipts_must_expose_summary_status_timestamp_and_thread_context"
    assert payload["policy"]["fast_control_policy"] == "active_handoff_items_must_carry_labeled_continue_or_repair_controls"
    assert payload["policy"]["claim_boundary"] == "deterministic_operator_surface_receipts_not_live_external_usability_study"
    assert "/api/operator/control-plane" in payload["policy"]["receipt_surfaces"]


@pytest.mark.asyncio
async def test_operator_cockpit_efficiency_benchmark_surface_reports_policy_metrics_and_claim_boundary(client):
    resp = await client.get("/api/operator/cockpit-efficiency-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "cockpit_operator_efficiency_benchmark"
    assert payload["summary"]["operator_status"] == "cockpit_efficiency_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["scripted_task_state"] == "inspect_to_audit_paths_measured"
    assert payload["summary"]["threshold_state"] == "action_and_time_budgets_visible"
    assert payload["summary"]["receipt_coverage_state"] == "all_scripted_tasks_have_receipts"
    assert payload["scorecard"]["task_count"] == 11
    assert payload["scorecard"]["max_actions_total"] == 33
    assert payload["scorecard"]["max_seconds_total"] == 195
    assert payload["scorecard"]["confidence_measurement_boundary"] == (
        "confidence_affordance_proxy_not_operator_reported_confidence"
    )
    assert payload["policy"]["measurement_policy"] == "scripted_tasks_require_action_time_error_and_receipt_metrics"
    assert payload["policy"]["baseline_policy"] == "baseline_is_current_seraph_fixture_not_competitor_superiority_claim"
    assert payload["policy"]["claim_boundary"] == (
        "deterministic_operator_efficiency_fixture_not_live_multi_operator_usability_study"
    )
    assert "/api/operator/m7-cockpit" in payload["policy"]["receipt_surfaces"]


@pytest.mark.asyncio
async def test_operator_cockpit_efficiency_benchmark_surface_degrades_summary_on_failures(client):
    summary = SimpleNamespace(
        total=5,
        passed=4,
        failed=1,
        duration_ms=50,
        results=[
            SimpleNamespace(
                name="cockpit_efficiency_receipt_coverage_behavior",
                passed=False,
                error="receipt missing",
            )
        ],
    )

    with patch(
        "src.cockpit.efficiency_benchmark._run_cockpit_efficiency_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        resp = await client.get("/api/operator/cockpit-efficiency-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "cockpit_efficiency_ci_regressions_detected_operator_visible"
    assert payload["summary"]["active_failure_count"] == 1
    assert payload["summary"]["receipt_coverage_state"] == "regressions_detected"
    assert payload["failure_report"][0]["scenario_name"] == "cockpit_efficiency_receipt_coverage_behavior"


@pytest.mark.asyncio
async def test_operator_memory_provider_quality_gate_surface_reports_policy_and_claim_boundary(client):
    resp = await client.get("/api/operator/memory-provider-quality-gate")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "memory_provider_quality_gate"
    assert payload["summary"]["operator_status"] == "memory_provider_quality_gate_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["declaration_state"] == "required_provider_declarations_visible"
    assert payload["summary"]["suppression_state"] == "noisy_stale_conflicting_or_unsafe_provider_evidence_suppressed"
    assert payload["policy"]["minimum_context_confidence"] == 0.5
    assert "evidence_id" in payload["policy"]["required_declarations"]
    assert "private" in payload["policy"]["privacy_boundaries_suppressed_before_context"]
    assert (
        payload["summary"]["claim_boundary"]
        == "deterministic_provider_quality_gate_not_live_external_memory_provider_superiority"
    )


@pytest.mark.asyncio
async def test_operator_memory_provider_quality_gate_surface_degrades_summary_on_failures(client):
    summary = SimpleNamespace(
        total=4,
        passed=3,
        failed=1,
        duration_ms=50,
        results=[
            SimpleNamespace(
                name="memory_provider_quality_gate_suppression_behavior",
                passed=False,
                error="private provider evidence reached context",
            )
        ],
    )

    with patch(
        "src.memory.provider_quality_gate._run_memory_provider_quality_gate_suite",
        AsyncMock(return_value=summary),
    ):
        resp = await client.get("/api/operator/memory-provider-quality-gate")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "memory_provider_quality_gate_regressions_detected_operator_visible"
    assert payload["summary"]["suppression_state"] == "regressions_detected"
    assert payload["failure_report"][0]["scenario_name"] == "memory_provider_quality_gate_suppression_behavior"


@pytest.mark.asyncio
async def test_operator_m8_guardian_brain_surface_reports_decisions_capabilities_and_restraint(client):
    with patch(
        "src.api.operator.build_guardian_state",
        AsyncMock(
            return_value=SimpleNamespace(
                action_posture="guarded_action",
                intent_resolution="clarify_or_continue",
                intent_uncertainty_level="ambiguous",
            )
        ),
    ):
        resp = await client.get("/api/operator/m8-guardian-brain?session_id=session-1")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "m8_guardian_brain_visible"
    assert payload["summary"]["decision_count"] == 7
    assert payload["summary"]["live_decision_count"] == 1
    assert payload["summary"]["benchmark_decision_count"] == 6
    assert payload["summary"]["receipt_source"] == "live_guardian_state_plus_deterministic_benchmark"
    assert payload["summary"]["capability_choice_count"] >= 3
    assert payload["summary"]["approval_preservation_count"] == 1
    assert "trust_preservation" in payload["summary"]["score_dimensions"]
    assert payload["live_decision_receipt"]["scenario_id"] == "operator_live_guardian_brain_behavior"
    assert payload["live_decision_receipt"]["inputs"]["source"] == "live_guardian_state"
    assert payload["live_decision_receipt"]["inputs"]["intent_resolution"] == "clarify_or_continue"
    assert payload["live_decision_receipt"]["claim_boundary"] == "live_guardian_state_derived_receipt_not_external_outcome_or_superiority_claim"
    assert {receipt["action"] for receipt in payload["decision_receipts"]} == {
        "act",
        "bundle",
        "clarify",
        "defer",
        "request_approval",
        "stay_silent",
    }
    assert payload["approval_receipts"][0]["action"] == "request_approval"
    assert payload["approval_receipts"][0]["selected_capability"]["requires_approval"] is True
    assert payload["claim_boundaries"]["not_claimed"] == [
        "superior_guardian_intelligence",
        "live_external_outcome_study",
        "automatic_privilege_escalation_from_memory_or_preferences",
    ]


@pytest.mark.asyncio
async def test_operator_m8_guardian_intervention_benchmark_surface_reports_policy_and_receipts(client):
    resp = await client.get("/api/operator/m8-guardian-intervention-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "m8_guardian_intervention_quality"
    assert payload["summary"]["operator_status"] == "m8_guardian_brain_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["decision_surface_state"] == "act_defer_bundle_clarify_approval_and_silence_receipts_visible"
    assert payload["summary"]["capability_choice_state"] == "selected_and_rejected_capability_lanes_visible"
    assert payload["policy"]["milestone_contract"] == "m8_guardian_brain_and_intervention_quality_ship_as_one_ready_pr"
    assert payload["policy"]["claim_boundary"] == "deterministic_guardian_judgment_receipts_not_live_superiority_claim"
    assert "/api/operator/m8-guardian-brain" in payload["policy"]["receipt_surfaces"]


@pytest.mark.asyncio
async def test_operator_live_replay_benchmark_surface_reports_policy_receipts_and_claim_boundary(client):
    resp = await client.get("/api/operator/live-long-horizon-replay-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "live_long_horizon_eval_replay_v1"
    assert payload["summary"]["operator_status"] == "live_replay_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["fixture_state"] == "time_stable_fake_provider_replays"
    assert payload["summary"]["coverage_state"] == "memory_workflow_reach_security_cockpit_covered"
    assert payload["policy"]["fixture_policy"] == "fake_providers_and_explicit_time_anchors_required"
    assert (
        payload["policy"]["claim_boundary"]
        == "deterministic_liveish_replay_proof_not_live_human_outcome_or_provider_attestation"
    )
    assert "/api/operator/live-long-horizon-replay-benchmark" in payload["policy"]["receipt_surfaces"]
    assert {fixture["surface"] for fixture in payload["replay_fixtures"]} == {
        "memory",
        "workflow",
        "reach",
        "security",
        "cockpit",
    }


@pytest.mark.asyncio
async def test_operator_live_replay_benchmark_surface_degrades_summary_on_failures(client):
    failing_summary = SimpleNamespace(
        total=5,
        passed=4,
        failed=1,
        duration_ms=100,
        results=[
            SimpleNamespace(
                name="live_replay_surface_coverage_behavior",
                passed=False,
                error="missing reach replay",
            )
        ],
    )

    with patch("src.replay.benchmark._run_live_replay_benchmark_suite", AsyncMock(return_value=failing_summary)):
        resp = await client.get("/api/operator/live-long-horizon-replay-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "live_replay_ci_regressions_detected_operator_visible"
    assert payload["summary"]["active_failure_count"] == 1
    assert payload["summary"]["coverage_state"] == "regressions_detected"
    assert payload["failure_report"][0]["scenario_name"] == "live_replay_surface_coverage_behavior"


@pytest.mark.asyncio
async def test_operator_memory_benchmark_surface_reports_failure_taxonomy_and_policy(client):
    resp = await client.get("/api/operator/memory-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "guardian_memory_quality"
    assert payload["summary"]["operator_status"] == "memory_proof_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert len(payload["dimensions"]) >= 5
    assert len(payload["failure_taxonomy"]) >= 5
    assert payload["policy"]["retrieval_ranking_policy"] == "contradiction_aware_query_and_project_weighted"
    assert payload["policy"]["ci_gate_mode"] == "required_benchmark_suite"


@pytest.mark.asyncio
async def test_operator_m6_memory_superiority_benchmark_surface_reports_policy_and_receipts(client):
    resp = await client.get("/api/operator/m6-memory-superiority-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "m6_memory_superiority"
    assert payload["summary"]["operator_status"] == "m6_memory_superiority_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["long_horizon_recall_state"] == "workflow_approval_artifact_audit_session_receipts_ranked"
    assert payload["summary"]["source_trust_privacy_state"] == "guardian_authority_external_advisory_no_secret_receipts"
    assert payload["policy"]["milestone_contract"] == "m6_memory_superiority_ships_as_one_ready_pr"
    assert "/api/operator/m6-memory-superiority-benchmark" in payload["policy"]["receipt_surfaces"]


@pytest.mark.asyncio
async def test_operator_workflow_endurance_benchmark_surface_reports_policy_and_state(client):
    resp = await client.get("/api/operator/workflow-endurance-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "workflow_endurance_and_repair"
    assert payload["summary"]["operator_status"] == "workflow_orchestration_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["policy"]["anticipatory_repair_policy"] == "prepare_repair_and_backup_branch_before_obvious_failure_points"
    assert payload["policy"]["backup_branch_policy"] == "checkpoint_backed_branch_receipts_must_remain_operator_selectable"


@pytest.mark.asyncio
async def test_operator_workflow_endurance_benchmark_surface_degrades_summary_on_failures(client):
    failing_summary = SimpleNamespace(
        total=4,
        passed=2,
        failed=2,
        duration_ms=13,
        results=[
            SimpleNamespace(
                passed=False,
                name="workflow_backup_branch_surface_behavior",
                error="backup branch regression",
            )
        ],
    )

    with patch("src.workflows.benchmark._run_workflow_endurance_benchmark_suite", AsyncMock(return_value=failing_summary)):
        resp = await client.get("/api/operator/workflow-endurance-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "ci_regressions_detected_operator_visible"
    assert payload["summary"]["anticipatory_repair_state"] == "regressions_detected"
    assert payload["summary"]["condensation_fidelity_state"] == "regressions_detected"
    assert payload["summary"]["branch_continuity_state"] == "regressions_detected"
    assert payload["failure_report"][0]["scenario_name"] == "workflow_backup_branch_surface_behavior"


@pytest.mark.asyncio
async def test_operator_live_workflow_endurance_canary_surface_reports_story_and_claim_boundary(client):
    resp = await client.get("/api/operator/live-workflow-endurance-canary")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "live_workflow_endurance_canary"
    assert payload["summary"]["operator_status"] == "live_workflow_canary_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["failure_injection_count"] == 1
    assert payload["summary"]["recovery_action_count"] == 1
    assert payload["summary"]["trust_boundary_block_count"] == 1
    assert payload["operator_story"]["multi_session_visible"] is True
    assert payload["operator_story"]["artifact_comparison_visible"] is True
    assert payload["operator_story"]["approval_preservation_visible"] is True
    assert payload["operator_story"]["trust_boundary_fail_closed_visible"] is True
    assert payload["policy"]["claim_boundary"] == "audit_projected_replayable_canary_not_durable_workflow_engine"
    assert "durable_workflow_state_machine" in payload["policy"]["not_claimed"]
    assert "/api/operator/benchmark-proof" in payload["policy"]["receipt_surfaces"]


@pytest.mark.asyncio
async def test_operator_live_workflow_endurance_canary_surface_degrades_summary_on_failures(client):
    failing_summary = SimpleNamespace(
        total=4,
        passed=3,
        failed=1,
        duration_ms=13,
        results=[
            SimpleNamespace(
                passed=False,
                name="live_workflow_canary_approval_preservation_behavior",
                error="approval preservation regression",
            )
        ],
    )

    with patch(
        "src.workflows.endurance_canary._run_live_workflow_endurance_canary_suite",
        AsyncMock(return_value=failing_summary),
    ):
        resp = await client.get("/api/operator/live-workflow-endurance-canary")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "live_workflow_canary_regressions_detected_operator_visible"
    assert payload["summary"]["active_failure_count"] == 1
    assert payload["failure_report"][0]["scenario_name"] == "live_workflow_canary_approval_preservation_behavior"


@pytest.mark.asyncio
async def test_operator_trust_boundary_benchmark_surface_reports_policy_and_receipts(client):
    resp = await client.get("/api/operator/trust-boundary-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "trust_boundary_and_safety_receipts"
    assert payload["summary"]["operator_status"] == "safety_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["secret_egress_state"] == "field_scoped_egress_allowlist_required"
    assert payload["policy"]["secret_egress_policy"] == "field_scoped_secret_refs_plus_required_credential_egress_allowlist"
    assert "/api/operator/benchmark-proof" in payload["policy"]["receipt_surfaces"]
    assert payload["policy"]["ci_gate_mode"] == "required_benchmark_suite"


@pytest.mark.asyncio
async def test_operator_secure_capability_host_benchmark_surface_reports_policy_and_receipts(client):
    resp = await client.get("/api/operator/secure-capability-host-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "secure_capability_host"
    assert payload["summary"]["operator_status"] == "secure_capability_host_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["host_isolation_state"] == "deterministic_choke_points_claim_bounded"
    assert payload["summary"]["credential_egress_state"] == "session_field_host_allowlist_enforced"
    assert payload["summary"]["workspace_secret_file_state"] == "generic_read_patch_blocked"
    assert payload["summary"]["workspace_escape_state"] == "workspace_relative_paths_enforced"
    assert payload["summary"]["process_environment_state"] == "ambient_secret_env_scrubbed"
    assert payload["summary"]["browser_cookie_session_state"] == "per_run_context_no_storage_state_receipts"
    assert payload["summary"]["hostile_provider_replay_state"] == "trust_expanding_replay_blocked"
    assert payload["summary"]["capability_trust_matrix_state"] == "owner_boundary_credential_mutation_receipts_visible"
    assert "secure_host_workspace_escape_boundary_behavior" in payload["scenario_names"]
    assert "secure_host_receipt_surface_completeness_behavior" in payload["scenario_names"]
    assert "full_host_container_isolation" in payload["isolation_strategy"]["not_claimed"]
    assert payload["browser_partition_policy"]["claim_boundary"] == (
        "deterministic_browser_partition_strategy_not_complete_authenticated_browser_isolation"
    )
    assert len(payload["capability_trust_regression_matrix"]) >= 7
    assert "/api/activity/ledger" in payload["receipt_surface_completeness"]["required_surfaces"]
    assert "claim_boundary" in payload["receipt_surface_completeness"]["required_receipt_fields"]
    assert payload["policy"]["host_isolation_policy"] == "deterministic_choke_points_with_explicit_non_container_claim_boundary"
    assert payload["policy"]["credential_egress_policy"] == "session_bound_field_scoped_destination_host_allowlisted_secret_refs"
    assert payload["policy"]["browser_cookie_session_policy"] == "per_run_browser_contexts_without_persisted_cookie_or_storage_state"
    assert payload["policy"]["hostile_provider_replay_policy"] == "provider_replay_or_fallback_must_not_expand_trust_class_or_reuse_sensitive_context"
    assert payload["policy"]["claim_boundary"] == "deterministic_secure_host_choke_points_not_full_host_container_isolation"
    assert "/api/operator/secure-capability-host-benchmark" in payload["policy"]["receipt_surfaces"]
    assert payload["policy"]["ci_gate_mode"] == "required_benchmark_suite"


@pytest.mark.asyncio
async def test_operator_trust_boundary_benchmark_surface_degrades_summary_on_failures(client):
    failing_summary = SimpleNamespace(
        total=7,
        passed=5,
        failed=2,
        duration_ms=12,
        results=[
            SimpleNamespace(
                passed=False,
                name="secret_ref_egress_boundary_behavior",
                error="secret ref egress regression",
            )
        ],
    )

    with patch("src.security.benchmark._run_trust_boundary_benchmark_suite", AsyncMock(return_value=failing_summary)):
        resp = await client.get("/api/operator/trust-boundary-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "ci_regressions_detected_operator_visible"
    assert payload["summary"]["secret_egress_state"] == "regressions_detected"
    assert payload["summary"]["delegation_partition_state"] == "regressions_detected"
    assert payload["summary"]["workflow_replay_state"] == "regressions_detected"
    assert payload["failure_report"][0]["scenario_name"] == "secret_ref_egress_boundary_behavior"


@pytest.mark.asyncio
async def test_operator_computer_use_benchmark_surface_reports_policy_and_receipts(client):
    resp = await client.get("/api/operator/computer-use-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "computer_use_browser_desktop"
    assert payload["summary"]["operator_status"] == "browser_desktop_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["browser_replay_state"] == "extract_html_and_screenshot_receipts_visible"
    assert payload["policy"]["browser_task_replay_policy"] == "extract_html_and_screenshot_actions_require_distinct_audit_receipts"
    assert "/api/operator/computer-use-benchmark" in payload["policy"]["receipt_surfaces"]
    assert payload["policy"]["ci_gate_mode"] == "required_benchmark_suite"


@pytest.mark.asyncio
async def test_operator_m2_execution_benchmark_surface_reports_completion_policy(client):
    resp = await client.get("/api/operator/m2-execution-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "m2_execution_supremacy"
    assert payload["summary"]["operator_status"] == "m2_execution_readiness_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["milestone_completion_state"] == "ready_to_close_m2"
    assert payload["policy"]["milestone_contract"] == "one_milestone_one_ready_pr"
    assert payload["policy"]["completion_policy"] == "all_execution_families_and_435_security_gauntlet_must_pass"
    assert "/api/operator/m2-execution-benchmark" in payload["policy"]["receipt_surfaces"]
    assert "execution_artifact_registry_behavior" in payload["scenario_names"]
    assert "execution_security_gauntlet_behavior" in payload["scenario_names"]


@pytest.mark.asyncio
async def test_operator_computer_use_benchmark_surface_degrades_summary_on_failures(client):
    failing_summary = SimpleNamespace(
        total=7,
        passed=5,
        failed=2,
        duration_ms=14,
        results=[
            SimpleNamespace(
                passed=False,
                name="desktop_notification_action_replay_behavior",
                error="desktop notification replay regression",
            )
        ],
    )

    with patch("src.browser.benchmark._run_computer_use_benchmark_suite", AsyncMock(return_value=failing_summary)):
        resp = await client.get("/api/operator/computer-use-benchmark")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "ci_regressions_detected_operator_visible"
    assert payload["summary"]["browser_replay_state"] == "regressions_detected"
    assert payload["summary"]["desktop_action_state"] == "regressions_detected"
    assert payload["summary"]["cross_surface_receipt_state"] == "regressions_detected"
    assert payload["failure_report"][0]["scenario_name"] == "desktop_notification_action_replay_behavior"


@pytest.mark.asyncio
async def test_operator_benchmark_proof_degrades_top_level_posture_when_child_benchmark_is_red(client):
    with patch(
        "src.api.operator.build_computer_use_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "computer_use_browser_desktop",
                    "benchmark_posture": "ci_regressions_detected_operator_visible",
                    "operator_status": "browser_desktop_receipts_visible",
                    "scenario_count": 7,
                    "dimension_count": 5,
                    "failure_mode_count": 5,
                    "active_failure_count": 1,
                    "browser_replay_state": "regressions_detected",
                    "desktop_action_state": "regressions_detected",
                    "cross_surface_receipt_state": "regressions_detected",
                },
                "scenario_names": ["browser_execution_task_replay_behavior"],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [
                    {
                        "type": "benchmark_regression",
                        "scenario_name": "desktop_notification_action_replay_behavior",
                        "summary": "desktop notification replay regression",
                        "reason": "deterministic_eval_failure",
                    }
                ],
                "policy": {
                    "browser_task_replay_policy": "extract_html_and_screenshot_actions_require_distinct_audit_receipts",
                    "desktop_action_replay_policy": "enqueue_dismiss_poll_and_ack_must_remain_cross_surface_replayable",
                    "cross_surface_continuity_policy": "browser_and_desktop_share_one_operator_visible_continuity_snapshot",
                    "operator_visibility": "benchmark_proof_plus_computer_use_receipts_visible",
                    "receipt_surfaces": ["/api/operator/computer-use-benchmark"],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 7, "passed": 6, "failed": 1, "duration_ms": 100},
            }
        ),
    ), patch(
        "src.api.operator.build_m2_execution_benchmark_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "m2_execution_supremacy",
                    "benchmark_posture": "m2_completion_ci_gated_operator_visible",
                    "operator_status": "m2_execution_readiness_visible",
                    "scenario_count": 11,
                    "dimension_count": 5,
                    "failure_mode_count": 6,
                    "active_failure_count": 0,
                    "terminal_process_state": "bounded_with_recovery_receipts",
                    "browser_http_state": "dns_redirect_and_subrequest_guarded",
                    "artifact_registry_state": "stable_ids_hashes_boundaries_and_recovery_hints_visible",
                    "security_gauntlet_state": "m2_435_threats_pinned",
                    "milestone_completion_state": "ready_to_close_m2",
                },
                "scenario_names": ["execution_security_gauntlet_behavior"],
                "dimensions": [],
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "milestone_contract": "one_milestone_one_ready_pr",
                    "completion_policy": "all_execution_families_and_435_security_gauntlet_must_pass",
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 11, "passed": 11, "failed": 0, "duration_ms": 100},
            }
        ),
    ):
        resp = await client.get("/api/operator/benchmark-proof")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "deterministic_proof_backed_with_regressions"
    assert payload["summary"]["computer_use_benchmark_posture"] == "ci_regressions_detected_operator_visible"


@pytest.mark.asyncio
async def test_operator_guardian_state_surfaces_confidence_and_explanation(client):
    guardian_state = SimpleNamespace(
        confidence=SimpleNamespace(
            overall="partial",
            observer="grounded",
            world_model="partial",
            memory="grounded",
            current_session="grounded",
            recent_sessions="partial",
        ),
        intent_uncertainty_level="high",
        intent_resolution="clarify_first",
        judgment_proof_lines=(
            "Project-target proof: Atlas remains the strongest active project anchor.",
            "Referent proof: the user message contains an unresolved referent.",
        ),
        action_posture="clarify_first",
        intent_uncertainty_diagnostics=(
            "Ambiguous referent detected in the latest user message.",
        ),
        learning_diagnostics=(
            "Fresh live outcomes are overruling older procedural guidance.",
        ),
        memory_provider_diagnostics=(
            "Provider evidence: canonical memory remains authoritative.",
        ),
        memory_reconciliation_diagnostics=(
            "Conflict policy: archive superseded project hints after reconciliation.",
        ),
        restraint_reasons=(
            "Intent remains weakly grounded, so clarification is safer than taking a confident action.",
        ),
        user_model_benchmark_diagnostics=(
            "User-model benchmark state: confidence=grounded, restraint_posture=clarify_before_personalizing, action_posture=clarify_first.",
        ),
        learning_guidance="Prefer clarification before interrupting.",
        recent_execution_summary="- Atlas deploy failed recently",
        world_model=SimpleNamespace(
            current_focus="Atlas release planning",
            focus_source="observer_goal_window",
            focus_alignment="aligned",
            intervention_receptivity="guarded",
            dominant_thread="Atlas launch thread",
            user_model_confidence="grounded",
            user_model_profile=SimpleNamespace(
                confidence="grounded",
                restraint_posture="clarify_before_personalizing",
                continuity_strategy="prefer_existing_thread",
                clarification_watchpoints=("Clarify interaction style when live and procedural preference evidence disagree.",),
                restraint_reasons=("Preference evidence is split, so Seraph should explain uncertainty first.",),
                evidence_store=("Prefers concise updates during Atlas launch work.",),
                facets=(
                    SimpleNamespace(
                        key="communication_style",
                        label="Communication preference",
                        value="brief literal",
                        confidence="grounded",
                        evidence_sources=("preference_memory", "live_learning"),
                        evidence_lines=("Prefers concise updates during Atlas launch work.",),
                    ),
                ),
            ),
            judgment_risks=("Competing project anchors still require conservative judgment.",),
            corroboration_sources=("observer", "memory", "recent_sessions"),
            preference_inference_diagnostics=("User-model evidence sources: observer, memory",),
            active_projects=("Atlas",),
            active_commitments=("Ship Atlas release notes",),
            active_blockers=("Pending release approval",),
            next_up=("Clarify whether the user meant Atlas or Hermes",),
        ),
        observer_context=SimpleNamespace(
            user_state="focused",
            interruption_mode="minimal",
            active_window="VS Code",
            active_project="Atlas",
            active_goals_summary="Ship Atlas safely",
            screen_context="Reviewing Atlas release notes",
            data_quality="good",
            is_working_hours=True,
        ),
    )

    with patch(
        "src.api.operator.build_guardian_state",
        AsyncMock(return_value=guardian_state),
    ):
        resp = await client.get("/api/operator/guardian-state", params={"session_id": "session-1"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["session_id"] == "session-1"
    assert payload["summary"]["overall_confidence"] == "partial"
    assert payload["summary"]["intent_resolution"] == "clarify_first"
    assert payload["summary"]["action_posture"] == "clarify_first"
    assert payload["summary"]["current_focus"] == "Atlas release planning"
    assert payload["summary"]["user_model_confidence"] == "grounded"
    assert payload["explanation"]["judgment_proof_lines"][0].startswith("Project-target proof:")
    assert payload["explanation"]["judgment_risks"][0].startswith("Competing project anchors")
    assert payload["explanation"]["learning_diagnostics"][0].startswith("Fresh live outcomes")
    assert payload["explanation"]["restraint_reasons"][0].startswith("Intent remains weakly grounded")
    assert payload["user_model"]["restraint_posture"] == "clarify_before_personalizing"
    assert payload["user_model"]["continuity_strategy"] == "prefer_existing_thread"
    assert payload["user_model"]["facets"][0]["label"] == "Communication preference"
    assert payload["operator_guidance"]["next_up"][0].startswith("Clarify whether the user meant")
    assert payload["observer"]["active_project"] == "Atlas"
    assert payload["observer"]["is_working_hours"] is True


@pytest.mark.asyncio
async def test_operator_workflow_orchestration_groups_sessions_and_step_focus(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-1",
                        "run_identity": "session-1:workflow_repo_review:1",
                        "workflow_name": "repo-review",
                        "summary": "Waiting on guarded approval",
                        "status": "awaiting_approval",
                        "availability": "blocked",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "started_at": "2026-04-08T10:00:00Z",
                        "updated_at": "2026-04-08T10:05:00Z",
                        "thread_continue_message": "Resume repo review.",
                        "pending_approval_count": 1,
                        "artifact_paths": ["notes/repo-review.md"],
                        "step_records": [
                            {
                                "id": "scope",
                                "index": 0,
                                "tool": "session_search",
                                "status": "succeeded",
                                "result_summary": "Scoped prior review context",
                            },
                            {
                                "id": "collect",
                                "index": 1,
                                "tool": "web_search",
                                "status": "succeeded",
                                "result_summary": "Collected repository evidence",
                            },
                            {
                                "id": "compare",
                                "index": 2,
                                "tool": "diff_compare",
                                "status": "succeeded",
                                "result_summary": "Compared current branch artifacts",
                            },
                            {
                                "id": "draft",
                                "index": 3,
                                "tool": "write_file",
                                "status": "succeeded",
                                "result_summary": "Drafted review receipt",
                            },
                            {
                                "id": "approve",
                                "index": 4,
                                "tool": "write_file",
                                "status": "awaiting_approval",
                                "result_summary": "Awaiting approval",
                                "recovery_hint": "Approve write_file to continue.",
                            },
                        ],
                        "checkpoint_candidates": [{"step_id": "collect", "label": "collect"}],
                    },
                    {
                        "id": "run-2",
                        "run_identity": "session-2:workflow_daily_brief:1",
                        "root_run_identity": "session-2:workflow_daily_brief:1",
                        "workflow_name": "daily-brief",
                        "summary": "Failed while drafting follow-up",
                        "status": "failed",
                        "availability": "blocked",
                        "thread_id": "session-2",
                        "thread_label": "Session 2",
                        "started_at": "2026-04-08T09:00:00Z",
                        "updated_at": "2026-04-08T09:07:00Z",
                        "thread_continue_message": "Retry the daily brief.",
                        "retry_from_step_draft": "Retry daily brief from draft step.",
                        "artifact_paths": ["notes/daily-brief.md"],
                        "replay_block_reason": "approval_context_changed",
                        "step_records": [
                            {
                                "id": "gather",
                                "index": 0,
                                "tool": "session_search",
                                "status": "succeeded",
                                "result_summary": "Gathered yesterday's brief",
                            },
                            {
                                "id": "outline",
                                "index": 1,
                                "tool": "llm_plan",
                                "status": "succeeded",
                                "result_summary": "Outlined follow-up summary",
                            },
                            {
                                "id": "draft",
                                "index": 2,
                                "tool": "write_file",
                                "status": "succeeded",
                                "result_summary": "Drafted daily brief",
                            },
                            {
                                "id": "publish",
                                "index": 3,
                                "tool": "write_file",
                                "status": "failed",
                                "error_summary": "write_file denied",
                                "recovery_hint": "Retry or repair permissions.",
                                "recovery_actions": [{"type": "set_tool_policy"}],
                                "is_recoverable": True,
                            },
                        ],
                    },
                    {
                        "id": "run-4",
                        "run_identity": "session-2:workflow_daily_brief:branch-1",
                        "root_run_identity": "session-2:workflow_daily_brief:1",
                        "parent_run_identity": "session-2:workflow_daily_brief:1",
                        "branch_kind": "branch_from_checkpoint",
                        "workflow_name": "daily-brief",
                        "summary": "Branched repair draft completed",
                        "status": "succeeded",
                        "availability": "ready",
                        "thread_id": "session-2",
                        "thread_label": "Session 2",
                        "started_at": "2026-04-08T09:10:00Z",
                        "updated_at": "2026-04-08T09:15:00Z",
                        "thread_continue_message": "Continue branched brief.",
                        "artifact_paths": ["notes/daily-brief-v2.md"],
                        "step_records": [
                            {
                                "id": "repair",
                                "index": 0,
                                "tool": "write_file",
                                "status": "succeeded",
                                "result_summary": "Published repaired brief",
                            },
                        ],
                    },
                    {
                        "id": "run-3",
                        "run_identity": "ambient:workflow_cleanup:1",
                        "workflow_name": "cleanup",
                        "summary": "Currently running cleanup.",
                        "status": "running",
                        "availability": "ready",
                        "thread_id": None,
                        "thread_label": None,
                        "started_at": "2026-04-08T08:00:00Z",
                        "updated_at": "2026-04-08T08:30:00Z",
                        "thread_continue_message": "Continue cleanup.",
                        "step_records": [
                            {
                                "id": "scan",
                                "index": 0,
                                "tool": "filesystem_read",
                                "status": "running",
                                "result_summary": "Scanning workspace",
                            },
                        ],
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(
                return_value=[
                    {"id": "session-1", "title": "Session 1"},
                    {"id": "session-2", "title": "Session 2"},
                ]
            ),
        ),
    ):
        resp = await client.get(
            "/api/operator/workflow-orchestration",
            params={"limit_sessions": 6, "limit_workflows": 8},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["tracked_sessions"] == 2
    assert payload["summary"]["workflow_count"] == 4
    assert payload["summary"]["active_workflows"] == 2
    assert payload["summary"]["blocked_workflows"] == 2
    assert payload["summary"]["awaiting_approval_workflows"] == 1
    assert payload["summary"]["recoverable_workflows"] == 1
    assert payload["summary"]["long_running_workflows"] == 2
    assert payload["summary"]["compacted_workflows"] == 2
    assert payload["summary"]["total_step_count"] == 11
    assert payload["summary"]["compacted_step_count"] == 3
    assert payload["summary"]["boundary_blocked_workflows"] == 1
    assert payload["summary"]["repair_ready_workflows"] == 1
    assert payload["summary"]["branch_ready_workflows"] == 2
    assert payload["summary"]["stalled_workflows"] == 2
    assert payload["summary"]["output_debugger_ready_workflows"] == 2
    assert payload["summary"]["attention_sessions"] == 3

    sessions = payload["sessions"]
    assert sessions[0]["thread_id"] == "session-2"
    assert sessions[0]["lead_workflow_name"] == "daily-brief"
    assert sessions[0]["blocked_workflows"] == 1
    assert sessions[0]["continue_message"] is None
    assert sessions[0]["lead_step_focus"]["kind"] == "failure"
    assert sessions[0]["long_running_workflow_count"] == 1
    assert sessions[0]["compacted_workflow_count"] == 1
    assert sessions[0]["total_step_count"] == 5
    assert sessions[0]["compacted_step_count"] == 1
    assert sessions[0]["lead_state_capsule"].startswith("4 steps")
    assert sessions[0]["queue_position"] == 1
    assert sessions[0]["queue_state"] == "boundary_blocked"
    assert sessions[0]["queue_reason"] == "1 workflow crossed a changed trust boundary and now needs repair or a fresh run."
    assert sessions[0]["attention_summary"] == "1 boundary blocked · 1 repair ready · 1 branch ready · 2 debugger ready"
    assert sessions[0]["queue_draft"].startswith("Review the workflow queue for Session 2.")
    assert sessions[0]["handoff_draft"].startswith("Prepare a workflow handoff for Session 2.")
    assert sessions[0]["boundary_blocked_workflows"] == 1
    assert sessions[0]["repair_ready_workflows"] == 1
    assert sessions[0]["branch_ready_workflows"] == 1
    assert sessions[0]["output_debugger_ready_workflows"] == 2
    assert sessions[0]["lead_output_path"] == "notes/daily-brief.md"
    assert sessions[0]["lead_related_output_paths"] == ["notes/daily-brief-v2.md"]
    assert sessions[0]["lead_output_history"][0]["path"] == "notes/daily-brief-v2.md"
    assert sessions[0]["lead_latest_branch_run_identity"] == "session-2:workflow_daily_brief:branch-1"
    assert sessions[0]["lead_latest_branch_summary"] == "Branched repair draft completed"
    assert sessions[1]["thread_id"] == "session-1"
    assert sessions[1]["continue_message"] == "Resume repo review."
    assert sessions[1]["lead_step_focus"]["kind"] == "active"
    assert sessions[1]["queue_position"] == 2
    assert sessions[1]["queue_state"] == "approval_gate"
    assert sessions[1]["queue_reason"] == "1 workflow awaits approval before the session can advance."
    assert sessions[1]["attention_summary"] == "1 approval gate · 1 branch ready · 1 stalled"
    assert sessions[2]["thread_id"] is None
    assert sessions[2]["thread_label"] == "Ambient workflows"
    assert sessions[2]["lead_step_focus"]["kind"] == "active"
    assert sessions[2]["queue_position"] == 3
    assert sessions[2]["queue_state"] == "stalled"

    workflows = payload["workflows"]
    assert workflows[0]["workflow_name"] == "repo-review"
    assert workflows[0]["step_focus"]["kind"] == "active"
    assert workflows[0]["checkpoint_candidate_count"] == 1
    assert workflows[0]["is_long_running"] is True
    assert workflows[0]["is_compacted"] is True
    assert workflows[0]["step_count"] == 5
    assert workflows[0]["compacted_step_count"] == 2
    assert len(workflows[0]["step_records"]) == 3
    assert workflows[0]["step_records"][0]["id"] == "compare"
    assert "checkpoint_branch" in workflows[0]["preserved_recovery_paths"]
    assert "approval_gate" in workflows[0]["preserved_recovery_paths"]
    assert workflows[0]["recovery_density"]["recommended_path"] == "approval_gate"
    assert workflows[0]["recovery_density"]["branch_ready"] is True
    assert workflows[0]["output_debugger"]["primary_output_path"] == "notes/repo-review.md"
    assert workflows[0]["output_debugger"]["history_outputs"][0]["path"] == "notes/repo-review.md"
    assert workflows[0]["output_debugger"]["checkpoint_labels"] == ["collect"]
    assert workflows[1]["workflow_name"] == "daily-brief"
    assert workflows[1]["retry_from_step_available"] is True
    assert workflows[1]["step_focus"]["kind"] == "failure"
    assert workflows[1]["step_focus"]["recovery_action_count"] == 1
    assert workflows[1]["is_compacted"] is True
    assert len(workflows[1]["step_records"]) == 3
    assert workflows[1]["step_records"][0]["id"] == "outline"
    assert workflows[1]["state_capsule"].startswith("4 steps")
    assert "step_repair" in workflows[1]["preserved_recovery_paths"]
    assert "boundary_receipt" in workflows[1]["preserved_recovery_paths"]
    assert "approval_gate" not in workflows[1]["preserved_recovery_paths"]
    assert workflows[1]["recovery_density"]["recommended_path"] == "fresh_run"
    assert workflows[1]["recovery_density"]["repair_ready"] is True
    assert workflows[1]["recovery_density"]["repair_action_types"] == ["set_tool_policy"]
    assert workflows[1]["output_debugger"]["comparison_ready"] is True
    assert workflows[1]["output_debugger"]["related_output_paths"] == ["notes/daily-brief-v2.md"]
    assert workflows[1]["output_debugger"]["history_outputs"][0]["path"] == "notes/daily-brief-v2.md"
    assert workflows[1]["output_debugger"]["latest_branch_status"] == "succeeded"
    assert workflows[1]["output_debugger"]["latest_branch_run_identity"] == "session-2:workflow_daily_brief:branch-1"
    assert workflows[2]["workflow_name"] == "cleanup"
    assert workflows[2]["step_focus"]["kind"] == "active"
    assert workflows[2]["recovery_density"]["stalled"] is True


@pytest.mark.asyncio
async def test_operator_workflow_orchestration_surfaces_anticipatory_repair_and_backup_branch_choices(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-1",
                        "run_identity": "session-1:workflow_release_brief:1",
                        "root_run_identity": "session-1:workflow_release_brief:1",
                        "workflow_name": "release-brief",
                        "summary": "Preparing release publication.",
                        "status": "running",
                        "availability": "ready",
                        "thread_id": "session-1",
                        "thread_label": "Release thread",
                        "started_at": "2026-04-11T08:00:00Z",
                        "updated_at": "2026-04-11T08:45:00Z",
                        "thread_continue_message": "Continue release brief.",
                        "artifact_paths": ["notes/release-brief.md"],
                        "checkpoint_candidates": [
                            {
                                "step_id": "draft",
                                "label": "draft (write_file)",
                                "kind": "branch_from_checkpoint",
                                "status": "succeeded",
                                "resume_draft": 'Run workflow "release-brief" with _seraph_resume_from_step="draft".',
                                "resume_supported": True,
                            },
                        ],
                        "step_records": [
                            {"id": "scope", "index": 0, "tool": "session_search", "status": "succeeded"},
                            {"id": "collect", "index": 1, "tool": "web_search", "status": "succeeded"},
                            {"id": "draft", "index": 2, "tool": "write_file", "status": "succeeded"},
                            {"id": "review", "index": 3, "tool": "diff_compare", "status": "running"},
                        ],
                    },
                    {
                        "id": "run-2",
                        "run_identity": "session-1:workflow_release_brief:branch-1",
                        "root_run_identity": "session-1:workflow_release_brief:1",
                        "parent_run_identity": "session-1:workflow_release_brief:1",
                        "branch_kind": "branch_from_checkpoint",
                        "workflow_name": "release-brief",
                        "summary": "Earlier branch comparison completed.",
                        "status": "succeeded",
                        "availability": "ready",
                        "thread_id": "session-1",
                        "thread_label": "Release thread",
                        "started_at": "2026-04-11T08:10:00Z",
                        "updated_at": "2026-04-11T08:15:00Z",
                        "artifact_paths": ["notes/release-brief-branch.md"],
                        "step_records": [
                            {"id": "publish", "index": 0, "tool": "write_file", "status": "succeeded"},
                        ],
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Release thread"}]),
        ),
    ):
        resp = await client.get("/api/operator/workflow-orchestration", params={"limit_sessions": 6, "limit_workflows": 8})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["anticipatory_ready_workflows"] == 1
    assert payload["summary"]["backup_branch_ready_workflows"] == 1
    session = payload["sessions"][0]
    assert session["lead_anticipatory_risk_level"] in {"elevated", "high"}
    assert "backup branch" in session["lead_anticipatory_summary"]
    assert session["lead_backup_branch_label"] == "draft (write_file)"
    assert '_seraph_resume_from_step="draft"' in session["lead_backup_branch_draft"]
    assert session["lead_anticipatory_repair_draft"].startswith("Before continuing workflow")
    workflow = next(item for item in payload["workflows"] if item["run_identity"] == "session-1:workflow_release_brief:1")
    assert workflow["anticipatory_plan"]["backup_branch_ready"] is True
    assert workflow["anticipatory_plan"]["risk_level"] in {"elevated", "high"}
    assert workflow["condensation_fidelity"]["state"] == "partial"


@pytest.mark.asyncio
async def test_operator_background_sessions_surface_managed_processes_and_branch_handoff(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-branch",
                        "workflow_name": "repo-review",
                        "summary": "branch review ready for continuation",
                        "status": "running",
                        "started_at": "2026-03-20T10:00:00Z",
                        "updated_at": "2026-03-20T10:05:00Z",
                        "thread_id": "session-1",
                        "thread_label": "Atlas thread",
                        "thread_continue_message": "Continue Atlas branch review.",
                        "artifact_paths": ["notes/branch-review.md"],
                        "branch_kind": "branch_from_checkpoint",
                        "branch_depth": 1,
                        "parent_run_identity": "session-1:workflow_repo_review:root",
                        "root_run_identity": "session-1:workflow_repo_review:root",
                        "run_identity": "session-1:workflow_repo_review:branch-1",
                        "availability": "ready",
                        "pending_approval_count": 0,
                        "checkpoint_candidates": [
                            {
                                "step_id": "draft",
                                "label": "Draft review",
                                "kind": "branch_from_checkpoint",
                                "status": "succeeded",
                            }
                        ],
                        "step_records": [
                            {"id": "draft", "tool": "write_file", "status": "running"},
                        ],
                    },
                    {
                        "id": "run-blocked",
                        "workflow_name": "cleanup",
                        "summary": "cleanup blocked waiting on approval",
                        "status": "awaiting_approval",
                        "started_at": "2026-03-20T09:00:00Z",
                        "updated_at": "2026-03-20T09:02:00Z",
                        "thread_id": "session-2",
                        "thread_label": "Cleanup thread",
                        "thread_continue_message": "Resume cleanup after approval.",
                        "artifact_paths": [],
                        "branch_kind": None,
                        "branch_depth": 0,
                        "parent_run_identity": None,
                        "root_run_identity": "session-2:workflow_cleanup:root",
                        "run_identity": "session-2:workflow_cleanup:root",
                        "availability": "blocked",
                        "pending_approval_count": 1,
                        "checkpoint_candidates": [],
                        "step_records": [
                            {"id": "approve", "tool": "write_file", "status": "awaiting_approval"},
                        ],
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(
                return_value=[
                    {
                        "id": "session-1",
                        "title": "Atlas thread",
                        "last_message": "Please review the branch output.",
                        "updated_at": "2026-03-20T10:04:00Z",
                    },
                    {
                        "id": "session-2",
                        "title": "Cleanup thread",
                        "last_message": "Cleanup is waiting on approval.",
                        "updated_at": "2026-03-20T09:01:00Z",
                    },
                    {
                        "id": "session-3",
                        "title": "Idle thread",
                        "last_message": "No background work here.",
                        "updated_at": "2026-03-20T08:00:00Z",
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.process_runtime_manager.list_all_processes",
            return_value=[
                {
                    "process_id": "proc-1",
                    "pid": 1234,
                    "command": "/usr/bin/python3",
                    "args": ["worker.py"],
                    "cwd": "/workspace",
                    "status": "running",
                    "exit_code": None,
                    "started_at": "2026-03-20T10:03:00Z",
                    "output_path": "/tmp/proc-1.log",
                    "worker_root": "/tmp/seraph-runtime/workers/proc-1",
                    "worker_disposable": True,
                    "trust_partition": "session_disposable_worker",
                    "session_scoped": True,
                    "session_id": "session-1",
                },
                {
                    "process_id": "proc-2",
                    "pid": 1235,
                    "command": "git",
                    "args": ["status"],
                    "cwd": "/workspace",
                    "status": "exited",
                    "exit_code": 0,
                    "started_at": "2026-03-20T09:03:00Z",
                    "output_path": "/tmp/proc-2.log",
                    "worker_root": "/tmp/seraph-runtime/workers/proc-2",
                    "worker_disposable": True,
                    "trust_partition": "session_disposable_worker",
                    "session_scoped": True,
                    "session_id": "session-2",
                },
            ],
        ),
    ):
        resp = await client.get(
            "/api/operator/background-sessions",
            params={"limit_sessions": 6, "limit_processes": 2},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["tracked_sessions"] == 2
    assert payload["summary"]["background_process_count"] == 2
    assert payload["summary"]["running_background_process_count"] == 1
    assert payload["summary"]["sessions_with_branch_handoff"] == 2
    assert payload["summary"]["sessions_with_active_workflows"] == 2

    first = payload["sessions"][0]
    assert first["session_id"] == "session-1"
    assert first["title"] == "Atlas thread"
    assert first["background_process_count"] == 1
    assert first["running_background_process_count"] == 1
    assert first["workflow_count"] == 1
    assert first["lead_workflow_name"] == "repo-review"
    assert first["continue_message"] == "Continue Atlas branch review."
    assert first["branch_handoff_available"] is True
    assert first["branch_handoff"]["target_type"] == "workflow_branch"
    assert first["branch_handoff"]["workflow_name"] == "repo-review"
    assert first["branch_handoff"]["artifact_paths"] == ["notes/branch-review.md"]
    assert first["branch_handoff"]["trust_partition"]["kind"] == "branch_handoff"
    assert first["branch_handoff"]["trust_partition"]["session_bound"] is True
    assert first["lead_process"]["process_id"] == "proc-1"
    assert first["lead_process"]["worker_disposable"] is True
    assert first["lead_process"]["trust_partition"] == "session_disposable_worker"
    assert first["background_processes"][0]["session_id"] == "session-1"
    assert first["background_processes"][0]["worker_disposable"] is True
    assert first["trust_partition"]["background_process_partitioned"] is True
    assert first["trust_partition"]["lead_process_disposable"] is True

    second = payload["sessions"][1]
    assert second["session_id"] == "session-2"
    assert second["blocked_workflows"] == 1
    assert second["branch_handoff"]["target_type"] == "workflow_run"
    assert second["continue_message"] == "Resume cleanup after approval."


@pytest.mark.asyncio
async def test_operator_m5_operating_layer_surfaces_jobs_runs_workflows_and_delegation(client):
    with (
        patch(
            "src.api.operator.scheduled_job_repository.list_jobs",
            AsyncMock(
                return_value=[
                    {
                        "id": "job-brief",
                        "name": "Morning brief",
                        "enabled": True,
                        "trigger_type": "cron",
                        "trigger_spec": {"cron": "0 9 * * *", "timezone": "UTC"},
                        "action_type": "deliver_message",
                        "action_spec": {"content": "Review priorities", "intervention_type": "advisory", "urgency": 3},
                        "session_id": "session-1",
                        "created_by_session_id": "session-1",
                        "last_run_at": "2026-05-05T09:00:00+00:00",
                        "last_outcome": "delivered",
                    },
                    {
                        "id": "job-workflow",
                        "name": "Release routine",
                        "enabled": False,
                        "trigger_type": "cron",
                        "trigger_spec": {"cron": "0 13 * * 1", "timezone": "UTC"},
                        "action_type": "run_workflow",
                        "action_spec": {"workflow_name": "release-check", "workflow_args": {"project": "Seraph"}},
                        "session_id": "session-2",
                        "created_by_session_id": "session-2",
                        "last_run_at": "2026-05-05T13:00:00+00:00",
                        "last_outcome": "skipped_disabled",
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.scheduled_job_repository.list_run_history",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-brief",
                        "scheduled_job_id": "job-brief",
                        "job_name": "Morning brief",
                        "trigger_type": "cron",
                        "action_type": "deliver_message",
                        "session_id": "session-1",
                        "created_by_session_id": "session-1",
                        "status": "finished",
                        "outcome": "delivered",
                        "error": None,
                        "approval_id": None,
                        "started_at": "2026-05-05T09:00:00+00:00",
                        "finished_at": "2026-05-05T09:00:01+00:00",
                        "metadata": {"delivery_outcome": "delivered"},
                    },
                    {
                        "id": "run-paused",
                        "scheduled_job_id": "job-workflow",
                        "job_name": "Release routine",
                        "trigger_type": "cron",
                        "action_type": "run_workflow",
                        "session_id": "session-2",
                        "created_by_session_id": "session-2",
                        "status": "skipped",
                        "outcome": "skipped_disabled",
                        "error": None,
                        "approval_id": None,
                        "started_at": "2026-05-05T13:00:00+00:00",
                        "finished_at": "2026-05-05T13:00:00+00:00",
                        "metadata": {"skip_reason": "job_disabled"},
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "run_identity": "session-1:workflow_release:root",
                        "root_run_identity": "session-1:workflow_release:root",
                        "workflow_name": "release-check",
                        "status": "awaiting_approval",
                        "availability": "blocked",
                        "thread_id": "session-1",
                        "branch_kind": "branch_from_checkpoint",
                        "branch_depth": 1,
                        "checkpoint_candidates": [{"step_id": "draft"}],
                        "retry_from_step_draft": "Retry from draft.",
                        "replay_allowed": False,
                        "replay_block_reason": "approval_context_changed",
                        "pending_approval_count": 1,
                        "approval_context": {
                            "delegated_specialists": ["workflow_runner"],
                            "delegated_tool_names": ["write_file"],
                            "trust_partition": {"mode": "delegated_specialist", "blocked": False},
                        },
                        "step_records": [{"id": "draft", "status": "awaiting_approval", "is_recoverable": True}],
                    }
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Release", "updated_at": "2026-05-05T09:01:00Z"}]),
        ),
        patch("src.api.operator.process_runtime_manager.list_all_processes", return_value=[]),
    ):
        resp = await client.get("/api/operator/m5-operating-layer")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["routine_count"] == 2
    assert payload["summary"]["scheduled_job_run_count"] == 2
    assert payload["routines"][1]["latest_run"]["outcome"] == "skipped_disabled"
    assert payload["workflows"][0]["claim_boundary"] == "audit_projected_workflow_receipt_not_durable_state_machine"
    assert payload["workflows"][0]["delegation_receipt"]["delegation_present"] is True
    assert payload["claim_boundaries"]["implemented_triggers"] == ["cron"]
    assert "heartbeat" in payload["claim_boundaries"]["future_triggers"]
    assert "full_durable_workflow_state_machine" in payload["claim_boundaries"]["not_claimed"]


@pytest.mark.asyncio
async def test_operator_workflow_orchestration_uses_most_recent_branch_for_debugger(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-root",
                        "run_identity": "session-1:workflow_repo_review:1",
                        "root_run_identity": "session-1:workflow_repo_review:1",
                        "workflow_name": "repo-review",
                        "summary": "Root review failed",
                        "status": "failed",
                        "availability": "blocked",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "started_at": "2026-04-08T10:00:00Z",
                        "updated_at": "2026-04-08T10:05:00Z",
                        "artifact_paths": ["notes/repo-review.md"],
                        "step_records": [
                            {
                                "id": "draft",
                                "index": 0,
                                "tool": "write_file",
                                "status": "failed",
                                "recovery_actions": [{"type": "set_tool_policy"}],
                                "is_recoverable": True,
                            }
                        ],
                    },
                    {
                        "id": "run-branch-old",
                        "run_identity": "session-1:workflow_repo_review:branch-old",
                        "root_run_identity": "session-1:workflow_repo_review:1",
                        "parent_run_identity": "session-1:workflow_repo_review:1",
                        "branch_kind": "branch_from_checkpoint",
                        "workflow_name": "repo-review",
                        "summary": "Older blocked branch",
                        "status": "failed",
                        "availability": "blocked",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "started_at": "2026-04-08T10:06:00Z",
                        "updated_at": "2026-04-08T10:07:00Z",
                        "artifact_paths": ["notes/repo-review-old-branch.md"],
                        "step_records": [
                            {"id": "repair", "index": 0, "tool": "write_file", "status": "failed"},
                        ],
                    },
                    {
                        "id": "run-branch-new",
                        "run_identity": "session-1:workflow_repo_review:branch-new",
                        "root_run_identity": "session-1:workflow_repo_review:1",
                        "parent_run_identity": "session-1:workflow_repo_review:1",
                        "branch_kind": "branch_from_checkpoint",
                        "workflow_name": "repo-review",
                        "summary": "Newest successful branch",
                        "status": "succeeded",
                        "availability": "ready",
                        "thread_id": "session-1",
                        "thread_label": "Session 1",
                        "started_at": "2026-04-08T10:08:00Z",
                        "updated_at": "2026-04-08T10:09:00Z",
                        "artifact_paths": ["notes/repo-review-new-branch.md"],
                        "step_records": [
                            {"id": "publish", "index": 0, "tool": "write_file", "status": "succeeded"},
                        ],
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Session 1"}]),
        ),
    ):
        resp = await client.get(
            "/api/operator/workflow-orchestration",
            params={"limit_sessions": 4, "limit_workflows": 4},
        )

    assert resp.status_code == 200
    payload = resp.json()
    root_workflow = payload["workflows"][0]
    assert root_workflow["output_debugger"]["latest_branch_run_identity"] == "session-1:workflow_repo_review:branch-new"
    assert root_workflow["output_debugger"]["latest_branch_summary"] == "Newest successful branch"
    assert root_workflow["output_debugger"]["latest_branch_output_path"] == "notes/repo-review-new-branch.md"


@pytest.mark.asyncio
async def test_operator_workflow_orchestration_attention_sessions_counts_full_population(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-a",
                        "run_identity": "session-a:workflow_a:1",
                        "workflow_name": "workflow-a",
                        "summary": "Awaiting approval",
                        "status": "awaiting_approval",
                        "availability": "blocked",
                        "thread_id": "session-a",
                        "thread_label": "Session A",
                        "started_at": "2026-04-08T10:00:00Z",
                        "updated_at": "2026-04-08T10:05:00Z",
                        "pending_approval_count": 1,
                        "step_records": [],
                    },
                    {
                        "id": "run-b",
                        "run_identity": "session-b:workflow_b:1",
                        "workflow_name": "workflow-b",
                        "summary": "Repair ready",
                        "status": "failed",
                        "availability": "blocked",
                        "thread_id": "session-b",
                        "thread_label": "Session B",
                        "started_at": "2026-04-08T09:00:00Z",
                        "updated_at": "2026-04-08T09:05:00Z",
                        "step_records": [
                            {
                                "id": "publish",
                                "index": 0,
                                "tool": "write_file",
                                "status": "failed",
                                "recovery_actions": [{"type": "set_tool_policy"}],
                                "is_recoverable": True,
                            }
                        ],
                    },
                    {
                        "id": "run-c",
                        "run_identity": "session-c:workflow_c:1",
                        "workflow_name": "workflow-c",
                        "summary": "Stalled run",
                        "status": "running",
                        "availability": "ready",
                        "thread_id": "session-c",
                        "thread_label": "Session C",
                        "started_at": "2026-04-08T07:00:00Z",
                        "updated_at": "2026-04-08T07:05:00Z",
                        "step_records": [
                            {"id": "scan", "index": 0, "tool": "filesystem_read", "status": "running"},
                        ],
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(
                return_value=[
                    {"id": "session-a", "title": "Session A"},
                    {"id": "session-b", "title": "Session B"},
                    {"id": "session-c", "title": "Session C"},
                ]
            ),
        ),
    ):
        resp = await client.get(
            "/api/operator/workflow-orchestration",
            params={"limit_sessions": 1, "limit_workflows": 4},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["attention_sessions"] == 3
    assert len(payload["sessions"]) == 1


@pytest.mark.asyncio
async def test_operator_engineering_memory_groups_repo_and_pr_bundles(client):
    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-pr",
                        "workflow_name": "repo-review",
                        "summary": "Review seraph-quest/seraph/pull/390 before merge.",
                        "status": "running",
                        "started_at": "2026-04-10T10:00:00Z",
                        "updated_at": "2026-04-10T10:05:00Z",
                        "thread_id": "session-1",
                        "thread_label": "PR review thread",
                        "thread_continue_message": "Continue review for seraph-quest/seraph/pull/390.",
                        "artifact_paths": ["notes/pr-390-review.md"],
                    },
                    {
                        "id": "run-repo",
                        "workflow_name": "planning",
                        "summary": "Refresh roadmap for seraph-quest/seraph.",
                        "status": "completed",
                        "started_at": "2026-04-09T09:00:00Z",
                        "updated_at": "2026-04-09T09:15:00Z",
                        "thread_id": "session-2",
                        "thread_label": "Roadmap thread",
                        "thread_continue_message": "Continue roadmap refresh for seraph-quest/seraph.",
                        "artifact_paths": ["notes/roadmap-refresh.md"],
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
                        "tool_name": "execute_source_mutation",
                        "summary": "Publish review receipt to PR 390.",
                        "risk_level": "high",
                        "created_at": "2026-04-10T10:03:00Z",
                        "thread_id": "session-1",
                        "thread_label": "PR review thread",
                        "resume_message": "Continue PR review publication.",
                        "approval_scope": {
                            "target": {
                                "reference": "seraph-quest/seraph/pull/390",
                            }
                        },
                    }
                ]
            ),
        ),
        patch(
            "src.api.operator.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-pr",
                        "event_type": "authenticated_source_mutation",
                        "tool_name": "add_review_to_pr",
                        "summary": "Published review receipt to seraph-quest/seraph/pull/390.",
                        "created_at": "2026-04-10T10:04:00Z",
                        "session_id": "session-1",
                        "details": {
                            "target_reference": "seraph-quest/seraph/pull/390",
                        },
                    },
                    {
                        "id": "audit-repo",
                        "event_type": "authenticated_source_mutation",
                        "tool_name": "create_pull_request",
                        "summary": "Opened planning PR from seraph-quest/seraph.",
                        "created_at": "2026-04-09T09:16:00Z",
                        "session_id": "session-2",
                        "details": {
                            "target_reference": "seraph-quest/seraph",
                        },
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.search_sessions",
            AsyncMock(
                return_value=[
                    {
                        "session_id": "session-1",
                        "title": "PR review thread",
                        "matched_at": "2026-04-10T10:02:00Z",
                        "snippet": "Need to finish seraph-quest/seraph/pull/390 review and publish the receipt.",
                        "source": "message",
                    },
                    {
                        "session_id": "session-2",
                        "title": "Roadmap thread",
                        "matched_at": "2026-04-09T09:10:00Z",
                        "snippet": "Planning work for seraph-quest/seraph roadmap and next batch.",
                        "source": "message",
                    },
                ]
            ),
        ),
    ):
        resp = await client.get(
            "/api/operator/engineering-memory",
            params={"q": "seraph", "limit_bundles": 6, "limit_session_matches": 3},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["query"] == "seraph"
    assert payload["summary"]["tracked_bundles"] == 2
    assert payload["summary"]["pull_request_bundle_count"] == 1
    assert payload["summary"]["repository_bundle_count"] == 1
    assert payload["summary"]["search_match_count"] == 2
    assert len(payload["search_matches"]) == 2

    first = payload["bundles"][0]
    assert first["reference"] == "seraph-quest/seraph/pull/390"
    assert first["target_kind"] == "pull_request"
    assert first["workflow_count"] == 1
    assert first["approval_count"] == 1
    assert first["audit_event_count"] == 1
    assert first["session_match_count"] == 1
    assert first["thread_ids"] == ["session-1"]
    assert first["thread_labels"] == ["PR review thread"]
    assert first["continue_message"] == "Continue review for seraph-quest/seraph/pull/390."
    assert first["artifact_paths"] == ["notes/pr-390-review.md"]
    assert first["session_matches"][0]["session_id"] == "session-1"
    assert first["review_receipts"][0]["source_kind"] == "audit"

    second = payload["bundles"][1]
    assert second["reference"] == "seraph-quest/seraph"
    assert second["target_kind"] == "repository"
    assert second["workflow_count"] == 1
    assert second["audit_event_count"] == 1
    assert second["session_match_count"] == 1
    assert second["thread_ids"] == ["session-2"]
    assert second["artifact_paths"] == ["notes/roadmap-refresh.md"]


@pytest.mark.asyncio
async def test_operator_continuity_graph_links_sessions_workflows_artifacts_and_notifications(client):
    intervention_1 = SimpleNamespace(
        id="intervention-1",
        session_id="session-1",
        intervention_type="alert",
        content_excerpt="Atlas branch review is waiting.",
        updated_at="2026-04-10T10:06:00Z",
        latest_outcome="notification_acked",
        transport="native_notification",
        policy_action="act",
    )
    intervention_2 = SimpleNamespace(
        id="intervention-2",
        session_id="session-2",
        intervention_type="advisory",
        content_excerpt="Bundle the roadmap follow-up.",
        updated_at="2026-04-10T09:12:00Z",
        latest_outcome="queued",
        transport=None,
        policy_action="bundle",
    )

    with (
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(
                return_value=[
                    {
                        "id": "session-1",
                        "title": "Atlas thread",
                        "last_message": "Please review the branch output.",
                        "updated_at": "2026-04-10T10:05:00Z",
                    },
                    {
                        "id": "session-2",
                        "title": "Roadmap thread",
                        "last_message": "Bundle the roadmap follow-up.",
                        "updated_at": "2026-04-10T09:10:00Z",
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-1",
                        "workflow_name": "repo-review",
                        "summary": "Review Atlas branch output before publish.",
                        "status": "running",
                        "started_at": "2026-04-10T10:00:00Z",
                        "updated_at": "2026-04-10T10:04:00Z",
                        "thread_id": "session-1",
                        "thread_label": "Atlas thread",
                        "thread_continue_message": "Continue Atlas branch review.",
                        "artifact_paths": ["notes/atlas-review.md"],
                        "run_identity": "session-1:repo-review:atlas",
                        "branch_kind": "recovery_branch",
                        "availability": "ready",
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
                        "tool_name": "execute_source_mutation",
                        "summary": "Publish Atlas review receipt.",
                        "created_at": "2026-04-10T10:03:00Z",
                        "thread_id": "session-1",
                        "thread_label": "Atlas thread",
                        "resume_message": "Resume Atlas publication after approval.",
                        "risk_level": "high",
                        "approval_scope": {
                            "target": {
                                "reference": "seraph-quest/seraph/pull/390",
                            }
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
                        thread_id="session-1",
                        title="Atlas review alert",
                        body="Atlas branch review is waiting.",
                        intervention_type="alert",
                        urgency=4,
                        resume_message="Continue from Atlas notification.",
                        created_at="2026-04-10T10:05:30Z",
                        intervention_id="intervention-1",
                        continuation_mode="resume_thread",
                        thread_source="session",
                    )
                ]
            ),
        ),
        patch(
            "src.api.operator.insight_queue.peek_all",
            AsyncMock(
                return_value=[
                    SimpleNamespace(
                        id="queued-1",
                        session_id="session-2",
                        intervention_id="intervention-2",
                        intervention_type="advisory",
                        content="Bundle the roadmap follow-up.",
                        created_at="2026-04-10T09:11:00Z",
                        reasoning="high_interruption_cost",
                    )
                ]
            ),
        ),
        patch(
            "src.api.operator.guardian_feedback_repository.list_recent",
            AsyncMock(return_value=[intervention_1, intervention_2]),
        ),
        patch(
            "src.api.operator.build_observer_continuity_snapshot",
            AsyncMock(
                return_value={
                    "summary": {
                        "continuity_health": "attention",
                        "primary_surface": "native_notification",
                        "recommended_focus": "Atlas thread",
                    },
                    "threads": [
                        {
                            "thread_id": "session-1",
                            "thread_label": "Atlas thread",
                            "summary": "Atlas branch review is waiting.",
                            "latest_updated_at": "2026-04-10T10:06:00Z",
                            "continue_message": "Continue Atlas branch review.",
                            "pending_notification_count": 1,
                            "queued_insight_count": 0,
                            "recent_intervention_count": 1,
                            "item_count": 3,
                            "primary_surface": "native_notification",
                            "continuity_surface": "native_notification",
                        },
                        {
                            "thread_id": "session-2",
                            "thread_label": "Roadmap thread",
                            "summary": "Bundle the roadmap follow-up.",
                            "latest_updated_at": "2026-04-10T09:12:00Z",
                            "continue_message": "Follow up on this deferred guardian item: Bundle the roadmap follow-up.",
                            "pending_notification_count": 0,
                            "queued_insight_count": 1,
                            "recent_intervention_count": 1,
                            "item_count": 2,
                            "primary_surface": "bundle_queue",
                            "continuity_surface": "bundle_queue",
                        },
                    ],
                }
            ),
        ),
    ):
        resp = await client.get("/api/operator/continuity-graph", params={"limit_sessions": 6})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["continuity_health"] == "attention"
    assert payload["summary"]["tracked_sessions"] == 2
    assert payload["summary"]["workflow_count"] == 1
    assert payload["summary"]["approval_count"] == 1
    assert payload["summary"]["notification_count"] == 1
    assert payload["summary"]["queued_insight_count"] == 1
    assert payload["summary"]["intervention_count"] == 2
    assert payload["summary"]["artifact_count"] == 1

    atlas_session = next(item for item in payload["sessions"] if item["thread_id"] == "session-1")
    assert atlas_session["metadata"]["workflow_count"] == 1
    assert atlas_session["metadata"]["approval_count"] == 1
    assert atlas_session["metadata"]["notification_count"] == 1
    assert atlas_session["metadata"]["artifact_count"] == 1
    assert atlas_session["continue_message"] == "Continue Atlas branch review."

    edge_kinds = {(item["kind"], item["source_id"], item["target_id"]) for item in payload["edges"]}
    assert ("session_workflow", "session:session-1", "workflow:run-1") in edge_kinds
    assert ("workflow_artifact", "workflow:run-1", "artifact:notes/atlas-review.md") in edge_kinds
    assert ("session_approval", "session:session-1", "approval:approval-1") in edge_kinds
    assert ("session_notification", "session:session-1", "notification:note-1") in edge_kinds
    assert ("notification_intervention", "notification:note-1", "intervention:intervention-1") in edge_kinds
    assert ("queued_intervention", "queued:queued-1", "intervention:intervention-2") in edge_kinds


@pytest.mark.asyncio
async def test_operator_engineering_memory_applies_window_and_reports_total_bundle_counts(client):
    now = datetime.now(timezone.utc)
    fresh_pr_started_at = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    fresh_pr_updated_at = (now - timedelta(hours=1, minutes=56)).isoformat().replace("+00:00", "Z")
    fresh_repo_started_at = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    fresh_repo_updated_at = (now - timedelta(hours=2, minutes=45)).isoformat().replace("+00:00", "Z")
    stale_started_at = (now - timedelta(hours=49)).isoformat().replace("+00:00", "Z")
    stale_updated_at = (now - timedelta(hours=48, minutes=55)).isoformat().replace("+00:00", "Z")
    fresh_approval_created_at = (now - timedelta(hours=1, minutes=57)).isoformat().replace("+00:00", "Z")
    stale_approval_created_at = (now - timedelta(hours=49, minutes=55)).isoformat().replace("+00:00", "Z")
    fresh_pr_audit_created_at = (now - timedelta(hours=1, minutes=56)).isoformat().replace("+00:00", "Z")
    fresh_repo_audit_created_at = (now - timedelta(hours=2, minutes=44)).isoformat().replace("+00:00", "Z")
    fresh_work_item_audit_created_at = (now - timedelta(hours=1, minutes=54)).isoformat().replace("+00:00", "Z")
    fresh_pr_matched_at = (now - timedelta(hours=1, minutes=58)).isoformat().replace("+00:00", "Z")
    fresh_repo_matched_at = (now - timedelta(hours=2, minutes=50)).isoformat().replace("+00:00", "Z")
    fresh_work_item_matched_at = (now - timedelta(hours=1, minutes=59)).isoformat().replace("+00:00", "Z")
    stale_matched_at = (now - timedelta(hours=49, minutes=50)).isoformat().replace("+00:00", "Z")

    with (
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-pr",
                        "workflow_name": "repo-review",
                        "summary": "Review seraph-quest/seraph/pull/390 before publish.",
                        "status": "running",
                        "started_at": fresh_pr_started_at,
                        "updated_at": fresh_pr_updated_at,
                        "thread_id": "session-1",
                        "thread_label": "PR review thread",
                        "thread_continue_message": "Continue review for seraph-quest/seraph/pull/390.",
                        "artifact_paths": ["notes/pr-390-review.md"],
                    },
                    {
                        "id": "run-repo",
                        "workflow_name": "roadmap-refresh",
                        "summary": "Planning work for seraph-quest/seraph roadmap.",
                        "status": "running",
                        "started_at": fresh_repo_started_at,
                        "updated_at": fresh_repo_updated_at,
                        "thread_id": "session-2",
                        "thread_label": "Roadmap thread",
                        "thread_continue_message": "Continue seraph-quest/seraph roadmap refresh.",
                        "artifact_paths": ["notes/roadmap-refresh.md"],
                    },
                    {
                        "id": "run-stale",
                        "workflow_name": "old-review",
                        "summary": "Stale follow-up for seraph-quest/seraph#12 should not appear.",
                        "status": "completed",
                        "started_at": stale_started_at,
                        "updated_at": stale_updated_at,
                        "thread_id": "session-stale",
                        "thread_label": "Stale thread",
                        "thread_continue_message": "Old follow-up",
                        "artifact_paths": [],
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.approval_repository.list_pending",
            AsyncMock(
                return_value=[
                    {
                        "id": "approval-work-item",
                        "tool_name": "execute_source_mutation",
                        "summary": "Publish receipt to seraph-quest/seraph#12.",
                        "created_at": fresh_approval_created_at,
                        "thread_id": "session-3",
                        "thread_label": "Issue thread",
                        "resume_message": "Resume seraph-quest/seraph#12 publication.",
                        "risk_level": "high",
                        "approval_scope": {
                            "target": {
                                "reference": "seraph-quest/seraph#12",
                            }
                        },
                    },
                    {
                        "id": "approval-stale",
                        "tool_name": "execute_source_mutation",
                        "summary": "Old stale approval for seraph-quest/seraph#77.",
                        "created_at": stale_approval_created_at,
                        "thread_id": "session-stale",
                        "thread_label": "Stale thread",
                        "resume_message": "Ignore stale approval",
                        "risk_level": "medium",
                        "approval_scope": {
                            "target": {
                                "reference": "seraph-quest/seraph#77",
                            }
                        },
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-pr",
                        "event_type": "authenticated_source_mutation",
                        "tool_name": "add_review_to_pr",
                        "summary": "Published review receipt to seraph-quest/seraph/pull/390.",
                        "created_at": fresh_pr_audit_created_at,
                        "session_id": "session-1",
                        "details": {
                            "target_reference": "seraph-quest/seraph/pull/390",
                        },
                    },
                    {
                        "id": "audit-repo",
                        "event_type": "authenticated_source_mutation",
                        "tool_name": "create_pull_request",
                        "summary": "Opened planning PR from seraph-quest/seraph.",
                        "created_at": fresh_repo_audit_created_at,
                        "session_id": "session-2",
                        "details": {
                            "target_reference": "seraph-quest/seraph",
                        },
                    },
                    {
                        "id": "audit-work-item",
                        "event_type": "authenticated_source_mutation",
                        "tool_name": "reply_to_issue",
                        "summary": "Posted follow-up to seraph-quest/seraph#12.",
                        "created_at": fresh_work_item_audit_created_at,
                        "session_id": "session-3",
                        "details": {
                            "target_reference": "seraph-quest/seraph#12",
                        },
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator.session_manager.search_sessions",
            AsyncMock(
                return_value=[
                    {
                        "session_id": "session-1",
                        "title": "PR review thread",
                        "matched_at": fresh_pr_matched_at,
                        "snippet": "Need to finish seraph-quest/seraph/pull/390 review and publish the receipt.",
                        "source": "message",
                    },
                    {
                        "session_id": "session-2",
                        "title": "Roadmap thread",
                        "matched_at": fresh_repo_matched_at,
                        "snippet": "Planning work for seraph-quest/seraph roadmap and next batch.",
                        "source": "message",
                    },
                    {
                        "session_id": "session-3",
                        "title": "Issue thread",
                        "matched_at": fresh_work_item_matched_at,
                        "snippet": "Need to follow up on seraph-quest/seraph#12.",
                        "source": "message",
                    },
                    {
                        "session_id": "session-stale",
                        "title": "Stale thread",
                        "matched_at": stale_matched_at,
                        "snippet": "Old note for seraph-quest/seraph#77.",
                        "source": "message",
                    },
                ]
            ),
        ),
    ):
        resp = await client.get(
            "/api/operator/engineering-memory",
            params={"q": "seraph", "limit_bundles": 2, "limit_session_matches": 3, "window_hours": 24},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["tracked_bundles"] == 3
    assert payload["summary"]["pull_request_bundle_count"] == 1
    assert payload["summary"]["repository_bundle_count"] == 1
    assert payload["summary"]["work_item_bundle_count"] == 1
    assert payload["summary"]["search_match_count"] == 3
    assert len(payload["bundles"]) == 2
    assert len(payload["search_matches"]) == 3
    assert all(bundle["reference"] != "seraph-quest/seraph#77" for bundle in payload["bundles"])


@pytest.mark.asyncio
async def test_operator_continuity_graph_preserves_cross_session_and_inferred_intervention_edges(client):
    intervention_cross_session = SimpleNamespace(
        id="intervention-cross",
        session_id="session-2",
        intervention_type="advisory",
        content_excerpt="Bundle the roadmap follow-up.",
        updated_at="2026-04-10T09:12:00Z",
        latest_outcome="queued",
        transport=None,
        policy_action="bundle",
    )

    with (
        patch(
            "src.api.operator.session_manager.list_sessions",
            AsyncMock(
                return_value=[
                    {
                        "id": "session-1",
                        "title": "Atlas thread",
                        "last_message": "Please review the branch output.",
                        "updated_at": "2026-04-10T10:05:00Z",
                    },
                    {
                        "id": "session-2",
                        "title": "Roadmap thread",
                        "last_message": "Bundle the roadmap follow-up.",
                        "updated_at": "2026-04-10T09:10:00Z",
                    },
                ]
            ),
        ),
        patch(
            "src.api.operator._list_workflow_runs",
            AsyncMock(
                return_value=[
                    {
                        "id": "run-1",
                        "workflow_name": "repo-review",
                        "summary": "Review Atlas branch output before publish.",
                        "status": "running",
                        "started_at": "2026-04-10T10:00:00Z",
                        "updated_at": "2026-04-10T10:04:00Z",
                        "thread_id": "session-1",
                        "thread_label": "Atlas thread",
                        "thread_continue_message": "Continue Atlas branch review.",
                        "artifact_paths": ["notes/atlas-review.md"],
                        "run_identity": "session-1:repo-review:atlas",
                        "branch_kind": "recovery_branch",
                        "availability": "ready",
                    }
                ]
            ),
        ),
        patch("src.api.operator.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch(
            "src.api.operator.native_notification_queue.list",
            AsyncMock(
                return_value=[
                    SimpleNamespace(
                        id="note-1",
                        session_id="session-1",
                        thread_id="session-1",
                        title="Atlas review alert",
                        body="Atlas branch review is waiting.",
                        intervention_type="alert",
                        urgency=4,
                        resume_message="Continue from Atlas notification.",
                        created_at="2026-04-10T10:05:30Z",
                        intervention_id="intervention-cross",
                        continuation_mode="resume_thread",
                        thread_source="session",
                    ),
                    SimpleNamespace(
                        id="note-2",
                        session_id="session-1",
                        thread_id="session-1",
                        title="Atlas inferred alert",
                        body="Atlas follow-up is waiting.",
                        intervention_type="alert",
                        urgency=3,
                        resume_message="Continue from Atlas inferred notification.",
                        created_at="2026-04-10T10:05:40Z",
                        intervention_id="intervention-missing",
                        continuation_mode="resume_thread",
                        thread_source="session",
                    ),
                ]
            ),
        ),
        patch("src.api.operator.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch(
            "src.api.operator.guardian_feedback_repository.list_recent",
            AsyncMock(return_value=[intervention_cross_session]),
        ),
        patch(
            "src.api.operator.build_observer_continuity_snapshot",
            AsyncMock(
                return_value={
                    "summary": {
                        "continuity_health": "attention",
                        "primary_surface": "native_notification",
                        "recommended_focus": "Atlas thread",
                    },
                    "threads": [
                        {
                            "thread_id": "session-1",
                            "thread_label": "Atlas thread",
                            "summary": "Atlas branch review is waiting.",
                            "latest_updated_at": "2026-04-10T10:06:00Z",
                            "continue_message": "Continue Atlas branch review.",
                            "pending_notification_count": 2,
                            "queued_insight_count": 0,
                            "recent_intervention_count": 0,
                            "item_count": 3,
                            "primary_surface": "native_notification",
                            "continuity_surface": "native_notification",
                        },
                        {
                            "thread_id": "session-2",
                            "thread_label": "Roadmap thread",
                            "summary": "Bundle the roadmap follow-up.",
                            "latest_updated_at": "2026-04-10T09:12:00Z",
                            "continue_message": "Bundle the roadmap follow-up.",
                            "pending_notification_count": 0,
                            "queued_insight_count": 0,
                            "recent_intervention_count": 1,
                            "item_count": 1,
                            "primary_surface": "bundle_queue",
                            "continuity_surface": "bundle_queue",
                        },
                    ],
                }
            ),
        ),
    ):
        resp = await client.get("/api/operator/continuity-graph", params={"limit_sessions": 1})

    assert resp.status_code == 200
    payload = resp.json()
    edge_kinds = {(item["kind"], item["source_id"], item["target_id"]) for item in payload["edges"]}
    assert ("notification_intervention", "notification:note-1", "intervention:intervention-cross") in edge_kinds
    assert ("notification_intervention", "notification:note-2", "intervention:intervention-missing") in edge_kinds

    inferred = next(item for item in payload["nodes"] if item["id"] == "intervention:intervention-missing")
    assert inferred["metadata"]["missing_recent_context"] is True
    assert inferred["metadata"]["inferred_from"] == "notification"


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
                            "selected_preference_score": 10.5,
                            "selected_capability_gap_count": 0,
                            "selected_live_feedback_penalty": 3.5,
                            "selected_route_score": 10.5,
                            "selected_failure_risk_score": 3.5,
                            "selected_production_readiness": "guarded",
                            "selected_live_feedback": {
                                "feedback_state": "recovering",
                                "recent_failure_count": 1,
                            },
                            "selection_policy_mode": "highest_ranked_attemptable",
                            "planning_winner_model": "openrouter/gpt-4o-mini",
                            "planning_winner_profile": "balanced",
                            "planning_winner_source": "primary",
                            "planning_winner_selected": True,
                            "best_alternate_model": "gpt-4.1-mini",
                            "best_alternate_profile": "balanced",
                            "best_alternate_source": "fallback",
                            "best_alternate_route_score": 7.0,
                            "selected_vs_best_alternate_margin": 3.5,
                            "attempt_order": ["gpt-4o-mini", "gpt-4.1-mini"],
                            "reroute_cause": "primary_timeout",
                            "rerouted_from_unhealthy_primary": False,
                            "rerouted_from_policy_guardrails": True,
                            "guardrail_compliant_targets_present": True,
                            "route_explanation": "selected openrouter/gpt-4o-mini; readiness=guarded; failure_risk=3.5; rejected=2",
                            "route_comparison_summary": "selected openrouter/gpt-4o-mini over gpt-4.1-mini by planning_score margin 3.5",
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
    assert routing_item["metadata"]["selected_preference_score"] == 10.5
    assert routing_item["metadata"]["selected_capability_gap_count"] == 0
    assert routing_item["metadata"]["selected_live_feedback_penalty"] == 3.5
    assert routing_item["metadata"]["selected_route_score"] == 10.5
    assert routing_item["metadata"]["selected_failure_risk_score"] == 3.5
    assert routing_item["metadata"]["selected_production_readiness"] == "guarded"
    assert routing_item["metadata"]["selected_live_feedback"]["feedback_state"] == "recovering"
    assert routing_item["metadata"]["selection_policy_mode"] == "highest_ranked_attemptable"
    assert routing_item["metadata"]["planning_winner_model"] == "openrouter/gpt-4o-mini"
    assert routing_item["metadata"]["planning_winner_profile"] == "balanced"
    assert routing_item["metadata"]["planning_winner_source"] == "primary"
    assert routing_item["metadata"]["planning_winner_selected"] is True
    assert routing_item["metadata"]["best_alternate_model"] == "gpt-4.1-mini"
    assert routing_item["metadata"]["best_alternate_profile"] == "balanced"
    assert routing_item["metadata"]["best_alternate_source"] == "fallback"
    assert routing_item["metadata"]["best_alternate_route_score"] == 7.0
    assert routing_item["metadata"]["selected_vs_best_alternate_margin"] == 3.5
    assert routing_item["metadata"]["route_explanation"].startswith("selected openrouter/gpt-4o-mini")
    assert routing_item["metadata"]["route_comparison_summary"].startswith(
        "selected openrouter/gpt-4o-mini over gpt-4.1-mini"
    )
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
