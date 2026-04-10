"""Operator API — threaded live timeline and team control plane surfaces."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import re
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
from src.tools.process_tools import process_runtime_manager

router = APIRouter()


_ENGINEERING_PULL_REQUEST_RE = re.compile(
    r"\b(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)\b"
)
_ENGINEERING_WORK_ITEM_RE = re.compile(
    r"\b(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>\d+)\b"
)
_ENGINEERING_REPOSITORY_RE = re.compile(
    r"\b(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)\b"
)
_ENGINEERING_FILELIKE_SUFFIXES = (
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".py",
    ".tsx",
    ".ts",
    ".jsx",
    ".js",
    ".css",
    ".html",
    ".sh",
    ".log",
)


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


def _engineering_query(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _engineering_filelike_reference(candidate: str) -> bool:
    lower = candidate.lower()
    return any(lower.endswith(suffix) for suffix in _ENGINEERING_FILELIKE_SUFFIXES)


def _extract_engineering_references(
    *values: str | None,
    allow_repository: bool = True,
) -> list[dict[str, str]]:
    matches: list[tuple[int, int, dict[str, str]]] = []

    def _occupied(start: int, end: int) -> bool:
        return any(not (end <= left or start >= right) for left, right, _ in matches)

    for raw_value in values:
        text = " ".join(str(raw_value or "").split()).strip()
        if not text:
            continue
        for pattern, target_kind in (
            (_ENGINEERING_PULL_REQUEST_RE, "pull_request"),
            (_ENGINEERING_WORK_ITEM_RE, "work_item"),
        ):
            for match in pattern.finditer(text):
                repository_reference = f"{match.group('owner')}/{match.group('repo')}"
                reference = (
                    f"{repository_reference}/pull/{match.group('number')}"
                    if target_kind == "pull_request"
                    else f"{repository_reference}#{match.group('number')}"
                )
                matches.append(
                    (
                        match.start(),
                        match.end(),
                        {
                            "reference": reference,
                            "target_kind": target_kind,
                            "repository_reference": repository_reference,
                        },
                    )
                )
        if not allow_repository:
            continue
        for match in _ENGINEERING_REPOSITORY_RE.finditer(text):
            if _occupied(match.start(), match.end()):
                continue
            reference = f"{match.group('owner')}/{match.group('repo')}"
            if _engineering_filelike_reference(reference):
                continue
            matches.append(
                (
                    match.start(),
                    match.end(),
                    {
                        "reference": reference,
                        "target_kind": "repository",
                        "repository_reference": reference,
                    },
                )
            )

    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, _, item in sorted(matches, key=lambda entry: (entry[0], entry[1])):
        reference = item["reference"]
        if reference in seen:
            continue
        seen.add(reference)
        ordered.append(item)
    return ordered


def _engineering_target_priority(target_kind: str) -> int:
    if target_kind == "pull_request":
        return 3
    if target_kind == "work_item":
        return 2
    if target_kind == "repository":
        return 1
    return 0


def _engineering_bundle_priority(bundle: dict[str, Any]) -> tuple[int, int, datetime]:
    signal_count = (
        int(bundle.get("workflow_count") or 0)
        + int(bundle.get("approval_count") or 0)
        + int(bundle.get("audit_event_count") or 0)
        + int(bundle.get("session_match_count") or 0)
    )
    return (
        _engineering_target_priority(str(bundle.get("target_kind") or "")),
        signal_count,
        _parse_iso(str(bundle.get("latest_updated_at") or "")),
    )


def _engineering_matches_query(
    source: dict[str, Any],
    normalized_query: str,
    matched_references_by_session: dict[str, set[str]],
) -> bool:
    if not normalized_query:
        return True
    session_id = source.get("session_id")
    reference = str(source.get("reference") or "")
    if (
        isinstance(session_id, str)
        and reference
        and reference in matched_references_by_session.get(session_id, set())
    ):
        return True
    haystacks = [
        reference,
        source.get("repository_reference"),
        source.get("title"),
        source.get("summary"),
        source.get("continue_message"),
        source.get("snippet"),
    ]
    return any(normalized_query in str(value or "").lower() for value in haystacks)


def _build_engineering_memory_bundles(
    workflow_runs: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    audit_events: list[dict[str, Any]],
    session_search_matches: list[dict[str, Any]],
    *,
    normalized_query: str,
    limit_bundles: int,
    limit_session_matches: int,
) -> list[dict[str, Any]]:
    matched_references_by_session: dict[str, set[str]] = {}
    for item in session_search_matches:
        if not isinstance(item, dict) or not isinstance(item.get("session_id"), str):
            continue
        session_id = str(item["session_id"])
        for ref in _extract_engineering_references(
            str(item.get("title") or ""),
            str(item.get("snippet") or ""),
        ):
            matched_references_by_session.setdefault(session_id, set()).add(ref["reference"])
    source_entries: list[dict[str, Any]] = []

    for run in workflow_runs:
        references = _extract_engineering_references(
            str(run.get("summary") or ""),
            str(workflow_surface_continue_message(run) or ""),
            str(run.get("thread_continue_message") or ""),
        )
        for ref in references:
            source_entries.append(
                {
                    **ref,
                    "source_kind": "workflow",
                    "session_id": run.get("thread_id"),
                    "thread_label": run.get("thread_label"),
                    "title": run.get("workflow_name"),
                    "summary": run.get("summary"),
                    "status": run.get("status"),
                    "updated_at": str(run.get("updated_at") or run.get("started_at") or ""),
                    "continue_message": workflow_surface_continue_message(run),
                    "artifact_paths": list(run.get("artifact_paths") or [])
                    if isinstance(run.get("artifact_paths"), list)
                    else [],
                }
            )

    for approval in pending_approvals:
        approval_metadata = approval_surface_metadata(approval)
        approval_scope = approval_metadata.get("approval_scope")
        target_reference = (
            approval_scope.get("target", {}).get("reference")
            if isinstance(approval_scope, dict)
            and isinstance(approval_scope.get("target"), dict)
            else None
        )
        references = _extract_engineering_references(str(target_reference or ""))
        for ref in references:
            source_entries.append(
                {
                    **ref,
                    "source_kind": "approval",
                    "session_id": approval.get("thread_id") or approval.get("session_id"),
                    "thread_label": approval.get("thread_label"),
                    "title": approval.get("tool_name"),
                    "summary": approval.get("summary"),
                    "status": approval.get("risk_level") or "pending",
                    "updated_at": str(approval.get("created_at") or ""),
                    "continue_message": approval.get("resume_message"),
                    "artifact_paths": [],
                }
            )

    for event in audit_events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        target_reference = details.get("target_reference") if isinstance(details.get("target_reference"), str) else ""
        references = _extract_engineering_references(
            target_reference,
            str(event.get("summary") or ""),
        )
        for ref in references:
            source_entries.append(
                {
                    **ref,
                    "source_kind": "audit",
                    "session_id": event.get("session_id"),
                    "thread_label": None,
                    "title": event.get("tool_name") or event.get("event_type"),
                    "summary": event.get("summary"),
                    "status": event.get("event_type"),
                    "updated_at": str(event.get("created_at") or ""),
                    "continue_message": None,
                    "artifact_paths": [],
                }
            )

    for match in session_search_matches:
        if not isinstance(match, dict):
            continue
        references = _extract_engineering_references(
            str(match.get("title") or ""),
            str(match.get("snippet") or ""),
        )
        for ref in references:
            source_entries.append(
                {
                    **ref,
                    "source_kind": "session_search",
                    "session_id": match.get("session_id"),
                    "thread_label": match.get("title"),
                    "title": match.get("title"),
                    "summary": match.get("snippet"),
                    "status": match.get("source"),
                    "updated_at": str(match.get("matched_at") or ""),
                    "continue_message": None,
                    "snippet": match.get("snippet"),
                    "artifact_paths": [],
                }
            )

    bundles: dict[str, dict[str, Any]] = {}
    for source in source_entries:
        if not _engineering_matches_query(source, normalized_query, matched_references_by_session):
            continue
        reference = str(source.get("reference") or "")
        if not reference:
            continue
        bundle = bundles.setdefault(
            reference,
            {
                "reference": reference,
                "target_kind": source.get("target_kind"),
                "repository_reference": source.get("repository_reference"),
                "latest_updated_at": source.get("updated_at") or "",
                "workflow_count": 0,
                "approval_count": 0,
                "audit_event_count": 0,
                "session_match_count": 0,
                "thread_ids": set(),
                "thread_labels": set(),
                "artifact_paths": set(),
                "continue_message": None,
                "evidence": [],
                "session_matches": [],
                "review_receipts": [],
            },
        )
        updated_at = str(source.get("updated_at") or "")
        if _parse_iso(updated_at) > _parse_iso(str(bundle.get("latest_updated_at") or "")):
            bundle["latest_updated_at"] = updated_at
        session_id = source.get("session_id")
        if isinstance(session_id, str) and session_id:
            bundle["thread_ids"].add(session_id)
        thread_label = source.get("thread_label")
        if isinstance(thread_label, str) and thread_label:
            bundle["thread_labels"].add(thread_label)
        artifact_paths = source.get("artifact_paths")
        if isinstance(artifact_paths, list):
            for artifact_path in artifact_paths:
                if isinstance(artifact_path, str) and artifact_path:
                    bundle["artifact_paths"].add(artifact_path)
        if not bundle.get("continue_message") and isinstance(source.get("continue_message"), str):
            bundle["continue_message"] = source.get("continue_message")

        source_kind = str(source.get("source_kind") or "")
        if source_kind == "workflow":
            bundle["workflow_count"] += 1
        elif source_kind == "approval":
            bundle["approval_count"] += 1
        elif source_kind == "audit":
            bundle["audit_event_count"] += 1
        elif source_kind == "session_search":
            bundle["session_match_count"] += 1

        evidence_entry = {
            "source_kind": source_kind,
            "title": source.get("title"),
            "summary": source.get("summary"),
            "status": source.get("status"),
            "thread_id": source.get("session_id"),
            "thread_label": source.get("thread_label"),
            "updated_at": updated_at,
        }
        if source_kind == "session_search":
            if len(bundle["session_matches"]) < limit_session_matches:
                bundle["session_matches"].append(
                    {
                        "session_id": source.get("session_id"),
                        "title": source.get("title"),
                        "matched_at": updated_at,
                        "snippet": source.get("snippet") or source.get("summary"),
                        "source": source.get("status"),
                    }
                )
        else:
            if len(bundle["evidence"]) < 4:
                bundle["evidence"].append(evidence_entry)
        if source_kind == "audit" and len(bundle["review_receipts"]) < 3:
            bundle["review_receipts"].append(evidence_entry)

    finalized: list[dict[str, Any]] = []
    for bundle in bundles.values():
        finalized.append(
            {
                **bundle,
                "thread_ids": sorted(bundle["thread_ids"]),
                "thread_labels": sorted(bundle["thread_labels"]),
                "artifact_paths": sorted(bundle["artifact_paths"]),
            }
        )
    return sorted(finalized, key=_engineering_bundle_priority, reverse=True)[:limit_bundles]


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


def _workflow_terminal_status(status: str) -> bool:
    return status in {"completed", "succeeded", "failed", "cancelled"}


def _workflow_step_focus(run: dict[str, Any]) -> dict[str, Any] | None:
    step_records = run.get("step_records")
    if not isinstance(step_records, list):
        return None
    records = [entry for entry in step_records if isinstance(entry, dict)]
    if not records:
        return None

    def _record_index(record: dict[str, Any]) -> int:
        value = record.get("index")
        return int(value) if isinstance(value, int | float) else -1

    records = sorted(records, key=_record_index)
    active_statuses = {"running", "pending", "awaiting_approval"}
    failure_statuses = {"failed", "degraded"}

    active = next((record for record in reversed(records) if str(record.get("status") or "") in active_statuses), None)
    if active is not None:
        return {
            "kind": "active",
            "step_id": active.get("id"),
            "tool": active.get("tool"),
            "status": active.get("status"),
            "summary": active.get("result_summary"),
            "error_summary": active.get("error_summary"),
            "recovery_hint": active.get("recovery_hint"),
            "recovery_action_count": len(active.get("recovery_actions") or []) if isinstance(active.get("recovery_actions"), list) else 0,
            "is_recoverable": bool(active.get("is_recoverable")),
        }

    failure = next((record for record in reversed(records) if str(record.get("status") or "") in failure_statuses), None)
    if failure is not None:
        return {
            "kind": "failure",
            "step_id": failure.get("id"),
            "tool": failure.get("tool"),
            "status": failure.get("status"),
            "summary": failure.get("result_summary"),
            "error_summary": failure.get("error_summary"),
            "recovery_hint": failure.get("recovery_hint"),
            "recovery_action_count": len(failure.get("recovery_actions") or []) if isinstance(failure.get("recovery_actions"), list) else 0,
            "is_recoverable": bool(failure.get("is_recoverable")),
        }

    latest = records[-1]
    return {
        "kind": "latest",
        "step_id": latest.get("id"),
        "tool": latest.get("tool"),
        "status": latest.get("status"),
        "summary": latest.get("result_summary"),
        "error_summary": latest.get("error_summary"),
        "recovery_hint": latest.get("recovery_hint"),
        "recovery_action_count": len(latest.get("recovery_actions") or []) if isinstance(latest.get("recovery_actions"), list) else 0,
        "is_recoverable": bool(latest.get("is_recoverable")),
    }


def _workflow_orchestration_priority(run: dict[str, Any]) -> tuple[int, datetime]:
    status = str(run.get("status") or "")
    availability = str(run.get("availability") or "")
    pending_approval_count = int(run.get("pending_approval_count") or 0)
    step_focus = _workflow_step_focus(run)
    step_kind = str(step_focus.get("kind") or "") if isinstance(step_focus, dict) else ""

    priority = 50
    if pending_approval_count > 0:
        priority = 100
    elif availability == "blocked":
        priority = 98
    elif status == "awaiting_approval":
        priority = 96
    elif status == "running":
        priority = 94
    elif status in {"failed", "degraded"}:
        priority = 92
    elif step_kind == "active":
        priority = 90
    elif step_kind == "failure":
        priority = 88
    elif run.get("retry_from_step_draft"):
        priority = 86
    elif step_focus and int(step_focus.get("recovery_action_count") or 0) > 0:
        priority = 84
    elif not _workflow_terminal_status(status):
        priority = 80

    return priority, _parse_iso(str(run.get("updated_at") or run.get("started_at") or ""))


def _workflow_orchestration_entries(
    workflow_runs: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    sorted_runs = sorted(
        workflow_runs,
        key=lambda run: _workflow_orchestration_priority(run),
        reverse=True,
    )
    entries: list[dict[str, Any]] = []
    for run in sorted_runs[:limit]:
        step_focus = _workflow_step_focus(run)
        checkpoint_candidates = run.get("checkpoint_candidates")
        output_path = None
        if isinstance(run.get("artifact_paths"), list) and run["artifact_paths"]:
            output_path = run["artifact_paths"][-1]
        entries.append({
            "id": run.get("id"),
            "tool_name": run.get("tool_name"),
            "run_identity": run.get("run_identity"),
            "root_run_identity": run.get("root_run_identity"),
            "parent_run_identity": run.get("parent_run_identity"),
            "workflow_name": run.get("workflow_name"),
            "summary": run.get("summary"),
            "status": run.get("status"),
            "availability": run.get("availability"),
            "session_id": run.get("session_id"),
            "started_at": run.get("started_at"),
            "updated_at": run.get("updated_at"),
            "thread_id": run.get("thread_id"),
            "thread_label": run.get("thread_label"),
            "continue_message": workflow_surface_continue_message(run),
            "thread_continue_message": workflow_surface_continue_message(run),
            "output_path": output_path,
            "artifact_paths": run.get("artifact_paths") if isinstance(run.get("artifact_paths"), list) else [],
            "step_records": run.get("step_records") if isinstance(run.get("step_records"), list) else [],
            "pending_approval_count": int(run.get("pending_approval_count") or 0),
            "pending_approval_ids": run.get("pending_approval_ids") if isinstance(run.get("pending_approval_ids"), list) else [],
            "checkpoint_candidate_count": len(checkpoint_candidates) if isinstance(checkpoint_candidates, list) else 0,
            "checkpoint_candidates": checkpoint_candidates if isinstance(checkpoint_candidates, list) else [],
            "retry_from_step_available": bool(run.get("retry_from_step_draft")),
            "retry_from_step_draft": run.get("retry_from_step_draft"),
            "replay_allowed": run.get("replay_allowed", True),
            "replay_block_reason": run.get("replay_block_reason"),
            "replay_recommended_actions": (
                run.get("replay_recommended_actions")
                if isinstance(run.get("replay_recommended_actions"), list)
                else []
            ),
            "step_focus": step_focus,
        })
    return entries


def _workflow_orchestration_sessions(
    workflow_runs: list[dict[str, Any]],
    *,
    session_titles: dict[str, str],
    limit: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in workflow_runs:
        thread_id = run.get("thread_id") if isinstance(run.get("thread_id"), str) else None
        key = thread_id or "__ambient__"
        grouped.setdefault(key, []).append(run)

    entries: list[dict[str, Any]] = []
    for key, runs in grouped.items():
        runs_sorted = sorted(runs, key=lambda run: _workflow_orchestration_priority(run), reverse=True)
        lead = runs_sorted[0]
        active_workflows = sum(1 for run in runs if not _workflow_terminal_status(str(run.get("status") or "")))
        blocked_workflows = sum(1 for run in runs if str(run.get("availability") or "") == "blocked")
        awaiting_approval_workflows = sum(1 for run in runs if str(run.get("status") or "") == "awaiting_approval")
        recoverable_workflows = sum(
            1
            for run in runs
            if bool(run.get("retry_from_step_draft"))
            or (
                isinstance(_workflow_step_focus(run), dict)
                and int((_workflow_step_focus(run) or {}).get("recovery_action_count") or 0) > 0
            )
        )
        latest_updated_at = max(
            _parse_iso(str(run.get("updated_at") or run.get("started_at") or "")) for run in runs
        ).isoformat()
        thread_id = lead.get("thread_id") if isinstance(lead.get("thread_id"), str) else None
        entries.append({
            "thread_id": thread_id,
            "thread_label": (
                lead.get("thread_label")
                or (session_titles.get(thread_id) if thread_id else None)
                or ("Ambient workflows" if key == "__ambient__" else None)
            ),
            "workflow_count": len(runs),
            "active_workflows": active_workflows,
            "blocked_workflows": blocked_workflows,
            "awaiting_approval_workflows": awaiting_approval_workflows,
            "recoverable_workflows": recoverable_workflows,
            "latest_updated_at": latest_updated_at,
            "lead_run_identity": lead.get("run_identity"),
            "lead_workflow_name": lead.get("workflow_name"),
            "lead_status": lead.get("status"),
            "lead_summary": lead.get("summary"),
            "continue_message": workflow_surface_continue_message(lead),
            "lead_step_focus": _workflow_step_focus(lead),
        })
    return sorted(
        entries,
        key=lambda entry: (
            int(entry["blocked_workflows"]),
            int(entry["awaiting_approval_workflows"]),
            int(entry["active_workflows"]),
            _parse_iso(str(entry["latest_updated_at"])),
        ),
        reverse=True,
    )[:limit]


def _background_session_priority(entry: dict[str, Any]) -> tuple[int, int, int, int, datetime]:
    return (
        int(entry.get("running_background_process_count") or 0),
        int(entry.get("active_workflows") or 0),
        int(entry.get("blocked_workflows") or 0),
        int(entry.get("branch_handoff_available") or 0),
        _parse_iso(str(entry.get("latest_updated_at") or "")),
    )


def _background_process_preview(processes: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for process in processes[:limit]:
        preview.append({
            "process_id": process.get("process_id"),
            "pid": process.get("pid"),
            "command": process.get("command"),
            "args": process.get("args") if isinstance(process.get("args"), list) else [],
            "cwd": process.get("cwd"),
            "status": process.get("status"),
            "exit_code": process.get("exit_code"),
            "started_at": process.get("started_at"),
            "session_id": process.get("session_id"),
        })
    return preview


def _background_handoff_bundle(runs: list[dict[str, Any]]) -> dict[str, Any]:
    branch_candidates = [
        run
        for run in runs
        if isinstance(run, dict)
        and (
            run.get("branch_kind") is not None
            or run.get("parent_run_identity") is not None
            or run.get("retry_from_step_draft")
            or run.get("thread_continue_message")
        )
    ]
    if not branch_candidates:
        return {
            "available": False,
            "target_type": "none",
            "continue_message": None,
            "workflow_name": None,
            "run_identity": None,
            "branch_kind": None,
            "branch_depth": 0,
            "artifact_paths": [],
            "resume_checkpoint_label": None,
            "summary": None,
        }

    selected = sorted(
        branch_candidates,
        key=lambda run: _workflow_orchestration_priority(run),
        reverse=True,
    )[0]
    return {
        "available": True,
        "target_type": (
            "workflow_branch"
            if selected.get("branch_kind") is not None or selected.get("parent_run_identity") is not None
            else "workflow_run"
        ),
        "continue_message": workflow_surface_continue_message(selected),
        "workflow_name": selected.get("workflow_name"),
        "run_identity": selected.get("run_identity"),
        "branch_kind": selected.get("branch_kind"),
        "branch_depth": int(selected.get("branch_depth") or 0),
        "artifact_paths": (
            list(selected.get("artifact_paths"))
            if isinstance(selected.get("artifact_paths"), list)
            else []
        ),
        "resume_checkpoint_label": workflow_surface_resume_metadata(selected).get("resume_checkpoint_label"),
        "summary": selected.get("summary"),
    }


def _background_session_entries(
    sessions: list[dict[str, Any]],
    workflow_runs: list[dict[str, Any]],
    process_payloads: list[dict[str, Any]],
    *,
    limit_sessions: int,
    limit_processes: int,
) -> list[dict[str, Any]]:
    session_index = {
        str(session["id"]): session
        for session in sessions
        if isinstance(session, dict) and isinstance(session.get("id"), str)
    }
    workflow_by_session: dict[str, list[dict[str, Any]]] = {}
    for run in workflow_runs:
        session_id = run.get("thread_id")
        if isinstance(session_id, str):
            workflow_by_session.setdefault(session_id, []).append(run)

    process_by_session: dict[str, list[dict[str, Any]]] = {}
    for process in process_payloads:
        session_id = process.get("session_id")
        if isinstance(session_id, str):
            process_by_session.setdefault(session_id, []).append(process)

    relevant_session_ids = sorted(set(workflow_by_session) | set(process_by_session))
    entries: list[dict[str, Any]] = []
    for session_id in relevant_session_ids:
        runs = workflow_by_session.get(session_id, [])
        processes = process_by_session.get(session_id, [])
        session = session_index.get(session_id, {})
        lead_run = (
            sorted(runs, key=lambda run: _workflow_orchestration_priority(run), reverse=True)[0]
            if runs
            else None
        )
        latest_updated_at = max(
            [
                _parse_iso(str(session.get("updated_at") or "")),
                *[_parse_iso(str(run.get("updated_at") or run.get("started_at") or "")) for run in runs],
                *[_parse_iso(str(process.get("started_at") or "")) for process in processes],
            ]
        ).isoformat()
        branch_handoff = _background_handoff_bundle(runs)
        active_workflows = sum(
            1 for run in runs if not _workflow_terminal_status(str(run.get("status") or ""))
        )
        blocked_workflows = sum(
            1 for run in runs if str(run.get("availability") or "") == "blocked"
        )
        running_background_process_count = sum(
            1 for process in processes if str(process.get("status") or "") == "running"
        )
        lead_process = processes[0] if processes else None
        entries.append({
            "session_id": session_id,
            "title": str(session.get("title") or "Untitled session"),
            "latest_updated_at": latest_updated_at,
            "last_message": session.get("last_message"),
            "workflow_count": len(runs),
            "active_workflows": active_workflows,
            "blocked_workflows": blocked_workflows,
            "background_process_count": len(processes),
            "running_background_process_count": running_background_process_count,
            "lead_workflow_name": lead_run.get("workflow_name") if isinstance(lead_run, dict) else None,
            "lead_workflow_status": lead_run.get("status") if isinstance(lead_run, dict) else None,
            "continue_message": (
                branch_handoff.get("continue_message")
                or (workflow_surface_continue_message(lead_run) if isinstance(lead_run, dict) else None)
            ),
            "branch_handoff_available": bool(branch_handoff.get("available")),
            "branch_handoff": branch_handoff,
            "lead_process": (
                {
                    "process_id": lead_process.get("process_id"),
                    "pid": lead_process.get("pid"),
                    "command": lead_process.get("command"),
                    "args": lead_process.get("args") if isinstance(lead_process.get("args"), list) else [],
                    "cwd": lead_process.get("cwd"),
                    "status": lead_process.get("status"),
                    "started_at": lead_process.get("started_at"),
                }
                if isinstance(lead_process, dict)
                else None
            ),
            "background_processes": _background_process_preview(processes, limit=limit_processes),
        })

    return sorted(entries, key=_background_session_priority, reverse=True)[:limit_sessions]


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


@router.get("/operator/workflow-orchestration")
async def get_operator_workflow_orchestration(
    limit_sessions: int = Query(default=6, ge=1, le=12),
    limit_workflows: int = Query(default=8, ge=1, le=20),
):
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions()
        if isinstance(session, dict) and session.get("id")
    }
    workflow_runs = await _list_workflow_runs(limit=max(limit_workflows * 6, 60), session_id=None)
    active_workflows = sum(
        1 for run in workflow_runs if not _workflow_terminal_status(str(run.get("status") or ""))
    )
    blocked_workflows = sum(
        1 for run in workflow_runs if str(run.get("availability") or "") == "blocked"
    )
    awaiting_approval_workflows = sum(
        1 for run in workflow_runs if str(run.get("status") or "") == "awaiting_approval"
    )
    recoverable_workflows = sum(
        1
        for run in workflow_runs
        if bool(run.get("retry_from_step_draft"))
        or (
            isinstance(_workflow_step_focus(run), dict)
            and int((_workflow_step_focus(run) or {}).get("recovery_action_count") or 0) > 0
        )
    )
    session_ids = {
        str(run.get("thread_id"))
        for run in workflow_runs
        if isinstance(run.get("thread_id"), str)
    }
    return {
        "summary": {
            "tracked_sessions": len(session_ids),
            "workflow_count": len(workflow_runs),
            "active_workflows": active_workflows,
            "blocked_workflows": blocked_workflows,
            "awaiting_approval_workflows": awaiting_approval_workflows,
            "recoverable_workflows": recoverable_workflows,
        },
        "sessions": _workflow_orchestration_sessions(
            workflow_runs,
            session_titles=session_titles,
            limit=limit_sessions,
        ),
        "workflows": _workflow_orchestration_entries(
            workflow_runs,
            limit=limit_workflows,
        ),
    }


@router.get("/operator/background-sessions")
async def get_operator_background_sessions(
    limit_sessions: int = Query(default=6, ge=1, le=20),
    limit_processes: int = Query(default=3, ge=1, le=10),
):
    sessions, workflow_runs = await asyncio.gather(
        session_manager.list_sessions(),
        _list_workflow_runs(limit=max(limit_sessions * 8, 60), session_id=None),
    )
    process_payloads = await asyncio.to_thread(process_runtime_manager.list_all_processes)
    background_sessions = _background_session_entries(
        sessions,
        workflow_runs,
        process_payloads if isinstance(process_payloads, list) else [],
        limit_sessions=limit_sessions,
        limit_processes=limit_processes,
    )
    return {
        "summary": {
            "tracked_sessions": len(background_sessions),
            "background_process_count": len(process_payloads) if isinstance(process_payloads, list) else 0,
            "running_background_process_count": sum(
                1
                for process in (process_payloads if isinstance(process_payloads, list) else [])
                if str(process.get("status") or "") == "running"
            ),
            "sessions_with_branch_handoff": sum(
                1 for entry in background_sessions if bool(entry.get("branch_handoff_available"))
            ),
            "sessions_with_active_workflows": sum(
                1 for entry in background_sessions if int(entry.get("active_workflows") or 0) > 0
            ),
        },
        "sessions": background_sessions,
    }


@router.get("/operator/engineering-memory")
async def get_operator_engineering_memory(
    q: str | None = Query(default=None),
    limit_bundles: int = Query(default=6, ge=1, le=20),
    limit_session_matches: int = Query(default=3, ge=1, le=10),
    window_hours: int = Query(default=168, ge=1, le=720),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    normalized_query = " ".join(str(q or "").split()).strip()
    workflow_runs, pending_approvals, audit_events, session_search_matches = await asyncio.gather(
        _list_workflow_runs(limit=max(limit_bundles * 8, 80), session_id=None),
        approval_repository.list_pending(session_id=None, limit=max(limit_bundles * 4, 40)),
        audit_repository.list_events(limit=max(limit_bundles * 10, 120), session_id=None, since=cutoff),
        session_manager.search_sessions(
            normalized_query,
            limit=max(limit_session_matches * 3, 8),
        )
        if normalized_query
        else asyncio.sleep(0, result=[]),
    )
    bundles = _build_engineering_memory_bundles(
        workflow_runs,
        pending_approvals,
        audit_events,
        session_search_matches if isinstance(session_search_matches, list) else [],
        normalized_query=_engineering_query(normalized_query),
        limit_bundles=limit_bundles,
        limit_session_matches=limit_session_matches,
    )
    return {
        "summary": {
            "query": normalized_query or None,
            "tracked_bundles": len(bundles),
            "repository_bundle_count": sum(
                1 for bundle in bundles if str(bundle.get("target_kind") or "") == "repository"
            ),
            "pull_request_bundle_count": sum(
                1 for bundle in bundles if str(bundle.get("target_kind") or "") == "pull_request"
            ),
            "work_item_bundle_count": sum(
                1 for bundle in bundles if str(bundle.get("target_kind") or "") == "work_item"
            ),
            "search_match_count": len(session_search_matches)
            if isinstance(session_search_matches, list)
            else 0,
        },
        "search_matches": (
            session_search_matches[:limit_session_matches]
            if isinstance(session_search_matches, list)
            else []
        ),
        "bundles": bundles,
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
                "selected_preference_score": details.get("selected_preference_score"),
                "selected_capability_gap_count": details.get("selected_capability_gap_count"),
                "selected_live_feedback_penalty": details.get("selected_live_feedback_penalty"),
                "selection_policy_mode": details.get("selection_policy_mode"),
                "planning_winner_model": details.get("planning_winner_model"),
                "planning_winner_profile": details.get("planning_winner_profile"),
                "planning_winner_source": details.get("planning_winner_source"),
                "planning_winner_selected": details.get("planning_winner_selected"),
                "best_alternate_model": details.get("best_alternate_model"),
                "best_alternate_profile": details.get("best_alternate_profile"),
                "best_alternate_source": details.get("best_alternate_source"),
                "best_alternate_route_score": details.get("best_alternate_route_score"),
                "selected_vs_best_alternate_margin": details.get("selected_vs_best_alternate_margin"),
                "attempt_order": details.get("attempt_order", []),
                "reroute_cause": details.get("reroute_cause"),
                "rerouted_from_unhealthy_primary": details.get("rerouted_from_unhealthy_primary"),
                "rerouted_from_policy_guardrails": details.get("rerouted_from_policy_guardrails"),
                "guardrail_compliant_targets_present": details.get("guardrail_compliant_targets_present"),
                "route_explanation": details.get("route_explanation"),
                "route_comparison_summary": details.get("route_comparison_summary"),
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
