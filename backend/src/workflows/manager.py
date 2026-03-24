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

from src.extensions.permissions import evaluate_tool_permissions
from src.extensions.registry import ExtensionRegistry, ExtensionRegistrySnapshot
from src.approval.repository import fingerprint_tool_call
from src.native_tools.registry import TOOL_METADATA, canonical_tool_name
from src.workflows.loader import Workflow, scan_workflow_paths

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
        canonical_step_tools = [canonical_tool_name(step.tool) for step in self.workflow.steps]

        for step, canonical_step_tool in zip(self.workflow.steps, canonical_step_tools, strict=False):
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
                "tool": canonical_step_tool,
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
        self._last_audit_payload = (
            summary,
            {
                "workflow_name": self.workflow.name,
                "run_fingerprint": run_fingerprint,
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
                "failed_step_ids": continued_error_steps,
                "runtime_profile": self.workflow.runtime_profile,
                "output_surface": self.workflow.output_surface,
                "canvas_output": _redact_canvas_output(canvas_output),
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
        policy_modes = self._infer_policy_modes(workflow)
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
            "execution_boundaries": self._infer_execution_boundaries(workflow),
            "risk_level": self._infer_risk_level(workflow),
            "accepts_secret_refs": self._accepts_secret_refs(workflow),
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
        missing_tools = [
            canonical_tool_name(tool_name) for tool_name in workflow.requires_tools
            if canonical_tool_name(tool_name) not in tool_set
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

    def _infer_policy_modes(self, workflow: Workflow) -> list[str]:
        step_tools = workflow.step_tools
        canonical_step_tools = [canonical_tool_name(tool_name) for tool_name in step_tools]
        if any(tool_name.startswith("mcp_") for tool_name in canonical_step_tools):
            return ["full"]
        if any(
            tool_name in {"write_file", "update_goal", "update_soul", "store_secret", "delete_secret"}
            for tool_name in canonical_step_tools
        ):
            return ["balanced", "full"]
        if any(tool_name in {"execute_code", "get_secret"} for tool_name in canonical_step_tools):
            return ["full"]
        return ["safe", "balanced", "full"]

    def _infer_execution_boundaries(self, workflow: Workflow) -> list[str]:
        boundaries: list[str] = []
        for tool_name in workflow.step_tools:
            canonical_name = canonical_tool_name(tool_name)
            if canonical_name.startswith("mcp_"):
                boundaries.append("external_mcp")
                continue
            tool_meta = TOOL_METADATA.get(canonical_name, {})
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
            canonical_name = canonical_tool_name(tool_name)
            if canonical_name.startswith("mcp_"):
                return True
            tool_meta = TOOL_METADATA.get(canonical_name, {})
            if bool(tool_meta.get("accepts_secret_refs", False)):
                return True
        return False


workflow_manager = WorkflowManager()
