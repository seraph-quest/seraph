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
from src.evals.benchmark_catalog import benchmark_suite_report
from src.evolution.engine import evolution_benchmark_gate_policy, list_evolution_targets
from src.guardian.benchmark import build_guardian_user_model_benchmark_report
from src.guardian.feedback import guardian_feedback_repository
from src.guardian.state import build_guardian_state
from src.memory.benchmark import build_guardian_memory_benchmark_report
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


def _within_operator_window(value: str | datetime | None, cutoff: datetime) -> bool:
    return _parse_iso(value) >= cutoff


def _build_engineering_memory_bundles(
    workflow_runs: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    audit_events: list[dict[str, Any]],
    session_search_matches: list[dict[str, Any]],
    *,
    normalized_query: str,
    limit_bundles: int | None,
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
    finalized_sorted = sorted(finalized, key=_engineering_bundle_priority, reverse=True)
    if isinstance(limit_bundles, int):
        return finalized_sorted[:limit_bundles]
    return finalized_sorted


def _continuity_graph_session_key(session_id: str | None) -> str:
    return session_id if isinstance(session_id, str) and session_id else "__ambient__"


def _continuity_graph_session_node_id(session_key: str) -> str:
    return "session:ambient" if session_key == "__ambient__" else f"session:{session_key}"


def _continuity_graph_kind_priority(kind: str) -> int:
    priorities = {
        "session": 7,
        "ambient_session": 6,
        "workflow_run": 5,
        "approval": 4,
        "notification": 3,
        "queued_insight": 2,
        "intervention": 1,
        "artifact": 0,
    }
    return priorities.get(kind, -1)


def _continuity_graph_node_priority(node: dict[str, Any]) -> tuple[int, datetime, str]:
    return (
        _continuity_graph_kind_priority(str(node.get("kind") or "")),
        _parse_iso(str(node.get("updated_at") or "")),
        str(node.get("id") or ""),
    )


def _continuity_graph_session_priority(session: dict[str, Any]) -> tuple[int, int, datetime]:
    return (
        int(session.get("linked_item_count") or 0),
        int(session.get("artifact_count") or 0),
        _parse_iso(str(session.get("updated_at") or "")),
    )


def _continuity_graph_edge(
    source_id: str,
    target_id: str,
    *,
    kind: str,
    session_key: str,
    updated_at: str,
    label: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"{kind}:{source_id}->{target_id}",
        "source_id": source_id,
        "target_id": target_id,
        "kind": kind,
        "label": label or kind.replace("_", " "),
        "updated_at": updated_at,
        "metadata": metadata or {},
        "_session_key": session_key,
    }


def _build_operator_continuity_graph(
    sessions: list[dict[str, Any]],
    workflow_runs: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    notifications: list[Any],
    queued_insights: list[Any],
    recent_interventions: list[Any],
    continuity_snapshot: dict[str, Any],
    *,
    session_id: str | None,
    limit_sessions: int,
) -> dict[str, Any]:
    session_index = {
        str(item["id"]): item
        for item in sessions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    snapshot_threads = {
        _continuity_graph_session_key(
            item.get("thread_id") if isinstance(item, dict) else None
        ): item
        for item in (continuity_snapshot.get("threads") if isinstance(continuity_snapshot, dict) else [])
        if isinstance(item, dict)
    }
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    edge_ids: set[str] = set()
    session_members: dict[str, set[str]] = {}
    session_artifacts: dict[str, set[str]] = {}
    intervention_sessions: dict[str, str] = {}
    selected_session_id = session_id if isinstance(session_id, str) and session_id else None
    intervention_source_sessions = {
        str(getattr(intervention, "id", "") or ""): str(getattr(intervention, "session_id", "") or "")
        for intervention in recent_interventions
        if isinstance(getattr(intervention, "id", None), str) and isinstance(getattr(intervention, "session_id", None), str)
    }

    def ensure_session_node(session_key: str) -> dict[str, Any]:
        node_id = _continuity_graph_session_node_id(session_key)
        if node_id in nodes:
            return nodes[node_id]
        session_payload = session_index.get(session_key, {}) if session_key != "__ambient__" else {}
        thread_payload = snapshot_threads.get(session_key, {})
        title = (
            str(session_payload.get("title") or "")
            or str(thread_payload.get("thread_label") or "")
            or ("Ambient continuity" if session_key == "__ambient__" else "Untitled session")
        )
        summary = (
            session_payload.get("last_message")
            or thread_payload.get("summary")
            or thread_payload.get("detail")
        )
        updated_at = max(
            _parse_iso(str(session_payload.get("updated_at") or "")),
            _parse_iso(str(thread_payload.get("latest_updated_at") or "")),
        ).isoformat()
        node = {
            "id": node_id,
            "kind": "ambient_session" if session_key == "__ambient__" else "session",
            "title": title,
            "summary": summary,
            "updated_at": updated_at,
            "thread_id": None if session_key == "__ambient__" else session_key,
            "continue_message": thread_payload.get("continue_message"),
            "metadata": {
                "pending_notification_count": int(thread_payload.get("pending_notification_count") or 0),
                "queued_insight_count": int(thread_payload.get("queued_insight_count") or 0),
                "recent_intervention_count": int(thread_payload.get("recent_intervention_count") or 0),
                "item_count": int(thread_payload.get("item_count") or 0),
                "primary_surface": thread_payload.get("primary_surface"),
                "continuity_surface": thread_payload.get("continuity_surface"),
            },
        }
        nodes[node_id] = node
        session_members.setdefault(session_key, set())
        session_artifacts.setdefault(session_key, set())
        return node

    def add_node(node: dict[str, Any], *, session_key: str | None = None) -> dict[str, Any]:
        existing = nodes.get(str(node["id"]))
        if existing is None:
            nodes[str(node["id"])] = node
        else:
            if _parse_iso(str(node.get("updated_at") or "")) > _parse_iso(str(existing.get("updated_at") or "")):
                existing["updated_at"] = node.get("updated_at")
            if not existing.get("summary") and node.get("summary"):
                existing["summary"] = node.get("summary")
            if not existing.get("continue_message") and node.get("continue_message"):
                existing["continue_message"] = node.get("continue_message")
        if session_key is not None:
            ensure_session_node(session_key)
            session_members.setdefault(session_key, set()).add(str(node["id"]))
        return nodes[str(node["id"])]

    def add_edge(edge: dict[str, Any]) -> None:
        edge_id = str(edge["id"])
        if edge_id in edge_ids:
            return
        edge_ids.add(edge_id)
        edges.append(edge)

    def ensure_intervention_reference_node(
        intervention_ref: str,
        *,
        source_node: dict[str, Any],
    ) -> str:
        target_id = f"intervention:{intervention_ref}"
        if target_id in nodes:
            return target_id
        add_node(
            {
                "id": target_id,
                "kind": "intervention",
                "title": str(source_node.get("title") or "guardian intervention"),
                "summary": str(source_node.get("summary") or ""),
                "updated_at": str(source_node.get("updated_at") or ""),
                "thread_id": source_node.get("thread_id"),
                "continue_message": source_node.get("continue_message"),
                "metadata": {
                    "latest_outcome": None,
                    "transport": None,
                    "policy_action": None,
                    "missing_recent_context": True,
                    "inferred_from": source_node.get("kind"),
                },
            }
        )
        return target_id

    def should_include_source(source_session_id: str | None) -> bool:
        if selected_session_id is None:
            return True
        return source_session_id == selected_session_id

    for run in workflow_runs:
        source_session_id = run.get("thread_id") if isinstance(run.get("thread_id"), str) else None
        if not should_include_source(source_session_id):
            continue
        session_key = _continuity_graph_session_key(source_session_id)
        workflow_id = f"workflow:{run.get('id')}"
        workflow_node = add_node(
            {
                "id": workflow_id,
                "kind": "workflow_run",
                "title": str(run.get("workflow_name") or "workflow"),
                "summary": str(run.get("summary") or ""),
                "updated_at": str(run.get("updated_at") or run.get("started_at") or ""),
                "thread_id": source_session_id,
                "continue_message": workflow_surface_continue_message(run),
                "metadata": {
                    "status": run.get("status"),
                    "run_identity": run.get("run_identity"),
                    "branch_kind": run.get("branch_kind"),
                    "availability": run.get("availability"),
                },
            },
            session_key=session_key,
        )
        add_edge(
            _continuity_graph_edge(
                _continuity_graph_session_node_id(session_key),
                workflow_id,
                kind="session_workflow",
                label="session runs workflow",
                session_key=session_key,
                updated_at=str(workflow_node.get("updated_at") or ""),
            )
        )
        for artifact_path in (
            list(run.get("artifact_paths"))
            if isinstance(run.get("artifact_paths"), list)
            else []
        ):
            if not isinstance(artifact_path, str) or not artifact_path:
                continue
            artifact_id = f"artifact:{artifact_path}"
            add_node(
                {
                    "id": artifact_id,
                    "kind": "artifact",
                    "title": artifact_path,
                    "summary": f"Artifact from {run.get('workflow_name') or 'workflow'}",
                    "updated_at": str(run.get("updated_at") or run.get("started_at") or ""),
                    "thread_id": source_session_id,
                    "continue_message": None,
                    "metadata": {
                        "path": artifact_path,
                    },
                },
                session_key=session_key,
            )
            session_artifacts.setdefault(session_key, set()).add(artifact_path)
            add_edge(
                _continuity_graph_edge(
                    workflow_id,
                    artifact_id,
                    kind="workflow_artifact",
                    label="workflow produced artifact",
                    session_key=session_key,
                    updated_at=str(run.get("updated_at") or run.get("started_at") or ""),
                    metadata={"path": artifact_path},
                )
            )

    for approval in pending_approvals:
        source_session_id = approval.get("thread_id") or approval.get("session_id")
        source_session_id = source_session_id if isinstance(source_session_id, str) else None
        if not should_include_source(source_session_id):
            continue
        session_key = _continuity_graph_session_key(source_session_id)
        approval_id = f"approval:{approval.get('id')}"
        approval_metadata = approval_surface_metadata(approval)
        add_node(
            {
                "id": approval_id,
                "kind": "approval",
                "title": str(approval.get("tool_name") or "approval"),
                "summary": str(approval.get("summary") or "Approval pending"),
                "updated_at": str(approval.get("created_at") or ""),
                "thread_id": source_session_id,
                "continue_message": approval.get("resume_message"),
                "metadata": {
                    "risk_level": approval.get("risk_level"),
                    "approval_scope": approval_metadata.get("approval_scope"),
                },
            },
            session_key=session_key,
        )
        add_edge(
            _continuity_graph_edge(
                _continuity_graph_session_node_id(session_key),
                approval_id,
                kind="session_approval",
                label="session awaits approval",
                session_key=session_key,
                updated_at=str(approval.get("created_at") or ""),
            )
        )

    for notification in notifications:
        payload = notification if isinstance(notification, dict) else getattr(notification, "__dict__", {})
        source_session_id = (
            payload.get("thread_id")
            or payload.get("session_id")
            or intervention_source_sessions.get(str(payload.get("intervention_id") or ""))
        )
        source_session_id = source_session_id if isinstance(source_session_id, str) else None
        if not should_include_source(source_session_id):
            continue
        session_key = _continuity_graph_session_key(source_session_id)
        notification_id = f"notification:{payload.get('id')}"
        add_node(
            {
                "id": notification_id,
                "kind": "notification",
                "title": str(payload.get("title") or "notification"),
                "summary": str(payload.get("body") or ""),
                "updated_at": str(payload.get("created_at") or ""),
                "thread_id": source_session_id,
                "continue_message": payload.get("resume_message") or payload.get("body"),
                "metadata": {
                    "intervention_id": payload.get("intervention_id"),
                    "continuation_mode": payload.get("continuation_mode"),
                    "thread_source": payload.get("thread_source"),
                },
            },
            session_key=session_key,
        )
        add_edge(
            _continuity_graph_edge(
                _continuity_graph_session_node_id(session_key),
                notification_id,
                kind="session_notification",
                label="session has notification",
                session_key=session_key,
                updated_at=str(payload.get("created_at") or ""),
            )
        )

    for insight in queued_insights:
        payload = insight if isinstance(insight, dict) else getattr(insight, "__dict__", {})
        source_session_id = payload.get("session_id") or intervention_source_sessions.get(
            str(payload.get("intervention_id") or "")
        )
        source_session_id = source_session_id if isinstance(source_session_id, str) else None
        if not should_include_source(source_session_id):
            continue
        session_key = _continuity_graph_session_key(source_session_id)
        insight_id = f"queued:{payload.get('id')}"
        add_node(
            {
                "id": insight_id,
                "kind": "queued_insight",
                "title": str(payload.get("intervention_type") or "queued insight"),
                "summary": str(payload.get("content") or ""),
                "updated_at": str(payload.get("created_at") or ""),
                "thread_id": source_session_id,
                "continue_message": (
                    f"Follow up on this deferred guardian item: {payload.get('content')}"
                    if payload.get("content")
                    else None
                ),
                "metadata": {
                    "intervention_id": payload.get("intervention_id"),
                    "reasoning": payload.get("reasoning"),
                },
            },
            session_key=session_key,
        )
        add_edge(
            _continuity_graph_edge(
                _continuity_graph_session_node_id(session_key),
                insight_id,
                kind="session_queued_insight",
                label="session has deferred guardian item",
                session_key=session_key,
                updated_at=str(payload.get("created_at") or ""),
            )
        )

    for intervention in recent_interventions:
        source_session_id = getattr(intervention, "session_id", None)
        source_session_id = source_session_id if isinstance(source_session_id, str) else None
        if not should_include_source(source_session_id):
            continue
        session_key = _continuity_graph_session_key(source_session_id)
        intervention_ref = str(getattr(intervention, "id", "") or "")
        intervention_id = f"intervention:{intervention_ref}"
        intervention_sessions[intervention_ref] = session_key
        add_node(
            {
                "id": intervention_id,
                "kind": "intervention",
                "title": str(getattr(intervention, "intervention_type", "intervention")),
                "summary": str(getattr(intervention, "content_excerpt", "") or getattr(intervention, "content", "")),
                "updated_at": _timeline_timestamp(getattr(intervention, "updated_at", None)),
                "thread_id": source_session_id,
                "continue_message": (
                    f"Continue from this guardian intervention: {getattr(intervention, 'content_excerpt', '')}"
                    if getattr(intervention, "content_excerpt", None)
                    else None
                ),
                "metadata": {
                    "latest_outcome": getattr(intervention, "latest_outcome", None),
                    "transport": getattr(intervention, "transport", None),
                    "policy_action": getattr(intervention, "policy_action", None),
                },
            },
            session_key=session_key,
        )
        add_edge(
            _continuity_graph_edge(
                _continuity_graph_session_node_id(session_key),
                intervention_id,
                kind="session_intervention",
                label="session has guardian intervention",
                session_key=session_key,
                updated_at=_timeline_timestamp(getattr(intervention, "updated_at", None)),
            )
        )

    for node in list(nodes.values()):
        if str(node.get("kind") or "") == "notification":
            intervention_ref = str(node.get("metadata", {}).get("intervention_id") or "")
            if intervention_ref:
                target_id = ensure_intervention_reference_node(intervention_ref, source_node=node)
                add_edge(
                    _continuity_graph_edge(
                        str(node["id"]),
                        target_id,
                        kind="notification_intervention",
                        label="notification delivers guardian intervention",
                        session_key=_continuity_graph_session_key(node.get("thread_id")),
                        updated_at=str(node.get("updated_at") or ""),
                    )
                )
        if str(node.get("kind") or "") == "queued_insight":
            intervention_ref = str(node.get("metadata", {}).get("intervention_id") or "")
            if intervention_ref:
                target_id = ensure_intervention_reference_node(intervention_ref, source_node=node)
                add_edge(
                    _continuity_graph_edge(
                        str(node["id"]),
                        target_id,
                        kind="queued_intervention",
                        label="deferred guardian item references intervention",
                        session_key=_continuity_graph_session_key(node.get("thread_id")),
                        updated_at=str(node.get("updated_at") or ""),
                    )
                )

    session_nodes: list[dict[str, Any]] = []
    for session_key, member_ids in session_members.items():
        session_node = ensure_session_node(session_key)
        counts: dict[str, int] = {}
        for member_id in member_ids:
            kind = str(nodes.get(member_id, {}).get("kind") or "")
            counts[kind] = counts.get(kind, 0) + 1
        metadata = dict(session_node.get("metadata") or {})
        metadata.update({
            "workflow_count": counts.get("workflow_run", 0),
            "approval_count": counts.get("approval", 0),
            "notification_count": counts.get("notification", 0),
            "queued_insight_count": counts.get("queued_insight", metadata.get("queued_insight_count", 0)),
            "intervention_count": counts.get("intervention", 0),
            "artifact_count": len(session_artifacts.get(session_key, set())),
            "linked_item_count": sum(counts.values()),
        })
        session_node["metadata"] = metadata
        session_node["linked_item_count"] = metadata["linked_item_count"]
        session_node["artifact_count"] = metadata["artifact_count"]
        session_nodes.append(session_node)

    selected_session_keys = [
        "__ambient__" if str(item.get("id")) == "session:ambient" else str(item.get("thread_id") or "")
        for item in sorted(session_nodes, key=_continuity_graph_session_priority, reverse=True)[:limit_sessions]
        if str(item.get("id") or "")
    ]
    selected_session_keys = [item for item in selected_session_keys if item]
    selected_session_set = set(selected_session_keys)

    filtered_edges = [
        {
            key: value
            for key, value in edge.items()
            if key != "_session_key"
        }
        for edge in sorted(
            (item for item in edges if str(item.get("_session_key") or "") in selected_session_set),
            key=lambda item: (
                _parse_iso(str(item.get("updated_at") or "")),
                str(item.get("id") or ""),
            ),
            reverse=True,
        )
    ]
    included_node_ids = {
        node_id
        for edge in filtered_edges
        for node_id in (str(edge.get("source_id") or ""), str(edge.get("target_id") or ""))
        if node_id
    }
    for session_key in selected_session_keys:
        included_node_ids.add(_continuity_graph_session_node_id(session_key))

    filtered_nodes = sorted(
        [node for node_id, node in nodes.items() if node_id in included_node_ids],
        key=_continuity_graph_node_priority,
        reverse=True,
    )
    filtered_sessions = [
        node
        for node in filtered_nodes
        if str(node.get("kind") or "") in {"session", "ambient_session"}
    ]

    summary = continuity_snapshot.get("summary") if isinstance(continuity_snapshot.get("summary"), dict) else {}
    return {
        "summary": {
            "continuity_health": summary.get("continuity_health"),
            "primary_surface": summary.get("primary_surface"),
            "recommended_focus": summary.get("recommended_focus"),
            "tracked_sessions": len(filtered_sessions),
            "workflow_count": sum(1 for node in filtered_nodes if str(node.get("kind") or "") == "workflow_run"),
            "approval_count": sum(1 for node in filtered_nodes if str(node.get("kind") or "") == "approval"),
            "notification_count": sum(1 for node in filtered_nodes if str(node.get("kind") or "") == "notification"),
            "queued_insight_count": sum(
                1 for node in filtered_nodes if str(node.get("kind") or "") == "queued_insight"
            ),
            "intervention_count": sum(
                1 for node in filtered_nodes if str(node.get("kind") or "") == "intervention"
            ),
            "artifact_count": sum(1 for node in filtered_nodes if str(node.get("kind") or "") == "artifact"),
            "edge_count": len(filtered_edges),
        },
        "sessions": filtered_sessions,
        "nodes": filtered_nodes,
        "edges": filtered_edges,
    }


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
        "version": "2026.4.10",
        "build_id": "SERAPH_PRIME_v2026.4.10",
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


def _workflow_step_records(run: dict[str, Any]) -> list[dict[str, Any]]:
    step_records = run.get("step_records")
    if not isinstance(step_records, list):
        return []
    records = [entry for entry in step_records if isinstance(entry, dict)]
    if not records:
        return []

    def _record_index(record: dict[str, Any]) -> int:
        value = record.get("index")
        return int(value) if isinstance(value, int | float) else -1

    return sorted(records, key=_record_index)


def _workflow_artifact_paths(run: dict[str, Any]) -> list[str]:
    artifact_paths = run.get("artifact_paths")
    if not isinstance(artifact_paths, list):
        return []
    return [path for path in artifact_paths if isinstance(path, str) and path]


def _workflow_visible_artifact_paths(
    artifact_paths: list[str],
    *,
    is_compacted: bool,
    preview_artifacts: int = 1,
) -> list[str]:
    if not is_compacted:
        return list(artifact_paths)
    if preview_artifacts <= 0:
        return []
    return artifact_paths[-preview_artifacts:]


def _workflow_preserved_recovery_paths(run: dict[str, Any], *, step_records: list[dict[str, Any]]) -> list[str]:
    preserved: list[str] = []
    if bool(run.get("retry_from_step_draft")):
        preserved.append("retry_from_step")
    checkpoint_candidates = run.get("checkpoint_candidates")
    if isinstance(checkpoint_candidates, list) and checkpoint_candidates:
        preserved.append("checkpoint_branch")
    if (
        int(run.get("pending_approval_count") or 0) > 0
        or str(run.get("status") or "") == "awaiting_approval"
    ):
        preserved.append("approval_gate")
    if any(
        bool(record.get("is_recoverable"))
        or (
            isinstance(record.get("recovery_actions"), list)
            and len(record.get("recovery_actions") or []) > 0
        )
        for record in step_records
    ):
        preserved.append("step_repair")
    if isinstance(run.get("replay_block_reason"), str) and run.get("replay_block_reason"):
        preserved.append("boundary_receipt")
    return preserved


def _workflow_state_compaction(run: dict[str, Any], *, preview_steps: int = 3) -> dict[str, Any]:
    step_records = _workflow_step_records(run)
    artifact_paths = _workflow_artifact_paths(run)
    total_step_count = len(step_records)
    visible_steps = step_records[-preview_steps:] if preview_steps > 0 else []
    compacted_step_count = max(total_step_count - len(visible_steps), 0)
    preserved_recovery_paths = _workflow_preserved_recovery_paths(run, step_records=step_records)
    started_at = _parse_iso(str(run.get("started_at") or ""))
    updated_at = _parse_iso(str(run.get("updated_at") or run.get("started_at") or ""))
    duration_minutes = max(int((updated_at - started_at).total_seconds() // 60), 0)
    is_long_running = total_step_count >= 4 or duration_minutes >= 60
    is_compacted = compacted_step_count > 0
    recent_step_labels = [
        " / ".join(
            value
            for value in [
                str(record.get("id") or "").strip() or None,
                str(record.get("tool") or "").strip() or None,
                str(record.get("status") or "").strip() or None,
            ]
            if value
        )
        for record in visible_steps
    ]
    capsule_parts = [
        f"{total_step_count} steps" if total_step_count else "0 steps",
        f"{compacted_step_count} compacted" if is_compacted else None,
        (
            f"{len(artifact_paths)} artifact"
            if len(artifact_paths) == 1
            else f"{len(artifact_paths)} artifacts"
        ) if artifact_paths else None,
        (
            "preserves " + ", ".join(path.replace("_", " ") for path in preserved_recovery_paths[:3])
            if preserved_recovery_paths
            else None
        ),
    ]
    return {
        "is_long_running": is_long_running,
        "is_compacted": is_compacted,
        "duration_minutes": duration_minutes,
        "total_step_count": total_step_count,
        "visible_step_count": len(visible_steps),
        "compacted_step_count": compacted_step_count,
        "artifact_count": len(artifact_paths),
        "preserved_recovery_paths": preserved_recovery_paths,
        "recent_step_labels": recent_step_labels,
        "state_capsule": " · ".join(part for part in capsule_parts if part),
    }


def _workflow_latest_failure_record(run: dict[str, Any]) -> dict[str, Any] | None:
    failure_statuses = {"failed", "degraded", "continued_error"}
    for record in reversed(_workflow_step_records(run)):
        if str(record.get("status") or "") in failure_statuses:
            return record
    return None


def _workflow_stalled(run: dict[str, Any], *, stalled_minutes: int = 90) -> bool:
    status = str(run.get("status") or "")
    if _workflow_terminal_status(status):
        return False
    updated_at = _parse_iso(str(run.get("updated_at") or run.get("started_at") or ""))
    age_minutes = max(int((datetime.now(timezone.utc) - updated_at).total_seconds() // 60), 0)
    return age_minutes >= stalled_minutes


def _workflow_family_runs(
    run: dict[str, Any],
    workflow_runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    root_identity = run.get("root_run_identity")
    run_identity = run.get("run_identity")
    thread_id = run.get("thread_id")
    workflow_name = run.get("workflow_name")

    if isinstance(root_identity, str) and root_identity:
        return [
            candidate
            for candidate in workflow_runs
            if isinstance(candidate, dict)
            and (
                candidate.get("root_run_identity") == root_identity
                or candidate.get("run_identity") == root_identity
            )
        ]
    if isinstance(run_identity, str) and run_identity:
        return [
            candidate
            for candidate in workflow_runs
            if isinstance(candidate, dict)
            and (
                candidate.get("run_identity") == run_identity
                or candidate.get("parent_run_identity") == run_identity
                or candidate.get("root_run_identity") == run_identity
            )
        ]
    if isinstance(thread_id, str) and thread_id and isinstance(workflow_name, str) and workflow_name:
        return [
            candidate
            for candidate in workflow_runs
            if isinstance(candidate, dict)
            and candidate.get("thread_id") == thread_id
            and candidate.get("workflow_name") == workflow_name
        ]
    return [run]


def _workflow_output_debugger(
    run: dict[str, Any],
    workflow_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    family_runs = sorted(
        _workflow_family_runs(run, workflow_runs),
        key=_workflow_orchestration_priority,
        reverse=True,
    )
    self_key = run.get("run_identity") or run.get("id")
    primary_output_path = (_workflow_artifact_paths(run) or [None])[-1]
    checkpoint_labels = [
        str(item.get("label") or item.get("step_id") or "").strip()
        for item in (run.get("checkpoint_candidates") if isinstance(run.get("checkpoint_candidates"), list) else [])
        if isinstance(item, dict) and str(item.get("label") or item.get("step_id") or "").strip()
    ]
    related_output_entries: list[tuple[datetime, dict[str, Any]]] = []
    branch_runs: list[dict[str, Any]] = []
    for candidate in family_runs:
        candidate_key = candidate.get("run_identity") or candidate.get("id")
        if candidate_key != self_key and (
            candidate.get("branch_kind") is not None or candidate.get("parent_run_identity") is not None
        ):
            branch_runs.append(candidate)
        updated_at = _parse_iso(str(candidate.get("updated_at") or candidate.get("started_at") or ""))
        for path in _workflow_artifact_paths(candidate):
            related_output_entries.append((
                updated_at,
                {
                    "path": path,
                    "run_identity": candidate.get("run_identity"),
                    "summary": candidate.get("summary"),
                    "status": candidate.get("status"),
                    "branch_kind": candidate.get("branch_kind"),
                    "updated_at": updated_at.isoformat(),
                    "is_primary": candidate_key == self_key,
                },
            ))
    history_outputs: list[dict[str, Any]] = []
    seen_output_paths: set[str] = set()
    for _, entry in sorted(related_output_entries, key=lambda item: item[0], reverse=True):
        path = str(entry.get("path") or "").strip()
        if not path or path in seen_output_paths:
            continue
        seen_output_paths.add(path)
        history_outputs.append(entry)
    related_output_paths = [
        str(entry.get("path") or "")
        for entry in history_outputs
        if str(entry.get("path") or "") != primary_output_path
    ][:3]
    branch_runs = sorted(
        branch_runs,
        key=lambda candidate: (
            _parse_iso(str(candidate.get("updated_at") or candidate.get("started_at") or "")),
            _workflow_orchestration_priority(candidate),
        ),
        reverse=True,
    )
    latest_branch = branch_runs[0] if branch_runs else None
    latest_branch_output_path = (
        (_workflow_artifact_paths(latest_branch) or [None])[-1]
        if isinstance(latest_branch, dict)
        else None
    )
    return {
        "family_run_count": len(family_runs),
        "branch_run_count": len(branch_runs),
        "history_output_count": len(history_outputs),
        "primary_output_path": primary_output_path,
        "related_output_paths": related_output_paths,
        "history_outputs": history_outputs[:4],
        "latest_branch_run_identity": latest_branch.get("run_identity") if isinstance(latest_branch, dict) else None,
        "latest_branch_summary": latest_branch.get("summary") if isinstance(latest_branch, dict) else None,
        "latest_branch_status": latest_branch.get("status") if isinstance(latest_branch, dict) else None,
        "latest_branch_output_path": latest_branch_output_path,
        "comparison_ready": bool(
            primary_output_path
            and latest_branch_output_path
            and latest_branch_output_path != primary_output_path
        ),
        "checkpoint_labels": checkpoint_labels,
    }


def _workflow_recovery_density(run: dict[str, Any]) -> dict[str, Any]:
    checkpoint_candidates = run.get("checkpoint_candidates")
    latest_failure = _workflow_latest_failure_record(run)
    replay_actions = (
        run.get("replay_recommended_actions")
        if isinstance(run.get("replay_recommended_actions"), list)
        else []
    )
    repair_actions = (
        latest_failure.get("recovery_actions")
        if isinstance(latest_failure, dict) and isinstance(latest_failure.get("recovery_actions"), list)
        else []
    )
    repair_action_types = [
        str(action.get("type") or "").strip()
        for action in repair_actions
        if isinstance(action, dict) and str(action.get("type") or "").strip()
    ]
    approval_pending = (
        int(run.get("pending_approval_count") or 0) > 0
        or str(run.get("status") or "") == "awaiting_approval"
    )
    boundary_blocked = isinstance(run.get("replay_block_reason"), str) and bool(run.get("replay_block_reason"))
    retry_ready = bool(run.get("retry_from_step_draft"))
    checkpoint_ready = isinstance(checkpoint_candidates, list) and len(checkpoint_candidates) > 0
    repair_ready = bool(repair_actions) or bool(latest_failure and latest_failure.get("is_recoverable"))
    branch_ready = bool(
        run.get("branch_kind") is not None
        or run.get("parent_run_identity") is not None
        or checkpoint_ready
    )
    replay_ready = bool(run.get("replay_allowed", True)) and not approval_pending and not boundary_blocked
    stalled = _workflow_stalled(run)
    recommended_path = "observe"
    if approval_pending:
        recommended_path = "approval_gate"
    elif boundary_blocked:
        recommended_path = "fresh_run"
    elif repair_ready:
        recommended_path = "step_repair"
    elif retry_ready:
        recommended_path = "retry_from_step"
    elif checkpoint_ready:
        recommended_path = "checkpoint_branch"
    elif branch_ready:
        recommended_path = "branch_continue"
    elif replay_ready:
        recommended_path = "replay"
    elif not _workflow_terminal_status(str(run.get("status") or "")):
        recommended_path = "continue_thread"
    return {
        "recommended_path": recommended_path,
        "approval_pending": approval_pending,
        "boundary_blocked": boundary_blocked,
        "retry_ready": retry_ready,
        "checkpoint_ready": checkpoint_ready,
        "repair_ready": repair_ready,
        "branch_ready": branch_ready,
        "replay_ready": replay_ready,
        "stalled": stalled,
        "checkpoint_candidate_count": len(checkpoint_candidates) if isinstance(checkpoint_candidates, list) else 0,
        "recovery_action_count": len(repair_actions) + len(replay_actions),
        "repair_action_types": repair_action_types,
        "repair_hint": (
            latest_failure.get("recovery_hint")
            if isinstance(latest_failure, dict) and isinstance(latest_failure.get("recovery_hint"), str)
            else None
        ),
        "failure_step_id": latest_failure.get("id") if isinstance(latest_failure, dict) else None,
        "failure_step_tool": latest_failure.get("tool") if isinstance(latest_failure, dict) else None,
    }


def _workflow_visible_step_records(
    step_records: list[dict[str, Any]],
    *,
    visible_step_count: int,
    is_compacted: bool,
) -> list[dict[str, Any]]:
    if not is_compacted:
        return list(step_records)
    if visible_step_count <= 0:
        return []
    return step_records[-visible_step_count:]


def _workflow_step_focus(run: dict[str, Any]) -> dict[str, Any] | None:
    records = _workflow_step_records(run)
    if not records:
        return None
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
        artifact_paths = _workflow_artifact_paths(run)
        output_path = artifact_paths[-1] if artifact_paths else None
        compaction = _workflow_state_compaction(run)
        recovery_density = _workflow_recovery_density(run)
        output_debugger = _workflow_output_debugger(run, workflow_runs)
        visible_step_records = _workflow_visible_step_records(
            _workflow_step_records(run),
            visible_step_count=int(compaction["visible_step_count"]),
            is_compacted=bool(compaction["is_compacted"]),
        )
        visible_artifact_paths = _workflow_visible_artifact_paths(
            artifact_paths,
            is_compacted=bool(compaction["is_compacted"]),
        )
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
            "artifact_paths": visible_artifact_paths,
            "step_records": visible_step_records,
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
            "is_long_running": compaction["is_long_running"],
            "is_compacted": compaction["is_compacted"],
            "duration_minutes": compaction["duration_minutes"],
            "step_count": compaction["total_step_count"],
            "visible_step_count": compaction["visible_step_count"],
            "compacted_step_count": compaction["compacted_step_count"],
            "artifact_count": compaction["artifact_count"],
            "preserved_recovery_paths": compaction["preserved_recovery_paths"],
            "recent_step_labels": compaction["recent_step_labels"],
            "state_capsule": compaction["state_capsule"],
            "recovery_density": recovery_density,
            "output_debugger": output_debugger,
        })
    return entries


def _workflow_session_queue_state(
    *,
    awaiting_approval_workflows: int,
    boundary_blocked_workflows: int,
    repair_ready_workflows: int,
    stalled_workflows: int,
    branch_ready_workflows: int,
    active_workflows: int,
) -> str:
    if awaiting_approval_workflows > 0:
        return "approval_gate"
    if boundary_blocked_workflows > 0:
        return "boundary_blocked"
    if repair_ready_workflows > 0:
        return "repair_ready"
    if stalled_workflows > 0:
        return "stalled"
    if branch_ready_workflows > 0:
        return "branch_ready"
    if active_workflows > 0:
        return "active"
    return "idle"


def _workflow_session_queue_reason(
    *,
    queue_state: str,
    awaiting_approval_workflows: int,
    boundary_blocked_workflows: int,
    repair_ready_workflows: int,
    stalled_workflows: int,
    branch_ready_workflows: int,
    output_debugger_ready_workflows: int,
) -> str | None:
    if queue_state == "approval_gate" and awaiting_approval_workflows > 0:
        return f"{awaiting_approval_workflows} workflow awaits approval before the session can advance."
    if queue_state == "boundary_blocked" and boundary_blocked_workflows > 0:
        return f"{boundary_blocked_workflows} workflow crossed a changed trust boundary and now needs repair or a fresh run."
    if queue_state == "repair_ready" and repair_ready_workflows > 0:
        return f"{repair_ready_workflows} workflow exposes a recoverable failed step that can be repaired now."
    if queue_state == "stalled" and stalled_workflows > 0:
        return f"{stalled_workflows} workflow has gone stale and needs explicit operator follow-through."
    if queue_state == "branch_ready" and branch_ready_workflows > 0:
        return f"{branch_ready_workflows} workflow already has branch or checkpoint context ready for continuation."
    if output_debugger_ready_workflows > 0:
        return f"{output_debugger_ready_workflows} workflow exposes enough history to compare outputs before continuing."
    return None


def _workflow_session_queue_draft(
    *,
    thread_label: str | None,
    lead: dict[str, Any],
    queue_state: str,
    queue_reason: str | None,
    state_capsule: str | None,
    lead_density: dict[str, Any],
    lead_debugger: dict[str, Any],
) -> str | None:
    label = thread_label or "Ambient workflows"
    workflow_name = str(lead.get("workflow_name") or "workflow")
    summary = str(lead.get("summary") or "").strip()
    parts = [
        f"Review the workflow queue for {label}.",
        f'Lead workflow "{workflow_name}" is currently {queue_state.replace("_", " ")}.',
        f'Status: {str(lead.get("status") or "unknown")}.',
        f'Summary: "{summary}".' if summary else None,
        queue_reason,
        (
            f'Next recommended path: {str(lead_density.get("recommended_path") or "observe").replace("_", " ")}.'
            if lead_density.get("recommended_path")
            else None
        ),
        f"State capsule: {state_capsule}." if state_capsule else None,
        (
            f'Latest output: "{lead_debugger.get("primary_output_path")}".'
            if lead_debugger.get("primary_output_path")
            else None
        ),
        (
            "Compare the latest branch output before continuing."
            if bool(lead_debugger.get("comparison_ready"))
            else None
        ),
    ]
    return " ".join(part for part in parts if isinstance(part, str) and part.strip())


def _workflow_session_handoff_draft(
    *,
    thread_label: str | None,
    lead: dict[str, Any],
    queue_state: str,
    attention_summary: str | None,
    state_capsule: str | None,
    lead_density: dict[str, Any],
    lead_debugger: dict[str, Any],
) -> str | None:
    label = thread_label or "Ambient workflows"
    workflow_name = str(lead.get("workflow_name") or "workflow")
    summary = str(lead.get("summary") or "").strip()
    latest_branch_summary = str(lead_debugger.get("latest_branch_summary") or "").strip()
    parts = [
        f"Prepare a workflow handoff for {label}.",
        f'Lead workflow "{workflow_name}" is currently {queue_state.replace("_", " ")}.',
        f'Summary: "{summary}".' if summary else None,
        f"Attention summary: {attention_summary}." if attention_summary else None,
        (
            f'Next recommended path: {str(lead_density.get("recommended_path") or "observe").replace("_", " ")}.'
            if lead_density.get("recommended_path")
            else None
        ),
        f"State capsule: {state_capsule}." if state_capsule else None,
        (
            f'Primary output: "{lead_debugger.get("primary_output_path")}".'
            if lead_debugger.get("primary_output_path")
            else None
        ),
        (
            f'Latest branch: "{latest_branch_summary}".'
            if latest_branch_summary
            else None
        ),
        (
            "Related outputs: "
            + ", ".join(f'"{path}"' for path in lead_debugger.get("related_output_paths") or [])
            + "."
            if isinstance(lead_debugger.get("related_output_paths"), list) and lead_debugger.get("related_output_paths")
            else None
        ),
    ]
    return " ".join(part for part in parts if isinstance(part, str) and part.strip())


def _workflow_orchestration_sessions(
    workflow_runs: list[dict[str, Any]],
    *,
    session_titles: dict[str, str],
    limit: int | None,
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
        compactions = [_workflow_state_compaction(run) for run in runs]
        recovery_densities = [_workflow_recovery_density(run) for run in runs]
        output_debuggers = [_workflow_output_debugger(run, workflow_runs) for run in runs]
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
        boundary_blocked_workflows = sum(1 for item in recovery_densities if bool(item["boundary_blocked"]))
        repair_ready_workflows = sum(1 for item in recovery_densities if bool(item["repair_ready"]))
        branch_ready_workflows = sum(1 for item in recovery_densities if bool(item["branch_ready"]))
        stalled_workflows = sum(1 for item in recovery_densities if bool(item["stalled"]))
        output_debugger_ready_workflows = sum(
            1
            for item in output_debuggers
            if bool(item["comparison_ready"]) or int(item["history_output_count"]) > 1
        )
        latest_updated_at = max(
            _parse_iso(str(run.get("updated_at") or run.get("started_at") or "")) for run in runs
        ).isoformat()
        thread_id = lead.get("thread_id") if isinstance(lead.get("thread_id"), str) else None
        queue_state = _workflow_session_queue_state(
            awaiting_approval_workflows=awaiting_approval_workflows,
            boundary_blocked_workflows=boundary_blocked_workflows,
            repair_ready_workflows=repair_ready_workflows,
            stalled_workflows=stalled_workflows,
            branch_ready_workflows=branch_ready_workflows,
            active_workflows=active_workflows,
        )
        lead_density = _workflow_recovery_density(lead)
        lead_debugger = _workflow_output_debugger(lead, workflow_runs)
        queue_reason = _workflow_session_queue_reason(
            queue_state=queue_state,
            awaiting_approval_workflows=awaiting_approval_workflows,
            boundary_blocked_workflows=boundary_blocked_workflows,
            repair_ready_workflows=repair_ready_workflows,
            stalled_workflows=stalled_workflows,
            branch_ready_workflows=branch_ready_workflows,
            output_debugger_ready_workflows=output_debugger_ready_workflows,
        )
        attention_summary = " · ".join(
            part
            for part in [
                f"{awaiting_approval_workflows} approval gate" if awaiting_approval_workflows else None,
                f"{boundary_blocked_workflows} boundary blocked" if boundary_blocked_workflows else None,
                f"{repair_ready_workflows} repair ready" if repair_ready_workflows else None,
                f"{branch_ready_workflows} branch ready" if branch_ready_workflows else None,
                f"{stalled_workflows} stalled" if stalled_workflows else None,
                f"{output_debugger_ready_workflows} debugger ready" if output_debugger_ready_workflows else None,
            ]
            if part
        )
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
            "total_step_count": sum(int(item["total_step_count"]) for item in compactions),
            "compacted_step_count": sum(int(item["compacted_step_count"]) for item in compactions),
            "compacted_workflow_count": sum(1 for item in compactions if bool(item["is_compacted"])),
            "long_running_workflow_count": sum(1 for item in compactions if bool(item["is_long_running"])),
            "artifact_count": sum(int(item["artifact_count"]) for item in compactions),
            "lead_state_capsule": _workflow_state_compaction(lead).get("state_capsule"),
            "boundary_blocked_workflows": boundary_blocked_workflows,
            "repair_ready_workflows": repair_ready_workflows,
            "branch_ready_workflows": branch_ready_workflows,
            "stalled_workflows": stalled_workflows,
            "output_debugger_ready_workflows": output_debugger_ready_workflows,
            "queue_state": queue_state,
            "queue_reason": queue_reason,
            "attention_summary": attention_summary or None,
            "queue_draft": _workflow_session_queue_draft(
                thread_label=(
                    lead.get("thread_label")
                    if isinstance(lead.get("thread_label"), str) and lead.get("thread_label")
                    else (session_titles.get(thread_id) if thread_id else "Ambient workflows")
                ),
                lead=lead,
                queue_state=queue_state,
                queue_reason=queue_reason,
                state_capsule=_workflow_state_compaction(lead).get("state_capsule"),
                lead_density=lead_density,
                lead_debugger=lead_debugger,
            ),
            "handoff_draft": _workflow_session_handoff_draft(
                thread_label=(
                    lead.get("thread_label")
                    if isinstance(lead.get("thread_label"), str) and lead.get("thread_label")
                    else (session_titles.get(thread_id) if thread_id else "Ambient workflows")
                ),
                lead=lead,
                queue_state=queue_state,
                attention_summary=attention_summary or None,
                state_capsule=_workflow_state_compaction(lead).get("state_capsule"),
                lead_density=lead_density,
                lead_debugger=lead_debugger,
            ),
            "lead_recommended_recovery_path": lead_density.get("recommended_path"),
            "lead_output_path": lead_debugger.get("primary_output_path"),
            "lead_related_output_paths": lead_debugger.get("related_output_paths"),
            "lead_output_history": lead_debugger.get("history_outputs"),
            "lead_latest_branch_run_identity": lead_debugger.get("latest_branch_run_identity"),
            "lead_latest_branch_summary": lead_debugger.get("latest_branch_summary"),
        })
    ordered = sorted(
        entries,
        key=lambda entry: (
            int(entry.get("boundary_blocked_workflows") or 0),
            int(entry.get("repair_ready_workflows") or 0),
            int(entry["blocked_workflows"]),
            int(entry["awaiting_approval_workflows"]),
            int(entry["active_workflows"]),
            _parse_iso(str(entry["latest_updated_at"])),
        ),
        reverse=True,
    )
    if isinstance(limit, int):
        ordered = ordered[:limit]
    for index, entry in enumerate(ordered, start=1):
        entry["queue_position"] = index
    return ordered


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
            "worker_disposable": bool(process.get("worker_disposable", False)),
            "trust_partition": str(process.get("trust_partition") or "unknown"),
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
            "trust_partition": None,
        }

    selected = sorted(
        branch_candidates,
        key=lambda run: _workflow_orchestration_priority(run),
        reverse=True,
    )[0]
    trust_boundary = _handoff_trust_boundary(selected) or workflow_surface_resume_metadata(selected).get("trust_boundary")
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
        "trust_partition": {
            "kind": "branch_handoff",
            "session_bound": bool(selected.get("thread_id")),
            "execution_boundaries": (
                list(selected.get("execution_boundaries"))
                if isinstance(selected.get("execution_boundaries"), list)
                else []
            ),
            "trust_boundary": trust_boundary,
        },
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
        trust_partition = {
            "kind": "background_session",
            "session_id": session_id,
            "background_process_partitioned": all(bool(process.get("session_scoped", False)) for process in processes),
            "lead_process_disposable": bool(isinstance(lead_process, dict) and lead_process.get("worker_disposable")),
            "branch_handoff_session_bound": bool(branch_handoff.get("available")) and bool(
                isinstance(branch_handoff.get("trust_partition"), dict)
                and branch_handoff["trust_partition"].get("session_bound")
            ),
        }
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
            "trust_partition": trust_partition,
            "lead_process": (
                {
                    "process_id": lead_process.get("process_id"),
                    "pid": lead_process.get("pid"),
                    "command": lead_process.get("command"),
                    "args": lead_process.get("args") if isinstance(lead_process.get("args"), list) else [],
                    "cwd": lead_process.get("cwd"),
                    "status": lead_process.get("status"),
                    "started_at": lead_process.get("started_at"),
                    "worker_disposable": bool(lead_process.get("worker_disposable", False)),
                    "trust_partition": str(lead_process.get("trust_partition") or "unknown"),
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


def _operator_guardian_state_payload(state: Any, *, session_id: str | None) -> dict[str, Any]:
    world_model = getattr(state, "world_model", None)
    confidence = getattr(state, "confidence", None)
    observer_context = getattr(state, "observer_context", None)
    user_model_profile = getattr(world_model, "user_model_profile", None)
    facets = list(getattr(user_model_profile, "facets", ()) or ())
    return {
        "summary": {
            "session_id": session_id,
            "overall_confidence": getattr(confidence, "overall", "unknown"),
            "observer_confidence": getattr(confidence, "observer", "unknown"),
            "world_model_confidence": getattr(confidence, "world_model", "unknown"),
            "memory_confidence": getattr(confidence, "memory", "unknown"),
            "current_session_confidence": getattr(confidence, "current_session", "unknown"),
            "recent_sessions_confidence": getattr(confidence, "recent_sessions", "unknown"),
            "intent_uncertainty_level": getattr(state, "intent_uncertainty_level", "clear"),
            "intent_resolution": getattr(state, "intent_resolution", "proceed"),
            "action_posture": getattr(state, "action_posture", "act_when_grounded"),
            "current_focus": getattr(world_model, "current_focus", "No clear focus signal"),
            "focus_source": getattr(world_model, "focus_source", "unknown"),
            "focus_alignment": getattr(world_model, "focus_alignment", "unknown"),
            "intervention_receptivity": getattr(world_model, "intervention_receptivity", "unknown"),
            "dominant_thread": getattr(world_model, "dominant_thread", "No dominant thread"),
            "user_model_confidence": getattr(world_model, "user_model_confidence", "empty"),
        },
        "explanation": {
            "judgment_proof_lines": list(getattr(state, "judgment_proof_lines", ()) or ()),
            "intent_uncertainty_diagnostics": list(
                getattr(state, "intent_uncertainty_diagnostics", ()) or ()
            ),
            "judgment_risks": list(getattr(world_model, "judgment_risks", ()) or ()),
            "corroboration_sources": list(getattr(world_model, "corroboration_sources", ()) or ()),
            "preference_inference_diagnostics": list(
                getattr(world_model, "preference_inference_diagnostics", ()) or ()
            ),
            "learning_diagnostics": list(getattr(state, "learning_diagnostics", ()) or ()),
            "memory_benchmark_diagnostics": list(
                getattr(state, "memory_benchmark_diagnostics", ()) or ()
            ),
            "memory_provider_diagnostics": list(
                getattr(state, "memory_provider_diagnostics", ()) or ()
            ),
            "memory_reconciliation_diagnostics": list(
                getattr(state, "memory_reconciliation_diagnostics", ()) or ()
            ),
            "restraint_reasons": list(getattr(state, "restraint_reasons", ()) or ()),
            "user_model_benchmark_diagnostics": list(
                getattr(state, "user_model_benchmark_diagnostics", ()) or ()
            ),
        },
        "user_model": {
            "confidence": getattr(user_model_profile, "confidence", "empty"),
            "restraint_posture": getattr(user_model_profile, "restraint_posture", "act_when_grounded"),
            "continuity_strategy": getattr(user_model_profile, "continuity_strategy", "preserve_current_context"),
            "clarification_watchpoints": list(
                getattr(user_model_profile, "clarification_watchpoints", ()) or ()
            ),
            "restraint_reasons": list(getattr(user_model_profile, "restraint_reasons", ()) or ()),
            "evidence_store": list(getattr(user_model_profile, "evidence_store", ()) or ()),
            "facets": [
                {
                    "key": getattr(facet, "key", "facet"),
                    "label": getattr(facet, "label", "Facet"),
                    "value": getattr(facet, "value", "unknown"),
                    "confidence": getattr(facet, "confidence", "unknown"),
                    "evidence_sources": list(getattr(facet, "evidence_sources", ()) or ()),
                    "evidence_lines": list(getattr(facet, "evidence_lines", ()) or ()),
                }
                for facet in facets
            ],
        },
        "operator_guidance": {
            "active_projects": list(getattr(world_model, "active_projects", ()) or ()),
            "active_commitments": list(getattr(world_model, "active_commitments", ()) or ()),
            "active_blockers": list(getattr(world_model, "active_blockers", ()) or ()),
            "next_up": list(getattr(world_model, "next_up", ()) or ()),
            "learning_guidance": getattr(state, "learning_guidance", ""),
            "recent_execution_summary": getattr(state, "recent_execution_summary", ""),
        },
        "observer": {
            "user_state": getattr(observer_context, "user_state", None),
            "interruption_mode": getattr(observer_context, "interruption_mode", None),
            "active_window": getattr(observer_context, "active_window", None),
            "active_project": getattr(observer_context, "active_project", None),
            "active_goals_summary": getattr(observer_context, "active_goals_summary", None),
            "screen_context": getattr(observer_context, "screen_context", None),
            "data_quality": getattr(observer_context, "data_quality", None),
            "is_working_hours": getattr(observer_context, "is_working_hours", None),
        },
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


@router.get("/operator/benchmark-proof")
async def get_operator_benchmark_proof():
    suites = benchmark_suite_report()
    memory_benchmark = await build_guardian_memory_benchmark_report()
    user_model_benchmark = await build_guardian_user_model_benchmark_report()
    evolution_targets = list_evolution_targets()
    required_suite_names = {
        str(name)
        for name in evolution_benchmark_gate_policy().get("required_benchmark_suites", [])
        if str(name).strip()
    }
    unique_scenarios = {
        str(scenario_name)
        for suite in suites
        for scenario_name in suite.get("scenario_names", [])
        if isinstance(scenario_name, str) and scenario_name.strip()
    }
    target_types = sorted(
        {
            str(target.get("target_type"))
            for target in evolution_targets
            if isinstance(target, dict) and target.get("target_type")
        }
    )
    return {
        "summary": {
            "suite_count": len(suites),
            "scenario_count": len(unique_scenarios),
            "benchmark_posture": "deterministic_proof_backed",
            "operator_status": "operator_visible",
            "remaining_gap": "live_provider_and_real_computer_use_depth",
            "governed_improvement_status": "review_gated",
            "memory_benchmark_posture": memory_benchmark["summary"]["benchmark_posture"],
            "user_model_benchmark_posture": user_model_benchmark["summary"]["benchmark_posture"],
        },
        "suites": suites,
        "memory_benchmark": memory_benchmark,
        "user_model_benchmark": user_model_benchmark,
        "governed_improvement": {
            "target_count": len(evolution_targets),
            "target_types": target_types,
            "gate_policy": evolution_benchmark_gate_policy(),
            "required_suite_count": len(required_suite_names),
        },
    }


@router.get("/operator/memory-benchmark")
async def get_operator_memory_benchmark():
    return await build_guardian_memory_benchmark_report()


@router.get("/operator/guardian-state")
async def get_operator_guardian_state(
    session_id: str | None = Query(default=None),
):
    state = await build_guardian_state(session_id=session_id)
    return _operator_guardian_state_payload(state, session_id=session_id)


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
    compactions = [_workflow_state_compaction(run) for run in workflow_runs]
    recovery_densities = [_workflow_recovery_density(run) for run in workflow_runs]
    output_debuggers = [_workflow_output_debugger(run, workflow_runs) for run in workflow_runs]
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
            "long_running_workflows": sum(1 for item in compactions if bool(item["is_long_running"])),
            "compacted_workflows": sum(1 for item in compactions if bool(item["is_compacted"])),
            "total_step_count": sum(int(item["total_step_count"]) for item in compactions),
            "compacted_step_count": sum(int(item["compacted_step_count"]) for item in compactions),
            "boundary_blocked_workflows": sum(1 for item in recovery_densities if bool(item["boundary_blocked"])),
            "repair_ready_workflows": sum(1 for item in recovery_densities if bool(item["repair_ready"])),
            "branch_ready_workflows": sum(1 for item in recovery_densities if bool(item["branch_ready"])),
            "stalled_workflows": sum(1 for item in recovery_densities if bool(item["stalled"])),
            "output_debugger_ready_workflows": sum(
                1
                for item in output_debuggers
                if bool(item["comparison_ready"]) or int(item["history_output_count"]) > 1
            ),
            "attention_sessions": sum(
                1
                for session in _workflow_orchestration_sessions(
                    workflow_runs,
                    session_titles=session_titles,
                    limit=None,
                )
                if str(session.get("queue_state") or "") not in {"idle", "active"}
            ),
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
    filtered_workflow_runs = [
        run
        for run in (workflow_runs if isinstance(workflow_runs, list) else [])
        if isinstance(run, dict) and _within_operator_window(run.get("updated_at") or run.get("started_at"), cutoff)
    ]
    filtered_pending_approvals = [
        approval
        for approval in (pending_approvals if isinstance(pending_approvals, list) else [])
        if isinstance(approval, dict) and _within_operator_window(approval.get("created_at"), cutoff)
    ]
    filtered_session_search_matches = [
        match
        for match in (session_search_matches if isinstance(session_search_matches, list) else [])
        if isinstance(match, dict) and _within_operator_window(match.get("matched_at"), cutoff)
    ]
    all_bundles = _build_engineering_memory_bundles(
        filtered_workflow_runs,
        filtered_pending_approvals,
        audit_events if isinstance(audit_events, list) else [],
        filtered_session_search_matches,
        normalized_query=_engineering_query(normalized_query),
        limit_bundles=None,
        limit_session_matches=limit_session_matches,
    )
    bundles = all_bundles[:limit_bundles]
    return {
        "summary": {
            "query": normalized_query or None,
            "tracked_bundles": len(all_bundles),
            "repository_bundle_count": sum(
                1 for bundle in all_bundles if str(bundle.get("target_kind") or "") == "repository"
            ),
            "pull_request_bundle_count": sum(
                1 for bundle in all_bundles if str(bundle.get("target_kind") or "") == "pull_request"
            ),
            "work_item_bundle_count": sum(
                1 for bundle in all_bundles if str(bundle.get("target_kind") or "") == "work_item"
            ),
            "search_match_count": len(filtered_session_search_matches),
        },
        "search_matches": (
            filtered_session_search_matches[:limit_session_matches]
        ),
        "bundles": bundles,
    }


@router.get("/operator/continuity-graph")
async def get_operator_continuity_graph(
    session_id: str | None = Query(default=None),
    limit_sessions: int = Query(default=6, ge=1, le=20),
):
    sessions, workflow_runs, pending_approvals, notifications, queued_insights, recent_interventions, continuity_snapshot = await asyncio.gather(
        session_manager.list_sessions(),
        _list_workflow_runs(limit=max(limit_sessions * 8, 60), session_id=session_id),
        approval_repository.list_pending(session_id=session_id, limit=max(limit_sessions * 4, 40)),
        native_notification_queue.list(),
        insight_queue.peek_all(),
        guardian_feedback_repository.list_recent(limit=max(limit_sessions * 8, 60), session_id=session_id),
        build_observer_continuity_snapshot(),
    )
    return _build_operator_continuity_graph(
        sessions if isinstance(sessions, list) else [],
        workflow_runs if isinstance(workflow_runs, list) else [],
        pending_approvals if isinstance(pending_approvals, list) else [],
        notifications if isinstance(notifications, list) else [],
        queued_insights if isinstance(queued_insights, list) else [],
        recent_interventions if isinstance(recent_interventions, list) else [],
        continuity_snapshot if isinstance(continuity_snapshot, dict) else {},
        session_id=session_id,
        limit_sessions=limit_sessions,
    )


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
