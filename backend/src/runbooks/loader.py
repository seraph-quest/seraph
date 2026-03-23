"""Runbook loader for explicit package-backed and loose runbook definitions."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import re
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _slugify(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return sanitized or "runbook"


def _record_runbook_error(
    errors: list[dict[str, str]] | None,
    *,
    path: str,
    message: str,
) -> None:
    logger.warning(message)
    if errors is not None:
        errors.append({"file_path": path, "message": message})


@dataclass
class Runbook:
    id: str
    title: str
    summary: str
    workflow: str | None = None
    starter_pack: str | None = None
    command: str | None = None
    file_path: str = ""
    source: str = "legacy"
    extension_id: str | None = None


def parse_runbook_content(
    content: str,
    *,
    path: str,
    errors: list[dict[str, str]] | None = None,
) -> Runbook | None:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        _record_runbook_error(errors, path=path, message=f"Runbook file {path} has invalid YAML: {exc}")
        return None
    if not isinstance(payload, dict):
        _record_runbook_error(errors, path=path, message=f"Runbook file {path} root must be a mapping")
        return None
    title = payload.get("title")
    summary = payload.get("summary")
    if not isinstance(title, str) or not title.strip():
        _record_runbook_error(errors, path=path, message=f"Runbook file {path} missing required 'title' field")
        return None
    if not isinstance(summary, str) or not summary.strip():
        _record_runbook_error(errors, path=path, message=f"Runbook file {path} missing required 'summary' field")
        return None
    workflow = payload.get("workflow")
    starter_pack = payload.get("starter_pack")
    command = payload.get("command")
    if workflow is not None and (not isinstance(workflow, str) or not workflow.strip()):
        _record_runbook_error(errors, path=path, message=f"Runbook file {path} has invalid 'workflow' field")
        return None
    if starter_pack is not None and (not isinstance(starter_pack, str) or not starter_pack.strip()):
        _record_runbook_error(errors, path=path, message=f"Runbook file {path} has invalid 'starter_pack' field")
        return None
    if command is not None and (not isinstance(command, str) or not command.strip()):
        _record_runbook_error(errors, path=path, message=f"Runbook file {path} has invalid 'command' field")
        return None
    targets = [bool(workflow), bool(starter_pack), bool(command)]
    if sum(targets) != 1:
        _record_runbook_error(
            errors,
            path=path,
            message=f"Runbook file {path} must define exactly one of 'workflow', 'starter_pack', or 'command'",
        )
        return None
    raw_id = payload.get("id")
    if raw_id is not None and (not isinstance(raw_id, str) or not raw_id.strip()):
        _record_runbook_error(errors, path=path, message=f"Runbook file {path} has invalid 'id' field")
        return None
    runbook_id = str(raw_id).strip() if isinstance(raw_id, str) and raw_id.strip() else f"runbook:{_slugify(os.path.splitext(os.path.basename(path))[0])}"
    return Runbook(
        id=runbook_id,
        title=title.strip(),
        summary=summary.strip(),
        workflow=workflow.strip() if isinstance(workflow, str) and workflow.strip() else None,
        starter_pack=starter_pack.strip() if isinstance(starter_pack, str) and starter_pack.strip() else None,
        command=command.strip() if isinstance(command, str) and command.strip() else None,
        file_path=path,
    )


def scan_runbook_paths(runbook_paths: list[str]) -> tuple[list[Runbook], list[dict[str, str]]]:
    runbooks: list[Runbook] = []
    errors: list[dict[str, str]] = []
    for path in sorted(runbook_paths):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                content = handle.read()
        except OSError as exc:
            _record_runbook_error(errors, path=path, message=f"Failed to read runbook file {path}: {exc}")
            continue
        runbook = parse_runbook_content(content, path=path, errors=errors)
        if runbook is not None:
            runbooks.append(runbook)
    return runbooks, errors
