"""Workflow manager and runtime."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from smolagents import Tool

from src.plugins.registry import TOOL_METADATA
from src.workflows.loader import Workflow, load_workflows

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

    def forward(self, *args, **kwargs):
        return self.__call__(*args, **kwargs)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        workflow_inputs = self._normalize_inputs(args, kwargs)
        context: dict[str, Any] = {
            "inputs": workflow_inputs,
            "steps": {},
            "last_result": "",
        }
        context.update(workflow_inputs)

        for step in self.workflow.steps:
            tool = self.tools_by_name.get(step.tool)
            if tool is None:
                raise RuntimeError(
                    f"Workflow '{self.workflow.name}' requires unavailable tool '{step.tool}'"
                )
            rendered_arguments = _render_value(step.arguments, context)
            try:
                result = tool(
                    **rendered_arguments,
                    sanitize_inputs_outputs=sanitize_inputs_outputs,
                )
            except Exception as exc:
                if not step.continue_on_error:
                    raise
                result = f"Error: {exc}"
            context["steps"][step.id] = {
                "tool": step.tool,
                "arguments": rendered_arguments,
                "result": result,
            }
            context["last_result"] = result

        if self.workflow.result_template:
            rendered = _render_value(self.workflow.result_template, context)
            return str(rendered)
        return _summarize_workflow_result(self.workflow, context["steps"])

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
        self._workflows = load_workflows(workflows_dir)
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

    def list_workflows(self) -> list[dict[str, Any]]:
        return [
            {
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
            }
            for workflow in self._workflows
        ]

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
            self._workflows = load_workflows(self._workflows_dir)
            self._apply_disabled()
        return self.list_workflows()

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
            if workflow.requires_tools and not all(
                tool_name in tool_set for tool_name in workflow.requires_tools
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
        }

    def _infer_policy_modes(self, workflow: Workflow) -> list[str]:
        if any(tool_name.startswith("mcp_") for tool_name in workflow.requires_tools):
            return ["full"]
        if any(
            tool_name in {"write_file", "update_goal", "update_soul", "store_secret", "delete_secret"}
            for tool_name in workflow.requires_tools
        ):
            return ["balanced", "full"]
        if any(tool_name in {"shell_execute", "get_secret"} for tool_name in workflow.requires_tools):
            return ["full"]
        return ["safe", "balanced", "full"]

    def _infer_execution_boundaries(self, workflow: Workflow) -> list[str]:
        boundaries: list[str] = []
        for tool_name in workflow.requires_tools:
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


workflow_manager = WorkflowManager()
