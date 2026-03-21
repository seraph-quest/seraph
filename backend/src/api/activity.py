"""Activity Ledger API — user-facing history of agent actions, spend, and thread continuity."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query

from src.agent.session import session_manager
from src.api.observer import _continuity_surface
from src.api.workflows import _list_workflow_runs
from src.approval.repository import approval_repository
from src.audit.repository import audit_repository
from src.guardian.feedback import guardian_feedback_repository
from src.llm_logger import list_recent_llm_calls
from src.observer.insight_queue import insight_queue
from src.observer.native_notification_queue import native_notification_queue

router = APIRouter()


def _parse_iso(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _timestamp(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _model_label(model: str | None) -> str:
    if not model:
        return "unknown"
    return str(model).split("/")[-1]


def _thread_label(session_titles: dict[str, str], thread_id: str | None) -> str | None:
    if not thread_id:
        return None
    return session_titles.get(thread_id)


def _category_for_kind(kind: str) -> str:
    if kind in {"llm_call", "routing"}:
        return "llm"
    if kind in {"workflow_run"}:
        return "workflow"
    if kind in {"approval"}:
        return "approval"
    if kind in {"notification", "queued_insight", "intervention"}:
        return "guardian"
    if kind in {"agent_run", "tool_call", "tool_result", "tool_failed"}:
        return "agent"
    return "system"


def _status_for_audit_event(event_type: str) -> str:
    if event_type == "tool_call":
        return "running"
    if event_type == "tool_result":
        return "succeeded"
    if event_type.endswith("_failed") or event_type in {"tool_failed", "integration_failed", "llm_primary_failure", "llm_fallback_failure"}:
        return "failed"
    if event_type.endswith("_timed_out"):
        return "timed_out"
    if event_type.endswith("_skipped"):
        return "skipped"
    if event_type.endswith("_succeeded"):
        return "succeeded"
    if event_type in {"approval_requested"}:
        return "pending"
    return "recorded"


def _title_for_audit_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "")
    tool_name = str(event.get("tool_name") or "")
    if event_type in {"tool_call", "tool_result", "tool_failed"}:
        return tool_name or "Tool"
    if event_type.startswith("agent_run_"):
        return "Agent run"
    if event_type.startswith("scheduler_job_"):
        return tool_name or "Scheduler job"
    if event_type.startswith("background_task_"):
        return tool_name or "Background task"
    if event_type.startswith("observer_delivery_"):
        return "Guardian delivery"
    if event_type.startswith("integration_"):
        return tool_name or "Integration"
    if event_type in {"llm_routing_decision", "llm_target_rerouted"}:
        return "LLM routing"
    return tool_name or event_type.replace("_", " ")


def _kind_for_audit_event(event_type: str) -> str | None:
    if event_type in {"tool_call", "tool_result"}:
        return event_type
    if event_type == "tool_failed":
        return "tool_failed"
    if event_type in {"llm_routing_decision", "llm_target_rerouted"}:
        return "routing"
    if event_type.startswith("agent_run_"):
        return "agent_run"
    if event_type.startswith("scheduler_job_"):
        return "scheduler_job"
    if event_type.startswith("background_task_"):
        return "background_task"
    if event_type.startswith("observer_delivery_"):
        return "delivery"
    if event_type.startswith("integration_"):
        return "integration"
    if event_type in {"tool_failed", "integration_failed", "llm_primary_failure", "llm_fallback_failure"}:
        return "failure"
    return None


def _request_id_from_item(item: dict[str, Any]) -> str | None:
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        request_id = metadata.get("request_id")
        if isinstance(request_id, str) and request_id:
            return request_id
    return None


def _group_key_for_item(item: dict[str, Any]) -> str:
    request_id = _request_id_from_item(item)
    if request_id:
        return f"request:{request_id}"
    if item["kind"] == "workflow_run":
        return f"workflow:{item['id']}"
    if item["kind"] == "approval":
        return f"approval:{item['id']}"
    if item["kind"] in {"intervention", "notification", "queued_insight"}:
        return f"guardian:{item['id']}"
    thread_id = item.get("thread_id") or "ambient"
    return f"{item['kind']}:{thread_id}:{item['updated_at']}"


def _llm_title_and_summary(entry: dict[str, Any], session_titles: dict[str, str]) -> tuple[str, str]:
    source = str(entry.get("source") or "background")
    session_id = entry.get("session_id") if isinstance(entry.get("session_id"), str) else None
    thread = session_titles.get(session_id or "", "current thread") if session_id else "background runtime"
    model = _model_label(entry.get("model"))
    if source == "rest_chat":
        return "LLM call", f"REST chat reasoning for {thread} using {model}"
    if source == "websocket_chat":
        return "LLM call", f"Conversation reasoning for {thread} using {model}"
    if source == "strategist_tick":
        return "LLM call", f"Strategist tick reasoning using {model}"
    if source == "session_runtime":
        return "LLM call", f"Thread runtime reasoning for {thread} using {model}"
    return "LLM call", f"Background reasoning using {model}"


def _entry_metric_count(items: list[dict[str, Any]], key: str) -> float:
    total = 0.0
    for item in items:
        value = item.get(key)
        if isinstance(value, (int, float)):
            total += float(value)
    return total


def _slice_visible_groups(items: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], int]:
    visible: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for item in items:
        group_key = str(item.get("group_key") or item["id"])
        if group_key not in seen_groups:
            if len(seen_groups) >= limit:
                continue
            seen_groups.add(group_key)
        visible.append(item)
    return visible, len(seen_groups)


def _compact_step_records(step_records: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not isinstance(step_records, list):
        return []
    compact: list[dict[str, Any]] = []
    for record in step_records:
        if not isinstance(record, dict):
            continue
        compact.append({
            "id": record.get("id"),
            "step_id": record.get("step_id"),
            "tool": record.get("tool"),
            "status": record.get("status"),
            "summary": record.get("summary") or record.get("result_summary") or record.get("error_summary"),
            "duration_ms": record.get("duration_ms"),
            "error_kind": record.get("error_kind"),
            "error_summary": record.get("error_summary"),
            "result_summary": record.get("result_summary"),
        })
    return compact


@router.get("/activity/ledger")
async def get_activity_ledger(
    limit: int = Query(default=40, ge=1, le=200),
    session_id: str | None = Query(default=None),
    window_hours: int = Query(default=24, ge=1, le=168),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions()
        if isinstance(session, dict) and session.get("id")
    }

    workflow_scan_limit = max(limit * 4, 200)
    approval_scan_limit = max(limit * 4, 200)
    intervention_scan_limit = max(limit * 4, 200)
    audit_scan_limit = 1000
    llm_scan_limit = 1000
    workflow_runs, pending_approvals, notifications, queued_insights, recent_interventions, audit_events, llm_calls = await asyncio.gather(
        _list_workflow_runs(limit=workflow_scan_limit, session_id=session_id),
        approval_repository.list_pending(session_id=session_id, limit=approval_scan_limit),
        native_notification_queue.list(),
        insight_queue.peek_all(),
        guardian_feedback_repository.list_recent(limit=intervention_scan_limit, session_id=session_id),
        audit_repository.list_events(limit=audit_scan_limit, session_id=session_id, since=cutoff),
        asyncio.to_thread(list_recent_llm_calls, limit=llm_scan_limit, session_id=session_id, since=cutoff),
    )

    items: list[dict[str, Any]] = []

    for run in workflow_runs:
        updated_at = str(run.get("updated_at") or run.get("started_at") or "")
        if _parse_iso(updated_at) < cutoff:
            continue
        items.append({
            "id": f"workflow:{run['id']}",
            "kind": "workflow_run",
            "category": "workflow",
            "title": str(run["workflow_name"]),
            "summary": str(run["summary"]),
            "status": str(run["status"]),
            "created_at": str(run["started_at"]),
            "updated_at": updated_at,
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
            "model": None,
            "provider": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost_usd": None,
            "duration_ms": None,
            "metadata": {
                "risk_level": run.get("risk_level"),
                "execution_boundaries": run.get("execution_boundaries", []),
                "step_count": len(run.get("step_records", []) or []),
                "step_records": _compact_step_records(run.get("step_records")),
                "failed_step_ids": list(run.get("continued_error_steps", []) or []),
                "failed_step_tool": run.get("failed_step_tool"),
                "pending_approval_count": run.get("pending_approval_count", 0),
                "availability": run.get("availability"),
                "branch_kind": run.get("branch_kind"),
                "run_identity": run.get("run_identity"),
                "run_fingerprint": run.get("run_fingerprint"),
                "resume_from_step": run.get("resume_from_step"),
                "resume_checkpoint_label": run.get("resume_checkpoint_label"),
                "checkpoint_candidates": run.get("checkpoint_candidates", []),
                "resume_plan": run.get("resume_plan"),
            },
        })

    for approval in pending_approvals:
        created_at = str(approval.get("created_at") or "")
        if _parse_iso(created_at) < cutoff:
            continue
        approval_session_id = approval.get("session_id")
        items.append({
            "id": f"approval:{approval['id']}",
            "kind": "approval",
            "category": "approval",
            "title": str(approval.get("tool_name") or "approval"),
            "summary": str(approval.get("summary") or "Approval pending"),
            "status": "pending",
            "created_at": created_at,
            "updated_at": created_at,
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
            "model": None,
            "provider": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost_usd": None,
            "duration_ms": None,
            "metadata": {
                "risk_level": approval.get("risk_level"),
                "tool_name": approval.get("tool_name"),
                "approval_id": approval.get("id"),
            },
        })

    for notification in notifications:
        created_at = _timestamp(notification.created_at)
        if _parse_iso(created_at) < cutoff:
            continue
        session_ref = notification.session_id
        if session_id and session_ref != session_id:
            continue
        thread_id = getattr(notification, "thread_id", None) or session_ref
        continuation_mode = getattr(notification, "continuation_mode", None)
        thread_source = getattr(notification, "thread_source", None)
        items.append({
            "id": f"notification:{notification.id}",
            "kind": "notification",
            "category": "guardian",
            "title": notification.title,
            "summary": notification.body,
            "status": "queued",
            "created_at": created_at,
            "updated_at": created_at,
            "thread_id": thread_id,
            "thread_label": _thread_label(session_titles, thread_id),
            "continue_message": notification.resume_message or notification.body,
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": None,
            "recommended_actions": [],
            "source": "native_notification",
            "model": None,
            "provider": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost_usd": None,
            "duration_ms": None,
            "metadata": {
                "surface": "notification",
                "intervention_type": notification.intervention_type,
                "urgency": notification.urgency,
                "thread_source": thread_source,
                "continuation_mode": continuation_mode,
                "notification_id": notification.id,
            },
        })

    for insight in queued_insights:
        created_at = _timestamp(insight.created_at)
        if _parse_iso(created_at) < cutoff:
            continue
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
            "category": "guardian",
            "title": insight.intervention_type,
            "summary": insight.content,
            "status": "queued",
            "created_at": created_at,
            "updated_at": created_at,
            "thread_id": thread_id,
            "thread_label": _thread_label(session_titles, thread_id),
            "continue_message": f"Continue from queued guardian bundle: {insight.content}",
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": None,
            "recommended_actions": [],
            "source": "bundle_queue",
            "model": None,
            "provider": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost_usd": None,
            "duration_ms": None,
            "metadata": {
                "urgency": insight.urgency,
                "reasoning": insight.reasoning,
                "intervention_id": insight.intervention_id,
            },
        })

    for intervention in recent_interventions:
        updated_at = _timestamp(intervention.updated_at)
        if _parse_iso(updated_at) < cutoff:
            continue
        if session_id and intervention.session_id != session_id:
            continue
        items.append({
            "id": f"intervention:{intervention.id}",
            "kind": "intervention",
            "category": "guardian",
            "title": intervention.intervention_type,
            "summary": intervention.content_excerpt,
            "status": intervention.latest_outcome,
            "created_at": updated_at,
            "updated_at": updated_at,
            "thread_id": intervention.session_id,
            "thread_label": _thread_label(session_titles, intervention.session_id),
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
            "model": None,
            "provider": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost_usd": None,
            "duration_ms": None,
            "metadata": {
                "policy_action": intervention.policy_action,
                "policy_reason": intervention.policy_reason,
                "transport": intervention.transport,
                "feedback_type": intervention.feedback_type,
                "intervention_id": intervention.id,
            },
        })

    request_summaries: dict[str, str] = {}
    for event in audit_events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        request_id = details.get("request_id") if isinstance(details.get("request_id"), str) else None
        if request_id and str(event.get("summary") or "").strip():
            request_summaries.setdefault(request_id, str(event.get("summary")))

        event_type = str(event.get("event_type") or "")
        kind = _kind_for_audit_event(event_type)
        if kind is None:
            continue
        created_at = str(event.get("created_at") or "")
        items.append({
            "id": f"audit:{event['id']}",
            "kind": kind,
            "category": _category_for_kind(kind),
            "title": _title_for_audit_event(event),
            "summary": str(event.get("summary") or event_type.replace("_", " ")),
            "status": _status_for_audit_event(event_type),
            "created_at": created_at,
            "updated_at": created_at,
            "thread_id": event.get("session_id"),
            "thread_label": _thread_label(session_titles, event.get("session_id")),
            "continue_message": None,
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": None,
            "recommended_actions": [],
            "source": (
                "routing" if kind == "routing"
                else "audit"
            ),
            "model": (
                str(details.get("selected_model"))
                if isinstance(details.get("selected_model"), str)
                else None
            ),
            "provider": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost_usd": None,
            "duration_ms": (
                int(details["duration_ms"])
                if isinstance(details.get("duration_ms"), int)
                else None
            ),
            "metadata": {
                **details,
                "event_type": event_type,
            },
        })

    for index, call in enumerate(llm_calls):
        created_at = str(call.get("timestamp") or "")
        session_ref = call.get("session_id") if isinstance(call.get("session_id"), str) else None
        if session_id and session_ref != session_id:
            continue
        title, summary = _llm_title_and_summary(call, session_titles)
        request_id = call.get("request_id") if isinstance(call.get("request_id"), str) else None
        related_summary = request_summaries.get(request_id or "")
        items.append({
            "id": f"llm:{request_id or index}:{created_at}",
            "kind": "llm_call",
            "category": "llm",
            "title": title,
            "summary": related_summary or summary,
            "status": str(call.get("status") or "unknown"),
            "created_at": created_at,
            "updated_at": created_at,
            "thread_id": session_ref,
            "thread_label": _thread_label(session_titles, session_ref),
            "continue_message": None,
            "replay_draft": None,
            "replay_allowed": False,
            "replay_block_reason": None,
            "recommended_actions": [],
            "source": str(call.get("source") or "background"),
            "model": str(call.get("model") or ""),
            "provider": str(call.get("provider") or ""),
            "prompt_tokens": int((call.get("tokens") or {}).get("input", 0)),
            "completion_tokens": int((call.get("tokens") or {}).get("output", 0)),
            "cost_usd": float(call.get("cost_usd", 0) or 0),
            "duration_ms": float(call.get("latency_ms", 0) or 0),
            "metadata": {
                "request_id": request_id,
                "call_type": call.get("call_type"),
                "stream": bool(call.get("stream")),
                "api_base": call.get("api_base"),
                "total_tokens": int((call.get("tokens") or {}).get("total", 0)),
                "actor": call.get("actor"),
                "error": call.get("error"),
            },
        })

    for item in items:
        item["group_key"] = _group_key_for_item(item)

    items = [
        item for item in items
        if _parse_iso(str(item.get("updated_at") or item.get("created_at"))) >= cutoff
    ]
    items.sort(
        key=lambda item: _parse_iso(str(item.get("updated_at") or item.get("created_at"))),
        reverse=True,
    )
    visible_items, visible_group_count = _slice_visible_groups(items, limit)
    llm_items = [item for item in items if item["kind"] == "llm_call"]
    partial_sources = [
        source
        for source, fetched_count, source_limit in (
            ("workflows", len(workflow_runs), workflow_scan_limit),
            ("approvals", len(pending_approvals), approval_scan_limit),
            ("interventions", len(recent_interventions), intervention_scan_limit),
            ("audit", len(audit_events), audit_scan_limit),
            ("llm", len(llm_calls), llm_scan_limit),
        )
        if fetched_count >= source_limit
    ]

    summary = {
        "window_hours": window_hours,
        "started_at": cutoff.isoformat(),
        "total_items": len(items),
        "visible_items": len(visible_items),
        "visible_groups": visible_group_count,
        "is_partial": bool(partial_sources),
        "partial_sources": partial_sources,
        "pending_approvals": sum(1 for item in items if item["kind"] == "approval"),
        "failure_count": sum(1 for item in items if item["status"] in {"failed", "timed_out"}),
        "llm_call_count": len(llm_items),
        "llm_cost_usd": round(_entry_metric_count(llm_items, "cost_usd"), 6),
        "input_tokens": int(_entry_metric_count(llm_items, "prompt_tokens")),
        "output_tokens": int(_entry_metric_count(llm_items, "completion_tokens")),
        "user_triggered_llm_calls": sum(
            1 for item in llm_items
            if item["kind"] == "llm_call" and item["source"] in {"rest_chat", "websocket_chat"}
        ),
        "autonomous_llm_calls": sum(
            1 for item in llm_items
            if item["kind"] == "llm_call" and item["source"] not in {"rest_chat", "websocket_chat"}
        ),
        "categories": {
            "llm": sum(1 for item in items if item["category"] == "llm"),
            "workflow": sum(1 for item in items if item["category"] == "workflow"),
            "approval": sum(1 for item in items if item["category"] == "approval"),
            "guardian": sum(1 for item in items if item["category"] == "guardian"),
            "agent": sum(1 for item in items if item["category"] == "agent"),
            "system": sum(1 for item in items if item["category"] == "system"),
        },
    }
    return {"items": visible_items, "summary": summary}
