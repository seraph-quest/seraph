"""Operator API — threaded live timeline and team control plane surfaces."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query

from config.settings import settings
from src.agent.session import session_manager
from src.app import _runtime_model_label, _runtime_provider_label
from src.extensions.lifecycle import list_extensions
from src.llm_logger import list_recent_llm_calls
from src.observer.manager import context_manager
from src.api.observer import _continuity_surface, build_observer_continuity_snapshot
from src.api.workflows import (
    _list_workflow_runs,
    workflow_surface_continue_message,
    workflow_surface_recommended_actions,
    workflow_surface_replay_draft,
    workflow_surface_resume_metadata,
)
from src.approval.repository import approval_repository
from src.approval.surfaces import approval_surface_metadata
from src.audit.repository import audit_repository
from src.guardian.feedback import guardian_feedback_repository
from src.observer.insight_queue import insight_queue
from src.observer.native_notification_queue import native_notification_queue

router = APIRouter()


def _parse_iso(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _timeline_timestamp(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _continuity_action_timestamp(snapshot: dict[str, Any]) -> str:
    daemon = snapshot.get("daemon") if isinstance(snapshot, dict) else {}
    if isinstance(daemon, dict):
        last_post = daemon.get("last_post")
        if isinstance(last_post, (int, float)):
            return datetime.fromtimestamp(float(last_post), tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _continuity_operator_items(
    snapshot: dict[str, Any],
    *,
    session_id: str | None,
    session_titles: dict[str, str],
) -> list[dict[str, Any]]:
    recovery_actions = snapshot.get("recovery_actions")
    if not isinstance(recovery_actions, list):
        return []
    updated_at = _continuity_action_timestamp(snapshot)
    items: list[dict[str, Any]] = []
    for action in recovery_actions:
        if not isinstance(action, dict):
            continue
        thread_id = action.get("thread_id") if isinstance(action.get("thread_id"), str) else None
        if session_id and thread_id not in {None, session_id}:
            continue
        items.append({
            "id": f"continuity:{action.get('id') or len(items)}",
            "kind": "reach_recovery",
            "title": str(action.get("label") or "Reach recovery"),
            "summary": str(action.get("detail") or "Inspect live continuity recovery."),
            "status": str(action.get("status") or "attention"),
            "created_at": updated_at,
            "updated_at": updated_at,
            "thread_id": thread_id,
            "thread_label": session_titles.get(thread_id) if thread_id else None,
            "continue_message": action.get("continue_message"),
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": None,
            "recommended_actions": [],
            "source": "continuity",
            "metadata": {
                "surface": action.get("surface"),
                "kind": action.get("kind"),
                "route": action.get("route"),
                "repair_hint": action.get("repair_hint"),
                "open_thread_available": bool(action.get("open_thread_available")),
                "continuity_health": (
                    snapshot.get("summary", {}).get("continuity_health")
                    if isinstance(snapshot.get("summary"), dict)
                    else None
                ),
                "primary_surface": (
                    snapshot.get("summary", {}).get("primary_surface")
                    if isinstance(snapshot.get("summary"), dict)
                    else None
                ),
                "recommended_focus": (
                    snapshot.get("summary", {}).get("recommended_focus")
                    if isinstance(snapshot.get("summary"), dict)
                    else None
                ),
                "presence_surface_count": (
                    snapshot.get("summary", {}).get("presence_surface_count")
                    if isinstance(snapshot.get("summary"), dict)
                    else None
                ),
                "attention_presence_surface_count": (
                    snapshot.get("summary", {}).get("attention_presence_surface_count")
                    if isinstance(snapshot.get("summary"), dict)
                    else None
                ),
            },
        })
    return items


def _runtime_status_payload() -> dict[str, Any]:
    model = settings.default_model.strip()
    return {
        "version": "2026.3.19",
        "build_id": "SERAPH_PRIME_v2026.3.19",
        "provider": _runtime_provider_label(),
        "model": model,
        "model_label": _runtime_model_label(model),
        "api_base": settings.llm_api_base.strip(),
        "timezone": settings.user_timezone,
        "llm_logging_enabled": settings.llm_log_enabled,
    }


def _control_plane_roles(tool_policy_mode: str, mcp_policy_mode: str, approval_mode: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "human_operator",
            "label": "Human operator",
            "scope": "workspace_governance",
            "summary": "Owns approvals, deployment posture, and final review of privileged mutations.",
            "status": "active",
            "permissions": [
                "approve high-risk actions",
                "configure extensions and connectors",
                "promote governed evolution proposals",
                "decide deployment and runtime posture",
            ],
            "boundaries": ["human_review", "workspace_write", "external_mcp"],
        },
        {
            "id": "guardian_runtime",
            "label": "Guardian runtime",
            "scope": "autonomous_triage",
            "summary": "Synthesizes continuity, usage, and intervention signals without owning privileged execution.",
            "status": "active",
            "permissions": [
                "queue interventions",
                "surface runtime diagnostics",
                "recommend follow-through",
            ],
            "boundaries": ["advisory_only", approval_mode],
        },
        {
            "id": "delegated_specialists",
            "label": "Delegated specialists",
            "scope": "bounded_execution",
            "summary": (
                "Run bounded delegated work under the current workspace approval posture."
                if settings.use_delegation
                else "Delegation is disabled for this runtime."
            ),
            "status": "enabled" if settings.use_delegation else "disabled",
            "permissions": [
                "bounded workflow execution",
                "specialist planning",
            ] if settings.use_delegation else [],
            "boundaries": ["delegation", tool_policy_mode],
        },
        {
            "id": "connector_runtime",
            "label": "Connector runtime",
            "scope": "authenticated_io",
            "summary": "Managed connectors run with explicit MCP and mutation boundaries instead of ambient trust.",
            "status": "guarded",
            "permissions": [
                "typed source evidence",
                "adapter-backed operations",
                "channel and presence routing",
            ],
            "boundaries": [mcp_policy_mode, "secret_ref_allowlist", "approval_scoped_writes"],
        },
    ]


def _usage_summary(
    workflow_runs: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    llm_calls: list[dict[str, Any]],
    audit_events: list[dict[str, Any]],
    *,
    window_hours: int,
) -> dict[str, Any]:
    terminal_statuses = {"completed", "succeeded", "failed", "cancelled"}
    active_workflows = sum(
        1
        for run in workflow_runs
        if str(run.get("status") or "").lower() not in terminal_statuses
    )
    blocked_workflows = sum(
        1
        for run in workflow_runs
        if str(run.get("availability") or "") == "blocked"
    )
    llm_cost_usd = round(
        sum(float(call.get("cost_usd", 0) or 0) for call in llm_calls),
        6,
    )
    input_tokens = sum(int((call.get("tokens") or {}).get("input", 0) or 0) for call in llm_calls)
    output_tokens = sum(int((call.get("tokens") or {}).get("output", 0) or 0) for call in llm_calls)
    user_llm_calls = sum(
        1
        for call in llm_calls
        if str(call.get("source") or "") in {"rest_chat", "websocket_chat"}
    )
    autonomous_llm_calls = len(llm_calls) - user_llm_calls
    failure_count = sum(
        1
        for event in audit_events
        if str(event.get("event_type") or "") in {
            "tool_failed",
            "integration_failed",
            "llm_primary_failure",
            "llm_fallback_failure",
        }
    )
    return {
        "window_hours": window_hours,
        "llm_call_count": len(llm_calls),
        "llm_cost_usd": llm_cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "user_triggered_llm_calls": user_llm_calls,
        "autonomous_llm_calls": autonomous_llm_calls,
        "failure_count": failure_count,
        "pending_approvals": len(pending_approvals),
        "active_workflows": active_workflows,
        "blocked_workflows": blocked_workflows,
    }


def _review_receipts(audit_events: list[dict[str, Any]], session_titles: dict[str, str]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for event in audit_events:
        event_type = str(event.get("event_type") or "")
        if not (
            event_type.startswith("approval_")
            or event_type.startswith("integration_")
            or event_type in {"tool_failed", "llm_routing_decision", "llm_target_rerouted"}
        ):
            continue
        session_id = event.get("session_id")
        receipts.append({
            "id": f"review:{event.get('id')}",
            "title": str(event.get("tool_name") or event_type.replace("_", " ")),
            "summary": str(event.get("summary") or event_type.replace("_", " ")),
            "status": event_type,
            "created_at": str(event.get("created_at") or ""),
            "thread_id": session_id,
            "thread_label": session_titles.get(str(session_id)) if session_id else None,
        })
    return receipts[:4]


def _handoff_trust_boundary(run: dict[str, Any]) -> dict[str, Any] | None:
    trust_boundary = run.get("trust_boundary")
    if isinstance(trust_boundary, dict):
        return trust_boundary
    replay_block_reason = str(run.get("replay_block_reason") or "")
    if replay_block_reason == "approval_context_changed":
        return {
            "status": "changed",
            "blocked": True,
            "reason": "approval_context_changed",
            "message": "Workflow trust boundary changed after the original run.",
            "requires_fresh_run": True,
        }
    if replay_block_reason == "approval_context_missing":
        return {
            "status": "missing",
            "blocked": True,
            "reason": "approval_context_missing",
            "message": "Workflow predates tracked trust-boundary metadata for this privileged surface.",
            "requires_fresh_run": True,
        }
    return None


def _handoff_entries(
    workflow_runs: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    continuity_snapshot: dict[str, Any],
    session_titles: dict[str, str],
    *,
    session_id: str | None,
) -> dict[str, Any]:
    approvals: list[dict[str, Any]] = []
    for approval in pending_approvals[:4]:
        approval_thread_id = approval.get("thread_id") or approval.get("session_id")
        approvals.append({
            "id": f"approval:{approval.get('id')}",
            "kind": "approval",
            "label": str(approval.get("tool_name") or "approval"),
            "detail": str(approval.get("summary") or "Approval pending"),
            "status": str(approval.get("risk_level") or "pending"),
            "thread_id": approval_thread_id,
            "thread_label": (
                approval.get("thread_label") or session_titles.get(str(approval_thread_id))
            ) if approval_thread_id else None,
            "continue_message": approval.get("resume_message"),
        })

    blocked_workflows: list[dict[str, Any]] = []
    for run in workflow_runs:
        if str(run.get("availability") or "") != "blocked":
            continue
        blocked_workflows.append({
            "id": f"workflow:{run.get('id')}",
            "kind": "workflow",
            "label": str(run.get("workflow_name") or "workflow"),
            "detail": str(run.get("summary") or "Workflow blocked"),
            "status": str(run.get("status") or "blocked"),
            "thread_id": run.get("thread_id"),
            "thread_label": run.get("thread_label"),
            "continue_message": workflow_surface_continue_message(run),
            "trust_boundary": _handoff_trust_boundary(run) or workflow_surface_resume_metadata(run).get("trust_boundary"),
        })
        if len(blocked_workflows) >= 4:
            break

    follow_ups: list[dict[str, Any]] = []
    recovery_actions = continuity_snapshot.get("recovery_actions")
    if isinstance(recovery_actions, list):
        for action in recovery_actions[:4]:
            if not isinstance(action, dict):
                continue
            thread_id = action.get("thread_id")
            if session_id and thread_id not in {None, session_id}:
                continue
            follow_ups.append({
                "id": f"continuity:{action.get('id')}",
                "kind": str(action.get("kind") or "continuity"),
                "label": str(action.get("label") or "Follow-up"),
                "detail": str(action.get("detail") or ""),
                "status": str(action.get("status") or "attention"),
                "thread_id": thread_id,
                "thread_label": session_titles.get(str(thread_id)) if thread_id else None,
                "continue_message": action.get("continue_message"),
            })

    return {
        "pending_approvals": approvals,
        "blocked_workflows": blocked_workflows,
        "follow_ups": follow_ups,
    }


@router.get("/operator/control-plane")
async def get_operator_control_plane(
    session_id: str | None = Query(default=None),
    window_hours: int = Query(default=24, ge=1, le=168),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ctx = context_manager.get_context()
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions()
        if isinstance(session, dict) and session.get("id")
    }

    workflow_runs, pending_approvals, continuity_snapshot, audit_events, llm_calls = await asyncio.gather(
        _list_workflow_runs(limit=60, session_id=session_id),
        approval_repository.list_pending(session_id=session_id, limit=20),
        build_observer_continuity_snapshot(),
        audit_repository.list_events(limit=40, session_id=session_id, since=cutoff),
        asyncio.to_thread(list_recent_llm_calls, limit=300, session_id=session_id, since=cutoff),
    )
    extension_payload = await asyncio.to_thread(list_extensions)
    extension_summary = extension_payload.get("summary", {}) if isinstance(extension_payload, dict) else {}
    extensions = extension_payload.get("extensions", []) if isinstance(extension_payload, dict) else []
    governed_extension_count = sum(
        1
        for extension in extensions
        if isinstance(extension, dict)
        and isinstance(extension.get("approval_profile"), dict)
        and extension["approval_profile"].get("requires_lifecycle_approval")
    )
    continuity_summary = (
        continuity_snapshot.get("summary")
        if isinstance(continuity_snapshot.get("summary"), dict)
        else {}
    )

    return {
        "governance": {
            "workspace_mode": "single_operator_guarded_workspace",
            "review_posture": "human review gates privileged mutations, extension lifecycle changes, and governed evolution proposals",
            "approval_mode": ctx.approval_mode,
            "tool_policy_mode": ctx.tool_policy_mode,
            "mcp_policy_mode": ctx.mcp_policy_mode,
            "delegation_enabled": bool(settings.use_delegation),
            "workspace_dir": settings.workspace_dir,
            "roles": _control_plane_roles(
                str(ctx.tool_policy_mode),
                str(ctx.mcp_policy_mode),
                str(ctx.approval_mode),
            ),
        },
        "usage": _usage_summary(
            workflow_runs,
            pending_approvals,
            llm_calls,
            audit_events,
            window_hours=window_hours,
        ),
        "runtime_posture": {
            "runtime": _runtime_status_payload(),
            "extensions": {
                "total": int(extension_summary.get("total") or 0),
                "ready": int(extension_summary.get("ready") or 0),
                "degraded": int(extension_summary.get("degraded") or 0),
                "governed": governed_extension_count,
                "issue_count": int(extension_summary.get("issue_count") or 0),
                "degraded_connector_count": int(extension_summary.get("degraded_connector_count") or 0),
            },
            "continuity": {
                "continuity_health": str(continuity_summary.get("continuity_health") or "unknown"),
                "primary_surface": continuity_summary.get("primary_surface"),
                "recommended_focus": continuity_summary.get("recommended_focus"),
                "actionable_thread_count": int(continuity_summary.get("actionable_thread_count") or 0),
                "degraded_route_count": int(continuity_summary.get("degraded_route_count") or 0),
                "degraded_source_adapter_count": int(continuity_summary.get("degraded_source_adapter_count") or 0),
                "attention_presence_surface_count": int(
                    continuity_summary.get("attention_presence_surface_count") or 0
                ),
            },
        },
        "handoff": {
            **_handoff_entries(
                workflow_runs,
                pending_approvals,
                continuity_snapshot,
                session_titles,
                session_id=session_id,
            ),
            "review_receipts": _review_receipts(audit_events, session_titles),
        },
    }


@router.get("/operator/timeline")
async def get_operator_timeline(
    limit: int = Query(default=20, ge=1, le=50),
    session_id: str | None = Query(default=None),
):
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions()
        if isinstance(session, dict) and session.get("id")
    }
    workflow_runs = await _list_workflow_runs(limit=max(limit, 8), session_id=session_id)
    pending_approvals = await approval_repository.list_pending(session_id=session_id, limit=max(limit, 12))
    notifications = await native_notification_queue.list()
    queued_insights = await insight_queue.peek_all()
    recent_interventions = await guardian_feedback_repository.list_recent(
        limit=max(limit, 12),
        session_id=session_id,
    )
    audit_events = await audit_repository.list_events(limit=max(limit, 20), session_id=session_id)
    continuity_snapshot = await build_observer_continuity_snapshot()

    items: list[dict[str, Any]] = []

    for run in workflow_runs:
        workflow_surface = workflow_surface_resume_metadata(run)
        items.append({
            "id": f"workflow:{run['id']}",
            "kind": "workflow_run",
            "title": str(run["workflow_name"]),
            "summary": str(run["summary"]),
            "status": str(run["status"]),
            "created_at": str(run["started_at"]),
            "updated_at": str(run["updated_at"]),
            "thread_id": run.get("thread_id"),
            "thread_label": run.get("thread_label"),
            "continue_message": workflow_surface_continue_message(run),
            "replay_draft": workflow_surface_replay_draft(run),
            "replay_allowed": workflow_surface["replay_allowed"],
            "replay_block_reason": run.get("replay_block_reason"),
            "recommended_actions": workflow_surface_recommended_actions(run),
            "source": "workflow",
            "metadata": {
                "run_identity": run.get("run_identity"),
                "run_fingerprint": run.get("run_fingerprint"),
                "risk_level": run.get("risk_level"),
                "execution_boundaries": run.get("execution_boundaries", []),
                "step_count": len(run.get("step_records", []) or []),
                "failed_step_ids": list(run.get("continued_error_steps", []) or []),
                "failed_step_tool": run.get("failed_step_tool"),
                "pending_approval_count": run.get("pending_approval_count", 0),
                "resume_from_step": workflow_surface["resume_from_step"],
                "resume_checkpoint_label": workflow_surface["resume_checkpoint_label"],
                "last_completed_step_id": run.get("last_completed_step_id"),
                "checkpoint_step_ids": list(run.get("checkpoint_step_ids", []) or []),
                "checkpoint_candidates": workflow_surface["checkpoint_candidates"],
                "branch_kind": run.get("branch_kind"),
                "branch_depth": run.get("branch_depth"),
                "parent_run_identity": run.get("parent_run_identity"),
                "root_run_identity": run.get("root_run_identity"),
                "resume_plan": workflow_surface["resume_plan"],
                "availability": run.get("availability"),
                "trust_boundary": workflow_surface["trust_boundary"],
            },
        })

    for approval in pending_approvals:
        approval_session_id = approval.get("session_id")
        approval_metadata = approval_surface_metadata(approval)
        items.append({
            "id": f"approval:{approval['id']}",
            "kind": "approval",
            "title": str(approval.get("tool_name") or "approval"),
            "summary": str(approval.get("summary") or "Approval pending"),
            "status": "pending",
            "created_at": str(approval.get("created_at") or ""),
            "updated_at": str(approval.get("created_at") or ""),
            "thread_id": approval.get("thread_id") or approval_session_id,
            "thread_label": (
                approval.get("thread_label")
                or (session_titles.get(str(approval_session_id)) if approval_session_id else None)
            ),
            "continue_message": approval.get("resume_message"),
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": "pending_approval",
            "recommended_actions": [],
            "source": "approval",
            "metadata": approval_metadata,
        })

    for notification in notifications:
        session_ref = notification.session_id
        if session_id and session_ref != session_id:
            continue
        thread_id = getattr(notification, "thread_id", None) or session_ref
        continuation_mode = getattr(notification, "continuation_mode", None)
        thread_source = getattr(notification, "thread_source", None)
        items.append({
            "id": f"notification:{notification.id}",
            "kind": "notification",
            "title": notification.title,
            "summary": notification.body,
            "status": "queued",
            "created_at": _timeline_timestamp(notification.created_at),
            "updated_at": _timeline_timestamp(notification.created_at),
            "thread_id": thread_id,
            "thread_label": session_titles.get(thread_id) if thread_id else None,
            "continue_message": notification.resume_message or notification.body,
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": None,
            "recommended_actions": [],
            "source": "native_notification",
            "metadata": {
                "surface": "notification",
                "intervention_type": notification.intervention_type,
                "urgency": notification.urgency,
                "thread_source": thread_source,
                "continuation_mode": continuation_mode,
            },
        })

    for insight in queued_insights:
        if session_id and getattr(insight, "session_id", None) not in {None, session_id}:
            continue
        if session_id:
            match = next(
                (
                    item for item in recent_interventions
                    if item.id == insight.intervention_id and item.session_id == session_id
                ),
                None,
            )
            if insight.intervention_id and getattr(insight, "session_id", None) is None and match is None:
                continue
        thread_id = next(
            (
                item.session_id for item in recent_interventions
                if item.id == insight.intervention_id
            ),
            None,
        ) or getattr(insight, "session_id", None)
        items.append({
            "id": f"queued:{insight.id}",
            "kind": "queued_insight",
            "title": insight.intervention_type,
            "summary": insight.content,
            "status": "queued",
            "created_at": _timeline_timestamp(insight.created_at),
            "updated_at": _timeline_timestamp(insight.created_at),
            "thread_id": thread_id,
            "thread_label": session_titles.get(thread_id) if thread_id else None,
            "continue_message": f"Continue from queued guardian bundle: {insight.content}",
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": None,
            "recommended_actions": [],
            "source": "bundle_queue",
            "metadata": {
                "urgency": insight.urgency,
                "reasoning": insight.reasoning,
            },
        })

    for intervention in recent_interventions:
        if session_id and intervention.session_id != session_id:
            continue
        items.append({
            "id": f"intervention:{intervention.id}",
            "kind": "intervention",
            "title": intervention.intervention_type,
            "summary": intervention.content_excerpt,
            "status": intervention.latest_outcome,
            "created_at": _timeline_timestamp(intervention.updated_at),
            "updated_at": _timeline_timestamp(intervention.updated_at),
            "thread_id": intervention.session_id,
            "thread_label": session_titles.get(intervention.session_id) if intervention.session_id else None,
            "continue_message": f"Continue from this guardian intervention: {intervention.content_excerpt}",
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": None,
            "recommended_actions": [],
            "source": _continuity_surface(
                latest_outcome=intervention.latest_outcome,
                transport=intervention.transport,
                policy_action=intervention.policy_action,
            ),
            "metadata": {
                "policy_action": intervention.policy_action,
                "policy_reason": intervention.policy_reason,
                "transport": intervention.transport,
                "feedback_type": intervention.feedback_type,
            },
        })

    for event in audit_events:
        event_type = str(event.get("event_type") or "")
        if event_type in {"llm_routing_decision", "llm_target_rerouted"}:
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            summary = str(event.get("summary") or event_type)
            metadata = {
                "event_type": event_type,
                "runtime_path": details.get("runtime_path"),
                "runtime_profile": details.get("runtime_profile"),
                "selected_model": details.get("selected_model"),
                "selected_profile": details.get("selected_profile"),
                "selected_source": details.get("selected_source"),
                "selected_reason_codes": details.get("selected_reason_codes", []),
                "selected_policy_score": details.get("selected_policy_score"),
                "required_policy_intents": details.get("required_policy_intents", []),
                "max_cost_tier": details.get("max_cost_tier"),
                "max_latency_tier": details.get("max_latency_tier"),
                "required_task_class": details.get("required_task_class"),
                "max_budget_class": details.get("max_budget_class"),
                "budget_steering_mode": details.get("budget_steering_mode"),
                "selected_budget_headroom": details.get("selected_budget_headroom"),
                "selected_budget_preference_score": details.get("selected_budget_preference_score"),
                "selected_route_score": details.get("selected_route_score"),
                "selected_failure_risk_score": details.get("selected_failure_risk_score"),
                "selected_production_readiness": details.get("selected_production_readiness"),
                "selected_live_feedback": details.get("selected_live_feedback"),
                "attempt_order": details.get("attempt_order", []),
                "reroute_cause": details.get("reroute_cause"),
                "rerouted_from_unhealthy_primary": details.get("rerouted_from_unhealthy_primary"),
                "rerouted_from_policy_guardrails": details.get("rerouted_from_policy_guardrails"),
                "guardrail_compliant_targets_present": details.get("guardrail_compliant_targets_present"),
                "route_explanation": details.get("route_explanation"),
                "rejected_target_count": details.get("rejected_target_count"),
                "rejected_target_summaries": details.get("rejected_target_summaries", []),
                "candidate_targets": details.get("candidate_targets", []),
                "simulated_routes": details.get("simulated_routes", []),
                "rejected_targets": details.get("rejected_targets", []),
            }
            items.append({
                "id": f"audit:{event['id']}",
                "kind": "routing",
                "title": str(event.get("tool_name") or "llm routing"),
                "summary": summary,
                "status": "rerouted" if event_type == "llm_target_rerouted" else "selected",
                "created_at": str(event.get("created_at") or ""),
                "updated_at": str(event.get("created_at") or ""),
                "thread_id": event.get("session_id"),
                "thread_label": session_titles.get(str(event.get("session_id"))) if event.get("session_id") else None,
                "continue_message": None,
                "replay_draft": None,
                "replay_allowed": False,
                "replay_block_reason": None,
                "recommended_actions": [],
                "source": "routing",
                "metadata": metadata,
            })
            continue
        if event_type not in {"tool_failed", "integration_failed", "llm_primary_failure", "llm_fallback_failure"}:
            continue
        items.append({
            "id": f"audit:{event['id']}",
            "kind": "audit",
            "title": str(event.get("tool_name") or event_type),
            "summary": str(event.get("summary") or event_type),
            "status": "failed",
            "created_at": str(event.get("created_at") or ""),
            "updated_at": str(event.get("created_at") or ""),
            "thread_id": event.get("session_id"),
            "thread_label": session_titles.get(str(event.get("session_id"))) if event.get("session_id") else None,
            "continue_message": None,
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": None,
            "recommended_actions": [],
            "source": "audit",
            "metadata": {
                "event_type": event_type,
                "risk_level": event.get("risk_level"),
            },
        })

    items.extend(
        _continuity_operator_items(
            continuity_snapshot,
            session_id=session_id,
            session_titles=session_titles,
        )
    )

    items.sort(
        key=lambda item: _parse_iso(str(item.get("updated_at") or item.get("created_at"))),
        reverse=True,
    )
    return {"items": items[:limit]}
