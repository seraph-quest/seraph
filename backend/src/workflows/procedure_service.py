"""API-owned preparation and authority boundary for reviewed procedures.

The service is intentionally small and boring: source proof is resolved from
the existing WorkBoard and durable-job rows, one immutable GuardianRoutineVersion
is written, and invocation/schedule requests are converted to existing board
and governed-schedule records.  Execution remains owned by the dispatcher and
the runtime worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Sequence, TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text, update

from src.db import engine as db_engine
from src.db.models import (
    Goal,
    GovernedScheduleBinding,
    GuardianRoutine,
    GuardianRoutineVersion,
    ProcedureV2Binding,
    ScheduledJob,
    WorkBoardAttempt,
    WorkBoardInputArtifact,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from config.settings import settings
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import deserialize_admission_budget
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.dispatcher import REGISTERED_CAPABILITIES, registered_executor_id
from src.work_board.input_artifacts import (
    _artifact_id,
    prepare_input_artifact,
    revoke_unpublished_input_artifact,
)
from src.work_board.repository import BoardError, WorkBoardRepository
from src.work_board.repository import _payload_digest
from src.workspace import canonical_workspace_root
from src.workflows.job_runtime import durable_job_repository
from src.workflows.procedure_contracts import (
    ROUTINE_V2_CAPABILITY_VERSION,
    ProcedureV2Plan,
    build_procedure_plan,
    get_procedure_template,
    plan_digest,
    preview_digest,
    validate_procedure_plan,
)

if TYPE_CHECKING:  # pragma: no cover
    from src.workflows.routines import RoutineService


PREVIEW_TTL = timedelta(minutes=15)
SCHEDULE_TTL = timedelta(days=7)
INVOCATION_SCOPE_PREFIX = "procedure-v2:"
ROUTINE_ID_NAMESPACE = UUID("4a2e6e75-1a7c-5cf6-ae1a-d8a7227e6d8f")


class ProcedureV2Error(ValueError):
    """Safe, typed API failure for the procedure v2 routes."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status_code: int = 409,
        recovery_action: str | None = None,
        retryable: bool = False,
        binding_id: str | None = None,
        audit_receipt_id: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.recovery_action = recovery_action
        self.retryable = bool(retryable)
        self.binding_id = binding_id
        self.audit_receipt_id = audit_receipt_id
        super().__init__(message or code)


class ProcedureSourceTaskRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    task_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1)


class ProcedureV2PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    template_id: str = Field(min_length=1, max_length=80)
    # JSON request bodies decode arrays as Python lists.  Keep the field
    # list-shaped at the HTTP boundary, then treat its order as immutable in
    # source resolution; a strict tuple field would reject a valid POST body
    # when callers use ``model_validate`` on decoded JSON.
    source_tasks: list[ProcedureSourceTaskRef] = Field(min_length=1, max_length=2)
    name: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("name")
    @classmethod
    def bounded_name(cls, value: str) -> str:
        import re

        if re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$", value.strip()) is None:
            raise ValueError("name contains unsupported characters")
        return value.strip()


class ProcedureV2CreateRequest(ProcedureV2PreviewRequest):
    preview_digest: str = Field(min_length=64, max_length=64)

    @field_validator("preview_digest")
    @classmethod
    def normalized_preview_digest(cls, value: str) -> str:
        value = value.lower()
        if any(char not in "0123456789abcdef" for char in value):
            raise ValueError("preview_digest must be hexadecimal")
        return value


class ProcedureV2InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    expected_goal_revision: int = Field(ge=1)
    parameters: dict[str, Any]
    invocation_uuid: str = Field(min_length=1, max_length=256)


class ProcedureV2ScheduleCadence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: str
    timezone: str
    daily_hour: int | None = None
    daily_minute: int | None = None

    @model_validator(mode="after")
    def canonical(self) -> "ProcedureV2ScheduleCadence":
        if self.kind not in {"hourly", "6h", "daily"}:
            raise ValueError("cadence kind is not supported")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("cadence timezone is not supported") from exc
        if self.kind == "daily":
            if (
                type(self.daily_hour) is not int
                or not 0 <= self.daily_hour <= 23
                or type(self.daily_minute) is not int
                or not 0 <= self.daily_minute <= 59
            ):
                raise ValueError("daily cadence requires a valid hour and minute")
        elif self.daily_hour is not None or self.daily_minute is not None:
            raise ValueError("daily hour and minute are only valid for daily cadence")
        return self


class ProcedureV2ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    expected_goal_revision: int = Field(ge=1)
    parameters: dict[str, Any]
    cadence: ProcedureV2ScheduleCadence
    expires_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("expires_at", mode="before")
    @classmethod
    def utc_expiry(cls, value: Any) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("expires_at must be an ISO-8601 datetime") from exc
        if not isinstance(value, datetime):
            raise ValueError("expires_at must include a timezone")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class V2VersionDescriptor:
    routine_id: str
    version_id: str
    version: int
    owner_principal_id: str
    owner_session_id: str
    routine_revision: int
    template_id: str
    schema_version: int
    plan_digest: str
    source_proof_digest: str
    preview_digest: str
    installed_package_digest: str
    plan: Mapping[str, Any]
    source_refs: tuple[Mapping[str, Any], ...]
    capability_versions: tuple[str, ...]
    parameter_schema: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class V2InvocationDescriptor:
    version: V2VersionDescriptor
    goal_id: str
    goal_revision: int
    parameters: Mapping[str, Any]
    invocation_uuid: str
    scope: str
    executable_steps: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class GoalAdmissionBudgetSnapshot:
    """The finite reviewed goal grant pinned to one governed schedule.

    The digest is the sole stable identity used across the API binding and the
    scheduler action spec.  The runtime can import the pure helper below to
    recompute it from a freshly read Goal row without maintaining a second
    budget contract.
    """

    goal_id: str
    goal_revision: int
    budget: GoalAdmissionBudget
    digest: str


def goal_admission_budget_snapshot(
    *,
    goal_id: str,
    goal_revision: int,
    budget: GoalAdmissionBudget | Mapping[str, Any],
) -> GoalAdmissionBudgetSnapshot:
    """Normalize one persisted budget and derive its canonical authority digest.

    This helper is deliberately pure: it performs no database read, approval,
    reservation, or external operation.  Callers must still enforce the
    current owner/session, finite window, and reviewed-grant checks around the
    returned snapshot.
    """

    normalized_goal_id = _text(goal_id)
    if not normalized_goal_id or type(goal_revision) is not int or goal_revision < 1:
        raise ValueError("goal identity is invalid")
    parsed = budget if isinstance(budget, GoalAdmissionBudget) else GoalAdmissionBudget.model_validate(budget)
    payload = {
        "goal_id": normalized_goal_id,
        "goal_revision": int(goal_revision),
        "admission_budget": parsed.model_dump(mode="json"),
    }
    return GoalAdmissionBudgetSnapshot(
        goal_id=normalized_goal_id,
        goal_revision=int(goal_revision),
        budget=parsed,
        digest=_digest(payload),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _preview_expiry(observed_at: datetime | None = None) -> datetime:
    """Return a minute-bucketed expiry so preview replay is deterministic."""

    observed = _utc(observed_at or _now())
    bucket = int(observed.timestamp()) // 60
    return datetime.fromtimestamp((bucket + int(PREVIEW_TTL.total_seconds() // 60)) * 60, timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _raw_digest(value: str | bytes) -> str:
    """Hash an already-serialized payload without JSON-encoding it again.

    Routine installation stores and later verifies ``source_provenance_json``
    as its canonical UTF-8 JSON bytes.  Keep that byte-level contract separate
    from ``_digest`` which intentionally canonicalizes structured values.
    """

    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _invocation_request_digest(
    *,
    routine_id: str,
    version_id: str,
    plan_digest_value: str,
    goal_id: str,
    goal_revision: int,
    parameters: Mapping[str, Any],
    invocation_uuid: str,
    routine_revision: int,
) -> str:
    return _digest(
        {
            "routine_id": routine_id,
            "version_id": version_id,
            "plan_digest": plan_digest_value,
            "goal_id": goal_id,
            "goal_revision": int(goal_revision),
            "parameters": dict(parameters),
            "invocation_uuid": invocation_uuid,
            "routine_revision": int(routine_revision),
        }
    )


def _invocation_body(request_digest: str) -> str:
    return f"Server-owned reviewed procedure invocation:{request_digest}"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        result = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return result


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _status(value: Any) -> str:
    return _text(getattr(value, "value", value)).lower()


@dataclass(frozen=True)
class _PublicationArtifactCleanup:
    """Bounded result of a publication-failure artifact reconciliation.

    ``revoked`` is the only result that permits a caller to recommend a new
    idempotency key.  Every other result preserves the deterministic key so a
    later request can reconcile an accepted publication or finish the pending
    work.  The reason is deliberately a fixed safe label; source input bytes
    and provider details never cross this boundary.
    """

    outcome: str
    reason: str | None = None


async def _cleanup_unpublished_procedure_artifact(
    owner: WorkBoardOwner,
    *,
    artifact_id: str,
    expected_revision: int,
) -> _PublicationArtifactCleanup:
    """Revoke one still-unpublished procedure input under the lifecycle fence."""

    async def publication_guard(db, _row: WorkBoardInputArtifact) -> bool:
        task_ref = (
            await db.execute(
                select(WorkBoardTask.task_id)
                .where(WorkBoardTask.input_artifact_id == artifact_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        schedule_ref = (
            await db.execute(
                select(GovernedScheduleBinding.binding_id)
                .where(GovernedScheduleBinding.input_artifact_id == artifact_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        return task_ref is None and schedule_ref is None

    try:
        async with db_engine.get_session() as db:
            await revoke_unpublished_input_artifact(
                db,
                owner,
                artifact_id=artifact_id,
                expected_revision=expected_revision,
                publication_guard=publication_guard,
            )
        return _PublicationArtifactCleanup("revoked")
    except BoardError as exc:
        if exc.code == "input_artifact_publication_protected":
            return _PublicationArtifactCleanup("protected", "artifact_publication_reference")
        return _PublicationArtifactCleanup("unknown", "artifact_cleanup_unconfirmed")
    except Exception:
        # A database/commit outcome is not proof that publication failed.  Do
        # not revoke a possibly accepted artifact after an ambiguous outcome.
        return _PublicationArtifactCleanup("unknown", "artifact_cleanup_unconfirmed")


def _publication_cleanup_error(
    original: ProcedureV2Error,
    cleanup: _PublicationArtifactCleanup,
    *,
    recovery_action: str,
) -> ProcedureV2Error:
    """Map cleanup state to a safe, honest procedure error envelope."""

    if cleanup.outcome == "revoked":
        suffix = (
            " The prepared input was revoked; use a new request key."
            if cleanup.reason is None
            else " The prepared input was revoked, but filesystem cleanup needs operator reconciliation; use a new request key."
        )
        return ProcedureV2Error(
            original.code,
            "The procedure was not published." + suffix,
            status_code=original.status_code,
            recovery_action=recovery_action,
            retryable=False,
            binding_id=original.binding_id,
            audit_receipt_id=original.audit_receipt_id,
        )
    return ProcedureV2Error(
        "procedure_publication_outcome_unknown",
        "The publication outcome could not be confirmed; retry the same request to reconcile it.",
        status_code=503,
        recovery_action="reconcile_publication",
        retryable=True,
        binding_id=original.binding_id,
        audit_receipt_id=original.audit_receipt_id,
    )


def _proof_digest(source_refs: Sequence[Mapping[str, Any]], plan: Mapping[str, Any]) -> str:
    return _digest({"source_refs": list(source_refs), "plan_digest": plan_digest(plan)})


def _procedure_create_request_digest(req: ProcedureV2CreateRequest) -> str:
    """Hash the original strict create request, independent of moving proof.

    A committed preparation must replay after the preview/source TTL without
    re-resolving its old leaf rows.  The request digest therefore binds only
    the canonical request DTO and explicit schema marker; immutable plan and
    source proof digests remain in the prepared version itself.
    """

    return _digest(
        {
            "schema_version": 2,
            "template_id": req.template_id,
            "source_tasks": [
                {
                    "task_id": item.task_id,
                    "expected_revision": item.expected_revision,
                }
                for item in req.source_tasks
            ],
            "name": req.name,
            "idempotency_key": req.idempotency_key,
            "preview_digest": req.preview_digest,
        }
    )


def _safe_effect_digest(item: Mapping[str, Any]) -> str | None:
    details = item.get("details") if isinstance(item.get("details"), Mapping) else {}
    for key in ("content_sha256", "artifact_sha256", "target_digest", "readback_digest"):
        value = _text(item.get(key) or details.get(key)).lower()
        if len(value) == 64 and all(char in "0123456789abcdef" for char in value):
            return value
    return None


def _verified_readback(effects: Sequence[Any]) -> bool:
    for raw in effects:
        if not isinstance(raw, Mapping):
            continue
        if _text(raw.get("receipt_kind")) != "readback" or _text(raw.get("status")) != "succeeded":
            continue
        details = raw.get("details") if isinstance(raw.get("details"), Mapping) else {}
        if raw.get("verified") is not True and details.get("verified") is not True:
            continue
        if _safe_effect_digest(raw):
            return True
    return False


def _unresolved_effect(effects: Sequence[Any]) -> bool:
    for raw in effects:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("reconciled") is True or _text(raw.get("reconciliation_status")) in {"reconciled", "resolved"}:
            continue
        if _text(raw.get("status")) in {"unknown", "unknown_external_effect", "cost_liability", "intent", "dispatched"}:
            return True
        details = raw.get("details") if isinstance(raw.get("details"), Mapping) else {}
        if details.get("reconciliation_required") or details.get("unknown_cost_outstanding"):
            return True
    return False


def _proof_refs(task: WorkBoardTask, attempt: WorkBoardAttempt, job: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[Any] = []
    for owner in (task, attempt):
        for field in ("artifact_refs_json", "result_refs_json", "receipt_refs_json"):
            raw = getattr(owner, field, None)
            decoded = _json(raw, [])
            if isinstance(decoded, list):
                values.extend(decoded)
    for field in ("artifacts", "effects"):
        raw = job.get(field)
        if isinstance(raw, list):
            values.extend(raw)
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        identifier = _text(
            item.get("artifact_id")
            or item.get("readback_id")
            or item.get("verification_id")
            or item.get("effect_id")
        )
        digest = _safe_effect_digest(item)
        if not identifier or not digest or (identifier, digest) in seen:
            continue
        seen.add((identifier, digest))
        refs.append(
            {
                "artifact_id": identifier,
                "sha256": digest,
                "receipt_kind": _text(item.get("receipt_kind")) or "artifact",
                "status": _text(item.get("status")) or "succeeded",
                "workflow_run_id": _text(item.get("workflow_run_id") or job.get("job_id") or job.get("run_identity")),
            }
        )
        if len(refs) >= 20:
            break
    return refs


_WORKSPACE_ARTIFACT_PATH = re.compile(r"^[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)+$")
_WORKSPACE_ARTIFACT_MAX_BYTES = 10 * 1024 * 1024


def _verified_workspace_artifact(path: Any, digest: Any) -> bool:
    """Verify the bytes behind a durable artifact/readback receipt.

    Durable projections are useful identity receipts, but an ``exists`` flag
    alone cannot prove that the output still exists after a later cleanup.
    Read the bounded, workspace-contained file with ``O_NOFOLLOW`` and compare
    its bytes to the recorded digest before accepting source proof.
    """

    relative = _text(path)
    expected = _text(digest).lower()
    if (
        not relative
        or len(relative) > 512
        or not _WORKSPACE_ARTIFACT_PATH.fullmatch(relative)
        or relative.startswith("/")
        or ".." in Path(relative).parts
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        return False
    root = canonical_workspace_root(settings.workspace_dir)
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root)
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, ValueError):
        return False
    try:
        stat_result = os.fstat(descriptor)
        if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_size > _WORKSPACE_ARTIFACT_MAX_BYTES:
            return False
        digest = hashlib.sha256()
        remaining = int(stat_result.st_size)
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                return False
            digest.update(chunk)
            remaining -= len(chunk)
        return digest.hexdigest() == expected
    except OSError:
        return False
    finally:
        os.close(descriptor)


def _native_proof_ref(proof: Mapping[str, Any], *, job_id: str) -> list[dict[str, Any]]:
    artifact_id = _text(proof.get("artifact_id"))
    readback_id = _text(proof.get("readback_id"))
    digest = _text(proof.get("content_sha256")).lower()
    identifier = artifact_id or readback_id
    if not identifier or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return []
    return [
        {
            "artifact_id": identifier,
            "sha256": digest,
            "receipt_kind": "readback",
            "status": "succeeded",
            "workflow_run_id": job_id,
        }
    ]


def _job_readback_file(job: Mapping[str, Any], *, job_id: str) -> tuple[str, str] | None:
    """Find and verify one exact artifact/readback pair from a job projection."""

    artifacts = job.get("artifacts") if isinstance(job.get("artifacts"), list) else []
    effects = job.get("effects") if isinstance(job.get("effects"), list) else []
    for effect in reversed(effects[-100:]):
        if not isinstance(effect, Mapping):
            continue
        details = effect.get("details") if isinstance(effect.get("details"), Mapping) else {}
        if (
            effect.get("receipt_kind") != "readback"
            or effect.get("status") not in {"succeeded", "read_back", "reconciled"}
            or details.get("verified") is not True
        ):
            continue
        path = _text(effect.get("target_path"))
        digest = _text(effect.get("content_sha256") or effect.get("target_digest")).lower()
        if not path or not _verified_workspace_artifact(path, digest):
            continue
        if any(
            isinstance(item, Mapping)
            and item.get("exists") is True
            and _text(item.get("file_path")) == path
            and _text(item.get("content_sha256")).lower() == digest
            for item in artifacts[-100:]
        ):
            return path, digest
    return None


def _validated_immutable_step_inputs(value: Any, spec: Any) -> dict[str, dict[str, Any]]:
    """Validate the private, copied executable inputs in a v2 version."""

    raw = value if isinstance(value, Mapping) else {}
    expected_browser_steps = {
        str(step.step_id)
        for step in spec.steps
        if str(step.capability_id) == "browser.public-task.v1"
    }
    if set(raw) != expected_browser_steps:
        if expected_browser_steps:
            raise ValueError("immutable browser input is missing")
        return {}
    from src.browser.task_runner import BrowserTaskInput, _browser_input_digests

    validated: dict[str, dict[str, Any]] = {}
    for step_id in sorted(expected_browser_steps):
        entry = raw.get(step_id)
        if not isinstance(entry, Mapping) or set(entry) != {
            "browser_input",
            "browser_input_digest",
            "input_envelope_digest",
            "action_consent_digest",
        }:
            raise ValueError("immutable browser input shape is invalid")
        model = BrowserTaskInput.model_validate(entry["browser_input"])
        envelope_digest, model_digest, consent_digest = _browser_input_digests(model)
        if (
            _text(entry.get("browser_input_digest")) != model_digest
            or _text(entry.get("input_envelope_digest")) != envelope_digest
            or _text(entry.get("action_consent_digest")) != consent_digest
        ):
            raise ValueError("immutable browser input digest is invalid")
        validated[step_id] = {
            "browser_input": model.model_dump(mode="json", exclude_none=True),
            "browser_input_digest": model_digest,
            "input_envelope_digest": envelope_digest,
            "action_consent_digest": consent_digest,
        }
    return validated


def _material_change(job: Mapping[str, Any]) -> bool:
    result = job.get("result") if isinstance(job.get("result"), Mapping) else {}
    if result.get("no_change") is True or _text(result.get("status")) == "no_change":
        return False
    if result.get("material_change") is True or result.get("changed") is True:
        return True
    for raw in job.get("effects") if isinstance(job.get("effects"), list) else []:
        if not isinstance(raw, Mapping):
            continue
        details = raw.get("details") if isinstance(raw.get("details"), Mapping) else {}
        if details.get("no_change") is True or _text(details.get("status")) == "no_change":
            continue
        if details.get("material_change") is True or details.get("changed") is True:
            return True
    return False


class ProcedureV2Service:
    def __init__(self, routine_service: "RoutineService") -> None:
        self.routines = routine_service

    async def _source_task(
        self,
        db: Any,
        ref: ProcedureSourceTaskRef,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        expected_capability_id: str,
        copy_browser_input: bool,
    ) -> tuple[WorkBoardTask, WorkBoardAttempt, Mapping[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        task = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == ref.task_id,
                    WorkBoardTask.owner_principal_id == owner_principal_id,
                    WorkBoardTask.owner_session_id == owner_session_id,
                )
            )
        ).scalar_one_or_none()
        if task is None:
            raise ProcedureV2Error("procedure_source_task_not_found", "The source task is unavailable", status_code=404, recovery_action="select_verified_task")
        if int(task.task_revision or 0) != int(ref.expected_revision):
            raise ProcedureV2Error("procedure_source_task_revision_stale", "The source task revision changed", recovery_action="refresh_source_task")
        if _status(task.status) != WorkBoardStatus.done.value:
            raise ProcedureV2Error("procedure_source_task_not_verified", "The source task is not completed", recovery_action="select_verified_task")
        attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == task.task_id)
                .where(WorkBoardAttempt.ended_at.is_not(None))
                .order_by(WorkBoardAttempt.ended_at.desc())
            )
        ).scalars().first()
        if attempt is None or _status(attempt.outcome) not in {"succeeded", "completed", "degraded", "verified"} or not _text(attempt.workflow_run_id):
            raise ProcedureV2Error("procedure_source_attempt_not_verified", "The source task has no completed durable attempt", recovery_action="select_verified_task")
        job = await durable_job_repository.get_job(_text(attempt.workflow_run_id))
        if not isinstance(job, Mapping):
            raise ProcedureV2Error("procedure_source_job_missing", "The source durable job is unavailable", recovery_action="reconcile_source_task")
        if (
            _text(job.get("goal_id")) != _text(task.goal_id)
            or int(job.get("goal_revision") or 0) != int(task.goal_revision)
            or _status(job.get("status")) != "succeeded"
            or _text(task.capability_id) != expected_capability_id
        ):
            raise ProcedureV2Error("procedure_source_job_binding_invalid", "The source durable job binding is not verified", recovery_action="reconcile_source_task")

        # Every supported source capability has a fixed native verifier.  The
        # browser root is service-owned but delegates the operator/session in
        # its declared authority; accepting a generic service owner here would
        # allow a same-shaped foreign durable row to become procedure proof.
        native_proof: Mapping[str, Any] | None = None
        immutable_input: dict[str, Any] | None = None
        if expected_capability_id == "browser.public-task.v1":
            try:
                from src.api.work_board import _browser_execution_payload
                from src.browser.task_runner import BrowserTaskInput, _browser_input_digests
                from src.work_board.input_artifacts import resolve_input_artifact_for_copy

                copied = await resolve_input_artifact_for_copy(
                    db,
                    WorkBoardOwner(principal_id=owner_principal_id, session_id=owner_session_id),
                    typed_input_ref=_text(task.typed_input_ref),
                    typed_input_digest=_text(task.typed_input_digest),
                    capability_id=expected_capability_id,
                    goal_id=task.goal_id,
                    goal_revision=int(task.goal_revision),
                ) if copy_browser_input else None
                browser_projection = await _browser_execution_payload(task, attempt, projection=job)
                if not isinstance(browser_projection, Mapping) or browser_projection.get("durable_status") != "succeeded":
                    raise ProcedureV2Error("procedure_source_proof_missing", "The browser source has no independently verified native proof", recovery_action="select_verified_task")
                if not all(_text(browser_projection.get(key)) for key in ("artifact_id", "readback_id", "file_path", "content_sha256")):
                    raise ProcedureV2Error("procedure_source_proof_missing", "The browser source has no complete native readback", recovery_action="select_verified_task")
                if not _verified_workspace_artifact(browser_projection.get("file_path"), browser_projection.get("content_sha256")):
                    raise ProcedureV2Error("procedure_source_proof_missing", "The browser source artifact is missing or changed", recovery_action="reconcile_source_task")
                native_proof = browser_projection
                if copied is not None:
                    if (
                        _text(copied.row.artifact_id) != _text(task.input_artifact_id)
                        or _text(copied.row.bound_task_id) != _text(task.task_id)
                        or int(copied.row.bound_task_revision or 0) > int(task.task_revision or 0)
                    ):
                        raise ProcedureV2Error(
                            "procedure_source_input_binding_invalid",
                            "The browser source input is not bound to the verified task",
                            recovery_action="select_verified_task",
                        )
                    model = BrowserTaskInput.model_validate(copied.input)
                    envelope_digest, model_digest, consent_digest = _browser_input_digests(model)
                    authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
                    if (
                        _text(authority.get("input_artifact_digest")) != _text(task.typed_input_digest)
                        or _text(authority.get("input_envelope_digest")) != envelope_digest
                        or _text(authority.get("browser_input_digest")) != model_digest
                        or _text(authority.get("action_consent_digest")) != consent_digest
                    ):
                        raise ProcedureV2Error("procedure_source_input_binding_invalid", "The browser source input does not match its native authority", recovery_action="select_verified_task")
                    immutable_input = {
                        "browser_input": model.model_dump(mode="json", exclude_none=True),
                        "browser_input_digest": model_digest,
                        "input_envelope_digest": envelope_digest,
                        "action_consent_digest": consent_digest,
                    }
            except ProcedureV2Error:
                raise
            except Exception as exc:
                raise ProcedureV2Error("procedure_source_input_binding_invalid", "The browser source input could not be verified", recovery_action="select_verified_task") from exc
        elif expected_capability_id == "calendar.meeting-prep.v1":
            try:
                from src.api.work_board import _calendar_execution_payload

                calendar_projection = await _calendar_execution_payload(task, attempt, db=db, projection=job)
            except Exception as exc:
                raise ProcedureV2Error("procedure_source_proof_missing", "The Calendar source proof could not be read", recovery_action="reconcile_source_task") from exc
            if (
                not isinstance(calendar_projection, Mapping)
                or calendar_projection.get("durable_status") != "succeeded"
                or not all(_text(calendar_projection.get(key)) for key in ("artifact_id", "readback_id", "file_path", "content_sha256"))
                or not _verified_workspace_artifact(calendar_projection.get("file_path"), calendar_projection.get("content_sha256"))
            ):
                raise ProcedureV2Error("procedure_source_proof_missing", "The Calendar source has no complete native readback", recovery_action="select_verified_task")
            native_proof = calendar_projection
        elif expected_capability_id == "guardian.research-watch.v1":
            try:
                from src.work_board.review import _verified_workflow_readback

                watch_proof = await _verified_workflow_readback(db, task, attempt)
            except Exception as exc:
                raise ProcedureV2Error("procedure_source_proof_missing", "The research source proof could not be read", recovery_action="reconcile_source_task") from exc
            if not isinstance(watch_proof, Mapping) or not _text(watch_proof.get("readback_id")):
                raise ProcedureV2Error("procedure_source_proof_missing", "The research source has no independently verified leaf proof", recovery_action="select_verified_task")
            if _job_readback_file(job, job_id=_text(attempt.workflow_run_id)) is None:
                raise ProcedureV2Error("procedure_source_proof_missing", "The research source artifact is missing or changed", recovery_action="reconcile_source_task")
            native_proof = watch_proof
        else:
            raise ProcedureV2Error("procedure_source_capability_unsupported", "The selected source capability has no native procedure verifier", recovery_action="select_verified_task")

        effects = job.get("effects") if isinstance(job.get("effects"), list) else []
        if _unresolved_effect(effects):
            raise ProcedureV2Error("procedure_source_proof_missing", "The selected source task has an unresolved external effect", recovery_action="reconcile_source_task")
        refs = _native_proof_ref(native_proof or {}, job_id=_text(attempt.workflow_run_id))
        if not refs:
            raise ProcedureV2Error("procedure_source_artifact_missing", "The source task has no current artifact/readback hash", recovery_action="select_verified_task")
        return task, attempt, job, refs, immutable_input

    async def _resolve_sources(
        self,
        req: ProcedureV2PreviewRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        copy_browser_inputs: bool = True,
    ) -> dict[str, Any]:
        try:
            spec = get_procedure_template(req.template_id)
        except Exception as exc:
            raise ProcedureV2Error("procedure_template_unknown", "The procedure template is not registered", status_code=422, recovery_action="choose_registered_template") from exc
        expected_count = len(spec.steps)
        if len(req.source_tasks) != expected_count:
            raise ProcedureV2Error("procedure_source_count_invalid", "The selected template requires an exact number of verified source tasks", status_code=422, recovery_action="select_verified_task")
        async with db_engine.get_session() as db:
            resolved: list[tuple[WorkBoardTask, WorkBoardAttempt, Mapping[str, Any], list[dict[str, Any]], dict[str, Any] | None]] = []
            for ref, step in zip(req.source_tasks, spec.steps, strict=True):
                resolved.append(
                    await self._source_task(
                        db,
                        ref,
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                        expected_capability_id=step.capability_id,
                        copy_browser_input=copy_browser_inputs,
                    )
                )
        first_task = resolved[0][0]
        source_refs: list[dict[str, Any]] = []
        step_inputs: dict[str, dict[str, str]] = {}
        immutable_step_inputs: dict[str, dict[str, Any]] = {}
        for index, ((task, attempt, job, refs, immutable_input), step) in enumerate(zip(resolved, spec.steps, strict=True)):
            if _text(task.capability_id) != step.capability_id:
                raise ProcedureV2Error("procedure_source_capability_mismatch", "The source task capability does not match the selected template", recovery_action="select_verified_task")
            registered = REGISTERED_CAPABILITIES.get(step.capability_id)
            if registered is None or str(registered.version) != step.capability_version:
                raise ProcedureV2Error("procedure_source_capability_unavailable", "The source capability version is unavailable", recovery_action="retry_after_prerequisite")
            if task.goal_id != first_task.goal_id or int(task.goal_revision) != int(first_task.goal_revision):
                raise ProcedureV2Error("procedure_source_goal_lineage_mismatch", "Source tasks must share one goal revision", recovery_action="select_verified_task")
            if index == 0 and spec.requires_material_change and not _material_change(job):
                raise ProcedureV2Error("procedure_source_no_material_change", "The source watch has no material change to continue", recovery_action="wait_for_material_change")
            input_ref = _text(task.typed_input_ref)
            input_digest = _text(task.typed_input_digest).lower()
            if not input_ref or len(input_digest) != 64:
                raise ProcedureV2Error("procedure_source_input_binding_missing", "The source task input binding is incomplete", recovery_action="select_verified_task")
            source_refs.append(
                {
                    "task_id": task.task_id,
                    "task_revision": int(task.task_revision),
                    "attempt_id": attempt.attempt_id,
                    "job_id": _text(attempt.workflow_run_id),
                    "artifact_ids_and_hashes": refs,
                    "capability_id": step.capability_id,
                    "capability_version": step.capability_version,
                    "goal_id": task.goal_id,
                    "goal_revision": int(task.goal_revision),
                }
            )
            step_inputs[step.step_id] = {
                "typed_input_ref": input_ref,
                "typed_input_digest": input_digest,
            }
            if immutable_input is not None:
                immutable_step_inputs[step.step_id] = immutable_input
        plan = build_procedure_plan(req.template_id, step_inputs=step_inputs)
        plan_payload = plan.model_dump(mode="json")
        return {
            "spec": spec,
            "plan": plan,
            "plan_payload": plan_payload,
            "source_refs": source_refs,
            "immutable_step_inputs": immutable_step_inputs,
            "goal_id": first_task.goal_id,
            "goal_revision": int(first_task.goal_revision),
        }

    @staticmethod
    def _preview_payload(
        resolved: Mapping[str, Any],
        req: ProcedureV2PreviewRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        spec = resolved["spec"]
        plan_payload = resolved["plan_payload"]
        payload = {
            "template_id": req.template_id,
            "name": req.name,
            "idempotency_key": req.idempotency_key,
            "owner_principal_id": owner_principal_id,
            "owner_session_id": owner_session_id,
            "expires_at": _utc(expires_at).isoformat(),
            "plan": plan_payload,
            "source_refs": resolved["source_refs"],
        }
        return {
            "status": "preview",
            "template_id": spec.template_id,
            "preview_digest": preview_digest(payload),
            "expires_at": _utc(expires_at).isoformat().replace("+00:00", "Z"),
            "plan": plan_payload,
            "source_refs": resolved["source_refs"],
            "parameter_schema": [
                {"name": name, "kind": kind, "required": required}
                for name, kind, required in spec.parameters
            ],
            "permissions": list(spec.permissions),
            "limits": {"max_steps": 2, "max_total_seconds": 300},
            "version_diff": None,
        }

    async def preview_from_tasks(
        self,
        req: ProcedureV2PreviewRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        resolved = await self._resolve_sources(req, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        return self._preview_payload(
            resolved,
            req,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            expires_at=_preview_expiry(),
        )

    async def _preview_for_digest(
        self,
        req: ProcedureV2CreateRequest,
        resolved: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        observed = _now()
        # A preview remains valid for its original 15-minute window.  Trying
        # the current and previous minute buckets keeps the digest stable
        # without storing a second preview ledger.
        base_expiry = _preview_expiry(observed)
        for offset in range(0, 16):
            expiry = base_expiry - timedelta(minutes=offset)
            candidate = self._preview_payload(
                resolved,
                req,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                expires_at=expiry,
            )
            if candidate["preview_digest"] == req.preview_digest:
                if _utc(datetime.fromisoformat(candidate["expires_at"].replace("Z", "+00:00"))) <= observed:
                    raise ProcedureV2Error("procedure_preview_expired", "The procedure preview has expired", recovery_action="create_fresh_preview")
                return candidate
        raise ProcedureV2Error("procedure_preview_digest_mismatch", "The procedure preview no longer matches verified source proof", recovery_action="create_fresh_preview")

    @staticmethod
    def _routine_id(owner_principal_id: str, owner_session_id: str, key: str) -> str:
        return uuid5(ROUTINE_ID_NAMESPACE, f"{owner_principal_id}:{owner_session_id}:{key}").hex

    async def _binding_response(self, binding: ProcedureV2Binding, *, owner_principal_id: str, owner_session_id: str) -> dict[str, Any]:
        if binding.version_id:
            version = await self._version_by_id(binding.version_id, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
            plan = validate_procedure_plan(_json(version.source_provenance_json, {}).get("plan") or {})
            routine = await self._routine(binding.deterministic_routine_id, owner_principal_id, owner_session_id)
            package = self.routines._package_readback(owner_principal_id, owner_session_id, routine.id, int(version.version), version.installed_package_digest)
            provenance = _json(version.source_provenance_json, {})
            install_job_id = f"routine-install:{routine.id}:v{int(version.version)}"
            install_job = await durable_job_repository.get_job(install_job_id)
            install_approval = await self.routines._v2_install_approval_projection(
                job_id=install_job_id,
                job=install_job if isinstance(install_job, Mapping) else None,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                routine_id=str(routine.id),
                version=int(version.version),
            )
            # The persisted provenance also contains the private copied
            # BrowserTaskInput used by execution.  Keep that server-owned
            # input inside the immutable version; read/list responses expose
            # only the allow-listed, content-free provenance projection.
            from src.workflows.routines import _safe_routine_provenance

            safe_provenance = _safe_routine_provenance(provenance)
            return {
                "status": "prepared",
                "binding_id": binding.binding_id,
                "routine_id": routine.id,
                "version_id": version.id,
                "version": int(version.version),
                "schema_version": 2,
                "template_id": binding.template_id,
                "revision": int(binding.revision),
                "request_digest": binding.request_digest,
                "preview_digest": binding.preview_digest,
                "preview_expires_at": _utc(binding.preview_expires_at).isoformat().replace("+00:00", "Z"),
                "plan": plan.model_dump(mode="json"),
                "source_refs": _json(binding.source_refs_json, []),
                "package_state": package.get("status", "not_installed"),
                "routine_state": routine.state,
                "install_job_id": install_job_id,
                **install_approval,
                "audit_receipt_id": f"procedure-binding:{binding.binding_id}",
                "recovery_action": binding.recovery_reason,
                "source_provenance": safe_provenance,
            }
        return {
            "status": binding.state,
            "binding_id": binding.binding_id,
            "template_id": binding.template_id,
            "revision": int(binding.revision),
            "request_digest": binding.request_digest,
            "preview_digest": binding.preview_digest,
            "preview_expires_at": _utc(binding.preview_expires_at).isoformat().replace("+00:00", "Z"),
            "recovery_action": binding.recovery_reason,
            "audit_receipt_id": f"procedure-binding:{binding.binding_id}",
        }

    async def _routine(self, routine_id: str, owner_principal_id: str, owner_session_id: str) -> GuardianRoutine:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GuardianRoutine).where(
                        GuardianRoutine.id == routine_id,
                        GuardianRoutine.owner_principal_id == owner_principal_id,
                        GuardianRoutine.owner_session_id == owner_session_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ProcedureV2Error("procedure_routine_not_found", "The prepared procedure is unavailable", status_code=404, recovery_action="recreate_procedure")
            db.expunge(row)
            return row

    async def _version_by_id(self, version_id: str, *, owner_principal_id: str, owner_session_id: str) -> GuardianRoutineVersion:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GuardianRoutineVersion)
                    .join(GuardianRoutine, GuardianRoutine.id == GuardianRoutineVersion.routine_id)
                    .where(
                        GuardianRoutineVersion.id == version_id,
                        GuardianRoutine.owner_principal_id == owner_principal_id,
                        GuardianRoutine.owner_session_id == owner_session_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ProcedureV2Error("procedure_version_not_found", "The prepared procedure version is unavailable", status_code=404, recovery_action="recreate_procedure")
            db.expunge(row)
            return row

    async def _immutable_inputs_for_version(
        self,
        descriptor: V2VersionDescriptor,
    ) -> dict[str, dict[str, Any]]:
        """Load copied executable inputs without exposing them in version DTOs."""

        version = await self._version_by_id(
            descriptor.version_id,
            owner_principal_id=descriptor.owner_principal_id,
            owner_session_id=descriptor.owner_session_id,
        )
        provenance = _json(version.source_provenance_json, {})
        try:
            spec = get_procedure_template(descriptor.template_id)
            return _validated_immutable_step_inputs(
                provenance.get("immutable_step_inputs") if isinstance(provenance, Mapping) else None,
                spec,
            )
        except Exception as exc:
            raise ProcedureV2Error("procedure_version_proof_invalid", "The reviewed executable input is invalid", recovery_action="recreate_procedure") from exc

    async def create_from_tasks(
        self,
        req: ProcedureV2CreateRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[dict[str, Any], int]:
        # A committed preparation is its own immutable replay receipt.  Check
        # it before resolving source artifacts so a matching request remains a
        # 200 replay after the short-lived preview/source inputs expire.  The
        # source task IDs and revisions are retained in the binding and are
        # sufficient to reject a same-key request that names another source;
        # no fresh authority or provider contact is needed for this path.
        async with db_engine.get_session() as db:
            committed = (
                await db.execute(
                    select(ProcedureV2Binding).where(
                        ProcedureV2Binding.owner_principal_id == owner_principal_id,
                        ProcedureV2Binding.owner_session_id == owner_session_id,
                        ProcedureV2Binding.idempotency_key == req.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if committed is not None:
                stored_refs = _json(committed.source_refs_json, [])
                requested_refs = [
                    (str(item.task_id), int(item.expected_revision))
                    for item in req.source_tasks
                ]
                try:
                    persisted_refs = [
                        (str(item.get("task_id")), int(item.get("task_revision") or 0))
                        for item in stored_refs
                        if isinstance(item, Mapping)
                    ] if isinstance(stored_refs, list) else []
                except (TypeError, ValueError, OverflowError):
                    raise ProcedureV2Error(
                        "procedure_binding_blocked",
                        "The prepared procedure requires reconciliation",
                        recovery_action="reconcile_preparation",
                        binding_id=committed.binding_id,
                    )
                if (
                    committed.request_digest != _procedure_create_request_digest(req)
                    or persisted_refs != requested_refs
                ):
                    raise ProcedureV2Error(
                        "procedure_binding_conflict",
                        "The idempotency key is bound to another reviewed procedure",
                        recovery_action="use_new_idempotency_key",
                        binding_id=committed.binding_id,
                    )
                if committed.state == "prepared":
                    if not committed.version_id:
                        raise ProcedureV2Error(
                            "procedure_binding_blocked",
                            "The prepared procedure requires reconciliation",
                            recovery_action="reconcile_preparation",
                            binding_id=committed.binding_id,
                        )
                    db.expunge(committed)
                    return await self._binding_response(
                        committed,
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                    ), 200
                if committed.state == "blocked":
                    raise ProcedureV2Error(
                        "procedure_binding_blocked",
                        "The prepared procedure requires reconciliation",
                        recovery_action="recreate_procedure",
                        binding_id=committed.binding_id,
                    )
                if committed.state not in {"preparing"}:
                    raise ProcedureV2Error(
                        "procedure_binding_state_invalid",
                        "The procedure preparation is in an unrecoverable state",
                        recovery_action="recreate_procedure",
                        binding_id=committed.binding_id,
                    )
        base_req = ProcedureV2PreviewRequest.model_validate(req.model_dump(exclude={"preview_digest"}))
        resolved = await self._resolve_sources(base_req, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        preview = await self._preview_for_digest(req, resolved, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id)
        request_digest = _procedure_create_request_digest(req)
        deterministic_id = self._routine_id(owner_principal_id, owner_session_id, req.idempotency_key)
        async with db_engine.get_session() as db:
            await db.commit()
            await db.execute(text("BEGIN IMMEDIATE"))
            binding = (
                await db.execute(
                    select(ProcedureV2Binding).where(
                        ProcedureV2Binding.owner_principal_id == owner_principal_id,
                        ProcedureV2Binding.owner_session_id == owner_session_id,
                        ProcedureV2Binding.idempotency_key == req.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if binding is not None:
                if binding.request_digest != request_digest or binding.preview_digest != req.preview_digest:
                    raise ProcedureV2Error("procedure_binding_conflict", "The idempotency key is bound to another reviewed procedure", recovery_action="use_new_idempotency_key", binding_id=binding.binding_id)
                if binding.state == "blocked":
                    raise ProcedureV2Error("procedure_binding_blocked", "The prepared procedure requires reconciliation", recovery_action="recreate_procedure", binding_id=binding.binding_id)
                if binding.state not in {"preparing", "prepared"}:
                    raise ProcedureV2Error("procedure_binding_state_invalid", "The procedure preparation is in an unrecoverable state", recovery_action="recreate_procedure", binding_id=binding.binding_id)
                db.expunge(binding)
                # A prepared row is a normal idempotent replay.  A preparing
                # row is the persisted crash fence: resume the exact
                # deterministic routine/version/install job instead of
                # returning an indefinite pending projection.
                replay = binding.state == "prepared"
            else:
                binding = ProcedureV2Binding(
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    idempotency_key=req.idempotency_key,
                    request_digest=request_digest,
                    source_refs_json=_canonical(resolved["source_refs"]),
                    deterministic_routine_id=deterministic_id,
                    routine_name=req.name,
                    template_id=req.template_id,
                    preview_digest=req.preview_digest,
                    preview_expires_at=_utc(datetime.fromisoformat(preview["expires_at"].replace("Z", "+00:00"))),
                    state="preparing",
                    revision=1,
                )
                db.add(binding)
                await db.flush()
                db.expunge(binding)
                replay = False
            await db.commit()
        if replay:
            return await self._binding_response(binding, owner_principal_id=owner_principal_id, owner_session_id=owner_session_id), 200

        try:
            plan = resolved["plan"]
            provenance = {
                "schema_version": 2,
                "template_id": req.template_id,
                "source_refs": resolved["source_refs"],
                "source_task_ids": [item["task_id"] for item in resolved["source_refs"]],
                "source_attempt_ids": [item["attempt_id"] for item in resolved["source_refs"]],
                "source_job_ids": [item["job_id"] for item in resolved["source_refs"]],
                "verified_artifact_ids_and_hashes": [
                    artifact
                    for item in resolved["source_refs"]
                    for artifact in item["artifact_ids_and_hashes"]
                ],
                # Browser input bytes are copied only after the native source
                # proof has passed.  This private version field is execution
                # authority for future leaf materialization; it is never part
                # of the public provenance projection.
                "immutable_step_inputs": resolved["immutable_step_inputs"],
                "capability_versions": [step.capability_version for step in resolved["spec"].steps],
                "plan_digest": plan_digest(plan),
                "parameter_schema": [
                    {"name": name, "kind": kind, "required": required}
                    for name, kind, required in resolved["spec"].parameters
                ],
                "plan": plan.model_dump(mode="json"),
                "preview_digest": req.preview_digest,
                "preview_expires_at": preview["expires_at"],
                "source_proof_digest": _proof_digest(resolved["source_refs"], plan),
                "deterministic_routine_id": deterministic_id,
            }
            from src.workflows.routine_templates import render_runbook, render_workflow, validate_generated_files

            workflow = render_workflow(routine_id=deterministic_id, version=1, name=req.name, template_id=req.template_id)
            runbook = render_runbook(routine_id=deterministic_id, version=1, name=req.name, template_id=req.template_id)
            if not validate_generated_files(workflow=workflow, runbook=runbook, routine_id=deterministic_id, version=1, template_id=req.template_id).get("valid"):
                raise ProcedureV2Error("procedure_template_invalid", "The generated reviewed procedure is invalid", status_code=500, recovery_action="retry_after_prerequisite")
            routine = GuardianRoutine(
                id=deterministic_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                name=req.name,
                state="prepared",
                revision=1,
                current_version=None,
            )
            version = GuardianRoutineVersion(
                id=str(uuid5(NAMESPACE_URL, f"seraph:procedure-v2-version:{deterministic_id}:1")),
                routine_id=deterministic_id,
                version=1,
                source_provenance_json=_canonical(provenance),
                workflow_bytes=workflow,
                workflow_sha256=hashlib.sha256(workflow.encode()).hexdigest(),
                runbook_bytes=runbook,
                runbook_sha256=hashlib.sha256(runbook.encode()).hexdigest(),
                source_repository=None,
                source_action=None,
                source_issue_number=None,
            )
            routine, version = await self.routines._persist_or_verify_board_preparation(routine, version)
            install_job_id = f"routine-install:{deterministic_id}:v1"
            authority = {
                "principal": owner_principal_id,
                "owner_kind": "user",
                "session_id": owner_session_id,
                "goal_owner_principal_id": owner_principal_id,
                "goal_owner_session_id": owner_session_id,
                "routine_id": deterministic_id,
                "routine_version": 1,
                "template_id": req.template_id,
                "plan_digest": provenance["plan_digest"],
                "source_proof_digest": provenance["source_proof_digest"],
                "workflow_sha256": version.workflow_sha256,
                "runbook_sha256": version.runbook_sha256,
                "source_provenance_sha256": _raw_digest(version.source_provenance_json),
                "capability_id": ROUTINE_V2_CAPABILITY_VERSION,
                "budget_microusd": 0,
            }
            install_job = await self.routines._admit_user_job(
                job_id=install_job_id,
                job_kind="routine_install",
                idempotency_key=f"{deterministic_id}:1",
                inputs={"routine_id": deterministic_id, "version": 1},
                authority=authority,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                goal_id=resolved["goal_id"],
                goal_revision=int(resolved["goal_revision"]),
                plan_revision=None,
                candidate_id=None,
                capability_version=ROUTINE_V2_CAPABILITY_VERSION,
            )
            if _status(install_job.get("status")) == "awaiting_approval":
                approval_id = _text((install_job.get("declared_authority") or {}).get("approval_id")) or None
            else:
                approval_id = await self.routines._hold_approval(
                    install_job,
                    tool_name="guardian:routine-install",
                    summary=f"Install reviewed guardian procedure {deterministic_id} version 1",
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
            async with db_engine.get_session() as db:
                result = await db.execute(
                    update(ProcedureV2Binding)
                    .where(
                        ProcedureV2Binding.binding_id == binding.binding_id,
                        ProcedureV2Binding.owner_principal_id == owner_principal_id,
                        ProcedureV2Binding.owner_session_id == owner_session_id,
                        ProcedureV2Binding.state == "preparing",
                        ProcedureV2Binding.revision == int(binding.revision),
                    )
                    .values(
                        version_id=version.id,
                        state="prepared",
                        recovery_reason=None,
                        revision=ProcedureV2Binding.revision + 1,
                        updated_at=_now(),
                    )
                )
                if int(result.rowcount or 0) != 1:
                    raise ProcedureV2Error("procedure_binding_recovery_race", "The procedure preparation changed concurrently", recovery_action="reconcile_preparation", binding_id=binding.binding_id)
            response = await self._binding_response(
                await self._binding(binding.binding_id, owner_principal_id, owner_session_id),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
            return response, 201
        except ProcedureV2Error:
            raise
        except Exception as exc:
            raise ProcedureV2Error("procedure_preparation_failed", "The reviewed procedure could not be prepared", status_code=503, recovery_action="reconcile_preparation", retryable=True, binding_id=binding.binding_id) from exc

    async def _binding(self, binding_id: str, owner_principal_id: str, owner_session_id: str) -> ProcedureV2Binding:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(ProcedureV2Binding).where(
                        ProcedureV2Binding.binding_id == binding_id,
                        ProcedureV2Binding.owner_principal_id == owner_principal_id,
                        ProcedureV2Binding.owner_session_id == owner_session_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ProcedureV2Error("procedure_binding_not_found", "The procedure preparation is unavailable", status_code=404, recovery_action="recreate_procedure")
            db.expunge(row)
            return row

    async def resolve_v2_version(
        self,
        routine_id: str,
        version_number: int,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> V2VersionDescriptor:
        routine = await self._routine(routine_id, owner_principal_id, owner_session_id)
        if _status(routine.state) != "active":
            raise ProcedureV2Error("procedure_routine_not_active", "The reviewed procedure is not active", recovery_action="activate_procedure")
        async with db_engine.get_session() as db:
            version = (
                await db.execute(
                    select(GuardianRoutineVersion).where(
                        GuardianRoutineVersion.routine_id == routine_id,
                        GuardianRoutineVersion.version == version_number,
                    )
                )
            ).scalar_one_or_none()
            if version is None:
                raise ProcedureV2Error("procedure_version_not_found", "The reviewed procedure version is unavailable", status_code=404, recovery_action="recreate_procedure")
            db.expunge(version)
        provenance = _json(version.source_provenance_json, {})
        if not isinstance(provenance, Mapping) or provenance.get("schema_version") != 2:
            raise ProcedureV2Error("procedure_version_schema_invalid", "The selected version is not a reviewed procedure v2 version", recovery_action="select_v2_version")
        try:
            plan = validate_procedure_plan(provenance.get("plan") or {})
            spec = get_procedure_template(str(provenance.get("template_id") or ""))
            immutable_step_inputs = _validated_immutable_step_inputs(provenance.get("immutable_step_inputs"), spec)
        except Exception as exc:
            raise ProcedureV2Error("procedure_version_proof_invalid", "The reviewed procedure plan is invalid", recovery_action="recreate_procedure") from exc
        if int(routine.current_version or 0) != int(version_number) or not _text(version.installed_package_digest):
            raise ProcedureV2Error("procedure_version_not_active", "The selected procedure version is not active", recovery_action="activate_procedure")
        package = self.routines._package_readback(owner_principal_id, owner_session_id, routine_id, version_number, version.installed_package_digest)
        if package.get("status") != "active" or package.get("digest") != version.installed_package_digest:
            raise ProcedureV2Error("procedure_package_not_current", "The reviewed procedure package is not active", recovery_action="review_and_activate_package")
        source_refs = provenance.get("source_refs")
        if not isinstance(source_refs, list) or len(source_refs) != len(spec.steps):
            raise ProcedureV2Error("procedure_source_proof_missing", "The immutable source proof is incomplete", recovery_action="recreate_procedure")
        # Re-read the exact source tasks/jobs and immutable readback evidence;
        # the copied browser input survives source-artifact TTL, but deleted
        # output proof blocks a new invocation.
        source_req = ProcedureV2PreviewRequest(
            template_id=spec.template_id,
            source_tasks=[
                ProcedureSourceTaskRef(task_id=str(item.get("task_id")), expected_revision=int(item.get("task_revision") or 0))
                for item in source_refs
            ],
            name="source-proof",
            idempotency_key="source-proof",
        )
        resolved = await self._resolve_sources(
            source_req,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            copy_browser_inputs=False,
        )
        if plan_digest(plan) != str(provenance.get("plan_digest") or ""):
            raise ProcedureV2Error("procedure_plan_digest_changed", "The immutable reviewed plan changed", recovery_action="recreate_procedure")
        source_proof_digest = _proof_digest(resolved["source_refs"], plan)
        if source_proof_digest != str(provenance.get("source_proof_digest") or ""):
            raise ProcedureV2Error("procedure_source_proof_changed", "The immutable source proof changed", recovery_action="recreate_procedure")
        return V2VersionDescriptor(
            routine_id=routine_id,
            version_id=version.id,
            version=int(version.version),
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            routine_revision=int(routine.revision),
            template_id=spec.template_id,
            schema_version=2,
            plan_digest=str(provenance["plan_digest"]),
            source_proof_digest=source_proof_digest,
            preview_digest=str(provenance.get("preview_digest") or ""),
            installed_package_digest=str(version.installed_package_digest),
            plan=MappingProxyType(plan.model_dump(mode="json")),
            source_refs=tuple(MappingProxyType(dict(item)) for item in resolved["source_refs"]),
            capability_versions=tuple(step.capability_version for step in spec.steps),
            parameter_schema=tuple(
                MappingProxyType({"name": name, "kind": kind, "required": required})
                for name, kind, required in spec.parameters
            ),
        )

    async def _validate_parameters(
        self,
        descriptor: V2VersionDescriptor,
        *,
        goal_id: str,
        expected_goal_revision: int,
        parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        spec = get_procedure_template(descriptor.template_id)
        expected_names = {name for name, _kind, _required in spec.parameters}
        if set(parameters) != expected_names:
            raise ProcedureV2Error("procedure_parameters_invalid", "Procedure parameters do not match the registered template", status_code=422, recovery_action="review_parameters")
        if not isinstance(parameters, Mapping):
            raise ProcedureV2Error("procedure_parameters_invalid", "Procedure parameters must be an object", status_code=422, recovery_action="review_parameters")
        normalized = dict(parameters)
        if descriptor.template_id == "selected-meeting-prep":
            from src.work_board.dispatcher import CalendarMeetingPrepInput

            try:
                meeting = CalendarMeetingPrepInput.model_validate(normalized)
            except Exception as exc:
                raise ProcedureV2Error("procedure_parameters_invalid", "Calendar meeting parameters do not match the M5 contract", status_code=422, recovery_action="review_parameters") from exc
            normalized = meeting.model_dump(mode="json")
            if normalized["goal_id"] != goal_id or normalized["goal_revision"] != expected_goal_revision:
                raise ProcedureV2Error("procedure_goal_binding_mismatch", "The meeting input goal does not match the outer invocation goal", recovery_action="select_current_goal")
        else:
            if (
                type(normalized.get("goal_id")) is not str
                or type(normalized.get("expected_goal_revision")) is not int
                or normalized.get("goal_id") != goal_id
                or normalized.get("expected_goal_revision") != expected_goal_revision
            ):
                raise ProcedureV2Error("procedure_goal_binding_mismatch", "The procedure parameters do not match the outer invocation goal", recovery_action="select_current_goal")
            if descriptor.template_id == "watch-and-public-browser":
                watch_id = _text(normalized.get("source_watch_id"))
                async with db_engine.get_session() as db:
                    from src.db.models import GuardianSourceWatch

                    watch = (
                        await db.execute(
                            select(GuardianSourceWatch).where(
                                GuardianSourceWatch.id == watch_id,
                                GuardianSourceWatch.owner_principal_id == descriptor.owner_principal_id,
                                GuardianSourceWatch.owner_session_id == descriptor.owner_session_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if (
                        watch is None
                        or _status(watch.state) != "active"
                        or type(normalized.get("source_watch_id")) is not str
                        or type(normalized.get("expected_watch_revision")) is not int
                        or int(watch.plan_revision or 0) != normalized.get("expected_watch_revision")
                    ):
                        raise ProcedureV2Error("procedure_watch_authority_stale", "The source watch authority is stale", recovery_action="refresh_source_watch")
        return normalized

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
        descriptor = await self.resolve_v2_version(
            routine_id,
            version_number,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if not _text(invocation_uuid) or len(_text(invocation_uuid)) > 256:
            raise ProcedureV2Error("procedure_invocation_id_invalid", "The invocation identity is invalid", status_code=422, recovery_action="retry_with_new_invocation")
        async with db_engine.get_session() as db:
            goal = (
                await db.execute(
                    select(Goal).where(
                        Goal.id == goal_id,
                        Goal.owner_principal_id == owner_principal_id,
                        Goal.owner_session_id == owner_session_id,
                    )
                )
            ).scalar_one_or_none()
            if goal is None or int(goal.revision or 0) != int(expected_goal_revision) or _status(goal.status) != "active":
                raise ProcedureV2Error("procedure_goal_authority_stale", "The current invocation goal is unavailable or stale", recovery_action="select_current_goal")
        normalized = await self._validate_parameters(
            descriptor,
            goal_id=goal_id,
            expected_goal_revision=expected_goal_revision,
            parameters=parameters,
        )
        immutable_step_inputs = await self._immutable_inputs_for_version(descriptor)
        executable: dict[str, Mapping[str, Any]] = {}
        for step in descriptor.plan.get("steps", []):
            if not isinstance(step, Mapping):
                raise ProcedureV2Error("procedure_plan_invalid", "The reviewed plan step is invalid", recovery_action="recreate_procedure")
            step_id = str(step["step_id"])
            step_input: dict[str, Any] = {
                "typed_input_ref": step["typed_input_ref"],
                "typed_input_digest": step["typed_input_digest"],
                "parameters": normalized,
                "goal_id": goal_id,
                "goal_revision": expected_goal_revision,
                **(
                    {
                        "watch_id": normalized["source_watch_id"],
                        "expected_plan_revision": normalized["expected_watch_revision"],
                    }
                    if "source_watch_id" in normalized
                    else {}
                ),
            }
            copied_browser_input = immutable_step_inputs.get(step_id)
            if copied_browser_input is not None:
                step_input.update(copied_browser_input)
            executable[step_id] = MappingProxyType(step_input)
        scope = f"{INVOCATION_SCOPE_PREFIX}{routine_id}:{descriptor.version_id}"
        return V2InvocationDescriptor(
            version=descriptor,
            goal_id=goal_id,
            goal_revision=int(expected_goal_revision),
            parameters=MappingProxyType(normalized),
            invocation_uuid=_text(invocation_uuid),
            scope=scope,
            executable_steps=MappingProxyType(executable),
        )

    async def _assert_publication_authority(self, db: Any, descriptor: V2InvocationDescriptor) -> None:
        routine = (
            await db.execute(
                select(GuardianRoutine).where(
                    GuardianRoutine.id == descriptor.version.routine_id,
                    GuardianRoutine.owner_principal_id == descriptor.version.owner_principal_id,
                    GuardianRoutine.owner_session_id == descriptor.version.owner_session_id,
                    GuardianRoutine.state == "active",
                    GuardianRoutine.revision == descriptor.version.routine_revision,
                    GuardianRoutine.current_version == descriptor.version.version,
                )
            )
        ).scalar_one_or_none()
        goal = (
            await db.execute(
                select(Goal).where(
                    Goal.id == descriptor.goal_id,
                    Goal.owner_principal_id == descriptor.version.owner_principal_id,
                    Goal.owner_session_id == descriptor.version.owner_session_id,
                    Goal.revision == descriptor.goal_revision,
                    Goal.status == "active",
                )
            )
        ).scalar_one_or_none()
        if routine is None or goal is None:
            raise BoardError("procedure_authority_stale", "The reviewed procedure authority changed", status_code=409)

    @staticmethod
    def _validate_schedule_goal_budget(
        goal: Goal | None,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        expected_goal_revision: int,
        now: datetime | None = None,
    ) -> GoalAdmissionBudgetSnapshot:
        """Validate the current finite reviewed grant before schedule writes."""

        if (
            goal is None
            or _text(getattr(goal, "owner_principal_id", None)) != _text(owner_principal_id)
            or _text(getattr(goal, "owner_session_id", None)) != _text(owner_session_id)
            or type(getattr(goal, "revision", None)) is not int
            or int(goal.revision) != int(expected_goal_revision)
            or _status(getattr(goal, "status", None)) != "active"
        ):
            raise ProcedureV2Error(
                "procedure_goal_authority_stale",
                "The current schedule goal is unavailable or stale",
                recovery_action="select_current_goal",
            )
        raw_budget = getattr(goal, "admission_budget_json", None)
        if not raw_budget:
            raise ProcedureV2Error(
                "procedure_goal_budget_missing",
                "The schedule goal has no reviewed finite admission budget",
                recovery_action="review_goal_budget",
            )
        budget = deserialize_admission_budget(goal)
        if budget is None:
            raise ProcedureV2Error(
                "procedure_goal_budget_invalid",
                "The persisted schedule goal budget is invalid",
                recovery_action="repair_goal_budget",
            )
        try:
            snapshot = goal_admission_budget_snapshot(
                goal_id=str(goal.id),
                goal_revision=int(goal.revision),
                budget=budget,
            )
        except (TypeError, ValueError) as exc:
            raise ProcedureV2Error(
                "procedure_goal_budget_invalid",
                "The persisted schedule goal budget is invalid",
                recovery_action="repair_goal_budget",
            ) from exc
        if not bool(budget.reviewed_grant) or not _text(budget.grant_id):
            raise ProcedureV2Error(
                "procedure_goal_budget_missing_reviewed_grant",
                "A reviewed goal grant is required before creating a schedule",
                recovery_action="review_goal_budget",
            )
        if not bool(getattr(goal, "proactive_enabled", False)):
            raise ProcedureV2Error(
                "procedure_goal_proactivity_disabled",
                "Proactive execution is disabled for this goal",
                recovery_action="enable_goal_proactivity",
            )
        period_expires_at = budget.period_expires_at
        if period_expires_at is None:
            raise ProcedureV2Error(
                "procedure_goal_budget_finite_expiry_required",
                "A finite goal budget expiry is required before creating a schedule",
                recovery_action="set_goal_budget_expiry",
            )
        observed = _utc(now or _now())
        period_started_at = _utc(budget.period_started_at) if budget.period_started_at is not None else None
        period_expires_at = _utc(period_expires_at)
        if period_started_at is not None and period_started_at > observed:
            raise ProcedureV2Error(
                "procedure_goal_budget_period_not_started",
                "The goal budget period has not started",
                recovery_action="wait_for_goal_budget_period",
            )
        if period_expires_at <= observed:
            raise ProcedureV2Error(
                "procedure_goal_budget_period_expired",
                "The goal budget period has expired",
                recovery_action="review_goal_budget",
            )
        return snapshot

    async def _load_schedule_goal_budget(
        self,
        *,
        goal_id: str,
        expected_goal_revision: int,
        owner_principal_id: str,
        owner_session_id: str,
        now: datetime | None = None,
    ) -> GoalAdmissionBudgetSnapshot:
        """Read and validate one owner/session-fenced schedule budget."""

        async with db_engine.get_session() as db:
            goal = (
                await db.execute(
                    select(Goal)
                    .where(
                        Goal.id == goal_id,
                        Goal.owner_principal_id == owner_principal_id,
                        Goal.owner_session_id == owner_session_id,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            return self._validate_schedule_goal_budget(
                goal,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                expected_goal_revision=expected_goal_revision,
                now=now,
            )

    async def _matching_invocation_replay(
        self,
        routine_id: str,
        req: ProcedureV2InvokeRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[WorkBoardTask, V2InvocationDescriptor] | None:
        """Recover a matching task before rechecking mutable authority.

        The task row and its server-owned invocation body are the durable
        idempotency receipt.  A matching replay must remain readable after a
        later routine/goal revoke; a digest mismatch still fails closed.
        """

        async with db_engine.get_session() as db:
            existing = (
                await db.execute(
                    select(WorkBoardTask)
                    .where(
                        WorkBoardTask.owner_principal_id == owner_principal_id,
                        WorkBoardTask.owner_session_id == owner_session_id,
                        WorkBoardTask.idempotency_scope.like(f"{INVOCATION_SCOPE_PREFIX}{routine_id}:%"),
                        WorkBoardTask.idempotency_key == req.invocation_uuid,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is None:
                return None
            version = (
                await db.execute(
                    select(GuardianRoutineVersion)
                    .join(GuardianRoutine, GuardianRoutine.id == GuardianRoutineVersion.routine_id)
                    .where(
                        GuardianRoutineVersion.routine_id == routine_id,
                        GuardianRoutineVersion.version == req.version,
                        GuardianRoutine.owner_principal_id == owner_principal_id,
                        GuardianRoutine.owner_session_id == owner_session_id,
                    )
                )
            ).scalar_one_or_none()
            if version is None:
                return None
            provenance = _json(version.source_provenance_json, {})
            if not isinstance(provenance, Mapping) or provenance.get("schema_version") != 2:
                return None
            try:
                plan = validate_procedure_plan(provenance.get("plan") or {})
                spec = get_procedure_template(str(provenance.get("template_id") or ""))
                immutable_step_inputs = _validated_immutable_step_inputs(provenance.get("immutable_step_inputs"), spec)
            except Exception:
                return None
            expected_digest = _invocation_request_digest(
                routine_id=routine_id,
                version_id=str(version.id),
                plan_digest_value=str(provenance.get("plan_digest") or plan_digest(plan)),
                goal_id=req.goal_id,
                goal_revision=req.expected_goal_revision,
                parameters=req.parameters,
                invocation_uuid=req.invocation_uuid,
                routine_revision=req.expected_routine_revision,
            )
            if existing.body != _invocation_body(expected_digest):
                # The legacy static body is allowed to fall through to the
                # normal authority path; a new body with a different digest
                # is an idempotency conflict and must not create a second task.
                if existing.body != "Server-owned reviewed procedure invocation.":
                    raise ProcedureV2Error(
                        "procedure_invocation_conflict",
                        "The invocation key is bound to another request",
                        recovery_action="retry_with_new_invocation",
                    )
                return None
            source_refs = provenance.get("source_refs") if isinstance(provenance.get("source_refs"), list) else []
            descriptor = V2VersionDescriptor(
                routine_id=routine_id,
                version_id=str(version.id),
                version=int(version.version),
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                routine_revision=int(req.expected_routine_revision),
                template_id=spec.template_id,
                schema_version=2,
                plan_digest=str(provenance.get("plan_digest") or plan_digest(plan)),
                source_proof_digest=str(provenance.get("source_proof_digest") or ""),
                preview_digest=str(provenance.get("preview_digest") or ""),
                installed_package_digest=str(version.installed_package_digest or ""),
                plan=MappingProxyType(plan.model_dump(mode="json")),
                source_refs=tuple(MappingProxyType(dict(item)) for item in source_refs if isinstance(item, Mapping)),
                capability_versions=tuple(step.capability_version for step in spec.steps),
                parameter_schema=tuple(
                    MappingProxyType({"name": name, "kind": kind, "required": required})
                    for name, kind, required in spec.parameters
                ),
            )
            executable = {
                str(step["step_id"]): MappingProxyType(
                    {
                        "typed_input_ref": step["typed_input_ref"],
                        "typed_input_digest": step["typed_input_digest"],
                        "parameters": dict(req.parameters),
                        "goal_id": req.goal_id,
                        "goal_revision": req.expected_goal_revision,
                        **(
                            {
                                "watch_id": req.parameters["source_watch_id"],
                                "expected_plan_revision": req.parameters["expected_watch_revision"],
                            }
                            if "source_watch_id" in req.parameters
                            else {}
                        ),
                        **(
                            immutable_step_inputs.get(str(step["step_id"]), {})
                            if isinstance(step, Mapping)
                            else {}
                        ),
                    }
                )
                for step in descriptor.plan.get("steps", [])
                if isinstance(step, Mapping)
            }
            invocation = V2InvocationDescriptor(
                version=descriptor,
                goal_id=req.goal_id,
                goal_revision=int(req.expected_goal_revision),
                parameters=MappingProxyType(dict(req.parameters)),
                invocation_uuid=req.invocation_uuid,
                scope=existing.idempotency_scope,
                executable_steps=MappingProxyType(executable),
            )
            db.expunge(existing)
            return existing, invocation

    async def invoke_v2(
        self,
        routine_id: str,
        req: ProcedureV2InvokeRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[dict[str, Any], int]:
        replay = await self._matching_invocation_replay(
            routine_id,
            req,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if replay is not None:
            existing, replay_descriptor = replay
            return self._invocation_response(
                existing,
                replay_descriptor,
                input_artifact_id=existing.input_artifact_id,
                input_digest=existing.typed_input_digest,
            ), 200
        descriptor = await self.validate_v2_invocation_authority(
            routine_id,
            req.version,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=req.goal_id,
            expected_goal_revision=req.expected_goal_revision,
            parameters=req.parameters,
            invocation_uuid=req.invocation_uuid,
        )
        request_digest = _invocation_request_digest(
            routine_id=routine_id,
            version_id=descriptor.version.version_id,
            plan_digest_value=descriptor.version.plan_digest,
            goal_id=descriptor.goal_id,
            goal_revision=descriptor.goal_revision,
            parameters=descriptor.parameters,
            invocation_uuid=descriptor.invocation_uuid,
            routine_revision=descriptor.version.routine_revision,
        )
        owner = WorkBoardOwner(principal_id=owner_principal_id, session_id=owner_session_id)
        scope = descriptor.scope
        async with db_engine.get_session() as db:
            existing = (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == owner_principal_id,
                        WorkBoardTask.owner_session_id == owner_session_id,
                        WorkBoardTask.idempotency_scope == scope,
                        WorkBoardTask.idempotency_key == descriptor.invocation_uuid,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                artifact_request = WorkBoardInputArtifactCreate(
                    schema_version=1,
                    capability_id=ROUTINE_V2_CAPABILITY_VERSION,
                    goal_id=descriptor.goal_id,
                    goal_revision=descriptor.goal_revision,
                    input={
                        "routine_id": descriptor.version.routine_id,
                        "version": descriptor.version.version,
                        "expected_routine_revision": descriptor.version.routine_revision,
                        "goal_id": descriptor.goal_id,
                        "expected_goal_revision": descriptor.goal_revision,
                        "parameters": dict(descriptor.parameters),
                        "invocation_uuid": descriptor.invocation_uuid,
                    },
                    idempotency_key=descriptor.invocation_uuid,
                )
                candidate_artifact_id = _artifact_id(owner, artifact_request)
                candidate_request = WorkBoardTaskCreate(
                    title=f"Run reviewed procedure: {routine_id}",
                    body=_invocation_body(request_digest),
                    goal_id=descriptor.goal_id,
                    goal_revision=descriptor.goal_revision,
                    status=WorkBoardStatus.todo,
                    capability_id=ROUTINE_V2_CAPABILITY_VERSION,
                    input_artifact_id=candidate_artifact_id,
                    executor_id=registered_executor_id(ROUTINE_V2_CAPABILITY_VERSION),
                    priority=50,
                    idempotency_scope=scope,
                    idempotency_key=descriptor.invocation_uuid,
                )
                if existing.idempotency_payload_digest != _payload_digest(candidate_request):
                    raise ProcedureV2Error("procedure_invocation_conflict", "The invocation key is bound to another request", recovery_action="retry_with_new_invocation")
                db.expunge(existing)
            else:
                existing = None
        if existing is not None:
            return self._invocation_response(existing, descriptor, input_artifact_id=existing.input_artifact_id, input_digest=existing.typed_input_digest), 200
        artifact = await self._prepare_invocation_artifact(owner, descriptor)
        request = WorkBoardTaskCreate(
            title=f"Run reviewed procedure: {routine_id}",
            body=_invocation_body(request_digest),
            goal_id=descriptor.goal_id,
            goal_revision=descriptor.goal_revision,
            status=WorkBoardStatus.todo,
            capability_id=ROUTINE_V2_CAPABILITY_VERSION,
            input_artifact_id=artifact.artifact_id,
            priority=50,
            idempotency_scope=scope,
            idempotency_key=descriptor.invocation_uuid,
        )
        repository = WorkBoardRepository()
        try:
            async with db_engine.get_session() as db:
                mutation = await repository.create_task(
                    db,
                    owner,
                    request,
                    publication_authority_check=lambda _db: self._assert_publication_authority(_db, descriptor),
                )
        except BoardError as exc:
            original = ProcedureV2Error(
                exc.code,
                "The procedure publication was rejected by current authority.",
                status_code=getattr(exc, "status_code", 409),
                recovery_action="retry_after_prerequisite",
                retryable=getattr(exc, "status_code", 409) >= 500,
            )
            cleanup = await _cleanup_unpublished_procedure_artifact(
                owner,
                artifact_id=artifact.artifact_id,
                expected_revision=int(artifact.revision),
            )
            raise _publication_cleanup_error(
                original,
                cleanup,
                recovery_action="retry_with_new_invocation",
            ) from exc
        except Exception as exc:
            # The writer/commit result is ambiguous.  Keep the deterministic
            # invocation key and pending artifact for canonical reconciliation.
            raise ProcedureV2Error(
                "procedure_publication_outcome_unknown",
                "The publication outcome could not be confirmed; retry the same request to reconcile it.",
                status_code=503,
                recovery_action="reconcile_publication",
                retryable=True,
            ) from exc
        return self._invocation_response(mutation.task, descriptor, input_artifact_id=artifact.artifact_id, input_digest=artifact.typed_input_digest), 201

    async def _prepare_invocation_artifact(self, owner: WorkBoardOwner, descriptor: V2InvocationDescriptor):
        payload = {
            "routine_id": descriptor.version.routine_id,
            "version": descriptor.version.version,
            "expected_routine_revision": descriptor.version.routine_revision,
            "goal_id": descriptor.goal_id,
            "expected_goal_revision": descriptor.goal_revision,
            "parameters": dict(descriptor.parameters),
            "invocation_uuid": descriptor.invocation_uuid,
        }
        async with db_engine.get_session() as db:
            try:
                return await prepare_input_artifact(
                    db,
                    owner,
                    WorkBoardInputArtifactCreate(
                        schema_version=1,
                        capability_id=ROUTINE_V2_CAPABILITY_VERSION,
                        goal_id=descriptor.goal_id,
                        goal_revision=descriptor.goal_revision,
                        input=payload,
                        idempotency_key=descriptor.invocation_uuid,
                    ),
                )
            except BoardError as exc:
                raise ProcedureV2Error(exc.code, str(exc), status_code=getattr(exc, "status_code", 409), recovery_action="retry_after_prerequisite", retryable=getattr(exc, "status_code", 409) >= 500) from exc

    @staticmethod
    def _invocation_response(task: WorkBoardTask, descriptor: V2InvocationDescriptor, *, input_artifact_id: str | None, input_digest: str | None) -> dict[str, Any]:
        return {
            "status": "accepted",
            "routine_id": descriptor.version.routine_id,
            "version": descriptor.version.version,
            "schema_version": 2,
            "template_id": descriptor.version.template_id,
            "invocation_uuid": descriptor.invocation_uuid,
            "scope": descriptor.scope,
            # These identity fields come from the durable task receipt so an
            # exact replay remains correlated with the originally admitted
            # goal even if the mutable authority descriptor is re-resolved.
            "goal_id": task.goal_id,
            "goal_revision": int(task.goal_revision),
            "task_id": task.task_id,
            "attempt_id": None,
            "job_id": None,
            "input_artifact_id": input_artifact_id,
            "input_digest": input_digest,
            "plan_digest": descriptor.version.plan_digest,
            "revision": int(task.task_revision),
            "request_digest": _invocation_request_digest(
                routine_id=descriptor.version.routine_id,
                version_id=descriptor.version.version_id,
                plan_digest_value=descriptor.version.plan_digest,
                goal_id=descriptor.goal_id,
                goal_revision=descriptor.goal_revision,
                parameters=descriptor.parameters,
                invocation_uuid=descriptor.invocation_uuid,
                routine_revision=descriptor.version.routine_revision,
            ),
            "audit_receipt_id": f"work-board-event:{getattr(task, 'task_id', '')}",
            "recovery_action": None,
        }

    async def schedule_v2(
        self,
        routine_id: str,
        req: ProcedureV2ScheduleRequest,
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> tuple[dict[str, Any], int]:
        descriptor = await self.validate_v2_invocation_authority(
            routine_id,
            req.version,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=req.goal_id,
            expected_goal_revision=req.expected_goal_revision,
            parameters=req.parameters,
            invocation_uuid=f"schedule:{req.idempotency_key}",
        )
        if descriptor.version.template_id not in {"public-browser-check", "watch-and-public-browser"}:
            raise ProcedureV2Error("procedure_template_not_schedulable", "The selected procedure template cannot be scheduled", status_code=422, recovery_action="choose_schedulable_template")
        now = _now()
        expires = _utc(req.expires_at)
        if expires <= now or expires > now + SCHEDULE_TTL:
            raise ProcedureV2Error("procedure_schedule_expiry_invalid", "Schedule expiry must be within seven days", status_code=422, recovery_action="choose_finite_expiry")
        budget_snapshot = await self._load_schedule_goal_budget(
            goal_id=req.goal_id,
            expected_goal_revision=req.expected_goal_revision,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            now=now,
        )
        budget_expires_at = _utc(budget_snapshot.budget.period_expires_at)  # validated as finite above
        if expires > budget_expires_at:
            raise ProcedureV2Error(
                "procedure_schedule_expiry_exceeds_goal_budget",
                "The schedule must expire within the reviewed goal budget period",
                recovery_action="choose_finite_expiry",
            )
        request_digest = _digest(
            {
                "routine_id": routine_id,
                "version": req.version,
                "goal_id": req.goal_id,
                "goal_revision": req.expected_goal_revision,
                "parameters": req.parameters,
                "cadence": req.cadence.model_dump(mode="json"),
                "expires_at": expires.isoformat(),
                "routine_revision": req.expected_routine_revision,
            }
        )
        from src.scheduler.governed_schedules import action_spec, cron_for_cadence, latest_due_slot

        try:
            action_spec("guardian.run_procedure.v2")
        except Exception as exc:
            raise ProcedureV2Error("procedure_schedule_action_unavailable", "The governed procedure scheduler is not enabled", recovery_action="retry_after_prerequisite") from exc
        cadence = req.cadence.model_dump(mode="json")
        try:
            trigger = cron_for_cadence(cadence)
        except Exception as exc:
            raise ProcedureV2Error("procedure_schedule_cadence_invalid", "The schedule cadence is invalid", status_code=422, recovery_action="review_cadence") from exc
        _ = trigger
        async with db_engine.get_session() as db:
            existing = (
                await db.execute(
                    select(GovernedScheduleBinding).where(
                        GovernedScheduleBinding.owner_principal_id == owner_principal_id,
                        GovernedScheduleBinding.owner_session_id == owner_session_id,
                        GovernedScheduleBinding.schedule_idempotency_key == req.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.schedule_request_digest != request_digest:
                    raise ProcedureV2Error("procedure_schedule_conflict", "The schedule key is bound to another request", recovery_action="use_new_idempotency_key", binding_id=existing.binding_id)
                return {
                    "status": "scheduled",
                    "scheduled_job_id": existing.scheduled_job_id,
                    "binding_id": existing.binding_id,
                    "revision": int(existing.binding_revision),
                    "action_type": existing.action_type,
                    "routine_id": routine_id,
                    "version": req.version,
                    "template_id": descriptor.version.template_id,
                    "goal_id": existing.goal_id,
                    "goal_revision": int(existing.goal_revision),
                    "schedule_idempotency_key": existing.schedule_idempotency_key,
                    "input_digest": existing.input_digest,
                    "expires_at": _utc(existing.expires_at).isoformat().replace("+00:00", "Z"),
                    "state": existing.state,
                    "pause_route": f"/api/governed-schedules/{existing.binding_id}",
                    "recovery_action": None,
                    "audit_receipt_id": f"governed-schedule:{existing.binding_id}",
                }, 200
        owner = WorkBoardOwner(principal_id=owner_principal_id, session_id=owner_session_id)
        artifact = await self._prepare_schedule_artifact(owner, descriptor, req)
        scheduled_job_id = str(uuid5(NAMESPACE_URL, f"seraph:procedure-v2-schedule:{owner_principal_id}:{owner_session_id}:{req.idempotency_key}"))
        binding_id = str(uuid5(NAMESPACE_URL, f"seraph:procedure-v2-binding:{owner_principal_id}:{owner_session_id}:{req.idempotency_key}"))
        from src.scheduler.governed_schedules import _digest as schedule_digest

        try:
            async with db_engine.get_session() as db:
                job = ScheduledJob(
                    id=scheduled_job_id,
                    name=f"Reviewed procedure: {routine_id}",
                    enabled=True,
                    trigger_type="governed",
                    trigger_spec_json=_canonical(cadence),
                    action_type="guardian.run_procedure.v2",
                    action_spec_json=_canonical(
                        {
                            "binding_id": binding_id,
                            "routine_id": routine_id,
                            "version": req.version,
                            "version_id": descriptor.version.version_id,
                            "template_id": descriptor.version.template_id,
                            "goal_id": req.goal_id,
                            "goal_revision": req.expected_goal_revision,
                            "parameters": req.parameters,
                            "consent_kind": "goal_budget",
                            "consent_id": None,
                            "consent_revision": 1,
                            "consent_digest": budget_snapshot.digest,
                            "goal_budget_digest": budget_snapshot.digest,
                            "goal_budget_grant_id": _text(budget_snapshot.budget.grant_id),
                            "goal_budget_max_outstanding_jobs": int(budget_snapshot.budget.max_outstanding_jobs),
                            "goal_budget_max_attempts": int(budget_snapshot.budget.max_attempts),
                            "goal_budget_max_runtime_seconds": int(budget_snapshot.budget.max_runtime_seconds),
                            "goal_budget_period_expires_at": budget_expires_at.isoformat(),
                        }
                    ),
                    session_id=owner_session_id,
                    created_by_session_id=owner_session_id,
                )
                binding = GovernedScheduleBinding(
                    binding_id=binding_id,
                    scheduled_job_id=scheduled_job_id,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    goal_id=req.goal_id,
                    goal_revision=req.expected_goal_revision,
                    capability_id="guardian.run_procedure.v2",
                    action_type="guardian.run_procedure.v2",
                    input_artifact_id=artifact.artifact_id,
                    input_digest=artifact.typed_input_digest,
                    action_digest=request_digest,
                    consent_kind="goal_budget",
                    read_consent_id=None,
                    consent_revision=1,
                    consent_digest=budget_snapshot.digest,
                    schedule_idempotency_key=req.idempotency_key,
                    schedule_request_digest=request_digest,
                    cadence_kind=cadence["kind"],
                    timezone=cadence["timezone"],
                    daily_hour=cadence.get("daily_hour"),
                    daily_minute=cadence.get("daily_minute"),
                    expires_at=expires,
                    state="active",
                )
                # Re-read the owner/session/goal and budget in this writer session
                # immediately before publishing either row.  The first admission
                # read prevents needless artifact work; this fence prevents a
                # concurrent goal/grant revision from becoming schedule authority.
                current_goal = (
                    await db.execute(
                        select(Goal)
                        .where(
                            Goal.id == req.goal_id,
                            Goal.owner_principal_id == owner_principal_id,
                            Goal.owner_session_id == owner_session_id,
                        )
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                final_budget = self._validate_schedule_goal_budget(
                    current_goal,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    expected_goal_revision=req.expected_goal_revision,
                    now=_now(),
                )
                if final_budget.digest != budget_snapshot.digest:
                    raise ProcedureV2Error(
                        "procedure_goal_budget_changed",
                        "The reviewed goal budget changed while the schedule was being prepared",
                        recovery_action="refresh_goal_budget",
                    )
                final_budget_expires_at = _utc(final_budget.budget.period_expires_at)
                if expires > final_budget_expires_at:
                    raise ProcedureV2Error(
                        "procedure_schedule_expiry_exceeds_goal_budget",
                        "The schedule must expire within the reviewed goal budget period",
                        recovery_action="choose_finite_expiry",
                    )
                db.add(job)
                db.add(binding)
                await db.flush()
        except ProcedureV2Error as exc:
            cleanup = await _cleanup_unpublished_procedure_artifact(
                owner,
                artifact_id=artifact.artifact_id,
                expected_revision=int(artifact.revision),
            )
            raise _publication_cleanup_error(
                exc,
                cleanup,
                recovery_action="use_new_idempotency_key",
            ) from exc
        except Exception as exc:
            # A flush/commit error leaves publication ambiguous.  Preserve the
            # exact schedule key and pending artifact for canonical replay.
            raise ProcedureV2Error(
                "procedure_publication_outcome_unknown",
                "The publication outcome could not be confirmed; retry the same request to reconcile it.",
                status_code=503,
                recovery_action="reconcile_publication",
                retryable=True,
            ) from exc
        next_slot = latest_due_slot(cadence, now_utc=now)
        return {
            "status": "scheduled",
            "scheduled_job_id": scheduled_job_id,
            "binding_id": binding_id,
            "revision": 1,
            "action_type": "guardian.run_procedure.v2",
            "routine_id": routine_id,
            "version": req.version,
            "template_id": descriptor.version.template_id,
            "goal_id": req.goal_id,
            "goal_revision": int(binding.goal_revision),
            "schedule_idempotency_key": binding.schedule_idempotency_key,
            "input_digest": artifact.typed_input_digest,
            "next_run": _utc(next_slot).isoformat().replace("+00:00", "Z") if next_slot else None,
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "state": "active",
            "pause_route": f"/api/governed-schedules/{binding_id}",
            "recovery_action": None,
            "audit_receipt_id": f"governed-schedule:{binding_id}",
        }, 201

    async def _prepare_schedule_artifact(self, owner: WorkBoardOwner, descriptor: V2InvocationDescriptor, req: ProcedureV2ScheduleRequest):
        payload = {
            "routine_id": descriptor.version.routine_id,
            "version": descriptor.version.version,
            "expected_routine_revision": descriptor.version.routine_revision,
            "goal_id": descriptor.goal_id,
            "expected_goal_revision": descriptor.goal_revision,
            "parameters": dict(descriptor.parameters),
            "invocation_uuid": f"schedule:{req.idempotency_key}",
        }
        async with db_engine.get_session() as db:
            try:
                return await prepare_input_artifact(
                    db,
                    owner,
                    WorkBoardInputArtifactCreate(
                        schema_version=1,
                        capability_id=ROUTINE_V2_CAPABILITY_VERSION,
                        goal_id=descriptor.goal_id,
                        goal_revision=descriptor.goal_revision,
                        input=payload,
                        idempotency_key=f"schedule:{req.idempotency_key}",
                    ),
                )
            except BoardError as exc:
                raise ProcedureV2Error(exc.code, str(exc), status_code=getattr(exc, "status_code", 409), recovery_action="retry_after_prerequisite") from exc


__all__ = [
    "ProcedureSourceTaskRef",
    "ProcedureV2CreateRequest",
    "ProcedureV2Error",
    "ProcedureV2InvokeRequest",
    "ProcedureV2PreviewRequest",
    "ProcedureV2ScheduleCadence",
    "ProcedureV2ScheduleRequest",
    "ProcedureV2Service",
    "GoalAdmissionBudgetSnapshot",
    "goal_admission_budget_snapshot",
    "V2InvocationDescriptor",
    "V2VersionDescriptor",
]
