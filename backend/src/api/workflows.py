"""Workflows API — list, toggle, reload reusable multi-step workflows."""

from collections import defaultdict
from datetime import datetime
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.agent.session import session_manager
from src.agent.factory import get_base_tools_and_active_skills
from src.approval.repository import fingerprint_tool_call
from src.approval.repository import approval_repository
from src.audit.repository import audit_repository
from src.audit.runtime import log_integration_event
from src.workflows.manager import workflow_manager

router = APIRouter()


class UpdateWorkflowRequest(BaseModel):
    enabled: bool


def _as_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _workflow_name_from_tool(tool_name: str) -> str:
    if tool_name.startswith("workflow_"):
        return tool_name.removeprefix("workflow_").replace("_", "-")
    return tool_name


def _extract_artifact_paths(value: Any) -> list[str]:
    paths: list[str] = []

    def visit(current: Any, key_hint: str | None = None) -> None:
        if isinstance(current, list):
            for item in current:
                visit(item, key_hint)
            return
        if isinstance(current, dict):
            for key, inner in current.items():
                visit(inner, key)
            return
        if (
            key_hint == "file_path"
            and isinstance(current, str)
            and current.strip()
            and current not in paths
        ):
            paths.append(current)

    visit(value)
    return paths


def _workflow_projection_key(event: dict[str, Any]) -> str:
    return f"{event.get('session_id') or 'global'}:{event.get('tool_name') or 'workflow'}"


def _approval_projection_key(
    *,
    session_id: str | None,
    tool_name: str,
    fingerprint: str | None,
) -> str:
    return f"{session_id or 'global'}:{tool_name}:{fingerprint or 'none'}"


def _workflow_replay_draft(workflow_name: str, arguments: dict[str, Any] | None) -> str:
    if not arguments:
        return f'Run workflow "{workflow_name}".'
    rendered = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in arguments.items()
    )
    return f'Run workflow "{workflow_name}" with {rendered}.'


def _workflow_replay_policy(
    *,
    risk_level: str,
    execution_boundaries: list[str],
    accepts_secret_refs: bool,
    pending_approval_count: int,
) -> tuple[bool, str | None]:
    if pending_approval_count > 0:
        return False, "pending_approval"
    if accepts_secret_refs:
        return False, "secret_ref_surface"
    if any(
        boundary in {"secret_management", "secret_read", "secret_injection"}
        for boundary in execution_boundaries
    ):
        return False, "secret_bearing_boundary"
    if risk_level == "high":
        return False, "high_risk_requires_manual_reentry"
    return True, None


def _timeline_entries_for_run(
    run: dict[str, Any],
    *,
    approvals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries = [
        {
            "kind": "workflow_started",
            "at": run["started_at"],
            "summary": "Workflow started",
        }
    ]
    for approval in approvals:
        entries.append({
            "kind": "approval_pending",
            "at": approval.get("created_at") or run["updated_at"],
            "summary": approval.get("summary")
            or f"Approval pending for {run['workflow_name']}",
            "approval_id": approval.get("id"),
            "risk_level": approval.get("risk_level"),
        })
    status = str(run["status"])
    entries.append({
        "kind": f"workflow_{status}",
        "at": run["updated_at"],
        "summary": run["summary"],
    })
    return entries


async def _list_workflow_runs(
    *,
    limit: int,
    session_id: str | None,
) -> list[dict[str, Any]]:
    events = await audit_repository.list_events(limit=max(limit * 6, 30), session_id=session_id)
    workflow_events = [
        event for event in events
        if isinstance(event.get("tool_name"), str) and str(event["tool_name"]).startswith("workflow_")
    ]
    workflow_events.sort(key=lambda item: item.get("created_at", ""))
    pending_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    completed: list[dict[str, Any]] = []
    pending_approvals = await approval_repository.list_pending(session_id=session_id, limit=100)
    pending_by_tool: dict[tuple[str | None, str], list[dict[str, Any]]] = defaultdict(list)
    pending_by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for approval in pending_approvals:
        tool_name = str(approval.get("tool_name") or "")
        approval_session_id = approval.get("session_id")
        pending_by_tool[(approval_session_id, tool_name)].append(approval)
        pending_by_signature[
            _approval_projection_key(
                session_id=approval_session_id if isinstance(approval_session_id, str) else None,
                tool_name=tool_name,
                fingerprint=str(approval.get("fingerprint") or ""),
            )
        ].append(approval)
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions()
        if isinstance(session, dict) and session.get("id")
    }

    for event in workflow_events:
        details = _as_record(event.get("details"))
        tool_name = str(event.get("tool_name") or "workflow")
        key = _workflow_projection_key(event)
        if event.get("event_type") == "tool_call":
            arguments = _as_record(details.get("arguments")) or None
            pending_by_key[key].append({
                "id": event["id"],
                "tool_name": tool_name,
                "workflow_name": str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
                "session_id": event.get("session_id"),
                "status": "running",
                "started_at": event["created_at"],
                "updated_at": event["created_at"],
                "summary": event.get("summary") or "",
                "step_tools": [],
                "artifact_paths": _extract_artifact_paths(arguments),
                "continued_error_steps": [],
                "arguments": arguments,
            })
            continue

        run_queue = pending_by_key.get(key, [])
        run = run_queue.pop(0) if run_queue else {
            "id": event["id"],
            "tool_name": tool_name,
            "workflow_name": str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
            "session_id": event.get("session_id"),
            "status": "running",
            "started_at": event["created_at"],
            "updated_at": event["created_at"],
            "summary": event.get("summary") or "",
            "step_tools": [],
            "artifact_paths": [],
            "continued_error_steps": [],
            "arguments": _as_record(details.get("arguments")) or None,
        }
        if not run_queue and key in pending_by_key:
            pending_by_key.pop(key, None)

        artifact_paths = list(run.get("artifact_paths", []))
        for path in details.get("artifact_paths") or []:
            if isinstance(path, str) and path.strip() and path not in artifact_paths:
                artifact_paths.append(path)
        for path in _extract_artifact_paths(details.get("arguments")):
            if path not in artifact_paths:
                artifact_paths.append(path)

        workflow_meta = workflow_manager.get_tool_metadata(tool_name) or {}
        approval_key = _approval_projection_key(
            session_id=run.get("session_id") if isinstance(run.get("session_id"), str) else None,
            tool_name=tool_name,
            fingerprint=(
                fingerprint_tool_call(tool_name, run.get("arguments") or {})
                if run.get("arguments")
                else None
            ),
        )
        approvals = pending_by_signature.get(approval_key) or pending_by_tool.get(
            (run.get("session_id"), tool_name),
            [],
        )

        run.update({
            "status": "failed" if event.get("event_type") == "tool_failed" else "succeeded",
            "updated_at": event["created_at"],
            "summary": event.get("summary") or run.get("summary") or "",
            "step_tools": [
                value for value in details.get("step_tools", [])
                if isinstance(value, str)
            ] or run.get("step_tools", []),
            "artifact_paths": artifact_paths,
            "continued_error_steps": [
                value for value in details.get("continued_error_steps", [])
                if isinstance(value, str)
            ] or run.get("continued_error_steps", []),
            "risk_level": workflow_meta.get("risk_level", "high"),
            "execution_boundaries": workflow_meta.get("execution_boundaries", ["unknown"]),
            "accepts_secret_refs": bool(workflow_meta.get("accepts_secret_refs", False)),
            "pending_approval_count": len(approvals),
            "pending_approval_ids": [approval["id"] for approval in approvals],
            "pending_approvals": approvals,
        })
        replay_allowed, replay_block_reason = _workflow_replay_policy(
            risk_level=str(run["risk_level"]),
            execution_boundaries=list(run["execution_boundaries"]),
            accepts_secret_refs=bool(run["accepts_secret_refs"]),
            pending_approval_count=len(approvals),
        )
        run.update({
            "thread_id": run.get("session_id"),
            "thread_label": (
                session_titles.get(str(run["session_id"]))
                if run.get("session_id")
                else None
            ),
            "thread_source": "session" if run.get("session_id") else "ambient",
            "replay_allowed": replay_allowed,
            "replay_block_reason": replay_block_reason,
            "replay_draft": (
                _workflow_replay_draft(str(run["workflow_name"]), run.get("arguments"))
                if replay_allowed
                else None
            ),
            "approval_recovery_message": (
                f"Review pending approval(s) for workflow '{run['workflow_name']}' before replaying."
                if len(approvals) > 0
                else None
            ),
            "timeline": _timeline_entries_for_run(run, approvals=approvals),
        })
        completed.append(run)

    for run_queue in pending_by_key.values():
        for run in run_queue:
            workflow_meta = workflow_manager.get_tool_metadata(str(run["tool_name"])) or {}
            approval_key = _approval_projection_key(
                session_id=run.get("session_id") if isinstance(run.get("session_id"), str) else None,
                tool_name=str(run["tool_name"]),
                fingerprint=(
                    fingerprint_tool_call(str(run["tool_name"]), run.get("arguments") or {})
                    if run.get("arguments")
                    else None
                ),
            )
            approvals = pending_by_signature.get(approval_key) or pending_by_tool.get(
                (run.get("session_id"), str(run["tool_name"])),
                [],
            )
            run.update({
                "risk_level": workflow_meta.get("risk_level", "high"),
                "execution_boundaries": workflow_meta.get("execution_boundaries", ["unknown"]),
                "accepts_secret_refs": bool(workflow_meta.get("accepts_secret_refs", False)),
                "status": "awaiting_approval" if len(approvals) > 0 else "running",
                "pending_approval_count": len(approvals),
                "pending_approval_ids": [approval["id"] for approval in approvals],
                "pending_approvals": approvals,
            })
            replay_allowed, replay_block_reason = _workflow_replay_policy(
                risk_level=str(run["risk_level"]),
                execution_boundaries=list(run["execution_boundaries"]),
                accepts_secret_refs=bool(run["accepts_secret_refs"]),
                pending_approval_count=len(approvals),
            )
            run.update({
                "thread_id": run.get("session_id"),
                "thread_label": (
                    session_titles.get(str(run["session_id"]))
                    if run.get("session_id")
                    else None
                ),
                "thread_source": "session" if run.get("session_id") else "ambient",
                "replay_allowed": replay_allowed,
                "replay_block_reason": replay_block_reason,
                "replay_draft": (
                    _workflow_replay_draft(str(run["workflow_name"]), run.get("arguments"))
                    if replay_allowed
                    else None
                ),
                "approval_recovery_message": (
                    f"Review pending approval(s) for workflow '{run['workflow_name']}' before replaying."
                    if len(approvals) > 0
                    else None
                ),
                "timeline": _timeline_entries_for_run(run, approvals=approvals),
            })
            completed.append(run)

    completed.sort(
        key=lambda item: datetime.fromisoformat(str(item["updated_at"]).replace("Z", "+00:00")),
        reverse=True,
    )
    return completed[:limit]


@router.get("/workflows")
async def list_workflows():
    base_tools, active_skill_names, mcp_mode = get_base_tools_and_active_skills()
    workflows = []
    for workflow in workflow_manager.list_workflows(
        available_tool_names=[tool.name for tool in base_tools],
        active_skill_names=active_skill_names,
    ):
        boundaries = workflow.get("execution_boundaries", [])
        risk_level = workflow.get("risk_level", "low")
        requires_approval = (
            risk_level == "high"
            or ("external_mcp" in boundaries and mcp_mode == "approval")
        )
        if "external_mcp" in boundaries and mcp_mode == "approval":
            approval_behavior = "always"
        elif risk_level == "high":
            approval_behavior = "high_risk_mode"
        else:
            approval_behavior = "never"
        workflows.append({
            **workflow,
            "requires_approval": requires_approval,
            "approval_behavior": approval_behavior,
        })
    return {"workflows": workflows}


@router.put("/workflows/{name}")
async def update_workflow(name: str, req: UpdateWorkflowRequest):
    ok = workflow_manager.enable(name) if req.enabled else workflow_manager.disable(name)
    if not ok:
        await log_integration_event(
            integration_type="workflow",
            name=name,
            outcome="failed",
            details={
                "status": "not_found",
                "enabled": req.enabled,
            },
        )
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    await log_integration_event(
        integration_type="workflow",
        name=name,
        outcome="succeeded",
        details={"enabled": req.enabled},
    )
    return {"status": "updated", "name": name, "enabled": req.enabled}


@router.post("/workflows/reload")
async def reload_workflows():
    workflows = workflow_manager.reload()
    await log_integration_event(
        integration_type="workflows",
        name="reload",
        outcome="succeeded",
        details={
            "count": len(workflows),
            "enabled_count": sum(1 for workflow in workflows if workflow.get("enabled", False)),
            "workflow_names": [workflow["name"] for workflow in workflows],
        },
    )
    return {"status": "reloaded", "count": len(workflows), "workflows": workflows}


@router.get("/workflows/runs")
async def list_workflow_runs(
    limit: int = Query(default=12, ge=1, le=50),
    session_id: str | None = Query(default=None),
):
    return {"runs": await _list_workflow_runs(limit=limit, session_id=session_id)}
