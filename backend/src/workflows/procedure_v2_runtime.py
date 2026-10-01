"""Durable runtime for the fixed ``guardian-routine.v2`` procedures.

The procedure API owns routine/version selection and source proof.  This
module owns the small execution kernel that turns one already-authorized
descriptor into a parent durable run and a deterministic sequence of leaf
runs.  It deliberately has no generic tool registry or model-selected
dispatch surface.  Leaf adapters are injected at the boundary so the
registered browser, watch, and Calendar adapters remain the owners of their
provider policy and readback rules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any

from src.workflows.job_runtime import (
    DurableJobError,
    DurableJobIdempotencyConflict,
    DurableJobIdentity,
    DurableJobLeaseError,
    DurableJobSpec,
    DurableJobTransitionError,
    UNCERTAIN_EXTERNAL_EFFECT_STATUSES,
    durable_job_repository,
)


from src.workflows.procedure_contracts import (
    PROCEDURE_V2_TEMPLATES,
    ROUTINE_V2_CAPABILITY_VERSION,
)


ROUTINE_V2_JOB_KIND = "guardian_routine_v2"
ROUTINE_V2_MAX_STEPS = 2
ROUTINE_V2_MAX_SECONDS = 300
# Keep the runtime's historical exported name as a view of the one canonical
# server registry.  This is intentionally an alias, not a second allowlist.
ROUTINE_V2_TEMPLATES = PROCEDURE_V2_TEMPLATES


class ProcedureV2RuntimeError(RuntimeError):
    """A bounded, operator-visible runtime failure."""

    def __init__(self, code: str, message: str | None = None, *, unknown: bool = False):
        self.code = code
        self.unknown = bool(unknown)
        super().__init__(message or code)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        dumped = dumper(mode="json")
        if isinstance(dumped, Mapping):
            return {
                key: _as_mapping(child) if _is_descriptor_object(child) else child
                for key, child in dumped.items()
            }
    if hasattr(value, "__dict__"):
        dumped = dict(vars(value))
        if isinstance(dumped, Mapping):
            return {
                key: _as_mapping(child) if _is_descriptor_object(child) else child
                for key, child in dumped.items()
            }
    if is_dataclass(value):
        dumped = asdict(value)
        if isinstance(dumped, Mapping):
            return dumped
    raise ProcedureV2RuntimeError("procedure_descriptor_invalid")


def _is_descriptor_object(value: Any) -> bool:
    return not isinstance(value, (str, bytes, bytearray, int, float, bool, type(None), Mapping, list, tuple, set)) and (
        callable(getattr(value, "model_dump", None)) or hasattr(value, "__dict__") or is_dataclass(value)
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _descriptor_plan(descriptor: Mapping[str, Any]) -> Mapping[str, Any]:
    plan = descriptor.get("plan")
    if not isinstance(plan, Mapping) and isinstance(descriptor.get("version"), Mapping):
        plan = descriptor["version"].get("plan")
    if not isinstance(plan, Mapping):
        raise ProcedureV2RuntimeError("procedure_plan_missing")
    return plan


def _descriptor_field(descriptor: Mapping[str, Any], field: str, default: Any = None) -> Any:
    if field in descriptor:
        return descriptor[field]
    nested = descriptor.get("version")
    if isinstance(nested, Mapping) and field in nested:
        return nested[field]
    return default


def _step_input(step: Mapping[str, Any], descriptor: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the one server-owned executable input for one plan step."""

    step_id = _text(step.get("step_id"))
    values = descriptor.get("executable_steps")
    if isinstance(values, Mapping) and isinstance(values.get(step_id), Mapping):
        return values[step_id]
    return {}


def _plan_steps(descriptor: Mapping[str, Any]) -> tuple[str, str, tuple[Mapping[str, Any], ...]]:
    plan = _descriptor_plan(descriptor)
    template_id = _text(plan.get("template_id") or descriptor.get("template_id"))
    if template_id not in ROUTINE_V2_TEMPLATES:
        raise ProcedureV2RuntimeError("procedure_template_invalid")
    if plan.get("schema_version") != 2 or plan.get("verifier") != "leaf_readbacks":
        raise ProcedureV2RuntimeError("procedure_plan_invalid")
    limits = plan.get("limits")
    if not isinstance(limits, Mapping) or int(limits.get("max_steps") or 0) != ROUTINE_V2_MAX_STEPS or int(limits.get("max_total_seconds") or 0) != ROUTINE_V2_MAX_SECONDS:
        raise ProcedureV2RuntimeError("procedure_limits_invalid")
    try:
        from src.workflows.procedure_contracts import get_procedure_template

        template = _as_mapping(get_procedure_template(template_id))
    except (ImportError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ProcedureV2RuntimeError("procedure_template_registry_unavailable") from exc
    template_steps = template.get("steps")
    if not isinstance(template_steps, Sequence) or isinstance(template_steps, (str, bytes, bytearray)):
        raise ProcedureV2RuntimeError("procedure_template_steps_invalid")
    expected_steps: list[tuple[str, str, str]] = []
    for item in template_steps:
        if not isinstance(item, Mapping):
            raise ProcedureV2RuntimeError("procedure_template_step_invalid")
        step_id = _text(item.get("step_id"))
        capability_id = _text(item.get("capability_id"))
        capability_version = _text(item.get("capability_version"))
        if not step_id or not capability_id or not capability_version:
            raise ProcedureV2RuntimeError("procedure_template_step_invalid")
        expected_steps.append((step_id, capability_id, capability_version))
    expected_ids = tuple(item[0] for item in expected_steps)
    if template_id not in ROUTINE_V2_TEMPLATES or len(expected_ids) > ROUTINE_V2_MAX_STEPS:
        raise ProcedureV2RuntimeError("procedure_template_invalid")
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes, bytearray)) or len(raw_steps) != len(expected_ids):
        raise ProcedureV2RuntimeError("procedure_steps_invalid")
    normalized: list[Mapping[str, Any]] = []
    for (expected_id, expected_capability_id, expected_capability_version), raw_step in zip(expected_steps, raw_steps, strict=True):
        if not isinstance(raw_step, Mapping):
            raise ProcedureV2RuntimeError("procedure_step_invalid")
        step_id = _text(raw_step.get("step_id"))
        capability_id = _text(raw_step.get("capability_id"))
        capability_version = _text(raw_step.get("capability_version"))
        if step_id != expected_id or capability_id != expected_capability_id or capability_version != expected_capability_version:
            raise ProcedureV2RuntimeError("procedure_step_binding_invalid")
        if not _text(raw_step.get("typed_input_ref")) or not _text(raw_step.get("typed_input_digest")):
            raise ProcedureV2RuntimeError("procedure_step_input_binding_missing")
        normalized.append(raw_step)
    raw_version = descriptor.get("version")
    if isinstance(raw_version, Mapping):
        raw_version = raw_version.get("version") or raw_version.get("routine_version")
    return template_id, _text(raw_version or descriptor.get("routine_version") or "1"), tuple(normalized)


def deterministic_child_job_id(parent_job_id: str, template_id: str, version: int | str, step_id: str) -> str:
    """Return the stable UUIDv5 identity for a fixed procedure leaf."""

    parent = _text(parent_job_id)
    if not parent or not _text(template_id) or not _text(step_id):
        raise ValueError("parent/template/step identity is required")
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"seraph:guardian-routine.v2:{parent}:{template_id}:{int(version)}:{step_id}",
        )
    )


def _native_child_job_id(
    parent_job_id: str,
    template_id: str,
    version: int | str,
    step: Mapping[str, Any],
    descriptor: Mapping[str, Any],
) -> tuple[str, str]:
    """Return the deterministic occurrence and adapter-native child IDs.

    Board-backed leaves use the deterministic UUID directly. Source Watch
    keeps that UUID as its occurrence and wraps it in the adapter-owned
    ``source-watch:{watch}:{occurrence}`` durable identity. Recovery must
    compare each form at its own boundary rather than treating the wrapper as
    a caller-selected child ID.
    """

    occurrence_id = deterministic_child_job_id(parent_job_id, template_id, version, _text(step.get("step_id")))
    if _text(step.get("capability_id")) != "guardian.research-watch.v1":
        return occurrence_id, occurrence_id
    inputs = _step_input(step, descriptor)
    watch_id = _text(inputs.get("watch_id"))
    if not watch_id:
        raise ProcedureV2RuntimeError("watch_input_binding_invalid")
    return occurrence_id, f"source-watch:{watch_id}:{occurrence_id}"


def _lease(projection: Mapping[str, Any]) -> tuple[str, int]:
    lease = projection.get("lease") if isinstance(projection.get("lease"), Mapping) else {}
    owner = _text(lease.get("owner"))
    try:
        fence = int(lease.get("fencing_token") or 0)
    except (TypeError, ValueError):
        fence = 0
    if not owner or fence <= 0:
        raise ProcedureV2RuntimeError("procedure_parent_lease_missing")
    return owner, fence


def _safe_result(result: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "status",
        "reason_code",
        "recovery_action",
        "readback_id",
        "artifact_ref",
        "artifact_sha256",
        "artifact_id",
        "packet_id",
        "job_id",
        "child_job_id",
        "memory_status",
        "skipped",
        "verified",
        "no_change",
        "unknown_external_effect",
    )
    return {key: result[key] for key in allowed if key in result and result[key] is not None}


def _native_child_refs(child: Mapping[str, Any]) -> dict[str, Any]:
    """Project the server-created native child handoff into safe checkpoint refs.

    The parent checkpoint is the recovery index for a procedure, so a child
    job id alone is insufficient after a restart.  Keep the durable task,
    attempt, input artifact, and capability identities here while excluding
    the typed input payload and adapter-private state.
    """

    binding = child.get("_procedure_binding")
    task = getattr(binding, "child_task", None) if binding is not None else None
    attempt = getattr(binding, "child_attempt", None) if binding is not None else None

    def value(name: str, fallback: Any = None) -> Any:
        if binding is not None:
            bound = getattr(binding, name, None)
            if bound is not None:
                return bound
        return child.get(name, fallback)

    refs: dict[str, Any] = {}
    candidates = {
        "child_job_id": value("child_job_id", child.get("job_id") or child.get("run_identity")),
        "child_task_id": getattr(task, "task_id", None),
        "child_attempt_id": getattr(attempt, "attempt_id", None),
        "child_task_revision": getattr(task, "task_revision", None),
        "child_admission_task_revision": value("child_admission_task_revision"),
        "child_fencing_token": getattr(attempt, "fencing_token", None),
        "child_input_artifact_id": value("child_input_artifact_id", child.get("input_artifact_id")),
        "child_input_artifact_digest": value("child_input_artifact_digest", child.get("input_artifact_digest")),
        "child_capability_id": value("child_capability_id", child.get("capability_id")),
        "child_capability_version": value("child_capability_version", child.get("capability_version")),
    }
    for key, item in candidates.items():
        if item is None or item == "":
            continue
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            continue
        refs[key] = item
    return refs


def _safe_watch_artifact_refs(value: Any) -> list[dict[str, Any]]:
    """Keep only the immutable identities needed to replay a Watch leaf."""

    if not isinstance(value, (list, tuple)):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("verified") is not True:
            continue
        artifact_id = _text(item.get("artifact_id"))
        file_path = _text(item.get("file_path"))
        content_sha256 = _text(item.get("content_sha256")).lower()
        artifact_type = _text(item.get("artifact_type"))
        if not artifact_id or not file_path or len(content_sha256) != 64:
            continue
        refs.append(
            {
                "artifact_id": artifact_id,
                "file_path": file_path,
                "content_sha256": content_sha256,
                "artifact_type": artifact_type,
                "verified": True,
            }
        )
    return refs


def _watch_child_refs(
    child: Mapping[str, Any],
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a Source Watch native root into a typed recovery checkpoint.

    Source Watch has no Work Board child task.  Its checkpoint therefore binds
    the native occurrence to the reviewed watch, plan, goal, operator session,
    and routine parent fence.  Only artifact identities and the no-change bit
    cross the coordinator boundary; source text remains adapter-owned.
    """

    job = child.get("job") if isinstance(child.get("job"), Mapping) else child
    inputs = job.get("inputs") if isinstance(job.get("inputs"), Mapping) else {}
    authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
    binding = child.get("_v2_watch_binding") if isinstance(child.get("_v2_watch_binding"), Mapping) else {}

    def first(*values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return None

    refs: dict[str, Any] = {
        "child_job_id": first(job.get("job_id"), job.get("run_identity"), child.get("job_id")),
        "child_capability_id": first(job.get("capability_id"), authority.get("capability_id")),
        "child_capability_version": first(job.get("capability_version"), child.get("capability_version")),
        "step_id": first(binding.get("step_id"), authority.get("routine_step_id")),
        "watch_id": first(inputs.get("watch_id"), authority.get("watch_id")),
        "watch_plan_revision": first(
            job.get("plan_revision"),
            authority.get("plan_revision"),
            inputs.get("expected_plan_revision"),
        ),
        "watch_occurrence_id": first(
            binding.get("occurrence_id"),
            inputs.get("occurrence_id"),
            authority.get("occurrence_id"),
        ),
        "watch_native_job_id": first(job.get("job_id"), job.get("run_identity"), child.get("job_id")),
        "watch_owner_principal_id": first(
            job.get("operator_principal_id"),
            authority.get("goal_owner_principal_id"),
            authority.get("owner_principal_id"),
        ),
        "watch_owner_session_id": first(
            job.get("operator_session_id"),
            inputs.get("owner_session_id"),
            authority.get("goal_owner_session_id"),
            authority.get("session_id"),
        ),
        "watch_goal_id": first(job.get("goal_id"), authority.get("goal_id")),
        "watch_goal_revision": first(job.get("goal_revision"), authority.get("goal_revision")),
        "watch_parent_job_id": first(binding.get("parent_job_id"), authority.get("routine_parent_job_id")),
        "watch_parent_fencing_token": first(
            binding.get("parent_fencing_token"), authority.get("routine_parent_fencing_token")
        ),
        "watch_step_id": first(binding.get("step_id"), authority.get("routine_step_id")),
    }
    for key in (
        "child_job_id",
        "child_capability_id",
        "child_capability_version",
        "step_id",
        "watch_id",
        "watch_occurrence_id",
        "watch_native_job_id",
        "watch_owner_principal_id",
        "watch_owner_session_id",
        "watch_goal_id",
        "watch_parent_job_id",
        "watch_step_id",
    ):
        value = _text(refs.get(key))
        if value:
            refs[key] = value
        else:
            refs.pop(key, None)
    for key in ("watch_plan_revision", "watch_goal_revision", "watch_parent_fencing_token"):
        value = refs.get(key)
        if type(value) is int and value > 0:
            refs[key] = value
        else:
            refs.pop(key, None)
    if result is not None:
        status = _text(result.get("status") or result.get("durable_status"))
        if status:
            refs["watch_terminal_status"] = status
        packet_id = _text(result.get("packet_id"))
        if packet_id:
            refs["watch_packet_id"] = packet_id
        artifact_refs = _safe_watch_artifact_refs(result.get("artifact_refs"))
        if artifact_refs:
            refs["watch_artifact_refs"] = artifact_refs
        if status in {"no_change", "baseline_initialized", "skipped_verified"} or result.get("no_change") is True:
            refs["watch_no_change"] = True
    return refs


def _result_status(result: Mapping[str, Any]) -> str:
    return _text(result.get("status") or result.get("durable_status")) or "blocked"


def _verified_readback(result: Mapping[str, Any]) -> bool:
    if result.get("verified") is True or result.get("status") in {"succeeded", "completed", "no_change", "baseline_initialized"}:
        readback = result.get("readback")
        if isinstance(readback, Mapping):
            return bool(readback.get("readback_id") and readback.get("verified_at")) or readback.get("verified") is True
        return bool(
            result.get("readback_id")
            or result.get("artifact_ref")
            or result.get("artifact_id")
            or result.get("packet_id")
        )
    return False


LeafExecutor = Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
LeafAdmitter = Callable[..., Awaitable[Mapping[str, Any]]]


class ProcedureV2Runtime:
    """Coordinate one v2 parent and its fixed, serial leaf children."""

    def __init__(
        self,
        *,
        jobs: Any | None = None,
        leaf_executors: Mapping[str, LeafExecutor] | None = None,
        resolver: Callable[..., Awaitable[Any]] | None = None,
        session_provider: Any | None = None,
        board_repository: Any | None = None,
        leaf_admitters: Mapping[str, LeafAdmitter] | None = None,
        runtime_controls: Any | None = None,
    ) -> None:
        self.jobs = jobs or durable_job_repository
        self.leaf_executors = dict(leaf_executors or {})
        self.resolver = resolver
        self.session_provider = session_provider
        self.board_repository = board_repository
        self.leaf_admitters = dict(leaf_admitters or {})
        self.runtime_controls = runtime_controls
        # Production dispatcher wiring installs the owner-bound validator at
        # execution time.  Keeping this callback optional preserves the pure
        # coordinator tests while ensuring the managed path cannot replay a
        # native child without a fresh Board/session read.
        self.replay_binding_verifier: Callable[..., Awaitable[Any]] | None = None

    async def _resolve_descriptor(
        self,
        descriptor: Mapping[str, Any] | Any | None,
        *,
        routine_id: str,
        version: int,
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str,
        expected_goal_revision: int,
        parameters: Mapping[str, Any],
        invocation_uuid: str,
    ) -> Mapping[str, Any]:
        if descriptor is not None:
            resolved = _as_mapping(descriptor)
            version_descriptor = resolved.get("version")
            if isinstance(version_descriptor, Mapping) and isinstance(version_descriptor.get("plan"), Mapping):
                # ``validate_v2_invocation_authority`` returns a typed
                # descriptor object.  Convert that one canonical wire shape
                # to the runtime mapping without accepting legacy aliases or
                # caller-provided compatibility fields.
                resolved = {
                    **dict(version_descriptor),
                    "version": version_descriptor.get("version"),
                    "routine_id": version_descriptor.get("routine_id"),
                    "goal_id": resolved.get("goal_id"),
                    "goal_revision": resolved.get("goal_revision"),
                    "parameters": resolved.get("parameters"),
                    "invocation_uuid": resolved.get("invocation_uuid"),
                    "scope": resolved.get("scope"),
                    "executable_steps": resolved.get("executable_steps") or {},
                }
            _plan_steps(resolved)
            return resolved
        resolver = self.resolver
        if resolver is None:
            try:
                from src.workflows.routines import validate_v2_invocation_authority
            except (ImportError, AttributeError) as exc:
                raise ProcedureV2RuntimeError("procedure_authority_resolver_unavailable") from exc
            resolver = validate_v2_invocation_authority
        resolved = await resolver(
            routine_id,
            int(version),
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=goal_id,
            expected_goal_revision=int(expected_goal_revision),
            parameters=dict(parameters),
            invocation_uuid=invocation_uuid,
        )
        descriptor_mapping = _as_mapping(resolved)
        version_descriptor = descriptor_mapping.get("version")
        if isinstance(version_descriptor, Mapping) and isinstance(version_descriptor.get("plan"), Mapping):
            descriptor_mapping = {
                **dict(version_descriptor),
                "version": version_descriptor.get("version"),
                "routine_id": version_descriptor.get("routine_id"),
                "goal_id": descriptor_mapping.get("goal_id"),
                "goal_revision": descriptor_mapping.get("goal_revision"),
                "parameters": descriptor_mapping.get("parameters"),
                "invocation_uuid": descriptor_mapping.get("invocation_uuid"),
                "scope": descriptor_mapping.get("scope"),
                "executable_steps": descriptor_mapping.get("executable_steps") or {},
            }
        _plan_steps(descriptor_mapping)
        return descriptor_mapping

    @staticmethod
    def _parent_identity(inputs: Mapping[str, Any], task_id: str, attempt_id: str) -> tuple[str, str]:
        routine_id = _text(inputs.get("routine_id"))
        version = int(inputs.get("version") or 0)
        invocation_uuid = _text(inputs.get("invocation_uuid")) or f"board:{task_id}:{attempt_id}"
        if not routine_id or version <= 0:
            raise ProcedureV2RuntimeError("procedure_invocation_binding_missing")
        try:
            invocation_uuid = str(uuid.UUID(invocation_uuid))
        except (ValueError, AttributeError):
            # Scheduled occurrences use opaque deterministic IDs.  Keep them
            # bounded and stable without treating arbitrary caller text as a
            # capability selector.
            if len(invocation_uuid) > 256:
                raise ProcedureV2RuntimeError("procedure_invocation_id_invalid")
        return f"procedure-v2:{routine_id}:{version}:{invocation_uuid}", invocation_uuid

    async def admit_parent(
        self,
        *,
        task: Any,
        attempt: Any,
        inputs: Mapping[str, Any],
        runtime_seconds: int = ROUTINE_V2_MAX_SECONDS,
        descriptor: Mapping[str, Any] | Any | None = None,
    ) -> dict[str, Any]:
        """Perform effect-free v2 parent admission for dispatcher linking."""

        routine_id = _text(inputs.get("routine_id"))
        version = int(inputs.get("version") or 0)
        goal_id = _text(getattr(task, "goal_id", None) or inputs.get("goal_id"))
        goal_revision = int(getattr(task, "goal_revision", None) or inputs.get("expected_goal_revision") or 0)
        owner_principal_id = _text(getattr(task, "owner_principal_id", None))
        owner_session_id = _text(getattr(task, "owner_session_id", None))
        task_id = _text(getattr(task, "task_id", None))
        attempt_id = _text(getattr(attempt, "attempt_id", None))
        parent_id, invocation_uuid = self._parent_identity(inputs, task_id, attempt_id)
        resolved = await self._resolve_descriptor(
            descriptor,
            routine_id=routine_id,
            version=version,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=goal_id,
            expected_goal_revision=goal_revision,
            parameters=inputs.get("parameters") if isinstance(inputs.get("parameters"), Mapping) else inputs,
            invocation_uuid=invocation_uuid,
        )
        template_id, descriptor_version, steps = _plan_steps(resolved)
        if int(descriptor_version or version) != version:
            raise ProcedureV2RuntimeError("procedure_version_mismatch")
        plan = _descriptor_plan(resolved)
        plan_digest = _text(resolved.get("plan_digest")) or _digest(plan)
        source_proof_digest = _text(resolved.get("source_proof_digest") or resolved.get("source_provenance_digest"))
        authority = {
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": owner_session_id,
            "operator_session_id": owner_session_id,
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "routine_id": routine_id,
            "routine_version": version,
            "routine_revision": int(resolved.get("routine_revision") or inputs.get("expected_routine_revision") or 0),
            "package_digest": _text(resolved.get("installed_package_digest")),
            "board_task_id": task_id,
            "board_attempt_id": attempt_id,
            "board_task_revision": int(getattr(task, "task_revision", 0) or 0),
            "board_fencing_token": int(getattr(attempt, "fencing_token", 0) or 0),
            "input_artifact_id": _text(getattr(task, "input_artifact_id", None)),
            "input_artifact_digest": _text(getattr(task, "typed_input_digest", None)),
            "template_id": template_id,
            "plan_digest": plan_digest,
            "source_proof_digest": source_proof_digest or None,
            "source_refs": resolved.get("source_refs") or [],
            "invocation_uuid": invocation_uuid,
            "scope": _text(resolved.get("scope") or f"procedure-v2:{routine_id}:{version}"),
            "capability_id": ROUTINE_V2_CAPABILITY_VERSION,
            "capability_versions": [
                _text(step.get("capability_version")) for step in steps
            ],
            "plan": plan,
            "executable_steps": resolved.get("executable_steps") or {},
            "finite_authority": True,
            "budget_microusd": 0,
            "limits": {"max_steps": ROUTINE_V2_MAX_STEPS, "max_total_seconds": ROUTINE_V2_MAX_SECONDS},
        }
        # The parent owns orchestration only.  Remote/browser resources belong
        # to the leaf adapter and must never be held while another leaf waits.
        bounded_seconds = max(1, min(int(runtime_seconds), ROUTINE_V2_MAX_SECONDS))
        spec = DurableJobSpec(
            identity=DurableJobIdentity(
                job_id=parent_id,
                owner_kind="user",
                owner_principal_id=owner_principal_id,
                job_kind=ROUTINE_V2_JOB_KIND,
                capability_version=ROUTINE_V2_CAPABILITY_VERSION,
                idempotency_scope="work-board-attempt",
                idempotency_key=f"{task_id}:{attempt_id}",
            ),
            inputs={
                "routine_id": routine_id,
                "version": version,
                "template_id": template_id,
                "plan_digest": plan_digest,
                "source_proof_digest": source_proof_digest or None,
                "source_refs": resolved.get("source_refs") or [],
                "invocation_uuid": invocation_uuid,
                "step_ids": [_text(step.get("step_id")) for step in steps],
                # This is the reviewed, server-derived executable plan.  It
                # contains no source text or credentials and lets a generated
                # fixed wrapper recover after a process restart without
                # asking the caller to resubmit mutable inputs.
                "plan": plan,
                "executable_steps": resolved.get("executable_steps") or {},
            },
            session_id=owner_session_id,
            conversation_id=owner_session_id,
            operator_session_id=owner_session_id,
            goal_id=goal_id or None,
            goal_revision=goal_revision or None,
            priority=int(getattr(task, "priority", 50) or 50),
            resource_claims=(),
            declared_authority=authority,
            deadline_at=_now() + timedelta(seconds=bounded_seconds),
            max_attempts=1,
            max_outstanding_jobs=1,
            run_fingerprint=_digest({"parent": parent_id, "plan_digest": plan_digest, "invocation_uuid": invocation_uuid}),
            budget_microusd=0,
        )
        admitted = await self.jobs.admit_job(spec)
        admitted_id = _text(admitted.get("job_id") or admitted.get("run_identity"))
        if admitted_id != parent_id:
            raise DurableJobIdempotencyConflict("procedure parent admission returned a different durable root")
        if _text(admitted.get("capability_version")) != ROUTINE_V2_CAPABILITY_VERSION:
            raise DurableJobIdempotencyConflict("procedure parent capability version changed")
        if _text(admitted.get("input_digest")) != _digest(spec.inputs):
            raise DurableJobIdempotencyConflict("procedure parent input digest changed")
        return {
            "job_id": parent_id,
            "status": _text(admitted.get("status")) or "accepted",
            "input_digest": admitted.get("input_digest"),
            "authority_digest": admitted.get("authority_digest"),
            "run_fingerprint": admitted.get("run_fingerprint"),
            "template_id": template_id,
            "plan_digest": plan_digest,
            "admission_only": True,
            "descriptor": resolved,
            "job": admitted,
        }

    async def _ensure_claimed(self, job_id: str, *, lease_seconds: int) -> Mapping[str, Any]:
        current = await self.jobs.get_job(job_id)
        if not isinstance(current, Mapping):
            raise ProcedureV2RuntimeError("procedure_parent_missing")
        if _text(current.get("status")) == "accepted":
            current = await self.jobs.queue_job(job_id, expected_revision=current.get("revision"))
        if _text(current.get("status")) == "queued":
            lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            current = await self.jobs.claim_job(
                job_id,
                owner=f"guardian-routine:{job_id}",
                lease_seconds=max(1, min(int(lease_seconds), ROUTINE_V2_MAX_SECONDS)),
                expected_state="queued",
                expected_revision=current.get("revision"),
                expected_fencing_token=lease.get("fencing_token", current.get("fencing_token")),
            )
        return current

    async def _materialize_child_board(
        self,
        parent: Mapping[str, Any],
        *,
        step: Mapping[str, Any],
        descriptor: Mapping[str, Any],
        child_id: str,
        inputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create and claim one native leaf board task with fresh input bytes.

        The procedure input is a reviewed source reference.  It is never bound
        to the child task.  A new owner/goal-bound artifact and a new board
        attempt are created under an idempotency key derived from the durable
        parent/step/child identity.  This helper deliberately performs only
        local SQLite work; the capability adapter owns every provider effect.
        """

        if self.session_provider is None or self.board_repository is None:
            raise ProcedureV2RuntimeError("procedure_leaf_board_binding_unavailable")
        from sqlalchemy import select
        from src.db.models import WorkBoardAttempt, WorkBoardStatus
        from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardTaskCreate
        from src.work_board.input_artifacts import (
            prepare_input_artifact,
            resolve_input_artifact_for_copy,
        )
        from src.work_board.repository import WorkBoardOwner

        parent_id = _text(parent.get("job_id") or parent.get("run_identity"))
        parent_authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        owner_id = _text(parent_authority.get("principal") or (parent.get("owner") or {}).get("principal_id"))
        session_id = _text(parent_authority.get("session_id") or parent.get("operator_session_id") or parent.get("session_id"))
        goal_id = _text(parent.get("goal_id") or parent_authority.get("goal_id"))
        goal_revision = int(parent.get("goal_revision") or parent_authority.get("goal_revision") or 0)
        parent_task_id = _text(parent_authority.get("board_task_id"))
        parent_attempt_id = _text(parent_authority.get("board_attempt_id"))
        parent_board_fence = int(parent_authority.get("board_fencing_token") or 0)
        step_id = _text(step.get("step_id"))
        capability_id = _text(step.get("capability_id"))
        capability_version = _text(step.get("capability_version"))
        if not owner_id or not session_id or not goal_id or goal_revision < 1 or not parent_task_id or not parent_attempt_id or parent_board_fence < 1:
            raise ProcedureV2RuntimeError("procedure_parent_board_binding_missing")

        owner = WorkBoardOwner(principal_id=owner_id, session_id=session_id)
        if capability_id == "browser.public-task.v1":
            # The source artifact is immutable reviewed plan data.  The copy
            # helper verifies its owner, capability, canonical bytes and hash,
            # but deliberately does not require its old goal revision: v2
            # invocation may select a new current goal.
            source = None
            try:
                async with self.session_provider() as db:
                    source = await resolve_input_artifact_for_copy(
                        db,
                        owner,
                        typed_input_ref=_text(step.get("typed_input_ref")),
                        typed_input_digest=_text(step.get("typed_input_digest")),
                        capability_id=capability_id,
                        goal_id=goal_id,
                        goal_revision=goal_revision,
                        allow_goal_change=True,
                    )
            except Exception as exc:
                raise ProcedureV2RuntimeError("browser_leaf_input_unavailable") from exc
            leaf_input = source.input
        elif capability_id == "calendar.meeting-prep.v1":
            # Calendar's exact M5 input is selected afresh for this invocation;
            # it must never reuse a prior event artifact as authority.
            parameters = inputs.get("parameters")
            if not isinstance(parameters, Mapping):
                raise ProcedureV2RuntimeError("calendar_leaf_input_invalid")
            leaf_input = dict(parameters)
        else:
            raise ProcedureV2RuntimeError("procedure_leaf_capability_unregistered")

        artifact_key = f"procedure-v2:{parent_id}:{step_id}:{child_id}"
        async with self.session_provider() as db:
            artifact = await prepare_input_artifact(
                db,
                owner,
                WorkBoardInputArtifactCreate(
                    schema_version=1,
                    capability_id=capability_id,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                    input=dict(leaf_input),
                    idempotency_key=artifact_key,
                ),
            )

            async def publication_guard(check_db: Any) -> None:
                parent_task = await self.board_repository.get_task(check_db, owner, parent_task_id)
                current_attempt = (
                    await check_db.execute(
                        select(WorkBoardAttempt).where(
                            WorkBoardAttempt.task_id == parent_task_id,
                            WorkBoardAttempt.attempt_id == parent_attempt_id,
                        )
                    )
                ).scalar_one_or_none()
                if (
                    parent_task.status is not WorkBoardStatus.running
                    or int(parent_task.goal_revision) != goal_revision
                    or int(parent_task.task_revision) < int(parent_authority.get("board_task_revision") or 0)
                    or current_attempt is None
                    or current_attempt.ended_at is not None
                    or current_attempt.cancel_requested_at is not None
                    or int(current_attempt.fencing_token or 0) != parent_board_fence
                    or not current_attempt.lease_owner
                ):
                    raise ProcedureV2RuntimeError("procedure_parent_authority_stale")
                if current_attempt.workflow_run_id not in {None, parent_id}:
                    raise ProcedureV2RuntimeError("procedure_parent_workflow_binding_stale")

            mutation = await self.board_repository.create_task(
                db,
                owner,
                WorkBoardTaskCreate(
                    title=f"Procedure leaf: {step_id}",
                    body="",
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                    status=WorkBoardStatus.todo,
                    capability_id=capability_id,
                    input_artifact_id=artifact.artifact_id,
                    executor_id=f"seraph-work-board:{capability_id}",
                    priority=int(parent.get("priority") or 50),
                    idempotency_scope="guardian-routine-v2-leaf",
                    idempotency_key=f"{parent_id}:{step_id}:{child_id}",
                    origin_thread_id=session_id,
                ),
                origin_session_id=session_id,
                publication_authority_check=publication_guard,
            )
            child_task = mutation.task
            lease_owner = _text(getattr(self, "board_lease_owner", None)) or "service:work-board"
            promoted = await self.board_repository.promote_task_ready(
                db,
                child_task.task_id,
                expected_revision=int(child_task.task_revision),
                actor_principal_id=lease_owner,
                actor_session_id=f"{lease_owner}:session",
            )
            if promoted is None:
                raise ProcedureV2RuntimeError("procedure_leaf_not_ready")
            child_task = promoted.task
            claim = await self.board_repository.claim_ready_task(
                db,
                child_task.task_id,
                expected_revision=int(child_task.task_revision),
                lease_owner=lease_owner,
                lease_seconds=max(1, min(self._remaining_seconds(parent), ROUTINE_V2_MAX_SECONDS)),
                actor_principal_id=lease_owner,
                actor_session_id=f"{lease_owner}:session",
            )
        if claim is None:
            raise ProcedureV2RuntimeError("procedure_leaf_claim_unavailable")
        return {
            "task": claim.task,
            "attempt": claim.attempt,
            "input_artifact_id": artifact.artifact_id,
            "input_artifact_digest": artifact.typed_input_digest,
            "input_artifact_ref": artifact.typed_input_ref,
            "input": dict(leaf_input),
            "capability_id": capability_id,
            "capability_version": capability_version,
        }

    async def _admit_child(
        self,
        parent: Mapping[str, Any],
        *,
        step: Mapping[str, Any],
        descriptor: Mapping[str, Any],
        child_id: str,
    ) -> Mapping[str, Any]:
        """Admit exactly one registered native leaf root.

        A v2 coordinator never creates a generic ``guardian_routine_v2_*``
        child.  Registered adapters either return their own durable root or
        fail closed before any external contact.
        """

        parent_id = _text(parent.get("job_id") or parent.get("run_identity"))
        parent_owner = parent.get("owner") if isinstance(parent.get("owner"), Mapping) else {}
        parent_owner_id = _text(parent_owner.get("principal_id"))
        session_id = _text(parent.get("operator_session_id") or parent.get("session_id"))
        parent_lease_owner, parent_fence = _lease(parent)
        self._remaining_seconds(parent)
        live_parent = await self.jobs.get_job(parent_id)
        if not isinstance(live_parent, Mapping) or _text(live_parent.get("status")) != "running":
            raise ProcedureV2RuntimeError("procedure_parent_not_running")
        self._remaining_seconds(live_parent)
        _live_parent_owner, live_parent_fence = _lease(live_parent)
        if live_parent_fence != parent_fence or _text(live_parent.get("job_id") or live_parent.get("run_identity")) != parent_id:
            raise ProcedureV2RuntimeError("procedure_parent_fence_stale")
        step_id = _text(step.get("step_id"))
        capability_id = _text(step.get("capability_id"))
        inputs = _step_input(step, descriptor)
        admitter = self.leaf_admitters.get(capability_id)
        if admitter is not None:
            binding = None
            if capability_id in {"browser.public-task.v1", "calendar.meeting-prep.v1"}:
                binding = await self._materialize_child_board(
                    parent,
                    step=step,
                    descriptor=descriptor,
                    child_id=child_id,
                    inputs=inputs,
                )
            admitted = await admitter(
                parent=parent,
                step=step,
                descriptor=descriptor,
                child_id=child_id,
                inputs=inputs,
                binding=binding,
            )
            if not isinstance(admitted, Mapping):
                raise ProcedureV2RuntimeError("procedure_leaf_admission_invalid")
            return dict(admitted)
        if capability_id == "guardian.research-watch.v1":
            from src.guardian.source_watch import source_watch_service

            watch_id = _text(inputs.get("watch_id"))
            expected_value = inputs.get("expected_plan_revision")
            if type(expected_value) is not int or expected_value < 1:
                raise ProcedureV2RuntimeError("watch_input_binding_invalid")
            expected_revision = expected_value
            if not watch_id:
                raise ProcedureV2RuntimeError("watch_input_binding_invalid")
            admitted = await source_watch_service.run_watch(
                watch_id,
                occurrence_id=child_id,
                expected_plan_revision=expected_revision,
                expected_owner_session_id=session_id,
                routine_parent_job_id=parent_id,
                routine_parent_fencing_token=parent_fence,
                routine_step_id=step_id,
                routine_parent_deadline_at=parent.get("deadline_at"),
                admit_only=True,
            )
            if not isinstance(admitted, Mapping):
                raise ProcedureV2RuntimeError("watch_leaf_admission_invalid")
            native_id = _text(admitted.get("job_id") or admitted.get("run_identity"))
            expected_native_id = f"source-watch:{watch_id}:{child_id}"
            if native_id != expected_native_id:
                raise ProcedureV2RuntimeError("watch_leaf_identity_mismatch")
            job_projection = admitted.get("job") if isinstance(admitted.get("job"), Mapping) else {}
            return {
                **dict(admitted),
                # Source Watch returns a compact admission receipt. Preserve
                # the native job's occurrence/authority envelope in this
                # server-only handoff so the second call cannot invent an
                # occurrence from a missing field.
                "inputs": dict(job_projection.get("inputs") or {}),
                "declared_authority": dict(job_projection.get("declared_authority") or {}),
                "_v2_watch_binding": {
                    "parent_job_id": parent_id,
                    "parent_fencing_token": parent_fence,
                    "step_id": step_id,
                    "occurrence_id": child_id,
                },
            }
        raise ProcedureV2RuntimeError("procedure_leaf_capability_unregistered")

    @staticmethod
    def _remaining_seconds(parent: Mapping[str, Any]) -> int:
        deadline = _parse_datetime(parent.get("deadline_at"))
        if deadline is None:
            raise ProcedureV2RuntimeError("procedure_parent_deadline_missing")
        remaining = int((deadline - _now()).total_seconds())
        if remaining <= 0:
            raise ProcedureV2RuntimeError("procedure_parent_deadline_expired")
        return max(1, min(ROUTINE_V2_MAX_SECONDS, remaining))

    async def _assert_parent_current_before_write(self, parent: Mapping[str, Any]) -> None:
        """Revalidate the parent before projecting a terminal receipt.

        Managed dispatcher runs install ``runtime_controls`` with the
        owner-bound Board/session validator.  The callback is deliberately
        server-only: the procedure descriptor cannot replace its task,
        attempt, owner, or fencing identities.  Pure coordinator tests may
        omit that callback; when they provide a durable deadline it is still
        enforced here.
        """

        if _text(parent.get("deadline_at")):
            self._remaining_seconds(parent)
        control = self.runtime_controls
        if control is None:
            return
        parent_id = _text(parent.get("job_id") or parent.get("run_identity"))
        owner = parent.get("owner") if isinstance(parent.get("owner"), Mapping) else {}
        authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
        binding = {
            "task_id": authority.get("board_task_id"),
            "attempt_id": authority.get("board_attempt_id"),
            "owner_principal_id": authority.get("principal") or owner.get("principal_id"),
            "owner_session_id": authority.get("session_id") or parent.get("operator_session_id") or parent.get("session_id"),
            "board_task_revision": authority.get("board_task_revision"),
            "board_fencing_token": authority.get("board_fencing_token"),
            "input_artifact_id": authority.get("input_artifact_id"),
            "routine_parent_job_id": parent_id,
            "routine_parent_fencing_token": lease.get("fencing_token"),
        }
        try:
            allowed = await control(**binding)
        except Exception as exc:  # trust-boundary failures are fail-closed
            raise ProcedureV2RuntimeError("procedure_parent_authority_stale", unknown=True) from exc
        if allowed is not True:
            raise ProcedureV2RuntimeError("procedure_parent_authority_stale", unknown=True)

    @staticmethod
    def _recorded_child_job_id(parent: Mapping[str, Any], step_id: str) -> str | None:
        """Read the last server-recorded native root for one step."""

        checkpoint = ProcedureV2Runtime._recorded_child_checkpoint(parent, step_id)
        if checkpoint is None:
            return None
        payload = checkpoint.get("payload")
        if isinstance(payload, Mapping):
            value = _text(payload.get("child_job_id"))
            if value:
                return value
        return None

    @staticmethod
    def _recorded_child_checkpoint(
        parent: Mapping[str, Any],
        step_id: str,
    ) -> Mapping[str, Any] | None:
        """Return the latest safe admission checkpoint for one native leaf."""

        checkpoints = parent.get("checkpoints")
        if not isinstance(checkpoints, list):
            return None
        admitted_id = f"procedure-v2:step:{step_id}:admitted"
        for receipt in reversed(checkpoints):
            if not isinstance(receipt, Mapping) or receipt.get("checkpoint_id") != admitted_id:
                continue
            if receipt.get("safe") is False:
                continue
            payload = receipt.get("payload")
            if isinstance(payload, Mapping) and _text(payload.get("child_job_id")):
                return receipt
        return None

    @staticmethod
    def _recorded_child_settled_checkpoint(
        parent: Mapping[str, Any],
        step_id: str,
    ) -> Mapping[str, Any] | None:
        """Return the terminal checkpoint, when a child reached settlement."""

        checkpoints = parent.get("checkpoints")
        if not isinstance(checkpoints, list):
            return None
        settled_id = f"procedure-v2:step:{step_id}:settled"
        for receipt in reversed(checkpoints):
            if not isinstance(receipt, Mapping) or receipt.get("checkpoint_id") != settled_id:
                continue
            if receipt.get("safe") is False:
                continue
            payload = receipt.get("payload")
            if isinstance(payload, Mapping) and _text(payload.get("child_job_id")):
                return receipt
        return None

    @staticmethod
    def _replay_checkpoint(
        admitted: Mapping[str, Any],
        settled: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        """Combine immutable admission refs with terminal result refs."""

        if settled is None:
            return admitted
        admitted_payload = admitted.get("payload") if isinstance(admitted.get("payload"), Mapping) else {}
        settled_payload = settled.get("payload") if isinstance(settled.get("payload"), Mapping) else {}
        return {**dict(admitted), "payload": {**dict(admitted_payload), **dict(settled_payload)}}

    async def _watch_terminal_replay_proof(
        self,
        *,
        child: Mapping[str, Any],
        payload: Mapping[str, Any],
        step: Mapping[str, Any],
        expected_child_id: str,
        parent_id: str,
        parent_fence: int,
    ) -> Mapping[str, Any] | None:
        """Verify a Watch terminal through its own readback contract.

        Source Watch has no Board child rows or browser artifact.  Replay must
        therefore bind the native occurrence to the persisted watch packet and
        then re-read the exact local outputs (or the no-change packet) without
        scanning sources or contacting a provider.
        """

        expected_version = _text(step.get("capability_version"))
        refs = {
            key: payload.get(key)
            for key in (
                "watch_id",
                "watch_plan_revision",
                "watch_occurrence_id",
                "watch_native_job_id",
                "watch_owner_principal_id",
                "watch_owner_session_id",
                "watch_goal_id",
                "watch_goal_revision",
                "watch_parent_job_id",
                "watch_parent_fencing_token",
                "watch_step_id",
            )
        }
        required_text = (
            "watch_id",
            "watch_occurrence_id",
            "watch_native_job_id",
            "watch_owner_principal_id",
            "watch_owner_session_id",
            "watch_goal_id",
            "watch_parent_job_id",
            "watch_step_id",
        )
        if any(not _text(refs.get(key)) for key in required_text):
            return None
        if any(type(refs.get(key)) is not int or refs[key] < 1 for key in (
            "watch_plan_revision",
            "watch_goal_revision",
            "watch_parent_fencing_token",
        )):
            return None
        occurrence_id = _text(refs["watch_occurrence_id"])
        expected_native_job_id = f"source-watch:{_text(refs['watch_id'])}:{occurrence_id}"
        if (
            _text(payload.get("watch_native_job_id")) != expected_child_id
            or _text(refs["watch_native_job_id"]) != expected_child_id
            or not occurrence_id
            or expected_child_id != expected_native_job_id
            or _text(refs["watch_parent_job_id"]) != parent_id
            or int(refs["watch_parent_fencing_token"]) != parent_fence
            or _text(refs["watch_step_id"]) != _text(step.get("step_id"))
        ):
            return None
        authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        job_inputs = child.get("inputs") if isinstance(child.get("inputs"), Mapping) else {}
        persisted_watch_id = _text(job_inputs.get("watch_id") or authority.get("watch_id"))
        persisted_occurrence_id = _text(job_inputs.get("occurrence_id") or authority.get("occurrence_id"))
        if (
            _text(child.get("job_id") or child.get("run_identity")) != expected_child_id
            or _text(child.get("job_kind")) != "guardian_source_watch"
            or _text(child.get("capability_version")) != expected_version
            or persisted_watch_id != _text(refs["watch_id"])
            or persisted_occurrence_id != occurrence_id
            or type(child.get("plan_revision")) is not int
            or int(child.get("plan_revision")) != int(refs["watch_plan_revision"])
            or _text(child.get("goal_id")) != _text(refs["watch_goal_id"])
            or int(child.get("goal_revision") or 0) != int(refs["watch_goal_revision"])
            or _text(authority.get("goal_owner_principal_id")) != _text(refs["watch_owner_principal_id"])
            or _text(authority.get("goal_owner_session_id")) != _text(refs["watch_owner_session_id"])
            or _text(authority.get("routine_parent_job_id")) != parent_id
            or int(authority.get("routine_parent_fencing_token") or 0) != parent_fence
            or _text(authority.get("routine_step_id")) != _text(step.get("step_id"))
            or _text(authority.get("capability_id")) != _text(step.get("capability_id"))
        ):
            return None
        terminal_status = _text(payload.get("watch_terminal_status"))
        if terminal_status not in {"succeeded", "degraded", "skipped_verified"}:
            return None
        if _text(child.get("status")) not in {"succeeded", "degraded"}:
            return None
        packet_id = _text(payload.get("watch_packet_id")) or None
        no_change = payload.get("watch_no_change") is True
        artifact_refs = _safe_watch_artifact_refs(payload.get("watch_artifact_refs"))
        from src.guardian.source_watch import source_watch_service

        return await source_watch_service.verify_procedure_replay(
            watch_id=_text(refs["watch_id"]),
            job_id=expected_child_id,
            expected_plan_revision=int(refs["watch_plan_revision"]),
            occurrence_id=occurrence_id,
            owner_principal_id=_text(refs["watch_owner_principal_id"]),
            owner_session_id=_text(refs["watch_owner_session_id"]),
            goal_id=_text(refs["watch_goal_id"]),
            goal_revision=int(refs["watch_goal_revision"]),
            terminal_status=terminal_status,
            packet_id=packet_id,
            artifact_refs=artifact_refs,
            no_change=no_change,
        )

    async def _native_terminal_replay_proof(
        self,
        *,
        parent: Mapping[str, Any],
        child: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
        step: Mapping[str, Any],
        expected_child_id: str,
    ) -> Mapping[str, Any] | None:
        """Verify a previously admitted native child before restart adoption.

        A successful durable row is only a recovery hint.  Adoption requires
        the exact deterministic child, the immutable checkpoint binding, the
        live parent fence, and capability-owned terminal evidence.  Browser
        roots use the same artifact/readback/cleanup verifier as BrowserTask-
        Runner replay; the generic path still requires a present artifact,
        verified readback, and a matching native root.
        """

        child_id = _text(child.get("job_id") or child.get("run_identity"))
        payload = checkpoint.get("payload")
        authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        if child_id != expected_child_id or not isinstance(payload, Mapping):
            return None
        parent_id = _text(parent.get("job_id") or parent.get("run_identity"))
        parent_lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
        try:
            parent_fence = int(parent_lease.get("fencing_token") or 0)
            child_parent_fence = int(child.get("parent_fencing_token") or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        verifier = self.replay_binding_verifier
        # A durable terminal projection is never sufficient authority on its
        # own.  The dispatcher installs the owner/session-bound canonical
        # Board verifier for the managed path; a missing seam must quarantine
        # the parent rather than silently accepting a projection-only replay.
        if verifier is None:
            return None
        try:
            canonical = await verifier(
                parent=parent,
                child=child,
                checkpoint=checkpoint,
                step=step,
                expected_child_id=expected_child_id,
            )
        except Exception:
            # A replay verifier is a fail-closed trust boundary.  The
            # parent is quarantined by the caller when it cannot prove
            # the canonical Board/session binding.
            return None
        if canonical is None or canonical is False:
            return None
        expected_capability = _text(step.get("capability_id"))
        expected_version = _text(step.get("capability_version"))
        if (
            _text(payload.get("child_job_id")) != expected_child_id
            or _text(payload.get("step_id")) != _text(step.get("step_id"))
            or _text(authority.get("routine_parent_job_id")) != parent_id
            or _text(child.get("parent_job_id")) != parent_id
            or _text(child.get("parent_run_identity")) != parent_id
            or _text(child.get("root_run_identity")) != parent_id
            or child_parent_fence != parent_fence
            or parent_fence < 1
            or _text(authority.get("routine_parent_fencing_token")) != str(parent_fence)
            or _text(authority.get("routine_step_id")) != _text(step.get("step_id"))
            or _text(authority.get("capability_id")) != expected_capability
            or _text(child.get("capability_version")) != expected_version
            or _text(payload.get("child_capability_id")) != expected_capability
            or _text(payload.get("child_capability_version")) != expected_version
        ):
            return None
        if expected_capability == "guardian.research-watch.v1":
            return await self._watch_terminal_replay_proof(
                child=child,
                payload=payload,
                step=step,
                expected_child_id=expected_child_id,
                parent_id=parent_id,
                parent_fence=parent_fence,
            )
        required_refs = (
            "child_job_id",
            "child_task_id",
            "child_attempt_id",
            "child_task_revision",
            "child_admission_task_revision",
            "child_fencing_token",
            "child_input_artifact_id",
            "child_input_artifact_digest",
            "child_capability_id",
            "child_capability_version",
        )
        if any(not _text(payload.get(key)) for key in required_refs):
            return None
        try:
            child_task_revision = int(payload.get("child_task_revision"))
            child_admission_revision = int(payload.get("child_admission_task_revision"))
            child_fence = int(payload.get("child_fencing_token"))
            authority_task_revision = int(authority.get("board_task_revision") or 0)
            authority_fence = int(authority.get("board_fencing_token") or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            _text(payload.get("child_job_id")) != expected_child_id
            or _text(payload.get("step_id")) != _text(step.get("step_id"))
            or _text(authority.get("board_task_id")) != _text(payload.get("child_task_id"))
            or _text(authority.get("board_attempt_id")) != _text(payload.get("child_attempt_id"))
            or authority_task_revision != child_admission_revision
            or authority_fence != child_fence
            or _text(authority.get("input_artifact_id")) != _text(payload.get("child_input_artifact_id"))
            or _text(authority.get("input_artifact_digest")) != _text(payload.get("child_input_artifact_digest"))
            or child_task_revision < child_admission_revision
            or child_fence < 1
        ):
            return None
        if expected_capability == "browser.public-task.v1":
            from config.settings import settings
            from src.browser.task_runner import BrowserTaskRunner

            verifier = BrowserTaskRunner(jobs=self.jobs, workspace_root=settings.workspace_dir)
            return verifier._terminal_replay_proof(
                child,
                expected_job_id=expected_child_id,
                workspace_root=settings.workspace_dir,
            )

        # Calendar and Source Watch own their richer verifier paths.  Until
        # those adapters are reached, require the durable native projection to
        # retain an existing artifact and independently verified readback
        # rather than accepting a generic successful result summary.
        artifacts = child.get("artifacts") if isinstance(child.get("artifacts"), list) else []
        artifact = next(
            (
                item
                for item in reversed(artifacts)
                if isinstance(item, Mapping)
                and item.get("exists") is True
                and _text(item.get("artifact_id"))
                and _text(item.get("file_path"))
                and _text(item.get("content_sha256"))
            ),
            None,
        )
        effects = child.get("effects") if isinstance(child.get("effects"), list) else []
        readback = next(
            (
                item
                for item in reversed(effects)
                if isinstance(item, Mapping)
                and _text(item.get("receipt_kind")) == "readback"
                and _text(item.get("status")) in {"succeeded", "read_back", "reconciled"}
                and isinstance(item.get("details"), Mapping)
                and item["details"].get("verified") is True
                and _text(item.get("readback_id"))
            ),
            None,
        )
        if artifact is None or readback is None:
            return None
        return {
            "artifact_ref": _text(artifact.get("file_path")),
            "artifact_sha256": _text(artifact.get("content_sha256")),
            "readback_id": _text(readback.get("readback_id")),
            "verified_at": _text(readback.get("verified_at")),
            "memory_status": "no_learning",
        }

    async def _terminate_parent_before_leaf(
        self,
        parent_job_id: str,
        *,
        reason: str,
        unknown: bool = False,
    ) -> Mapping[str, Any]:
        """Fail a parent before native leaf admission/contact.

        Admission errors happen before a native root exists, so there is no
        child ledger for the coordinator to settle.  Closing the parent with
        its own lease makes the failed boundary durable without inventing a
        synthetic child effect.
        """

        current = await self.jobs.get_job(parent_job_id)
        if not isinstance(current, Mapping):
            return {"status": "blocked", "job_id": parent_job_id}
        if _text(current.get("status")) in {
            "succeeded",
            "degraded",
            "blocked",
            "failed",
            "cancelled",
            *UNCERTAIN_EXTERNAL_EFFECT_STATUSES,
        }:
            return current
        owner, fence = _lease(current)
        try:
            return await self.jobs.transition_job(
                parent_job_id,
                "unknown_external_effect" if unknown else "blocked",
                owner=owner,
                fencing_token=fence,
                expected_revision=int(current.get("revision") or 0),
                reason=reason,
                result={
                    "status": "unknown_external_effect" if unknown else "blocked",
                    "reason_code": reason,
                    "memory_status": "no_learning",
                },
                result_summary=reason,
            )
        except DurableJobError:
            return await self.jobs.get_job(parent_job_id) or current

    async def _execute_leaf(
        self,
        *,
        step: Mapping[str, Any],
        descriptor: Mapping[str, Any],
        child: Mapping[str, Any],
        leaf_executors: Mapping[str, LeafExecutor] | None = None,
    ) -> Mapping[str, Any]:
        capability_id = _text(step.get("capability_id"))
        executor = (leaf_executors or self.leaf_executors).get(capability_id)
        if executor is None:
            # Production wiring is intentionally lazy: dispatcher registers
            # the actual leaf callbacks to avoid a browser/calendar import
            # cycle.  A missing callback is a bounded capability denial.
            raise ProcedureV2RuntimeError("procedure_leaf_executor_unavailable")
        result = await executor(step, descriptor, child)
        if not isinstance(result, Mapping):
            raise ProcedureV2RuntimeError("procedure_leaf_result_invalid")
        return result

    async def execute_leaf(
        self,
        *,
        parent_job_id: str,
        child_job_id: str,
        step_id: str,
        context: Any,
    ) -> dict[str, Any]:
        """Execute one already-admitted fixed leaf for generated wrappers."""

        parent = await self.jobs.get_job(parent_job_id)
        child = await self.jobs.get_job(child_job_id)
        if not isinstance(parent, Mapping) or not isinstance(child, Mapping):
            raise ProcedureV2RuntimeError("procedure_child_missing")
        if _text(parent.get("status")) != "running":
            raise ProcedureV2RuntimeError("procedure_parent_not_running")
        parent_owner = parent.get("owner") if isinstance(parent.get("owner"), Mapping) else {}
        if (
            _text(parent_owner.get("principal_id")) != _text(getattr(context, "principal_id", None))
            or _text(parent.get("operator_session_id") or parent.get("session_id")) != _text(getattr(context, "session_id", None))
        ):
            raise ProcedureV2RuntimeError("procedure_owner_binding_stale")
        child_authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        if (
            _text(child.get("parent_job_id")) != parent_job_id
            or _text(child_authority.get("step_id")) != step_id
            or int(child.get("parent_fencing_token") or 0) != int((parent.get("lease") or {}).get("fencing_token") or 0)
        ):
            raise ProcedureV2RuntimeError("procedure_child_binding_invalid")
        descriptor = dict(parent.get("inputs") if isinstance(parent.get("inputs"), Mapping) else {})
        if not isinstance(descriptor.get("plan"), Mapping) or not isinstance(descriptor.get("executable_steps"), Mapping):
            raise ProcedureV2RuntimeError("procedure_parent_descriptor_missing")
        template_id, version, steps = _plan_steps(descriptor)
        step = next((item for item in steps if _text(item.get("step_id")) == step_id), None)
        if step is None:
            raise ProcedureV2RuntimeError("procedure_step_not_registered")
        result = await self._execute_leaf(step=step, descriptor=descriptor, child=child)
        status = _result_status(result)
        if status in {"no_change", "baseline_initialized"}:
            status = "skipped_verified"
            result = {**dict(result), "status": status, "skipped": True, "verified": True}
        elif status in {"succeeded", "completed"} and not _verified_readback(result):
            result = {**dict(result), "status": "blocked", "reason_code": "leaf_readback_missing"}
            status = "blocked"
        settled = await self._settle_child(child, result=result, status=status, reason=_text(result.get("reason_code")) or status)
        return {"status": _text(settled.get("status")) or status, "job_id": parent_job_id, "child_job_id": child_job_id, **_safe_result(result)}

    async def _settle_child(
        self,
        child: Mapping[str, Any],
        *,
        result: Mapping[str, Any],
        status: str,
        reason: str,
    ) -> Mapping[str, Any]:
        child_id = _text(child.get("job_id") or child.get("run_identity"))
        current = await self.jobs.get_job(child_id) or child
        if _text(current.get("status")) in {
            "succeeded",
            "degraded",
            "blocked",
            "failed",
            "cancelled",
            *UNCERTAIN_EXTERNAL_EFFECT_STATUSES,
        }:
            # Native adapters own their durable effect/readback/terminal
            # ledger.  The coordinator may only observe that terminal root;
            # it must never manufacture a second generic leaf effect.
            return current
        raise ProcedureV2RuntimeError(
            "procedure_leaf_terminal_missing",
            "the native leaf did not publish a durable terminal state",
            unknown=status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES or bool(result.get("unknown_external_effect")),
        )

    async def execute_parent(
        self,
        parent_job_id: str,
        *,
        descriptor: Mapping[str, Any] | Any,
        context: Any | None = None,
        leaf_executors: Mapping[str, LeafExecutor] | None = None,
    ) -> dict[str, Any]:
        """Run the fixed plan serially under one leased parent."""

        parent = await self.jobs.get_job(parent_job_id)
        if not isinstance(parent, Mapping):
            raise ProcedureV2RuntimeError("procedure_parent_missing")
        descriptor_mapping = _as_mapping(descriptor)
        template_id, version, steps = _plan_steps(descriptor_mapping)
        if _text(parent.get("capability_version")) != ROUTINE_V2_CAPABILITY_VERSION or _text(parent.get("job_kind")) != ROUTINE_V2_JOB_KIND:
            raise ProcedureV2RuntimeError("procedure_parent_binding_invalid")
        if _text(parent.get("status")) in {"accepted", "queued"}:
            parent = await self._ensure_claimed(parent_job_id, lease_seconds=self._remaining_seconds(parent))
        if _text(parent.get("status")) != "running":
            return {"status": _text(parent.get("status")) or "blocked", "job_id": parent_job_id, "reason_code": "procedure_parent_not_running", "memory_status": "no_learning"}
        parent_owner, parent_fence = _lease(parent)
        authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
        if authority.get("template_id") != template_id:
            raise ProcedureV2RuntimeError("procedure_parent_template_mismatch")
        outcomes: list[dict[str, Any]] = []
        skip_next_step_id: str | None = None
        skip_dependency_job_id: str | None = None
        for index, step in enumerate(steps):
            step_id = _text(step.get("step_id"))
            if skip_next_step_id == step_id:
                skip_next_step_id = None
                skip_outcome = {
                    "step_id": step_id,
                    "status": "skipped",
                    "reason_code": "watch_no_material_change",
                    "skipped": True,
                    "watch_child_job_id": skip_dependency_job_id,
                    "memory_status": "no_learning",
                }
                try:
                    current_parent = await self.jobs.get_job(parent_job_id) or parent
                    await self._assert_parent_current_before_write(current_parent)
                    previous_step = steps[index - 1] if index > 0 else None
                    if (
                        skip_dependency_job_id
                        and isinstance(previous_step, Mapping)
                        and _text(previous_step.get("capability_id")) == "guardian.research-watch.v1"
                    ):
                        dependency = await self.jobs.get_job(skip_dependency_job_id)
                        admitted = self._recorded_child_checkpoint(current_parent, _text(previous_step.get("step_id")))
                        settled = self._recorded_child_settled_checkpoint(current_parent, _text(previous_step.get("step_id")))
                        if not isinstance(dependency, Mapping) or not isinstance(admitted, Mapping):
                            raise ProcedureV2RuntimeError("procedure_child_terminal_proof_missing", unknown=True)
                        proof = await self._native_terminal_replay_proof(
                            parent=current_parent,
                            child=dependency,
                            checkpoint=self._replay_checkpoint(admitted, settled),
                            step=previous_step,
                            expected_child_id=skip_dependency_job_id,
                        )
                        if proof is None or proof.get("no_change") is not True:
                            raise ProcedureV2RuntimeError("procedure_child_terminal_proof_missing", unknown=True)
                    # Re-read the parent after the native proof.  Cancellation,
                    # board re-fencing, or deadline expiry during that local
                    # readback must prevent even a skipped-step checkpoint.
                    current_parent = await self.jobs.get_job(parent_job_id) or current_parent
                    await self._assert_parent_current_before_write(current_parent)
                    current_parent_owner, current_parent_fence = _lease(current_parent)
                    current_parent = await self.jobs.record_checkpoint(
                        parent_job_id,
                        checkpoint_id=f"procedure-v2:step:{step_id}:skipped",
                        state={"step_id": step_id, "status": "skipped"},
                        checkpoint_payload=skip_outcome,
                        owner=current_parent_owner,
                        fencing_token=current_parent_fence,
                        expected_revision=int(current_parent.get("revision") or 0),
                    )
                except (ProcedureV2RuntimeError, DurableJobError) as exc:
                    terminal = await self._terminate_parent_before_leaf(
                        parent_job_id,
                        reason=getattr(exc, "code", None) or "procedure_parent_authority_stale",
                        unknown=True,
                    )
                    return {
                        "status": _text(terminal.get("status")) or "unknown_external_effect",
                        "job_id": parent_job_id,
                        "reason_code": getattr(exc, "code", None) or "procedure_parent_authority_stale",
                        "memory_status": "no_learning",
                    }
                outcomes.append(skip_outcome)
                parent = current_parent
                parent_owner, parent_fence = _lease(parent)
                continue
            derived_child_id, expected_child_id = _native_child_job_id(
                parent_job_id,
                template_id,
                version,
                step,
                descriptor_mapping,
            )
            parent = await self.jobs.get_job(parent_job_id) or parent
            if _text(parent.get("status")) != "running":
                return {"status": _text(parent.get("status")) or "blocked", "job_id": parent_job_id, "child_job_id": derived_child_id, "reason_code": "procedure_parent_not_running", "memory_status": "no_learning"}
            parent_authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
            if _text(parent_authority.get("plan_digest")) != (_text(descriptor_mapping.get("plan_digest")) or _digest(_descriptor_plan(descriptor_mapping))):
                raise ProcedureV2RuntimeError("procedure_plan_changed")
            recorded_checkpoint = self._recorded_child_checkpoint(parent, step_id)
            settled_checkpoint = self._recorded_child_settled_checkpoint(parent, step_id)
            recorded_child_id = self._recorded_child_job_id(parent, step_id)
            if recorded_child_id and recorded_child_id != expected_child_id:
                terminal = await self._terminate_parent_before_leaf(
                    parent_job_id,
                    reason="procedure_child_identity_mismatch",
                    unknown=True,
                )
                return {
                    "status": _text(terminal.get("status")) or "unknown_external_effect",
                    "job_id": parent_job_id,
                    "child_job_id": recorded_child_id,
                    "reason_code": "procedure_child_identity_mismatch",
                    "memory_status": "no_learning",
                }
            existing_child_id = recorded_child_id or expected_child_id
            existing = await self.jobs.get_job(existing_child_id)
            if recorded_child_id and not isinstance(existing, Mapping):
                terminal = await self._terminate_parent_before_leaf(
                    parent_job_id,
                    reason="procedure_child_missing",
                    unknown=True,
                )
                return {
                    "status": _text(terminal.get("status")) or "unknown_external_effect",
                    "job_id": parent_job_id,
                    "child_job_id": existing_child_id,
                    "reason_code": "procedure_child_missing",
                    "memory_status": "no_learning",
                }
            if isinstance(existing, Mapping) and _text(existing.get("status")) in {"succeeded", "degraded"}:
                if recorded_checkpoint is None:
                    terminal = await self._terminate_parent_before_leaf(
                        parent_job_id,
                        reason="procedure_child_checkpoint_missing",
                        unknown=True,
                    )
                    return {
                        "status": _text(terminal.get("status")) or "unknown_external_effect",
                        "job_id": parent_job_id,
                        "child_job_id": existing_child_id,
                        "reason_code": "procedure_child_checkpoint_missing",
                        "memory_status": "no_learning",
                    }
                replay_proof = await self._native_terminal_replay_proof(
                    parent=parent,
                    child=existing,
                    checkpoint=self._replay_checkpoint(recorded_checkpoint, settled_checkpoint),
                    step=step,
                    expected_child_id=expected_child_id,
                )
                if replay_proof is None:
                    terminal = await self._terminate_parent_before_leaf(
                        parent_job_id,
                        reason="procedure_child_terminal_proof_missing",
                        unknown=True,
                    )
                    return {
                        "status": _text(terminal.get("status")) or "unknown_external_effect",
                        "job_id": parent_job_id,
                        "child_job_id": existing_child_id,
                        "reason_code": "procedure_child_terminal_proof_missing",
                        "memory_status": "no_learning",
                    }
                outcomes.append(
                    {
                        "step_id": step_id,
                        "child_job_id": existing_child_id,
                        "status": "succeeded",
                        "replayed": True,
                        **_safe_result(replay_proof),
                    }
                )
                if replay_proof.get("no_change") is True and index + 1 < len(steps):
                    next_step = steps[index + 1]
                    if (
                        step_id == "source_watch"
                        and _text(step.get("capability_id")) == "guardian.research-watch.v1"
                        and _text(next_step.get("step_id")) == "public_browser_check"
                        and _text(next_step.get("capability_id")) == "browser.public-task.v1"
                    ):
                        skip_next_step_id = _text(next_step.get("step_id"))
                        skip_dependency_job_id = existing_child_id
                continue
            if recorded_child_id and isinstance(existing, Mapping) and _text(existing.get("status")) not in {
                "succeeded",
                "degraded",
            }:
                terminal = await self._terminate_parent_before_leaf(
                    parent_job_id,
                    reason="procedure_child_terminal_unknown",
                    unknown=True,
                )
                return {
                    "status": _text(terminal.get("status")) or "unknown_external_effect",
                    "job_id": parent_job_id,
                    "child_job_id": existing_child_id,
                    "reason_code": "procedure_child_terminal_unknown",
                    "memory_status": "no_learning",
                }
            if isinstance(existing, Mapping) and not recorded_child_id:
                terminal = await self._terminate_parent_before_leaf(
                    parent_job_id,
                    reason="procedure_child_uncheckpointed",
                    unknown=True,
                )
                return {
                    "status": _text(terminal.get("status")) or "unknown_external_effect",
                    "job_id": parent_job_id,
                    "child_job_id": existing_child_id,
                    "reason_code": "procedure_child_uncheckpointed",
                    "memory_status": "no_learning",
                }
            try:
                child = await self._admit_child(parent, step=step, descriptor=descriptor_mapping, child_id=derived_child_id)
            except ProcedureV2RuntimeError as exc:
                terminal = await self._terminate_parent_before_leaf(
                    parent_job_id,
                    reason=exc.code,
                    unknown=exc.unknown,
                )
                return {
                    "status": _text(terminal.get("status")) or ("unknown_external_effect" if exc.unknown else "blocked"),
                    "job_id": parent_job_id,
                    "reason_code": exc.code,
                    "memory_status": "no_learning",
                }
            child_id = _text(child.get("job_id") or child.get("run_identity"))
            if not child_id:
                raise ProcedureV2RuntimeError("procedure_leaf_identity_missing")
            parent_revision = int(parent.get("revision") or 0)
            parent = await self.jobs.record_checkpoint(
                parent_job_id,
                checkpoint_id=f"procedure-v2:step:{step_id}:admitted",
                state={"step_id": step_id, "child_job_id": child_id, "step_index": index},
                checkpoint_payload={
                    "step_id": step_id,
                    "child_job_id": child_id,
                    "input_digest": step.get("typed_input_digest"),
                    "status": "admitted",
                    **_native_child_refs(child),
                    **(_watch_child_refs(child) if _text(step.get("capability_id")) == "guardian.research-watch.v1" else {}),
                },
                owner=parent_owner,
                fencing_token=parent_fence,
                expected_revision=parent_revision,
            )
            try:
                result = await self._execute_leaf(
                    step=step,
                    descriptor=descriptor_mapping,
                    child=child,
                    leaf_executors=leaf_executors,
                )
            except ProcedureV2RuntimeError as exc:
                result = {"status": "unknown_external_effect" if exc.unknown else "blocked", "reason_code": exc.code, "unknown_external_effect": exc.unknown, "memory_status": "no_learning"}
            except asyncio.CancelledError:
                # Cancellation cannot be mistaken for a clean leaf result.
                result = {"status": "unknown_external_effect", "reason_code": "leaf_cancelled_after_dispatch", "unknown_external_effect": True, "memory_status": "no_learning"}
            except Exception as exc:  # bounded adapter boundary
                result = {"status": "blocked", "reason_code": type(exc).__name__.lower()[:128], "memory_status": "no_learning"}
            status = _result_status(result)
            if _text(step.get("capability_id")) == "guardian.research-watch.v1" and status in {
                "succeeded",
                "degraded",
                "no_change",
                "baseline_initialized",
            }:
                # Source Watch owns its terminal row and workspace effects. A
                # successful adapter return is only an execution receipt; the
                # coordinator must independently re-read the exact packet,
                # artifact hashes, and durable readbacks before admitting the
                # next leaf. This also proves a no-change observation before
                # skipping a dependent Browser step.
                current_child = await self.jobs.get_job(child_id)
                proof_status = "skipped_verified" if status in {"no_change", "baseline_initialized"} else status
                proof_result = {
                    **dict(result),
                    "status": proof_status,
                    "skipped": proof_status == "skipped_verified",
                    "no_change": status in {"no_change", "baseline_initialized"},
                }
                watch_proof = None
                if isinstance(current_child, Mapping):
                    current_parent = await self.jobs.get_job(parent_job_id)
                    admitted_checkpoint = (
                        self._recorded_child_checkpoint(current_parent, step_id)
                        if isinstance(current_parent, Mapping)
                        else None
                    )
                    if isinstance(current_parent, Mapping) and isinstance(admitted_checkpoint, Mapping):
                        admitted_payload = (
                            admitted_checkpoint.get("payload")
                            if isinstance(admitted_checkpoint.get("payload"), Mapping)
                            else {}
                        )
                        watch_payload = _watch_child_refs(current_child, proof_result)
                        proof_checkpoint = {
                            **dict(admitted_checkpoint),
                            "payload": {**dict(admitted_payload), **watch_payload},
                        }
                        watch_proof = await self._native_terminal_replay_proof(
                            parent=current_parent,
                            child=current_child,
                            checkpoint=proof_checkpoint,
                            step=step,
                            expected_child_id=child_id,
                        )
                if watch_proof is None:
                    terminal = await self._terminate_parent_before_leaf(
                        parent_job_id,
                        reason="procedure_child_terminal_proof_missing",
                        unknown=True,
                    )
                    return {
                        "status": _text(terminal.get("status")) or "unknown_external_effect",
                        "job_id": parent_job_id,
                        "child_job_id": child_id,
                        "reason_code": "procedure_child_terminal_proof_missing",
                        "memory_status": "no_learning",
                    }
                result = {**proof_result, **dict(watch_proof), "verified": True}
                if status in {"no_change", "baseline_initialized"}:
                    status = "skipped_verified"
                    result["status"] = status
                    result["skipped"] = True
                    result["memory_status"] = "no_learning"
            elif status in {"no_change", "baseline_initialized"}:
                status = "skipped_verified"
                result = {**dict(result), "status": status, "skipped": True, "verified": True, "memory_status": "no_learning"}
            elif status in {"succeeded", "completed"} and not _verified_readback(result):
                status = "blocked"
                result = {**dict(result), "status": status, "reason_code": "leaf_readback_missing", "memory_status": "no_learning"}
            try:
                settled = await self._settle_child(child, result=result, status=status, reason=_text(result.get("reason_code")) or status)
            except ProcedureV2RuntimeError as exc:
                observed_child = await self.jobs.get_job(child_id)
                observed_status = _text(observed_child.get("status")) if isinstance(observed_child, Mapping) else ""
                settled = (
                    observed_child
                    if observed_status in {"succeeded", "degraded", "blocked", "failed", "cancelled", *UNCERTAIN_EXTERNAL_EFFECT_STATUSES}
                    else {"status": "unknown_external_effect" if exc.unknown else "blocked"}
                )
                result = {
                    **dict(result),
                    "status": _text(settled.get("status")) or "blocked",
                    "reason_code": exc.code,
                    "unknown_external_effect": exc.unknown,
                    "memory_status": "no_learning",
                }
            child_status = _text(settled.get("status"))
            child_outcome = {
                "step_id": step_id,
                "child_job_id": child_id,
                "status": child_status,
                **_native_child_refs(child),
                **(_watch_child_refs(child, result) if _text(step.get("capability_id")) == "guardian.research-watch.v1" else {}),
                **_safe_result(result),
            }
            outcomes.append(child_outcome)
            try:
                current_parent = await self.jobs.get_job(parent_job_id) or parent
                await self._assert_parent_current_before_write(current_parent)
                current_parent_owner, current_parent_fence = _lease(current_parent)
                current_parent = await self.jobs.record_checkpoint(
                    parent_job_id,
                    checkpoint_id=f"procedure-v2:step:{step_id}:settled",
                    state={"step_id": step_id, "child_job_id": child_id, "status": child_status},
                    checkpoint_payload=child_outcome,
                    owner=current_parent_owner,
                    fencing_token=current_parent_fence,
                    expected_revision=int(current_parent.get("revision") or 0),
                )
            except (ProcedureV2RuntimeError, DurableJobError) as exc:
                terminal = await self._terminate_parent_before_leaf(
                    parent_job_id,
                    reason=getattr(exc, "code", None) or "procedure_parent_authority_stale",
                    unknown=True,
                )
                return {
                    "status": _text(terminal.get("status")) or "unknown_external_effect",
                    "job_id": parent_job_id,
                    "child_job_id": child_id,
                    "reason_code": getattr(exc, "code", None) or "procedure_parent_authority_stale",
                    "memory_status": "no_learning",
                }
            if child_status not in {"succeeded", "degraded"}:
                reason = _text(result.get("reason_code")) or ("reconcile_external_effect" if child_status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES else "procedure_leaf_blocked")
                latest_parent = await self.jobs.get_job(parent_job_id) or current_parent
                latest_lease = latest_parent.get("lease") if isinstance(latest_parent.get("lease"), Mapping) else {}
                to_status = "unknown_external_effect" if child_status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES else "blocked"
                try:
                    latest_parent = await self.jobs.transition_job(
                        parent_job_id,
                        to_status,
                        owner=_text(latest_lease.get("owner")) or current_parent_owner,
                        fencing_token=int(latest_lease.get("fencing_token") or current_parent_fence),
                        expected_revision=int(latest_parent.get("revision") or 0),
                        reason=reason,
                        result={"status": to_status, "child_job_id": child_id, "memory_status": "no_learning"},
                        result_summary=reason,
                    )
                except DurableJobError:
                    latest_parent = await self.jobs.get_job(parent_job_id) or latest_parent
                return {"status": _text(latest_parent.get("status")) or to_status, "job_id": parent_job_id, "child_job_id": child_id, "child": child_outcome, "outcomes": outcomes, "reason_code": reason, "memory_status": "no_learning"}
            parent = current_parent
            parent_owner, parent_fence = _lease(parent)
            if result.get("no_change") is True and index + 1 < len(steps):
                next_step = steps[index + 1]
                if (
                    step_id == "source_watch"
                    and _text(step.get("capability_id")) == "guardian.research-watch.v1"
                    and _text(next_step.get("step_id")) == "public_browser_check"
                    and _text(next_step.get("capability_id")) == "browser.public-task.v1"
                ):
                    skip_next_step_id = _text(next_step.get("step_id"))
                    skip_dependency_job_id = child_id
        # The parent itself gets a readback only after all required child
        # readbacks exist.  It owns no remote resource claim.
        summary = {"template_id": template_id, "outcomes": outcomes, "memory_status": "no_learning"}
        try:
            latest = await self.jobs.get_job(parent_job_id) or parent
            await self._assert_parent_current_before_write(latest)
            owner, fence = _lease(latest)
            effect = await self.jobs.record_effect(
                parent_job_id,
                effect_type="guardian_routine_v2_parent",
                effect_id=f"procedure-v2-parent:{parent_job_id}",
                target_path=f"procedure-v2:{parent_job_id}",
                target_digest=_digest(summary),
                status="succeeded",
                details=summary,
                owner=owner,
                fencing_token=fence,
                expected_revision=int(latest.get("revision") or 0),
            )
            latest_after_effect = await self.jobs.get_job(parent_job_id) or effect
            await self._assert_parent_current_before_write(latest_after_effect)
            effect_owner, effect_fence = _lease(latest_after_effect)
            readback = await self.jobs.record_readback(
                parent_job_id,
                target_path=f"procedure-v2:{parent_job_id}",
                status="succeeded",
                effect_id=f"procedure-v2-parent:{parent_job_id}",
                effect_type="guardian_routine_v2_parent",
                target_digest=_digest(summary),
                readback_id=f"procedure-v2-parent-readback:{parent_job_id}",
                verified_at=_now().isoformat().replace("+00:00", "Z"),
                details={"verified": True, "memory_status": "no_learning", "outcomes": outcomes},
                owner=effect_owner,
                fencing_token=effect_fence,
                expected_revision=int(effect.get("revision") or latest_after_effect.get("revision") or 0),
            )
            latest = await self.jobs.get_job(parent_job_id) or readback
            await self._assert_parent_current_before_write(latest)
            lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
            final = await self.jobs.transition_job(
                parent_job_id,
                "succeeded",
                owner=_text(lease.get("owner")) or effect_owner,
                fencing_token=int(lease.get("fencing_token") or effect_fence),
                expected_revision=int(latest.get("revision") or 0),
                reason="procedure_completed",
                result={"status": "succeeded", "template_id": template_id, "outcomes": outcomes, "memory_status": "no_learning"},
                result_summary="verified procedure leaf readbacks completed",
            )
        except (ProcedureV2RuntimeError, DurableJobError) as exc:
            terminal = await self._terminate_parent_before_leaf(
                parent_job_id,
                reason=getattr(exc, "code", None) or "procedure_parent_authority_stale",
                unknown=True,
            )
            return {
                "status": _text(terminal.get("status")) or "unknown_external_effect",
                "job_id": parent_job_id,
                "outcomes": outcomes,
                "reason_code": getattr(exc, "code", None) or "procedure_parent_authority_stale",
                "memory_status": "no_learning",
            }
        return {"status": "succeeded", "job_id": parent_job_id, "template_id": template_id, "outcomes": outcomes, "memory_status": "no_learning", "projection": final}

    async def reconcile_parent(self, parent_job_id: str, *, descriptor: Mapping[str, Any] | Any) -> dict[str, Any]:
        """Return the exact persisted parent/leaf projection for recovery."""

        parent = await self.jobs.get_job(parent_job_id)
        if not isinstance(parent, Mapping):
            raise ProcedureV2RuntimeError("procedure_parent_missing")
        template_id, version, steps = _plan_steps(_as_mapping(descriptor))
        children = []
        for step in steps:
            step_id = _text(step.get("step_id"))
            child_id = self._recorded_child_job_id(parent, step_id) or deterministic_child_job_id(
                parent_job_id,
                template_id,
                version,
                step_id,
            )
            child = await self.jobs.get_job(child_id)
            children.append({"step_id": step_id, "child_job_id": child_id, "status": _text(child.get("status")) if isinstance(child, Mapping) else "missing", "job": child})
        return {"status": _text(parent.get("status")) or "blocked", "job_id": parent_job_id, "children": children, "recovery_action": "reconcile_external_effect" if _text(parent.get("status")) in UNCERTAIN_EXTERNAL_EFFECT_STATUSES else None}


procedure_v2_runtime = ProcedureV2Runtime()


__all__ = [
    "ProcedureV2Runtime",
    "ProcedureV2RuntimeError",
    "ROUTINE_V2_CAPABILITY_VERSION",
    "ROUTINE_V2_JOB_KIND",
    "ROUTINE_V2_TEMPLATES",
    "deterministic_child_job_id",
    "procedure_v2_runtime",
]
