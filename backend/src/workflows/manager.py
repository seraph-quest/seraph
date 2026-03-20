"""Workflow manager and runtime."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import time
from typing import Any

from smolagents import Tool

from src.approval.repository import fingerprint_tool_call
from src.plugins.registry import TOOL_METADATA
from src.workflows.loader import Workflow, scan_workflows

logger = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"{{\s*([^}]+)\s*}}")


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
        lines.append(f"- {step.id} ({step.tool}): {result}")
    return "\n".join(lines)


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
        self.output_type = "string"
        self.is_initialized = True
        self._last_audit_payload: tuple[str, dict[str, Any]] | None = None

    def forward(self, *args, **kwargs):
        return self.__call__(*args, **kwargs)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        workflow_inputs = self._normalize_inputs(args, kwargs)
        run_fingerprint = fingerprint_tool_call(self.name, workflow_inputs)
        context: dict[str, Any] = {
            "inputs": workflow_inputs,
            "steps": {},
            "last_result": "",
        }
        context.update(workflow_inputs)
        continued_error_steps: list[str] = []
        artifact_paths = _collect_artifact_paths(workflow_inputs)
        step_records: list[dict[str, Any]] = []

        for step in self.workflow.steps:
            tool = self.tools_by_name.get(step.tool)
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
                if not step.continue_on_error:
                    raise
                result = f"Error: {exc}"
                continued_error_steps.append(step.id)
                step_status = "continued_error"
                error_kind = type(exc).__name__
                error_summary = str(exc).strip()[:160] or error_kind
            step_completed_at = _utc_now_iso()
            duration_ms = int((time.perf_counter() - started) * 1000)
            context["steps"][step.id] = {
                "tool": step.tool,
                "arguments": rendered_arguments,
                "result": result,
            }
            context["last_result"] = result
            for path in step_artifact_paths:
                if path not in artifact_paths:
                    artifact_paths.append(path)
            step_records.append({
                "id": step.id,
                "index": len(step_records) + 1,
                "tool": step.tool,
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

        status = "degraded" if continued_error_steps else "succeeded"
        summary = f"{self.name} {status} ({len(self.workflow.steps)} steps)"
        if continued_error_steps:
            summary += f" with {len(continued_error_steps)} continued error step"
            if len(continued_error_steps) != 1:
                summary += "s"
        self._last_audit_payload = (
            summary,
            {
                "workflow_name": self.workflow.name,
                "run_fingerprint": run_fingerprint,
                "step_count": len(self.workflow.steps),
                "step_tools": [step.tool for step in self.workflow.steps],
                "step_records": step_records,
                "artifact_paths": artifact_paths,
                "continued_error_steps": continued_error_steps,
                "failed_step_ids": continued_error_steps,
                "content_redacted": True,
            },
        )
        return result_text

    def get_audit_result_payload(
        self,
        _arguments: dict[str, Any],
        _result: Any,
    ) -> tuple[str, dict[str, Any]] | None:
        return self._last_audit_payload

    def _normalize_inputs(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
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

        normalized: dict[str, Any] = {}
        for input_name, spec in self.workflow.inputs.items():
            if input_name in provided:
                normalized[input_name] = provided[input_name]
                continue
            if "default" in spec and spec["default"] is not None:
                normalized[input_name] = spec["default"]
                continue
            if spec.get("required", True):
                raise ValueError(
                    f"Workflow '{self.workflow.name}' missing required input '{input_name}'"
                )
        return normalized


class WorkflowManager:
    def __init__(self) -> None:
        self._workflows: list[Workflow] = []
        self._load_errors: list[dict[str, str]] = []
        self._workflows_dir: str = ""
        self._config_path: str = ""
        self._disabled: set[str] = set()

    def init(self, workflows_dir: str) -> None:
        self._workflows_dir = workflows_dir
        self._config_path = os.path.join(
            os.path.dirname(workflows_dir),
            "workflows-config.json",
        )
        self._load_config()
        self._workflows, self._load_errors = scan_workflows(workflows_dir)
        self._apply_disabled()
        logger.info(
            "WorkflowManager initialized: %d workflows loaded",
            len(self._workflows),
        )

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
        workflows: list[dict[str, Any]] = []
        for workflow in self._workflows:
            item = {
                "name": workflow.name,
                "tool_name": workflow.tool_name,
                "description": workflow.description,
                "inputs": workflow.inputs,
                "requires_tools": workflow.requires_tools,
                "requires_skills": workflow.requires_skills,
                "user_invocable": workflow.user_invocable,
                "enabled": workflow.enabled,
                "step_count": len(workflow.steps),
                "file_path": workflow.file_path,
                "policy_modes": self._infer_policy_modes(workflow),
                "execution_boundaries": self._infer_execution_boundaries(workflow),
                "risk_level": self._infer_risk_level(workflow),
                "accepts_secret_refs": self._accepts_secret_refs(workflow),
            }
            if available_tool_names is not None and active_skill_names is not None:
                item.update(
                    self._get_runtime_availability(
                        workflow,
                        available_tool_names,
                        active_skill_names,
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
            self._workflows, self._load_errors = scan_workflows(self._workflows_dir)
            self._apply_disabled()
        return self.list_workflows()

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "workflows": self.list_workflows(),
            "load_errors": list(self._load_errors),
            "loaded_count": len(self._workflows),
            "error_count": len(self._load_errors),
        }

    def get_active_workflows(
        self,
        available_tool_names: list[str],
        active_skill_names: list[str],
    ) -> list[Workflow]:
        tool_set = set(available_tool_names)
        skill_set = set(active_skill_names)
        result: list[Workflow] = []
        for workflow in self._workflows:
            if not workflow.enabled:
                continue
            if workflow.step_tools and not all(
                tool_name in tool_set for tool_name in workflow.step_tools
            ):
                continue
            if workflow.requires_skills and not all(
                skill_name in skill_set for skill_name in workflow.requires_skills
            ):
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
        policy_modes = self._infer_policy_modes(workflow)
        return {
            "description": workflow.description,
            "inputs": workflow.inputs,
            "policy_modes": policy_modes,
            "requires_tools": workflow.requires_tools,
            "requires_skills": workflow.requires_skills,
            "step_count": len(workflow.steps),
            "execution_boundaries": self._infer_execution_boundaries(workflow),
            "risk_level": self._infer_risk_level(workflow),
            "accepts_secret_refs": self._accepts_secret_refs(workflow),
        }

    def _get_runtime_availability(
        self,
        workflow: Workflow,
        available_tool_names: list[str],
        active_skill_names: list[str],
    ) -> dict[str, Any]:
        tool_set = set(available_tool_names)
        skill_set = set(active_skill_names)
        missing_tools = [
            tool_name for tool_name in workflow.requires_tools
            if tool_name not in tool_set
        ]
        missing_skills = [
            skill_name for skill_name in workflow.requires_skills
            if skill_name not in skill_set
        ]
        return {
            "is_available": not missing_tools and not missing_skills,
            "missing_tools": missing_tools,
            "missing_skills": missing_skills,
        }

    def _infer_policy_modes(self, workflow: Workflow) -> list[str]:
        step_tools = workflow.step_tools
        if any(tool_name.startswith("mcp_") for tool_name in step_tools):
            return ["full"]
        if any(
            tool_name in {"write_file", "update_goal", "update_soul", "store_secret", "delete_secret"}
            for tool_name in step_tools
        ):
            return ["balanced", "full"]
        if any(tool_name in {"shell_execute", "get_secret"} for tool_name in step_tools):
            return ["full"]
        return ["safe", "balanced", "full"]

    def _infer_execution_boundaries(self, workflow: Workflow) -> list[str]:
        boundaries: list[str] = []
        for tool_name in workflow.step_tools:
            if tool_name.startswith("mcp_"):
                boundaries.append("external_mcp")
                continue
            tool_meta = TOOL_METADATA.get(tool_name, {})
            for boundary in tool_meta.get("execution_boundaries", []):
                if boundary not in boundaries:
                    boundaries.append(boundary)
        return boundaries or ["unknown"]

    def _infer_risk_level(self, workflow: Workflow) -> str:
        policy_modes = self._infer_policy_modes(workflow)
        if policy_modes == ["full"]:
            return "high"
        if policy_modes == ["balanced", "full"]:
            return "medium"
        return "low"

    def _accepts_secret_refs(self, workflow: Workflow) -> bool:
        for tool_name in workflow.step_tools:
            if tool_name.startswith("mcp_"):
                return True
            tool_meta = TOOL_METADATA.get(tool_name, {})
            if bool(tool_meta.get("accepts_secret_refs", False)):
                return True
        return False


workflow_manager = WorkflowManager()
