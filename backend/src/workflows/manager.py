"""Workflow manager and runtime."""

from __future__ import annotations

import asyncio
import contextvars
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any

from smolagents import Tool
from sqlmodel import col, select

from src.audit.formatting import format_tool_call_summary, redact_for_audit
from src.approval.runtime import get_current_session_id, get_current_trust_principal
from src.db.engine import get_session
from src.db.models import AuditEvent
from src.extensions.governance import build_governance_status
from src.extensions.permissions import evaluate_tool_permissions
from src.extensions.registry import ExtensionRegistry, ExtensionRegistrySnapshot
from src.extensions.state import extension_state_entries, load_extension_state_payload
from src.memory.flush import flush_session_memory_sync
from src.approval.repository import fingerprint_tool_call
from src.native_tools.registry import TOOL_METADATA, canonical_tool_name
from src.tools.policy import get_tool_source_context, tool_accepts_secret_refs
from src.workflows.loader import Workflow, scan_workflow_paths
from src.workflows.durable_state import WorkflowStateRepository, workflow_state_repository
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobRepository,
    DurableJobSpec,
    durable_job_repository,
    durable_lease_id,
)
from src.workflows.run_identity import build_workflow_run_identity, parse_workflow_run_identity

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
    "_seraph_parent_revision": {
        "type": "integer",
        "description": "Durable revision expected for the parent workflow checkpoint.",
        "nullable": True,
    },
    "_seraph_parent_lease_id": {
        "type": "string",
        "description": "Durable lease id expected for the parent workflow checkpoint.",
        "nullable": True,
    },
    "_seraph_parent_fencing_token": {
        "type": "integer",
        "description": "Durable fencing token expected for the parent workflow checkpoint.",
        "nullable": True,
    },
}
_WORKFLOW_CONTROL_FIELD_NAMES = set(_WORKFLOW_CONTROL_INPUTS)


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    caller_context = contextvars.copy_context()

    def runner() -> None:
        try:
            result["value"] = caller_context.run(asyncio.run, coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    # A synchronous tool can be called from an async request loop.  Keep the
    # compatibility bridge bounded so a stuck DB operation cannot pin the
    # request thread forever or silently leave a second event loop behind.
    thread.join(timeout=30.0)
    if thread.is_alive():
        raise RuntimeError("durable workflow async bridge timed out after 30 seconds")
    if "error" in result:
        raise result["error"]
    if "value" in result:
        return result["value"]
    return None


def _workflow_durable_owner_fields() -> dict[str, str]:
    """Carry the authenticated execution principal into durable workflow state."""
    principal = get_current_trust_principal()
    if principal is None:
        return {}
    current_session_id = str(get_current_session_id() or "").strip()
    principal_session_id = str(getattr(principal, "session_id", "") or "").strip()
    if (
        not principal.authenticated
        or principal.revoked
        or not current_session_id
        or not principal_session_id
        or principal_session_id != current_session_id
    ):
        raise DurableWorkflowStateUnavailable(
            "canonical workflow admission requires a current session-bound principal"
        )
    principal_id = str(principal.principal_id or "").strip()
    principal_type = getattr(principal.principal_type, "value", principal.principal_type)
    if not principal_id:
        raise DurableWorkflowStateUnavailable(
            "canonical workflow admission requires a non-empty principal identity"
        )
    if str(principal_type or "").strip().lower() == "operator":
        return {
            "owner_kind": "user",
            "owner_principal_id": principal_id,
        }
    if str(principal_type or "").strip().lower() == "service":
        return {
            "owner_kind": "service",
            "owner_principal_id": principal_id,
            "service_id": principal_id,
        }
    raise DurableWorkflowStateUnavailable(
        "canonical workflow admission principal type is unsupported"
    )


def _workflow_recovery_owner(principal_id: str, session_id: str) -> str:
    """Return the stable owner label used by operator recovery leases."""
    principal_digest = hashlib.sha256(principal_id.encode("utf-8", errors="replace")).hexdigest()[:16]
    session_digest = hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"operator:{principal_digest}:{session_digest}"


def _workflow_canonical_lease_owner(run_identity: str) -> str:
    """Return the stable runner lease owner for a typed workflow row.

    The API recovery path reads the same canonical row as the workflow runner.
    Keeping this derivation in one helper prevents recovery from accidentally
    substituting its legacy operator lease label for the runner fence.
    """
    digest = hashlib.sha256(str(run_identity).encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"workflow-runner:{digest}"


def _workflow_parent_v2_state(details: dict[str, Any]) -> tuple[int | None, dict[str, Any]]:
    metadata = details.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    orchestration = details.get("orchestration_v2")
    if not isinstance(orchestration, dict):
        orchestration = metadata.get("orchestration_v2")
    if not isinstance(orchestration, dict):
        orchestration = {}
    raw_revision = orchestration.get("revision", details.get("revision"))
    try:
        revision = int(raw_revision) if raw_revision is not None else None
    except (TypeError, ValueError):
        revision = None
    lease = orchestration.get("lease", details.get("lease"))
    return revision, lease if isinstance(lease, dict) else {}


def _assert_workflow_parent_recovery_authority(
    *,
    parent_run_identity: str,
    details: dict[str, Any],
    control_inputs: dict[str, Any],
) -> None:
    """Fail closed before reusing checkpoint data from a caller-supplied run.

    A parent identity is only a lookup key.  Reuse additionally requires a
    durable owner/session binding and the active lease revision acquired by the
    authenticated recovery route.
    """
    principal = get_current_trust_principal()
    current_session_id = get_current_session_id()
    if (
        principal is None
        or not principal.authenticated
        or principal.revoked
        or not str(principal.principal_id or "").strip()
        or not current_session_id
        or not str(principal.session_id or "").strip()
        or str(principal.session_id).strip() != str(current_session_id).strip()
    ):
        raise RuntimeError("Workflow checkpoint recovery requires an authenticated session-bound principal")

    try:
        parent_session_id, _tool_name, _fingerprint, _discriminator = parse_workflow_run_identity(
            parent_run_identity
        )
    except ValueError as exc:
        raise RuntimeError("Workflow checkpoint recovery identity is invalid") from exc
    if parent_session_id != current_session_id:
        raise RuntimeError("Workflow checkpoint recovery session does not match the authenticated session")
    if details.get("state_source") != "durable_workflow_state":
        raise RuntimeError("Workflow checkpoint recovery requires durable parent state")
    if str(details.get("durable_run_identity") or "").strip() != parent_run_identity:
        raise RuntimeError("Workflow checkpoint recovery durable identity is not bound to the requested parent")
    if str(details.get("session_id") or "").strip() != str(current_session_id).strip():
        raise RuntimeError("Workflow checkpoint recovery parent session is not bound to the authenticated session")

    principal_type = getattr(principal.principal_type, "value", principal.principal_type)
    expected_owner_kind = "user" if str(principal_type or "").strip().lower() == "operator" else (
        "service" if str(principal_type or "").strip().lower() == "service" else None
    )
    owner_kind = str(details.get("owner_kind") or "").strip().lower()
    owner_principal_id = str(details.get("owner_principal_id") or "").strip()
    if (
        expected_owner_kind is None
        or owner_kind != expected_owner_kind
        or not owner_principal_id
        or owner_principal_id != str(principal.principal_id).strip()
    ):
        raise RuntimeError("Workflow checkpoint recovery parent owner is not bound to the authenticated principal")
    if expected_owner_kind == "service" and str(details.get("service_id") or "").strip() != owner_principal_id:
        raise RuntimeError("Workflow checkpoint recovery service owner binding is invalid")

    revision, lease = _workflow_parent_v2_state(details)
    expected_revision = control_inputs.get("_seraph_parent_revision")
    try:
        expected_revision = int(expected_revision) if expected_revision is not None else None
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Workflow checkpoint recovery revision is invalid") from exc
    expected_lease_id = str(control_inputs.get("_seraph_parent_lease_id") or "").strip()
    lease_id = str(lease.get("lease_id") or "").strip()
    lease_owner = str(lease.get("owner") or "").strip()
    expires_at = lease.get("expires_at")
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Workflow checkpoint recovery parent lease is invalid") from exc
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if not lease_id or expires <= datetime.now(timezone.utc):
        raise RuntimeError("Workflow checkpoint recovery parent lease is unavailable")
    record_schema_version = 0
    try:
        record_schema_version = int(details.get("record_schema_version") or 0)
    except (TypeError, ValueError):
        raise RuntimeError("Workflow checkpoint recovery schema version is invalid")
    expected_lease_owner = (
        _workflow_canonical_lease_owner(parent_run_identity)
        if record_schema_version >= 2
        else _workflow_recovery_owner(str(principal.principal_id), str(current_session_id))
    )
    if lease_owner != expected_lease_owner:
        raise RuntimeError("Workflow checkpoint recovery parent lease owner is invalid")
    if not expected_lease_id or expected_lease_id != lease_id:
        raise RuntimeError("Workflow checkpoint recovery parent lease does not match the requested lease")
    if revision is None or expected_revision is None or expected_revision != revision:
        raise RuntimeError("Workflow checkpoint recovery parent revision is stale")
    try:
        lease_revision = int(lease.get("revision"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Workflow checkpoint recovery parent lease revision is invalid") from exc
    if lease_revision != revision:
        raise RuntimeError("Workflow checkpoint recovery parent lease revision is stale")
    if record_schema_version >= 2:
        try:
            persisted_fence = int(lease.get("fencing_token"))
        except (TypeError, ValueError):
            persisted_fence = None
        if persisted_fence is None or persisted_fence <= 0:
            raise RuntimeError("Workflow checkpoint recovery parent fence is invalid")
        try:
            expected_fence = int(control_inputs["_seraph_parent_fencing_token"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Workflow checkpoint recovery parent fence is required") from exc
        if expected_fence != persisted_fence:
            raise RuntimeError("Workflow checkpoint recovery parent fence is stale")
        try:
            derived_lease_id = durable_lease_id(parent_run_identity, persisted_fence)
        except ValueError as exc:
            raise RuntimeError("Workflow checkpoint recovery parent lease identity is invalid") from exc
        if lease_id != derived_lease_id or expected_lease_id != derived_lease_id:
            raise RuntimeError("Workflow checkpoint recovery parent lease identity is invalid")


def _run_durable_state_write(coro) -> Any | None:
    try:
        return _run_async(coro)
    except Exception as exc:  # pragma: no cover - defensive fail-soft path for partially migrated/local DBs.
        logger.warning("Durable workflow state write skipped: %s", exc)
        return None


def _is_missing_durable_state_schema_error(exc: Exception) -> bool:
    message = str(exc).lower()
    durable_state_tables = ("workflow_run_states", "workflow_step_states")
    return any(table in message for table in durable_state_tables) and (
        "no such table" in message or "does not exist" in message
    )


class DurableWorkflowStateUnavailable(RuntimeError):
    """Raised when a workflow cannot safely continue without durable state."""

    def __init__(self, message: str, *, phase: str | None = None) -> None:
        super().__init__(message)
        self.phase = phase
        # A write failure after an effect boundary is an uncertain execution,
        # while a pre-admission failure is simply unavailable.  Callers use
        # these stable fields for operator receipts without parsing text.
        self.workflow_status = "degraded"
        self.external_effect_status = "unknown_external_effect"


def _run_required_durable_state_write(
    coro,
    *,
    phase: str,
    allow_missing_schema: bool = False,
) -> Any:
    try:
        return _run_async(coro)
    except Exception as exc:
        if allow_missing_schema and _is_missing_durable_state_schema_error(exc):
            logger.warning("Durable workflow state required write skipped during %s: %s", phase, exc)
            return None
        logger.warning("Durable workflow state required write failed during %s: %s", phase, exc)
        raise DurableWorkflowStateUnavailable(
            f"Durable workflow state unavailable before {phase}; refusing unsafe workflow continuation.",
            phase=phase,
        ) from exc


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


def _safe_workflow_error_summary(exc: Exception) -> str:
    return f"{type(exc).__name__} (details redacted)"


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


def _normalize_credential_egress_policies(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        mode = str(item.get("mode") or "").strip()
        transport = str(item.get("transport") or "").strip()
        allowed_hosts = tuple(_normalize_string_list(item.get("allowed_hosts")))
        key = (mode, transport, allowed_hosts)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "mode": mode or "unknown",
                "transport": transport or "unknown",
                "allowed_hosts": list(allowed_hosts),
            }
        )
    normalized.sort(
        key=lambda item: (
            str(item.get("mode") or ""),
            str(item.get("transport") or ""),
            tuple(item.get("allowed_hosts") or []),
        )
    )
    return normalized


def _normalize_trust_partition(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    mode = str(value.get("mode") or "").strip()
    if not mode:
        return None
    return {
        "mode": mode,
        "background_capable": bool(value.get("background_capable", False)),
        "authenticated_source": bool(value.get("authenticated_source", False)),
        "credential_egress_policy_count": int(value.get("credential_egress_policy_count", 0) or 0),
        "blocked": bool(value.get("blocked", False)),
    }


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
    credential_egress_policies = _normalize_credential_egress_policies(value.get("credential_egress_policies"))
    trust_partition = _normalize_trust_partition(value.get("trust_partition"))
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
            credential_egress_policies,
            trust_partition,
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
    if credential_egress_policies:
        normalized["credential_egress_policies"] = credential_egress_policies
    if trust_partition:
        normalized["trust_partition"] = trust_partition
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
    if _normalize_credential_egress_policies(value.get("credential_egress_policies")):
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


def _durable_redacted_payload(value: Any) -> dict[str, Any]:
    argument_keys: list[str] = []
    if isinstance(value, dict):
        argument_keys = sorted(str(key) for key in value.keys())
    return {
        "redacted": True,
        "reason": "checkpoint_context_disallowed",
        "argument_keys": argument_keys,
    }


def _durable_arguments(value: Any, *, checkpoint_context_allowed: bool) -> dict[str, Any]:
    if checkpoint_context_allowed:
        return value if isinstance(value, dict) else {}
    return _durable_redacted_payload(value)


def _durable_result(value: Any, *, checkpoint_context_allowed: bool) -> Any | None:
    if checkpoint_context_allowed:
        return value
    return None


def _durable_checkpoint(value: Any, *, checkpoint_context_allowed: bool) -> Any | None:
    if checkpoint_context_allowed:
        return _json_safe_value(value)
    return None


def _durable_audit_receipt_id(run_identity: str, status: str) -> str:
    return f"{run_identity}:durable-audit:{status}"


def _delegated_artifact_review_metadata(
    *,
    approval_context: dict[str, Any],
    durable_audit_receipt_id: str,
) -> dict[str, Any]:
    return {
        "durable_audit_receipt_id": durable_audit_receipt_id,
        "risk_level": approval_context.get("risk_level"),
        "execution_boundaries": approval_context.get("execution_boundaries", []),
        "delegated_specialists": approval_context.get("delegated_specialists", []),
        "delegated_tool_names": approval_context.get("delegated_tool_names", []),
        "trust_partition": approval_context.get("trust_partition"),
        "content_redacted": True,
    }


def _record_delegated_artifact_reviews(
    *,
    run_identity: str,
    root_run_identity: str | None,
    parent_run_identity: str | None,
    workflow_name: str,
    approval_context: dict[str, Any],
    artifact_paths: list[str],
    durable_audit_receipt_id: str,
    state_repository: Any | None = None,
) -> None:
    delegated_specialists = [
        str(item)
        for item in approval_context.get("delegated_specialists", [])
        if isinstance(item, str) and item.strip()
    ]
    delegated_tool_names = [
        str(item)
        for item in approval_context.get("delegated_tool_names", [])
        if isinstance(item, str) and item.strip()
    ]
    if not artifact_paths or not (delegated_specialists or delegated_tool_names):
        return
    state_repository = state_repository or workflow_state_repository
    if not hasattr(state_repository, "record_artifact_review"):
        return
    reviewer = delegated_specialists[0] if delegated_specialists else None
    metadata = _delegated_artifact_review_metadata(
        approval_context=approval_context,
        durable_audit_receipt_id=durable_audit_receipt_id,
    )
    for artifact_path in sorted({path for path in artifact_paths if isinstance(path, str) and path.strip()}):
        review_result = _run_workflow_state_write(state_repository, state_repository.record_artifact_review(
            run_identity=run_identity,
            root_run_identity=root_run_identity or run_identity,
            parent_run_identity=parent_run_identity,
            workflow_name=workflow_name,
            artifact_path=artifact_path,
            owner="delegated_specialist",
            review_state="pending_operator_review",
            reviewer=reviewer,
            metadata=metadata,
        ), phase="artifact_review")
        receipt = review_result.get("receipt") if isinstance(review_result, dict) else None
        if isinstance(receipt, dict) and receipt.get("status") == "rejected":
            logger.warning(
                "Delegated artifact review was explicitly rejected: %s",
                receipt.get("reason") or "unknown_reason",
            )


def _workflow_payload_digest(value: Any) -> str:
    """Return a stable digest for a workflow step or output target."""
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _workflow_contract_fields(
    audit_arguments: dict[str, Any],
    approval_context: dict[str, Any],
) -> dict[str, Any]:
    """Project optional goal/plan and execution budget fields into admission."""
    def pick(name: str) -> Any:
        value = audit_arguments.get(name)
        return value if value is not None else approval_context.get(name)

    def optional_text(name: str) -> str | None:
        value = pick(name)
        if value is None or not str(value).strip():
            return None
        normalized = str(value).strip()
        if len(normalized) > 512 or any(ord(char) < 32 for char in normalized):
            raise DurableWorkflowStateUnavailable(f"durable workflow {name} is malformed")
        return normalized

    def optional_int(name: str, default: int | None = None) -> int | None:
        value = pick(name)
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            raise DurableWorkflowStateUnavailable(f"durable workflow {name} is malformed")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise DurableWorkflowStateUnavailable(f"durable workflow {name} is malformed") from exc
        if normalized < 0:
            raise DurableWorkflowStateUnavailable(f"durable workflow {name} is malformed")
        return normalized

    raw_dependencies = pick("dependencies")
    if raw_dependencies is None:
        dependencies: tuple[str, ...] = ()
    elif isinstance(raw_dependencies, str):
        dependencies = (optional_text("dependencies") or "",)
    elif isinstance(raw_dependencies, (list, tuple, set)):
        dependencies = tuple(
            sorted(
                {
                    normalized
                    for item in raw_dependencies
                    if (normalized := str(item).strip())
                }
            )
        )
    else:
        raise DurableWorkflowStateUnavailable("durable workflow dependencies are malformed")
    return {
        "goal_id": optional_text("goal_id"),
        "goal_revision": optional_int("goal_revision"),
        "plan_revision": optional_int("plan_revision"),
        "candidate_id": optional_text("candidate_id"),
        "dependencies": dependencies,
        "deadline_at": pick("deadline_at"),
        "priority": optional_int("priority", 50),
        "max_attempts": optional_int("max_attempts", 1),
        "budget_microusd": optional_int("budget_microusd"),
    }


class _CanonicalWorkflowStateWriter:
    """Adapt the workflow manager's evidence calls to the typed job record.

    ``WorkflowStateRepository`` remains available for historical, untyped
    projections and test doubles. Once a workflow has an authenticated owner,
    this adapter is the only writer used by the manager. It records step
    intent/readback and checkpoint evidence through ``DurableJobRepository``;
    it does not create a second lifecycle or queue.
    """

    def __init__(
        self,
        repository: DurableJobRepository,
        *,
        job: dict[str, Any],
        owner: str,
        fencing_token: int,
        checkpoint_context_allowed: bool,
    ) -> None:
        self.repository = repository
        self.job_id = str(job["job_id"])
        self.owner = owner
        self.fencing_token = int(fencing_token)
        self.revision = int(job.get("revision") or 0)
        self.checkpoint_context_allowed = checkpoint_context_allowed
        persisted_owner = job.get("owner") if isinstance(job.get("owner"), dict) else {}
        self.owner_kind = str(persisted_owner.get("kind") or "")
        self.owner_principal_id = str(persisted_owner.get("principal_id") or "")
        self.service_id = str(persisted_owner.get("service_id") or "") or None
        self.session_id = str(job.get("session_id") or "") or None

    def _assert_runtime_owner(self) -> None:
        """Recheck the ambient principal before every canonical write."""
        fields = _workflow_durable_owner_fields()
        if (
            fields.get("owner_kind") != self.owner_kind
            or fields.get("owner_principal_id") != self.owner_principal_id
            or (fields.get("service_id") or None) != self.service_id
            or str(get_current_session_id() or "") != str(self.session_id or "")
        ):
            raise DurableWorkflowStateUnavailable(
                "canonical workflow write requires the admitted authenticated owner and session"
            )

    def _sync(self, result: dict[str, Any]) -> dict[str, Any]:
        lease = result.get("lease") if isinstance(result, dict) else None
        if isinstance(lease, dict) and lease.get("fencing_token") is not None:
            self.fencing_token = int(lease["fencing_token"])
        if isinstance(result, dict) and result.get("revision") is not None:
            self.revision = int(result["revision"])
        return result

    async def create_run(self, **_kwargs: Any) -> dict[str, Any]:
        """Return the already-admitted row without a legacy write."""
        self._assert_runtime_owner()
        current = await self.repository.get_job(self.job_id)
        if current is None:
            raise RuntimeError("canonical workflow durable job disappeared before execution")
        return self._sync(current)

    async def get_checkpoint_payload(self, run_identity: str) -> dict[str, Any] | None:
        self._assert_runtime_owner()
        current = await self.repository.get_job(run_identity)
        if current is None:
            return None
        checkpoint_context: dict[str, Any] = {}
        step_records: list[dict[str, Any]] = []
        for receipt in current.get("checkpoints", []):
            if not isinstance(receipt, dict):
                continue
            payload = receipt.get("payload")
            if not isinstance(payload, dict):
                continue
            step_id = str(payload.get("step_id") or "").strip()
            state = payload.get("state")
            if not step_id or not isinstance(state, dict):
                continue
            checkpoint_context[step_id] = state
            step_records.append({
                "id": step_id,
                "index": int(payload.get("step_index") or len(step_records) + 1),
                "tool": str(payload.get("tool") or "unknown"),
                "status": str(payload.get("status") or "unknown"),
                "arguments": state.get("arguments", {}),
                "result": state.get("result"),
                "artifact_paths": list(payload.get("artifact_paths") or []),
                "result_summary": payload.get("result_summary"),
                "error_kind": payload.get("error_kind"),
                "error_summary": payload.get("error_summary"),
            })
        if not checkpoint_context:
            return None
        owner = current.get("owner") if isinstance(current.get("owner"), dict) else {}
        authority = current.get("declared_authority")
        lease = current.get("lease") if isinstance(current.get("lease"), dict) else {}
        try:
            current_fence = int(lease.get("fencing_token") or 0)
            lease = {
                **lease,
                "lease_id": durable_lease_id(run_identity, current_fence),
                "revision": int(current.get("revision") or 0),
            }
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("canonical workflow checkpoint lease metadata is malformed") from exc
        return {
            "record_schema_version": int(current.get("record_schema_version") or 0),
            "job_id": current.get("job_id") or run_identity,
            "workflow_name": current.get("workflow_name"),
            "session_id": current.get("session_id"),
            "owner_kind": owner.get("kind"),
            "owner_principal_id": owner.get("principal_id"),
            "service_id": owner.get("service_id"),
            "parent_job_id": current.get("parent_job_id"),
            "parent_fencing_token": current.get("parent_fencing_token"),
            "revision": current.get("revision"),
            "lease": lease,
            "durable_run_identity": run_identity,
            "state_source": "durable_workflow_state",
            "approval_context": authority if isinstance(authority, dict) else {},
            "checkpoint_context": checkpoint_context,
            "step_records": step_records,
            "artifact_paths": [
                item.get("file_path")
                for item in current.get("artifacts", [])
                if isinstance(item, dict) and isinstance(item.get("file_path"), str)
            ],
        }

    async def _record_checkpoint(
        self,
        *,
        checkpoint_id: str,
        state: Any,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._assert_runtime_owner()
        result = await self.repository.record_checkpoint(
            self.job_id,
            checkpoint_id=checkpoint_id,
            state=state,
            checkpoint_payload=payload if self.checkpoint_context_allowed else None,
            owner=self.owner,
            fencing_token=self.fencing_token,
            expected_revision=self.revision,
        )
        return self._sync(result)

    async def record_step_started(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_runtime_owner()
        step_id = str(kwargs.get("step_id") or "").strip()
        tool_name = str(kwargs.get("tool_name") or "unknown")
        arguments = kwargs.get("arguments") if isinstance(kwargs.get("arguments"), dict) else {}
        effect_id = f"workflow-step:{step_id}"
        target_path = f"workflow-step:{self.job_id}:{step_id}"
        target_digest = _workflow_payload_digest(arguments)
        result = await self.repository.record_effect(
            self.job_id,
            effect_type="workflow_step",
            effect_id=effect_id,
            target_path=target_path,
            target_digest=target_digest,
            status="intent",
            details={"step_id": step_id, "tool": tool_name},
            owner=self.owner,
            fencing_token=self.fencing_token,
            expected_revision=self.revision,
        )
        self._sync(result)
        state = {
            "tool": tool_name,
            "arguments": arguments,
            "result": None,
        }
        return await self._record_checkpoint(
            checkpoint_id=f"step:{step_id}",
            state=state,
            payload={
                "step_id": step_id,
                "step_index": int(kwargs.get("step_index") or 0),
                "tool": tool_name,
                "status": "started",
                "state": state,
                "artifact_paths": [],
            },
        )

    async def record_step_completed(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_runtime_owner()
        step_id = str(kwargs.get("step_id") or "").strip()
        checkpoint = kwargs.get("checkpoint") if isinstance(kwargs.get("checkpoint"), dict) else {}
        result_value = kwargs.get("result")
        target_path = f"workflow-step:{self.job_id}:{step_id}"
        target_digest = _workflow_payload_digest(
            checkpoint if checkpoint else {"result": _summarize_value_shape(result_value)}
        )
        # The original intent digest is authoritative. Read it back before
        # changing the checkpoint so an input/result representation cannot
        # silently become a different external target.
        current = await self.repository.get_job(self.job_id)
        prior = next(
            (
                item
                for item in (current or {}).get("effects", [])
                if isinstance(item, dict) and item.get("effect_id") == f"workflow-step:{step_id}"
            ),
            None,
        )
        if isinstance(prior, dict) and prior.get("target_digest"):
            target_digest = str(prior["target_digest"])
        step_status = str(kwargs.get("status") or "succeeded")
        readback = await self.repository.record_readback(
            self.job_id,
            target_path=target_path,
            target_digest=target_digest,
            effect_id=f"workflow-step:{step_id}",
            status="failed" if step_status == "continued_error" else "succeeded",
            content_sha256=_workflow_payload_digest(result_value),
            details={"verified": step_status != "continued_error", "step_id": step_id},
            owner=self.owner,
            fencing_token=self.fencing_token,
            expected_revision=self.revision,
        )
        self._sync(readback)
        artifact_paths = [
            str(path)
            for path in kwargs.get("artifact_paths", [])
            if isinstance(path, str) and path.strip()
        ]
        for path in artifact_paths:
            artifact = await self.repository.record_artifact(
                self.job_id,
                file_path=path,
                owner=self.owner,
                fencing_token=self.fencing_token,
                expected_revision=self.revision,
            )
            self._sync(artifact)
        state = checkpoint or {"result": result_value}
        return await self._record_checkpoint(
            checkpoint_id=f"step:{step_id}",
            state=state,
            payload={
                "step_id": step_id,
                "step_index": int(kwargs.get("step_index") or 0),
                "tool": str(kwargs.get("tool_name") or "unknown"),
                "status": str(kwargs.get("status") or "succeeded"),
                "state": state,
                "artifact_paths": artifact_paths,
                "result_summary": kwargs.get("result_summary"),
                "error_kind": kwargs.get("error_kind"),
                "error_summary": kwargs.get("error_summary"),
            },
        )

    async def record_step_failed(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_runtime_owner()
        step_id = str(kwargs.get("step_id") or "").strip()
        target_path = f"workflow-step:{self.job_id}:{step_id}"
        current = await self.repository.get_job(self.job_id)
        prior = next(
            (
                item
                for item in (current or {}).get("effects", [])
                if isinstance(item, dict) and item.get("effect_id") == f"workflow-step:{step_id}"
            ),
            None,
        )
        target_digest = str(prior.get("target_digest")) if isinstance(prior, dict) and prior.get("target_digest") else _workflow_payload_digest(step_id)
        readback = await self.repository.record_readback(
            self.job_id,
            target_path=target_path,
            target_digest=target_digest,
            effect_id=f"workflow-step:{step_id}",
            status="failed",
            details={"verified": False, "step_id": step_id},
            owner=self.owner,
            fencing_token=self.fencing_token,
            expected_revision=self.revision,
        )
        self._sync(readback)
        artifact_paths = [
            str(path)
            for path in kwargs.get("artifact_paths", [])
            if isinstance(path, str) and path.strip()
        ]
        state = {"result": None, "arguments": {}, "tool": str(kwargs.get("step_id") or "")}
        return await self._record_checkpoint(
            checkpoint_id=f"step:{step_id}",
            state=state,
            payload={
                "step_id": step_id,
                "step_index": 0,
                "tool": str(kwargs.get("step_id") or "unknown"),
                "status": "failed",
                "state": state,
                "artifact_paths": artifact_paths,
                "error_kind": kwargs.get("error_kind"),
                "error_summary": kwargs.get("error_summary"),
            },
        )

    async def adopt_restored_steps(
        self,
        *,
        step_records: list[dict[str, Any]],
        context: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        self._assert_runtime_owner()
        for record in step_records:
            step_id = str(record.get("id") or "").strip()
            state = context.get(step_id)
            if not step_id or not isinstance(state, dict):
                continue
            result = await self._record_checkpoint(
                checkpoint_id=f"step:{step_id}",
                state=state,
                payload={
                    "step_id": step_id,
                    "step_index": int(record.get("index") or 0),
                    "tool": str(record.get("tool") or "unknown"),
                    "status": "checkpoint_reused",
                    "state": state,
                    "artifact_paths": list(record.get("artifact_paths") or []),
                    "result_summary": record.get("result_summary"),
                },
            )
        current = await self.repository.get_job(self.job_id)
        return self._sync(current or {})

    async def finish_run(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_runtime_owner()
        status = str(kwargs.get("status") or "failed")
        metadata = kwargs.get("metadata") if isinstance(kwargs.get("metadata"), dict) else {}
        summary = str(metadata.get("summary") or kwargs.get("error") or status)
        if status in {"succeeded", "completed", "degraded"}:
            output_digest = _workflow_payload_digest(summary)
            readback = await self.repository.record_effect(
                self.job_id,
                effect_type="workflow_output",
                effect_id=f"workflow-output:{self.job_id}",
                receipt_kind="readback",
                target_path=f"workflow-output:{self.job_id}",
                target_digest=output_digest,
                status="succeeded",
                content_sha256=output_digest,
                details={
                    "verified": True,
                    "goal_completion": status in {"succeeded", "completed"},
                    "artifact_delivery": bool(kwargs.get("artifact_paths")),
                    "execution_status": status,
                },
                owner=self.owner,
                fencing_token=self.fencing_token,
                expected_revision=self.revision,
            )
            self._sync(readback)
            completed = await self.repository.transition_job(
                self.job_id,
                "degraded" if status == "degraded" else "succeeded",
                owner=self.owner,
                fencing_token=self.fencing_token,
                expected_revision=self.revision,
                result_summary=summary,
                reason="workflow_completed",
            )
            return self._sync(completed)
        failed = await self.repository.transition_job(
            self.job_id,
            "failed",
            owner=self.owner,
            fencing_token=self.fencing_token,
            expected_revision=self.revision,
            reason=str(kwargs.get("error") or "workflow_failed"),
            result_summary=summary,
        )
        return self._sync(failed)

    async def record_artifact_review(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_runtime_owner()
        path = str(kwargs.get("artifact_path") or "").strip()
        if not path:
            return await self.repository.get_job(self.job_id) or {}
        current = await self.repository.get_job(self.job_id)
        if isinstance(current, dict) and current.get("status") in {"succeeded", "degraded", "cancelled"}:
            return {
                **current,
                "receipt": {
                    "kind": "artifact_review",
                    "status": "rejected",
                    "reason": "terminal_run_review_window_closed",
                    "artifact_path": path,
                    "operator_visible": True,
                },
            }
        review_state = str(kwargs.get("review_state") or "pending_operator_review")
        reviewer = str(kwargs.get("reviewer") or "") or None
        metadata = kwargs.get("metadata") if isinstance(kwargs.get("metadata"), dict) else {}
        result = await self.repository.record_effect(
            self.job_id,
            effect_type="artifact_review",
            effect_id=f"artifact-review:{_workflow_payload_digest(path)[:24]}",
            target_path=path,
            target_digest=_workflow_payload_digest(path),
            status="succeeded",
            details={
                "review_state": review_state,
                "reviewer": reviewer,
                "metadata": metadata,
                "operator_visible": True,
            },
            owner=self.owner,
            fencing_token=self.fencing_token,
            expected_revision=self.revision,
        )
        return self._sync(result)

    async def mark_uncertain(self, *, phase: str) -> dict[str, Any] | None:
        """Leave an operator-visible uncertainty when a required write fails."""
        self._assert_runtime_owner()
        result = await self.repository.transition_job(
            self.job_id,
            "unknown_external_effect",
            owner=self.owner,
            fencing_token=self.fencing_token,
            expected_revision=self.revision,
            reason=f"durable_write_failed:{phase}",
            result_summary="required durable workflow write failed; reconcile before retry",
        )
        return self._sync(result)


def _run_workflow_state_write(state_repository: Any, coro, *, phase: str) -> Any:
    """Run one workflow write with strict semantics for typed durable jobs.

    Legacy projections retain their partial-migration compatibility behavior.
    Once the manager has admitted a typed job, every checkpoint/effect/final
    write is required; on a failure we best-effort fence the row into the
    explicit unknown-external-effect state before surfacing the error.
    """
    canonical = isinstance(state_repository, _CanonicalWorkflowStateWriter)
    try:
        return _run_required_durable_state_write(
            coro,
            phase=phase,
            allow_missing_schema=not canonical,
        )
    except DurableWorkflowStateUnavailable:
        if canonical:
            try:
                _run_async(state_repository.mark_uncertain(phase=phase))
            except Exception as uncertainty_error:  # pragma: no cover - the original failure is authoritative.
                logger.error(
                    "Unable to persist durable uncertainty after %s failure: %s",
                    phase,
                    uncertainty_error,
                )
        raise


def _admit_canonical_workflow_job(
    *,
    repository: DurableJobRepository,
    run_identity: str,
    workflow: Workflow,
    tool_name: str,
    session_id: str | None,
    run_fingerprint: str,
    audit_arguments: dict[str, Any],
    approval_context: dict[str, Any],
    checkpoint_context_allowed: bool,
    owner_fields: dict[str, str],
    parent_job_id: str | None = None,
    parent_fencing_token: int | None = None,
) -> _CanonicalWorkflowStateWriter:
    owner_kind = str(owner_fields["owner_kind"])
    owner_principal_id = str(owner_fields["owner_principal_id"])
    service_id = owner_fields.get("service_id")
    runtime_owner = _workflow_durable_owner_fields()
    if (
        runtime_owner.get("owner_kind") != owner_kind
        or runtime_owner.get("owner_principal_id") != owner_principal_id
        or (runtime_owner.get("service_id") or None) != (str(service_id).strip() if service_id else None)
    ):
        raise DurableWorkflowStateUnavailable(
            "canonical workflow admission owner does not match the current principal"
        )
    contract = _workflow_contract_fields(audit_arguments, approval_context)
    authority = {
        **approval_context,
        "principal": owner_principal_id,
        "owner_kind": owner_kind,
        "capability": tool_name,
        "session_id": session_id,
    }
    for field_name in (
        "goal_id",
        "goal_revision",
        "plan_revision",
        "candidate_id",
        "deadline_at",
        "priority",
        "max_attempts",
        "budget_microusd",
    ):
        if contract[field_name] is not None:
            authority[field_name] = contract[field_name]
    authority["dependencies"] = list(contract["dependencies"])
    if service_id:
        authority["service_id"] = service_id
    identity = DurableJobIdentity(
        job_id=run_identity,
        owner_kind=owner_kind,
        owner_principal_id=owner_principal_id,
        job_kind=workflow.name,
        capability_version="workflow-v2",
        idempotency_scope=f"workflow:{workflow.name}",
        idempotency_key=run_identity,
    )
    admitted = _run_async(repository.admit_job(DurableJobSpec(
        identity=identity,
        inputs=_durable_arguments(audit_arguments, checkpoint_context_allowed=checkpoint_context_allowed),
        session_id=session_id,
        parent_job_id=parent_job_id,
        parent_fencing_token=parent_fencing_token,
        run_fingerprint=run_fingerprint,
        goal_id=contract["goal_id"],
        goal_revision=contract["goal_revision"],
        plan_revision=contract["plan_revision"],
        candidate_id=contract["candidate_id"],
        dependencies=contract["dependencies"],
        deadline_at=contract["deadline_at"],
        budget_microusd=contract["budget_microusd"],
        declared_authority=authority,
        priority=int(contract["priority"] or 50),
        resource_claims=("cpu",),
        max_attempts=int(contract["max_attempts"] or 1),
        service_id=service_id,
    )))
    if admitted.get("status") == "accepted":
        admitted = _run_async(repository.queue_job(run_identity))
    if admitted.get("status") != "queued":
        raise DurableWorkflowStateUnavailable(
            f"canonical workflow job was not queued (status={admitted.get('status')})"
        )
    runner = _workflow_canonical_lease_owner(run_identity)
    claimed = _run_async(repository.claim_job(run_identity, owner=runner, lease_seconds=300))
    if claimed.get("status") != "running":
        raise DurableWorkflowStateUnavailable(
            f"canonical workflow job was not claimed (status={claimed.get('status')})"
        )
    lease = claimed.get("lease") if isinstance(claimed.get("lease"), dict) else {}
    token = lease.get("fencing_token")
    if token is None:
        raise DurableWorkflowStateUnavailable("canonical workflow claim returned no fencing token")
    return _CanonicalWorkflowStateWriter(
        repository,
        job=claimed,
        owner=runner,
        fencing_token=int(token),
        checkpoint_context_allowed=checkpoint_context_allowed,
    )


def _verified_extension_runtime_block(extension: Any, state_entry: dict[str, Any] | None) -> dict[str, Any] | None:
    manifest = getattr(extension, "manifest", None)
    if manifest is None or getattr(manifest.trust, "value", None) != "verified":
        return None
    governance = build_governance_status(
        manifest,
        root_path=getattr(extension, "root_path", None),
        state_entry=state_entry,
    )
    if not governance.get("fail_closed"):
        return None
    return governance


async def _load_workflow_checkpoint_payload(
    run_identity: str,
    *,
    state_repository: Any | None = None,
) -> dict[str, Any] | None:
    state_repository = state_repository or workflow_state_repository
    try:
        durable_payload = await state_repository.get_checkpoint_payload(run_identity)
    except Exception as exc:  # pragma: no cover - defensive fail-soft path for partially migrated/local DBs.
        logger.warning("Durable workflow checkpoint lookup skipped: %s", exc)
        durable_payload = None
    if durable_payload is not None:
        return durable_payload
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
    credential_egress_policies: list[dict[str, Any]] = []
    delegated_specialists: list[str] = []
    delegated_tool_names: list[str] = []
    delegation_target_unresolved = False
    trust_partition: dict[str, Any] | None = None
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
            for policy in delegate_context.get("credential_egress_policies", []):
                if isinstance(policy, dict) and policy not in credential_egress_policies:
                    credential_egress_policies.append(policy)
            normalized_partition = _normalize_trust_partition(delegate_context.get("trust_partition"))
            if normalized_partition is not None:
                trust_partition = normalized_partition
    return {
        "workflow_name": workflow.name,
        "risk_level": risk_level,
        "execution_boundaries": sorted(dict.fromkeys(execution_boundaries or ["unknown"])),
        "accepts_secret_refs": accepts_secret_refs,
        "authenticated_source": authenticated_source,
        "source_systems": source_systems,
        "credential_egress_policies": credential_egress_policies,
        "delegated_specialists": sorted(dict.fromkeys(delegated_specialists)),
        "delegated_tool_names": sorted(dict.fromkeys(delegated_tool_names)),
        "delegation_target_unresolved": delegation_target_unresolved,
        "trust_partition": trust_partition,
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
        checkpoint_context_allowed = _checkpoint_context_allowed(approval_context)
        run_fingerprint = fingerprint_tool_call(
            self.name,
            audit_arguments,
            approval_context=approval_context,
        )
        current_session_id = get_current_session_id()
        direct_run_discriminator = f"run-{time.time_ns()}"
        durable_run_identity = build_workflow_run_identity(
            current_session_id,
            self.name,
            run_fingerprint,
            run_discriminator=direct_run_discriminator,
        )
        parent_run_identity = control_inputs.get("_seraph_parent_run_identity")
        root_run_identity = control_inputs.get("_seraph_root_run_identity") or durable_run_identity
        parent_fencing_token = control_inputs.get("_seraph_parent_fencing_token")
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
        checkpoint_context: dict[str, dict[str, Any]] = {}
        start_index = 0
        durable_owner_fields = _workflow_durable_owner_fields()
        state_repository: Any = workflow_state_repository
        if durable_owner_fields and isinstance(workflow_state_repository, WorkflowStateRepository):
            state_repository = _admit_canonical_workflow_job(
                repository=durable_job_repository,
                run_identity=durable_run_identity,
                workflow=self.workflow,
                tool_name=self.name,
                session_id=current_session_id,
                run_fingerprint=run_fingerprint,
                audit_arguments=audit_arguments,
                approval_context=approval_context,
                checkpoint_context_allowed=checkpoint_context_allowed,
                owner_fields=durable_owner_fields,
                parent_job_id=(str(parent_run_identity).strip() if parent_run_identity else None),
                parent_fencing_token=(
                    int(parent_fencing_token)
                    if parent_fencing_token is not None
                    else None
                ),
            )
        _run_workflow_state_write(state_repository, state_repository.create_run(
            run_identity=durable_run_identity,
            workflow_name=self.workflow.name,
            tool_name=self.name,
            session_id=current_session_id,
            run_fingerprint=run_fingerprint,
            arguments=_durable_arguments(audit_arguments, checkpoint_context_allowed=checkpoint_context_allowed),
            approval_context=approval_context,
            parent_run_identity=parent_run_identity,
            root_run_identity=root_run_identity,
            branch_kind=control_inputs.get("_seraph_branch_kind"),
            branch_depth=int(control_inputs.get("_seraph_branch_depth") or 0),
            **durable_owner_fields,
        ), phase="workflow_start")
        if control_inputs.get("_seraph_resume_from_step"):
            try:
                start_index, restored_step_records, restored_artifact_paths, restored_context = self._restore_checkpoint_context(
                    control_inputs=control_inputs,
                    canonical_step_tools=canonical_step_tools,
                    approval_context=approval_context,
                    state_repository=state_repository,
                )
            except Exception as exc:
                safe_error_summary = _safe_workflow_error_summary(exc)
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
                    error=safe_error_summary,
                    durable_run_identity=durable_run_identity,
                )
                durable_audit_receipt_id = _durable_audit_receipt_id(durable_run_identity, "failed")
                _run_workflow_state_write(state_repository, state_repository.finish_run(
                    run_identity=durable_run_identity,
                    status="failed",
                    checkpoint_context=checkpoint_context if checkpoint_context_allowed else {},
                    artifact_paths=artifact_paths,
                    continued_error_steps=continued_error_steps,
                    last_completed_step_id=None,
                    error=safe_error_summary,
                    metadata={
                        "summary": f"{self.name} failed before checkpoint resume",
                        "durable_audit_receipt_id": durable_audit_receipt_id,
                        "content_redacted": True,
                    },
                ), phase="checkpoint_resume_failure")
                raise
            for step_id, state in restored_context.items():
                context["steps"][step_id] = state
                context["last_result"] = state.get("result", "")
                checkpoint_context[step_id] = _json_safe_value(state)
            step_records.extend(restored_step_records)
            for path in restored_artifact_paths:
                if path not in artifact_paths:
                    artifact_paths.append(path)
            if isinstance(state_repository, _CanonicalWorkflowStateWriter):
                _run_workflow_state_write(
                    state_repository,
                    state_repository.adopt_restored_steps(
                        step_records=restored_step_records,
                        context=restored_context,
                    ),
                    phase="checkpoint_resume",
                )

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
            _run_workflow_state_write(state_repository, state_repository.record_step_started(
                run_identity=durable_run_identity,
                workflow_name=self.workflow.name,
                step_id=step.id,
                step_index=len(step_records) + 1,
                tool_name=canonical_step_tool,
                arguments=_durable_arguments(rendered_arguments, checkpoint_context_allowed=checkpoint_context_allowed),
            ), phase=f"step_start:{step.id}")
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
                safe_error_summary = _safe_workflow_error_summary(exc)
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
                        "error_summary": safe_error_summary,
                        "started_at": step_started_at,
                        "completed_at": step_completed_at,
                        "duration_ms": duration_ms,
                    })
                    _run_workflow_state_write(state_repository, state_repository.record_step_failed(
                        run_identity=durable_run_identity,
                        step_id=step.id,
                        status="failed",
                        result=None,
                        result_summary=None,
                        artifact_paths=step_artifact_paths,
                        checkpoint=None,
                        error_kind=type(exc).__name__,
                        error_summary=safe_error_summary,
                    ), phase=f"step_failed:{step.id}")
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
                        error=safe_error_summary,
                        durable_run_identity=durable_run_identity,
                    )
                    durable_audit_receipt_id = _durable_audit_receipt_id(durable_run_identity, "failed")
                    _record_delegated_artifact_reviews(
                        run_identity=durable_run_identity,
                        root_run_identity=root_run_identity,
                        parent_run_identity=parent_run_identity,
                        workflow_name=self.workflow.name,
                        approval_context=approval_context,
                        artifact_paths=artifact_paths,
                        durable_audit_receipt_id=durable_audit_receipt_id,
                        state_repository=state_repository,
                    )
                    _run_workflow_state_write(state_repository, state_repository.finish_run(
                        run_identity=durable_run_identity,
                        status="failed",
                        checkpoint_context=checkpoint_context if checkpoint_context_allowed else {},
                        artifact_paths=artifact_paths,
                        continued_error_steps=continued_error_steps,
                        last_completed_step_id=next(
                            (
                                str(record["id"])
                                for record in reversed(step_records)
                                if record.get("id") and str(record.get("status") or "") not in {"failed", "continued_error"}
                            ),
                            None,
                        ),
                        error=safe_error_summary,
                        metadata={
                            "summary": f"{self.name} failed",
                            "durable_audit_receipt_id": durable_audit_receipt_id,
                            "content_redacted": True,
                        },
                    ), phase="workflow_failed")
                    raise
                result = f"Error: {safe_error_summary}"
                continued_error_steps.append(step.id)
                step_status = "continued_error"
                error_kind = type(exc).__name__
                error_summary = safe_error_summary
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
            _run_workflow_state_write(state_repository, state_repository.record_step_completed(
                run_identity=durable_run_identity,
                step_id=step.id,
                status=step_status,
                result=_durable_result(result, checkpoint_context_allowed=checkpoint_context_allowed),
                result_summary=_summarize_value_shape(result),
                artifact_paths=step_artifact_paths,
                checkpoint=_durable_checkpoint(
                    context["steps"][step.id],
                    checkpoint_context_allowed=checkpoint_context_allowed,
                ),
                error_kind=error_kind,
                error_summary=error_summary,
            ), phase=f"step_completed:{step.id}")

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
            durable_run_identity=durable_run_identity,
        )
        durable_audit_receipt_id = _durable_audit_receipt_id(durable_run_identity, status)
        _record_delegated_artifact_reviews(
            run_identity=durable_run_identity,
            root_run_identity=root_run_identity,
            parent_run_identity=parent_run_identity,
            workflow_name=self.workflow.name,
            approval_context=approval_context,
            artifact_paths=artifact_paths,
            durable_audit_receipt_id=durable_audit_receipt_id,
            state_repository=state_repository,
        )
        _run_workflow_state_write(state_repository, state_repository.finish_run(
            run_identity=durable_run_identity,
            status=status,
            checkpoint_context=checkpoint_context if checkpoint_context_allowed else {},
            artifact_paths=artifact_paths,
            continued_error_steps=continued_error_steps,
            last_completed_step_id=next(
                (
                    str(record["id"])
                    for record in reversed(step_records)
                    if record.get("id") and str(record.get("status") or "") not in {"failed", "continued_error"}
                ),
                None,
            ),
            metadata={
                "summary": summary,
                "canvas_output": _redact_canvas_output(canvas_output),
                "content_redacted": True,
                "durable_audit_receipt_id": durable_audit_receipt_id,
            },
        ), phase="workflow_finish")
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
        if isinstance(control_inputs.get("_seraph_parent_fencing_token"), int):
            details["parent_fencing_token"] = control_inputs["_seraph_parent_fencing_token"]
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
        durable_run_identity: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        payload_summary = summary or f"{self.name} {status}"
        payload = {
            "workflow_name": self.workflow.name,
            "run_fingerprint": run_fingerprint,
            "durable_run_identity": durable_run_identity,
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
        state_repository: Any | None = None,
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
        details = _run_async(
            _load_workflow_checkpoint_payload(
                parent_run_identity,
                state_repository=state_repository,
            )
        )
        if not isinstance(details, dict):
            raise RuntimeError(
                f"Workflow '{self.workflow.name}' could not load checkpoint state from '{parent_run_identity}'"
            )
        _assert_workflow_parent_recovery_authority(
            parent_run_identity=parent_run_identity,
            details=details,
            control_inputs=control_inputs,
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
            if key in {"_seraph_branch_depth", "_seraph_parent_revision", "_seraph_parent_fencing_token"}:
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
        state_entries = extension_state_entries(load_extension_state_payload())
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
            extension = extensions_by_id.get(workflow.extension_id) if workflow.extension_id else None
            governance_block = _verified_extension_runtime_block(
                extension,
                state_entries.get(workflow.extension_id) if workflow.extension_id else None,
            )
            permission_profile = evaluate_tool_permissions(
                extension,
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
                "governance_runtime_blocked": governance_block is not None,
                "governance_fail_closed_reason": (
                    str(governance_block.get("fail_closed_reason") or "governance_blocked")
                    if governance_block is not None
                    else None
                ),
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
                        governance_block=governance_block,
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
        state_entries = extension_state_entries(load_extension_state_payload())
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
            extension = extensions_by_id.get(workflow.extension_id) if workflow.extension_id else None
            governance_block = _verified_extension_runtime_block(
                extension,
                state_entries.get(workflow.extension_id) if workflow.extension_id else None,
            )
            if governance_block is not None:
                continue
            permission_profile = evaluate_tool_permissions(
                extension,
                tool_names=workflow.step_tools,
            )
            availability = self._get_runtime_availability(
                workflow,
                list(tool_set),
                list(skill_set),
                available_runtime_profiles=available_runtime_profiles,
                available_output_surfaces=available_output_surfaces,
                permission_profile=permission_profile,
                governance_block=governance_block,
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
        governance_block: dict[str, Any] | None = None,
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
        governance_blocked = governance_block is not None
        governance_fail_closed_reason = (
            str(governance_block.get("fail_closed_reason") or "governance_blocked")
            if governance_block is not None
            else None
        )
        return {
            "is_available": (
                not missing_tools
                and not missing_skills
                and not missing_runtime_profiles
                and not missing_output_surfaces
                and not missing_manifest_tools
                and not missing_manifest_execution_boundaries
                and not missing_manifest_network
                and not governance_blocked
            ),
            "missing_tools": missing_tools,
            "missing_skills": missing_skills,
            "missing_runtime_profiles": missing_runtime_profiles,
            "missing_output_surfaces": missing_output_surfaces,
            "missing_manifest_tools": missing_manifest_tools,
            "missing_manifest_execution_boundaries": missing_manifest_execution_boundaries,
            "missing_manifest_network": missing_manifest_network,
            "governance_runtime_blocked": governance_blocked,
            "governance_fail_closed_reason": governance_fail_closed_reason,
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
