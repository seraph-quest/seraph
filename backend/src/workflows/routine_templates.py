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


def _v2_spec(template_id: str):
    # Keep the v1 module import graph unchanged.  The v2 contract owns the
    # fixed registry and imports no routine service or runtime adapter.
    from src.workflows.procedure_contracts import get_procedure_template

    return get_procedure_template(template_id)


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


def render_workflow(
    *,
    routine_id: str,
    version: int,
    name: str,
    template_id: str | None = None,
) -> str:
    workflow_name = routine_slug(routine_id, version)
    if template_id is not None:
        spec = _v2_spec(template_id)
        payload: dict[str, Any] = {
            "name": workflow_name,
            "description": _description(name),
            "user_invocable": False,
            "requires": {"tools": [step.step_id for step in spec.steps]},
            # The only executable workflow input is the trusted parent job.
            # Step parameters are resolved from the immutable version and
            # current invocation descriptor by the native runtime adapter.
            "inputs": {
                "routine_invocation_job_id": {"type": "string", "required": True},
            },
            "steps": [
                {
                    "id": step.step_id,
                    "tool": step.step_id,
                    "arguments": {"routine_invocation_job_id": "{{ routine_invocation_job_id }}"},
                }
                for step in spec.steps
            ],
            "result": "Inspect the verified leaf artifacts and readbacks.",
        }
        frontmatter = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False).strip()
        return f"---\n{frontmatter}\n---\n\nThis routine is executed only through its owner-bound invocation.\n"
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


def render_runbook(
    *,
    routine_id: str,
    version: int,
    name: str,
    template_id: str | None = None,
) -> str:
    workflow_name = routine_slug(routine_id, version)
    if template_id is not None:
        spec = _v2_spec(template_id)
        payload = {
            "id": f"runbook:{workflow_name}",
            "title": _description(name),
            "summary": "Run the reviewed fixed procedure leaves.",
            "workflow": workflow_name,
            "procedure": {
                "schema_version": 2,
                "template_id": spec.template_id,
                "steps": [
                    {
                        "id": step.step_id,
                        "capability_id": step.capability_id,
                        "capability_version": step.capability_version,
                    }
                    for step in spec.steps
                ],
            },
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    payload = {
        "id": f"runbook:{workflow_name}",
        "title": _description(name),
        "summary": "Run the verified guardian watch and its reviewed follow-through.",
        "workflow": workflow_name,
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)


def validate_generated_files(
    *,
    workflow: str,
    runbook: str,
    routine_id: str,
    version: int,
    template_id: str | None = None,
) -> dict[str, Any]:
    if template_id is not None:
        expected = routine_slug(routine_id, version)
        errors: list[str] = []
        try:
            spec = _v2_spec(template_id)
            # Workflow files contain YAML frontmatter followed by Markdown,
            # so they are parsed through the existing workflow loader rather
            # than ``yaml.safe_load`` (which rejects the second document).
            parsed_workflow = parse_workflow_content(workflow, path=f"{expected}.md", errors=[])
            parsed_runbook = yaml.safe_load(runbook)
        except Exception:
            errors = ["procedure_v2_template_invalid"]
            spec = None
            parsed_workflow = None
            parsed_runbook = None
        if spec is not None:
            parsed = parse_workflow_content(workflow, path=f"{expected}.md", errors=[])
            if parsed is None:
                errors.append("workflow_parse_failed")
            else:
                if parsed.name != expected or parsed.user_invocable:
                    errors.append("workflow_identity_invalid")
                if [step.id for step in parsed.steps] != [step.step_id for step in spec.steps]:
                    errors.append("workflow_step_order_invalid")
                if [step.tool for step in parsed.steps] != [step.step_id for step in spec.steps]:
                    errors.append("workflow_tool_allowlist_invalid")
                if set(parsed.inputs) != {"routine_invocation_job_id"}:
                    errors.append("workflow_input_surface_invalid")
            if not isinstance(parsed_runbook, dict) or set(parsed_runbook) != {
                "id", "title", "summary", "workflow", "procedure"
            }:
                errors.append("runbook_contract_invalid")
            else:
                procedure = parsed_runbook.get("procedure")
                expected_steps = [
                    {
                        "id": step.step_id,
                        "capability_id": step.capability_id,
                        "capability_version": step.capability_version,
                    }
                    for step in spec.steps
                ]
                if (
                    parsed_runbook.get("id") != f"runbook:{expected}"
                    or parsed_runbook.get("workflow") != expected
                    or not isinstance(procedure, dict)
                    or set(procedure) != {"schema_version", "template_id", "steps"}
                    or procedure.get("schema_version") != 2
                    or procedure.get("template_id") != spec.template_id
                    or procedure.get("steps") != expected_steps
                ):
                    errors.append("runbook_contract_invalid")
            return {
                "valid": not errors,
                "errors": errors,
                "workflow_name": expected,
                "template_id": spec.template_id,
                "schema_version": 2,
                "step_order": [step.step_id for step in spec.steps],
            }
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
