"""Durable, owner-bound control operations for the Calendar integration.

Calendar setup and revocation are small control operations, but they still
cross a provider or credential boundary.  This module keeps their identity,
idempotency, effect intent, encrypted result artifact, and readback on the
existing :class:`DurableJobRepository` seam.  It deliberately does not add a
queue or a Calendar-specific ledger.
"""

from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Literal, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from src.db.models import GoogleServiceConnection, OperatorSession, WorkflowRunState
from src.integrations.google_calendar import digest
from src.vault import decrypt, encrypt
from src.workspace import canonical_workspace_root
from src.workflows import job_runtime
from src.workflows.job_runtime import (
    DurableJobAdmissionDenied,
    DurableJobError,
    DurableJobIdempotencyConflict,
    DurableJobIdentity,
    DurableJobSpec,
    durable_job_repository,
)


CONTROL_VERIFY = "calendar_connection_verify"
CONTROL_REVOKE_CONNECTION = "calendar_connection_revoke"
CONTROL_REVOKE_CONSENT = "calendar_consent_revoke"
CONTROL_JOB_KINDS = frozenset(
    {CONTROL_VERIFY, CONTROL_REVOKE_CONNECTION, CONTROL_REVOKE_CONSENT}
)
CONTROL_CAPABILITY_VERSION = "calendar-control-v1"
CONTROL_CAPABILITIES = {
    CONTROL_VERIFY: "calendar.connection.verify.v1",
    CONTROL_REVOKE_CONNECTION: "calendar.connection.revoke.v1",
    CONTROL_REVOKE_CONSENT: "calendar.consent.revoke.v1",
}
CONTROL_MAX_RUNTIME_SECONDS = 30
CONTROL_MAX_PLAINTEXT_BYTES = 64 * 1024
CONTROL_MAX_ENCRYPTED_BYTES = 96 * 1024

# The durable job row is the cross-process authority. This small in-process
# join lock closes the common ASGI race where two requests for a fresh control
# both pass the read-before-admission check and then one tries to queue/claim
# the row while the other is still committing it. It is scoped by the
# deterministic control root, so unrelated controls retain independent lanes
# and a restart still relies on the durable row.
_CONTROL_LOCKS: dict[str, asyncio.Lock] = {}
_CONTROL_LOCKS_GUARD = asyncio.Lock()


async def _control_lock(job_id: str) -> asyncio.Lock:
    async with _CONTROL_LOCKS_GUARD:
        lock = _CONTROL_LOCKS.get(job_id)
        if lock is None:
            # Keep this process-local index bounded when operators use many
            # unique idempotency keys. Locked entries are retained; an
            # unlocked entry may be evicted because the durable row remains
            # the replay authority.
            if len(_CONTROL_LOCKS) >= 256:
                for candidate_id, candidate_lock in tuple(_CONTROL_LOCKS.items()):
                    if not candidate_lock.locked():
                        _CONTROL_LOCKS.pop(candidate_id, None)
                        break
            lock = asyncio.Lock()
            _CONTROL_LOCKS[job_id] = lock
        return lock

_CONTROL_PATH = re.compile(
    r"^artifacts/work-board/calendar/setup/control-[0-9a-f]{32}\.enc$"
)
_CONTROL_ID = re.compile(r"^[A-Za-z0-9_.:@+,-]{1,256}$")


class CalendarControlError(RuntimeError):
    """Safe failure for a Calendar control operation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        recovery_action: str | None = None,
        uncertain: bool = False,
    ) -> None:
        self.code = str(code)[:128]
        self.status_code = int(status_code)
        self.recovery_action = recovery_action
        self.uncertain = bool(uncertain)
        super().__init__(str(message)[:500])


class CalendarControlReconciliationRequired(CalendarControlError):
    def __init__(self, message: str = "Calendar control requires reconciliation") -> None:
        super().__init__(
            "calendar_control_reconciliation_required",
            message,
            status_code=409,
            recovery_action="reconcile_existing_control",
            uncertain=True,
        )


@dataclass(frozen=True, slots=True)
class CalendarControlRequest:
    operation: str
    target_id: str
    owner_principal_id: str
    owner_session_id: str
    expected_revision: int
    idempotency_key: str
    request_digest: str
    reason_digest: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in CONTROL_JOB_KINDS:
            raise ValueError("unsupported Calendar control operation")
        for field_name in (
            "target_id",
            "owner_principal_id",
            "owner_session_id",
            "idempotency_key",
            "request_digest",
        ):
            value = str(getattr(self, field_name) or "")
            if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
                raise ValueError(f"invalid Calendar control {field_name}")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("Calendar control expected_revision must be positive")


@dataclass(frozen=True, slots=True)
class CalendarControlLease:
    job_id: str
    owner: str
    fencing_token: int
    revision: int


@dataclass(frozen=True, slots=True)
class CalendarControlExecution:
    payload: dict[str, Any]
    details: Mapping[str, Any] = field(default_factory=dict)


ControlExecutor = Callable[[CalendarControlLease], Awaitable[CalendarControlExecution]]


def control_job_id(request: CalendarControlRequest) -> str:
    seed = (
        request.owner_principal_id,
        request.owner_session_id,
        request.operation,
        request.target_id,
        request.idempotency_key,
    )
    return f"calendar-control:{uuid.uuid5(uuid.NAMESPACE_URL, 'seraph:' + '|'.join(seed)).hex}"


def control_scope(request: CalendarControlRequest) -> str:
    return f"calendar-control:{request.operation}:{digest(request.target_id)[:24]}"


def control_inputs(request: CalendarControlRequest) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "operation": request.operation,
        "target_id": request.target_id,
        "expected_revision": request.expected_revision,
        "request_digest": request.request_digest,
    }
    if request.reason_digest:
        value["reason_digest"] = request.reason_digest
    return value


def control_authority(request: CalendarControlRequest) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "capability_id": CONTROL_CAPABILITIES[request.operation],
        "principal": request.owner_principal_id,
        "owner_kind": "user",
        "service_id": None,
        "session_id": request.owner_session_id,
        "operator_session_id": request.owner_session_id,
        "operation": request.operation,
        "target_id": request.target_id,
        "expected_revision": request.expected_revision,
        "request_digest": request.request_digest,
        "finite_authority": True,
        "runtime_cap_seconds": CONTROL_MAX_RUNTIME_SECONDS,
        "budget_microusd": 0,
    }


def control_spec(request: CalendarControlRequest) -> tuple[DurableJobSpec, str, str]:
    inputs = control_inputs(request)
    authority = control_authority(request)
    input_digest = digest(inputs)
    authority_digest = digest(authority)
    spec = DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=control_job_id(request),
            owner_kind="user",
            owner_principal_id=request.owner_principal_id,
            job_kind=request.operation,
            capability_version=CONTROL_CAPABILITY_VERSION,
            idempotency_scope=control_scope(request),
            idempotency_key=request.idempotency_key,
        ),
        inputs=inputs,
        session_id=request.owner_session_id,
        conversation_id=request.owner_session_id,
        operator_session_id=request.owner_session_id,
        priority=60,
        resource_claims=("calendar-control",),
        declared_authority=authority,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=CONTROL_MAX_RUNTIME_SECONDS),
        max_attempts=1,
        max_outstanding_jobs=1,
        service_id=None,
        run_fingerprint=input_digest,
        budget_microusd=0,
        budget_digest=digest({"budget_microusd": 0}),
    )
    return spec, input_digest, authority_digest


def _control_artifact_path(job_id: str) -> str:
    return f"artifacts/work-board/calendar/setup/control-{digest(job_id)[:32]}.enc"


def _open_parent(relative_path: str, *, create: bool) -> int | None:
    candidate = PurePosixPath(relative_path)
    if not _CONTROL_PATH.fullmatch(candidate.as_posix()):
        return None
    try:
        root = canonical_workspace_root(settings.workspace_dir)
    except (OSError, TypeError, ValueError):
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | getattr(os, "O_CLOEXEC", 0)
    parent_fd: int | None = None
    try:
        parent_fd = os.open(root, flags)
        for component in candidate.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            return None
        result = parent_fd
        parent_fd = None
        return result
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def write_control_artifact(job_id: str, payload: Mapping[str, Any]) -> tuple[str, bytes, str]:
    """Encrypt and atomically write one bounded setup/control artifact."""

    plaintext = json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not plaintext or len(plaintext) > CONTROL_MAX_PLAINTEXT_BYTES:
        raise OSError("Calendar control result exceeds the bounded plaintext limit")
    encrypted = encrypt(plaintext.decode("utf-8")).encode("utf-8")
    if not encrypted or len(encrypted) > CONTROL_MAX_ENCRYPTED_BYTES:
        raise OSError("Calendar control result exceeds the bounded encrypted limit")
    relative_path = _control_artifact_path(job_id)
    parent_fd = _open_parent(relative_path, create=True)
    if parent_fd is None:
        raise OSError("Calendar control artifact directory is unavailable")
    final_name = PurePosixPath(relative_path).name
    temporary_name = f".{final_name}.{hashlib.sha256(encrypted).hexdigest()[:12]}.tmp"
    descriptor: int | None = None
    published = False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        view = memoryview(encrypted)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("Calendar control artifact write made no progress")
            view = view[count:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary_name, final_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        published = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass
    return relative_path, encrypted, hashlib.sha256(encrypted).hexdigest()


def read_control_artifact(job_id: str, *, expected_sha256: str | None = None) -> dict[str, Any] | None:
    """Read one owner-routed setup artifact through a bounded descriptor."""

    relative_path = _control_artifact_path(job_id)
    parent_fd = _open_parent(relative_path, create=False)
    if parent_fd is None:
        return None
    descriptor: int | None = None
    try:
        descriptor = os.open(
            PurePosixPath(relative_path).name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > CONTROL_MAX_ENCRYPTED_BYTES:
            return None
        encrypted = b""
        while len(encrypted) <= CONTROL_MAX_ENCRYPTED_BYTES:
            chunk = os.read(descriptor, CONTROL_MAX_ENCRYPTED_BYTES + 1 - len(encrypted))
            if not chunk:
                break
            encrypted += chunk
        after = os.fstat(descriptor)
        if (
            len(encrypted) > CONTROL_MAX_ENCRYPTED_BYTES
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            return None
        if expected_sha256 and hashlib.sha256(encrypted).hexdigest() != expected_sha256:
            return None
        decoded = json.loads(decrypt(encrypted.decode("utf-8")))
        return decoded if isinstance(decoded, dict) else None
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _job_projection(job: WorkflowRunState) -> dict[str, Any]:
    def load(value: str | None, fallback: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback

    return {
        "job_id": job.run_identity,
        "status": job.status,
        "owner": {"kind": job.owner_kind, "principal_id": job.owner_principal_id, "service_id": job.service_id},
        "session_id": job.session_id,
        "operator_session_id": job.operator_session_id,
        "job_kind": job.job_kind,
        "capability_version": job.capability_version,
        "revision": int(job.revision or 0),
        "failure_reason": job.failure_reason,
        "declared_authority": load(job.declared_authority_json, {}),
        "artifacts": load(job.artifact_receipts_json, []),
        "effects": load(job.effect_receipts_json, []),
    }


def _control_authority_is_canonical(
    authority: Mapping[str, Any],
    *,
    operation: str,
    owner_principal_id: str,
    owner_session_id: str,
    target_id: str,
    expected_revision: int | None = None,
) -> bool:
    """Require the persisted control authority before trusting its proof."""

    if (
        authority.get("schema_version") != 1
        or authority.get("capability_id") != CONTROL_CAPABILITIES.get(operation)
        or authority.get("principal") != owner_principal_id
        or authority.get("owner_kind") != "user"
        or authority.get("service_id") is not None
        or authority.get("session_id") != owner_session_id
        or authority.get("operator_session_id") != owner_session_id
        or authority.get("operation") != operation
        or authority.get("target_id") != target_id
        or authority.get("finite_authority") is not True
        or authority.get("runtime_cap_seconds") != CONTROL_MAX_RUNTIME_SECONDS
        or authority.get("budget_microusd") != 0
        or not isinstance(authority.get("request_digest"), str)
        or not authority.get("request_digest")
    ):
        return False
    return expected_revision is None or authority.get("expected_revision") == expected_revision


def _readback_is_verified(
    job: Mapping[str, Any],
    *,
    operation: str,
    path: str,
    content_sha256: str,
) -> bool:
    effects = job.get("effects") if isinstance(job.get("effects"), list) else []
    return any(
        isinstance(item, Mapping)
        and item.get("receipt_kind") == "readback"
        and item.get("effect_type") == operation
        and item.get("target_path") == path
        and item.get("status") == "succeeded"
        and item.get("content_sha256") == content_sha256
        and isinstance(item.get("readback_id"), str)
        and bool(item.get("readback_id"))
        and isinstance(item.get("verified_at"), str)
        and bool(item.get("verified_at"))
        and isinstance(item.get("details"), Mapping)
        and item["details"].get("verified") is True
        for item in effects
    )


def _artifact_sha(job: Mapping[str, Any], *, path: str) -> str | None:
    artifacts = job.get("artifacts") if isinstance(job.get("artifacts"), list) else []
    for item in artifacts:
        if (
            isinstance(item, Mapping)
            and item.get("artifact_type") == "calendar_control_result"
            and item.get("file_path") == path
            and item.get("exists") is True
        ):
            value = item.get("content_sha256")
            return str(value) if isinstance(value, str) and value else None
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _remaining_control_seconds(value: Any) -> float:
    """Return the authoritative remaining control deadline.

    The deadline is read back from the durable claim rather than recomputed
    from the local request.  A malformed or elapsed deadline is a
    reconciliation condition after intent has been recorded.
    """

    if not isinstance(value, str) or not value:
        raise CalendarControlReconciliationRequired(
            "The durable Calendar control deadline is unavailable"
        )
    try:
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CalendarControlReconciliationRequired(
            "The durable Calendar control deadline is malformed"
        ) from exc
    remaining = (_as_utc(deadline) - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise CalendarControlReconciliationRequired(
            "The Calendar control deadline expired before execution completed"
        )
    return remaining


def control_result(job: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a verified result only from the canonical artifact/readback pair."""

    if job.get("status") != "succeeded":
        return None
    job_id = str(job.get("job_id") or "")
    operation = str(job.get("job_kind") or "")
    if not job_id or operation not in CONTROL_JOB_KINDS:
        return None
    path = _control_artifact_path(job_id)
    expected_sha = _artifact_sha(job, path=path)
    if expected_sha is None or not _readback_is_verified(
        job,
        operation=operation,
        path=path,
        content_sha256=expected_sha,
    ):
        return None
    result = read_control_artifact(job_id, expected_sha256=expected_sha)
    if not isinstance(result, dict):
        return None
    if operation == CONTROL_VERIFY:
        if set(result) != {
            "connection",
            "calendars",
            "calendar_list_revision",
            "pages_read",
            "truncated",
            "provider_status",
        }:
            return None
        calendars = result.get("calendars")
        if (
            not isinstance(result.get("connection"), Mapping)
            or not isinstance(calendars, list)
            or len(calendars) > 50
            or not isinstance(result.get("calendar_list_revision"), str)
            or not isinstance(result.get("pages_read"), int)
            or result.get("pages_read") < 1
            or type(result.get("truncated")) is not bool
            or result.get("provider_status") != "verified"
        ):
            return None
        for item in calendars:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"calendar_id", "summary"}
                or not isinstance(item.get("calendar_id"), str)
                or not isinstance(item.get("summary"), str)
            ):
                return None
    elif operation == CONTROL_REVOKE_CONNECTION:
        if set(result) != {"connection"} or not isinstance(result.get("connection"), Mapping):
            return None
    elif operation == CONTROL_REVOKE_CONSENT:
        if set(result) != {"consent"} or not isinstance(result.get("consent"), Mapping):
            return None
    return result


async def find_verified_setup(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    connection_id: str,
    connection_revision: int | None = None,
) -> dict[str, Any] | None:
    """Find the bounded owner-private setup proof without provider contact.

    Durable inputs are intentionally redacted by the repository.  The setup
    connection identity is therefore matched against the safe authority JSON,
    which contains only the opaque connection ID and revision.
    """

    async with job_runtime.get_session() as db:
        operator_session = await db.get(OperatorSession, owner_session_id)
        now = datetime.now(timezone.utc)
        if (
            operator_session is None
            or operator_session.revoked_at is not None
            or _as_utc(operator_session.idle_expires_at) <= now
            or _as_utc(operator_session.absolute_expires_at) <= now
        ):
            return None
        connection = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.connection_id == connection_id,
                    GoogleServiceConnection.owner_principal_id == owner_principal_id,
                    GoogleServiceConnection.owner_session_id == owner_session_id,
                )
            )
        ).scalar_one_or_none()
        if (
            connection is None
            or connection.state != "active"
            or not connection.verified_setup_job_id
            or (
                connection_revision is not None
                and connection.revision != connection_revision
            )
        ):
            return None
        row = await db.execute(
            select(WorkflowRunState).where(
                WorkflowRunState.run_identity == connection.verified_setup_job_id,
                WorkflowRunState.owner_principal_id == owner_principal_id,
                WorkflowRunState.session_id == owner_session_id,
                WorkflowRunState.operator_session_id == owner_session_id,
                WorkflowRunState.owner_kind == "user",
                WorkflowRunState.service_id.is_(None),
                WorkflowRunState.job_kind == CONTROL_VERIFY,
            )
        )
        candidate_row = row.scalar_one_or_none()
        if candidate_row is None:
            return None
        candidate = _job_projection(candidate_row)
        authority = candidate.get("declared_authority")
        if not isinstance(authority, Mapping) or not _control_authority_is_canonical(
            authority,
            operation=CONTROL_VERIFY,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            target_id=connection_id,
            expected_revision=connection_revision,
        ):
            return None
        result = control_result(candidate)
    if result is None:
        return None
    projected_connection = result.get("connection")
    if not isinstance(projected_connection, Mapping):
        return None
    if (
        projected_connection.get("connection_id") != connection_id
        or projected_connection.get("state") != "active"
        or (
            connection_revision is not None
            and projected_connection.get("revision") != connection_revision
        )
    ):
        return None
    return result


def _validate_existing_job(job: Mapping[str, Any], request: CalendarControlRequest) -> None:
    owner = job.get("owner") if isinstance(job.get("owner"), Mapping) else {}
    authority = job.get("declared_authority")
    if (
        owner.get("kind") != "user"
        or owner.get("principal_id") != request.owner_principal_id
        or owner.get("service_id") is not None
        or job.get("session_id") != request.owner_session_id
        or job.get("operator_session_id") != request.owner_session_id
        or job.get("job_kind") != request.operation
        or job.get("capability_version") != CONTROL_CAPABILITY_VERSION
        or job.get("job_id") != control_job_id(request)
        or not isinstance(authority, Mapping)
        or not _control_authority_is_canonical(
            authority,
            operation=request.operation,
            owner_principal_id=request.owner_principal_id,
            owner_session_id=request.owner_session_id,
            target_id=request.target_id,
            expected_revision=request.expected_revision,
        )
    ):
        raise CalendarControlReconciliationRequired()


async def _prior_control(request: CalendarControlRequest, *, input_digest: str, authority_digest: str) -> dict[str, Any] | None:
    job = await durable_job_repository.get_by_idempotency_binding(
        owner_principal_id=request.owner_principal_id,
        goal_id=None,
        goal_revision=None,
        idempotency_scope=control_scope(request),
        idempotency_key=request.idempotency_key,
        expected_job_id=control_job_id(request),
        owner_kind="user",
        service_id=None,
        session_id=request.owner_session_id,
        operator_session_id=request.owner_session_id,
        job_kind=request.operation,
        capability_version=CONTROL_CAPABILITY_VERSION,
        input_digest=input_digest,
        authority_digest=authority_digest,
        run_fingerprint=input_digest,
    )
    if job is not None:
        _validate_existing_job(job, request)
    return job


async def _admit_control(spec: DurableJobSpec) -> Mapping[str, Any]:
    """Admit a control, retrying only the known placeholder-session race.

    ``DurableJobRepository`` creates a redacted ``Session`` placeholder while
    admitting a job.  Two first-time control requests for the same live
    operator session can both observe that placeholder as absent and one can
    lose the database uniqueness race before the repository reaches its
    idempotency check.  The failed transaction is rolled back by the session
    context; retrying the same canonical admission lets the winner's
    placeholder/job be observed without adding a Calendar queue or ledger.
    """

    for attempt in range(3):
        try:
            return await durable_job_repository.admit_job(spec)
        except IntegrityError as exc:
            if "sessions.id" not in str(exc) or attempt >= 2:
                raise
            await asyncio.sleep(0)
    raise AssertionError("unreachable control admission retry")


async def _wait_for_existing_control(
    request: CalendarControlRequest,
    *,
    input_digest: str,
    authority_digest: str,
) -> dict[str, Any]:
    """Join an exact concurrent admission without running a second callback."""

    # A concurrent caller may arrive just before the owner settles the root.
    # Join with a small bounded backoff; do not turn idempotency into a hot
    # polling loop that can starve the API.  The durable root remains visible
    # for a later explicit reconciliation if it does not settle here.
    # Cross-process callers have no shared asyncio lock. Give the durable
    # winner a bounded join window of at most sixteen reads with exponential
    # backoff; a pending/unknown root remains an explicit reconciliation
    # result after the window instead of turning into an unbounded poll loop.
    for attempt in range(16):
        try:
            existing = await _prior_control(
                request,
                input_digest=input_digest,
                authority_digest=authority_digest,
            )
        except DurableJobIdempotencyConflict as exc:
            # A changed request digest is still a hard conflict.  Only the
            # exact canonical request may join the owner's durable root.
            raise CalendarControlError(
                "calendar_control_idempotency_conflict",
                "The Calendar control key is bound to another request",
                status_code=409,
                recovery_action="use_new_idempotency_key",
            ) from exc
        if existing is not None:
            result = control_result(existing)
            if result is not None:
                return result
            status = str(existing.get("status") or "")
            if status in {"failed", "blocked", "unknown_external_effect", "cost_liability", "cancelled"}:
                raise CalendarControlReconciliationRequired(
                    "The same Calendar control key requires reconciliation"
                )
        await asyncio.sleep(min(0.025 * (2**attempt), 0.5))
    raise CalendarControlReconciliationRequired(
        "The same Calendar control key is still pending"
    )


async def _run_control(request: CalendarControlRequest, execute: ControlExecutor) -> dict[str, Any]:
    """Admit, claim, execute, and durably settle one Calendar control.

    The callback runs only after the effect intent is recorded.  Any exception
    after that point leaves the job in an explicit unknown state, so a retry
    with the same key cannot contact the provider or repeat local cleanup.
    """

    spec, input_digest, authority_digest = control_spec(request)
    try:
        prior = await _prior_control(request, input_digest=input_digest, authority_digest=authority_digest)
    except DurableJobIdempotencyConflict as exc:
        raise CalendarControlError(
            "calendar_control_idempotency_conflict",
            "The Calendar control key is bound to another request",
            status_code=409,
            recovery_action="use_new_idempotency_key",
        ) from exc
    if prior is not None:
        result = control_result(prior)
        if result is not None:
            return result
        raise CalendarControlReconciliationRequired(
            "The same Calendar control key is already pending or requires reconciliation"
        )
    try:
        admission = await _admit_control(spec)
    except DurableJobAdmissionDenied as exc:
        raise CalendarControlError(
            "calendar_control_admission_blocked",
            "Calendar control admission is currently blocked",
            status_code=409,
            recovery_action="wait_or_reconcile_existing_work",
        ) from exc
    except DurableJobIdempotencyConflict as exc:
        try:
            return await _wait_for_existing_control(
                request,
                input_digest=input_digest,
                authority_digest=authority_digest,
            )
        except CalendarControlError:
            raise
        except DurableJobError as replay_exc:
            raise CalendarControlReconciliationRequired() from replay_exc
    except DurableJobError as exc:
        raise CalendarControlReconciliationRequired() from exc

    if admission.get("receipt", {}).get("status") == "deduped":
        try:
            replay_job = await _prior_control(request, input_digest=input_digest, authority_digest=authority_digest)
        except DurableJobIdempotencyConflict as exc:
            raise CalendarControlError(
                "calendar_control_idempotency_conflict",
                "The Calendar control key is bound to another request",
                status_code=409,
                recovery_action="use_new_idempotency_key",
            ) from exc
        if replay_job is None:
            return await _wait_for_existing_control(
                request,
                input_digest=input_digest,
                authority_digest=authority_digest,
            )
        result = control_result(replay_job)
        if result is not None:
            return result
        return await _wait_for_existing_control(
            request,
            input_digest=input_digest,
            authority_digest=authority_digest,
        )
    try:
        queued = await durable_job_repository.queue_job(
            spec.identity.job_id,
            expected_state="accepted",
            expected_revision=int(admission.get("revision") or 0),
        )
        lease_owner = f"calendar-control:{request.owner_principal_id}:{spec.identity.job_id}"
        claimed = await durable_job_repository.claim_job(
            spec.identity.job_id,
            owner=lease_owner,
            lease_seconds=CONTROL_MAX_RUNTIME_SECONDS,
            expected_state="queued",
            expected_revision=int(queued.get("revision") or 0),
        )
    except DurableJobError as exc:
        raise CalendarControlReconciliationRequired() from exc
    lease = claimed.get("lease") if isinstance(claimed.get("lease"), Mapping) else {}
    fencing_token = lease.get("fencing_token")
    if type(fencing_token) is not int or fencing_token <= 0:
        raise CalendarControlReconciliationRequired()
    current_revision = int(claimed.get("revision") or 0)
    effect_id = f"calendar-control-effect:{spec.identity.job_id}"
    # Bind intent and readback to the same canonical result path.  The path is
    # deterministic before execution, so a successful readback can settle the
    # original intent without creating a second effect target.
    effect_target_path = _control_artifact_path(spec.identity.job_id)

    async def _settle_unknown_after_cancellation() -> None:
        """Record unresolved effect state without contacting the provider."""

        current = await durable_job_repository.get_job(spec.identity.job_id)
        if not isinstance(current, Mapping) or str(current.get("status") or "") != "running":
            return
        current_revision = int(current.get("revision") or 0)
        unknown = await durable_job_repository.record_effect(
            spec.identity.job_id,
            effect_type=request.operation,
            effect_id=effect_id,
            target_path=effect_target_path,
            target_digest=request.request_digest,
            adapter_idempotency_key=request.idempotency_key,
            status="unknown",
            details={
                "operation": request.operation,
                "recovery_action": "reconcile_existing_control",
                "cancellation_after_intent": True,
            },
            owner=lease_owner,
            fencing_token=fencing_token,
            expected_revision=current_revision,
        )
        await durable_job_repository.transition_job(
            spec.identity.job_id,
            "unknown_external_effect",
            owner=lease_owner,
            fencing_token=fencing_token,
            expected_revision=int(unknown.get("revision") or 0),
            reason="cancelled_after_intent",
        )

    try:
        intent = await durable_job_repository.record_effect(
            spec.identity.job_id,
            effect_type=request.operation,
            effect_id=effect_id,
            target_path=effect_target_path,
            target_digest=request.request_digest,
            adapter_idempotency_key=request.idempotency_key,
            status="intent",
            details={"operation": request.operation, "target_digest": digest(request.target_id)},
            owner=lease_owner,
            fencing_token=fencing_token,
            expected_revision=current_revision,
        )
        lease = CalendarControlLease(
            job_id=spec.identity.job_id,
            owner=lease_owner,
            fencing_token=fencing_token,
            revision=int(intent.get("revision") or 0),
        )
        # Provider callbacks, credential decryption performed by the
        # callback, and publication of the bounded result artifact share the
        # authoritative durable deadline.  ``to_thread`` keeps a blocking
        # fsync from freezing the API loop; timeout still leaves the durable
        # intent unknown, so a retry cannot contact the provider again.
        remaining = _remaining_control_seconds(claimed.get("deadline_at"))
        try:
            async with asyncio.timeout(remaining):
                execution = await execute(lease)
                if not isinstance(execution, CalendarControlExecution) or not isinstance(execution.payload, dict):
                    raise RuntimeError("Calendar control executor returned an invalid result")
                path, encrypted, encrypted_sha = await asyncio.to_thread(
                    write_control_artifact,
                    spec.identity.job_id,
                    execution.payload,
                )
                artifact = await durable_job_repository.record_artifact(
                    spec.identity.job_id,
                    file_path=path,
                    artifact_type="calendar_control_result",
                    content=encrypted,
                    owner=lease.owner,
                    fencing_token=lease.fencing_token,
                    expected_revision=lease.revision,
                )
                readback = await durable_job_repository.record_readback(
                    spec.identity.job_id,
                    target_path=path,
                    status="succeeded",
                    effect_id=effect_id,
                    effect_type=request.operation,
                    # The readback observes the same immutable control intent.
                    # The ciphertext hash belongs in ``content_sha256``;
                    # changing the target digest here would turn a successful
                    # proof into a second, conflicting effect target.
                    target_digest=request.request_digest,
                    content_sha256=encrypted_sha,
                    readback_id=f"calendar-control-readback:{spec.identity.job_id}",
                    verified_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    details={"verified": True, "artifact_id": artifact.get("receipt", {}).get("artifact_id"), "memory_status": "no_learning"},
                    owner=lease.owner,
                    fencing_token=lease.fencing_token,
                    expected_revision=int(artifact.get("revision") or 0),
                )
                await durable_job_repository.transition_job(
                    spec.identity.job_id,
                    "succeeded",
                    owner=lease.owner,
                    fencing_token=lease.fencing_token,
                    expected_revision=int(readback.get("revision") or 0),
                    result_summary="Calendar control completed",
                )
        except asyncio.CancelledError:
            # ``CancelledError`` inherits directly from ``BaseException`` and
            # would otherwise leave the claimed durable root in ``running``.
            # Reconcile the exact existing job within its authoritative
            # deadline, then preserve the original cancellation. The helper
            # only writes the existing effect identity and never calls the
            # provider or creates a second control.
            try:
                remaining = _remaining_control_seconds(claimed.get("deadline_at"))
                async with asyncio.timeout(remaining):
                    await _settle_unknown_after_cancellation()
            except BaseException:
                # Best effort is bounded by the existing durable deadline;
                # if a second cancellation, lease race, or database failure
                # interrupts settlement, keep the original cancellation
                # signal and leave the durable row visible for reconciliation.
                pass
            raise
        except TimeoutError as exc:
            raise CalendarControlReconciliationRequired(
                "The Calendar control exceeded its durable execution deadline"
            ) from exc
        return execution.payload
    except CalendarControlError as exc:
        # A typed callback failure still happens after the durable intent was
        # recorded.  Preserve that boundary in the job row.  Known local
        # pre-effect failures can be marked failed; failures that may have
        # crossed the provider or vault boundary remain unknown and require
        # the same-key reconciliation path.
        try:
            current = await durable_job_repository.get_job(spec.identity.job_id)
            current_status = str(current.get("status") or "") if isinstance(current, Mapping) else ""
            current_revision = int(current.get("revision") or 0) if isinstance(current, Mapping) else 0
            if current_status == "running":
                state = "unknown" if exc.uncertain else "failed"
                settled = await durable_job_repository.record_effect(
                    spec.identity.job_id,
                    effect_type=request.operation,
                    effect_id=effect_id,
                    target_path=effect_target_path,
                    target_digest=request.request_digest,
                    adapter_idempotency_key=request.idempotency_key,
                    status=state,
                    details={
                        "operation": request.operation,
                        "recovery_action": exc.recovery_action,
                        "error_code": exc.code,
                    },
                    owner=lease_owner,
                    fencing_token=fencing_token,
                    expected_revision=current_revision,
                )
                await durable_job_repository.transition_job(
                    spec.identity.job_id,
                    "unknown_external_effect" if exc.uncertain else "failed",
                    owner=lease_owner,
                    fencing_token=fencing_token,
                    expected_revision=int(settled.get("revision") or 0),
                    reason=exc.code,
                )
        except Exception:
            # Keep the typed public error.  The durable row remains visible for
            # operator reconciliation if the settlement itself raced a lease
            # or database failure.
            pass
        raise
    except Exception as exc:
        try:
            current = await durable_job_repository.get_job(spec.identity.job_id)
            current_status = str(current.get("status") or "") if isinstance(current, Mapping) else ""
            current_revision = int(current.get("revision") or 0) if isinstance(current, Mapping) else current_revision
            if current_status == "running":
                unknown = await durable_job_repository.record_effect(
                    spec.identity.job_id,
                    effect_type=request.operation,
                    effect_id=effect_id,
                    target_path=effect_target_path,
                    target_digest=request.request_digest,
                    adapter_idempotency_key=request.idempotency_key,
                    status="unknown",
                    details={"operation": request.operation, "recovery_action": "reconcile_existing_control"},
                    owner=lease_owner,
                    fencing_token=fencing_token,
                    expected_revision=current_revision,
                )
                await durable_job_repository.transition_job(
                    spec.identity.job_id,
                    "unknown_external_effect",
                    owner=lease_owner,
                    fencing_token=fencing_token,
                    expected_revision=int(unknown.get("revision") or 0),
                    reason="calendar_control_reconciliation_required",
                )
        except Exception:
            # The original exception remains the useful bounded signal; the
            # durable row is still visible for operator reconciliation.
            pass
        raise CalendarControlReconciliationRequired() from exc


async def run_control(request: CalendarControlRequest, execute: ControlExecutor) -> dict[str, Any]:
    """Run one durable Calendar control with a per-root local join fence.

    The durable repository remains authoritative across processes and restarts;
    the local lock only prevents same-process callers from racing the
    accepted->queued->claimed transition. A waiting caller re-enters the
    normal exact replay path after the winner releases the lock, so it never
    invokes the provider callback a second time.
    """

    lock = await _control_lock(control_job_id(request))
    async with lock:
        return await _run_control(request, execute)


__all__ = [
    "CONTROL_CAPABILITIES",
    "CONTROL_VERIFY",
    "CONTROL_REVOKE_CONNECTION",
    "CONTROL_REVOKE_CONSENT",
    "CalendarControlError",
    "CalendarControlExecution",
    "CalendarControlLease",
    "CalendarControlReconciliationRequired",
    "CalendarControlRequest",
    "control_job_id",
    "control_result",
    "find_verified_setup",
    "read_control_artifact",
    "run_control",
    "write_control_artifact",
]
