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
async def test_operator_benchmark_proof_surfaces_suite_coverage_and_evolution_gates(client):
    with patch(
        "src.api.operator.list_evolution_targets",
        return_value=[
            {"target_type": "skill", "source_path": "/tmp/skills/web-briefing.md"},
            {"target_type": "prompt_pack", "source_path": "/tmp/extensions/review-pack/prompts/review.md"},
        ],
    ):
        resp = await client.get("/api/operator/benchmark-proof")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_count"] == 4
    assert payload["summary"]["benchmark_posture"] == "deterministic_proof_backed"
    assert payload["summary"]["governed_improvement_status"] == "review_gated"
    assert payload["governed_improvement"]["target_count"] == 2
    assert payload["governed_improvement"]["target_types"] == ["prompt_pack", "skill"]
    assert payload["governed_improvement"]["gate_policy"]["requires_human_review"] is True
    assert "memory_continuity_workflows" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]

    memory_suite = next(item for item in payload["suites"] if item["name"] == "memory_continuity_workflows")
    assert "workflow_operating_layer_behavior" in memory_suite["scenario_names"]
    assert memory_suite["scenario_count"] >= 10

    computer_suite = next(item for item in payload["suites"] if item["name"] == "computer_use_browser_desktop")
    assert "browser_runtime_audit" in computer_suite["scenario_names"]


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
        learning_guidance="Prefer clarification before interrupting.",
        recent_execution_summary="- Atlas deploy failed recently",
        world_model=SimpleNamespace(
            current_focus="Atlas release planning",
            focus_source="observer_goal_window",
            focus_alignment="aligned",
            intervention_receptivity="guarded",
            dominant_thread="Atlas launch thread",
            user_model_confidence="grounded",
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
    assert payload["summary"]["current_focus"] == "Atlas release planning"
    assert payload["summary"]["user_model_confidence"] == "grounded"
    assert payload["explanation"]["judgment_proof_lines"][0].startswith("Project-target proof:")
    assert payload["explanation"]["judgment_risks"][0].startswith("Competing project anchors")
    assert payload["explanation"]["learning_diagnostics"][0].startswith("Fresh live outcomes")
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
    assert first["lead_process"]["process_id"] == "proc-1"
    assert first["background_processes"][0]["session_id"] == "session-1"

    second = payload["sessions"][1]
    assert second["session_id"] == "session-2"
    assert second["blocked_workflows"] == 1
    assert second["branch_handoff"]["target_type"] == "workflow_run"
    assert second["continue_message"] == "Resume cleanup after approval."


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
                        "started_at": "2026-04-10T10:00:00Z",
                        "updated_at": "2026-04-10T10:04:00Z",
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
                        "started_at": "2026-04-10T09:00:00Z",
                        "updated_at": "2026-04-10T09:15:00Z",
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
                        "started_at": "2026-04-07T08:00:00Z",
                        "updated_at": "2026-04-07T08:05:00Z",
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
                        "created_at": "2026-04-10T10:03:00Z",
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
                        "created_at": "2026-04-07T07:00:00Z",
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
                        "created_at": "2026-04-10T09:16:00Z",
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
                        "created_at": "2026-04-10T10:06:00Z",
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
                        "matched_at": "2026-04-10T10:02:00Z",
                        "snippet": "Need to finish seraph-quest/seraph/pull/390 review and publish the receipt.",
                        "source": "message",
                    },
                    {
                        "session_id": "session-2",
                        "title": "Roadmap thread",
                        "matched_at": "2026-04-10T09:10:00Z",
                        "snippet": "Planning work for seraph-quest/seraph roadmap and next batch.",
                        "source": "message",
                    },
                    {
                        "session_id": "session-3",
                        "title": "Issue thread",
                        "matched_at": "2026-04-10T10:01:00Z",
                        "snippet": "Need to follow up on seraph-quest/seraph#12.",
                        "source": "message",
                    },
                    {
                        "session_id": "session-stale",
                        "title": "Stale thread",
                        "matched_at": "2026-04-07T07:05:00Z",
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
