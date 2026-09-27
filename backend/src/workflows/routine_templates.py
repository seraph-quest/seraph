"""Deterministic, fixed-surface files for guardian routines."""

from __future__ import annotations

import re
from typing import Any

import yaml

from src.runbooks.loader import parse_runbook_content
from src.workflows.loader import parse_workflow_content


ROUTINE_STEP_IDS = ("guardian_watch_run", "github_followthrough")
ROUTINE_TOOL_NAMES = ROUTINE_STEP_IDS
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")


def routine_slug(routine_id: str, version: int) -> str:
    compact = str(routine_id or "").replace("-", "").strip()
    if not compact or any(char not in "0123456789abcdefABCDEF" for char in compact):
        raise ValueError("routine id is invalid")
    if int(version) < 1:
        raise ValueError("routine version is invalid")
    return f"routine-{compact}-v{int(version)}"


def _description(name: str) -> str:
    value = str(name or "").strip()
    if not _SAFE_NAME.fullmatch(value):
        raise ValueError("routine name must contain only bounded printable text")
    return value


def render_workflow(*, routine_id: str, version: int, name: str) -> str:
    workflow_name = routine_slug(routine_id, version)
    payload: dict[str, Any] = {
        "name": workflow_name,
        "description": _description(name),
        "user_invocable": False,
        "requires": {"tools": list(ROUTINE_TOOL_NAMES)},
        "inputs": {
            "routine_invocation_job_id": {"type": "string", "required": True},
        },
        "steps": [
            {
                "id": "guardian_watch_run",
                "tool": "guardian_watch_run",
                "arguments": {"routine_invocation_job_id": "{{ routine_invocation_job_id }}"},
            },
            {
                "id": "github_followthrough",
                "tool": "github_followthrough",
                "arguments": {"routine_invocation_job_id": "{{ routine_invocation_job_id }}"},
            },
        ],
        "result": "Inspect the verified artifacts and destination readback.",
    }
    frontmatter = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False).strip()
    return f"---\n{frontmatter}\n---\n\nThis routine is executed only through its owner-bound invocation.\n"


def render_runbook(*, routine_id: str, version: int, name: str) -> str:
    workflow_name = routine_slug(routine_id, version)
    payload = {
        "id": f"runbook:{workflow_name}",
        "title": _description(name),
        "summary": "Run the verified guardian watch and its reviewed follow-through.",
        "workflow": workflow_name,
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)


def validate_generated_files(*, workflow: str, runbook: str, routine_id: str, version: int) -> dict[str, Any]:
    expected = routine_slug(routine_id, version)
    errors: list[str] = []
    parsed_workflow = parse_workflow_content(workflow, path=f"{expected}.md", errors=[])
    parsed_runbook = parse_runbook_content(runbook, path=f"{expected}.yaml", errors=[])
    if parsed_workflow is None:
        errors.append("workflow_parse_failed")
    else:
        if parsed_workflow.name != expected or parsed_workflow.user_invocable:
            errors.append("workflow_identity_invalid")
        if [step.id for step in parsed_workflow.steps] != list(ROUTINE_STEP_IDS):
            errors.append("workflow_step_order_invalid")
        if [step.tool for step in parsed_workflow.steps] != list(ROUTINE_TOOL_NAMES):
            errors.append("workflow_tool_allowlist_invalid")
        if set(parsed_workflow.inputs) != {"routine_invocation_job_id"}:
            errors.append("workflow_input_surface_invalid")
    if parsed_runbook is None or parsed_runbook.workflow != expected or parsed_runbook.command:
        errors.append("runbook_contract_invalid")
    return {
        "valid": not errors,
        "errors": errors,
        "workflow_name": expected,
        "step_order": list(ROUTINE_STEP_IDS),
    }


__all__ = [
    "ROUTINE_STEP_IDS",
    "ROUTINE_TOOL_NAMES",
    "render_runbook",
    "render_workflow",
    "routine_slug",
    "validate_generated_files",
]
