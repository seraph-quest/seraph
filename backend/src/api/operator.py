"""Operator API — threaded live timeline across workflows, approvals, notifications, and guardian continuity."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from src.agent.session import session_manager
from src.api.observer import _continuity_surface
from src.api.workflows import _list_workflow_runs
from src.approval.repository import approval_repository
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
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _timeline_timestamp(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


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

    items: list[dict[str, Any]] = []

    for run in workflow_runs:
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
            "continue_message": (
                run.get("thread_continue_message")
                or run.get("approval_recovery_message")
                or run.get("retry_from_step_draft")
                or run.get("replay_draft")
            ),
            "replay_draft": run.get("replay_draft"),
            "replay_allowed": run.get("replay_allowed"),
            "replay_block_reason": run.get("replay_block_reason"),
            "recommended_actions": run.get("replay_recommended_actions", []),
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
                "resume_from_step": run.get("resume_from_step"),
                "resume_checkpoint_label": run.get("resume_checkpoint_label"),
                "last_completed_step_id": run.get("last_completed_step_id"),
                "checkpoint_step_ids": list(run.get("checkpoint_step_ids", []) or []),
                "checkpoint_candidates": run.get("checkpoint_candidates", []),
                "branch_kind": run.get("branch_kind"),
                "branch_depth": run.get("branch_depth"),
                "parent_run_identity": run.get("parent_run_identity"),
                "root_run_identity": run.get("root_run_identity"),
                "resume_plan": run.get("resume_plan"),
                "availability": run.get("availability"),
            },
        })

    for approval in pending_approvals:
        approval_session_id = approval.get("session_id")
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
            "metadata": {
                "risk_level": approval.get("risk_level"),
                "tool_name": approval.get("tool_name"),
            },
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
                "attempt_order": details.get("attempt_order", []),
                "reroute_cause": details.get("reroute_cause"),
                "rerouted_from_unhealthy_primary": details.get("rerouted_from_unhealthy_primary"),
                "rerouted_from_policy_guardrails": details.get("rerouted_from_policy_guardrails"),
                "guardrail_compliant_targets_present": details.get("guardrail_compliant_targets_present"),
                "rejected_target_count": details.get("rejected_target_count"),
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

    items.sort(
        key=lambda item: _parse_iso(str(item.get("updated_at") or item.get("created_at"))),
        reverse=True,
    )
    return {"items": items[:limit]}
