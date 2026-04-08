"""Workflow manager and runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
import time
from typing import Any

from smolagents import Tool
from sqlmodel import col, select

from src.audit.formatting import format_tool_call_summary, redact_for_audit
from src.approval.runtime import get_current_session_id
from src.db.engine import get_session
from src.db.models import AuditEvent
from src.extensions.permissions import evaluate_tool_permissions
from src.extensions.registry import ExtensionRegistry, ExtensionRegistrySnapshot
from src.memory.flush import flush_session_memory_sync
from src.approval.repository import fingerprint_tool_call
from src.native_tools.registry import TOOL_METADATA, canonical_tool_name
from src.tools.policy import get_tool_source_context, tool_accepts_secret_refs
from src.workflows.loader import Workflow, scan_workflow_paths
from src.workflows.run_identity import parse_workflow_run_identity

logger = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"{{\s*([^}]+)\s*}}")
_WORKFLOW_CONTROL_INPUTS: dict[str, dict[str, Any]] = {
    "_seraph_parent_run_identity": {
        "type": "string",
        "description": "Optional Seraph workflow lineage parent run identity.",
        "nullable": True,
    },
    "_seraph_root_run_identity": {
        "type": "string",
        "description": "Optional Seraph workflow lineage root run identity.",
        "nullable": True,
    },
    "_seraph_branch_kind": {
        "type": "string",
        "description": "Optional Seraph workflow control mode such as replay_from_start or retry_failed_step.",
        "nullable": True,
    },
    "_seraph_branch_depth": {
        "type": "integer",
        "description": "Optional Seraph workflow lineage depth.",
        "nullable": True,
    },
    "_seraph_resume_from_step": {
        "type": "string",
        "description": "Optional checkpoint step id to resume or branch from.",
        "nullable": True,
    },
}
_WORKFLOW_CONTROL_FIELD_NAMES = set(_WORKFLOW_CONTROL_INPUTS)


def _run_async(coro):
    return asyncio.run(coro)


def _resolve_context_expr(expr: str, context: dict[str, Any]) -> Any:
    parts = [part.strip() for part in expr.split(".") if part.strip()]
    if not parts:
        raise KeyError(expr)
    current: Any = context
    for index, part in enumerate(parts):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if index == 0 and part in context.get("inputs", {}):
            current = context["inputs"][part]
            continue
        raise KeyError(expr)
    return current


def _render_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = _TEMPLATE_RE.fullmatch(value.strip())
        if match:
            return _resolve_context_expr(match.group(1), context)

        def _replace(template_match: re.Match[str]) -> str:
            resolved = _resolve_context_expr(template_match.group(1), context)
            if isinstance(resolved, (dict, list)):
                return json.dumps(resolved, ensure_ascii=False)
            return str(resolved)

        return _TEMPLATE_RE.sub(_replace, value)
    if isinstance(value, list):
        return [_render_value(item, context) for item in value]
    if isinstance(value, dict):
        return {
            key: _render_value(item, context)
            for key, item in value.items()
        }
    return value


def _summarize_workflow_result(workflow: Workflow, step_results: dict[str, dict[str, Any]]) -> str:
    lines = [f"Workflow '{workflow.name}' completed."]
    for step in workflow.steps:
        step_state = step_results.get(step.id, {})
        result = str(step_state.get("result", ""))
        if len(result) > 240:
            result = result[:237] + "..."
        lines.append(f"- {step.id} ({canonical_tool_name(step.tool)}): {result}")
    return "\n".join(lines)


def _build_canvas_output(
    workflow: Workflow,
    *,
    result_text: str,
    step_records: list[dict[str, Any]],
    artifact_paths: list[str],
) -> dict[str, Any] | None:
    if not workflow.output_surface:
        return None
    step_items = [
        f"{step['id']} · {step['tool']} · {step['status']}"
        + (f" · {step['result_summary']}" if step.get("result_summary") else "")
        for step in step_records
    ]
    configured_sections = workflow.output_surface_sections or ["Summary", "Steps"]
    sections: list[dict[str, Any]] = []
    for label in configured_sections:
        normalized = label.strip().casefold()
        if normalized == "summary":
            items = [result_text]
        elif normalized == "steps":
            items = step_items
        elif normalized == "artifacts":
            items = list(artifact_paths)
        else:
            items = [result_text]
        if items:
            sections.append({"label": label, "items": items})
    if artifact_paths and not any(section["label"].strip().casefold() == "artifacts" for section in sections):
        sections.append({"label": "Artifacts", "items": list(artifact_paths)})
    return {
        "surface": workflow.output_surface,
        "title": workflow.output_surface_title or workflow.name,
        "summary": result_text,
        "section_count": len(sections),
        "sections": sections,
    }


def _redact_canvas_output(canvas_output: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(canvas_output, dict):
        return None
    redacted_sections: list[dict[str, Any]] = []
    raw_sections = canvas_output.get("sections")
    if isinstance(raw_sections, list):
        for section in raw_sections:
            if not isinstance(section, dict):
                continue
            items = section.get("items")
            item_count = len(items) if isinstance(items, list) else 0
            redacted_sections.append(
                {
                    "label": str(section.get("label") or ""),
                    "item_count": item_count,
                }
            )
    return {
        "surface": str(canvas_output.get("surface") or ""),
        "title": str(canvas_output.get("title") or ""),
        "summary": "workflow content redacted",
        "section_count": int(canvas_output.get("section_count") or len(redacted_sections)),
        "sections": redacted_sections,
    }


def _collect_artifact_paths(value: Any) -> list[str]:
    paths: list[str] = []

    def _visit(current: Any, key_hint: str | None = None) -> None:
        if isinstance(current, dict):
            for key, inner in current.items():
                _visit(inner, str(key))
            return
        if isinstance(current, list):
            for item in current:
                _visit(item, key_hint)
            return
        if (
            key_hint == "file_path"
            and isinstance(current, str)
            and current.strip()
            and current not in paths
        ):
            paths.append(current)

    _visit(value)
    return paths


def _summarize_value_shape(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return "empty text"
        return f"text ({len(stripped)} chars)"
    if isinstance(value, dict):
        return f"object ({len(value)} keys)"
    if isinstance(value, list):
        return f"list ({len(value)} items)"
    if isinstance(value, tuple):
        return f"tuple ({len(value)} items)"
    return type(value).__name__


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonicalize_tool_names(tool_names: list[str]) -> list[str]:
    return list(dict.fromkeys(canonical_tool_name(tool_name) for tool_name in tool_names))


def _json_safe_value(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except TypeError:
        return str(value)


def _max_risk_level(current: str, candidate: str) -> str:
    ranks = {"low": 0, "medium": 1, "high": 2}
    current_rank = ranks.get(current, ranks["high"])
    candidate_rank = ranks.get(candidate, ranks["high"])
    return current if current_rank >= candidate_rank else candidate


def _append_unique_source_systems(
    target: list[dict[str, Any]],
    additions: list[dict[str, Any]],
) -> None:
    for source_system in additions:
        if isinstance(source_system, dict) and source_system not in target:
            target.append(source_system)


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: dict[str, None] = {}
    for item in value:
        text = str(item).strip()
        if text:
            normalized[text] = None
    return sorted(normalized)


def _normalize_source_systems(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, bool, tuple[str, ...]]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        server_name = str(item.get("server_name") or "").strip()
        hostname = str(item.get("hostname") or "").strip()
        source = str(item.get("source") or "").strip()
        authenticated_source = bool(item.get("authenticated_source", False))
        credential_sources = tuple(_normalize_string_list(item.get("credential_sources")))
        key = (
            server_name,
            hostname,
            source,
            authenticated_source,
            credential_sources,
        )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "server_name": server_name,
                "hostname": hostname,
                "source": source,
                "authenticated_source": authenticated_source,
                "credential_sources": list(credential_sources),
            }
        )
    normalized.sort(
        key=lambda item: (
            str(item.get("server_name") or ""),
            str(item.get("hostname") or ""),
            str(item.get("source") or ""),
            bool(item.get("authenticated_source", False)),
            tuple(item.get("credential_sources") or []),
        )
    )
    return normalized


def normalize_workflow_approval_context(
    value: Any,
    *,
    workflow_name: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    risk_level = str(value.get("risk_level") or "").strip()
    execution_boundaries = _normalize_string_list(value.get("execution_boundaries"))
    step_tools = _normalize_string_list(value.get("step_tools"))
    delegated_specialists = _normalize_string_list(value.get("delegated_specialists"))
    delegated_tool_names = _normalize_string_list(value.get("delegated_tool_names"))
    authenticated_source = bool(value.get("authenticated_source", False))
    delegation_target_unresolved = bool(value.get("delegation_target_unresolved", False))
    source_systems = _normalize_source_systems(value.get("source_systems"))
    if not any(
        [
            risk_level,
            execution_boundaries,
            step_tools,
            delegated_specialists,
            delegated_tool_names,
            "accepts_secret_refs" in value,
            authenticated_source,
            delegation_target_unresolved,
            source_systems,
        ]
    ):
        return None
    normalized = {
        "workflow_name": str(value.get("workflow_name") or workflow_name or "").strip() or None,
        "risk_level": risk_level or "unknown",
        "execution_boundaries": execution_boundaries,
        "accepts_secret_refs": bool(value.get("accepts_secret_refs", False)),
        "step_tools": step_tools,
    }
    if delegated_specialists:
        normalized["delegated_specialists"] = delegated_specialists
    if delegated_tool_names:
        normalized["delegated_tool_names"] = delegated_tool_names
    if authenticated_source:
        normalized["authenticated_source"] = True
    if delegation_target_unresolved:
        normalized["delegation_target_unresolved"] = True
    if source_systems:
        normalized["source_systems"] = source_systems
    return normalized


def approval_context_requires_tracked_lineage(value: dict[str, Any] | None) -> bool:
    if not isinstance(value, dict):
        return False
    if bool(value.get("accepts_secret_refs", False)):
        return True
    if bool(value.get("authenticated_source", False)):
        return True
    if bool(value.get("delegation_target_unresolved", False)):
        return True
    if _normalize_string_list(value.get("delegated_specialists")):
        return True
    boundaries = {
        str(boundary)
        for boundary in value.get("execution_boundaries", [])
        if isinstance(boundary, str)
    }
    if boundaries & {
        "authenticated_external_source",
        "delegation",
        "external_mcp",
        "secret_injection",
        "secret_management",
        "secret_read",
    }:
        return True
    return str(value.get("risk_level") or "") == "high"


def _delegate_step_approval_context(
    workflow: Workflow,
    step: Any,
    workflow_inputs: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    from src.tools.delegate_task_tool import infer_delegation_approval_context

    if canonical_tool_name(getattr(step, "tool", "")) != "delegate_task":
        return None
    raw_arguments = getattr(step, "arguments", None)
    if not isinstance(raw_arguments, dict):
        return infer_delegation_approval_context(None, None)
    render_context = {"inputs": dict(workflow_inputs or {})}
    render_context.update(workflow_inputs or {})
    try:
        rendered_arguments = _render_value(raw_arguments, render_context)
    except KeyError:
        rendered_arguments = raw_arguments
    if not isinstance(rendered_arguments, dict):
        return infer_delegation_approval_context(None, None)

    def _resolved_string(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        if "{{" in value and "}}" in value:
            return None
        stripped = value.strip()
        return stripped or None

    return infer_delegation_approval_context(
        _resolved_string(rendered_arguments.get("task")),
        _resolved_string(rendered_arguments.get("specialist")),
    )


def _policy_modes_for_approval_context(approval_context: dict[str, Any]) -> list[str]:
    if bool(approval_context.get("delegation_target_unresolved", False)):
        return ["full"]
    if bool(approval_context.get("authenticated_source", False)):
        return ["full"]
    if bool(approval_context.get("accepts_secret_refs", False)):
        return ["full"]
    boundaries = {
        str(boundary)
        for boundary in approval_context.get("execution_boundaries", [])
        if isinstance(boundary, str)
    }
    if boundaries & {
        "authenticated_external_source",
        "container_process_execution",
        "container_process_management",
        "external_mcp",
        "sandbox_execution",
        "secret_injection",
        "secret_read",
    }:
        return ["full"]
    if approval_context.get("risk_level") == "high":
        return ["full"]
    if approval_context.get("risk_level") == "medium":
        return ["balanced", "full"]
    return ["safe", "balanced", "full"]


def _checkpoint_context_allowed(approval_context: dict[str, Any] | None) -> bool:
    if approval_context is None:
        return True
    if bool(approval_context.get("accepts_secret_refs", False)):
        return False
    if bool(approval_context.get("authenticated_source", False)):
        return False
    if bool(approval_context.get("delegation_target_unresolved", False)):
        return False
    boundaries = {
        str(boundary)
        for boundary in approval_context.get("execution_boundaries", [])
        if isinstance(boundary, str)
    }
    return not bool(
        boundaries & {"secret_management", "secret_read", "secret_injection", "authenticated_external_source"}
    )


async def _load_workflow_checkpoint_payload(run_identity: str) -> dict[str, Any] | None:
    try:
        session_id, tool_name, run_fingerprint, run_discriminator = parse_workflow_run_identity(run_identity)
    except ValueError:
        return None
    async with get_session() as db:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.tool_name == tool_name)
            .where(AuditEvent.event_type.in_(("tool_result", "tool_failed")))
            .order_by(col(AuditEvent.created_at).desc())
        )
        if session_id is None:
            stmt = stmt.where(col(AuditEvent.session_id).is_(None))
        else:
            stmt = stmt.where(AuditEvent.session_id == session_id)
        result = await db.execute(stmt)
        events = result.scalars().all()
    matching_by_fingerprint: list[dict[str, Any]] = []
    for event in events:
        if not event.details_json:
            continue
        try:
            details = json.loads(event.details_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(details, dict):
            continue
        if str(details.get("run_fingerprint") or "none") != run_fingerprint:
            continue
        matching_by_fingerprint.append(details)
        if (
            run_discriminator is None
            or str(details.get("call_event_id") or "").strip() == run_discriminator
        ):
            return details
    if run_discriminator is not None and len(matching_by_fingerprint) == 1:
        return matching_by_fingerprint[0]
    return None


def _approval_context_for_workflow(
    workflow: Workflow,
    tools_by_name: dict[str, Any] | None = None,
    workflow_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_step_tools = _canonicalize_tool_names(workflow.step_tools)
    execution_boundaries: list[str] = []
    accepts_secret_refs = False
    authenticated_source = False
    source_systems: list[dict[str, Any]] = []
    delegated_specialists: list[str] = []
    delegated_tool_names: list[str] = []
    delegation_target_unresolved = False
    risk_level = "low"
    for tool_name in workflow.step_tools:
        canonical_name = canonical_tool_name(tool_name)
        runtime_tool = None
        if isinstance(tools_by_name, dict):
            runtime_tool = tools_by_name.get(tool_name) or tools_by_name.get(canonical_name)
        source_context = get_tool_source_context(runtime_tool)
        is_mcp = canonical_name.startswith("mcp_")
        accepts_secret_refs = accepts_secret_refs or tool_accepts_secret_refs(
            canonical_name,
            is_mcp=is_mcp,
            tool=runtime_tool,
        )
        if canonical_name.startswith("mcp_"):
            if "external_mcp" not in execution_boundaries:
                execution_boundaries.append("external_mcp")
            if isinstance(source_context, dict) and bool(source_context.get("authenticated_source")):
                authenticated_source = True
                if "authenticated_external_source" not in execution_boundaries:
                    execution_boundaries.append("authenticated_external_source")
                source_systems.append(
                    {
                        "server_name": str(source_context.get("server_name") or ""),
                        "hostname": str(source_context.get("hostname") or ""),
                        "source": str(source_context.get("source") or "manual"),
                        "authenticated_source": True,
                        "credential_sources": _normalize_string_list(source_context.get("credential_sources")),
                    }
                )
            continue
        tool_meta = TOOL_METADATA.get(canonical_name, {})
        for boundary in tool_meta.get("execution_boundaries", []):
            if boundary not in execution_boundaries:
                execution_boundaries.append(boundary)
        if bool(tool_meta.get("accepts_secret_refs", False)):
            accepts_secret_refs = True
    if any(tool_name.startswith("mcp_") for tool_name in canonical_step_tools):
        risk_level = "high"
    elif any(
        tool_name in {"write_file", "update_goal", "update_soul", "store_secret", "delete_secret"}
        for tool_name in canonical_step_tools
    ):
        risk_level = "medium"
    elif any(tool_name in {"execute_code", "get_secret"} for tool_name in canonical_step_tools):
        risk_level = "high"
    steps = getattr(workflow, "steps", None)
    if isinstance(steps, list):
        for step in steps:
            delegate_context = _delegate_step_approval_context(workflow, step, workflow_inputs)
            if not isinstance(delegate_context, dict):
                continue
            delegated_specialist = delegate_context.get("delegated_specialist")
            if isinstance(delegated_specialist, str) and delegated_specialist:
                delegated_specialists.append(delegated_specialist)
            for delegated_tool_name in delegate_context.get("delegated_tool_names", []):
                if isinstance(delegated_tool_name, str) and delegated_tool_name:
                    delegated_tool_names.append(delegated_tool_name)
            if bool(delegate_context.get("delegation_target_unresolved", False)):
                delegation_target_unresolved = True
            risk_level = _max_risk_level(risk_level, str(delegate_context.get("risk_level") or "high"))
            accepts_secret_refs = accepts_secret_refs or bool(delegate_context.get("accepts_secret_refs", False))
            authenticated_source = authenticated_source or bool(delegate_context.get("authenticated_source", False))
            for boundary in delegate_context.get("execution_boundaries", []):
                if isinstance(boundary, str) and boundary not in execution_boundaries:
                    execution_boundaries.append(boundary)
            _append_unique_source_systems(
                source_systems,
                list(delegate_context.get("source_systems", [])),
            )
    return {
        "workflow_name": workflow.name,
        "risk_level": risk_level,
        "execution_boundaries": sorted(dict.fromkeys(execution_boundaries or ["unknown"])),
        "accepts_secret_refs": accepts_secret_refs,
        "authenticated_source": authenticated_source,
        "source_systems": source_systems,
        "delegated_specialists": sorted(dict.fromkeys(delegated_specialists)),
        "delegated_tool_names": sorted(dict.fromkeys(delegated_tool_names)),
        "delegation_target_unresolved": delegation_target_unresolved,
        "step_tools": sorted(dict.fromkeys(canonical_step_tools)),
    }


class WorkflowTool(Tool):
    """Dynamic Tool wrapper that executes a reusable workflow definition."""

    skip_forward_signature_validation = True

    def __init__(self, workflow: Workflow, tools_by_name: dict[str, Tool]):
        super().__init__()
        self.workflow = workflow
        self.tools_by_name = tools_by_name
        self.name = workflow.tool_name
        self.description = workflow.description
        self.inputs = {
            input_name: {
                "type": str(spec.get("type", "string")),
                "description": str(spec.get("description", "")),
                "nullable": not bool(spec.get("required", True)),
            }
            for input_name, spec in workflow.inputs.items()
        }
        self.inputs.update(_WORKFLOW_CONTROL_INPUTS)
        self.output_type = "string"
        self.is_initialized = True
        self._last_audit_payload: tuple[str, dict[str, Any]] | None = None
        self._last_audit_failure_payload: tuple[str, dict[str, Any]] | None = None

    def forward(self, *args, **kwargs):
        return self.__call__(*args, **kwargs)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        self._last_audit_payload = None
        self._last_audit_failure_payload = None
        workflow_inputs, control_inputs = self._normalize_inputs(args, kwargs)
        audit_arguments = {**workflow_inputs, **control_inputs}
        approval_context = self.get_approval_context(workflow_inputs)
        run_fingerprint = fingerprint_tool_call(
            self.name,
            audit_arguments,
            approval_context=approval_context,
        )
        current_session_id = get_current_session_id()
        context: dict[str, Any] = {
            "inputs": workflow_inputs,
            "steps": {},
            "last_result": "",
        }
        context.update(workflow_inputs)
        continued_error_steps: list[str] = []
        artifact_paths = _collect_artifact_paths(workflow_inputs)
        step_records: list[dict[str, Any]] = []
        canonical_step_tools = [canonical_tool_name(step.tool) for step in self.workflow.steps]
        checkpoint_context_allowed = _checkpoint_context_allowed(approval_context)
        checkpoint_context: dict[str, dict[str, Any]] = {}
        start_index = 0
        if control_inputs.get("_seraph_resume_from_step"):
            start_index, restored_step_records, restored_artifact_paths, restored_context = self._restore_checkpoint_context(
                control_inputs=control_inputs,
                canonical_step_tools=canonical_step_tools,
                approval_context=approval_context,
            )
            for step_id, state in restored_context.items():
                context["steps"][step_id] = state
                context["last_result"] = state.get("result", "")
                checkpoint_context[step_id] = _json_safe_value(state)
            step_records.extend(restored_step_records)
            for path in restored_artifact_paths:
                if path not in artifact_paths:
                    artifact_paths.append(path)

        for index, (step, canonical_step_tool) in enumerate(zip(self.workflow.steps, canonical_step_tools, strict=False)):
            if index < start_index:
                continue
            tool = self.tools_by_name.get(step.tool)
            if tool is None:
                tool = self.tools_by_name.get(canonical_step_tool)
            if tool is None:
                raise RuntimeError(
                    f"Workflow '{self.workflow.name}' requires unavailable tool '{step.tool}'"
                )
            rendered_arguments = _render_value(step.arguments, context)
            step_artifact_paths = _collect_artifact_paths(rendered_arguments)
            step_status = "succeeded"
            error_kind: str | None = None
            error_summary: str | None = None
            step_started_at = _utc_now_iso()
            started = time.perf_counter()
            try:
                result = tool(
                    **rendered_arguments,
                    sanitize_inputs_outputs=sanitize_inputs_outputs,
                )
            except Exception as exc:
                step_completed_at = _utc_now_iso()
                duration_ms = int((time.perf_counter() - started) * 1000)
                if not step.continue_on_error:
                    step_records.append({
                        "id": step.id,
                        "index": len(step_records) + 1,
                        "tool": canonical_step_tool,
                        "status": "failed",
                        "argument_keys": (
                            sorted(str(key) for key in rendered_arguments.keys())
                            if isinstance(rendered_arguments, dict)
                            else []
                        ),
                        "artifact_paths": step_artifact_paths,
                        "result_summary": None,
                        "error_kind": type(exc).__name__,
                        "error_summary": str(exc).strip()[:160] or type(exc).__name__,
                        "started_at": step_started_at,
                        "completed_at": step_completed_at,
                        "duration_ms": duration_ms,
                    })
                    self._last_audit_failure_payload = self._build_audit_payload(
                        status="failed",
                        run_fingerprint=run_fingerprint,
                        approval_context=approval_context,
                        canonical_step_tools=canonical_step_tools,
                        step_records=step_records,
                        artifact_paths=artifact_paths,
                        continued_error_steps=continued_error_steps,
                        canvas_output=None,
                        checkpoint_context=checkpoint_context,
                        checkpoint_context_allowed=checkpoint_context_allowed,
                        control_inputs=control_inputs,
                        error=str(exc),
                    )
                    raise
                result = f"Error: {exc}"
                continued_error_steps.append(step.id)
                step_status = "continued_error"
                error_kind = type(exc).__name__
                error_summary = str(exc).strip()[:160] or error_kind
                step_completed_at = _utc_now_iso()
                duration_ms = int((time.perf_counter() - started) * 1000)
            else:
                step_completed_at = _utc_now_iso()
                duration_ms = int((time.perf_counter() - started) * 1000)
            context["steps"][step.id] = {
                "tool": canonical_step_tool,
                "arguments": rendered_arguments,
                "result": result,
            }
            context["last_result"] = result
            checkpoint_context[step.id] = _json_safe_value(context["steps"][step.id])
            for path in step_artifact_paths:
                if path not in artifact_paths:
                    artifact_paths.append(path)
            step_records.append({
                "id": step.id,
                "index": len(step_records) + 1,
                "tool": canonical_step_tool,
                "status": step_status,
                "argument_keys": (
                    sorted(str(key) for key in rendered_arguments.keys())
                    if isinstance(rendered_arguments, dict)
                    else []
                ),
                "artifact_paths": step_artifact_paths,
                "result_summary": _summarize_value_shape(result),
                "error_kind": error_kind,
                "error_summary": error_summary,
                "started_at": step_started_at,
                "completed_at": step_completed_at,
                "duration_ms": duration_ms,
            })

        result_text = ""
        if self.workflow.result_template:
            rendered = _render_value(self.workflow.result_template, context)
            result_text = str(rendered)
        else:
            result_text = _summarize_workflow_result(self.workflow, context["steps"])
        canvas_output = _build_canvas_output(
            self.workflow,
            result_text=result_text,
            step_records=step_records,
            artifact_paths=artifact_paths,
        )

        status = "degraded" if continued_error_steps else "succeeded"
        summary = f"{self.name} {status} ({len(self.workflow.steps)} steps)"
        if continued_error_steps:
            summary += f" with {len(continued_error_steps)} continued error step"
            if len(continued_error_steps) != 1:
                summary += "s"
        self._last_audit_payload = self._build_audit_payload(
            status=status,
            run_fingerprint=run_fingerprint,
            approval_context=approval_context,
            canonical_step_tools=canonical_step_tools,
            step_records=step_records,
            artifact_paths=artifact_paths,
            continued_error_steps=continued_error_steps,
            canvas_output=canvas_output,
            checkpoint_context=checkpoint_context,
            checkpoint_context_allowed=checkpoint_context_allowed,
            control_inputs=control_inputs,
            summary=summary,
        )
        if current_session_id:
            flush_session_memory_sync(
                session_id=current_session_id,
                trigger="workflow_completed",
                workflow_name=self.workflow.name,
            )
        return result_text

    def get_audit_result_payload(
        self,
        _arguments: dict[str, Any],
        _result: Any,
    ) -> tuple[str, dict[str, Any]] | None:
        return self._last_audit_payload

    def get_audit_failure_payload(
        self,
        _arguments: dict[str, Any],
        _error: Exception,
    ) -> tuple[str, dict[str, Any]] | None:
        return self._last_audit_failure_payload

    def get_audit_call_payload(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        workflow_inputs, control_inputs = self._normalize_provided_inputs(
            arguments,
            require_required_inputs=False,
        )
        normalized_audit_arguments = {**workflow_inputs, **control_inputs}
        approval_context = self.get_approval_context(workflow_inputs)
        return (
            format_tool_call_summary(self.name, arguments, set()),
            {
                "arguments": redact_for_audit(arguments),
                "workflow_name": self.workflow.name,
                "run_fingerprint": fingerprint_tool_call(
                    self.name,
                    normalized_audit_arguments,
                    approval_context=approval_context,
                ),
                "approval_context": approval_context,
                **self._control_audit_details(control_inputs),
            },
        )

    def get_approval_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _approval_context_for_workflow(self.workflow, self.tools_by_name, arguments)

    def _control_audit_details(self, control_inputs: dict[str, Any]) -> dict[str, Any]:
        details: dict[str, Any] = {}
        if isinstance(control_inputs.get("_seraph_parent_run_identity"), str):
            details["parent_run_identity"] = control_inputs["_seraph_parent_run_identity"]
        if isinstance(control_inputs.get("_seraph_root_run_identity"), str):
            details["root_run_identity"] = control_inputs["_seraph_root_run_identity"]
        if isinstance(control_inputs.get("_seraph_branch_kind"), str):
            details["branch_kind"] = control_inputs["_seraph_branch_kind"]
        if isinstance(control_inputs.get("_seraph_resume_from_step"), str):
            details["resume_from_step"] = control_inputs["_seraph_resume_from_step"]
        if isinstance(control_inputs.get("_seraph_branch_depth"), int):
            details["branch_depth"] = control_inputs["_seraph_branch_depth"]
        return details

    def _build_audit_payload(
        self,
        *,
        status: str,
        run_fingerprint: str,
        approval_context: dict[str, Any],
        canonical_step_tools: list[str],
        step_records: list[dict[str, Any]],
        artifact_paths: list[str],
        continued_error_steps: list[str],
        canvas_output: dict[str, Any] | None,
        checkpoint_context: dict[str, dict[str, Any]],
        checkpoint_context_allowed: bool,
        control_inputs: dict[str, Any],
        summary: str | None = None,
        error: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        payload_summary = summary or f"{self.name} {status}"
        payload = {
            "workflow_name": self.workflow.name,
            "run_fingerprint": run_fingerprint,
            "approval_context": approval_context,
            "step_count": len(self.workflow.steps),
            "step_tools": canonical_step_tools,
            "step_records": step_records,
            "checkpoint_step_ids": [str(step["id"]) for step in step_records if step.get("id")],
            "last_completed_step_id": (
                next(
                    (
                        str(step["id"])
                        for step in reversed(step_records)
                        if step.get("id") and str(step.get("status") or "") not in {"failed", "continued_error"}
                    ),
                    None,
                )
            ),
            "artifact_paths": artifact_paths,
            "continued_error_steps": continued_error_steps,
            "failed_step_ids": [
                str(step["id"])
                for step in step_records
                if str(step.get("status") or "") in {"failed", "continued_error"}
            ],
            "runtime_profile": self.workflow.runtime_profile,
            "output_surface": self.workflow.output_surface,
            "canvas_output": _redact_canvas_output(canvas_output),
            "content_redacted": True,
            "checkpoint_context_available": checkpoint_context_allowed and bool(checkpoint_context),
            **self._control_audit_details(control_inputs),
        }
        if checkpoint_context_allowed and checkpoint_context:
            payload["checkpoint_context"] = checkpoint_context
        if error is not None:
            payload["error"] = redact_for_audit(error)
        return payload_summary, payload

    def _restore_checkpoint_context(
        self,
        *,
        control_inputs: dict[str, Any],
        canonical_step_tools: list[str],
        approval_context: dict[str, Any],
    ) -> tuple[int, list[dict[str, Any]], list[str], dict[str, dict[str, Any]]]:
        requested_step_id = str(control_inputs.get("_seraph_resume_from_step") or "").strip()
        if not requested_step_id:
            return 0, [], [], {}
        step_ids = [step.id for step in self.workflow.steps]
        if requested_step_id not in step_ids:
            raise ValueError(
                f"Workflow '{self.workflow.name}' has no step '{requested_step_id}'"
            )
        start_index = step_ids.index(requested_step_id)
        if start_index == 0:
            return 0, [], [], {}
        parent_run_identity = str(control_inputs.get("_seraph_parent_run_identity") or "").strip()
        if not parent_run_identity:
            raise RuntimeError(
                f"Workflow '{self.workflow.name}' requires a parent run identity to resume from step '{requested_step_id}'"
            )
        details = _run_async(_load_workflow_checkpoint_payload(parent_run_identity))
        if not isinstance(details, dict):
            raise RuntimeError(
                f"Workflow '{self.workflow.name}' could not load checkpoint state from '{parent_run_identity}'"
            )
        if str(details.get("workflow_name") or self.workflow.name) != self.workflow.name:
            raise RuntimeError(
                f"Workflow '{self.workflow.name}' cannot reuse checkpoint state from a different workflow"
            )
        recorded_approval_context = normalize_workflow_approval_context(
            details.get("approval_context"),
            workflow_name=self.workflow.name,
        )
        current_approval_context = normalize_workflow_approval_context(
            approval_context,
            workflow_name=self.workflow.name,
        )
        if (
            recorded_approval_context is not None
            and recorded_approval_context != current_approval_context
        ):
            raise RuntimeError(
                f"Workflow '{self.workflow.name}' cannot resume from step '{requested_step_id}' "
                "because the parent run changed its trust boundary"
            )
        if (
            recorded_approval_context is None
            and approval_context_requires_tracked_lineage(current_approval_context)
        ):
            raise RuntimeError(
                f"Workflow '{self.workflow.name}' cannot resume from step '{requested_step_id}' "
                "because the parent run predates trust-boundary tracking for the current workflow surface"
            )
        raw_checkpoint_context = details.get("checkpoint_context")
        if not isinstance(raw_checkpoint_context, dict):
            raise RuntimeError(
                f"Workflow '{self.workflow.name}' cannot resume from step '{requested_step_id}' because the parent run has no reusable checkpoint context"
            )
        parent_step_records = details.get("step_records")
        parent_step_records = parent_step_records if isinstance(parent_step_records, list) else []
        restored_step_records: list[dict[str, Any]] = []
        restored_artifact_paths: list[str] = []
        restored_context: dict[str, dict[str, Any]] = {}
        for index, step in enumerate(self.workflow.steps[:start_index], start=1):
            raw_state = raw_checkpoint_context.get(step.id)
            if not isinstance(raw_state, dict):
                raise RuntimeError(
                    f"Workflow '{self.workflow.name}' is missing checkpoint state for step '{step.id}'"
                )
            step_arguments = raw_state.get("arguments")
            if not isinstance(step_arguments, dict):
                step_arguments = {}
            restored_state = {
                "tool": str(raw_state.get("tool") or canonical_step_tools[index - 1]),
                "arguments": step_arguments,
                "result": raw_state.get("result"),
            }
            restored_context[step.id] = restored_state
            parent_step_record = next(
                (
                    item for item in parent_step_records
                    if isinstance(item, dict) and str(item.get("id") or "") == step.id
                ),
                {},
            )
            step_artifact_paths = [
                path for path in parent_step_record.get("artifact_paths", [])
                if isinstance(path, str) and path.strip()
            ] if isinstance(parent_step_record, dict) else []
            for path in _collect_artifact_paths(step_arguments):
                if path not in step_artifact_paths:
                    step_artifact_paths.append(path)
            for path in step_artifact_paths:
                if path not in restored_artifact_paths:
                    restored_artifact_paths.append(path)
            restored_step_records.append({
                "id": step.id,
                "index": len(restored_step_records) + 1,
                "tool": canonical_step_tools[index - 1],
                "status": "checkpoint_reused",
                "argument_keys": sorted(str(key) for key in step_arguments.keys()),
                "artifact_paths": step_artifact_paths,
                "result_summary": _summarize_value_shape(restored_state["result"]),
                "error_kind": None,
                "error_summary": None,
                "started_at": parent_step_record.get("started_at") if isinstance(parent_step_record, dict) else None,
                "completed_at": parent_step_record.get("completed_at") if isinstance(parent_step_record, dict) else None,
                "duration_ms": parent_step_record.get("duration_ms") if isinstance(parent_step_record, dict) else None,
                "reused_from_run_identity": parent_run_identity,
                "source_step_status": (
                    parent_step_record.get("status")
                    if isinstance(parent_step_record, dict)
                    else None
                ),
            })
        return start_index, restored_step_records, restored_artifact_paths, restored_context

    def _split_inputs(self, provided: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        workflow_inputs = {
            key: value
            for key, value in provided.items()
            if key in self.workflow.inputs
        }
        control_inputs: dict[str, Any] = {}
        for key in _WORKFLOW_CONTROL_FIELD_NAMES:
            if key not in provided:
                continue
            value = provided[key]
            if value is None or value == "":
                continue
            if key == "_seraph_branch_depth":
                try:
                    control_inputs[key] = int(value)
                except (TypeError, ValueError):
                    continue
            else:
                control_inputs[key] = str(value)
        return workflow_inputs, control_inputs

    def _normalize_inputs(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            provided = dict(args[0])
        elif kwargs:
            provided = dict(kwargs)
        else:
            input_names = list(self.workflow.inputs.keys())
            provided = {
                name: args[idx]
                for idx, name in enumerate(input_names)
                if idx < len(args)
            }
        return self._normalize_provided_inputs(provided)

    def _normalize_provided_inputs(
        self,
        provided: dict[str, Any],
        *,
        require_required_inputs: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        provided_workflow_inputs, control_inputs = self._split_inputs(provided)
        normalized: dict[str, Any] = {}
        for input_name, spec in self.workflow.inputs.items():
            if input_name in provided_workflow_inputs:
                normalized[input_name] = provided_workflow_inputs[input_name]
                continue
            if "default" in spec and spec["default"] is not None:
                normalized[input_name] = spec["default"]
                continue
            if require_required_inputs and spec.get("required", True):
                raise ValueError(
                    f"Workflow '{self.workflow.name}' missing required input '{input_name}'"
                )
        return normalized, control_inputs


class WorkflowManager:
    def __init__(self) -> None:
        self._workflows: list[Workflow] = []
        self._load_errors: list[dict[str, str]] = []
        self._shared_manifest_errors: list[dict[str, str]] = []
        self._workflows_dir: str = ""
        self._manifest_roots: list[str] = []
        self._config_path: str = ""
        self._disabled: set[str] = set()
        self._registry: ExtensionRegistry | None = None

    def init(self, workflows_dir: str, *, manifest_roots: list[str] | None = None) -> None:
        self._workflows_dir = workflows_dir
        self._manifest_roots = list(manifest_roots or [os.path.join(os.path.dirname(workflows_dir), "extensions")])
        self._config_path = os.path.join(
            os.path.dirname(workflows_dir),
            "workflows-config.json",
        )
        self._registry = ExtensionRegistry(
            manifest_roots=self._manifest_roots,
            skill_dirs=[],
            workflow_dirs=[workflows_dir],
            mcp_runtime=None,
        )
        self._load_config()
        self._reload_from_registry()
        self._apply_disabled()
        logger.info(
            "WorkflowManager initialized: %d workflows loaded",
            len(self._workflows),
        )

    def _reload_from_registry(self) -> None:
        snapshot = self._snapshot()
        runtime_defaults_by_name: dict[str, str] = {}
        canvas_metadata_by_name: dict[str, dict[str, Any]] = {}
        for contribution in snapshot.list_contributions("workflow_runtimes"):
            if isinstance(contribution.metadata.get("registry_conflict"), dict):
                continue
            name = contribution.metadata.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            default_output_surface = contribution.metadata.get("default_output_surface")
            if isinstance(default_output_surface, str) and default_output_surface.strip():
                runtime_defaults_by_name[name.strip()] = default_output_surface.strip()
        for contribution in snapshot.list_contributions("canvas_outputs"):
            if isinstance(contribution.metadata.get("registry_conflict"), dict):
                continue
            name = contribution.metadata.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            raw_sections = contribution.metadata.get("sections")
            raw_artifact_types = contribution.metadata.get("artifact_types")
            canvas_metadata_by_name[name.strip()] = {
                "title": str(contribution.metadata.get("title") or ""),
                "sections": [
                    str(item).strip()
                    for item in raw_sections
                    if isinstance(item, str) and item.strip()
                ] if isinstance(raw_sections, list) else [],
                "artifact_types": [
                    str(item).strip()
                    for item in raw_artifact_types
                    if isinstance(item, str) and item.strip()
                ] if isinstance(raw_artifact_types, list) else [],
            }
        contribution_paths: list[str] = []
        contribution_index: dict[str, tuple[str, str | None, int]] = {}
        for contribution in snapshot.list_contributions("workflows"):
            resolved_path = contribution.metadata.get("resolved_path")
            path = str(resolved_path) if isinstance(resolved_path, str) and resolved_path else contribution.reference
            normalized_path = os.path.abspath(path)
            contribution_paths.append(path)
            contribution_index[normalized_path] = (
                contribution.source,
                contribution.extension_id,
                int(contribution.metadata.get("manifest_root_index", len(self._manifest_roots))),
            )

        workflows, parse_errors = scan_workflow_paths(contribution_paths)
        manifest_priority_by_path: dict[str, int] = {}
        for workflow in workflows:
            source, extension_id, manifest_root_index = contribution_index.get(
                os.path.abspath(workflow.file_path),
                ("legacy", None, len(self._manifest_roots)),
            )
            workflow.source = source
            workflow.extension_id = extension_id
            if not workflow.output_surface and workflow.runtime_profile:
                workflow.output_surface = runtime_defaults_by_name.get(workflow.runtime_profile, "")
            if workflow.output_surface:
                canvas_metadata = canvas_metadata_by_name.get(workflow.output_surface, {})
                workflow.output_surface_title = str(canvas_metadata.get("title") or "")
                workflow.output_surface_sections = list(canvas_metadata.get("sections") or [])
                workflow.output_surface_artifact_types = list(canvas_metadata.get("artifact_types") or [])
            manifest_priority_by_path[os.path.abspath(workflow.file_path)] = manifest_root_index

        load_errors: list[dict[str, str]] = []
        shared_manifest_errors: list[dict[str, str]] = []
        for error in snapshot.load_errors:
            payload = {
                "file_path": error.source,
                "message": error.message,
                "phase": error.phase,
            }
            if self._error_affects_workflows(error.source, error.phase, error.details):
                load_errors.append(payload)
                continue
            if error.phase in {"manifest", "compatibility", "layout"}:
                shared_manifest_errors.append(payload)
        for error in parse_errors:
            path = str(error.get("file_path") or "")
            source = contribution_index.get(
                os.path.abspath(path),
                ("legacy", None, len(self._manifest_roots)),
            )[0]
            load_errors.append(
                {
                    "file_path": path,
                    "message": str(error.get("message") or "workflow parse error"),
                    "phase": "manifest-workflows" if source == "manifest" else "legacy-workflows",
                }
            )

        deduped_workflows: list[Workflow] = []
        by_name: dict[str, Workflow] = {}
        by_tool_name: dict[str, Workflow] = {}
        for workflow in sorted(
            workflows,
            key=lambda item: (
                0 if item.source == "manifest" else 1,
                manifest_priority_by_path.get(os.path.abspath(item.file_path), len(self._manifest_roots)),
                item.file_path,
            ),
        ):
            existing_name = by_name.get(workflow.name)
            if existing_name is not None:
                load_errors.append(
                    {
                        "file_path": workflow.file_path,
                        "message": (
                            f"Duplicate workflow name '{workflow.name}' from {workflow.file_path}; "
                            f"keeping {existing_name.file_path}"
                        ),
                        "phase": "duplicate-workflow-name",
                    }
                )
                continue
            existing_tool = by_tool_name.get(workflow.tool_name)
            if existing_tool is not None:
                load_errors.append(
                    {
                        "file_path": workflow.file_path,
                        "message": (
                            f"Duplicate workflow tool '{workflow.tool_name}' from {workflow.file_path}; "
                            f"keeping {existing_tool.file_path}"
                        ),
                        "phase": "duplicate-workflow-tool-name",
                    }
                )
                continue
            by_name[workflow.name] = workflow
            by_tool_name[workflow.tool_name] = workflow
            deduped_workflows.append(workflow)

        self._workflows = deduped_workflows
        self._load_errors = load_errors
        self._shared_manifest_errors = shared_manifest_errors

    def _snapshot(self) -> ExtensionRegistrySnapshot:
        if self._registry is None:
            self._registry = ExtensionRegistry(
                manifest_roots=self._manifest_roots,
                skill_dirs=[],
                workflow_dirs=[self._workflows_dir] if self._workflows_dir else [],
                mcp_runtime=None,
            )
        return self._registry.snapshot()

    def _error_affects_workflows(
        self,
        source: str,
        phase: str,
        details: list[dict[str, Any]] | None = None,
    ) -> bool:
        if phase == "legacy-workflows":
            return True
        if phase == "manifest":
            if details:
                for detail in details:
                    loc = detail.get("loc")
                    if (
                        isinstance(loc, list)
                        and len(loc) >= 2
                        and str(loc[0]) == "contributes"
                        and str(loc[1]) == "workflows"
                    ):
                        return True
                return False
        if phase in {"compatibility", "layout"}:
            for detail in details or []:
                contributed_types = detail.get("contributed_types")
                if isinstance(contributed_types, list) and "workflows" in contributed_types:
                    return True
        if phase not in {"manifest", "compatibility", "layout"}:
            return False
        package_root = source
        if os.path.basename(source) in {"manifest.yaml", "manifest.yml"}:
            package_root = os.path.dirname(source)
        return os.path.isdir(os.path.join(package_root, "workflows"))

    def _load_config(self) -> None:
        if os.path.isfile(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._disabled = set(data.get("disabled", []))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Failed to load workflows config: %s", exc)
                self._disabled = set()
        else:
            self._disabled = set()

    def _save_config(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump({"disabled": sorted(self._disabled)}, f, indent=2)
        except OSError as exc:
            logger.warning("Failed to save workflows config: %s", exc)

    def _apply_disabled(self) -> None:
        for workflow in self._workflows:
            if workflow.name in self._disabled:
                workflow.enabled = False

    def list_workflows(
        self,
        *,
        available_tool_names: list[str] | None = None,
        active_skill_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        snapshot = self._snapshot()
        extensions_by_id = {extension.id: extension for extension in snapshot.extensions}
        available_runtime_profiles = {
            str(item.metadata.get("name"))
            for item in snapshot.list_contributions("workflow_runtimes")
            if not isinstance(item.metadata.get("registry_conflict"), dict)
            if isinstance(item.metadata.get("name"), str) and str(item.metadata.get("name")).strip()
        }
        available_output_surfaces = {
            str(item.metadata.get("name"))
            for item in snapshot.list_contributions("canvas_outputs")
            if not isinstance(item.metadata.get("registry_conflict"), dict)
            if isinstance(item.metadata.get("name"), str) and str(item.metadata.get("name")).strip()
        }
        workflows: list[dict[str, Any]] = []
        for workflow in self._workflows:
            permission_profile = evaluate_tool_permissions(
                extensions_by_id.get(workflow.extension_id) if workflow.extension_id else None,
                tool_names=workflow.step_tools,
            )
            item = {
                "name": workflow.name,
                "tool_name": workflow.tool_name,
                "description": workflow.description,
                "inputs": workflow.inputs,
                "requires_tools": _canonicalize_tool_names(workflow.requires_tools),
                "requires_skills": workflow.requires_skills,
                "user_invocable": workflow.user_invocable,
                "enabled": workflow.enabled,
                "step_count": len(workflow.steps),
                "file_path": workflow.file_path,
                "source": workflow.source,
                "extension_id": workflow.extension_id,
                "runtime_profile": workflow.runtime_profile,
                "output_surface": workflow.output_surface,
                "output_surface_title": workflow.output_surface_title,
                "output_surface_sections": list(workflow.output_surface_sections),
                "output_surface_artifact_types": list(workflow.output_surface_artifact_types),
                "policy_modes": self._infer_policy_modes(workflow),
                "execution_boundaries": self._infer_execution_boundaries(workflow),
                "risk_level": self._infer_risk_level(workflow),
                "accepts_secret_refs": self._accepts_secret_refs(workflow),
                "permission_status": permission_profile["status"],
                "missing_manifest_tools": list(permission_profile["missing_tools"]),
                "missing_manifest_execution_boundaries": list(permission_profile["missing_execution_boundaries"]),
                "requires_network": bool(permission_profile["requires_network"]),
                "missing_manifest_network": bool(permission_profile["missing_network"]),
                "approval_behavior": permission_profile["approval_behavior"],
                "requires_approval": bool(permission_profile["requires_approval"]),
            }
            if available_tool_names is not None and active_skill_names is not None:
                item.update(
                    self._get_runtime_availability(
                        workflow,
                        available_tool_names,
                        active_skill_names,
                        available_runtime_profiles=available_runtime_profiles,
                        available_output_surfaces=available_output_surfaces,
                        permission_profile=permission_profile,
                    )
                )
            workflows.append(item)
        return workflows

    def get_workflow(self, name: str) -> Workflow | None:
        for workflow in self._workflows:
            if workflow.name == name:
                return workflow
        return None

    def get_workflow_by_tool_name(self, tool_name: str) -> Workflow | None:
        for workflow in self._workflows:
            if workflow.tool_name == tool_name:
                return workflow
        return None

    def enable(self, name: str) -> bool:
        workflow = self.get_workflow(name)
        if workflow is None:
            return False
        workflow.enabled = True
        self._disabled.discard(name)
        self._save_config()
        return True

    def disable(self, name: str) -> bool:
        workflow = self.get_workflow(name)
        if workflow is None:
            return False
        workflow.enabled = False
        self._disabled.add(name)
        self._save_config()
        return True

    def reload(self) -> list[dict[str, Any]]:
        if self._workflows_dir:
            self._reload_from_registry()
            self._apply_disabled()
        return self.list_workflows()

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "workflows": self.list_workflows(),
            "load_errors": list(self._load_errors),
            "shared_manifest_errors": list(self._shared_manifest_errors),
            "loaded_count": len(self._workflows),
            "error_count": len(self._load_errors),
            "shared_error_count": len(self._shared_manifest_errors),
        }

    def get_active_workflows(
        self,
        available_tool_names: list[str],
        active_skill_names: list[str],
    ) -> list[Workflow]:
        tool_set = {canonical_tool_name(name) for name in available_tool_names}
        skill_set = set(active_skill_names)
        snapshot = self._snapshot()
        extensions_by_id = {extension.id: extension for extension in snapshot.extensions}
        available_runtime_profiles = {
            str(item.metadata.get("name"))
            for item in snapshot.list_contributions("workflow_runtimes")
            if not isinstance(item.metadata.get("registry_conflict"), dict)
            if isinstance(item.metadata.get("name"), str) and str(item.metadata.get("name")).strip()
        }
        available_output_surfaces = {
            str(item.metadata.get("name"))
            for item in snapshot.list_contributions("canvas_outputs")
            if not isinstance(item.metadata.get("registry_conflict"), dict)
            if isinstance(item.metadata.get("name"), str) and str(item.metadata.get("name")).strip()
        }
        result: list[Workflow] = []
        for workflow in self._workflows:
            if not workflow.enabled:
                continue
            permission_profile = evaluate_tool_permissions(
                extensions_by_id.get(workflow.extension_id) if workflow.extension_id else None,
                tool_names=workflow.step_tools,
            )
            availability = self._get_runtime_availability(
                workflow,
                list(tool_set),
                list(skill_set),
                available_runtime_profiles=available_runtime_profiles,
                available_output_surfaces=available_output_surfaces,
                permission_profile=permission_profile,
            )
            if not permission_profile["ok"] or not availability["is_available"]:
                continue
            result.append(workflow)
        return result

    def build_workflow_tools(
        self,
        available_tools: list[Tool],
        active_skill_names: list[str],
    ) -> list[Tool]:
        tools_by_name = {tool.name: tool for tool in available_tools}
        active_workflows = self.get_active_workflows(
            list(tools_by_name.keys()),
            active_skill_names,
        )
        return [
            WorkflowTool(workflow, tools_by_name)
            for workflow in active_workflows
        ]

    def get_tool_metadata(self, tool_name: str) -> dict[str, Any] | None:
        workflow = self.get_workflow_by_tool_name(tool_name)
        if workflow is None:
            return None
        approval_context = _approval_context_for_workflow(workflow)
        policy_modes = self._infer_policy_modes(workflow, approval_context)
        return {
            "description": workflow.description,
            "inputs": workflow.inputs,
            "runtime_profile": workflow.runtime_profile,
            "output_surface": workflow.output_surface,
            "output_surface_title": workflow.output_surface_title,
            "output_surface_sections": list(workflow.output_surface_sections),
            "output_surface_artifact_types": list(workflow.output_surface_artifact_types),
            "policy_modes": policy_modes,
            "requires_tools": _canonicalize_tool_names(workflow.requires_tools),
            "requires_skills": workflow.requires_skills,
            "step_count": len(workflow.steps),
            "execution_boundaries": self._infer_execution_boundaries(workflow, approval_context),
            "risk_level": self._infer_risk_level(workflow, approval_context),
            "accepts_secret_refs": self._accepts_secret_refs(workflow, approval_context),
            "approval_context": approval_context,
        }

    def _get_runtime_availability(
        self,
        workflow: Workflow,
        available_tool_names: list[str],
        active_skill_names: list[str],
        *,
        available_runtime_profiles: set[str] | None = None,
        available_output_surfaces: set[str] | None = None,
        permission_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool_set = {canonical_tool_name(name) for name in available_tool_names}
        skill_set = set(active_skill_names)
        runtime_profiles = available_runtime_profiles or set()
        output_surfaces = available_output_surfaces or set()
        required_runtime_tools = _canonicalize_tool_names(
            list(workflow.requires_tools) + list(workflow.step_tools)
        )
        missing_tools = [
            tool_name for tool_name in required_runtime_tools
            if tool_name not in tool_set
        ]
        missing_skills = [
            skill_name for skill_name in workflow.requires_skills
            if skill_name not in skill_set
        ]
        missing_runtime_profiles = (
            [workflow.runtime_profile]
            if workflow.runtime_profile and workflow.runtime_profile not in runtime_profiles
            else []
        )
        missing_output_surfaces = (
            [workflow.output_surface]
            if workflow.output_surface and workflow.output_surface not in output_surfaces
            else []
        )
        missing_manifest_tools = list((permission_profile or {}).get("missing_tools", []))
        missing_manifest_execution_boundaries = list(
            (permission_profile or {}).get("missing_execution_boundaries", [])
        )
        missing_manifest_network = bool((permission_profile or {}).get("missing_network", False))
        return {
            "is_available": (
                not missing_tools
                and not missing_skills
                and not missing_runtime_profiles
                and not missing_output_surfaces
                and not missing_manifest_tools
                and not missing_manifest_execution_boundaries
                and not missing_manifest_network
            ),
            "missing_tools": missing_tools,
            "missing_skills": missing_skills,
            "missing_runtime_profiles": missing_runtime_profiles,
            "missing_output_surfaces": missing_output_surfaces,
            "missing_manifest_tools": missing_manifest_tools,
            "missing_manifest_execution_boundaries": missing_manifest_execution_boundaries,
            "missing_manifest_network": missing_manifest_network,
        }

    def _infer_policy_modes(self, workflow: Workflow, approval_context: dict[str, Any] | None = None) -> list[str]:
        context = approval_context or _approval_context_for_workflow(workflow)
        return _policy_modes_for_approval_context(context)

    def _infer_execution_boundaries(
        self,
        workflow: Workflow,
        approval_context: dict[str, Any] | None = None,
    ) -> list[str]:
        context = approval_context or _approval_context_for_workflow(workflow)
        boundaries = context.get("execution_boundaries", [])
        return list(boundaries) if isinstance(boundaries, list) and boundaries else ["unknown"]

    def _infer_risk_level(self, workflow: Workflow, approval_context: dict[str, Any] | None = None) -> str:
        context = approval_context or _approval_context_for_workflow(workflow)
        if isinstance(context.get("risk_level"), str):
            return str(context["risk_level"])
        policy_modes = self._infer_policy_modes(workflow, context)
        if policy_modes == ["full"]:
            return "high"
        if policy_modes == ["balanced", "full"]:
            return "medium"
        return "low"

    def _accepts_secret_refs(self, workflow: Workflow, approval_context: dict[str, Any] | None = None) -> bool:
        context = approval_context or _approval_context_for_workflow(workflow)
        return bool(context.get("accepts_secret_refs", False))


workflow_manager = WorkflowManager()
