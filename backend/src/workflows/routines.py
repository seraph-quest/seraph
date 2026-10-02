"""Owner-bound reusable guardian routines.

This module deliberately keeps routine metadata small.  Source observation,
package governance, approvals, and external follow-through remain owned by
their existing services; a routine only stores verified provenance and binds a
fresh invocation to current identities before delegating.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Any, AsyncIterator, Mapping

import yaml

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from config.settings import settings
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.db import engine as db_engine
from src.db.models import (
    Goal,
    GuardianDecisionPacket,
    GuardianRoutine,
    GuardianRoutineVersion,
    ProcedureV2Binding,
    WorkBoardAttempt,
    WorkBoardLink,
    WorkBoardRoutineBinding,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.extensions.capability_pack import (
    CapabilityPackLifecycle,
    capability_pack_digest,
    parse_capability_pack_manifest,
    validate_capability_pack_path,
)
from src.extensions.github_followthrough import (
    GitHubFollowthroughError,
    GitHubFollowthroughService,
    JOB_KIND as GITHUB_FOLLOWTHROUGH_JOB_KIND,
    PrepareRequest,
    ROUTINE_BINDING_KEYS,
    ROUTINE_PUBLICATION_CHILD_JOB_KIND,
    _operation_id,
)
from src.extensions.workspace_package import (
    save_workspace_contribution,
    workspace_capability_package_root,
)
from src.guardian.source_watch import source_watch_service
from src.security.trust_contract import AuthorityGrant
from src.tools.filesystem_tool import (
    _read_workspace_text_bounded,
    _safe_resolve,
    _write_workspace_text_bounded,
)
from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.repository import (
    BoardError,
    WorkBoardRepository,
    _safe_receipt_refs,
    safe_sha256_digest,
    safe_workflow_run_id,
)
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobSpec,
    DurableJobTransitionError,
    UNRESOLVED_EFFECT_STATUSES,
    _serialize,
    durable_job_repository,
)
from src.workflows.routine_templates import (
    render_runbook,
    render_workflow,
    routine_slug,
    validate_generated_files,
)
from src.workflows.routine_steps import RoutineStepContext, github_followthrough, guardian_watch_run
from src.workflows.procedure_contracts import (
    ROUTINE_V2_CAPABILITY_VERSION,
    build_procedure_plan,
    get_procedure_template,
    plan_digest,
    validate_procedure_plan,
)
from src.workflows.procedure_service import (
    ProcedureV2CreateRequest,
    ProcedureV2Error,
    ProcedureV2InvokeRequest,
    ProcedureV2PreviewRequest,
    ProcedureV2ScheduleRequest,
    ProcedureV2Service,
    V2InvocationDescriptor,
    _proof_digest,
    _plan_step_input_digests,
    _validated_immutable_step_inputs,
)


ROUTINE_CAPABILITY_VERSION = "guardian-routine.v1"
ROUTINE_SERVICE_ID = "guardian-routine"
ROUTINE_INSTALL_TOOL = "guardian:routine-install"
ROUTINE_INVOKE_TOOL = "guardian:routine-invoke"
ROUTINE_DEADLINE_SECONDS = 600
ROUTINE_MAX_RUNTIME_SECONDS = 900
APPROVAL_TTL_SECONDS = 5 * 60
_SAFE_APPROVAL_CLEANUP_OUTCOMES = frozenset(
    {"denied", "not_bound", "missing", "approved", "consumed", "expired"}
)
ROUTINE_PACK_SCHEMA_VERSION = 2
ROUTINE_PACK_ROOT = "extensions/routine-packs"
ROUTINE_INSTALL_STAGING_ROOT = "artifacts/routine-install-staging"
ROUTINE_PACK_CAPABILITY_ID = ROUTINE_CAPABILITY_VERSION
ROUTINE_PACK_RUNBOOK_REFERENCE = "runbooks/verified-guardian-procedure.yaml"
ROUTINE_PACK_INPUTS = {
    "goal_id": {"type": "string", "required": True},
    "goal_revision": {"type": "integer", "required": True},
    "source_watch_id": {"type": "string", "required": True},
    "source_watch_revision": {"type": "integer", "required": True},
    "source_task_id": {"type": "string", "required": False},
    "source_attempt_id": {"type": "string", "required": False},
    "action_task_id": {"type": "string", "required": False},
    "action_attempt_id": {"type": "string", "required": False},
    "artifact_digest_refs": {"type": "digest-reference-list", "required": False},
}
ROUTINE_PACK_STEP_CONTRACT = (
    {"id": "guardian_watch_run", "capability": "guardian_watch_run", "tool": "guardian_watch_run"},
    {"id": "github_followthrough", "capability": "github_followthrough", "tool": "github_followthrough"},
)
BOARD_ROUTINE_PREVIEW_TTL_SECONDS = 15 * 60
BOARD_ROUTINE_NAMESPACE = uuid.UUID("b8cc4b0b-09bc-5f2d-9f91-3f1a3f76a8d4")
BOARD_ROUTINE_INPUT_SCHEMA_VERSION = 1
_ROUTINE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$"


class RoutineError(ValueError):
    def __init__(self, code: str, message: str | None = None, *, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(message or code)


class RoutineFromRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_watch_job_id: str = Field(min_length=1, max_length=256)
    source_packet_id: str = Field(min_length=1, max_length=256)
    source_m3_job_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=80, pattern=_ROUTINE_NAME_PATTERN)


class RoutineFromBoardPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_task_id: str = Field(min_length=1, max_length=256)
    action_task_id: str = Field(min_length=1, max_length=256)
    expected_source_revision: int = Field(ge=1)
    expected_action_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80, pattern=_ROUTINE_NAME_PATTERN)
    idempotency_key: str = Field(min_length=1, max_length=256)


class RoutineFromBoardCreateRequest(RoutineFromBoardPreviewRequest):
    preview_digest: str = Field(min_length=64, max_length=64)


class RoutineVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_watch_job_id: str = Field(min_length=1, max_length=256)
    source_packet_id: str = Field(min_length=1, max_length=256)
    source_m3_job_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=80, pattern=_ROUTINE_NAME_PATTERN)
    expected_routine_revision: int = Field(ge=1)


class RoutineInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)
    approval_id: str = Field(min_length=1, max_length=256)


class RoutineActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)


class RoutinePackageRequest(BaseModel):
    """Server derives every package identity and content from this selector."""

    model_config = ConfigDict(extra="forbid")

    expected_routine_revision: int = Field(ge=1)


class RoutinePackageActivationRequest(RoutinePackageRequest):
    approval_id: str = Field(min_length=1, max_length=256)


class RoutinePackageDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_routine_revision: int = Field(ge=1)
    decision: str = Field(pattern=r"^(approved|denied)$")


class RoutineInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    expected_goal_revision: int = Field(ge=1)
    source_watch_id: str = Field(min_length=1, max_length=256)
    expected_watch_revision: int = Field(ge=1)
    invocation_uuid: str = Field(min_length=1, max_length=80)


class RoutineExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=256)
    expected_routine_revision: int = Field(ge=1)


class RoutinePublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=160)
    body: str = Field(min_length=1, max_length=32_000)


class RoutineRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_routine_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class RoutineRollbackRequest(RoutineRevisionRequest):
    target_version: int = Field(ge=1)


class RoutineRecoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    """Normalize database timestamps without treating SQLite naive values as local time."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_runtime_seconds(value: Any) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError, OverflowError):
        requested = ROUTINE_DEADLINE_SECONDS
    return max(1, min(requested, ROUTINE_MAX_RUNTIME_SECONDS))


def _remaining_runtime_seconds(job: Mapping[str, Any]) -> int:
    """Bound a routine lease to the linked durable job's remaining deadline."""

    raw_deadline = job.get("deadline_at")
    deadline: datetime | None = None
    if isinstance(raw_deadline, datetime):
        deadline = raw_deadline
    elif isinstance(raw_deadline, str) and raw_deadline.strip():
        try:
            deadline = datetime.fromisoformat(raw_deadline.strip().replace("Z", "+00:00"))
        except ValueError:
            deadline = None
    if deadline is None:
        return ROUTINE_DEADLINE_SECONDS
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    remaining = int((deadline - _now()).total_seconds())
    if remaining <= 0:
        raise RoutineError("routine_runtime_deadline_expired", status_code=409)
    return _bounded_runtime_seconds(remaining)


def _routine_approval_scope(job: Mapping[str, Any], tool_name: str) -> dict[str, Any]:
    """Expose only the fixed routine authority that an operator is approving."""

    authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
    common = {
        "capability_id": str(authority.get("capability_id") or ROUTINE_CAPABILITY_VERSION),
        "job_id": str(job.get("job_id") or ""),
        "goal_id": str(job.get("goal_id") or ""),
        "goal_revision": int(job.get("goal_revision") or 0),
        "routine_id": str(authority.get("routine_id") or ""),
        "routine_version": int(authority.get("routine_version") or 0),
    }
    if tool_name == ROUTINE_INSTALL_TOOL:
        if common["capability_id"] == ROUTINE_V2_CAPABILITY_VERSION:
            scope = {
                "action": "install_reviewed_procedure_v2",
                **common,
                "template_id": str(authority.get("template_id") or ""),
                "plan_digest": str(authority.get("plan_digest") or ""),
                "source_proof_digest": str(authority.get("source_proof_digest") or ""),
                "workflow_sha256": str(authority.get("workflow_sha256") or ""),
                "runbook_sha256": str(authority.get("runbook_sha256") or ""),
            }
            required = (
                "job_id",
                "goal_id",
                "routine_id",
                "template_id",
                "plan_digest",
                "source_proof_digest",
                "workflow_sha256",
                "runbook_sha256",
            )
            if (
                any(not str(scope.get(key) or "").strip() for key in required)
                or scope["goal_revision"] < 1
                or scope["routine_version"] < 1
            ):
                raise RoutineError("routine_approval_scope_incomplete")
            return scope
        scope = {
            "action": "install_guardian_procedure",
            **common,
            "workflow_sha256": str(authority.get("workflow_sha256") or ""),
            "runbook_sha256": str(authority.get("runbook_sha256") or ""),
            "source_provenance_sha256": str(authority.get("source_provenance_sha256") or ""),
            "source_packet_id": str(authority.get("source_packet_id") or ""),
            "source_dossier_sha256": str(authority.get("source_dossier_sha256") or ""),
            "source_repository": str(authority.get("source_repository") or ""),
            "source_action": str(authority.get("source_action") or ""),
            "source_target": str(authority.get("source_target") or ""),
        }
        required = (
            "job_id",
            "goal_id",
            "routine_id",
            "workflow_sha256",
            "runbook_sha256",
            "source_provenance_sha256",
            "source_packet_id",
            "source_repository",
            "source_action",
            "source_target",
        )
        if any(not str(scope.get(key) or "").strip() for key in required) or scope["goal_revision"] < 1 or scope["routine_version"] < 1:
            raise RoutineError("routine_approval_scope_incomplete")
        return scope
    if tool_name == ROUTINE_INVOKE_TOOL:
        scope = {
            "action": "run_guardian_procedure",
            **common,
            "routine_revision": int(authority.get("routine_revision") or 0),
            "workflow_sha256": str(authority.get("workflow_sha256") or ""),
            "runbook_sha256": str(authority.get("runbook_sha256") or ""),
            "package_digest": str(authority.get("package_digest") or ""),
            "source_watch_id": str(authority.get("source_watch_id") or ""),
            "source_watch_revision": int(authority.get("source_watch_revision") or 0),
            "github_connection_id": str(authority.get("github_connection_id") or ""),
            "github_connection_revision": int(authority.get("github_connection_revision") or 0),
            "github_repository": str(authority.get("github_repository") or ""),
            "github_action": str(authority.get("github_action") or ""),
            "github_target": str(authority.get("github_target") or ""),
        }
        required = (
            "job_id",
            "goal_id",
            "routine_id",
            "workflow_sha256",
            "runbook_sha256",
            "package_digest",
            "source_watch_id",
            "github_connection_id",
            "github_repository",
            "github_action",
            "github_target",
        )
        if (
            any(not str(scope.get(key) or "").strip() for key in required)
            or scope["goal_revision"] < 1
            or scope["routine_version"] < 1
            or scope["routine_revision"] < 1
            or scope["source_watch_revision"] < 1
            or scope["github_connection_revision"] < 1
        ):
            raise RoutineError("routine_approval_scope_incomplete")
        return scope
    raise RoutineError("routine_approval_tool_invalid", status_code=422)


def _operator_has_grant(operator: Any, grant: AuthorityGrant) -> bool:
    grants = {
        str(getattr(item, "value", item))
        for item in (getattr(getattr(operator, "principal", None), "grants", ()) or ())
    }
    return grant.value in grants


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return result


def _sha(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _routine_pack_token(routine_id: str) -> str:
    """Return the server-owned, path-safe identity for a routine package."""

    candidate = str(routine_id or "").strip().replace("-", "").lower()
    try:
        if len(candidate) != 32:
            raise ValueError
        uuid.UUID(hex=candidate)
    except (ValueError, AttributeError, TypeError) as exc:
        raise RoutineError("routine_id_invalid", status_code=422) from exc
    return candidate


def _routine_pack_id(routine_id: str, version: int) -> str:
    """Derive a stable unique lifecycle ID for one immutable routine version."""

    if int(version) < 1:
        raise RoutineError("routine_version_invalid", status_code=422)
    return f"seraph.routine.{_routine_pack_token(routine_id)}.v{int(version)}"


def _routine_pack_root(routine_id: str, version: int) -> Path:
    """Derive the package root; callers cannot provide or override this path."""

    if int(version) < 1:
        raise RoutineError("routine_version_invalid", status_code=422)
    token = _routine_pack_token(routine_id)
    relative = f"{ROUTINE_PACK_ROOT}/{token}/v{int(version)}"
    try:
        root = _safe_resolve(relative)
    except (OSError, ValueError) as exc:
        raise RoutineError("routine_package_path_blocked", status_code=409) from exc
    return root


def _routine_install_staging_root(routine_id: str, version: int) -> Path:
    """Return the private, non-discoverable staging root for one install."""

    if int(version) < 1:
        raise RoutineError("routine_version_invalid", status_code=422)
    token = _routine_pack_token(routine_id)
    try:
        return _safe_resolve(f"{ROUTINE_INSTALL_STAGING_ROOT}/{token}/v{int(version)}")
    except (OSError, ValueError) as exc:
        raise RoutineError("routine_install_path_blocked", status_code=409) from exc


def _cleanup_install_staging_root(root: Path) -> None:
    """Remove only a server-derived staging tree, never a caller path."""

    if root.is_symlink() or not root.exists():
        return
    if not root.is_dir():
        return
    try:
        shutil.rmtree(root)
    except OSError:
        # Staging is outside the discoverable extension roots.  Leave an
        # unreadable residue for the next startup cleanup rather than deleting
        # through an unexpected filesystem entry.
        return


def _staged_install_entries() -> list[tuple[str, int, Path]]:
    """Enumerate only server-shaped private install staging trees.

    Invalid names, symlinks, and unreadable entries are deliberately omitted.
    The startup recovery therefore preserves anything it cannot prove was
    created by the routine installer.
    """

    try:
        root = _safe_resolve(ROUTINE_INSTALL_STAGING_ROOT)
    except (OSError, ValueError):
        return []
    if root.is_symlink() or not root.is_dir():
        return []
    try:
        routine_dirs = tuple(root.iterdir())
    except OSError:
        return []
    entries: list[tuple[str, int, Path]] = []
    for routine_dir in routine_dirs:
        if routine_dir.is_symlink() or not routine_dir.is_dir():
            continue
        try:
            token = _routine_pack_token(routine_dir.name)
        except RoutineError:
            continue
        try:
            version_dirs = tuple(routine_dir.iterdir())
        except OSError:
            continue
        for version_dir in version_dirs:
            if version_dir.is_symlink() or not version_dir.is_dir():
                continue
            raw_version = version_dir.name
            if not raw_version.startswith("v"):
                continue
            try:
                version = int(raw_version[1:])
            except (TypeError, ValueError):
                continue
            if version < 1 or raw_version != f"v{version}":
                continue
            entries.append((token, version, version_dir))
    return entries


def _routine_pack_runbook_payload(
    *,
    routine_id: str,
    version: GuardianRoutineVersion,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the package runbook for the persisted procedure schema.

    ``CapabilityPackManifest.schema_version == 2`` is the generic pack
    format.  The routine's procedure schema is selected from validated source
    provenance so a legacy guardian-routine.v1 version keeps its exact bytes.
    """

    safe_provenance = _safe_routine_provenance(provenance)
    if provenance.get("schema_version") == 2:
        try:
            template_id = str(provenance["template_id"])
            spec = get_procedure_template(template_id)
            plan = validate_procedure_plan(provenance["plan"])
        except Exception as exc:
            raise RoutineError("routine_package_procedure_invalid", status_code=409) from exc
        observed_plan_digest = str(provenance.get("plan_digest") or "").lower()
        if observed_plan_digest != plan_digest(plan):
            raise RoutineError("routine_package_procedure_digest_invalid", status_code=409)
        return {
            "id": f"runbook:{_routine_pack_id(routine_id, int(version.version))}",
            "title": "Seraph reviewed guardian procedure",
            "summary": "A reviewed, owner-bound fixed procedure definition.",
            "starter_pack": "seraph.guardian-routine.v2",
            "inputs": {
                "routine_invocation_job_id": {"type": "string", "required": True},
            },
            "procedure": {
                "schema_version": 2,
                "capability_id": ROUTINE_V2_CAPABILITY_VERSION,
                "template_id": spec.template_id,
                "steps": [
                    {
                        "id": step.step_id,
                        "capability_id": step.capability_id,
                        "capability_version": step.capability_version,
                    }
                    for step in spec.steps
                ],
                "plan_digest": observed_plan_digest,
            },
            "bindings": {
                "routine_id": _routine_pack_token(routine_id),
                "version": int(version.version),
                "workflow_sha256": str(version.workflow_sha256),
                "runbook_sha256": str(version.runbook_sha256),
                "source_provenance_sha256": _sha(_dump(safe_provenance)),
                "source_provenance": safe_provenance,
                "plan_digest": observed_plan_digest,
            },
        }
    artifact_refs: list[dict[str, Any]] = []
    for key in (
        "source_task_artifact_refs",
        "action_task_artifact_refs",
        "source_attempt_receipt_refs",
        "action_attempt_receipt_refs",
    ):
        value = safe_provenance.get(key)
        if isinstance(value, list):
            artifact_refs.extend(item for item in value if isinstance(item, dict))
    return {
        "id": f"runbook:{_routine_pack_id(routine_id, int(version.version))}",
        "title": "Seraph verified guardian procedure",
        "summary": "A reviewed, owner-bound guardian procedure definition with fixed local steps.",
        "starter_pack": "seraph.guardian-routine.v1",
        "inputs": dict(ROUTINE_PACK_INPUTS),
        "procedure": {
            "schema_version": 1,
            "capability_id": ROUTINE_PACK_CAPABILITY_ID,
            "steps": [dict(step) for step in ROUTINE_PACK_STEP_CONTRACT],
        },
        "bindings": {
            "routine_id": _routine_pack_token(routine_id),
            "version": int(version.version),
            "workflow_sha256": str(version.workflow_sha256),
            "legacy_runbook_sha256": str(version.runbook_sha256),
            "source_provenance_sha256": _sha(_dump(safe_provenance)),
            "source_provenance": safe_provenance,
            "typed_inputs": {
                "goal_id": safe_provenance.get("goal_id"),
                "goal_revision": safe_provenance.get("goal_revision"),
                "source_watch_id": safe_provenance.get("source_watch_id"),
                "source_watch_revision": safe_provenance.get("plan_revision"),
                "source_task_id": safe_provenance.get("source_task_id"),
                "source_attempt_id": safe_provenance.get("source_attempt_id"),
                "action_task_id": safe_provenance.get("action_task_id"),
                "action_attempt_id": safe_provenance.get("action_attempt_id"),
            },
            "artifact_digest_refs": artifact_refs,
        },
    }


def _routine_pack_manifest_payload(
    *, routine_id: str, version: int, provenance: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    pack_id = _routine_pack_id(routine_id, version)
    if isinstance(provenance, Mapping) and provenance.get("schema_version") == 2:
        try:
            spec = get_procedure_template(str(provenance.get("template_id") or ""))
            plan = validate_procedure_plan(provenance.get("plan") or {})
        except Exception as exc:
            raise RoutineError("routine_package_procedure_invalid", status_code=409) from exc
        if str(provenance.get("plan_digest") or "").lower() != plan_digest(plan):
            raise RoutineError("routine_package_procedure_digest_invalid", status_code=409)
        return {
            "schema_version": ROUTINE_PACK_SCHEMA_VERSION,
            "id": pack_id,
            "version": f"2.0.{int(version)}",
            "kind": "capability-pack",
            "publisher": {"name": "Seraph", "provenance": "local-reviewed"},
            "signature": {"state": "unsigned-local", "signer": None},
            "compatibility": {"seraph": ">=0"},
            "dependencies": [],
            "contributes": {
                "capabilities": list(spec.permissions),
                "skills": [],
                "workflows": [],
                "prompts": [],
                "sources": [],
                "reports": [],
                "evals": [],
                "runbooks": [ROUTINE_PACK_RUNBOOK_REFERENCE],
            },
            "authority": {
                "tools": [step.step_id for step in spec.steps],
                "filesystem": ["artifact_read", "artifact_write"],
                "network": False,
                "secrets": [],
                "approval": "always",
            },
            "resources": {
                "inference_priority": "approved_operator",
                "max_inference_cost_microusd": 0,
                "max_runtime_seconds": 300,
                "max_artifact_bytes": 10 * 1024 * 1024,
            },
            "data_policy": {
                "classes": ["private", "operator_context"]
                if spec.template_id == "selected-meeting-prep"
                else ["public"],
                "egress": [],
            },
            "policy_overlays": [],
            "lifecycle": {
                "hooks": {
                    "activate": "required",
                    "pause": "required",
                    "update": "required",
                    "revoke": "required",
                    "uninstall": "required",
                },
                "artifact_migration": "preserve",
                "revoke_running_jobs": "cancel_at_safe_checkpoint",
            },
            "display_name": f"Seraph reviewed procedure {spec.template_id} v{int(version)}",
            "summary": "Fixed reviewed procedure; fresh authority is required per invocation.",
        }
    return {
        "schema_version": ROUTINE_PACK_SCHEMA_VERSION,
        "id": pack_id,
        "version": f"1.0.{int(version)}",
        "kind": "capability-pack",
        "publisher": {"name": "Seraph", "provenance": "local-reviewed"},
        "signature": {"state": "unsigned-local", "signer": None},
        "compatibility": {"seraph": ">=0"},
        "dependencies": [],
        "contributes": {
            "capabilities": [ROUTINE_PACK_CAPABILITY_ID],
            "skills": [],
            "workflows": [],
            "prompts": [],
            "sources": [],
            "reports": [],
            "evals": [],
            "runbooks": [ROUTINE_PACK_RUNBOOK_REFERENCE],
        },
        "authority": {
            "tools": [],
            "filesystem": [],
            "network": False,
            "secrets": [],
            "approval": "always",
        },
        "resources": {
            "inference_priority": "approved_operator",
            "max_inference_cost_microusd": 0,
            "max_runtime_seconds": ROUTINE_DEADLINE_SECONDS,
            "max_artifact_bytes": 10 * 1024 * 1024,
        },
        "data_policy": {"classes": ["public"], "egress": []},
        "policy_overlays": [],
        "lifecycle": {
            "hooks": {
                "activate": "required",
                "pause": "required",
                "update": "required",
                "revoke": "required",
                "uninstall": "required",
            },
            "artifact_migration": "preserve",
            "revoke_running_jobs": "cancel_at_safe_checkpoint",
        },
        "display_name": f"Seraph guardian procedure v{int(version)}",
        "summary": "Local reviewed procedure definition; invocation authority is issued per goal.",
    }


def _validate_routine_pack_runbook(content: str, *, routine_id: str, version: GuardianRoutineVersion) -> dict[str, Any]:
    """Validate the package runbook selected by immutable source provenance."""

    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise RoutineError("routine_package_runbook_invalid", status_code=409) from exc
    if not isinstance(payload, dict):
        raise RoutineError("routine_package_runbook_invalid", status_code=409)
    provenance = _load(version.source_provenance_json, {})
    if isinstance(provenance, Mapping) and provenance.get("schema_version") == 2:
        try:
            template_id = str(provenance.get("template_id") or "")
            spec = get_procedure_template(template_id)
            plan = validate_procedure_plan(provenance.get("plan") or {})
        except Exception as exc:
            raise RoutineError("routine_package_procedure_invalid", status_code=409) from exc
        expected_digest = plan_digest(plan)
        expected = {
            "id": f"runbook:{_routine_pack_id(routine_id, int(version.version))}",
            "title": "Seraph reviewed guardian procedure",
            "summary": "A reviewed, owner-bound fixed procedure definition.",
            "starter_pack": "seraph.guardian-routine.v2",
            "inputs": {
                "routine_invocation_job_id": {"type": "string", "required": True},
            },
            "procedure": {
                "schema_version": 2,
                "capability_id": ROUTINE_V2_CAPABILITY_VERSION,
                "template_id": template_id,
                "steps": [
                    {
                        "id": step.step_id,
                        "capability_id": step.capability_id,
                        "capability_version": step.capability_version,
                    }
                    for step in spec.steps
                ],
                "plan_digest": expected_digest,
            },
            "bindings": {
                "routine_id": _routine_pack_token(routine_id),
                "version": int(version.version),
                "workflow_sha256": str(version.workflow_sha256),
                "runbook_sha256": str(version.runbook_sha256),
                "source_provenance_sha256": _sha(_dump(_safe_routine_provenance(provenance))),
                "source_provenance": _safe_routine_provenance(provenance),
                "plan_digest": expected_digest,
            },
        }
        if payload != expected:
            raise RoutineError("routine_package_runbook_contract_invalid", status_code=409)
        return payload
    required = {"id", "title", "summary", "starter_pack", "inputs", "procedure", "bindings"}
    if (
        set(payload) != required
        or payload.get("starter_pack") != "seraph.guardian-routine.v1"
        or payload.get("id") != f"runbook:{_routine_pack_id(routine_id, int(version.version))}"
    ):
        raise RoutineError("routine_package_runbook_invalid", status_code=409)
    if payload.get("inputs") != ROUTINE_PACK_INPUTS:
        raise RoutineError("routine_package_runbook_contract_invalid", status_code=409)
    procedure = payload.get("procedure")
    if not isinstance(procedure, Mapping) or procedure.get("schema_version") != 1 or procedure.get("capability_id") != ROUTINE_PACK_CAPABILITY_ID:
        raise RoutineError("routine_package_runbook_contract_invalid", status_code=409)
    if procedure.get("steps") != [dict(step) for step in ROUTINE_PACK_STEP_CONTRACT]:
        raise RoutineError("routine_package_runbook_contract_invalid", status_code=409)
    bindings = payload.get("bindings")
    if not isinstance(bindings, Mapping):
        raise RoutineError("routine_package_runbook_binding_invalid", status_code=409)
    expected_provenance = _safe_routine_provenance(provenance)
    expected = {
        "routine_id": _routine_pack_token(routine_id),
        "version": int(version.version),
        "workflow_sha256": str(version.workflow_sha256),
        "legacy_runbook_sha256": str(version.runbook_sha256),
        "source_provenance_sha256": _sha(_dump(expected_provenance)),
        "source_provenance": expected_provenance,
        "typed_inputs": {
            "goal_id": expected_provenance.get("goal_id"),
            "goal_revision": expected_provenance.get("goal_revision"),
            "source_watch_id": expected_provenance.get("source_watch_id"),
            "source_watch_revision": expected_provenance.get("plan_revision"),
            "source_task_id": expected_provenance.get("source_task_id"),
            "source_attempt_id": expected_provenance.get("source_attempt_id"),
            "action_task_id": expected_provenance.get("action_task_id"),
            "action_attempt_id": expected_provenance.get("action_attempt_id"),
        },
    }
    if any(bindings.get(key) != value for key, value in expected.items()):
        raise RoutineError("routine_package_runbook_binding_invalid", status_code=409)
    artifact_refs = bindings.get("artifact_digest_refs")
    expected_refs: list[dict[str, Any]] = []
    for key in ("source_task_artifact_refs", "action_task_artifact_refs", "source_attempt_receipt_refs", "action_attempt_receipt_refs"):
        value = expected_provenance.get(key)
        if isinstance(value, list):
            expected_refs.extend(item for item in value if isinstance(item, dict))
    if artifact_refs != expected_refs:
        raise RoutineError("routine_package_runbook_binding_invalid", status_code=409)
    return payload


def _create_once_workspace_text(path: Path, content: str) -> None:
    """Create one package member atomically and reject every mutation."""

    try:
        workspace = Path(settings.workspace_dir).resolve()
        relative = path.relative_to(workspace).as_posix()
        path = _safe_resolve(relative)
    except (OSError, ValueError) as exc:
        raise RoutineError("routine_package_path_blocked", status_code=409) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            stored, truncated = _read_workspace_text_bounded(path, max_bytes=256 * 1024)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise RoutineError("routine_package_mutation_detected", status_code=409) from exc
        if truncated or stored != content:
            raise RoutineError("routine_package_mutation_detected", status_code=409)
        return
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    try:
        directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise RoutineError("routine_package_durability_failed", status_code=503) from exc


def _snapshot_install_files(paths: list[Path]) -> dict[Path, bytes | None]:
    """Capture only the bounded files an install may change.

    The canonical routine transaction deliberately spans these local writes.
    If its revision CAS fails, restore the exact pre-install bytes instead of
    leaving a package contribution that the SQL selector did not authorize.
    """

    snapshot: dict[Path, bytes | None] = {}
    for path in dict.fromkeys(paths):
        try:
            if path.is_symlink():
                raise RoutineError("routine_install_path_blocked", status_code=409)
            if path.exists():
                if not path.is_file():
                    raise RoutineError("routine_install_path_blocked", status_code=409)
                snapshot[path] = path.read_bytes()
            else:
                snapshot[path] = None
        except OSError as exc:
            raise RoutineError("routine_install_path_unavailable", status_code=503) from exc
    return snapshot


def _restore_install_files(snapshot: Mapping[Path, bytes | None]) -> None:
    """Best-effort rollback for files created by an uncommitted install."""

    for path, original in snapshot.items():
        try:
            if path.is_symlink():
                # Never unlink a replacement symlink during recovery.
                continue
            if original is None:
                if path.is_file():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
        except OSError:
            # The durable routine row is the authority.  A failed local
            # cleanup remains visible through the install failure/recovery
            # receipt and must not turn into an unsafe broad directory delete.
            continue


def _safe_board_id(value: Any, *, field: str, max_length: int = 256) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > max_length or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-/"
        for character in candidate
    ):
        raise RoutineError("board_source_identifier_invalid", f"{field} is not a safe board identifier", status_code=422)
    return candidate


def _preview_bucket(timestamp: datetime | None = None) -> int:
    observed = timestamp or _now()
    return int(observed.timestamp()) // 60


def _preview_expiry(bucket: int) -> datetime:
    return datetime.fromtimestamp(
        (int(bucket) + BOARD_ROUTINE_PREVIEW_TTL_SECONDS // 60) * 60,
        tz=timezone.utc,
    )


def _safe_routine_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the allow-listed public provenance projection.

    Routine source proof is already content-free, but the read/export boundary
    must remain safe if a legacy or future producer adds a private field.
    """

    allowed = {
        "source_watch_job_id",
        "source_packet_id",
        "source_m3_job_id",
        "source_watch_id",
        "goal_id",
        "goal_revision",
        "plan_revision",
        "dossier_artifact_id",
        "dossier_sha256",
        "m3_result_digest",
        "source_repository",
        "source_action",
        "source_target",
        "preview_digest",
        "deterministic_routine_id",
        "source_task_id",
        "source_attempt_id",
        "action_task_id",
        "action_attempt_id",
        "source_task_revision",
        "action_task_revision",
        "source_packet_ids",
        "source_task_artifact_refs",
        "action_task_artifact_refs",
        "source_attempt_receipt_refs",
        "action_attempt_receipt_refs",
        # Reviewed procedure v2 provenance.  These fields are immutable
        # identifiers, digests, and the strict server-owned plan; no source
        # content, URL, credential, or caller authority is retained here.
        "schema_version",
        "template_id",
        "source_refs",
        "source_task_ids",
        "source_attempt_ids",
        "source_job_ids",
        "verified_artifact_ids_and_hashes",
        "capability_versions",
        "plan_digest",
        "parameter_schema",
        "plan",
        "source_proof_digest",
        "preview_expires_at",
        "version",
    }
    evidence_keys = {
        "source_task_artifact_refs",
        "action_task_artifact_refs",
        "source_attempt_receipt_refs",
        "action_attempt_receipt_refs",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        if key not in value:
            continue
        item = value[key]
        if key in evidence_keys:
            bounded_refs = _safe_board_evidence_refs(item)
            if bounded_refs:
                result[key] = bounded_refs
            continue
        if key in {"plan", "parameter_schema"}:
            try:
                plan = validate_procedure_plan(item) if key == "plan" else item
                value_to_store = (
                    plan.model_dump(mode="json")
                    if key == "plan"
                    else [
                        {
                            "name": entry.get("name"),
                            "kind": entry.get("kind"),
                            "required": entry.get("required"),
                        }
                        for entry in item
                        if isinstance(entry, Mapping)
                    ]
                )
                if isinstance(value_to_store, (dict, list)):
                    result[key] = value_to_store
            except Exception:
                continue
            continue
        if key == "source_refs":
            if isinstance(item, list) and len(item) <= 2:
                safe_refs: list[dict[str, Any]] = []
                for entry in item:
                    if not isinstance(entry, Mapping):
                        continue
                    safe_entry = {
                        field: entry[field]
                        for field in (
                            "task_id",
                            "task_revision",
                            "attempt_id",
                            "job_id",
                            "artifact_ids_and_hashes",
                            "capability_id",
                            "capability_version",
                            "goal_id",
                            "goal_revision",
                        )
                        if field in entry
                    }
                    artifacts = safe_entry.get("artifact_ids_and_hashes")
                    if isinstance(artifacts, list) and len(artifacts) <= 20:
                        safe_entry["artifact_ids_and_hashes"] = [
                            {
                                "artifact_id": artifact.get("artifact_id"),
                                "sha256": artifact.get("sha256"),
                            }
                            for artifact in artifacts
                            if isinstance(artifact, Mapping)
                            and isinstance(artifact.get("artifact_id"), str)
                            and isinstance(artifact.get("sha256"), str)
                        ]
                    safe_refs.append(safe_entry)
                if len(safe_refs) == len(item):
                    result[key] = safe_refs
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
        elif isinstance(item, list) and len(item) <= 20 and all(
            isinstance(entry, (str, int, float, bool)) or entry is None for entry in item
        ):
            result[key] = item
    return result


def _safe_board_evidence_refs(
    value: Any,
    *,
    limit: int = 20,
    expected_workflow_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Keep only bounded board artifact/readback IDs paired with SHA-256 data.

    The board repository already owns receipt validation and its allowed field
    vocabulary.  Reuse that projection, then narrow it further for routine
    provenance: paths, effect IDs, summaries, and arbitrary receipt fields are
    never copied into a reusable procedure's public source record.
    """

    decoded = _load(value, []) if isinstance(value, str) else value
    safe_refs = _safe_receipt_refs(decoded, limit=limit, preserve_effect_ids=False)
    allowed = {
        "artifact_id",
        "artifact_type",
        "receipt_kind",
        "readback_id",
        "verification_id",
        "workflow_run_id",
        "content_sha256",
        "target_digest",
        "status",
        "verified",
        "readback_status",
        "verification_status",
        "effect_id_digest",
    }
    identifier_keys = ("artifact_id", "readback_id", "verification_id", "workflow_run_id")
    digest_keys = ("content_sha256", "target_digest")
    result: list[dict[str, Any]] = []
    expected_run_id = safe_workflow_run_id(expected_workflow_run_id) if expected_workflow_run_id else None
    if expected_workflow_run_id and expected_run_id is None:
        return result
    for item in safe_refs:
        bounded = {key: item[key] for key in allowed if key in item}
        if expected_run_id:
            observed_run_id = str(bounded.get("workflow_run_id") or "").strip()
            if observed_run_id and observed_run_id != expected_run_id:
                continue
            bounded["workflow_run_id"] = expected_run_id
        if not any(str(bounded.get(key) or "").strip() for key in identifier_keys):
            continue
        if not any(str(bounded.get(key) or "").strip() for key in digest_keys):
            continue
        result.append(bounded)
        if len(result) >= limit:
            break
    return result


def _strict_board_evidence_refs(
    value: Any,
    *,
    kind: str,
    expected_workflow_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Validate the board's persisted evidence before durable binding.

    ``_safe_board_evidence_refs`` is intentionally a projection helper: it
    drops fields it cannot safely expose.  A routine source binding cannot
    use that lossy behavior because a dropped status or run identity could
    turn a malformed board receipt into an apparently valid one.  This
    helper therefore rejects the complete entry when any required field is
    absent or malformed, while still returning only the existing safe
    receipt vocabulary.
    """

    decoded = _load(value, []) if isinstance(value, str) else value
    if not isinstance(decoded, list) or not decoded or len(decoded) > 20:
        return []
    expected_run = safe_workflow_run_id(expected_workflow_run_id) if expected_workflow_run_id else None
    if expected_workflow_run_id and expected_run is None:
        return []
    result: list[dict[str, Any]] = []
    for raw in decoded:
        if not isinstance(raw, Mapping):
            return []
        evidence_identifiers = (
            ("artifact_id",)
            if kind == "artifact"
            else ("readback_id", "verification_id")
        )
        # Attempts also retain safe job/status receipts beside the dedicated
        # readback proof. Ignore those non-evidence rows, but fail closed when
        # a row claiming to be evidence is incomplete or malformed.
        if not any(str(raw.get(key) or "").strip() for key in evidence_identifiers):
            continue
        safe = _safe_board_evidence_refs([raw])
        if len(safe) != 1:
            return []
        bounded = safe[0]
        raw_run = str(raw.get("workflow_run_id") or "").strip()
        if raw_run:
            if safe_workflow_run_id(raw_run) != raw_run:
                return []
            if expected_run and raw_run != expected_run:
                return []
        elif expected_run and kind == "readback":
            # Attempt receipts must carry their immutable run binding.  The
            # outer WorkBoardAttempt link is not a substitute for a missing
            # receipt field when the receipt is copied into provenance.
            return []
        if kind == "artifact":
            if not str(bounded.get("artifact_id") or "").strip():
                return []
            if not safe_sha256_digest(
                bounded.get("content_sha256") or bounded.get("target_digest")
            ):
                return []
            if "status" in raw and str(raw.get("status") or "").strip() != "succeeded":
                return []
            if "verified" in raw and raw.get("verified") is not True:
                return []
        elif kind == "readback":
            if not str(bounded.get("readback_id") or "").strip():
                return []
            if str(raw.get("status") or "").strip() != "succeeded":
                return []
            if not safe_sha256_digest(
                bounded.get("content_sha256") or bounded.get("target_digest")
            ):
                return []
            if "verified" in raw and raw.get("verified") is not True:
                return []
            if "readback_status" in raw and raw.get("readback_status") != "verified":
                return []
            if "verification_status" in raw and raw.get("verification_status") != "passed":
                return []
        else:
            return []
        if expected_run:
            bounded["workflow_run_id"] = expected_run
        result.append(bounded)
    return result


def _board_projection_run_id(job: Mapping[str, Any] | None) -> str | None:
    if not isinstance(job, Mapping):
        return None
    run_identity = str(job.get("run_identity") or "").strip()
    job_id = str(job.get("job_id") or "").strip()
    if run_identity and job_id and run_identity != job_id:
        return None
    return safe_workflow_run_id(run_identity or job_id)


def _board_canonical_digest(value: Mapping[str, Any]) -> str | None:
    details = value.get("details") if isinstance(value.get("details"), Mapping) else {}
    return safe_sha256_digest(
        value.get("content_sha256")
        or value.get("target_digest")
        or details.get("content_sha256")
        or details.get("target_digest")
        or details.get("readback_digest")
    )


def _bind_board_task_artifacts(
    refs: list[dict[str, Any]],
    job: Mapping[str, Any] | None,
    *,
    workflow_run_id: str,
) -> list[dict[str, Any]]:
    """Bind task artifact IDs/digests to the exact durable run artifacts."""

    if (
        _board_projection_run_id(job) != workflow_run_id
        or not isinstance(job, Mapping)
        or str(job.get("status") or "") != "succeeded"
    ):
        raise RoutineError("board_journey_task_artifact_binding_mismatch")
    artifacts = job.get("artifacts")
    if not isinstance(artifacts, list):
        raise RoutineError("board_journey_task_artifact_binding_mismatch")
    bound: list[dict[str, Any]] = []
    for ref in refs:
        artifact_id = str(ref.get("artifact_id") or "").strip()
        expected_digest = safe_sha256_digest(ref.get("content_sha256") or ref.get("target_digest"))
        if not artifact_id or expected_digest is None:
            raise RoutineError("board_journey_task_artifact_binding_mismatch")
        ref_run_id = str(ref.get("workflow_run_id") or "").strip()
        if ref_run_id and ref_run_id != workflow_run_id:
            raise RoutineError("board_journey_task_artifact_binding_mismatch")
        matches = [
            item
            for item in artifacts
            if isinstance(item, Mapping) and str(item.get("artifact_id") or "").strip() == artifact_id
        ]
        if len(matches) != 1:
            raise RoutineError("board_journey_task_artifact_binding_mismatch")
        artifact = matches[0]
        artifact_run_id = str(
            artifact.get("workflow_run_id") or artifact.get("run_id") or artifact.get("job_id") or ""
        ).strip()
        if artifact_run_id and artifact_run_id != workflow_run_id:
            raise RoutineError("board_journey_task_artifact_binding_mismatch")
        artifact_digest = safe_sha256_digest(
            artifact.get("content_sha256") or artifact.get("target_digest")
        )
        if artifact_digest is None or artifact_digest != expected_digest:
            raise RoutineError("board_journey_task_artifact_binding_mismatch")
        artifact_status = str(artifact.get("status") or "").strip()
        if artifact_status and artifact_status not in {"succeeded", "recorded"}:
            raise RoutineError("board_journey_task_artifact_binding_mismatch")
        if "exists" in artifact and artifact.get("exists") is not True:
            raise RoutineError("board_journey_task_artifact_binding_mismatch")
        normalized = {
            "artifact_id": artifact_id,
            "workflow_run_id": workflow_run_id,
            "status": "succeeded",
            "content_sha256": artifact_digest,
        }
        artifact_type = str(artifact.get("artifact_type") or "").strip()
        if artifact_type:
            normalized["artifact_type"] = artifact_type
        bound.append(normalized)
    return bound


def _bind_guardian_packet_artifacts(
    refs: list[dict[str, Any]],
    packet: GuardianDecisionPacket,
    job: Mapping[str, Any] | None,
    *,
    workflow_run_id: str,
) -> list[dict[str, Any]]:
    """Bind source-watch task artifacts to its immutable verified packet."""

    if (
        str(packet.run_identity or "") != workflow_run_id
        or str(packet.status or "") != "succeeded"
        or str(packet.verification_status or "") != "passed"
        or _board_projection_run_id(job) != workflow_run_id
        or not isinstance(job, Mapping)
        or str(job.get("status") or "") != "succeeded"
    ):
        raise RoutineError("board_journey_task_artifact_binding_mismatch")
    canonical = {
        str(packet.dossier_artifact_id or ""): safe_sha256_digest(packet.dossier_sha256),
        str(packet.task_artifact_id or ""): safe_sha256_digest(packet.task_sha256),
    }
    if len(canonical) != 2 or "" in canonical or any(value is None for value in canonical.values()):
        raise RoutineError("board_journey_task_artifact_binding_mismatch")
    bound: list[dict[str, Any]] = []
    for ref in refs:
        artifact_id = str(ref.get("artifact_id") or "").strip()
        expected_digest = safe_sha256_digest(ref.get("content_sha256") or ref.get("target_digest"))
        ref_run_id = str(ref.get("workflow_run_id") or "").strip()
        if (
            artifact_id not in canonical
            or expected_digest is None
            or canonical[artifact_id] != expected_digest
            or (ref_run_id and ref_run_id != workflow_run_id)
        ):
            raise RoutineError("board_journey_task_artifact_binding_mismatch")
        normalized = {
            "artifact_id": artifact_id,
            "workflow_run_id": workflow_run_id,
            "status": "succeeded",
            "content_sha256": expected_digest,
        }
        artifact_type = str(ref.get("artifact_type") or "").strip()
        if artifact_type:
            normalized["artifact_type"] = artifact_type
        bound.append(normalized)
    return bound


def _bind_board_attempt_readbacks(
    refs: list[dict[str, Any]],
    job: Mapping[str, Any] | None,
    *,
    workflow_run_id: str,
) -> list[dict[str, Any]]:
    """Bind board readback IDs/digests to one canonical durable effect."""

    if (
        _board_projection_run_id(job) != workflow_run_id
        or not isinstance(job, Mapping)
        or str(job.get("status") or "") != "succeeded"
    ):
        raise RoutineError("board_journey_attempt_readback_binding_mismatch")
    effects = job.get("effects")
    if not isinstance(effects, list):
        raise RoutineError("board_journey_attempt_readback_binding_mismatch")
    bound: list[dict[str, Any]] = []
    for ref in refs:
        readback_id = str(ref.get("readback_id") or "").strip()
        expected_digest = safe_sha256_digest(ref.get("content_sha256") or ref.get("target_digest"))
        if not readback_id or expected_digest is None:
            raise RoutineError("board_journey_attempt_readback_binding_mismatch")
        matches: list[Mapping[str, Any]] = []
        for effect in effects:
            if not isinstance(effect, Mapping) or str(effect.get("receipt_kind") or "") != "readback":
                continue
            details = effect.get("details") if isinstance(effect.get("details"), Mapping) else {}
            canonical_readback_id = str(
                effect.get("readback_id") or details.get("readback_id") or ""
            ).strip()
            if canonical_readback_id == readback_id:
                matches.append(effect)
        if len(matches) != 1:
            raise RoutineError("board_journey_attempt_readback_binding_mismatch")
        effect = matches[0]
        details = effect.get("details") if isinstance(effect.get("details"), Mapping) else {}
        effect_run_id = str(effect.get("workflow_run_id") or details.get("workflow_run_id") or "").strip()
        if effect_run_id and effect_run_id != workflow_run_id:
            raise RoutineError("board_journey_attempt_readback_binding_mismatch")
        if str(effect.get("status") or "") != "succeeded" or effect.get("reconciled") is not True:
            raise RoutineError("board_journey_attempt_readback_binding_mismatch")
        canonical_digest = _board_canonical_digest(effect)
        if canonical_digest is None or canonical_digest != expected_digest:
            raise RoutineError("board_journey_attempt_readback_binding_mismatch")
        effect_id = str(effect.get("effect_id") or "").strip()
        expected_effect_digest = str(ref.get("effect_id_digest") or "").strip().lower()
        if expected_effect_digest and (
            not effect_id
            or hashlib.sha256(effect_id.encode("utf-8")).hexdigest()[:16] != expected_effect_digest
        ):
            raise RoutineError("board_journey_attempt_readback_binding_mismatch")
        bound_ref = {
            "readback_id": readback_id,
            "workflow_run_id": workflow_run_id,
            "status": "succeeded",
            "verified": True,
            "readback_status": "verified",
            "verification_status": "passed",
            "content_sha256": canonical_digest,
        }
        if expected_effect_digest:
            bound_ref["effect_id_digest"] = expected_effect_digest
        bound.append(bound_ref)
    return bound


def _same_revision(value: Any, expected: int) -> bool:
    try:
        return int(value) == int(expected)
    except (TypeError, ValueError, OverflowError):
        return False


def _verified_readback(job: Mapping[str, Any] | None) -> bool:
    if not isinstance(job, Mapping) or job.get("status") != "succeeded":
        return False
    for effect in job.get("effects", []):
        if not isinstance(effect, Mapping):
            continue
        if (
            effect.get("receipt_kind") == "readback"
            and effect.get("status") == "succeeded"
            and effect.get("reconciled") is True
        ):
            return True
    return False


def _owner_matches(job: Mapping[str, Any] | None, principal_id: str, session_id: str) -> bool:
    if not isinstance(job, Mapping):
        return False
    authority = job.get("declared_authority")
    if not isinstance(authority, Mapping):
        authority = {}
    owner = job.get("owner") if isinstance(job.get("owner"), Mapping) else {}
    owner_kind = str(owner.get("kind") or authority.get("owner_kind") or "")
    delegated_principal = str(
        authority.get("goal_owner_principal_id")
        or (owner.get("principal_id") if owner_kind == "user" else "")
        or ""
    )
    delegated_session = str(
        authority.get("goal_owner_session_id")
        or authority.get("session_id")
        or ""
    )
    persisted_sessions = {
        str(value)
        for value in (
            authority.get("session_id"),
            job.get("operator_session_id"),
            job.get("session_id"),
        )
        if str(value or "")
    }
    if owner_kind == "service" and (
        not authority.get("goal_owner_principal_id")
        or not authority.get("goal_owner_session_id")
    ):
        return False
    if owner_kind == "user" and str(owner.get("principal_id") or "") != str(principal_id):
        return False
    return (
        delegated_principal == str(principal_id)
        and delegated_session == str(session_id)
        and persisted_sessions
        and persisted_sessions == {str(session_id)}
    )


def _safe_invocation_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RoutineError("invocation_uuid_invalid", status_code=422) from exc
    return str(parsed)


def _board_invocation_input_descriptor(
    *,
    routine_id: str,
    req: RoutineInvokeRequest,
    invocation_uuid: str,
) -> tuple[dict[str, Any], str, str, str, str]:
    """Build the immutable typed-input identity for one board invocation."""

    envelope = {
        "schema_version": BOARD_ROUTINE_INPUT_SCHEMA_VERSION,
        "capability_id": ROUTINE_CAPABILITY_VERSION,
        "input": {
            "routine_id": routine_id,
            "version": int(req.version),
            "expected_routine_revision": int(req.expected_routine_revision),
            "goal_id": req.goal_id,
            "expected_goal_revision": int(req.expected_goal_revision),
            "source_watch_id": req.source_watch_id,
            "expected_watch_revision": int(req.expected_watch_revision),
        },
    }
    encoded = _dump(envelope)
    typed_digest = _sha(encoded)
    relative_path = f"artifacts/work-board/routine-inputs/{invocation_uuid}-{typed_digest}.json"
    return envelope, encoded, typed_digest, relative_path, f"workspace-json:{relative_path}"


def _read_board_invocation_input(
    *,
    relative_path: str,
    expected_digest: str,
    expected_envelope: Mapping[str, Any],
) -> None:
    """Verify the immutable board input bytes and their exact typed binding."""

    try:
        text, truncated = _read_workspace_text_bounded(
            _safe_resolve(relative_path),
            max_bytes=64 * 1024,
        )
    except (OSError, ValueError) as exc:
        raise RoutineError("routine_invocation_input_unavailable", str(exc), status_code=503) from exc
    if truncated or _sha(text) != expected_digest:
        raise RoutineError("routine_invocation_input_integrity", status_code=409)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise RoutineError("routine_invocation_input_integrity", status_code=409) from exc
    if parsed != dict(expected_envelope):
        raise RoutineError("routine_invocation_input_integrity", status_code=409)


def _child_job_id(invocation_uuid: str, step_id: str) -> str:
    """Return the stable UUIDv5 job identity for one fixed routine step."""

    namespace = uuid.UUID(invocation_uuid)
    child_uuid = uuid.uuid5(namespace, f"seraph:guardian-routine:{step_id}")
    return f"routine-child:{child_uuid.hex}"


def _job_checkpoint(job: Mapping[str, Any] | None, checkpoint_id: str) -> dict[str, Any] | None:
    if not isinstance(job, Mapping):
        return None
    for item in reversed(job.get("checkpoints", []) or []):
        if not isinstance(item, Mapping) or item.get("checkpoint_id") != checkpoint_id:
            continue
        payload = item.get("payload")
        return dict(payload) if isinstance(payload, Mapping) else dict(item)
    return None


def _job_checkpoint_any(
    job: Mapping[str, Any] | None,
    checkpoint_ids: tuple[str, ...],
) -> dict[str, Any] | None:
    """Return the newest checkpoint from a small, explicitly bound set."""

    if not isinstance(job, Mapping):
        return None
    wanted = set(checkpoint_ids)
    for item in reversed(job.get("checkpoints", []) or []):
        if not isinstance(item, Mapping) or item.get("checkpoint_id") not in wanted:
            continue
        payload = item.get("payload")
        return dict(payload) if isinstance(payload, Mapping) else dict(item)
    return None


def _publication_binding_checkpoint(job: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Read either the pre-prepare adoption fence or its finalized binding."""

    return _job_checkpoint_any(
        job,
        ("routine-child:adoption_pending", "routine-child:prepared"),
    )


def _expected_publication_job_id(owner_principal_id: str, operation_uuid: str) -> str:
    """Derive M3's deterministic job id before calling its prepare boundary."""

    return f"ghfollow_{_operation_id(owner_principal_id, uuid.UUID(operation_uuid)).hex}"


class RoutineService:
    """Small service over existing package, watch, approval, and job stores."""

    async def _v2_install_approval_projection(
        self,
        *,
        job_id: str,
        job: Mapping[str, Any] | None,
        owner_principal_id: str,
        owner_session_id: str,
        routine_id: str,
        version: int,
    ) -> dict[str, Any]:
        """Project one v2 install approval from its canonical bound row.

        The durable install job is the locator, but its embedded approval id
        is not sufficient proof for a public projection.  Require the job's
        immutable owner/session/routine binding and then re-read the canonical
        ApprovalRequest with the same owner/session and durable job id.  A
        missing or mismatched row is deliberately indistinguishable from a
        missing approval so an operator cannot use another session's receipt.
        Expiry is evaluated against current UTC time without mutating the
        approval row; the existing approval lifecycle remains the authority
        for decisions and consumption.
        """

        def text_value(value: Any) -> str:
            return str(value or "").strip()

        def missing(*, recovery_action: str = "create_fresh_preview") -> dict[str, Any]:
            return {
                "approval_id": None,
                "install_approval_status": "missing",
                "install_approval_expires_at": None,
                "install_recovery_action": recovery_action,
            }

        expected_job_id = str(job_id or "").strip()
        if not expected_job_id or not isinstance(job, Mapping):
            return missing()
        if str(job.get("job_id") or "").strip() != expected_job_id:
            return missing()
        authority = job.get("declared_authority")
        if not isinstance(authority, Mapping):
            return missing()
        if (
            str(authority.get("principal") or "").strip() != str(owner_principal_id).strip()
            or str(authority.get("owner_kind") or "").strip() != "user"
            or str(authority.get("session_id") or "").strip() != str(owner_session_id).strip()
            or str(authority.get("routine_id") or "").strip() != str(routine_id).strip()
        ):
            return missing()
        try:
            authority_version = int(authority.get("routine_version") or 0)
        except (TypeError, ValueError, OverflowError):
            return missing()
        if authority_version != int(version):
            return missing()
        approval_id = text_value(authority.get("approval_id"))
        if not approval_id:
            return missing()

        approval = await approval_repository.get(approval_id)
        if approval is None:
            return missing()
        if (
            text_value(getattr(approval, "id", None)) != approval_id
            or text_value(getattr(approval, "session_id", None)) != str(owner_session_id).strip()
            or text_value(getattr(approval, "owner_principal_id", None)) != str(owner_principal_id).strip()
            or text_value(getattr(approval, "operator_session_id", None)) != str(owner_session_id).strip()
            or text_value(getattr(approval, "tool_name", None)) != ROUTINE_INSTALL_TOOL
        ):
            return missing()
        details = _load(getattr(approval, "details_json", None), {})
        if not isinstance(details, Mapping):
            return missing()
        if (
            text_value(details.get("approval_id")) != approval_id
            or text_value(details.get("durable_approval_id")) != approval_id
            or text_value(details.get("durable_job_id")) != expected_job_id
            or text_value(details.get("durable_owner_kind")) != "user"
            or text_value(details.get("durable_owner_principal_id")) != str(owner_principal_id).strip()
            or text_value(details.get("approval_owner_operator_session_id")) != str(owner_session_id).strip()
        ):
            return missing()

        status = text_value(getattr(approval, "status", None)).lower()
        if status not in {"pending", "approved", "denied", "expired", "consumed"}:
            return missing()
        expires_at_value = getattr(approval, "expires_at", None)
        expires_at = _utc(expires_at_value) if isinstance(expires_at_value, datetime) else None
        if status in {"pending", "approved"} and expires_at is None:
            return missing()
        if status in {"pending", "approved"} and expires_at <= _now():
            status = "expired"
        recovery_action: str | None = None
        if status in {"expired", "denied", "missing"}:
            recovery_action = "create_fresh_preview"
        elif status == "consumed":
            # A consumed approval may correspond to an install whose final
            # receipt has not reached this projection yet.  Keep that receipt
            # addressable so the existing lifecycle can reconcile it; never
            # mint a second approval here.
            recovery_action = "reconcile_install_receipt"
        return {
            "approval_id": approval_id,
            "install_approval_status": status,
            "install_approval_expires_at": expires_at.isoformat().replace("+00:00", "Z") if expires_at else None,
            "install_recovery_action": recovery_action,
        }

    def _procedure_v2(self) -> ProcedureV2Service:
        """Return the narrow schema-2 service behind the routines surface."""

        return ProcedureV2Service(self)

    async def preview_from_tasks(
        self,
        req: ProcedureV2PreviewRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        return await self._procedure_v2().preview_from_tasks(
            req,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )

    async def create_from_tasks(
        self,
        req: ProcedureV2CreateRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[dict[str, Any], int]:
        return await self._procedure_v2().create_from_tasks(
            req,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )

    async def invoke_v2(
        self,
        routine_id: str,
        req: ProcedureV2InvokeRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[dict[str, Any], int]:
        return await self._procedure_v2().invoke_v2(
            routine_id,
            req,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )

    async def schedule_v2(
        self,
        routine_id: str,
        req: ProcedureV2ScheduleRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[dict[str, Any], int]:
        return await self._procedure_v2().schedule_v2(
            routine_id,
            req,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )

    async def resolve_v2_version(
        self,
        routine_id: str,
        version: int,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ):
        return await self._procedure_v2().resolve_v2_version(
            routine_id,
            version,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )

    async def validate_v2_invocation_authority(
        self,
        routine_id: str,
        version_number: int,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str,
        expected_goal_revision: int,
        parameters: Mapping[str, Any],
        invocation_uuid: str,
    ) -> V2InvocationDescriptor:
        return await self._procedure_v2().validate_v2_invocation_authority(
            routine_id,
            version_number,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=goal_id,
            expected_goal_revision=expected_goal_revision,
            parameters=parameters,
            invocation_uuid=invocation_uuid,
        )

    async def _routine(self, routine_id: str, owner_principal_id: str) -> GuardianRoutine:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GuardianRoutine).where(
                        GuardianRoutine.id == routine_id,
                        GuardianRoutine.owner_principal_id == owner_principal_id,
                    )
                )
            ).scalars().first()
            if row is None:
                raise RoutineError("routine_not_found", status_code=404)
            db.expunge(row)
            return row

    @staticmethod
    def _require_routine_owner_session(routine: GuardianRoutine, owner_session_id: str) -> None:
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)

    async def _require_active_routine(
        self,
        routine_id: str,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        expected_revision: int,
    ) -> GuardianRoutine:
        """Re-read the canonical routine before any child admission/recovery."""

        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal", status_code=409)
        if routine.state != "active" or int(routine.revision or 0) != int(expected_revision):
            raise RoutineError("routine_not_active_or_stale")
        return routine

    async def _require_active_package_binding(
        self,
        routine_id: str,
        *,
        version: Any,
        expected_digest: str,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Revalidate the exact selected package before a durable resume.

        Routine and package lifecycle state are separate records.  An approval
        can therefore outlive a package revoke, quarantine, or on-disk
        mutation.  Every approval/recovery boundary must re-read the selected
        version and its active lifecycle pointer before it can consume that
        approval or dispatch a child.
        """

        try:
            selected_version = int(version)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RoutineError("routine_version_binding_invalid") from exc
        digest = str(expected_digest or "")
        if selected_version <= 0 or not digest:
            raise RoutineError("routine_version_not_installed")
        selected = await self._version(routine_id, selected_version)
        installed_digest = str(selected.installed_package_digest or "")
        if not installed_digest:
            raise RoutineError("routine_version_not_installed")
        if installed_digest != digest:
            raise RoutineError("package_review_required")
        package = self._package_readback(
            owner_principal_id,
            owner_session_id,
            routine_id,
            selected_version,
            digest,
        )
        if package.get("status") != "active" or package.get("digest") != digest:
            raise RoutineError("package_review_required")
        return package

    async def _version(self, routine_id: str, version: int) -> GuardianRoutineVersion:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GuardianRoutineVersion).where(
                        GuardianRoutineVersion.routine_id == routine_id,
                        GuardianRoutineVersion.version == version,
                    )
                )
            ).scalars().first()
            if row is None:
                raise RoutineError("routine_version_not_found", status_code=404)
            db.expunge(row)
            return row

    @staticmethod
    def _version_json(version: GuardianRoutineVersion) -> dict[str, Any]:
        return {
            "id": version.id,
            "routine_id": version.routine_id,
            "version": version.version,
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "installed_package_digest": version.installed_package_digest,
            "source_provenance": _safe_routine_provenance(_load(version.source_provenance_json, {})),
            "source_repository": version.source_repository,
            "source_action": version.source_action,
            "source_issue_number": version.source_issue_number,
            "created_at": version.created_at.isoformat(),
            "installed_at": version.installed_at.isoformat() if version.installed_at else None,
        }

    async def read(self, routine_id: str, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        async with db_engine.get_session() as db:
            versions = (
                await db.execute(
                    select(GuardianRoutineVersion)
                    .where(GuardianRoutineVersion.routine_id == routine.id)
                    .order_by(GuardianRoutineVersion.version)
                )
            ).scalars().all()
            procedure_bindings = (
                await db.execute(
                    select(ProcedureV2Binding).where(
                        ProcedureV2Binding.owner_principal_id == owner_principal_id,
                        ProcedureV2Binding.owner_session_id == owner_session_id,
                        ProcedureV2Binding.deterministic_routine_id == routine.id,
                    )
                )
            ).scalars().all()
            for item in versions:
                db.expunge(item)
            for item in procedure_bindings:
                db.expunge(item)
        binding_by_version_id = {
            str(item.version_id): item for item in procedure_bindings if item.version_id
        }
        version_payloads: list[dict[str, Any]] = []
        package_by_version: dict[int, dict[str, Any]] = {}
        for item in versions:
            package = self._package_readback(
                owner_principal_id,
                owner_session_id,
                str(routine.id),
                int(item.version),
                item.installed_package_digest,
            )
            if (
                not item.installed_package_digest
                and package.get("status") == "blocked"
                and package.get("reason") == "routine_package_unavailable"
            ):
                package = {"status": "not_installed", "digest": None, "review_id": None}
            package_by_version[int(item.version)] = package
            payload = self._version_json(item)
            payload["package"] = package
            provenance = _load(item.source_provenance_json, {})
            if isinstance(provenance, Mapping) and provenance.get("schema_version") == 2:
                binding = binding_by_version_id.get(str(item.id))
                install_job_id = f"routine-install:{routine.id}:v{int(item.version)}"
                install_job = await durable_job_repository.get_job(install_job_id)
                install_approval = await self._v2_install_approval_projection(
                    job_id=install_job_id,
                    job=install_job if isinstance(install_job, Mapping) else None,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    routine_id=str(routine.id),
                    version=int(item.version),
                )
                payload.update(
                    {
                        "schema_version": 2,
                        "template_id": str(provenance.get("template_id") or ""),
                        "plan_digest": str(provenance.get("plan_digest") or ""),
                        "source_proof_digest": str(provenance.get("source_proof_digest") or ""),
                        "source_refs": provenance.get("source_refs") if isinstance(provenance.get("source_refs"), list) else [],
                        "parameter_schema": provenance.get("parameter_schema") if isinstance(provenance.get("parameter_schema"), list) else [],
                        "procedure_binding": {
                            "binding_id": binding.binding_id if binding else None,
                            "state": binding.state if binding else "prepared",
                            "revision": int(binding.revision) if binding else None,
                            "preview_digest": binding.preview_digest if binding else provenance.get("preview_digest"),
                            "preview_expires_at": (
                                _utc(binding.preview_expires_at).isoformat().replace("+00:00", "Z")
                                if binding
                                else provenance.get("preview_expires_at")
                            ),
                            "install_job_id": install_job_id,
                            **install_approval,
                        },
                    }
                )
            version_payloads.append(payload)
        package = package_by_version.get(
            int(routine.current_version or 0),
            {"status": "not_installed", "digest": None, "review_id": None},
        )
        return {
            "id": routine.id,
            "owner_principal_id": routine.owner_principal_id,
            "state": routine.state,
            "revision": routine.revision,
            "current_version": routine.current_version,
            "name": routine.name,
            "versions": version_payloads,
            "package": package,
        }

    async def list(self, *, owner_principal_id: str, owner_session_id: str) -> list[dict[str, Any]]:
        async with db_engine.get_session() as db:
            rows = (
                await db.execute(
                    select(GuardianRoutine)
                    .where(GuardianRoutine.owner_principal_id == owner_principal_id)
                    .where(GuardianRoutine.owner_session_id == owner_session_id)
                    .order_by(GuardianRoutine.updated_at.desc())
                )
            ).scalars().all()
            ids = [row.id for row in rows]
            for row in rows:
                db.expunge(row)
        result = []
        for routine_id in ids:
            result.append(await self.read(routine_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id))
        return result

    def _materialize_routine_package(
        self,
        routine_id: str,
        version: GuardianRoutineVersion,
        *,
        allow_create: bool,
        root_override: Path | None = None,
    ) -> dict[str, Any]:
        """Create or verify one immutable v2 package from persisted routine data."""

        if str(version.routine_id) != str(routine_id):
            raise RoutineError("routine_package_version_binding_invalid", status_code=409)
        using_override = root_override is not None
        root = Path(root_override) if using_override else _routine_pack_root(routine_id, int(version.version))
        provenance = _load(version.source_provenance_json, {})
        manifest_payload = _routine_pack_manifest_payload(
            routine_id=routine_id,
            version=int(version.version),
            provenance=provenance if isinstance(provenance, Mapping) else None,
        )
        manifest_content = yaml.safe_dump(manifest_payload, sort_keys=True, allow_unicode=False)
        runbook_payload = _routine_pack_runbook_payload(
            routine_id=routine_id,
            version=version,
            provenance=provenance if isinstance(provenance, Mapping) else {},
        )
        runbook_content = yaml.safe_dump(runbook_payload, sort_keys=True, allow_unicode=False)
        expected_files = {
            "manifest.yaml": manifest_content,
            ROUTINE_PACK_RUNBOOK_REFERENCE: runbook_content,
        }
        if root.exists() and not root.is_dir():
            raise RoutineError("routine_package_mutation_detected", status_code=409)
        if version.installed_package_digest and not root.exists():
            raise RoutineError("routine_package_mutation_detected", status_code=409)
        if not root.exists():
            if not allow_create:
                raise RoutineError("routine_package_unavailable", status_code=409)
            root.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating parents so an existing symlink cannot turn
        # a later member write into an out-of-workspace mutation.  Staged
        # installs deliberately keep their private root here; resolving back
        # to the discoverable package root would expose files before the
        # canonical selector transaction commits.
        if not using_override:
            root = _routine_pack_root(routine_id, int(version.version))
        if root.is_symlink() or not root.is_dir():
            raise RoutineError("routine_package_mutation_detected", status_code=409)
        for relative, content in expected_files.items():
            path = root / relative
            if not path.exists() and version.installed_package_digest:
                raise RoutineError("routine_package_mutation_detected", status_code=409)
            if not path.exists() and not allow_create:
                raise RoutineError("routine_package_unavailable", status_code=409)
            _create_once_workspace_text(path, content)
        for path in root.rglob("*"):
            if path.is_symlink() or (path.is_file() and path.relative_to(root).as_posix() not in expected_files):
                raise RoutineError("routine_package_mutation_detected", status_code=409)
        try:
            manifest = parse_capability_pack_manifest(
                manifest_content,
                source=str(root / "manifest.yaml"),
            )
            if manifest.id != _routine_pack_id(routine_id, int(version.version)):
                raise RoutineError("routine_package_manifest_binding_invalid", status_code=409)
            validation = validate_capability_pack_path(root, manifest)
            if not validation.get("ok"):
                raise RoutineError("routine_package_validation_failed", status_code=409)
            _validate_routine_pack_runbook(
                runbook_content,
                routine_id=routine_id,
                version=version,
            )
            stored_manifest, manifest_truncated = _read_workspace_text_bounded(
                root / "manifest.yaml",
                max_bytes=256 * 1024,
            )
            stored_runbook, runbook_truncated = _read_workspace_text_bounded(
                root / ROUTINE_PACK_RUNBOOK_REFERENCE,
                max_bytes=256 * 1024,
            )
            if (
                manifest_truncated
                or runbook_truncated
                or stored_manifest != manifest_content
                or stored_runbook != runbook_content
            ):
                raise RoutineError("routine_package_mutation_detected", status_code=409)
            digest = capability_pack_digest(root)
        except RoutineError:
            raise
        except Exception as exc:
            raise RoutineError("routine_package_validation_failed", status_code=409) from exc
        return {
            "root": root,
            "pack_id": manifest.id,
            "manifest": manifest,
            "manifest_payload": manifest.model_dump(mode="json"),
            "manifest_content": manifest_content,
            "runbook_content": runbook_content,
            "digest": digest,
        }

    def _package_readback(
        self,
        owner_principal_id: str,
        owner_session_id: str,
        routine_id: str | None = None,
        version: int | None = None,
        expected_digest: str | None = None,
    ) -> dict[str, Any]:
        """Read the exact per-version package and its owner-bound lifecycle pointer."""

        if routine_id is None or version is None:
            return {"status": "blocked", "reason": "routine_package_version_required"}
        try:
            package_root = _routine_pack_root(routine_id, int(version))
            if not package_root.is_dir():
                return {"status": "blocked", "reason": "routine_package_unavailable"}
            manifest_path = package_root / "manifest.yaml"
            manifest_text, manifest_truncated = _read_workspace_text_bounded(manifest_path, max_bytes=256 * 1024)
            manifest = parse_capability_pack_manifest(manifest_text, source=str(manifest_path))
            if manifest.id != _routine_pack_id(routine_id, int(version)):
                return {"status": "blocked", "reason": "routine_package_manifest_binding_invalid"}
            validation = validate_capability_pack_path(package_root, manifest)
            if not validation.get("ok") or manifest_truncated:
                return {"status": "blocked", "reason": "routine_package_mutation_detected"}
            runbook_path = package_root / ROUTINE_PACK_RUNBOOK_REFERENCE
            runbook_text, runbook_truncated = _read_workspace_text_bounded(runbook_path, max_bytes=256 * 1024)
            try:
                runbook_payload = yaml.safe_load(runbook_text)
            except yaml.YAMLError:
                runbook_payload = None
            procedure_payload = runbook_payload.get("procedure") if isinstance(runbook_payload, Mapping) else None
            common_runbook_shape = (
                not runbook_truncated
                and isinstance(runbook_payload, Mapping)
                and set(runbook_payload)
                == {"id", "title", "summary", "starter_pack", "inputs", "procedure", "bindings"}
                and runbook_payload.get("id")
                == f"runbook:{_routine_pack_id(routine_id, int(version))}"
                and isinstance(procedure_payload, Mapping)
            )
            v1_runbook_valid = common_runbook_shape and (
                runbook_payload.get("starter_pack") == "seraph.guardian-routine.v1"
                and runbook_payload.get("inputs") == ROUTINE_PACK_INPUTS
                and procedure_payload.get("steps") == [dict(step) for step in ROUTINE_PACK_STEP_CONTRACT]
            )
            v2_runbook_valid = False
            if common_runbook_shape and procedure_payload.get("schema_version") == 2:
                template_id = str(procedure_payload.get("template_id") or "")
                try:
                    spec = get_procedure_template(template_id)
                    v2_plan = validate_procedure_plan(
                        {
                            "schema_version": 2,
                            "template_id": template_id,
                            "steps": [
                                {
                                    "step_id": step.step_id,
                                    "capability_id": step.capability_id,
                                    "capability_version": step.capability_version,
                                    "typed_input_ref": "server-owned-ref",
                                    "typed_input_digest": "0" * 64,
                                }
                                for step in spec.steps
                            ],
                            "parameters": [
                                {"name": name, "kind": kind, "required": required}
                                for name, kind, required in spec.parameters
                            ],
                            "verifier": "leaf_readbacks",
                            "limits": {"max_steps": 2, "max_total_seconds": 300},
                        }
                    )
                    v2_runbook_valid = (
                        runbook_payload.get("starter_pack") == "seraph.guardian-routine.v2"
                        and runbook_payload.get("inputs")
                        == {"routine_invocation_job_id": {"type": "string", "required": True}}
                        and procedure_payload.get("capability_id") == ROUTINE_V2_CAPABILITY_VERSION
                        and procedure_payload.get("steps")
                        == [
                            {
                                "id": step.step_id,
                                "capability_id": step.capability_id,
                                "capability_version": step.capability_version,
                            }
                            for step in spec.steps
                        ]
                        and isinstance(procedure_payload.get("plan_digest"), str)
                        and len(procedure_payload["plan_digest"]) == 64
                        and v2_plan.template_id == template_id
                    )
                except Exception:
                    v2_runbook_valid = False
            if not (v1_runbook_valid or v2_runbook_valid):
                return {"status": "blocked", "reason": "routine_package_runbook_contract_invalid"}
            lifecycle = CapabilityPackLifecycle()
            status = lifecycle.status(
                manifest.id,
                owner_principal_id=owner_principal_id,
                session_id=owner_session_id,
            )
            active = status.get("active") if isinstance(status, Mapping) else None
            digest = capability_pack_digest(package_root)
            if expected_digest and digest != str(expected_digest):
                return {"status": "blocked", "reason": "routine_package_mutation_detected", "digest": digest}
            available = status.get("available_versions", []) if isinstance(status, Mapping) else []
            reviewed = next(
                (
                    item
                    for item in available
                    if isinstance(item, Mapping) and item.get("digest") == digest
                ),
                None,
            )
            return {
                "pack_id": manifest.id,
                "status": str(
                    active.get("status")
                    if isinstance(active, Mapping)
                    else ("reviewed" if isinstance(reviewed, Mapping) else "not_reviewed")
                ),
                "digest": digest,
                "review_id": (
                    active.get("review_id")
                    if isinstance(active, Mapping)
                    else (reviewed.get("review_id") if isinstance(reviewed, Mapping) else None)
                ),
                "authority_digest": manifest.authority_digest,
                "version": manifest.version,
            }
        except RoutineError as exc:
            return {"status": "blocked", "reason": exc.code}
        except Exception as exc:
            return {"status": "blocked", "reason": type(exc).__name__}

    async def _assert_routine_source_current(
        self,
        version: GuardianRoutineVersion,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Revalidate source goal/provenance before a package review mutation."""

        raw_provenance = _load(version.source_provenance_json, {})
        if (
            isinstance(raw_provenance, Mapping)
            and type(raw_provenance.get("schema_version")) is int
            and raw_provenance.get("schema_version") == 2
        ):
            return await self._assert_v2_routine_source_current(
                version,
                raw_provenance,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )

        provenance = _safe_routine_provenance(raw_provenance if isinstance(raw_provenance, Mapping) else {})
        goal_id = str(provenance.get("goal_id") or "").strip()
        try:
            goal_revision = int(provenance.get("goal_revision") or 0)
        except (TypeError, ValueError):
            goal_revision = 0
        try:
            plan_revision = int(provenance.get("plan_revision") or 0)
        except (TypeError, ValueError):
            plan_revision = 0
        if not goal_id or goal_revision < 1 or plan_revision < 1:
            raise RoutineError("routine_source_goal_stale")
        async with db_engine.get_session() as db:
            goal = (
                await db.execute(
                    select(Goal).where(
                        Goal.id == goal_id,
                        Goal.owner_principal_id == owner_principal_id,
                        Goal.owner_session_id == owner_session_id,
                    )
                )
            ).scalars().first()
            if (
                goal is None
                or int(goal.revision or 0) != goal_revision
                or str(getattr(goal.status, "value", goal.status) or "") != "active"
            ):
                raise RoutineError("routine_source_goal_stale")
            task_specs = (
                ("source", provenance.get("source_task_id"), provenance.get("source_task_revision"), provenance.get("source_attempt_id")),
                ("action", provenance.get("action_task_id"), provenance.get("action_task_revision"), provenance.get("action_attempt_id")),
            )
            for label, task_id, task_revision, attempt_id in task_specs:
                if not task_id:
                    continue
                task = (
                    await db.execute(
                        select(WorkBoardTask).where(
                            WorkBoardTask.task_id == str(task_id),
                            WorkBoardTask.owner_principal_id == owner_principal_id,
                            WorkBoardTask.owner_session_id == owner_session_id,
                        )
                    )
                ).scalars().first()
                if (
                    task is None
                    or str(getattr(task.status, "value", task.status) or "") != WorkBoardStatus.done.value
                    or int(task.goal_revision or 0) != goal_revision
                    or (task_revision is not None and int(task.task_revision or 0) != int(task_revision))
                ):
                    raise RoutineError(f"routine_source_{label}_task_stale")
                if attempt_id:
                    attempt = (
                        await db.execute(
                            select(WorkBoardAttempt).where(
                                WorkBoardAttempt.task_id == task.task_id,
                                WorkBoardAttempt.attempt_id == str(attempt_id),
                            )
                        )
                    ).scalars().first()
                    if attempt is None or attempt.ended_at is None:
                        raise RoutineError(f"routine_source_{label}_attempt_stale")
        source_watch_id = str(provenance.get("source_watch_id") or "").strip()
        if source_watch_id:
            watch = await source_watch_service.get_watch(
                source_watch_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
            if (
                not isinstance(watch, Mapping)
                or str(watch.get("goal_id") or "") != goal_id
                or int(watch.get("goal_revision") or 0) != goal_revision
                or int(watch.get("plan_revision") or 0) != plan_revision
            ):
                raise RoutineError("routine_source_watch_stale")
        return provenance

    async def _assert_v2_routine_source_current(
        self,
        version: GuardianRoutineVersion,
        raw_provenance: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Revalidate immutable v2 source proof without applying the v1 shape.

        A v2 version has no legacy ``plan_revision`` or top-level source task
        fields.  Re-resolve its persisted source references through the native
        procedure verifier, then check the exact persisted plan/proof digests
        before deriving the current goal lineage for the package lifecycle.
        """

        template_id = raw_provenance.get("template_id")
        source_refs = raw_provenance.get("source_refs")
        if not isinstance(template_id, str) or not template_id.strip() or not isinstance(source_refs, list):
            raise RoutineError("routine_source_proof_stale")
        if not source_refs or len(source_refs) > 2 or any(not isinstance(item, Mapping) for item in source_refs):
            raise RoutineError("routine_source_proof_stale")

        try:
            source_tasks = [
                {
                    "task_id": str(item["task_id"]),
                    "expected_revision": item["task_revision"],
                }
                for item in source_refs
            ]
            request = ProcedureV2PreviewRequest.model_validate(
                {
                    "template_id": template_id,
                    "source_tasks": source_tasks,
                    "name": "routine-source-revalidation",
                    "idempotency_key": f"routine-source-revalidation:{version.id}",
                }
            )
            resolved = await self._procedure_v2()._resolve_sources(
                request,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                copy_browser_inputs=False,
            )
            plan = validate_procedure_plan(raw_provenance.get("plan") or {})
            _validated_immutable_step_inputs(
                raw_provenance.get("immutable_step_inputs"),
                resolved["spec"],
                expected_step_input_digests=_plan_step_input_digests(plan),
            )
            expected_plan_digest = plan_digest(plan)
            if str(raw_provenance.get("plan_digest") or "").lower() != expected_plan_digest:
                raise RoutineError("routine_source_proof_stale")
            if plan_digest(resolved["plan"]) != expected_plan_digest:
                raise RoutineError("routine_source_proof_stale")
            if str(resolved["spec"].template_id) != template_id:
                raise RoutineError("routine_source_proof_stale")
            if _dump(resolved["source_refs"]) != _dump(source_refs):
                raise RoutineError("routine_source_proof_stale")
            if str(raw_provenance.get("source_proof_digest") or "").lower() != _proof_digest(
                resolved["source_refs"], plan
            ):
                raise RoutineError("routine_source_proof_stale")
            expected_capability_versions = [
                step.capability_version for step in resolved["spec"].steps
            ]
            if raw_provenance.get("capability_versions") != expected_capability_versions:
                raise RoutineError("routine_source_proof_stale")
        except RoutineError:
            raise
        except Exception as exc:
            raise RoutineError("routine_source_proof_stale") from exc

        goal_id = str(resolved.get("goal_id") or "").strip()
        try:
            goal_revision = int(resolved.get("goal_revision") or 0)
        except (TypeError, ValueError, OverflowError):
            goal_revision = 0
        if not goal_id or goal_revision < 1:
            raise RoutineError("routine_source_goal_stale")
        async with db_engine.get_session() as db:
            goal = (
                await db.execute(
                    select(Goal).where(
                        Goal.id == goal_id,
                        Goal.owner_principal_id == owner_principal_id,
                        Goal.owner_session_id == owner_session_id,
                    )
                )
            ).scalars().first()
            if (
                goal is None
                or int(goal.revision or 0) != goal_revision
                or str(getattr(goal.status, "value", goal.status) or "") != "active"
            ):
                raise RoutineError("routine_source_goal_stale")

        provenance = _safe_routine_provenance(raw_provenance)
        provenance["schema_version"] = 2
        provenance["goal_id"] = goal_id
        provenance["goal_revision"] = goal_revision
        return provenance

    @staticmethod
    def _package_goal_id(provenance: Mapping[str, Any]) -> str:
        goal_id = str(provenance.get("goal_id") or "").strip()
        if not goal_id:
            raise RoutineError("routine_source_goal_stale")
        return goal_id

    def _package_status_for_materialized(
        self,
        materialized: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        lifecycle = CapabilityPackLifecycle()
        status = lifecycle.status(
            str(materialized["pack_id"]),
            owner_principal_id=owner_principal_id,
            session_id=owner_session_id,
        )
        if not isinstance(status, Mapping):
            raise RoutineError("routine_package_status_unavailable", status_code=503)
        return dict(status)

    async def package_preview(
        self,
        routine_id: str,
        version_number: int,
        *,
        expected_routine_revision: int,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if routine.revision != expected_routine_revision:
            raise RoutineError("routine_revision_stale")
        version = await self._version(routine_id, version_number)
        materialized = self._materialize_routine_package(routine_id, version, allow_create=True)
        if version.installed_package_digest and version.installed_package_digest != materialized["digest"]:
            raise RoutineError("routine_package_mutation_detected")
        status = self._package_status_for_materialized(
            materialized,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        active = status.get("active") if isinstance(status.get("active"), Mapping) else None
        reviewed = next(
            (
                item
                for item in status.get("available_versions", [])
                if isinstance(item, Mapping) and item.get("digest") == materialized["digest"]
            ),
            None,
        )
        return {
            "routine_id": routine_id,
            "version": int(version.version),
            "pack_id": materialized["pack_id"],
            "digest": materialized["digest"],
            "installed_package_digest": version.installed_package_digest,
            "review_id": active.get("review_id") if active else (reviewed.get("review_id") if reviewed else None),
            "status": active.get("status") if active else ("reviewed" if reviewed else "not_reviewed"),
            "manifest": materialized["manifest_payload"],
            "runbook": yaml.safe_load(str(materialized["runbook_content"])),
        }

    async def export_procedure(
        self,
        routine_id: str,
        version_number: int,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Export the exact installed, digest-verified declarative procedure.

        The exported record contains only the server-owned capability-pack
        manifest and fixed runbook contract. It deliberately omits invocation
        approvals, credentials, session grants, and private source bodies.
        """

        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        version = await self._version(routine_id, version_number)
        if not version.installed_package_digest:
            raise RoutineError("routine_version_not_installed")
        materialized = self._materialize_routine_package(routine_id, version, allow_create=False)
        if materialized["digest"] != version.installed_package_digest:
            raise RoutineError("routine_package_mutation_detected", status_code=409)
        runbook = yaml.safe_load(str(materialized["runbook_content"]))
        if not isinstance(runbook, Mapping):
            raise RoutineError("routine_package_runbook_invalid", status_code=409)
        return {
            "schema_version": 1,
            "kind": "seraph.reviewed_procedure.v1",
            "pack_id": str(materialized["pack_id"]),
            "version": int(version.version),
            "package_digest": str(materialized["digest"]),
            "manifest": dict(materialized["manifest_payload"]),
            "runbook": dict(runbook),
        }

    async def review_package(
        self,
        routine_id: str,
        version_number: int,
        *,
        expected_routine_revision: int,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if routine.revision != expected_routine_revision:
            raise RoutineError("routine_revision_stale")
        version = await self._version(routine_id, version_number)
        if not version.installed_package_digest:
            raise RoutineError("routine_version_not_installed")
        provenance = await self._assert_routine_source_current(
            version,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        materialized = self._materialize_routine_package(routine_id, version, allow_create=False)
        if materialized["digest"] != version.installed_package_digest:
            raise RoutineError("routine_package_mutation_detected")
        try:
            result = CapabilityPackLifecycle().review(
                materialized["manifest"],
                root_path=materialized["root"],
                goal_id=self._package_goal_id(provenance),
                reviewed_by=owner_principal_id,
                authority_expansion_approved=False,
            )
        except Exception as exc:
            raise RoutineError("routine_package_review_failed", status_code=409) from exc
        return {
            "routine_id": routine_id,
            "version": int(version.version),
            "pack_id": materialized["pack_id"],
            "digest": materialized["digest"],
            "review": result.get("review"),
            "receipt": result.get("receipt"),
        }

    async def prepare_package_approval(
        self,
        routine_id: str,
        version_number: int,
        *,
        expected_routine_revision: int,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if routine.revision != expected_routine_revision:
            raise RoutineError("routine_revision_stale")
        version = await self._version(routine_id, version_number)
        if not version.installed_package_digest:
            raise RoutineError("routine_version_not_installed")
        provenance = await self._assert_routine_source_current(
            version,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        materialized = self._materialize_routine_package(routine_id, version, allow_create=False)
        if materialized["digest"] != version.installed_package_digest:
            raise RoutineError("routine_package_mutation_detected")
        status = self._package_status_for_materialized(
            materialized,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        reviewed = next(
            (
                item
                for item in status.get("available_versions", [])
                if isinstance(item, Mapping) and item.get("digest") == materialized["digest"]
            ),
            None,
        )
        review_id = str(reviewed.get("review_id") or "") if isinstance(reviewed, Mapping) else ""
        if not review_id:
            raise RoutineError("package_review_required")
        try:
            result = CapabilityPackLifecycle().prepare_operator_approval(
                materialized["pack_id"],
                action="activate",
                goal_id=self._package_goal_id(provenance),
                digest=materialized["digest"],
                version=str(materialized["manifest"].version),
                owner_principal_id=owner_principal_id,
                session_id=owner_session_id,
                content_digest=materialized["digest"],
                authority_digest=materialized["manifest"].authority_digest,
            )
        except Exception as exc:
            raise RoutineError("routine_package_approval_prepare_failed", status_code=409) from exc
        return {
            "routine_id": routine_id,
            "version": int(version.version),
            "pack_id": materialized["pack_id"],
            "digest": materialized["digest"],
            "review_id": review_id,
            "approval": result.get("approval"),
            "receipt": result.get("receipt"),
        }

    async def decide_package_approval(
        self,
        routine_id: str,
        version_number: int,
        approval_id: str,
        req: RoutinePackageDecisionRequest,
        *,
        expected_routine_revision: int,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if routine.revision != expected_routine_revision:
            raise RoutineError("routine_revision_stale")
        version = await self._version(routine_id, version_number)
        if not version.installed_package_digest:
            raise RoutineError("routine_version_not_installed")
        provenance = await self._assert_routine_source_current(
            version,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        materialized = self._materialize_routine_package(routine_id, version, allow_create=False)
        if materialized["digest"] != version.installed_package_digest:
            raise RoutineError("routine_package_mutation_detected")
        lifecycle = CapabilityPackLifecycle()
        try:
            approval = lifecycle.get_operator_approval(
                materialized["pack_id"],
                approval_id,
                owner_principal_id=owner_principal_id,
                session_id=owner_session_id,
            )
            if (
                approval.get("digest") != materialized["digest"]
                or approval.get("goal_id") != self._package_goal_id(provenance)
                or approval.get("version") != materialized["manifest"].version
            ):
                raise RoutineError("routine_package_approval_binding_invalid")
            result = lifecycle.resolve_operator_approval(
                materialized["pack_id"],
                approval_id,
                decision=req.decision,
                owner_principal_id=owner_principal_id,
                session_id=owner_session_id,
            )
        except RoutineError:
            raise
        except Exception as exc:
            raise RoutineError("routine_package_approval_decision_failed", status_code=409) from exc
        return {
            "routine_id": routine_id,
            "version": int(version.version),
            "pack_id": materialized["pack_id"],
            "digest": materialized["digest"],
            "approval": result.get("approval"),
            "receipt": result.get("receipt"),
        }

    async def activate_package(
        self,
        routine_id: str,
        version_number: int,
        req: RoutinePackageActivationRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if routine.revision != req.expected_routine_revision:
            raise RoutineError("routine_revision_stale")
        version = await self._version(routine_id, version_number)
        if not version.installed_package_digest:
            raise RoutineError("routine_version_not_installed")
        provenance = await self._assert_routine_source_current(
            version,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        materialized = self._materialize_routine_package(routine_id, version, allow_create=False)
        if materialized["digest"] != version.installed_package_digest:
            raise RoutineError("routine_package_mutation_detected")
        status = self._package_status_for_materialized(
            materialized,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        reviewed = next(
            (
                item
                for item in status.get("available_versions", [])
                if isinstance(item, Mapping) and item.get("digest") == materialized["digest"]
            ),
            None,
        )
        review_id = str(reviewed.get("review_id") or "") if isinstance(reviewed, Mapping) else ""
        if not review_id:
            raise RoutineError("package_review_required")
        try:
            result = CapabilityPackLifecycle().activate(
                materialized["manifest"],
                root_path=materialized["root"],
                goal_id=self._package_goal_id(provenance),
                review_id=review_id,
                approval_id=req.approval_id,
                owner_principal_id=owner_principal_id,
                session_id=owner_session_id,
                content_digest=materialized["digest"],
                authority_digest=materialized["manifest"].authority_digest,
            )
        except Exception as exc:
            raise RoutineError("routine_package_activation_failed", status_code=409) from exc
        return {
            "routine_id": routine_id,
            "version": int(version.version),
            "pack_id": materialized["pack_id"],
            "digest": materialized["digest"],
            "status": result.get("status"),
            "pointer": result.get("pointer"),
            "receipt": result.get("receipt"),
        }

    @staticmethod
    def _board_job_has_unresolved_effect(job: Mapping[str, Any] | None) -> bool:
        if not isinstance(job, Mapping):
            return True
        if str(job.get("status") or "") in {
            *UNRESOLVED_EFFECT_STATUSES,
            "unknown_external_effect",
            "cost_liability",
        }:
            return True
        for effect in job.get("effects", []) or []:
            if not isinstance(effect, Mapping):
                continue
            if str(effect.get("status") or "") in {
                *UNRESOLVED_EFFECT_STATUSES,
                "unknown",
                "unknown_external_effect",
                "cost_liability",
            }:
                return True
        return False

    async def _resolve_board_journey(
        self,
        req: RoutineFromBoardPreviewRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Resolve and verify the two immutable Done board receipts."""

        source_task_id = _safe_board_id(req.source_task_id, field="source_task_id")
        action_task_id = _safe_board_id(req.action_task_id, field="action_task_id")
        if source_task_id == action_task_id:
            raise RoutineError("board_journey_tasks_must_be_distinct", status_code=422)

        async with db_engine.get_session() as db:
            rows = (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.task_id.in_([source_task_id, action_task_id]),
                        WorkBoardTask.owner_principal_id == owner_principal_id,
                        WorkBoardTask.owner_session_id == owner_session_id,
                    )
                )
            ).scalars().all()
            by_id = {str(row.task_id): row for row in rows}
            source_task = by_id.get(source_task_id)
            action_task = by_id.get(action_task_id)
            if source_task is None or action_task is None:
                raise RoutineError("board_journey_task_not_owned", status_code=404)
            if source_task.status != WorkBoardStatus.done or action_task.status != WorkBoardStatus.done:
                raise RoutineError("board_journey_tasks_not_done")
            if source_task.task_revision != int(req.expected_source_revision):
                raise RoutineError("source_task_revision_stale")
            if action_task.task_revision != int(req.expected_action_revision):
                raise RoutineError("action_task_revision_stale")
            if source_task.capability_id != "guardian.research-watch.v1":
                raise RoutineError("source_task_capability_mismatch")
            if action_task.capability_id != "work.github-followthrough.v1":
                raise RoutineError("action_task_capability_mismatch")
            if source_task.goal_id != action_task.goal_id or source_task.goal_revision != action_task.goal_revision:
                raise RoutineError("board_journey_goal_binding_mismatch")
            links = (
                await db.execute(
                    select(WorkBoardLink).where(
                        WorkBoardLink.owner_principal_id == owner_principal_id,
                        WorkBoardLink.owner_session_id == owner_session_id,
                        WorkBoardLink.parent_task_id == source_task_id,
                        WorkBoardLink.child_task_id == action_task_id,
                    )
                )
            ).scalars().all()
            if len(links) != 1:
                raise RoutineError("board_journey_link_missing_or_ambiguous")
            source_attempts = (
                await db.execute(
                    select(WorkBoardAttempt)
                    .where(
                        WorkBoardAttempt.task_id == source_task_id,
                        WorkBoardAttempt.ended_at.is_not(None),
                    )
                    .order_by(WorkBoardAttempt.ended_at.desc(), WorkBoardAttempt.attempt_id.desc())
                )
            ).scalars().all()
            action_attempts = (
                await db.execute(
                    select(WorkBoardAttempt)
                    .where(
                        WorkBoardAttempt.task_id == action_task_id,
                        WorkBoardAttempt.ended_at.is_not(None),
                    )
                    .order_by(WorkBoardAttempt.ended_at.desc(), WorkBoardAttempt.attempt_id.desc())
                )
            ).scalars().all()
            if not source_attempts or not action_attempts:
                raise RoutineError("board_journey_attempt_missing")
            source_attempt = source_attempts[0]
            action_attempt = action_attempts[0]
            source_job_id = str(source_attempt.workflow_run_id or "").strip()
            action_job_id = str(action_attempt.workflow_run_id or "").strip()
            if not source_job_id or not action_job_id:
                raise RoutineError("board_journey_workflow_link_missing")
            packets = (
                await db.execute(
                    select(GuardianDecisionPacket).where(
                        GuardianDecisionPacket.run_identity == source_job_id,
                    )
                )
            ).scalars().all()
            if len(packets) != 1:
                raise RoutineError("source_packet_missing_or_ambiguous")
            packet = packets[0]
            source_task_artifact_refs = _strict_board_evidence_refs(
                source_task.artifact_refs_json,
                kind="artifact",
            )
            action_task_artifact_refs = _strict_board_evidence_refs(
                action_task.artifact_refs_json,
                kind="artifact",
            )
            source_attempt_receipt_refs = _strict_board_evidence_refs(
                source_attempt.receipt_refs_json,
                kind="readback",
                expected_workflow_run_id=source_job_id,
            )
            action_attempt_receipt_refs = _strict_board_evidence_refs(
                action_attempt.receipt_refs_json,
                kind="readback",
                expected_workflow_run_id=action_job_id,
            )
            if not all(
                (
                    source_task_artifact_refs,
                    source_attempt_receipt_refs,
                    action_attempt_receipt_refs,
                )
            ):
                raise RoutineError("board_journey_evidence_missing")
            source_refs = {
                "source_task_id": source_task_id,
                "source_attempt_id": str(source_attempt.attempt_id),
                "source_task_revision": int(source_task.task_revision),
                "source_watch_job_id": source_job_id,
                "source_packet_id": str(packet.id),
                "action_task_id": action_task_id,
                "action_attempt_id": str(action_attempt.attempt_id),
                "action_task_revision": int(action_task.task_revision),
                "source_m3_job_id": action_job_id,
                "goal_id": str(source_task.goal_id),
                "goal_revision": int(source_task.goal_revision),
                "source_task_artifact_refs": source_task_artifact_refs,
                "action_task_artifact_refs": action_task_artifact_refs,
                "source_attempt_receipt_refs": source_attempt_receipt_refs,
                "action_attempt_receipt_refs": action_attempt_receipt_refs,
            }

        source_job = await durable_job_repository.get_job(source_refs["source_watch_job_id"])
        action_job = await durable_job_repository.get_job(source_refs["source_m3_job_id"])
        if not _verified_readback(source_job) or not _verified_readback(action_job):
            raise RoutineError("board_journey_runs_not_verified")
        source_refs["source_task_artifact_refs"] = _bind_guardian_packet_artifacts(
        source_refs["source_task_artifact_refs"],
        packet,
        source_job,
        workflow_run_id=source_refs["source_watch_job_id"],
    )
        # External follow-through tasks may have no stored workspace artifact:
        # their durable target readback is the action evidence. Preserve any
        # task artifact refs when present, while requiring and binding the
        # action attempt's readback below in every case.
        if source_refs["action_task_artifact_refs"]:
            source_refs["action_task_artifact_refs"] = _bind_board_task_artifacts(
                source_refs["action_task_artifact_refs"],
                action_job,
                workflow_run_id=source_refs["source_m3_job_id"],
            )
        source_refs["source_attempt_receipt_refs"] = _bind_board_attempt_readbacks(
            source_refs["source_attempt_receipt_refs"],
            source_job,
            workflow_run_id=source_refs["source_watch_job_id"],
        )
        source_refs["action_attempt_receipt_refs"] = _bind_board_attempt_readbacks(
            source_refs["action_attempt_receipt_refs"],
            action_job,
            workflow_run_id=source_refs["source_m3_job_id"],
        )
        if self._board_job_has_unresolved_effect(source_job) or self._board_job_has_unresolved_effect(action_job):
            raise RoutineError("board_journey_unknown_effect")
        outcome_values = {
            str(packet.memory_status or "").strip(),
            str((source_job.get("result") or {}).get("learning") if isinstance(source_job, Mapping) and isinstance(source_job.get("result"), Mapping) else "").strip(),
            str((source_job.get("result") or {}).get("memory_status") if isinstance(source_job, Mapping) and isinstance(source_job.get("result"), Mapping) else "").strip(),
            str((action_job.get("result") or {}).get("learning") if isinstance(action_job, Mapping) and isinstance(action_job.get("result"), Mapping) else "").strip(),
            str((action_job.get("result") or {}).get("memory_status") if isinstance(action_job, Mapping) and isinstance(action_job.get("result"), Mapping) else "").strip(),
        }
        if not outcome_values.intersection({"accepted", "no_learning"}):
            raise RoutineError("board_journey_learning_outcome_missing")

        source_request = RoutineFromRunRequest(
            source_watch_job_id=source_refs["source_watch_job_id"],
            source_packet_id=source_refs["source_packet_id"],
            source_m3_job_id=source_refs["source_m3_job_id"],
            name=req.name,
        )
        provenance, verified_packet = await self._source_proof(
            source_watch_job_id=source_request.source_watch_job_id,
            source_packet_id=source_request.source_packet_id,
            source_m3_job_id=source_request.source_m3_job_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if str(verified_packet.id) != source_refs["source_packet_id"]:
            raise RoutineError("source_packet_identity_changed")
        return {
            "request": source_request,
            "source_refs": source_refs,
            "provenance": provenance,
            "packet": verified_packet,
        }

    @staticmethod
    def _board_preview_digest(
        *,
        source_refs: Mapping[str, Any],
        owner_principal_id: str,
        owner_session_id: str,
        idempotency_key: str,
        name: str,
        expiry: datetime,
    ) -> str:
        return _sha(
            _dump(
                {
                    "schema_version": "seraph.work-board-routine-preview.v1",
                    "owner_principal_id": owner_principal_id,
                    "owner_session_id": owner_session_id,
                    "idempotency_key": idempotency_key,
                    "name": name.strip(),
                    "source_refs": dict(source_refs),
                    "expires_at": expiry.isoformat(),
                }
            )
        )

    @classmethod
    def _board_preview_response(
        cls,
        resolved: Mapping[str, Any],
        req: RoutineFromBoardPreviewRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        bucket: int,
    ) -> dict[str, Any]:
        expiry = _preview_expiry(bucket)
        source_refs = dict(resolved["source_refs"])
        digest = cls._board_preview_digest(
            source_refs=source_refs,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            idempotency_key=req.idempotency_key,
            name=req.name,
            expiry=expiry,
        )
        return {
            "preview_digest": digest,
            "source_refs": source_refs,
            "version_plan": {
                "version": 1,
                "steps": ["guardian_watch_run", "github_followthrough"],
                "workflow": "owner_bound_fixed_guardian_routine",
            },
            "typed_parameters": {
                "goal_id": source_refs["goal_id"],
                "goal_revision": source_refs["goal_revision"],
                "source_watch_id": resolved["provenance"].get("source_watch_id"),
                "source_watch_revision": resolved["provenance"].get("plan_revision"),
                "invocation_uuid": "fresh_uuid_per_invocation",
            },
            "permissions": {
                "capability_id": ROUTINE_CAPABILITY_VERSION,
                "external_mutation": "fresh_operator_grant_required",
                "package_review": "current_active_digest_required",
                "shell_or_arbitrary_connector": False,
            },
            "limits": {
                "runtime_seconds": ROUTINE_DEADLINE_SECONDS,
                "attempts": 1,
                "remote_inference": False,
            },
            "verifier": {
                "source": "independent_workflow_readback",
                "required": True,
                "unknown_effect": "blocked",
            },
            "expires_at": expiry.isoformat(),
            "safe_summary": f"Fixed guardian watch and reviewed GitHub follow-through for goal {source_refs['goal_id']}",
        }

    @classmethod
    def _preview_digest_matches(
        cls,
        resolved: Mapping[str, Any],
        req: RoutineFromBoardCreateRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        observed = now or _now()
        current_bucket = _preview_bucket(observed)
        for bucket in range(current_bucket, current_bucket - 16, -1):
            preview = cls._board_preview_response(
                resolved,
                req,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                bucket=bucket,
            )
            if preview["preview_digest"] == req.preview_digest.lower():
                expiry = datetime.fromisoformat(preview["expires_at"])
                if expiry <= observed:
                    raise RoutineError("routine_preview_expired")
                return preview
        raise RoutineError("routine_preview_digest_mismatch")

    @staticmethod
    def _board_binding_source_matches(
        binding: WorkBoardRoutineBinding,
        provenance: Mapping[str, Any],
        *,
        preview_digest: str,
        deterministic_routine_id: str,
    ) -> bool:
        """Check the immutable source identity before linking a recovered row.

        A pending binding is the durable handoff between the preview request
        and the existing routine lifecycle.  Recovery must therefore compare
        every source identity that was present at preview time; a routine ID
        alone is insufficient if a process stopped between the routine write
        and the binding update.
        """

        expected = {
            "preview_digest": str(preview_digest).lower(),
            "deterministic_routine_id": str(deterministic_routine_id),
            "source_task_id": str(binding.source_task_id),
            "action_task_id": str(binding.action_task_id),
        }
        for key, value in expected.items():
            if str(provenance.get(key) or "") != value:
                return False
        # The preview digest covers the complete source_refs object.  These
        # identities must still be present in the persisted version before a
        # pending binding can be reconciled after restart.
        action_artifact_refs = provenance.get("action_task_artifact_refs")
        action_artifacts_valid = not action_artifact_refs or bool(
            _safe_board_evidence_refs(action_artifact_refs)
        )
        return all(
            str(provenance.get(key) or "").strip()
            for key in (
                "source_watch_job_id",
                "source_packet_id",
                "source_m3_job_id",
                "source_attempt_id",
                "action_attempt_id",
            )
        ) and all(
            (
                _safe_board_evidence_refs(provenance.get("source_task_artifact_refs")),
                action_artifacts_valid,
                _safe_board_evidence_refs(
                    provenance.get("source_attempt_receipt_refs"),
                    expected_workflow_run_id=str(provenance.get("source_watch_job_id") or ""),
                ),
                _safe_board_evidence_refs(
                    provenance.get("action_attempt_receipt_refs"),
                    expected_workflow_run_id=str(provenance.get("source_m3_job_id") or ""),
                ),
            )
        )

    async def _board_install_job_is_bound(
        self,
        binding: WorkBoardRoutineBinding,
        routine: GuardianRoutine,
        version: GuardianRoutineVersion,
        provenance: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Verify the durable install job that belongs to a recovered routine."""

        install_job_id = str(binding.install_job_id or f"routine-install:{routine.id}:v1")
        job = await durable_job_repository.get_job(install_job_id)
        if not isinstance(job, Mapping):
            return None
        if str(job.get("job_kind") or "") != "routine_install":
            return None
        if str(job.get("job_id") or install_job_id) != install_job_id:
            return None
        owner = job.get("owner") if isinstance(job.get("owner"), Mapping) else {}
        authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        if (
            str(owner.get("principal_id") or "") != str(binding.owner_principal_id)
            or str(job.get("operator_session_id") or job.get("session_id") or authority.get("session_id") or "")
            != str(binding.owner_session_id)
        ):
            return None
        if str(authority.get("routine_id") or "") != str(routine.id):
            return None
        try:
            authority_version = int(authority.get("routine_version") or 0)
        except (TypeError, ValueError):
            return None
        if authority_version != 1:
            return None
        if str(authority.get("source_packet_id") or "") != str(provenance.get("source_packet_id") or ""):
            return None
        if str(authority.get("source_m3_job_id") or "") != str(provenance.get("source_m3_job_id") or ""):
            return None
        allowed_states = {
            "accepted",
            "queued",
            "running",
            "awaiting_approval",
            "succeeded",
            "degraded",
        }
        if str(job.get("status") or "") not in allowed_states:
            return None
        return job

    @staticmethod
    def _is_board_binding_recovery_error(code: str) -> bool:
        """Return whether a failed readback makes a binding unrecoverable.

        These failures are identity/readback failures, rather than transient
        operator input errors.  A retry must therefore leave a durable
        blocked projection instead of attempting ``from_run`` again.
        """

        return code in {
            "routine_not_found",
            "routine_version_not_found",
            "routine_binding_identity_conflict",
            "routine_binding_provenance_mismatch",
            "routine_binding_install_unknown",
        }

    async def _persist_board_binding_blocked(
        self,
        binding: WorkBoardRoutineBinding,
        *,
        reason: str,
    ) -> None:
        """Persist a failed pending/prepared readback as a safe block.

        The revision check keeps a concurrent successful reconciliation from
        being downgraded by a stale recovery request.  A row that changed
        underneath this call is left for the newer owner of the reconciliation
        decision to read back.
        """

        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(WorkBoardRoutineBinding).where(
                        WorkBoardRoutineBinding.binding_id == binding.binding_id,
                        WorkBoardRoutineBinding.owner_principal_id == binding.owner_principal_id,
                        WorkBoardRoutineBinding.owner_session_id == binding.owner_session_id,
                    )
                )
            ).scalars().first()
            if row is None:
                return
            if int(row.revision or 1) != int(binding.revision or 1):
                return
            if row.state not in {"pending", "prepared"}:
                return
            row.state = "blocked"
            row.recovery_reason = str(reason)[:256]
            row.updated_at = _now()
            row.revision = int(row.revision or 1) + 1
            await db.flush()

    async def _persist_board_binding_prepared(
        self,
        binding: WorkBoardRoutineBinding,
        prepared: Mapping[str, Any],
    ) -> None:
        """Complete the binding link after verified routine/job readback."""

        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(WorkBoardRoutineBinding).where(
                        WorkBoardRoutineBinding.binding_id == binding.binding_id,
                        WorkBoardRoutineBinding.owner_principal_id == binding.owner_principal_id,
                        WorkBoardRoutineBinding.owner_session_id == binding.owner_session_id,
                    )
                )
            ).scalars().first()
            if row is None:
                raise RoutineError("routine_binding_missing_after_recovery", status_code=500)
            if int(row.revision or 1) != int(binding.revision or 1):
                raise RoutineError("routine_binding_recovery_race")
            if row.state == "prepared":
                return
            if row.state not in {"pending", "blocked"}:
                raise RoutineError("routine_binding_recovery_required")
            row.routine_id = str(prepared["routine_id"])
            row.install_job_id = str(prepared["install_job_id"])
            row.state = "prepared"
            row.recovery_reason = None
            row.updated_at = _now()
            row.revision = int(row.revision or 1) + 1
            await db.flush()

    async def _read_board_binding(
        self,
        binding: WorkBoardRoutineBinding,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Return a safe prepared binding after restart reconciliation."""

        if (
            str(binding.owner_principal_id) != str(owner_principal_id)
            or str(binding.owner_session_id) != str(owner_session_id)
        ):
            raise RoutineError("routine_binding_owner_mismatch", status_code=403)
        routine_id = str(binding.routine_id or binding.deterministic_routine_id or "")
        if routine_id != str(binding.deterministic_routine_id):
            raise RoutineError("routine_binding_identity_conflict")
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        version = await self._version(routine_id, 1)
        provenance = _load(version.source_provenance_json, {})
        if not isinstance(provenance, Mapping) or not self._board_binding_source_matches(
            binding,
            provenance,
            preview_digest=binding.preview_digest,
            deterministic_routine_id=binding.deterministic_routine_id,
        ):
            raise RoutineError("routine_binding_provenance_mismatch")
        install_job = await self._board_install_job_is_bound(binding, routine, version, provenance)
        if install_job is None:
            raise RoutineError("routine_binding_install_unknown")
        install_authority = install_job.get("declared_authority")
        approval_id = None
        if isinstance(install_authority, Mapping):
            candidate_approval_id = str(install_authority.get("approval_id") or "").strip()
            if candidate_approval_id:
                approval_id = candidate_approval_id
        return {
            "routine_id": routine.id,
            "state": "prepared",
            "status": "prepared",
            "revision": int(binding.revision or 1),
            "version": 1,
            "install_job_id": str(binding.install_job_id or f"routine-install:{routine.id}:v1"),
            "approval_id": approval_id,
            "preview_digest": binding.preview_digest,
            "binding_id": binding.binding_id,
        }

    async def preview_from_board(
        self,
        req: RoutineFromBoardPreviewRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Build a non-persistent procedure proposal from two verified tasks."""

        if not req.name.strip() or not req.idempotency_key.strip():
            raise RoutineError("routine_preview_input_invalid", status_code=422)
        resolved = await self._resolve_board_journey(
            req,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        return self._board_preview_response(
            resolved,
            req,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            bucket=_preview_bucket(),
        )

    async def create_from_board(
        self,
        req: RoutineFromBoardCreateRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Persist one deterministic binding, then enter the existing routine lifecycle.

        The binding is committed before ``from_run`` is called.  If the process
        dies after the routine transaction commits, a retry can link the exact
        deterministic routine and install job. A blocked retry may call
        ``from_run`` again, but that method verifies and reuses only the same
        deterministic routine/version/job binding.
        """

        preview_digest = req.preview_digest.lower()
        existing: WorkBoardRoutineBinding | None = None
        async with db_engine.get_session() as db:
            existing = (
                await db.execute(
                    select(WorkBoardRoutineBinding).where(
                        WorkBoardRoutineBinding.owner_principal_id == owner_principal_id,
                        WorkBoardRoutineBinding.owner_session_id == owner_session_id,
                        WorkBoardRoutineBinding.idempotency_key == req.idempotency_key,
                    )
                )
            ).scalars().first()
            if existing is not None:
                db.expunge(existing)
        if existing is not None:
            if str(existing.preview_digest).lower() != preview_digest:
                raise RoutineError("routine_preview_conflict")
            # The idempotency row is a replay handle, not a source of truth.
            # Continue through the live resolver below before returning a
            # prepared receipt so task ownership, revisions, evidence,
            # packet identity, and request name are revalidated after a
            # restart.  Name and task identity are available on the binding;
            # reject an altered request before resolver errors can obscure the
            # typed idempotency conflict.
            if (
                str(existing.source_task_id or "") != str(req.source_task_id)
                or str(existing.action_task_id or "") != str(req.action_task_id)
                or str(existing.routine_name or "").strip() != req.name.strip()
            ):
                raise RoutineError("routine_preview_conflict")

        try:
            resolved = await self._resolve_board_journey(
                RoutineFromBoardPreviewRequest.model_validate(req.model_dump(exclude={"preview_digest"})),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        except RoutineError as exc:
            # A caller that reuses an idempotency key with a changed revision
            # or source request must receive a typed conflict.  The resolver
            # has still run, so a live source/evidence change cannot silently
            # return the old prepared receipt.
            if existing is not None and exc.code in {
                "source_task_revision_stale",
                "action_task_revision_stale",
                "routine_preview_digest_mismatch",
            }:
                raise RoutineError("routine_preview_conflict") from exc
            raise
        try:
            preview = self._preview_digest_matches(
                resolved,
                req,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        except RoutineError as exc:
            if existing is not None and exc.code in {
                "routine_preview_digest_mismatch",
                "routine_preview_expired",
            }:
                raise RoutineError("routine_preview_conflict") from exc
            raise
        source_refs = dict(resolved["source_refs"])
        if existing is not None:
            # A pending/blocked row is never reused with a new source proof.
            # Its digest and source task identities are immutable; a changed
            # preview must be a new operator idempotency key.
            if str(existing.preview_digest).lower() != str(preview["preview_digest"]).lower():
                raise RoutineError("routine_preview_conflict")
            deterministic_id = str(existing.deterministic_routine_id)
            provenance_extra = {
                "preview_digest": preview["preview_digest"],
                "deterministic_routine_id": deterministic_id,
                **source_refs,
            }
            if existing.state == "blocked":
                try:
                    prepared = await self._read_board_binding(
                        existing,
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                    )
                except RoutineError as read_error:
                    if not self._is_board_binding_recovery_error(read_error.code):
                        raise
                    try:
                        # from_run verifies any deterministic rows left by the
                        # interrupted first attempt before it reuses them. Its
                        # durable install binding is idempotent by routine ID.
                        await self.from_run(
                            resolved["request"],
                            owner_principal_id=owner_principal_id,
                            owner_session_id=owner_session_id,
                            routine_id=deterministic_id,
                            provenance_extra=provenance_extra,
                        )
                        prepared = await self._read_board_binding(
                            existing,
                            owner_principal_id=owner_principal_id,
                            owner_session_id=owner_session_id,
                        )
                    except Exception as recovery_error:
                        raise RoutineError("routine_binding_recovery_required") from recovery_error
                await self._persist_board_binding_prepared(existing, prepared)
                async with db_engine.get_session() as db:
                    recovered_binding = (
                        await db.execute(
                            select(WorkBoardRoutineBinding).where(
                                WorkBoardRoutineBinding.binding_id == existing.binding_id,
                                WorkBoardRoutineBinding.owner_principal_id == owner_principal_id,
                                WorkBoardRoutineBinding.owner_session_id == owner_session_id,
                            )
                        )
                    ).scalars().first()
                    if recovered_binding is None:
                        raise RoutineError("routine_binding_missing_after_recovery", status_code=500)
                    db.expunge(recovered_binding)
                return await self._read_board_binding(
                    recovered_binding,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
            # A restart may have committed the routine but not the binding
            # update.  Reconcile it by identity and return the same result.
            try:
                prepared = await self._read_board_binding(
                    existing,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
            except RoutineError as exc:
                if not self._is_board_binding_recovery_error(exc.code):
                    raise
                await self._persist_board_binding_blocked(existing, reason=exc.code)
                raise RoutineError("routine_binding_recovery_required") from exc
            await self._persist_board_binding_prepared(existing, prepared)
            return prepared
        binding_id = uuid.uuid4().hex
        deterministic_id = uuid.uuid5(BOARD_ROUTINE_NAMESPACE, binding_id).hex
        binding = WorkBoardRoutineBinding(
            binding_id=binding_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            idempotency_key=req.idempotency_key,
            preview_digest=str(preview["preview_digest"]),
            source_task_id=str(source_refs["source_task_id"]),
            action_task_id=str(source_refs["action_task_id"]),
            routine_name=req.name.strip(),
            deterministic_routine_id=deterministic_id,
            state="pending",
            revision=1,
        )
        try:
            async with db_engine.get_session() as db:
                db.add(binding)
                await db.flush()
                db.expunge(binding)
        except IntegrityError as exc:
            # A concurrent creator owns the canonical idempotency row.  Read
            # it back and return only if its digest is the same.
            async with db_engine.get_session() as db:
                winner = (
                    await db.execute(
                        select(WorkBoardRoutineBinding).where(
                            WorkBoardRoutineBinding.owner_principal_id == owner_principal_id,
                            WorkBoardRoutineBinding.owner_session_id == owner_session_id,
                            WorkBoardRoutineBinding.idempotency_key == req.idempotency_key,
                        )
                    )
                ).scalars().first()
                if winner is None:
                    raise RoutineError("routine_binding_persist_failed") from exc
                db.expunge(winner)
            if str(winner.preview_digest).lower() != preview_digest:
                raise RoutineError("routine_preview_conflict") from exc
            if (
                str(winner.source_task_id or "") != str(req.source_task_id)
                or str(winner.action_task_id or "") != str(req.action_task_id)
                or str(winner.routine_name or "").strip() != req.name.strip()
            ):
                raise RoutineError("routine_preview_conflict") from exc
            if winner.state == "prepared" and winner.routine_id:
                return await self._read_board_binding(
                    winner,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
            raise RoutineError("routine_binding_recovery_required") from exc

        provenance_extra = {
            "preview_digest": preview["preview_digest"],
            "deterministic_routine_id": deterministic_id,
            **source_refs,
        }
        try:
            created = await self.from_run(
                resolved["request"],
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                routine_id=deterministic_id,
                provenance_extra=provenance_extra,
            )
        except Exception as exc:
            async with db_engine.get_session() as db:
                row = (
                    await db.execute(
                        select(WorkBoardRoutineBinding).where(
                            WorkBoardRoutineBinding.binding_id == binding_id,
                            WorkBoardRoutineBinding.owner_principal_id == owner_principal_id,
                            WorkBoardRoutineBinding.owner_session_id == owner_session_id,
                        )
                    )
                ).scalars().first()
                if row is not None:
                    row.state = "blocked"
                    row.recovery_reason = "routine_creation_failed"
                    row.updated_at = _now()
                    row.revision = int(row.revision or 1) + 1
            raise
        install_job_id = str(created.get("install_job_id") or f"routine-install:{deterministic_id}:v1")
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(WorkBoardRoutineBinding).where(
                        WorkBoardRoutineBinding.binding_id == binding_id,
                        WorkBoardRoutineBinding.owner_principal_id == owner_principal_id,
                        WorkBoardRoutineBinding.owner_session_id == owner_session_id,
                    )
                )
            ).scalars().first()
            if row is None:
                raise RoutineError("routine_binding_missing_after_create", status_code=500)
            row.routine_id = deterministic_id
            row.install_job_id = install_job_id
            row.state = "prepared"
            row.recovery_reason = None
            row.updated_at = _now()
            row.revision = int(row.revision or 1) + 1
            await db.flush()
            db.expunge(row)
        # Read back from the canonical rows, validating the durable install
        # identity before exposing a prepared state to the operator.
        return await self._read_board_binding(
            row,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )

    async def _replay_existing_board_invocation(
        self,
        *,
        routine_id: str,
        req: RoutineInvokeRequest,
        invocation_uuid: str,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any] | None:
        """Return an existing safe board receipt before current authority checks.

        A lost HTTP response must remain recoverable even if the routine, goal,
        or source watch has since become stale.  The owner/session-scoped task
        row and its immutable typed-input artifact are the only authority for
        this recovery path.  A new request still follows the normal authority
        checks in ``invoke``.
        """

        envelope, _encoded, typed_digest, relative_path, typed_reference = _board_invocation_input_descriptor(
            routine_id=routine_id,
            req=req,
            invocation_uuid=invocation_uuid,
        )
        async with db_engine.get_session() as db:
            existing = (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == owner_principal_id,
                        WorkBoardTask.owner_session_id == owner_session_id,
                        WorkBoardTask.idempotency_scope == "guardian-routine-invocation",
                        WorkBoardTask.idempotency_key == invocation_uuid,
                    )
                )
            ).scalars().first()
            if existing is None:
                return None
            try:
                exact_binding = (
                    existing.capability_id == ROUTINE_CAPABILITY_VERSION
                    and existing.goal_id == req.goal_id
                    and int(existing.goal_revision) == int(req.expected_goal_revision)
                    and existing.typed_input_digest == typed_digest
                    and existing.typed_input_ref == typed_reference
                )
            except (TypeError, ValueError):
                exact_binding = False
            if not exact_binding:
                raise RoutineError("routine_invocation_idempotency_conflict")
            existing_task_id = existing.task_id
            existing_task_revision = int(existing.task_revision)

        try:
            _read_board_invocation_input(
                relative_path=relative_path,
                expected_digest=typed_digest,
                expected_envelope=envelope,
            )
        except RoutineError as exc:
            if exc.code == "routine_invocation_input_unavailable":
                raise
            raise RoutineError("routine_invocation_idempotency_conflict") from exc
        return {
            "status": "queued",
            "task_id": existing_task_id,
            "task_revision": existing_task_revision,
            "deduped": True,
            "preview": {
                "routine_id": routine_id,
                "routine_revision": req.expected_routine_revision,
                "version": req.version,
                "goal_id": req.goal_id,
                "goal_revision": req.expected_goal_revision,
                "source_watch_id": req.source_watch_id,
                "source_watch_revision": req.expected_watch_revision,
                "steps": ["guardian_watch_run", "github_followthrough"],
            },
        }

    async def _create_board_invocation_task(
        self,
        *,
        routine: GuardianRoutine,
        version: GuardianRoutineVersion,
        req: RoutineInvokeRequest,
        owner_principal_id: str,
        owner_session_id: str,
        provenance: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create the one canonical Todo task for a direct routine invocation.

        The dispatcher owns durable admission and execution.  This route only
        writes a typed, immutable board intent; a later M2 pass claims it and
        calls ``invoke`` again with ``work_board_task_id`` set.
        """

        invocation_uuid = _safe_invocation_uuid(req.invocation_uuid)
        envelope, encoded, typed_digest, relative_path, typed_reference = _board_invocation_input_descriptor(
            routine_id=routine.id,
            req=req,
            invocation_uuid=invocation_uuid,
        )
        # The UUID is the idempotency key, while the digest is the immutable
        # content identity.  Including both prevents two racing requests that
        # reuse a UUID with different inputs from overwriting the winning
        # task's typed-input artifact before the unique board insert settles.
        owner = WorkBoardOwner(
            principal_id=owner_principal_id,
            session_id=owner_session_id,
        )
        repository = WorkBoardRepository()

        async with db_engine.get_session() as db:
            existing = (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == owner_principal_id,
                        WorkBoardTask.owner_session_id == owner_session_id,
                        WorkBoardTask.idempotency_scope == "guardian-routine-invocation",
                        WorkBoardTask.idempotency_key == invocation_uuid,
                    )
                )
            ).scalars().first()
            if existing is not None:
                if (
                    existing.capability_id != ROUTINE_CAPABILITY_VERSION
                    or existing.goal_id != req.goal_id
                    or int(existing.goal_revision) != int(req.expected_goal_revision)
                    or existing.typed_input_digest != typed_digest
                    or existing.typed_input_ref != typed_reference
                ):
                    raise RoutineError("routine_invocation_idempotency_conflict")
                _read_board_invocation_input(
                    relative_path=relative_path,
                    expected_digest=typed_digest,
                    expected_envelope=envelope,
                )
                return {
                    "status": "queued",
                    "task_id": existing.task_id,
                    "task_revision": int(existing.task_revision),
                    "deduped": True,
                    "preview": {
                        "routine_id": routine.id,
                        "routine_revision": routine.revision,
                        "version": req.version,
                        "goal_id": req.goal_id,
                        "goal_revision": req.expected_goal_revision,
                        "source_watch_id": req.source_watch_id,
                        "source_watch_revision": req.expected_watch_revision,
                        "package_digest": version.installed_package_digest,
                        "steps": ["guardian_watch_run", "github_followthrough"],
                    },
                }

            # The generated path is inside the canonical workspace and the
            # descriptor-relative writer rejects symlink components.  It is
            # written before the task row so a task never advertises a typed
            # input that has not been durably materialized.
            try:
                _write_workspace_text_bounded(
                    _safe_resolve(relative_path),
                    encoded,
                    max_bytes=64 * 1024,
                    create_parents=True,
                )
            except (OSError, ValueError) as exc:
                raise RoutineError("routine_invocation_input_unavailable", str(exc), status_code=503) from exc
            _read_board_invocation_input(
                relative_path=relative_path,
                expected_digest=typed_digest,
                expected_envelope=envelope,
            )
            source_provenance_refs = {
                key: _safe_board_id(value, field=key)
                for key, value in {
                    "source_task_id": provenance.get("source_task_id"),
                    "source_attempt_id": provenance.get("source_attempt_id"),
                    "action_task_id": provenance.get("action_task_id"),
                    "action_attempt_id": provenance.get("action_attempt_id"),
                    "source_watch_job_id": provenance.get("source_watch_job_id"),
                    "source_packet_id": provenance.get("source_packet_id"),
                    "source_m3_job_id": provenance.get("source_m3_job_id"),
                }.items()
                if str(value or "").strip()
            }
            provenance_body = "; ".join(
                f"{key}={value}" for key, value in sorted(source_provenance_refs.items())
            )
            request = WorkBoardTaskCreate(
                title=f"Run routine: {routine.name}",
                body=(
                    "Operator-approved reusable procedure invocation awaiting governed dispatch. "
                    f"Source journey references: {provenance_body}"
                ),
                goal_id=req.goal_id,
                goal_revision=req.expected_goal_revision,
                status=WorkBoardStatus.todo,
                capability_id=ROUTINE_CAPABILITY_VERSION,
                typed_input_ref=typed_reference,
                typed_input_digest=typed_digest,
                priority=50,
                idempotency_scope="guardian-routine-invocation",
                idempotency_key=invocation_uuid,
            )
            try:
                mutation = await repository.create_task(
                    db,
                    owner,
                    request,
                    origin_session_id=owner_session_id,
                )
            except BoardError as exc:
                raise RoutineError(exc.code, exc.message, status_code=exc.status_code) from exc
            task = mutation.task
            return {
                "status": "queued",
                "task_id": task.task_id,
                "task_revision": int(task.task_revision),
                "deduped": bool(mutation.idempotent_replay),
                "preview": {
                    "routine_id": routine.id,
                    "routine_revision": routine.revision,
                    "version": req.version,
                    "goal_id": req.goal_id,
                    "goal_revision": req.expected_goal_revision,
                    "source_watch_id": req.source_watch_id,
                    "source_watch_revision": req.expected_watch_revision,
                    "package_digest": version.installed_package_digest,
                    "steps": ["guardian_watch_run", "github_followthrough"],
                    "source_provenance": _safe_routine_provenance(provenance),
                },
            }

    async def _source_proof(
        self,
        *,
        source_watch_job_id: str,
        source_packet_id: str,
        source_m3_job_id: str,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[dict[str, Any], GuardianDecisionPacket]:
        source_watch = await durable_job_repository.get_job(source_watch_job_id)
        m3_job = await durable_job_repository.get_job(source_m3_job_id)
        if not isinstance(source_watch, Mapping) or str(source_watch.get("job_kind") or "") != "guardian_source_watch":
            raise RoutineError("source_watch_job_kind_mismatch")
        if not isinstance(m3_job, Mapping) or str(m3_job.get("job_kind") or "") != GITHUB_FOLLOWTHROUGH_JOB_KIND:
            raise RoutineError("source_m3_job_kind_mismatch")
        if not _verified_readback(source_watch) or not _verified_readback(m3_job):
            raise RoutineError("source_runs_not_verified")
        # Both source jobs are part of the proof.  Accepting one owner match
        # would let a caller combine another operator's observation with its
        # own publication receipt.
        if not _owner_matches(source_watch, owner_principal_id, owner_session_id) or not _owner_matches(m3_job, owner_principal_id, owner_session_id):
            raise RoutineError("routine_owner_mismatch", status_code=404)
        async with db_engine.get_session() as db:
            packet = (
                await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == source_packet_id))
            ).scalars().first()
            if packet is None:
                raise RoutineError("source_packet_not_found", status_code=404)
            db.expunge(packet)
        if packet.run_identity != source_watch_job_id or packet.status != "succeeded" or packet.verification_status != "passed":
            raise RoutineError("source_packet_not_verified")
        watch = await source_watch_service.get_watch(
            packet.source_watch_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if (
            watch is None
            or watch.get("goal_id") != packet.goal_id
            or str(watch.get("owner_principal_id") or "") != str(owner_principal_id)
            or str(watch.get("owner_session_id") or "") != str(owner_session_id)
        ):
            raise RoutineError("source_watch_not_owned", status_code=404)
        if int(watch.get("goal_revision", 0)) != int(packet.goal_revision) or int(watch.get("plan_revision", 0)) != int(packet.plan_revision):
            raise RoutineError("source_watch_revision_stale")
        dossier_sha = str(packet.dossier_sha256 or _sha(packet.proposal_text))
        if str(packet.dossier_artifact_id or "").strip() == "":
            raise RoutineError("source_dossier_artifact_missing")
        dossier_path = str(packet.dossier_path or "").strip()
        if not dossier_path:
            raise RoutineError("source_dossier_artifact_missing")
        try:
            resolved_dossier = _safe_resolve(dossier_path)
            dossier_text, truncated = _read_workspace_text_bounded(resolved_dossier, max_bytes=72 * 1024)
        except (OSError, ValueError) as exc:
            raise RoutineError("source_dossier_artifact_unreadable") from exc
        if truncated or _sha(dossier_text) != dossier_sha:
            raise RoutineError("source_dossier_digest_mismatch")
        # The prepared M3 artifact is the immutable input authority. Durable
        # result projections intentionally redact the full publication body,
        # so routine proof must read the exact prepared payload and bind every
        # source identity before accepting a reusable destination.
        try:
            prepared = await GitHubFollowthroughService()._read_prepared(m3_job)
        except (GitHubFollowthroughError, OSError, ValueError, TypeError) as exc:
            raise RoutineError("source_m3_input_binding_missing") from exc
        if (
            str(prepared.job_id) != str(source_m3_job_id)
            or str(prepared.owner_principal_id) != str(owner_principal_id)
            or str(prepared.owner_session_id) != str(owner_session_id)
            or str(prepared.dossier_artifact_id) != str(packet.dossier_artifact_id)
            or str(prepared.dossier_sha256) != dossier_sha
            or str(prepared.source_watch_id) != str(packet.source_watch_id)
            or str(prepared.goal_id) != str(packet.goal_id)
            or not _same_revision(prepared.goal_revision, packet.goal_revision)
            or not _same_revision(prepared.plan_revision, packet.plan_revision)
        ):
            raise RoutineError("source_m3_dossier_binding_missing")
        m3_authority = (
            m3_job.get("declared_authority")
            if isinstance(m3_job.get("declared_authority"), Mapping)
            else {}
        )
        routine_binding = m3_authority.get("routine_binding")
        expected_m3_inputs = {
            "operation_id": str(prepared.operation_id),
            "repository": prepared.repository,
            "connection_id": prepared.connection_id,
            "connection_revision": int(prepared.connection_revision),
            "action": prepared.action,
            "issue_number": prepared.issue_number,
            "title_sha256": _sha(prepared.title or ""),
            "body_sha256": _sha(prepared.body),
            "dossier_artifact_id": prepared.dossier_artifact_id,
            "dossier_sha256": prepared.dossier_sha256,
            "source_watch_id": prepared.source_watch_id,
            "goal_id": prepared.goal_id,
            "goal_revision": int(prepared.goal_revision),
            "plan_revision": int(prepared.plan_revision),
        }
        if routine_binding is not None:
            expected_binding_fields = {
                "routine_id",
                "routine_revision",
                "routine_version",
                "package_digest",
                "parent_invocation_job_id",
                "publication_child_job_id",
                "invocation_uuid",
                "owner_principal_id",
                "owner_session_id",
                "goal_id",
                "goal_revision",
                "source_watch_id",
                "connection_id",
                "connection_revision",
                "repository",
                "action",
                "operation_uuid",
            }
            if not isinstance(routine_binding, Mapping) or set(routine_binding) != expected_binding_fields:
                raise RoutineError("source_m3_routine_binding_invalid")
            try:
                binding_goal_revision = int(routine_binding["goal_revision"])
                binding_connection_revision = int(routine_binding["connection_revision"])
                binding_operation_id = _operation_id(
                    owner_principal_id,
                    uuid.UUID(str(routine_binding["operation_uuid"])),
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise RoutineError("source_m3_routine_binding_invalid") from exc
            if (
                str(routine_binding.get("owner_principal_id") or "") != str(owner_principal_id)
                or str(routine_binding.get("owner_session_id") or "") != str(owner_session_id)
                or str(routine_binding.get("goal_id") or "") != str(prepared.goal_id)
                or binding_goal_revision != int(prepared.goal_revision)
                or str(routine_binding.get("source_watch_id") or "") != str(prepared.source_watch_id)
                or str(routine_binding.get("connection_id") or "") != str(prepared.connection_id)
                or binding_connection_revision != int(prepared.connection_revision)
                or str(routine_binding.get("repository") or "") != str(prepared.repository)
                or str(routine_binding.get("action") or "") != str(prepared.action)
                or str(binding_operation_id) != str(prepared.operation_id)
            ):
                raise RoutineError("source_m3_routine_binding_mismatch")
            expected_m3_inputs["routine_binding"] = dict(routine_binding)
        handoff_digest = str(m3_authority.get("parent_handoff_digest") or "").strip()
        if handoff_digest:
            if len(handoff_digest) != 64 or any(ch not in "0123456789abcdef" for ch in handoff_digest):
                raise RoutineError("source_m3_handoff_digest_invalid")
            expected_m3_inputs["parent_handoff_digest"] = handoff_digest
        expected_m3_input_digest = _sha(_dump(expected_m3_inputs))
        if str(m3_job.get("input_digest") or "") != expected_m3_input_digest:
            raise RoutineError("source_m3_input_digest_mismatch")
        if (
            str(m3_authority.get("source_watch_id") or "") != str(packet.source_watch_id)
            or str(m3_authority.get("dossier_artifact_id") or "") != str(packet.dossier_artifact_id)
            or str(m3_authority.get("dossier_sha256") or "") != dossier_sha
        ):
            raise RoutineError("source_m3_authority_binding_missing")
        fixed_repository = str(prepared.repository).strip()
        fixed_action = str(prepared.action).strip()
        # A create_issue operation has no pre-existing issue number. Keep a
        # typed target marker so later invocations cannot retarget the action.
        fixed_target = str(
            prepared.issue_number if prepared.issue_number is not None else ("create_issue" if fixed_action == "create_issue" else "")
        ).strip()
        if not fixed_repository or fixed_action not in {"create_issue", "create_comment"} or not fixed_target:
            raise RoutineError("source_m3_destination_binding_missing")
        provenance = {
            "source_watch_job_id": source_watch_job_id,
            "source_packet_id": source_packet_id,
            "source_m3_job_id": source_m3_job_id,
            "source_watch_id": packet.source_watch_id,
            "goal_id": packet.goal_id,
            "goal_revision": packet.goal_revision,
            "plan_revision": packet.plan_revision,
            "dossier_artifact_id": packet.dossier_artifact_id,
            "dossier_sha256": dossier_sha,
            "m3_result_digest": m3_job.get("result", {}).get("digest") if isinstance(m3_job.get("result"), Mapping) else None,
            "source_repository": fixed_repository,
            "source_action": fixed_action,
            "source_target": fixed_target,
        }
        return provenance, packet

    async def _admit_user_job(
        self,
        *,
        job_id: str,
        job_kind: str,
        idempotency_key: str,
        inputs: Mapping[str, Any],
        authority: Mapping[str, Any],
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str,
        goal_revision: int,
        plan_revision: int | None,
        candidate_id: str | None,
        work_board_idempotency_key: str | None = None,
        runtime_seconds: int = ROUTINE_DEADLINE_SECONDS,
        capability_version: str = ROUTINE_CAPABILITY_VERSION,
    ) -> dict[str, Any]:
        runtime_seconds = _bounded_runtime_seconds(runtime_seconds)
        admitted = await durable_job_repository.admit_job(
            DurableJobSpec(
                identity=DurableJobIdentity(
                    job_id=job_id,
                    owner_kind="user",
                    owner_principal_id=owner_principal_id,
                    job_kind=job_kind,
                    capability_version=capability_version,
                    idempotency_scope=("work-board-attempt" if work_board_idempotency_key else job_kind),
                    idempotency_key=(work_board_idempotency_key or idempotency_key),
                ),
                inputs=dict(inputs),
                session_id=owner_session_id,
                operator_session_id=owner_session_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                plan_revision=plan_revision,
                candidate_id=candidate_id,
                priority=50,
                declared_authority=dict(authority),
                deadline_at=_now() + timedelta(seconds=runtime_seconds),
                # Routine parents acquire one claim for the initial approval,
                # one for the verified watch step, one for publication
                # preparation, and one for the separately approved
                # publication/readback step. Install jobs need only the
                # approval claim and one installation claim. These are
                # explicit guarded phases of one routine invocation, not
                # automatic retries.
                max_attempts=(4 if job_kind == "routine_invocation" else 2),
                service_id=None,
                budget_microusd=0,
            )
        )
        if admitted.get("status") == "accepted":
            admitted = await durable_job_repository.queue_job(
                job_id,
                expected_revision=admitted.get("revision"),
                expected_fencing_token=int(admitted.get("fencing_token") or 0),
            )
        if admitted.get("status") == "queued":
            admitted = await durable_job_repository.claim_job(
                job_id,
                owner=f"routine:{job_id}",
                expected_revision=admitted.get("revision"),
                expected_fencing_token=(admitted.get("lease") or {}).get(
                    "fencing_token", admitted.get("fencing_token")
                ),
                lease_seconds=runtime_seconds,
            )
        return admitted

    async def _admit_child_job(
        self,
        parent: Mapping[str, Any],
        *,
        child_id: str,
        step_id: str,
        inputs: Mapping[str, Any],
        authority: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Admit one fixed child under the currently leased routine parent.

        ``parent_fencing_token`` is part of the canonical durable runtime
        contract.  Every child state write is therefore rejected once the
        parent lease is lost or the parent is paused/recovered by another
        worker.  The helper has no generic tool or argument dispatch surface.
        """

        parent_id = str(parent.get("job_id") or "")
        parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
        parent_fence = int(parent_lease.get("fencing_token") or 0)
        parent_owner = parent.get("owner") if isinstance(parent.get("owner"), Mapping) else {}
        owner_principal_id = str(parent_owner.get("principal_id") or "")
        session_id = str(parent.get("session_id") or parent.get("operator_session_id") or "")
        if not parent_id or parent.get("status") != "running" or parent_fence <= 0:
            raise RoutineError("routine_parent_lease_required")
        if not owner_principal_id or not session_id:
            raise RoutineError("routine_parent_owner_missing")
        parent_authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        routine_id = str(authority.get("routine_id") or parent_authority.get("routine_id") or "")
        try:
            routine_revision = int(authority.get("routine_revision") or parent_authority.get("routine_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise RoutineError("routine_revision_binding_invalid") from exc
        if not routine_id or routine_revision <= 0:
            raise RoutineError("routine_child_routine_binding_missing")
        # This read is deliberately inside the admission helper, immediately
        # before the durable child CAS.  pause/revoke flips the routine row
        # first, so a post-CAS child cannot appear after cancellation begins.
        await self._require_active_routine(
            routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=session_id,
            expected_revision=routine_revision,
        )
        child_authority = {
            **dict(authority),
            "routine_id": routine_id,
            "routine_revision": routine_revision,
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": session_id,
            "parent_job_id": parent_id,
            "parent_fencing_token": parent_fence,
            "step_id": step_id,
            "routine_invocation_job_id": parent_id,
            "capability_id": ROUTINE_CAPABILITY_VERSION,
            "budget_microusd": 0,
        }
        existing = await durable_job_repository.get_job(child_id)
        child_lease_seconds = _remaining_runtime_seconds(parent)
        if existing is not None:
            if (
                existing.get("parent_job_id") != parent_id
                or str(existing.get("job_kind") or "") != f"routine_{step_id}_child"
            ):
                raise RoutineError("routine_child_binding_conflict")
            if int(existing.get("parent_fencing_token") or 0) != parent_fence:
                # A blocked/terminal child is an immutable historical receipt
                # after parent recovery acquires a new fence.  It may be read
                # for idempotent recovery, but it must never be written under
                # the new parent lease.  A live child with an old fence is a
                # hard conflict and cannot be resumed.
                if existing.get("status") in {
                    "blocked",
                    "awaiting_approval",
                    "succeeded",
                    "degraded",
                    "cancelled",
                    "unknown_external_effect",
                    "cost_liability",
                    "failed",
                }:
                    return existing
                raise RoutineError("routine_child_parent_fence_stale")
            if existing.get("status") == "accepted":
                existing = await durable_job_repository.queue_job(child_id, expected_revision=existing.get("revision"))
            if existing.get("status") == "queued":
                existing = await durable_job_repository.claim_job(
                    child_id,
                    owner=f"routine-child:{child_id}",
                    expected_revision=existing.get("revision"),
                    expected_fencing_token=(existing.get("lease") or {}).get("fencing_token"),
                    lease_seconds=child_lease_seconds,
                )
            return existing
        parent_deadline = parent.get("deadline_at")
        admitted = await durable_job_repository.admit_job(
            DurableJobSpec(
                identity=DurableJobIdentity(
                    job_id=child_id,
                    owner_kind="user",
                    owner_principal_id=owner_principal_id,
                    job_kind=f"routine_{step_id}_child",
                    capability_version=ROUTINE_CAPABILITY_VERSION,
                    idempotency_scope="guardian-routine-child",
                    idempotency_key=f"{parent_id}:{step_id}",
                ),
                inputs=dict(inputs),
                session_id=session_id,
                operator_session_id=session_id,
                parent_job_id=parent_id,
                parent_fencing_token=parent_fence,
                goal_id=parent.get("goal_id"),
                goal_revision=parent.get("goal_revision"),
                plan_revision=parent.get("plan_revision"),
                priority=int(parent.get("priority") or 50),
                declared_authority=child_authority,
                deadline_at=parent_deadline or (_now() + timedelta(seconds=child_lease_seconds)),
                max_attempts=1,
                budget_microusd=0,
                run_fingerprint=_sha(_dump({"parent": parent_id, "step": step_id, "inputs": dict(inputs)})),
            )
        )
        if admitted.get("status") == "accepted":
            queued = await durable_job_repository.queue_job(child_id, expected_revision=admitted.get("revision"))
            admitted = await durable_job_repository.claim_job(
                child_id,
                owner=f"routine-child:{child_id}",
                expected_revision=queued.get("revision"),
                expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"),
                lease_seconds=child_lease_seconds,
            )
        try:
            await self._require_active_routine(
                routine_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=session_id,
                expected_revision=routine_revision,
            )
        except RoutineError:
            current_child = await durable_job_repository.get_job(child_id) or admitted
            child_lease = current_child.get("lease") if isinstance(current_child.get("lease"), Mapping) else {}
            if current_child.get("status") in {"running", "queued", "accepted"}:
                try:
                    await durable_job_repository.cancel_job(
                        child_id,
                        owner=str(child_lease.get("owner") or "") or None,
                        fencing_token=int(child_lease.get("fencing_token") or 0) or None,
                        expected_revision=current_child.get("revision"),
                        reason="routine_state_changed_before_child_start",
                    )
                except Exception:
                    pass
            raise
        return admitted

    async def _record_child_checkpoint(
        self,
        child: Mapping[str, Any],
        *,
        checkpoint_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        return await durable_job_repository.record_checkpoint(
            str(child["job_id"]),
            checkpoint_id=checkpoint_id,
            state={"step_id": (child.get("declared_authority") or {}).get("step_id"), **dict(payload)},
            checkpoint_payload=dict(payload),
            safe=True,
            owner=str(lease.get("owner") or ""),
            fencing_token=int(lease.get("fencing_token") or 0),
            expected_revision=int(child.get("revision") or 0),
        )

    async def _persist_adopted_publication_checkpoints(
        self,
        parent: Mapping[str, Any],
        child: Mapping[str, Any],
        *,
        publication_child_id: str,
        m3_job_id: str,
        m3_job: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Promote an M3 admission adopted after a crash to canonical proof.

        ``adoption_pending`` is deliberately written before the external
        prepare boundary.  Once the deterministic M3 job exists, recovery must
        persist both sides of the binding before returning a preview so later
        board reads never have to infer the M3 job from an incomplete fence.
        """

        current_parent = await durable_job_repository.get_job(str(parent["job_id"])) or dict(parent)
        if current_parent.get("status") != "running":
            raise RoutineError("routine_parent_not_recoverable")
        parent_lease = current_parent.get("lease") if isinstance(current_parent.get("lease"), Mapping) else {}
        child_current = await durable_job_repository.get_job(publication_child_id) or dict(child)
        child_parent_fence = int(child_current.get("parent_fencing_token") or 0)
        current_parent_fence = int(parent_lease.get("fencing_token") or 0)
        if child_parent_fence != current_parent_fence or child_current.get("status") == "blocked":
            owner = current_parent.get("owner") if isinstance(current_parent.get("owner"), Mapping) else {}
            return await self._adopt_recovered_publication_child(
                current_parent,
                child_current,
                publication_child_id=publication_child_id,
                m3_job_id=m3_job_id,
                m3_job=m3_job,
                owner_principal_id=str(owner.get("principal_id") or ""),
                owner_session_id=str(
                    current_parent.get("operator_session_id")
                    or current_parent.get("session_id")
                    or ""
                ),
            )
        declared = m3_job.get("declared_authority") if isinstance(m3_job.get("declared_authority"), Mapping) else {}
        approval_id = str(
            m3_job.get("approval_id")
            or declared.get("approval_id")
            or ""
        ) or None
        m3_status = str(m3_job.get("status") or "")
        child_authority = (
            child_current.get("declared_authority")
            if isinstance(child_current.get("declared_authority"), Mapping)
            else {}
        )
        prepared_payload = {
            "m3_job_id": m3_job_id,
            "approval_id": approval_id,
            "status": m3_status,
            "publication_operation_uuid": child_authority.get("publication_operation_uuid"),
            "recovery": "adopted_child_checkpoint",
        }
        prepared_child = await self._record_child_checkpoint(
            child_current,
            checkpoint_id="routine-child:prepared",
            payload=prepared_payload,
        )
        parent_checkpoint = await durable_job_repository.record_checkpoint(
            str(current_parent["job_id"]),
            checkpoint_id="routine:publication_child_recorded",
            state={
                "step_id": "github_followthrough",
                "child_job_id": publication_child_id,
                "m3_job_id": m3_job_id,
            },
            checkpoint_payload={
                "step_id": "github_followthrough",
                "child_job_id": publication_child_id,
                "m3_job_id": m3_job_id,
                "approval_id": approval_id,
                "status": m3_status,
                "publication_operation_uuid": child_authority.get("publication_operation_uuid"),
                "recovery": "adopted_child_checkpoint",
            },
            safe=True,
            owner=str(parent_lease.get("owner") or ""),
            fencing_token=int(parent_lease.get("fencing_token") or 0),
            expected_revision=current_parent.get("revision"),
        )
        refreshed_parent = await durable_job_repository.get_job(str(current_parent["job_id"])) or parent_checkpoint
        return refreshed_parent, prepared_child

    async def _adopt_recovered_publication_child(
        self,
        parent: Mapping[str, Any],
        child: Mapping[str, Any],
        *,
        publication_child_id: str,
        m3_job_id: str,
        m3_job: Mapping[str, Any],
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Adopt only the deterministic, approval-held M3 child after takeover."""

        parent_id = str(parent.get("job_id") or "")
        parent_authority = (
            parent.get("declared_authority")
            if isinstance(parent.get("declared_authority"), Mapping)
            else {}
        )
        child_authority = (
            child.get("declared_authority")
            if isinstance(child.get("declared_authority"), Mapping)
            else {}
        )
        invocation_uuid = str(parent_authority.get("invocation_uuid") or "")
        if not parent_id or not invocation_uuid:
            raise RoutineError("routine_publication_binding_missing")
        try:
            operation_uuid = str(
                uuid.uuid5(uuid.UUID(invocation_uuid), "seraph:guardian-routine:publication")
            )
            expected_m3_job_id = _expected_publication_job_id(owner_principal_id, operation_uuid)
            routine_revision = int(parent_authority.get("routine_revision") or 0)
            routine_version = int(parent_authority.get("routine_version") or 0)
            goal_revision = int(parent.get("goal_revision") or 0)
            connection_revision = int(parent_authority.get("github_connection_revision") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RoutineError("routine_publication_binding_invalid") from exc
        if (
            routine_revision <= 0
            or routine_version <= 0
            or goal_revision <= 0
            or connection_revision <= 0
            or expected_m3_job_id != m3_job_id
            or publication_child_id != _child_job_id(invocation_uuid, "publication")
        ):
            raise RoutineError("routine_publication_binding_conflict")
        binding_checkpoint = _publication_binding_checkpoint(child)
        if (
            not binding_checkpoint
            or str(binding_checkpoint.get("m3_job_id") or "") != m3_job_id
            or str(child_authority.get("m3_job_id") or "") != m3_job_id
            or str(child_authority.get("publication_operation_uuid") or "") != operation_uuid
        ):
            raise RoutineError("routine_publication_provenance_missing")

        await self._require_active_routine(
            str(parent_authority.get("routine_id") or ""),
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            expected_revision=routine_revision,
        )
        await self._require_active_package_binding(
            str(parent_authority.get("routine_id") or ""),
            version=parent_authority.get("routine_version"),
            expected_digest=str(parent_authority.get("package_digest") or ""),
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )

        expected_binding = {
            "routine_id": str(parent_authority.get("routine_id") or ""),
            "routine_revision": routine_revision,
            "routine_version": routine_version,
            "package_digest": str(parent_authority.get("package_digest") or ""),
            "parent_invocation_job_id": parent_id,
            "publication_child_job_id": publication_child_id,
            "invocation_uuid": invocation_uuid,
            "owner_principal_id": owner_principal_id,
            "owner_session_id": owner_session_id,
            "goal_id": str(parent.get("goal_id") or ""),
            "goal_revision": goal_revision,
            "source_watch_id": str(parent_authority.get("source_watch_id") or ""),
            "connection_id": str(parent_authority.get("github_connection_id") or ""),
            "connection_revision": connection_revision,
            "repository": str(parent_authority.get("github_repository") or ""),
            "action": str(parent_authority.get("github_action") or ""),
            "operation_uuid": operation_uuid,
        }
        m3_authority = (
            m3_job.get("declared_authority")
            if isinstance(m3_job.get("declared_authority"), Mapping)
            else {}
        )
        persisted_binding = m3_authority.get("routine_binding")
        if (
            m3_job.get("job_id") != m3_job_id
            or m3_job.get("job_kind") != GITHUB_FOLLOWTHROUGH_JOB_KIND
            or m3_job.get("status") not in {"awaiting_approval", "queued"}
            or not isinstance(persisted_binding, Mapping)
            or dict(persisted_binding) != expected_binding
        ):
            raise RoutineError("routine_publication_m3_not_approval_held")
        try:
            prepared = await GitHubFollowthroughService()._read_prepared(m3_job)
        except (GitHubFollowthroughError, OSError, ValueError, TypeError) as exc:
            raise RoutineError("routine_publication_m3_binding_invalid") from exc
        try:
            expected_operation_id = _operation_id(
                owner_principal_id,
                uuid.UUID(operation_uuid),
            )
        except (TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise RoutineError("routine_publication_m3_binding_invalid") from exc
        if (
            str(prepared.job_id) != m3_job_id
            or str(prepared.operation_id) != str(expected_operation_id)
            or str(prepared.owner_principal_id) != owner_principal_id
            or str(prepared.owner_session_id) != owner_session_id
            or str(prepared.goal_id) != expected_binding["goal_id"]
            or int(prepared.goal_revision) != goal_revision
            or str(prepared.source_watch_id) != expected_binding["source_watch_id"]
            or str(prepared.connection_id) != expected_binding["connection_id"]
            or int(prepared.connection_revision) != connection_revision
            or str(prepared.repository) != expected_binding["repository"]
            or str(prepared.action) != expected_binding["action"]
        ):
            raise RoutineError("routine_publication_m3_binding_invalid")
        approval_id = str(m3_authority.get("approval_id") or "")
        if not approval_id:
            raise RoutineError("routine_publication_approval_missing")
        parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
        child_parent_fence = int(child.get("parent_fencing_token") or 0)
        try:
            return await durable_job_repository.adopt_routine_publication_child(
                parent_id,
                publication_child_id,
                parent_owner=str(parent_lease.get("owner") or ""),
                parent_fencing_token=int(parent_lease.get("fencing_token") or 0),
                expected_parent_revision=int(parent.get("revision") or 0),
                expected_child_parent_fencing_token=child_parent_fence,
                expected_child_revision=int(child.get("revision") or 0),
                m3_job_id=m3_job_id,
                approval_id=approval_id,
                expected_m3_binding=expected_binding,
            )
        except Exception as exc:
            raise RoutineError("routine_publication_adoption_requires_reconciliation", str(exc)) from exc

    async def _settle_child(
        self,
        child: Mapping[str, Any],
        *,
        result: Mapping[str, Any],
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record a child result before changing its durable state."""

        current = await durable_job_repository.get_job(str(child["job_id"])) or dict(child)
        if current.get("status") != "running":
            return current
        lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
        owner = str(lease.get("owner") or "")
        fence = int(lease.get("fencing_token") or 0)
        revision = int(current.get("revision") or 0)
        safe_result = {
            key: result.get(key)
            for key in ("status", "reason_code", "packet_id", "job_id", "approval_id", "m1_job_id")
            if result.get(key) is not None
        }
        digest = _sha(_dump({"status": status, "reason": reason, **safe_result}))
        effect = await durable_job_repository.record_effect(
            str(child["job_id"]),
            effect_type="guardian_routine_child",
            target_path=f"routine-child:{child['job_id']}",
            target_digest=digest,
            status="succeeded" if status in {"succeeded", "degraded"} else "blocked",
            details={"step_id": (current.get("declared_authority") or {}).get("step_id"), "reason": reason, **safe_result},
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        revision = int(effect.get("revision") or revision)
        if status in {"succeeded", "degraded"}:
            readback = await durable_job_repository.record_readback(
                str(child["job_id"]),
                target_path=f"routine-child:{child['job_id']}",
                effect_id=(effect.get("receipt") or {}).get("effect_id"),
                effect_type="guardian_routine_child",
                target_digest=digest,
                status="succeeded",
                details={"verified": True, "reason": reason, **safe_result},
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
            )
            revision = int(readback.get("revision") or revision)
        return await durable_job_repository.transition_job(
            str(child["job_id"]),
            status,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
            reason=reason,
            result={"step_id": (current.get("declared_authority") or {}).get("step_id"), **safe_result, "learning": "no_learning"},
            result_summary=reason,
        )

    async def _owned_child(
        self,
        child_job_id: str,
        *,
        step_id: str,
        context: RoutineStepContext,
    ) -> dict[str, Any]:
        child = await durable_job_repository.get_job(child_job_id)
        if child is None:
            raise RoutineError("routine_child_not_found", status_code=404)
        authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        owner = child.get("owner") if isinstance(child.get("owner"), Mapping) else {}
        if (
            str(authority.get("step_id") or "") != step_id
            or str(authority.get("routine_invocation_job_id") or "") != str(authority.get("parent_job_id") or "")
            or not str(context.runtime_job_id or "").strip()
            or str(authority.get("parent_job_id") or "") != str(context.runtime_job_id)
            or str(owner.get("principal_id") or "") != context.principal_id
            or str(child.get("session_id") or "") != context.session_id
            or str(lease.get("owner") or "") != context.lease_owner
            or int(lease.get("fencing_token") or 0) != int(context.fencing_token)
            or child.get("status") != "running"
        ):
            raise RoutineError("routine_child_lease_or_step_invalid")
        # The child lease alone is insufficient: a worker can retain it after
        # the routine parent has been recovered.  Read the canonical parent
        # immediately before the capability call and require the immutable
        # child binding to name that parent's current live fence.
        parent = await durable_job_repository.get_job(str(context.runtime_job_id))
        parent_lease = parent.get("lease") if isinstance(parent, Mapping) and isinstance(parent.get("lease"), Mapping) else {}
        if (
            not isinstance(parent, Mapping)
            or parent.get("status") != "running"
            or int(child.get("parent_fencing_token") or 0) <= 0
            or int(child.get("parent_fencing_token") or 0) != int(parent_lease.get("fencing_token") or 0)
        ):
            raise RoutineError("routine_child_parent_fence_stale")
        routine_id = str(authority.get("routine_id") or "")
        routine_revision = int(authority.get("routine_revision") or 0)
        if routine_id and routine_revision:
            await self._require_active_routine(
                routine_id,
                owner_principal_id=context.principal_id,
                owner_session_id=context.session_id,
                expected_revision=routine_revision,
            )
        return child

    async def _hold_approval(self, job: Mapping[str, Any], *, tool_name: str, summary: str, owner_principal_id: str, owner_session_id: str) -> str:
        approval_scope = _routine_approval_scope(job, tool_name)
        fingerprint = fingerprint_tool_call(
            tool_name,
            {"job_id": job.get("job_id"), "authority_digest": job.get("authority_digest")},
            approval_context=approval_scope,
        )
        details = {
            "approval_operator_principal_id": owner_principal_id,
            "approval_owner_principal_id": owner_principal_id,
            "approval_owner_operator_session_id": owner_session_id,
            "operator_principal_id": owner_principal_id,
            "operator_session_id": owner_session_id,
            "approval_conversation_id": owner_session_id,
            "durable_job_id": job.get("job_id"),
            "durable_owner_kind": "user",
            "durable_owner_principal_id": owner_principal_id,
            "durable_service_id": None,
            "durable_authority_digest": job.get("authority_digest"),
            "durable_goal_id": job.get("goal_id"),
            "durable_goal_revision": job.get("goal_revision"),
            "durable_plan_revision": job.get("plan_revision"),
            "durable_capability_version": job.get("capability_version"),
            "durable_budget_digest": job.get("budget_digest"),
            "candidate_id": job.get("candidate_id"),
            "approval_expires_at": (_now() + timedelta(seconds=APPROVAL_TTL_SECONDS)).timestamp(),
            "authority_scope": dict(approval_scope),
            "approval_scope": approval_scope,
            "approval_context": approval_scope,
        }
        approval = await approval_repository.get_or_create_pending(
            session_id=owner_session_id,
            tool_name=tool_name,
            risk_level="high",
            summary=summary,
            fingerprint=fingerprint,
            details=details,
        )
        lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else {}
        bound = await durable_job_repository.bind_approval_id(
            str(job["job_id"]),
            approval.id,
            owner=str(lease.get("owner") or ""),
            fencing_token=int(lease.get("fencing_token") or 0),
            expected_revision=int(job.get("revision") or 0),
        )
        await approval_repository.update_pending_details(
            approval.id,
            owner_principal_id=owner_principal_id,
            operator_session_id=owner_session_id,
            updates={
                "durable_authority_digest": bound.get("authority_digest"),
                "authority_digest": bound.get("authority_digest"),
                "approval_expires_at": approval.expires_at.timestamp() if approval.expires_at else None,
            },
        )
        bound_lease = bound.get("lease") if isinstance(bound.get("lease"), Mapping) else {}
        await durable_job_repository.transition_job(
            str(job["job_id"]),
            "awaiting_approval",
            owner=str(bound_lease.get("owner") or ""),
            fencing_token=int(bound_lease.get("fencing_token") or 0),
            expected_revision=int(bound.get("revision") or 0),
            reason="routine_approval_required",
        )
        return str(approval.id)

    async def _cancel_m3_job_safely(
        self,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        m3_job_id: str,
    ) -> dict[str, Any]:
        """Ask M3 to cancel its reservation and return a visible receipt.

        M4 cannot claim a successful pause/revoke while M3 still owns an
        approval or dispatch reservation.  Cancellation errors are therefore
        returned as a bounded operator action instead of being swallowed.
        """

        if not owner_principal_id or not owner_session_id or not m3_job_id:
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": "m3_child_binding_missing",
                "operator_action": "reconcile_or_cancel",
            }
        try:
            result = await GitHubFollowthroughService().cancel(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                job_id=m3_job_id,
            )
        except Exception as exc:
            # M3 deliberately keeps unknown or in-flight effects for its
            # reconciliation path.  M4 must never overwrite that receipt.
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": type(exc).__name__,
                "operator_action": "reconcile_or_cancel",
            }
        status = str(result.get("status") or "blocked") if isinstance(result, Mapping) else "blocked"
        # The adapter may return ``cancelled`` after preserving an intent or
        # dispatched effect.  That is an unresolved external liability, not a
        # safe child cancellation.  Re-read the canonical M3 row and its
        # effect ledger before allowing M4 to settle its wrapper.
        try:
            canonical = await durable_job_repository.get_job(m3_job_id)
        except Exception as exc:
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": type(exc).__name__,
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        if not isinstance(canonical, Mapping):
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": "m3_job_missing_after_cancel",
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        reported_cleanup = (
            str(result.get("approval_cleanup") or "")
            if isinstance(result, Mapping)
            else ""
        )
        if reported_cleanup and reported_cleanup not in _SAFE_APPROVAL_CLEANUP_OUTCOMES:
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": f"m3_approval_cleanup_{reported_cleanup}",
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        m3_authority = canonical.get("declared_authority")
        m3_authority = m3_authority if isinstance(m3_authority, Mapping) else {}
        approval_id = str(m3_authority.get("approval_id") or "")
        canonical_status_for_approval = str(canonical.get("status") or status)
        if approval_id and canonical_status_for_approval in {"cancelled", "succeeded"}:
            try:
                approval = await approval_repository.get(approval_id)
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "blocked",
                    "reason_code": "m3_approval_cleanup_unavailable",
                    "m3_job_id": m3_job_id,
                    "operator_action": "reconcile_or_cancel",
                    "error": type(exc).__name__,
                }
            approval_status = str(getattr(approval, "status", "") or "") if approval is not None else "missing"
            if approval_status == "pending" or (
                approval_status not in _SAFE_APPROVAL_CLEANUP_OUTCOMES
            ):
                return {
                    "ok": False,
                    "status": "blocked",
                    "reason_code": (
                        "m3_approval_still_pending"
                        if approval_status == "pending"
                        else "m3_approval_cleanup_unknown"
                    ),
                    "m3_job_id": m3_job_id,
                    "operator_action": "reconcile_or_cancel",
                }
        canonical_status = str(canonical.get("status") or status)
        effects = [item for item in canonical.get("effects") or [] if isinstance(item, Mapping)]
        unresolved_external = any(
            item.get("effect_type") == "github_publication"
            and item.get("status") in {"intent", "dispatched", "unknown"}
            for item in effects
        )
        release_pending = (
            isinstance(result, Mapping)
            and isinstance(result.get("connection_release"), Mapping)
            and result["connection_release"].get("status") == "pending"
        )
        if unresolved_external or release_pending or canonical_status in {
            "unknown_external_effect",
            "cost_liability",
        }:
            return {
                "ok": False,
                "status": canonical_status,
                "reason_code": "m3_external_effect_unresolved",
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        if canonical_status in {"cancelled", "succeeded"}:
            return {"ok": True, "status": canonical_status, "m3_job_id": m3_job_id}
        return {
            "ok": False,
            "status": canonical_status,
            "reason_code": "m3_cancel_not_settled",
            "m3_job_id": m3_job_id,
            "operator_action": "reconcile_or_cancel",
        }

    async def _cancel_adoption_pending_publication_child_safely(
        self,
        child: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Fence an adoption-pending wrapper before routine pause/revoke.

        The wrapper child is admitted before M3 and the two rows are
        intentionally separate.  When the deterministic M3 row is absent,
        cancel the exact wrapper child first, then re-read the deterministic
        identity and compensate if admission won that interleaving.  A caller
        may settle the routine only after both durable rows prove a no-effect
        terminal state.
        """

        child_id = str(child.get("job_id") or "")
        checkpoint = _job_checkpoint(child, "routine-child:adoption_pending") or {}
        if (
            not child_id
            or str(checkpoint.get("status") or "") != "prepare_pending"
            or list(child.get("effects") or [])
        ):
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": "routine_publication_adoption_not_pre_admission",
                "operator_action": "reconcile_or_cancel",
                "child_job_id": child_id or None,
            }

        authority = child.get("declared_authority")
        authority = authority if isinstance(authority, Mapping) else {}
        m3_job_id = str(
            checkpoint.get("m3_job_id")
            or authority.get("m3_job_id")
            or ""
        )
        operation_uuid = str(
            checkpoint.get("publication_operation_uuid")
            or checkpoint.get("operation_uuid")
            or authority.get("publication_operation_uuid")
            or authority.get("operation_uuid")
            or ""
        )
        if not m3_job_id and operation_uuid:
            try:
                m3_job_id = _expected_publication_job_id(owner_principal_id, operation_uuid)
            except (TypeError, ValueError, AttributeError, OverflowError):
                m3_job_id = ""
        if not m3_job_id:
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": "routine_publication_m3_binding_missing",
                "operator_action": "reconcile_or_cancel",
                "child_job_id": child_id,
            }

        # A persisted M3 row always wins over the pre-admission assumption.
        # Route it through the canonical adapter so approval/connection
        # reservations and unknown effects retain their own receipts.
        try:
            existing_m3 = await durable_job_repository.get_job(m3_job_id)
        except Exception as exc:
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": type(exc).__name__,
                "child_job_id": child_id,
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        if existing_m3 is not None:
            cancellation = await self._cancel_m3_job_safely(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                m3_job_id=m3_job_id,
            )
            return {
                **dict(cancellation),
                "child_job_id": child_id,
                "m3_job_id": m3_job_id,
                "child_already_cancelled": False,
            }

        child_status = str(child.get("status") or "")
        lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        if child_status in {"accepted", "queued", "running", "awaiting_approval", "blocked"}:
            try:
                await durable_job_repository.cancel_job(
                    child_id,
                    owner=str(lease.get("owner") or "") if child_status == "running" else None,
                    fencing_token=int(lease.get("fencing_token") or 0) if child_status == "running" else None,
                    expected_revision=child.get("revision"),
                    reason="routine_publication_prepare_cancelled",
                )
            except Exception:
                # The reread below distinguishes a competing successful CAS
                # from a child that remains live and needs reconciliation.
                pass

        try:
            settled_child = await durable_job_repository.get_job(child_id)
            raced_m3 = await durable_job_repository.get_job(m3_job_id)
        except Exception as exc:
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": type(exc).__name__,
                "child_job_id": child_id,
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        if not isinstance(settled_child, Mapping) or str(settled_child.get("status") or "") != "cancelled":
            return {
                "ok": False,
                "status": "blocked",
                "reason_code": "routine_publication_child_cancel_unsettled",
                "child_job_id": child_id,
                "m3_job_id": m3_job_id,
                "operator_action": "reconcile_or_cancel",
            }
        if raced_m3 is not None:
            cancellation = await self._cancel_m3_job_safely(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                m3_job_id=m3_job_id,
            )
            if not cancellation.get("ok"):
                return {
                    **dict(cancellation),
                    "child_job_id": child_id,
                    "m3_job_id": m3_job_id,
                    "child_already_cancelled": True,
                }
            # The adapter has re-read M3 after cancellation.  Keep the
            # wrapper result only if that canonical read proved settled.
            return {
                "ok": True,
                "status": cancellation.get("status") or "cancelled",
                "child_job_id": child_id,
                "m3_job_id": m3_job_id,
                "child_already_cancelled": True,
            }
        return {
            "ok": True,
            "status": "cancelled",
            "child_job_id": child_id,
            "m3_job_id": m3_job_id,
            "child_already_cancelled": True,
            "pre_admission": True,
        }

    async def _cancel_stale_child(
        self,
        child: Mapping[str, Any],
        *,
        parent: Mapping[str, Any],
        step_id: str,
        reason_code: str = "routine_child_parent_fence_stale",
        operator_action: str = "restart_routine_invocation",
        cancel_external: bool = True,
    ) -> dict[str, Any]:
        """Stop a child whose parent fence is no longer authoritative.

        Child transitions other than cancellation are parent-fenced by the
        durable runtime.  Cancellation is intentionally the one terminal
        escape hatch, so an old worker cannot keep a stale child live.  M3 is
        asked to settle first; its canonical effect receipt remains the
        authority when an external publication may already exist.
        """

        child_id = str(child.get("job_id") or "")
        owner = child.get("owner") if isinstance(child.get("owner"), Mapping) else {}
        lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        m3_job_id = ""
        m3_cancellation: dict[str, Any] | None = None
        m1_job_id = ""
        m1_cancellation: dict[str, Any] | None = None
        if step_id == "github_followthrough":
            binding = _publication_binding_checkpoint(child) or {}
            m3_job_id = str(binding.get("m3_job_id") or "")
            if m3_job_id and cancel_external:
                child_authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
                m3_cancellation = await self._cancel_m3_job_safely(
                    owner_principal_id=str(owner.get("principal_id") or ""),
                    owner_session_id=str(
                        child.get("operator_session_id")
                        or child.get("session_id")
                        or child_authority.get("session_id")
                        or ""
                    ),
                    m3_job_id=m3_job_id,
                )
        elif step_id == "guardian_watch_run" and cancel_external:
            # M1 owns the source-watch fence and its durable occurrence.  A
            # stale M4 wrapper must release that reservation before it is
            # cancelled; otherwise a paused/revoked routine can strand an
            # active watch and allow a later scheduler occurrence to race it.
            child_authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
            dispatch = _job_checkpoint(child, "routine-child:dispatch_started") or {}
            m1_job_id = str(dispatch.get("m1_job_id") or "")
            if not m1_job_id:
                for effect in child.get("effects", []) or []:
                    details = effect.get("details") if isinstance(effect, Mapping) else None
                    if isinstance(details, Mapping) and details.get("m1_job_id"):
                        m1_job_id = str(details.get("m1_job_id"))
                        break
            watch_id = str(child_authority.get("source_watch_id") or "")
            if not m1_job_id and watch_id and child_id:
                # M1 derives its occurrence from the M4 child ID. This closes
                # the crash window between M1 admission and the child receipt
                # that records the returned M1 ID.
                m1_job_id = f"source-watch:{watch_id}:{child_id}"
            owner_principal_id = str(owner.get("principal_id") or "")
            owner_session_id = str(
                child.get("operator_session_id")
                or child.get("session_id")
                or child_authority.get("session_id")
                or ""
            )
            if m1_job_id and watch_id:
                try:
                    watch = await source_watch_service.get_watch(
                        watch_id,
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                    )
                    m1_job = await durable_job_repository.get_job(m1_job_id)
                    m1_status = str(m1_job.get("status") or "") if isinstance(m1_job, Mapping) else ""
                    if m1_status in {"accepted", "queued", "running", "awaiting_approval", "blocked"}:
                        if not isinstance(watch, Mapping):
                            raise RuntimeError("source_watch_missing")
                        active_fence = int(watch.get("active_job_fence") or 0)
                        if active_fence <= 0 or str(watch.get("active_job_id") or "") != m1_job_id:
                            raise RuntimeError("source_watch_execution_fence_stale")
                        m1_cancellation = await source_watch_service.cancel_watch_job(
                            watch_id=watch_id,
                            job_id=m1_job_id,
                            expected_plan_revision=int(watch.get("plan_revision") or 0),
                            expected_fencing_token=active_fence,
                            owner_principal_id=owner_principal_id,
                            owner_session_id=owner_session_id,
                        )
                        if str(m1_cancellation.get("status") or "") != "cancelled":
                            m1_cancellation = {
                                "ok": False,
                                "status": str(m1_cancellation.get("status") or "blocked"),
                                "reason_code": "m1_cancel_not_settled",
                                "operator_action": "recover_or_cancel",
                            }
                        else:
                            m1_cancellation = {"ok": True, **dict(m1_cancellation)}
                    elif m1_status in {"unknown_external_effect", "cost_liability"}:
                        m1_cancellation = {
                            "ok": False,
                            "status": m1_status,
                            "reason_code": "m1_external_effect_unresolved",
                            "operator_action": "recover_or_cancel",
                        }
                    elif isinstance(watch, Mapping) and str(watch.get("active_job_id") or "") == m1_job_id:
                        raise RuntimeError("source_watch_job_missing")
                except Exception as exc:
                    m1_cancellation = {
                        "ok": False,
                        "status": "blocked",
                        "reason_code": type(exc).__name__,
                        "operator_action": "recover_or_cancel",
                    }

        child_result: Mapping[str, Any] | None = None
        status = str(child.get("status") or "")
        external_cancellation = (
            m3_cancellation
            if m3_cancellation and not m3_cancellation.get("ok")
            else m1_cancellation
            if m1_cancellation and not m1_cancellation.get("ok")
            else None
        )
        if external_cancellation is None and status in {"accepted", "queued", "running", "awaiting_approval", "blocked"} and child_id:
            try:
                child_result = await durable_job_repository.cancel_job(
                    child_id,
                    owner=str(lease.get("owner") or "") or None,
                    fencing_token=int(lease.get("fencing_token") or 0) or None,
                    expected_revision=child.get("revision"),
                    reason=reason_code,
                )
            except Exception as exc:
                child_result = None
                return {
                    "status": "blocked",
                    "job_id": parent.get("job_id"),
                    "child_job_id": child_id,
                    "m3_job_id": m3_job_id or None,
                    "reason_code": "routine_stale_child_cancel_failed",
                    "recovery": "reconcile_or_cancel",
                    "operator_action": "reconcile_or_cancel",
                    "cancel_error": type(exc).__name__,
                    "operator_visible": True,
                    "learning": "no_learning",
                }

        m3_unresolved = bool(m3_cancellation and not m3_cancellation.get("ok"))
        m1_unresolved = bool(m1_cancellation and not m1_cancellation.get("ok"))
        external_unresolved = m3_unresolved or m1_unresolved
        result_status = str(child_result.get("status") or "cancelled") if child_result else status or "cancelled"
        recovery = "reconcile_or_cancel" if m3_unresolved else "recover_or_cancel" if m1_unresolved else operator_action
        outcome = {
            "status": "blocked" if external_unresolved else result_status,
            "job_id": parent.get("job_id"),
            "child_job_id": child_id,
            "child_status": result_status,
            "m3_job_id": m3_job_id or None,
            "m1_job_id": m1_job_id or None,
            "reason_code": (
                str(m3_cancellation.get("reason_code") or "m3_external_effect_unresolved")
                if m3_unresolved and m3_cancellation
                else str(m1_cancellation.get("reason_code") or "m1_watch_effect_unresolved")
                if m1_unresolved and m1_cancellation
                else reason_code
            ),
            "recovery": recovery,
            "operator_action": recovery,
            "operator_visible": True,
            "learning": "no_learning",
        }
        latest = await durable_job_repository.get_job(str(parent.get("job_id") or "")) or dict(parent)
        if latest.get("status") == "running":
            parent_lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
            try:
                await durable_job_repository.transition_job(
                    str(parent["job_id"]),
                    "blocked",
                    owner=str(parent_lease.get("owner") or "") or None,
                    fencing_token=int(parent_lease.get("fencing_token") or 0) or None,
                    expected_revision=latest.get("revision"),
                    reason=str(outcome["reason_code"]),
                    result={
                        "child_job_id": child_id,
                        "m3_job_id": m3_job_id or None,
                        "recovery": recovery,
                        "operator_action": recovery,
                        "learning": "no_learning",
                    },
                    result_summary="stale routine child was stopped; operator recovery is required",
                )
            except Exception:
                # The current parent receipt is still returned below.  A
                # competing recovery owns the canonical transition and will
                # expose its own fence result to the operator.
                pass
        return outcome

    async def _list_routine_jobs(
        self,
        routine_id: str,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Read every typed routine job for one persisted owner/session.

        ``DurableJobRepository.list_jobs`` intentionally caps operator pages at
        100. Pause/revoke is a fencing operation, so it must use a scoped
        durable query that cannot silently leave the 101st child live.
        """

        if not owner_principal_id or not owner_session_id:
            return
        page_size = 100
        offset = 0
        while True:
            async with db_engine.get_session() as db:
                rows = (
                    await db.execute(
                        select(WorkflowRunState)
                        .where(
                            WorkflowRunState.record_schema_version >= 2,
                            WorkflowRunState.owner_principal_id == owner_principal_id,
                            WorkflowRunState.operator_session_id == owner_session_id,
                            WorkflowRunState.job_kind.like("routine_%"),
                        )
                        .order_by(WorkflowRunState.updated_at.desc(), WorkflowRunState.run_identity.desc())
                        .offset(offset)
                        .limit(page_size)
                    )
                ).scalars().all()
                if not rows:
                    return
                page_jobs: list[dict[str, Any]] = []
                for row in rows:
                    authority = _load(row.declared_authority_json, {})
                    if not isinstance(authority, Mapping) or str(authority.get("routine_id") or "") != routine_id:
                        continue
                    db.expunge(row)
                    page_jobs.append(_serialize(row))
                page_complete = len(rows) < page_size
            for job in page_jobs:
                yield job
            if page_complete:
                return
            offset += len(rows)

    async def _cancel_pending_jobs(
        self,
        routine_id: str,
        *,
        reason: str,
        owner_principal_id: str | None = None,
        owner_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cancel still-admissible children and return any unsettled receipts."""

        async def _job_stream() -> AsyncIterator[dict[str, Any]]:
            if owner_principal_id and owner_session_id:
                async for job in self._list_routine_jobs(
                    routine_id,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                ):
                    yield job
                return
            # Keep an explicit compatibility path for old internal callers;
            # pause/revoke and rollback always provide both persisted owner
            # bindings and therefore use the paged scoped query above.
            for job in await durable_job_repository.list_jobs(limit=100):
                yield job

        # Snapshot the complete scoped set before mutating any row.  Cancelling
        # a job changes ``updated_at`` and therefore changes the offset based
        # query used by ``_list_routine_jobs``; streaming and mutating in the
        # same pass can skip every other child after the first page.
        jobs = [job async for job in _job_stream()]
        failures: list[dict[str, Any]] = []
        parent_jobs: list[dict[str, Any]] = []
        for job in jobs:
            authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
            if str(authority.get("routine_id") or "") != routine_id:
                continue
            child_job_id = str(job.get("job_id") or "")
            step_id = str(authority.get("step_id") or "")
            if str(job.get("job_kind") or "") == "routine_invocation" and not step_id:
                # Defer the parent until every child cancellation is known.
                # A failed M3 cancellation must leave the parent visibly
                # recoverable rather than making a terminal success/cancel
                # receipt hide the unresolved external reservation.
                parent_jobs.append(job)
                continue
            status = str(job.get("status") or "")
            if status in {"succeeded", "degraded", "cancelled"}:
                # A settled wrapper proves its owned child boundary is
                # complete. Revoke must not attempt to cancel an already
                # verified M3 publication (or a completed source-watch run).
                continue
            m3_cancel_failed = False
            child_already_cancelled = False
            m3_job_id = ""
            # The M3 job owns the external approval/connection reservation.
            # A routine pause/revoke must ask that service to cancel its
            # recorded child; an M4 metadata transition alone would leave a
            # live publication approval behind.
            if step_id == "github_followthrough":
                child_owner = str((job.get("owner") or {}).get("principal_id") or "")
                child_session = str(
                    job.get("operator_session_id")
                    or job.get("session_id")
                    or authority.get("session_id")
                    or ""
                )
                adoption_pending = _job_checkpoint(job, "routine-child:adoption_pending") or {}
                prepared = _job_checkpoint(job, "routine-child:prepared") or {}
                if not prepared and str(adoption_pending.get("status") or "") == "prepare_pending":
                    cancellation = await self._cancel_adoption_pending_publication_child_safely(
                        job,
                        owner_principal_id=child_owner,
                        owner_session_id=child_session,
                    )
                    child_already_cancelled = bool(cancellation.get("child_already_cancelled"))
                else:
                    prepared = prepared or adoption_pending
                    m3_job_id = str(prepared.get("m3_job_id") or authority.get("m3_job_id") or "")
                    m3_job = await durable_job_repository.get_job(m3_job_id) if m3_job_id else None
                    if m3_job and m3_job.get("status") == "succeeded" and _verified_readback(m3_job):
                        # Recovery may complete M3 immediately before the M4
                        # wrapper is settled. Revoke can cancel that stale
                        # wrapper, but it must preserve the verified external
                        # outcome and never ask the publication service to
                        # cancel an already completed effect.
                        cancellation = {"ok": True, "m3_job_id": m3_job_id}
                    else:
                        cancellation = await self._cancel_m3_job_safely(
                            owner_principal_id=child_owner,
                            owner_session_id=child_session,
                            m3_job_id=m3_job_id,
                        )
                if not cancellation.get("ok"):
                    m3_cancel_failed = True
                    failures.append(
                        {
                            "job_id": child_job_id,
                            "step_id": step_id,
                            "m3_job_id": cancellation.get("m3_job_id") or m3_job_id or None,
                            "status": cancellation.get("status") or "blocked",
                            "reason_code": cancellation.get("reason_code") or "m3_cancel_failed",
                            "operator_action": cancellation.get("operator_action") or "reconcile_or_cancel",
                        }
                    )
            # Leave the M4 child in its durable pending/blocked state while
            # M3 still owns an unresolved reservation. Cancelling only the
            # wrapper would hide the operator action needed at the canonical
            # external-effect boundary.
            if m3_cancel_failed:
                continue
            if step_id == "guardian_watch_run":
                # M1 owns the watch fence and its approval-held job. Revoke
                # must release that canonical reservation before the child is
                # cancelled, otherwise a paused routine can strand a watch.
                m1_cancel_failed = False
                dispatch = _job_checkpoint(job, "routine-child:dispatch_started") or {}
                m1_job_id = str(dispatch.get("m1_job_id") or "")
                if not m1_job_id:
                    for effect in job.get("effects", []) or []:
                        details = effect.get("details") if isinstance(effect, Mapping) else None
                        if isinstance(details, Mapping) and details.get("m1_job_id"):
                            m1_job_id = str(details.get("m1_job_id"))
                            break
                watch_id = str(authority.get("source_watch_id") or "")
                if not m1_job_id and watch_id and child_job_id:
                    m1_job_id = f"source-watch:{watch_id}:{child_job_id}"
                if m1_job_id and watch_id:
                    try:
                        watch = await source_watch_service.get_watch(
                            watch_id,
                            owner_principal_id=owner_principal_id or str(job.get("owner", {}).get("principal_id") or ""),
                            owner_session_id=owner_session_id or str(job.get("operator_session_id") or job.get("session_id") or authority.get("session_id") or ""),
                        )
                        m1_job = await durable_job_repository.get_job(m1_job_id)
                        m1_status = str(m1_job.get("status") or "") if isinstance(m1_job, Mapping) else ""
                        if m1_status in {
                            "accepted", "queued", "running", "awaiting_approval", "blocked"
                        }:
                            if not isinstance(watch, Mapping):
                                raise RuntimeError("source_watch_missing")
                            active_fence = int(watch.get("active_job_fence") or 0)
                            if active_fence <= 0 or str(watch.get("active_job_id") or "") != m1_job_id:
                                raise RuntimeError("source_watch_execution_fence_stale")
                            await source_watch_service.cancel_watch_job(
                                watch_id=watch_id,
                                job_id=m1_job_id,
                                expected_plan_revision=int(watch.get("plan_revision") or 0),
                                expected_fencing_token=active_fence,
                                owner_principal_id=owner_principal_id or str(job.get("owner", {}).get("principal_id") or ""),
                                owner_session_id=owner_session_id or str(job.get("operator_session_id") or job.get("session_id") or authority.get("session_id") or ""),
                            )
                        elif isinstance(watch, Mapping) and str(watch.get("active_job_id") or "") == m1_job_id:
                            raise RuntimeError("source_watch_job_missing")
                    except Exception as exc:
                        # Keep the child and M1 receipts for the explicit
                        # recovery route if a concurrent worker owns the fence.
                        m1_cancel_failed = True
                        failures.append(
                            {
                                "job_id": child_job_id,
                                "step_id": step_id,
                                "status": "blocked",
                                "reason_code": type(exc).__name__,
                                "operator_action": "recover_or_cancel",
                            }
                        )
                if m1_cancel_failed:
                    continue
            if child_already_cancelled:
                continue
            if status not in {"accepted", "queued", "running", "awaiting_approval", "blocked"}:
                continue
            lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else {}
            try:
                await durable_job_repository.cancel_job(
                    child_job_id,
                    owner=str(lease.get("owner")) if status == "running" else None,
                    fencing_token=int(lease.get("fencing_token")) if status == "running" else None,
                    expected_revision=int(job.get("revision") or 0),
                    reason=reason,
                )
            except Exception as exc:
                # A concurrent claim or an uncertain effect is left for the
                # canonical runtime recovery path rather than being overwritten.
                failures.append(
                    {
                        "job_id": child_job_id,
                        "step_id": step_id,
                        "status": "blocked",
                        "reason_code": type(exc).__name__,
                        "operator_action": "reconcile_or_cancel" if step_id == "github_followthrough" else "recover_or_cancel",
                    }
                )
        for parent in parent_jobs:
            parent_id = str(parent.get("job_id") or "")
            parent_status = str(parent.get("status") or "")
            if parent_status not in {"accepted", "queued", "running", "awaiting_approval", "blocked"}:
                continue
            lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
            if failures:
                if parent_status == "blocked":
                    continue
                try:
                    await durable_job_repository.transition_job(
                        parent_id,
                        "blocked",
                        owner=str(lease.get("owner")) if parent_status == "running" else None,
                        fencing_token=int(lease.get("fencing_token")) if parent_status == "running" else None,
                        expected_revision=int(parent.get("revision") or 0),
                        reason="routine_child_cancellation_incomplete",
                        result={"recovery": "reconcile_or_cancel", "operator_action": "reconcile_or_cancel", "learning": "no_learning"},
                        result_summary="routine child cancellation is incomplete; reconcile before resuming",
                    )
                except Exception as exc:
                    failures.append(
                        {
                            "job_id": parent_id,
                            "step_id": "routine_invocation",
                            "status": "blocked",
                            "reason_code": type(exc).__name__,
                            "operator_action": "reconcile_or_cancel",
                        }
                    )
                continue
            try:
                await durable_job_repository.cancel_job(
                    parent_id,
                    owner=str(lease.get("owner")) if parent_status == "running" else None,
                    fencing_token=int(lease.get("fencing_token")) if parent_status == "running" else None,
                    expected_revision=int(parent.get("revision") or 0),
                    reason=reason,
                )
            except Exception as exc:
                failures.append(
                    {
                        "job_id": parent_id,
                        "step_id": "routine_invocation",
                        "status": "blocked",
                        "reason_code": type(exc).__name__,
                        "operator_action": "recover_or_cancel",
                    }
                )
        return failures

    async def _invocation_publication_cancel_binding(
        self,
        parent: Mapping[str, Any],
        *,
        invocation_job_id: str,
        routine_id: str,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any] | None:
        """Resolve the one publication job owned by this invocation.

        The GitHub publication is deliberately admitted as a separate M3 job,
        so ``cancel_job_tree`` cannot discover it through the durable parent
        links.  This resolver uses only the invocation's durable checkpoint,
        deterministic child identity, and M3's exact routine binding.  It
        never scans other jobs or accepts a job id supplied by the caller.
        ``None`` means that this invocation has not admitted a publication
        child; a mapping with ``ok=False`` is a binding failure that must keep
        the invocation recoverable.
        """

        authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        parent_checkpoint = _job_checkpoint(parent, "routine:publication_child_recorded") or {}
        recorded_child_id = str(parent_checkpoint.get("child_job_id") or "")
        recorded_m3_job_id = str(parent_checkpoint.get("m3_job_id") or "")
        invocation_uuid = str(authority.get("invocation_uuid") or "")
        derived_child_id = ""
        derived_m3_job_id = ""
        derived_operation_uuid = ""
        if invocation_uuid:
            try:
                parsed_invocation_uuid = uuid.UUID(invocation_uuid)
                invocation_uuid = str(parsed_invocation_uuid)
                derived_child_id = _child_job_id(invocation_uuid, "publication")
                derived_operation_uuid = str(
                    uuid.uuid5(parsed_invocation_uuid, "seraph:guardian-routine:publication")
                )
                derived_m3_job_id = _expected_publication_job_id(
                    owner_principal_id,
                    derived_operation_uuid,
                )
            except (AttributeError, TypeError, ValueError, OverflowError):
                return {
                    "ok": False,
                    "reason_code": "routine_publication_binding_invalid",
                    "child_job_id": recorded_child_id or None,
                    "m3_job_id": recorded_m3_job_id or None,
                }
        if recorded_child_id and derived_child_id and recorded_child_id != derived_child_id:
            return {
                "ok": False,
                "reason_code": "routine_publication_binding_conflict",
                "child_job_id": recorded_child_id,
                "m3_job_id": recorded_m3_job_id or None,
            }
        child_job_id = recorded_child_id or derived_child_id
        if not child_job_id:
            # No invocation UUID and no persisted publication checkpoint means
            # this invocation never crossed the publication admission boundary.
            return None

        child = await durable_job_repository.get_job(child_job_id)
        explicit_publication_binding = bool(recorded_child_id or recorded_m3_job_id)
        if child is None:
            # A crash can occur after M3 admission but before the M4 child row
            # is visible.  In that case the deterministic M3 identity is still
            # safe to inspect; a missing M3 row proves there is nothing to
            # cancel only when no checkpoint already recorded an admission.
            m3_candidate = recorded_m3_job_id or derived_m3_job_id
            if not m3_candidate:
                return {
                    "ok": False,
                    "reason_code": "routine_publication_child_missing",
                    "child_job_id": child_job_id,
                    "m3_job_id": None,
                } if explicit_publication_binding else None
            m3_job = await durable_job_repository.get_job(m3_candidate)
            if m3_job is None:
                if explicit_publication_binding:
                    return {
                        "ok": False,
                        "reason_code": "routine_publication_m3_missing",
                        "child_job_id": child_job_id,
                        "m3_job_id": m3_candidate,
                    }
                return None
            child_authority: Mapping[str, Any] = {}
            child_checkpoint: Mapping[str, Any] = {}
            m3_job_id = m3_candidate
        else:
            child_authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
            child_checkpoint = _publication_binding_checkpoint(child) or {}
            child_checkpoint_id = ""
            for checkpoint in reversed(child.get("checkpoints", []) or []):
                if not isinstance(checkpoint, Mapping) or checkpoint.get("checkpoint_id") not in {
                    "routine-child:adoption_pending",
                    "routine-child:prepared",
                }:
                    continue
                child_checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
                break
            child_m3_job_id = str(child_authority.get("m3_job_id") or "")
            checkpoint_m3_job_id = str(child_checkpoint.get("m3_job_id") or "")
            m3_ids = {
                value
                for value in (
                    recorded_m3_job_id,
                    child_m3_job_id,
                    checkpoint_m3_job_id,
                    derived_m3_job_id,
                )
                if value
            }
            if len(m3_ids) > 1:
                return {
                    "ok": False,
                    "reason_code": "routine_publication_binding_conflict",
                    "child_job_id": child_job_id,
                    "m3_job_id": recorded_m3_job_id or child_m3_job_id or checkpoint_m3_job_id or None,
                }
            m3_job_id = next(iter(m3_ids), "")
            if not m3_job_id:
                return {
                    "ok": False,
                    "reason_code": "routine_publication_binding_missing",
                    "child_job_id": child_job_id,
                    "m3_job_id": None,
                }
            if (
                str(child.get("job_kind") or "") != ROUTINE_PUBLICATION_CHILD_JOB_KIND
                or str(child.get("parent_job_id") or "") != invocation_job_id
                or str(
                    (child.get("owner") if isinstance(child.get("owner"), Mapping) else {}).get("principal_id")
                    or ""
                )
                != owner_principal_id
                or str(child.get("operator_session_id") or child.get("session_id") or "") != owner_session_id
                or str(child_authority.get("routine_id") or "") != routine_id
                or str(child_authority.get("parent_job_id") or "") != invocation_job_id
                or str(child_authority.get("routine_invocation_job_id") or "") != invocation_job_id
                or str(child_authority.get("step_id") or "") != "github_followthrough"
                or (child_m3_job_id and child_m3_job_id != m3_job_id)
                or (
                    child_checkpoint
                    and str(child_checkpoint.get("m3_job_id") or "") != m3_job_id
                )
            ):
                return {
                    "ok": False,
                    "reason_code": "routine_publication_binding_conflict",
                    "child_job_id": child_job_id,
                    "m3_job_id": m3_job_id,
                }
            child_operation_uuid = str(child_authority.get("publication_operation_uuid") or child_checkpoint.get("publication_operation_uuid") or "")
            if derived_operation_uuid and child_operation_uuid and child_operation_uuid != derived_operation_uuid:
                return {
                    "ok": False,
                    "reason_code": "routine_publication_binding_conflict",
                    "child_job_id": child_job_id,
                    "m3_job_id": m3_job_id,
                }
            if not invocation_uuid and child_operation_uuid:
                derived_operation_uuid = child_operation_uuid
            m3_job = await durable_job_repository.get_job(m3_job_id)
            if m3_job is None:
                # ``adoption_pending`` is written before the M3 prepare
                # boundary.  A missing deterministic M3 row at this exact
                # checkpoint can therefore be a harmless pre-admission
                # crash, but only after the M4 child itself is fenced and
                # durably settled.  Re-read the deterministic row after that
                # CAS to compensate for a concurrent prepare that won the
                # admission race.
                pre_admission = (
                    child_checkpoint_id == "routine-child:adoption_pending"
                    and str(child_checkpoint.get("status") or "") == "prepare_pending"
                    and not list(child.get("effects") or [])
                )
                if not pre_admission:
                    return {
                        "ok": False,
                        "reason_code": "routine_publication_m3_missing",
                        "child_job_id": child_job_id,
                        "m3_job_id": m3_job_id,
                    }
                child_status = str(child.get("status") or "")
                child_lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
                if child_status in {"accepted", "queued", "running", "awaiting_approval", "blocked"}:
                    try:
                        await durable_job_repository.cancel_job(
                            child_job_id,
                            owner=str(child_lease.get("owner") or "") if child_status == "running" else None,
                            fencing_token=int(child_lease.get("fencing_token") or 0) if child_status == "running" else None,
                            expected_revision=child.get("revision"),
                            reason="routine_publication_prepare_cancelled",
                        )
                    except Exception:
                        # Re-read below. A competing worker may have won the
                        # child CAS, which is safe only if the receipt proves
                        # the same settled state.
                        pass
                settled_child = await durable_job_repository.get_job(child_job_id)
                raced_m3 = await durable_job_repository.get_job(m3_job_id)
                if raced_m3 is None:
                    if (
                        isinstance(settled_child, Mapping)
                        and str(settled_child.get("status") or "") == "cancelled"
                        and not list(settled_child.get("effects") or [])
                    ):
                        return {
                            "ok": True,
                            "child_job_id": child_job_id,
                            "m3_job_id": None,
                            "pre_admission": True,
                        }
                    return {
                        "ok": False,
                        "reason_code": "routine_publication_child_cancel_unsettled",
                        "child_job_id": child_job_id,
                        "m3_job_id": m3_job_id,
                    }
                if not isinstance(settled_child, Mapping) or str(settled_child.get("status") or "") != "cancelled":
                    return {
                        "ok": False,
                        "reason_code": "routine_publication_child_cancel_unsettled",
                        "child_job_id": child_job_id,
                        "m3_job_id": m3_job_id,
                    }
                child = settled_child
                child_authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
                child_checkpoint = _publication_binding_checkpoint(child) or child_checkpoint
                m3_job = raced_m3

        m3_authority = m3_job.get("declared_authority") if isinstance(m3_job, Mapping) and isinstance(m3_job.get("declared_authority"), Mapping) else {}
        m3_binding = m3_authority.get("routine_binding")
        if (
            not isinstance(m3_job, Mapping)
            or str(m3_job.get("job_id") or "") != m3_job_id
            or str(m3_job.get("job_kind") or "") != GITHUB_FOLLOWTHROUGH_JOB_KIND
            or not isinstance(m3_binding, Mapping)
            or set(m3_binding) != set(ROUTINE_BINDING_KEYS)
        ):
            return {
                "ok": False,
                "reason_code": "routine_publication_binding_invalid",
                "child_job_id": child_job_id,
                "m3_job_id": m3_job_id,
            }

        m3_owner = m3_job.get("owner") if isinstance(m3_job.get("owner"), Mapping) else {}
        m3_session = str(
            m3_authority.get("session_id")
            or m3_job.get("operator_session_id")
            or m3_job.get("session_id")
            or ""
        )
        if (
            str(m3_owner.get("principal_id") or "") != owner_principal_id
            or m3_session != owner_session_id
        ):
            return {
                "ok": False,
                "reason_code": "routine_invocation_owner_mismatch",
                "child_job_id": child_job_id,
                "m3_job_id": m3_job_id,
            }

        expected_binding: dict[str, Any] = {
            "routine_id": routine_id,
            "parent_invocation_job_id": invocation_job_id,
            "publication_child_job_id": child_job_id,
            "owner_principal_id": owner_principal_id,
            "owner_session_id": owner_session_id,
        }
        if invocation_uuid:
            expected_binding["invocation_uuid"] = invocation_uuid
        if derived_operation_uuid:
            expected_binding["operation_uuid"] = derived_operation_uuid
        for key in (
            "routine_revision",
            "routine_version",
            "package_digest",
            "goal_id",
            "goal_revision",
            "source_watch_id",
            "connection_id",
            "connection_revision",
            "repository",
            "action",
        ):
            value = authority.get(key)
            if value not in (None, ""):
                expected_binding[key] = value
        for key in (
            "routine_revision",
            "routine_version",
            "package_digest",
            "source_watch_id",
            "connection_id",
            "connection_revision",
            "repository",
            "action",
        ):
            value = child_authority.get(key)
            if value not in (None, "") and key not in expected_binding:
                expected_binding[key] = value
        if "goal_id" not in expected_binding and parent.get("goal_id") not in (None, ""):
            expected_binding["goal_id"] = parent.get("goal_id")
        if "goal_revision" not in expected_binding and parent.get("goal_revision") not in (None, ""):
            expected_binding["goal_revision"] = parent.get("goal_revision")

        def _binding_equal(key: str, actual: Any, expected: Any) -> bool:
            if key in {"routine_revision", "routine_version", "goal_revision", "connection_revision"}:
                try:
                    return int(actual) == int(expected)
                except (TypeError, ValueError, OverflowError):
                    return False
            return str(actual or "") == str(expected or "")

        if any(
            not _binding_equal(key, m3_binding.get(key), expected)
            for key, expected in expected_binding.items()
        ):
            return {
                "ok": False,
                "reason_code": "routine_publication_binding_conflict",
                "child_job_id": child_job_id,
                "m3_job_id": m3_job_id,
            }
        return {
            "ok": True,
            "child_job_id": child_job_id,
            "m3_job_id": m3_job_id,
            "m3_job": dict(m3_job),
        }

    async def _block_invocation_cancellation(
        self,
        parent: Mapping[str, Any],
        *,
        child_job_id: str | None,
        m3_job_id: str | None,
        reason_code: str,
    ) -> dict[str, Any]:
        """Keep an invocation recoverable when its separate M3 cannot settle."""

        parent_id = str(parent.get("job_id") or "")
        current = await durable_job_repository.get_job(parent_id) or dict(parent)
        status = str(current.get("status") or "blocked")
        if status in {"accepted", "queued", "running", "awaiting_approval"}:
            lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            kwargs: dict[str, Any] = {
                "expected_revision": current.get("revision"),
                "reason": reason_code,
                "result": {
                    "child_job_id": child_job_id,
                    "m3_job_id": m3_job_id,
                    "recovery": "reconcile_or_cancel",
                    "operator_action": "reconcile_or_cancel",
                    "learning": "no_learning",
                },
                "result_summary": "publication cancellation requires external reconciliation",
            }
            if status == "running":
                kwargs.update(
                    owner=str(lease.get("owner") or "") or None,
                    fencing_token=int(lease.get("fencing_token") or 0) or None,
                )
            try:
                await durable_job_repository.transition_job(parent_id, "blocked", **kwargs)
            except Exception:
                # A competing recovery may have won the same CAS.  The
                # authoritative projection is read below and returned.
                pass
            current = await durable_job_repository.get_job(parent_id) or current
            status = str(current.get("status") or status)
        return {
            "job_id": parent_id,
            "step_id": "github_followthrough",
            "child_job_id": child_job_id,
            "m3_job_id": m3_job_id,
            "status": status,
            "reason_code": reason_code,
            "recovery": "reconcile_or_cancel",
            "operator_action": "reconcile_or_cancel",
            "operator_visible": True,
            "learning": "no_learning",
        }

    async def cancel_invocation_job_tree(
        self,
        invocation_job_id: str,
        *,
        routine_id: str,
        owner_principal_id: str,
        owner_session_id: str,
        reason: str,
    ) -> list[dict[str, Any]]:
        """Cancel only one board-selected invocation and its descendants.

        The routine-wide pause/revoke helper intentionally remains separate;
        a board task may cancel one invocation without changing the routine's
        future activation state.
        """

        job = await durable_job_repository.get_job(invocation_job_id)
        if not isinstance(job, Mapping):
            raise RoutineError("routine_invocation_not_found")
        if str(job.get("job_kind") or "") != "routine_invocation":
            raise RoutineError("routine_invocation_binding_mismatch")
        owner = job.get("owner") if isinstance(job.get("owner"), Mapping) else {}
        authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        persisted_session = str(authority.get("session_id") or job.get("operator_session_id") or job.get("session_id") or "")
        if str(owner.get("principal_id") or "") != str(owner_principal_id) or persisted_session != str(owner_session_id):
            raise RoutineError("routine_invocation_owner_mismatch")
        if str(authority.get("routine_id") or "") != str(routine_id):
            raise RoutineError("routine_invocation_binding_mismatch")

        publication = await self._invocation_publication_cancel_binding(
            job,
            invocation_job_id=invocation_job_id,
            routine_id=routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if publication and not publication.get("ok"):
            return [
                await self._block_invocation_cancellation(
                    job,
                    child_job_id=publication.get("child_job_id"),
                    m3_job_id=publication.get("m3_job_id"),
                    reason_code=str(publication.get("reason_code") or "routine_publication_binding_invalid"),
                )
            ]
        if publication and publication.get("m3_job_id"):
            m3_cancellation = await self._cancel_m3_job_safely(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                m3_job_id=str(publication["m3_job_id"]),
            )
            # Only a canonical ``cancelled`` M3 receipt proves that no
            # external effect remains.  ``succeeded`` is intentionally not a
            # safe cancellation outcome: it records an external effect that
            # the operator must reconcile before the invocation can settle.
            if not m3_cancellation.get("ok") or str(m3_cancellation.get("status") or "") != "cancelled":
                reason_code = str(
                    m3_cancellation.get("reason_code")
                    or "m3_external_effect_unresolved"
                )
                return [
                    await self._block_invocation_cancellation(
                        job,
                        child_job_id=str(publication["child_job_id"]),
                        m3_job_id=str(publication["m3_job_id"]),
                        reason_code=reason_code,
                    )
                ]
        return await durable_job_repository.cancel_job_tree(
            invocation_job_id,
            reason=str(reason)[:128],
        )

    async def _persist_or_verify_board_preparation(
        self,
        routine: GuardianRoutine,
        version: GuardianRoutineVersion,
    ) -> tuple[GuardianRoutine, GuardianRoutineVersion]:
        """Complete an exact interrupted board preparation without duplicating it."""

        for _attempt in range(2):
            try:
                async with db_engine.get_session() as db:
                    stored_routine = (
                        await db.execute(select(GuardianRoutine).where(GuardianRoutine.id == routine.id))
                    ).scalars().first()
                    stored_version = (
                        await db.execute(
                            select(GuardianRoutineVersion).where(
                                GuardianRoutineVersion.routine_id == routine.id,
                                GuardianRoutineVersion.version == version.version,
                            )
                        )
                    ).scalars().first()
                    if stored_routine is None and stored_version is None:
                        db.add(routine)
                        db.add(version)
                        await db.flush()
                        stored_routine = routine
                        stored_version = version
                    elif stored_routine is None:
                        raise RoutineError("routine_creation_binding_conflict")
                    elif (
                        str(stored_routine.owner_principal_id or "") != str(routine.owner_principal_id)
                        or str(stored_routine.owner_session_id or "") != str(routine.owner_session_id)
                        or str(stored_routine.name or "") != str(routine.name)
                        or str(stored_routine.state or "") != "prepared"
                        or int(stored_routine.revision or 0) != 1
                        or stored_routine.current_version is not None
                    ):
                        raise RoutineError("routine_creation_binding_conflict")
                    elif stored_version is None:
                        db.add(version)
                        await db.flush()
                        stored_version = version
                    elif (
                        stored_version.source_provenance_json != version.source_provenance_json
                        or stored_version.workflow_sha256 != version.workflow_sha256
                        or stored_version.workflow_bytes != version.workflow_bytes
                        or stored_version.runbook_sha256 != version.runbook_sha256
                        or stored_version.runbook_bytes != version.runbook_bytes
                        or stored_version.source_repository != version.source_repository
                        or stored_version.source_action != version.source_action
                        or stored_version.source_issue_number != version.source_issue_number
                    ):
                        raise RoutineError("routine_creation_binding_conflict")
                    db.expunge(stored_routine)
                    db.expunge(stored_version)
                    return stored_routine, stored_version
            except IntegrityError:
                # Another retry may have committed the exact deterministic
                # rows between our read and insert. Re-read once and verify
                # every field before reusing them.
                continue
        raise RoutineError("routine_creation_binding_conflict")

    async def from_run(
        self,
        req: RoutineFromRunRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        routine_id: str | None = None,
        provenance_extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        provenance, packet = await self._source_proof(
            source_watch_job_id=req.source_watch_job_id,
            source_packet_id=req.source_packet_id,
            source_m3_job_id=req.source_m3_job_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        # Generate the definitive routine id once; the template must carry it.
        # Board-derived procedures supply a server-generated deterministic ID;
        # the legacy /from-run route retains its historical random ID.
        routine_id = str(routine_id or uuid.uuid4().hex).strip()
        try:
            if len(routine_id) != 32:
                raise ValueError
            uuid.UUID(hex=routine_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise RoutineError("routine_id_invalid", status_code=422) from exc
        if provenance_extra:
            safe_extra = _safe_routine_provenance(dict(provenance_extra))
            provenance = {**provenance, **safe_extra}
            provenance = _safe_routine_provenance(provenance)
        workflow = render_workflow(routine_id=routine_id, version=1, name=req.name)
        runbook = render_runbook(routine_id=routine_id, version=1, name=req.name)
        check = validate_generated_files(workflow=workflow, runbook=runbook, routine_id=routine_id, version=1)
        if not check["valid"]:
            raise RoutineError("routine_template_invalid", status_code=500)
        version = GuardianRoutineVersion(
            routine_id=routine_id,
            version=1,
            source_provenance_json=_dump(provenance),
            workflow_bytes=workflow,
            workflow_sha256=_sha(workflow),
            runbook_bytes=runbook,
            runbook_sha256=_sha(runbook),
            source_repository=str(provenance.get("source_repository") or "") or None,
            source_action=str(provenance.get("source_action") or "") or None,
            source_issue_number=(
                int(provenance["source_target"])
                if str(provenance.get("source_action") or "") == "create_comment"
                and str(provenance.get("source_target") or "").isdigit()
                else None
            ),
        )
        routine = GuardianRoutine(
            id=routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            name=req.name.strip(),
            state="prepared",
            revision=1,
            current_version=None,
        )
        routine, version = await self._persist_or_verify_board_preparation(routine, version)
        job_id = f"routine-install:{routine_id}:v1"
        authority = {
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": owner_session_id,
            "goal_owner_principal_id": owner_principal_id,
            "goal_owner_session_id": owner_session_id,
            "routine_id": routine_id,
            "routine_version": 1,
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "source_provenance_sha256": _sha(version.source_provenance_json),
            "source_repository": version.source_repository,
            "source_action": version.source_action,
            "source_target": provenance.get("source_target"),
            "source_packet_id": provenance.get("source_packet_id"),
            "source_dossier_sha256": provenance.get("dossier_sha256"),
            "source_m3_job_id": provenance.get("source_m3_job_id"),
            "capability_id": ROUTINE_CAPABILITY_VERSION,
            "budget_microusd": 0,
        }
        job = await self._admit_user_job(
            job_id=job_id,
            job_kind="routine_install",
            idempotency_key=f"{routine_id}:1",
            inputs={"routine_id": routine_id, "version": 1},
            authority=authority,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=packet.goal_id,
            goal_revision=packet.goal_revision,
            plan_revision=packet.plan_revision,
            candidate_id=None,
        )
        if job.get("status") == "running":
            approval_id = await self._hold_approval(
                job,
                tool_name=ROUTINE_INSTALL_TOOL,
                summary=f"Install reviewed guardian routine {routine_id} version 1",
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        elif job.get("status") == "awaiting_approval":
            install_authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
            approval_id = str(install_authority.get("approval_id") or "")
            if not approval_id:
                raise RoutineError("routine_install_approval_missing")
        else:
            raise RoutineError("routine_install_job_not_recoverable")
        return {
            "status": "prepared",
            "routine": {"id": routine_id, "revision": 1, "state": "prepared", "current_version": None, "name": req.name.strip()},
            "version": self._version_json(version),
            "preview": {"workflow": workflow, "runbook": runbook, "workflow_sha256": version.workflow_sha256, "runbook_sha256": version.runbook_sha256},
            "approval_id": approval_id,
            "install_job_id": job_id,
        }

    async def add_version(self, routine_id: str, req: RoutineVersionRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if routine.revision != req.expected_routine_revision:
            raise RoutineError("routine_revision_stale")
        provenance, packet = await self._source_proof(
            source_watch_job_id=req.source_watch_job_id,
            source_packet_id=req.source_packet_id,
            source_m3_job_id=req.source_m3_job_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        async with db_engine.get_session() as db:
            current = (
                await db.execute(select(GuardianRoutineVersion).where(GuardianRoutineVersion.routine_id == routine_id).order_by(GuardianRoutineVersion.version.desc()))
            ).scalars().first()
            next_version = int(current.version if current else 0) + 1
            workflow = render_workflow(routine_id=routine_id, version=next_version, name=req.name)
            runbook = render_runbook(routine_id=routine_id, version=next_version, name=req.name)
            row = GuardianRoutineVersion(
                routine_id=routine_id,
                version=next_version,
                source_provenance_json=_dump(provenance),
                workflow_bytes=workflow,
                workflow_sha256=_sha(workflow),
                runbook_bytes=runbook,
                runbook_sha256=_sha(runbook),
                source_repository=str(provenance.get("source_repository") or "") or None,
                source_action=str(provenance.get("source_action") or "") or None,
                source_issue_number=(
                    int(provenance["source_target"])
                    if str(provenance.get("source_action") or "") == "create_comment"
                    and str(provenance.get("source_target") or "").isdigit()
                    else None
                ),
            )
            result = await db.execute(
                update(GuardianRoutine)
                .where(
                    GuardianRoutine.id == routine_id,
                    GuardianRoutine.owner_principal_id == owner_principal_id,
                    GuardianRoutine.owner_session_id == owner_session_id,
                    GuardianRoutine.revision == routine.revision,
                    GuardianRoutine.state != "revoked",
                )
                .values(revision=GuardianRoutine.revision + 1, name=req.name.strip(), updated_at=_now())
            )
            if result.rowcount != 1:
                raise RoutineError("routine_revision_stale")
            db.add(row)
            await db.flush()
            db.expunge(row)
        job_id = f"routine-install:{routine_id}:v{next_version}"
        authority = {
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": owner_session_id,
            "goal_owner_principal_id": owner_principal_id,
            "goal_owner_session_id": owner_session_id,
            "routine_id": routine_id,
            "routine_version": next_version,
            "workflow_sha256": row.workflow_sha256,
            "runbook_sha256": row.runbook_sha256,
            "source_provenance_sha256": _sha(row.source_provenance_json),
            "source_repository": row.source_repository,
            "source_action": row.source_action,
            "source_target": provenance.get("source_target"),
            "source_packet_id": provenance.get("source_packet_id"),
            "source_dossier_sha256": provenance.get("dossier_sha256"),
            "source_m3_job_id": provenance.get("source_m3_job_id"),
            "capability_id": ROUTINE_CAPABILITY_VERSION,
            "budget_microusd": 0,
        }
        job = await self._admit_user_job(
            job_id=job_id,
            job_kind="routine_install",
            idempotency_key=f"{routine_id}:{next_version}",
            inputs={"routine_id": routine_id, "version": next_version},
            authority=authority,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=packet.goal_id,
            goal_revision=packet.goal_revision,
            plan_revision=packet.plan_revision,
            candidate_id=None,
        )
        approval_id = await self._hold_approval(
            job,
            tool_name=ROUTINE_INSTALL_TOOL,
            summary=f"Install reviewed guardian routine {routine_id} version {next_version}",
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        return {"status": "prepared", "routine_id": routine_id, "version": self._version_json(row), "preview": {"workflow": workflow, "runbook": runbook}, "approval_id": approval_id, "install_job_id": job_id}

    async def _revalidate_install_selection(
        self,
        job: Mapping[str, Any],
        *,
        routine_id: str,
        version_number: int,
        expected_routine_revision: int,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[GuardianRoutine, GuardianRoutineVersion]:
        """Re-read the canonical routine/package binding at an install fence."""

        current_routine = await self._routine(routine_id, owner_principal_id)
        current_version = await self._version(routine_id, int(version_number))
        self._assert_install_selection(
            job,
            current_routine,
            current_version,
            routine_id=routine_id,
            version_number=version_number,
            expected_routine_revision=expected_routine_revision,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        return current_routine, current_version

    def _assert_install_selection(
        self,
        job: Mapping[str, Any],
        current_routine: GuardianRoutine,
        current_version: GuardianRoutineVersion,
        *,
        routine_id: str,
        version_number: int,
        expected_routine_revision: int,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> None:
        """Validate one already-loaded install selection.

        The same predicate is used by the normal approval reread and by the
        immediate DB transaction that serializes package writes with
        pause/revoke. Keeping it in one helper prevents the locked path from
        silently accepting a weaker binding.
        """

        self._require_routine_owner_session(current_routine, owner_session_id)
        if str(current_routine.owner_principal_id or "") != str(owner_principal_id or ""):
            raise RoutineError("routine_owner_mismatch", status_code=403)
        if str(current_routine.state or "") == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if str(current_routine.state or "") not in {
            "prepared",
            "installed",
            "active",
            "paused",
        }:
            raise RoutineError("routine_install_state_stale")
        if int(current_routine.revision or 0) != int(expected_routine_revision):
            raise RoutineError("routine_revision_stale")
        if int(current_version.version) != int(version_number):
            raise RoutineError("routine_install_binding_stale")
        if str(current_version.routine_id or "") != str(routine_id):
            raise RoutineError("routine_install_binding_stale")
        authority = job.get("declared_authority")
        authority = authority if isinstance(authority, Mapping) else {}
        try:
            authority_version = int(authority.get("routine_version") or 0)
        except (TypeError, ValueError, OverflowError):
            authority_version = 0
        if (
            str(authority.get("routine_id") or "") != str(routine_id)
            or authority_version != int(version_number)
            or str(authority.get("workflow_sha256") or "")
            != str(current_version.workflow_sha256 or "")
            or str(authority.get("runbook_sha256") or "")
            != str(current_version.runbook_sha256 or "")
            or str(authority.get("source_provenance_sha256") or "")
            != _sha(current_version.source_provenance_json)
        ):
            raise RoutineError("routine_install_binding_stale")
        if current_version.installed_package_digest:
            raise RoutineError("routine_install_package_already_bound")

    @staticmethod
    def _assert_install_job_lease(
        durable_job: WorkflowRunState,
        claimed_job: Mapping[str, Any],
    ) -> None:
        """Require the durable install claim before changing the selector.

        The approval/claim response is a projection that can become stale
        while the installer is preparing local bytes.  The canonical job row
        is therefore re-read inside the same writer transaction as the
        routine selector update.  A reclaimed or expired fence cannot commit
        an installed package even when its caller retained an old claim.
        """

        if str(getattr(durable_job, "status", "") or "") != "running":
            raise RoutineError("routine_install_job_fence_stale")
        claimed_lease = claimed_job.get("lease")
        claimed_lease = claimed_lease if isinstance(claimed_lease, Mapping) else {}
        claimed_owner = str(claimed_lease.get("owner") or "")
        try:
            claimed_fence = int(claimed_lease.get("fencing_token") or 0)
            durable_fence = int(getattr(durable_job, "fencing_token", 0) or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RoutineError("routine_install_job_fence_stale") from exc
        if (
            not claimed_owner
            or claimed_fence <= 0
            or str(getattr(durable_job, "lease_owner", "") or "") != claimed_owner
            or durable_fence != claimed_fence
        ):
            raise RoutineError("routine_install_job_fence_stale")
        expires_at = getattr(durable_job, "lease_expires_at", None)
        if isinstance(expires_at, str) and expires_at.strip():
            try:
                expires_at = datetime.fromisoformat(expires_at.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise RoutineError("routine_install_job_fence_stale") from exc
        if not isinstance(expires_at, datetime):
            raise RoutineError("routine_install_job_fence_stale")
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= _now():
            raise RoutineError("routine_install_job_fence_stale")

    async def _resume_approval(
        self,
        job: Mapping[str, Any],
        approval_id: str,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        routine_id: str | None = None,
        version_number: int | None = None,
        expected_routine_revision: int | None = None,
    ) -> dict[str, Any]:
        approval = await approval_repository.get(approval_id)
        if approval is None or approval.status != "approved":
            raise RoutineError("approval_not_current")
        if (
            str(getattr(approval, "owner_principal_id", None) or "") != str(owner_principal_id or "")
            or str(getattr(approval, "operator_session_id", None) or "") != str(owner_session_id or "")
        ):
            raise RoutineError("approval_owner_session_mismatch", status_code=403)
        details = _load(approval.details_json, {})
        if not isinstance(details, Mapping) or str(details.get("durable_job_id") or "") != str(job.get("job_id")):
            raise RoutineError("approval_job_binding_mismatch")
        expires_at = float(details.get("approval_expires_at") or details.get("expires_at") or 0)
        if expires_at <= _now().timestamp():
            raise RoutineError("approval_expired")
        if (
            routine_id is not None
            and version_number is not None
            and expected_routine_revision is not None
        ):
            # This is the final canonical read before consuming approval. A
            # revoke or package selection change therefore cannot authorize
            # the install or enter its file-write phase.
            await self._revalidate_install_selection(
                job,
                routine_id=routine_id,
                version_number=version_number,
                expected_routine_revision=expected_routine_revision,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        receipt = {
            "status": "approved",
            "authenticated": True,
            "approval_id": approval_id,
            "operator_principal_id": owner_principal_id,
            "operator_session_id": owner_session_id,
            "owner_kind": "user",
            "owner_principal_id": owner_principal_id,
            "service_id": None,
            "authority_digest": job.get("authority_digest"),
            "goal_id": job.get("goal_id"),
            "goal_revision": job.get("goal_revision"),
            "plan_revision": job.get("plan_revision"),
            "capability_version": job.get("capability_version"),
            "budget_microusd": 0,
            "budget_digest": job.get("budget_digest"),
            "expires_at": expires_at,
        }
        return await durable_job_repository.resume_approved_job(
            str(job["job_id"]),
            approval_receipt=receipt,
            approval_id=approval_id,
            authority_digest=str(job.get("authority_digest") or ""),
            goal_id=job.get("goal_id"),
            goal_revision=job.get("goal_revision"),
            plan_revision=job.get("plan_revision"),
            capability_version=str(job.get("capability_version") or ROUTINE_CAPABILITY_VERSION),
            owner_kind="user",
            owner_principal_id=owner_principal_id,
            service_id=None,
            budget_microusd=0,
            budget_digest=str(job.get("budget_digest") or ""),
            operator_principal_id=owner_principal_id,
            operator_session_id=owner_session_id,
            expires_at=expires_at,
            expected_revision=int(job.get("revision") or 0),
        )

    async def _record_install_receipts(
        self,
        *,
        job_id: str,
        package_files: tuple[tuple[Path, str, str], ...],
        package_digest: str,
        owner: str,
        fencing_token: int,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Fill missing local install receipts using stable identities."""

        current = await durable_job_repository.get_job(job_id)
        if current is None:
            raise RoutineError("routine_install_job_missing")
        revision = int(current.get("revision") or expected_revision)
        artifacts = current.get("artifacts") if isinstance(current.get("artifacts"), list) else []
        effects = current.get("effects") if isinstance(current.get("effects"), list) else []
        for path, content, kind in package_files:
            target_path = str(path.relative_to(settings.workspace_dir))
            content_digest = _sha(content)
            matching_artifacts = [
                item
                for item in artifacts
                if isinstance(item, Mapping)
                and str(item.get("file_path") or "") == target_path
                and str(item.get("artifact_type") or "") == str(kind)
            ]
            if any(
                str(item.get("content_sha256") or "")
                and str(item.get("content_sha256")) != content_digest
                for item in matching_artifacts
            ):
                raise RoutineError("routine_install_artifact_conflict")
            if not any(
                str(item.get("content_sha256") or "") == content_digest
                for item in matching_artifacts
            ):
                current = await durable_job_repository.record_artifact(
                    job_id,
                    file_path=target_path,
                    artifact_type=kind,
                    content=content,
                    owner=owner,
                    fencing_token=fencing_token,
                    expected_revision=revision,
                )
                revision = int(current.get("revision") or revision)
                artifacts = current.get("artifacts") if isinstance(current.get("artifacts"), list) else []

            expected_effect_id = (
                "routine-install:"
                + _sha(f"{job_id}|{target_path}|{kind}|{content_digest}")[:32]
            )
            matching_effects = [
                item
                for item in effects
                if isinstance(item, Mapping)
                and str(item.get("effect_type") or "") == "routine_install"
                and str(item.get("target_path") or "") == target_path
                and str(item.get("target_digest") or "") == content_digest
            ]
            unresolved = next(
                (
                    item
                    for item in matching_effects
                    if str(item.get("status") or "") in UNRESOLVED_EFFECT_STATUSES
                ),
                None,
            )
            if unresolved is not None:
                raise RoutineError("routine_install_effect_reconciliation_required")
            effect = next(
                (
                    item
                    for item in matching_effects
                    if str(item.get("status") or "") == "succeeded"
                ),
                None,
            )
            effect_id = str((effect or {}).get("effect_id") or expected_effect_id)
            if effect is None:
                current = await durable_job_repository.record_effect(
                    job_id,
                    effect_type="routine_install",
                    effect_id=effect_id,
                    target_path=target_path,
                    target_digest=content_digest,
                    content_sha256=content_digest,
                    status="succeeded",
                    details={"verified": True, "package_digest": package_digest},
                    owner=owner,
                    fencing_token=fencing_token,
                    expected_revision=revision,
                )
                revision = int(current.get("revision") or revision)
                effects = current.get("effects") if isinstance(current.get("effects"), list) else []
                effect = next(
                    (
                        item
                        for item in effects
                        if isinstance(item, Mapping)
                        and str(item.get("effect_id") or "") == effect_id
                    ),
                    effect,
                )

            readback = next(
                (
                    item
                    for item in effects
                    if isinstance(item, Mapping)
                    and str(item.get("effect_id") or "") == effect_id
                    and str(item.get("receipt_kind") or "") == "readback"
                    and str(item.get("status") or "") == "succeeded"
                    and isinstance(item.get("details"), Mapping)
                    and item["details"].get("verified") is True
                ),
                None,
            )
            if readback is None:
                current = await durable_job_repository.record_readback(
                    job_id,
                    target_path=target_path,
                    effect_id=effect_id,
                    effect_type="routine_install",
                    target_digest=content_digest,
                    content_sha256=content_digest,
                    status="succeeded",
                    details={"verified": True, "package_digest": package_digest},
                    owner=owner,
                    fencing_token=fencing_token,
                    expected_revision=revision,
                )
                revision = int(current.get("revision") or revision)
                effects = current.get("effects") if isinstance(current.get("effects"), list) else []
        return current

    async def _reconcile_committed_install(
        self,
        routine: GuardianRoutine,
        version: GuardianRoutineVersion,
        *,
        job_id: str,
        job: Mapping[str, Any],
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any] | None:
        """Close the durable install receipt after the package commit.

        Package rows and the durable job live in separate stores.  A worker can
        die after the package transaction and before its terminal transition.
        Repeated install calls use the verified package readback as the proof
        for a narrow, idempotent local-finalization retry.
        """

        # The caller may still hold detached pre-commit ORM objects.  Reload
        # both records so a process failure immediately after the package
        # transaction can be reconciled in the same request as well as on a
        # later retry.
        try:
            latest_routine = await self._routine(str(routine.id), owner_principal_id)
            latest_version = await self._version(str(routine.id), int(version.version))
        except RoutineError:
            return None
        routine = latest_routine
        version = latest_version
        durable_authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        try:
            durable_version = int(durable_authority.get("routine_version") or 0)
        except (TypeError, ValueError):
            return None
        if (
            str(job.get("job_kind") or "") != "routine_install"
            or str(durable_authority.get("routine_id") or "") != str(routine.id)
            or durable_version != int(version.version)
        ):
            return None
        package_digest = str(version.installed_package_digest or "")
        if not package_digest or routine.state not in {"installed", "active", "paused"}:
            return None
        workflow_name = f"{routine_slug(str(routine.id), int(version.version))}.md"
        runbook_name = f"{routine_slug(str(routine.id), int(version.version))}.yaml"
        staging_root = _routine_install_staging_root(str(routine.id), int(version.version))
        result = {
            "package_digest": package_digest,
            "routine_id": str(routine.id),
            "version": int(version.version),
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "learning": "no_learning",
        }

        current = await durable_job_repository.get_job(job_id) or dict(job)
        if current.get("status") == "succeeded":
            package = self._package_readback(
                owner_principal_id,
                owner_session_id,
                str(routine.id),
                int(version.version),
                package_digest,
            )
            if package.get("digest") != package_digest:
                return None
            _cleanup_install_staging_root(staging_root)
            return await self.read(
                str(routine.id),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        # Recover an expired worker fence before taking a new local-finalize
        # lease.  A live competing lease is deliberately left pending.
        if current.get("status") == "running":
            try:
                recovered = await durable_job_repository.recover_stale_job(job_id)
            except Exception:
                # A targeted recovery failure is commonly the durable
                # repository reporting that another worker still owns a live
                # lease.  Re-reading the row is useful for a concurrent
                # terminal/block transition, but a still-running row must
                # never be treated as our lease for receipt writes.
                current = await durable_job_repository.get_job(job_id) or current
                if current.get("status") == "running":
                    return None
            else:
                current = recovered
                # ``recover_stale_job`` must return a non-running row before
                # this reconciler can resume and claim a fresh fence.  A
                # running result is fail-closed even if a repository adapter
                # reports it as a no-op.
                if current.get("status") == "running":
                    return None
        if current.get("status") == "blocked":
            try:
                current = await durable_job_repository.resume_job(
                    job_id,
                    expected_revision=current.get("revision"),
                    reason="routine_install_local_finalize_retry",
                )
            except Exception:
                current = await durable_job_repository.get_job(job_id) or current
                if current.get("status") == "running":
                    return None
        if current.get("status") == "accepted":
            try:
                current = await durable_job_repository.queue_job(
                    job_id,
                    expected_revision=current.get("revision"),
                    reason="routine_install_local_finalize_retry",
                )
            except Exception:
                current = await durable_job_repository.get_job(job_id) or current
                if current.get("status") == "running":
                    return None
        claimed_reconciliation_lease = False
        if current.get("status") == "queued":
            try:
                current = await durable_job_repository.claim_job(
                    job_id,
                    owner=f"routine:{job_id}:reconcile",
                    expected_revision=current.get("revision"),
                    expected_fencing_token=(current.get("lease") or {}).get("fencing_token"),
                    lease_seconds=ROUTINE_DEADLINE_SECONDS,
                )
                claimed_reconciliation_lease = current.get("status") == "running"
            except Exception:
                current = await durable_job_repository.get_job(job_id) or current
                if current.get("status") == "running":
                    return None
        if current.get("status") == "succeeded":
            package = self._package_readback(
                owner_principal_id,
                owner_session_id,
                str(routine.id),
                int(version.version),
                package_digest,
            )
            if package.get("digest") != package_digest:
                return None
            _cleanup_install_staging_root(staging_root)
            return await self.read(
                str(routine.id),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        if current.get("status") != "running" or not claimed_reconciliation_lease:
            # Receipt writes require the fence returned by our own claim.  A
            # pre-existing running row belongs to a competing worker and is
            # deliberately left pending for that worker or later recovery.
            return None
        lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
        lease_owner = str(lease.get("owner") or "")
        fencing_token = int(lease.get("fencing_token") or 0)
        if not lease_owner or fencing_token <= 0:
            return None
        # Recovery must own the current durable install fence before it writes
        # any discoverable package files. The publisher rechecks this exact
        # claim under the same database lock as its filesystem writes.
        try:
            published = await self._publish_staged_install_under_routine_lock(
                job=current,
                routine_id=str(routine.id),
                version_number=int(version.version),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                staging_root=staging_root,
                package_digest=package_digest,
                workflow_name=workflow_name,
                runbook_name=runbook_name,
            )
        except Exception:
            return None
        package = self._package_readback(
            owner_principal_id,
            owner_session_id,
            str(routine.id),
            int(version.version),
            package_digest,
        )
        if package.get("digest") != package_digest:
            return None
        package_files = tuple(published.get("package_files") or ())
        if not package_files:
            return None
        try:
            current = await self._record_install_receipts(
                job_id=job_id,
                package_files=package_files,
                package_digest=package_digest,
                owner=lease_owner,
                fencing_token=fencing_token,
                expected_revision=int(current.get("revision") or 0),
            )
            lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else lease
            lease_owner = str(lease.get("owner") or lease_owner)
            fencing_token = int(lease.get("fencing_token") or fencing_token)
            settled = await durable_job_repository.transition_job(
                job_id,
                "succeeded",
                owner=lease_owner,
                fencing_token=fencing_token,
                expected_revision=current.get("revision"),
                result=result,
                result_summary="routine package commit reconciled into its durable install receipt",
            )
        except Exception:
            # Keep the private staging tree and the canonical install selector
            # intact. A later retry can fill only the missing receipt(s).
            return None
        if settled.get("status") == "succeeded":
            _cleanup_install_staging_root(staging_root)
            return await self.read(
                str(routine.id),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        raise RoutineError(
            "routine_install_local_finalize_pending",
            "the package is committed, but its durable install receipt is still pending recovery",
        )

    async def recover_pending_installs(self) -> list[dict[str, Any]]:
        """Recover or remove private install files left by a process crash.

        The installer writes only to ``ROUTINE_INSTALL_STAGING_ROOT`` while its
        canonical transaction is open.  A startup scan can therefore delete a
        staging tree when no installed digest was committed, or finish
        publication when the canonical version row proves that the commit won.
        Any database, path, or lifecycle ambiguity is retained and reported as
        blocked so recovery never guesses whether a procedure is executable.
        """

        entries = _staged_install_entries()
        if not entries:
            return []
        try:
            async with db_engine.get_session() as db:
                routine_rows = (await db.execute(select(GuardianRoutine))).scalars().all()
                version_rows = (await db.execute(select(GuardianRoutineVersion))).scalars().all()
                for row in (*routine_rows, *version_rows):
                    try:
                        db.expunge(row)
                    except Exception:
                        # The session uses expire_on_commit=False in the
                        # canonical engine. A test or alternate session may
                        # omit expunge; detached access is still safe below.
                        pass
        except Exception:
            # A failed canonical read is not evidence that the DB rows are
            # absent. Keep every staging tree for a later operator-visible
            # retry rather than deleting a possibly committed install.
            return [
                {
                    "status": "blocked",
                    "reason_code": "routine_install_recovery_db_unavailable",
                    "staging_count": len(entries),
                }
            ]

        routines_by_token: dict[str, GuardianRoutine] = {}
        for row in routine_rows:
            try:
                routines_by_token[_routine_pack_token(str(row.id))] = row
            except RoutineError:
                continue
        versions_by_key: dict[tuple[str, int], GuardianRoutineVersion] = {}
        for row in version_rows:
            try:
                versions_by_key[
                    (_routine_pack_token(str(row.routine_id)), int(row.version))
                ] = row
            except (RoutineError, TypeError, ValueError):
                continue

        receipts: list[dict[str, Any]] = []
        for token, version_number, staging_root in entries:
            routine = routines_by_token.get(token)
            version = versions_by_key.get((token, version_number))
            if routine is None or version is None:
                # No canonical selector/version exists. This can only be an
                # abandoned pre-admission tree after the successful DB read.
                _cleanup_install_staging_root(staging_root)
                receipts.append(
                    {
                        "status": "cleaned",
                        "reason_code": "routine_install_orphan_staging",
                        "routine_token": token,
                        "version": version_number,
                    }
                )
                continue

            installed_digest = str(version.installed_package_digest or "")
            if not installed_digest:
                # The DB transaction did not commit. Staged workflow/package
                # bytes are never in a discoverable extension root.
                _cleanup_install_staging_root(staging_root)
                receipts.append(
                    {
                        "status": "cleaned",
                        "reason_code": "routine_install_uncommitted_staging",
                        "routine_id": str(routine.id),
                        "version": version_number,
                    }
                )
                continue

            if (
                str(routine.state or "") not in {"installed", "active", "paused"}
                or int(routine.current_version or 0) != version_number
            ):
                receipts.append(
                    {
                        "status": "blocked",
                        "reason_code": "routine_install_lifecycle_mismatch",
                        "routine_id": str(routine.id),
                        "version": version_number,
                    }
                )
                continue

            job_id = f"routine-install:{routine.id}:v{version_number}"
            try:
                install_job = await durable_job_repository.get_job(job_id)
            except Exception:
                install_job = None
            if install_job is None:
                receipts.append(
                    {
                        "status": "blocked",
                        "reason_code": "routine_install_job_missing",
                        "routine_id": str(routine.id),
                        "version": version_number,
                    }
                )
                continue
            reconciled = await self._reconcile_committed_install(
                routine,
                version,
                job_id=job_id,
                job=install_job,
                owner_principal_id=str(routine.owner_principal_id),
                owner_session_id=str(routine.owner_session_id),
            )
            if reconciled is not None:
                receipts.append(
                    {
                        "status": "recovered",
                        "reason_code": "routine_install_publication_reconciled",
                        "routine_id": str(routine.id),
                        "version": version_number,
                    }
                )
            else:
                receipts.append(
                    {
                        "status": "blocked",
                        "reason_code": "routine_install_publication_pending",
                        "routine_id": str(routine.id),
                        "version": version_number,
                    }
                )
        return receipts

    def _verified_public_install(
        self,
        routine: GuardianRoutine,
        version: GuardianRoutineVersion,
        *,
        package_digest: str,
        workflow_name: str,
        runbook_name: str,
    ) -> dict[str, Any]:
        """Read back every discoverable install file without writing it."""

        workspace_root = workspace_capability_package_root()
        workflow_path = workspace_root / "workflows" / workflow_name
        runbook_path = workspace_root / "runbooks" / runbook_name
        for path, content, expected_sha in (
            (workflow_path, version.workflow_bytes, version.workflow_sha256),
            (runbook_path, version.runbook_bytes, version.runbook_sha256),
        ):
            if path.is_symlink() or not path.is_file():
                raise RoutineError("routine_install_publication_missing")
            stored, truncated = _read_workspace_text_bounded(path, max_bytes=128 * 1024)
            if truncated or stored != content or _sha(stored) != expected_sha:
                raise RoutineError("routine_install_publication_mismatch")

        active = self._materialize_routine_package(
            str(routine.id),
            version,
            allow_create=False,
        )
        if str(active.get("digest") or "") != str(package_digest):
            raise RoutineError("routine_install_publication_mismatch")
        return {
            "package_digest": str(active["digest"]),
            "package_files": (
                (workflow_path, version.workflow_bytes, "routine_workflow"),
                (runbook_path, version.runbook_bytes, "routine_runbook"),
                (active["root"] / "manifest.yaml", str(active["manifest_content"]), "routine_pack_manifest"),
                (active["root"] / ROUTINE_PACK_RUNBOOK_REFERENCE, str(active["runbook_content"]), "routine_pack_runbook"),
            ),
        }

    async def _publish_staged_install_under_routine_lock(
        self,
        *,
        job: Mapping[str, Any],
        routine_id: str,
        version_number: int,
        owner_principal_id: str,
        owner_session_id: str,
        staging_root: Path,
        package_digest: str,
        workflow_name: str,
        runbook_name: str,
    ) -> dict[str, Any]:
        """Publish under both the current install fence and routine writer lock."""

        async with db_engine.get_session() as db:
            bind = db.get_bind()
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
            if dialect_name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            job_id = str(job.get("job_id") or "")
            if not job_id:
                raise RoutineError("routine_install_job_missing")
            job_statement = select(WorkflowRunState).where(
                WorkflowRunState.run_identity == job_id,
            )
            if dialect_name != "sqlite":
                job_statement = job_statement.with_for_update()
            durable_job = (await db.execute(job_statement)).scalars().first()
            if durable_job is None:
                raise RoutineError("routine_install_job_missing")
            self._assert_install_job_lease(durable_job, job)
            routine_statement = select(GuardianRoutine).where(
                GuardianRoutine.id == routine_id,
                GuardianRoutine.owner_principal_id == owner_principal_id,
                GuardianRoutine.owner_session_id == owner_session_id,
            )
            version_statement = select(GuardianRoutineVersion).where(
                GuardianRoutineVersion.routine_id == routine_id,
                GuardianRoutineVersion.version == int(version_number),
            )
            if dialect_name != "sqlite":
                routine_statement = routine_statement.with_for_update()
                version_statement = version_statement.with_for_update()
            routine = (await db.execute(routine_statement)).scalars().first()
            version = (await db.execute(version_statement)).scalars().first()
            if routine is None:
                raise RoutineError("routine_not_found", status_code=404)
            if version is None:
                raise RoutineError("routine_version_not_found", status_code=404)
            if str(routine.owner_principal_id or "") != str(owner_principal_id or ""):
                raise RoutineError("routine_owner_mismatch", status_code=403)
            self._require_routine_owner_session(routine, owner_session_id)
            if str(routine.state or "") not in {"installed", "active", "paused"}:
                raise RoutineError("routine_install_publication_stale")
            if int(routine.current_version or 0) != int(version_number):
                raise RoutineError("routine_install_publication_stale")
            if str(version.installed_package_digest or "") != str(package_digest):
                raise RoutineError("routine_install_publication_binding_stale")

            active_package = _routine_pack_root(routine_id, int(version_number))
            public_snapshot = _snapshot_install_files(
                [
                    workspace_capability_package_root() / "manifest.yaml",
                    workspace_capability_package_root() / "workflows" / workflow_name,
                    workspace_capability_package_root() / "runbooks" / runbook_name,
                    active_package / "manifest.yaml",
                    active_package / ROUTINE_PACK_RUNBOOK_REFERENCE,
                ]
            )
            active_package_existed = active_package.exists()
            try:
                published = self._publish_staged_install(
                    routine,
                    version,
                    staging_root=staging_root,
                    package_digest=package_digest,
                    workflow_name=workflow_name,
                    runbook_name=runbook_name,
                )
            except Exception:
                preserve_active_package = False
                if (
                    not active_package_existed
                    and active_package.is_dir()
                    and not active_package.is_symlink()
                ):
                    try:
                        # ``replace`` below is atomic and intentionally moves
                        # the private package into the discoverable root. If
                        # verification fails after that move, put the exact
                        # package back under private staging so a retry can
                        # validate and publish it again. A process crash after
                        # the move is also safe: startup can verify the exact
                        # active package when staging is absent.
                        staged_package = staging_root / "package"
                        if not staged_package.exists():
                            staged_package.parent.mkdir(parents=True, exist_ok=True)
                            active_package.replace(staged_package)
                        else:
                            shutil.rmtree(active_package)
                    except OSError:
                        # Preserve the public package for startup readback if
                        # the filesystem cannot move it back. Deleting it
                        # would destroy the only durable copy and make a
                        # committed install unrecoverable.
                        preserve_active_package = not staged_package.exists()
                if preserve_active_package:
                    package_paths = {
                        active_package / "manifest.yaml",
                        active_package / ROUTINE_PACK_RUNBOOK_REFERENCE,
                    }
                    _restore_install_files(
                        {
                            path: original
                            for path, original in public_snapshot.items()
                            if path not in package_paths
                        }
                    )
                else:
                    # Move the package back into private staging before
                    # restoring the public snapshot. The snapshot records the
                    # active package files as absent; restoring it first would
                    # strip those files from the only intact package copy.
                    _restore_install_files(public_snapshot)
                raise
            # File publication can outlast a short lease. Recheck before the
            # transaction releases its lock so a stale installer cannot make
            # an unowned package discoverable.
            self._assert_install_job_lease(durable_job, job)
            return published

    def _publish_staged_install(
        self,
        routine: GuardianRoutine,
        version: GuardianRoutineVersion,
        *,
        staging_root: Path,
        package_digest: str,
        workflow_name: str,
        runbook_name: str,
    ) -> dict[str, Any]:
        """Publish a committed private install into the discoverable roots."""

        if not staging_root.exists():
            # Older recovery records may have already removed the private
            # staging tree after publication. Reconcile only when every public
            # file still matches the canonical version and digest.
            return self._verified_public_install(
                routine,
                version,
                package_digest=package_digest,
                workflow_name=workflow_name,
                runbook_name=runbook_name,
            )
        if staging_root.is_symlink() or not staging_root.is_dir():
            raise RoutineError("routine_install_staging_missing")
        stage_workflow = staging_root / "workspace" / "workflows" / workflow_name
        stage_runbook = staging_root / "workspace" / "runbooks" / runbook_name
        for path, content, expected_sha in (
            (stage_workflow, version.workflow_bytes, version.workflow_sha256),
            (stage_runbook, version.runbook_bytes, version.runbook_sha256),
        ):
            if path.is_symlink() or not path.is_file():
                raise RoutineError("routine_install_staging_missing")
            stored, truncated = _read_workspace_text_bounded(path, max_bytes=128 * 1024)
            if truncated or stored != content or _sha(stored) != expected_sha:
                raise RoutineError("routine_install_staging_mismatch")
        stage_package = staging_root / "package"
        active_package = _routine_pack_root(str(routine.id), int(version.version))
        if stage_package.is_symlink():
            raise RoutineError("routine_install_staging_missing")
        if not stage_package.exists():
            # ``replace`` is atomic, so a process can die after moving the
            # private package into its discoverable root while leaving the
            # staging workspace and durable install job behind.  Validate
            # that exact active package against the canonical digest and
            # resume from its readback; never recreate or guess a package on
            # a missing-source path.
            if active_package.is_symlink() or not active_package.is_dir():
                raise RoutineError("routine_install_staging_missing")
            active = self._materialize_routine_package(
                str(routine.id),
                version,
                allow_create=False,
            )
            if str(active.get("digest") or "") != str(package_digest):
                raise RoutineError("routine_install_staging_mismatch")
            return self._verified_public_install(
                routine,
                version,
                package_digest=package_digest,
                workflow_name=workflow_name,
                runbook_name=runbook_name,
            )
        if not stage_package.is_dir():
            raise RoutineError("routine_install_staging_missing")
        staged = self._materialize_routine_package(
            str(routine.id),
            version,
            allow_create=False,
            root_override=stage_package,
        )
        if str(staged.get("digest") or "") != str(package_digest):
            raise RoutineError("routine_install_staging_mismatch")

        # Validate every private byte before publishing any discoverable
        # workflow or package entry. A crash before the canonical DB commit
        # therefore cannot leave a partial active procedure behind.
        workflow_path = save_workspace_contribution(
            "workflows",
            file_name=workflow_name,
            content=version.workflow_bytes,
        )
        runbook_path = save_workspace_contribution(
            "runbooks",
            file_name=runbook_name,
            content=version.runbook_bytes,
        )
        if active_package.exists():
            if active_package.is_symlink() or not active_package.is_dir():
                raise RoutineError("routine_package_mutation_detected", status_code=409)
            existing = self._materialize_routine_package(
                str(routine.id),
                version,
                allow_create=False,
            )
            if str(existing.get("digest") or "") != str(package_digest):
                raise RoutineError("routine_package_mutation_detected", status_code=409)
        else:
            active_package.parent.mkdir(parents=True, exist_ok=True)
            stage_package.replace(active_package)
        # Keep the staging tree until durable artifact/effect/readback receipts
        # and the terminal job transition succeed. A crash in that interval can
        # then verify or republish the exact public bytes without guessing.
        return self._verified_public_install(
            routine,
            version,
            package_digest=package_digest,
            workflow_name=workflow_name,
            runbook_name=runbook_name,
        )

    async def _install_package_under_routine_lock(
        self,
        *,
        job: Mapping[str, Any],
        routine_id: str,
        version_number: int,
        expected_routine_revision: int,
        owner_principal_id: str,
        owner_session_id: str,
        workflow_name: str,
        runbook_name: str,
    ) -> dict[str, Any]:
        """Write and commit one package while fencing pause/revoke.

        SQLite's immediate writer transaction and a server database's row
        locks cover the canonical selector read, bounded local package writes,
        and installed-state CAS.  Pause/revoke therefore either wins before
        this section (and no file is written) or waits until the package
        commit is durable. Durable job receipts are intentionally recorded
        by the caller after this transaction releases its lock.
        """

        staging_root = _routine_install_staging_root(routine_id, int(version_number))
        package_root = staging_root / "package"
        workflow_target = staging_root / "workspace" / "workflows" / workflow_name
        runbook_target = staging_root / "workspace" / "runbooks" / runbook_name
        file_snapshot = _snapshot_install_files(
            [
                workflow_target,
                runbook_target,
                package_root / "manifest.yaml",
                package_root / ROUTINE_PACK_RUNBOOK_REFERENCE,
            ]
        )
        committed = False
        try:
            async with db_engine.get_session() as db:
                bind = db.get_bind()
                dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
                if dialect_name == "sqlite":
                    # A deferred transaction would allow pause/revoke to
                    # commit after our read but before the first local write.
                    await db.execute(text("BEGIN IMMEDIATE"))

                routine_statement = select(GuardianRoutine).where(
                    GuardianRoutine.id == routine_id,
                    GuardianRoutine.owner_principal_id == owner_principal_id,
                    GuardianRoutine.owner_session_id == owner_session_id,
                )
                version_statement = select(GuardianRoutineVersion).where(
                    GuardianRoutineVersion.routine_id == routine_id,
                    GuardianRoutineVersion.version == int(version_number),
                )
                if dialect_name != "sqlite":
                    routine_statement = routine_statement.with_for_update()
                    version_statement = version_statement.with_for_update()
                routine = (await db.execute(routine_statement)).scalars().first()
                version = (await db.execute(version_statement)).scalars().first()
                if routine is None:
                    raise RoutineError("routine_not_found", status_code=404)
                if version is None:
                    raise RoutineError("routine_version_not_found", status_code=404)
                job_id = str(job.get("job_id") or "")
                if not job_id:
                    raise RoutineError("routine_install_job_missing")
                job_statement = select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == job_id,
                )
                if dialect_name != "sqlite":
                    job_statement = job_statement.with_for_update()
                durable_job = (await db.execute(job_statement)).scalars().first()
                if durable_job is None:
                    raise RoutineError("routine_install_job_missing")
                self._assert_install_job_lease(durable_job, job)
                self._assert_install_selection(
                    job,
                    routine,
                    version,
                    routine_id=routine_id,
                    version_number=int(version_number),
                    expected_routine_revision=expected_routine_revision,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )

                _create_once_workspace_text(workflow_target, version.workflow_bytes)
                _create_once_workspace_text(runbook_target, version.runbook_bytes)
                workflow_path = workflow_target
                runbook_path = runbook_target
                for path, content, expected_sha in (
                    (workflow_path, version.workflow_bytes, version.workflow_sha256),
                    (runbook_path, version.runbook_bytes, version.runbook_sha256),
                ):
                    stored, truncated = _read_workspace_text_bounded(
                        path,
                        max_bytes=128 * 1024,
                    )
                    if truncated or stored != content or _sha(stored) != expected_sha:
                        raise RoutineError("routine_install_readback_mismatch")
                # v2 versions carry a fixed template-specific package schema;
                # v1 keeps the legacy two-step guardian validation.  The
                # install fence already verified the immutable version, so
                # choose the validator from that persisted provenance rather
                # than silently applying the v1 parser to a v2 package.
                provenance = _load(version.source_provenance_json, {})
                template_id = (
                    str(provenance.get("template_id") or "")
                    if isinstance(provenance, Mapping)
                    and provenance.get("schema_version") == 2
                    else None
                )
                validation = validate_generated_files(
                    workflow=version.workflow_bytes,
                    runbook=version.runbook_bytes,
                    routine_id=routine_id,
                    version=version.version,
                    template_id=template_id,
                )
                if not validation.get("valid"):
                    raise RoutineError("routine_install_parse_failed")

                materialized = self._materialize_routine_package(
                    routine_id,
                    version,
                    allow_create=True,
                    root_override=package_root,
                )
                package_digest = str(materialized["digest"])
                package_files = (
                    (
                        workflow_path,
                        version.workflow_bytes,
                        "routine_workflow",
                    ),
                    (
                        runbook_path,
                        version.runbook_bytes,
                        "routine_runbook",
                    ),
                    (
                        materialized["root"] / "manifest.yaml",
                        str(materialized["manifest_content"]),
                        "routine_pack_manifest",
                    ),
                    (
                        materialized["root"] / ROUTINE_PACK_RUNBOOK_REFERENCE,
                        str(materialized["runbook_content"]),
                        "routine_pack_runbook",
                    ),
                )
                # The local materialization above can take enough time for a
                # lease to expire.  Recheck the same locked row immediately
                # before the canonical selector update so an expired worker
                # cannot commit an install that it no longer owns.
                self._assert_install_job_lease(durable_job, job)
                version_update = await db.execute(
                    update(GuardianRoutineVersion)
                    .where(
                        GuardianRoutineVersion.id == version.id,
                        GuardianRoutineVersion.installed_package_digest.is_(None),
                    )
                    .values(
                        installed_package_digest=package_digest,
                        installed_at=_now(),
                    )
                )
                routine_update = await db.execute(
                    update(GuardianRoutine)
                    .where(
                        GuardianRoutine.id == routine_id,
                        GuardianRoutine.owner_principal_id == owner_principal_id,
                        GuardianRoutine.owner_session_id == owner_session_id,
                        GuardianRoutine.revision == int(expected_routine_revision),
                        GuardianRoutine.state != "revoked",
                    )
                    .values(
                        state="installed",
                        current_version=version.version,
                        revision=GuardianRoutine.revision + 1,
                        updated_at=_now(),
                    )
                )
                if version_update.rowcount != 1 or routine_update.rowcount != 1:
                    raise RoutineError("routine_install_revision_stale")
            committed = True
            return {
                "routine": routine,
                "version": version,
                "package_digest": package_digest,
                "package_files": package_files,
                "staging_root": staging_root,
            }
        except Exception:
            if not committed:
                _restore_install_files(file_snapshot)
            raise

    async def install(self, routine_id: str, req: RoutineInstallRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        version = await self._version(routine_id, req.version)
        provenance = _load(version.source_provenance_json, {})
        job_id = f"routine-install:{routine_id}:v{version.version}"
        job = await durable_job_repository.get_job(job_id)
        if job is None:
            raise RoutineError("routine_install_job_missing")
        if routine.revision != req.expected_routine_revision:
            # A crash after the package transaction committed increments the
            # routine revision before the durable install terminal transition.
            # Replaying the original request must still reach the proof-bound
            # reconciliation path; no approval-held or uncommitted job may
            # bypass the normal revision CAS.
            if job.get("status") != "awaiting_approval":
                reconciled = await self._reconcile_committed_install(
                    routine,
                    version,
                    job_id=job_id,
                    job=job,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
                if reconciled is not None:
                    return reconciled
            raise RoutineError("routine_revision_stale")
        if job.get("status") != "awaiting_approval":
            reconciled = await self._reconcile_committed_install(
                routine,
                version,
                job_id=job_id,
                job=job,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
            if reconciled is not None:
                return reconciled
            raise RoutineError("routine_install_not_awaiting_approval")
        queued = await self._resume_approval(
            job,
            req.approval_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            routine_id=routine_id,
            version_number=int(version.version),
            expected_routine_revision=req.expected_routine_revision,
        )
        claimed = await durable_job_repository.claim_job(
            job_id,
            owner=f"routine:{job_id}",
            expected_revision=queued.get("revision"),
            expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"),
            lease_seconds=ROUTINE_DEADLINE_SECONDS,
        )
        lease = claimed.get("lease") or {}
        owner = str(lease.get("owner") or "")
        fence = int(lease.get("fencing_token") or 0)
        revision = int(claimed.get("revision") or 0)
        workflow_name = f"{routine_slug(routine_id, version.version)}.md"
        runbook_name = f"{routine_slug(routine_id, version.version)}.yaml"
        committed = False
        try:
            # The approval resume and claim are separate durable operations.
            # This next section acquires the canonical routine writer lock
            # before any local package write.  It either observes a winning
            # pause/revoke and writes nothing, or commits the installed
            # selector before pause/revoke can change its revision.
            installation = await self._install_package_under_routine_lock(
                job=claimed,
                routine_id=routine_id,
                version_number=int(version.version),
                expected_routine_revision=req.expected_routine_revision,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                workflow_name=workflow_name,
                runbook_name=runbook_name,
            )
            routine = installation["routine"]
            version = installation["version"]
            package_digest = str(installation["package_digest"])
            committed = True
            published = await self._publish_staged_install_under_routine_lock(
                job=claimed,
                routine_id=routine_id,
                version_number=int(version.version),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                staging_root=installation["staging_root"],
                package_digest=package_digest,
                workflow_name=workflow_name,
                runbook_name=runbook_name,
            )
            package_digest = str(published["package_digest"])
            package_files = tuple(published["package_files"])
            # The canonical routine/version commit has completed and its DB
            # lock has been released.  Durable artifact/effect/readback
            # receipts are deliberately written afterward; committed-install
            # recovery closes a crash between these two durable boundaries.
            receipt_job = await self._record_install_receipts(
                job_id=job_id,
                package_files=package_files,
                package_digest=package_digest,
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
            )
            revision = int(receipt_job.get("revision") or revision)
            await durable_job_repository.transition_job(
                job_id,
                "succeeded",
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
                result={"package_digest": package_digest, "routine_id": routine_id, "version": version.version, "workflow_sha256": version.workflow_sha256, "runbook_sha256": version.runbook_sha256, "learning": "no_learning"},
                result_summary="routine files installed, parsed, hashed, and read back",
            )
            _cleanup_install_staging_root(installation["staging_root"])
            return await self.read(routine_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        except Exception as exc:
            if committed:
                # The package transaction is authoritative. Retry the local
                # terminal receipt instead of projecting an installed routine
                # while its install job remains running.
                try:
                    reconciled = await self._reconcile_committed_install(
                        routine,
                        version,
                        job_id=job_id,
                        job=claimed,
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                    )
                except RoutineError:
                    raise
                if reconciled is not None:
                    return reconciled
            current = await durable_job_repository.get_job(job_id)
            if current and current.get("status") == "running":
                # The canonical selector commit is authoritative once it has
                # returned.  A receipt failure therefore enters the explicit
                # local-finalization recovery state; marking this job failed
                # would make startup recovery skip the committed package and
                # strand its artifact/effect/readback receipts.
                current_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
                try:
                    await durable_job_repository.transition_job(
                        job_id,
                        "blocked",
                        owner=str(current_lease.get("owner") or "") or None,
                        fencing_token=int(current_lease.get("fencing_token") or 0) or None,
                        expected_revision=current.get("revision"),
                        reason="routine_install_local_finalize_pending",
                        result={"recovery": "reconcile_committed_install", "learning": "no_learning"},
                        result_summary="package commit succeeded; durable install receipts require local recovery",
                    )
                except Exception:
                    # A competing recovery owns the current fence.  Leave the
                    # canonical row untouched for that worker to reconcile.
                    pass
            raise RoutineError("routine_install_local_finalize_pending", str(exc)) from exc

    async def activate(self, routine_id: str, req: RoutineActivateRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)
        if routine.revision != req.expected_routine_revision:
            raise RoutineError("routine_revision_stale")
        version = await self._version(routine_id, req.version)
        if not version.installed_package_digest:
            raise RoutineError("routine_version_not_installed")
        package = self._package_readback(
            owner_principal_id,
            owner_session_id,
            routine_id,
            int(req.version),
            version.installed_package_digest,
        )
        if package.get("status") != "active" or package.get("digest") != version.installed_package_digest:
            raise RoutineError("package_review_required")
        async with db_engine.get_session() as db:
            result = await db.execute(update(GuardianRoutine).where(GuardianRoutine.id == routine_id, GuardianRoutine.owner_principal_id == owner_principal_id, GuardianRoutine.owner_session_id == owner_session_id, GuardianRoutine.revision == req.expected_routine_revision, GuardianRoutine.state != "revoked").values(state="active", current_version=req.version, revision=GuardianRoutine.revision + 1, updated_at=_now()))
            if result.rowcount != 1:
                raise RoutineError("routine_revision_stale")
        return await self.read(routine_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)

    async def invoke(
        self,
        routine_id: str,
        req: RoutineInvokeRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        work_board_idempotency_key: str | None = None,
        work_board_task_id: str | None = None,
        work_board_parent_handoff_context: list[dict[str, Any]] | None = None,
        work_board_parent_handoff_digest: str | None = None,
        runtime_seconds: int = ROUTINE_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        invocation_uuid = _safe_invocation_uuid(req.invocation_uuid)
        if work_board_idempotency_key is not None and not str(work_board_idempotency_key).strip():
            raise RoutineError("work_board_binding_invalid")
        parent_handoff_context = list(work_board_parent_handoff_context or [])
        parent_handoff_digest = str(work_board_parent_handoff_digest or "")
        if parent_handoff_context:
            encoded_handoffs = _dump(parent_handoff_context)
            if (
                work_board_idempotency_key is None
                or not work_board_task_id
                or len(encoded_handoffs.encode("utf-8")) > 32_768
                or _sha(encoded_handoffs) != parent_handoff_digest
                or any(
                    not isinstance(item, dict)
                    or item.get("status") != "verified"
                    or item.get("child_task_id") != work_board_task_id
                    for item in parent_handoff_context
                )
            ):
                raise RoutineError("work_board_handoff_binding_invalid")
        elif parent_handoff_digest:
            raise RoutineError("work_board_handoff_binding_invalid")
        routine = await self._routine(routine_id, owner_principal_id)
        self._require_routine_owner_session(routine, owner_session_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        try:
            requested_version = int(req.version)
            current_version = int(routine.current_version or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RoutineError("routine_version_not_current") from exc
        if requested_version != current_version:
            raise RoutineError("routine_version_not_current")
        # A replay is read-only, but it must use the same current-version
        # selection gate as a new admission.  Otherwise an old invocation
        # could be returned or resumed after a newer routine version became
        # current.
        if work_board_task_id is None:
            existing_receipt = await self._replay_existing_board_invocation(
                routine_id=routine_id,
                req=req,
                invocation_uuid=invocation_uuid,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
            if existing_receipt is not None:
                return existing_receipt
        if routine.revision != req.expected_routine_revision or routine.state != "active":
            raise RoutineError("routine_not_active_or_stale")
        version = await self._version(routine_id, req.version)
        if not version.installed_package_digest:
            raise RoutineError("routine_version_not_installed")
        # Installation approval is durable, but package governance may have
        # changed since the version was installed.  Re-read the current
        # package review immediately before admitting a new invocation.
        package = self._package_readback(
            owner_principal_id,
            owner_session_id,
            routine_id,
            int(req.version),
            version.installed_package_digest,
        )
        if package.get("status") != "active" or package.get("digest") != version.installed_package_digest:
            raise RoutineError("package_review_required")
        watch = await source_watch_service.get_watch(
            req.source_watch_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if watch is None or watch.get("goal_id") != req.goal_id:
            raise RoutineError("source_watch_not_owned", status_code=404)
        if str(watch.get("state") or "") != "active":
            raise RoutineError("source_watch_not_active", status_code=409)
        if (
            int(watch.get("goal_revision", 0)) != int(req.expected_goal_revision)
            or int(watch.get("plan_revision", 0)) != int(req.expected_watch_revision)
        ):
            raise RoutineError("source_watch_revision_stale")
        async with db_engine.get_session() as db:
            goal = (await db.execute(select(Goal).where(Goal.id == req.goal_id))).scalars().first()
            if (
                goal is None
                or str(goal.owner_principal_id or "") != owner_principal_id
                or str(goal.owner_session_id or "") != owner_session_id
                or int(goal.revision or 0) != int(req.expected_goal_revision)
                or str(getattr(goal.status, "value", goal.status) or "") != "active"
            ):
                raise RoutineError("goal_binding_stale")
        provenance = _load(version.source_provenance_json, {})
        if work_board_task_id is None:
            # Direct operator invocation is a board intent.  Capability and
            # credential checks run again in the managed dispatcher, where a
            # missing route/credential can become a visible Blocked task
            # instead of disappearing as an HTTP-only failure.
            return await self._create_board_invocation_task(
                routine=routine,
                version=version,
                req=req,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                provenance=provenance,
            )
        connection = await GitHubFollowthroughService().get_connection(owner_principal_id)
        if connection.get("mode") != "active":
            raise RoutineError("github_connection_not_active")
        fixed_repository = str(provenance.get("source_repository") or "")
        if fixed_repository and connection.get("repository") != fixed_repository:
            raise RoutineError("github_connection_repository_changed")
        job_id = f"routine-invocation:{routine_id}:{invocation_uuid}"
        authority = {
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": owner_session_id,
            "goal_owner_principal_id": owner_principal_id,
            "goal_owner_session_id": owner_session_id,
            "routine_id": routine_id,
            "routine_revision": routine.revision,
            "routine_version": req.version,
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "package_digest": version.installed_package_digest,
            "source_watch_id": req.source_watch_id,
            "source_watch_revision": req.expected_watch_revision,
            "github_connection_id": connection.get("id"),
            "github_connection_revision": connection.get("revision"),
            "github_repository": connection.get("repository"),
            "github_action": provenance.get("source_action"),
            "github_target": provenance.get("source_target"),
            "invocation_uuid": invocation_uuid,
            "capability_id": ROUTINE_CAPABILITY_VERSION,
            "budget_microusd": 0,
        }
        invocation_inputs = {
            "routine_id": routine_id,
            "routine_version": req.version,
            "source_watch_id": req.source_watch_id,
            "source_watch_revision": req.expected_watch_revision,
            "invocation_uuid": invocation_uuid,
        }
        if parent_handoff_context:
            invocation_inputs["parent_handoff_context"] = parent_handoff_context
            invocation_inputs["parent_handoff_digest"] = parent_handoff_digest
            authority["parent_handoff_digest"] = parent_handoff_digest
        job = await self._admit_user_job(
            job_id=job_id,
            job_kind="routine_invocation",
            idempotency_key=f"{owner_principal_id}:{routine_id}:{invocation_uuid}",
            inputs=invocation_inputs,
            authority=authority,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=req.goal_id,
            goal_revision=req.expected_goal_revision,
            plan_revision=int(watch.get("plan_revision") or 1),
            candidate_id=None,
            work_board_idempotency_key=work_board_idempotency_key,
            runtime_seconds=runtime_seconds,
        )
        receipt = job.get("receipt") if isinstance(job.get("receipt"), Mapping) else {}
        deduped = receipt.get("status") == "deduped"
        durable_authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        durable_approval_id = str(durable_authority.get("approval_id") or "") or None
        if deduped:
            # The durable row is authoritative for retries.  Never create or
            # replace an approval for a terminal, running, or already-held
            # invocation that won the idempotency race.
            if (
                job.get("status") == "running"
                and not durable_approval_id
                and not any(isinstance(item, Mapping) for item in (job.get("effects") or []))
            ):
                # A process can stop after the exact board binding is
                # claimed but before the approval row is created.  Reuse the
                # same leased durable run and create the one canonical
                # approval; no capability step or external effect is replayed.
                approval_id = await self._hold_approval(
                    job,
                    tool_name=ROUTINE_INVOKE_TOOL,
                    summary=f"Run guardian routine {routine_id} for goal {req.goal_id}",
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
                return {
                    "status": "awaiting_approval",
                    "job_id": str(job.get("job_id") or job_id),
                    "approval_id": approval_id,
                    "deduped": True,
                    "preview": {"routine_id": routine_id, "routine_revision": routine.revision, "version": req.version, "goal_id": req.goal_id, "goal_revision": req.expected_goal_revision, "source_watch_id": req.source_watch_id, "source_watch_revision": req.expected_watch_revision, "package_digest": version.installed_package_digest, "steps": ["guardian_watch_run", "github_followthrough"]},
                }
            return {
                "status": job.get("status"),
                "job_id": str(job.get("job_id") or job_id),
                "approval_id": durable_approval_id,
                "result": job.get("result"),
                "deduped": True,
                "preview": {"routine_id": routine_id, "routine_revision": routine.revision, "version": req.version, "goal_id": req.goal_id, "goal_revision": req.expected_goal_revision, "source_watch_id": req.source_watch_id, "source_watch_revision": req.expected_watch_revision, "package_digest": version.installed_package_digest, "steps": ["guardian_watch_run", "github_followthrough"]},
            }
        if job.get("status") == "awaiting_approval":
            # A repository implementation may return the existing row without
            # the explicit dedupe receipt.  Its persisted authority still
            # wins, and a second approval must not be created.
            approval_id = durable_approval_id
            return {
                "status": job.get("status"),
                "job_id": str(job.get("job_id") or job_id),
                "approval_id": approval_id,
                "result": job.get("result"),
                "deduped": True,
                "preview": {"routine_id": routine_id, "routine_revision": routine.revision, "version": req.version, "goal_id": req.goal_id, "goal_revision": req.expected_goal_revision, "source_watch_id": req.source_watch_id, "source_watch_revision": req.expected_watch_revision, "package_digest": version.installed_package_digest, "steps": ["guardian_watch_run", "github_followthrough"]},
            }
        approval_id = await self._hold_approval(job, tool_name=ROUTINE_INVOKE_TOOL, summary=f"Run guardian routine {routine_id} for goal {req.goal_id}", owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        return {"status": "awaiting_approval", "job_id": job_id, "approval_id": approval_id, "preview": {"routine_id": routine_id, "routine_revision": routine.revision, "version": req.version, "goal_id": req.goal_id, "goal_revision": req.expected_goal_revision, "source_watch_id": req.source_watch_id, "source_watch_revision": req.expected_watch_revision, "package_digest": version.installed_package_digest, "steps": ["guardian_watch_run", "github_followthrough"]}}

    async def execute_invocation(self, routine_id: str, job_id: str, req: RoutineExecuteRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._require_active_routine(
            routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            expected_revision=req.expected_routine_revision,
        )
        job = await durable_job_repository.get_job(job_id)
        if not job or job.get("job_kind") != "routine_invocation" or not _owner_matches(job, owner_principal_id, owner_session_id):
            raise RoutineError("routine_invocation_not_owned", status_code=404)
        authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        try:
            bound_version = int(authority.get("routine_version") or 0)
            current_version = int(routine.current_version or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RoutineError("routine_version_not_current") from exc
        if bound_version <= 0 or bound_version != current_version:
            return await self._block_parent_for_recovery_prerequisite(
                job,
                reason_code="routine_version_not_current",
            )
        # The approval is bound to the package selected when the parent was
        # admitted.  Re-read that exact version and lifecycle pointer before
        # consuming the approval; a revoke/quarantine after admission must
        # leave the parent recoverably blocked without starting M1.
        try:
            await self._require_active_package_binding(
                routine_id,
                version=authority.get("routine_version"),
                expected_digest=str(authority.get("package_digest") or ""),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        except RoutineError as exc:
            if exc.code in {
                "package_review_required",
                "routine_version_not_installed",
                "routine_version_binding_invalid",
            }:
                return await self._block_parent_for_recovery_prerequisite(
                    job,
                    reason_code=exc.code,
                )
            raise
        queued = await self._resume_approval(job, req.approval_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        claimed = await durable_job_repository.claim_job(job_id, owner=f"routine:{job_id}", expected_revision=queued.get("revision"), expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"), lease_seconds=_remaining_runtime_seconds(job))
        parent_lease = claimed.get("lease") or {}
        parent_fence = int(parent_lease.get("fencing_token") or 0)
        parent_revision = int(claimed.get("revision") or 0)
        authority = claimed.get("declared_authority") if isinstance(claimed.get("declared_authority"), Mapping) else {}
        try:
            bound_routine_revision = int(authority.get("routine_revision") or 0)
            resumed_bound_version = int(authority.get("routine_version") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RoutineError("routine_revision_binding_invalid") from exc
        try:
            resumed_routine = await self._require_active_routine(
                routine_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                expected_revision=bound_routine_revision,
            )
            if resumed_bound_version <= 0 or int(resumed_routine.current_version or 0) != resumed_bound_version:
                raise RoutineError("routine_version_not_current")
        except RoutineError as exc:
            await durable_job_repository.cancel_job(
                job_id,
                owner=str(parent_lease.get("owner") or "") or None,
                fencing_token=parent_fence or None,
                expected_revision=parent_revision,
                reason=exc.code,
            )
            raise
        try:
            # Close the race between approval consumption/claim and child
            # admission.  The source-watch child is never allowed to run from
            # a package that was revoked while the parent was being resumed.
            await self._require_active_package_binding(
                routine_id,
                version=authority.get("routine_version"),
                expected_digest=str(authority.get("package_digest") or ""),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        except RoutineError:
            await durable_job_repository.cancel_job(
                job_id,
                owner=str(parent_lease.get("owner") or "") or None,
                fencing_token=parent_fence or None,
                expected_revision=parent_revision,
                reason="package_review_required",
            )
            raise
        watch_id = str(authority.get("source_watch_id") or "")
        watch_revision = int(authority.get("source_watch_revision") or 0)
        invocation_uuid = str(authority.get("invocation_uuid") or "")
        if not watch_id or not invocation_uuid or watch_revision <= 0:
            raise RoutineError("routine_invocation_binding_missing")
        watch_child_id = _child_job_id(invocation_uuid, "watch")
        watch_child = await self._admit_child_job(
            claimed,
            child_id=watch_child_id,
            step_id="guardian_watch_run",
            inputs={
                "routine_invocation_job_id": job_id,
                "source_watch_id": watch_id,
                "source_watch_revision": watch_revision,
                "invocation_uuid": invocation_uuid,
            },
            authority={
                "routine_id": routine_id,
                "routine_revision": bound_routine_revision,
                "routine_version": authority.get("routine_version"),
                "source_watch_id": watch_id,
                "source_watch_revision": watch_revision,
                "invocation_uuid": invocation_uuid,
            },
        )
        if watch_child.get("status") != "running":
            raise RoutineError("routine_watch_child_not_running")
        parent_checkpoint = await durable_job_repository.record_checkpoint(
            job_id,
            checkpoint_id="routine:watch_child_recorded",
            state={"step_id": "guardian_watch_run", "child_job_id": watch_child_id, "watch_id": watch_id},
            checkpoint_payload={"step_id": "guardian_watch_run", "child_job_id": watch_child_id, "watch_id": watch_id, "watch_revision": watch_revision},
            safe=True,
            owner=str(parent_lease.get("owner") or ""),
            fencing_token=parent_fence,
            expected_revision=parent_revision,
        )
        child_lease = watch_child.get("lease") or {}
        context = RoutineStepContext(
            principal_id=owner_principal_id,
            session_id=owner_session_id,
            lease_owner=str(child_lease.get("owner") or ""),
            fencing_token=int(child_lease.get("fencing_token") or 0),
            runtime_job_id=job_id,
        )
        # M1 owns source observation and its own local-write approval.  Never
        # manufacture that approval from the routine approval.
        try:
            await self._require_active_package_binding(
                routine_id,
                version=authority.get("routine_version"),
                expected_digest=str(authority.get("package_digest") or ""),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
            result = await guardian_watch_run(
                job_id,
                child_job_id=watch_child_id,
                context=context,
                service=self,
            )
        except RoutineError as exc:
            child_current = await durable_job_repository.get_job(watch_child_id)
            if child_current and child_current.get("status") == "running":
                await self._settle_child(
                    child_current,
                    result={"status": "blocked", "reason_code": exc.code},
                    status="blocked",
                    reason=exc.code,
                )
            current = await durable_job_repository.get_job(job_id)
            if current and current.get("status") == "running":
                parent_lease = current.get("lease") or {}
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=parent_lease.get("owner"),
                    fencing_token=parent_lease.get("fencing_token"),
                    expected_revision=current.get("revision"),
                    reason=exc.code,
                    result={
                        "learning": "no_learning",
                        "reason_code": exc.code,
                        "recovery_action": "restore_prerequisite",
                    },
                    result_summary="routine package is no longer current before source-watch execution",
                )
            return {
                "status": "blocked",
                "job_id": job_id,
                "child_job_id": watch_child_id,
                "reason_code": exc.code,
                "recovery_action": "restore_prerequisite",
                "learning": "no_learning",
            }
        except Exception as exc:
            child_current = await durable_job_repository.get_job(watch_child_id)
            if child_current and child_current.get("status") == "running":
                await self._settle_child(
                    child_current,
                    result={"status": "blocked", "reason_code": type(exc).__name__},
                    status="blocked",
                    reason="watch_child_dispatch_failed",
                )
            current = await durable_job_repository.get_job(job_id)
            if current and current.get("status") == "running":
                parent_lease = current.get("lease") or {}
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=parent_lease.get("owner"),
                    fencing_token=parent_lease.get("fencing_token"),
                    expected_revision=current.get("revision"),
                    reason="watch_child_dispatch_failed",
                    result={"learning": "no_learning", "child_job_id": watch_child_id, "reason_code": type(exc).__name__},
                    result_summary="the persisted watch child could not be dispatched",
                )
            return {"status": "blocked", "job_id": job_id, "child_job_id": watch_child_id, "learning": "no_learning"}
        m1_status = str(result.get("status") or "blocked")
        parent_current = await durable_job_repository.get_job(job_id) or parent_checkpoint
        if result.get("status") in {"no_change", "rebaseline_initialized"}:
            await self._finalize_parent(
                parent_current,
                result={
                    "watch_child_job_id": watch_child_id,
                    "m1_job_id": result.get("job_id"),
                    "status": result.get("status"),
                },
                reason=str(result.get("status")),
            )
            return {"status": result.get("status"), "job_id": job_id, "learning": "no_learning", "publication": "skipped"}
        current = await durable_job_repository.get_job(job_id)
        if current and current.get("status") == "running":
            parent_lease = current.get("lease") or {}
            child_reason = "awaiting_child_approval" if m1_status == "awaiting_approval" else "watch_child_completed"
            await durable_job_repository.record_checkpoint(
                job_id,
                checkpoint_id="routine:watch_readback_verified" if m1_status == "succeeded" else "routine:watch_child_blocked",
                state={"step_id": "guardian_watch_run", "child_job_id": watch_child_id, "status": m1_status},
                checkpoint_payload={
                    "step_id": "guardian_watch_run",
                    "child_job_id": watch_child_id,
                    "status": m1_status,
                    "m1_job_id": result.get("m1_job_id") or result.get("job_id"),
                    "packet_id": result.get("packet_id"),
                    "approval_id": result.get("approval_id"),
                    "reason_code": result.get("reason_code"),
                },
                safe=True,
                owner=str(parent_lease.get("owner") or ""),
                fencing_token=int(parent_lease.get("fencing_token") or 0),
                expected_revision=current.get("revision"),
            )
            current = await durable_job_repository.get_job(job_id) or current
            parent_lease = current.get("lease") or {}
            if m1_status not in {"succeeded", "awaiting_publication_preview"}:
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=parent_lease.get("owner"),
                    fencing_token=parent_lease.get("fencing_token"),
                    expected_revision=current.get("revision"),
                    reason=child_reason,
                    result={"learning": "no_learning", "child_job_id": watch_child_id, "child": {"status": m1_status}},
                    result_summary=child_reason,
                )
                return {"status": "blocked", "job_id": job_id, "child_job_id": watch_child_id, "child": result, "learning": "no_learning"}
            current = await durable_job_repository.get_job(job_id) or current
            parent_lease = current.get("lease") or {}
            await durable_job_repository.transition_job(
                job_id,
                "blocked",
                owner=parent_lease.get("owner"),
                fencing_token=parent_lease.get("fencing_token"),
                expected_revision=current.get("revision"),
                reason="awaiting_publication_preview",
                result={"learning": "no_learning", "child_job_id": watch_child_id, "child": {"status": m1_status}},
                result_summary="fresh M3 publication preview is required",
            )
        return {"status": "awaiting_publication_preview", "job_id": job_id, "child_job_id": watch_child_id, "child": result, "learning": "no_learning"}

    async def execute_generated_step(
        self,
        routine_invocation_job_id: str,
        step_id: str,
        *,
        context: RoutineStepContext,
    ) -> dict[str, Any]:
        """Execute one fixed generated workflow step through the same child CAS.

        Generated workflow files are declarative; they do not receive generic
        service handles or caller-supplied arguments. The invocation ID is
        resolved to the owner-bound routine parent, then to the deterministic
        M1 or M3 child before dispatch.
        """

        if not isinstance(context, RoutineStepContext):
            raise PermissionError("generated routine step requires trusted runtime context")
        requested_parent_id = str(routine_invocation_job_id or "").strip()
        if not requested_parent_id or context.runtime_job_id != requested_parent_id:
            raise PermissionError("routine runtime parent mismatch")
        if step_id not in {"guardian_watch_run", "github_followthrough"}:
            raise RoutineError("routine_step_not_allowed", status_code=422)
        if step_id == "github_followthrough" and not context.external_mutation_granted:
            raise PermissionError("routine follow-through requires external_mutation authority")
        parent = await durable_job_repository.get_job(str(routine_invocation_job_id).strip())
        if (
            not parent
            or parent.get("job_kind") != "routine_invocation"
            or not _owner_matches(parent, context.principal_id, context.session_id)
        ):
            raise RoutineError("routine_invocation_not_running")
        if parent.get("status") != "running":
            # A generated workflow may be resumed after the M1 child has
            # blocked on its own approval or after the M3 preview was created.
            # Only the persisted parent/checkpoint path may reopen it; callers
            # cannot supply a new child identity or mutable destination.
            if step_id != "github_followthrough" or parent.get("status") != "blocked":
                return {
                    "status": parent.get("status") or "blocked",
                    "job_id": str(routine_invocation_job_id),
                    "reason_code": "routine_invocation_not_running",
                    "operator_visible": True,
                    "learning": "no_learning",
                }
            parent = await self._claim_parent_for_recovery(
                parent,
                owner_principal_id=context.principal_id,
                owner_session_id=context.session_id,
            )
        authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        routine_id = str(authority.get("routine_id") or "")
        routine_revision = int(authority.get("routine_revision") or 0)
        invocation_uuid = str(authority.get("invocation_uuid") or "")
        if not routine_id or routine_revision <= 0 or not invocation_uuid:
            raise RoutineError("routine_invocation_binding_missing")
        await self._require_active_routine(
            routine_id,
            owner_principal_id=context.principal_id,
            owner_session_id=context.session_id,
            expected_revision=routine_revision,
        )
        child_suffix = "watch" if step_id == "guardian_watch_run" else "publication"
        child_id = _child_job_id(invocation_uuid, child_suffix)
        parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
        if (
            str(parent_lease.get("owner") or "") != str(context.lease_owner or "")
            or int(parent_lease.get("fencing_token") or 0) != int(context.fencing_token or 0)
        ):
            # This invocation context belongs to a worker that lost the
            # parent lease.  Leave any child owned by the replacement worker
            # untouched and return a durable-looking operator receipt rather
            # than dispatching from stale authority.
            return {
                "status": "blocked",
                "job_id": str(routine_invocation_job_id),
                "child_job_id": child_id,
                "reason_code": "routine_parent_fence_stale",
                "recovery": "retry_current_invocation",
                "operator_action": "recover_or_cancel",
                "operator_visible": True,
                "learning": "no_learning",
            }
        child = await durable_job_repository.get_job(child_id)
        if child is None:
            if step_id == "github_followthrough":
                watch_checkpoint = _job_checkpoint(parent, "routine:watch_readback_verified")
                if not watch_checkpoint or str(watch_checkpoint.get("status") or "") != "succeeded":
                    return {
                        "status": "awaiting_child_approval",
                        "job_id": str(routine_invocation_job_id),
                        "child_job_id": child_id,
                        "reason_code": "awaiting_child_approval",
                        "operator_visible": True,
                        "learning": "no_learning",
                    }
                parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
                await durable_job_repository.record_checkpoint(
                    str(routine_invocation_job_id),
                    checkpoint_id="routine:publication_required",
                    state={"step_id": step_id, "child_job_id": child_id, "status": "awaiting_publication_preview"},
                    checkpoint_payload={"step_id": step_id, "child_job_id": child_id, "status": "awaiting_publication_preview"},
                    safe=True,
                    owner=str(parent_lease.get("owner") or ""),
                    fencing_token=int(parent_lease.get("fencing_token") or 0),
                    expected_revision=parent.get("revision"),
                )
                latest_parent = await durable_job_repository.get_job(str(routine_invocation_job_id)) or parent
                latest_lease = latest_parent.get("lease") if isinstance(latest_parent.get("lease"), Mapping) else parent_lease
                if latest_parent.get("status") == "running":
                    await durable_job_repository.transition_job(
                        str(routine_invocation_job_id),
                        "blocked",
                        owner=str(latest_lease.get("owner") or ""),
                        fencing_token=int(latest_lease.get("fencing_token") or 0),
                        expected_revision=latest_parent.get("revision"),
                        reason="awaiting_publication_preview",
                        result={"learning": "no_learning", "child_job_id": child_id},
                        result_summary="fresh M3 publication preview is required",
                    )
                return {
                    "status": "awaiting_publication_preview",
                    "job_id": str(routine_invocation_job_id),
                    "child_job_id": child_id,
                    "reason_code": "awaiting_publication_preview",
                    "operator_visible": True,
                    "learning": "no_learning",
                }
            child = await self._admit_child_job(
                parent,
                child_id=child_id,
                step_id=step_id,
                inputs={
                    "routine_invocation_job_id": str(routine_invocation_job_id),
                    "invocation_uuid": invocation_uuid,
                },
                authority={
                    "routine_id": routine_id,
                    "routine_revision": routine_revision,
                    "routine_version": authority.get("routine_version"),
                    "source_watch_id": authority.get("source_watch_id"),
                    "source_watch_revision": authority.get("source_watch_revision"),
                    "invocation_uuid": invocation_uuid,
                },
            )
        else:
            parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
            child_parent_fence = int(child.get("parent_fencing_token") or 0)
            current_parent_fence = int(parent_lease.get("fencing_token") or 0)
            if (
                parent.get("status") != "running"
                or child_parent_fence <= 0
                or child_parent_fence != current_parent_fence
            ) and child.get("status") in {
                "accepted",
                "queued",
                "running",
                "awaiting_approval",
                "blocked",
            }:
                if step_id == "github_followthrough":
                    # M3 is a separately admitted durable job.  A stale
                    # wrapper-child fence therefore cannot justify cancelling
                    # the approved publication: recovery must first inspect
                    # the M3 effect ledger and either adopt the approval-held
                    # child under the current parent fence or reconcile an
                    # uncertain outcome.  Block the parent durably while
                    # leaving M3 and the wrapper evidence available.
                    prepared = _publication_binding_checkpoint(child) or {}
                    return await self._block_parent_for_publication_adoption(
                        parent,
                        reason_code="routine_parent_fence_stale",
                        child_job_id=child_id,
                        m3_job_id=str(prepared.get("m3_job_id") or ""),
                    )
                return await self._cancel_stale_child(
                    child,
                    parent=parent,
                    step_id=step_id,
                )
        if child.get("status") == "blocked" and step_id == "github_followthrough":
            prepared = _job_checkpoint(child, "routine-child:prepared") or {}
            if prepared.get("m3_job_id"):
                resumed = await durable_job_repository.resume_job(
                    str(child["job_id"]),
                    expected_revision=child.get("revision"),
                    reason="routine_followthrough_resume",
                )
                if resumed.get("status") == "queued":
                    child = await durable_job_repository.claim_job(
                        str(child["job_id"]),
                        owner=f"routine-child:{child['job_id']}",
                        expected_revision=resumed.get("revision"),
                        expected_fencing_token=(resumed.get("lease") or {}).get("fencing_token"),
                        lease_seconds=_remaining_runtime_seconds(parent),
                    )
        if child.get("status") != "running":
            if child.get("status") in {"succeeded", "degraded", "cancelled", "blocked", "failed"}:
                return {"status": child.get("status"), "child_job_id": child_id, "recovery": "terminal_child"}
            raise RoutineError("routine_child_not_running")
        lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        child_context = RoutineStepContext(
            principal_id=context.principal_id,
            session_id=context.session_id,
            lease_owner=str(lease.get("owner") or ""),
            fencing_token=int(lease.get("fencing_token") or 0),
            external_mutation_granted=context.external_mutation_granted,
            runtime_job_id=str(routine_invocation_job_id),
        )
        if step_id == "guardian_watch_run":
            result = await self.execute_watch_step(child_id, context=child_context)
            latest_parent = await durable_job_repository.get_job(str(routine_invocation_job_id)) or parent
            if latest_parent.get("status") == "running":
                parent_lease = latest_parent.get("lease") if isinstance(latest_parent.get("lease"), Mapping) else {}
                m1_status = str(result.get("status") or "blocked")
                if m1_status in {"no_change", "rebaseline_initialized"}:
                    await self._finalize_parent(
                        latest_parent,
                        result={
                            "watch_child_job_id": child_id,
                            "m1_job_id": result.get("job_id"),
                            "status": m1_status,
                        },
                        reason=m1_status,
                    )
                else:
                    checkpoint_id = "routine:watch_readback_verified" if m1_status == "succeeded" else "routine:watch_child_blocked"
                    checkpoint = await durable_job_repository.record_checkpoint(
                        str(routine_invocation_job_id),
                        checkpoint_id=checkpoint_id,
                        state={"step_id": step_id, "child_job_id": child_id, "status": m1_status},
                        checkpoint_payload={
                            "step_id": step_id,
                            "child_job_id": child_id,
                            "status": m1_status,
                            "m1_job_id": result.get("m1_job_id") or result.get("job_id"),
                            "packet_id": result.get("packet_id"),
                            "approval_id": result.get("approval_id"),
                            "reason_code": result.get("reason_code"),
                        },
                        safe=True,
                        owner=str(parent_lease.get("owner") or ""),
                        fencing_token=int(parent_lease.get("fencing_token") or 0),
                        expected_revision=latest_parent.get("revision"),
                    )
                    current_parent = await durable_job_repository.get_job(str(routine_invocation_job_id)) or checkpoint
                    current_lease = current_parent.get("lease") if isinstance(current_parent.get("lease"), Mapping) else parent_lease
                    await durable_job_repository.transition_job(
                        str(routine_invocation_job_id),
                        "blocked",
                        owner=str(current_lease.get("owner") or ""),
                        fencing_token=int(current_lease.get("fencing_token") or 0),
                        expected_revision=current_parent.get("revision"),
                        reason=("awaiting_child_approval" if m1_status == "awaiting_approval" else "awaiting_publication_preview" if m1_status == "succeeded" else "watch_child_blocked"),
                        result={"learning": "no_learning", "child_job_id": child_id, "child": {"status": m1_status}},
                        result_summary="generated routine watch step requires the next guarded boundary",
                    )
            return result
        result = await self.execute_followthrough_step(child_id, context=child_context)
        latest_parent = await durable_job_repository.get_job(str(routine_invocation_job_id)) or parent
        if latest_parent.get("status") == "running":
            m3_status = str(result.get("status") or "blocked")
            if m3_status == "succeeded":
                await self._finalize_parent(
                    latest_parent,
                    result={
                        "publication_child_job_id": child_id,
                        "m3_job_id": result.get("m3_job_id") or result.get("job_id"),
                        "status": m3_status,
                        "remote_id": result.get("remote_id"),
                        "browser_url": result.get("remote_url"),
                    },
                    reason="publication_readback_verified",
                )
            elif m3_status in {"unknown_external_effect", "cost_liability"}:
                parent_lease = latest_parent.get("lease") if isinstance(latest_parent.get("lease"), Mapping) else {}
                await durable_job_repository.transition_job(
                    str(routine_invocation_job_id),
                    m3_status,
                    owner=str(parent_lease.get("owner") or ""),
                    fencing_token=int(parent_lease.get("fencing_token") or 0),
                    expected_revision=latest_parent.get("revision"),
                    reason="publication_requires_reconciliation",
                    result={"learning": "no_learning", "child_job_id": child_id, "m3_job_id": result.get("m3_job_id") or result.get("job_id"), "operator_action": "reconcile_or_cancel"},
                    result_summary="publication outcome is uncertain and requires M3 reconciliation",
                )
            else:
                parent_lease = latest_parent.get("lease") if isinstance(latest_parent.get("lease"), Mapping) else {}
                if m3_status == "blocked":
                    result = {
                        **dict(result),
                        "recovery": "reconcile_or_cancel",
                        "operator_action": "reconcile_or_cancel",
                    }
                await durable_job_repository.transition_job(
                    str(routine_invocation_job_id),
                    "blocked",
                    owner=str(parent_lease.get("owner") or ""),
                    fencing_token=int(parent_lease.get("fencing_token") or 0),
                    expected_revision=latest_parent.get("revision"),
                    reason="awaiting_publication_approval" if m3_status == "awaiting_approval" else "publication_requires_reconciliation" if m3_status == "blocked" else "publication_child_blocked",
                    result={"learning": "no_learning", "child_job_id": child_id, "m3_job_id": result.get("m3_job_id") or result.get("job_id"), "operator_action": "reconcile_or_cancel" if m3_status == "blocked" else None},
                    result_summary="publication remains pending or blocked",
                )
        return result

    async def execute_watch_step(self, job_id: str, *, context: Any) -> dict[str, Any]:
        if not isinstance(context, RoutineStepContext):
            raise PermissionError("routine watch step requires trusted runtime context")
        child = await self._owned_child(job_id, step_id="guardian_watch_run", context=context)
        authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        watch_id = str(authority.get("source_watch_id") or "")
        watch_revision = int(authority.get("source_watch_revision") or 0)
        if not watch_id or watch_revision <= 0:
            raise RoutineError("routine_watch_child_binding_missing")
        await self._record_child_checkpoint(child, checkpoint_id="routine-child:dispatch_started", payload={"step_id": "guardian_watch_run", "watch_id": watch_id})
        try:
            result = await source_watch_service.run_watch(
                watch_id,
                occurrence_id=job_id,
                expected_plan_revision=watch_revision,
                expected_owner_session_id=context.session_id,
            )
        except Exception as exc:
            result = {"status": "blocked", "reason_code": type(exc).__name__, "job_id": job_id}
        status = str(result.get("status") or "blocked")
        child_status = {
            "no_change": "succeeded",
            "rebaseline_initialized": "succeeded",
            "succeeded": "succeeded",
            "degraded": "degraded",
            # The approval belongs to M1's own durable job.  Reusing it for
            # this child would be an approval-boundary violation, so retain a
            # blocked child receipt until recovery observes M1's outcome.
            "awaiting_approval": "blocked",
            "blocked": "blocked",
        }.get(status, "blocked")
        safe_result = dict(result)
        safe_result["m1_job_id"] = result.get("job_id")
        settled = await self._settle_child(child, result=safe_result, status=child_status, reason=status)
        return {**safe_result, "child_job_id": job_id, "child_status": settled.get("status")}

    async def execute_followthrough_step(self, job_id: str, *, context: Any) -> dict[str, Any]:
        if not isinstance(context, RoutineStepContext):
            raise PermissionError("routine follow-through step requires trusted runtime context")
        child = await self._owned_child(job_id, step_id="github_followthrough", context=context)
        authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        prepared_checkpoint = _publication_binding_checkpoint(child) or {}
        m3_job_id = str(authority.get("m3_job_id") or prepared_checkpoint.get("m3_job_id") or "")
        if not m3_job_id:
            raise RoutineError("routine_followthrough_child_binding_missing")
        await self._record_child_checkpoint(child, checkpoint_id="routine-child:dispatch_started", payload={"step_id": "github_followthrough", "m3_job_id": m3_job_id})
        try:
            result = await GitHubFollowthroughService().execute(
                owner_principal_id=context.principal_id,
                job_id=m3_job_id,
                owner_session_id=context.session_id,
                external_mutation_granted=context.external_mutation_granted,
            )
        except Exception as exc:
            result = {"status": "blocked", "reason_code": type(exc).__name__, "job_id": m3_job_id}
        status = str(result.get("status") or "blocked")
        child_status = {
            "succeeded": "succeeded",
            "degraded": "degraded",
            "unknown_external_effect": "unknown_external_effect",
            "cost_liability": "cost_liability",
            "awaiting_approval": "blocked",
            "blocked": "blocked",
            "cancelled": "cancelled",
        }.get(status, "blocked")
        settled = await self._settle_child(child, result=result, status=child_status, reason=status)
        return {**dict(result), "child_job_id": job_id, "m3_job_id": m3_job_id, "child_status": settled.get("status")}

    async def _claim_parent_for_recovery(
        self,
        job: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        if not _owner_matches(job, owner_principal_id, owner_session_id):
            raise RoutineError("routine_invocation_not_owned", status_code=404)
        current = dict(job)
        if current.get("status") == "blocked":
            resumed = await durable_job_repository.resume_job(
                str(current["job_id"]),
                expected_revision=current.get("revision"),
                reason="routine_recovery_resume",
            )
            current = resumed
        if current.get("status") == "queued":
            current = await durable_job_repository.claim_job(
                str(current["job_id"]),
                owner=f"routine:{current['job_id']}",
                expected_revision=current.get("revision"),
                expected_fencing_token=(current.get("lease") or {}).get("fencing_token"),
                lease_seconds=_remaining_runtime_seconds(current),
            )
        if current.get("status") != "running":
            raise RoutineError("routine_parent_not_recoverable")
        return current

    async def _reacquire_parent_after_child(
        self,
        previous: Mapping[str, Any],
        *,
        routine_id: str,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any] | None:
        """Renew or reacquire the parent fence before a post-child write.

        Child execution is awaited work. During that wait a stale recovery,
        pause, or another worker may replace the parent lease. Re-reading the
        parent and heartbeating the same fence keeps the following checkpoint
        or transition bound to the worker that actually ran the child; a
        blocked parent is resumed through the normal queued claim path.
        """

        job_id = str(previous.get("job_id") or "")
        latest = await durable_job_repository.get_job(job_id) if job_id else None
        if not latest or not _owner_matches(latest, owner_principal_id, owner_session_id):
            return None
        authority = latest.get("declared_authority") if isinstance(latest.get("declared_authority"), Mapping) else {}
        if str(authority.get("routine_id") or "") != str(routine_id):
            return None
        try:
            routine_revision = int(authority.get("routine_revision") or 0)
        except (TypeError, ValueError):
            return None
        if routine_revision <= 0:
            return None
        try:
            await self._require_active_routine(
                routine_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                expected_revision=routine_revision,
            )
        except RoutineError:
            return None
        if latest.get("status") == "blocked":
            try:
                return await self._claim_parent_for_recovery(
                    latest,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
            except RoutineError:
                return None
        if latest.get("status") != "running":
            return None
        previous_lease = previous.get("lease") if isinstance(previous.get("lease"), Mapping) else {}
        latest_lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
        owner = str(latest_lease.get("owner") or "")
        fencing_token = int(latest_lease.get("fencing_token") or 0)
        if (
            owner != str(previous_lease.get("owner") or "")
            or fencing_token != int(previous_lease.get("fencing_token") or 0)
            or not owner
            or fencing_token <= 0
        ):
            return None
        try:
            return await durable_job_repository.heartbeat_job(
                job_id,
                owner=owner,
                fencing_token=fencing_token,
                lease_seconds=_remaining_runtime_seconds(previous),
                expected_state="running",
                expected_revision=latest.get("revision"),
                expected_fencing_token=fencing_token,
            )
        except Exception:
            return None

    async def prepare_publication(
        self,
        routine_id: str,
        job_id: str,
        req: RoutinePublicationRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        external_mutation_granted: bool = False,
    ) -> dict[str, Any]:
        """Prepare exactly one fixed M3 destination from the persisted watch child."""

        if not external_mutation_granted:
            raise RoutineError("external_mutation_grant_required", status_code=403)

        routine = await self._routine(routine_id, owner_principal_id)
        parent = await durable_job_repository.get_job(job_id)
        if (
            not parent
            or parent.get("job_kind") != "routine_invocation"
            or not _owner_matches(parent, owner_principal_id, owner_session_id)
        ):
            raise RoutineError("routine_invocation_not_owned", status_code=404)
        authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        try:
            routine_revision = int(authority.get("routine_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise RoutineError("routine_revision_binding_invalid") from exc
        await self._require_active_routine(
            routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            expected_revision=routine_revision,
        )
        expected_package = str(authority.get("package_digest") or "")
        package = self._package_readback(
            owner_principal_id,
            owner_session_id,
            routine_id,
            int(authority.get("routine_version") or 0),
            expected_package,
        )
        if package.get("status") != "active" or package.get("digest") != expected_package:
            raise RoutineError("package_review_required")
        watch_checkpoint = _job_checkpoint(parent, "routine:watch_readback_verified")
        if not watch_checkpoint or str(watch_checkpoint.get("status") or "") != "succeeded":
            raise RoutineError("routine_watch_readback_required")
        packet_id = str(watch_checkpoint.get("packet_id") or "")
        m1_job_id = str(watch_checkpoint.get("m1_job_id") or "")
        if not packet_id or not m1_job_id:
            raise RoutineError("routine_watch_provenance_missing")
        m1_job = await durable_job_repository.get_job(m1_job_id)
        if not _verified_readback(m1_job):
            raise RoutineError("routine_watch_readback_required")
        async with db_engine.get_session() as db:
            packet = (
                await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet_id))
            ).scalars().first()
            if packet is None or packet.status != "succeeded" or packet.verification_status != "passed":
                raise RoutineError("routine_watch_packet_not_verified")
            if packet.goal_id != parent.get("goal_id") or int(packet.goal_revision) != int(parent.get("goal_revision") or 0):
                raise RoutineError("routine_watch_goal_changed")
            if str(packet.dossier_artifact_id or "") == "" or str(packet.dossier_sha256 or "") == "":
                raise RoutineError("routine_dossier_missing")
            db.expunge(packet)
        provenance = _load((await self._version(routine_id, int(authority.get("routine_version") or 0))).source_provenance_json, {})
        action = str(authority.get("github_action") or provenance.get("source_action") or "")
        repository = str(authority.get("github_repository") or provenance.get("source_repository") or "")
        target = str(authority.get("github_target") or provenance.get("source_target") or "")
        if action not in {"create_issue", "create_comment"} or not repository or not target:
            raise RoutineError("routine_destination_binding_missing")
        connection = await GitHubFollowthroughService().get_connection(owner_principal_id)
        if (
            connection.get("mode") != "active"
            or connection.get("repository") != repository
            or str(connection.get("id") or "") != str(authority.get("github_connection_id") or "")
            or int(connection.get("revision") or 0) != int(authority.get("github_connection_revision") or 0)
        ):
            raise RoutineError("github_connection_binding_changed")
        current = await self._claim_parent_for_recovery(parent, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        await self._require_active_routine(
            routine_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            expected_revision=routine_revision,
        )
        invocation_uuid = str(authority.get("invocation_uuid") or "")
        publication_child_id = _child_job_id(invocation_uuid, "publication")
        # Derive the M3 identity before the external prepare call.  Persisting
        # this identity on the M4 child lets pause/revoke and restart recovery
        # adopt an approval-held M3 job even if the process dies between the
        # M3 admission and the later binding checkpoint.
        operation_uuid = str(uuid.uuid5(uuid.UUID(invocation_uuid), "seraph:guardian-routine:publication"))
        expected_m3_job_id = _expected_publication_job_id(owner_principal_id, operation_uuid)
        existing_child = await durable_job_repository.get_job(publication_child_id)
        if existing_child is not None:
            checkpoint = _job_checkpoint(existing_child, "routine-child:prepared")
            if checkpoint and checkpoint.get("m3_job_id"):
                m3_job_id = str(checkpoint["m3_job_id"])
                if m3_job_id != expected_m3_job_id:
                    raise RoutineError("routine_publication_binding_conflict")
                m3_job = await durable_job_repository.get_job(m3_job_id) or {}
                if not m3_job:
                    # The prior prepare may have crashed before M3 admission;
                    # retain the deterministic child and let the idempotent
                    # prepare below recreate the missing durable job.
                    checkpoint = None
                else:
                    existing_m3 = await GitHubFollowthroughService()._prepare_job_response(m3_job)
                    latest_parent = await durable_job_repository.get_job(job_id) or current
                    if latest_parent.get("status") == "running":
                        latest_parent, existing_child = await self._persist_adopted_publication_checkpoints(
                            latest_parent,
                            existing_child,
                            publication_child_id=publication_child_id,
                            m3_job_id=m3_job_id,
                            m3_job=m3_job,
                        )
                        parent_lease = latest_parent.get("lease") or {}
                        await durable_job_repository.transition_job(
                            job_id,
                            "blocked",
                            owner=parent_lease.get("owner"),
                            fencing_token=parent_lease.get("fencing_token"),
                            expected_revision=latest_parent.get("revision"),
                            reason="awaiting_publication_approval" if m3_job.get("status") != "succeeded" else "publication_readback_pending",
                            result={"learning": "no_learning", "publication_child_job_id": publication_child_id, "m3_job_id": m3_job_id},
                            result_summary="existing publication child is authoritative",
                        )
                    return {"status": existing_m3.get("status"), "child_job_id": publication_child_id, "m3": existing_m3}
            adoption = _publication_binding_checkpoint(existing_child)
            if adoption and adoption.get("m3_job_id"):
                m3_job_id = str(adoption["m3_job_id"])
                if m3_job_id != expected_m3_job_id:
                    raise RoutineError("routine_publication_binding_conflict")
                m3_job = await durable_job_repository.get_job(m3_job_id) or {}
                if m3_job:
                    existing_m3 = await GitHubFollowthroughService()._prepare_job_response(m3_job)
                    latest_parent = await durable_job_repository.get_job(job_id) or current
                    if latest_parent.get("status") == "running":
                        latest_parent, existing_child = await self._persist_adopted_publication_checkpoints(
                            latest_parent,
                            existing_child,
                            publication_child_id=publication_child_id,
                            m3_job_id=m3_job_id,
                            m3_job=m3_job,
                        )
                        parent_lease = latest_parent.get("lease") or {}
                        await durable_job_repository.transition_job(
                            job_id,
                            "blocked",
                            owner=parent_lease.get("owner"),
                            fencing_token=parent_lease.get("fencing_token"),
                            expected_revision=latest_parent.get("revision"),
                            reason="awaiting_publication_approval" if m3_job.get("status") != "succeeded" else "publication_readback_pending",
                            result={"learning": "no_learning", "publication_child_job_id": publication_child_id, "m3_job_id": m3_job_id},
                            result_summary="adopted publication child is authoritative",
                        )
                    return {"status": existing_m3.get("status"), "child_job_id": publication_child_id, "m3": existing_m3, "recovery": "adopted"}
        child = await self._admit_child_job(
            current,
            child_id=publication_child_id,
            step_id="github_followthrough",
            inputs={"routine_invocation_job_id": job_id, "invocation_uuid": invocation_uuid},
            authority={
                "routine_id": routine_id,
                "routine_revision": routine_revision,
                "routine_version": authority.get("routine_version"),
                "package_digest": authority.get("package_digest"),
                "invocation_uuid": invocation_uuid,
                "source_watch_id": authority.get("source_watch_id"),
                "github_connection_id": connection.get("id"),
                "github_connection_revision": int(connection.get("revision") or 0),
                "github_repository": repository,
                "github_action": action,
                "m3_job_id": expected_m3_job_id,
                "publication_operation_uuid": operation_uuid,
            },
        )
        # M3 creates its own durable approval job.  Record the deterministic
        # identity before crossing that boundary so a crash after M3 admission
        # can be adopted by pause/revoke/recovery instead of becoming an
        # unowned approval reservation.
        child = await self._record_child_checkpoint(
            child,
            checkpoint_id="routine-child:adoption_pending",
            payload={
                "m3_job_id": expected_m3_job_id,
                "publication_operation_uuid": operation_uuid,
                "status": "prepare_pending",
            },
        )
        child_lease = child.get("lease") or {}
        child_context = RoutineStepContext(
            principal_id=owner_principal_id,
            session_id=owner_session_id,
            lease_owner=str(child_lease.get("owner") or ""),
            fencing_token=int(child_lease.get("fencing_token") or 0),
            runtime_job_id=job_id,
        )
        # M3 derives the publication job and its approval from this stable
        # UUID.  The routine stores only the resulting child identity and
        # never embeds the publication body in its reusable files.
        try:
            issue_number: int | None = None
            if action == "create_comment":
                try:
                    issue_number = int(target)
                except (TypeError, ValueError) as exc:
                    raise RoutineError("routine_comment_target_invalid", status_code=422) from exc
            publication_request = PrepareRequest(
                conversation_id=owner_session_id,
                goal_id=str(parent.get("goal_id")),
                goal_revision=int(parent.get("goal_revision") or 0),
                dossier_artifact_id=str(packet.dossier_artifact_id),
                dossier_sha256=str(packet.dossier_sha256),
                connection_revision=int(connection.get("revision") or 0),
                action=action,
                title=req.title if action == "create_issue" else None,
                body=req.body,
                issue_number=issue_number,
                idempotency_key=operation_uuid,
            )
            prepared = await GitHubFollowthroughService().prepare(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                external_mutation_granted=external_mutation_granted,
                request=publication_request,
            )
        except Exception as exc:
            await self._settle_child(child, result={"status": "blocked", "reason_code": type(exc).__name__}, status="blocked", reason="publication_prepare_blocked")
            latest_parent = await durable_job_repository.get_job(job_id) or current
            if latest_parent.get("status") == "running":
                parent_lease = latest_parent.get("lease") or {}
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=parent_lease.get("owner"),
                    fencing_token=parent_lease.get("fencing_token"),
                    expected_revision=latest_parent.get("revision"),
                    reason="publication_prepare_blocked",
                    result={"learning": "no_learning", "publication_child_job_id": publication_child_id, "reason_code": type(exc).__name__},
                    result_summary="publication preparation is blocked",
                )
            if isinstance(exc, GitHubFollowthroughError):
                raise RoutineError(exc.code, str(exc), status_code=exc.status_code) from exc
            raise
        m3_job_id = str(prepared.get("job_id") or "")
        if not m3_job_id:
            await self._settle_child(child, result={"status": "blocked", "reason_code": "m3_job_id_missing"}, status="blocked", reason="publication_prepare_blocked")
            raise RoutineError("publication_child_missing")
        if m3_job_id != expected_m3_job_id:
            cancellation = await self._cancel_m3_job_safely(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                m3_job_id=m3_job_id,
            )
            await self._settle_child(
                child,
                result={
                    "status": "blocked",
                    "reason_code": "routine_publication_binding_conflict",
                    "m3_job_id": m3_job_id,
                    "operator_action": cancellation.get("operator_action") if not cancellation.get("ok") else None,
                },
                status="blocked",
                reason="routine_publication_binding_conflict",
            )
            latest_parent = await durable_job_repository.get_job(job_id) or current
            if latest_parent.get("status") == "running":
                parent_lease = latest_parent.get("lease") or {}
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=parent_lease.get("owner"),
                    fencing_token=parent_lease.get("fencing_token"),
                    expected_revision=latest_parent.get("revision"),
                    reason="routine_publication_binding_conflict",
                    result={
                        "learning": "no_learning",
                        "publication_child_job_id": publication_child_id,
                        "m3_job_id": m3_job_id,
                        "operator_action": cancellation.get("operator_action") if not cancellation.get("ok") else "reconcile_or_cancel",
                    },
                    result_summary="M3 returned a job identity different from the deterministic routine binding",
                )
            raise RoutineError("routine_publication_binding_conflict")
        current = await durable_job_repository.get_job(job_id) or current
        # ``prepare`` creates an M3 durable job before M4 records its own
        # binding.  If pause/revoke won the race, cancel that M3 reservation
        # immediately; otherwise it would be invisible to the routine scan.
        if current.get("status") != "running":
            await self._cancel_m3_job_safely(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                m3_job_id=m3_job_id,
            )
            return {
                "status": current.get("status"),
                "child_job_id": publication_child_id,
                "m3_job_id": m3_job_id,
                "recovery": "parent_not_running",
                "learning": "no_learning",
            }
        parent_lease = current.get("lease") or {}
        try:
            await durable_job_repository.record_checkpoint(
                job_id,
                checkpoint_id="routine:publication_child_recorded",
                state={"step_id": "github_followthrough", "child_job_id": publication_child_id, "m3_job_id": m3_job_id},
                checkpoint_payload={"step_id": "github_followthrough", "child_job_id": publication_child_id, "m3_job_id": m3_job_id, "approval_id": prepared.get("approval_id")},
                safe=True,
                owner=str(parent_lease.get("owner") or ""),
                fencing_token=int(parent_lease.get("fencing_token") or 0),
                expected_revision=current.get("revision"),
            )
            child = await durable_job_repository.get_job(publication_child_id) or child
            child = await self._record_child_checkpoint(
                child,
                checkpoint_id="routine-child:prepared",
                payload={
                    "m3_job_id": m3_job_id,
                    "publication_operation_uuid": operation_uuid,
                    "approval_id": prepared.get("approval_id"),
                    "status": prepared.get("status"),
                },
            )
        except Exception as exc:
            await self._cancel_m3_job_safely(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                m3_job_id=m3_job_id,
            )
            raise RoutineError("routine_publication_binding_lost", str(exc)) from exc
        child_current = await durable_job_repository.get_job(publication_child_id) or child
        if str(prepared.get("status") or "") == "awaiting_approval":
            await self._settle_child(child_current, result={"status": "awaiting_approval", "approval_id": prepared.get("approval_id"), "m3_job_id": m3_job_id}, status="blocked", reason="awaiting_publication_approval")
            current = await durable_job_repository.get_job(job_id) or current
            parent_lease = current.get("lease") or {}
            await durable_job_repository.transition_job(
                job_id,
                "blocked",
                owner=parent_lease.get("owner"),
                fencing_token=parent_lease.get("fencing_token"),
                expected_revision=current.get("revision"),
                reason="awaiting_publication_approval",
                result={"learning": "no_learning", "child_job_id": publication_child_id, "m3_job_id": m3_job_id},
                result_summary="fresh M3 publication approval is required",
            )
        return {"status": prepared.get("status"), "child_job_id": publication_child_id, "m3": prepared}

    async def _finalize_parent(
        self,
        parent: Mapping[str, Any],
        *,
        result: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        current = await durable_job_repository.get_job(str(parent["job_id"])) or dict(parent)
        if current.get("status") != "running":
            return current
        lease = current.get("lease") or {}
        owner = str(lease.get("owner") or "")
        fence = int(lease.get("fencing_token") or 0)
        source_run_id = str(result.get("m3_job_id") or result.get("m1_job_id") or "")
        if not source_run_id:
            raise RoutineError("routine_verified_child_readback_missing", status_code=409)
        source_run = await durable_job_repository.get_job(source_run_id)
        source_effect = next(
            (
                item
                for item in reversed(source_run.get("effects", []) if source_run else [])
                if isinstance(item, Mapping)
                and item.get("receipt_kind") == "readback"
                and item.get("status") == "succeeded"
                and item.get("reconciled") is True
                and isinstance(item.get("details"), Mapping)
                and item["details"].get("verified") is True
            ),
            None,
        )
        if source_run is None or source_run.get("status") != "succeeded" or source_effect is None:
            raise RoutineError("routine_verified_child_readback_missing", status_code=409)
        source_effect_details = source_effect.get("details")
        source_effect_details = source_effect_details if isinstance(source_effect_details, Mapping) else {}
        source_readback_id = str(
            source_effect.get("readback_id") or source_effect_details.get("readback_id") or ""
        )
        source_verified_at = str(
            source_effect.get("verified_at") or source_effect_details.get("verified_at") or ""
        )
        source_digest = str(
            source_effect.get("content_sha256")
            or source_effect.get("target_digest")
            or source_effect_details.get("content_sha256")
            or ""
        )
        if not source_readback_id or not source_verified_at or not source_digest:
            raise RoutineError("routine_verified_child_readback_missing", status_code=409)
        safe = {
            key: result.get(key)
            for key in ("watch_child_job_id", "publication_child_job_id", "m1_job_id", "m3_job_id", "packet_id", "status", "remote_id", "browser_url")
            if result.get(key) is not None
        }
        safe["learning"] = "no_learning"
        source_proof = {
            "workflow_run_id": source_run_id,
            "readback_id": source_readback_id,
            "verified_at": source_verified_at,
            "content_sha256": source_digest,
            "target_path": str(source_effect.get("target_path") or ""),
            "effect_type": str(source_effect.get("effect_type") or ""),
        }
        digest = _sha(_dump({"reason": reason, **safe, "source_readback": source_proof}))
        routine_readback_id = "routine-readback:" + hashlib.sha256(
            f"{current.get('job_id')}:{source_readback_id}".encode("utf-8")
        ).hexdigest()[:32]
        routine_verified_at = _now().isoformat()
        effect = await durable_job_repository.record_effect(
            str(parent["job_id"]),
            effect_type="guardian_routine_outcome",
            target_path=f"routine:{(current.get('declared_authority') or {}).get('routine_id')}",
            target_digest=digest,
            status="succeeded",
            details={
                "verified": True,
                "learning": "no_learning",
                "reason": reason,
                "source_readback": source_proof,
                **safe,
            },
            owner=owner,
            fencing_token=fence,
            expected_revision=current.get("revision"),
        )
        readback = await durable_job_repository.record_readback(
            str(parent["job_id"]),
            target_path=f"routine:{(current.get('declared_authority') or {}).get('routine_id')}",
            effect_id=(effect.get("receipt") or {}).get("effect_id"),
            effect_type="guardian_routine_outcome",
            target_digest=digest,
            content_sha256=digest,
            readback_id=routine_readback_id,
            verified_at=routine_verified_at,
            status="succeeded",
            details={
                "verified": True,
                "learning": "no_learning",
                "reason": reason,
                "readback_id": routine_readback_id,
                "verified_at": routine_verified_at,
                "source_readback": source_proof,
                **safe,
            },
            owner=owner,
            fencing_token=fence,
            expected_revision=effect.get("revision"),
        )
        finalized = await durable_job_repository.record_checkpoint(
            str(parent["job_id"]),
            checkpoint_id="routine:finalized",
            state={"reason": reason, **safe},
            checkpoint_payload={"reason": reason, **safe},
            safe=True,
            owner=owner,
            fencing_token=fence,
            expected_revision=readback.get("revision"),
        )
        return await durable_job_repository.transition_job(
            str(parent["job_id"]),
            "succeeded",
            owner=owner,
            fencing_token=fence,
            expected_revision=finalized.get("revision"),
            result=safe,
            result_summary=reason,
        )

    async def _block_parent_for_recovery_prerequisite(
        self,
        parent: Mapping[str, Any],
        *,
        reason_code: str,
        recovery_action: str = "restore_prerequisite",
    ) -> dict[str, Any]:
        """Leave a routine parent visibly blocked after a failed recheck."""

        job_id = str(parent.get("job_id") or "")
        current = await durable_job_repository.get_job(job_id) if job_id else None
        current = current or dict(parent)
        if current.get("status") in {"running", "awaiting_approval", "accepted", "queued"}:
            lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            try:
                current = await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=str(lease.get("owner") or "") or None,
                    fencing_token=int(lease.get("fencing_token") or 0) or None,
                    expected_revision=current.get("revision"),
                    reason=reason_code,
                    result={
                        "learning": "no_learning",
                        "reason_code": reason_code,
                        "recovery_action": recovery_action,
                    },
                    result_summary="routine recovery prerequisite is no longer current",
                )
            except Exception:
                current = await durable_job_repository.get_job(job_id) or current
        return {
            "status": current.get("status") or "blocked",
            "job_id": job_id,
            "reason_code": reason_code,
            "recovery_action": recovery_action,
            "operator_visible": True,
            "learning": "no_learning",
        }

    async def _block_parent_for_publication_adoption(
        self,
        parent: Mapping[str, Any],
        *,
        reason_code: str,
        child_job_id: str,
        m3_job_id: str,
    ) -> dict[str, Any]:
        """Leave an unadoptable publication visibly blocked for reconciliation."""

        parent_id = str(parent.get("job_id") or "")
        current = await durable_job_repository.get_job(parent_id) if parent_id else None
        current = current or dict(parent)
        if current.get("status") == "running":
            lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            try:
                current = await durable_job_repository.transition_job(
                    parent_id,
                    "blocked",
                    owner=str(lease.get("owner") or ""),
                    fencing_token=int(lease.get("fencing_token") or 0),
                    expected_revision=current.get("revision"),
                    reason="routine_publication_adoption_requires_reconciliation",
                    result={
                        "learning": "no_learning",
                        "reason_code": reason_code,
                        "publication_child_job_id": child_job_id,
                        "m3_job_id": m3_job_id,
                        "operator_action": "reconcile_or_cancel",
                    },
                    result_summary="publication recovery could not prove a safe child adoption",
                )
            except Exception:
                current = await durable_job_repository.get_job(parent_id) or current
        return {
            "status": current.get("status") or "blocked",
            "job_id": parent_id,
            "child_job_id": child_job_id,
            "m3_job_id": m3_job_id,
            "reason_code": reason_code,
            "recovery": "reconcile_or_cancel",
            "operator_action": "reconcile_or_cancel",
            "operator_visible": True,
            "learning": "no_learning",
        }

    async def recover(
        self,
        routine_id: str,
        job_id: str,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        external_mutation_granted: bool = False,
    ) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)
        job = await durable_job_repository.get_job(job_id)
        if not job or job.get("job_kind") != "routine_invocation" or not _owner_matches(job, owner_principal_id, owner_session_id):
            raise RoutineError("routine_invocation_not_owned", status_code=404)
        if job.get("status") in {"succeeded", "degraded", "cancelled", "unknown_external_effect", "cost_liability"}:
            return {"status": job.get("status"), "job_id": job_id, "recovery": "terminal_receipt", "operator_visible": True}
        authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        try:
            await self._require_active_routine(
                routine_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                expected_revision=int(authority.get("routine_revision") or routine.revision),
            )
            await self._require_active_package_binding(
                routine_id,
                version=authority.get("routine_version"),
                expected_digest=str(authority.get("package_digest") or ""),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        except RoutineError as exc:
            if exc.code in {
                "package_review_required",
                "routine_version_not_installed",
                "routine_version_binding_invalid",
            }:
                return await self._block_parent_for_recovery_prerequisite(
                    job,
                    reason_code=exc.code,
                )
            raise
        current = await self._claim_parent_for_recovery(job, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        watch_checkpoint = (
            _job_checkpoint(current, "routine:watch_child_blocked")
            or _job_checkpoint(current, "routine:watch_readback_verified")
            or _job_checkpoint(current, "routine:watch_child_recorded")
        )
        publication_checkpoint = _job_checkpoint(current, "routine:publication_child_recorded")
        if not publication_checkpoint:
            # The child adoption checkpoint is written immediately before M3
            # prepare.  A crash after M3 creates its approval job but before
            # the later parent checkpoint must still be recoverable by the
            # deterministic invocation/child identity.
            invocation_uuid = str(authority.get("invocation_uuid") or "")
            if invocation_uuid:
                try:
                    operation_uuid = str(
                        uuid.uuid5(uuid.UUID(invocation_uuid), "seraph:guardian-routine:publication")
                    )
                    expected_m3_job_id = _expected_publication_job_id(owner_principal_id, operation_uuid)
                    publication_child_id = _child_job_id(invocation_uuid, "publication")
                except (AttributeError, TypeError, ValueError) as exc:
                    raise RoutineError("routine_invocation_binding_missing") from exc
                child = await durable_job_repository.get_job(publication_child_id)
                child_checkpoint = _publication_binding_checkpoint(child)
                if child_checkpoint:
                    m3_job_id = str(child_checkpoint.get("m3_job_id") or "")
                    if not m3_job_id:
                        raise RoutineError("routine_publication_provenance_missing")
                    if m3_job_id != expected_m3_job_id:
                        latest = await durable_job_repository.get_job(job_id) or current
                        if latest.get("status") == "running":
                            lease = latest.get("lease") or {}
                            try:
                                await durable_job_repository.transition_job(
                                    job_id,
                                    "blocked",
                                    owner=lease.get("owner"),
                                    fencing_token=lease.get("fencing_token"),
                                    expected_revision=latest.get("revision"),
                                    reason="routine_publication_binding_conflict",
                                    result={
                                        "learning": "no_learning",
                                        "publication_child_job_id": publication_child_id,
                                        "m3_job_id": m3_job_id,
                                        "operator_action": "reconcile_or_cancel",
                                    },
                                    result_summary="persisted publication child binding does not match its deterministic M3 identity",
                                )
                            except Exception:
                                pass
                        return {
                            "status": "blocked",
                            "job_id": job_id,
                            "child_job_id": publication_child_id,
                            "m3_job_id": m3_job_id,
                            "reason_code": "routine_publication_binding_conflict",
                            "recovery": "reconcile_or_cancel",
                            "operator_action": "reconcile_or_cancel",
                            "learning": "no_learning",
                            "operator_visible": True,
                        }
                    m3_job = await durable_job_repository.get_job(m3_job_id)
                    if not m3_job:
                        return await self._cancel_stale_child(
                            child or {},
                            parent=current,
                            step_id="github_followthrough",
                            reason_code="routine_publication_child_missing",
                            operator_action="restart_routine_invocation",
                            cancel_external=False,
                        )
                    publication_checkpoint = {
                        **dict(child_checkpoint),
                        "child_job_id": publication_child_id,
                    }
                    try:
                        current, child = await self._persist_adopted_publication_checkpoints(
                            current,
                            child or {},
                            publication_child_id=publication_child_id,
                            m3_job_id=m3_job_id,
                            m3_job=m3_job,
                        )
                    except Exception as exc:
                        reason_code = (
                            exc.code if isinstance(exc, RoutineError) else "routine_publication_adoption_failed"
                        )
                        return await self._block_parent_for_publication_adoption(
                            current,
                            reason_code=reason_code,
                            child_job_id=publication_child_id,
                            m3_job_id=m3_job_id,
                        )
        if publication_checkpoint:
            m3_job_id = str(publication_checkpoint.get("m3_job_id") or "")
            if not m3_job_id:
                raise RoutineError("routine_publication_provenance_missing")
            publication_child_id = str(publication_checkpoint.get("child_job_id") or "")
            publication_child = (
                await durable_job_repository.get_job(publication_child_id)
                if publication_child_id
                else None
            )
            m3_job = await durable_job_repository.get_job(m3_job_id)
            if not m3_job:
                raise RoutineError("routine_publication_child_missing")
            parent_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            if (
                publication_child
                and publication_child.get("status") in {
                    "accepted",
                    "queued",
                    "running",
                    "awaiting_approval",
                    "blocked",
                }
                and int(publication_child.get("parent_fencing_token") or 0)
                != int(parent_lease.get("fencing_token") or 0)
            ):
                if m3_job.get("status") not in {"awaiting_approval", "queued"}:
                    return await self._cancel_stale_child(
                        publication_child,
                        parent=current,
                        step_id="github_followthrough",
                    )
                try:
                    current, publication_child = await self._persist_adopted_publication_checkpoints(
                        current,
                        publication_child,
                        publication_child_id=publication_child_id,
                        m3_job_id=m3_job_id,
                        m3_job=m3_job,
                    )
                except Exception as exc:
                    reason_code = (
                        exc.code if isinstance(exc, RoutineError) else "routine_publication_adoption_failed"
                    )
                    return await self._block_parent_for_publication_adoption(
                        current,
                        reason_code=reason_code,
                        child_job_id=publication_child_id,
                        m3_job_id=m3_job_id,
                    )
                parent_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            if m3_job.get("status") in {"awaiting_approval", "queued", "running"}:
                if not external_mutation_granted:
                    latest = await durable_job_repository.get_job(job_id) or current
                    if latest.get("status") == "running":
                        lease = latest.get("lease") or {}
                        await durable_job_repository.transition_job(
                            job_id,
                            "blocked",
                            owner=lease.get("owner"),
                            fencing_token=lease.get("fencing_token"),
                            expected_revision=latest.get("revision"),
                            reason="external_mutation_grant_required",
                            result={"learning": "no_learning", "m3_job_id": m3_job_id},
                            result_summary="publication recovery requires current external-mutation authority",
                        )
                    return {
                        "status": "blocked",
                        "job_id": job_id,
                        "m3_job_id": m3_job_id,
                        "reason_code": "external_mutation_grant_required",
                        "learning": "no_learning",
                        "operator_visible": True,
                    }
                # Recovery may sit blocked while an operator reviews the
                # publication. Re-read the routine and the parent lease at the
                # final handoff point so a pause, revoke, or lease takeover
                # cannot dispatch M3 under stale authority.
                fresh_parent = await durable_job_repository.get_job(job_id) or current
                fresh_authority = fresh_parent.get("declared_authority") if isinstance(fresh_parent.get("declared_authority"), Mapping) else authority
                await self._require_active_routine(
                    routine_id,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    expected_revision=int(fresh_authority.get("routine_revision") or routine.revision),
                )
                claimed_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
                fresh_lease = fresh_parent.get("lease") if isinstance(fresh_parent.get("lease"), Mapping) else {}
                if (
                    fresh_parent.get("status") != "running"
                    or int(fresh_lease.get("fencing_token") or 0) != int(claimed_lease.get("fencing_token") or 0)
                ):
                    return {
                        "status": fresh_parent.get("status") or "blocked",
                        "job_id": job_id,
                        "m3_job_id": m3_job_id,
                        "reason_code": "routine_recovery_fence_stale",
                        "recovery": "reconcile",
                        "learning": "no_learning",
                        "operator_visible": True,
                    }
                current = fresh_parent
                try:
                    await self._require_active_package_binding(
                        routine_id,
                        version=fresh_authority.get("routine_version"),
                        expected_digest=str(fresh_authority.get("package_digest") or ""),
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                    )
                except RoutineError as exc:
                    if exc.code not in {
                        "package_review_required",
                        "routine_version_not_installed",
                        "routine_version_binding_invalid",
                    }:
                        raise
                    return await self._block_parent_for_recovery_prerequisite(
                        current,
                        reason_code=exc.code,
                    )
                m3_result = await GitHubFollowthroughService().execute(
                    owner_principal_id=owner_principal_id,
                    job_id=m3_job_id,
                    owner_session_id=owner_session_id,
                    external_mutation_granted=external_mutation_granted,
                )
                m3_job = await durable_job_repository.get_job(m3_job_id) or m3_job
            else:
                m3_result = m3_job
            reacquired = await self._reacquire_parent_after_child(
                current,
                routine_id=routine_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
            if reacquired is None:
                latest = await durable_job_repository.get_job(job_id) or current
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "m3_job_id": m3_job_id,
                    "child": m3_result,
                    "reason_code": "routine_recovery_fence_stale",
                    "recovery": "reconcile",
                    "learning": "no_learning",
                    "operator_visible": True,
                    "parent_status": latest.get("status") or "blocked",
                }
            current = reacquired
            if m3_job.get("status") == "succeeded" and _verified_readback(m3_job):
                done = await self._finalize_parent(
                    current,
                    result={"publication_child_job_id": publication_checkpoint.get("child_job_id"), "m3_job_id": m3_job_id, "status": "succeeded", "remote_id": m3_result.get("remote_id"), "browser_url": m3_result.get("remote_url")},
                    reason="publication_readback_verified",
                )
                return {"status": "succeeded", "job_id": job_id, "child": m3_result, "learning": "no_learning", "durable": done}
            child_status = str(m3_job.get("status") or m3_result.get("status") or "blocked")
            if child_status in {"unknown_external_effect", "blocked"}:
                lease = current.get("lease") or {}
                await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=lease.get("owner"),
                    fencing_token=lease.get("fencing_token"),
                    expected_revision=current.get("revision"),
                    reason="publication_requires_reconciliation",
                    result={
                        "learning": "no_learning",
                        "m3_job_id": m3_job_id,
                        "child_status": child_status,
                        "operator_action": "reconcile_or_cancel",
                    },
                    result_summary="publication child is blocked or has an unknown external effect; reconcile before retry or cancel",
                )
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "m3_job_id": m3_job_id,
                    "child_status": child_status,
                    "child": m3_result,
                    "reason_code": "publication_requires_reconciliation",
                    "recovery": "reconcile_or_cancel",
                    "operator_action": "reconcile_or_cancel",
                    "learning": "no_learning",
                    "operator_visible": True,
                }
            lease = current.get("lease") or {}
            await durable_job_repository.transition_job(
                job_id,
                "blocked",
                owner=lease.get("owner"),
                fencing_token=lease.get("fencing_token"),
                expected_revision=current.get("revision"),
                reason="awaiting_publication_approval",
                result={"learning": "no_learning", "m3_job_id": m3_job_id, "child_status": child_status},
                result_summary="publication remains pending or requires reconciliation",
            )
            return {"status": "awaiting_publication_approval", "job_id": job_id, "m3_job_id": m3_job_id, "child": m3_result, "learning": "no_learning"}
        if watch_checkpoint:
            m1_job_id = str(watch_checkpoint.get("m1_job_id") or "")
            packet_id = str(watch_checkpoint.get("packet_id") or "")
            watch_child_id = str(watch_checkpoint.get("child_job_id") or "")
            if not watch_child_id:
                invocation_uuid = str(authority.get("invocation_uuid") or "")
                if invocation_uuid:
                    watch_child_id = _child_job_id(invocation_uuid, "watch")
            if not m1_job_id and watch_child_id:
                watch_id = str(
                    watch_checkpoint.get("watch_id")
                    or authority.get("source_watch_id")
                    or ""
                )
                if watch_id:
                    m1_job_id = f"source-watch:{watch_id}:{watch_child_id}"
            if not packet_id:
                # ``watch_child_recorded`` is written before M1 dispatch. A
                # crash at this boundary has no parent-owned readback yet.
                # Close the deterministic M1 reservation (if one exists),
                # cancel the wrapper, and leave a visible operator recovery
                # receipt instead of returning with the parent still running.
                watch_child = (
                    await durable_job_repository.get_job(watch_child_id)
                    if watch_child_id
                    else None
                )
                if watch_child:
                    cancellation = await self._cancel_stale_child(
                        watch_child,
                        parent=current,
                        step_id="guardian_watch_run",
                        reason_code="routine_watch_outcome_checkpoint_missing",
                        operator_action="recover_or_cancel",
                    )
                    return {
                        **cancellation,
                        "status": "blocked",
                        "job_id": job_id,
                        "child_job_id": watch_child_id,
                        "m1_job_id": m1_job_id or None,
                        "reason_code": cancellation.get("reason_code")
                        or "routine_watch_outcome_checkpoint_missing",
                        "recovery": cancellation.get("recovery") or "recover_or_cancel",
                        "operator_action": cancellation.get("operator_action") or "recover_or_cancel",
                        "operator_visible": True,
                        "learning": "no_learning",
                    }
                latest = await durable_job_repository.get_job(job_id) or current
                if latest.get("status") == "running":
                    lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
                    await durable_job_repository.transition_job(
                        job_id,
                        "blocked",
                        owner=str(lease.get("owner") or ""),
                        fencing_token=int(lease.get("fencing_token") or 0),
                        expected_revision=latest.get("revision"),
                        reason="routine_watch_outcome_checkpoint_missing",
                        result={
                            "learning": "no_learning",
                            "child_job_id": watch_child_id,
                            "m1_job_id": m1_job_id or None,
                            "operator_action": "recover_or_cancel",
                        },
                        result_summary="source-watch child outcome is missing and requires operator recovery",
                    )
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "child_job_id": watch_child_id,
                    "m1_job_id": m1_job_id or None,
                    "reason_code": "routine_watch_outcome_checkpoint_missing",
                    "recovery": "recover_or_cancel",
                    "operator_action": "recover_or_cancel",
                    "operator_visible": True,
                    "learning": "no_learning",
                }
            if m1_job_id and packet_id:
                watch_child = (
                    await durable_job_repository.get_job(watch_child_id)
                    if watch_child_id
                    else None
                )
                parent_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
                if (
                    watch_child
                    and watch_child.get("status") in {
                        "accepted",
                        "queued",
                        "running",
                        "awaiting_approval",
                        "blocked",
                    }
                    and int(watch_child.get("parent_fencing_token") or 0)
                    != int(parent_lease.get("fencing_token") or 0)
                ):
                    return await self._cancel_stale_child(
                        watch_child,
                        parent=current,
                        step_id="guardian_watch_run",
                    )
                m1_job = await durable_job_repository.get_job(m1_job_id)
                if m1_job and m1_job.get("status") == "awaiting_approval":
                    approval_id = str(watch_checkpoint.get("approval_id") or (m1_job.get("declared_authority") or {}).get("approval_id") or "")
                    approval = await approval_repository.get(approval_id)
                    if approval and approval.status == "approved":
                        async with db_engine.get_session() as db:
                            packet = (await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet_id))).scalars().first()
                            if packet is None:
                                raise RoutineError("routine_watch_packet_not_found")
                            packet_digest = _sha(packet.proposal_text + packet.task_text)
                            watch_id = packet.source_watch_id
                            plan_revision = int(packet.plan_revision)
                            db.expunge(packet)
                        await source_watch_service.execute_packet(
                            watch_id=watch_id,
                            packet_id=packet_id,
                            expected_packet_digest=packet_digest,
                            approval_id=approval_id,
                            expected_approval_revision=m1_job.get("revision"),
                            owner_principal_id=owner_principal_id,
                            owner_session_id=owner_session_id,
                        )
                        m1_job = await durable_job_repository.get_job(m1_job_id) or m1_job
                        watch_checkpoint = {**watch_checkpoint, "status": "succeeded"}
                if m1_job and m1_job.get("status") == "succeeded" and _verified_readback(m1_job):
                    reacquired = await self._reacquire_parent_after_child(
                        current,
                        routine_id=routine_id,
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                    )
                    if reacquired is None:
                        latest = await durable_job_repository.get_job(job_id) or current
                        return {
                            "status": "blocked",
                            "job_id": job_id,
                            "child_job_id": watch_child_id,
                            "packet_id": packet_id,
                            "reason_code": "routine_recovery_fence_stale",
                            "recovery": "reconcile",
                            "learning": "no_learning",
                            "operator_visible": True,
                            "parent_status": latest.get("status") or "blocked",
                        }
                    current = reacquired
                    latest = current
                    lease = latest.get("lease") or {}
                    updated = await durable_job_repository.record_checkpoint(
                        job_id,
                        checkpoint_id="routine:watch_readback_verified",
                        state={"step_id": "guardian_watch_run", "child_job_id": watch_child_id, "status": "succeeded"},
                        checkpoint_payload={**watch_checkpoint, "status": "succeeded", "m1_job_id": m1_job_id, "packet_id": packet_id},
                        safe=True,
                        owner=lease.get("owner"),
                        fencing_token=lease.get("fencing_token"),
                        expected_revision=latest.get("revision"),
                    )
                    latest = await durable_job_repository.get_job(job_id) or updated
                    lease = latest.get("lease") or {}
                    await durable_job_repository.transition_job(job_id, "blocked", owner=lease.get("owner"), fencing_token=lease.get("fencing_token"), expected_revision=latest.get("revision"), reason="awaiting_publication_preview", result={"learning": "no_learning", "watch_child_job_id": watch_child_id, "m1_job_id": m1_job_id, "packet_id": packet_id}, result_summary="fresh M3 publication preview is required")
                    return {"status": "awaiting_publication_preview", "job_id": job_id, "child_job_id": watch_child_id, "packet_id": packet_id, "learning": "no_learning"}
            latest = await durable_job_repository.get_job(job_id) or current
            if latest.get("status") == "running":
                lease = latest.get("lease") or {}
                await durable_job_repository.transition_job(job_id, "blocked", owner=lease.get("owner"), fencing_token=lease.get("fencing_token"), expected_revision=latest.get("revision"), reason="awaiting_child_approval", result={"learning": "no_learning", "watch_child_job_id": watch_child_id}, result_summary="the persisted M1 child still requires approval or recovery")
            return {"status": "awaiting_child_approval", "job_id": job_id, "child_job_id": watch_child_id, "learning": "no_learning"}
        return {"status": current.get("status"), "job_id": job_id, "recovery": "no_child_checkpoint", "operator_visible": True}

    async def pause_or_revoke(self, routine_id: str, *, state: str, expected_revision: int, reason: str, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        if state not in {"paused", "revoked"}:
            raise RoutineError("routine_state_invalid", status_code=422)
        routine = await self._routine(routine_id, owner_principal_id)
        if routine.revision != expected_revision:
            raise RoutineError("routine_revision_stale")
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        # Flip the canonical routine state first. Child admission and child
        # execution both re-read this CAS-bound revision, so a pause/revoke
        # cannot race a new child into an effect after the operator request.
        async with db_engine.get_session() as db:
            result = await db.execute(update(GuardianRoutine).where(GuardianRoutine.id == routine_id, GuardianRoutine.owner_principal_id == owner_principal_id, GuardianRoutine.owner_session_id == owner_session_id, GuardianRoutine.revision == expected_revision, GuardianRoutine.state != "revoked").values(state=state, revision=GuardianRoutine.revision + 1, updated_at=_now()))
            if result.rowcount != 1:
                raise RoutineError("routine_revision_stale")
        cancellation_failures = await self._cancel_pending_jobs(
            routine_id,
            reason=f"routine_{state}:{reason}",
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if cancellation_failures:
            return {
                "status": "blocked",
                "routine_state": state,
                "routine_id": routine_id,
                "reason": reason,
                "reason_code": "routine_child_cancellation_incomplete",
                "recovery": "reconcile_or_cancel",
                "operator_action": "reconcile_or_cancel",
                "blocked_jobs": cancellation_failures,
                "operator_visible": True,
            }
        return {"status": state, "routine_id": routine_id, "reason": reason}

    async def rollback(self, routine_id: str, req: RoutineRollbackRequest, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        routine = await self._routine(routine_id, owner_principal_id)
        if routine.state == "revoked":
            raise RoutineError("routine_revoked_terminal")
        prior_state = str(routine.state or "")
        if str(routine.owner_session_id or "") != str(owner_session_id or ""):
            raise RoutineError("routine_owner_session_mismatch", status_code=403)
        version = await self._version(routine_id, req.target_version)
        if routine.revision != req.expected_routine_revision or not version.installed_package_digest:
            raise RoutineError("routine_revision_or_version_invalid")
        package = self._package_readback(
            owner_principal_id,
            owner_session_id,
            routine_id,
            int(req.target_version),
            version.installed_package_digest,
        )
        if package.get("status") != "active" or package.get("digest") != version.installed_package_digest:
            raise RoutineError("package_review_required")
        # Quarantine the selector before touching any child job.  A rollback
        # must never make a routine active while an old invocation still owns
        # an unresolved cancellation.  Keep the target version unselected
        # until every cancellation has a durable success receipt.
        async with db_engine.get_session() as db:
            result = await db.execute(
                update(GuardianRoutine)
                .where(
                    GuardianRoutine.id == routine_id,
                    GuardianRoutine.owner_principal_id == owner_principal_id,
                    GuardianRoutine.owner_session_id == owner_session_id,
                    GuardianRoutine.revision == req.expected_routine_revision,
                    GuardianRoutine.state != "revoked",
                )
                .values(
                    state="paused",
                    revision=GuardianRoutine.revision + 1,
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                raise RoutineError("routine_revision_stale")
        try:
            cancellation_failures = await self._cancel_pending_jobs(
                routine_id,
                reason=f"routine_rollback:{req.reason}",
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        except Exception as exc:
            # The quarantine update above is the safety boundary.  If the
            # cancellation enumerator itself fails, retain paused state and
            # expose an operator recovery receipt instead of reactivating.
            cancellation_failures = [
                {
                    "job_id": None,
                    "status": "blocked",
                    "reason_code": type(exc).__name__,
                    "operator_action": "reconcile_or_cancel",
                }
            ]
        if cancellation_failures:
            return {
                "status": "blocked",
                "routine_state": "paused",
                "routine_id": routine_id,
                "reason_code": "routine_child_cancellation_incomplete",
                "recovery": "reconcile_or_cancel",
                "operator_action": "reconcile_or_cancel",
                "blocked_jobs": cancellation_failures,
                "operator_visible": True,
            }
        # Only a successful cancellation pass may select the target version.
        # Preserve the operator's prior lifecycle state: a paused routine
        # remains paused after selecting a different version and can only be
        # resumed by the explicit routine resume path.
        async with db_engine.get_session() as db:
            result = await db.execute(
                update(GuardianRoutine)
                .where(
                    GuardianRoutine.id == routine_id,
                    GuardianRoutine.owner_principal_id == owner_principal_id,
                    GuardianRoutine.owner_session_id == owner_session_id,
                    GuardianRoutine.revision == int(req.expected_routine_revision) + 1,
                    GuardianRoutine.state == "paused",
                    GuardianRoutine.state != "revoked",
                )
                .values(
                    state=prior_state,
                    current_version=req.target_version,
                    revision=GuardianRoutine.revision + 1,
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                raise RoutineError("routine_revision_stale")
        return await self.read(routine_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)

routine_service = RoutineService()
routine_router = APIRouter(prefix="/capabilities/routines")


def _operator(request: Request):
    from src.api.capabilities import _require_authenticated_capability_operator

    return _require_authenticated_capability_operator(request)


def _http_error(exc: RoutineError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})


def _procedure_http_error(exc: ProcedureV2Error) -> HTTPException:
    detail = {
        "code": exc.code,
        "message": str(exc),
        "recovery_action": exc.recovery_action,
        "retryable": exc.retryable,
        "binding_id": exc.binding_id,
        "audit_receipt_id": exc.audit_receipt_id,
    }
    return HTTPException(status_code=exc.status_code, detail=detail)


@routine_router.get("")
async def list_routines(request: Request):
    operator = _operator(request)
    from src.auth.ownership import selected_read_scopes, selected_read_principal, RECOVERED_FIELDS
    recovered = await selected_read_scopes(operator, "routine")
    routines = await routine_service.list(owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    for routine_id, historical_owner in recovered.items():
        routine = await routine_service.read(routine_id, owner_principal_id=await selected_read_principal(operator, "routine", routine_id), owner_session_id=historical_owner)
        routine.update(RECOVERED_FIELDS)
        routines.append(routine)
    return {"routines": routines}


@routine_router.get("/{routine_id}")
async def get_routine(routine_id: str, request: Request):
    operator = _operator(request)
    from src.auth.ownership import selected_read_scopes, selected_read_principal, RECOVERED_FIELDS
    recovered = await selected_read_scopes(operator, "routine")
    try:
        routine = await routine_service.read(routine_id, owner_principal_id=(await selected_read_principal(operator, "routine", routine_id)) if routine_id in recovered else operator.principal.principal_id, owner_session_id=recovered.get(routine_id, operator.session_id))
        if routine_id in recovered:
            routine.update(RECOVERED_FIELDS)
        return routine
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/from-run")
async def create_routine(req: RoutineFromRunRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.from_run(req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/from-board/preview")
async def preview_board_routine(req: RoutineFromBoardPreviewRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.preview_from_board(
            req,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/from-board")
async def create_board_routine(req: RoutineFromBoardCreateRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.create_from_board(
            req,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/from-tasks/preview")
async def preview_procedure_from_tasks(req: ProcedureV2PreviewRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.preview_from_tasks(
            req,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except ProcedureV2Error as exc:
        raise _procedure_http_error(exc) from exc


@routine_router.post("/from-tasks")
async def create_procedure_from_tasks(req: ProcedureV2CreateRequest, request: Request):
    operator = _operator(request)
    try:
        payload, status_code = await routine_service.create_from_tasks(
            req,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
        return JSONResponse(status_code=status_code, content=payload)
    except ProcedureV2Error as exc:
        raise _procedure_http_error(exc) from exc


@routine_router.post("/{routine_id}/versions/{version}/package/preview")
async def preview_routine_package(routine_id: str, version: int, req: RoutinePackageRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.package_preview(
            routine_id,
            version,
            expected_routine_revision=req.expected_routine_revision,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.get("/{routine_id}/versions/{version}/export")
async def export_routine_procedure(routine_id: str, version: int, request: Request):
    """Download the authenticated owner's exact installed procedure version."""

    operator = _operator(request)
    try:
        return await routine_service.export_procedure(
            routine_id,
            version,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/versions/{version}/package/review")
async def review_routine_package(routine_id: str, version: int, req: RoutinePackageRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.review_package(
            routine_id,
            version,
            expected_routine_revision=req.expected_routine_revision,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/versions/{version}/package/approvals")
async def prepare_routine_package_approval(routine_id: str, version: int, req: RoutinePackageRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.prepare_package_approval(
            routine_id,
            version,
            expected_routine_revision=req.expected_routine_revision,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/versions/{version}/package/approvals/{approval_id}/decision")
async def decide_routine_package_approval(
    routine_id: str,
    version: int,
    approval_id: str,
    req: RoutinePackageDecisionRequest,
    request: Request,
):
    operator = _operator(request)
    try:
        return await routine_service.decide_package_approval(
            routine_id,
            version,
            approval_id,
            req,
            expected_routine_revision=req.expected_routine_revision,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/versions/{version}/package/activate")
async def activate_routine_package(
    routine_id: str,
    version: int,
    req: RoutinePackageActivationRequest,
    request: Request,
):
    operator = _operator(request)
    try:
        return await routine_service.activate_package(
            routine_id,
            version,
            req,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/versions")
async def add_routine_version(routine_id: str, req: RoutineVersionRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.add_version(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/install")
async def install_routine(routine_id: str, req: RoutineInstallRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.install(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/activate")
async def activate_routine(routine_id: str, req: RoutineActivateRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.activate(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/invoke")
async def invoke_routine(routine_id: str, req: RoutineInvokeRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.invoke(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/invoke-v2")
async def invoke_procedure_v2(routine_id: str, req: ProcedureV2InvokeRequest, request: Request):
    operator = _operator(request)
    try:
        payload, status_code = await routine_service.invoke_v2(
            routine_id,
            req,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
        return JSONResponse(status_code=status_code, content=payload)
    except ProcedureV2Error as exc:
        raise _procedure_http_error(exc) from exc


@routine_router.post("/{routine_id}/schedule-v2")
async def schedule_procedure_v2(routine_id: str, req: ProcedureV2ScheduleRequest, request: Request):
    operator = _operator(request)
    try:
        payload, status_code = await routine_service.schedule_v2(
            routine_id,
            req,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
        return JSONResponse(status_code=status_code, content=payload)
    except ProcedureV2Error as exc:
        raise _procedure_http_error(exc) from exc


@routine_router.post("/{routine_id}/invocations/{job_id}/execute")
async def execute_routine(routine_id: str, job_id: str, req: RoutineExecuteRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.execute_invocation(routine_id, job_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/invocations/{job_id}/prepare-publication")
async def prepare_routine_publication(routine_id: str, job_id: str, req: RoutinePublicationRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.prepare_publication(
            routine_id,
            job_id,
            req,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            external_mutation_granted=_operator_has_grant(operator, AuthorityGrant.EXTERNAL_MUTATION),
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/pause")
async def pause_routine(routine_id: str, req: RoutineRevisionRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.pause_or_revoke(routine_id, state="paused", expected_revision=req.expected_routine_revision, reason=req.reason, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/revoke")
async def revoke_routine(routine_id: str, req: RoutineRevisionRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.pause_or_revoke(routine_id, state="revoked", expected_revision=req.expected_routine_revision, reason=req.reason, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/rollback")
async def rollback_routine(routine_id: str, req: RoutineRollbackRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.rollback(routine_id, req, owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
    except RoutineError as exc:
        raise _http_error(exc) from exc


@routine_router.post("/{routine_id}/invocations/{job_id}/recover")
async def recover_routine(routine_id: str, job_id: str, req: RoutineRecoverRequest, request: Request):
    operator = _operator(request)
    try:
        return await routine_service.recover(
            routine_id,
            job_id,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            external_mutation_granted=_operator_has_grant(operator, AuthorityGrant.EXTERNAL_MUTATION),
        )
    except RoutineError as exc:
        raise _http_error(exc) from exc


__all__ = [
    "ProcedureV2CreateRequest",
    "ProcedureV2Error",
    "ProcedureV2InvokeRequest",
    "ProcedureV2PreviewRequest",
    "ProcedureV2ScheduleRequest",
    "RoutineError",
    "RoutineFromBoardCreateRequest",
    "RoutineFromBoardPreviewRequest",
    "RoutinePackageActivationRequest",
    "RoutinePackageDecisionRequest",
    "RoutinePackageRequest",
    "RoutineService",
    "routine_router",
    "routine_service",
]
