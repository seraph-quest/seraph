"""Workflow loader — parses markdown workflow files with YAML frontmatter."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def sanitize_workflow_name(name: str) -> str:
    """Convert a workflow name into a safe runtime/tool identifier."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_").lower()
    return sanitized or "workflow"


def workflow_tool_name(name: str) -> str:
    """Return the tool name exposed for a workflow."""
    return f"workflow_{sanitize_workflow_name(name)}"


def _record_workflow_error(
    errors: list[dict[str, str]] | None,
    *,
    path: str,
    message: str,
) -> None:
    logger.warning(message)
    if errors is not None:
        errors.append({"file_path": path, "message": message})


@dataclass
class WorkflowStep:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    continue_on_error: bool = False


@dataclass
class Workflow:
    name: str
    description: str
    inputs: dict[str, dict[str, Any]]
    steps: list[WorkflowStep]
    requires_tools: list[str] = field(default_factory=list)
    requires_skills: list[str] = field(default_factory=list)
    user_invocable: bool = False
    enabled: bool = True
    file_path: str = ""
    body: str = ""
    result_template: str = ""

    @property
    def tool_name(self) -> str:
        return workflow_tool_name(self.name)

    @property
    def step_tools(self) -> list[str]:
        return list(dict.fromkeys(step.tool for step in self.steps))


def _parse_workflow_file(path: str, *, errors: list[dict[str, str]] | None = None) -> Workflow | None:
    """Parse a single markdown workflow file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        _record_workflow_error(errors, path=path, message=f"Failed to read workflow file {path}: {exc}")
        return None

    if not content.startswith("---"):
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} missing YAML frontmatter")
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} has malformed frontmatter")
        return None

    frontmatter_str = parts[1].strip()
    body = parts[2].strip()

    try:
        frontmatter = yaml.safe_load(frontmatter_str)
    except yaml.YAMLError as exc:
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} has invalid YAML: {exc}")
        return None

    if not isinstance(frontmatter, dict):
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} frontmatter is not a mapping")
        return None

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    steps_raw = frontmatter.get("steps")
    if not name or not isinstance(name, str):
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} missing required 'name' field")
        return None
    if not description or not isinstance(description, str):
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} missing required 'description' field")
        return None
    if not isinstance(steps_raw, list) or not steps_raw:
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} missing required non-empty 'steps' list")
        return None

    requires = frontmatter.get("requires", {})
    inputs = frontmatter.get("inputs", {})
    result_template = frontmatter.get("result", "")
    if not isinstance(requires, dict):
        requires = {}
    if not isinstance(inputs, dict):
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} has invalid 'inputs' field")
        return None
    if result_template and not isinstance(result_template, str):
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} has invalid 'result' field")
        return None

    parsed_steps: list[WorkflowStep] = []
    seen_ids: set[str] = set()
    for idx, step_raw in enumerate(steps_raw, start=1):
        if not isinstance(step_raw, dict):
            _record_workflow_error(errors, path=path, message=f"Workflow file {path} step {idx} is not a mapping")
            return None
        tool = step_raw.get("tool")
        if not tool or not isinstance(tool, str):
            _record_workflow_error(errors, path=path, message=f"Workflow file {path} step {idx} missing 'tool'")
            return None
        step_id = step_raw.get("id") or f"step_{idx}"
        if not isinstance(step_id, str):
            _record_workflow_error(errors, path=path, message=f"Workflow file {path} step {idx} has invalid 'id'")
            return None
        if step_id in seen_ids:
            _record_workflow_error(errors, path=path, message=f"Workflow file {path} has duplicate step id '{step_id}'")
            return None
        seen_ids.add(step_id)
        arguments = step_raw.get("arguments", {})
        if not isinstance(arguments, dict):
            _record_workflow_error(errors, path=path, message=f"Workflow file {path} step {idx} has invalid 'arguments'")
            return None
        parsed_steps.append(
            WorkflowStep(
                tool=tool,
                arguments=arguments,
                id=step_id,
                continue_on_error=bool(step_raw.get("continue_on_error", False)),
            )
        )

    requires_tools = requires.get("tools", []) if isinstance(requires, dict) else []
    requires_skills = requires.get("skills", []) if isinstance(requires, dict) else []
    if not isinstance(requires_tools, list) or not all(isinstance(item, str) for item in requires_tools):
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} has invalid requires.tools")
        return None
    if not isinstance(requires_skills, list) or not all(isinstance(item, str) for item in requires_skills):
        _record_workflow_error(errors, path=path, message=f"Workflow file {path} has invalid requires.skills")
        return None

    normalized_inputs: dict[str, dict[str, Any]] = {}
    for input_name, input_spec in inputs.items():
        if not isinstance(input_name, str) or not isinstance(input_spec, dict):
            _record_workflow_error(errors, path=path, message=f"Workflow file {path} has invalid input definition")
            return None
        normalized_inputs[input_name] = {
            "type": input_spec.get("type", "string"),
            "description": input_spec.get("description", ""),
            "required": bool(input_spec.get("required", True)),
            "default": input_spec.get("default"),
        }

    step_tools = list(dict.fromkeys(step.tool for step in parsed_steps))
    missing_required_tools = [
        tool_name for tool_name in step_tools
        if tool_name not in requires_tools
    ]
    if missing_required_tools:
        _record_workflow_error(
            errors,
            path=path,
            message=(
                f"Workflow file {path} has undeclared step tools: "
                f"{', '.join(missing_required_tools)}"
            ),
        )
        return None

    return Workflow(
        name=name,
        description=description,
        inputs=normalized_inputs,
        steps=parsed_steps,
        requires_tools=requires_tools,
        requires_skills=requires_skills,
        user_invocable=bool(frontmatter.get("user_invocable", False)),
        enabled=bool(frontmatter.get("enabled", True)),
        file_path=path,
        body=body,
        result_template=result_template if isinstance(result_template, str) else "",
    )


def scan_workflows(workflows_dir: str) -> tuple[list[Workflow], list[dict[str, str]]]:
    """Scan directory for markdown workflows and parse them."""
    if not os.path.isdir(workflows_dir):
        logger.info("Workflows directory %s does not exist, skipping", workflows_dir)
        return [], []

    workflows: list[Workflow] = []
    errors: list[dict[str, str]] = []
    for filename in sorted(os.listdir(workflows_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(workflows_dir, filename)
        workflow = _parse_workflow_file(path, errors=errors)
        if workflow is not None:
            workflows.append(workflow)
            logger.info("Loaded workflow: %s from %s", workflow.name, filename)

    return workflows, errors


def load_workflows(workflows_dir: str) -> list[Workflow]:
    workflows, _ = scan_workflows(workflows_dir)
    return workflows
