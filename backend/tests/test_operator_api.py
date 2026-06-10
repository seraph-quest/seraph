from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.cockpit.production_operator_control import (
    PRODUCTION_OPERATOR_CONTROL_BLOCKED_CLAIMS,
    PRODUCTION_OPERATOR_CONTROL_CLAIM_BOUNDARY,
    PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES,
    PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES,
)
from src.cockpit.dense_operator_recovery import (
    DENSE_OPERATOR_RECOVERY_BLOCKED_CLAIMS,
    DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY,
)
from src.evals.production_parity_readiness import PRODUCTION_PARITY_READINESS_SCENARIO_NAMES
from src.evals.final_parity_audit import (
    FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES,
    FINAL_PARITY_AUDIT_BLOCKED_CLAIMS,
    FINAL_PARITY_AUDIT_CLAIM_BOUNDARY,
    FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES,
    OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES,
)
from src.extensions.marketplace_lifecycle import (
    CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES,
    GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES,
    MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES,
    MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS,
    MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY,
)
from src.extensions.live_marketplace_attestation import (
    LIVE_MARKETPLACE_ATTESTATION_BLOCKED_CLAIMS,
    LIVE_MARKETPLACE_ATTESTATION_CLAIM_BOUNDARY,
    MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SCENARIO_NAMES,
    PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SCENARIO_NAMES,
    THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES,
)
from src.extensions.production_marketplace_security import (
    HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES,
    INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES,
    MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES,
    PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES,
    PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS,
    PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY,
    PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES,
)
from src.extensions.browser_provider_usability import (
    BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES,
    BROWSER_PROVIDER_USABILITY_BLOCKED_CLAIMS,
    BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY,
    LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES,
    MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES,
)
from src.extensions.safe_browser_computer_use import (
    AUTONOMOUS_BROWSER_SAFETY_SCENARIO_NAMES,
    BROWSER_PROVIDER_RELIABILITY_MATRIX_SCENARIO_NAMES,
    BROWSER_SESSION_PARTITIONING_SCENARIO_NAMES,
    INDEPENDENT_BROWSER_USABILITY_REVIEW_SCENARIO_NAMES,
    LIVE_BROWSER_TASK_DEPTH_SCENARIO_NAMES,
    SAFE_BROWSER_COMPUTER_USE_BLOCKED_CLAIMS,
    SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY,
    SITE_SPECIFIC_BROWSER_RECOVERY_SCENARIO_NAMES,
)
from src.extensions.production_reach_hardening import (
    BROWSER_COMPUTER_USE_RELIABILITY_V2_SCENARIO_NAMES,
    GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SCENARIO_NAMES,
    PRODUCTION_REACH_BROWSER_VOICE_CLAIM_BOUNDARY,
    PRODUCTION_REACH_CHANNEL_HARDENING_SCENARIO_NAMES,
)
from src.extensions.live_reach_media import (
    CROSS_SURFACE_CONTINUITY_RECOVERY_SCENARIO_NAMES,
    LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SCENARIO_NAMES,
    LIVE_REACH_MEDIA_CLAIM_BOUNDARY,
    PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SCENARIO_NAMES,
)
from src.extensions.production_reach_voice_mobile import (
    BROAD_CHANNEL_SLA_OPERATIONS_SCENARIO_NAMES,
    MOBILE_EXECUTION_CONTINUITY_SCENARIO_NAMES,
    PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY,
    PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SCENARIO_NAMES,
)
from src.extensions.field_reach_operations import (
    ALWAYS_AVAILABLE_REACH_SLO_SCENARIO_NAMES,
    BROAD_REACH_FIELD_OPERATIONS_SCENARIO_NAMES,
    BROAD_REACH_FIELD_OPS_CLAIM_BOUNDARY,
    VOICE_MEDIA_QUALITY_OPERATIONS_SCENARIO_NAMES,
)
from src.guardian.live_learning_quality import (
    CANONICAL_MEMORY_RECONCILIATION_V2_SCENARIO_NAMES,
    GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SCENARIO_NAMES,
    LIVE_GUARDIAN_LEARNING_QUALITY_BLOCKED_CLAIMS,
    LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY,
    LIVE_GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES,
    MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SCENARIO_NAMES,
    PROVIDER_USEFULNESS_REGRESSION_SCENARIO_NAMES,
)
from src.guardian.live_human_outcome_learning import (
    GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SCENARIO_NAMES,
    LIVE_HUMAN_OUTCOME_LEARNING_BLOCKED_CLAIMS,
    LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY,
    LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SCENARIO_NAMES,
    MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SCENARIO_NAMES,
)
from src.guardian.independent_learning_memory_parity import (
    INDEPENDENT_LEARNING_MEMORY_PARITY_BLOCKED_CLAIMS,
    INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY,
)
from src.workflows.durable_state import (
    DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES,
    DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES,
    PRODUCTION_DURABLE_ORCHESTRATION_SCENARIO_NAMES,
)
from src.workflows.live_orchestration import (
    LIVE_EXTERNAL_ORCHESTRATION_BLOCKED_CLAIMS,
    LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY,
    LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES,
    ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES,
)
from src.workflows.production_sla_orchestration import (
    DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES,
    EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES,
    PRODUCTION_SLA_ORCHESTRATION_BLOCKED_CLAIMS,
    PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY,
    PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES,
)
from src.workflows.continuous_orchestration_slo import (
    CONTINUOUS_ORCHESTRATION_SLO_BLOCKED_CLAIMS,
    CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY,
    CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES,
    CRASH_FAILOVER_SOAK_SCENARIO_NAMES,
    SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES,
)
from src.security.production_isolation import (
    PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES,
    PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES,
    PRODUCTION_ISOLATION_SECURITY_BLOCKED_CLAIMS,
    PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
    SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES,
)
from src.security.independent_review import (
    INDEPENDENT_SECURE_HOST_REVIEW_BLOCKED_CLAIMS,
    INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY,
    INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES,
    LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES,
    SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES,
)
from src.security.container_grade_host import (
    CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES,
    CONTAINER_GRADE_SECURE_HOST_BLOCKED_CLAIMS,
    CONTAINER_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
    EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES,
    SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES,
)


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
async def test_operator_production_parity_readiness_surface_blocks_completion_claims(client):
    resp = await client.get("/api/operator/production-parity-readiness")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "production_parity_readiness"
    assert payload["summary"]["completion_state"] == "readiness_contract_only_full_parity_unproven"
    assert payload["summary"]["scenario_count"] == len(PRODUCTION_PARITY_READINESS_SCENARIO_NAMES)
    assert "fully_at_parity" in payload["policy"]["blocked_claims"]
    assert "/api/operator/production-parity-readiness" in payload["policy"]["receipt_surfaces"]


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
    with ExitStack() as stack:
        stack.enter_context(patch(
        "src.api.operator.list_evolution_targets",
        return_value=[
            {"target_type": "skill", "source_path": "/tmp/skills/web-briefing.md"},
            {"target_type": "prompt_pack", "source_path": "/tmp/extensions/review-pack/prompts/review.md"},
        ],
        ))
        stack.enter_context(patch(
        "src.api.operator.build_production_parity_readiness_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "production_parity_readiness",
                    "benchmark_posture": "production_parity_readiness_ci_gated_operator_visible",
                    "operator_status": "production_parity_readiness_visible",
                    "scenario_count": len(PRODUCTION_PARITY_READINESS_SCENARIO_NAMES),
                    "batch_count": 7,
                    "future_batch_count": 6,
                    "blocked_claim_count": 9,
                    "proof_path_count": 16,
                    "operator_receipt_target_count": 5,
                    "project_field_count": 7,
                    "duplicate_guardrail_count": 4,
                    "failure_mode_count": 5,
                    "active_failure_count": 0,
                    "claim_boundary": (
                        "readiness_contract_for_production_parity_train_not_full_parity_or_superiority"
                    ),
                    "completion_state": "readiness_contract_only_full_parity_unproven",
                },
                "scenario_names": list(PRODUCTION_PARITY_READINESS_SCENARIO_NAMES),
                "batch_contracts": [
                    {
                        "issue": 476,
                        "proof_suites": ["production_parity_readiness"],
                        "operator_receipt_target": "/api/operator/production-parity-readiness",
                    }
                ],
                "duplicate_guardrails": [],
                "validation_plan": {},
                "failure_taxonomy": [],
                "failure_report": [],
                "policy": {
                    "claim_boundary": (
                        "readiness_contract_for_production_parity_train_not_full_parity_or_superiority"
                    ),
                    "blocked_claims": [
                        "fully_at_parity",
                        "reference_systems_exceeded",
                        "production_ready",
                        "secure_private_by_default",
                        "ironclaw_class_secure_execution",
                        "broad_openclaw_class_reach",
                        "voice_or_multimodal_parity",
                        "solved_durable_workflows",
                        "production_secure_marketplace",
                    ],
                    "receipt_surfaces": [
                        "/api/operator/production-parity-readiness",
                        "/api/operator/benchmark-proof",
                    ],
                    "not_claimed": ["full_parity_achieved"],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {
                    "total": len(PRODUCTION_PARITY_READINESS_SCENARIO_NAMES),
                    "passed": len(PRODUCTION_PARITY_READINESS_SCENARIO_NAMES),
                    "failed": 0,
                    "duration_ms": 100,
                },
            }
        ),
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
        "src.api.operator.build_durable_workflow_state_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "durable_workflow_engine_v1",
                    "benchmark_posture": "durable_workflow_engine_ci_gated_operator_visible",
                    "operator_status": "durable_workflow_engine_visible",
                    "scenario_count": len(DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES),
                    "active_failure_count": 0,
                    "durable_state_state": "checkpointed_state_and_resume_metadata_visible",
                    "recovery_state": "crash_safe_continuation_receipts_visible",
                    "trigger_state": "heartbeat_and_reactive_trigger_receipts_visible",
                },
                "scenario_names": list(DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES),
                "policy": {
                    "operator_visibility": "durable_workflow_engine_state_and_benchmark_proof_visible",
                    "receipt_surfaces": [
                        "/api/operator/durable-workflow-engine",
                        "/api/operator/benchmark-proof",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "failure_report": [],
                "latest_run": {
                    "total": len(DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES),
                    "passed": len(DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES),
                    "failed": 0,
                    "duration_ms": 100,
                },
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_durable_workflow_v2_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "durable_workflow_engine_v2",
                    "production_suite_name": "production_durable_orchestration",
                    "benchmark_posture": "durable_workflow_engine_v2_ci_gated_operator_visible",
                    "operator_status": "durable_workflow_engine_v2_recovery_receipts_visible",
                    "scenario_count": len(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
                    "production_scenario_count": len(PRODUCTION_DURABLE_ORCHESTRATION_SCENARIO_NAMES),
                    "lease_receipt_count": 2,
                    "blocked_recovery_count": 1,
                },
                "scenario_names": list(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
                "production_scenario_names": list(PRODUCTION_DURABLE_ORCHESTRATION_SCENARIO_NAMES),
                "policy": {
                    "operator_surfaces": [
                        "/api/operator/durable-workflow-engine-v2",
                        "/api/operator/benchmark-proof",
                    ],
                    "claim_boundary": "production_orchestration_receipts_not_langgraph_class_or_exactly_once_engine",
                    "blocked_claims": ["langgraph_class_durable_workflows"],
                },
                "failure_report": [],
                "latest_run": {
                    "total": len(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
                    "passed": len(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
                    "failed": 0,
                    "duration_ms": 100,
                },
            }
        ),
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
        "src.api.operator.build_production_secure_host_hardening_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "production_secure_host_hardening",
                    "benchmark_posture": "production_secure_host_hardening_ci_gated_operator_visible",
                    "operator_status": "production_secure_host_hardening_receipts_visible",
                    "claim_boundary": "production_secure_host_hardening_proof_not_secure_private_by_default_or_ironclaw_class",
                    "child_suite_count": 2,
                    "scenario_count": 9,
                    "active_failure_count": 0,
                    "live_isolation_state": "privileged_paths_fail_closed_with_receipts",
                    "secret_redaction_state": "replay_and_redaction_fail_closed",
                    "browser_recovery_partition_state": "per_session_recovery_without_profile_bleed",
                    "private_network_egress_state": "private_targets_blocked_with_receipts",
                    "extension_revocation_state": "runtime_contributions_cut_off_after_revocation",
                    "workflow_provider_replay_state": "same_or_narrower_trust_replay_required",
                    "operator_receipt_state": "allow_deny_recover_receipts_visible",
                },
                "scenario_names": [
                    "production_secure_host_batch_contract_behavior",
                    "production_secure_host_receipt_schema_behavior",
                    "production_secure_host_claim_boundary_behavior",
                    "operator_production_secure_host_hardening_surface_behavior",
                    "secure_host_live_secret_redaction_replay_behavior",
                    "secure_host_live_browser_recovery_partition_behavior",
                    "secure_host_live_private_network_egress_behavior",
                    "secure_host_live_extension_revocation_behavior",
                    "secure_host_live_workflow_replay_trust_drift_behavior",
                ],
                "negative_cases": [],
                "live_isolation_controls": [],
                "receipt_schema": [],
                "operator_surfaces": ["/api/operator/secure-capability-host-hardening"],
                "policy": {
                    "claim_boundary": "production_secure_host_hardening_proof_not_secure_private_by_default_or_ironclaw_class",
                    "blocked_claims": [
                        "secure_private_by_default",
                        "production_security",
                        "ironclaw_class_secure_execution",
                    ],
                    "receipt_surfaces": ["/api/operator/secure-capability-host-hardening"],
                },
                "failure_report": [],
                "latest_run": {"total": 9, "passed": 9, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_production_isolation_security_report",
        AsyncMock(
            return_value={
                "summary": {
                    "benchmark_posture": "production_isolation_security_ci_gated_operator_visible",
                    "operator_status": "production_isolation_security_receipts_visible",
                    "isolation_receipt_count": 5,
                    "red_team_case_count": 6,
                    "incident_drill_count": 6,
                    "operator_control_count": 9,
                    "fail_closed_isolation_count": 5,
                    "blocked_red_team_count": 6,
                    "recorded_live_drill_count": 5,
                    "deterministic_contract_count": 6,
                    "incident_operator_notification_visible": True,
                    "credential_rotation_visible": True,
                    "required_controls_visible": True,
                    "evidence_modes": ["deterministic_contract", "recorded_live_drill"],
                    "claim_boundary": PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
                    "scenario_count": (
                        len(PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES)
                        + len(PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES)
                        + len(SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES)
                    ),
                    "active_failure_count": 0,
                },
                "scenario_names": {
                    "production_isolation_hardening_v2": list(PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES),
                    "privileged_path_red_team_gauntlet_v2": list(
                        PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES
                    ),
                    "security_incident_recovery_drill": list(SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES),
                },
                "contract": {
                    "summary": {
                        "claim_boundary": PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
                    },
                    "isolation_receipts": [],
                    "red_team_cases": [],
                    "incident_drill_receipts": [],
                    "operator_controls": [],
                    "policy": {
                        "claim_boundary": PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
                        "blocked_claims": list(PRODUCTION_ISOLATION_SECURITY_BLOCKED_CLAIMS),
                    },
                },
                "failure_report": [],
                "policy": {
                    "claim_boundary": PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
                    "blocked_claims": list(PRODUCTION_ISOLATION_SECURITY_BLOCKED_CLAIMS),
                    "receipt_surfaces": ["/api/operator/production-isolation-hardening"],
                    "not_claimed": ["ironclaw_class_secure_execution", "full_host_container_isolation"],
                },
                "latest_run": {"total": 17, "passed": 17, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
        "src.api.operator.build_one_reach_channel_canary_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "one_excellent_reach_channel_canary",
                    "benchmark_posture": "one_reach_channel_canary_ci_gated_operator_visible",
                    "operator_status": "one_reach_channel_canary_visible",
                    "selected_channel": "native_notification",
                    "scenario_count": 5,
                    "active_failure_count": 0,
                    "pairing_state": "paired",
                    "revocation_state": "revoked",
                    "health_state": "ready",
                    "degraded_state": "daemon_offline",
                    "retry_state": "bounded_retry_with_fallback_visible",
                    "thread_continuity_state": "channel_thread_session_and_memory_context_linked",
                    "approval_handoff_state": "pending_operator_approval",
                    "audit_receipt_count": 7,
                    "e2e_step_count": 4,
                    "channel_sprawl_state": "rejected_until_native_notification_canary_meets_bar",
                    "claim_boundary": "deterministic_native_notification_canary_not_broad_live_channel_reach",
                },
                "scenario_names": [
                    "one_reach_channel_selection_scope_behavior",
                    "native_notification_pairing_revocation_behavior",
                    "native_notification_health_retry_degraded_behavior",
                    "native_notification_continuity_approval_audit_behavior",
                    "operator_one_reach_channel_canary_surface_behavior",
                ],
                "protocol": {"replay_command": "uv run python -m src.evals.harness --benchmark-suite one_excellent_reach_channel_canary --indent 0"},
                "policy": {
                    "selected_channel": "native_notification",
                    "claim_boundary": "deterministic_native_notification_canary_not_broad_live_channel_reach",
                    "not_claimed": ["live_slack_discord_telegram_delivery"],
                    "receipt_surfaces": [
                        "/api/operator/one-reach-channel-canary",
                        "/api/operator/control-plane",
                        "/api/operator/benchmark-proof",
                    ],
                },
                "receipt": {},
                "operator_story": {
                    "single_channel_selected": True,
                    "channel_sprawl_rejected": True,
                    "pairing_visible": True,
                    "revocation_fail_closed_visible": True,
                    "health_visible": True,
                    "retry_visible": True,
                    "thread_continuity_visible": True,
                    "memory_context_visible": True,
                    "approval_handoff_visible": True,
                    "audit_trail_visible": True,
                    "degraded_state_ui_visible": True,
                    "e2e_flow_visible": True,
                },
                "failure_report": [],
                "latest_run": {"total": 5, "passed": 5, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
        "src.api.operator.build_guardian_safe_multimodal_voice_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "guardian_safe_multimodal_voice",
                    "benchmark_posture": "guardian_safe_multimodal_voice_ci_gated_operator_visible",
                    "operator_status": "guardian_safe_voice_media_receipts_visible",
                    "scenario_count": 6,
                    "dimension_count": 5,
                    "failure_mode_count": 6,
                    "active_failure_count": 0,
                    "capability_governance_state": "owner_trust_permission_data_access_mutation_revocation_visible",
                    "transcript_audit_privacy_state": "capture_destination_provider_context_correction_deletion_visible",
                    "continuity_approval_state": "thread_memory_approval_workflow_continuity_preserved",
                    "exposure_revocation_state": "silent_screen_file_credential_media_network_expansion_blocked",
                    "guardian_value_state": "voice_media_requires_guardian_value_reason",
                    "claim_boundary": "governed_voice_media_proof_not_live_broad_multimodal_runtime_or_voice_parity",
                },
                "scenario_names": [
                    "multimodal_voice_capability_governance_behavior",
                    "multimodal_voice_transcript_audit_privacy_behavior",
                    "multimodal_voice_continuity_approval_behavior",
                    "multimodal_voice_exposure_revocation_behavior",
                    "multimodal_voice_guardian_value_behavior",
                    "operator_guardian_safe_multimodal_voice_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "capability_families": [],
                "governance_receipts": [],
                "failure_report": [],
                "policy": {
                    "guardian_value_policy": "voice_media_must_improve_timing_accessibility_situational_awareness_or_intervention_quality",
                    "claim_boundary": "governed_voice_media_proof_not_live_broad_multimodal_runtime_or_voice_parity",
                    "receipt_surfaces": [
                        "/api/operator/guardian-safe-multimodal-voice",
                        "/api/operator/benchmark-proof",
                    ],
                    "not_claimed": [
                        "live_broad_voice_runtime",
                        "voice_parity",
                        "multimodal_parity",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 6, "passed": 6, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_guardian_learning_arbitration_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "guardian_learning_arbitration_v2",
                    "benchmark_posture": "guardian_learning_arbitration_ci_gated_operator_visible",
                    "operator_status": "guardian_learning_arbitration_receipts_visible",
                    "scenario_count": 7,
                    "dimension_count": 5,
                    "failure_mode_count": 6,
                    "active_failure_count": 0,
                    "outcome_count": 6,
                    "negative_case_count": 6,
                    "outcome_coverage_state": "act_defer_bundle_clarify_approval_stay_silent_receipts_visible",
                    "negative_case_state": "stale_conflict_ambiguous_degraded_unsafe_negative_outcome_cases_visible",
                    "guardian_value_state": "learning_improves_restraint_clarification_timing_approval_recovery_or_follow_through",
                    "claim_boundary": "deterministic_learning_arbitration_receipts_not_guardian_intelligence_superiority",
                },
                "scenario_names": [
                    "guardian_learning_arbitration_act_behavior",
                    "guardian_learning_arbitration_defer_behavior",
                    "guardian_learning_arbitration_bundle_behavior",
                    "guardian_learning_arbitration_clarify_behavior",
                    "guardian_learning_arbitration_approval_behavior",
                    "guardian_learning_arbitration_stay_silent_behavior",
                    "operator_guardian_learning_arbitration_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "arbitration_receipts": [],
                "failure_report": [],
                "policy": {
                    "guardian_value_policy": "learning_must_improve_restraint_clarification_timing_approval_recovery_or_follow_through_not_intervention_volume",
                    "claim_boundary": "deterministic_learning_arbitration_receipts_not_guardian_intelligence_superiority",
                    "receipt_surfaces": [
                        "/api/operator/guardian-learning-arbitration",
                        "/api/operator/benchmark-proof",
                    ],
                    "not_claimed": [
                        "guardian_intelligence_superiority",
                        "live_human_outcome_study",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 7, "passed": 7, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_live_guardian_learning_quality_report",
        AsyncMock(
            return_value={
                "summary": {
                    "operator_status": "live_guardian_learning_quality_receipts_visible",
                    "benchmark_posture": "live_guardian_learning_quality_ci_gated_operator_visible",
                    "scenario_count": (
                        len(LIVE_GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES)
                        + len(GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SCENARIO_NAMES)
                        + len(MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SCENARIO_NAMES)
                        + len(CANONICAL_MEMORY_RECONCILIATION_V2_SCENARIO_NAMES)
                        + len(PROVIDER_USEFULNESS_REGRESSION_SCENARIO_NAMES)
                    ),
                    "outcome_cohort_count": 8,
                    "typed_outcome_count": 8,
                    "policy_delta_count": 4,
                    "false_positive_receipt_count": 7,
                    "false_negative_receipt_count": 8,
                    "stale_evidence_decay_count": 4,
                    "provider_count": 3,
                    "provider_behavior_change_count": 2,
                    "provider_quarantine_count": 1,
                    "canonical_precedence_preserved": True,
                    "delete_export_receipts_visible": True,
                    "provider_regression_count": 4,
                    "provider_regressions_passed": True,
                    "claim_boundary": LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY,
                    "active_failure_count": 0,
                },
                "scenario_names": {
                    "live_guardian_learning_quality": list(LIVE_GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES),
                    "guardian_intervention_outcome_cohorts": list(
                        GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SCENARIO_NAMES
                    ),
                    "memory_provider_ecosystem_maturity_v1": list(
                        MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SCENARIO_NAMES
                    ),
                    "canonical_memory_reconciliation_v2": list(
                        CANONICAL_MEMORY_RECONCILIATION_V2_SCENARIO_NAMES
                    ),
                    "provider_usefulness_regression": list(PROVIDER_USEFULNESS_REGRESSION_SCENARIO_NAMES),
                },
                "contract": {
                    "summary": {
                        "operator_status": "live_guardian_learning_quality_receipts_visible",
                        "outcome_cohort_count": 8,
                        "typed_outcome_count": 8,
                        "provider_quarantine_count": 1,
                        "delete_export_receipts_visible": True,
                    },
                    "outcome_cohorts": [
                        {"outcome": "accepted", "typed_outcome_recorded": True},
                        {"outcome": "ignored", "typed_outcome_recorded": True},
                        {"outcome": "corrected", "typed_outcome_recorded": True},
                        {"outcome": "deferred", "typed_outcome_recorded": True},
                        {"outcome": "harmful", "typed_outcome_recorded": True},
                        {"outcome": "helpful", "typed_outcome_recorded": True},
                        {"outcome": "channel_shifted", "typed_outcome_recorded": True},
                        {"outcome": "followthrough", "typed_outcome_recorded": True},
                    ],
                    "canonical_reconciliation": {
                        "canonical_precedence": {"provider_override_blocked": True},
                        "delete_export": {
                            "delete_receipt_visible": True,
                            "export_receipt_visible": True,
                        },
                    },
                    "provider_regressions": [
                        {"regression_id": "provider-behavior-change", "passed": True},
                        {"regression_id": "provider-latency-outage", "passed": True},
                        {"regression_id": "provider-privacy", "passed": True},
                        {"regression_id": "provider-quarantine", "passed": True},
                    ],
                },
                "failure_report": [],
                "policy": {
                    "claim_boundary": LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY,
                    "blocked_claims": list(LIVE_GUARDIAN_LEARNING_QUALITY_BLOCKED_CLAIMS),
                    "receipt_surfaces": [
                        "/api/operator/live-guardian-learning-quality",
                        "/api/operator/benchmark-proof",
                    ],
                    "not_claimed": [
                        "live_human_outcome_study",
                        "guardian_intelligence_superiority",
                        "external_memory_provider_parity",
                    ],
                },
                "latest_run": {"total": 28, "passed": 28, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_live_human_outcome_learning_report",
        AsyncMock(
            return_value={
                "summary": {
                    "operator_status": "live_human_outcome_learning_receipts_visible",
                    "benchmark_posture": "live_human_outcome_learning_ci_gated_operator_visible",
                    "scenario_count": (
                        len(LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SCENARIO_NAMES)
                        + len(GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SCENARIO_NAMES)
                        + len(MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SCENARIO_NAMES)
                    ),
                    "study_mode": "recorded_live_anonymized",
                    "outcome_cohort_count": 7,
                    "typed_outcome_count": 7,
                    "consented_cohort_count": 7,
                    "anonymized_cohort_count": 7,
                    "bias_limitation_count": 7,
                    "causal_attribution_count": 4,
                    "bounded_causal_claim_count": 4,
                    "reversible_learning_change_count": 7,
                    "provider_monitor_count": 4,
                    "provider_quarantine_count": 2,
                    "stale_decay_monitor_count": 4,
                    "privacy_regression_count": 1,
                    "claim_boundary": LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY,
                    "active_failure_count": 0,
                },
                "scenario_names": {
                    "live_human_outcome_quality_study": list(
                        LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SCENARIO_NAMES
                    ),
                    "guardian_learning_causal_attribution": list(
                        GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SCENARIO_NAMES
                    ),
                    "memory_provider_live_regression_monitor": list(
                        MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SCENARIO_NAMES
                    ),
                },
                "contract": {
                    "summary": {
                        "operator_status": "live_human_outcome_learning_receipts_visible",
                        "study_mode": "recorded_live_anonymized",
                        "outcome_cohort_count": 7,
                        "causal_attribution_count": 4,
                        "provider_monitor_count": 4,
                    },
                    "study_receipts": [
                        {"outcome": "accepted", "consent": {"operator_consent_recorded": True}},
                        {"outcome": "ignored", "consent": {"operator_consent_recorded": True}},
                        {"outcome": "corrected", "consent": {"operator_consent_recorded": True}},
                        {"outcome": "deferred", "consent": {"operator_consent_recorded": True}},
                        {"outcome": "harmful", "consent": {"operator_consent_recorded": True}},
                        {"outcome": "helpful", "consent": {"operator_consent_recorded": True}},
                        {"outcome": "followthrough", "consent": {"operator_consent_recorded": True}},
                    ],
                    "causal_attribution": [
                        {
                            "attribution_id": "cf-causal-restraint-counterfactual",
                            "claim_scope": "bounded_to_recorded_live_matched_contexts",
                            "counterfactual_outcome": "similar_context_would_have_interrupted",
                        }
                    ],
                    "memory_provider_monitors": [
                        {
                            "provider_id": "noisy_archive_provider",
                            "quarantine_state": "quarantined",
                            "behavior_change_allowed": False,
                        }
                    ],
                },
                "failure_report": [],
                "policy": {
                    "claim_boundary": LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY,
                    "blocked_claims": list(LIVE_HUMAN_OUTCOME_LEARNING_BLOCKED_CLAIMS),
                    "receipt_surfaces": [
                        "/api/operator/live-human-outcome-learning-proof",
                        "/api/operator/benchmark-proof",
                    ],
                    "not_claimed": [
                        "randomized_controlled_trial",
                        "guardian_intelligence_superiority",
                        "solved_live_learning",
                    ],
                },
                "latest_run": {"total": 15, "passed": 15, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
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
        ))
        stack.enter_context(patch(
        "src.api.operator.build_marketplace_lifecycle_report",
        AsyncMock(
            return_value={
                "summary": {
                    "operator_status": "marketplace_lifecycle_maturity_receipts_visible",
                    "benchmark_posture": "marketplace_lifecycle_maturity_ci_gated_operator_visible",
                    "scenario_count": (
                        len(MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES)
                        + len(GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES)
                        + len(CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES)
                    ),
                    "lifecycle_action_count": 9,
                    "family_count": 11,
                    "negative_case_count": 5,
                    "staged_rollout_count": 2,
                    "permission_delta_receipt_count": 2,
                    "risk_delta_receipt_count": 9,
                    "rollback_receipt_count": 8,
                    "quarantine_receipt_count": 2,
                    "failed_update_recovery_visible": True,
                    "cross_family_coverage_visible": True,
                    "package_count_substitution_blocked": True,
                    "claim_boundary": MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY,
                    "active_failure_count": 0,
                },
                "scenario_names": {
                    "marketplace_grade_capability_lifecycle": list(
                        MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES
                    ),
                    "governed_capability_lifecycle_v2": list(
                        GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES
                    ),
                    "capability_rollback_failure_diagnostics": list(
                        CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES
                    ),
                },
                "contract": {
                    "summary": {
                        "operator_status": "marketplace_lifecycle_maturity_receipts_visible",
                        "lifecycle_action_count": 9,
                        "family_count": 11,
                        "negative_case_count": 5,
                        "staged_rollout_count": 2,
                        "package_count_substitution_blocked": True,
                    },
                    "lifecycle_receipts": [
                        {
                            "action": "update",
                            "permission_delta": {"added": ["network.request"], "removed": []},
                            "risk_delta": {"risk_after": "high"},
                            "rollback": {"available": True},
                        }
                    ],
                    "negative_cases": [
                        {"case_id": "failed-update", "state": "rolled_back", "fails_closed": True},
                        {"case_id": "permission-creep", "state": "quarantined", "fails_closed": True},
                    ],
                },
                "failure_report": [],
                "policy": {
                    "claim_boundary": MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY,
                    "blocked_claims": list(MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS),
                    "receipt_surfaces": [
                        "/api/extensions",
                        "/api/extensions/validate",
                        "/api/operator/marketplace-lifecycle-maturity",
                        "/api/operator/benchmark-proof",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 21, "passed": 21, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_live_marketplace_attestation_report",
        AsyncMock(
            return_value={
                "summary": {
                    "operator_status": "live_marketplace_attestation_receipts_visible",
                    "benchmark_posture": "live_marketplace_attestation_ci_gated_operator_visible",
                    "scenario_count": (
                        len(THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES)
                        + len(MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SCENARIO_NAMES)
                        + len(PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SCENARIO_NAMES)
                    ),
                    "attested_package_count": 4,
                    "recorded_live_operation_count": 6,
                    "publisher_review_count": 4,
                    "blocked_attestation_count": 1,
                    "incident_operation_count": 4,
                    "signature_verified_count": 3,
                    "publisher_verified_count": 3,
                    "vulnerability_attestation_count": 4,
                    "rollback_ready_count": 4,
                    "fail_closed_operation_count": 4,
                    "redaction_receipt_count": 2,
                    "package_count_substitution_blocked": True,
                    "claim_boundary": LIVE_MARKETPLACE_ATTESTATION_CLAIM_BOUNDARY,
                    "active_failure_count": 0,
                },
                "scenario_names": {
                    "third_party_marketplace_attestation": list(
                        THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES
                    ),
                    "marketplace_operations_incident_drill": list(
                        MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SCENARIO_NAMES
                    ),
                    "publisher_review_and_package_trust": list(
                        PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SCENARIO_NAMES
                    ),
                },
                "contract": {
                    "summary": {
                        "operator_status": "live_marketplace_attestation_receipts_visible",
                        "attested_package_count": 4,
                        "recorded_live_operation_count": 6,
                        "publisher_review_count": 4,
                        "package_count_substitution_blocked": True,
                    },
                    "third_party_attestations": [
                        {
                            "package_id": "marketplace.suspicious-exporter",
                            "signature_status": "missing",
                            "publisher_verification": "unverified",
                            "compatibility": "blocked",
                        }
                    ],
                    "operations": [
                        {"operation_id": "cg-malicious-exporter", "state": "quarantined", "fails_closed": True},
                        {"operation_id": "cg-failed-update-recovery", "state": "rolled_back", "fails_closed": True},
                    ],
                    "publisher_reviews": [
                        {
                            "publisher_id": "pub.unverified.unknown",
                            "review_state": "stale_or_missing",
                            "operator_action": "deny_and_quarantine",
                        }
                    ],
                },
                "failure_report": [],
                "policy": {
                    "claim_boundary": LIVE_MARKETPLACE_ATTESTATION_CLAIM_BOUNDARY,
                    "blocked_claims": list(LIVE_MARKETPLACE_ATTESTATION_BLOCKED_CLAIMS),
                    "receipt_surfaces": [
                        "/api/extensions",
                        "/api/extensions/validate",
                        "/api/operator/marketplace-lifecycle-maturity",
                        "/api/operator/live-marketplace-attestation-proof",
                        "/api/operator/benchmark-proof",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 15, "passed": 15, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_browser_provider_usability_report",
        AsyncMock(
            return_value={
                "summary": {
                    "operator_status": "browser_provider_usability_receipts_visible",
                    "benchmark_posture": "browser_provider_usability_ci_gated_operator_visible",
                    "scenario_count": (
                        len(MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES)
                        + len(LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES)
                        + len(BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES)
                    ),
                    "provider_attestation_count": 3,
                    "recorded_live_provider_count": 2,
                    "session_partition_count": 2,
                    "credential_boundary_count": 3,
                    "download_upload_boundary_count": 3,
                    "degraded_or_blocked_provider_count": 2,
                    "multi_operator_task_count": 3,
                    "max_operator_count": 3,
                    "keyboard_path_count": 3,
                    "accessibility_receipt_count": 3,
                    "reversible_action_count": 3,
                    "recovery_drill_count": 4,
                    "fail_closed_recovery_count": 4,
                    "external_action_block_count": 4,
                    "claim_boundary": BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY,
                    "active_failure_count": 0,
                },
                "scenario_names": {
                    "managed_browser_provider_attestation": list(
                        MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES
                    ),
                    "live_multi_operator_usability_study": list(
                        LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES
                    ),
                    "browser_computer_use_recovery_drill": list(
                        BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES
                    ),
                },
                "contract": {
                    "summary": {
                        "operator_status": "browser_provider_usability_receipts_visible",
                        "provider_attestation_count": 3,
                        "multi_operator_task_count": 3,
                        "recovery_drill_count": 4,
                    },
                    "provider_attestation_receipts": [
                        {
                            "provider_id": "openclaw-remote-cdp-existing-session",
                            "provider_mode": "remote_cdp_existing_session",
                            "provider_degradation": {"state": "blocked_until_partitioned"},
                        }
                    ],
                    "multi_operator_usability_receipts": [
                        {
                            "task_id": "ch-usability-inspect-recover-handoff",
                            "operator_count": 3,
                            "keyboard_path_complete": True,
                            "error_rate": 0.0,
                        }
                    ],
                    "recovery_drill_receipts": [
                        {
                            "drill_id": "ch-recovery-provider-crash",
                            "fails_closed": True,
                            "external_action_allowed": False,
                        }
                    ],
                },
                "failure_report": [],
                "policy": {
                    "claim_boundary": BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY,
                    "blocked_claims": list(BROWSER_PROVIDER_USABILITY_BLOCKED_CLAIMS),
                    "receipt_surfaces": [
                        "/api/operator/browser-provider-usability-proof",
                        "/api/operator/benchmark-proof",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 15, "passed": 15, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_live_external_orchestration_report",
        AsyncMock(
            return_value={
                "summary": {
                    "operator_status": "live_external_orchestration_receipts_visible",
                    "benchmark_posture": "live_external_orchestration_ci_gated_operator_visible",
                    "scenario_count": (
                        len(LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES)
                        + len(ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES)
                    ),
                    "provider_receipt_count": 3,
                    "crash_study_count": 3,
                    "operator_control_count": 6,
                    "recorded_live_receipt_count": 4,
                    "deterministic_contract_count": 2,
                    "side_effect_boundary_count": 3,
                    "replay_suppression_count": 3,
                    "required_controls_visible": True,
                    "evidence_modes": ["deterministic_contract", "recorded_live_fixture"],
                    "claim_boundary": LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY,
                    "active_failure_count": 0,
                },
                "scenario_names": {
                    "live_external_orchestration_attestation": list(
                        LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES
                    ),
                    "orchestration_crash_recovery_study": list(
                        ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES
                    ),
                },
                "contract": {
                    "summary": {
                        "operator_status": "live_external_orchestration_receipts_visible",
                        "provider_receipt_count": 3,
                        "crash_study_count": 3,
                        "required_controls_visible": True,
                    },
                    "provider_attestation_receipts": [
                        {
                            "provider": "temporal_cloud_recorded_live_fixture",
                            "evidence_mode": "recorded_live_fixture",
                            "provider_identity_visible": True,
                            "idempotency_key": "workflow:release-brief:collect:20260610T0118Z",
                            "side_effect_boundary": "external_email_send_blocked_until_operator_approval",
                        }
                    ],
                    "crash_recovery_study_receipts": [
                        {
                            "study_id": "cc-crash-after-side-effect",
                            "replay_suppression": "side_effect_receipt_blocks_second_write_until_operator_confirms",
                            "resume_authority": "manual_audit_required_before_retry",
                        }
                    ],
                    "operator_recovery_receipts": [
                        {"action": "inspect", "enabled": True, "receipt_after_action": "r1"},
                        {"action": "resume", "enabled": True, "receipt_after_action": "r2"},
                        {"action": "retry", "enabled": True, "receipt_after_action": "r3"},
                        {"action": "branch", "enabled": True, "receipt_after_action": "r4"},
                        {"action": "cancel", "enabled": True, "receipt_after_action": "r5"},
                        {"action": "audit", "enabled": True, "receipt_after_action": "r6"},
                    ],
                },
                "failure_report": [],
                "policy": {
                    "claim_boundary": LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY,
                    "blocked_claims": list(LIVE_EXTERNAL_ORCHESTRATION_BLOCKED_CLAIMS),
                    "receipt_surfaces": [
                        "/api/operator/live-external-orchestration",
                        "/api/operator/benchmark-proof",
                        "/api/operator/durable-workflow-engine-v2",
                    ],
                    "not_claimed": [
                        "exactly_once_production_scheduler",
                        "crash_proof_workflow_engine",
                        "full_parity_achieved",
                    ],
                },
                "latest_run": {"total": 11, "passed": 11, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_production_sla_orchestration_report",
        AsyncMock(
            return_value={
                "summary": {
                    "operator_status": "production_sla_orchestration_receipts_visible",
                    "benchmark_posture": "production_sla_orchestration_ci_gated_operator_visible",
                    "scenario_count": (
                        len(PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES)
                        + len(EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES)
                        + len(DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES)
                    ),
                    "sla_window_count": 3,
                    "failure_injection_count": 3,
                    "duplicate_side_effect_audit_count": 3,
                    "operator_control_count": 6,
                    "recorded_live_receipt_count": 4,
                    "deterministic_contract_count": 2,
                    "evidence_modes": ["deterministic_contract", "recorded_live_fixture"],
                    "all_provider_identities_visible": True,
                    "all_sla_windows_within_budget": True,
                    "all_failure_injections_have_resume_authority": True,
                    "duplicate_audits_reconciled": True,
                    "required_controls_visible": True,
                    "claim_boundary": PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY,
                    "active_failure_count": 0,
                },
                "scenario_names": {
                    "production_sla_orchestration": list(PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES),
                    "exactly_once_recovery_evidence": list(EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES),
                    "duplicate_side_effect_audit": list(DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES),
                },
                "contract": {
                    "summary": {
                        "operator_status": "production_sla_orchestration_receipts_visible",
                        "sla_window_count": 3,
                        "failure_injection_count": 3,
                        "duplicate_side_effect_audit_count": 3,
                        "required_controls_visible": True,
                    },
                    "sla_window_receipts": [
                        {
                            "provider": "temporal_cloud_recorded_live_fixture",
                            "provider_identity_visible": True,
                            "max_jitter_ms": 1000,
                            "jitter_budget_ms": 5000,
                        }
                    ],
                    "failure_injection_receipts": [
                        {
                            "failure_injection_method": "worker_process_kill_before_external_write",
                            "idempotency_scope": "workflow_run_step",
                            "resume_authority": "automatic_resume_allowed_after_checkpoint",
                        }
                    ],
                    "duplicate_side_effect_audit_receipts": [
                        {
                            "audit_id": "cj-audit-email-send",
                            "reconciliation_status": "no_duplicate_side_effect_detected",
                        }
                    ],
                    "operator_recovery_receipts": [
                        {"action": "inspect", "enabled": True, "receipt_after_action": "r1"},
                        {"action": "audit", "enabled": True, "receipt_after_action": "r2"},
                        {"action": "resume", "enabled": True, "receipt_after_action": "r3"},
                        {"action": "repair", "enabled": True, "receipt_after_action": "r4"},
                        {"action": "branch", "enabled": True, "receipt_after_action": "r5"},
                        {"action": "cancel", "enabled": True, "receipt_after_action": "r6"},
                    ],
                },
                "failure_report": [],
                "policy": {
                    "claim_boundary": PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY,
                    "blocked_claims": list(PRODUCTION_SLA_ORCHESTRATION_BLOCKED_CLAIMS),
                    "receipt_surfaces": [
                        "/api/operator/production-sla-orchestration",
                        "/api/operator/benchmark-proof",
                        "/api/operator/live-external-orchestration",
                        "/api/operator/durable-workflow-engine-v2",
                    ],
                    "not_claimed": [
                        "unconditional_exactly_once_scheduler",
                        "crash_proof_workflow_engine",
                        "full_product_parity",
                    ],
                },
                "latest_run": {"total": 15, "passed": 15, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        stack.enter_context(patch(
        "src.api.operator.build_governed_capability_pack_hardening_report",
        AsyncMock(
            return_value={
                "summary": {
                    "suite_name": "governed_capability_pack_hardening",
                    "benchmark_posture": "governed_pack_hardening_ci_gated_operator_visible",
                    "operator_status": "governed_capability_pack_hardening_receipts_visible",
                    "scenario_count": 6,
                    "dimension_count": 6,
                    "failure_mode_count": 7,
                    "active_failure_count": 0,
                    "review_receipt_state": "risk_delta_and_blocked_claim_receipts_visible",
                    "compatibility_downgrade_state": "incompatible_and_downgrade_states_named",
                    "permission_creep_state": "underdeclaration_and_drift_fail_closed",
                    "supply_chain_state": "signature_digest_key_revocation_fail_closed",
                    "rollback_state": "rollback_availability_and_action_visible",
                    "claim_boundary": "governed_capability_pack_hardening_receipts_not_production_marketplace_security_or_ecosystem_maturity_or_package_count_superiority",
                },
                "scenario_names": [
                    "capability_pack_review_receipt_behavior",
                    "capability_pack_compatibility_downgrade_behavior",
                    "capability_pack_permission_creep_behavior",
                    "capability_pack_supply_chain_suspicion_behavior",
                    "capability_pack_rollback_ready_behavior",
                    "operator_governed_capability_pack_hardening_surface_behavior",
                ],
                "dimensions": [],
                "failure_taxonomy": [],
                "hardening_receipts": [],
                "failure_report": [],
                "policy": {
                    "claim_boundary": "governed_capability_pack_hardening_receipts_not_production_marketplace_security_or_ecosystem_maturity_or_package_count_superiority",
                    "receipt_surfaces": [
                        "/api/extensions",
                        "/api/extensions/validate",
                        "/api/operator/benchmark-proof",
                        "/api/operator/governed-capability-pack-hardening",
                    ],
                    "ci_gate_mode": "required_benchmark_suite",
                },
                "latest_run": {"total": 6, "passed": 6, "failed": 0, "duration_ms": 100},
            }
        ),
        ))
        resp = await client.get("/api/operator/benchmark-proof")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_count"] == 98
    assert payload["summary"]["benchmark_posture"] == "deterministic_proof_backed"
    assert (
        payload["summary"]["production_parity_readiness_posture"]
        == "production_parity_readiness_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["production_parity_readiness_claim_boundary"]
        == "readiness_contract_for_production_parity_train_not_full_parity_or_superiority"
    )
    assert payload["summary"]["governed_improvement_status"] == "review_gated_canary_required"
    assert payload["summary"]["memory_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["summary"]["user_model_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["summary"]["workflow_endurance_benchmark_posture"] == "ci_gated_operator_visible"
    assert (
        payload["summary"]["live_workflow_endurance_canary_posture"]
        == "live_workflow_canary_ci_gated_operator_visible"
    )
    assert payload["summary"]["durable_workflow_engine_posture"] == "durable_workflow_engine_ci_gated_operator_visible"
    assert (
        payload["summary"]["durable_workflow_engine_v2_posture"]
        == "durable_workflow_engine_v2_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["durable_workflow_engine_v2_claim_boundary"]
        == "production_orchestration_receipts_not_langgraph_class_or_exactly_once_engine"
    )
    assert (
        payload["summary"]["live_external_orchestration_posture"]
        == "live_external_orchestration_ci_gated_operator_visible"
    )
    assert payload["summary"]["live_external_orchestration_claim_boundary"] == (
        LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["production_sla_orchestration_posture"]
        == "production_sla_orchestration_ci_gated_operator_visible"
    )
    assert payload["summary"]["production_sla_orchestration_claim_boundary"] == (
        PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["continuous_orchestration_slo_posture"]
        == "continuous_orchestration_slo_ci_gated_operator_visible"
    )
    assert payload["summary"]["continuous_orchestration_slo_claim_boundary"] == (
        CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY
    )
    assert payload["summary"]["m5_operating_layer_benchmark_posture"] == "m5_ci_gated_operator_visible"
    assert payload["summary"]["trust_boundary_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["summary"]["secure_capability_host_benchmark_posture"] == "secure_host_ci_gated_operator_visible"
    assert (
        payload["summary"]["production_secure_host_hardening_posture"]
        == "production_secure_host_hardening_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["production_secure_host_hardening_claim_boundary"]
        == "production_secure_host_hardening_proof_not_secure_private_by_default_or_ironclaw_class"
    )
    assert (
        payload["summary"]["production_isolation_security_posture"]
        == "production_isolation_security_ci_gated_operator_visible"
    )
    assert payload["summary"]["production_isolation_security_claim_boundary"] == (
        PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["container_grade_secure_host_posture"]
        == "container_grade_secure_host_ci_gated_operator_visible"
    )
    assert payload["summary"]["container_grade_secure_host_claim_boundary"] == (
        CONTAINER_GRADE_SECURE_HOST_CLAIM_BOUNDARY
    )
    assert payload["summary"]["computer_use_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["summary"]["one_reach_channel_canary_posture"] == "one_reach_channel_canary_ci_gated_operator_visible"
    assert payload["summary"]["m2_execution_benchmark_posture"] == "m2_completion_ci_gated_operator_visible"
    assert payload["summary"]["m7_operator_cockpit_benchmark_posture"] == "m7_ci_gated_operator_visible"
    assert payload["summary"]["cockpit_efficiency_benchmark_posture"] == "cockpit_efficiency_ci_gated_operator_visible"
    assert payload["summary"]["m8_guardian_brain_benchmark_posture"] == "m8_ci_gated_operator_visible"
    assert (
        payload["summary"]["guardian_safe_multimodal_voice_benchmark_posture"]
        == "guardian_safe_multimodal_voice_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["guardian_safe_multimodal_voice_claim_boundary"]
        == "governed_voice_media_proof_not_live_broad_multimodal_runtime_or_voice_parity"
    )
    assert (
        payload["summary"]["production_reach_browser_voice_posture"]
        == "production_reach_browser_voice_ci_gated_operator_visible"
    )
    assert payload["summary"]["production_reach_browser_voice_claim_boundary"] == (
        "production_reach_browser_voice_receipts_not_broad_reach_voice_or_browser_parity"
    )
    assert (
        payload["summary"]["live_reach_media_posture"]
        == "live_reach_media_ci_gated_operator_visible"
    )
    assert payload["summary"]["live_reach_media_claim_boundary"] == LIVE_REACH_MEDIA_CLAIM_BOUNDARY
    assert (
        payload["summary"]["production_reach_voice_mobile_posture"]
        == "production_reach_voice_mobile_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["production_reach_voice_mobile_claim_boundary"]
        == PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["broad_reach_field_ops_posture"]
        == "broad_reach_field_ops_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["broad_reach_field_ops_claim_boundary"]
        == BROAD_REACH_FIELD_OPS_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["guardian_learning_arbitration_benchmark_posture"]
        == "guardian_learning_arbitration_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["guardian_learning_arbitration_claim_boundary"]
        == "deterministic_learning_arbitration_receipts_not_guardian_intelligence_superiority"
    )
    assert (
        payload["summary"]["live_guardian_learning_quality_posture"]
        == "live_guardian_learning_quality_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["live_guardian_learning_quality_claim_boundary"]
        == LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["live_human_outcome_learning_posture"]
        == "live_human_outcome_learning_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["live_human_outcome_learning_claim_boundary"]
        == LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["independent_learning_memory_parity_posture"]
        == "independent_learning_memory_parity_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["independent_learning_memory_parity_claim_boundary"]
        == INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY
    )
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
    assert (
        payload["summary"]["governed_capability_pack_hardening_posture"]
        == "governed_pack_hardening_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["governed_capability_pack_hardening_claim_boundary"]
        == "governed_capability_pack_hardening_receipts_not_production_marketplace_security_or_ecosystem_maturity_or_package_count_superiority"
    )
    assert (
        payload["summary"]["marketplace_lifecycle_maturity_posture"]
        == "marketplace_lifecycle_maturity_ci_gated_operator_visible"
    )
    assert payload["summary"]["marketplace_lifecycle_maturity_claim_boundary"] == MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY
    assert (
        payload["summary"]["live_marketplace_attestation_posture"]
        == "live_marketplace_attestation_ci_gated_operator_visible"
    )
    assert payload["summary"]["live_marketplace_attestation_claim_boundary"] == (
        LIVE_MARKETPLACE_ATTESTATION_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["production_marketplace_security_posture"]
        == "production_marketplace_security_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["production_marketplace_security_claim_boundary"]
        == PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["browser_provider_usability_posture"]
        == "browser_provider_usability_ci_gated_operator_visible"
    )
    assert payload["summary"]["browser_provider_usability_claim_boundary"] == (
        BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["safe_browser_computer_use_posture"]
        == "safe_browser_computer_use_ci_gated_operator_visible"
    )
    assert payload["summary"]["safe_browser_computer_use_claim_boundary"] == (
        SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["production_operator_control_parity_posture"]
        == "production_operator_control_parity_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["production_operator_control_parity_claim_boundary"]
        == PRODUCTION_OPERATOR_CONTROL_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["dense_operator_recovery_control_posture"]
        == "dense_operator_recovery_control_ci_gated_operator_visible"
    )
    assert (
        payload["summary"]["dense_operator_recovery_control_claim_boundary"]
        == DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY
    )
    assert (
        payload["summary"]["final_parity_readiness_posture"]
        == "final_parity_audit_ci_gated_operator_visible"
    )
    assert payload["summary"]["final_parity_readiness_claim_boundary"] == FINAL_PARITY_AUDIT_CLAIM_BOUNDARY
    assert payload["summary"]["m2_completion_state"] == "ready_to_close_m2"
    assert payload["summary"]["governed_improvement_benchmark_posture"] == "ci_gated_operator_visible"
    assert payload["m5_operating_layer_benchmark"]["summary"]["suite_name"] == "m5_jobs_routines_workflows_delegation"
    assert payload["durable_workflow_engine"]["summary"]["suite_name"] == "durable_workflow_engine_v1"
    assert payload["dense_operator_recovery_control"]["summary"]["task_matrix_count"] >= 8
    assert payload["dense_operator_recovery_control"]["policy"]["claim_boundary"] == DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY
    assert payload["governed_improvement"]["target_count"] == 2
    assert payload["governed_improvement"]["target_types"] == ["prompt_pack", "skill"]
    assert payload["governed_improvement"]["gate_policy"]["requires_human_review"] is True
    assert payload["governed_improvement"]["summary"]["suite_name"] == "governed_improvement"
    assert payload["governed_improvement"]["summary"]["canary_rollout_state"] == "review_candidates_canary_only"
    assert payload["governed_improvement"]["policy"]["rollback_policy"] == "candidate_receipt_and_source_baseline_required_before_promotion"
    assert payload["governed_improvement"]["recent_receipts"][0]["acceptance_state"] == "ready_for_canary"
    assert "guardian_memory_quality" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "memory_continuity_workflows" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "production_parity_readiness" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "m9_governed_ecosystem" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert (
        "guardian_safe_multimodal_voice"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "guardian_learning_arbitration_v2"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert "live_guardian_learning_quality" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert (
        "guardian_intervention_outcome_cohorts"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "memory_provider_ecosystem_maturity_v1"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert "long_work_debugging_recovery" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "operator_control_density" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert (
        "independent_operator_usability_accessibility"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "canonical_memory_reconciliation_v2"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert "provider_usefulness_regression" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert (
        "live_human_outcome_quality_study"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "guardian_learning_causal_attribution"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "memory_provider_live_regression_monitor"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "governed_capability_pack_hardening"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "marketplace_grade_capability_lifecycle"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "governed_capability_lifecycle_v2"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "capability_rollback_failure_diagnostics"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "third_party_marketplace_attestation"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "marketplace_operations_incident_drill"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "publisher_review_and_package_trust"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "production_operator_control_parity"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "production_parity_train"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert "live_workflow_endurance_canary" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "one_excellent_reach_channel_canary" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "durable_workflow_engine_v1" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert "durable_workflow_engine_v2" in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    assert (
        "production_durable_orchestration"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "live_external_orchestration_attestation"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "orchestration_crash_recovery_study"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "production_isolation_hardening_v2"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "privileged_path_red_team_gauntlet_v2"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "security_incident_recovery_drill"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "container_grade_capability_isolation"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "external_security_validation_v1"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "secret_egress_certification_drill"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "broad_reach_field_operations"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "voice_media_quality_operations"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "always_available_reach_slo"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "production_reach_channel_hardening"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "browser_computer_use_reliability_v2"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "guardian_safe_voice_media_runtime"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "live_broad_reach_channel_attestation"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "production_voice_media_provider_runtime"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )
    assert (
        "cross_surface_continuity_recovery"
        in payload["governed_improvement"]["gate_policy"]["required_benchmark_suites"]
    )

    guardian_memory_suite = next(item for item in payload["suites"] if item["name"] == "guardian_memory_quality")
    assert "memory_contradiction_ranking_behavior" in guardian_memory_suite["scenario_names"]
    assert guardian_memory_suite["scenario_count"] >= 8
    user_model_suite = next(item for item in payload["suites"] if item["name"] == "guardian_user_model_restraint")
    assert "guardian_clarification_restraint_behavior" in user_model_suite["scenario_names"]
    assert user_model_suite["scenario_count"] >= 4

    production_parity_readiness_suite = next(
        item for item in payload["suites"] if item["name"] == "production_parity_readiness"
    )
    assert "production_parity_claim_gate_behavior" in production_parity_readiness_suite["scenario_names"]
    assert production_parity_readiness_suite["scenario_count"] == len(PRODUCTION_PARITY_READINESS_SCENARIO_NAMES)
    assert payload["production_parity_readiness"]["summary"]["completion_state"] == (
        "readiness_contract_only_full_parity_unproven"
    )
    assert "fully_at_parity" in payload["production_parity_readiness"]["policy"]["blocked_claims"]

    memory_suite = next(item for item in payload["suites"] if item["name"] == "memory_continuity_workflows")
    assert "workflow_operating_layer_behavior" in memory_suite["scenario_names"]
    assert memory_suite["scenario_count"] >= 10
    workflow_suite = next(item for item in payload["suites"] if item["name"] == "workflow_endurance_and_repair")
    assert "workflow_anticipatory_repair_behavior" in workflow_suite["scenario_names"]
    assert workflow_suite["scenario_count"] >= 4
    live_workflow_canary_suite = next(item for item in payload["suites"] if item["name"] == "live_workflow_endurance_canary")
    assert "live_workflow_canary_protocol_behavior" in live_workflow_canary_suite["scenario_names"]
    assert live_workflow_canary_suite["scenario_count"] == 4
    durable_workflow_engine_suite = next(item for item in payload["suites"] if item["name"] == "durable_workflow_engine_v1")
    assert "durable_workflow_state_kernel_behavior" in durable_workflow_engine_suite["scenario_names"]
    assert durable_workflow_engine_suite["scenario_count"] == len(DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES)
    production_durable_orchestration_suite = next(
        item for item in payload["suites"] if item["name"] == "production_durable_orchestration"
    )
    assert "production_durable_orchestration_claim_boundary_behavior" in production_durable_orchestration_suite["scenario_names"]
    assert production_durable_orchestration_suite["scenario_count"] == len(PRODUCTION_DURABLE_ORCHESTRATION_SCENARIO_NAMES)
    durable_workflow_engine_v2_suite = next(item for item in payload["suites"] if item["name"] == "durable_workflow_engine_v2")
    assert "durable_workflow_v2_lease_ownership_behavior" in durable_workflow_engine_v2_suite["scenario_names"]
    assert durable_workflow_engine_v2_suite["scenario_count"] == len(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES)
    assert payload["durable_workflow_engine_v2"]["summary"]["suite_name"] == "durable_workflow_engine_v2"
    assert "langgraph_class_durable_workflows" in payload["durable_workflow_engine_v2"]["policy"]["blocked_claims"]
    live_external_orchestration_suite = next(
        item for item in payload["suites"] if item["name"] == "live_external_orchestration_attestation"
    )
    assert (
        "live_external_scheduler_provider_identity_behavior"
        in live_external_orchestration_suite["scenario_names"]
    )
    assert live_external_orchestration_suite["scenario_count"] == len(LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES)
    crash_recovery_suite = next(
        item for item in payload["suites"] if item["name"] == "orchestration_crash_recovery_study"
    )
    assert "orchestration_crash_checkpoint_recovery_behavior" in crash_recovery_suite["scenario_names"]
    assert crash_recovery_suite["scenario_count"] == len(ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES)
    production_sla_suite = next(
        item for item in payload["suites"] if item["name"] == "production_sla_orchestration"
    )
    assert "production_sla_provider_window_behavior" in production_sla_suite["scenario_names"]
    assert production_sla_suite["scenario_count"] == len(PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES)
    exactly_once_suite = next(
        item for item in payload["suites"] if item["name"] == "exactly_once_recovery_evidence"
    )
    assert "exactly_once_idempotency_scope_behavior" in exactly_once_suite["scenario_names"]
    assert exactly_once_suite["scenario_count"] == len(EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES)
    duplicate_audit_suite = next(
        item for item in payload["suites"] if item["name"] == "duplicate_side_effect_audit"
    )
    assert "duplicate_side_effect_audit_receipt_behavior" in duplicate_audit_suite["scenario_names"]
    assert duplicate_audit_suite["scenario_count"] == len(DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES)
    assert payload["production_sla_orchestration"]["summary"]["operator_status"] == (
        "production_sla_orchestration_receipts_visible"
    )
    assert "exactly_once_production_scheduling" in payload["production_sla_orchestration"]["policy"]["blocked_claims"]
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
    production_secure_host_suite = next(
        item for item in payload["suites"] if item["name"] == "production_secure_host_hardening"
    )
    assert "production_secure_host_receipt_schema_behavior" in production_secure_host_suite["scenario_names"]
    assert production_secure_host_suite["scenario_count"] == 4
    secure_host_live_v2_suite = next(
        item for item in payload["suites"] if item["name"] == "secure_capability_host_live_isolation_v2"
    )
    assert "secure_host_live_private_network_egress_behavior" in secure_host_live_v2_suite["scenario_names"]
    assert "secure_host_live_extension_revocation_behavior" in secure_host_live_v2_suite["scenario_names"]
    assert secure_host_live_v2_suite["scenario_count"] == 5
    production_isolation_suite = next(
        item for item in payload["suites"] if item["name"] == "production_isolation_hardening_v2"
    )
    assert "production_isolation_worker_boundary_behavior" in production_isolation_suite["scenario_names"]
    assert production_isolation_suite["scenario_count"] == len(PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES)
    red_team_suite = next(
        item for item in payload["suites"] if item["name"] == "privileged_path_red_team_gauntlet_v2"
    )
    assert "red_team_secret_replay_exfiltration_behavior" in red_team_suite["scenario_names"]
    assert red_team_suite["scenario_count"] == len(PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES)
    incident_drill_suite = next(
        item for item in payload["suites"] if item["name"] == "security_incident_recovery_drill"
    )
    assert "security_incident_revocation_drill_behavior" in incident_drill_suite["scenario_names"]
    assert incident_drill_suite["scenario_count"] == len(SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES)
    independent_review_suite = next(
        item for item in payload["suites"] if item["name"] == "independent_secure_host_review"
    )
    assert "independent_security_review_scope_behavior" in independent_review_suite["scenario_names"]
    assert independent_review_suite["scenario_count"] == len(INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES)
    hostile_drill_suite = next(item for item in payload["suites"] if item["name"] == "live_hostile_isolation_drills")
    assert "live_hostile_credential_exfiltration_drill_behavior" in hostile_drill_suite["scenario_names"]
    assert hostile_drill_suite["scenario_count"] == len(LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES)
    recovery_authority_suite = next(item for item in payload["suites"] if item["name"] == "secure_host_recovery_authority")
    assert "secure_host_post_incident_audit_behavior" in recovery_authority_suite["scenario_names"]
    assert recovery_authority_suite["scenario_count"] == len(SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES)
    assert payload["independent_secure_host_review"]["summary"]["operator_status"] == (
        "independent_secure_host_review_receipts_visible"
    )
    assert set(INDEPENDENT_SECURE_HOST_REVIEW_BLOCKED_CLAIMS) <= set(
        payload["independent_secure_host_review"]["policy"]["blocked_claims"]
    )
    container_grade_suite = next(
        item for item in payload["suites"] if item["name"] == "container_grade_capability_isolation"
    )
    assert "container_grade_isolation_model_behavior" in container_grade_suite["scenario_names"]
    assert container_grade_suite["scenario_count"] == len(CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES)
    external_validation_suite = next(
        item for item in payload["suites"] if item["name"] == "external_security_validation_v1"
    )
    assert "external_security_review_scope_behavior" in external_validation_suite["scenario_names"]
    assert external_validation_suite["scenario_count"] == len(EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES)
    secret_egress_suite = next(
        item for item in payload["suites"] if item["name"] == "secret_egress_certification_drill"
    )
    assert "secret_egress_raw_value_denial_behavior" in secret_egress_suite["scenario_names"]
    assert secret_egress_suite["scenario_count"] == len(SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES)
    assert payload["container_grade_secure_host"]["summary"]["operator_status"] == (
        "container_grade_secure_host_validation_visible"
    )
    assert set(CONTAINER_GRADE_SECURE_HOST_BLOCKED_CLAIMS) <= set(
        payload["container_grade_secure_host"]["policy"]["blocked_claims"]
    )
    assert "hardware_backed_isolation" in payload["container_grade_secure_host"]["policy"]["blocked_claims"]

    computer_suite = next(item for item in payload["suites"] if item["name"] == "computer_use_browser_desktop")
    assert "browser_execution_task_replay_behavior" in computer_suite["scenario_names"]
    one_reach_channel_suite = next(item for item in payload["suites"] if item["name"] == "one_excellent_reach_channel_canary")
    assert "one_reach_channel_selection_scope_behavior" in one_reach_channel_suite["scenario_names"]
    assert one_reach_channel_suite["scenario_count"] == 5
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
    multimodal_voice_suite = next(item for item in payload["suites"] if item["name"] == "guardian_safe_multimodal_voice")
    assert "multimodal_voice_capability_governance_behavior" in multimodal_voice_suite["scenario_names"]
    assert multimodal_voice_suite["scenario_count"] == 6
    production_reach_suite = next(
        item for item in payload["suites"] if item["name"] == "production_reach_channel_hardening"
    )
    assert "production_reach_external_messaging_pairing_behavior" in production_reach_suite["scenario_names"]
    assert production_reach_suite["scenario_count"] == len(PRODUCTION_REACH_CHANNEL_HARDENING_SCENARIO_NAMES)
    browser_reliability_suite = next(
        item for item in payload["suites"] if item["name"] == "browser_computer_use_reliability_v2"
    )
    assert "browser_reliability_session_partition_behavior" in browser_reliability_suite["scenario_names"]
    assert browser_reliability_suite["scenario_count"] == len(BROWSER_COMPUTER_USE_RELIABILITY_V2_SCENARIO_NAMES)
    voice_runtime_suite = next(
        item for item in payload["suites"] if item["name"] == "guardian_safe_voice_media_runtime"
    )
    assert "voice_media_runtime_guardian_value_behavior" in voice_runtime_suite["scenario_names"]
    assert voice_runtime_suite["scenario_count"] == len(GUARDIAN_SAFE_VOICE_MEDIA_RUNTIME_SCENARIO_NAMES)
    live_reach_suite = next(
        item for item in payload["suites"] if item["name"] == "live_broad_reach_channel_attestation"
    )
    assert "live_reach_mobile_push_identity_consent_behavior" in live_reach_suite["scenario_names"]
    assert live_reach_suite["scenario_count"] == len(LIVE_BROAD_REACH_CHANNEL_ATTESTATION_SCENARIO_NAMES)
    voice_provider_suite = next(
        item for item in payload["suites"] if item["name"] == "production_voice_media_provider_runtime"
    )
    assert "voice_media_stt_provider_consent_capture_behavior" in voice_provider_suite["scenario_names"]
    assert voice_provider_suite["scenario_count"] == len(PRODUCTION_VOICE_MEDIA_PROVIDER_RUNTIME_SCENARIO_NAMES)
    continuity_recovery_suite = next(
        item for item in payload["suites"] if item["name"] == "cross_surface_continuity_recovery"
    )
    assert "cross_surface_browser_desktop_mobile_handoff_behavior" in continuity_recovery_suite["scenario_names"]
    assert continuity_recovery_suite["scenario_count"] == len(CROSS_SURFACE_CONTINUITY_RECOVERY_SCENARIO_NAMES)
    learning_arbitration_suite = next(item for item in payload["suites"] if item["name"] == "guardian_learning_arbitration_v2")
    assert "guardian_learning_arbitration_act_behavior" in learning_arbitration_suite["scenario_names"]
    assert learning_arbitration_suite["scenario_count"] == 7
    live_learning_suite = next(item for item in payload["suites"] if item["name"] == "live_guardian_learning_quality")
    assert "live_learning_policy_delta_behavior" in live_learning_suite["scenario_names"]
    assert live_learning_suite["scenario_count"] == len(LIVE_GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES)
    outcome_cohorts_suite = next(item for item in payload["suites"] if item["name"] == "guardian_intervention_outcome_cohorts")
    assert "intervention_outcome_harmful_behavior" in outcome_cohorts_suite["scenario_names"]
    assert outcome_cohorts_suite["scenario_count"] == len(GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SCENARIO_NAMES)
    provider_ecosystem_suite = next(
        item for item in payload["suites"] if item["name"] == "memory_provider_ecosystem_maturity_v1"
    )
    assert "memory_provider_usefulness_metric_behavior" in provider_ecosystem_suite["scenario_names"]
    assert provider_ecosystem_suite["scenario_count"] == len(MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SCENARIO_NAMES)
    canonical_reconciliation_suite = next(
        item for item in payload["suites"] if item["name"] == "canonical_memory_reconciliation_v2"
    )
    assert "canonical_memory_delete_export_receipt_behavior" in canonical_reconciliation_suite["scenario_names"]
    assert canonical_reconciliation_suite["scenario_count"] == len(CANONICAL_MEMORY_RECONCILIATION_V2_SCENARIO_NAMES)
    provider_regression_suite = next(item for item in payload["suites"] if item["name"] == "provider_usefulness_regression")
    assert "provider_usefulness_quarantine_regression_behavior" in provider_regression_suite["scenario_names"]
    assert provider_regression_suite["scenario_count"] == len(PROVIDER_USEFULNESS_REGRESSION_SCENARIO_NAMES)
    human_outcome_suite = next(item for item in payload["suites"] if item["name"] == "live_human_outcome_quality_study")
    assert "live_human_outcome_cohort_consent_behavior" in human_outcome_suite["scenario_names"]
    assert human_outcome_suite["scenario_count"] == len(LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SCENARIO_NAMES)
    causal_suite = next(item for item in payload["suites"] if item["name"] == "guardian_learning_causal_attribution")
    assert "causal_attribution_counterfactual_restraint_behavior" in causal_suite["scenario_names"]
    assert causal_suite["scenario_count"] == len(GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SCENARIO_NAMES)
    provider_monitor_suite = next(
        item for item in payload["suites"] if item["name"] == "memory_provider_live_regression_monitor"
    )
    assert "memory_provider_live_usefulness_delta_behavior" in provider_monitor_suite["scenario_names"]
    assert provider_monitor_suite["scenario_count"] == len(MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SCENARIO_NAMES)
    m6_memory_suite = next(item for item in payload["suites"] if item["name"] == "m6_memory_superiority")
    assert "m6_long_horizon_recall_behavior" in m6_memory_suite["scenario_names"]
    assert m6_memory_suite["scenario_count"] == 7
    memory_provider_gate_suite = next(item for item in payload["suites"] if item["name"] == "memory_provider_quality_gate")
    assert "memory_provider_quality_gate_contract_behavior" in memory_provider_gate_suite["scenario_names"]
    assert memory_provider_gate_suite["scenario_count"] == 4
    m9_suite = next(item for item in payload["suites"] if item["name"] == "m9_governed_ecosystem")
    assert "m9_manifest_governance_behavior" in m9_suite["scenario_names"]
    assert m9_suite["scenario_count"] == 6
    hardening_suite = next(item for item in payload["suites"] if item["name"] == "governed_capability_pack_hardening")
    assert "capability_pack_review_receipt_behavior" in hardening_suite["scenario_names"]
    assert hardening_suite["scenario_count"] == 6
    marketplace_lifecycle_suite = next(
        item for item in payload["suites"] if item["name"] == "marketplace_grade_capability_lifecycle"
    )
    assert "marketplace_lifecycle_install_receipt_behavior" in marketplace_lifecycle_suite["scenario_names"]
    assert marketplace_lifecycle_suite["scenario_count"] == len(MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES)
    governed_lifecycle_suite = next(
        item for item in payload["suites"] if item["name"] == "governed_capability_lifecycle_v2"
    )
    assert "capability_lifecycle_permission_delta_behavior" in governed_lifecycle_suite["scenario_names"]
    assert governed_lifecycle_suite["scenario_count"] == len(GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES)
    rollback_diagnostics_suite = next(
        item for item in payload["suites"] if item["name"] == "capability_rollback_failure_diagnostics"
    )
    assert "capability_failed_update_recovery_behavior" in rollback_diagnostics_suite["scenario_names"]
    assert rollback_diagnostics_suite["scenario_count"] == len(CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES)
    attestation_suite = next(
        item for item in payload["suites"] if item["name"] == "third_party_marketplace_attestation"
    )
    assert "third_party_package_provenance_signature_behavior" in attestation_suite["scenario_names"]
    assert attestation_suite["scenario_count"] == len(THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES)
    operations_suite = next(
        item for item in payload["suites"] if item["name"] == "marketplace_operations_incident_drill"
    )
    assert "marketplace_malicious_package_quarantine_behavior" in operations_suite["scenario_names"]
    assert operations_suite["scenario_count"] == len(MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SCENARIO_NAMES)
    publisher_suite = next(
        item for item in payload["suites"] if item["name"] == "publisher_review_and_package_trust"
    )
    assert "publisher_review_staleness_behavior" in publisher_suite["scenario_names"]
    package_review_suite = next(
        item for item in payload["suites"] if item["name"] == "independent_package_security_review"
    )
    assert "independent_package_review_scope_behavior" in package_review_suite["scenario_names"]
    assert package_review_suite["scenario_count"] == len(INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES)
    hostile_suite = next(item for item in payload["suites"] if item["name"] == "hostile_ecosystem_package_drills")
    assert "hostile_dependency_confusion_fail_closed_behavior" in hostile_suite["scenario_names"]
    assert hostile_suite["scenario_count"] == len(HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES)
    package_network_suite = next(
        item for item in payload["suites"] if item["name"] == "package_network_incident_operations"
    )
    assert "package_network_private_ssrf_denial_behavior" in package_network_suite["scenario_names"]
    assert package_network_suite["scenario_count"] == len(PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES)
    publisher_vulnerability_suite = next(
        item for item in payload["suites"] if item["name"] == "publisher_trust_vulnerability_handling"
    )
    assert "vulnerability_database_freshness_behavior" in publisher_vulnerability_suite["scenario_names"]
    assert publisher_vulnerability_suite["scenario_count"] == len(PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES)
    rollback_quarantine_suite = next(
        item for item in payload["suites"] if item["name"] == "marketplace_rollback_quarantine_diagnostics"
    )
    assert "marketplace_update_failed_rollback_behavior" in rollback_quarantine_suite["scenario_names"]
    assert rollback_quarantine_suite["scenario_count"] == len(MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES)
    managed_browser_suite = next(
        item for item in payload["suites"] if item["name"] == "managed_browser_provider_attestation"
    )
    assert "managed_browser_provider_identity_evidence_behavior" in managed_browser_suite["scenario_names"]
    assert managed_browser_suite["scenario_count"] == len(MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES)
    multi_operator_suite = next(
        item for item in payload["suites"] if item["name"] == "live_multi_operator_usability_study"
    )
    assert "multi_operator_inspect_recover_handoff_behavior" in multi_operator_suite["scenario_names"]
    assert multi_operator_suite["scenario_count"] == len(LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES)
    recovery_drill_suite = next(
        item for item in payload["suites"] if item["name"] == "browser_computer_use_recovery_drill"
    )
    assert "browser_recovery_provider_crash_behavior" in recovery_drill_suite["scenario_names"]
    assert recovery_drill_suite["scenario_count"] == len(BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES)
    broad_channel_suite = next(
        item for item in payload["suites"] if item["name"] == "broad_channel_sla_operations"
    )
    assert "channel_sla_provider_window_behavior" in broad_channel_suite["scenario_names"]
    assert broad_channel_suite["scenario_count"] == len(BROAD_CHANNEL_SLA_OPERATIONS_SCENARIO_NAMES)
    voice_quality_suite = next(
        item for item in payload["suites"] if item["name"] == "production_voice_media_quality_gates"
    )
    assert "voice_media_stt_quality_gate_behavior" in voice_quality_suite["scenario_names"]
    assert voice_quality_suite["scenario_count"] == len(PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SCENARIO_NAMES)
    mobile_execution_suite = next(
        item for item in payload["suites"] if item["name"] == "mobile_execution_continuity"
    )
    assert "mobile_execution_notification_approval_handoff_behavior" in mobile_execution_suite["scenario_names"]
    assert mobile_execution_suite["scenario_count"] == len(MOBILE_EXECUTION_CONTINUITY_SCENARIO_NAMES)
    broad_field_ops_suite = next(
        item for item in payload["suites"] if item["name"] == "broad_reach_field_operations"
    )
    assert "broad_reach_provider_matrix_behavior" in broad_field_ops_suite["scenario_names"]
    assert broad_field_ops_suite["scenario_count"] == len(BROAD_REACH_FIELD_OPERATIONS_SCENARIO_NAMES)
    voice_ops_suite = next(
        item for item in payload["suites"] if item["name"] == "voice_media_quality_operations"
    )
    assert "voice_media_field_quality_gate_behavior" in voice_ops_suite["scenario_names"]
    assert voice_ops_suite["scenario_count"] == len(VOICE_MEDIA_QUALITY_OPERATIONS_SCENARIO_NAMES)
    reach_slo_suite = next(item for item in payload["suites"] if item["name"] == "always_available_reach_slo")
    assert "reach_slo_window_budget_behavior" in reach_slo_suite["scenario_names"]
    assert reach_slo_suite["scenario_count"] == len(ALWAYS_AVAILABLE_REACH_SLO_SCENARIO_NAMES)
    assert publisher_suite["scenario_count"] == len(PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SCENARIO_NAMES)
    operator_control_suite = next(
        item for item in payload["suites"] if item["name"] == "production_operator_control_parity"
    )
    assert "operator_control_train_receipt_behavior" in operator_control_suite["scenario_names"]
    assert operator_control_suite["scenario_count"] == len(PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES)
    parity_train_suite = next(item for item in payload["suites"] if item["name"] == "production_parity_train")
    assert "production_parity_train_batch_merge_receipt_behavior" in parity_train_suite["scenario_names"]
    assert parity_train_suite["scenario_count"] == len(PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES)
    final_source_suite = next(item for item in payload["suites"] if item["name"] == "final_source_backed_parity_audit")
    assert "final_current_source_coverage_behavior" in final_source_suite["scenario_names"]
    assert final_source_suite["scenario_count"] == len(FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES)
    final_claim_suite = next(item for item in payload["suites"] if item["name"] == "final_claim_ledger_reconciliation")
    assert "final_forbidden_claim_block_behavior" in final_claim_suite["scenario_names"]
    assert final_claim_suite["scenario_count"] == len(FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES)
    final_operator_suite = next(
        item for item in payload["suites"] if item["name"] == "operator_final_parity_readiness_report"
    )
    assert "operator_final_no_false_completion_behavior" in final_operator_suite["scenario_names"]
    assert final_operator_suite["scenario_count"] == len(OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES)
    assert payload["final_parity_readiness"]["summary"]["operator_status"] == "final_parity_readiness_report_visible"
    assert payload["final_parity_readiness"]["summary"]["completed_batch_count"] == 22
    assert payload["final_parity_readiness"]["summary"]["bounded_parity_proof_train_completion_wording_allowed"] is True
    assert "fully_at_parity" in payload["final_parity_readiness"]["policy"]["blocked_claims"]
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
    assert payload["one_reach_channel_canary"]["summary"]["suite_name"] == "one_excellent_reach_channel_canary"
    assert payload["one_reach_channel_canary"]["summary"]["selected_channel"] == "native_notification"
    assert payload["one_reach_channel_canary"]["policy"]["claim_boundary"] == (
        "deterministic_native_notification_canary_not_broad_live_channel_reach"
    )
    assert payload["trust_boundary_benchmark"]["summary"]["suite_name"] == "trust_boundary_and_safety_receipts"
    assert payload["trust_boundary_benchmark"]["policy"]["secret_egress_policy"] == "field_scoped_secret_refs_plus_required_credential_egress_allowlist"
    assert payload["secure_capability_host_benchmark"]["summary"]["suite_name"] == "secure_capability_host"
    assert payload["secure_capability_host_benchmark"]["policy"]["claim_boundary"] == "deterministic_secure_host_choke_points_not_full_host_container_isolation"
    assert payload["production_secure_host_hardening"]["summary"]["suite_name"] == "production_secure_host_hardening"
    assert payload["production_secure_host_hardening"]["policy"]["claim_boundary"] == (
        "production_secure_host_hardening_proof_not_secure_private_by_default_or_ironclaw_class"
    )
    assert (
        payload["production_isolation_security"]["summary"]["benchmark_posture"]
        == "production_isolation_security_ci_gated_operator_visible"
    )
    assert payload["production_isolation_security"]["policy"]["claim_boundary"] == (
        PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY
    )
    assert "ironclaw_class_secure_execution" in payload["production_isolation_security"]["policy"]["blocked_claims"]
    assert payload["governed_capability_pack_hardening"]["summary"]["suite_name"] == "governed_capability_pack_hardening"
    assert payload["governed_capability_pack_hardening"]["policy"]["ci_gate_mode"] == "required_benchmark_suite"
    assert payload["marketplace_lifecycle_maturity"]["summary"]["operator_status"] == (
        "marketplace_lifecycle_maturity_receipts_visible"
    )
    assert payload["marketplace_lifecycle_maturity"]["summary"]["lifecycle_action_count"] == 9
    assert payload["marketplace_lifecycle_maturity"]["summary"]["family_count"] == 11
    assert payload["marketplace_lifecycle_maturity"]["summary"]["negative_case_count"] == 5
    assert payload["marketplace_lifecycle_maturity"]["summary"]["staged_rollout_count"] == 2
    assert payload["marketplace_lifecycle_maturity"]["policy"]["claim_boundary"] == MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY
    assert "production_secure_marketplace" in payload["marketplace_lifecycle_maturity"]["policy"]["blocked_claims"]
    assert payload["live_marketplace_attestation"]["summary"]["operator_status"] == (
        "live_marketplace_attestation_receipts_visible"
    )
    assert payload["live_marketplace_attestation"]["summary"]["attested_package_count"] == 4
    assert payload["live_marketplace_attestation"]["summary"]["recorded_live_operation_count"] == 6
    assert payload["live_marketplace_attestation"]["summary"]["publisher_review_count"] == 4
    assert payload["live_marketplace_attestation"]["summary"]["blocked_attestation_count"] == 1
    assert payload["live_marketplace_attestation"]["summary"]["fail_closed_operation_count"] == 4
    assert payload["live_marketplace_attestation"]["policy"]["claim_boundary"] == (
        LIVE_MARKETPLACE_ATTESTATION_CLAIM_BOUNDARY
    )
    assert "production_secure_marketplace" in payload["live_marketplace_attestation"]["policy"]["blocked_claims"]
    assert payload["production_marketplace_security"]["summary"]["operator_status"] == (
        "production_marketplace_security_receipts_visible"
    )
    assert payload["production_marketplace_security"]["summary"]["hostile_drill_count"] == 8
    assert payload["production_marketplace_security"]["summary"]["package_network_incident_count"] == 6
    assert payload["production_marketplace_security"]["summary"]["production_secure_marketplace_claim_allowed"] is False
    assert payload["production_marketplace_security"]["policy"]["claim_boundary"] == (
        PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY
    )
    assert "third_party_package_security_solved" in payload["production_marketplace_security"]["policy"]["blocked_claims"]
    assert payload["browser_provider_usability"]["summary"]["operator_status"] == (
        "browser_provider_usability_receipts_visible"
    )
    assert payload["browser_provider_usability"]["summary"]["provider_attestation_count"] == 3
    assert payload["browser_provider_usability"]["summary"]["multi_operator_task_count"] == 3
    assert payload["browser_provider_usability"]["summary"]["recovery_drill_count"] == 4
    assert payload["browser_provider_usability"]["summary"]["fail_closed_recovery_count"] == 4
    assert payload["browser_provider_usability"]["policy"]["claim_boundary"] == (
        BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY
    )
    assert "safe_browser_automation" in payload["browser_provider_usability"]["policy"]["blocked_claims"]
    assert payload["safe_browser_computer_use"]["summary"]["operator_status"] == (
        "safe_browser_computer_use_receipts_visible"
    )
    assert payload["safe_browser_computer_use"]["summary"]["dangerous_action_default_block_count"] == 7
    assert payload["safe_browser_computer_use"]["summary"]["site_recovery_drill_count"] == 8
    assert payload["safe_browser_computer_use"]["summary"]["provider_reliability_receipt_count"] == 3
    assert payload["safe_browser_computer_use"]["summary"]["secret_or_credential_leak_count"] == 0
    assert payload["safe_browser_computer_use"]["policy"]["claim_boundary"] == (
        SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY
    )
    assert "full_browser_parity" in payload["safe_browser_computer_use"]["policy"]["blocked_claims"]
    assert payload["production_operator_control"]["summary"]["operator_status"] == (
        "production_operator_control_parity_receipts_visible"
    )
    assert payload["production_operator_control"]["summary"]["control_surface_count"] == 6
    assert payload["production_operator_control"]["summary"]["train_batch_count"] == 7
    assert payload["production_operator_control"]["summary"]["merged_prior_batch_count"] == 6
    assert payload["production_operator_control"]["summary"]["required_actions_visible"] is True
    assert payload["production_operator_control"]["policy"]["claim_boundary"] == PRODUCTION_OPERATOR_CONTROL_CLAIM_BOUNDARY
    assert set(PRODUCTION_OPERATOR_CONTROL_BLOCKED_CLAIMS) <= set(
        payload["production_operator_control"]["policy"]["blocked_claims"]
    )
    assert payload["live_external_orchestration"]["summary"]["operator_status"] == (
        "live_external_orchestration_receipts_visible"
    )
    assert payload["live_external_orchestration"]["summary"]["provider_receipt_count"] == 3
    assert payload["live_external_orchestration"]["summary"]["crash_study_count"] == 3
    assert payload["live_external_orchestration"]["summary"]["required_controls_visible"] is True
    assert payload["live_external_orchestration"]["policy"]["claim_boundary"] == (
        LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY
    )
    assert set(LIVE_EXTERNAL_ORCHESTRATION_BLOCKED_CLAIMS) <= set(
        payload["live_external_orchestration"]["policy"]["blocked_claims"]
    )
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
    assert payload["guardian_safe_multimodal_voice_benchmark"]["summary"]["suite_name"] == "guardian_safe_multimodal_voice"
    assert payload["guardian_safe_multimodal_voice_benchmark"]["policy"]["guardian_value_policy"] == (
        "voice_media_must_improve_timing_accessibility_situational_awareness_or_intervention_quality"
    )
    assert payload["production_reach_browser_voice"]["summary"]["operator_status"] == (
        "production_reach_browser_voice_receipts_visible"
    )
    assert payload["production_reach_browser_voice"]["summary"]["paired_external_messaging_channel_count"] >= 1
    assert payload["production_reach_browser_voice"]["summary"]["browser_session_partition_count"] >= 2
    assert payload["production_reach_browser_voice"]["summary"]["voice_media_deletion_path_count"] >= 2
    assert payload["production_reach_browser_voice"]["policy"]["claim_boundary"] == PRODUCTION_REACH_BROWSER_VOICE_CLAIM_BOUNDARY
    assert payload["live_reach_media"]["summary"]["operator_status"] == "live_reach_media_receipts_visible"
    assert payload["live_reach_media"]["summary"]["recorded_live_channel_count"] >= 2
    assert payload["live_reach_media"]["summary"]["voice_media_provider_count"] >= 3
    assert payload["live_reach_media"]["summary"]["cross_surface_recovery_count"] >= 2
    assert payload["live_reach_media"]["policy"]["claim_boundary"] == LIVE_REACH_MEDIA_CLAIM_BOUNDARY
    assert payload["production_reach_voice_mobile"]["summary"]["operator_status"] == (
        "production_reach_voice_mobile_receipts_visible"
    )
    assert payload["production_reach_voice_mobile"]["summary"]["channel_provider_count"] >= 4
    assert payload["production_reach_voice_mobile"]["summary"]["voice_media_quality_gate_pass_count"] >= 3
    assert payload["production_reach_voice_mobile"]["summary"]["mobile_action_continuity_count"] >= 2
    assert (
        payload["production_reach_voice_mobile"]["policy"]["claim_boundary"]
        == PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY
    )
    assert payload["guardian_learning_arbitration_benchmark"]["summary"]["suite_name"] == "guardian_learning_arbitration_v2"
    assert payload["guardian_learning_arbitration_benchmark"]["policy"]["guardian_value_policy"] == (
        "learning_must_improve_restraint_clarification_timing_approval_recovery_or_follow_through_not_intervention_volume"
    )
    assert payload["live_guardian_learning_quality"]["summary"]["operator_status"] == (
        "live_guardian_learning_quality_receipts_visible"
    )
    assert payload["live_guardian_learning_quality"]["summary"]["outcome_cohort_count"] == 8
    assert payload["live_guardian_learning_quality"]["summary"]["provider_quarantine_count"] == 1
    assert payload["live_guardian_learning_quality"]["summary"]["delete_export_receipts_visible"] is True
    assert payload["live_guardian_learning_quality"]["policy"]["claim_boundary"] == (
        LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY
    )
    assert "guardian_intelligence_superiority" in payload["live_guardian_learning_quality"]["policy"]["blocked_claims"]
    assert payload["live_human_outcome_learning"]["summary"]["operator_status"] == (
        "live_human_outcome_learning_receipts_visible"
    )
    assert payload["live_human_outcome_learning"]["summary"]["study_mode"] == "recorded_live_anonymized"
    assert payload["live_human_outcome_learning"]["summary"]["outcome_cohort_count"] == 7
    assert payload["live_human_outcome_learning"]["summary"]["causal_attribution_count"] == 4
    assert payload["live_human_outcome_learning"]["summary"]["provider_monitor_count"] == 4
    assert payload["live_human_outcome_learning"]["policy"]["claim_boundary"] == (
        LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY
    )
    assert "live_human_outcome_superiority" in payload["live_human_outcome_learning"]["policy"]["blocked_claims"]
    assert payload["independent_learning_memory_parity"]["summary"]["operator_status"] == (
        "independent_learning_memory_parity_receipts_visible"
    )
    assert payload["independent_learning_memory_parity"]["summary"]["cohort_count"] == 3
    assert payload["independent_learning_memory_parity"]["summary"]["bounded_causal_claim_count"] == 3
    assert payload["independent_learning_memory_parity"]["summary"]["provider_parity_dimension_count"] >= 10
    assert payload["independent_learning_memory_parity"]["summary"]["secret_or_credential_leak_count"] == 0
    assert payload["independent_learning_memory_parity"]["policy"]["claim_boundary"] == (
        INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY
    )
    assert "full_memory_provider_parity" in (
        payload["independent_learning_memory_parity"]["policy"]["blocked_claims"]
    )
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
async def test_operator_governed_capability_pack_hardening_reports_policy_receipts_and_claim_boundary(client):
    resp = await client.get("/api/operator/governed-capability-pack-hardening")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "governed_capability_pack_hardening"
    assert payload["summary"]["operator_status"] == "governed_capability_pack_hardening_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["review_receipt_state"] == "risk_delta_and_blocked_claim_receipts_visible"
    assert payload["summary"]["rollback_state"] == "rollback_availability_and_action_visible"
    assert payload["summary"]["claim_boundary"] == (
        "governed_capability_pack_hardening_receipts_not_production_marketplace_security_or_ecosystem_maturity_or_package_count_superiority"
    )
    assert len(payload["dimensions"]) >= 6
    assert len(payload["failure_taxonomy"]) >= 7
    assert payload["policy"]["rollback_policy"] == (
        "install_update_and_downgrade_previews_expose_rollback_availability_and_action"
    )
    assert "/api/operator/governed-capability-pack-hardening" in payload["policy"]["receipt_surfaces"]
    hardening_receipts = {receipt["scenario_id"]: receipt for receipt in payload["hardening_receipts"]}
    assert "capability_pack_review_receipt_behavior" in hardening_receipts
    assert "package_count_superiority" in payload["policy"]["blocked_claims"]
    permission_receipt = hardening_receipts["capability_pack_permission_creep_behavior"]
    assert {"underdeclared_permissions", "extension_permission_creep"} <= set(permission_receipt["negative_cases"])
    assert {"complete_permission_declaration", "reviewed_permission_envelope"} <= set(permission_receipt["blocked_claims"])
    assert permission_receipt["fail_closed_required"] is True
    failed_update_receipt = hardening_receipts["capability_pack_supply_chain_suspicion_behavior"]
    assert "failed_update" in failed_update_receipt["negative_cases"]
    assert failed_update_receipt["runtime_access_removed"] is True
    rollback_receipt = hardening_receipts["capability_pack_rollback_ready_behavior"]
    assert {"remove_new_pack", "restore_previous_workspace_pack"} <= set(rollback_receipt["rollback_actions"])


@pytest.mark.asyncio
async def test_operator_marketplace_lifecycle_maturity_surface_reports_batch_ca_receipts(client):
    resp = await client.get("/api/operator/marketplace-lifecycle-maturity")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "marketplace_lifecycle_maturity_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "marketplace_lifecycle_maturity_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES)
        + len(GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES)
        + len(CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES)
    )
    assert payload["summary"]["lifecycle_action_count"] == 9
    assert payload["summary"]["family_count"] == 11
    assert payload["summary"]["negative_case_count"] == 5
    assert payload["summary"]["staged_rollout_count"] == 2
    assert payload["summary"]["failed_update_recovery_visible"] is True
    assert payload["summary"]["package_count_substitution_blocked"] is True
    assert payload["policy"]["claim_boundary"] == MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY
    assert set(MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/marketplace-lifecycle-maturity" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["marketplace_grade_capability_lifecycle"] == list(
        MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES
    )
    failed_update = next(
        item for item in payload["contract"]["negative_cases"] if item["case_id"] == "failed-update"
    )
    assert failed_update["state"] == "rolled_back"


@pytest.mark.asyncio
async def test_operator_live_marketplace_attestation_surface_reports_batch_cg_receipts(client):
    resp = await client.get("/api/operator/live-marketplace-attestation-proof")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "live_marketplace_attestation_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "live_marketplace_attestation_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES)
        + len(MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SCENARIO_NAMES)
        + len(PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SCENARIO_NAMES)
    )
    assert payload["summary"]["attested_package_count"] == 4
    assert payload["summary"]["recorded_live_operation_count"] == 6
    assert payload["summary"]["publisher_review_count"] == 4
    assert payload["summary"]["blocked_attestation_count"] == 1
    assert payload["summary"]["fail_closed_operation_count"] == 4
    assert payload["summary"]["package_count_substitution_blocked"] is True
    assert payload["policy"]["claim_boundary"] == LIVE_MARKETPLACE_ATTESTATION_CLAIM_BOUNDARY
    assert set(LIVE_MARKETPLACE_ATTESTATION_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/live-marketplace-attestation-proof" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["third_party_marketplace_attestation"] == list(
        THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES
    )
    suspicious = next(
        item for item in payload["contract"]["third_party_attestations"]
        if item["package_id"] == "marketplace.suspicious-exporter"
    )
    assert suspicious["signature_status"] == "missing"
    failed_update = next(
        item for item in payload["contract"]["operations"]
        if item["operation_id"] == "cg-failed-update-recovery"
    )
    assert failed_update["state"] == "rolled_back"


@pytest.mark.asyncio
async def test_operator_production_marketplace_security_surface_reports_batch_co_receipts(client):
    resp = await client.get("/api/operator/production-marketplace-security")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "production_marketplace_security_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "production_marketplace_security_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES)
        + len(HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES)
        + len(PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES)
        + len(PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES)
        + len(MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES)
    )
    assert payload["summary"]["independent_package_review_count"] == 4
    assert payload["summary"]["hostile_drill_count"] == 8
    assert payload["summary"]["package_network_incident_count"] == 6
    assert payload["summary"]["publisher_vulnerability_review_count"] == 5
    assert payload["summary"]["rollback_quarantine_diagnostic_count"] == 7
    assert payload["summary"]["production_secure_marketplace_claim_allowed"] is False
    assert payload["policy"]["claim_boundary"] == PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY
    assert set(PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/production-marketplace-security" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["independent_package_security_review"] == list(
        INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES
    )
    network_incident = next(
        item for item in payload["contract"]["package_network_incidents"]
        if item["package_network_incident_class"] == "secret_ref_injection"
    )
    assert network_incident["secret_ref_policy"] == "destination_host_mismatch_denied"
    stale_vulnerability = next(
        item for item in payload["contract"]["publisher_vulnerability_reviews"]
        if item["receipt_id"] == "co-vulnerability-stale-db-negative"
    )
    assert stale_vulnerability["operator_action"] == "deny_until_rescan"


@pytest.mark.asyncio
async def test_operator_browser_provider_usability_surface_reports_batch_ch_receipts(client):
    resp = await client.get("/api/operator/browser-provider-usability-proof")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "browser_provider_usability_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "browser_provider_usability_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES)
        + len(LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES)
        + len(BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES)
    )
    assert payload["summary"]["provider_attestation_count"] == 3
    assert payload["summary"]["recorded_live_provider_count"] == 2
    assert payload["summary"]["credential_boundary_count"] == 3
    assert payload["summary"]["multi_operator_task_count"] == 3
    assert payload["summary"]["max_operator_count"] == 3
    assert payload["summary"]["keyboard_path_count"] == 3
    assert payload["summary"]["recovery_drill_count"] == 4
    assert payload["summary"]["fail_closed_recovery_count"] == 4
    assert payload["policy"]["claim_boundary"] == BROWSER_PROVIDER_USABILITY_CLAIM_BOUNDARY
    assert set(BROWSER_PROVIDER_USABILITY_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/browser-provider-usability-proof" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["managed_browser_provider_attestation"] == list(
        MANAGED_BROWSER_PROVIDER_ATTESTATION_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["live_multi_operator_usability_study"] == list(
        LIVE_MULTI_OPERATOR_USABILITY_STUDY_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["browser_computer_use_recovery_drill"] == list(
        BROWSER_COMPUTER_USE_RECOVERY_DRILL_SCENARIO_NAMES
    )
    remote = next(
        item for item in payload["contract"]["provider_attestation_receipts"]
        if item["provider_id"] == "openclaw-remote-cdp-existing-session"
    )
    assert remote["provider_degradation"]["state"] == "blocked_until_partitioned"
    assert all(item["fails_closed"] for item in payload["contract"]["recovery_drill_receipts"])


@pytest.mark.asyncio
async def test_operator_safe_autonomous_browser_computer_use_surface_reports_batch_cp_receipts(client):
    resp = await client.get("/api/operator/safe-autonomous-browser-computer-use")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "safe_browser_computer_use_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "safe_browser_computer_use_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(LIVE_BROWSER_TASK_DEPTH_SCENARIO_NAMES)
        + len(AUTONOMOUS_BROWSER_SAFETY_SCENARIO_NAMES)
        + len(BROWSER_SESSION_PARTITIONING_SCENARIO_NAMES)
        + len(SITE_SPECIFIC_BROWSER_RECOVERY_SCENARIO_NAMES)
        + len(BROWSER_PROVIDER_RELIABILITY_MATRIX_SCENARIO_NAMES)
        + len(INDEPENDENT_BROWSER_USABILITY_REVIEW_SCENARIO_NAMES)
    )
    assert payload["summary"]["provider_mode_count"] == 3
    assert payload["summary"]["task_sample_total"] >= 30
    assert payload["summary"]["dangerous_action_default_block_count"] == 7
    assert payload["summary"]["session_isolation_satisfied_count"] == 6
    assert payload["summary"]["site_recovery_drill_count"] == 8
    assert payload["summary"]["provider_reliability_receipt_count"] == 3
    assert payload["summary"]["independent_usability_sample_total"] >= 20
    assert payload["summary"]["raw_receipt_missing_count"] == 0
    assert payload["summary"]["secret_or_credential_leak_count"] == 0
    assert payload["summary"]["receipt_artifact_secret_scan_status"] == "passed"
    assert payload["policy"]["claim_boundary"] == SAFE_BROWSER_COMPUTER_USE_CLAIM_BOUNDARY
    assert set(SAFE_BROWSER_COMPUTER_USE_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/safe-autonomous-browser-computer-use" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["live_browser_task_depth"] == list(
        LIVE_BROWSER_TASK_DEPTH_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["browser_provider_reliability_matrix"] == list(
        BROWSER_PROVIDER_RELIABILITY_MATRIX_SCENARIO_NAMES
    )
    assert all(
        item["external_mutation_allowed_without_approval"] is False
        for item in payload["contract"]["dangerous_action_taxonomy"]
    )


@pytest.mark.asyncio
async def test_operator_production_control_parity_surface_reports_batch_cb_receipts(client):
    resp = await client.get("/api/operator/production-operator-control-parity")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "production_operator_control_parity_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "production_operator_control_parity_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES)
        + len(PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES)
    )
    assert payload["summary"]["control_surface_count"] == 6
    assert payload["summary"]["train_batch_count"] == 7
    assert payload["summary"]["merged_prior_batch_count"] == 6
    assert payload["summary"]["required_actions_visible"] is True
    assert payload["policy"]["claim_boundary"] == PRODUCTION_OPERATOR_CONTROL_CLAIM_BOUNDARY
    assert set(PRODUCTION_OPERATOR_CONTROL_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/production-operator-control-parity" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["production_operator_control_parity"] == list(
        PRODUCTION_OPERATOR_CONTROL_PARITY_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["production_parity_train"] == list(PRODUCTION_PARITY_TRAIN_SCENARIO_NAMES)
    cb_train = next(item for item in payload["contract"]["train_receipts"] if item["batch"] == "CB")
    assert cb_train["evidence_state"] == "active_branch_receipts_visible_until_pr_merge"
    assert cb_train["merged_pr"] is None
    assert any(item["audit_id"] == "cb-audit-critic" for item in payload["contract"]["final_audit_receipts"])


@pytest.mark.asyncio
async def test_operator_final_parity_readiness_surface_reports_batch_ci_receipts(client):
    resp = await client.get("/api/operator/final-parity-readiness-report")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "final_parity_readiness_report_visible"
    assert payload["summary"]["benchmark_posture"] == "final_parity_audit_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES)
        + len(FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES)
        + len(OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES)
    )
    assert payload["summary"]["source_receipt_count"] == 7
    assert payload["summary"]["competitor_count"] == 3
    assert payload["summary"]["completed_batch_count"] == 22
    assert payload["summary"]["final_batch_status"] == "done"
    assert payload["summary"]["all_sources_reachable_with_caveats"] is True
    assert payload["summary"]["claim_lift_matrix_count"] >= 6
    assert payload["summary"]["continued_blocked_stronger_claim_count"] == payload["summary"]["exact_stronger_claim_count"]
    cl_batch = next(
        item for item in payload["contract"]["batch_reconciliation_receipts"]
        if item["batch"] == "CL"
    )
    assert cl_batch["status"] == "done"
    assert cl_batch["merged_pr"] == 516
    cm_batch = next(
        item for item in payload["contract"]["batch_reconciliation_receipts"]
        if item["batch"] == "CM"
    )
    assert cm_batch["status"] == "done"
    assert cm_batch["merged_pr"] == 517
    cn_batch = next(
        item for item in payload["contract"]["batch_reconciliation_receipts"]
        if item["batch"] == "CN"
    )
    assert cn_batch["status"] == "done"
    assert cn_batch["merged_pr"] == 518
    co_batch = next(
        item for item in payload["contract"]["batch_reconciliation_receipts"]
        if item["batch"] == "CO"
    )
    assert co_batch["status"] == "done"
    assert co_batch["issue"] == 510
    assert co_batch["merged_pr"] == 519
    cp_batch = next(
        item for item in payload["contract"]["batch_reconciliation_receipts"]
        if item["batch"] == "CP"
    )
    assert cp_batch["status"] == "done"
    assert cp_batch["issue"] == 511
    assert cp_batch["merged_pr"] == 520
    cq_batch = next(
        item for item in payload["contract"]["batch_reconciliation_receipts"]
        if item["batch"] == "CQ"
    )
    assert cq_batch["status"] == "done"
    assert cq_batch["issue"] == 512
    assert cq_batch["merged_pr"] == 521
    assert cq_batch["project_status"] == "Done"
    assert cq_batch["project_pr"] == "Merged"
    assert cq_batch["code_review"] == "Passed"
    assert payload["summary"]["residual_gap_count"] == 7
    assert payload["summary"]["full_parity_claim_allowed"] is False
    assert payload["summary"]["reference_systems_exceeded_claim_allowed"] is False
    assert payload["policy"]["claim_boundary"] == FINAL_PARITY_AUDIT_CLAIM_BOUNDARY
    assert set(FINAL_PARITY_AUDIT_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/final-parity-readiness-report" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["final_source_backed_parity_audit"] == list(
        FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["final_claim_ledger_reconciliation"] == list(
        FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["operator_final_parity_readiness_report"] == list(
        OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES
    )
    assert {"Hermes", "OpenClaw", "IronClaw"} <= {
        item["system"] for item in payload["contract"]["current_source_receipts"]
    }
    assert all(
        item["access_status"] == "reachable"
        and item["access_caveat"]
        and item["competitor_claim_uncertainty"]
        for item in payload["contract"]["current_source_receipts"]
    )
    assert {item["claim_id"] for item in payload["contract"]["claim_lift_matrix"]} >= {
        "SCL-028",
        "SCL-029",
        "SCL-030",
        "SCL-031",
        "SCL-032",
        "SCL-033",
    }
    assert all(
        item["outcome"] == "continued_blocked"
        for item in payload["contract"]["exact_stronger_claim_outcomes"]
    )
    assert all(
        item["disposition"] == "accepted"
        for item in payload["contract"]["critic_disposition_receipts"]
    )


@pytest.mark.asyncio
async def test_operator_live_external_orchestration_surface_reports_batch_cc_receipts(client):
    resp = await client.get("/api/operator/live-external-orchestration")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "live_external_orchestration_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "live_external_orchestration_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES)
        + len(ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES)
    )
    assert payload["summary"]["provider_receipt_count"] == 3
    assert payload["summary"]["crash_study_count"] == 3
    assert payload["summary"]["required_controls_visible"] is True
    assert payload["policy"]["claim_boundary"] == LIVE_EXTERNAL_ORCHESTRATION_CLAIM_BOUNDARY
    assert set(LIVE_EXTERNAL_ORCHESTRATION_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/live-external-orchestration" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["live_external_orchestration_attestation"] == list(
        LIVE_EXTERNAL_ORCHESTRATION_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["orchestration_crash_recovery_study"] == list(
        ORCHESTRATION_CRASH_RECOVERY_STUDY_SCENARIO_NAMES
    )
    assert "exactly_once_production_scheduler" in payload["policy"]["not_claimed"]
    assert any(
        item["evidence_mode"] == "recorded_live_fixture"
        for item in payload["contract"]["provider_attestation_receipts"]
    )
    assert any(
        item["study_id"] == "cc-crash-after-side-effect"
        for item in payload["contract"]["crash_recovery_study_receipts"]
    )


@pytest.mark.asyncio
async def test_operator_production_sla_orchestration_surface_reports_batch_cj_receipts(client):
    resp = await client.get("/api/operator/production-sla-orchestration")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "production_sla_orchestration_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "production_sla_orchestration_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES)
        + len(EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES)
        + len(DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES)
    )
    assert payload["summary"]["sla_window_count"] == 3
    assert payload["summary"]["failure_injection_count"] == 3
    assert payload["summary"]["duplicate_side_effect_audit_count"] == 3
    assert payload["summary"]["all_sla_windows_within_budget"] is True
    assert payload["summary"]["duplicate_audits_reconciled"] is True
    assert payload["policy"]["claim_boundary"] == PRODUCTION_SLA_ORCHESTRATION_CLAIM_BOUNDARY
    assert set(PRODUCTION_SLA_ORCHESTRATION_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/production-sla-orchestration" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["production_sla_orchestration"] == list(
        PRODUCTION_SLA_ORCHESTRATION_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["exactly_once_recovery_evidence"] == list(
        EXACTLY_ONCE_RECOVERY_EVIDENCE_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["duplicate_side_effect_audit"] == list(
        DUPLICATE_SIDE_EFFECT_AUDIT_SCENARIO_NAMES
    )
    assert "unconditional_exactly_once_scheduler" in payload["policy"]["not_claimed"]
    assert any(
        item["evidence_mode"] == "recorded_live_fixture"
        for item in payload["contract"]["sla_window_receipts"]
    )
    assert any(
        item["study_id"] == "cj-failure-after-side-effect-before-ack"
        for item in payload["contract"]["failure_injection_receipts"]
    )


@pytest.mark.asyncio
async def test_operator_continuous_orchestration_slo_surface_reports_batch_cs_receipts(client):
    resp = await client.get("/api/operator/continuous-orchestration-slo")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "continuous_orchestration_slo_visible"
    assert payload["summary"]["benchmark_posture"] == "continuous_orchestration_slo_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES)
        + len(CRASH_FAILOVER_SOAK_SCENARIO_NAMES)
        + len(SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES)
    )
    assert payload["summary"]["monitor_sample_count"] == 3
    assert payload["summary"]["crash_failover_soak_count"] == 3
    assert payload["summary"]["side_effect_reconciliation_count"] == 3
    assert payload["summary"]["all_monitors_within_budget"] is True
    assert payload["summary"]["all_failovers_within_budget"] is True
    assert payload["summary"]["reconciliation_complete"] is True
    assert payload["summary"]["runtime_status"] == "continuous_orchestration_runtime_ledger_visible"
    assert payload["summary"]["runtime_observation_count"] == 9
    assert payload["summary"]["active_budget_breach_count"] == 0
    assert payload["summary"]["active_duplicate_risk_count"] == 0
    assert payload["summary"]["active_recovery_queue_count"] >= 2
    assert payload["contract"]["runtime_operations"]["runtime_receipt_digest"]
    assert "cs-soak-provider-ack-loss" in payload["contract"]["runtime_operations"]["operator_recovery_queue"]
    assert payload["policy"]["claim_boundary"] == CONTINUOUS_ORCHESTRATION_SLO_CLAIM_BOUNDARY
    assert set(CONTINUOUS_ORCHESTRATION_SLO_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/continuous-orchestration-slo" in payload["policy"]["receipt_surfaces"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["scenario_names"]["continuous_orchestration_slo_monitor"] == list(
        CONTINUOUS_ORCHESTRATION_SLO_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["crash_failover_soak_v1"] == list(CRASH_FAILOVER_SOAK_SCENARIO_NAMES)
    assert payload["scenario_names"]["side_effect_reconciliation_v2"] == list(
        SIDE_EFFECT_RECONCILIATION_V2_SCENARIO_NAMES
    )
    assert "unconditional_exactly_once_scheduler" in payload["policy"]["not_claimed"]
    assert any(
        item["evidence_mode"] == "recorded_live_fixture"
        for item in payload["contract"]["monitor_samples"]
    )
    assert any(
        item["side_effect_state"] == "completed_unacknowledged"
        for item in payload["contract"]["crash_failover_soak_receipts"]
    )


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
async def test_operator_live_guardian_learning_quality_surface_reports_batch_bz_receipts(client):
    resp = await client.get("/api/operator/live-guardian-learning-quality")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "live_guardian_learning_quality_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "live_guardian_learning_quality_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == 28
    assert payload["summary"]["outcome_cohort_count"] == 8
    assert payload["summary"]["typed_outcome_count"] == 8
    assert payload["summary"]["provider_quarantine_count"] == 1
    assert payload["summary"]["canonical_precedence_preserved"] is True
    assert payload["summary"]["delete_export_receipts_visible"] is True
    assert payload["summary"]["provider_regressions_passed"] is True
    assert payload["policy"]["claim_boundary"] == LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY
    assert set(LIVE_GUARDIAN_LEARNING_QUALITY_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/live-guardian-learning-quality" in payload["policy"]["receipt_surfaces"]
    assert "guardian_intelligence_superiority" in payload["policy"]["not_claimed"]
    outcomes = {item["outcome"] for item in payload["contract"]["outcome_cohorts"]}
    assert outcomes == {
        "accepted",
        "ignored",
        "corrected",
        "deferred",
        "harmful",
        "helpful",
        "channel_shifted",
        "followthrough",
    }
    assert payload["contract"]["canonical_reconciliation"]["canonical_precedence"]["provider_override_blocked"] is True


@pytest.mark.asyncio
async def test_operator_live_human_outcome_learning_surface_reports_batch_cf_receipts(client):
    resp = await client.get("/api/operator/live-human-outcome-learning-proof")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "live_human_outcome_learning_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "live_human_outcome_learning_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == 15
    assert payload["summary"]["study_mode"] == "recorded_live_anonymized"
    assert payload["summary"]["outcome_cohort_count"] == 7
    assert payload["summary"]["consented_cohort_count"] == 7
    assert payload["summary"]["anonymized_cohort_count"] == 7
    assert payload["summary"]["causal_attribution_count"] == 4
    assert payload["summary"]["bounded_causal_claim_count"] == 4
    assert payload["summary"]["provider_quarantine_count"] == 2
    assert payload["policy"]["claim_boundary"] == LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY
    assert set(LIVE_HUMAN_OUTCOME_LEARNING_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/live-human-outcome-learning-proof" in payload["policy"]["receipt_surfaces"]
    assert "guardian_intelligence_superiority" in payload["policy"]["not_claimed"]
    outcomes = {item["outcome"] for item in payload["contract"]["study_receipts"]}
    assert outcomes >= {"accepted", "ignored", "corrected", "harmful", "helpful", "followthrough"}
    assert all(item["claim_scope"].startswith("bounded_to_") for item in payload["contract"]["causal_attribution"])
    assert any(
        item["quarantine_state"] == "quarantined"
        and item["behavior_change_allowed"] is False
        for item in payload["contract"]["memory_provider_monitors"]
    )


@pytest.mark.asyncio
async def test_operator_independent_learning_memory_parity_surface_reports_batch_cm_receipts(client):
    resp = await client.get("/api/operator/independent-learning-memory-parity")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "independent_learning_memory_parity_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "independent_learning_memory_parity_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == 14
    assert payload["summary"]["cohort_count"] == 3
    assert payload["summary"]["independent_evaluator_count"] == 3
    assert payload["summary"]["implementation_independent_evaluator_count"] == 3
    assert payload["summary"]["sample_size_total"] >= 150
    assert payload["summary"]["bounded_causal_claim_count"] == 3
    assert payload["summary"]["provider_parity_dimension_count"] >= 10
    assert payload["summary"]["provider_failed_dimension_count"] >= 1
    assert payload["summary"]["provider_promotion_blocked_count"] == 2
    assert payload["summary"]["secret_or_credential_leak_count"] == 0
    assert payload["policy"]["claim_boundary"] == INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY
    assert set(INDEPENDENT_LEARNING_MEMORY_PARITY_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/independent-learning-memory-parity" in payload["policy"]["receipt_surfaces"]
    assert "memory_superiority" in payload["policy"]["not_claimed"]
    assert all(
        item["claim_scope"].startswith("bounded_to_")
        for item in payload["contract"]["task_scoped_causal_attribution"]
    )
    assert any(
        item["quarantine_state"] == "quarantined"
        and item["behavior_change_allowed"] is False
        and "privacy_boundary" in item["failed_dimensions"]
        and "privacy_boundary" not in item["passed_dimensions"]
        and item["promotion_blocked"] is True
        for item in payload["contract"]["memory_provider_parity_matrix"]
    )


@pytest.mark.asyncio
async def test_operator_dense_operator_recovery_control_surface_reports_batch_cn_receipts(client):
    resp = await client.get("/api/operator/dense-operator-recovery-control")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["operator_status"] == "dense_operator_recovery_control_receipts_visible"
    assert payload["summary"]["benchmark_posture"] == "dense_operator_recovery_control_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == 15
    assert payload["summary"]["debugging_receipt_count"] >= 4
    assert payload["summary"]["control_action_count"] >= 11
    assert payload["summary"]["task_matrix_count"] >= 8
    assert payload["summary"]["required_controls_visible"] is True
    assert payload["summary"]["cross_batch_recovery_view_visible"] is True
    assert payload["policy"]["claim_boundary"] == DENSE_OPERATOR_RECOVERY_CLAIM_BOUNDARY
    assert set(DENSE_OPERATOR_RECOVERY_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "/api/operator/dense-operator-recovery-control" in payload["policy"]["receipt_surfaces"]
    assert "solved_operator_control" in payload["policy"]["not_claimed"]
    assert any(
        item["operator_task"] == "inspect_cross_batch_residual_risk"
        and "production_sla_orchestration" in item["cross_batch_receipts"]
        and "browser_provider_usability" in item["cross_batch_receipts"]
        for item in payload["contract"]["debugging_receipts"]
    )
    assert any(
        item["action"] == "quarantine"
        and item["requires_operator_review"] is True
        and item["quarantine_release_condition"] == "independent_review_plus_hash_match_plus_no_privacy_regression"
        and item["receipt_after_action"]
        for item in payload["contract"]["control_density_receipts"]
    )
    assert any(
        item["action"] == "rollback"
        and item["rollback_restore_point"] == "pre_mutation_checkpoint_or_package_version_with_hash"
        for item in payload["contract"]["control_density_receipts"]
    )
    assert payload["summary"]["receipt_integrity_manifest_count"] == payload["summary"]["receipt_integrity_verified_count"]
    assert all(
        item["verified"] is True
        and item["outcome_verified"] is True
        and len(item["content_sha256"]) == 64
        for item in payload["contract"]["receipt_integrity_manifest"]
    )
    assert all(
        item["keyboard_only_path_complete"] is True
        and item["reviewer_independence"]
        for item in payload["contract"]["independent_usability_accessibility_receipts"]
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
async def test_operator_guardian_safe_multimodal_voice_surface_reports_policy_receipts_and_claim_boundary(client):
    resp = await client.get("/api/operator/guardian-safe-multimodal-voice")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "guardian_safe_multimodal_voice"
    assert payload["summary"]["operator_status"] == "guardian_safe_voice_media_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["capability_governance_state"] == (
        "owner_trust_permission_data_access_mutation_revocation_visible"
    )
    assert payload["summary"]["guardian_value_state"] == "voice_media_requires_guardian_value_reason"
    assert payload["summary"]["claim_boundary"] == (
        "governed_voice_media_proof_not_live_broad_multimodal_runtime_or_voice_parity"
    )
    assert len(payload["capability_families"]) == 5
    assert len(payload["governance_receipts"]) == 5
    assert payload["policy"]["exposure_policy"] == (
        "browser_vision_and_media_analysis_cannot_expand_screen_file_credential_camera_microphone_or_network_exposure_silently"
    )
    assert "voice_parity" in payload["policy"]["not_claimed"]
    assert "/api/operator/guardian-safe-multimodal-voice" in payload["policy"]["receipt_surfaces"]


@pytest.mark.asyncio
async def test_operator_production_reach_browser_voice_surface_reports_batch_by_receipts(client):
    resp = await client.get("/api/operator/production-reach-browser-voice")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "production_reach_browser_voice_ci_gated_operator_visible"
    assert payload["summary"]["operator_status"] == "production_reach_browser_voice_receipts_visible"
    assert payload["summary"]["paired_external_messaging_channel_count"] >= 1
    assert payload["summary"]["browser_session_partition_count"] >= 2
    assert payload["summary"]["browser_crash_recovery_count"] >= 1
    assert payload["summary"]["voice_media_deletion_path_count"] >= 2
    assert payload["summary"]["voice_media_revocation_fail_closed_count"] >= 2
    assert payload["policy"]["claim_boundary"] == PRODUCTION_REACH_BROWSER_VOICE_CLAIM_BOUNDARY
    assert "openclaw_class_reach" in payload["policy"]["blocked_claims"]
    assert "/api/operator/benchmark-proof" in payload["policy"]["receipt_surfaces"]


@pytest.mark.asyncio
async def test_operator_live_reach_media_surface_reports_batch_ce_receipts(client):
    resp = await client.get("/api/operator/live-reach-media-proof")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "live_reach_media_ci_gated_operator_visible"
    assert payload["summary"]["operator_status"] == "live_reach_media_receipts_visible"
    assert payload["summary"]["recorded_live_channel_count"] >= 2
    assert payload["summary"]["paired_channel_count"] >= 2
    assert payload["summary"]["revocation_fail_closed_count"] >= 3
    assert payload["summary"]["voice_media_consent_count"] >= 3
    assert payload["summary"]["voice_media_failure_fallback_count"] >= 3
    assert payload["summary"]["approval_survived_surface_shift_count"] >= 2
    assert payload["policy"]["claim_boundary"] == LIVE_REACH_MEDIA_CLAIM_BOUNDARY
    assert "openclaw_class_reach" in payload["policy"]["blocked_claims"]
    assert "voice_parity" in payload["policy"]["blocked_claims"]
    assert "/api/operator/benchmark-proof" in payload["policy"]["receipt_surfaces"]


@pytest.mark.asyncio
async def test_operator_production_reach_voice_mobile_surface_reports_batch_cl_receipts(client):
    resp = await client.get("/api/operator/production-reach-voice-mobile")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "production_reach_voice_mobile_ci_gated_operator_visible"
    assert payload["summary"]["operator_status"] == "production_reach_voice_mobile_receipts_visible"
    assert payload["summary"]["channel_provider_count"] >= 4
    assert payload["summary"]["sla_window_visible_count"] >= 3
    assert payload["summary"]["rate_limit_abuse_visible_count"] >= 4
    assert payload["summary"]["voice_media_quality_gate_pass_count"] >= 3
    assert payload["summary"]["voice_media_regression_fallback_count"] >= 3
    assert payload["summary"]["mobile_approval_handoff_count"] >= 2
    assert payload["summary"]["mobile_revocation_fail_closed_count"] >= 2
    assert payload["policy"]["claim_boundary"] == PRODUCTION_REACH_VOICE_MOBILE_CLAIM_BOUNDARY
    assert "openclaw_class_reach" in payload["policy"]["blocked_claims"]
    assert "production_ready_product" in payload["policy"]["blocked_claims"]
    assert payload["scenario_names"]["broad_channel_sla_operations"] == list(
        BROAD_CHANNEL_SLA_OPERATIONS_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["production_voice_media_quality_gates"] == list(
        PRODUCTION_VOICE_MEDIA_QUALITY_GATES_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["mobile_execution_continuity"] == list(
        MOBILE_EXECUTION_CONTINUITY_SCENARIO_NAMES
    )


@pytest.mark.asyncio
async def test_operator_broad_reach_field_ops_surface_reports_batch_cu_receipts(client):
    resp = await client.get("/api/operator/broad-reach-field-ops")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "broad_reach_field_ops_ci_gated_operator_visible"
    assert payload["summary"]["operator_status"] == "broad_reach_field_ops_receipts_visible"
    assert payload["summary"]["channel_provider_count"] >= 6
    assert payload["summary"]["paired_channel_count"] >= 5
    assert payload["summary"]["recorded_live_field_window_count"] >= 2
    assert payload["summary"]["auth_consent_revocation_visible_count"] >= 6
    assert payload["summary"]["rate_limit_abuse_drill_count"] >= 6
    assert payload["summary"]["degraded_recovery_drill_count"] >= 6
    assert payload["summary"]["continuity_receipt_count"] >= 6
    assert payload["summary"]["safe_receipt_redaction_count"] >= 12
    assert payload["summary"]["coverage_gap_count"] >= 4
    assert payload["summary"]["voice_media_quality_gate_pass_count"] >= 4
    assert payload["summary"]["voice_media_latency_gate_pass_count"] >= 4
    assert payload["summary"]["voice_media_privacy_control_count"] >= 4
    assert payload["summary"]["slo_budget_met_count"] >= 2
    assert payload["summary"]["provider_failure_recovery_count"] >= 2
    assert payload["summary"]["offline_recovery_count"] >= 2
    assert payload["summary"]["claim_boundary"] == BROAD_REACH_FIELD_OPS_CLAIM_BOUNDARY
    assert payload["scenario_names"]["broad_reach_field_operations"] == list(
        BROAD_REACH_FIELD_OPERATIONS_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["voice_media_quality_operations"] == list(
        VOICE_MEDIA_QUALITY_OPERATIONS_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["always_available_reach_slo"] == list(
        ALWAYS_AVAILABLE_REACH_SLO_SCENARIO_NAMES
    )
    assert "always_available_operation" in payload["policy"]["blocked_claims"]
    assert "openclaw_class_reach" in payload["policy"]["blocked_claims"]
    assert payload["policy"]["safe_receipt_redaction_boundary"] == (
        "redacted_no_message_body_secret_contact_audio_or_media_payload"
    )
    assert "/api/operator/broad-reach-field-ops" in payload["policy"]["receipt_surfaces"]
    signal = next(
        item
        for item in payload["contract"]["provider_channel_field_matrix"]
        if item["provider"] == "signal-bridge"
    )
    assert signal["operator_identity"]["pairing_state"] == "requires_pairing"
    assert signal["field_window"]["window_met"] is False
    assert signal["safe_receipt"]["contains_contact_identifier"] is False


@pytest.mark.asyncio
async def test_operator_guardian_learning_arbitration_surface_reports_policy_receipts_and_claim_boundary(client):
    resp = await client.get("/api/operator/guardian-learning-arbitration")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "guardian_learning_arbitration_v2"
    assert payload["summary"]["operator_status"] == "guardian_learning_arbitration_receipts_visible"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["outcome_coverage_state"] == (
        "act_defer_bundle_clarify_approval_stay_silent_receipts_visible"
    )
    assert payload["summary"]["negative_case_state"] == (
        "stale_conflict_ambiguous_degraded_unsafe_negative_outcome_cases_visible"
    )
    assert payload["summary"]["claim_boundary"] == (
        "deterministic_learning_arbitration_receipts_not_guardian_intelligence_superiority"
    )
    assert {receipt["actual_action"] for receipt in payload["arbitration_receipts"]} == {
        "act",
        "bundle",
        "clarify",
        "defer",
        "request_approval",
        "stay_silent",
    }
    assert payload["policy"]["guardian_value_policy"] == (
        "learning_must_improve_restraint_clarification_timing_approval_recovery_or_follow_through_not_intervention_volume"
    )
    assert "guardian_intelligence_superiority" in payload["policy"]["not_claimed"]
    assert "/api/operator/guardian-learning-arbitration" in payload["policy"]["receipt_surfaces"]


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
async def test_operator_one_reach_channel_canary_surface_reports_story_and_claim_boundary(client):
    resp = await client.get("/api/operator/one-reach-channel-canary")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "one_excellent_reach_channel_canary"
    assert payload["summary"]["operator_status"] == "one_reach_channel_canary_visible"
    assert payload["summary"]["selected_channel"] == "native_notification"
    assert payload["summary"]["scenario_count"] == len(payload["scenario_names"])
    assert payload["summary"]["pairing_state"] == "paired"
    assert payload["summary"]["revocation_state"] == "revoked"
    assert payload["summary"]["degraded_state"] == "daemon_offline"
    assert payload["summary"]["approval_handoff_state"] == "pending_operator_approval"
    assert payload["operator_story"]["single_channel_selected"] is True
    assert payload["operator_story"]["channel_sprawl_rejected"] is True
    assert payload["operator_story"]["revocation_fail_closed_visible"] is True
    assert payload["operator_story"]["memory_context_visible"] is True
    assert payload["operator_story"]["degraded_state_ui_visible"] is True
    assert payload["operator_story"]["e2e_flow_visible"] is True
    assert payload["policy"]["claim_boundary"] == "deterministic_native_notification_canary_not_broad_live_channel_reach"
    assert "live_slack_discord_telegram_delivery" in payload["policy"]["not_claimed"]


@pytest.mark.asyncio
async def test_operator_one_reach_channel_canary_surface_degrades_summary_on_failures(client):
    failing_summary = SimpleNamespace(
        total=5,
        passed=4,
        failed=1,
        duration_ms=13,
        results=[
            SimpleNamespace(
                passed=False,
                name="native_notification_health_retry_degraded_behavior",
                error="retry receipt regression",
            )
        ],
    )

    with patch(
        "src.extensions.reach_channel_canary._run_one_reach_channel_canary_suite",
        AsyncMock(return_value=failing_summary),
    ):
        resp = await client.get("/api/operator/one-reach-channel-canary")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["benchmark_posture"] == "one_reach_channel_canary_regressions_detected_operator_visible"
    assert payload["summary"]["active_failure_count"] == 1
    assert payload["failure_report"][0]["scenario_name"] == "native_notification_health_retry_degraded_behavior"


@pytest.mark.asyncio
async def test_operator_durable_workflow_engine_surface_delegates_to_state_report(client):
    payload = {
        "summary": {
            "suite_name": "durable_workflow_engine_v1",
            "benchmark_posture": "durable_workflow_engine_ci_gated_operator_visible",
            "operator_status": "durable_workflow_engine_visible",
            "scenario_count": len(DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES),
            "active_failure_count": 0,
            "durable_state_state": "checkpointed_state_and_resume_metadata_visible",
            "recovery_state": "crash_safe_continuation_receipts_visible",
            "trigger_state": "heartbeat_and_reactive_trigger_receipts_visible",
        },
        "scenario_names": list(DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES),
        "policy": {
            "receipt_surfaces": [
                "/api/operator/durable-workflow-engine",
                "/api/operator/benchmark-proof",
            ],
            "ci_gate_mode": "required_benchmark_suite",
        },
        "latest_run": {
            "total": len(DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES),
            "passed": len(DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES),
            "failed": 0,
            "duration_ms": 11,
        },
    }

    with patch(
        "src.api.operator.build_durable_workflow_state_report",
        AsyncMock(return_value=payload),
    ) as build_report:
        resp = await client.get("/api/operator/durable-workflow-engine")

    assert resp.status_code == 200
    assert resp.json()["summary"]["suite_name"] == "durable_workflow_engine_v1"
    assert resp.json()["summary"]["operator_status"] == "durable_workflow_engine_visible"
    assert "/api/operator/durable-workflow-engine" in resp.json()["policy"]["receipt_surfaces"]
    build_report.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_operator_durable_workflow_engine_v2_surface_delegates_to_report(client):
    payload = {
        "summary": {
            "suite_name": "durable_workflow_engine_v2",
            "production_suite_name": "production_durable_orchestration",
            "benchmark_posture": "durable_workflow_engine_v2_ci_gated_operator_visible",
            "operator_status": "durable_workflow_engine_v2_recovery_receipts_visible",
            "scenario_count": len(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
            "lease_receipt_count": 2,
            "blocked_recovery_count": 1,
        },
        "scenario_names": list(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
        "policy": {
            "operator_surfaces": [
                "/api/operator/durable-workflow-engine-v2",
                "/api/operator/benchmark-proof",
            ],
            "blocked_claims": ["langgraph_class_durable_workflows"],
        },
        "latest_run": {
            "total": len(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
            "passed": len(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
            "failed": 0,
            "duration_ms": 11,
        },
    }

    with patch(
        "src.api.operator.build_durable_workflow_v2_report",
        AsyncMock(return_value=payload),
    ) as build_report:
        resp = await client.get("/api/operator/durable-workflow-engine-v2")

    assert resp.status_code == 200
    assert resp.json()["summary"]["suite_name"] == "durable_workflow_engine_v2"
    assert resp.json()["summary"]["operator_status"] == "durable_workflow_engine_v2_recovery_receipts_visible"
    assert "/api/operator/durable-workflow-engine-v2" in resp.json()["policy"]["operator_surfaces"]
    build_report.assert_awaited_once_with()


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
async def test_operator_secure_capability_host_hardening_surface_reports_v2_policy_and_receipts(client):
    resp = await client.get("/api/operator/secure-capability-host-hardening")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "production_secure_host_hardening"
    assert payload["summary"]["operator_status"] == "production_secure_host_hardening_receipts_visible"
    assert payload["summary"]["live_isolation_state"] == "privileged_paths_fail_closed_with_receipts"
    assert payload["summary"]["secret_redaction_state"] == "replay_and_redaction_fail_closed"
    assert payload["summary"]["browser_recovery_partition_state"] == "per_session_recovery_without_profile_bleed"
    assert payload["summary"]["private_network_egress_state"] == "private_targets_blocked_with_receipts"
    assert payload["summary"]["extension_revocation_state"] == "runtime_contributions_cut_off_after_revocation"
    assert payload["summary"]["workflow_provider_replay_state"] == "same_or_narrower_trust_replay_required"
    assert "secure_host_live_private_network_egress_behavior" in payload["scenario_names"]
    assert "secure_host_live_extension_revocation_behavior" in payload["scenario_names"]
    assert "actor_or_session" in payload["receipt_schema"]
    assert "redaction_status" in payload["receipt_schema"]
    assert "/api/operator/secure-capability-host-hardening" in payload["operator_surfaces"]
    assert any(
        receipt["attempted_action"] == "secret_ref_connector_call"
        and receipt["policy_decision"] == "blocked"
        and "redaction_failure" in receipt["blocked_reasons"]
        for receipt in payload["enforcement_receipts"]
    )
    assert any(
        receipt["attempted_action"] == "browser_recovery"
        and receipt["policy_decision"] == "allowed"
        and receipt["isolation_mode"] == "per_session_browser_recovery_partition"
        for receipt in payload["enforcement_receipts"]
    )
    assert any(
        receipt["attempted_action"] == "connector_private_network_fetch"
        and "private_network_egress" in receipt["blocked_reasons"]
        for receipt in payload["enforcement_receipts"]
    )
    assert payload["policy"]["claim_boundary"] == (
        "production_secure_host_hardening_proof_not_secure_private_by_default_or_ironclaw_class"
    )
    assert "secure_private_by_default" in payload["policy"]["blocked_claims"]
    assert "ironclaw_class_secure_execution" in payload["policy"]["blocked_claims"]


@pytest.mark.asyncio
async def test_operator_production_isolation_hardening_surface_reports_batch_cd_receipts(client):
    resp = await client.get("/api/operator/production-isolation-hardening")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "production_isolation_security"
    assert payload["summary"]["benchmark_posture"] == "production_isolation_security_ci_gated_operator_visible"
    assert payload["summary"]["operator_status"] == "production_isolation_security_receipts_visible"
    assert payload["summary"]["isolation_receipt_count"] >= 5
    assert payload["summary"]["red_team_case_count"] >= 6
    assert payload["summary"]["incident_drill_count"] >= 6
    assert payload["summary"]["required_controls_visible"] is True
    assert payload["summary"]["claim_boundary"] == PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY
    assert payload["scenario_names"]["production_isolation_hardening_v2"] == list(
        PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["privileged_path_red_team_gauntlet_v2"] == list(
        PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["security_incident_recovery_drill"] == list(
        SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES
    )
    assert payload["latest_run"]["failed"] == 0
    assert "/api/operator/production-isolation-hardening" in payload["policy"]["receipt_surfaces"]
    assert "secure_private_by_default" in payload["policy"]["blocked_claims"]
    assert "ironclaw_class_secure_execution" in payload["policy"]["blocked_claims"]
    assert any(item["surface"] == "browser_computer_use" for item in payload["contract"]["isolation_receipts"])
    assert any(item["case_id"] == "browser_session_bleed" for item in payload["contract"]["red_team_cases"])
    assert any(
        item["incident_type"] == "credential_boundary_drift"
        for item in payload["contract"]["incident_drill_receipts"]
    )


@pytest.mark.asyncio
async def test_operator_independent_secure_host_review_surface_reports_batch_ck_receipts(client):
    resp = await client.get("/api/operator/independent-secure-host-review")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "independent_secure_host_review"
    assert payload["summary"]["benchmark_posture"] == "independent_secure_host_review_ci_gated_operator_visible"
    assert payload["summary"]["operator_status"] == "independent_secure_host_review_receipts_visible"
    assert payload["summary"]["reviewed_surface_count"] >= 6
    assert payload["summary"]["finding_count"] >= 4
    assert payload["summary"]["unsupported_isolation_claim_visible"] is True
    assert payload["summary"]["hostile_drill_fail_closed_count"] == payload["summary"]["hostile_drill_count"]
    assert payload["summary"]["operator_recovery_actions_visible"] is True
    assert payload["summary"]["claim_boundary"] == INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY
    assert payload["scenario_names"]["independent_secure_host_review"] == list(
        INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["live_hostile_isolation_drills"] == list(
        LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["secure_host_recovery_authority"] == list(
        SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES
    )
    assert payload["latest_run"]["failed"] == 0
    assert "/api/operator/independent-secure-host-review" in payload["policy"]["receipt_surfaces"]
    assert set(INDEPENDENT_SECURE_HOST_REVIEW_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert any(
        item["surface"] == "hardware_backed_isolation" and item["implemented"] is False
        for item in payload["contract"]["isolation_evidence_matrix"]
    )
    assert any(
        item["attack_family"] == "credential_exfiltration"
        for item in payload["contract"]["hostile_drill_receipts"]
    )
    assert any(item["action"] == "rotate" for item in payload["contract"]["recovery_authority_receipts"])


@pytest.mark.asyncio
async def test_operator_container_grade_secure_host_surface_reports_batch_ct_receipts(client):
    resp = await client.get("/api/operator/container-grade-secure-host")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["suite_name"] == "container_grade_secure_host"
    assert payload["summary"]["benchmark_posture"] == "container_grade_secure_host_ci_gated_operator_visible"
    assert payload["summary"]["operator_status"] == "container_grade_secure_host_validation_visible"
    assert payload["summary"]["isolation_decision_count"] >= 6
    assert payload["summary"]["implemented_boundary_count"] >= 5
    assert payload["summary"]["unsupported_boundary_count"] >= 1
    assert payload["summary"]["missing_hardware_boundary_visible"] is True
    assert payload["summary"]["finding_count"] >= 4
    assert payload["summary"]["remediated_or_waived_findings"] == payload["summary"]["finding_count"]
    assert payload["summary"]["secret_egress_drill_count"] >= 4
    assert payload["summary"]["secret_leak_count"] == 0
    assert payload["summary"]["all_secret_drills_safe"] is True
    assert payload["summary"]["recovery_authority_count"] >= 5
    assert payload["summary"]["claim_boundary"] == CONTAINER_GRADE_SECURE_HOST_CLAIM_BOUNDARY
    assert payload["scenario_names"]["container_grade_capability_isolation"] == list(
        CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["external_security_validation_v1"] == list(
        EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES
    )
    assert payload["scenario_names"]["secret_egress_certification_drill"] == list(
        SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES
    )
    assert payload["latest_run"]["failed"] == 0
    assert "/api/operator/container-grade-secure-host" in payload["policy"]["receipt_surfaces"]
    assert set(CONTAINER_GRADE_SECURE_HOST_BLOCKED_CLAIMS) <= set(payload["policy"]["blocked_claims"])
    assert "hardware_backed_isolation" in payload["policy"]["blocked_claims"]
    assert "external_security_certification" in payload["policy"]["blocked_claims"]
    assert all(
        "raw_report_path" not in receipt
        and receipt["real_external_certification"] is False
        and receipt["evidence_boundary"] == "fixture_validation_record_not_external_security_certification"
        for receipt in payload["contract"]["external_security_validation_receipts"]
    )
    assert any(
        item["capability_class"] == "hardware_backed_runtime" and item["implemented"] is False
        for item in payload["contract"]["isolation_model_decisions"]
    )
    assert any(
        item["destination_host"] == "169.254.169.254" and item["decision"] == "blocked"
        for item in payload["contract"]["secret_egress_certification_drills"]
    )
    assert any(item["action"] == "rotate" for item in payload["contract"]["incident_recovery_validation_receipts"])


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
