"""Workflows API — list, toggle, reload reusable multi-step workflows."""

from collections import defaultdict
from datetime import datetime
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.api.capabilities import _recommended_tool_policy_mode
from src.agent.session import session_manager
from src.agent.factory import get_base_tools_and_active_skills
from src.approval.repository import fingerprint_tool_call
from src.approval.repository import approval_repository
from src.audit.repository import audit_repository
from src.audit.runtime import log_integration_event
from src.tools.policy import get_current_tool_policy_mode
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


def _workflow_event_fingerprint(tool_name: str, details: dict[str, Any]) -> str:
    run_fingerprint = details.get("run_fingerprint")
    if isinstance(run_fingerprint, str) and run_fingerprint.strip():
        return run_fingerprint
    arguments = _as_record(details.get("arguments"))
    if arguments:
        return fingerprint_tool_call(tool_name, arguments)
    return "none"


def _workflow_projection_key(event: dict[str, Any], details: dict[str, Any]) -> str:
    tool_name = str(event.get("tool_name") or "workflow")
    fingerprint = _workflow_event_fingerprint(tool_name, details)
    return f"{event.get('session_id') or 'global'}:{tool_name}:{fingerprint}"


def _workflow_projection_prefix(session_id: str | None, tool_name: str) -> str:
    return f"{session_id or 'global'}:{tool_name}:"


def _approval_projection_key(
    *,
    session_id: str | None,
    tool_name: str,
    fingerprint: str | None,
) -> str:
    return f"{session_id or 'global'}:{tool_name}:{fingerprint or 'none'}"


def _workflow_run_approval_key(run: dict[str, Any]) -> str:
    tool_name = str(run.get("tool_name") or "workflow")
    run_fingerprint = run.get("run_fingerprint")
    fingerprint = (
        run_fingerprint
        if isinstance(run_fingerprint, str) and run_fingerprint.strip()
        else (
            fingerprint_tool_call(tool_name, run.get("arguments") or {})
            if run.get("arguments")
            else None
        )
    )
    return _approval_projection_key(
        session_id=run.get("session_id") if isinstance(run.get("session_id"), str) else None,
        tool_name=tool_name,
        fingerprint=fingerprint,
    )


def _workflow_replay_draft(workflow_name: str, arguments: dict[str, Any] | None) -> str:
    if not arguments:
        return f'Run workflow "{workflow_name}".'
    rendered = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in arguments.items()
    )
    return f'Run workflow "{workflow_name}" with {rendered}.'


def _workflow_retry_from_step_draft(
    workflow_name: str,
    *,
    step_id: str,
    arguments: dict[str, Any] | None,
) -> str:
    return (
        f"{_workflow_replay_draft(workflow_name, arguments).rstrip('.')} "
        f"Resume from step \"{step_id}\"."
    )


def _workflow_replay_policy(
    *,
    availability: str,
    risk_level: str,
    execution_boundaries: list[str],
    accepts_secret_refs: bool,
    pending_approval_count: int,
) -> tuple[bool, str | None]:
    if availability == "disabled":
        return False, "workflow_disabled"
    if availability != "ready":
        return False, "workflow_unavailable"
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


def _workflow_runtime_statuses() -> dict[str, dict[str, Any]]:
    base_tools, active_skill_names, _ = get_base_tools_and_active_skills()
    available_tool_names = [tool.name for tool in base_tools]
    workflows = workflow_manager.list_workflows(
        available_tool_names=available_tool_names,
        active_skill_names=active_skill_names,
    )
    statuses: dict[str, dict[str, Any]] = {}
    for workflow in workflows:
        enabled = bool(workflow.get("enabled", False))
        is_available = bool(workflow.get("is_available", False))
        if not enabled:
            availability = "disabled"
        elif is_available:
            availability = "ready"
        else:
            availability = "blocked"
        statuses[str(workflow["name"])] = {
            **workflow,
            "availability": availability,
            "missing_tools": list(workflow.get("missing_tools", [])),
            "missing_skills": list(workflow.get("missing_skills", [])),
        }
    return statuses


def _workflow_replay_recommended_actions(workflow_status: dict[str, Any] | None) -> list[dict[str, Any]]:
    if workflow_status is None:
        return []
    actions: list[dict[str, Any]] = []
    if not bool(workflow_status.get("enabled", False)):
        actions.append({
            "type": "toggle_workflow",
            "label": "Enable workflow",
            "name": workflow_status["name"],
            "enabled": True,
        })
    for skill_name in workflow_status.get("missing_skills", []) or []:
        actions.append({
            "type": "toggle_skill",
            "label": f"Enable {skill_name}",
            "name": skill_name,
            "enabled": True,
        })
    current_tool_mode = get_current_tool_policy_mode()
    for tool_name in workflow_status.get("missing_tools", []) or []:
        suggested_mode = _recommended_tool_policy_mode(
            current_mode=current_tool_mode,
            blocked_reason=None,
        )
        if suggested_mode is None:
            continue
        actions.append({
            "type": "set_tool_policy",
            "label": f"Allow {tool_name}",
            "mode": suggested_mode,
        })
    if not actions:
        actions.append({
            "type": "open_settings",
            "label": "Open settings",
            "target": "workflows",
        })
    return actions


def _resume_checkpoint_label(*, approvals: list[dict[str, Any]], continued_error_steps: list[str]) -> str | None:
    if approvals:
        return "Approval gate"
    if continued_error_steps:
        return "Retry failed step"
    return None


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
    for step in run.get("step_records", []) or []:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "step")
        step_tool = str(step.get("tool") or "tool")
        step_status = str(step.get("status") or "succeeded")
        result_summary = str(step.get("result_summary") or "").strip()
        entries.append({
            "kind": f"workflow_step_{step_status}",
            "at": run["updated_at"],
            "summary": (
                f"{step_id} ({step_tool}) {step_status.replace('_', ' ')}"
                + (f" · {result_summary}" if result_summary else "")
            ),
            "step_id": step_id,
            "step_tool": step_tool,
            "result_summary": result_summary,
            "error_kind": step.get("error_kind"),
        })
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
    workflow_statuses = _workflow_runtime_statuses()
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
        key = _workflow_projection_key(event, details)
        run_fingerprint = _workflow_event_fingerprint(tool_name, details)
        if event.get("event_type") == "tool_call":
            arguments = _as_record(details.get("arguments")) or None
            pending_by_key[key].append({
                "id": event["id"],
                "tool_name": tool_name,
                "workflow_name": str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
                "session_id": event.get("session_id"),
                "run_fingerprint": run_fingerprint,
                "status": "running",
                "started_at": event["created_at"],
                "updated_at": event["created_at"],
                "summary": event.get("summary") or "",
                "step_tools": [],
                "step_records": [],
                "artifact_paths": _extract_artifact_paths(arguments),
                "continued_error_steps": [],
                "arguments": arguments,
            })
            continue

        run_queue = pending_by_key.get(key, [])
        if not run_queue and run_fingerprint == "none":
            prefix = _workflow_projection_prefix(
                event.get("session_id") if isinstance(event.get("session_id"), str) else None,
                tool_name,
            )
            fallback_key = next(
                (
                    pending_key for pending_key, queue in pending_by_key.items()
                    if pending_key.startswith(prefix) and queue
                ),
                None,
            )
            if fallback_key is not None:
                key = fallback_key
                run_queue = pending_by_key.get(fallback_key, [])
        run = run_queue.pop(0) if run_queue else {
            "id": event["id"],
            "tool_name": tool_name,
            "workflow_name": str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
            "session_id": event.get("session_id"),
            "run_fingerprint": run_fingerprint,
            "status": "running",
            "started_at": event["created_at"],
            "updated_at": event["created_at"],
            "summary": event.get("summary") or "",
            "step_tools": [],
            "step_records": [],
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
        workflow_status = workflow_statuses.get(str(run["workflow_name"]))
        approval_key = _workflow_run_approval_key(run)
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
            "step_records": [
                value for value in details.get("step_records", [])
                if isinstance(value, dict)
            ] or run.get("step_records", []),
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
            "availability": (
                workflow_status.get("availability", "unknown")
                if workflow_status is not None
                else "unknown"
            ),
            "replay_inputs": run.get("arguments") or {},
            "parameter_schema": (
                workflow_status.get("inputs", {})
                if workflow_status is not None and isinstance(workflow_status.get("inputs"), dict)
                else {}
            ),
            "replay_recommended_actions": _workflow_replay_recommended_actions(workflow_status),
        })
        replay_allowed, replay_block_reason = _workflow_replay_policy(
            availability=str(run["availability"]),
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
            "resume_from_step": (
                "approval_gate"
                if approvals
                else (run["continued_error_steps"][0] if run.get("continued_error_steps") else None)
            ),
            "retry_from_step_draft": (
                _workflow_retry_from_step_draft(
                    str(run["workflow_name"]),
                    step_id=str(run["continued_error_steps"][0]),
                    arguments=run.get("arguments"),
                )
                if replay_allowed and run.get("continued_error_steps")
                else None
            ),
            "resume_checkpoint_label": _resume_checkpoint_label(
                approvals=approvals,
                continued_error_steps=list(run.get("continued_error_steps", [])),
            ),
            "approval_recovery_message": (
                f"Review pending approval(s) for workflow '{run['workflow_name']}' before replaying."
                if len(approvals) > 0
                else (
                    f"Repair workflow '{run['workflow_name']}' before replaying."
                    if str(run["availability"]) != "ready"
                    else None
                )
            ),
            "thread_continue_message": (
                approvals[0].get("resume_message")
                if approvals and isinstance(approvals[0], dict)
                else None
            ),
            "run_identity": f"{run.get('session_id') or 'global'}:{tool_name}:{run_fingerprint}",
            "timeline": _timeline_entries_for_run(run, approvals=approvals),
        })
        completed.append(run)

    for run_queue in pending_by_key.values():
        for run in run_queue:
            workflow_meta = workflow_manager.get_tool_metadata(str(run["tool_name"])) or {}
            workflow_status = workflow_statuses.get(str(run["workflow_name"]))
            approval_key = _workflow_run_approval_key(run)
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
                "availability": (
                    workflow_status.get("availability", "unknown")
                    if workflow_status is not None
                    else "unknown"
                ),
                "replay_inputs": run.get("arguments") or {},
                "parameter_schema": (
                    workflow_status.get("inputs", {})
                    if workflow_status is not None and isinstance(workflow_status.get("inputs"), dict)
                    else {}
                ),
                "replay_recommended_actions": _workflow_replay_recommended_actions(workflow_status),
            })
            replay_allowed, replay_block_reason = _workflow_replay_policy(
                availability=str(run["availability"]),
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
                "resume_from_step": (
                    "approval_gate"
                    if approvals
                    else (run["continued_error_steps"][0] if run.get("continued_error_steps") else None)
                ),
                "resume_checkpoint_label": _resume_checkpoint_label(
                    approvals=approvals,
                    continued_error_steps=list(run.get("continued_error_steps", [])),
                ),
                "approval_recovery_message": (
                    f"Review pending approval(s) for workflow '{run['workflow_name']}' before replaying."
                    if len(approvals) > 0
                    else (
                        f"Repair workflow '{run['workflow_name']}' before replaying."
                        if str(run["availability"]) != "ready"
                        else None
                    )
                ),
                "thread_continue_message": (
                    approvals[0].get("resume_message")
                    if approvals and isinstance(approvals[0], dict)
                    else None
                ),
                "run_identity": f"{run.get('session_id') or 'global'}:{run['tool_name']}:{run.get('run_fingerprint') or 'none'}",
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
