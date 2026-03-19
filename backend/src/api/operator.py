"""Operator API — threaded live timeline across workflows, approvals, notifications, and guardian continuity."""

from __future__ import annotations

from datetime import datetime
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


def _parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.min
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
    recent_interventions = await guardian_feedback_repository.list_recent(limit=max(limit, 12))
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
                or run.get("replay_draft")
            ),
            "replay_draft": run.get("replay_draft"),
            "replay_allowed": run.get("replay_allowed"),
            "replay_block_reason": run.get("replay_block_reason"),
            "recommended_actions": run.get("replay_recommended_actions", []),
            "source": "workflow",
            "metadata": {
                "risk_level": run.get("risk_level"),
                "execution_boundaries": run.get("execution_boundaries", []),
                "pending_approval_count": run.get("pending_approval_count", 0),
                "resume_from_step": run.get("resume_from_step"),
                "resume_checkpoint_label": run.get("resume_checkpoint_label"),
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
        items.append({
            "id": f"notification:{notification.id}",
            "kind": "notification",
            "title": notification.title,
            "summary": notification.body,
            "status": "queued",
            "created_at": notification.created_at.isoformat(),
            "updated_at": notification.created_at.isoformat(),
            "thread_id": session_ref,
            "thread_label": session_titles.get(session_ref) if session_ref else None,
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
            },
        })

    for insight in queued_insights:
        if session_id:
            match = next(
                (
                    item for item in recent_interventions
                    if item.id == insight.intervention_id and item.session_id == session_id
                ),
                None,
            )
            if insight.intervention_id and match is None:
                continue
        thread_id = next(
            (
                item.session_id for item in recent_interventions
                if item.id == insight.intervention_id
            ),
            None,
        )
        items.append({
            "id": f"queued:{insight.id}",
            "kind": "queued_insight",
            "title": insight.intervention_type,
            "summary": insight.content,
            "status": "queued",
            "created_at": insight.created_at.isoformat(),
            "updated_at": insight.created_at.isoformat(),
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
            "created_at": intervention.updated_at.isoformat(),
            "updated_at": intervention.updated_at.isoformat(),
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
