"""Workflows API — list, toggle, reload reusable multi-step workflows."""

from collections import defaultdict
from datetime import datetime
import json
import os
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import col, select

from config.settings import settings
from src.api.capabilities import _recommended_tool_policy_mode
from src.agent.session import session_manager
from src.agent.factory import get_base_tools_and_active_skills
from src.approval.repository import fingerprint_tool_call
from src.approval.repository import approval_repository
from src.audit.repository import audit_repository
from src.audit.runtime import log_integration_event
from src.db.engine import get_session
from src.db.models import AuditEvent
from src.extensions.registry import ExtensionRegistry
from src.extensions.registry import default_manifest_roots_for_workspace
from src.extensions.workflow_runtimes import list_workflow_runtime_inventory
from src.extensions.workspace_package import save_workspace_contribution
from src.tools.policy import get_current_tool_policy_mode
from src.workflows.loader import parse_workflow_content
from src.workflows.manager import approval_context_requires_tracked_lineage, workflow_manager
from src.workflows.run_identity import build_workflow_run_identity, parse_workflow_run_identity

router = APIRouter()

_WORKFLOW_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


class UpdateWorkflowRequest(BaseModel):
    enabled: bool


class WorkflowResumePlanRequest(BaseModel):
    step_id: str | None = None


class WorkflowDraftRequest(BaseModel):
    content: str
    file_name: str | None = None


def _safe_markdown_filename(name: str) -> str:
    value = _WORKFLOW_FILENAME_RE.sub("-", name.strip()).strip("-_").lower()
    return f"{value or 'workflow'}.md"


def _resolve_workflow_file_name(file_name: str | None, *, default_name: str) -> str:
    if not file_name:
        return default_name
    candidate = file_name.strip()
    normalized = os.path.normpath(candidate)
    if (
        not candidate
        or os.path.isabs(candidate)
        or normalized.startswith("..")
        or os.path.basename(normalized) != normalized
    ):
        raise HTTPException(status_code=400, detail="Workflow file name must stay within the managed workspace package")
    stem, _ = os.path.splitext(normalized)
    return _safe_markdown_filename(stem or normalized)


def _ensure_workflow_manager_workspace_extensions_loaded() -> None:
    workflows_dir = workflow_manager._workflows_dir or os.path.join(settings.workspace_dir, "workflows")
    manifest_roots = list(workflow_manager._manifest_roots or [])
    changed = not bool(workflow_manager._workflows_dir)
    for root in default_manifest_roots_for_workspace(settings.workspace_dir):
        if root not in manifest_roots:
            manifest_roots.append(root)
            changed = True
    if changed:
        workflow_manager.init(workflows_dir, manifest_roots=manifest_roots)


def _workflow_extension_snapshot():
    return ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()


def _workflow_surface_maps(snapshot) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    runtime_defaults_by_name: dict[str, str] = {}
    canvas_metadata_by_name: dict[str, dict[str, Any]] = {}
    for contribution in snapshot.list_contributions("workflow_runtimes"):
        if isinstance(contribution.metadata.get("registry_conflict"), dict):
            continue
        name = contribution.metadata.get("name")
        default_output_surface = contribution.metadata.get("default_output_surface")
        if (
            isinstance(name, str)
            and name.strip()
            and isinstance(default_output_surface, str)
            and default_output_surface.strip()
        ):
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
    return runtime_defaults_by_name, canvas_metadata_by_name


def _validate_workflow_content(content: str, *, path: str = "<draft>") -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    workflow = parse_workflow_content(content, path=path, errors=errors)
    if workflow is None:
        return {
            "valid": False,
            "errors": errors,
            "workflow": None,
            "runtime_ready": False,
            "missing_tools": [],
            "missing_skills": [],
        }

    base_tools, active_skill_names, mcp_mode = get_base_tools_and_active_skills()
    available_tool_names = [tool.name for tool in base_tools]
    missing_tools = [
        tool_name for tool_name in workflow.step_tools
        if tool_name not in set(available_tool_names)
    ]
    missing_skills = [
        skill_name for skill_name in workflow.requires_skills
        if skill_name not in set(active_skill_names)
    ]
    snapshot = _workflow_extension_snapshot()
    runtime_defaults_by_name, canvas_metadata_by_name = _workflow_surface_maps(snapshot)
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
    missing_runtime_profiles = (
        [workflow.runtime_profile]
        if workflow.runtime_profile and workflow.runtime_profile not in available_runtime_profiles
        else []
    )
    declared_output_surface = workflow.output_surface
    effective_output_surface = workflow.output_surface or (
        runtime_defaults_by_name.get(workflow.runtime_profile, "")
        if workflow.runtime_profile
        else ""
    )
    canvas_metadata = canvas_metadata_by_name.get(effective_output_surface, {})
    missing_output_surfaces = (
        [effective_output_surface]
        if effective_output_surface and effective_output_surface not in available_output_surfaces
        else []
    )
    execution_boundaries = workflow_manager._infer_execution_boundaries(workflow)
    risk_level = workflow_manager._infer_risk_level(workflow)
    policy_modes = workflow_manager._infer_policy_modes(workflow)
    requires_approval = (
        risk_level == "high"
        or ("external_mcp" in execution_boundaries and mcp_mode == "approval")
    )
    return {
        "valid": True,
        "errors": [],
        "workflow": {
            "name": workflow.name,
            "tool_name": workflow.tool_name,
            "description": workflow.description,
            "inputs": workflow.inputs,
            "requires_tools": workflow.requires_tools,
            "requires_skills": workflow.requires_skills,
            "runtime_profile": workflow.runtime_profile,
            "output_surface": effective_output_surface,
            "declared_output_surface": declared_output_surface,
            "effective_output_surface": effective_output_surface,
            "output_surface_title": str(canvas_metadata.get("title") or ""),
            "output_surface_sections": list(canvas_metadata.get("sections") or []),
            "output_surface_artifact_types": list(canvas_metadata.get("artifact_types") or []),
            "user_invocable": workflow.user_invocable,
            "enabled": workflow.enabled,
            "file_path": workflow.file_path,
            "step_count": len(workflow.steps),
            "policy_modes": policy_modes,
            "execution_boundaries": execution_boundaries,
            "risk_level": risk_level,
            "accepts_secret_refs": workflow_manager._accepts_secret_refs(workflow),
        },
        "runtime_ready": (
            not missing_tools
            and not missing_skills
            and not missing_runtime_profiles
            and not missing_output_surfaces
            and workflow.enabled
        ),
        "missing_tools": missing_tools,
        "missing_skills": missing_skills,
        "missing_runtime_profiles": missing_runtime_profiles,
        "missing_output_surfaces": missing_output_surfaces,
        "requires_approval": requires_approval,
    }


def _as_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


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


def _normalize_approval_context(value: Any, *, workflow_name: str | None = None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    risk_level = str(value.get("risk_level") or "").strip()
    execution_boundaries = _normalize_string_list(value.get("execution_boundaries"))
    step_tools = _normalize_string_list(value.get("step_tools"))
    delegated_specialists = _normalize_string_list(value.get("delegated_specialists"))
    authenticated_source = bool(value.get("authenticated_source", False))
    delegation_target_unresolved = bool(value.get("delegation_target_unresolved", False))
    source_systems = _normalize_source_systems(value.get("source_systems"))
    if not any(
        [
            risk_level,
            execution_boundaries,
            step_tools,
            delegated_specialists,
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
    if authenticated_source:
        normalized["authenticated_source"] = True
    if delegation_target_unresolved:
        normalized["delegation_target_unresolved"] = True
    if source_systems:
        normalized["source_systems"] = source_systems
    return normalized


def _workflow_current_approval_context(
    *,
    workflow_name: str,
    workflow_meta: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_approval_context(
        workflow_meta.get("approval_context"),
        workflow_name=workflow_name,
    )
    if normalized is not None:
        return normalized
    normalized = {
        "workflow_name": workflow_name,
        "risk_level": str(workflow_meta.get("risk_level") or "high"),
        "execution_boundaries": _normalize_string_list(
            workflow_meta.get("execution_boundaries") or ["unknown"]
        ),
        "accepts_secret_refs": bool(workflow_meta.get("accepts_secret_refs", False)),
        "step_tools": _normalize_string_list(workflow_meta.get("step_tools")),
    }
    if bool(workflow_meta.get("authenticated_source", False)):
        normalized["authenticated_source"] = True
    source_systems = _normalize_source_systems(workflow_meta.get("source_systems"))
    if source_systems:
        normalized["source_systems"] = source_systems
    return normalized


def _workflow_runtime_approval_contexts() -> dict[str, dict[str, Any]]:
    base_tools, active_skill_names, _mcp_mode = get_base_tools_and_active_skills()
    runtime_contexts: dict[str, dict[str, Any]] = {}
    for tool in workflow_manager.build_workflow_tools(base_tools, active_skill_names):
        tool_name = getattr(tool, "name", None)
        if not isinstance(tool_name, str) or not tool_name.startswith("workflow_"):
            continue
        hook = getattr(tool, "get_approval_context", None)
        if not callable(hook):
            continue
        normalized = _normalize_approval_context(
            hook({}),
            workflow_name=_workflow_name_from_tool(tool_name),
        )
        if normalized is not None:
            runtime_contexts[tool_name] = normalized
    return runtime_contexts


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
        return fingerprint_tool_call(
            tool_name,
            arguments,
            approval_context=_normalize_approval_context(details.get("approval_context")),
        )
    return "none"


def _workflow_projection_key(event: dict[str, Any], details: dict[str, Any]) -> str:
    tool_name = str(event.get("tool_name") or "workflow")
    fingerprint = _workflow_event_fingerprint(tool_name, details)
    return build_workflow_run_identity(
        event.get("session_id") if isinstance(event.get("session_id"), str) else None,
        tool_name,
        fingerprint,
        run_discriminator=_workflow_run_discriminator(details),
    )


def _workflow_projection_prefix(session_id: str | None, tool_name: str) -> str:
    return f"{session_id or 'global'}:{tool_name}:"


def _workflow_run_discriminator(details: dict[str, Any]) -> str | None:
    call_event_id = details.get("call_event_id")
    if isinstance(call_event_id, str) and call_event_id.strip():
        return call_event_id.strip()
    return None


def _workflow_identity_discriminator(event: dict[str, Any], details: dict[str, Any]) -> str | None:
    run_discriminator = _workflow_run_discriminator(details)
    if run_discriminator is not None:
        return run_discriminator
    if str(event.get("event_type") or "") == "tool_call":
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id.strip():
            return event_id.strip()
    return None


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
            fingerprint_tool_call(
                tool_name,
                run.get("arguments") or {},
                approval_context=_normalize_approval_context(run.get("approval_context")),
            )
            if run.get("arguments")
            else None
        )
    )
    return _approval_projection_key(
        session_id=run.get("session_id") if isinstance(run.get("session_id"), str) else None,
        tool_name=tool_name,
        fingerprint=fingerprint,
    )


def _workflow_replay_draft(
    workflow_name: str,
    arguments: dict[str, Any] | None,
    *,
    control_inputs: dict[str, Any] | None = None,
) -> str:
    serialized_items: list[str] = []
    for key, value in (arguments or {}).items():
        serialized_items.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    for key, value in (control_inputs or {}).items():
        if value is None:
            continue
        serialized_items.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    if not serialized_items:
        return f'Run workflow "{workflow_name}".'
    return f'Run workflow "{workflow_name}" with {", ".join(serialized_items)}.'


def _workflow_retry_from_step_draft(
    workflow_name: str,
    *,
    step_id: str,
    arguments: dict[str, Any] | None,
    parent_run_identity: str | None = None,
    root_run_identity: str | None = None,
    branch_kind: str = "retry_failed_step",
    branch_depth: int | None = None,
) -> str:
    control_inputs: dict[str, Any] = {
        "_seraph_resume_from_step": step_id,
        "_seraph_branch_kind": branch_kind,
    }
    if parent_run_identity:
        control_inputs["_seraph_parent_run_identity"] = parent_run_identity
    if root_run_identity:
        control_inputs["_seraph_root_run_identity"] = root_run_identity
    if isinstance(branch_depth, int):
        control_inputs["_seraph_branch_depth"] = branch_depth
    return _workflow_replay_draft(
        workflow_name,
        arguments,
        control_inputs=control_inputs,
    )


def _workflow_branch_lineage(
    *,
    run_identity: str,
    details: dict[str, Any],
    approvals: list[dict[str, Any]],
    continued_error_steps: list[str],
) -> dict[str, Any]:
    parent_run_identity = details.get("parent_run_identity")
    if not isinstance(parent_run_identity, str) or not parent_run_identity.strip():
        parent_run_identity = None
    root_run_identity = details.get("root_run_identity")
    if not isinstance(root_run_identity, str) or not root_run_identity.strip():
        root_run_identity = parent_run_identity or run_identity
    branch_kind = details.get("branch_kind")
    if not isinstance(branch_kind, str) or not branch_kind.strip():
        if approvals:
            branch_kind = "approval_resume"
        elif continued_error_steps:
            branch_kind = "retry_failed_step"
        else:
            branch_kind = "replay_from_start"
    branch_depth = details.get("branch_depth")
    if not isinstance(branch_depth, int) or branch_depth < 0:
        branch_depth = 1 if parent_run_identity else 0
    return {
        "parent_run_identity": parent_run_identity,
        "root_run_identity": root_run_identity,
        "branch_kind": branch_kind,
        "branch_depth": branch_depth,
        "is_branch_run": parent_run_identity is not None,
    }


def _workflow_checkpoint_candidates(
    run: dict[str, Any],
    *,
    approvals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    workflow_name = str(run["workflow_name"])
    arguments = run.get("arguments")
    continued_error_steps = set(str(step_id) for step_id in run.get("continued_error_steps", []))
    root_run_identity = (
        str(run.get("root_run_identity"))
        if isinstance(run.get("root_run_identity"), str) and str(run.get("root_run_identity")).strip()
        else str(run.get("run_identity") or "")
    )
    next_branch_depth = (
        int(run.get("branch_depth")) + 1
        if isinstance(run.get("branch_depth"), int) and int(run.get("branch_depth")) >= 0
        else (1 if run.get("run_identity") else 0)
    )
    candidates: list[dict[str, Any]] = []
    if approvals:
        candidates.append({
            "step_id": "approval_gate",
            "label": "Approval gate",
            "kind": "approval_gate",
            "status": "pending",
            "step_tool": None,
            "resume_draft": None,
            "continue_message": (
                approvals[0].get("resume_message")
                if approvals and isinstance(approvals[0], dict)
                else None
            ),
        })
    for index, step in enumerate(run.get("step_records", []) or []):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        step_tool = str(step.get("tool") or "tool")
        step_status = str(step.get("status") or "unknown")
        is_failed = step_id in continued_error_steps or step_status in {"failed", "continued_error"}
        resume_supported = bool(run.get("checkpoint_context_available")) or index == 0
        candidates.append({
            "step_id": step_id,
            "label": f"{step_id} ({step_tool})",
            "kind": "retry_failed_step" if is_failed else "branch_from_checkpoint",
            "status": step_status,
            "step_tool": step_tool,
            "resume_draft": (
                _workflow_retry_from_step_draft(
                    workflow_name,
                    step_id=step_id,
                    arguments=arguments,
                    parent_run_identity=str(run.get("run_identity") or "") or None,
                    root_run_identity=root_run_identity or None,
                    branch_kind="retry_failed_step" if is_failed else "branch_from_checkpoint",
                    branch_depth=next_branch_depth,
                )
                if resume_supported
                else None
            ),
            "continue_message": None,
            "resume_supported": resume_supported,
        })
    return candidates


def _resolve_resume_step_id(
    run: dict[str, Any],
    *,
    checkpoint_candidates: list[dict[str, Any]],
    requested_step_id: str | None = None,
) -> str | None:
    candidate_ids = {
        str(checkpoint.get("step_id") or "")
        for checkpoint in checkpoint_candidates
        if isinstance(checkpoint, dict)
    }
    has_pending_approval = "approval_gate" in candidate_ids
    if isinstance(requested_step_id, str) and requested_step_id.strip():
        normalized = requested_step_id.strip()
        if normalized not in candidate_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow run '{run['run_identity']}' has no checkpoint '{normalized}'",
            )
        if has_pending_approval and normalized != "approval_gate":
            raise HTTPException(
                status_code=409,
                detail="Workflow run must clear the approval gate before branching from a later checkpoint",
            )
        if normalized == "approval_gate":
            return normalized
        return normalized
    if run.get("resume_from_step"):
        normalized = str(run["resume_from_step"])
        if normalized in candidate_ids:
            selected_checkpoint = next(
                (
                    checkpoint for checkpoint in checkpoint_candidates
                    if str(checkpoint.get("step_id") or "") == normalized
                ),
                None,
            )
            if (
                normalized != "approval_gate"
                and isinstance(selected_checkpoint, dict)
                and selected_checkpoint.get("resume_supported") is False
            ):
                return None
            return normalized
    return None


def _workflow_resume_plan(
    run: dict[str, Any],
    *,
    approvals: list[dict[str, Any]],
    requested_step_id: str | None = None,
) -> dict[str, Any]:
    checkpoint_candidates = _workflow_checkpoint_candidates(run, approvals=approvals)
    resume_step_id = _resolve_resume_step_id(
        run,
        checkpoint_candidates=checkpoint_candidates,
        requested_step_id=requested_step_id,
    )
    selected_checkpoint = next(
        (
            checkpoint for checkpoint in checkpoint_candidates
            if str(checkpoint.get("step_id") or "") == str(resume_step_id or "")
        ),
        None,
    )
    if (
        isinstance(selected_checkpoint, dict)
        and resume_step_id is not None
        and resume_step_id != "approval_gate"
        and selected_checkpoint.get("resume_supported") is False
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Workflow run '{run['run_identity']}' cannot branch from checkpoint "
                f"'{resume_step_id}' because the parent run did not persist reusable checkpoint state"
            ),
        )
    branch_kind = str(run.get("branch_kind") or "replay_from_start")
    if resume_step_id == "approval_gate":
        branch_kind = "approval_resume"
    elif isinstance(selected_checkpoint, dict):
        checkpoint_kind = str(selected_checkpoint.get("kind") or "")
        if checkpoint_kind == "retry_failed_step":
            branch_kind = "retry_failed_step"
        elif checkpoint_kind == "branch_from_checkpoint":
            branch_kind = "branch_from_checkpoint"
    replay_draft = (
        str(selected_checkpoint.get("resume_draft"))
        if isinstance(selected_checkpoint, dict) and selected_checkpoint.get("resume_draft")
        else run.get("retry_from_step_draft")
    )
    if not replay_draft and run.get("replay_draft"):
        replay_draft = str(run["replay_draft"])
    return {
        "source_run_identity": run["run_identity"],
        "parent_run_identity": run["run_identity"],
        "root_run_identity": run.get("root_run_identity") or run["run_identity"],
        "branch_kind": branch_kind,
        "resume_from_step": resume_step_id,
        "resume_checkpoint_label": (
            selected_checkpoint.get("label")
            if isinstance(selected_checkpoint, dict)
            else run.get("resume_checkpoint_label")
        ),
        "replay_allowed": bool(run.get("replay_allowed")),
        "replay_block_reason": run.get("replay_block_reason"),
        "draft": replay_draft,
        "continue_message": (
            selected_checkpoint.get("continue_message")
            if isinstance(selected_checkpoint, dict)
            else None
        ) or run.get("thread_continue_message") or run.get("approval_recovery_message"),
        "requires_manual_execution": True,
        "checkpoint_candidates": checkpoint_candidates,
    }


def _parse_run_identity(run_identity: str) -> tuple[str | None, str, str, str | None]:
    try:
        return parse_workflow_run_identity(run_identity)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Workflow run '{run_identity}' not found") from exc


def _serialize_audit_event(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "session_id": event.session_id,
        "actor": event.actor,
        "event_type": event.event_type,
        "tool_name": event.tool_name,
        "risk_level": event.risk_level,
        "policy_mode": event.policy_mode,
        "summary": event.summary,
        "details": json.loads(event.details_json) if event.details_json else None,
        "created_at": event.created_at.isoformat(),
    }


async def _load_workflow_events_for_identity(run_identity: str) -> tuple[list[dict[str, Any]], str | None]:
    session_id, tool_name, _run_fingerprint, run_discriminator = _parse_run_identity(run_identity)
    async with get_session() as db:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.tool_name == tool_name)
            .order_by(col(AuditEvent.created_at).desc())
        )
        if session_id is None:
            stmt = stmt.where(col(AuditEvent.session_id).is_(None))
        else:
            stmt = stmt.where(AuditEvent.session_id == session_id)
        result = await db.execute(stmt)
        events = result.scalars().all()
    serialized = [_serialize_audit_event(event) for event in events]
    if run_discriminator is None:
        return serialized, session_id
    return [
        event
        for event in serialized
        if _workflow_identity_discriminator(event, _as_record(event.get("details"))) == run_discriminator
    ], session_id


def _workflow_replay_policy(
    *,
    availability: str,
    risk_level: str,
    execution_boundaries: list[str],
    accepts_secret_refs: bool,
    pending_approval_count: int,
    approval_context_mismatch: bool,
    approval_context_missing_for_protected_surface: bool,
) -> tuple[bool, str | None]:
    if availability == "disabled":
        return False, "workflow_disabled"
    if availability != "ready":
        return False, "workflow_unavailable"
    if approval_context_mismatch:
        return False, "approval_context_changed"
    if approval_context_missing_for_protected_surface:
        return False, "approval_context_missing"
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


def _workflow_resume_surface_allowed(*, replay_block_reason: str | None) -> bool:
    return replay_block_reason not in {"approval_context_changed", "approval_context_missing"}


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


def _step_recovery_recommended_actions(
    *,
    step: dict[str, Any],
    workflow_status: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    actions = _workflow_replay_recommended_actions(workflow_status)
    step_tool = str(step.get("tool") or "")
    missing_tools = workflow_status.get("missing_tools") if isinstance(workflow_status, dict) else []
    if not isinstance(missing_tools, list):
        missing_tools = []
    step_requires_policy_repair = (
        step_tool
        and step_tool != "unknown"
        and str(workflow_status.get("availability") or "") == "blocked"
        and step_tool in {str(tool) for tool in missing_tools}
    )
    if step_requires_policy_repair:
        current_tool_mode = get_current_tool_policy_mode()
        suggested_mode = _recommended_tool_policy_mode(
            current_mode=current_tool_mode,
            blocked_reason=None,
        )
        if suggested_mode is not None:
            actions.append({
                "type": "set_tool_policy",
                "label": f"Allow {step_tool}",
                "mode": suggested_mode,
            })
    seen: set[tuple[str, str | None, str | None]] = set()
    deduped: list[dict[str, Any]] = []
    for action in actions:
        key = (
            str(action.get("type") or ""),
            str(action.get("name")) if action.get("name") is not None else None,
            str(action.get("mode")) if action.get("mode") is not None else None,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _step_recovery_hint(step: dict[str, Any]) -> str | None:
    step_tool = str(step.get("tool") or "step")
    error_kind = str(step.get("error_kind") or "").strip()
    error_summary = str(step.get("error_summary") or "").strip()
    if error_kind or error_summary:
        base = error_summary or error_kind.replace("_", " ")
        return f"{step_tool} failed and needs repair before replay"
    return f"Review {step_tool} inputs and retry this step"


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
            "at": step.get("completed_at") or step.get("started_at") or run["updated_at"],
            "summary": (
                f"{step_id} ({step_tool}) {step_status.replace('_', ' ')}"
                + (f" · {result_summary}" if result_summary else "")
            ),
            "step_id": step_id,
            "step_tool": step_tool,
            "result_summary": result_summary,
            "error_kind": step.get("error_kind"),
            "error_summary": step.get("error_summary"),
            "duration_ms": step.get("duration_ms"),
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
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if events is None:
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
    workflow_runtime_contexts = _workflow_runtime_approval_contexts()
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
                "checkpoint_step_ids": [],
                "last_completed_step_id": None,
                "artifact_paths": _extract_artifact_paths(arguments),
                "continued_error_steps": [],
                "arguments": arguments,
                "approval_context": _normalize_approval_context(
                    details.get("approval_context"),
                    workflow_name=str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
                ),
            })
            continue

        run_queue = pending_by_key.get(key, [])
        run_discriminator = _workflow_run_discriminator(details)
        if not run_queue and run_discriminator is not None:
            legacy_key = build_workflow_run_identity(
                event.get("session_id") if isinstance(event.get("session_id"), str) else None,
                tool_name,
                run_fingerprint,
            )
            legacy_queue = pending_by_key.get(legacy_key, [])
            legacy_index = next(
                (
                    index
                    for index, pending_run in enumerate(legacy_queue)
                    if str(pending_run.get("id") or "") == run_discriminator
                ),
                None,
            )
            if legacy_index is not None:
                key = legacy_key
                run_queue = pending_by_key.get(legacy_key, [])
                run = run_queue.pop(legacy_index)
            else:
                run = None
        else:
            run = None
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
        if run is None:
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
            "checkpoint_step_ids": [],
            "last_completed_step_id": None,
            "artifact_paths": [],
            "continued_error_steps": [],
            "arguments": _as_record(details.get("arguments")) or None,
            "approval_context": _normalize_approval_context(
                details.get("approval_context"),
                workflow_name=str(details.get("workflow_name") or _workflow_name_from_tool(tool_name)),
            ),
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
        recorded_approval_context = (
            _normalize_approval_context(
                details.get("approval_context"),
                workflow_name=str(run["workflow_name"]),
            )
            or _normalize_approval_context(
                run.get("approval_context"),
                workflow_name=str(run["workflow_name"]),
            )
        )
        current_approval_context = _workflow_current_approval_context(
            workflow_name=str(run["workflow_name"]),
            workflow_meta={
                **workflow_meta,
                "approval_context": workflow_runtime_contexts.get(tool_name)
                or workflow_meta.get("approval_context"),
            },
        )
        effective_approval_context = recorded_approval_context or current_approval_context
        approval_context_mismatch = bool(
            recorded_approval_context is not None
            and recorded_approval_context != current_approval_context
        )
        approval_context_missing_for_protected_surface = bool(
            recorded_approval_context is None
            and approval_context_requires_tracked_lineage(current_approval_context)
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
            "checkpoint_step_ids": [
                value for value in details.get("checkpoint_step_ids", [])
                if isinstance(value, str)
            ] or run.get("checkpoint_step_ids", []),
            "last_completed_step_id": (
                str(details.get("last_completed_step_id"))
                if details.get("last_completed_step_id") is not None
                else run.get("last_completed_step_id")
            ),
            "artifact_paths": artifact_paths,
            "continued_error_steps": [
                value for value in details.get("continued_error_steps", [])
                if isinstance(value, str)
            ] or run.get("continued_error_steps", []),
            "runtime_profile": (
                str(details.get("runtime_profile"))
                if details.get("runtime_profile") is not None
                else workflow_meta.get("runtime_profile", "")
            ),
            "output_surface": (
                str(details.get("output_surface"))
                if details.get("output_surface") is not None
                else workflow_meta.get("output_surface", "")
            ),
            "canvas_output": (
                details.get("canvas_output")
                if isinstance(details.get("canvas_output"), dict)
                else run.get("canvas_output")
            ),
            "checkpoint_context_available": bool(
                details.get("checkpoint_context_available")
                or isinstance(details.get("checkpoint_context"), dict)
            ),
            "approval_context": effective_approval_context,
            "recorded_approval_context": recorded_approval_context,
            "current_approval_context": current_approval_context,
            "approval_context_mismatch": approval_context_mismatch,
            "risk_level": (
                str(effective_approval_context.get("risk_level"))
                if effective_approval_context is not None
                else workflow_meta.get("risk_level", "high")
            ),
            "execution_boundaries": (
                list(effective_approval_context.get("execution_boundaries", []))
                if effective_approval_context is not None
                else workflow_meta.get("execution_boundaries", ["unknown"])
            ),
            "accepts_secret_refs": (
                bool(effective_approval_context.get("accepts_secret_refs", False))
                if effective_approval_context is not None
                else bool(workflow_meta.get("accepts_secret_refs", False))
            ),
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
        step_records = run.get("step_records") or []
        if isinstance(step_records, list):
            for step in step_records:
                if not isinstance(step, dict):
                    continue
                step["recovery_actions"] = _step_recovery_recommended_actions(
                    step=step,
                    workflow_status=workflow_status,
                )
                step["recovery_hint"] = _step_recovery_hint(step)
                step["is_recoverable"] = bool(step["recovery_actions"])
        replay_allowed, replay_block_reason = _workflow_replay_policy(
            availability=str(run["availability"]),
            risk_level=str(run["risk_level"]),
            execution_boundaries=list(run["execution_boundaries"]),
            accepts_secret_refs=bool(run["accepts_secret_refs"]),
            pending_approval_count=len(approvals),
            approval_context_mismatch=bool(run.get("approval_context_mismatch")),
            approval_context_missing_for_protected_surface=approval_context_missing_for_protected_surface,
        )
        run_identity = build_workflow_run_identity(
            run.get("session_id") if isinstance(run.get("session_id"), str) else None,
            tool_name,
            run_fingerprint,
            run_discriminator=run_discriminator,
        )
        lineage = _workflow_branch_lineage(
            run_identity=run_identity,
            details=details,
            approvals=approvals,
            continued_error_steps=list(run.get("continued_error_steps", [])),
        )
        resume_surface_allowed = _workflow_resume_surface_allowed(
            replay_block_reason=replay_block_reason,
        )
        resume_from_step = (
            (
                "approval_gate"
                if approvals
                else (run["continued_error_steps"][0] if run.get("continued_error_steps") else None)
            )
            if resume_surface_allowed
            else None
        )
        retry_from_step_draft = (
            _workflow_retry_from_step_draft(
                str(run["workflow_name"]),
                step_id=str(run["continued_error_steps"][0]),
                arguments=run.get("arguments"),
                parent_run_identity=run_identity,
                root_run_identity=str(lineage.get("root_run_identity") or run_identity),
                branch_kind="retry_failed_step",
                branch_depth=int(lineage.get("branch_depth") or 0) + 1,
            )
            if resume_surface_allowed
            and replay_allowed
            and run.get("continued_error_steps")
            and (
                bool(run.get("checkpoint_context_available"))
                or (
                    isinstance(run.get("step_records"), list)
                    and run.get("step_records")
                    and str(run["continued_error_steps"][0]) == str(run["step_records"][0].get("id") or "")
                )
            )
            else None
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
            "resume_from_step": resume_from_step,
            "retry_from_step_draft": retry_from_step_draft,
            "resume_checkpoint_label": _resume_checkpoint_label(
                approvals=approvals,
                continued_error_steps=list(run.get("continued_error_steps", [])),
            ) if resume_surface_allowed else None,
            "approval_recovery_message": (
                f"Workflow '{run['workflow_name']}' changed its trust boundary after this run. Start a fresh run instead of replaying or resuming."
                if bool(run.get("approval_context_mismatch"))
                else (
                    f"Workflow '{run['workflow_name']}' predates trust-boundary tracking for its current privileged surface. Start a fresh run instead of replaying or resuming."
                    if approval_context_missing_for_protected_surface
                    else (
                        f"Review pending approval(s) for workflow '{run['workflow_name']}' before replaying."
                        if len(approvals) > 0
                        else (
                            f"Repair workflow '{run['workflow_name']}' before replaying."
                            if str(run["availability"]) != "ready"
                            else None
                        )
                    )
                )
            ),
            "thread_continue_message": (
                approvals[0].get("resume_message")
                if approvals and isinstance(approvals[0], dict)
                else None
            ),
            "run_identity": run_identity,
            "timeline": _timeline_entries_for_run(run, approvals=approvals),
            "failed_step_tool": (
                next(
                    (
                        str(step.get("tool") or "")
                        for step in run.get("step_records", []) or []
                        if isinstance(step, dict)
                        and str(step.get("id") or "") in set(run.get("continued_error_steps", []))
                    ),
                    None,
                )
            ),
            **lineage,
        })
        if resume_surface_allowed:
            run["checkpoint_candidates"] = _workflow_checkpoint_candidates(run, approvals=approvals)
            run["resume_plan"] = _workflow_resume_plan(run, approvals=approvals)
        else:
            run["checkpoint_candidates"] = []
            run["resume_plan"] = None
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
            recorded_approval_context = _normalize_approval_context(
                run.get("approval_context"),
                workflow_name=str(run["workflow_name"]),
            )
            current_approval_context = _workflow_current_approval_context(
                workflow_name=str(run["workflow_name"]),
                workflow_meta={
                    **workflow_meta,
                    "approval_context": workflow_runtime_contexts.get(str(run["tool_name"]))
                    or workflow_meta.get("approval_context"),
                },
            )
            effective_approval_context = recorded_approval_context or current_approval_context
            approval_context_mismatch = bool(
                recorded_approval_context is not None
                and recorded_approval_context != current_approval_context
            )
            approval_context_missing_for_protected_surface = bool(
                recorded_approval_context is None
                and approval_context_requires_tracked_lineage(current_approval_context)
            )
            run.update({
                "approval_context": effective_approval_context,
                "recorded_approval_context": recorded_approval_context,
                "current_approval_context": current_approval_context,
                "approval_context_mismatch": approval_context_mismatch,
                "checkpoint_context_available": bool(run.get("checkpoint_context_available")),
                "risk_level": (
                    str(effective_approval_context.get("risk_level"))
                    if effective_approval_context is not None
                    else workflow_meta.get("risk_level", "high")
                ),
                "execution_boundaries": (
                    list(effective_approval_context.get("execution_boundaries", []))
                    if effective_approval_context is not None
                    else workflow_meta.get("execution_boundaries", ["unknown"])
                ),
                "accepts_secret_refs": (
                    bool(effective_approval_context.get("accepts_secret_refs", False))
                    if effective_approval_context is not None
                    else bool(workflow_meta.get("accepts_secret_refs", False))
                ),
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
                "checkpoint_step_ids": list(run.get("checkpoint_step_ids", [])),
                "last_completed_step_id": run.get("last_completed_step_id"),
                "parameter_schema": (
                    workflow_status.get("inputs", {})
                    if workflow_status is not None and isinstance(workflow_status.get("inputs"), dict)
                    else {}
                ),
                "replay_recommended_actions": _workflow_replay_recommended_actions(workflow_status),
            })
            step_records = run.get("step_records") or []
            if isinstance(step_records, list):
                for step in step_records:
                    if not isinstance(step, dict):
                        continue
                    step["recovery_actions"] = _step_recovery_recommended_actions(
                        step=step,
                        workflow_status=workflow_status,
                    )
                    step["recovery_hint"] = _step_recovery_hint(step)
                    step["is_recoverable"] = bool(step["recovery_actions"])
            replay_allowed, replay_block_reason = _workflow_replay_policy(
                availability=str(run["availability"]),
                risk_level=str(run["risk_level"]),
                execution_boundaries=list(run["execution_boundaries"]),
                accepts_secret_refs=bool(run["accepts_secret_refs"]),
                pending_approval_count=len(approvals),
                approval_context_mismatch=bool(run.get("approval_context_mismatch")),
                approval_context_missing_for_protected_surface=approval_context_missing_for_protected_surface,
            )
            run_identity = build_workflow_run_identity(
                run.get("session_id") if isinstance(run.get("session_id"), str) else None,
                str(run["tool_name"]),
                str(run.get("run_fingerprint") or "none"),
                run_discriminator=(
                    str(run.get("id"))
                    if isinstance(run.get("id"), str) and str(run.get("id")).strip()
                    else None
                ),
            )
            lineage = _workflow_branch_lineage(
                run_identity=run_identity,
                details={},
                approvals=approvals,
                continued_error_steps=list(run.get("continued_error_steps", [])),
            )
            resume_surface_allowed = _workflow_resume_surface_allowed(
                replay_block_reason=replay_block_reason,
            )
            resume_from_step = (
                (
                    "approval_gate"
                    if approvals
                    else (run["continued_error_steps"][0] if run.get("continued_error_steps") else None)
                )
                if resume_surface_allowed
                else None
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
                "resume_from_step": resume_from_step,
                "retry_from_step_draft": (
                    _workflow_retry_from_step_draft(
                        str(run["workflow_name"]),
                        step_id=str(run["continued_error_steps"][0]),
                        arguments=run.get("arguments"),
                        parent_run_identity=run_identity,
                        root_run_identity=str(lineage.get("root_run_identity") or run_identity),
                        branch_kind="retry_failed_step",
                        branch_depth=int(lineage.get("branch_depth") or 0) + 1,
                    )
                    if resume_surface_allowed
                    and replay_allowed
                    and run.get("continued_error_steps")
                    and (
                        bool(run.get("checkpoint_context_available"))
                        or (
                            isinstance(run.get("step_records"), list)
                            and run.get("step_records")
                            and str(run["continued_error_steps"][0]) == str(run["step_records"][0].get("id") or "")
                        )
                    )
                    else None
                ),
                "resume_checkpoint_label": _resume_checkpoint_label(
                    approvals=approvals,
                    continued_error_steps=list(run.get("continued_error_steps", [])),
                ) if resume_surface_allowed else None,
                "approval_recovery_message": (
                    f"Workflow '{run['workflow_name']}' changed its trust boundary after this run. Start a fresh run instead of replaying or resuming."
                    if bool(run.get("approval_context_mismatch"))
                    else (
                        f"Workflow '{run['workflow_name']}' predates trust-boundary tracking for its current privileged surface. Start a fresh run instead of replaying or resuming."
                        if approval_context_missing_for_protected_surface
                        else (
                            f"Review pending approval(s) for workflow '{run['workflow_name']}' before replaying."
                            if len(approvals) > 0
                            else (
                                f"Repair workflow '{run['workflow_name']}' before replaying."
                                if str(run["availability"]) != "ready"
                                else None
                            )
                        )
                    )
                ),
                "thread_continue_message": (
                    approvals[0].get("resume_message")
                    if approvals and isinstance(approvals[0], dict)
                    else None
                ),
                "run_identity": run_identity,
                "timeline": _timeline_entries_for_run(run, approvals=approvals),
                "failed_step_tool": (
                    next(
                        (
                            str(step.get("tool") or "")
                            for step in run.get("step_records", []) or []
                            if isinstance(step, dict)
                            and str(step.get("id") or "") in set(run.get("continued_error_steps", []))
                        ),
                        None,
                    )
                ),
                **lineage,
            })
            if resume_surface_allowed:
                run["checkpoint_candidates"] = _workflow_checkpoint_candidates(run, approvals=approvals)
                run["resume_plan"] = _workflow_resume_plan(run, approvals=approvals)
            else:
                run["checkpoint_candidates"] = []
                run["resume_plan"] = None
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


@router.get("/workflows/diagnostics")
async def workflow_diagnostics():
    diagnostics = workflow_manager.get_diagnostics()
    return diagnostics


@router.get("/workflows/runtimes")
async def list_workflow_runtimes():
    snapshot = _workflow_extension_snapshot()
    inventory = list_workflow_runtime_inventory(snapshot.list_contributions("workflow_runtimes"))
    return {
        "runtimes": [
            {
                "extension_id": item.extension_id,
                "name": item.name,
                "engine_kind": item.engine_kind,
                "description": item.description,
                "delegation_mode": item.delegation_mode,
                "checkpoint_policy": item.checkpoint_policy,
                "structured_output": item.structured_output,
                "default_output_surface": item.default_output_surface,
                "reference": item.reference,
            }
            for item in inventory
        ]
    }


@router.get("/workflows/{name}/source")
async def get_workflow_source(name: str):
    workflow = workflow_manager.get_workflow(name)
    if workflow is None or not workflow.file_path:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    try:
        with open(workflow.file_path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read workflow source: {exc}") from exc
    validation = _validate_workflow_content(content, path=workflow.file_path)
    return {
        "name": name,
        "file_path": workflow.file_path,
        "content": content,
        **validation,
    }


@router.post("/workflows/validate")
async def validate_workflow_draft(req: WorkflowDraftRequest):
    return _validate_workflow_content(req.content, path=req.file_name or "<draft>")


@router.post("/workflows/save")
async def save_workflow_draft(req: WorkflowDraftRequest):
    validation = _validate_workflow_content(req.content, path=req.file_name or "<draft>")
    if not bool(validation["valid"]) or not isinstance(validation["workflow"], dict):
        raise HTTPException(status_code=400, detail={"message": "Workflow draft is invalid", **validation})
    file_name = _resolve_workflow_file_name(
        req.file_name,
        default_name=_safe_markdown_filename(str(validation["workflow"]["name"])),
    )
    _ensure_workflow_manager_workspace_extensions_loaded()
    target_path = str(save_workspace_contribution("workflows", file_name=file_name, content=req.content))
    workflows = workflow_manager.reload()
    await log_integration_event(
        integration_type="workflow",
        name=str(validation["workflow"]["name"]),
        outcome="succeeded",
        details={
            "saved_path": target_path,
            "validation": validation,
        },
    )
    return {
        "status": "saved",
        "file_path": target_path,
        "workflows": workflows,
        **_validate_workflow_content(req.content, path=target_path),
    }


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


@router.post("/workflows/runs/{run_identity:path}/resume-plan")
async def build_workflow_resume_plan(
    run_identity: str,
    req: WorkflowResumePlanRequest | None = None,
):
    runs = await _list_workflow_runs(limit=100, session_id=None)
    run = next((item for item in runs if item.get("run_identity") == run_identity), None)
    if run is None:
        scoped_events, scoped_session_id = await _load_workflow_events_for_identity(run_identity)
        if scoped_events:
            runs = await _list_workflow_runs(limit=max(len(scoped_events), 1), session_id=scoped_session_id, events=scoped_events)
            run = next((item for item in runs if item.get("run_identity") == run_identity), None)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Workflow run '{run_identity}' not found")
    if str(run.get("replay_block_reason") or "") in {"approval_context_changed", "approval_context_missing"}:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Workflow run '{run_identity}' cannot resume because its trust boundary "
                "changed after the original run. Start a fresh run instead."
                if str(run.get("replay_block_reason") or "") == "approval_context_changed"
                else (
                    f"Workflow run '{run_identity}' cannot resume because it predates trust-boundary "
                    "tracking for the current privileged workflow surface. Start a fresh run instead."
                )
            ),
        )
    resume_plan = _workflow_resume_plan(
        run,
        approvals=list(run.get("pending_approvals", [])),
        requested_step_id=req.step_id if req is not None else None,
    )
    return {
        "run_identity": run_identity,
        "workflow_name": run["workflow_name"],
        "resume_plan": resume_plan,
    }
